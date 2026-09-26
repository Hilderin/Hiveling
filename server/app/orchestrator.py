"""Orchestrator: sequential dispatch of tasks to the first free worker.

The engine is reused by the CLI (``server/run.py``) and by the dashboard
(``server/dashboard.py``). When a ``RunStore`` is provided, it records the run
and its per-task state so the web UI can display it live.
"""

from __future__ import annotations

import glob
import io
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from rich.console import Console
from rich.table import Table

from .history import History
from .plan import Plan, PlanError, Task, WorkerEndpoint
from .runs import RunStore, build_task_states, new_run_id
from .worker_client import WorkerBusy, WorkerClient, WorkerError

TERMINAL_STATUSES = {"succeeded", "failed", "canceled"}


@dataclass
class TaskResult:
    task_id: str
    status: str
    worker: str | None = None
    job_id: str | None = None
    duration_s: float | None = None
    files: list[str] = field(default_factory=list)
    history_rel: str | None = None
    error: str | None = None


class Orchestrator:
    def __init__(
        self,
        plan: Plan,
        *,
        history_dir: Path,
        poll_interval: float = 2.0,
        keep_going: bool = False,
        worker_wait_timeout: float = 1800.0,
        console: Console | None = None,
        dry_run: bool = False,
        only: list[str] | None = None,
        run_store: RunStore | None = None,
        run_id: str | None = None,
        cancel_event: threading.Event | None = None,
        inputs_run_id: str | None = None,
        assume_deps_ok: bool = False,
        quiet: bool = False,
        workers: list[WorkerEndpoint] | None = None,
        workers_provider: Callable[[], list[WorkerEndpoint]] | None = None,
        task_cancel_provider: Callable[[str], bool] | None = None,
    ) -> None:
        self.plan = plan
        self.history = History(history_dir)
        self.poll_interval = poll_interval
        self.keep_going = keep_going
        self.worker_wait_timeout = worker_wait_timeout
        self.console = console or Console()
        self.dry_run = dry_run
        self.only = set(only) if only else None
        self.run_store = run_store
        self.run_id = run_id or new_run_id()
        self.cancel_event = cancel_event
        self.inputs_run_id = inputs_run_id
        self.assume_deps_ok = assume_deps_ok
        self.quiet = quiet
        self.workers = list(workers or [])
        self.workers_provider = workers_provider
        self.task_cancel_provider = task_cancel_provider
        self._clients: dict[str, WorkerClient] = {}
        self._rr = 0  # round-robin across workers

    # ------------------------------------------------------------------ public
    def _log(self, message: str) -> None:
        if not self.quiet:
            self.console.print(message)

    def _is_canceled(self) -> bool:
        return self.cancel_event is not None and self.cancel_event.is_set()

    def _task_is_canceled(self, task_id: str) -> bool:
        """True when this specific task was canceled via cancel_task."""
        return self.task_cancel_provider is not None and self.task_cancel_provider(task_id)

    def _store_task(self, task_id: str, **fields) -> None:
        if self.run_store is not None:
            self.run_store.update_task(self.run_id, task_id, **fields)

    def run(self) -> int:
        self._log(
            f"[bold]Plan[/] {self.plan.path.name}  [dim](run {self.run_id})[/]"
        )

        if self.dry_run:
            self._print_plan()
            return 0

        if self.run_store is not None:
            self.run_store.create(
                self.run_id,
                plan_path=str(self.plan.path),
                plan_name=self.plan.path.name,
                only=sorted(self.only) if self.only else None,
                keep_going=self.keep_going,
                tasks=build_task_states(self.plan, sorted(self.only) if self.only else None),
                inputs_run_id=self.inputs_run_id,
            )

        if not self._current_workers():
            self._log("[yellow]no workers configured (workers.yaml)[/]")

        results: dict[str, str] = {}
        canceled = False
        try:
            self._print_workers()
            tasks = self.plan.tasks
            for index, task in enumerate(tasks):
                if self.only is not None and task.id not in self.only:
                    continue
                if self._is_canceled():
                    canceled = True
                    for remaining in tasks[index:]:
                        if self.only is not None and remaining.id not in self.only:
                            continue
                        results[remaining.id] = "canceled"
                        self._store_task(remaining.id, status="canceled", error="canceled by user")
                    break

                if self._task_is_canceled(task.id):
                    self._log(f"[yellow]canceled[/] {task.id} [dim](canceled while pending)[/]")
                    results[task.id] = "canceled"
                    self._store_task(task.id, status="canceled", error="canceled by user")
                    continue

                failed_deps: list[str] = []
                if not self.assume_deps_ok:
                    dependencies = list(dict.fromkeys(list(task.depends_on) + list(task.inputs_from)))
                    failed_deps = [
                        dep for dep in dependencies if results.get(dep) != "succeeded"
                    ]
                if failed_deps:
                    self._log(
                        f"[yellow]skip[/] {task.id} [dim](unsatisfied dependencies: "
                        f"{', '.join(failed_deps)})[/]"
                    )
                    results[task.id] = "skipped"
                    self._store_task(
                        task.id,
                        status="skipped",
                        error=f"unsatisfied dependencies: {', '.join(failed_deps)}",
                    )
                    continue

                result = self._run_task(task)
                results[task.id] = result.status
                if result.status == "canceled" and self._is_canceled():
                    canceled = True
                stop_run = result.status != "succeeded" and not self.keep_going
                if result.status == "canceled" and not self._is_canceled():
                    # A single task was canceled with cancel_task: the run continues.
                    stop_run = False
                if stop_run:
                    if result.status == "canceled":
                        self._log("[yellow]run canceled[/]")
                    else:
                        self._log("[red]stopping after failure[/] (use --keep-going to continue)")
                    break
        finally:
            self._close_clients()

        if canceled:
            self._cancel_pending_tasks()
        status = self._final_status(results, canceled)
        if self.run_store is not None:
            self.run_store.update(self.run_id, status=status, finished_at=time.time())

        return self._summary(results)

    def _cancel_pending_tasks(self) -> None:
        if self.run_store is None:
            return
        run = self.run_store.read(self.run_id)
        if not run:
            return
        for task in run.get("tasks", []):
            if task.get("status") == "pending":
                self._store_task(task["id"], status="canceled", error="canceled by user")

    def _final_status(self, results: dict[str, str], canceled: bool) -> str:
        if canceled:
            return "canceled"
        if any(status in ("failed", "canceled") for status in results.values()):
            return "failed"
        return "succeeded"

    # ----------------------------------------------------------------- workers
    def _current_workers(self) -> list[WorkerEndpoint]:
        """Workers from the provider (live file) or the static list."""
        if self.workers_provider is not None:
            try:
                return self.workers_provider()
            except Exception:
                return self.workers
        return self.workers

    def _sync_clients(self) -> list[WorkerClient]:
        """Refresh the client pool from the current worker list.

        Called before each dispatch and on every wait iteration, so a worker
        added to workers.yaml while a run is in flight is picked up.
        """
        workers = {w.url: w for w in self._current_workers()}
        for url in list(self._clients):
            if url not in workers:
                try:
                    self._clients[url].close()
                except Exception:
                    pass
                del self._clients[url]
        for url, endpoint in workers.items():
            if url not in self._clients:
                self._clients[url] = WorkerClient(endpoint)
        return list(self._clients.values())

    def _close_clients(self) -> None:
        for client in self._clients.values():
            try:
                client.close()
            except Exception:
                pass
        self._clients.clear()

    def _print_workers(self) -> None:
        if self.quiet:
            return
        for client in self._sync_clients():
            try:
                health = client.health()
                state = "[red]busy[/]" if health.get("busy") else "[green]free[/]"
                self.console.print(
                    f"  worker [bold]{client.endpoint.name}[/] {client.base_url} -> {state}"
                )
            except WorkerError as exc:
                self.console.print(
                    f"  worker [bold]{client.endpoint.name}[/] {client.base_url} -> "
                    f"[red]unreachable[/] [dim]{exc}[/]"
                )

    def _free_clients(self, clients: list[WorkerClient]) -> list[WorkerClient]:
        """Return reachable workers that are not busy."""
        free: list[WorkerClient] = []
        for client in clients:
            try:
                health = client.health()
            except WorkerError:
                continue
            if not health.get("busy"):
                free.append(client)
        return free

    def _wait_for_worker(self, task: Task) -> WorkerClient | None:
        deadline = time.time() + self.worker_wait_timeout
        announced = False
        while time.time() < deadline:
            if self._is_canceled() or self._task_is_canceled(task.id):
                return None
            free = self._free_clients(self._sync_clients())
            if free:
                client = free[self._rr % len(free)]
                self._rr += 1
                return client
            if not announced:
                self._log("[yellow]no free worker available, waiting...[/]")
                announced = True
            time.sleep(self.poll_interval)
        return None

    # ------------------------------------------------------------------ tasks
    def _run_task(self, task: Task) -> TaskResult:
        while True:
            client = self._wait_for_worker(task)
            if client is None:
                canceled = self._is_canceled() or self._task_is_canceled(task.id)
                return self._record_result(
                    TaskResult(task.id, "canceled" if canceled else "failed",
                               error="canceled by user" if canceled else "no worker available")
                )
            try:
                result = self._execute_on(client, task)
            except WorkerBusy:
                self._log(f"[yellow]{client.endpoint.name} busy[/], looking for another worker")
                continue
            except PlanError as exc:
                self._log(f"[red]plan error:[/] {exc}")
                result = TaskResult(task.id, "failed", worker=client.endpoint.name, error=str(exc))
            except WorkerError as exc:
                self._log(f"[red]worker error:[/] {exc}")
                result = TaskResult(task.id, "failed", worker=client.endpoint.name, error=str(exc))
            return self._record_result(result)

    def _record_result(self, result: TaskResult) -> TaskResult:
        self._store_task(
            result.task_id,
            status=result.status,
            worker=result.worker,
            job_id=result.job_id,
            duration_s=result.duration_s,
            changed_files=result.files,
            history_rel=result.history_rel,
            error=result.error,
        )
        return result

    def _execute_on(self, client: WorkerClient, task: Task) -> TaskResult:
        job_id = f"{task.id}-{uuid.uuid4().hex[:8]}"

        request = {
            "job_id": job_id,
            "prompt": task.prompt,
            "model": task.model,
            "agent": task.agent,
            "auto": task.auto,
            "timeout_s": task.timeout_s,
            "variant": task.variant,
            "title": task.title,
            "env": task.env,
        }
        # 'files' sent to OpenCode is intentionally empty: input files are
        # already extracted into the working directory.
        spec = {k: v for k, v in request.items() if v not in (None, {})}
        spec["files"] = []

        inputs = self._resolve_inputs(task)

        client.create_job(spec)  # raises WorkerBusy when busy
        self._store_task(task.id, status="running", worker=client.endpoint.name, job_id=job_id)
        self._log(
            f"[bold cyan]{task.id}[/] -> [bold]{client.endpoint.name}[/] "
            f"[dim]({job_id}, model {task.model or 'default'})[/]"
        )

        if inputs:
            self._log(f"  uploading {len(inputs)} input file(s)")
            client.upload_files(job_id, self._zip_inputs(inputs))

        client.start_job(job_id)
        status = self._poll(client, job_id, task)

        logs: dict | None = None
        files_zip: bytes | None = None
        try:
            logs = client.logs(job_id)
        except WorkerError:
            pass
        if task.download != "none":
            try:
                files_zip = client.download_files(job_id, task.download)
            except WorkerError as exc:
                self._log(f"  [yellow]could not download files:[/] {exc}")

        directory = self.history.save(
            task.id,
            self.run_id,
            request=request,
            status=status,
            logs=logs,
            files_zip=files_zip,
            worker={"name": client.endpoint.name, "url": client.base_url, "job_id": job_id},
        )

        self._print_result(status, directory)
        return TaskResult(
            task_id=task.id,
            status=status.get("status", "failed"),
            worker=client.endpoint.name,
            job_id=job_id,
            duration_s=status.get("duration_s"),
            files=sorted(set(status.get("added", [])) | set(status.get("modified", []))),
            history_rel=directory.relative_to(self.history.root).as_posix(),
            error=status.get("error"),
        )

    def _poll(self, client: WorkerClient, job_id: str, task: Task) -> dict:
        deadline = None
        if task.timeout_s:
            deadline = time.time() + float(task.timeout_s) + 60.0
        last_status = None
        transient = 0
        started = time.time()

        while True:
            if self._is_canceled() or self._task_is_canceled(task.id):
                try:
                    client.cancel(job_id)
                except WorkerError:
                    pass
                return {"job_id": job_id, "status": "canceled", "error": "canceled by user"}

            try:
                status = client.get_job(job_id)
                transient = 0
            except WorkerError as exc:
                transient += 1
                if transient > 10:
                    return {
                        "job_id": job_id,
                        "status": "failed",
                        "error": f"worker unreachable: {exc}",
                    }
                time.sleep(self.poll_interval)
                continue

            current = status.get("status")
            if current != last_status:
                self._log(f"  [dim]status: {current} ({time.time() - started:.0f}s)[/]")
                last_status = current

            if current in TERMINAL_STATUSES:
                return status

            if deadline and time.time() > deadline:
                try:
                    client.cancel(job_id)
                except WorkerError:
                    pass
                return {**status, "status": "failed", "error": "timeout on the server side"}

            time.sleep(self.poll_interval)

    # ------------------------------------------------------------------ files
    def _resolve_inputs(self, task: Task) -> list[tuple[str, Path]]:
        """Resolve ``files`` patterns and ``inputs_from`` outputs.

        Returns a list of ``(name_in_zip, source_path)``.
        """
        pairs: dict[str, Path] = {}  # arcname -> source (dedup, last one wins)

        for spec in task.files:
            matches = glob.glob(str(self.plan.base_dir / spec), recursive=True)
            if not matches:
                raise PlanError(f"task '{task.id}': file not found: {spec}")
            for match in matches:
                path = Path(match)
                if path.is_dir():
                    for child in path.rglob("*"):
                        if child.is_file():
                            pairs[self._arcname(child)] = child
                elif path.is_file():
                    pairs[self._arcname(path)] = path

        source_run = self.inputs_run_id or self.run_id
        for source in task.inputs_from:
            source_dir = self.history.run_dir(source, source_run) / "files"
            if not source_dir.is_dir():
                raise PlanError(
                    f"task '{task.id}': files from '{source}' not found "
                    f"(did that task run and download its files?)"
                )
            for child in sorted(source_dir.rglob("*")):
                if child.is_file() and child.suffix != ".zip":
                    pairs[child.relative_to(source_dir).as_posix()] = child

        return list(pairs.items())

    def _arcname(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.plan.base_dir).as_posix()
        except ValueError:
            return path.name

    @staticmethod
    def _zip_inputs(pairs: list[tuple[str, Path]]) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for arc, path in pairs:
                archive.write(path, arc)
        return buffer.getvalue()

    # --------------------------------------------------------------- display
    def _display(self, path: Path) -> str:
        try:
            return path.relative_to(Path.cwd()).as_posix()
        except ValueError:
            return str(path)

    def _print_result(self, status: dict, directory: Path) -> None:
        if self.quiet:
            return
        state = status.get("status")
        color = {"succeeded": "green", "failed": "red", "canceled": "yellow"}.get(state, "white")
        files = sorted(set(status.get("added", [])) | set(status.get("modified", [])))
        tokens = status.get("tokens") or {}
        cost = status.get("cost")
        self.console.print(
            f"  [{color}]{state}[/] in {status.get('duration_s')}s"
            f" [dim]| tokens {tokens.get('total', '?')} | cost {cost if cost is not None else '?'}[/]"
        )
        if files:
            self.console.print(f"  changed files: {', '.join(files)}")
        if status.get("error"):
            self.console.print(f"  [red]error:[/] {status['error']}")
        self.console.print(f"  [dim]history: {self._display(directory)}[/]")

    def _print_plan(self) -> None:
        table = Table(title="Plan (dry-run)")
        table.add_column("order", justify="right")
        table.add_column("task")
        table.add_column("depends_on")
        table.add_column("inputs_from")
        table.add_column("model")
        table.add_column("files")
        for index, task in enumerate(self.plan.tasks, 1):
            table.add_row(
                str(index),
                task.id,
                ", ".join(task.depends_on),
                ", ".join(task.inputs_from),
                task.model or "(default)",
                ", ".join(task.files) or "-",
            )
        self.console.print(table)

    def _summary(self, results: dict[str, str]) -> int:
        if not self.quiet:
            table = Table(title="Summary")
            table.add_column("task")
            table.add_column("status")
            for task_id, status in results.items():
                color = {
                    "succeeded": "green",
                    "failed": "red",
                    "canceled": "yellow",
                    "skipped": "yellow",
                }.get(status, "white")
                table.add_row(task_id, f"[{color}]{status}[/]")
            self.console.print(table)

            failures = [
                task_id for task_id, status in results.items() if status in ("failed", "canceled")
            ]
            if failures:
                self.console.print(f"[red]{len(failures)} task(s) failed[/]")

        failures = [
            task_id for task_id, status in results.items() if status in ("failed", "canceled")
        ]
        return 1 if failures else 0

"""Orchestrator: parallel DAG dispatch of tasks to the available workers.

Tasks whose dependencies have succeeded are dispatched concurrently, one per
free worker. A failed task stops new dispatches (fail fast) but never kills the
tasks already in flight: the run stays ``running`` until they finish, so
``wait_for_run`` only reports a terminal run once every in-flight task is done.

The engine is reused by the CLI (``server/run.py``) and by the dashboard
(``server/dashboard.py``). When a ``RunStore`` is provided, it records the run
and its per-task state so the web UI can display it live.
"""

from __future__ import annotations

import glob
import io
import json
import logging
import queue
import shutil
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from rich.console import Console
from rich.table import Table

from .gate import (
    GATE_AGENT_ID,
    build_gate_prompt,
    is_valid_verdict,
    with_gate_feedback,
)
from .history import History
from .plan import Plan, PlanError, Task, WorkerEndpoint, load_plan
from .runs import RunStore, build_task_states, new_run_id
from .worker_client import WorkerBusy, WorkerClient, WorkerError

logger = logging.getLogger("hiveling.server.orchestrator")

TERMINAL_STATUSES = {"succeeded", "failed", "canceled"}


@dataclass
class TaskResult:
    task_id: str
    status: str
    worker: str | None = None
    worker_url: str | None = None
    job_id: str | None = None
    duration_s: float | None = None
    files: list[str] = field(default_factory=list)
    history_rel: str | None = None
    error: str | None = None
    commits: list = field(default_factory=list)
    artifacts: list = field(default_factory=list)
    merge: dict = field(default_factory=dict)
    # Gate tasks only: the raw answer and whether it contains a VALID line.
    text: str = ""
    gate_valid: bool | None = None


class Orchestrator:
    def __init__(
        self,
        plan: Plan,
        *,
        history_dir: Path,
        poll_interval: float = 2.0,
        worker_wait_timeout: float = 1800.0,
        console: Console | None = None,
        dry_run: bool = False,
        only: list[str] | None = None,
        max_parallel: int | None = None,
        run_store: RunStore | None = None,
        run_id: str | None = None,
        cancel_event: threading.Event | None = None,
        inputs_run_id: str | None = None,
        assume_deps_ok: bool = False,
        quiet: bool = False,
        workers: list[WorkerEndpoint] | None = None,
        workers_provider: Callable[[], list[WorkerEndpoint]] | None = None,
        task_cancel_provider: Callable[[str], bool] | None = None,
        on_state_change: Callable[[], None] | None = None,
        plan_path: Path | None = None,
        plan_base_dir: Path | None = None,
        plan_snapshot: Path | None = None,
        owner: dict | None = None,
        resume: bool = False,
        initial_results: dict[str, str] | None = None,
        resume_jobs: dict[str, dict] | None = None,
    ) -> None:
        self.plan = plan
        self.history = History(history_dir)
        self.poll_interval = poll_interval
        self.worker_wait_timeout = worker_wait_timeout
        self.console = console or Console()
        self.dry_run = dry_run
        self.only = set(only) if only else None
        # 0 (or unset) means "as many tasks as there are free workers"; an
        # explicit value also caps the number of concurrent tasks.
        if max_parallel is None:
            max_parallel = plan.defaults.get("max_parallel")
        self.max_parallel = max(0, int(max_parallel or 0))
        self.run_store = run_store
        self.run_id = run_id or new_run_id()
        self.cancel_event = cancel_event
        self.inputs_run_id = inputs_run_id
        self.assume_deps_ok = assume_deps_ok
        self.quiet = quiet
        self.workers = list(workers or [])
        self.workers_provider = workers_provider
        self.task_cancel_provider = task_cancel_provider
        self.on_state_change = on_state_change
        # Live plan editing: the run reads this file between tasks.
        self.plan_path = Path(plan_path) if plan_path else plan.path
        self.plan_base_dir = Path(plan_base_dir) if plan_base_dir else plan.base_dir
        self.plan_snapshot = Path(plan_snapshot) if plan_snapshot else None
        self.owner = owner
        self.resume = resume
        self.initial_results = dict(initial_results or {})
        self.resume_jobs = dict(resume_jobs or {})
        # Gate bookkeeping: an in-memory mirror of the run's gate state, so the
        # loop can decide without a store round-trip; it is persisted on the
        # task states and reloaded on recovery.
        self._gate_attempts: dict[str, int] = {}
        self._gate_feedback: dict[str, str] = {}
        if self.run_store is not None and self.run_id:
            self._load_gate_state()
        self._plan_stamp = self._stamp(self.plan_path)
        self._clients: dict[str, WorkerClient] = {}
        self._rr = 0  # round-robin across workers

    def _load_gate_state(self) -> None:
        run = self.run_store.read(self.run_id)
        if not run:
            return
        for state in run.get("tasks", []):
            attempts = state.get("gate_attempt") or 0
            if attempts:
                self._gate_attempts[state["id"]] = attempts
            feedback = state.get("gate_feedback")
            if feedback:
                self._gate_feedback[state["id"]] = feedback

    @staticmethod
    def _stamp(path: Path) -> tuple[int, int] | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

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
            self._touch()

    def _touch(self) -> None:
        """Signal a state change to waiters (see RunManager.wait_for_run)."""
        if self.on_state_change is not None:
            try:
                self.on_state_change()
            except Exception:
                logger.debug("run %s: state listener failed", self.run_id, exc_info=True)

    def run(self) -> int:
        self._log(
            f"[bold]Plan[/] {self.plan.path.name}  [dim](run {self.run_id})[/]"
        )

        if self.dry_run:
            self._print_plan()
            return 0

        if self.run_store is not None and not self.resume:
            self.run_store.create(
                self.run_id,
                plan_path=str(self.plan.path),
                plan_name=self.plan.path.name,
                only=sorted(self.only) if self.only else None,
                tasks=build_task_states(self.plan, sorted(self.only) if self.only else None),
                inputs_run_id=self.inputs_run_id,
                plan_snapshot=str(self.plan_snapshot) if self.plan_snapshot else None,
                base_dir=str(self.plan_base_dir),
                owner=self.owner,
            )
            logger.info(
                "run %s: created (plan=%s snapshot=%s tasks=%d)",
                self.run_id,
                self.plan.path.name,
                self.plan_snapshot,
                len(self.plan.tasks),
            )

        if not self._current_workers():
            self._log("[yellow]no workers configured (workers.yaml)[/]")
            logger.warning("run %s: no workers configured (workers.yaml)", self.run_id)

        results: dict[str, str] = dict(self.initial_results)
        canceled = False
        # ``halt`` stops dispatching new tasks (fail fast / cancel) but the tasks
        # already running are always awaited before the run is finalized.
        halt = False
        done: queue.Queue = queue.Queue()
        running: dict[str, tuple[WorkerClient | None, threading.Thread]] = {}
        inbox: list = []
        worker_deadline: float | None = None
        try:
            self._print_workers()
            # The task list is re-derived from the (possibly edited) plan on
            # every pass, so live edits apply between dispatches.
            while True:
                self._refresh_plan()
                tasks = self._plan_tasks()

                # Collect the tasks that finished since the last pass. Only
                # this loop mutates ``results``/``running``, so no lock is
                # needed there.
                events = inbox
                inbox = []
                while True:
                    try:
                        events.append(done.get_nowait())
                    except queue.Empty:
                        break
                if events and self._handle_events(events, results, running):
                    # A freshly failed task fails the run fast, but in-flight
                    # tasks keep running to completion.
                    self._log("[red]task failed, stopping run[/]")
                    logger.warning(
                        "run %s: task failed, stopping run", self.run_id
                    )
                    halt = True

                if self._is_canceled() and not canceled:
                    canceled = True
                    halt = True
                    logger.warning("run %s: canceled by user", self.run_id)

                capacity_blocked = False
                if not halt:
                    _, capacity_blocked, requirements_failed = self._dispatch_ready(
                        tasks, results, running, done
                    )
                    if requirements_failed:
                        self._log(
                            "[red]a task has no matching worker, stopping run[/]"
                        )
                        logger.warning(
                            "run %s: a task has no matching worker, stopping run",
                            self.run_id,
                        )
                        halt = True

                if halt and not running:
                    if canceled:
                        self._cancel_remaining(tasks, results, running)
                    else:
                        self._skip_remaining(
                            tasks,
                            results,
                            running,
                            reason="run_stopped",
                            message="run stopped after a task failed",
                        )
                    break
                if not halt and not running and not self._has_pending(tasks, results):
                    break

                # No free worker while ready tasks wait: fail them once the
                # configured wait elapses (as the sequential engine did).
                if not running:
                    if capacity_blocked:
                        if worker_deadline is None:
                            self._log("[yellow]no free worker available, waiting...[/]")
                            worker_deadline = time.time() + self.worker_wait_timeout
                        elif time.time() > worker_deadline:
                            self._fail_no_worker(tasks, results, running)
                            halt = True
                            worker_deadline = None
                    else:
                        worker_deadline = None
                else:
                    worker_deadline = None

                # Block until a task finishes (or poll), so completions wake us
                # immediately and configuration changes are noticed regularly.
                try:
                    inbox.append(done.get(timeout=self.poll_interval))
                except queue.Empty:
                    pass
        finally:
            # Let every in-flight task finish before tearing the clients down;
            # this is what keeps the run "running" (and wait_for_run blocked)
            # until the last worker job is drained.
            for _, thread in list(running.values()):
                thread.join()
            self._close_clients()

        if canceled:
            self._cancel_pending_tasks()
        status = self._final_status(results, canceled)
        if self.run_store is not None:
            self.run_store.update(self.run_id, status=status, finished_at=time.time())
            self._touch()
        logger.info(
            "run %s: finished status=%s results=%s", self.run_id, status, results
        )

        return self._summary(results)

    # ------------------------------------------------------------ live edits
    def _refresh_plan(self) -> None:
        """Reload the run's plan if it changed on disk, applying pending edits."""
        if self.run_store is None:
            return
        stamp = self._stamp(self.plan_path)
        if stamp == self._plan_stamp:
            return
        self._plan_stamp = stamp
        if stamp is None:
            logger.warning("run %s: plan file disappeared: %s", self.run_id, self.plan_path)
            return
        try:
            plan = load_plan(self.plan_path, base_dir=self.plan_base_dir)
        except PlanError as exc:
            logger.warning("run %s: plan edit ignored (invalid): %s", self.run_id, exc)
            return

        added, removed = self._apply_plan(plan)
        self.plan = plan
        if added or removed:
            logger.info(
                "run %s: plan updated (added=%s removed=%s)",
                self.run_id,
                added or "-",
                removed or "-",
            )

    def _apply_plan(self, plan: Plan) -> tuple[list[str], list[str]]:
        """Reconcile run.json tasks with a freshly loaded plan.

        New tasks are appended as pending; pending tasks that disappeared from
        the plan are marked canceled. Terminal/running tasks are never touched.
        The whole read-modify-write runs under the store lock so it cannot lose
        a concurrent task update from a worker thread.
        """
        only = sorted(self.only) if self.only else None
        new_states = {state["id"]: state for state in build_task_states(plan, only)}
        added: list[str] = []
        removed: list[str] = []

        def reconcile(run: dict) -> None:
            existing = {task.get("id") for task in run.get("tasks", [])}
            for task_id, state in new_states.items():
                if task_id not in existing:
                    run["tasks"].append(state)
                    added.append(task_id)
            for task in run.get("tasks", []):
                if task.get("id") not in new_states and task.get("status") == "pending":
                    task["status"] = "skipped"
                    task["skip_reason"] = "removed"
                    task["error"] = "removed from plan"
                    removed.append(task["id"])

        run = self.run_store.update_from(self.run_id, reconcile)
        if run is None:
            return [], []
        if added or removed:
            self._touch()
        return added, removed

    def _cancel_remaining(
        self,
        tasks: list[Task],
        results: dict[str, str],
        running: dict[str, tuple[WorkerClient | None, threading.Thread]],
    ) -> None:
        for remaining in tasks:
            if remaining.id in results or remaining.id in running:
                continue
            results[remaining.id] = "canceled"
            self._store_task(remaining.id, status="canceled", error="canceled by user")

    def _skip_remaining(
        self,
        tasks: list[Task],
        results: dict[str, str],
        running: dict[str, tuple[WorkerClient | None, threading.Thread]],
        *,
        reason: str,
        message: str,
    ) -> None:
        for remaining in tasks:
            if remaining.id in results or remaining.id in running:
                continue
            results[remaining.id] = "skipped"
            self._store_task(
                remaining.id, status="skipped", skip_reason=reason, error=message
            )

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

    # --------------------------------------------------------------- scheduler
    def _plan_tasks(self) -> list[Task]:
        """The plan's tasks, honoring ``only``."""
        return [
            task for task in self.plan.tasks if self.only is None or task.id in self.only
        ]

    @staticmethod
    def _has_pending(tasks: list[Task], results: dict[str, str]) -> bool:
        return any(task.id not in results for task in tasks)

    @staticmethod
    def _dependencies(task: Task) -> list[str]:
        return list(dict.fromkeys(list(task.depends_on) + list(task.inputs_from)))

    def _unsatisfied(
        self, task: Task, results: dict[str, str], known: set[str]
    ) -> list[str]:
        """Dependencies that can never succeed (failed, canceled or unknown)."""
        bad: list[str] = []
        for dep in self._dependencies(task):
            if dep in results:
                if results[dep] != "succeeded":
                    bad.append(dep)
            elif dep not in known:
                bad.append(dep)
        return bad

    def _deps_pending(self, task: Task, results: dict[str, str]) -> bool:
        return any(dep not in results for dep in self._dependencies(task))

    def _handle_events(
        self,
        events: list,
        results: dict[str, str],
        running: dict[str, tuple[WorkerClient | None, threading.Thread]],
    ) -> bool:
        """Apply finished-task events. Returns True when the run must stop."""
        new_failure = False
        for kind, task_id, client, result in events:
            running.pop(task_id, None)
            if kind == "busy":
                name = client.endpoint.name if client is not None else "worker"
                self._log(f"[yellow]{name} busy[/], looking for another worker")
                continue
            self._record_result(result)
            task = self._plan_task(task_id)
            if task is not None and task.kind == "gate" and result.gate_valid is not None:
                # Keep the persisted verdict in sync with the last evaluation so
                # a gate that was rejected once then passed no longer shows the
                # stale INVALID.
                self._store_task(
                    task_id, gate_verdict="VALID" if result.gate_valid else "INVALID"
                )
            if (
                task is not None
                and task.kind == "gate"
                and result.status == "succeeded"
                and result.gate_valid is False
            ):
                # A gate that answered without VALID is a *rejection*: reset the
                # analysed tasks (bounded) and re-run the gate, instead of
                # failing the run on the first negative verdict.
                reset = self._handle_gate_rejection(task, result)
                if reset is None:
                    results[task_id] = "failed"
                    new_failure = True
                else:
                    for reset_id in reset:
                        results.pop(reset_id, None)
                continue
            results[task_id] = result.status
            logger.info("run %s: task %s -> %s", self.run_id, task_id, result.status)
            if result.status == "failed":
                new_failure = True
        return new_failure

    def _dispatch_ready(
        self,
        tasks: list[Task],
        results: dict[str, str],
        running: dict[str, tuple[WorkerClient | None, threading.Thread]],
        done: queue.Queue,
    ) -> tuple[bool, bool, bool]:
        """Start every ready task that fits on a matching free worker.

        Returns ``(dispatched, capacity_blocked, requirements_failed)``:
        ``capacity_blocked`` is True when a ready task had to wait because no
        worker (or no parallelism slot) was available, and
        ``requirements_failed`` is True when a task's ``requirements`` cannot be
        satisfied by any reachable worker (the task is marked failed).
        """
        dispatched = False
        capacity_blocked = False
        requirements_failed = False
        known = {task.id for task in tasks} | set(results)
        # Reachable workers, probed once per pass. ``claimed`` tracks the ones
        # already taken in this pass so a worker is never offered twice while
        # still counting as a candidate for requirement matching.
        available: list[tuple[WorkerClient, dict, bool, bool]] | None = None
        claimed: set[str] = set()

        for task in tasks:
            if task.id in results or task.id in running:
                continue

            if self._task_is_canceled(task.id):
                self._log(
                    f"[yellow]canceled[/] {task.id} [dim](canceled while pending)[/]"
                )
                logger.info(
                    "run %s: task %s canceled while pending", self.run_id, task.id
                )
                results[task.id] = "canceled"
                self._store_task(task.id, status="canceled", error="canceled by user")
                continue

            if not self.assume_deps_ok:
                bad = self._unsatisfied(task, results, known)
                if bad:
                    self._log(
                        f"[yellow]skip[/] {task.id} [dim](unsatisfied dependencies: "
                        f"{', '.join(bad)})[/]"
                    )
                    logger.info(
                        "run %s: task %s skipped (unsatisfied deps: %s)",
                        self.run_id,
                        task.id,
                        ", ".join(bad),
                    )
                    results[task.id] = "skipped"
                    self._store_task(
                        task.id,
                        status="skipped",
                        skip_reason="dependency",
                        error=f"unsatisfied dependencies: {', '.join(bad)}",
                    )
                    continue
                if self._deps_pending(task, results):
                    continue  # dependencies not finished yet

            # Reattaching to a job left running by a previous server process
            # bypasses worker selection: it belongs to a specific worker.
            if task.id in self.resume_jobs:
                self._start_task(task, None, running, done)
                dispatched = True
                continue

            if self.max_parallel and len(running) >= self.max_parallel:
                capacity_blocked = True
                continue
            if available is None:
                available = self._available_clients(running)

            pick: WorkerClient | None = None
            matching_exists = False
            for offset in range(len(available)):
                index = (self._rr + offset) % len(available)
                client, capabilities, busy, reserved = available[index]
                if not self._matches(task, capabilities):
                    continue
                matching_exists = True
                if busy or reserved or client.base_url in claimed:
                    continue
                pick = client
                self._rr = index + 1
                break

            if pick is None:
                if not matching_exists:
                    # No reachable worker can ever run this task: fail fast
                    # instead of waiting for the worker timeout.
                    message = self._no_match_message(task, available)
                    self._log(f"[red]no matching worker[/] {task.id} [dim]{message}[/]")
                    logger.warning(
                        "run %s: task %s has no matching worker (%s)",
                        self.run_id,
                        task.id,
                        message,
                    )
                    results[task.id] = "failed"
                    self._store_task(
                        task.id,
                        status="failed",
                        error=f"no worker matches requirements: {message}",
                    )
                    requirements_failed = True
                    continue
                capacity_blocked = True
                continue

            claimed.add(pick.base_url)
            self._start_task(task, pick, running, done)
            dispatched = True

        return dispatched, capacity_blocked, requirements_failed

    @staticmethod
    def _matches(task: Task, capabilities: dict) -> bool:
        """True when a worker's advertised capabilities satisfy task requirements."""
        req = task.requirements or {}
        if not req:
            return True
        req_os = req.get("os")
        if req_os and str(req_os) != str(capabilities.get("os") or ""):
            return False
        worker_tags = {str(tag) for tag in (capabilities.get("tags") or [])}
        if not {str(tag) for tag in (req.get("tags") or [])}.issubset(worker_tags):
            return False
        worker_providers = {str(p) for p in (capabilities.get("providers") or [])}
        if not {str(p) for p in (req.get("providers") or [])}.issubset(worker_providers):
            return False
        worker_labels = capabilities.get("labels") or {}
        for key, value in (req.get("labels") or {}).items():
            if str(worker_labels.get(key)) != str(value):
                return False
        return True

    @staticmethod
    def _no_match_message(task: Task, available: list) -> str:
        req = task.requirements or {}
        seen = [
            f"{client.endpoint.name}(os={caps.get('os')},"
            f" tags={','.join(caps.get('tags') or []) or '-'},"
            f" providers={','.join(caps.get('providers') or []) or '-'}"
            + (f", labels={caps.get('labels')}" if caps.get("labels") else "")
            + ")"
            for client, caps, _busy, _reserved in available
        ]
        wanted = ", ".join(f"{key}={value}" for key, value in req.items())
        reachable = "; ".join(seen) if seen else "none"
        return f"need [{wanted}] but reachable workers are: {reachable}"

    def _fail_no_worker(
        self,
        tasks: list[Task],
        results: dict[str, str],
        running: dict[str, tuple[WorkerClient | None, threading.Thread]],
    ) -> None:
        """Fail the ready tasks that could not get a worker in time."""
        known = {task.id for task in tasks} | set(results)
        failed = 0
        for task in tasks:
            if task.id in results or task.id in running:
                continue
            if not self.assume_deps_ok:
                if self._unsatisfied(task, results, known) or self._deps_pending(
                    task, results
                ):
                    continue
            results[task.id] = "failed"
            self._store_task(task.id, status="failed", error="no worker available")
            failed += 1
        if failed:
            logger.warning(
                "run %s: %d task(s) failed: no worker available", self.run_id, failed
            )

    def _start_task(
        self,
        task: Task,
        client: WorkerClient | None,
        running: dict[str, tuple[WorkerClient | None, threading.Thread]],
        done: queue.Queue,
    ) -> None:
        thread = threading.Thread(
            target=self._task_main,
            args=(task, client, done),
            daemon=True,
            name=f"task-{self.run_id}-{task.id}",
        )
        running[task.id] = (client, thread)
        thread.start()

    def _task_main(
        self,
        task: Task,
        client: WorkerClient | None,
        done: queue.Queue,
    ) -> None:
        """Worker-thread body: run one task and report back through ``done``."""
        try:
            resume = self.resume_jobs.get(task.id)
            if resume is not None:
                result = self._adopt_job(task, resume)
            else:
                result = self._execute_on(client, task)
        except WorkerBusy:
            done.put(("busy", task.id, client, None))
            return
        except PlanError as exc:
            self._log(f"[red]plan error:[/] {exc}")
            result = TaskResult(
                task.id,
                "failed",
                worker=client.endpoint.name if client else None,
                worker_url=client.base_url if client else None,
                error=str(exc),
            )
        except WorkerError as exc:
            self._log(f"[red]worker error:[/] {exc}")
            result = TaskResult(
                task.id,
                "failed",
                worker=client.endpoint.name if client else None,
                worker_url=client.base_url if client else None,
                error=str(exc),
            )
        except Exception as exc:  # a dead thread would hang the run forever
            logger.exception("run %s: task %s crashed", self.run_id, task.id)
            result = TaskResult(
                task.id,
                "failed",
                worker=client.endpoint.name if client else None,
                worker_url=client.base_url if client else None,
                error=f"orchestrator error: {exc}",
            )
        done.put(("result", task.id, client, result))

    # ----------------------------------------------------------------- workers
    def _current_workers(self) -> list[WorkerEndpoint]:
        """Workers from the provider (live file) or the static list."""
        if self.workers_provider is not None:
            try:
                return self.workers_provider()
            except Exception:
                return self.workers
        return self.workers

    def _sync_clients(self, keep: set[str] | None = None) -> list[WorkerClient]:
        """Refresh the client pool from the current worker list.

        Called before each dispatch and on every wait iteration, so a worker
        added to workers.yaml while a run is in flight is picked up. Clients in
        ``keep`` (workers with an in-flight task) are never closed, even if the
        worker disappeared from the file, so their task can finish.
        """
        workers = {w.url: w for w in self._current_workers()}
        for url in list(self._clients):
            if url in workers or (keep and url in keep):
                continue
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

    def _probe_clients(
        self, clients: list[WorkerClient]
    ) -> list[tuple[WorkerClient, dict, bool]]:
        """Return ``(client, capabilities, busy)`` for every reachable worker."""
        probed: list[tuple[WorkerClient, dict, bool]] = []
        for client in clients:
            try:
                health = client.health()
            except WorkerError:
                continue
            capabilities = health.get("capabilities") or {}
            probed.append((client, capabilities, bool(health.get("busy"))))
        return probed

    def _available_clients(
        self, running: dict[str, tuple[WorkerClient | None, threading.Thread]]
    ) -> list[tuple[WorkerClient, dict, bool, bool]]:
        """Reachable workers as ``(client, capabilities, busy, reserved)``.

        A worker is *reserved* as soon as a task of this run is dispatched to it;
        that reservation is what keeps two tasks off the same worker even before
        its ``/health`` reports ``busy``. Reserved and busy workers stay in the
        list so requirement matching can tell "busy but capable" (wait) from "no
        capable worker at all" (fail fast).
        """
        reserved = {
            client.base_url for client, _ in running.values() if client is not None
        }
        probed = self._probe_clients(self._sync_clients(keep=reserved))
        return [
            (client, capabilities, busy, client.base_url in reserved)
            for client, capabilities, busy in probed
        ]

    # ------------------------------------------------------------------ tasks
    def _adopt_job(self, task: Task, job: dict) -> TaskResult:
        """Reattach to a job left running by a previous server process."""
        url = job.get("worker_url")
        job_id = job.get("job_id")
        name = job.get("worker") or url or "worker"
        if not url or not job_id:
            logger.error("run %s: task %s cannot resume (missing worker/job id)", self.run_id, task.id)
            return TaskResult(task.id, "failed", error="cannot resume: missing worker/job id")
        logger.info("run %s: task %s reattaching to %s job %s", self.run_id, task.id, name, job_id)
        self._log(f"[bold cyan]{task.id}[/] reattaching to [bold]{name}[/] [dim]({job_id})[/]")
        client = WorkerClient(WorkerEndpoint(name=name, url=url))
        try:
            return self._finish_job(client, task, job_id, request={})
        except WorkerError as exc:
            return TaskResult(task.id, "failed", worker=name, worker_url=url,
                              job_id=job_id, error=f"resume failed: {exc}")
        finally:
            try:
                client.close()
            except Exception:
                pass

    def _record_result(self, result: TaskResult) -> TaskResult:
        self._store_task(
            result.task_id,
            status=result.status,
            worker=result.worker,
            worker_url=result.worker_url,
            job_id=result.job_id,
            duration_s=result.duration_s,
            changed_files=result.files,
            history_rel=result.history_rel,
            error=result.error,
            commits=result.commits,
            artifacts=result.artifacts,
            merge=result.merge,
        )
        return result

    def _substitute_resources(self, task_id: str, resources: list[dict]) -> list[dict]:
        """Resolve ``{run}`` / ``{task}`` in resource option strings.

        Only task-local substitutions exist. Cross-task references are not
        supported: a consumer writes the producer's branch name and lists the
        dependency in ``depends_on`` (see the design doc, section 7.5).
        """
        run_id = self.run_id

        def substitute(value):
            if isinstance(value, str):
                return value.replace("{run}", run_id).replace("{task}", task_id)
            if isinstance(value, list):
                return [substitute(item) for item in value]
            if isinstance(value, dict):
                return {key: substitute(item) for key, item in value.items()}
            return value

        return [substitute(resource) for resource in (resources or [])]

    def _execute_on(self, client: WorkerClient, task: Task) -> TaskResult:
        job_id = f"{task.id}-{uuid.uuid4().hex[:8]}"
        is_gate = task.kind == "gate"

        if is_gate:
            # The gate is server-configured: hardcoded agent, no resources, a
            # fresh workspace, and the decision material uploaded as files.
            attempt = self._gate_attempts.get(task.id, 0) + 1
            prompt = build_gate_prompt(
                task.prompt, task.gate_targets, attempt, task.gate_max_attempts
            )
            resources: list[dict] = []
            agent = GATE_AGENT_ID
            opencode = task.opencode
            artifacts: dict = {"download": "none"}
        else:
            feedback = self._gate_feedback.get(task.id)
            prompt = with_gate_feedback(task.prompt, feedback) if feedback else task.prompt
            resources = self._retry_resources(task.resources) if feedback else task.resources
            agent = task.agent
            opencode = task.opencode
            artifacts = task.artifacts

        request = {
            "job_id": job_id,
            "task_id": task.id,
            "run_id": self.run_id,
            "prompt": prompt,
            "model": task.model,
            "agent": agent,
            "timeout_s": task.timeout_s,
            "variant": task.variant,
            "title": task.title,
            "env": task.env,
            "resources": self._substitute_resources(task.id, resources),
            "artifacts": artifacts,
            "opencode": opencode,
        }
        # 'files' sent to OpenCode is intentionally empty: input files are
        # already extracted into the working directory.
        spec = {k: v for k, v in request.items() if v not in (None, {})}
        spec["files"] = []

        # Snapshot the base_dir once: a live plan edit must not change where
        # this already-running task resolves its inputs.
        base_dir = self.plan.base_dir
        if is_gate:
            inputs = self._gate_inputs(task)
        else:
            inputs = self._resolve_inputs(task, base_dir)
            if self._gate_feedback.get(task.id):
                inputs = inputs + self._feedback_inputs(task)

        client.create_job(spec)  # raises WorkerBusy when busy
        self._store_task(task.id, status="running", worker=client.endpoint.name,
                         worker_url=client.base_url, job_id=job_id)
        logger.info(
            "run %s: task %s dispatched to %s (job %s, model %s)",
            self.run_id,
            task.id,
            client.endpoint.name,
            job_id,
            task.model or "default",
        )
        self._log(
            f"[bold cyan]{task.id}[/] -> [bold]{client.endpoint.name}[/] "
            f"[dim]({job_id}, model {task.model or 'default'})[/]"
        )

        if inputs:
            self._log(f"  uploading {len(inputs)} input file(s)")
            client.upload_files(job_id, self._zip_inputs(inputs))

        client.start_job(job_id)
        return self._finish_job(client, task, job_id, request=request)

    def _finish_job(
        self, client: WorkerClient, task: Task, job_id: str, request: dict
    ) -> TaskResult:
        """Poll a job to completion, then save its artifacts and result."""
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
        state = status.get("status", "failed")
        text = status.get("result_text") or ""
        logger.info(
            "run %s: task %s finished status=%s duration=%ss",
            self.run_id,
            task.id,
            state,
            status.get("duration_s"),
        )
        return TaskResult(
            task_id=task.id,
            status=state,
            worker=client.endpoint.name,
            worker_url=client.base_url,
            job_id=job_id,
            duration_s=status.get("duration_s"),
            files=sorted(set(status.get("added", [])) | set(status.get("modified", []))),
            history_rel=directory.relative_to(self.history.root).as_posix(),
            error=status.get("error"),
            commits=list(status.get("commits") or []),
            artifacts=list(status.get("artifacts") or []),
            merge=dict(status.get("merge") or {}),
            text=text,
            gate_valid=is_valid_verdict(text) if task.kind == "gate" else None,
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
                if "HTTP 404" in str(exc):
                    logger.warning(
                        "run %s: task %s job %s disappeared from worker %s",
                        self.run_id,
                        task.id,
                        job_id,
                        client.endpoint.name,
                    )
                    return {
                        "job_id": job_id,
                        "status": "failed",
                        "error": f"job not found on worker: {exc}",
                    }
                transient += 1
                if transient > 10:
                    logger.warning(
                        "run %s: task %s worker %s unreachable after %d tries",
                        self.run_id,
                        task.id,
                        client.endpoint.name,
                        transient,
                    )
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
                logger.debug(
                    "run %s: task %s status=%s (%.0fs)",
                    self.run_id,
                    task.id,
                    current,
                    time.time() - started,
                )
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
    def _resolve_inputs(
        self, task: Task, base_dir: Path | None = None
    ) -> list[tuple[str, Path]]:
        """Resolve ``files`` patterns and ``inputs_from`` outputs.

        Returns a list of ``(name_in_zip, source_path)``. ``base_dir`` is snap­
        shot at dispatch time so a live plan edit cannot change where an already
        running task reads its inputs.
        """
        base = base_dir if base_dir is not None else self.plan.base_dir
        pairs: dict[str, Path] = {}  # arcname -> source (dedup, last one wins)

        for spec in task.files:
            matches = glob.glob(str(base / spec), recursive=True)
            if not matches:
                raise PlanError(f"task '{task.id}': file not found: {spec}")
            for match in matches:
                path = Path(match)
                if path.is_dir():
                    for child in path.rglob("*"):
                        if child.is_file():
                            pairs[self._arcname(child, base)] = child
                elif path.is_file():
                    pairs[self._arcname(path, base)] = path

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

    def _arcname(self, path: Path, base_dir: Path | None = None) -> str:
        base = base_dir if base_dir is not None else self.plan.base_dir
        try:
            return path.resolve().relative_to(base).as_posix()
        except ValueError:
            return path.name

    @staticmethod
    def _zip_inputs(pairs: list[tuple[str, Path]]) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for arc, path in pairs:
                archive.write(path, arc)
        return buffer.getvalue()

    # ------------------------------------------------------------------- gates
    def _plan_task(self, task_id: str) -> Task | None:
        for task in self.plan.tasks:
            if task.id == task_id:
                return task
        return None

    @staticmethod
    def _retry_resources(resources: list[dict]) -> list[dict]:
        """Force-push on a retry: the branch already exists on the remote.

        Safe because the consumer depends on the gate, so no downstream task has
        read the rejected attempt's branch.
        """
        retried: list[dict] = []
        for resource in resources or []:
            if resource.get("type") == "git":
                resource = dict(resource)
                options = dict(resource.get("with") or {})
                options["force"] = True
                resource["with"] = options
            retried.append(resource)
        return retried

    def _feedback_inputs(self, task: Task) -> list[tuple[str, Path]]:
        """The gate rejection, uploaded for a retried task to read."""
        root = self.history.run_dir(task.id, self.run_id) / "gate-feedback"
        target = root / "_hiveling" / "gate-feedback.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {"task": task.id, "feedback": self._gate_feedback.get(task.id, "")},
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return [("_hiveling/gate-feedback.json", target)]

    def _gate_inputs(self, task: Task) -> list[tuple[str, Path]]:
        """Build the decision material a gate reads under ``_hiveling/``.

        A gate runs with no repository, so it can only judge what the
        orchestrator hands it: the plan, every analysed task's status/result and
        the files that task downloaded. No branch concept is involved.
        """
        root = self.history.run_dir(task.id, self.run_id) / "gate-input"
        shutil.rmtree(root, ignore_errors=True)
        hiveling = root / "_hiveling"
        (hiveling / "tasks").mkdir(parents=True, exist_ok=True)

        run = self.run_store.read(self.run_id) if self.run_store is not None else None
        states = {state.get("id"): state for state in (run or {}).get("tasks", [])}

        attempt = self._gate_attempts.get(task.id, 0) + 1
        previous = states.get(task.id, {}).get("gate_feedback")
        self._write_json(
            hiveling / "gate.json",
            {
                "id": task.id,
                "attempt": attempt,
                "max_attempts": task.gate_max_attempts,
                "criteria": task.prompt,
                "targets": list(task.gate_targets),
                "previous_verdicts": [previous] if previous else [],
            },
        )
        self._write_json(
            hiveling / "plan.json",
            {
                "tasks": [
                    {
                        "id": item.id,
                        "kind": item.kind,
                        "prompt": item.prompt,
                        "depends_on": item.depends_on,
                        "inputs_from": item.inputs_from,
                        "agent": item.agent,
                    }
                    for item in self.plan.tasks
                ]
            },
        )

        results: dict = {}
        for target_id in task.gate_targets:
            state = states.get(target_id, {})
            plan_task = self._plan_task(target_id)
            task_dir = hiveling / "tasks" / target_id
            task_dir.mkdir(parents=True, exist_ok=True)
            (task_dir / "prompt.txt").write_text(
                plan_task.prompt if plan_task else "", encoding="utf-8"
            )
            history_dir = self.history.run_dir(target_id, self.run_id)
            result_path = history_dir / "result.txt"
            result_text = (
                result_path.read_text(encoding="utf-8", errors="replace")
                if result_path.is_file()
                else ""
            )
            (task_dir / "result.txt").write_text(result_text, encoding="utf-8")
            files_dir = history_dir / "files"
            if files_dir.is_dir():
                shutil.copytree(files_dir, task_dir / "files", dirs_exist_ok=True)
            results[target_id] = {
                "status": state.get("status"),
                "result_text": result_text,
                "changed_files": list(state.get("changed_files") or []),
                "commits": list(state.get("commits") or []),
                "error": state.get("error"),
                "attempts": state.get("attempts") or 0,
            }
        self._write_json(hiveling / "results.json", results)

        pairs: list[tuple[str, Path]] = []
        for path in sorted(hiveling.rglob("*")):
            if path.is_file():
                pairs.append((path.relative_to(root).as_posix(), path))
        return pairs

    @staticmethod
    def _write_json(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def _handle_gate_rejection(self, task: Task, result: TaskResult) -> list[str] | None:
        """Reset the analysed tasks and the gate, or exhaust the attempts.

        Returns the task ids to drop from ``results`` so they are dispatched
        again, or ``None`` when ``max_attempts`` is reached (the gate fails).
        """
        attempt = self._gate_attempts.get(task.id, 0) + 1
        max_attempts = task.gate_max_attempts or 1
        if attempt >= max_attempts:
            self._store_task(
                task.id,
                status="failed",
                gate_attempt=attempt,
                gate_verdict="INVALID",
                error=f"gate rejected after {attempt} attempt(s): "
                + self._one_line(result.text),
            )
            self._log(
                f"[red]gate {task.id} rejected after {attempt} attempt(s)[/]"
            )
            return None

        self._gate_attempts[task.id] = attempt
        state = self.run_store.read(self.run_id) if self.run_store is not None else None
        by_id = {t.get("id"): t for t in (state or {}).get("tasks", [])}
        if result.history_rel:
            self._archive_attempt(self.history.root / result.history_rel, attempt)
        for target_id in task.gate_targets:
            self._gate_feedback[target_id] = result.text
            previous = by_id.get(target_id, {})
            if previous.get("history_rel"):
                self._archive_attempt(
                    self.history.root / previous["history_rel"], attempt
                )
            self._store_task(
                target_id,
                status="pending",
                error=None,
                last_error=previous.get("error"),
                skip_reason=None,
                worker=None,
                worker_url=None,
                job_id=None,
                duration_s=None,
                history_rel=None,
                changed_files=[],
                commits=[],
                artifacts=[],
                merge={},
                attempts=(previous.get("attempts") or 0) + 1,
                gate_feedback=result.text,
            )
        self._store_task(
            task.id,
            status="pending",
            error=None,
            skip_reason=None,
            worker=None,
            worker_url=None,
            job_id=None,
            duration_s=None,
            changed_files=[],
            commits=[],
            artifacts=[],
            merge={},
            gate_attempt=attempt,
            gate_feedback=result.text,
            gate_verdict="INVALID",
        )
        self._log(
            f"[yellow]gate[/] {task.id} [dim]rejected (attempt {attempt}/"
            f"{max_attempts}); re-running {', '.join(task.gate_targets)}[/]"
        )
        return list(task.gate_targets) + [task.id]

    @staticmethod
    def _archive_attempt(directory: Path, attempt: int) -> None:
        if not directory.is_dir():
            return
        archive = directory / f"attempt-{attempt}"
        try:
            archive.mkdir(parents=True, exist_ok=True)
            for name in ("status.json", "result.txt"):
                source = directory / name
                if source.is_file():
                    (archive / name).write_bytes(source.read_bytes())
        except OSError:
            logger.debug("could not archive attempt %s in %s", attempt, directory)

    @staticmethod
    def _one_line(text: str, limit: int = 300) -> str:
        collapsed = " ".join((text or "").split())
        return collapsed[:limit]

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
        for commit in status.get("commits") or []:
            action = "pushed" if commit.get("pushed") else "committed"
            self.console.print(
                f"  [dim]{action} {commit.get('branch')} "
                f"@ {str(commit.get('sha') or '')[:8]}[/]"
            )
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

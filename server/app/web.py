"""FastAPI application for the server: JSON API + dashboard + MCP endpoint.

It exposes the run history, live run state and lets the user cancel or retry
runs, view/edit the plan behind a run, and start runs by pushing a plan YAML
over HTTP. The same operations are exposed to OpenCode over MCP (Streamable
HTTP) on ``/mcp``. The JSON API has no authentication; the MCP endpoint
supports an optional bearer token.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel

from .dashboard_html import DASHBOARD_HTML
from .history import archive_attempt
from .mcp_server import BearerAuthMiddleware, create_mcp_server
from .orchestrator import Orchestrator
from .plan import PlanError, load_plan
from .plan_files import (
    is_editable_plan,
    list_plan_files,
    persist_pushed_plan,
    plan_name,
    plan_summary,
    resolve_known_plan,
    write_plan,
)
from .readers import read_json, read_text, zip_dir
from .redact import make_redactor
from .runs import TERMINAL_RUN_STATUSES, RunStore, new_run_id
from .usage import run_usage
from .workers import WorkerRegistry, probe_workers

logger = logging.getLogger("hiveling.server.web")

# Static assets (logo, favicon) shipped alongside the dashboard.
STATIC_DIR = Path(__file__).resolve().parent / "static"

EVENTS_TAIL_LINES = 400
WORKER_PROBE_TIMEOUT = 2.0
TASK_TERMINAL_STATUSES = {"succeeded", "failed", "canceled", "skipped"}
# A run is only reported terminal to waiters once no task is still running:
# after a failure the orchestrator stops dispatching but lets the in-flight
# tasks finish, so ``wait_for_run`` must not wake early on the run status alone.
TASK_ACTIVE_STATUSES = {"running"}
# A run owner is considered alive if it refreshed its heartbeat within this
# window; otherwise a restart is allowed to recover the run.
LEASE_TTL_S = 30.0


def is_run_terminal(run: dict) -> bool:
    """True when the run finished and no task is still in flight."""
    if run.get("status") not in TERMINAL_RUN_STATUSES:
        return False
    return not any(
        task.get("status") in TASK_ACTIVE_STATUSES for task in run.get("tasks", [])
    )


@dataclass
class DashboardConfig:
    data_dir: Path
    plans_dir: Path
    poll_interval: float = 2.0
    worker_wait_timeout: float = 1800.0
    # None = use the plan's max_parallel (or unlimited when the plan has none).
    max_parallel: int | None = None
    workers_file: Path | None = None
    heartbeat_s: float = 60.0
    mcp_token: str | None = None
    mcp_allowed_hosts: list[str] = field(default_factory=list)
    mcp_allowed_origins: list[str] = field(default_factory=list)

    @property
    def history_dir(self) -> Path:
        return self.data_dir / "history"

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    @property
    def pushed_plans_dir(self) -> Path:
        return self.data_dir / "plans"

    @property
    def workers_path(self) -> Path:
        return self.workers_file or (self.data_dir / "workers.yaml")


class SavePlanRequest(BaseModel):
    content: str


class RunManager:
    """Starts runs in background threads and tracks their cancel events."""

    def __init__(self, config: DashboardConfig):
        self.config = config
        self.store = RunStore(config.runs_dir)
        self.workers = WorkerRegistry(config.workers_path)
        self._active: dict[str, dict] = {}
        self._lock = threading.RLock()
        # Waiters (wait_for_run) sleep on this condition; it is notified on every
        # run/task state change via _notify_state.
        self._condition = threading.Condition(self._lock)
        self._revisions: dict[str, int] = {}
        self._stop = threading.Event()

    def _notify_state(self, run_id: str) -> None:
        with self._condition:
            self._revisions[run_id] = self._revisions.get(run_id, 0) + 1
            self._condition.notify_all()

    def start(
        self,
        plan_path: Path,
        *,
        only: list[str] | None = None,
    ) -> str:
        plan = load_plan(plan_path)  # raises PlanError
        run_id = new_run_id()
        snapshot = self._write_snapshot(run_id, plan.path)
        cancel_event = threading.Event()
        orchestrator = Orchestrator(
            plan,
            history_dir=self.config.history_dir,
            poll_interval=self.config.poll_interval,
            worker_wait_timeout=self.config.worker_wait_timeout,
            only=only,
            max_parallel=self.config.max_parallel,
            run_store=self.store,
            run_id=run_id,
            cancel_event=cancel_event,
            quiet=True,
            workers_provider=self.workers.get,
            task_cancel_provider=lambda task_id: self._task_cancel_requested(run_id, task_id),
            on_state_change=lambda: self._notify_state(run_id),
            plan_path=snapshot,
            plan_base_dir=plan.base_dir,
            plan_snapshot=snapshot,
            owner=self._owner(),
        )
        thread = threading.Thread(target=orchestrator.run, daemon=True, name=f"run-{run_id}")
        with self._lock:
            self._active[run_id] = {
                "event": cancel_event,
                "thread": thread,
                "task_cancels": {},
                "orchestrator": orchestrator,
            }
        logger.info(
            "run %s: starting plan=%s snapshot=%s", run_id, plan.path.name, snapshot
        )
        thread.start()
        return run_id

    def _write_snapshot(self, run_id: str, source: Path) -> Path:
        """Copy the validated plan into the run directory (recovery + live edits)."""
        run_dir = self.store.path(run_id).parent
        run_dir.mkdir(parents=True, exist_ok=True)
        target = run_dir / "plan.yaml"
        try:
            content = source.read_text(encoding="utf-8")
        except OSError:
            content = ""
        target.write_text(content, encoding="utf-8")
        return target

    @staticmethod
    def _owner() -> dict:
        return {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "heartbeat_at": time.time(),
        }

    def _task_cancel_requested(self, run_id: str, task_id: str) -> bool:
        with self._lock:
            entry = self._active.get(run_id)
            if entry is None:
                return False
            event = entry["task_cancels"].get(task_id)
            return event is not None and event.is_set()

    def cancel_task(self, run_id: str, task_id: str) -> dict:
        """Cancel one task of a run.

        A running task is scheduled for cancellation (the orchestrator stops its
        worker job at the next poll); a pending task is marked canceled so it is
        never started. Dependent tasks are then skipped and the run continues.
        """
        run = self.store.read(run_id)
        if run is None:
            return {"ok": False, "reason": "run not found"}
        state = next((t for t in run.get("tasks", []) if t.get("id") == task_id), None)
        if state is None:
            return {"ok": False, "reason": "task not found"}
        if state.get("status") in TASK_TERMINAL_STATUSES:
            return {"ok": False, "reason": f"task already {state['status']}"}
        with self._lock:
            entry = self._active.get(run_id)
            if entry is not None:
                entry.setdefault("task_cancels", {}).setdefault(task_id, threading.Event()).set()
        self.store.update_task(run_id, task_id, status="canceled", error="canceled by user")
        self._notify_state(run_id)
        return {"ok": True, "task_id": task_id, "status": "canceled"}

    def cancel(self, run_id: str) -> bool:
        with self._lock:
            entry = self._active.get(run_id)
        if entry is None:
            return False
        entry["event"].set()
        self.store.update(run_id, cancel_requested=True)
        self._notify_state(run_id)
        logger.warning("run %s: cancel requested", run_id)
        return True

    def is_active(self, run_id: str) -> bool:
        with self._lock:
            entry = self._active.get(run_id)
        return entry is not None and entry["thread"].is_alive()

    # ----------------------------------------------------------- recovery
    def recover(self) -> None:
        """Resume runs left 'running' by a previous server process.

        A run of another *live* server (fresh heartbeat) is left alone. Each
        recovered run keeps its task statuses, reattaches to any worker job
        still running, and continues from the first task without a result.
        """
        claimed = 0
        for summary in self.store.list(limit=1_000_000):
            if summary.get("status") != "running":
                continue
            run_id = summary["run_id"]
            run = self.store.read(run_id)
            if run is None:
                continue
            if not self._claim(run_id, run):
                logger.info("run %s: owned by another live server, skipping recovery", run_id)
                continue
            claimed += 1
            try:
                self._resume(run)
            except Exception:
                logger.exception("run %s: recovery failed", run_id)
                self.store.update(
                    run_id,
                    status="failed",
                    error="recovery failed",
                    finished_at=time.time(),
                )
        if claimed:
            logger.info("recovery: %d run(s) resumed", claimed)

    def _claim(self, run_id: str, run: dict) -> bool:
        owner = run.get("owner") or {}
        same_process = owner.get("pid") == os.getpid()
        fresh = (time.time() - (owner.get("heartbeat_at") or 0)) < LEASE_TTL_S
        if not same_process and fresh:
            return False
        self.store.update(run_id, owner=self._owner())
        return True

    def _resume(self, run: dict) -> None:
        run_id = run["run_id"]
        source = run.get("plan_path")
        base_dir = run.get("base_dir") or (str(Path(source).parent) if source else None)
        snapshot = run.get("plan_snapshot")
        plan_file = None
        for candidate in (snapshot, source):
            if candidate and Path(candidate).is_file():
                plan_file = Path(candidate)
                break
        if plan_file is None:
            logger.error("run %s: no plan file to recover from", run_id)
            self.store.update(
                run_id, status="failed", error="plan snapshot missing; cannot recover",
                finished_at=time.time(),
            )
            return
        try:
            plan = load_plan(plan_file, base_dir=base_dir)
        except PlanError as exc:
            logger.error("run %s: invalid plan snapshot: %s", run_id, exc)
            self.store.update(
                run_id, status="failed", error=f"invalid plan snapshot: {exc}",
                finished_at=time.time(),
            )
            return

        initial: dict[str, str] = {}
        resume_jobs: dict[str, dict] = {}
        for task in run.get("tasks", []):
            status = task.get("status")
            if status in TASK_TERMINAL_STATUSES:
                initial[task["id"]] = status
            elif status == "running":
                if task.get("worker_url") and task.get("job_id"):
                    resume_jobs[task["id"]] = {
                        "worker": task.get("worker"),
                        "worker_url": task.get("worker_url"),
                        "job_id": task.get("job_id"),
                    }
                else:
                    initial[task["id"]] = "failed"
                    self.store.update_task(
                        run_id, task["id"], status="failed",
                        error="cannot resume: missing worker/job id",
                    )

        cancel_event = threading.Event()
        if run.get("cancel_requested"):
            cancel_event.set()

        orchestrator = Orchestrator(
            plan,
            history_dir=self.config.history_dir,
            poll_interval=self.config.poll_interval,
            worker_wait_timeout=self.config.worker_wait_timeout,
            only=run.get("only") or None,
            max_parallel=self.config.max_parallel,
            run_store=self.store,
            run_id=run_id,
            cancel_event=cancel_event,
            inputs_run_id=run.get("inputs_run_id"),
            quiet=True,
            workers_provider=self.workers.get,
            task_cancel_provider=lambda task_id: self._task_cancel_requested(run_id, task_id),
            on_state_change=lambda: self._notify_state(run_id),
            plan_path=plan_file,
            plan_base_dir=base_dir,
            plan_snapshot=Path(snapshot) if snapshot else plan_file,
            resume=True,
            initial_results=initial,
            resume_jobs=resume_jobs,
        )
        thread = threading.Thread(target=orchestrator.run, daemon=True, name=f"run-{run_id}")
        with self._lock:
            self._active[run_id] = {
                "event": cancel_event,
                "thread": thread,
                "task_cancels": {},
                "orchestrator": orchestrator,
            }
        logger.info(
            "run %s: resumed (%d job(s) reattached, %d task(s) already done)",
            run_id,
            len(resume_jobs),
            len(initial),
        )
        thread.start()
        self._notify_state(run_id)

    # --------------------------------------------------------------- resume
    def resume_run(self, run_id: str) -> dict:
        """Re-arm a finished run: reset its failed and skipped tasks to pending.

        The run keeps its succeeded tasks, re-reads its plan snapshot (so live
        edits apply) and continues from where it stopped. The previous error is
        preserved in ``last_error`` and the finished attempt's evidence
        (``status.json``, ``result.txt``, ``events.jsonl``, …) is archived under
        ``<task history>/attempt-<n>/``.
        """
        if self.is_active(run_id):
            return {"ok": False, "reason": "run is already running"}
        run = self.store.read(run_id)
        if run is None:
            return {"ok": False, "reason": "run not found"}
        plan_file = self.run_plan_file(run_id)
        if plan_file is None:
            return {"ok": False, "reason": "no plan file for this run"}
        base_dir = run.get("base_dir") or None
        try:
            plan = load_plan(plan_file, base_dir=base_dir)
        except PlanError as exc:
            return {"ok": False, "reason": f"invalid plan: {exc}"}
        plan_ids = {task.id for task in plan.tasks}

        reset: list[str] = []
        for task in run.get("tasks", []):
            if task.get("id") not in plan_ids:
                continue  # removed from the plan: nothing to re-run
            if task.get("status") not in ("failed", "skipped"):
                continue
            attempt = task.get("attempts") or 0
            if task.get("history_rel"):
                archive_attempt(self.config.history_dir / task["history_rel"], attempt)
            task["last_error"] = task.get("error")
            task["status"] = "pending"
            task["error"] = None
            task["skip_reason"] = None
            task["worker"] = None
            task["worker_url"] = None
            task["job_id"] = None
            task["duration_s"] = None
            task["changed_files"] = []
            task["history_rel"] = None
            reset.append(task["id"])

        if not reset:
            return {"ok": False, "reason": "no failed or skipped task to resume"}

        run["status"] = "running"
        run["error"] = None
        run["finished_at"] = None
        run["cancel_requested"] = False
        run["resumed_at"] = time.time()
        run["attempts"] = (run.get("attempts") or 1) + 1
        self.store.write(run)
        self._claim(run_id, run)
        logger.warning(
            "run %s: resume requested (reset %d task(s): %s)",
            run_id,
            len(reset),
            ", ".join(reset),
        )
        self._resume(run)
        return {"ok": True, "run_id": run_id, "reset": reset}

    # ------------------------------------------------------------ wait
    def wait_for_run(
        self, run_id: str, timeout_s: float = 600.0, until: str = "terminal"
    ) -> dict:
        """Block until the run reaches a terminal state (default) or changes.

        ``until`` is ``terminal`` (succeeded/failed/canceled) or ``change``
        (any state change). A run counts as terminal only once every in-flight
        task has finished, so a failure never makes the waiter return while
        other tasks are still running. Returns the run state plus the wake
        reason; on timeout it returns ``{"timed_out": true}`` with the current
        state.

        The condition is only used to *wake up* on changes; the run document is
        always read without holding the manager lock, so the orchestrator (which
        holds the store lock while writing then notifies) can never deadlock us.
        """
        if until not in ("terminal", "change"):
            return {"ok": False, "reason": "until must be 'terminal' or 'change'"}
        deadline = time.time() + max(0.0, timeout_s)
        with self._condition:
            start_rev = self._revisions.get(run_id, 0)

        while True:
            run = self.store.read(run_id)
            if run is None:
                return {"ok": False, "reason": "run not found"}
            status = run.get("status")
            if until == "terminal" and is_run_terminal(run):
                return {"run_id": run_id, "status": status, "event": "terminal", "run": run}

            with self._condition:
                changed = self._revisions.get(run_id, 0) != start_rev
            if until == "change" and changed:
                return {
                    "run_id": run_id,
                    "status": status,
                    "event": "change",
                    "run": run,
                }

            remaining = deadline - time.time()
            if remaining <= 0:
                return {
                    "run_id": run_id,
                    "status": status,
                    "event": "timeout",
                    "timed_out": True,
                    "run": run,
                }
            # Sleep until notified (a change) or at most 1s, then re-read. This
            # releases the lock while waiting.
            with self._condition:
                self._condition.wait(timeout=min(remaining, 1.0))

    # ----------------------------------------------------------- heartbeat
    def start_heartbeat(self) -> None:
        if self.config.heartbeat_s <= 0:
            logger.info("heartbeat: disabled")
            return
        thread = threading.Thread(target=self._heartbeat_loop, daemon=True, name="heartbeat")
        thread.start()

    def stop_heartbeat(self) -> None:
        self._stop.set()

    def _heartbeat_loop(self) -> None:
        interval = max(5.0, self.config.heartbeat_s)
        while not self._stop.wait(interval):
            with self._lock:
                tracked = list(self._active)
            alive: list[str] = []
            for run_id in tracked:
                if self.is_active(run_id):
                    alive.append(run_id)
                    self.store.update(run_id, owner=self._owner())
                else:
                    # The run finished (or its thread died): stop tracking it.
                    with self._lock:
                        self._active.pop(run_id, None)
            logger.info(
                "heartbeat: active_runs=%d workers=%d",
                len(alive),
                len(self.workers.get()),
            )

    # ------------------------------------------------------- run's plan file
    def run_plan_file(self, run_id: str) -> Path | None:
        """The file holding a run's plan: its snapshot, else its source."""
        run = self.store.read(run_id)
        if run is None:
            return None
        snapshot = run.get("plan_snapshot")
        if snapshot and Path(snapshot).is_file():
            return Path(snapshot)
        source = run.get("plan_path")
        if source and Path(source).is_file():
            return Path(source)
        return None

    def update_run_plan(self, run_id: str, content: str) -> Path:
        """Apply a live plan edit to a run (snapshot), and to its editable source.

        Raises :class:`PlanError` when the new content is invalid.
        """
        run = self.store.read(run_id)
        if run is None:
            raise PlanError(f"run not found: {run_id}")
        target = self.run_plan_file(run_id)
        if target is None:
            raise PlanError("no plan file for this run")
        # The snapshot lives under the run dir, so relative prompt_file/files
        # paths must still resolve against the plan's original base directory.
        base_dir = Path(run["base_dir"]) if run.get("base_dir") else None
        write_plan(target, content, base_dir=base_dir)
        source = run.get("plan_path")
        if source and is_editable_plan(self.config.plans_dir, self.config.data_dir, Path(source)):
            if Path(source).resolve() != target.resolve():
                write_plan(Path(source), content, base_dir=base_dir)
        logger.info("run %s: plan updated (%s)", run_id, target)
        return target


def _transport_security(config: DashboardConfig) -> TransportSecuritySettings | None:
    """Build the MCP Host/Origin allowlist.

    With no explicit host the SDK keeps its localhost-only default, which is
    right for local development. As soon as a host or origin is configured we
    add the localhost entries so local access keeps working.
    """
    hosts = list(config.mcp_allowed_hosts)
    origins = list(config.mcp_allowed_origins)
    if not hosts and not origins:
        return None
    for host in ("127.0.0.1", "localhost", "[::1]"):
        for entry in (host, f"{host}:*"):
            if entry not in hosts:
                hosts.append(entry)
    return TransportSecuritySettings(allowed_hosts=hosts, allowed_origins=origins)


def create_app(config: DashboardConfig, manager: RunManager) -> FastAPI:
    mcp_server = create_mcp_server(manager, config)
    mcp_app = mcp_server.streamable_http_app(
        transport_security=_transport_security(config),
    )

    # Every response passes through this: clients and LLMs address plans, runs,
    # tasks and workers by name/id, never by the path they live at on disk.
    redact = make_redactor(config)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        manager.recover()
        manager.start_heartbeat()
        try:
            # A mounted sub-app's lifespan never runs, so the host owns the MCP
            # session manager (without this the first /mcp request fails).
            async with AsyncExitStack() as stack:
                await stack.enter_async_context(mcp_server.session_manager.run())
                yield
        finally:
            manager.stop_heartbeat()

    app = FastAPI(title="Hiveling dashboard", version="1.0.0", lifespan=lifespan)

    if config.mcp_token:
        app.add_middleware(BearerAuthMiddleware, token=config.mcp_token)

    # ----------------------------------------------------------------- helpers
    def resolve_plan_name(name: str) -> Path:
        try:
            return resolve_known_plan(config.plans_dir, name)
        except PlanError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ------------------------------------------------------------------- runs
    @app.get("/api/runs")
    def api_runs(
        q: str | None = None,
        status: str = "all",
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        return redact(
            manager.store.query(
                q=q, status=status, limit=max(1, min(limit, 200)), offset=offset
            )
        )

    @app.post("/api/runs", status_code=201)
    async def api_run_start(request: Request) -> dict:
        """Start a run.

        JSON body: ``{"plan": "demo.yaml"}`` to use an existing plan file, or
        ``{"plan_yaml": "<yaml content>", "name": "optional.yaml"}`` to push a
        plan. A raw YAML body (``Content-Type: application/x-yaml``) is also
        accepted. Optional ``only`` (list).
        """
        content_type = request.headers.get("content-type", "")
        body = await request.body()
        plan_name: str | None = None
        plan_yaml: str | None = None
        name: str | None = None
        only: list[str] = []

        if "json" in content_type:
            try:
                data = json.loads(body or b"{}")
            except json.JSONDecodeError as exc:
                raise HTTPException(status_code=400, detail=f"invalid JSON: {exc}") from exc
            plan_name = data.get("plan")
            plan_yaml = data.get("plan_yaml")
            name = data.get("name")
            only = [str(x) for x in (data.get("only") or [])]
        else:
            plan_name = request.query_params.get("plan")
            plan_yaml = body.decode("utf-8") if body else None
            name = request.query_params.get("name")
            only = request.query_params.getlist("only")

        if plan_yaml:
            try:
                plan_path = persist_pushed_plan(config.pushed_plans_dir, name, plan_yaml)
            except PlanError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        elif plan_name:
            plan_path = resolve_plan_name(plan_name)
            if not plan_path.is_file():
                raise HTTPException(status_code=404, detail="plan not found")
        else:
            raise HTTPException(status_code=400, detail="provide 'plan' or 'plan_yaml'")

        try:
            run_id = manager.start(plan_path, only=only or None)
        except PlanError as exc:
            if plan_yaml:
                plan_path.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"run_id": run_id}

    @app.get("/api/runs/{run_id}")
    def api_run_get(run_id: str) -> dict:
        run = manager.store.read(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        run["active"] = manager.is_active(run_id)
        # Cumulative tokens/cost across every task and retry (read before
        # redaction removes the tasks' history paths).
        run["usage"] = run_usage(run, config.history_dir)
        return redact(run)

    @app.post("/api/runs/{run_id}/cancel")
    def api_run_cancel(run_id: str) -> dict:
        if manager.store.read(run_id) is None:
            raise HTTPException(status_code=404, detail="run not found")
        return {"ok": manager.cancel(run_id)}

    @app.post("/api/runs/{run_id}/resume")
    def api_run_resume(run_id: str) -> dict:
        result = manager.resume_run(run_id)
        if not result.get("ok"):
            reason = result.get("reason", "")
            status = 404 if reason == "run not found" else 409
            raise HTTPException(status_code=status, detail=reason)
        return result

    @app.get("/api/runs/{run_id}/wait")
    def api_run_wait(run_id: str, timeout_s: float = 600.0, until: str = "terminal") -> dict:
        result = manager.wait_for_run(run_id, timeout_s=timeout_s, until=until)
        if result.get("reason") == "run not found":
            raise HTTPException(status_code=404, detail="run not found")
        if result.get("reason", "").startswith("until must be"):
            raise HTTPException(status_code=400, detail=result["reason"])
        return redact(result)

    @app.post("/api/runs/{run_id}/tasks/{task_id}/cancel")
    def api_task_cancel(run_id: str, task_id: str) -> dict:
        result = manager.cancel_task(run_id, task_id)
        if result.get("reason") == "run not found":
            raise HTTPException(status_code=404, detail="run not found")
        if result.get("reason") == "task not found":
            raise HTTPException(status_code=404, detail="task not found")
        return result

    # ------------------------------------------------------------ run's plan
    def run_plan_path(run_id: str) -> tuple[dict, Path]:
        run = manager.store.read(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        path = manager.run_plan_file(run_id)
        if path is None:
            raise HTTPException(status_code=404, detail="plan file no longer exists")
        return run, path

    @app.get("/api/runs/{run_id}/plan")
    def api_run_plan_get(run_id: str) -> dict:
        run, path = run_plan_path(run_id)
        base_dir = Path(run["base_dir"]) if run.get("base_dir") else None
        return {
            "name": path.name,
            "content": path.read_text(encoding="utf-8"),
            "summary": redact(plan_summary(path, base_dir=base_dir)),
        }

    @app.put("/api/runs/{run_id}/plan")
    def api_run_plan_put(run_id: str, body: SavePlanRequest) -> dict:
        """Edit a run's plan (live: applied between tasks; also writes the source)."""
        try:
            manager.update_run_plan(run_id, body.content)
        except PlanError as exc:
            detail = str(exc)
            status = 404 if detail.startswith("run not found") else 400
            raise HTTPException(status_code=status, detail=detail) from exc
        return {"ok": True}

    # ------------------------------------------------------------------ tasks
    def task_history_dir(run: dict, task_id: str) -> Path | None:
        for task in run.get("tasks", []):
            if task.get("id") == task_id:
                if task.get("history_rel"):
                    return config.history_dir / task["history_rel"]
                break
        return None

    @app.get("/api/runs/{run_id}/tasks/{task_id}")
    def api_task_get(run_id: str, task_id: str) -> dict:
        run = manager.store.read(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        state = next((t for t in run.get("tasks", []) if t.get("id") == task_id), None)
        if state is None:
            raise HTTPException(status_code=404, detail="task not found")
        directory = task_history_dir(run, task_id)
        detail: dict = {"task": state, "status": {}, "request": {}, "result": "",
                        "events": "", "event_lines": 0, "stderr": ""}
        if directory and directory.is_dir():
            detail["status"] = read_json(directory / "status.json")
            detail["request"] = read_json(directory / "request.json")
            result_path = directory / "result.txt"
            detail["result"] = result_path.read_text(encoding="utf-8") if result_path.is_file() else ""
            events, total = read_text(directory / "events.jsonl", EVENTS_TAIL_LINES)
            detail["events"] = events
            detail["event_lines"] = total
            stderr, _ = read_text(directory / "stderr.log", EVENTS_TAIL_LINES)
            detail["stderr"] = stderr
        # A task that has not started yet has no history/request.json; resolve
        # the prompt from the run's plan so it is visible before the job starts.
        if not detail["request"].get("prompt"):
            plan_file = manager.run_plan_file(run_id)
            if plan_file is not None:
                try:
                    plan = load_plan(plan_file, base_dir=run.get("base_dir"))
                    detail["request"]["prompt"] = plan.task_by_id(task_id).prompt
                except PlanError:
                    pass
        return redact(detail)

    @app.get("/api/runs/{run_id}/tasks/{task_id}/files")
    def api_task_files(run_id: str, task_id: str) -> Response:
        run = manager.store.read(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        directory = task_history_dir(run, task_id)
        if directory is None:
            raise HTTPException(status_code=404, detail="task not found")
        zip_path = directory / "files.zip"
        if zip_path.is_file():
            data = zip_path.read_bytes()
        elif (directory / "files").is_dir():
            data = zip_dir(directory / "files")
        else:
            raise HTTPException(status_code=404, detail="no files for this task")
        return Response(
            content=data,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{task_id}.zip"'},
        )

    @app.get("/api/runs/{run_id}/tasks/{task_id}/gate-input")
    def api_gate_input(run_id: str, task_id: str) -> Response:
        """Zip the decision material a gate read (its ``_hiveling/`` payload)."""
        run = manager.store.read(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        directory = task_history_dir(run, task_id)
        if directory is None:
            raise HTTPException(status_code=404, detail="task not found")
        gate_dir = directory / "gate-input" / "_hiveling"
        if not gate_dir.is_dir():
            raise HTTPException(status_code=404, detail="no gate input for this task")
        return Response(
            content=zip_dir(gate_dir),
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{task_id}-gate-input.zip"'
            },
        )

    # ----------------------------------------------------------------- plans
    @app.get("/api/plans")
    def api_plans() -> dict:
        """Stored plans the home page can start a run from."""
        plans = []
        for path in list_plan_files(config.plans_dir):
            summary = plan_summary(path)
            plans.append(
                {
                    "name": plan_name(config.plans_dir, path),
                    "editable": is_editable_plan(
                        config.plans_dir, config.data_dir, path
                    ),
                    "task_count": len(summary["tasks"]),
                    "error": summary["error"] or "",
                }
            )
        return redact({"plans": plans})

    # ---------------------------------------------------------------- workers
    @app.get("/api/workers")
    def api_workers() -> dict:
        data = probe_workers(manager.workers, WORKER_PROBE_TIMEOUT)
        return redact(data)

    # -------------------------------------------------------------- dashboard
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> str:
        return DASHBOARD_HTML

    # Deep links: the single-page app is served on every run/task/gate route so
    # a task or gate opens on its own page (not inline under the plan) and the
    # URL stays shareable.
    @app.get("/history", response_class=HTMLResponse)
    def history_page() -> str:
        return DASHBOARD_HTML

    @app.get("/run/{run_id}", response_class=HTMLResponse)
    def run_page(run_id: str) -> str:
        return DASHBOARD_HTML

    @app.get("/run/{run_id}/task/{task_id}", response_class=HTMLResponse)
    def task_page(run_id: str, task_id: str) -> str:
        return DASHBOARD_HTML

    @app.get("/run/{run_id}/gate/{gate_id}", response_class=HTMLResponse)
    def gate_page(run_id: str, gate_id: str) -> str:
        return DASHBOARD_HTML

    # ------------------------------------------------------------------- MCP
    # Mounted last so every API/dashboard route above wins; the MCP endpoint is
    # at /mcp. A mounted sub-app's lifespan never runs, hence the host lifespan.
    app.mount("/", mcp_app)

    return app

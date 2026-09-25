"""FastAPI application for the server: JSON API + dashboard.

It exposes the run history, live run state and lets the user cancel or retry
runs, view/edit the plan behind a run, and start runs by pushing a plan YAML
over HTTP. V1 has no authentication.
"""

from __future__ import annotations

import io
import json
import threading
import time
import uuid
import zipfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

from .dashboard_html import DASHBOARD_HTML
from .orchestrator import Orchestrator
from .plan import PlanError, WorkerEndpoint, load_plan
from .runs import RunStore, new_run_id
from .worker_client import WorkerClient, WorkerError

EVENTS_TAIL_LINES = 400
WORKER_PROBE_TIMEOUT = 2.0


@dataclass
class DashboardConfig:
    data_dir: Path
    plans_dir: Path
    poll_interval: float = 2.0
    worker_wait_timeout: float = 1800.0

    @property
    def history_dir(self) -> Path:
        return self.data_dir / "history"

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    @property
    def pushed_plans_dir(self) -> Path:
        return self.data_dir / "plans"


class SavePlanRequest(BaseModel):
    content: str


class RunManager:
    """Starts runs in background threads and tracks their cancel events."""

    def __init__(self, config: DashboardConfig):
        self.config = config
        self.store = RunStore(config.runs_dir)
        self._active: dict[str, dict] = {}
        self._lock = threading.Lock()

    def start(
        self,
        plan_path: Path,
        *,
        only: list[str] | None = None,
        keep_going: bool = False,
        inputs_run_id: str | None = None,
        assume_deps_ok: bool = False,
    ) -> str:
        plan = load_plan(plan_path)  # raises PlanError
        run_id = new_run_id()
        cancel_event = threading.Event()
        orchestrator = Orchestrator(
            plan,
            history_dir=self.config.history_dir,
            poll_interval=self.config.poll_interval,
            keep_going=keep_going,
            worker_wait_timeout=self.config.worker_wait_timeout,
            only=only,
            run_store=self.store,
            run_id=run_id,
            cancel_event=cancel_event,
            inputs_run_id=inputs_run_id,
            assume_deps_ok=assume_deps_ok,
            quiet=True,
        )
        thread = threading.Thread(target=orchestrator.run, daemon=True)
        with self._lock:
            self._active[run_id] = {"event": cancel_event, "thread": thread}
        thread.start()
        return run_id

    def cancel(self, run_id: str) -> bool:
        with self._lock:
            entry = self._active.get(run_id)
        if entry is None:
            return False
        entry["event"].set()
        return True

    def is_active(self, run_id: str) -> bool:
        with self._lock:
            entry = self._active.get(run_id)
        return entry is not None and entry["thread"].is_alive()

    def reconcile(self) -> None:
        """Mark runs left 'running' by a previous server process as interrupted."""
        for summary in self.store.list(limit=1000):
            if summary.get("status") != "running":
                continue
            run = self.store.read(summary["run_id"])
            if run is None:
                continue
            now = time.time()
            for task in run.get("tasks", []):
                if task.get("status") == "running":
                    task.update(status="failed", error="server restarted during the run",
                                finished_at=now)
                elif task.get("status") == "pending":
                    task.update(status="canceled", finished_at=now)
            run["status"] = "failed"
            run["error"] = "server restarted during the run"
            run["finished_at"] = now
            self.store.write(run)


def _read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _read_text(path: Path, tail_lines: int | None = None) -> tuple[str, int]:
    if not path.is_file():
        return "", 0
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "", 0
    lines = text.splitlines()
    total = len(lines)
    if tail_lines is not None and total > tail_lines:
        text = "\n".join(lines[-tail_lines:])
    return text, total


def _zip_dir(directory: Path) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(directory).as_posix())
    return buffer.getvalue()


def _safe_file_name(name: str) -> str:
    cleaned = "".join(c if (c.isalnum() or c in "._-") else "-" for c in name).strip(".-")
    return cleaned or "plan"


def create_app(config: DashboardConfig, manager: RunManager) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        manager.reconcile()
        yield

    app = FastAPI(title="Hiveling dashboard", version="1.0.0", lifespan=lifespan)

    # ----------------------------------------------------------------- helpers
    def plans_dir() -> Path:
        return config.plans_dir.resolve()

    def resolve_known_plan(name: str) -> Path:
        candidate = (plans_dir() / name).resolve()
        base = plans_dir()
        if candidate != base and base not in candidate.parents:
            raise HTTPException(status_code=400, detail="invalid plan path")
        if candidate.suffix not in (".yaml", ".yml"):
            raise HTTPException(status_code=400, detail="plan must be a .yaml/.yml file")
        return candidate

    def list_plan_files() -> list[Path]:
        base = plans_dir()
        if not base.exists():
            return []
        return sorted(p for p in base.rglob("*") if p.suffix in (".yaml", ".yml") and p.is_file())

    def plan_summary(path: Path) -> dict:
        try:
            plan = load_plan(path)
            return {
                "workers": [{"name": w.name, "url": w.url} for w in plan.workers],
                "tasks": [
                    {
                        "id": t.id,
                        "depends_on": t.depends_on,
                        "inputs_from": t.inputs_from,
                        "model": t.model,
                    }
                    for t in plan.tasks
                ],
                "error": None,
            }
        except PlanError as exc:
            return {"workers": [], "tasks": [], "error": str(exc)}

    def is_editable_plan(path: Path) -> bool:
        resolved = path.resolve()
        for base in (plans_dir(), config.data_dir.resolve()):
            if resolved == base or base in resolved.parents:
                return True
        return False

    # ------------------------------------------------------------------- runs
    @app.get("/api/runs")
    def api_runs(
        q: str | None = None,
        status: str = "all",
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        return manager.store.query(q=q, status=status, limit=max(1, min(limit, 200)), offset=offset)

    @app.post("/api/runs", status_code=201)
    async def api_run_start(request: Request) -> dict:
        """Start a run.

        JSON body: ``{"plan": "demo.yaml"}`` to use an existing plan file, or
        ``{"plan_yaml": "<yaml content>", "name": "optional.yaml"}`` to push a
        plan. A raw YAML body (``Content-Type: application/x-yaml``) is also
        accepted. Optional ``only`` (list) and ``keep_going`` (bool).
        """
        content_type = request.headers.get("content-type", "")
        body = await request.body()
        plan_name: str | None = None
        plan_yaml: str | None = None
        name: str | None = None
        only: list[str] = []
        keep_going = False

        if "json" in content_type:
            try:
                data = json.loads(body or b"{}")
            except json.JSONDecodeError as exc:
                raise HTTPException(status_code=400, detail=f"invalid JSON: {exc}") from exc
            plan_name = data.get("plan")
            plan_yaml = data.get("plan_yaml")
            name = data.get("name")
            only = [str(x) for x in (data.get("only") or [])]
            keep_going = bool(data.get("keep_going"))
        else:
            plan_name = request.query_params.get("plan")
            plan_yaml = body.decode("utf-8") if body else None
            name = request.query_params.get("name")
            only = request.query_params.getlist("only")
            keep_going = request.query_params.get("keep_going", "").lower() in ("1", "true", "yes")

        if plan_yaml:
            try:
                plan_path = _persist_pushed_plan(config, name, plan_yaml)
            except PlanError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        elif plan_name:
            plan_path = resolve_known_plan(plan_name)
            if not plan_path.is_file():
                raise HTTPException(status_code=404, detail="plan not found")
        else:
            raise HTTPException(status_code=400, detail="provide 'plan' or 'plan_yaml'")

        try:
            run_id = manager.start(plan_path, only=only or None, keep_going=keep_going)
        except PlanError as exc:
            if plan_yaml:
                plan_path.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"run_id": run_id, "plan_path": str(plan_path)}

    @app.get("/api/runs/{run_id}")
    def api_run_get(run_id: str) -> dict:
        run = manager.store.read(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        run["active"] = manager.is_active(run_id)
        return run

    @app.post("/api/runs/{run_id}/cancel")
    def api_run_cancel(run_id: str) -> dict:
        if manager.store.read(run_id) is None:
            raise HTTPException(status_code=404, detail="run not found")
        return {"ok": manager.cancel(run_id)}

    @app.post("/api/runs/{run_id}/tasks/{task_id}/retry")
    def api_task_retry(run_id: str, task_id: str) -> dict:
        run = manager.store.read(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        plan_path = Path(run["plan_path"])
        if not plan_path.is_file():
            raise HTTPException(status_code=400, detail=f"plan no longer exists: {plan_path}")
        try:
            new_id = manager.start(
                plan_path,
                only=[task_id],
                keep_going=True,
                inputs_run_id=run_id,
                assume_deps_ok=True,
            )
        except PlanError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"run_id": new_id}

    # ------------------------------------------------------------ run's plan
    def run_plan_path(run_id: str) -> tuple[dict, Path]:
        run = manager.store.read(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        path = Path(run["plan_path"])
        if not path.is_file():
            raise HTTPException(status_code=404, detail="plan file no longer exists")
        if not is_editable_plan(path):
            raise HTTPException(status_code=403, detail="plan is not editable from the dashboard")
        return run, path

    @app.get("/api/runs/{run_id}/plan")
    def api_run_plan_get(run_id: str) -> dict:
        _, path = run_plan_path(run_id)
        return {
            "name": path.name,
            "path": str(path),
            "content": path.read_text(encoding="utf-8"),
            "summary": plan_summary(path),
        }

    @app.put("/api/runs/{run_id}/plan")
    def api_run_plan_put(run_id: str, body: SavePlanRequest) -> dict:
        _, path = run_plan_path(run_id)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(body.content, encoding="utf-8")
        try:
            load_plan(tmp)
        except PlanError as exc:
            tmp.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        tmp.replace(path)
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
            detail["status"] = _read_json(directory / "status.json")
            detail["request"] = _read_json(directory / "request.json")
            result_path = directory / "result.txt"
            detail["result"] = result_path.read_text(encoding="utf-8") if result_path.is_file() else ""
            events, total = _read_text(directory / "events.jsonl", EVENTS_TAIL_LINES)
            detail["events"] = events
            detail["event_lines"] = total
            stderr, _ = _read_text(directory / "stderr.log", EVENTS_TAIL_LINES)
            detail["stderr"] = stderr
        return detail

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
            data = _zip_dir(directory / "files")
        else:
            raise HTTPException(status_code=404, detail="no files for this task")
        return Response(
            content=data,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{task_id}.zip"'},
        )

    # ---------------------------------------------------------------- workers
    def known_workers() -> list[dict]:
        seen: dict[str, dict] = {}
        paths = list(list_plan_files())
        for summary in manager.store.list(limit=500):
            run = manager.store.read(summary["run_id"])
            if run and run.get("plan_path"):
                candidate = Path(run["plan_path"])
                if candidate.is_file():
                    paths.append(candidate)
        for path in paths:
            try:
                plan = load_plan(path)
            except PlanError:
                continue
            for worker in plan.workers:
                seen.setdefault(worker.url, {"name": worker.name, "url": worker.url})
        return list(seen.values())

    @app.get("/api/workers")
    def api_workers() -> dict:
        results = []
        for worker in known_workers():
            entry = {"name": worker["name"], "url": worker["url"],
                     "reachable": False, "busy": None, "active_job": None, "opencode_bin": None}
            client = WorkerClient(
                WorkerEndpoint(name=worker["name"], url=worker["url"]),
                timeout=WORKER_PROBE_TIMEOUT,
            )
            try:
                health = client.health()
                entry.update(
                    reachable=True,
                    busy=health.get("busy"),
                    active_job=health.get("active_job"),
                    opencode_bin=health.get("opencode_bin"),
                )
            except WorkerError:
                pass
            finally:
                client.close()
            results.append(entry)
        return {"workers": results, "data_dir": str(config.data_dir)}

    # -------------------------------------------------------------- dashboard
    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> str:
        return DASHBOARD_HTML

    return app


def _persist_pushed_plan(config: DashboardConfig, name: str | None, content: str) -> Path:
    """Validate a pushed plan and store it under the data directory."""
    directory = config.pushed_plans_dir
    directory.mkdir(parents=True, exist_ok=True)
    if name:
        base = _safe_file_name(name)
        if not base.endswith((".yaml", ".yml")):
            base += ".yaml"
    else:
        base = f"pushed-{time.strftime('%Y-%m-%dT%H-%M-%S')}-{uuid.uuid4().hex[:4]}.yaml"
    target = directory / base
    target.write_text(content, encoding="utf-8")
    try:
        load_plan(target)
    except PlanError:
        target.unlink(missing_ok=True)
        raise
    return target

"""Model Context Protocol server exposed as a remote (Streamable HTTP) endpoint.

The MCP tools are a thin wrapper over the orchestrator's :class:`RunManager` and
:class:`RunStore`, i.e. the same in-process objects the JSON API and the
dashboard use. Nothing is duplicated: plans, runs and tasks are read and
mutated through the shared service layer.

The tools are deliberately:
- **non-blocking**: ``run_plan`` starts a run in a background thread and returns
  a ``run_id`` immediately; the caller polls ``get_run``;
- **model-friendly**: expected failures raise ``ToolError`` so the model can
  read the message and adapt.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError

from .plan import PlanError
from .plan_files import (
    is_editable_plan,
    list_plan_files,
    persist_pushed_plan,
    plan_name,
    plan_summary,
    resolve_known_plan,
    safe_file_name,
    write_plan,
)
from .plan_schema import PLAN_EXAMPLE, PLAN_REFERENCE, PlanInput, plan_to_yaml
from .readers import read_json, read_text
from .redact import make_redactor
from .workers import probe_workers

EVENTS_TAIL_LINES = 200

INSTRUCTIONS = """\
Hiveling orchestrates a plan.yaml across one or more remote OpenCode workers.

Workers are NOT declared in a plan: they are configured once in workers.yaml
(default `<data-dir>/workers.yaml`). Changes to that file are picked up
automatically; use list_workers to see the current workers and their health.

Typical flow:
1. list_plans to discover plans, or run_plan with an inline plan_yaml.
2. run_plan returns a run_id immediately (execution is asynchronous).
3. wait_for_run(run_id) blocks until the run finishes (or use get_run / get_task
   to follow progress). Tasks end in succeeded, failed, canceled or skipped.
4. A run stops at the first failed task (fail fast): no new task is started,
   but tasks already running finish before the run is reported terminal; the
   remaining tasks are marked skipped. Analyse the failure, adjust with
   update_run_plan if needed, then resume_run to re-run the failed and skipped
   tasks and continue the run.
5. cancel_run stops a whole run, cancel_task cancels a single task (running
   tasks are stopped, pending tasks never run, and the run continues).
6. get_plan / update_plan inspect or edit a stored plan. To edit the plan of a
   run (even while it runs), use get_run_plan / update_run_plan.

A run is a DAG: tasks run in parallel, one per free worker, in plan/dependency
order; a task whose dependencies did not succeed is skipped.

To CREATE or EDIT a plan, learn its format first: call get_plan_schema (or
read the resource hiveling://schema/plan) for the full reference and a canonical
example. Prefer create_plan, whose argument is a structured plan object; use
update_plan only to replace a plan with raw YAML.
"""


def create_mcp_server(manager, config) -> MCPServer:
    """Build the Hiveling MCP server bound to a ``RunManager``/``DashboardConfig``."""
    mcp = MCPServer(
        "hiveling",
        title="Hiveling orchestrator",
        instructions=INSTRUCTIONS,
    )

    # Clients and LLMs address plans, runs, tasks and workers by name/id. The
    # path where the server/workers keep them on disk is never returned.
    redact = make_redactor(config)

    # ------------------------------------------------------------------ helpers
    def history_dir_for(run: dict, task_id: str) -> Path | None:
        for task in run.get("tasks", []):
            if task.get("id") == task_id:
                if task.get("history_rel"):
                    return config.history_dir / task["history_rel"]
                break
        return None

    def start_run(
        plan_path: Path,
        *,
        only: list[str] | None = None,
    ) -> str:
        try:
            return manager.start(
                plan_path,
                only=only or None,
            )
        except PlanError as exc:
            raise ToolError(str(exc)) from exc

    # -------------------------------------------------------------------- plans
    @mcp.tool()
    def list_plans() -> dict[str, Any]:
        """List the plan files available on the server.

        Use the returned ``name`` with run_plan or get_plan.
        """
        plans = []
        for path in list_plan_files(config.plans_dir):
            summary = plan_summary(path)
            plans.append(
                {
                    "name": plan_name(config.plans_dir, path),
                    "editable": is_editable_plan(config.plans_dir, config.data_dir, path),
                    "task_count": len(summary["tasks"]),
                    "error": redact.text(summary["error"] or ""),
                }
            )
        return {"plans": plans}

    @mcp.tool()
    def get_plan(name: str) -> dict[str, Any]:
        """Return the YAML content and a summary of a stored plan.

        ``name`` is a plan name from list_plans (relative to the plans dir).
        """
        try:
            path = resolve_known_plan(config.plans_dir, name)
        except PlanError as exc:
            raise ToolError(str(exc)) from exc
        if not path.is_file():
            raise ToolError(f"plan not found: {name}")
        return {
            "name": plan_name(config.plans_dir, path),
            "content": path.read_text(encoding="utf-8"),
            "summary": redact(plan_summary(path)),
        }

    @mcp.tool()
    def update_plan(name: str, content: str) -> dict[str, Any]:
        """Replace a stored plan with new YAML content.

        The content is validated before being written. Editing a plan does not
        affect a run already in flight: it applies to the next run.
        """
        try:
            path = resolve_known_plan(config.plans_dir, name)
        except PlanError as exc:
            raise ToolError(str(exc)) from exc
        if not path.is_file():
            raise ToolError(f"plan not found: {name}")
        if not is_editable_plan(config.plans_dir, config.data_dir, path):
            raise ToolError(f"plan is not editable: {name}")
        try:
            write_plan(path, content)
        except PlanError as exc:
            raise ToolError(str(exc)) from exc
        return {"ok": True, "name": plan_name(config.plans_dir, path)}

    @mcp.tool()
    def get_plan_schema() -> dict[str, Any]:
        """Return the full plan.yaml reference and a canonical example.

        Call this before creating or editing a plan if you are unsure of the
        format. The same reference is available as the resource
        ``hiveling://schema/plan`` and the example as ``hiveling://example/plan``.
        """
        return {
            "format": "plan.yaml",
            "version": 1,
            "reference": PLAN_REFERENCE,
            "example": PLAN_EXAMPLE,
        }

    @mcp.tool()
    def create_plan(name: str, plan: PlanInput, overwrite: bool = False) -> dict[str, Any]:
        """Create (or overwrite) a stored plan from a structured definition.

        This is the preferred way to author a plan: ``plan`` is a typed object
        whose shape is published in this tool's input schema, so you can fill it
        in field by field. Workers are not part of a plan (they come from
        workers.yaml). The generated YAML is validated before being written,
        and returned along with a summary. Pass ``overwrite=true`` to replace an
        existing plan. Use get_plan_schema for prose documentation and an
        example.
        """
        target = safe_file_name(name)
        if not target.endswith((".yaml", ".yml")):
            target += ".yaml"
        try:
            path = resolve_known_plan(config.plans_dir, target)
        except PlanError as exc:
            raise ToolError(str(exc)) from exc
        if path.exists() and not overwrite:
            raise ToolError(f"plan already exists: {target} (pass overwrite=true to replace it)")

        content = plan_to_yaml(plan)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            write_plan(path, content)
        except PlanError as exc:
            raise ToolError(str(exc)) from exc
        return {
            "ok": True,
            "name": plan_name(config.plans_dir, path),
            "content": content,
            "summary": redact(plan_summary(path)),
        }

    @mcp.tool()
    def get_run_plan(run_id: str) -> dict[str, Any]:
        """Return the plan a run is executing (its snapshot) and a summary.

        This is the plan used by the run, which may differ from the stored plan
        of the same name after a live edit.
        """
        run = manager.store.read(run_id)
        if run is None:
            raise ToolError(f"run not found: {run_id}")
        path = manager.run_plan_file(run_id)
        if path is None:
            raise ToolError(f"no plan file for run: {run_id}")
        base_dir = Path(run["base_dir"]) if run.get("base_dir") else None
        return {
            "run_id": run_id,
            "name": path.name,
            "content": path.read_text(encoding="utf-8"),
            "summary": redact(plan_summary(path, base_dir=base_dir)),
        }

    @mcp.tool()
    def update_run_plan(run_id: str, content: str) -> dict[str, Any]:
        """Edit the plan of a run, including while it is running.

        The new YAML is validated then applied between tasks: pending tasks are
        updated, new tasks are added and scheduled, and pending tasks removed
        from the plan are canceled. Tasks already running or finished are left
        untouched. When the run's source plan is editable, it is updated too, so
        future runs see the change. Use update_plan to edit a stored plan that is
        not tied to a run.
        """
        try:
            manager.update_run_plan(run_id, content)
        except PlanError as exc:
            raise ToolError(str(exc)) from exc
        return {"ok": True, "run_id": run_id}

    # --------------------------------------------------------------------- runs
    @mcp.tool()
    def run_plan(
        plan: str | None = None,
        plan_yaml: str | None = None,
        name: str | None = None,
        only: list[str] | None = None,
    ) -> dict[str, Any]:
        """Start a plan. Returns a run_id immediately (execution is asynchronous).

        Provide either ``plan`` (a name from list_plans) or ``plan_yaml`` (raw
        YAML; optional ``name`` to store it). ``only`` restricts execution to
        the listed task ids. Execution stops at the first failed task; use
        wait_for_run then resume_run to continue after an adjustment.

        For the YAML format call get_plan_schema; to author a new plan prefer
        create_plan, then run it by name.
        """
        if plan_yaml:
            try:
                plan_path = persist_pushed_plan(config.pushed_plans_dir, name, plan_yaml)
            except PlanError as exc:
                raise ToolError(str(exc)) from exc
        elif plan:
            try:
                plan_path = resolve_known_plan(config.plans_dir, plan)
            except PlanError as exc:
                raise ToolError(str(exc)) from exc
            if not plan_path.is_file():
                raise ToolError(f"plan not found: {plan}")
        else:
            raise ToolError("provide 'plan' (a name from list_plans) or 'plan_yaml'")

        run_id = start_run(plan_path, only=only)
        return {"run_id": run_id, "status": "started"}

    @mcp.tool()
    def list_runs(
        q: str | None = None,
        status: str = "all",
        limit: int = 25,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List runs, newest first.

        ``q`` searches the plan name and run id; ``status`` filters on run
        status (``all``, ``running``, ``succeeded``, ``failed``, ``canceled``).
        """
        return redact(
            manager.store.query(
                q=q, status=status, limit=max(1, min(limit, 200)), offset=max(0, offset)
            )
        )

    @mcp.tool()
    def get_run(run_id: str) -> dict[str, Any]:
        """Return a run's status plus the state of every task.

        Poll this to follow a run started with run_plan.
        """
        run = manager.store.read(run_id)
        if run is None:
            raise ToolError(f"run not found: {run_id}")
        run["active"] = manager.is_active(run_id)
        return redact(run)

    @mcp.tool()
    def get_task(run_id: str, task_id: str, events_tail_lines: int = 50) -> dict[str, Any]:
        """Return one task's detail: status, result, error and changed files.

        ``events_tail_lines`` limits the OpenCode event log returned (0 for
        none); the full log stays available in the dashboard.
        """
        run = manager.store.read(run_id)
        if run is None:
            raise ToolError(f"run not found: {run_id}")
        state = next((t for t in run.get("tasks", []) if t.get("id") == task_id), None)
        if state is None:
            raise ToolError(f"task not found: {task_id}")

        detail: dict[str, Any] = {
            "task": state,
            "status": {},
            "request": {},
            "result": "",
            "events": "",
            "event_lines": 0,
            "stderr": "",
            "commits": list(state.get("commits") or []),
            "artifacts": list(state.get("artifacts") or []),
            "merge": dict(state.get("merge") or {}),
        }
        directory = history_dir_for(run, task_id)
        if directory and directory.is_dir():
            detail["status"] = read_json(directory / "status.json")
            detail["request"] = read_json(directory / "request.json")
            if detail["status"].get("commits"):
                detail["commits"] = detail["status"]["commits"]
            if detail["status"].get("artifacts"):
                detail["artifacts"] = detail["status"]["artifacts"]
            if detail["status"].get("merge"):
                detail["merge"] = detail["status"]["merge"]
            result_path = directory / "result.txt"
            detail["result"] = (
                result_path.read_text(encoding="utf-8") if result_path.is_file() else ""
            )
            tail = None if events_tail_lines <= 0 else events_tail_lines
            events, total = read_text(directory / "events.jsonl", tail)
            detail["events"] = events
            detail["event_lines"] = total
            stderr, _ = read_text(directory / "stderr.log", tail)
            detail["stderr"] = stderr
        return redact(detail)

    @mcp.tool()
    def cancel_run(run_id: str) -> dict[str, Any]:
        """Cancel a whole run.

        Every running task's worker job is canceled and pending tasks are
        marked canceled.
        """
        if manager.store.read(run_id) is None:
            raise ToolError(f"run not found: {run_id}")
        return {"run_id": run_id, "ok": manager.cancel(run_id)}

    @mcp.tool()
    def cancel_task(run_id: str, task_id: str) -> dict[str, Any]:
        """Cancel a single task of a run.

        If the task is running, its worker job is terminated; if it is pending,
        it is marked canceled and never started. Dependent tasks are then
        skipped (their dependency did not succeed) and the run continues with
        the remaining tasks. This does NOT stop the whole run — use cancel_run
        for that.
        """
        result = manager.cancel_task(run_id, task_id)
        if not result.get("ok"):
            raise ToolError(f"cannot cancel task {task_id} in run {run_id}: {result.get('reason')}")
        return {"run_id": run_id, "task_id": task_id, "status": "canceled"}

    # ------------------------------------------------------------ waiting
    @mcp.tool()
    def wait_for_run(run_id: str, timeout_s: float = 600.0, until: str = "terminal") -> dict[str, Any]:
        """Block until a run finishes (or changes), then return its state.

        ``until="terminal"`` (default) returns when the run reaches succeeded,
        failed or canceled *and* every in-flight task has finished, so a
        failure never returns while other tasks are still running.
        ``until="change"`` returns after any state change. ``timeout_s`` bounds
        the wait; on timeout the current state is returned with
        ``timed_out=true``. Use this instead of polling get_run.
        """
        result = manager.wait_for_run(run_id, timeout_s=timeout_s, until=until)
        if result.get("reason") == "run not found":
            raise ToolError(f"run not found: {run_id}")
        if str(result.get("reason", "")).startswith("until must be"):
            raise ToolError(result["reason"])
        return redact(result)

    @mcp.tool()
    def resume_run(run_id: str) -> dict[str, Any]:
        """Continue a finished run by re-running its failed and skipped tasks.

        The run keeps its succeeded tasks and re-reads its plan snapshot (so
        live edits apply), then continues from where it stopped. Use it to
        retry after an orchestrator has adjusted the plan. The previous error is
        preserved in each task's ``last_error``.
        """
        result = manager.resume_run(run_id)
        if not result.get("ok"):
            raise ToolError(f"cannot resume run {run_id}: {result.get('reason')}")
        return redact(result)

    # ------------------------------------------------------------------ workers
    @mcp.tool()
    def list_workers() -> dict[str, Any]:
        """Return the configured workers and whether they are reachable/busy.

        Workers come from ``workers.yaml`` (not from plans). The file is reloaded
        automatically when it changes, so an added worker shows up without a
        server restart.
        """
        data = probe_workers(manager.workers)
        return redact(data)

    # --------------------------------------------------------------- resources
    @mcp.resource(
        "hiveling://schema/plan",
        name="plan-schema",
        title="plan.yaml reference",
        description="Full plan.yaml format reference (same as get_plan_schema).",
        mime_type="text/markdown",
    )
    def plan_schema_resource() -> str:
        return PLAN_REFERENCE

    @mcp.resource(
        "hiveling://example/plan",
        name="plan-example",
        title="Example plan",
        description="A canonical plan.yaml example to copy from.",
        mime_type="text/yaml",
    )
    def plan_example_resource() -> str:
        return PLAN_EXAMPLE

    @mcp.resource(
        "hiveling://plans/{name}",
        name="stored-plan",
        title="Stored plan",
        description="YAML content of a stored plan (name relative to the plans dir).",
        mime_type="text/yaml",
    )
    def stored_plan_resource(name: str) -> str:
        try:
            path = resolve_known_plan(config.plans_dir, name)
        except PlanError as exc:
            raise ResourceNotFoundError(str(exc)) from exc
        if not path.is_file():
            raise ResourceNotFoundError(f"plan not found: {name}")
        return path.read_text(encoding="utf-8")

    return mcp


class BearerAuthMiddleware:
    """Minimal pure-ASGI bearer-token guard for the MCP endpoint.

    Implemented at the ASGI level (not ``BaseHTTPMiddleware``) so it never
    buffers the Streamable HTTP response streams.
    """

    def __init__(self, app, *, token: str, path_prefix: str = "/mcp"):
        self.app = app
        self.token = token
        self.path_prefix = path_prefix

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and scope["path"].startswith(self.path_prefix):
            headers = scope.get("headers") or []
            supplied = ""
            for key, value in headers:
                if key == b"authorization":
                    supplied = value.decode("latin-1")
                    break
            if supplied != f"Bearer {self.token}":
                from starlette.responses import JSONResponse

                response = JSONResponse(
                    {"error": "unauthorized"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)

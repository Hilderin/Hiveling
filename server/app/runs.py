"""Persistent state for orchestration runs, used by the dashboard.

Each run is stored as ``<runs_dir>/<run_id>/run.json`` and holds the overall
status plus a list of task states. Task artifacts themselves stay in the
history directory (``<history_dir>/<task_id>/<run_id>/``).
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")

TERMINAL_RUN_STATUSES = {"succeeded", "failed", "canceled"}


def safe_name(name: str) -> str:
    cleaned = _SAFE.sub("_", name).strip("._")
    return cleaned or "task"


def new_run_id() -> str:
    return time.strftime("%Y-%m-%dT%H-%M-%S") + "-" + uuid.uuid4().hex[:4]


def build_task_states(plan, only: list[str] | None = None) -> list[dict]:
    """Build the initial task states for a run from a plan."""
    only_set = set(only) if only else None
    states: list[dict] = []
    for task in plan.tasks:
        if only_set is not None and task.id not in only_set:
            continue
        states.append(
            {
                "id": task.id,
                "kind": getattr(task, "kind", "task"),
                "status": "pending",
                "model": task.model,
                "agent": task.agent,
                "depends_on": list(task.depends_on),
                "inputs_from": list(task.inputs_from),
                "files_spec": list(task.files),
                "requirements": dict(task.requirements or {}),
                "resources": [dict(resource) for resource in (task.resources or [])],
                "download": task.download,
                "history_rel": None,
                "worker": None,
                "worker_url": None,
                "job_id": None,
                "duration_s": None,
                "error": None,
                "last_error": None,
                "skip_reason": None,
                "attempts": 0,
                "changed_files": [],
                "commits": [],
                "artifacts": [],
                "merge": {},
                # Gate-only fields (absent/zero for plain tasks).
                "gate_targets": list(getattr(task, "gate_targets", []) or []),
                "gate_max_attempts": getattr(task, "gate_max_attempts", 0) or 0,
                "gate_attempt": 0,
                "gate_feedback": None,
                "gate_verdict": None,
            }
        )
    return states


def _counts(tasks: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for task in tasks:
        status = task.get("status", "pending")
        counts[status] = counts.get(status, 0) + 1
    return counts


class RunStore:
    def __init__(self, root: Path):
        self.root = root
        self._lock = threading.RLock()

    def path(self, run_id: str) -> Path:
        return self.root / safe_name(run_id) / "run.json"

    def write(self, run: dict) -> None:
        path = self.path(run["run_id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(run, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    def read(self, run_id: str) -> dict | None:
        path = self.path(run_id)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def create(
        self,
        run_id: str,
        *,
        plan_path: str,
        plan_name: str,
        only: list[str] | None,
        tasks: list[dict],
        inputs_run_id: str | None = None,
        plan_snapshot: str | None = None,
        base_dir: str | None = None,
        owner: dict | None = None,
    ) -> dict:
        now = time.time()
        run = {
            "run_id": run_id,
            "plan_path": plan_path,
            "plan_name": plan_name,
            # Immutable copy of the plan this run executes; recovery and live
            # edits read and write this file, never the original.
            "plan_snapshot": plan_snapshot,
            # Directory relative paths in the plan resolve against.
            "base_dir": base_dir,
            "owner": owner,
            "cancel_requested": False,
            "only": only or [],
            "inputs_run_id": inputs_run_id,
            "status": "running",
            "attempts": 1,
            "resumed_at": None,
            "created_at": now,
            "started_at": now,
            "finished_at": None,
            "error": None,
            "tasks": tasks,
        }
        with self._lock:
            self.write(run)
        return run

    def update(self, run_id: str, **fields) -> dict | None:
        with self._lock:
            run = self.read(run_id)
            if run is None:
                return None
            run.update(fields)
            self.write(run)
            return run

    def update_task(self, run_id: str, task_id: str, **fields) -> dict | None:
        with self._lock:
            run = self.read(run_id)
            if run is None:
                return None
            for task in run.get("tasks", []):
                if task.get("id") == task_id:
                    task.update(fields)
                    break
            self.write(run)
            return run

    def update_from(self, run_id: str, mutate) -> dict | None:
        """Atomically apply ``mutate(run)`` and persist the result.

        The whole read-modify-write happens under the store lock, so callers
        can safely reconcile a run while task threads update individual tasks.
        """
        with self._lock:
            run = self.read(run_id)
            if run is None:
                return None
            mutate(run)
            self.write(run)
            return run

    def list(self, limit: int = 100) -> list[dict]:
        runs: list[dict] = []
        if not self.root.exists():
            return runs
        with self._lock:
            for path in self.root.glob("*/run.json"):
                try:
                    run = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                runs.append(self._summarize(run))
        runs.sort(key=lambda r: r.get("created_at") or 0, reverse=True)
        return runs[:limit]

    def query(
        self,
        *,
        q: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """Return a page of run summaries with optional search and status filter."""
        runs = self.list(limit=1_000_000)
        if status == "active":
            runs = [r for r in runs if r.get("status") == "running"]
        elif status and status != "all":
            runs = [r for r in runs if r.get("status") == status]
        if q:
            needle = q.lower()
            runs = [
                r
                for r in runs
                if needle in (r.get("plan_name") or "").lower()
                or needle in (r.get("run_id") or "").lower()
            ]
        total = len(runs)
        offset = max(0, offset)
        return {
            "runs": runs[offset : offset + limit],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    @staticmethod
    def _summarize(run: dict) -> dict:
        tasks = run.get("tasks", [])
        return {
            "run_id": run.get("run_id"),
            "plan_name": run.get("plan_name"),
            "plan_path": run.get("plan_path"),
            "plan_snapshot": run.get("plan_snapshot"),
            "status": run.get("status"),
            "created_at": run.get("created_at"),
            "started_at": run.get("started_at"),
            "finished_at": run.get("finished_at"),
            "only": run.get("only") or [],
            "attempts": run.get("attempts", 1),
            "resumed_at": run.get("resumed_at"),
            "cancel_requested": bool(run.get("cancel_requested")),
            "counts": _counts(tasks),
            "task_count": len(tasks),
        }

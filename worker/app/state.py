"""In-memory job state with on-disk persistence.

V1: only one active job at a time. Each job lives in
``<workspace>/<job_id>/`` and has a ``status.json`` file that the server can
read (and that is handy for debugging).
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TERMINAL_STATUSES = {"succeeded", "failed", "canceled"}
ACTIVE_STATUSES = {"accepted", "running"}


@dataclass
class Job:
    job_id: str
    dir: Path
    spec: dict = field(default_factory=dict)
    status: str = "accepted"
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    exit_code: int | None = None
    error: str | None = None
    result_text: str = ""
    session_id: str | None = None
    tool_calls: list[str] = field(default_factory=list)
    tokens: dict | None = None
    cost: float | None = None
    added: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    commits: list = field(default_factory=list)
    artifacts: list = field(default_factory=list)
    cancel_requested: bool = False
    shutdown_requested: bool = False
    process: Any = None
    thread: Any = None

    @property
    def workdir(self) -> Path:
        return self.dir / "work"

    def to_status(self) -> dict:
        duration = None
        if self.started_at is not None and self.finished_at is not None:
            duration = round(self.finished_at - self.started_at, 3)
        return {
            "job_id": self.job_id,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_s": duration,
            "exit_code": self.exit_code,
            "error": self.error,
            "result_text": self.result_text,
            "session_id": self.session_id,
            "tool_calls": self.tool_calls,
            "tokens": self.tokens,
            "cost": self.cost,
            "added": self.added,
            "modified": self.modified,
            "deleted": self.deleted,
            "commits": self.commits,
            "artifacts": self.artifacts,
        }

    def save(self) -> None:
        """Atomically write ``status.json``."""
        payload = json.dumps(self.to_status(), indent=2, ensure_ascii=False)
        tmp = self.dir / "status.json.tmp"
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(self.dir / "status.json")


class Registry:
    """Thread-safe job registry."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.RLock()

    def add(self, job: Job) -> None:
        with self._lock:
            self._jobs[job.job_id] = job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def all(self) -> list[Job]:
        with self._lock:
            return list(self._jobs.values())

    def active(self) -> Job | None:
        with self._lock:
            for job in self._jobs.values():
                if job.status in ACTIVE_STATUSES:
                    return job
        return None

    def is_busy(self) -> bool:
        return self.active() is not None

    def load_from_disk(self, workspace: Path) -> None:
        """Reload jobs already present at startup.

        A job still marked active means the worker restarted mid-run: mark it
        as failed so the worker does not stay stuck.
        """
        if not workspace.exists():
            return
        for status_file in workspace.glob("*/status.json"):
            try:
                data = json.loads(status_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            job_id = status_file.parent.name
            job = Job(job_id=job_id, dir=status_file.parent, spec={})
            job.status = data.get("status", "failed")
            job.created_at = data.get("created_at") or job.created_at
            job.started_at = data.get("started_at")
            job.finished_at = data.get("finished_at")
            job.exit_code = data.get("exit_code")
            job.error = data.get("error")
            job.result_text = data.get("result_text") or ""
            job.session_id = data.get("session_id")
            job.tool_calls = data.get("tool_calls") or []
            job.tokens = data.get("tokens")
            job.cost = data.get("cost")
            job.added = data.get("added") or []
            job.modified = data.get("modified") or []
            job.deleted = data.get("deleted") or []
            job.commits = data.get("commits") or []
            job.artifacts = data.get("artifacts") or []
            if job.status in ACTIVE_STATUSES:
                job.status = "failed"
                job.error = "worker restarted during the job"
                job.finished_at = time.time()
                job.save()
            self.add(job)

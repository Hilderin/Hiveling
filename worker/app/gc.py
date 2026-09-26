"""Workspace garbage collection.

Every job lives in its own directory under the worker workspace
(``<workspace>/<job_id>``) and is never reused, so without retention those
directories (and the git worktrees they contain) grow forever. :func:`prune`
removes **terminal** jobs only; a job that is accepted or running is never
touched.

Two independent criteria, both off by default:

- ``keep``: retain the newest N terminal jobs, remove the older ones;
- ``older_than_days``: remove terminal jobs last modified more than N days ago.

The server re-reads ``status.json`` for finished jobs, so removing a job
directory only drops its artifacts; a run that already finished is unaffected.
Stale git worktree metadata is cleaned by the git provider's ``worktree prune``
on the next use.
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from pathlib import Path

logger = logging.getLogger("hiveling.worker.gc")

TERMINAL_STATUSES = {"succeeded", "failed", "canceled"}


def _status_mtime(job_dir: Path) -> float:
    status_file = job_dir / "status.json"
    try:
        return status_file.stat().st_mtime
    except OSError:
        try:
            return job_dir.stat().st_mtime
        except OSError:
            return 0.0


def prune(
    workspace: Path,
    *,
    keep: int = 0,
    older_than_days: float = 0.0,
) -> dict:
    """Remove old terminal jobs. Returns ``{"removed": n, "kept": n}``."""
    if keep <= 0 and older_than_days <= 0:
        return {"removed": 0, "kept": 0}
    if not workspace.exists():
        return {"removed": 0, "kept": 0}

    entries: list[tuple[float, Path]] = []
    for job_dir in workspace.iterdir():
        if not job_dir.is_dir():
            continue
        status_file = job_dir / "status.json"
        if not status_file.is_file():
            # A directory without status.json is not one of our finished jobs
            # (or is still being created): leave it alone.
            continue
        try:
            status = json.loads(status_file.read_text(encoding="utf-8-sig")).get("status")
        except (OSError, json.JSONDecodeError):
            continue
        if status not in TERMINAL_STATUSES:
            continue
        entries.append((_status_mtime(job_dir), job_dir))

    entries.sort(key=lambda item: item[0], reverse=True)  # newest first
    now = time.time()
    removed = 0
    for index, (mtime, job_dir) in enumerate(entries):
        by_count = keep > 0 and index >= keep
        by_age = older_than_days > 0 and (now - mtime) > older_than_days * 86400
        if not (by_count or by_age):
            continue
        shutil.rmtree(job_dir, ignore_errors=True)
        removed += 1
    result = {"removed": removed, "kept": len(entries) - removed}
    if removed:
        logger.info(
            "gc: removed %d old job(s) from %s (kept %d)",
            removed,
            workspace,
            result["kept"],
        )
    return result

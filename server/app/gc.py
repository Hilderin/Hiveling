"""History and run retention for the server data directory.

Runs are stored as ``<runs_dir>/<run_id>/run.json`` and their artifacts as
``<history_dir>/<task_id>/<run_id>/``. Without retention both grow forever.
:func:`prune_runs` removes **terminal** runs only (a running run is never
touched), by count and/or age, together with their history directories.

Both criteria are off by default (``keep=0`` and ``older_than_days=0``).
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from pathlib import Path

logger = logging.getLogger("hiveling.server.gc")

TERMINAL_STATUSES = {"succeeded", "failed", "canceled"}


def _run_state(run_dir: Path) -> tuple[str | None, float]:
    run_file = run_dir / "run.json"
    try:
        data = json.loads(run_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, 0.0
    status = data.get("status")
    timestamp = data.get("finished_at") or data.get("started_at") or 0.0
    return status, float(timestamp or 0.0)


def prune_runs(
    runs_dir: Path,
    history_dir: Path,
    *,
    keep: int = 0,
    older_than_days: float = 0.0,
) -> dict:
    """Remove old terminal runs and their history. Returns a small summary."""
    if keep <= 0 and older_than_days <= 0:
        return {"removed": 0, "kept": 0}
    if not runs_dir.exists():
        return {"removed": 0, "kept": 0}

    entries: list[tuple[float, Path, str]] = []
    for run_dir in runs_dir.iterdir():
        if not run_dir.is_dir():
            continue
        status, timestamp = _run_state(run_dir)
        if status is None:
            continue
        if status not in TERMINAL_STATUSES:
            continue
        if not timestamp:
            timestamp = run_dir.stat().st_mtime
        entries.append((timestamp, run_dir, run_dir.name))

    entries.sort(key=lambda item: item[0], reverse=True)  # newest first
    now = time.time()
    removed = 0
    for index, (timestamp, run_dir, run_id) in enumerate(entries):
        by_count = keep > 0 and index >= keep
        by_age = older_than_days > 0 and (now - timestamp) > older_than_days * 86400
        if not (by_count or by_age):
            continue
        shutil.rmtree(run_dir, ignore_errors=True)
        if history_dir.exists():
            for task_dir in history_dir.iterdir():
                if task_dir.is_dir():
                    shutil.rmtree(task_dir / run_id, ignore_errors=True)
        removed += 1

    result = {"removed": removed, "kept": len(entries) - removed}
    if removed:
        logger.info(
            "gc: removed %d old run(s) and their history (kept %d)",
            removed,
            result["kept"],
        )
    return result

"""Aggregate token usage and cost for a run, counting every attempt.

The worker records each job's tokens/cost in its attempt's ``status.json``. A
task's history directory holds the latest attempt at its root and the archived
previous attempts under ``attempt-<n>/`` (see :func:`history.archive_attempt`).
Summing every ``status.json`` therefore counts retries exactly once, whether
they come from a run resume, a gate re-arm or a task re-dispatch.
"""

from __future__ import annotations

from pathlib import Path

from .readers import read_json


def _add_tokens(target: dict, tokens: dict) -> None:
    for key in ("input", "output", "reasoning"):
        target[key] += int(tokens.get(key) or 0)
    cache = tokens.get("cache") or {}
    target["cache"]["read"] += int(cache.get("read") or 0)
    target["cache"]["write"] += int(cache.get("write") or 0)


def _new_tokens() -> dict:
    return {
        "input": 0,
        "output": 0,
        "reasoning": 0,
        "cache": {"read": 0, "write": 0},
    }


def _sum_tokens(tokens: dict) -> int:
    total = tokens.get("total")
    if total is not None:
        return int(total or 0)
    cache = tokens.get("cache") or {}
    return (
        int(tokens.get("input") or 0)
        + int(tokens.get("output") or 0)
        + int(tokens.get("reasoning") or 0)
        + int(cache.get("read") or 0)
        + int(cache.get("write") or 0)
    )


def _status_paths(directory: Path) -> list[Path]:
    """The current attempt's status plus every archived ``attempt-<n>/``."""
    if not directory.is_dir():
        return []
    paths = [directory / "status.json"]
    paths += sorted(directory.glob("attempt-*/status.json"))
    return paths


def _usage_for_task(directory: Path | None) -> dict:
    tokens = _new_tokens()
    cost = 0.0
    duration = 0.0
    attempts = 0
    if directory is None:
        return {"tokens": tokens, "cost": cost, "duration_s": duration, "attempts": attempts}
    for path in _status_paths(directory):
        status = read_json(path)
        if not isinstance(status, dict) or not status:
            continue
        _add_tokens(tokens, status.get("tokens") or {})
        raw_cost = status.get("cost")
        if raw_cost is not None:
            cost += float(raw_cost)
        raw_duration = status.get("duration_s")
        if raw_duration is not None:
            duration += float(raw_duration)
        attempts += 1
    tokens["total"] = _sum_tokens(tokens)
    return {"tokens": tokens, "cost": cost, "duration_s": duration, "attempts": attempts}


def run_usage(run: dict, history_dir: Path) -> dict:
    """Return a run's cumulative tokens/cost/duration, per task and overall.

    ``run`` is the raw run document (its tasks still carry ``history_rel``), so
    call this before redaction strips those paths. ``duration_s`` sums the real
    per-attempt ``duration_s`` reported by the worker (current attempt plus every
    archived ``attempt-<n>/``), never the run's start-to-end wall clock.
    """
    tasks: dict[str, dict] = {}
    total_tokens = _new_tokens()
    total_cost = 0.0
    total_duration = 0.0
    total_attempts = 0
    for task in run.get("tasks", []):
        task_id = task.get("id")
        if not task_id:
            continue
        rel = task.get("history_rel")
        directory = history_dir / rel if rel else None
        usage = _usage_for_task(directory)
        tasks[task_id] = usage
        _add_tokens(total_tokens, usage["tokens"])
        total_cost += usage["cost"]
        total_duration += usage["duration_s"]
        total_attempts += usage["attempts"]
    total_tokens["total"] = _sum_tokens(total_tokens)
    return {
        "tokens": total_tokens,
        "cost": total_cost,
        "duration_s": total_duration,
        "attempts": total_attempts,
        "tasks": tasks,
    }

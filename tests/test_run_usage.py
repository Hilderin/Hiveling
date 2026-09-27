"""Run-level token/cost usage must count every attempt, not just the last one.

Regression for the dashboard's run detail: a run's cumulative usage has to sum
the current attempt of each task plus its archived ``attempt-<n>/`` statuses, so
a resume, a gate re-arm or a task re-dispatch is not forgotten.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from server.app.usage import run_usage
from server.app.web import DashboardConfig, RunManager, create_app

TOKENS_OLD = {"input": 1000, "output": 200, "reasoning": 50,
              "cache": {"read": 5000, "write": 0}}
TOKENS_NEW = {"input": 2000, "output": 400, "reasoning": 100,
              "cache": {"read": 8000, "write": 0}}


def _write_status(directory, tokens, cost):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "status.json").write_text(
        json.dumps({"status": "succeeded", "tokens": tokens, "cost": cost}),
        encoding="utf-8",
    )


def test_run_usage_sums_current_and_archived_attempts(tmp_path):
    history = tmp_path / "history"
    task_dir = history / "a" / "run-1"
    _write_status(task_dir, TOKENS_NEW, 0.02)
    _write_status(task_dir / "attempt-1", TOKENS_OLD, 0.01)
    _write_status(task_dir / "attempt-2", TOKENS_OLD, 0.01)

    run = {"tasks": [{"id": "a", "history_rel": "a/run-1"}]}
    usage = run_usage(run, history)

    # 3 attempts; tokens.total is derived when the worker did not send it.
    assert usage["attempts"] == 3
    assert usage["tokens"]["input"] == 4000
    assert usage["tokens"]["cache"]["read"] == 18000
    assert usage["tokens"]["total"] == 4000 + 800 + 200 + 18000
    assert round(usage["cost"], 5) == 0.04
    assert usage["tasks"]["a"]["attempts"] == 3


def test_run_endpoint_exposes_usage(tmp_path):
    plans = tmp_path / "plans"
    plans.mkdir()
    plan_file = plans / "p.yaml"
    plan_file.write_text("version: 1\ntasks:\n  - id: a\n    prompt: x\n", encoding="utf-8")
    config = DashboardConfig(
        data_dir=tmp_path, plans_dir=plans, workers_file=tmp_path / "workers.yaml"
    )
    manager = RunManager(config)
    manager.store.create(
        "run-1",
        plan_path=str(plan_file),
        plan_name="p.yaml",
        only=None,
        plan_snapshot=str(plan_file),
        base_dir=str(tmp_path),
        tasks=[{"id": "a", "kind": "task", "status": "succeeded",
                "attempts": 2, "history_rel": "a/run-1"}],
    )
    task_dir = tmp_path / "history" / "a" / "run-1"
    _write_status(task_dir, TOKENS_NEW, 0.02)
    _write_status(task_dir / "attempt-1", TOKENS_OLD, 0.01)

    with TestClient(create_app(config, manager)) as client:
        run = client.get("/api/runs/run-1").json()

    assert run["usage"]["attempts"] == 2
    assert run["usage"]["tokens"]["total"] == 2000 + 400 + 100 + 8000 + 1000 + 200 + 50 + 5000
    assert round(run["usage"]["cost"], 5) == 0.03
    # History paths are still stripped from the response.
    assert "history_rel" not in run["tasks"][0]

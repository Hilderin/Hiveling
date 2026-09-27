"""Resume archiving: the finished attempt is kept, numbered consistently."""

import pytest

from server.app.web import DashboardConfig, RunManager

PLAN = """\
version: 1
tasks:
  - id: a
    prompt: produce
"""


def _manager(tmp_path) -> tuple[RunManager, object]:
    plans = tmp_path / "plans"
    plans.mkdir()
    plan_file = plans / "p.yaml"
    plan_file.write_text(PLAN, encoding="utf-8")
    config = DashboardConfig(
        data_dir=tmp_path,
        plans_dir=plans,
        workers_file=tmp_path / "workers.yaml",
    )
    manager = RunManager(config)
    return manager, plan_file


def test_resume_archives_the_finished_attempt(tmp_path):
    manager, plan_file = _manager(tmp_path)
    history = tmp_path / "history" / "a" / "run-1"
    history.mkdir(parents=True)
    (history / "status.json").write_text('{"status": "failed"}', encoding="utf-8")
    (history / "result.txt").write_text("boom", encoding="utf-8")
    (history / "events.jsonl").write_text("{}\n", encoding="utf-8")

    manager.store.create(
        "run-1",
        plan_path=str(plan_file),
        plan_name="p.yaml",
        only=None,
        plan_snapshot=str(plan_file),
        tasks=[
            {
                "id": "a",
                "kind": "task",
                "status": "failed",
                "attempts": 1,
                "history_rel": "a/run-1",
                "error": "boom",
                "depends_on": [],
            }
        ],
    )
    # Do not start a real orchestrator thread in this unit test.
    manager._resume = lambda run: None

    result = manager.resume_run("run-1")

    assert result["ok"] is True
    assert result["reset"] == ["a"]
    assert (history / "attempt-1" / "status.json").is_file()
    assert (history / "attempt-1" / "result.txt").read_text(encoding="utf-8") == "boom"
    assert (history / "attempt-1" / "events.jsonl").is_file()
    task = manager.store.read("run-1")["tasks"][0]
    assert task["status"] == "pending"
    # The counter is not bumped here; the next dispatch bumps it to 2.
    assert task["attempts"] == 1
    assert task["last_error"] == "boom"


def test_resume_without_history_leaves_no_archive(tmp_path):
    manager, plan_file = _manager(tmp_path)
    manager.store.create(
        "run-1",
        plan_path=str(plan_file),
        plan_name="p.yaml",
        only=None,
        plan_snapshot=str(plan_file),
        tasks=[
            {
                "id": "a",
                "kind": "task",
                "status": "skipped",
                "attempts": 0,
                "history_rel": None,
                "error": None,
                "depends_on": [],
            }
        ],
    )
    manager._resume = lambda run: None

    result = manager.resume_run("run-1")

    assert result["ok"] is True
    assert result["reset"] == ["a"]
    assert not (tmp_path / "history" / "a" / "run-1" / "attempt-1").exists()


def test_resume_without_work_is_refused(tmp_path):
    manager, plan_file = _manager(tmp_path)
    manager.store.create(
        "run-1",
        plan_path=str(plan_file),
        plan_name="p.yaml",
        only=None,
        plan_snapshot=str(plan_file),
        tasks=[{"id": "a", "kind": "task", "status": "succeeded", "attempts": 1, "depends_on": []}],
    )
    assert manager.resume_run("run-1") == {
        "ok": False,
        "reason": "no failed, canceled or skipped task to resume",
    }


def test_resume_rearms_a_canceled_task(tmp_path):
    """A run stopped by canceling a task can be resumed without editing the plan."""
    manager, plan_file = _manager(tmp_path)
    manager.store.create(
        "run-1",
        plan_path=str(plan_file),
        plan_name="p.yaml",
        only=None,
        plan_snapshot=str(plan_file),
        tasks=[
            {"id": "done", "kind": "task", "status": "succeeded", "attempts": 1, "depends_on": []},
            {"id": "a", "kind": "task", "status": "canceled", "attempts": 0,
             "error": "canceled by user", "depends_on": []},
        ],
    )
    manager._resume = lambda run: None

    result = manager.resume_run("run-1")

    assert result["ok"] is True
    assert result["reset"] == ["a"]
    tasks = {t["id"]: t for t in manager.store.read("run-1")["tasks"]}
    assert tasks["a"]["status"] == "pending"
    assert tasks["a"]["error"] is None
    assert tasks["a"]["last_error"] == "canceled by user"
    # The succeeded task is left untouched.
    assert tasks["done"]["status"] == "succeeded"

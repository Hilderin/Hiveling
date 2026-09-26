"""Artifact channels: extra globs and run-state fields."""

from pathlib import Path

from server.app.plan import Plan, Task
from server.app.runs import build_task_states
from worker.app.files import list_globs


def test_list_globs(tmp_path):
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "reports" / "b.json").write_text("b", encoding="utf-8")
    (tmp_path / "other.txt").write_text("o", encoding="utf-8")
    assert list_globs(tmp_path, ["reports/*.txt"]) == ["reports/a.txt"]
    assert list_globs(tmp_path, ["**/*.json"]) == ["reports/b.json"]
    assert list_globs(tmp_path, ["nope/**"]) == []


def test_build_task_states_includes_artifacts_fields(tmp_path):
    plan = Plan(
        path=tmp_path / "p.yaml",
        base_dir=tmp_path,
        version=1,
        defaults={},
        tasks=[Task(id="a", prompt="p")],
    )
    states = build_task_states(plan)
    assert states[0]["commits"] == []
    assert states[0]["artifacts"] == []

"""Plan resources and artifacts: merge-by-id, download override, parsing."""

import pytest

from server.app.plan import (
    PlanError,
    _merge_opencode,
    _merge_resources,
    load_plan,
)


def test_merge_resources_by_id_task_overrides():
    defaults = [
        {"type": "git", "id": "app", "with": {"ref": "main"}},
        {"type": "env", "with": {"vars": {"A": "1"}}},
    ]
    task = [{"type": "git", "id": "app", "with": {"ref": "develop"}}]
    merged = _merge_resources(defaults, task)
    ids = [r["id"] for r in merged]
    assert ids[0] == "app"
    assert len(merged) == 2
    by_id = {r["id"]: r for r in merged}
    assert by_id["app"]["with"]["ref"] == "develop"
    assert any(r["type"] == "env" for r in merged)


def test_anonymous_resources_accumulate():
    merged = _merge_resources([{"type": "env"}], [{"type": "env"}])
    assert len(merged) == 2
    assert all(r["id"] for r in merged)


def test_resource_without_type_fails():
    with pytest.raises(PlanError):
        _merge_resources([], [{"id": "x"}])


def test_merge_opencode_lists_concat_dicts_merge():
    defaults = {
        "opencode": {
            "from": ["a"],
            "config": {"x": 1},
            "agents_paths": ["p"],
        }
    }
    task = {"from": ["b"], "config": {"y": 2}}
    merged = _merge_opencode(defaults, task)
    assert merged["from"] == ["a", "b"]
    assert merged["config"] == {"x": 1, "y": 2}
    assert merged["agents_paths"] == ["p"]


def test_load_plan_merges_resources_and_artifacts(tmp_path):
    path = tmp_path / "p.yaml"
    path.write_text(
        "version: 1\n"
        "defaults:\n"
        "  resources:\n"
        "    - type: ephemeral\n"
        "    - {type: env, id: base, with: {vars: {A: '1'}}}\n"
        "  artifacts:\n"
        "    download: all\n"
        "tasks:\n"
        "  - id: a\n"
        "    prompt: hi\n"
        "    resources:\n"
        "      - {type: env, id: base, with: {vars: {A: '2'}}}\n"
        "    artifacts:\n"
        "      download: none\n",
        encoding="utf-8",
    )
    task = load_plan(path).task_by_id("a")
    by_id = {r["id"]: r for r in task.resources}
    assert by_id["base"]["with"]["vars"] == {"A": "2"}
    assert task.download == "none"
    assert task.artifacts["download"] == "none"

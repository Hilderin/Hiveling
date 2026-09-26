"""Plan-level requirements: merging defaults + task and parsing."""

from server.app.plan import _merge_requirements, load_plan


def test_merge_requirements_unions_lists_and_labels():
    defaults = {
        "os": "windows",
        "tags": ["legacy"],
        "labels": {"site": "a"},
        "providers": ["git"],
    }
    task = {"tags": ["mssql"], "labels": {"site": "b", "team": "x"}}
    merged = _merge_requirements(defaults, task)
    assert merged["os"] == "windows"
    assert merged["tags"] == ["legacy", "mssql"]
    assert merged["providers"] == ["git"]
    assert merged["labels"] == {"site": "b", "team": "x"}


def test_task_os_overrides_default():
    assert _merge_requirements({"os": "windows"}, {"os": "linux"})["os"] == "linux"


def test_merge_dedupes_preserving_order():
    merged = _merge_requirements({"tags": ["a"]}, {"tags": ["a", "b"]})
    assert merged["tags"] == ["a", "b"]


def test_load_plan_parses_and_orders_requirements(tmp_path):
    path = tmp_path / "p.yaml"
    path.write_text(
        "version: 1\n"
        "defaults:\n"
        "  requirements:\n"
        "    os: windows\n"
        "    tags: [legacy]\n"
        "tasks:\n"
        "  - id: a\n"
        "    prompt: hi\n"
        "    requirements:\n"
        "      tags: [mssql]\n"
        "      labels: {site: mtl}\n"
        "  - id: b\n"
        "    prompt: there\n",
        encoding="utf-8",
    )
    plan = load_plan(path)
    assert plan.task_by_id("a").requirements == {
        "os": "windows",
        "tags": ["legacy", "mssql"],
        "labels": {"site": "mtl"},
    }
    # Defaults only.
    assert plan.task_by_id("b").requirements == {"os": "windows", "tags": ["legacy"]}

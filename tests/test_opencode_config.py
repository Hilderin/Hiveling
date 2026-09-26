"""OpenCode config injection: inline, path sources, provenance, baseline."""

import json

import pytest

from worker.app.environment import Context, EnvironmentError, temp_permissions
from worker.app.opencode_config import inject


def make_ctx(tmp_path, roots=None) -> Context:
    return Context(
        workspace=tmp_path / "loc",
        job_dir=tmp_path,
        task_id="t1",
        run_id="r1",
        path_roots=roots if roots is not None else [str(tmp_path)],
    )


def test_inline_and_path_sources(tmp_path):
    c = make_ctx(tmp_path)
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "reviewer.md").write_text("Review only.\n", encoding="utf-8")
    skills = tmp_path / "skills" / "run-tests"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("---\nname: run-tests\n---\nbody\n", encoding="utf-8")
    md = tmp_path / "TEAM.md"
    md.write_text("team rules\n", encoding="utf-8")

    report = inject(
        c.workspace,
        plan_opencode={
            "config": {"model": "x/y"},
            "agents": {"inline": "inline body"},
            "skills": [{"name": "inline", "content": "c"}],
            "agents_paths": [str(agents)],
            "skills_paths": [str(tmp_path / "skills")],
            "agents_md": [str(md)],
        },
        baseline_dir=None,
        provenance=[],
        fragments=[],
        ctx=c,
    )

    config = json.loads((c.workspace / "opencode.json").read_text(encoding="utf-8"))
    assert config["model"] == "x/y"
    assert str((tmp_path / "skills").resolve()) in config["skills"]

    assert (c.workspace / ".opencode" / "agents" / "reviewer.md").exists()
    assert (c.workspace / ".opencode" / "agents" / "inline.md").exists()
    assert (c.workspace / ".opencode" / "skills" / "inline" / "SKILL.md").exists()

    text = (c.workspace / "AGENTS.md").read_text(encoding="utf-8")
    assert "team rules" in text
    assert "<!-- source:" in text
    assert report.agents


def test_baseline_bundle_then_plan_overrides(tmp_path):
    baseline = tmp_path / "base"
    (baseline / "agents").mkdir(parents=True)
    (baseline / "agents" / "base.md").write_text("base\n", encoding="utf-8")
    (baseline / "opencode.json").write_text('{"model": "base/model"}\n', encoding="utf-8")

    c = make_ctx(tmp_path)
    inject(
        c.workspace,
        plan_opencode={"config": {"model": "plan/model"}},
        baseline_dir=baseline,
        provenance=[],
        fragments=[],
        ctx=c,
    )
    config = json.loads((c.workspace / "opencode.json").read_text(encoding="utf-8"))
    assert config["model"] == "plan/model"
    assert (c.workspace / ".opencode" / "agents" / "base.md").exists()


def test_provenance_under_location_skips_agents_md(tmp_path):
    c = make_ctx(tmp_path)
    repo = c.workspace / "src" / "app"
    (repo / ".opencode" / "agents").mkdir(parents=True)
    (repo / ".opencode" / "agents" / "r.md").write_text("r\n", encoding="utf-8")
    (repo / "AGENTS.md").write_text("repo md\n", encoding="utf-8")

    inject(
        c.workspace,
        plan_opencode={},
        baseline_dir=None,
        provenance=[(repo, True)],
        fragments=[],
        ctx=c,
    )
    # .opencode is aggregated (not discovered upward), AGENTS.md is left alone.
    assert (c.workspace / ".opencode" / "agents" / "r.md").exists()
    assert not (c.workspace / "AGENTS.md").exists()


def test_provenance_outside_location_concatenates_agents_md(tmp_path):
    c = make_ctx(tmp_path)
    repo = tmp_path / "external" / "app"
    repo.mkdir(parents=True)
    (repo / "AGENTS.md").write_text("external md\n", encoding="utf-8")

    inject(
        c.workspace,
        plan_opencode={},
        baseline_dir=None,
        provenance=[(repo, False)],
        fragments=[],
        ctx=c,
    )
    assert "external md" in (c.workspace / "AGENTS.md").read_text(encoding="utf-8")


def test_provider_fragment_is_merged(tmp_path):
    c = make_ctx(tmp_path)
    inject(
        c.workspace,
        plan_opencode={},
        baseline_dir=None,
        provenance=[],
        fragments=[{"permissions": [{"action": "read", "resource": "/x/**", "effect": "allow"}]}],
        ctx=c,
    )
    config = json.loads((c.workspace / "opencode.json").read_text(encoding="utf-8"))
    resources = [rule["resource"] for rule in config["permissions"]]
    assert "/x/**" in resources


def test_temp_dir_permissions_added_by_default(tmp_path):
    c = make_ctx(tmp_path)
    inject(
        c.workspace,
        plan_opencode={},
        baseline_dir=None,
        provenance=[],
        fragments=[],
        ctx=c,
    )
    config = json.loads((c.workspace / "opencode.json").read_text(encoding="utf-8"))
    temp = temp_permissions()["permissions"][0]["resource"]
    assert temp in [rule["resource"] for rule in config["permissions"]]
    assert any(
        rule["action"] == "external_directory" and rule["resource"] == temp
        for rule in config["permissions"]
    )


def test_temp_dir_permissions_can_be_overridden(tmp_path):
    """A later layer wins: a plan deny after the built-in allow is preserved."""
    c = make_ctx(tmp_path)
    temp = temp_permissions()["permissions"][0]["resource"]
    inject(
        c.workspace,
        plan_opencode={
            "config": {
                "permissions": [
                    {"action": "external_directory", "resource": temp, "effect": "deny"}
                ]
            }
        },
        baseline_dir=None,
        provenance=[],
        fragments=[],
        ctx=c,
    )
    config = json.loads((c.workspace / "opencode.json").read_text(encoding="utf-8"))
    matching = [r for r in config["permissions"] if r["resource"] == temp]
    assert matching[-1]["effect"] == "deny"


def test_repo_config_overrides_baseline_and_plan_overrides_repo(tmp_path):
    baseline = tmp_path / "base"
    baseline.mkdir()
    (baseline / "opencode.json").write_text(
        '{"model": "base", "theme": "dark"}\n', encoding="utf-8"
    )
    repo = tmp_path / "repo"
    (repo / ".opencode").mkdir(parents=True)
    (repo / ".opencode" / "opencode.json").write_text(
        '{"model": "repo", "agent": "build"}\n', encoding="utf-8"
    )
    c = make_ctx(tmp_path)
    inject(
        c.workspace,
        plan_opencode={"config": {"model": "plan"}},
        baseline_dir=baseline,
        provenance=[(repo, False)],
        fragments=[],
        ctx=c,
    )
    config = json.loads((c.workspace / "opencode.json").read_text(encoding="utf-8"))
    assert config["model"] == "plan"   # plan wins
    assert config["theme"] == "dark"   # baseline retained
    assert config["agent"] == "build"  # repo retained


def test_permissions_are_concatenated_across_layers(tmp_path):
    c = make_ctx(tmp_path)
    fragments = [
        {"permissions": [{"action": "external_directory", "resource": "/x/**", "effect": "allow"}]}
    ]
    inject(
        c.workspace,
        plan_opencode={"config": {"permissions": [{"action": "edit", "resource": "*", "effect": "allow"}]}},
        baseline_dir=None,
        provenance=[],
        fragments=fragments,
        ctx=c,
    )
    config = json.loads((c.workspace / "opencode.json").read_text(encoding="utf-8"))
    resources = [rule["resource"] for rule in config["permissions"]]
    assert "*" in resources and "/x/**" in resources


def test_source_outside_path_roots_fails(tmp_path):
    c = make_ctx(tmp_path, roots=[str(tmp_path / "allowed")])
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(EnvironmentError):
        inject(
            c.workspace,
            plan_opencode={"agents_paths": [str(outside)]},
            baseline_dir=None,
            provenance=[],
            fragments=[],
            ctx=c,
        )

"""The structured plan schema (used by create_plan) serializes resources."""

from server.app.plan_schema import PlanInput, plan_to_yaml


def test_resources_use_with_alias_in_yaml():
    plan = PlanInput(
        tasks=[
            {
                "id": "a",
                "prompt": "p",
                "resources": [
                    {"type": "git", "id": "app", "with": {"repo": "x", "ref": "main"}}
                ],
                "artifacts": {"download": "none"},
            }
        ]
    )
    content = plan_to_yaml(plan)
    assert "resources:" in content
    assert "with:" in content
    assert "repo: x" in content
    assert "download: none" in content


def test_requirements_and_resources_roundtrip():
    plan = PlanInput(
        defaults={"requirements": {"os": "windows"}},
        tasks=[{"id": "a", "prompt": "p"}],
    )
    content = plan_to_yaml(plan)
    assert "requirements:" in content
    assert "os: windows" in content


def test_auto_is_no_longer_a_plan_option():
    """`auto` was removed: permissions come from the injected opencode.json.

    The worker no longer passes `--auto`, so an unknown `auto` key must be
    rejected instead of silently ignored.
    """
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        PlanInput(tasks=[{"id": "a", "prompt": "p", "auto": False}])
    with pytest.raises(ValidationError):
        PlanInput(defaults={"auto": True}, tasks=[{"id": "a", "prompt": "p"}])

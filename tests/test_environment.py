"""Worker environment lifecycle and the ephemeral provider."""

import pytest

from worker.app.environment import (
    Context,
    Environment,
    EnvironmentError,
    JobOutcome,
    Resource,
    parse_resources,
)


def make_ctx(tmp_path) -> Context:
    return Context(workspace=tmp_path / "work", job_dir=tmp_path)


def test_ephemeral_is_a_noop(tmp_path):
    env = Environment([Resource(type="ephemeral", id="e")], make_ctx(tmp_path))
    env.prepare()
    assert env.env == {}
    assert env.opencode_fragments == []
    result = env.finalize(JobOutcome(status="succeeded", succeeded=True))
    assert result.commits == []
    env.teardown()


def test_unknown_provider_fails_on_prepare(tmp_path):
    env = Environment([Resource(type="git", id="g")], make_ctx(tmp_path))
    with pytest.raises(EnvironmentError) as exc:
        env.prepare()
    assert "git" in str(exc.value)


def test_ephemeral_rejects_unknown_option(tmp_path):
    env = Environment(
        [Resource(type="ephemeral", id="e", options={"bogus": 1})], make_ctx(tmp_path)
    )
    with pytest.raises(EnvironmentError):
        env.prepare()


def test_parse_resources_wire_format():
    resources = parse_resources(
        [{"type": "git", "id": "app", "with": {"ref": "main"}}]
    )
    assert resources[0].type == "git"
    assert resources[0].id == "app"
    assert resources[0].options == {"ref": "main"}


def test_parse_resources_requires_type():
    with pytest.raises(EnvironmentError):
        parse_resources([{"id": "x"}])

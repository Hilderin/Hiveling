"""The command escape hatch and the provider allowlist."""

import pytest

from worker.app.capabilities import Capabilities
from worker.app.environment import Context, Environment, EnvironmentError, Resource


def make_ctx(tmp_path, providers=None) -> Context:
    capabilities = {}
    if providers is not None:
        capabilities["providers"] = providers
    return Context(
        workspace=tmp_path / "work",
        job_dir=tmp_path,
        task_id="t1",
        capabilities=capabilities,
    )


def test_command_not_advertised_by_default(tmp_path):
    caps = Capabilities(tmp_path / "absent.yaml").get()
    assert "command" not in caps["providers"]
    assert "git" in caps["providers"]


def test_command_prepare_runs(tmp_path):
    ctx = make_ctx(tmp_path, providers=["command"])
    env = Environment(
        [Resource(type="command", id="c", options={"prepare": "echo hi > out.txt"})],
        ctx,
    )
    env.prepare()
    assert (ctx.workspace / "out.txt").read_text(encoding="utf-8").strip() == "hi"


def test_command_failure_fails_prepare(tmp_path):
    ctx = make_ctx(tmp_path, providers=["command"])
    env = Environment(
        [Resource(type="command", id="c", options={"prepare": "exit 3"})], ctx
    )
    with pytest.raises(EnvironmentError):
        env.prepare()


def test_command_refused_when_not_enabled(tmp_path):
    ctx = make_ctx(tmp_path, providers=["ephemeral", "git"])
    env = Environment(
        [Resource(type="command", id="c", options={"prepare": "echo hi"})], ctx
    )
    with pytest.raises(EnvironmentError) as exc:
        env.prepare()
    assert "not enabled" in str(exc.value)


def test_command_finalize_runs_only_after_prepare(tmp_path):
    ctx = make_ctx(tmp_path, providers=["command"])
    env = Environment(
        [Resource(type="command", id="c", options={"finalize": "echo done > fin.txt"})],
        ctx,
    )
    env.prepare()
    assert not (ctx.workspace / "fin.txt").exists()
    env.finalize(None)
    assert (ctx.workspace / "fin.txt").exists()

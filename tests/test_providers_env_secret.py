"""The env and secret providers."""

import pytest

from worker.app.environment import Context, Environment, EnvironmentError, Resource
from worker.app.secrets import SecretStore


def make_ctx(tmp_path, secrets=None) -> Context:
    return Context(workspace=tmp_path / "work", job_dir=tmp_path, secrets=secrets)


def test_env_provider_injects_and_expands(tmp_path, monkeypatch):
    monkeypatch.setenv("BASE", "/root")
    env = Environment(
        [Resource(type="env", id="e", options={"vars": {"A": "1", "P": "${BASE}/x"}})],
        make_ctx(tmp_path),
    )
    env.prepare()
    assert env.env == {"A": "1", "P": "/root/x"}


def test_env_provider_requires_vars(tmp_path):
    env = Environment(
        [Resource(type="env", id="e", options={})], make_ctx(tmp_path)
    )
    with pytest.raises(EnvironmentError):
        env.prepare()


def test_secret_from_file(tmp_path):
    secrets = tmp_path / "secrets.yaml"
    secrets.write_text("TOKEN: s3cr3t\n", encoding="utf-8")
    store = SecretStore(secrets, environ={})
    env = Environment(
        [Resource(type="secret", id="s", options={"name": "TOKEN", "as": "API_TOKEN"})],
        make_ctx(tmp_path, store),
    )
    env.prepare()
    assert env.env == {"API_TOKEN": "s3cr3t"}


def test_secret_falls_back_to_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_SECRET", "from-env")
    store = SecretStore(tmp_path / "absent.yaml")
    env = Environment(
        [Resource(type="secret", id="s", options={"name": "MY_SECRET"})],
        make_ctx(tmp_path, store),
    )
    env.prepare()
    assert env.env == {"MY_SECRET": "from-env"}


def test_secret_missing_required_fails(tmp_path):
    store = SecretStore(tmp_path / "absent.yaml", environ={})
    env = Environment(
        [Resource(type="secret", id="s", options={"name": "MISSING"})],
        make_ctx(tmp_path, store),
    )
    with pytest.raises(EnvironmentError):
        env.prepare()


def test_secret_missing_optional_is_skipped(tmp_path):
    store = SecretStore(tmp_path / "absent.yaml", environ={})
    env = Environment(
        [Resource(type="secret", id="s", options={"name": "MISSING", "required": False})],
        make_ctx(tmp_path, store),
    )
    env.prepare()
    assert env.env == {}

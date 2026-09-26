"""The git and path providers (using local bare repositories)."""

import os
import subprocess
from pathlib import Path

import pytest

from worker.app.environment import (
    Context,
    Environment,
    EnvironmentError,
    JobOutcome,
    Resource,
)

GIT_ENV = {
    **os.environ,
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def git(cwd, *args, check=True):
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=check,
        env=GIT_ENV,
    )


@pytest.fixture
def bare_repo(tmp_path):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    seed = tmp_path / "seed"
    git(tmp_path, "clone", str(remote), str(seed))
    (seed / "README.md").write_text("hello\n", encoding="utf-8")
    git(seed, "add", ".")
    git(seed, "commit", "-m", "init")
    git(seed, "push", "origin", "main")
    return remote


def make_ctx(tmp_path, task_id="t1", run_id="r1", roots=None):
    return Context(
        workspace=tmp_path / "loc",
        job_dir=tmp_path,
        task_id=task_id,
        run_id=run_id,
        path_roots=roots if roots is not None else [str(tmp_path)],
    )


def test_git_canonical_clone_commit_push(tmp_path, bare_repo):
    ctx = make_ctx(tmp_path)
    resource = Resource(
        type="git",
        id="app",
        options={
            "repo": str(bare_repo),
            "path": str(tmp_path / "clone"),
            "worktree": False,
            "ref": "main",
            "branch": "hiveling/r1/t1",
            "publish": "push",
        },
    )
    env = Environment([resource], ctx)
    env.prepare()
    repo = Path(env.prepared[0].state["repo_dir"])
    assert (repo / "README.md").is_file()

    (repo / "feature.txt").write_text("x", encoding="utf-8")
    result = env.finalize(JobOutcome(status="succeeded", succeeded=True, workdir=repo))
    assert result.commits and result.commits[0]["pushed"] is True
    env.teardown()

    out = subprocess.run(
        ["git", "--git-dir", str(bare_repo), "rev-parse", "hiveling/r1/t1"],
        capture_output=True,
        text=True,
        env=GIT_ENV,
    )
    assert out.returncode == 0


def test_git_worktree_is_under_location_and_commits(tmp_path, bare_repo):
    ctx = make_ctx(tmp_path)
    resource = Resource(
        type="git",
        id="app",
        options={
            "repo": str(bare_repo),
            "path": str(tmp_path / "durable"),
            "worktree": True,
            "ref": "main",
            "branch": "hiveling/r1/t1",
            "publish": "commit",
        },
    )
    env = Environment([resource], ctx)
    env.prepare()
    workdir = Path(env.prepared[0].state["repo_dir"])
    assert workdir == (tmp_path / "loc" / "src" / "app").resolve()
    assert (workdir / "README.md").is_file()

    (workdir / "feat.txt").write_text("y", encoding="utf-8")
    result = env.finalize(JobOutcome(status="succeeded", succeeded=True, workdir=workdir))
    assert result.commits and result.commits[0]["pushed"] is False
    assert result.commits[0]["branch"] == "hiveling/r1/t1"


def test_git_create_branch_twice_fails(tmp_path, bare_repo):
    ctx = make_ctx(tmp_path)
    opts = {
        "repo": str(bare_repo),
        "path": str(tmp_path / "clone"),
        "worktree": False,
        "ref": "main",
        "branch": "hiveling/r1/t1",
        "branch_mode": "create",
        "publish": "none",
    }
    env1 = Environment([Resource(type="git", id="app", options=opts)], ctx)
    env1.prepare()
    env2 = Environment([Resource(type="git", id="app", options=opts)], ctx)
    with pytest.raises(EnvironmentError):
        env2.prepare()


def test_git_path_outside_roots_fails(tmp_path):
    ctx = make_ctx(tmp_path, roots=[str(tmp_path / "allowed")])
    resource = Resource(
        type="git",
        id="app",
        options={"repo": "/nonexistent", "path": str(tmp_path / "elsewhere")},
    )
    with pytest.raises(EnvironmentError):
        Environment([resource], ctx).prepare()


def test_path_provider_exposes_folder(tmp_path):
    ctx = make_ctx(tmp_path)
    exposed = tmp_path / "shared"
    exposed.mkdir()
    resource = Resource(type="path", id="fixtures", options={"path": str(exposed), "mode": "ro"})
    env = Environment([resource], ctx)
    env.prepare()
    assert env.prepared[0].paths[0]["path"] == str(exposed)
    assert env.prepared[0].paths[0]["mode"] == "ro"


def test_path_provider_missing_folder_fails(tmp_path):
    ctx = make_ctx(tmp_path)
    resource = Resource(type="path", id="x", options={"path": str(tmp_path / "absent")})
    with pytest.raises(EnvironmentError):
        Environment([resource], ctx).prepare()

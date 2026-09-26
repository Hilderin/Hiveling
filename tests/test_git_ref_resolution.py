"""Regression: a consumed branch exists only as origin/<ref> in a fresh clone.

The plan names a branch as it exists on the remote; `git worktree add` and
`git checkout` must resolve it (local ref, else origin/<ref>), not pass the raw
name — otherwise a consumer task fails at prepare with "not a valid object name".
"""

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
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=check, env=GIT_ENV
    )


@pytest.fixture
def bare_repo(tmp_path):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    seed = tmp_path / "seed"
    git(tmp_path, "clone", "-q", str(remote), str(seed))
    (seed / "README.md").write_text("base\n", encoding="utf-8")
    git(seed, "add", "-A")
    git(seed, "commit", "-qm", "init")
    git(seed, "push", "-q", "origin", "main")
    return remote


def make_ctx(tmp_path) -> Context:
    return Context(
        workspace=tmp_path / "loc",
        job_dir=tmp_path,
        task_id="consumer",
        run_id="r1",
        path_roots=[str(tmp_path)],
    )


def publish_branch(bare_repo, tmp_path, name: str, filename: str, content: str):
    work = tmp_path / f"producer-{name}"
    git(tmp_path, "clone", "-q", str(bare_repo), str(work))
    git(work, "checkout", "-q", "-b", name)
    (work / filename).write_text(content, encoding="utf-8")
    git(work, "add", "-A")
    git(work, "commit", "-qm", name)
    git(work, "push", "-q", "origin", name)


def test_worktree_consumes_remote_only_branch(tmp_path, bare_repo):
    """The producer branch is only on origin; the consumer worktree must resolve it."""
    publish_branch(bare_repo, tmp_path, "hiveling/r1/analyst", "analysis.txt", "x\n")
    env = Environment(
        [
            Resource(
                type="git",
                id="repo",
                options={
                    "repo": str(bare_repo),
                    "path": "repo",
                    "worktree": True,
                    "ref": "hiveling/r1/analyst",
                    "branch": "hiveling/r1/consumer",
                    "publish": "none",
                },
            )
        ],
        make_ctx(tmp_path),
    )
    env.prepare()
    workdir = Path(env.prepared[0].state["repo_dir"])
    assert (workdir / "analysis.txt").read_text(encoding="utf-8") == "x\n"


def test_canonical_checkout_consumes_remote_only_branch(tmp_path, bare_repo):
    publish_branch(bare_repo, tmp_path, "hiveling/r1/analyst", "analysis.txt", "y\n")
    env = Environment(
        [
            Resource(
                type="git",
                id="repo",
                options={
                    "repo": str(bare_repo),
                    "path": str(tmp_path / "clone"),
                    "worktree": False,
                    "ref": "hiveling/r1/analyst",
                    "branch": "hiveling/r1/consumer",
                    "publish": "none",
                },
            )
        ],
        make_ctx(tmp_path),
    )
    env.prepare()
    assert (Path(env.prepared[0].state["repo_dir"]) / "analysis.txt").is_file()


def test_missing_ref_fails_with_a_clear_error(tmp_path, bare_repo):
    env = Environment(
        [
            Resource(
                type="git",
                id="repo",
                options={
                    "repo": str(bare_repo),
                    "path": "repo",
                    "worktree": True,
                    "ref": "hiveling/r1/does-not-exist",
                    "branch": "hiveling/r1/consumer",
                    "publish": "none",
                },
            )
        ],
        make_ctx(tmp_path),
    )
    with pytest.raises(EnvironmentError) as exc:
        env.prepare()
    assert "does-not-exist" in str(exc.value)

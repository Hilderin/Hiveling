"""The git provider `merge` option and worker-side conflict resolution."""

import os
import subprocess
from pathlib import Path

import pytest

from worker.app import opencode_run
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


def make_branch(bare: Path, name: str, changes: dict[str, str]) -> None:
    work = bare.parent / f"work-{name}"
    git(bare.parent, "clone", "-q", str(bare), str(work))
    git(work, "checkout", "-q", "-b", name)
    for filename, content in changes.items():
        (work / filename).write_text(content, encoding="utf-8")
    git(work, "add", "-A")
    git(work, "commit", "-qm", name)
    git(work, "push", "-q", "origin", name)


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


def make_ctx(tmp_path, repo_dir) -> Context:
    return Context(
        workspace=tmp_path / "loc",
        job_dir=tmp_path,
        task_id="integrate",
        run_id="r1",
        path_roots=[str(tmp_path)],
        opencode_bin="/usr/bin/true",  # replaced by the fake in conflict tests
    )


def resource(bare: Path, tmp_path: Path, merge: list[str], *, clone="clone") -> Resource:
    return Resource(
        type="git",
        id="app",
        options={
            "repo": str(bare),
            "path": str(tmp_path / clone),
            "worktree": False,
            "ref": "main",
            "branch": "integrate",
            "merge": merge,
            "publish": "push",
        },
    )


def test_clean_merge_creates_merge_commits(tmp_path, bare_repo):
    make_branch(bare_repo, "feat-a", {"a.txt": "A\n"})
    make_branch(bare_repo, "feat-b", {"b.txt": "B\n"})
    ctx = make_ctx(tmp_path, tmp_path)
    env = Environment([resource(bare_repo, tmp_path, ["feat-a", "feat-b"])], ctx)
    env.prepare()
    state = env.prepared[0].state
    assert state["merge"]["conflicts"] == []
    assert len(state["merge"]["commits"]) == 2
    repo = Path(state["repo_dir"])
    merges = subprocess.run(
        ["git", "log", "--merges", "--oneline"],
        cwd=repo,
        capture_output=True,
        text=True,
        env=GIT_ENV,
    ).stdout
    assert merges.count("\n") >= 2
    assert (repo / "a.txt").exists() and (repo / "b.txt").exists()


def test_conflict_is_resolved_by_opencode(tmp_path, bare_repo, monkeypatch):
    make_branch(bare_repo, "feat-a", {"shared.txt": "A\n"})
    make_branch(bare_repo, "feat-b", {"shared.txt": "B\n"})

    def fake_run(binary, cwd, prompt, **kwargs):
        unresolved = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=U"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            env=GIT_ENV,
        ).stdout.split()
        for name in unresolved:
            (Path(cwd) / name).write_text("A\nB\n", encoding="utf-8")
            subprocess.run(["git", "add", name], cwd=str(cwd), check=True, env=GIT_ENV)

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    monkeypatch.setattr(opencode_run, "run", fake_run)
    ctx = make_ctx(tmp_path, tmp_path)
    env = Environment([resource(bare_repo, tmp_path, ["feat-a", "feat-b"])], ctx)
    env.prepare()
    state = env.prepared[0].state
    assert state["merge"]["conflicts"] == ["shared.txt"]
    repo = Path(state["repo_dir"])
    assert (repo / "shared.txt").read_text(encoding="utf-8") == "A\nB\n"
    assert "conflict marker" not in subprocess.run(
        ["git", "diff", "--check"], cwd=repo, capture_output=True, text=True, env=GIT_ENV
    ).stdout


def test_publish_push_without_changes_fails(tmp_path, bare_repo):
    """`publish: push` must fail when the task produced nothing.

    Regression: an agent that edits the wrong tree "succeeded" with
    `pushed: false`, and the consumer then failed with `merge ref not found`.
    """
    ctx = make_ctx(tmp_path, tmp_path)
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
    with pytest.raises(EnvironmentError) as exc:
        env.finalize(JobOutcome(status="succeeded", succeeded=True))
    assert "nothing to push" in str(exc.value)


def test_publish_push_with_a_merge_but_no_edit_succeeds(tmp_path, bare_repo):
    """A prepare-time `merge` is a real change, so an empty edit still pushes."""
    make_branch(bare_repo, "feat-a", {"a.txt": "A\n"})
    ctx = make_ctx(tmp_path, tmp_path)
    resource = Resource(
        type="git",
        id="app",
        options={
            "repo": str(bare_repo),
            "path": str(tmp_path / "clone"),
            "worktree": False,
            "ref": "main",
            "branch": "hiveling/r1/t1",
            "merge": ["feat-a"],
            "publish": "push",
        },
    )
    env = Environment([resource], ctx)
    env.prepare()
    result = env.finalize(JobOutcome(status="succeeded", succeeded=True))
    assert result.commits and result.commits[0]["pushed"] is True


def test_unresolved_conflict_fails_prepare(tmp_path, bare_repo, monkeypatch):
    make_branch(bare_repo, "feat-a", {"shared.txt": "A\n"})
    make_branch(bare_repo, "feat-b", {"shared.txt": "B\n"})

    def fake_noop(binary, cwd, prompt, **kwargs):
        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    monkeypatch.setattr(opencode_run, "run", fake_noop)
    ctx = make_ctx(tmp_path, tmp_path)
    env = Environment([resource(bare_repo, tmp_path, ["feat-a", "feat-b"])], ctx)
    with pytest.raises(EnvironmentError) as exc:
        env.prepare()
    assert "could not be resolved" in str(exc.value)

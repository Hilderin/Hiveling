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


def test_git_worktree_relative_path_stages_clone_outside_workspace(tmp_path, bare_repo):
    """A relative `path` must not leave a second tree in the job workspace.

    Regression: the clone at `<workspace>/repo` looked like the checkout, so the
    agent edited it while the provider committed the real worktree at
    `<workspace>/src/app` — `publish: push` silently pushed nothing.
    """
    ctx = make_ctx(tmp_path)
    resource = Resource(
        type="git",
        id="app",
        options={
            "repo": str(bare_repo),
            "path": "repo",
            "worktree": True,
            "ref": "main",
            "branch": "hiveling/r1/t1",
            "publish": "commit",
        },
    )
    env = Environment([resource], ctx)
    env.prepare()
    prepared = env.prepared[0]
    workdir = Path(prepared.state["repo_dir"])
    assert workdir == (tmp_path / "loc" / "src" / "app").resolve()
    assert Path(prepared.state["clone"]) == (tmp_path / "repos" / "app").resolve()
    assert not (tmp_path / "loc" / "repo").exists()

    (workdir / "feat.txt").write_text("z", encoding="utf-8")
    result = env.finalize(JobOutcome(status="succeeded", succeeded=True, workdir=workdir))
    assert result.commits and result.commits[0]["branch"] == "hiveling/r1/t1"
    env.teardown()


def test_git_worktree_path_collision_is_rejected(tmp_path, bare_repo):
    ctx = make_ctx(tmp_path)
    resource = Resource(
        type="git",
        id="app",
        options={
            "repo": str(bare_repo),
            "path": str(tmp_path / "loc" / "src" / "app"),
            "worktree": True,
            "ref": "main",
            "branch": "hiveling/r1/t1",
            "publish": "commit",
        },
    )
    with pytest.raises(EnvironmentError):
        Environment([resource], ctx).prepare()


def test_summarize_working_dirs_names_the_worktree(tmp_path, bare_repo):
    from worker.app.environment import summarize_working_dirs

    ctx = make_ctx(tmp_path)
    resource = Resource(
        type="git",
        id="app",
        options={
            "repo": str(bare_repo),
            "path": "repo",
            "worktree": True,
            "ref": "main",
            "branch": "hiveling/r1/t1",
            "publish": "push",
        },
    )
    env = Environment([resource], ctx)
    env.prepare()
    note = summarize_working_dirs(env.prepared, ctx.workspace)
    assert "./src/app" in note
    assert "hiveling/r1/t1" in note
    assert "publish push" in note
    assert summarize_working_dirs([], ctx.workspace) == ""


# ------------------------------------------------ shared branch / resume (auto)
def _shared_branch_options(bare_repo, **overrides):
    options = {
        "repo": str(bare_repo),
        "path": "repo",
        "worktree": True,
        "ref": "main",
        "branch": "hiveling/r1/feature",
        "publish": "push",
    }
    options.update(overrides)
    return options


def _prepare(base, bare_repo, **overrides):
    ctx = make_ctx(base, task_id=overrides.pop("task_id", "t1"))
    resource = Resource(
        type="git", id="app", options=_shared_branch_options(bare_repo, **overrides)
    )
    env = Environment([resource], ctx)
    env.prepare()
    return ctx, env


def test_git_auto_resumes_an_existing_branch(tmp_path, bare_repo):
    """One branch per feature: a later task reopens the earlier task's tree."""
    _ctx1, env1 = _prepare(tmp_path / "job1", bare_repo, task_id="design")
    work1 = Path(env1.prepared[0].state["repo_dir"])
    assert env1.prepared[0].state["resumed"] is False
    (work1 / "design.md").write_text("v1", encoding="utf-8")
    env1.finalize(JobOutcome(status="succeeded", succeeded=True, workdir=work1))
    env1.teardown()

    _ctx2, env2 = _prepare(tmp_path / "job2", bare_repo, task_id="design-review")
    prepared = env2.prepared[0]
    work2 = Path(prepared.state["repo_dir"])
    assert prepared.state["resumed"] is True
    # The reviewer sees the producer's artifact without any `ref` pointing at it.
    assert (work2 / "design.md").read_text(encoding="utf-8") == "v1"


def test_git_auto_falls_back_to_ref_without_the_branch(tmp_path, bare_repo):
    _ctx, env = _prepare(tmp_path, bare_repo)
    prepared = env.prepared[0]
    assert prepared.state["resumed"] is False
    assert (Path(prepared.state["repo_dir"]) / "README.md").is_file()


def test_git_create_fails_when_the_branch_exists_on_the_remote(tmp_path, bare_repo):
    _ctx1, env1 = _prepare(tmp_path / "job1", bare_repo, task_id="design")
    work1 = Path(env1.prepared[0].state["repo_dir"])
    (work1 / "design.md").write_text("v1", encoding="utf-8")
    env1.finalize(JobOutcome(status="succeeded", succeeded=True, workdir=work1))
    env1.teardown()

    with pytest.raises(EnvironmentError, match="already exists"):
        _prepare(tmp_path / "job2", bare_repo, branch_mode="create")


def test_git_reuse_without_the_branch_fails(tmp_path, bare_repo):
    with pytest.raises(EnvironmentError, match="does not exist"):
        _prepare(tmp_path, bare_repo, branch_mode="reuse")


def test_git_recreate_resets_the_branch_to_ref(tmp_path, bare_repo):
    _ctx1, env1 = _prepare(tmp_path / "job1", bare_repo, task_id="t")
    work1 = Path(env1.prepared[0].state["repo_dir"])
    (work1 / "old.txt").write_text("x", encoding="utf-8")
    env1.finalize(JobOutcome(status="succeeded", succeeded=True, workdir=work1))
    env1.teardown()

    _ctx2, env2 = _prepare(
        tmp_path / "job2", bare_repo, task_id="t", branch_mode="recreate"
    )
    prepared = env2.prepared[0]
    assert prepared.state["resumed"] is False
    assert not (Path(prepared.state["repo_dir"]) / "old.txt").exists()


def test_git_resumed_no_op_push_succeeds(tmp_path, bare_repo):
    """A re-run that changes nothing must not fail with 'nothing to push'."""
    _ctx1, env1 = _prepare(tmp_path / "job1", bare_repo, task_id="design")
    work1 = Path(env1.prepared[0].state["repo_dir"])
    (work1 / "design.md").write_text("v1", encoding="utf-8")
    env1.finalize(JobOutcome(status="succeeded", succeeded=True, workdir=work1))
    env1.teardown()

    _ctx2, env2 = _prepare(tmp_path / "job2", bare_repo, task_id="design")
    work2 = Path(env2.prepared[0].state["repo_dir"])
    result = env2.finalize(JobOutcome(status="succeeded", succeeded=True, workdir=work2))
    assert result.commits and result.commits[0]["pushed"] is True


def test_git_finalize_reconciles_a_parallel_push(tmp_path, bare_repo):
    """Two tasks sharing one branch both push; the second merges the first back."""
    # B prepares before A pushes, so it starts from `ref` (branch absent).
    _ctxB, envB = _prepare(tmp_path / "jobB", bare_repo, task_id="taskB")
    workB = Path(envB.prepared[0].state["repo_dir"])
    assert envB.prepared[0].state["resumed"] is False

    _ctxA, envA = _prepare(tmp_path / "jobA", bare_repo, task_id="taskA")
    workA = Path(envA.prepared[0].state["repo_dir"])
    (workA / "a.txt").write_text("a", encoding="utf-8")
    envA.finalize(JobOutcome(status="succeeded", succeeded=True, workdir=workA))
    envA.teardown()

    (workB / "b.txt").write_text("b", encoding="utf-8")
    result = envB.finalize(JobOutcome(status="succeeded", succeeded=True, workdir=workB))
    assert result.commits and result.commits[0]["pushed"] is True

    # The remote branch now carries both parallel tasks' files.
    listing = git(
        tmp_path, "--git-dir", str(bare_repo), "ls-tree", "-r", "--name-only",
        "hiveling/r1/feature",
    ).stdout
    assert "a.txt" in listing
    assert "b.txt" in listing

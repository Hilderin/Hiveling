"""The ``git`` provider: clone/reset/checkout, optional worktree, commit/push.

Two layouts share the same options:

- **worktree** (``worktree: true``): a durable clone at ``path`` and a per-task
  worktree under the job location, on its own ``branch``. Parallel tasks on the
  same repo never collide and canonical checkouts are untouched.
- **canonical** (``worktree: false``, the default): the clone at ``path`` is
  checked out in place (legacy multi-repo layouts, absolute paths).

``ref`` is the start point; ``branch`` is the target the task commits to.
Dependencies between tasks are always explicit in the plan (``depends_on``);
branch names are written out, so no cross-task magic is needed.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

from ..environment import (
    Context,
    EnvironmentError,
    FinalizeResult,
    JobOutcome,
    Prepared,
    Resource,
    check_path_allowed,
    external_permissions,
)

logger = logging.getLogger("hiveling.worker.git")

_ALLOWED = {
    "repo",
    "path",
    "worktree",
    "ref",
    "branch",
    "branch_mode",
    "push_to",
    "set_upstream",
    "force",
    "clean",
    "cache",
    "publish",
    "remote",
    "commit_message",
}
_BRANCH_MODES = {"create", "reuse", "recreate"}
_CLEAN_MODES = {"none", "git", "full"}
_PUBLISH_MODES = {"none", "commit", "push"}
_CONFIG_NAMES = ("opencode.json", "opencode.jsonc")


def _git_env() -> dict:
    env = os.environ.copy()
    # Never block on an interactive credential prompt.
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_ASKPASS", "")
    return env


def _git(args: list[str], cwd: Path, *, check: bool = True) -> subprocess.CompletedProcess:
    logger.debug("git %s (cwd=%s)", " ".join(args), cwd)
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=_git_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=check,
    )


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return _git(args, cwd, check=False)


class GitProvider:
    name = "git"

    # ------------------------------------------------------------------ checks
    def validate(self, resource: Resource, ctx: Context) -> None:
        unknown = set(resource.options) - _ALLOWED
        if unknown:
            raise EnvironmentError(
                f"git: unknown option(s): {', '.join(sorted(unknown))}"
            )
        if not resource.options.get("repo"):
            raise EnvironmentError("git: 'repo' is required")
        if not resource.options.get("path"):
            raise EnvironmentError("git: 'path' is required")
        branch_mode = resource.options.get("branch_mode", "create")
        if branch_mode not in _BRANCH_MODES:
            raise EnvironmentError(
                f"git: 'branch_mode' must be one of {sorted(_BRANCH_MODES)}"
            )
        clean = resource.options.get("clean", "git")
        if clean not in _CLEAN_MODES:
            raise EnvironmentError(f"git: 'clean' must be one of {sorted(_CLEAN_MODES)}")
        publish = resource.options.get("publish", "push")
        if publish not in _PUBLISH_MODES:
            raise EnvironmentError(
                f"git: 'publish' must be one of {sorted(_PUBLISH_MODES)}"
            )

    # ----------------------------------------------------------------- helpers
    @staticmethod
    def _resolve_path(value: str, ctx: Context) -> Path:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = ctx.workspace / path
        return path.resolve()

    @staticmethod
    def _branch(resource: Resource, ctx: Context) -> str:
        configured = resource.options.get("branch")
        if configured:
            return str(configured)
        run = ctx.run_id or "run"
        return f"hiveling/{run}/{ctx.task_id or 'task'}"

    @staticmethod
    def _clean(repo: Path, mode: str, cache: list[str]) -> None:
        if mode == "none":
            return
        excludes: list[str] = []
        for entry in cache:
            excludes += ["-e", str(entry)]
        _git(["reset", "--hard"], repo, check=False)
        _git(["clean", "-fd", *excludes], repo, check=False)

    def _ensure_clone(self, repo: Path, url: str) -> None:
        if (repo / ".git").exists():
            _git(["fetch", "--all", "--prune", "--tags"], repo, check=False)
            return
        if repo.exists() and any(repo.iterdir()):
            raise EnvironmentError(
                f"git: '{repo}' exists but is not a git repository"
            )
        repo.parent.mkdir(parents=True, exist_ok=True)
        result = _git(["clone", url, str(repo)], repo.parent)
        if result.returncode != 0:
            raise EnvironmentError(
                f"git: clone of '{url}' failed: {result.stderr.strip()[-500:]}"
            )

    # ---------------------------------------------------------------- lifecycle
    def prepare(self, resource: Resource, ctx: Context) -> Prepared:
        options = resource.options
        repo_path = self._resolve_path(str(options["path"]), ctx)
        check_path_allowed(repo_path, ctx, label="git")
        worktree = bool(options.get("worktree", False))
        ref = str(options.get("ref") or "HEAD")
        branch = self._branch(resource, ctx)
        branch_mode = str(options.get("branch_mode", "create"))
        clean = str(options.get("clean", "git"))
        cache = [str(c) for c in (options.get("cache") or [])]

        if clean == "full" and not worktree and repo_path.exists():
            shutil.rmtree(repo_path, ignore_errors=True)

        self._ensure_clone(repo_path, str(options["repo"]))

        if worktree:
            workdir = (ctx.workspace / "src" / resource.id).resolve()
            self._add_worktree(repo_path, workdir, branch, ref, branch_mode)
        else:
            workdir = repo_path
            self._checkout(repo_path, branch, ref, branch_mode, clean, cache)

        config_root = workdir if (workdir / ".opencode").exists() or any(
            (workdir / name).exists() for name in _CONFIG_NAMES
        ) else None
        if (workdir / "AGENTS.md").exists():
            config_root = workdir

        under_location = self._is_under(workdir, ctx.workspace)
        return Prepared(
            resource=resource,
            paths=[{"path": str(workdir), "mode": "rw"}],
            opencode=None if under_location else external_permissions(workdir),
            state={
                "repo_dir": str(workdir),
                "clone": str(repo_path),
                "branch": branch,
                "worktree": worktree,
                "config_root": str(config_root) if config_root else None,
                "under_location": under_location,
            },
        )

    @staticmethod
    def _is_under(path: Path, root: Path) -> bool:
        root = root.resolve()
        return path == root or root in path.parents

    def _checkout(
        self,
        repo: Path,
        branch: str,
        ref: str,
        branch_mode: str,
        clean: str,
        cache: list[str],
    ) -> None:
        # Prefer the remote-tracking ref for fetch results.
        start = ref
        self._clean(repo, clean, cache)
        exists = _run(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], repo)
        if branch_mode == "recreate" and exists.returncode == 0:
            _git(["branch", "-D", branch], repo, check=False)
            exists = _run(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], repo)
        if branch_mode == "create" and exists.returncode == 0:
            raise EnvironmentError(f"git: branch '{branch}' already exists")
        if exists.returncode == 0:
            result = _git(["checkout", branch], repo)
        else:
            result = _git(["checkout", "-b", branch, start], repo)
        if result.returncode != 0:
            raise EnvironmentError(
                f"git: could not check out '{branch}': {result.stderr.strip()[-500:]}"
            )

    def _add_worktree(
        self, clone: Path, workdir: Path, branch: str, ref: str, branch_mode: str
    ) -> None:
        _git(["worktree", "prune"], clone, check=False)
        if workdir.exists():
            shutil.rmtree(workdir, ignore_errors=True)
            _git(["worktree", "prune"], clone, check=False)
        exists = _run(
            ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], clone
        )
        if branch_mode == "recreate" and exists.returncode == 0:
            _git(["branch", "-D", branch], clone, check=False)
            exists = _run(
                ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], clone
            )
        if branch_mode == "create" and exists.returncode == 0:
            raise EnvironmentError(f"git: branch '{branch}' already exists")
        workdir.parent.mkdir(parents=True, exist_ok=True)
        if exists.returncode == 0:
            result = _git(["worktree", "add", str(workdir), branch], clone)
        else:
            result = _git(["worktree", "add", "-b", branch, str(workdir), ref], clone)
        if result.returncode != 0:
            raise EnvironmentError(
                f"git: could not create worktree for '{branch}': "
                f"{result.stderr.strip()[-500:]}"
            )

    # ---------------------------------------------------------------- finalize
    def finalize(
        self,
        resource: Resource,
        ctx: Context,
        prepared: Prepared,
        outcome: JobOutcome,
    ) -> FinalizeResult:
        options = resource.options
        publish = str(options.get("publish", "push"))
        if not outcome.succeeded or publish == "none":
            return FinalizeResult()

        repo = Path(prepared.state["repo_dir"])
        branch = str(prepared.state["branch"])
        remote = str(options.get("remote", "origin"))
        push_to = str(options.get("push_to") or branch)
        set_upstream = bool(options.get("set_upstream", True))
        force = bool(options.get("force", False))

        _git(["add", "-A"], repo, check=False)
        status = _run(["status", "--porcelain"], repo)
        committed = False
        if status.stdout.strip():
            message = str(
                options.get("commit_message")
                or f"hiveling: {ctx.task_id or resource.id}"
            )
            commit = _git(["commit", "-m", message], repo)
            if commit.returncode != 0:
                raise EnvironmentError(
                    f"git: commit failed: {commit.stderr.strip()[-500:]}"
                )
            committed = True

        if publish == "commit" or not committed:
            # Nothing to push, or push not requested.
            sha = _run(["rev-parse", "HEAD"], repo).stdout.strip()
            return FinalizeResult(
                commits=[self._commit_entry(repo, branch, sha, remote, False, options)]
            )

        push_args = ["push"]
        if set_upstream:
            push_args.append("-u")
        if force:
            push_args.append("--force")
        push_args += [remote, f"{branch}:{push_to}"]
        pushed = _git(push_args, repo)
        if pushed.returncode != 0:
            raise EnvironmentError(
                f"git: push failed: {pushed.stderr.strip()[-500:]}"
            )
        sha = _run(["rev-parse", "HEAD"], repo).stdout.strip()
        return FinalizeResult(
            commits=[self._commit_entry(repo, branch, sha, remote, True, options)]
        )

    @staticmethod
    def _commit_entry(
        repo: Path, branch: str, sha: str, remote: str, pushed: bool, options: dict
    ) -> dict:
        return {
            "repo": str(options.get("repo")),
            "path": str(repo),
            "branch": branch,
            "sha": sha,
            "remote": remote if pushed else None,
            "pushed": pushed,
        }

    def teardown(self, resource: Resource, ctx: Context, prepared: Prepared) -> None:
        # Worktrees live under the per-job workspace and are removed with it.
        return None


PROVIDER = GitProvider()

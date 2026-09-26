"""The ``git`` provider: clone/reset/checkout, optional worktree, commit/push.

Two layouts share the same options:

- **worktree** (``worktree: true``): a durable clone at ``path`` and a per-task
  worktree under the job location, on ``branch``.
- **canonical** (``worktree: false``, the default): the clone at ``path`` is
  checked out in place (legacy multi-repo layouts, absolute paths).

``ref`` is the start point; ``branch`` is the target the task commits to. A
feature's tasks share one ``branch``: with the default ``branch_mode: auto`` the
provider **resumes** ``branch`` when it already exists on the remote and only
falls back to ``ref`` when it does not, so a producer, its reviewer and a gate
re-run all reopen the same tree and see each other's commits. When several tasks
share a branch and run in parallel, ``finalize`` merges the latest remote branch
back in (conflicts resolved by OpenCode) before pushing, so their pushes do not
clobber each other.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

from .. import opencode_run
from ..environment import (
    Context,
    EnvironmentError,
    FinalizeResult,
    JobOutcome,
    Prepared,
    Resource,
    check_path_allowed,
    emit_event,
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
    "merge",
}
_BRANCH_MODES = {"auto", "create", "reuse", "recreate"}
_CLEAN_MODES = {"none", "git", "full"}
_PUBLISH_MODES = {"none", "commit", "push"}
_CONFIG_NAMES = ("opencode.json", "opencode.jsonc")

# Generic prompt used when a `merge` produces conflicts: the worker resolves
# them itself with a nested OpenCode run, so the plan stays simple.
CONFLICT_PROMPT = (
    "A git merge produced conflicts in this repository. Resolve every conflict "
    "in the working tree so the merge is complete and correct:\n"
    "1. Run `git status` and inspect each conflicted file.\n"
    "2. Edit each file to combine both sides correctly and remove every "
    "conflict marker (<<<<<<<, =======, >>>>>>>).\n"
    "3. Run `git add` on the resolved files.\n"
    "Do not change anything unrelated to the conflicts and do not commit."
)


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
        branch_mode = resource.options.get("branch_mode", "auto")
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
        merge = resource.options.get("merge")
        if merge is not None and (
            not isinstance(merge, list) or not all(isinstance(r, str) for r in merge)
        ):
            raise EnvironmentError("git: 'merge' must be a list of refs")

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
        # Default to one branch per run: every task of a run shares it, so
        # consumers and gate re-runs see the producer's commits (see the module
        # docstring). A task that needs its own line sets `branch` explicitly.
        run = ctx.run_id or "run"
        return f"hiveling/{run}"

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
            # A reused clone must be refreshed: a task that consumes another
            # task's just-pushed branch needs that ref locally, and a silent
            # fetch failure would surface later as a cryptic `worktree add`
            # error. Fetch explicitly and fail loudly if it did not work.
            fetch = _git(
                ["fetch", "--all", "--prune", "--tags", "--force"],
                repo,
                check=False,
            )
            if fetch.returncode != 0:
                raise EnvironmentError(
                    f"git: fetch failed in '{repo}': {fetch.stderr.strip()[-500:]}"
                )
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
        requested_path = str(options["path"])
        worktree = bool(options.get("worktree", False))
        # With `worktree: true` the durable clone is only a staging area: the task
        # commits from the per-task worktree under `src/<id>`. Keep a relative
        # clone outside the job workspace so the agent cannot mistake it for the
        # checkout: a clone at `<workspace>/<path>` shadows `src/<id>` and
        # silently swallows the task's edits (nothing is committed or pushed).
        # Absolute paths keep their meaning (a durable, path_roots-scoped clone).
        if worktree and not Path(requested_path).expanduser().is_absolute():
            repo_path = (ctx.job_dir / "repos" / resource.id).resolve()
        else:
            repo_path = self._resolve_path(requested_path, ctx)
            check_path_allowed(repo_path, ctx, label="git")
        ref = str(options.get("ref") or "HEAD")
        branch = self._branch(resource, ctx)
        branch_mode = str(options.get("branch_mode", "auto"))
        clean = str(options.get("clean", "git"))
        cache = [str(c) for c in (options.get("cache") or [])]

        if clean == "full" and not worktree and repo_path.exists():
            shutil.rmtree(repo_path, ignore_errors=True)

        workdir = (ctx.workspace / "src" / resource.id).resolve() if worktree else repo_path
        if worktree and workdir == repo_path:
            raise EnvironmentError(
                f"git: 'path' ({requested_path}) resolves to the task worktree "
                f"'{workdir}'; choose a different path or set worktree: false"
            )

        self._ensure_clone(repo_path, str(options["repo"]))

        if worktree:
            resumed = self._add_worktree(repo_path, workdir, branch, ref, branch_mode)
        else:
            resumed = self._checkout(repo_path, branch, ref, branch_mode, clean, cache)

        # Merge the requested refs into the branch. A conflict is resolved by a
        # nested OpenCode run (see CONFLICT_PROMPT), not by failing the prepare.
        start_sha = _run(["rev-parse", "HEAD"], workdir).stdout.strip()
        merge_refs = [str(r) for r in (options.get("merge") or [])]
        merge_state = self._merge_refs(workdir, merge_refs, ctx) if merge_refs else None

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
                "resumed": resumed,
                "config_root": str(config_root) if config_root else None,
                "under_location": under_location,
                "start_sha": start_sha,
                "merge": merge_state,
            },
        )

    # ------------------------------------------------------------------- merge
    @staticmethod
    def _conflicts(repo: Path) -> tuple[list[str], list[str]]:
        unmerged = [
            line
            for line in _run(["diff", "--name-only", "--diff-filter=U"], repo).stdout.split()
            if line
        ]
        check = (
            _run(["diff", "--check"], repo).stdout
            + _run(["diff", "--cached", "--check"], repo).stdout
        )
        markers = [line for line in check.splitlines() if "conflict marker" in line]
        return unmerged, markers

    @staticmethod
    def _merge_in_progress(repo: Path) -> bool:
        return (
            _run(["rev-parse", "--verify", "--quiet", "MERGE_HEAD"], repo).returncode
            == 0
        )

    @staticmethod
    def _resolve_ref(repo: Path, ref: str, *, label: str = "ref") -> str:
        """Resolve a start point to a local ref/sha.

        A plan names a branch as it exists on the remote (e.g.
        ``hiveling/<run>/<task>``), but in a freshly fetched clone that name
        only exists as ``origin/<ref>``: pass the real ref to `git worktree add`
        / `git checkout`, otherwise git cannot find it. If the ref is not
        available yet (blog or fork, or a racing fetch), fetch once and retry
        before failing.
        """
        def lookup() -> str | None:
            for candidate in (ref, f"origin/{ref}"):
                if (
                    _run(["rev-parse", "--verify", "--quiet", candidate], repo).returncode
                    == 0
                ):
                    return candidate
            return None

        found = lookup()
        if found is None:
            _git(["fetch", "--all", "--prune", "--tags", "--force"], repo, check=False)
            found = lookup()
        if found is None:
            raise EnvironmentError(f"git: {label} not found: '{ref}'")
        return found

    def _resolve_conflicts(self, repo: Path, ctx: Context) -> None:
        if not ctx.opencode_bin:
            raise EnvironmentError(
                "git: cannot resolve merge conflicts: opencode binary not found"
            )
        standalone = "--standalone" in (ctx.opencode_flags or frozenset())
        timeout = ctx.timeout_s or ctx.default_timeout_s or None
        logger.info("git: resolving merge conflicts with opencode in %s", repo)
        try:
            result = opencode_run.run(
                ctx.opencode_bin,
                repo,
                CONFLICT_PROMPT,
                model=ctx.model,
                agent=ctx.agent,
                standalone=standalone,
                timeout_s=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise EnvironmentError(f"git: conflict resolution timed out: {exc}") from exc
        logger.info("git: conflict resolution finished (exit=%s)", result.returncode)

    def _merge_refs(self, repo: Path, refs: list[str], ctx: Context) -> dict:
        conflicts: list[str] = []
        commits: list[dict] = []
        for ref in refs:
            target = self._resolve_ref(repo, ref, label="merge ref")
            result = _git(["merge", "--no-commit", "--no-ff", target], repo, check=False)
            unmerged, markers = self._conflicts(repo)
            if unmerged or markers:
                logger.info(
                    "git: merge conflict on %s (%d file(s)), asking OpenCode",
                    ref,
                    len(unmerged) or len(markers),
                )
                emit_event(
                    ctx,
                    {
                        "type": "hiveling.merge",
                        "phase": "conflict",
                        "ref": ref,
                        "files": unmerged or markers,
                    },
                )
                conflicts.extend(unmerged or markers)
                self._resolve_conflicts(repo, ctx)
                unmerged, markers = self._conflicts(repo)
                if unmerged or markers:
                    emit_event(
                        ctx,
                        {
                            "type": "hiveling.merge",
                            "phase": "unresolved",
                            "ref": ref,
                            "files": unmerged or markers,
                        },
                    )
                    raise EnvironmentError(
                        f"git: merge conflicts on '{ref}' could not be resolved: "
                        f"{', '.join((unmerged or markers)[:20])}"
                    )
                emit_event(ctx, {"type": "hiveling.merge", "phase": "resolved", "ref": ref})
            elif result.returncode != 0:
                raise EnvironmentError(
                    f"git: merge of '{ref}' failed: {result.stderr.strip()[-500:]}"
                )

            if self._merge_in_progress(repo):
                commit = _git(["commit", "--no-edit", "-m", f"hiveling: merge {ref}"], repo)
                if commit.returncode != 0:
                    raise EnvironmentError(
                        f"git: merge commit for '{ref}' failed: "
                        f"{commit.stderr.strip()[-500:]}"
                    )
            commits.append(
                {"ref": ref, "sha": _run(["rev-parse", "HEAD"], repo).stdout.strip()}
            )
        return {"refs": refs, "conflicts": sorted(set(conflicts)), "commits": commits}

    @staticmethod
    def _is_under(path: Path, root: Path) -> bool:
        root = root.resolve()
        return path == root or root in path.parents

    @staticmethod
    def _local_branch(repo: Path, branch: str) -> bool:
        return (
            _run(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], repo)
            .returncode
            == 0
        )

    @staticmethod
    def _remote_branch(repo: Path, branch: str) -> str | None:
        ref = f"refs/remotes/origin/{branch}"
        if _run(["rev-parse", "--verify", "--quiet", ref], repo).returncode == 0:
            return ref
        return None

    def _resolve_branch(
        self, repo: Path, branch: str, branch_mode: str
    ) -> tuple[bool, bool, str | None]:
        """Decide how to obtain ``branch``.

        Returns ``(resumed, local, remote)``: ``resumed`` means the branch
        already exists and its commits must be kept; ``local``/``remote`` say
        where it exists. Enforces the ``create``/``reuse`` modes (``recreate``
        is handled by the caller with ``-B``).
        """
        local = self._local_branch(repo, branch)
        remote = None if local else self._remote_branch(repo, branch)
        if branch_mode == "create" and (local or remote):
            raise EnvironmentError(f"git: branch '{branch}' already exists")
        if branch_mode == "reuse" and not (local or remote):
            raise EnvironmentError(
                f"git: branch '{branch}' does not exist (branch_mode: reuse)"
            )
        resumed = branch_mode in ("auto", "reuse") and bool(local or remote)
        return resumed, local, remote

    def _checkout(
        self,
        repo: Path,
        branch: str,
        ref: str,
        branch_mode: str,
        clean: str,
        cache: list[str],
    ) -> bool:
        """Check out ``branch`` in place; return True when it was resumed."""
        resumed, local, remote = self._resolve_branch(repo, branch, branch_mode)
        if branch_mode == "recreate":
            start = self._resolve_ref(repo, ref, label="ref")
            result = _git(["checkout", "-B", branch, start], repo)
        elif resumed and local:
            result = _git(["checkout", branch], repo)
            if result.returncode == 0 and remote:
                # A durable clone can be behind the remote branch (another task
                # pushed): fast-forward to the latest, never rebase.
                _git(["merge", "--ff-only", remote], repo, check=False)
        elif resumed:
            result = _git(["checkout", "-b", branch, remote], repo)
        else:
            start = self._resolve_ref(repo, ref, label="ref")
            result = _git(["checkout", "-b", branch, start], repo)
        if result.returncode != 0:
            raise EnvironmentError(
                f"git: could not check out '{branch}': {result.stderr.strip()[-500:]}"
            )
        self._clean(repo, clean, cache)
        return resumed

    def _add_worktree(
        self, clone: Path, workdir: Path, branch: str, ref: str, branch_mode: str
    ) -> bool:
        """Create ``branch``'s worktree; return True when it was resumed."""
        _git(["worktree", "prune"], clone, check=False)
        if workdir.exists():
            shutil.rmtree(workdir, ignore_errors=True)
            _git(["worktree", "prune"], clone, check=False)
        resumed, local, remote = self._resolve_branch(clone, branch, branch_mode)
        workdir.parent.mkdir(parents=True, exist_ok=True)
        if branch_mode == "recreate":
            start = self._resolve_ref(clone, ref, label="ref")
            result = _git(
                ["worktree", "add", "-B", branch, str(workdir), start], clone
            )
        elif resumed and local:
            result = _git(["worktree", "add", str(workdir), branch], clone)
        elif resumed:
            result = _git(
                ["worktree", "add", "-b", branch, str(workdir), remote], clone
            )
        else:
            # The branch does not exist yet: create it from the start point
            # (a local ref or `origin/<ref>`), which `_resolve_ref` resolves.
            start = self._resolve_ref(clone, ref, label="ref")
            result = _git(
                ["worktree", "add", "-b", branch, str(workdir), start], clone
            )
        if result.returncode != 0:
            raise EnvironmentError(
                f"git: could not create worktree for '{branch}': "
                f"{result.stderr.strip()[-500:]}"
            )
        return resumed

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
        has_status_changes = bool(status.stdout.strip())
        committed = False
        if has_status_changes:
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

        # Push when this run produced a commit, or when the branch already moved
        # during prepare (an integration `merge` commits before the task runs).
        start_sha = str(prepared.state.get("start_sha") or "")
        head = _run(["rev-parse", "HEAD"], repo).stdout.strip()
        moved = bool(start_sha and head and head != start_sha)
        resolved = bool((prepared.state.get("merge") or {}).get("commits"))
        resumed = bool(prepared.state.get("resumed"))

        # A task that asked to push must have something to push. Publishing an
        # empty branch means the tree the provider commits was not the one the
        # agent edited (the classic `<workspace>/<path>` vs `<workspace>/src/<id>`
        # mix-up), so fail instead of silently "succeeding" with nothing on the
        # remote. A branch advanced by a prepare-time `merge` is a real change,
        # and a resumed branch that produced no change is a legitimate no-op
        # (the task agrees with the tree its reviewer left): only a fresh branch
        # that produced nothing is an error. Review tasks that consume a branch
        # and touch nothing should use `publish: none` (or `commit`).
        if (
            publish == "push"
            and not committed
            and not moved
            and not resolved
            and not resumed
        ):
            raise EnvironmentError(
                f"git: nothing to push for branch '{branch}' ({repo}); the task "
                f"made no change. Is the prompt pointing at the checkout "
                f"('src/{resource.id}' with worktree: true), or should this task "
                f"use publish: none?"
            )

        if publish == "commit" or (not committed and not moved and not resumed):
            # Nothing to push, or push not requested.
            return FinalizeResult(
                commits=[self._commit_entry(repo, branch, head, remote, False, options)]
            )

        if not force:
            # Tasks of a feature share one branch. If a parallel task advanced it
            # meanwhile, merge that work back so the push is fast-forward.
            self._reconcile_remote(repo, push_to, remote, ctx)

        push_args = ["push"]
        if set_upstream:
            push_args.append("-u")
        if force:
            push_args.append("--force")
        push_args += [remote, f"{branch}:{push_to}"]
        pushed = _git(push_args, repo)
        if pushed.returncode != 0 and not force:
            # Lost a race with another task on the shared branch: reconcile
            # again and retry once.
            self._reconcile_remote(repo, push_to, remote, ctx)
            pushed = _git(push_args, repo)
        if pushed.returncode != 0:
            raise EnvironmentError(
                f"git: push failed: {pushed.stderr.strip()[-500:]}"
            )
        sha = _run(["rev-parse", "HEAD"], repo).stdout.strip()
        return FinalizeResult(
            commits=[self._commit_entry(repo, branch, sha, remote, True, options)]
        )

    def _reconcile_remote(
        self, repo: Path, branch: str, remote: str, ctx: Context
    ) -> None:
        """Merge the latest remote ``branch`` into the local one before a push.

        Tasks of a feature share one branch: each resumes it at prepare and
        commits. A parallel task may push meanwhile, so merge its work back
        (conflicts are resolved by OpenCode, as for a prepare-time ``merge``) and
        keep the push fast-forward.
        """
        fetch = _git(
            ["fetch", remote, "--prune", "--tags", "--force"], repo, check=False
        )
        if fetch.returncode != 0:
            return  # offline: let the push surface the real error
        tracking = f"refs/remotes/{remote}/{branch}"
        if _run(["rev-parse", "--verify", "--quiet", tracking], repo).returncode != 0:
            return
        if _run(["merge-base", "--is-ancestor", tracking, "HEAD"], repo).returncode == 0:
            return
        if _git(["merge", "--ff-only", tracking], repo, check=False).returncode == 0:
            return
        result = _git(["merge", "--no-ff", "--no-edit", tracking], repo, check=False)
        unmerged, markers = self._conflicts(repo)
        if unmerged or markers:
            emit_event(
                ctx,
                {
                    "type": "hiveling.merge",
                    "phase": "conflict",
                    "ref": tracking,
                    "files": unmerged or markers,
                },
            )
            self._resolve_conflicts(repo, ctx)
            unmerged, markers = self._conflicts(repo)
            if unmerged or markers:
                emit_event(
                    ctx,
                    {
                        "type": "hiveling.merge",
                        "phase": "unresolved",
                        "ref": tracking,
                        "files": unmerged or markers,
                    },
                )
                raise EnvironmentError(
                    f"git: merge conflicts on '{remote}/{branch}' could not be "
                    f"resolved: {', '.join((unmerged or markers)[:20])}"
                )
            emit_event(
                ctx, {"type": "hiveling.merge", "phase": "resolved", "ref": tracking}
            )
        elif result.returncode != 0:
            raise EnvironmentError(
                f"git: could not merge '{remote}/{branch}' before push: "
                f"{result.stderr.strip()[-500:]}"
            )
        if self._merge_in_progress(repo):
            commit = _git(
                ["commit", "--no-edit", "-m", f"hiveling: merge {remote}/{branch}"],
                repo,
            )
            if commit.returncode != 0:
                raise EnvironmentError(
                    f"git: merge commit for '{remote}/{branch}' failed: "
                    f"{commit.stderr.strip()[-500:]}"
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

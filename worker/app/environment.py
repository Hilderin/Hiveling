"""Environment provisioning: materialize a task's resources around OpenCode.

A plan declares typed ``resources``; each one is handled by a worker-side
``Provider`` with a small ``validate``/``prepare``/``finalize``/``teardown``
lifecycle. The ``ephemeral`` provider is the default and reproduces the
historical behavior (a fresh working directory, zip in/out); other providers
(git, path, env, secret, command) are added by later stages.

Lifecycle, driven by :mod:`app.executor`:

    validate -> prepare -> (snapshot, opencode run, snapshot) -> finalize -> teardown

A validate/prepare failure aborts the job *before* OpenCode starts; a finalize
failure marks the job failed; teardown always runs.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger("hiveling.worker.environment")


class EnvironmentError(Exception):
    """A resource could not be validated, prepared or finalized."""


@dataclass
class Resource:
    """One typed resource from the plan."""

    type: str
    id: str
    options: dict = field(default_factory=dict)
    when: dict = field(default_factory=dict)


@dataclass
class Context:
    """What a provider may inspect while provisioning."""

    workspace: Path
    job_dir: Path
    task_id: str = ""
    run_id: str | None = None
    path_roots: list[str] = field(default_factory=list)
    capabilities: dict = field(default_factory=dict)
    # A worker-side SecretStore (resolves names, never values, from the plan).
    secrets: Any = None
    # OpenCode invocation details, so a provider can run a nested OpenCode
    # (e.g. the git provider resolving merge conflicts).
    opencode_bin: str | None = None
    opencode_flags: frozenset = frozenset()
    model: str | None = None
    agent: str | None = None
    timeout_s: float | None = None
    default_timeout_s: float = 900.0


@dataclass
class Prepared:
    """State returned by ``Provider.prepare`` and consumed around the run."""

    resource: Resource
    env: dict[str, str] = field(default_factory=dict)
    paths: list[dict] = field(default_factory=list)
    opencode: dict | None = None
    locks: list = field(default_factory=list)
    reports: dict = field(default_factory=dict)
    artifacts: list = field(default_factory=list)
    state: dict = field(default_factory=dict)


@dataclass
class JobOutcome:
    """The OpenCode outcome handed to ``Provider.finalize``."""

    status: str
    succeeded: bool
    changed_files: list[str] = field(default_factory=list)
    workdir: Path | None = None


@dataclass
class FinalizeResult:
    changed_files: list[str] = field(default_factory=list)
    commits: list[dict] = field(default_factory=list)
    artifacts: list = field(default_factory=list)
    reports: dict = field(default_factory=dict)


class Provider(Protocol):
    name: str

    def validate(self, resource: Resource, ctx: Context) -> None: ...

    def prepare(self, resource: Resource, ctx: Context) -> Prepared: ...

    def finalize(
        self,
        resource: Resource,
        ctx: Context,
        prepared: Prepared,
        outcome: JobOutcome,
    ) -> FinalizeResult: ...

    def teardown(self, resource: Resource, ctx: Context, prepared: Prepared) -> None: ...


def default_registry() -> dict[str, Provider]:
    """Providers implemented by this worker build, keyed by ``type``."""
    from .providers import command, env, ephemeral, git, path, secret

    return {
        ephemeral.PROVIDER.name: ephemeral.PROVIDER,
        env.PROVIDER.name: env.PROVIDER,
        secret.PROVIDER.name: secret.PROVIDER,
        git.PROVIDER.name: git.PROVIDER,
        path.PROVIDER.name: path.PROVIDER,
        command.PROVIDER.name: command.PROVIDER,
    }


class Environment:
    """Provision a task's resources around a single OpenCode run."""

    def __init__(
        self,
        resources: list[Resource],
        ctx: Context,
        registry: dict[str, Provider] | None = None,
    ):
        self.ctx = ctx
        self.registry = registry if registry is not None else default_registry()
        self.resources = resources
        self.prepared: list[Prepared] = []

    def _provider(self, resource: Resource) -> Provider:
        provider = self.registry.get(resource.type)
        if provider is None:
            known = ", ".join(sorted(self.registry)) or "none"
            raise EnvironmentError(
                f"unknown resource provider '{resource.type}' "
                f"(worker has: {known})"
            )
        return provider

    def prepare(self) -> None:
        """Validate every resource, then prepare them in order."""
        # Enforce the worker's provider allowlist (advertised capabilities),
        # so opted-out providers such as `command` are refused even though the
        # code is present.
        allowed = None
        if self.ctx.capabilities:
            configured = self.ctx.capabilities.get("providers")
            if configured is not None:
                allowed = {str(provider) for provider in configured}
        for resource in self.resources:
            if allowed is not None and resource.type not in allowed:
                raise EnvironmentError(
                    f"resource provider '{resource.type}' is not enabled on this "
                    f"worker (enabled: {', '.join(sorted(allowed)) or 'none'})"
                )
        for resource in self.resources:
            self._provider(resource).validate(resource, self.ctx)
        try:
            for resource in self.resources:
                provider = self._provider(resource)
                logger.info(
                    "environment: preparing %s (%s)", resource.id, resource.type
                )
                self.prepared.append(provider.prepare(resource, self.ctx))
        except Exception as exc:
            self.teardown()
            self.prepared.clear()
            if isinstance(exc, EnvironmentError):
                raise
            raise EnvironmentError(f"prepare failed: {exc}") from exc

    @property
    def env(self) -> dict[str, str]:
        """Environment variables contributed by every prepared resource."""
        merged: dict[str, str] = {}
        for prepared in self.prepared:
            merged.update(prepared.env)
        return merged

    @property
    def opencode_fragments(self) -> list[dict]:
        """OpenCode config fragments contributed by the prepared resources."""
        return [p.opencode for p in self.prepared if p.opencode]

    def config_roots(self) -> list[tuple[Path, bool]]:
        """``(root, under_location)`` provenance reported by the providers."""
        roots: list[tuple[Path, bool]] = []
        for prepared in self.prepared:
            root = prepared.state.get("config_root")
            if root:
                roots.append((Path(root), bool(prepared.state.get("under_location"))))
        return roots

    def finalize(self, outcome: JobOutcome) -> FinalizeResult:
        """Finalize prepared resources in reverse order (publish results)."""
        merged = FinalizeResult()
        for prepared in reversed(self.prepared):
            provider = self._provider(prepared.resource)
            try:
                outcome_result = provider.finalize(
                    prepared.resource, self.ctx, prepared, outcome
                )
            except Exception as exc:
                if isinstance(exc, EnvironmentError):
                    raise
                raise EnvironmentError(
                    f"resource '{prepared.resource.id}' finalize failed: {exc}"
                ) from exc
            merged.changed_files += outcome_result.changed_files
            merged.commits += outcome_result.commits
            merged.artifacts += outcome_result.artifacts
            merged.reports.update(outcome_result.reports)
        return merged

    def teardown(self) -> None:
        """Release locks and clean up, always (never raises)."""
        for prepared in reversed(self.prepared):
            provider = self._provider(prepared.resource)
            try:
                provider.teardown(prepared.resource, self.ctx, prepared)
            except Exception:
                logger.warning(
                    "environment: teardown failed for %s",
                    prepared.resource.id,
                    exc_info=True,
                )


def summarize_working_dirs(prepared: list[Prepared], workspace: Path) -> str:
    """A short prompt preamble telling the agent where each resource lives.

    OpenCode runs from the job workspace, but resource working trees do not: a
    ``git`` resource with ``worktree: true`` is checked out under
    ``<workspace>/src/<id>`` while the workspace root stays a project shell.
    Without this note the agent only learns the layout from the plan prompt,
    which is easy to get wrong and silently lose the edits. Prepending the
    actual directories removes that class of failure.
    """
    lines: list[str] = []
    for item in prepared:
        resource = item.resource
        for entry in item.paths:
            raw = str(entry.get("path") or "")
            if not raw:
                continue
            path = Path(raw)
            try:
                shown = "./" + path.relative_to(workspace).as_posix()
            except ValueError:
                shown = path.as_posix()
            detail = ""
            if resource.type == "git":
                bits = []
                branch = (item.state or {}).get("branch")
                if branch:
                    bits.append(f"branch {branch}")
                bits.append(f"publish {resource.options.get('publish', 'push')}")
                detail = " (" + ", ".join(bits) + ")"
            elif resource.type == "path":
                mode = entry.get("mode") or "rw"
                detail = f" ({mode})"
            lines.append(f"- {resource.type} '{resource.id}': {shown}{detail}")
    if not lines:
        return ""
    return (
        "[Working environment]\n"
        "OpenCode runs from the job workspace root. Resource working "
        "directories:\n"
        + "\n".join(lines)
        + "\nDo repository file and git operations inside the resource working "
        "directory, not at the workspace root.\n"
        "[/Working environment]\n\n"
    )


def emit_event(ctx: Context, event: dict) -> None:
    """Append a synthetic event to the job's ``events.jsonl``.

    Providers use this for traceability (e.g. the git provider records merge
    conflicts and their resolution). The executor truncates the file at the
    start of a job and appends the OpenCode stream to it, so both end up in the
    same per-task event log shown in the dashboard.
    """
    try:
        path = Path(ctx.job_dir) / "events.jsonl"
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        logger.debug("could not write event %s", event, exc_info=True)


def check_path_allowed(path: Path, ctx: Context, *, label: str = "path") -> None:
    """Enforce the worker ``path_roots`` allowlist.

    Relative paths (resolved under the job workspace) and paths inside the
    workspace are always allowed; an absolute path outside it needs a
    ``path_roots`` entry (``["*"]`` disables the check explicitly).
    """
    if ctx.path_roots == ["*"]:
        return
    resolved = path.resolve()
    workspace = ctx.workspace.resolve()
    if resolved == workspace or workspace in resolved.parents:
        return
    if not ctx.path_roots:
        raise EnvironmentError(
            f"{label}: '{path}' is outside the job workspace and the worker has "
            f"no path_roots configured"
        )
    for root in ctx.path_roots:
        root_path = Path(root).expanduser().resolve()
        if resolved == root_path or root_path in resolved.parents:
            return
    raise EnvironmentError(f"{label}: '{path}' is outside the worker path_roots")


def resolve_worker_path(value: str, ctx: Context, *, label: str = "path") -> Path:
    """Resolve a plan-declared path (relative to the workspace) and check it."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = ctx.workspace / path
    path = path.resolve()
    check_path_allowed(path, ctx, label=label)
    return path


def external_permissions(path: Path, *, write: bool = True) -> dict:
    """OpenCode permission rules granting access to a path outside the location."""
    pattern = str(Path(path).resolve() / "**")
    rules = [
        {"action": "read", "resource": pattern, "effect": "allow"},
        {"action": "external_directory", "resource": pattern, "effect": "allow"},
    ]
    if write:
        rules.append({"action": "edit", "resource": pattern, "effect": "allow"})
    return {"permissions": rules}


def parse_resources(raw_resources: list | None) -> list[Resource]:
    """Build :class:`Resource` objects from the wire format."""
    resources: list[Resource] = []
    for index, raw in enumerate(raw_resources or []):
        if not isinstance(raw, dict):
            raise EnvironmentError(f"invalid resource at index {index}: {raw!r}")
        rtype = str(raw.get("type") or "").strip()
        if not rtype:
            raise EnvironmentError(f"resource at index {index} has no 'type'")
        resources.append(
            Resource(
                type=rtype,
                id=str(raw.get("id") or f"{rtype}-{index}"),
                options=dict(raw.get("with") or {}),
                when=dict(raw.get("when") or {}),
            )
        )
    return resources

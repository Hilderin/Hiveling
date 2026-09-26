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
    from .providers import ephemeral

    return {ephemeral.PROVIDER.name: ephemeral.PROVIDER}


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

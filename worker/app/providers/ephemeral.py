"""The default provider: a fresh, isolated working directory.

It reproduces the historical worker behavior exactly: the job runs in
``<job>/work`` (created by the executor), input files are extracted into it and
changed files are zipped back. It prepares no environment, injects no variables
and publishes nothing, so it is a pure no-op around the lifecycle.
"""

from __future__ import annotations

from ..environment import (
    Context,
    FinalizeResult,
    JobOutcome,
    Prepared,
    Resource,
)

# Options accepted but ignored: the filesystem behavior is already handled by
# the executor (fresh workdir, download handled server-side).
_ALLOWED_OPTIONS = {"clean", "download"}


class EphemeralProvider:
    name = "ephemeral"

    def validate(self, resource: Resource, ctx: Context) -> None:
        unknown = set(resource.options) - _ALLOWED_OPTIONS
        if unknown:
            raise EnvironmentError(
                f"ephemeral: unknown option(s): {', '.join(sorted(unknown))}"
            )

    def prepare(self, resource: Resource, ctx: Context) -> Prepared:
        return Prepared(resource=resource)

    def finalize(
        self,
        resource: Resource,
        ctx: Context,
        prepared: Prepared,
        outcome: JobOutcome,
    ) -> FinalizeResult:
        return FinalizeResult()

    def teardown(self, resource: Resource, ctx: Context, prepared: Prepared) -> None:
        return None


# Imported here to keep the option error meaningful without a top-level cycle.
from ..environment import EnvironmentError  # noqa: E402

PROVIDER = EphemeralProvider()

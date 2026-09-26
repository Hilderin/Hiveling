"""The ``secret`` provider: inject a named secret from the worker store.

The plan only carries the secret *name*; the value is resolved on the worker
(``secrets.yaml`` or the worker's environment) and exposed as an environment
variable for the OpenCode run.
"""

from __future__ import annotations

import os

from ..environment import (
    Context,
    EnvironmentError,
    FinalizeResult,
    JobOutcome,
    Prepared,
    Resource,
)

_ALLOWED = {"name", "as", "required"}


class SecretProvider:
    name = "secret"

    def validate(self, resource: Resource, ctx: Context) -> None:
        unknown = set(resource.options) - _ALLOWED
        if unknown:
            raise EnvironmentError(
                f"secret: unknown option(s): {', '.join(sorted(unknown))}"
            )
        if not resource.options.get("name"):
            raise EnvironmentError("secret: 'name' is required")

    def prepare(self, resource: Resource, ctx: Context) -> Prepared:
        name = str(resource.options["name"])
        target = str(resource.options.get("as") or name)
        required = resource.options.get("required", True)

        value = None
        if ctx.secrets is not None:
            value = ctx.secrets.get(name)
        if value is None:
            value = os.environ.get(name)
        if value is None:
            if required:
                raise EnvironmentError(f"secret '{name}' not found on the worker")
            return Prepared(resource=resource)
        return Prepared(resource=resource, env={target: value})

    def finalize(
        self, resource: Resource, ctx: Context, prepared: Prepared, outcome: JobOutcome
    ) -> FinalizeResult:
        return FinalizeResult()

    def teardown(self, resource: Resource, ctx: Context, prepared: Prepared) -> None:
        return None


PROVIDER = SecretProvider()

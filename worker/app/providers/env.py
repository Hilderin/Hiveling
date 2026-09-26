"""The ``env`` provider: inject environment variables for the OpenCode run.

Values may reference the worker's own environment with ``${NAME}``; an unknown
name is left literal.
"""

from __future__ import annotations

import os
import re

from ..environment import (
    Context,
    EnvironmentError,
    FinalizeResult,
    JobOutcome,
    Prepared,
    Resource,
)

_EXPAND = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand(value: str, environ: dict) -> str:
    def replace(match: re.Match) -> str:
        name = match.group(1)
        return str(environ[name]) if name in environ else match.group(0)

    return _EXPAND.sub(replace, value)


class EnvProvider:
    name = "env"

    def validate(self, resource: Resource, ctx: Context) -> None:
        unknown = set(resource.options) - {"vars"}
        if unknown:
            raise EnvironmentError(
                f"env: unknown option(s): {', '.join(sorted(unknown))}"
            )
        variables = resource.options.get("vars")
        if not isinstance(variables, dict):
            raise EnvironmentError("env: 'vars' must be a mapping")

    def prepare(self, resource: Resource, ctx: Context) -> Prepared:
        env = {
            str(key): _expand(str(value), os.environ)
            for key, value in (resource.options.get("vars") or {}).items()
        }
        return Prepared(resource=resource, env=env)

    def finalize(
        self, resource: Resource, ctx: Context, prepared: Prepared, outcome: JobOutcome
    ) -> FinalizeResult:
        return FinalizeResult()

    def teardown(self, resource: Resource, ctx: Context, prepared: Prepared) -> None:
        return None


PROVIDER = EnvProvider()

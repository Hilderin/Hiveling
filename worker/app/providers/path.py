"""The ``path`` provider: expose an existing folder to OpenCode.

Nothing is cloned or reset; the folder is simply advertised as a resource so
OpenCode may read (``mode: ro``) or read/write (``mode: rw``) it. Absolute
paths must live under the worker's ``path_roots``. This is the provider for
legacy layouts: fixtures, local database data directories and shared caches that
must stay at a fixed location.
"""

from __future__ import annotations

from ..environment import (
    Context,
    EnvironmentError,
    FinalizeResult,
    JobOutcome,
    Prepared,
    Resource,
    external_permissions,
    resolve_worker_path,
)

_ALLOWED = {"path", "mode", "visible"}


class PathProvider:
    name = "path"

    def validate(self, resource: Resource, ctx: Context) -> None:
        unknown = set(resource.options) - _ALLOWED
        if unknown:
            raise EnvironmentError(
                f"path: unknown option(s): {', '.join(sorted(unknown))}"
            )
        if not resource.options.get("path"):
            raise EnvironmentError("path: 'path' is required")
        if str(resource.options.get("mode", "ro")) not in {"ro", "rw"}:
            raise EnvironmentError("path: 'mode' must be 'ro' or 'rw'")

    def prepare(self, resource: Resource, ctx: Context) -> Prepared:
        path = resolve_worker_path(str(resource.options["path"]), ctx, label="path")
        if not path.exists():
            raise EnvironmentError(f"path: '{path}' does not exist")
        mode = str(resource.options.get("mode", "ro"))
        visible = bool(resource.options.get("visible", True))
        return Prepared(
            resource=resource,
            paths=[{"path": str(path), "mode": mode, "visible": visible}],
            opencode=external_permissions(path, write=(mode == "rw")),
            state={"path": str(path), "mode": mode},
        )

    def finalize(
        self, resource: Resource, ctx: Context, prepared: Prepared, outcome: JobOutcome
    ) -> FinalizeResult:
        return FinalizeResult()

    def teardown(self, resource: Resource, ctx: Context, prepared: Prepared) -> None:
        return None


PROVIDER = PathProvider()

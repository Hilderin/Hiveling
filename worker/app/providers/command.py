"""The ``command`` provider: an explicit escape hatch.

Runs shell commands at prepare and/or finalize, for setups without a dedicated
provider yet (docker, k8s, a database reset, ...). It is **opt-in**: a worker
only accepts it when ``command`` is listed in its capabilities ``providers``.

Commands run with the job workspace as cwd (or ``cwd``), inherit the worker
environment plus ``env``, and receive ``HIVELING_PHASE`` / ``HIVELING_TASK_ID`` /
``HIVELING_RUN_ID``. A non-zero exit fails the phase (a finalize failure fails
the task, as for every provider).
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from ..environment import (
    Context,
    EnvironmentError,
    FinalizeResult,
    JobOutcome,
    Prepared,
    Resource,
    resolve_worker_path,
)

logger = logging.getLogger("hiveling.worker.command")

_ALLOWED = {"prepare", "finalize", "shell", "env", "cwd", "timeout_s"}
_SHELLS = {"bash", "sh", "cmd", "powershell", "pwsh"}


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    raise EnvironmentError(f"command: expected a string or list, got {value!r}")


def _shell_argv(command: str, shell: str | None) -> list[str]:
    if shell in {"cmd"}:
        return ["cmd", "/c", command]
    if shell in {"powershell", "pwsh"}:
        return [shell, "-NoProfile", "-Command", command]
    if shell in {"bash", "sh"}:
        return [shell, "-c", command]
    if os.name == "nt":
        return ["cmd", "/c", command]
    return ["/bin/sh", "-c", command]


class CommandProvider:
    name = "command"

    def validate(self, resource: Resource, ctx: Context) -> None:
        unknown = set(resource.options) - _ALLOWED
        if unknown:
            raise EnvironmentError(
                f"command: unknown option(s): {', '.join(sorted(unknown))}"
            )
        shell = resource.options.get("shell")
        if shell is not None and str(shell) not in _SHELLS:
            raise EnvironmentError(
                f"command: 'shell' must be one of {sorted(_SHELLS)}"
            )
        for phase in ("prepare", "finalize"):
            _as_list(resource.options.get(phase))

    def _run(self, resource: Resource, ctx: Context, phase: str) -> None:
        commands = _as_list(resource.options.get(phase))
        if not commands:
            return
        shell = resource.options.get("shell")
        cwd = ctx.workspace
        if resource.options.get("cwd"):
            cwd = resolve_worker_path(str(resource.options["cwd"]), ctx, label="command")
        cwd.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env.update(
            {str(key): str(value) for key, value in (resource.options.get("env") or {}).items()}
        )
        env["HIVELING_PHASE"] = phase
        env["HIVELING_TASK_ID"] = ctx.task_id
        env["HIVELING_RUN_ID"] = str(ctx.run_id or "")
        timeout = resource.options.get("timeout_s")
        for command in commands:
            logger.info("command: %s %s: %s", resource.id, phase, command)
            result = subprocess.run(
                _shell_argv(command, str(shell) if shell else None),
                cwd=str(cwd),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=float(timeout) if timeout else None,
            )
            if result.stdout.strip():
                logger.info("command: %s stdout: %s", resource.id, result.stdout.strip()[-2000:])
            if result.returncode != 0:
                tail = (result.stderr or result.stdout or "").strip()[-1000:]
                raise EnvironmentError(
                    f"command '{command}' ({phase}) failed with exit {result.returncode}: {tail}"
                )

    def prepare(self, resource: Resource, ctx: Context) -> Prepared:
        self._run(resource, ctx, "prepare")
        return Prepared(resource=resource)

    def finalize(
        self, resource: Resource, ctx: Context, prepared: Prepared, outcome: JobOutcome
    ) -> FinalizeResult:
        self._run(resource, ctx, "finalize")
        return FinalizeResult()

    def teardown(self, resource: Resource, ctx: Context, prepared: Prepared) -> None:
        return None


PROVIDER = CommandProvider()

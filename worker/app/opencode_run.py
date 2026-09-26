"""Run the OpenCode CLI for internal worker needs.

Used by providers that must invoke OpenCode themselves, e.g. the git provider
resolving merge conflicts. It mirrors the executor's invocation: V2 runs with
``--standalone`` (a private server bound to the process cwd) and ``PWD`` is set
to the working directory, so the nested run stays in the repository.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def build_command(
    binary: str,
    prompt: str,
    *,
    model: str | None = None,
    agent: str | None = None,
    auto: bool = True,
    standalone: bool = False,
    format_json: bool = True,
) -> list[str]:
    command = [binary, "run"]
    if format_json:
        command += ["--format", "json"]
    if standalone:
        command.append("--standalone")
    if auto:
        command.append("--auto")
    if model:
        command += ["--model", model]
    if agent:
        command += ["--agent", agent]
    command.append(prompt)
    return command


def run(
    binary: str,
    cwd: Path,
    prompt: str,
    *,
    model: str | None = None,
    agent: str | None = None,
    auto: bool = True,
    standalone: bool = False,
    timeout_s: float | None = None,
    env: dict | None = None,
) -> subprocess.CompletedProcess:
    run_env = os.environ.copy()
    if env:
        run_env.update({str(key): str(value) for key, value in env.items()})
    if os.name != "nt":
        run_env["PWD"] = str(cwd)
    command = build_command(
        binary,
        prompt,
        model=model,
        agent=agent,
        auto=auto,
        standalone=standalone,
    )
    return subprocess.run(
        command,
        cwd=str(cwd),
        env=run_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_s,
    )

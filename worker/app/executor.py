"""Run a task through the OpenCode CLI.

The worker spawns ``opencode run --format json`` as a subprocess, captures the
JSON events, then computes added/modified/deleted files.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from functools import lru_cache
from pathlib import Path

from .config import Config
from .snapshot import diff, snapshot
from .state import Job


def resolve_binary(configured: str | None) -> str:
    """Locate the opencode binary (explicit config, then PATH)."""
    if configured:
        return configured
    found = shutil.which("opencode")
    if not found:
        raise FileNotFoundError(
            "'opencode' binary not found. Install it or set OPENCODE_BIN / --opencode-bin."
        )
    return found


def _new_session_kwargs() -> dict:
    if os.name == "nt":  # pragma: no cover - Windows
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _terminate(process: subprocess.Popen) -> None:
    """Kill the OpenCode process and its children."""
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":  # pragma: no cover - Windows
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                check=False,
            )
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except Exception:
        try:
            process.terminate()
        except Exception:
            pass
    try:
        process.wait(timeout=10)
        return
    except Exception:
        pass
    try:
        if os.name == "nt":  # pragma: no cover - Windows
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                check=False,
            )
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


@lru_cache(maxsize=None)
def _run_flags(binary: str) -> frozenset[str]:
    """Flags supported by ``opencode run``, probed once per binary.

    OpenCode V1 exposes ``--dir`` and ``--variant``; V2 dropped both (the
    working directory comes from the process cwd and the variant is part of
    the model string as ``provider/model#variant``). If the probe fails we
    assume V2, the syntax used by current releases.
    """
    try:
        proc = subprocess.run(
            [binary, "run", "--help"], capture_output=True, text=True, timeout=20
        )
    except Exception:
        return frozenset()
    return frozenset(re.findall(r"--[a-z][a-z0-9-]*", proc.stdout + proc.stderr))


def _build_command(binary: str, job: Job, workdir: Path) -> list[str]:
    spec = job.spec
    flags = _run_flags(binary)
    command = [binary, "run", "--format", "json"]
    if "--dir" in flags:
        command += ["--dir", str(workdir)]
    if spec.get("auto", True):
        command.append("--auto")
    model = spec.get("model")
    variant = spec.get("variant")
    if variant and "--variant" in flags:
        command += ["--variant", variant]
        variant = None
    if model:
        if variant and "#" not in model:
            model = f"{model}#{variant}"
        command += ["--model", model]
    if spec.get("agent"):
        command += ["--agent", spec["agent"]]
    if spec.get("title"):
        command += ["--title", spec["title"]]
    for extra in spec.get("files") or []:
        command += ["--file", str(extra)]
    command.append(spec["prompt"])
    return command


class _EventAccumulator:
    """Collects the useful fields from OpenCode's JSON events."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.texts: list[str] = []
        self.session_id: str | None = None
        self.tools: list[str] = []
        self.tokens: dict | None = None
        self.cost: float | None = None
        self.errors: list[str] = []

    def handle(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return
        with self.lock:
            session_id = event.get("sessionID")
            if session_id:
                self.session_id = session_id
            part = event.get("part") or {}
            event_type = event.get("type")
            if event_type == "text":
                text = part.get("text")
                if text:
                    self.texts.append(text)
            elif event_type == "tool_use":
                tool = part.get("tool")
                title = (part.get("state") or {}).get("title")
                if tool:
                    self.tools.append(f"{tool}: {title}" if title else str(tool))
            elif event_type in ("step_finish", "step-finish"):
                if part.get("tokens"):
                    self.tokens = part["tokens"]
                if part.get("cost") is not None:
                    self.cost = part["cost"]
            elif event_type == "error":
                self.errors.append(self._format_error(event.get("error")))

    @staticmethod
    def _format_error(error: object) -> str:
        if not isinstance(error, dict):
            return str(error) if error else "unknown error"
        message = (error.get("data") or {}).get("message") or error.get("name") or "unknown error"
        ref = (error.get("data") or {}).get("ref")
        return f"{message} (ref {ref})" if ref else str(message)

    def result_text(self) -> str:
        with self.lock:
            return "\n".join(self.texts).strip()

    def error_text(self) -> str:
        with self.lock:
            return "; ".join(self.errors)


def _pump(stream, sink, accumulator: _EventAccumulator | None) -> None:
    """Read a stream line by line, write it to ``sink`` and parse it."""
    try:
        for line in iter(stream.readline, ""):
            sink.write(line)
            sink.flush()
            if accumulator is not None:
                accumulator.handle(line)
    except Exception:
        pass
    finally:
        try:
            stream.close()
        except Exception:
            pass


def execute(job: Job, config: Config) -> None:
    """Run the job (called from a dedicated thread). Updates ``job``."""
    workdir = job.workdir
    workdir.mkdir(parents=True, exist_ok=True)
    events_path = job.dir / "events.jsonl"
    stderr_path = job.dir / "stderr.log"
    before_path = job.dir / "snapshot_before.json"

    before = snapshot(workdir)
    before_path.write_text(json.dumps(before, indent=2), encoding="utf-8")

    accumulator = _EventAccumulator()
    timeout_s = float(job.spec.get("timeout_s") or config.default_timeout_s)

    job.status = "running"
    job.started_at = time.time()
    job.save()

    error: str | None = None
    return_code: int | None = None
    timed_out = False

    try:
        binary = resolve_binary(config.opencode_bin)
        command = _build_command(binary, job, workdir)
        env = os.environ.copy()
        for key, value in (job.spec.get("env") or {}).items():
            env[str(key)] = str(value)

        with open(events_path, "w", encoding="utf-8") as events_file, open(
            stderr_path, "w", encoding="utf-8"
        ) as stderr_file:
            process = subprocess.Popen(
                command,
                cwd=str(workdir),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                **_new_session_kwargs(),
            )
            job.process = process

            stdout_thread = threading.Thread(
                target=_pump, args=(process.stdout, events_file, accumulator), daemon=True
            )
            stderr_thread = threading.Thread(
                target=_pump, args=(process.stderr, stderr_file, None), daemon=True
            )
            stdout_thread.start()
            stderr_thread.start()

            try:
                return_code = process.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                error = f"timeout after {timeout_s:.0f}s"
                _terminate(process)
                return_code = process.poll()

            stdout_thread.join(timeout=10)
            stderr_thread.join(timeout=10)

    except FileNotFoundError as exc:
        error = str(exc)
    except Exception as exc:  # pragma: no cover - safety net
        error = f"unexpected error: {exc!r}"

    job.process = None

    after = snapshot(workdir)
    added, modified, deleted = diff(before, after)
    (job.dir / "snapshot_after.json").write_text(json.dumps(after, indent=2), encoding="utf-8")

    job.result_text = accumulator.result_text()
    job.session_id = accumulator.session_id
    job.tool_calls = accumulator.tools
    job.tokens = accumulator.tokens
    job.cost = accumulator.cost
    job.added = added
    job.modified = modified
    job.deleted = deleted
    job.exit_code = return_code

    if job.shutdown_requested:
        job.status = "failed"
        job.error = "worker shutting down"
    elif job.cancel_requested:
        job.status = "canceled"
        job.error = "job canceled by the server"
    elif timed_out:
        job.status = "failed"
        job.error = error or f"timeout after {timeout_s:.0f}s"
    elif error is not None:
        job.status = "failed"
        job.error = error
    elif return_code == 0:
        job.status = "succeeded"
    else:
        job.status = "failed"
        stderr_tail = ""
        try:
            stderr_tail = stderr_path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            pass
        message = stderr_tail or accumulator.error_text()
        if message:
            job.error = message[-2000:]

    job.finished_at = time.time()
    job.save()

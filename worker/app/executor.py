"""Run a task through the OpenCode CLI.

The worker spawns ``opencode run --format json`` as a subprocess, captures the
JSON events, then computes added/modified/deleted files.
"""

from __future__ import annotations

import json
import logging
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
from .environment import (
    Context,
    Environment,
    EnvironmentError,
    JobOutcome,
    parse_resources,
    summarize_working_dirs,
)
from .snapshot import diff, snapshot
from .state import Job

logger = logging.getLogger("hiveling.worker.executor")
_PROMPT_PREVIEW = 300


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
    except Exception as exc:
        logger.warning(
            "could not probe flags of %s (%r); assuming OpenCode v2", binary, exc
        )
        return frozenset()
    return frozenset(re.findall(r"--[a-z][a-z0-9-]*", proc.stdout + proc.stderr))


def _describe_command(command: list[str]) -> str:
    """Render a command for logs, truncating the (potentially huge) prompt."""
    if not command:
        return ""
    *head, prompt = command
    if len(prompt) > _PROMPT_PREVIEW:
        prompt = f"{prompt[:_PROMPT_PREVIEW]}…(+{len(prompt) - _PROMPT_PREVIEW} chars)"
    return " ".join([*head, repr(prompt)])


def _build_command(
    binary: str, job: Job, workdir: Path, prompt_prefix: str = ""
) -> list[str]:
    spec = job.spec
    flags = _run_flags(binary)
    command = [binary, "run", "--format", "json"]
    # V2 connects to a shared background service by default, whose project may
    # not be the job directory; --standalone forces a private server bound to
    # the process cwd, so every task runs in its own working directory.
    if "--standalone" in flags:
        command.append("--standalone")
    if "--dir" in flags:
        command += ["--dir", str(workdir)]
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
    command.append((prompt_prefix or "") + spec["prompt"])
    return command


class _EventAccumulator:
    """Collects the useful fields from OpenCode's JSON events."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.texts: list[str] = []
        self.session_id: str | None = None
        self.tools: list[str] = []
        # Usage is summed over every OpenCode step: a single job emits one
        # `step_finish` per assistant step, and each carries only that step's
        # usage. Keeping the last event (the old behaviour) reported the last
        # step alone instead of the job's real token/cost totals.
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
                self._add_usage(part.get("tokens"), part.get("cost"))
            elif event_type == "error":
                self.errors.append(self._format_error(event.get("error")))

    def _add_usage(self, tokens: dict | None, cost: float | None) -> None:
        """Accumulate one step's token counts and cost into the job totals."""
        if tokens:
            if self.tokens is None:
                self.tokens = {
                    "input": 0,
                    "output": 0,
                    "reasoning": 0,
                    "cache": {"read": 0, "write": 0},
                }
            for key in ("input", "output", "reasoning"):
                if tokens.get(key) is not None:
                    self.tokens[key] += tokens[key]
            cache = tokens.get("cache") or {}
            for key in ("read", "write"):
                if cache.get(key) is not None:
                    self.tokens["cache"][key] += cache[key]
            cache = self.tokens["cache"]
            self.tokens["total"] = (
                self.tokens["input"]
                + self.tokens["output"]
                + self.tokens["reasoning"]
                + cache["read"]
                + cache["write"]
            )
        if cost is not None:
            self.cost = (self.cost or 0.0) + cost

    @staticmethod
    def _format_error(error: object) -> str:
        if not isinstance(error, dict):
            return str(error) if error else "unknown error"
        data = error.get("data") or {}
        # OpenCode emits `{type, message}` (e.g. a transport/socket error) and,
        # for some errors, `{name, data: {message, ref}}`; read both so a real
        # cause is never collapsed into the bare "unknown error".
        message = (
            data.get("message")
            or error.get("message")
            or error.get("name")
            or error.get("type")
            or "unknown error"
        )
        ref = data.get("ref")
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


def _capabilities(config: Config) -> dict:
    capabilities = getattr(config, "capabilities", None)
    if capabilities is None:
        return {}
    try:
        return capabilities.get()
    except Exception:
        return {}


def _fail_before_start(job: Job, error: str) -> None:
    """Mark a job failed before OpenCode ran (environment error)."""
    job.status = "failed"
    job.error = error
    job.finished_at = time.time()
    job.save()
    logger.warning("job %s: %s", job.job_id, error)


def execute(job: Job, config: Config) -> None:
    """Run the job (called from a dedicated thread). Updates ``job``."""
    workdir = job.workdir
    workdir.mkdir(parents=True, exist_ok=True)
    events_path = job.dir / "events.jsonl"
    stderr_path = job.dir / "stderr.log"
    before_path = job.dir / "snapshot_before.json"
    # Truncate the event log now. Providers may append synthetic events (e.g.
    # merge conflicts) during prepare, before the OpenCode stream is appended.
    try:
        events_path.write_text("", encoding="utf-8")
    except OSError:
        pass

    # Provision the task's resources before doing anything else: a validation or
    # prepare failure must abort before OpenCode starts.
    capabilities = _capabilities(config)
    try:
        opencode_binary = resolve_binary(config.opencode_bin)
        opencode_flags = _run_flags(opencode_binary)
    except FileNotFoundError:
        opencode_binary = None
        opencode_flags = frozenset()
    timeout_s = float(job.spec.get("timeout_s") or config.default_timeout_s)
    ctx = Context(
        workspace=workdir,
        job_dir=job.dir,
        task_id=str(job.spec.get("task_id") or job.job_id),
        run_id=job.spec.get("run_id"),
        path_roots=list(capabilities.get("path_roots") or []),
        capabilities=capabilities,
        secrets=getattr(config, "secrets", None),
        opencode_bin=opencode_binary,
        opencode_flags=opencode_flags,
        model=job.spec.get("model"),
        agent=job.spec.get("agent"),
        timeout_s=timeout_s,
        default_timeout_s=config.default_timeout_s,
    )
    environment: Environment | None = None
    try:
        resources = parse_resources(job.spec.get("resources"))
    except EnvironmentError as exc:
        _fail_before_start(job, f"environment error: {exc}")
        return
    if resources:
        environment = Environment(resources, ctx)
        try:
            environment.prepare()
        except EnvironmentError as exc:
            _fail_before_start(job, f"environment error: {exc}")
            return
        job.merge = {
            prepared.resource.id: prepared.state["merge"]
            for prepared in environment.prepared
            if prepared.state.get("merge")
        }

    # Inject the OpenCode runtime config (agents, skills, AGENTS.md, permissions)
    # at the location directory before OpenCode starts. This always runs: the
    # built-in temp-directory permissions must apply to every job, even one with
    # no resources and no plan-level `opencode` block.
    plan_opencode = job.spec.get("opencode") or {}
    try:
        from .opencode_config import inject as inject_opencode

        report = inject_opencode(
            workdir,
            plan_opencode=plan_opencode,
            baseline_dir=getattr(config, "opencode_dir", None),
            provenance=environment.config_roots() if environment else [],
            fragments=environment.opencode_fragments if environment else [],
            ctx=ctx,
        )
        logger.info(
            "job %s: opencode config injected (agents=%d skills=%d agents_md=%d)",
            job.job_id,
            len(report.agents),
            len(report.skills),
            len(report.agents_md),
        )
    except EnvironmentError as exc:
        _fail_before_start(job, f"opencode config error: {exc}")
        return

    # Snapshot *after* provisioning, so a checkout/clean is not reported as a
    # change made by OpenCode.
    before = snapshot(workdir)
    before_path.write_text(json.dumps(before, indent=2), encoding="utf-8")

    accumulator = _EventAccumulator()

    job.status = "running"
    job.started_at = time.time()
    job.save()
    logger.info(
        "job %s: executing (timeout=%.0fs, workdir=%s)",
        job.job_id,
        timeout_s,
        workdir,
    )

    error: str | None = None
    return_code: int | None = None
    timed_out = False

    try:
        binary = resolve_binary(config.opencode_bin)
        prompt_prefix = (
            summarize_working_dirs(environment.prepared, workdir)
            if environment is not None
            else ""
        )
        command = _build_command(binary, job, workdir, prompt_prefix)
        logger.info(
            "job %s: opencode binary %s, flags %s",
            job.job_id,
            binary,
            sorted(_run_flags(binary)),
        )
        logger.info("job %s: command: %s", job.job_id, _describe_command(command))
        env = os.environ.copy()
        if environment is not None:
            env.update(environment.env)
        for key, value in (job.spec.get("env") or {}).items():
            env[str(key)] = str(value)
        if os.name != "nt":
            # OpenCode V2 resolves the working directory from $PWD, and
            # subprocess(cwd=...) does not update it: without this, a job would
            # run in the worker's launch directory instead of its own.
            env["PWD"] = str(workdir)

        with open(events_path, "a", encoding="utf-8") as events_file, open(
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
            logger.info("job %s: opencode started (pid=%s)", job.job_id, process.pid)

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
                logger.warning(
                    "job %s: timed out after %.0fs, terminating opencode (pid=%s)",
                    job.job_id,
                    timeout_s,
                    process.pid,
                )
                _terminate(process)
                return_code = process.poll()

            stdout_thread.join(timeout=10)
            stderr_thread.join(timeout=10)

    except FileNotFoundError as exc:
        error = str(exc)
        logger.error("job %s: %s", job.job_id, exc)
    except Exception as exc:  # pragma: no cover - safety net
        error = f"unexpected error: {exc!r}"
        logger.exception("job %s: unexpected error during execution", job.job_id)

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

    # Publish the task's resources (commit/push, collect artifacts). A finalize
    # failure is a real failure; teardown always runs.
    finalize_error: str | None = None
    if environment is not None:
        succeeded = (
            not job.shutdown_requested
            and not job.cancel_requested
            and not timed_out
            and error is None
            and return_code == 0
        )
        try:
            outcome_result = environment.finalize(
                JobOutcome(
                    status="succeeded" if succeeded else "failed",
                    succeeded=succeeded,
                    changed_files=sorted(set(added) | set(modified)),
                    workdir=workdir,
                )
            )
            job.commits = outcome_result.commits
            job.artifacts = outcome_result.artifacts
        except EnvironmentError as exc:
            finalize_error = str(exc)
        finally:
            environment.teardown()

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

    if finalize_error is not None:
        job.status = "failed"
        job.error = ((job.error + " | ") if job.error else "") + (
            f"finalize failed: {finalize_error}"
        )

    job.finished_at = time.time()
    job.save()
    logger.info(
        "job %s: finished status=%s exit_code=%s duration=%.3fs",
        job.job_id,
        job.status,
        job.exit_code,
        job.finished_at - (job.started_at or job.finished_at),
    )
    if job.status == "failed" and job.error:
        logger.warning("job %s: %s", job.job_id, job.error)

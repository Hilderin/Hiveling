"""FastAPI application for the worker.

Protocol (v1, no authentication):

- ``GET    /health``                  worker state (free/busy)
- ``POST   /jobs``                    create a job (status ``accepted``)
- ``PUT    /jobs/{id}/files``         upload an input zip (optional)
- ``POST   /jobs/{id}/start``         start execution
- ``GET    /jobs/{id}``               detailed status
- ``GET    /jobs/{id}/files?which=``  zip of modified files (or ``all``)
- ``GET    /jobs/{id}/logs``          stdout (events) + stderr
- ``DELETE /jobs/{id}``               cancel a job
- ``GET    /jobs``                    list jobs
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .capabilities import Capabilities, detect_resources
from .config import Config
from .gc import prune as prune_jobs
from .executor import execute, _terminate
from .files import build_zip, extract_zip, list_files, list_globs
from .secrets import SecretStore
from .state import ACTIVE_STATUSES, TERMINAL_STATUSES, Job, Registry

logger = logging.getLogger("hiveling.worker")


class JobSpec(BaseModel):
    job_id: str | None = None
    prompt: str
    model: str | None = None
    agent: str | None = None
    timeout_s: float | None = None
    variant: str | None = None
    title: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    files: list[str] = Field(default_factory=list)
    task_id: str | None = None
    run_id: str | None = None
    resources: list[dict] = Field(default_factory=list)
    artifacts: dict = Field(default_factory=dict)
    opencode: dict = Field(default_factory=dict)


def _janitor(registry: Registry, config: Config, stop: threading.Event) -> None:
    """Expire jobs created but never started, and emit a periodic heartbeat."""
    interval = max(1.0, min(5.0, config.accept_timeout_s / 2))
    if config.heartbeat_s > 0:
        interval = min(interval, max(0.5, config.heartbeat_s))
    last_heartbeat = time.time()
    last_gc = time.time()
    gc_enabled = config.retention_jobs > 0 or config.retention_days > 0
    while not stop.wait(interval):
        now = time.time()
        if gc_enabled and now - last_gc >= 3600:
            last_gc = now
            prune_jobs(
                config.workspace,
                keep=config.retention_jobs,
                older_than_days=config.retention_days,
            )
        for job in registry.all():
            if job.status == "accepted" and now - job.created_at > config.accept_timeout_s:
                job.status = "failed"
                job.error = "job expired: never started by the server"
                job.finished_at = now
                job.save()
                logger.warning(
                    "job %s expired: never started within %.0fs",
                    job.job_id,
                    config.accept_timeout_s,
                )
        if config.heartbeat_s > 0 and now - last_heartbeat >= config.heartbeat_s:
            last_heartbeat = now
            active = registry.active()
            logger.info(
                "heartbeat: jobs=%d busy=%s",
                len(registry.all()),
                active.job_id if active else "-",
            )


def create_app(config: Config) -> FastAPI:
    registry = Registry()
    registry.load_from_disk(config.workspace)
    capabilities = Capabilities(config.capabilities_file)
    config.capabilities = capabilities
    config.secrets = SecretStore(config.secrets_file)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        stop = threading.Event()
        thread = threading.Thread(
            target=_janitor,
            args=(registry, config, stop),
            daemon=True,
            name="janitor",
        )
        thread.start()
        logger.info(
            "application startup: %d job(s) restored from %s",
            len(registry.all()),
            config.workspace,
        )
        logger.info(
            "capabilities: file=%s %s",
            config.capabilities_file,
            capabilities.get(),
        )
        if config.retention_jobs > 0 or config.retention_days > 0:
            prune_jobs(
                config.workspace,
                keep=config.retention_jobs,
                older_than_days=config.retention_days,
            )
        try:
            yield
        finally:
            stop.set()
            active = [job for job in registry.all() if job.status in ACTIVE_STATUSES]
            if active:
                logger.warning(
                    "shutting down: aborting %d active job(s): %s",
                    len(active),
                    ", ".join(job.job_id for job in active),
                )
            # Terminate running jobs so no orphan opencode process survives.
            for job in registry.all():
                if job.status in ACTIVE_STATUSES:
                    job.shutdown_requested = True
                    if job.process is not None:
                        logger.warning(
                            "job %s: terminating opencode (pid=%s)",
                            job.job_id,
                            job.process.pid,
                        )
                        _terminate(job.process)
                    job.status = "failed"
                    job.error = "worker shutting down"
                    job.finished_at = time.time()
                    job.save()
            logger.info("shutdown complete")

    app = FastAPI(title="Hiveling worker", version="1.0.0", lifespan=lifespan)

    def get_job_or_404(job_id: str) -> Job:
        job = registry.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"unknown job: {job_id}")
        return job

    @app.get("/")
    def root() -> dict:
        return {"name": "Hiveling worker", "version": "1.0.0", "workspace": str(config.workspace)}

    @app.get("/health")
    def health() -> dict:
        active = registry.active()
        try:
            binary = shutil.which(config.opencode_bin or "opencode")
        except Exception:
            binary = None
        logger.debug(
            "health check: busy=%s jobs=%d opencode_bin=%s",
            active.job_id if active else "-",
            len(registry.all()),
            binary,
        )
        return {
            "status": "ok",
            "busy": active is not None,
            "active_job": active.job_id if active else None,
            "opencode_bin": binary,
            "jobs": len(registry.all()),
            "capabilities": capabilities.get(),
            "resources": detect_resources(),
        }

    @app.get("/jobs")
    def list_jobs() -> list[dict]:
        return [job.to_status() for job in registry.all()]

    @app.post("/jobs", status_code=201)
    def create_job(spec: JobSpec) -> dict:
        if registry.is_busy():
            raise HTTPException(status_code=409, detail="worker busy")
        job_id = spec.job_id or uuid.uuid4().hex
        if registry.get(job_id) is not None:
            raise HTTPException(status_code=409, detail=f"job already exists: {job_id}")

        job_dir = config.workspace / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        job = Job(job_id=job_id, dir=job_dir, spec=spec.model_dump())
        job.workdir.mkdir(parents=True, exist_ok=True)
        (job_dir / "request.json").write_text(
            json.dumps(spec.model_dump(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        registry.add(job)
        job.save()
        logger.info(
            "job %s accepted (model=%s agent=%s files=%d)",
            job_id,
            spec.model,
            spec.agent,
            len(spec.files),
        )
        return job.to_status()

    @app.put("/jobs/{job_id}/files")
    async def upload_files(job_id: str, request: Request) -> dict:
        job = get_job_or_404(job_id)
        if job.status != "accepted":
            raise HTTPException(status_code=409, detail=f"job already started ({job.status})")
        data = await request.body()
        if not data:
            raise HTTPException(status_code=400, detail="empty body")
        try:
            extracted = extract_zip(data, job.workdir)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"invalid zip: {exc}") from exc
        (job.dir / "inputs.json").write_text(
            json.dumps(extracted, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        logger.info(
            "job %s: input files uploaded (%d bytes, %d file(s))",
            job_id,
            len(data),
            len(extracted),
        )
        return {"job_id": job_id, "extracted": extracted}

    @app.post("/jobs/{job_id}/start")
    def start_job(job_id: str) -> dict:
        job = get_job_or_404(job_id)
        if job.status != "accepted":
            raise HTTPException(status_code=409, detail=f"job cannot start ({job.status})")
        thread = threading.Thread(
            target=execute, args=(job, config), daemon=True, name=f"job-{job_id}"
        )
        job.thread = thread
        thread.start()
        logger.info("job %s: start requested", job_id)
        return job.to_status()

    @app.get("/jobs/{job_id}")
    def get_job(job_id: str) -> dict:
        return get_job_or_404(job_id).to_status()

    @app.get("/jobs/{job_id}/files")
    def get_files(job_id: str, which: str = "modified") -> Response:
        job = get_job_or_404(job_id)
        if which == "all":
            files = list_files(job.workdir)
        elif which == "modified":
            files = sorted(set(job.added) | set(job.modified))
            # Extra artifacts the plan asked for, beyond the workdir diff.
            patterns = (job.spec.get("artifacts") or {}).get("paths") or []
            files = sorted(set(files) | set(list_globs(job.workdir, patterns)))
        else:
            raise HTTPException(status_code=400, detail="which must be 'modified' or 'all'")
        data = build_zip(job.workdir, files)
        return Response(
            content=data,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{job_id}.zip"'},
        )

    @app.get("/jobs/{job_id}/logs")
    def get_logs(job_id: str) -> dict:
        job = get_job_or_404(job_id)
        events = ""
        stderr = ""
        events_path = job.dir / "events.jsonl"
        stderr_path = job.dir / "stderr.log"
        if events_path.exists():
            events = events_path.read_text(encoding="utf-8", errors="replace")
        if stderr_path.exists():
            stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
        return {"job_id": job_id, "stdout": events, "stderr": stderr}

    @app.delete("/jobs/{job_id}")
    def cancel_job(job_id: str) -> dict:
        job = get_job_or_404(job_id)
        if job.status in TERMINAL_STATUSES:
            return job.to_status()
        job.cancel_requested = True
        if job.process is not None:
            logger.warning(
                "job %s: cancel requested, terminating opencode (pid=%s)",
                job_id,
                job.process.pid,
            )
            _terminate(job.process)
        if job.status == "accepted":
            job.status = "canceled"
            job.error = "job canceled before start"
            job.finished_at = time.time()
            job.save()
        return job.to_status()

    return app

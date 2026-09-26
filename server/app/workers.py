"""External worker configuration (``workers.yaml``) with hot reload.

Workers are no longer declared in plans. They live in a single file, by default
``<data-dir>/workers.yaml``::

    workers:
      - name: windows10
        host: 10.0.0.174
        port: 8787
      - name: local
        url: http://127.0.0.1:8787

:class:`WorkerRegistry` reloads the file whenever it changes on disk (mtime +
size), so adding a worker while the server runs is picked up without a restart.
"""

from __future__ import annotations

import threading
from pathlib import Path

import yaml

from .plan import PlanError, WorkerEndpoint, parse_worker
from .worker_client import WorkerClient, WorkerError


class WorkerConfigError(Exception):
    """Invalid or unreadable workers configuration."""


def parse_workers_file(path: Path) -> list[WorkerEndpoint]:
    """Read ``workers.yaml`` and return the declared endpoints."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise WorkerConfigError(f"invalid YAML in {path}: {exc}") from exc
    except OSError as exc:
        raise WorkerConfigError(f"cannot read {path}: {exc}") from exc

    if isinstance(raw, list):
        entries = raw
    elif isinstance(raw, dict):
        entries = raw.get("workers")
    else:
        raise WorkerConfigError("the workers file must be a mapping with a 'workers' list")

    if not isinstance(entries, list) or not entries:
        raise WorkerConfigError("'workers' must be a non-empty list")

    try:
        return [parse_worker(entry, i) for i, entry in enumerate(entries)]
    except PlanError as exc:
        raise WorkerConfigError(str(exc)) from exc


class WorkerRegistry:
    """Keeps the worker list in memory and reloads it when the file changes."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._workers: list[WorkerEndpoint] = []
        self._error: str | None = None
        self._stamp: tuple[int, int] | None = None
        self._load(force=True)

    def _stamp_now(self) -> tuple[int, int] | None:
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def _load(self, force: bool = False) -> None:
        stamp = self._stamp_now()
        if not force and stamp == self._stamp:
            return
        self._stamp = stamp

        if stamp is None:
            self._workers = []
            self._error = f"workers file not found: {self.path}"
            return

        try:
            workers = parse_workers_file(self.path)
        except WorkerConfigError as exc:
            # Keep the last good list so a typo during an edit does not wipe it.
            self._error = str(exc)
            return

        self._workers = workers
        self._error = None

    def get(self) -> list[WorkerEndpoint]:
        with self._lock:
            self._load()
            return list(self._workers)

    def error(self) -> str | None:
        with self._lock:
            self._load()
            return self._error


def probe_workers(registry: WorkerRegistry, timeout: float = 2.0) -> dict:
    """Return the reachable/busy state of every configured worker."""
    results: list[dict] = []
    for worker in registry.get():
        entry = {
            "name": worker.name,
            "url": worker.url,
            "reachable": False,
            "busy": None,
            "active_job": None,
            "opencode_bin": None,
        }
        client = WorkerClient(worker, timeout=timeout)
        try:
            health = client.health()
            entry.update(
                reachable=True,
                busy=health.get("busy"),
                active_job=health.get("active_job"),
                opencode_bin=health.get("opencode_bin"),
            )
        except WorkerError:
            pass
        finally:
            client.close()
        results.append(entry)
    return {"workers": results, "error": registry.error()}

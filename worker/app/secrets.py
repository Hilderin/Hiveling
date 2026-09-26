"""Worker-side secret store.

Secrets are **never** carried in a plan: a plan references a name, and the
worker resolves it here. Values come from an optional ``secrets.yaml`` file
(a flat ``name: value`` mapping, hot-reloaded) and fall back to the worker's own
environment variables.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

import yaml

logger = logging.getLogger("hiveling.worker.secrets")


class SecretStore:
    def __init__(self, path: Path | None = None, environ: dict | None = None):
        self.path = Path(path).expanduser().resolve() if path else None
        self.environ = environ if environ is not None else os.environ
        self._lock = threading.Lock()
        self._stamp: tuple[int, int] | None = None
        self._values: dict[str, str] = {}
        self._error: str | None = None
        self._reload(force=True)

    def _stamp_now(self) -> tuple[int, int] | None:
        if self.path is None:
            return None
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def _reload(self, force: bool = False) -> None:
        stamp = self._stamp_now()
        if not force and stamp == self._stamp:
            return
        self._stamp = stamp
        if self.path is None or stamp is None:
            self._values = {}
            self._error = None
            return
        try:
            raw = yaml.safe_load(self.path.read_text(encoding="utf-8-sig")) or {}
        except (OSError, yaml.YAMLError) as exc:
            self._error = f"invalid secrets file {self.path}: {exc}"
            logger.warning("%s", self._error)
            return
        if not isinstance(raw, dict):
            self._error = f"secrets file must be a mapping: {self.path}"
            logger.warning("%s", self._error)
            return
        self._values = {str(key): str(value) for key, value in raw.items()}
        self._error = None

    def get(self, name: str) -> str | None:
        """Resolve a secret by name (file first, then environment)."""
        with self._lock:
            self._reload()
            values = dict(self._values)
        if name in values:
            return values[name]
        value = self.environ.get(name)
        return str(value) if value is not None else None

    def error(self) -> str | None:
        with self._lock:
            self._reload()
            return self._error

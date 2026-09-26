"""Worker capabilities: what this machine can offer to a plan.

Capabilities are auto-detected (os, arch, tools, implemented providers) and
extended from an optional ``capabilities.yaml`` file (``tags``, ``labels``,
``tools``, ``providers`` and ``path_roots``). They are advertised through
``GET /health`` so the server can match a task's ``requirements`` against the
workers that can actually run it.

The file is hot-reloaded when it changes, so a test or an operator can add a tag
without restarting the worker.
"""

from __future__ import annotations

import logging
import platform
import shutil
import threading
from pathlib import Path

import yaml

logger = logging.getLogger("hiveling.worker.capabilities")

# Providers implemented by this worker build. Extended as providers land.
KNOWN_PROVIDERS: tuple[str, ...] = ("ephemeral", "env", "secret", "git", "path")

# Tools probed on PATH when advertising capabilities.
DETECTED_TOOLS: tuple[str, ...] = (
    "git",
    "node",
    "npm",
    "python",
    "python3",
    "dotnet",
    "docker",
    "uv",
)


def detect_os() -> str:
    name = platform.system().lower()
    if name.startswith("win"):
        return "windows"
    if name == "darwin":
        return "macos"
    return name or "unknown"


def detect_arch() -> str:
    machine = platform.machine().lower()
    return {
        "amd64": "x64",
        "x86_64": "x64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }.get(machine, machine or "unknown")


def detect_tools() -> list[str]:
    return sorted({tool for tool in DETECTED_TOOLS if shutil.which(tool)})


class Capabilities:
    """Load ``capabilities.yaml`` (hot-reloaded) and merge it with auto-detection."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path).expanduser().resolve() if path else None
        self._lock = threading.Lock()
        self._stamp: tuple[int, int] | None = None
        self._configured: dict = {}
        self._error: str | None = None
        self._detected = {
            "os": detect_os(),
            "arch": detect_arch(),
            "tools": detect_tools(),
            "providers": sorted(KNOWN_PROVIDERS),
        }
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
            # A missing file is fine: auto-detection alone is a valid answer.
            self._configured = {}
            self._error = None
            return
        try:
            # utf-8-sig tolerates a BOM from Windows editors/PowerShell.
            raw = yaml.safe_load(self.path.read_text(encoding="utf-8-sig")) or {}
        except (OSError, yaml.YAMLError) as exc:
            self._error = f"invalid capabilities file {self.path}: {exc}"
            logger.warning("%s", self._error)
            return
        if not isinstance(raw, dict):
            self._error = f"capabilities file must be a mapping: {self.path}"
            logger.warning("%s", self._error)
            return
        self._configured = raw
        self._error = None

    def get(self) -> dict:
        """The advertised capabilities (auto-detected merged with the file)."""
        with self._lock:
            self._reload()
            configured = dict(self._configured)
            error = self._error
        detected = self._detected

        configured_providers = configured.get("providers")
        if configured_providers is None:
            providers = detected["providers"]
        else:
            providers = [str(p) for p in configured_providers]

        configured_tools = {str(t) for t in (configured.get("tools") or [])}
        result: dict = {
            "os": detected["os"],
            "arch": detected["arch"],
            "tools": sorted(set(detected["tools"]) | configured_tools),
            "providers": sorted(providers),
            "tags": [str(t) for t in (configured.get("tags") or [])],
            "labels": {
                str(key): str(value)
                for key, value in (configured.get("labels") or {}).items()
            },
        }
        if configured.get("path_roots") is not None:
            result["path_roots"] = [str(p) for p in configured["path_roots"]]
        if error:
            result["error"] = error
        return result

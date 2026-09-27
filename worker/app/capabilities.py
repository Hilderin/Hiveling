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
import os
import platform
import shutil
import subprocess
import threading
from pathlib import Path

import yaml

logger = logging.getLogger("hiveling.worker.capabilities")

# Providers implemented by this worker build.
IMPLEMENTED_PROVIDERS: tuple[str, ...] = (
    "ephemeral",
    "env",
    "secret",
    "git",
    "path",
    "command",
)

# Advertised when capabilities.yaml does not list `providers`. `command` runs
# arbitrary shell commands, so it is opt-in: list it explicitly to enable it.
DEFAULT_ENABLED_PROVIDERS: tuple[str, ...] = (
    "ephemeral",
    "env",
    "secret",
    "git",
    "path",
)

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


def _proc_cpuinfo() -> list[dict[str, str]]:
    """Parse ``/proc/cpuinfo`` into one dict per logical CPU (Linux only)."""
    try:
        text = Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    blocks: list[dict[str, str]] = []
    for block in text.split("\n\n"):
        info: dict[str, str] = {}
        for line in block.splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                info[key.strip()] = value.strip()
        if info:
            blocks.append(info)
    return blocks


def _physical_cores(blocks: list[dict[str, str]]) -> int | None:
    pairs = {
        (block.get("physical id"), block.get("core id"))
        for block in blocks
        if block.get("core id") is not None
    }
    pairs.discard((None, None))
    return len(pairs) or None


def _cpu_speed_mhz(blocks: list[dict[str, str]]) -> float | None:
    """A representative CPU clock in MHz, best effort, cross-platform."""
    speeds: list[float] = []
    for block in blocks:
        raw = block.get("cpu MHz")
        if not raw:
            continue
        try:
            speeds.append(float(raw))
        except ValueError:
            pass
    if speeds:
        # The per-core MHz reflect the current clock; report the fastest core.
        return round(max(speeds), 1)
    if os.name == "nt":  # pragma: no cover - Windows
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
            ) as key:
                value, _ = winreg.QueryValueEx(key, "~MHz")
                return float(value)
        except Exception:
            return None
    try:
        out = subprocess.run(
            ["sysctl", "-n", "hw.cpufrequency"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip().isdigit():
            return round(int(out.stdout.strip()) / 1_000_000, 1)
    except Exception:
        pass
    return None


def _memory_bytes() -> tuple[int | None, int | None]:
    """Total and currently available physical RAM in bytes, best effort."""
    if os.name == "nt":  # pragma: no cover - Windows
        try:
            import ctypes

            class _MemoryStatusEx(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = _MemoryStatusEx()
            status.dwLength = ctypes.sizeof(_MemoryStatusEx)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.ullTotalPhys), int(status.ullAvailPhys)
        except Exception:
            pass
        return None, None

    total: int | None = None
    available: int | None = None
    meminfo = Path("/proc/meminfo")
    if meminfo.is_file():
        try:
            values: dict[str, str] = {}
            for line in meminfo.read_text(encoding="utf-8", errors="replace").splitlines():
                key, _, raw = line.partition(":")
                values[key.strip()] = raw.strip()
            for name, target in (("MemTotal", "total"), ("MemAvailable", "available")):
                token = values.get(name, "").split()
                if token and token[0].isdigit():
                    if target == "total":
                        total = int(token[0]) * 1024
                    else:
                        available = int(token[0]) * 1024
        except OSError:
            pass
    if total is None:
        try:
            page = os.sysconf("SC_PAGE_SIZE")
            total = page * os.sysconf("SC_PHYS_PAGES")
            try:
                available = page * os.sysconf("SC_AVPHYS_PAGES")
            except (ValueError, OSError):
                pass
        except (ValueError, OSError, AttributeError):
            pass
    return total, available


def detect_resources() -> dict:
    """Live machine resources advertised through ``GET /health``.

    Unlike :class:`Capabilities` this is intentionally *not* cached: available
    RAM changes while the worker runs, so the server sees the current value on
    every probe. CPU count/speed and total RAM are stable; a failure to detect
    any of them yields ``None`` instead of breaking the health check.
    """
    blocks = _proc_cpuinfo()
    total, available = _memory_bytes()
    try:
        load_average: list[float] | None = [round(x, 2) for x in os.getloadavg()]
    except (OSError, AttributeError):
        load_average = None
    model = next((b.get("model name") for b in blocks if b.get("model name")), None)
    return {
        "cpu_count": os.cpu_count(),
        "cpu_count_physical": _physical_cores(blocks),
        "cpu_speed_mhz": _cpu_speed_mhz(blocks),
        "cpu_model": model or (platform.processor() or None),
        "ram_total_bytes": total,
        "ram_available_bytes": available,
        "load_average": load_average,
    }


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
            "providers": sorted(DEFAULT_ENABLED_PROVIDERS),
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

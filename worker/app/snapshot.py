"""Detect files changed inside the working directory.

A snapshot (size + mtime) is taken before and after OpenCode runs. This is
intentionally simple and independent of git.
"""

from __future__ import annotations

from pathlib import Path

# Technical directories ignored by change detection.
EXCLUDED_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "build",
    ".idea",
    ".vscode",
    ".cache",
}


def _is_excluded(rel: Path) -> bool:
    return any(part in EXCLUDED_DIRS for part in rel.parts)


def snapshot(root: Path) -> dict[str, list[int]]:
    """Return ``{relative_path: [size, mtime_ns]}``."""
    result: dict[str, list[int]] = {}
    if not root.exists():
        return result
    for path in root.rglob("*"):
        if path.is_dir():
            continue
        rel = path.relative_to(root)
        if _is_excluded(rel):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        result[rel.as_posix()] = [stat.st_size, stat.st_mtime_ns]
    return result


def diff(
    before: dict[str, list[int]], after: dict[str, list[int]]
) -> tuple[list[str], list[str], list[str]]:
    """Return ``(added, modified, deleted)`` sorted."""
    added: list[str] = []
    modified: list[str] = []
    deleted: list[str] = []
    for name, meta in after.items():
        if name not in before:
            added.append(name)
        elif before[name] != meta:
            modified.append(name)
    for name in before:
        if name not in after:
            deleted.append(name)
    return sorted(added), sorted(modified), sorted(deleted)

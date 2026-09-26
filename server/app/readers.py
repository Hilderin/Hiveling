"""Small file helpers shared by the JSON API and the MCP server."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path


def read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def read_text(path: Path, tail_lines: int | None = None) -> tuple[str, int]:
    """Return ``(text, total_lines)``; optionally keep only the last lines."""
    if not path.is_file():
        return "", 0
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "", 0
    lines = text.splitlines()
    total = len(lines)
    if tail_lines is not None and total > tail_lines:
        text = "\n".join(lines[-tail_lines:])
    return text, total


def zip_dir(directory: Path) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(directory).as_posix())
    return buffer.getvalue()

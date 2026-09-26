"""Zip helpers used to exchange files over HTTP."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path


def _safe_target(dest: Path, name: str) -> Path:
    """Prevent zip entries from escaping the destination directory."""
    base = dest.resolve()
    target = (dest / name).resolve()
    if target != base and base not in target.parents:
        raise ValueError(f"invalid path in archive: {name}")
    return target


def extract_zip(data: bytes, dest: Path) -> list[str]:
    """Extract a zip archive into ``dest`` and return the written files."""
    dest.mkdir(parents=True, exist_ok=True)
    extracted: list[str] = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            target = _safe_target(dest, info.filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, open(target, "wb") as out:
                out.write(src.read())
            extracted.append(info.filename)
    return extracted


def build_zip(root: Path, files: list[str]) -> bytes:
    """Build a zip archive from paths relative to ``root``."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for rel in files:
            path = root / rel
            if not path.is_file():
                continue
            archive.write(path, rel)
    return buffer.getvalue()


def list_globs(root: Path, patterns: list[str]) -> list[str]:
    """Return the files under ``root`` matching ``patterns`` (relative paths)."""
    import glob

    found: list[str] = []
    base = root.resolve()
    for pattern in patterns:
        for match in glob.glob(str(root / pattern), recursive=True):
            path = Path(match)
            if not path.is_file():
                continue
            try:
                found.append(path.resolve().relative_to(base).as_posix())
            except ValueError:
                continue
    return sorted(set(found))


def list_files(root: Path) -> list[str]:
    """List every file under ``root`` (relative POSIX paths)."""
    from .snapshot import _is_excluded

    files: list[str] = []
    if not root.exists():
        return files
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if _is_excluded(rel):
            continue
        files.append(rel.as_posix())
    return sorted(files)

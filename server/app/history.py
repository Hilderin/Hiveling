"""Write the execution history to disk.

Layout: ``<history_dir>/<task_id>/<run_id>/`` containing, among others,
``request.json``, ``status.json``, ``result.txt``, ``events.jsonl``,
``stderr.log``, ``files.zip`` and ``files/`` (extracted).
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_name(name: str) -> str:
    cleaned = _SAFE.sub("_", name).strip("._")
    return cleaned or "task"


def _extract_into(zip_bytes: bytes, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    base = dest.resolve()
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            target = (dest / info.filename).resolve()
            if target != base and base not in target.parents:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, open(target, "wb") as out:
                out.write(src.read())


class History:
    def __init__(self, root: Path):
        self.root = root

    def run_dir(self, task_id: str, run_id: str) -> Path:
        return self.root / _safe_name(task_id) / _safe_name(run_id)

    def save(
        self,
        task_id: str,
        run_id: str,
        *,
        request: dict,
        status: dict,
        logs: dict | None,
        files_zip: bytes | None,
        worker: dict,
    ) -> Path:
        directory = self.run_dir(task_id, run_id)
        directory.mkdir(parents=True, exist_ok=True)

        (directory / "request.json").write_text(
            json.dumps(request, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        (directory / "status.json").write_text(
            json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        (directory / "result.txt").write_text(
            status.get("result_text") or "", encoding="utf-8"
        )
        (directory / "worker.json").write_text(
            json.dumps(worker, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        if logs:
            (directory / "events.jsonl").write_text(logs.get("stdout") or "", encoding="utf-8")
            (directory / "stderr.log").write_text(logs.get("stderr") or "", encoding="utf-8")

        if files_zip is not None:
            (directory / "files.zip").write_bytes(files_zip)
            _extract_into(files_zip, directory / "files")

        return directory

"""Plan file management shared by the JSON API and the MCP server.

These helpers keep the path/validation rules in one place: the plan directory
is a sandbox and every plan must validate with :func:`load_plan` before it is
stored.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

from .plan import PlanError, load_plan


def plans_root(plans_dir: Path) -> Path:
    return plans_dir.resolve()


def safe_file_name(name: str) -> str:
    cleaned = "".join(c if (c.isalnum() or c in "._-") else "-" for c in name).strip(".-")
    return cleaned or "plan"


def list_plan_files(plans_dir: Path) -> list[Path]:
    base = plans_root(plans_dir)
    if not base.exists():
        return []
    return sorted(p for p in base.rglob("*") if p.suffix in (".yaml", ".yml") and p.is_file())


def plan_name(plans_dir: Path, path: Path) -> str:
    """Return the plan path relative to the plans directory (POSIX)."""
    try:
        return path.resolve().relative_to(plans_root(plans_dir)).as_posix()
    except ValueError:
        return path.name


def resolve_known_plan(plans_dir: Path, name: str) -> Path:
    """Resolve a plan name inside the plans directory or raise :class:`PlanError`."""
    base = plans_root(plans_dir)
    candidate = (base / name).resolve()
    if candidate != base and base not in candidate.parents:
        raise PlanError("invalid plan path")
    if candidate.suffix not in (".yaml", ".yml"):
        raise PlanError("plan must be a .yaml/.yml file")
    return candidate


def is_editable_plan(plans_dir: Path, data_dir: Path, path: Path) -> bool:
    resolved = path.resolve()
    for base in (plans_root(plans_dir), data_dir.resolve()):
        if resolved == base or base in resolved.parents:
            return True
    return False


def plan_summary(path: Path) -> dict:
    try:
        plan = load_plan(path)
        return {
            "tasks": [
                {
                    "id": t.id,
                    "depends_on": t.depends_on,
                    "inputs_from": t.inputs_from,
                    "model": t.model,
                }
                for t in plan.tasks
            ],
            "error": None,
        }
    except PlanError as exc:
        return {"tasks": [], "error": str(exc)}


def persist_pushed_plan(pushed_plans_dir: Path, name: str | None, content: str) -> Path:
    """Validate a pushed plan body and store it under the plans directory."""
    directory = pushed_plans_dir
    directory.mkdir(parents=True, exist_ok=True)
    if name:
        base = safe_file_name(name)
        if not base.endswith((".yaml", ".yml")):
            base += ".yaml"
    else:
        base = f"pushed-{time.strftime('%Y-%m-%dT%H-%M-%S')}-{uuid.uuid4().hex[:4]}.yaml"
    target = directory / base
    target.write_text(content, encoding="utf-8")
    try:
        load_plan(target)
    except PlanError:
        target.unlink(missing_ok=True)
        raise
    return target


def write_plan(path: Path, content: str) -> None:
    """Validate ``content`` then atomically replace ``path``."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    try:
        load_plan(tmp)
    except PlanError:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(path)

"""Reading and validation of the ``plan.yaml`` file."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


class PlanError(Exception):
    """Plan reading or validation error."""


@dataclass
class WorkerEndpoint:
    name: str
    url: str


@dataclass
class Task:
    id: str
    prompt: str
    files: list[str] = field(default_factory=list)
    model: str | None = None
    agent: str | None = None
    auto: bool = True
    timeout_s: float | None = None
    variant: str | None = None
    title: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)
    inputs_from: list[str] = field(default_factory=list)
    download: str = "modified"  # modified | all | none


@dataclass
class Plan:
    path: Path
    base_dir: Path
    version: int
    defaults: dict
    tasks: list[Task]

    def task_by_id(self, task_id: str) -> Task:
        for task in self.tasks:
            if task.id == task_id:
                return task
        raise PlanError(f"unknown task: {task_id}")


def parse_worker(entry, index: int) -> WorkerEndpoint:
    """Parse one worker entry (a URL string or a host/port mapping)."""
    if isinstance(entry, str):
        return WorkerEndpoint(name=f"worker-{index}", url=entry)
    if not isinstance(entry, dict):
        raise PlanError(f"invalid worker at index {index}: {entry!r}")
    name = entry.get("name") or f"worker-{index}"
    if "url" in entry:
        return WorkerEndpoint(name=name, url=str(entry["url"]))
    if "host" in entry and "port" in entry:
        return WorkerEndpoint(name=name, url=f"http://{entry['host']}:{entry['port']}")
    raise PlanError(f"worker '{name}': provide 'url' or 'host' + 'port'")


def _load_prompt(raw: dict, base_dir: Path) -> str:
    prompt = raw.get("prompt") or ""
    prompt_file = raw.get("prompt_file")
    if prompt_file:
        path = (base_dir / prompt_file).resolve()
        if not path.is_file():
            raise PlanError(f"prompt_file not found: {path}")
        prompt = "\n\n".join(filter(None, [prompt.strip(), path.read_text(encoding="utf-8").strip()]))
    if not prompt.strip():
        raise PlanError(f"task '{raw.get('id')}': 'prompt' or 'prompt_file' is required")
    return prompt


def _parse_task(raw: dict, defaults: dict, base_dir: Path) -> Task:
    if not isinstance(raw, dict):
        raise PlanError(f"invalid task: {raw!r}")
    task_id = raw.get("id")
    if not task_id:
        raise PlanError("every task must have an 'id'")

    env = dict(defaults.get("env") or {})
    env.update(raw.get("env") or {})

    files = list(defaults.get("files") or []) + list(raw.get("files") or [])

    def pick(key: str, fallback: object = None):
        return raw[key] if key in raw else defaults.get(key, fallback)

    timeout = pick("timeout_s")
    return Task(
        id=str(task_id),
        prompt=_load_prompt(raw, base_dir),
        files=[str(f) for f in files],
        model=pick("model"),
        agent=pick("agent"),
        auto=bool(pick("auto", True)),
        timeout_s=float(timeout) if timeout is not None else None,
        variant=pick("variant"),
        title=pick("title") or str(task_id),
        env={str(k): str(v) for k, v in env.items()},
        depends_on=[str(d) for d in (raw.get("depends_on") or [])],
        inputs_from=[str(d) for d in (raw.get("inputs_from") or [])],
        download=str(pick("download", "modified")),
    )


def _validate_and_order(tasks: list[Task]) -> list[Task]:
    by_id = {task.id: task for task in tasks}
    if len(by_id) != len(tasks):
        seen: set[str] = set()
        for task in tasks:
            if task.id in seen:
                raise PlanError(f"duplicate task id: {task.id}")
            seen.add(task.id)

    for task in tasks:
        for dep in task.depends_on:
            if dep not in by_id:
                raise PlanError(f"task '{task.id}' depends on an unknown task: {dep}")
        for source in task.inputs_from:
            if source not in by_id:
                raise PlanError(f"task '{task.id}' uses files from an unknown task: {source}")

    # Stable topological sort (file order is preserved on ties).
    ordered: list[Task] = []
    visiting: set[str] = set()
    done: set[str] = set()

    def visit(task: Task) -> None:
        if task.id in done:
            return
        if task.id in visiting:
            raise PlanError(f"dependency cycle detected around '{task.id}'")
        visiting.add(task.id)
        for dep in task.depends_on:
            visit(by_id[dep])
        for source in task.inputs_from:
            visit(by_id[source])
        visiting.discard(task.id)
        done.add(task.id)
        ordered.append(task)

    for task in tasks:
        visit(task)
    return ordered


def load_plan(path: str | Path, *, base_dir: str | Path | None = None) -> Plan:
    """Load and validate a plan.

    ``base_dir`` overrides the directory used to resolve ``prompt_file`` and
    ``files``. It is needed when the plan is read from a run's snapshot (which
    lives under ``.data/runs/<id>/``) while its relative paths still refer to
    the original plan directory.
    """
    plan_path = Path(path).expanduser().resolve()
    if not plan_path.is_file():
        raise PlanError(f"plan not found: {plan_path}")
    try:
        raw = yaml.safe_load(plan_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise PlanError(f"invalid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise PlanError("the plan must be a YAML mapping")

    if "workers" in raw:
        raise PlanError(
            "'workers' is no longer configured in a plan; "
            "declare workers in the workers.yaml file instead"
        )

    tasks_raw = raw.get("tasks")
    if not tasks_raw:
        raise PlanError("'tasks' is required")
    defaults = raw.get("defaults") or {}

    resolve_base = (
        Path(base_dir).expanduser().resolve() if base_dir else plan_path.parent
    )
    tasks = [_parse_task(entry, defaults, resolve_base) for entry in tasks_raw]
    ordered = _validate_and_order(tasks)

    return Plan(
        path=plan_path,
        base_dir=resolve_base,
        version=int(raw.get("version", 1)),
        defaults=defaults,
        tasks=ordered,
    )

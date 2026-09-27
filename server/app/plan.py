"""Reading and validation of the ``plan.yaml`` file."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .gate import DEFAULT_MAX_ATTEMPTS, GATE_AGENT_ID, GATE_AGENT_MD


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
    timeout_s: float | None = None
    variant: str | None = None
    title: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)
    inputs_from: list[str] = field(default_factory=list)
    download: str = "modified"  # modified | all | none
    requirements: dict = field(default_factory=dict)
    resources: list[dict] = field(default_factory=list)
    artifacts: dict = field(default_factory=dict)
    opencode: dict = field(default_factory=dict)
    # A gate is a read-only node that judges the tasks it lists; it is compiled
    # from the top-level ``gates`` key into a Task so scheduling and the
    # topological sort are reused unchanged.
    kind: str = "task"  # task | gate
    gate_targets: list[str] = field(default_factory=list)
    gate_max_attempts: int = DEFAULT_MAX_ATTEMPTS


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


def _merge_requirements(defaults: dict, task: dict) -> dict:
    """Merge task requirements over defaults (lists union, labels merge by key)."""
    merged: dict = {}
    if defaults.get("os") is not None:
        merged["os"] = str(defaults["os"])
    for key in ("tags", "providers"):
        base = [str(x) for x in (defaults.get(key) or [])]
        extra = [str(x) for x in (task.get(key) or [])]
        values = list(dict.fromkeys(base + extra))
        if values:
            merged[key] = values
    labels = {str(k): str(v) for k, v in (defaults.get("labels") or {}).items()}
    labels.update({str(k): str(v) for k, v in (task.get("labels") or {}).items()})
    if labels:
        merged["labels"] = labels
    if task.get("os") is not None:
        merged["os"] = str(task["os"])
    return merged


def _merge_resources(default_defaults: list | None, task_resources: list | None) -> list[dict]:
    """Merge resources by ``id``: task overrides default, new ids append.

    A resource without ``id`` gets a generated, position-based id, so anonymous
    resources never collide and simply accumulate.
    """
    merged: dict[str, dict] = {}
    order: list[str] = []
    for raw in list(default_defaults or []) + list(task_resources or []):
        if not isinstance(raw, dict):
            raise PlanError(f"invalid resource: {raw!r}")
        rtype = raw.get("type")
        if not rtype:
            raise PlanError(f"resource without a 'type': {raw!r}")
        resource_id = str(raw.get("id") or f"{rtype}-{len(order)}")
        entry = dict(raw)
        entry["id"] = resource_id
        if resource_id not in merged:
            order.append(resource_id)
        merged[resource_id] = entry
    return [merged[resource_id] for resource_id in order]


def substitute_resources(resources: list[dict], run_id: str, task_id: str) -> list[dict]:
    """Resolve ``{run}`` / ``{task}`` in resource option strings.

    Only task-local substitutions exist; cross-task references are not
    supported (a consumer writes the producer's branch name and lists the
    dependency in ``depends_on``). This is the same substitution the scheduler
    applies before dispatching a task, exposed so the UI/MCP can display the
    resolved resources of a task that has not run yet.
    """

    def substitute(value):
        if isinstance(value, str):
            return value.replace("{run}", run_id).replace("{task}", task_id)
        if isinstance(value, list):
            return [substitute(item) for item in value]
        if isinstance(value, dict):
            return {key: substitute(item) for key, item in value.items()}
        return value

    return [substitute(resource) for resource in (resources or [])]


def _merge_opencode(defaults: dict, override: dict | None) -> dict:
    """Merge defaults + task OpenCode blocks: dicts merge, lists concatenate."""
    base = defaults.get("opencode")
    over = override or {}
    if not isinstance(base, dict):
        base = {}
    merged: dict = {}
    for key in set(base) | set(over):
        left = base.get(key)
        right = over.get(key)
        if isinstance(left, dict) and isinstance(right, dict):
            merged[key] = {**left, **right}
        elif isinstance(left, list) and isinstance(right, list):
            merged[key] = left + right
        elif key in over:
            merged[key] = right
        else:
            merged[key] = left
    return merged


def _merge_artifacts(defaults: dict, override: dict | None) -> dict:
    merged: dict = {}
    base = defaults.get("artifacts")
    if isinstance(base, dict):
        merged.update(base)
    if isinstance(override, dict):
        merged.update(override)
    return merged


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
    artifacts = _merge_artifacts(defaults, raw.get("artifacts") or {})
    download = str(artifacts.get("download") or pick("download", "modified"))
    resources = _merge_resources(defaults.get("resources"), raw.get("resources"))
    return Task(
        id=str(task_id),
        prompt=_load_prompt(raw, base_dir),
        files=[str(f) for f in files],
        model=pick("model"),
        agent=pick("agent"),
        timeout_s=float(timeout) if timeout is not None else None,
        variant=pick("variant"),
        title=pick("title") or str(task_id),
        env={str(k): str(v) for k, v in env.items()},
        depends_on=[str(d) for d in (raw.get("depends_on") or [])],
        inputs_from=[str(d) for d in (raw.get("inputs_from") or [])],
        download=download,
        requirements=_merge_requirements(
            defaults.get("requirements") or {}, raw.get("requirements") or {}
        ),
        resources=resources,
        artifacts=artifacts,
        opencode=_merge_opencode(defaults, raw.get("opencode") or {}),
    )


def _parse_gate(raw: dict, defaults: dict, base_dir: Path) -> Task:
    """Compile a top-level ``gates`` entry into a gate Task.

    Gates do not inherit ``defaults.resources``/``opencode``/``requirements``:
    they run with the server-provided agent in a fresh workspace, so a plan's
    repo paths or permission fragments cannot leak into them.
    """
    if not isinstance(raw, dict):
        raise PlanError(f"invalid gate: {raw!r}")
    gate_id = raw.get("id")
    if not gate_id:
        raise PlanError("every gate must have an 'id'")
    targets = raw.get("tasks")
    if not isinstance(targets, list) or not targets:
        raise PlanError(f"gate '{gate_id}': 'tasks' must be a non-empty list of task ids")
    max_attempts = raw.get("max_attempts")
    max_attempts = DEFAULT_MAX_ATTEMPTS if max_attempts is None else int(max_attempts)
    if max_attempts < 1:
        raise PlanError(f"gate '{gate_id}': 'max_attempts' must be >= 1")

    timeout = raw.get("timeout_s", defaults.get("timeout_s"))
    return Task(
        id=str(gate_id),
        prompt=_load_prompt(raw, base_dir),
        model=raw.get("model", defaults.get("model")),
        agent=GATE_AGENT_ID,
        timeout_s=float(timeout) if timeout is not None else None,
        variant=raw.get("variant", defaults.get("variant")),
        title=raw.get("title") or str(gate_id),
        depends_on=[str(target) for target in targets],
        download="none",
        resources=[],
        opencode={"agents": {GATE_AGENT_ID: GATE_AGENT_MD}},
        kind="gate",
        gate_targets=[str(target) for target in targets],
        gate_max_attempts=max_attempts,
    )


def _validate_gates(tasks: list[Task]) -> None:
    """A gate analyses plain tasks, and their consumers must depend on it.

    Without this rule a task could consume a gated task directly and run on an
    un-approved artifact (the review-as-decoration problem).
    """
    by_id = {task.id: task for task in tasks}
    gated: dict[str, str] = {}
    for task in tasks:
        if task.kind != "gate":
            continue
        for target_id in task.gate_targets:
            target = by_id.get(target_id)
            if target is None:
                raise PlanError(
                    f"gate '{task.id}' analyses an unknown task: {target_id}"
                )
            if target.kind == "gate":
                raise PlanError(
                    f"gate '{task.id}' cannot analyse another gate: {target_id}"
                )
            if target_id in gated:
                raise PlanError(
                    f"task '{target_id}' is analysed by more than one gate "
                    f"('{gated[target_id]}' and '{task.id}')"
                )
            gated[target_id] = task.id

    for task in tasks:
        if task.kind == "gate":
            continue
        deps = set(task.depends_on) | set(task.inputs_from)
        for dep in deps:
            gate_id = gated.get(dep)
            if gate_id and gate_id not in deps and gated.get(task.id) != gate_id:
                # A task inside the same gated unit may consume another target
                # (e.g. a reviewer that depends on the producer it reviews):
                # both are reset together when the gate rejects, so there is no
                # un-approved artifact to protect against.
                raise PlanError(
                    f"task '{task.id}' consumes gated task '{dep}' but does "
                    f"not depend on its gate '{gate_id}'"
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
        raw = yaml.safe_load(plan_path.read_text(encoding="utf-8-sig")) or {}
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
    gates_raw = raw.get("gates") or []
    if not isinstance(gates_raw, list):
        raise PlanError("'gates' must be a list")
    defaults = raw.get("defaults") or {}

    resolve_base = (
        Path(base_dir).expanduser().resolve() if base_dir else plan_path.parent
    )
    tasks = [_parse_task(entry, defaults, resolve_base) for entry in tasks_raw]
    gates = [_parse_gate(entry, defaults, resolve_base) for entry in gates_raw]
    ordered = _validate_and_order(tasks + gates)
    _validate_gates(ordered)

    return Plan(
        path=plan_path,
        base_dir=resolve_base,
        version=int(raw.get("version", 1)),
        defaults=defaults,
        tasks=ordered,
    )

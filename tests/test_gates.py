"""Gates: plan parsing/validation and the bounded reset loop."""

import pytest

from server.app.gate import build_gate_prompt, is_valid_verdict
from server.app.orchestrator import Orchestrator, TaskResult
from server.app.plan import PlanError, load_plan
from server.app.runs import RunStore, build_task_states

BASIC = """\
version: 1
tasks:
  - id: a
    prompt: produce
  - id: c
    prompt: consume
    depends_on: [ga]
gates:
  - id: ga
    tasks: [a]
    prompt: check the result
    max_attempts: 3
"""


def _write(tmp_path, text):
    path = tmp_path / "plan.yaml"
    path.write_text(text, encoding="utf-8")
    return path


# ----------------------------------------------------------------- parsing
def test_gate_is_compiled_into_a_task(tmp_path):
    plan = load_plan(_write(tmp_path, BASIC))
    assert [t.id for t in plan.tasks] == ["a", "ga", "c"]
    gate = plan.task_by_id("ga")
    assert gate.kind == "gate"
    assert gate.gate_targets == ["a"]
    assert gate.gate_max_attempts == 3
    assert gate.agent == "hiveling-gate"
    assert gate.resources == []
    assert "hiveling-gate" in gate.opencode["agents"]


def test_gate_ignores_defaults_resources_and_opencode(tmp_path):
    text = """\
version: 1
defaults:
  resources:
    - {type: git, id: repo, with: {repo: R, path: repo}}
  opencode:
    agents_paths: [./src/repo/.opencode/agents]
tasks:
  - id: a
    prompt: produce
gates:
  - id: ga
    tasks: [a]
    prompt: check
"""
    plan = load_plan(_write(tmp_path, text))
    gate = plan.task_by_id("ga")
    # No inherited repo resource, and the only opencode key is the gate agent.
    assert gate.resources == []
    assert list(gate.opencode) == ["agents"]


def test_gate_default_max_attempts(tmp_path):
    text = "version: 1\ntasks:\n  - {id: a, prompt: x}\ngates:\n  - {id: ga, tasks: [a], prompt: check}\n"
    assert load_plan(_write(tmp_path, text)).task_by_id("ga").gate_max_attempts == 20


def test_consumer_without_gate_dependency_is_rejected(tmp_path):
    text = """\
version: 1
tasks:
  - {id: a, prompt: produce}
  - {id: b, prompt: consume, depends_on: [a]}
gates:
  - {id: ga, tasks: [a], prompt: check}
"""
    with pytest.raises(PlanError, match="does not depend on its gate"):
        load_plan(_write(tmp_path, text))


def test_reviewer_target_may_depend_on_producer_target(tmp_path):
    """A reviewer inside the gate's tasks may consume the producer it reviews."""
    text = """\
version: 1
tasks:
  - {id: design, prompt: produce}
  - {id: design-review, prompt: review, depends_on: [design]}
  - {id: implement, prompt: implement, depends_on: [design-gate]}
gates:
  - {id: design-gate, tasks: [design, design-review], prompt: check}
"""
    plan = load_plan(_write(tmp_path, text))
    assert [t.id for t in plan.tasks] == [
        "design",
        "design-review",
        "design-gate",
        "implement",
    ]


def test_gate_feedback_tells_a_reviewer_to_re_inspect():
    from server.app.gate import with_gate_feedback

    text = with_gate_feedback("review it", "the design contradicts itself")
    assert "the design contradicts itself" in text
    assert "re-inspect the updated artifact" in text
    assert "rewrite your report" in text


def test_gate_unknown_target_is_rejected(tmp_path):
    text = """\
version: 1
tasks:
  - {id: a, prompt: produce}
gates:
  - {id: ga, tasks: [missing], prompt: check}
"""
    with pytest.raises(PlanError, match="unknown task"):
        load_plan(_write(tmp_path, text))


def test_gate_cannot_analyse_a_gate(tmp_path):
    text = """\
version: 1
tasks:
  - {id: a, prompt: produce}
gates:
  - {id: ga, tasks: [a], prompt: check}
  - {id: gb, tasks: [ga], prompt: check again}
"""
    with pytest.raises(PlanError, match="cannot analyse another gate"):
        load_plan(_write(tmp_path, text))


def test_task_belongs_to_one_gate(tmp_path):
    text = """\
version: 1
tasks:
  - {id: a, prompt: produce}
gates:
  - {id: ga, tasks: [a], prompt: check}
  - {id: gb, tasks: [a], prompt: check again}
"""
    with pytest.raises(PlanError, match="more than one gate"):
        load_plan(_write(tmp_path, text))


# ------------------------------------------------------------------- verdict
@pytest.mark.parametrize(
    "text,expected",
    [
        ("VALID", True),
        ("looks good\nVALID\n", True),
        ("- VALID.", True),
        ("  valid  ", True),
        ("INVALID", False),
        ("not VALID", False),
        ("needs work:\n- fix routing\n- fix skip link", False),
        ("", False),
    ],
)
def test_is_valid_verdict(text, expected):
    assert is_valid_verdict(text) is expected


def test_build_gate_prompt_includes_targets_and_contract():
    prompt = build_gate_prompt("check it", ["a", "b"], attempt=2, max_attempts=5)
    assert "check it" in prompt
    assert "a, b" in prompt
    assert "attempt 2" in prompt.lower()
    assert "VALID" in prompt


# ----------------------------------------------------------------- reset loop
def _orchestrator(tmp_path, text):
    path = _write(tmp_path, text)
    plan = load_plan(path)
    store = RunStore(tmp_path / "runs")
    run_id = "2026-01-01T00-00-00-test"
    store.create(
        run_id,
        plan_path=str(path),
        plan_name="plan.yaml",
        only=None,
        tasks=build_task_states(plan),
    )
    orch = Orchestrator(
        plan, history_dir=tmp_path / "history", run_store=store, run_id=run_id, quiet=True
    )
    return orch, store, run_id


def test_gate_rejection_resets_targets_and_gate(tmp_path):
    orch, store, run_id = _orchestrator(tmp_path, BASIC)
    orch._store_task("a", status="succeeded")
    orch._store_task("ga", status="running")
    results = {"a": "succeeded", "ga": "running"}
    running = {"ga": (None, None)}
    event = TaskResult("ga", "succeeded", text="fix routing", gate_valid=False, history_rel=None)

    failed = orch._handle_events([("result", "ga", None, event)], results, running)

    assert failed is False
    assert results == {}  # a and ga are reset, c still cannot run
    run = store.read(run_id)
    states = {t["id"]: t for t in run["tasks"]}
    assert states["a"]["status"] == "pending"
    assert states["a"]["attempts"] == 1
    assert states["a"]["gate_feedback"] == "fix routing"
    assert states["ga"]["status"] == "pending"
    assert states["ga"]["gate_attempt"] == 1
    assert orch._gate_attempts["ga"] == 1
    assert orch._gate_feedback["a"] == "fix routing"


def test_gate_exhaustion_fails_the_run(tmp_path):
    text = BASIC.replace("max_attempts: 3", "max_attempts: 1")
    orch, store, run_id = _orchestrator(tmp_path, text)
    orch._store_task("a", status="succeeded")
    results = {"a": "succeeded"}
    running = {"ga": (None, None)}
    event = TaskResult("ga", "succeeded", text="still wrong", gate_valid=False, history_rel=None)

    failed = orch._handle_events([("result", "ga", None, event)], results, running)

    assert failed is True
    assert results["ga"] == "failed"
    states = {t["id"]: t for t in store.read(run_id)["tasks"]}
    assert states["ga"]["status"] == "failed"
    assert "rejected after 1 attempt" in states["ga"]["error"]


def test_gate_valid_succeeds(tmp_path):
    orch, store, run_id = _orchestrator(tmp_path, BASIC)
    results = {"ga": "running"}
    running = {"ga": (None, None)}
    event = TaskResult("ga", "succeeded", text="VALID", gate_valid=True, history_rel=None)

    failed = orch._handle_events([("result", "ga", None, event)], results, running)

    assert failed is False
    assert results["ga"] == "succeeded"
    states = {t["id"]: t for t in store.read(run_id)["tasks"]}
    assert states["ga"]["gate_verdict"] == "VALID"


def test_gate_verdict_cleared_to_valid_after_a_rejection(tmp_path):
    """A gate rejected once then passed must not keep the stale INVALID verdict."""
    orch, store, run_id = _orchestrator(tmp_path, BASIC)
    # First evaluation: rejection (resets the target and the gate).
    orch._handle_events(
        [("result", "ga", None, TaskResult("ga", "succeeded", text="fix it", gate_valid=False, history_rel=None))],
        {"a": "succeeded", "ga": "running"},
        {"ga": (None, None)},
    )
    assert {t["id"]: t for t in store.read(run_id)["tasks"]}["ga"]["gate_verdict"] == "INVALID"
    # Second evaluation: VALID.
    results = {"a": "succeeded", "ga": "running"}
    orch._handle_events(
        [("result", "ga", None, TaskResult("ga", "succeeded", text="VALID", gate_valid=True, history_rel=None))],
        results,
        {"ga": (None, None)},
    )
    gate = {t["id"]: t for t in store.read(run_id)["tasks"]}["ga"]
    assert gate["status"] == "succeeded"
    assert gate["gate_verdict"] == "VALID"
    assert gate["gate_attempt"] == 1


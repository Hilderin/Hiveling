"""Worker affinity: prefer a worker that already ran a task of this run.

Reusing the same worker lets its durable clone/worktree serve the next task of
the run instead of cloning the repository again on another worker. The
preference must never override free/busy state or requirements.
"""

from types import SimpleNamespace

from server.app.orchestrator import Orchestrator
from server.app.plan import Plan, Task


def _client(name: str):
    return SimpleNamespace(
        base_url=f"http://{name}", endpoint=SimpleNamespace(name=name)
    )


def _orchestrator(tmp_path) -> "Orchestrator":
    plan = Plan(path=tmp_path / "p.yaml", base_dir=tmp_path, version=1, defaults={}, tasks=[])
    return Orchestrator(plan, history_dir=tmp_path / "history")


def _task(requirements: dict | None = None) -> Task:
    return Task(id="t", prompt="p", requirements=requirements or {})


def test_prefers_the_worker_used_by_this_run(tmp_path):
    orch = _orchestrator(tmp_path)
    a, b = _client("a"), _client("b")
    # 'b' ran the previous task of the run; it must be preferred when free.
    orch._run_workers[b.base_url] = 1.0
    available = [(a, {}, False, False), (b, {}, False, False)]

    pick, index, matching = orch._choose_worker(_task(), available, set())

    assert pick is b
    assert matching is True


def test_round_robin_without_affinity(tmp_path):
    orch = _orchestrator(tmp_path)
    a, b = _client("a"), _client("b")
    available = [(a, {}, False, False), (b, {}, False, False)]

    first, _, _ = orch._choose_worker(_task(), available, set())
    second, _, _ = orch._choose_worker(_task(), available, set())

    assert first is a
    assert second is b


def test_affinity_falls_back_when_the_worker_is_busy(tmp_path):
    orch = _orchestrator(tmp_path)
    a, b = _client("a"), _client("b")
    orch._run_workers[b.base_url] = 1.0
    available = [(a, {}, False, False), (b, {}, True, False)]  # b is busy

    pick, _, _ = orch._choose_worker(_task(), available, set())

    assert pick is a


def test_affinity_respects_requirements(tmp_path):
    orch = _orchestrator(tmp_path)
    a, b = _client("a"), _client("b")
    orch._run_workers[b.base_url] = 1.0
    # b is the affinity worker but lacks the required tag: a must be chosen.
    available = [
        (a, {"tags": ["linux"]}, False, False),
        (b, {"tags": ["windows"]}, False, False),
    ]

    pick, _, _ = orch._choose_worker(_task({"tags": ["linux"]}), available, set())

    assert pick is a


def test_no_matching_worker_is_reported(tmp_path):
    orch = _orchestrator(tmp_path)
    a = _client("a")
    available = [(a, {"tags": ["windows"]}, False, False)]

    pick, _, matching = orch._choose_worker(_task({"tags": ["linux"]}), available, set())

    assert pick is None
    assert matching is False

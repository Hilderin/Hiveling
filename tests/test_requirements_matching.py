"""Scheduler requirement matching against advertised worker capabilities."""

from server.app.orchestrator import Orchestrator
from server.app.plan import Task


def make_task(requirements: dict) -> Task:
    return Task(id="t", prompt="p", requirements=requirements)


def test_no_requirements_matches_anything():
    assert Orchestrator._matches(make_task({}), {})
    assert Orchestrator._matches(make_task({}), {"os": "windows"})


def test_os_must_match_exactly():
    assert Orchestrator._matches(make_task({"os": "windows"}), {"os": "windows"})
    assert not Orchestrator._matches(make_task({"os": "windows"}), {"os": "linux"})
    assert not Orchestrator._matches(make_task({"os": "windows"}), {})


def test_tags_are_a_subset():
    assert Orchestrator._matches(make_task({"tags": ["a"]}), {"tags": ["a", "b"]})
    assert Orchestrator._matches(make_task({"tags": ["a", "b"]}), {"tags": ["a", "b"]})
    assert not Orchestrator._matches(make_task({"tags": ["a", "c"]}), {"tags": ["a", "b"]})
    assert not Orchestrator._matches(make_task({"tags": ["a"]}), {})


def test_providers_are_a_subset():
    caps = {"providers": ["ephemeral", "git"]}
    assert Orchestrator._matches(make_task({"providers": ["git"]}), caps)
    assert not Orchestrator._matches(make_task({"providers": ["docker"]}), caps)


def test_labels_match_exactly_per_key():
    caps = {"labels": {"site": "mtl", "team": "core"}}
    assert Orchestrator._matches(make_task({"labels": {"site": "mtl"}}), caps)
    assert not Orchestrator._matches(make_task({"labels": {"site": "qc"}}), caps)
    assert not Orchestrator._matches(make_task({"labels": {"missing": "x"}}), caps)


def test_combined_requirements():
    task = make_task({"os": "windows", "tags": ["legacy"], "providers": ["git"]})
    good = {"os": "windows", "tags": ["legacy", "x"], "providers": ["ephemeral", "git"]}
    bad = {"os": "linux", "tags": ["legacy"], "providers": ["ephemeral", "git"]}
    assert Orchestrator._matches(task, good)
    assert not Orchestrator._matches(task, bad)

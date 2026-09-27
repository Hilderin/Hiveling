"""Live task events: the SSE endpoint streams a task's events.jsonl.

A running task is tailed from its worker job; a finished task replays the saved
log. Either way the browser sees each event as it arrives instead of only the
final result. These tests cover the terminal replay path (no worker needed).
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from server.app.web import DashboardConfig, RunManager, create_app

PLAN = """\
version: 1
tasks:
  - id: a
    prompt: x
  - id: b
    prompt: y
"""


def _setup(tmp_path):
    plans = tmp_path / "plans"
    plans.mkdir()
    plan_file = plans / "p.yaml"
    plan_file.write_text(PLAN, encoding="utf-8")
    config = DashboardConfig(
        data_dir=tmp_path,
        plans_dir=plans,
        workers_file=tmp_path / "workers.yaml",
    )
    manager = RunManager(config)
    manager.store.create(
        "run-1",
        plan_path=str(plan_file),
        plan_name="p.yaml",
        only=None,
        plan_snapshot=str(plan_file),
        base_dir=str(tmp_path),
        tasks=[
            {"id": "a", "kind": "task", "status": "succeeded", "attempts": 1,
             "history_rel": "a/run-1", "depends_on": []},
            {"id": "b", "kind": "task", "status": "skipped", "attempts": 0,
             "history_rel": None, "depends_on": []},
        ],
    )
    history = tmp_path / "history" / "a" / "run-1"
    history.mkdir(parents=True)
    (history / "events.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"type": "text", "part": {"text": "hello"}}),
                json.dumps(
                    {"type": "error", "message": f"failed at {tmp_path}/x"},
                    ensure_ascii=False,
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return config, manager


def _stream(client, url):
    with client.stream("GET", url) as response:
        return response.status_code, response.headers.get("content-type", ""), list(
            response.iter_lines()
        )


def test_terminal_task_streams_saved_events_then_ends(tmp_path):
    config, manager = _setup(tmp_path)
    with TestClient(create_app(config, manager)) as client:
        status, content_type, lines = _stream(
            client, "/api/runs/run-1/tasks/a/events"
        )

    assert status == 200
    assert content_type.startswith("text/event-stream")
    payloads = [line[len("data: "):] for line in lines if line.startswith("data: ")]
    assert any("hello" in payload for payload in payloads)
    assert any("failed at" in payload for payload in payloads)
    assert lines[-1] == "event: end" or "event: end" in lines
    # The server's data dir is scrubbed from event payloads.
    assert str(tmp_path) not in "\n".join(lines)
    assert "<data-dir>" in "\n".join(lines)


def test_terminal_task_without_history_just_ends(tmp_path):
    config, manager = _setup(tmp_path)
    with TestClient(create_app(config, manager)) as client:
        status, content_type, lines = _stream(
            client, "/api/runs/run-1/tasks/b/events"
        )

    assert status == 200
    assert content_type.startswith("text/event-stream")
    assert "event: end" in lines


def test_unknown_task_stream_is_404(tmp_path):
    config, manager = _setup(tmp_path)
    with TestClient(create_app(config, manager)) as client:
        response = client.get("/api/runs/run-1/tasks/missing/events")
    assert response.status_code == 404

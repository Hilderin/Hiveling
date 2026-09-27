"""Clients and LLMs must never see where the server/workers keep their files.

Regression: ``list_workers`` returned ``data_dir`` and ``workers_file`` (how the
reporter located ``.data``), and plan/run/task responses returned ``plan_path``,
``base_dir``, ``plan_snapshot``, ``history_rel`` and the worker URL. The JSON API
and the MCP tools now return logical identifiers (name, run id, task id, worker
name) only.
"""

import asyncio
import json

from fastapi.testclient import TestClient

from server.app.mcp_server import create_mcp_server
from server.app.redact import make_redactor
from server.app.web import DashboardConfig, RunManager, create_app

PLAN = """\
version: 1
tasks:
  - id: a
    prompt: produce
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
        owner={"pid": 1, "host": "secret-host", "heartbeat_at": 0.0},
        tasks=[
            {
                "id": "a",
                "kind": "task",
                "status": "succeeded",
                "attempts": 1,
                "history_rel": "a/run-1",
                "worker": "local",
                "worker_url": "http://10.0.0.174:8787",
                "job_id": "a-1234",
                "depends_on": [],
                "resources": [
                    {
                        "type": "git",
                        "id": "repo",
                        "with": {
                            "repo": "https://example.test/app",
                            "path": "D:\\data\\repo",
                            "branch": "hiveling/{run}/feature",
                            "ref": "main",
                            "publish": "push",
                        },
                    }
                ],
            }
        ],
    )
    history = tmp_path / "history" / "a" / "run-1"
    history.mkdir(parents=True)
    (history / "status.json").write_text('{"status": "succeeded"}', encoding="utf-8")
    (history / "request.json").write_text(
        json.dumps(
            {
                "prompt": "produce",
                "resources": [{"type": "path", "id": "p", "with": {"path": "D:\\data\\p"}}],
                "opencode": {"agents_paths": ["D:\\data\\agents"]},
            }
        ),
        encoding="utf-8",
    )
    (history / "result.txt").write_text("done", encoding="utf-8")
    return config, manager


def _without_probe_patch(module, base):
    original = module.probe_workers

    def fake(*_args, **_kwargs):
        return {
            "error": f"workers file not found: {base}/workers.yaml",
            "workers": [
                {
                    "name": "win",
                    "url": "http://10.0.0.174:8787",
                    "reachable": True,
                    "busy": False,
                    "opencode_bin": f"{base}/bin/opencode",
                    "capabilities": {"os": "windows", "path_roots": [f"{base}/src"]},
                }
            ],
        }

    module.probe_workers = fake
    return original


def test_workers_endpoint_hides_server_and_worker_paths(tmp_path):
    import server.app.web as web

    config, manager = _setup(tmp_path)
    original = _without_probe_patch(web, config.data_dir)
    try:
        with TestClient(create_app(config, manager)) as client:
            data = client.get("/api/workers").json()
    finally:
        web.probe_workers = original

    assert "data_dir" not in data
    assert "workers_file" not in data
    worker = data["workers"][0]
    assert worker["name"] == "win"
    assert "url" not in worker
    assert "opencode_bin" not in worker
    assert "path_roots" not in worker["capabilities"]
    assert str(config.data_dir) not in json.dumps(data)


def test_run_and_task_endpoints_hide_paths(tmp_path):
    config, manager = _setup(tmp_path)
    with TestClient(create_app(config, manager)) as client:
        summary = client.get("/api/runs").json()["runs"][0]
        run = client.get("/api/runs/run-1").json()
        task = client.get("/api/runs/run-1/tasks/a").json()
        plan = client.get("/api/runs/run-1/plan").json()

    for payload in (summary, run):
        assert "plan_path" not in payload
        assert "plan_snapshot" not in payload
    assert "base_dir" not in run
    assert "owner" not in run
    assert "history_rel" not in run["tasks"][0]
    assert "worker_url" not in run["tasks"][0]
    # The logical identifiers a client needs stay available.
    assert run["run_id"] == "run-1"
    assert run["tasks"][0]["worker"] == "local"

    assert "history_rel" not in task["task"]
    assert "worker_url" not in task["task"]
    assert "path" not in task["request"]["resources"][0]["with"]
    assert "agents_paths" not in task["request"]["opencode"]
    # Resolved git resources are exposed so branches are visible in the UI; the
    # local path is still stripped.
    assert task["resources"][0]["with"]["branch"] == "hiveling/run-1/feature"
    assert "path" not in task["resources"][0]["with"]

    assert "path" not in plan
    assert "source_path" not in plan
    assert "snapshot" not in plan
    assert plan["name"] == "p.yaml"


def test_mcp_tools_hide_paths(tmp_path):
    import server.app.mcp_server as mcp_mod

    config, manager = _setup(tmp_path)
    mcp = create_mcp_server(manager, config)
    original = _without_probe_patch(mcp_mod, config.data_dir)
    try:
        workers = asyncio.run(mcp.call_tool("list_workers", {})).structured_content
        run = asyncio.run(mcp.call_tool("get_run", {"run_id": "run-1"})).structured_content
        task = asyncio.run(
            mcp.call_tool("get_task", {"run_id": "run-1", "task_id": "a"})
        ).structured_content
    finally:
        mcp_mod.probe_workers = original

    assert "data_dir" not in workers
    assert "workers_file" not in workers
    assert "url" not in workers["workers"][0]
    assert "path_roots" not in workers["workers"][0]["capabilities"]

    assert "plan_path" not in run
    assert "plan_snapshot" not in run
    assert "base_dir" not in run
    assert "owner" not in run
    assert "history_rel" not in run["tasks"][0]
    assert "worker_url" not in run["tasks"][0]

    assert "path" not in task["request"]["resources"][0]["with"]
    assert "agents_paths" not in task["request"]["opencode"]
    assert task["resources"][0]["with"]["branch"] == "hiveling/run-1/feature"
    assert "path" not in task["resources"][0]["with"]


def test_redactor_scrubs_server_roots_in_text(tmp_path):
    config = DashboardConfig(
        data_dir=tmp_path,
        plans_dir=tmp_path / "plans",
        workers_file=tmp_path / "workers.yaml",
    )
    redact = make_redactor(config)
    payload = redact(
        {
            "data_dir": str(tmp_path),
            "workers": [
                {"url": "http://10.0.0.174:8787", "capabilities": {"path_roots": ["D:\\src"]}}
            ],
            "error": f"plan not found: {tmp_path}/plans/missing.yaml",
            "run_id": "run-1",
        }
    )
    assert payload["run_id"] == "run-1"
    assert "data_dir" not in payload
    assert "url" not in payload["workers"][0]
    assert "path_roots" not in payload["workers"][0]["capabilities"]
    assert str(tmp_path) not in json.dumps(payload)
    assert "<plans-dir>" in payload["error"]

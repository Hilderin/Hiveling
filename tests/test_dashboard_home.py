"""Dashboard home page, plans API and live plan editing while a run is active.

Covers the todo items that live in the server/dashboard layer:
- a home view on the right, reachable by clicking the header title;
- browser back/forward via pushState + popstate;
- the plans API the home page starts runs from;
- editing a run's plan (including relative ``prompt_file``) while it runs;
- no manual "Refresh" button.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from server.app.dashboard_html import DASHBOARD_HTML
from server.app.web import DashboardConfig, RunManager, create_app

PLAN_WITH_PROMPT_FILE = """\
version: 1
tasks:
  - id: a
    prompt_file: prompt.md
"""


def _config(tmp_path) -> tuple[DashboardConfig, RunManager]:
    plans = tmp_path / "plans"
    plans.mkdir()
    config = DashboardConfig(
        data_dir=tmp_path, plans_dir=plans, workers_file=tmp_path / "workers.yaml"
    )
    return config, RunManager(config)


def test_dashboard_html_has_home_back_nav_and_no_refresh_button():
    assert "home-view" in DASHBOARD_HTML
    assert "openHome" in DASHBOARD_HTML
    assert "popstate" in DASHBOARD_HTML
    assert "pushState" in DASHBOARD_HTML
    # Cancel run asks for confirmation through the in-page modal.
    assert "askConfirm" in DASHBOARD_HTML
    assert "confirm-overlay" in DASHBOARD_HTML
    assert "window.confirm" not in DASHBOARD_HTML
    # The disruptive manual refresh button is gone (the button, not the fn).
    assert 'onclick="refresh(true)"' not in DASHBOARD_HTML
    assert '>Refresh<' not in DASHBOARD_HTML


def test_plans_endpoint_lists_stored_plans(tmp_path):
    config, manager = _config(tmp_path)
    (config.plans_dir / "p.yaml").write_text(
        "version: 1\ntasks:\n  - id: a\n    prompt: x\n", encoding="utf-8"
    )
    (config.plans_dir / "broken.yaml").write_text("version: 1\ntasks: []\n", encoding="utf-8")

    with TestClient(create_app(config, manager)) as client:
        data = client.get("/api/plans").json()

    by_name = {p["name"]: p for p in data["plans"]}
    assert by_name["p.yaml"]["task_count"] == 1
    assert by_name["p.yaml"]["editable"] is True
    assert by_name["p.yaml"]["error"] == ""
    assert by_name["broken.yaml"]["error"]


def test_update_run_plan_resolves_relative_prompt_file_from_base_dir(tmp_path):
    config, manager = _config(tmp_path)
    source = config.plans_dir / "p.yaml"
    source.write_text(PLAN_WITH_PROMPT_FILE, encoding="utf-8")
    (config.plans_dir / "prompt.md").write_text("do it", encoding="utf-8")

    # The run executes a snapshot under the data dir; its relative prompt_file
    # still refers to the plans dir, captured as the run's base_dir.
    snapshot = tmp_path / "runs" / "run-1" / "plan.yaml"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text(PLAN_WITH_PROMPT_FILE, encoding="utf-8")
    manager.store.create(
        "run-1",
        plan_path=str(source),
        plan_name="p.yaml",
        only=None,
        plan_snapshot=str(snapshot),
        base_dir=str(config.plans_dir),
        tasks=[{"id": "a", "kind": "task", "status": "running", "attempts": 1}],
    )

    # Editing while the run is active validates against base_dir, so it works.
    edited = PLAN_WITH_PROMPT_FILE.replace("id: a", "id: a\n    title: edited")
    manager.update_run_plan("run-1", edited)

    assert "title: edited" in snapshot.read_text(encoding="utf-8")
    assert "title: edited" in source.read_text(encoding="utf-8")

"""Workspace and history garbage collection."""

import json
import os
import time

from server.app.gc import prune_runs
from worker.app.gc import prune


def make_job(workspace, job_id, status, age_days=0.0):
    job_dir = workspace / job_id
    job_dir.mkdir(parents=True)
    status_file = job_dir / "status.json"
    status_file.write_text(json.dumps({"status": status}), encoding="utf-8")
    stamp = time.time() - age_days * 86400
    os.utime(status_file, (stamp, stamp))


def test_worker_retention_defaults_are_50_and_14():
    from worker.app.config import Config

    config = Config.from_args([])
    assert config.retention_jobs == 50
    assert config.retention_days == 14.0


def test_worker_gc_keeps_newest_and_never_running(tmp_path):
    workspace = tmp_path / "worker"
    workspace.mkdir()
    make_job(workspace, "old", "succeeded", age_days=10)
    make_job(workspace, "mid", "failed", age_days=5)
    make_job(workspace, "run", "running", age_days=20)
    make_job(workspace, "new", "succeeded", age_days=1)
    result = prune(workspace, keep=1)
    assert result["removed"] == 2
    assert (workspace / "new").exists()
    assert (workspace / "run").exists()  # running is never touched
    assert not (workspace / "old").exists()
    assert not (workspace / "mid").exists()


def test_worker_gc_tolerates_bom(tmp_path):
    workspace = tmp_path / "worker"
    workspace.mkdir()
    old = workspace / "old"
    old.mkdir()
    status = old / "status.json"
    status.write_text('\ufeff{"status": "succeeded"}', encoding="utf-8")
    stamp = time.time() - 3600
    os.utime(status, (stamp, stamp))
    make_job(workspace, "new", "succeeded", age_days=0)
    result = prune(workspace, keep=1)
    assert result["removed"] == 1
    assert not old.exists()


def test_worker_gc_disabled_by_default(tmp_path):
    workspace = tmp_path / "worker"
    workspace.mkdir()
    make_job(workspace, "old", "succeeded", age_days=99)
    assert prune(workspace)["removed"] == 0
    assert (workspace / "old").exists()


def test_worker_gc_by_age(tmp_path):
    workspace = tmp_path / "worker"
    workspace.mkdir()
    make_job(workspace, "old", "succeeded", age_days=30)
    make_job(workspace, "new", "succeeded", age_days=1)
    assert prune(workspace, older_than_days=7)["removed"] == 1
    assert (workspace / "new").exists()


def make_run(runs, history, run_id, status, age_days):
    run_dir = runs / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps({"status": status, "finished_at": time.time() - age_days * 86400}),
        encoding="utf-8",
    )
    task_dir = history / "task" / run_id
    task_dir.mkdir(parents=True)
    (task_dir / "result.txt").write_text("x", encoding="utf-8")


def test_server_gc_prunes_runs_and_history(tmp_path):
    runs = tmp_path / "runs"
    history = tmp_path / "history"
    make_run(runs, history, "r1", "succeeded", 10)
    make_run(runs, history, "r2", "failed", 1)
    make_run(runs, history, "r3", "running", 10)
    result = prune_runs(runs, history, keep=1)
    assert result["removed"] == 1
    assert not (runs / "r1").exists()
    assert (runs / "r3").exists()  # running kept
    assert not (history / "task" / "r1").exists()
    assert (history / "task" / "r3").exists()


def test_server_gc_disabled_by_default(tmp_path):
    runs = tmp_path / "runs"
    history = tmp_path / "history"
    make_run(runs, history, "r1", "succeeded", 99)
    assert prune_runs(runs, history)["removed"] == 0
    assert (runs / "r1").exists()

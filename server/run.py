"""Orchestrator entry point.

Usage:
    python server/run.py examples/demo.yaml
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from rich.console import Console  # noqa: E402

from app.logging_setup import log_shutdown, log_startup, setup_logging  # noqa: E402
from app.orchestrator import Orchestrator  # noqa: E402
from app.plan import PlanError, load_plan  # noqa: E402
from app.runs import RunStore, new_run_id  # noqa: E402
from app.workers import WorkerRegistry  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="server",
        description="Hiveling orchestrator: runs a plan.yaml on remote workers.",
    )
    parser.add_argument("plan", help="path to the plan.yaml file")
    parser.add_argument(
        "--history-dir",
        default=".data/history",
        help="history directory (default: ./.data/history)",
    )
    parser.add_argument(
        "--runs-dir",
        default=".data/runs",
        help="run state directory shown in the dashboard (default: ./.data/runs)",
    )
    parser.add_argument(
        "--workers-file",
        default=".data/workers.yaml",
        help="workers configuration file (default: ./.data/workers.yaml)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=None,
        help="interval (s) between two status checks (default: plan poll_interval_s or 2)",
    )
    parser.add_argument(
        "--worker-wait",
        type=float,
        default=1800.0,
        help="max time (s) to wait for a free worker (default: 1800)",
    )
    parser.add_argument(
        "--max-parallel",
        type=int,
        default=None,
        help="max concurrent tasks; default: the plan's max_parallel, else one per free worker",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=None,
        help="run only this task (repeatable)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plan without executing it",
    )
    parser.add_argument(
        "--log-dir",
        default=os.environ.get("HIVELING_LOG_DIR", ".data/logs"),
        help="directory for the rotating server log (default: ./.data/logs)",
    )
    parser.add_argument("--log-level", default=os.environ.get("HIVELING_LOG_LEVEL", "info"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    console = Console()
    log_path = setup_logging(Path(args.log_dir), args.log_level)
    logger = logging.getLogger("hiveling.server")

    try:
        plan = load_plan(args.plan)
    except PlanError as exc:
        console.print(f"[red]invalid plan:[/] {exc}")
        logger.error("invalid plan %s: %s", args.plan, exc)
        return 2

    poll_interval = args.poll_interval
    if poll_interval is None:
        poll_interval = float(plan.defaults.get("poll_interval_s", 2.0))

    run_id = new_run_id()
    runs_dir = Path(args.runs_dir).expanduser().resolve()
    run_store = RunStore(runs_dir)
    run_dir = run_store.path(run_id).parent
    run_dir.mkdir(parents=True, exist_ok=True)
    snapshot = run_dir / "plan.yaml"
    snapshot.write_text(plan.path.read_text(encoding="utf-8"), encoding="utf-8")

    registry = WorkerRegistry(Path(args.workers_file).expanduser().resolve())
    if registry.error():
        console.print(f"[yellow]workers:[/] {registry.error()}")
    console.print(f"[dim]run id: {run_id}[/]")
    log_startup(plan=args.plan, run_id=run_id, workers_file=args.workers_file, log_file=log_path)

    # Ctrl+C cancels the run (jobs stop, then in-flight tasks are drained)
    # instead of abruptly killing the worker threads mid-request.
    cancel_event = threading.Event()

    def _sigint(_signum, _frame) -> None:
        console.print("[yellow]interrupted: canceling run on the next poll...[/]")
        cancel_event.set()

    signal.signal(signal.SIGINT, _sigint)

    orchestrator = Orchestrator(
        plan,
        history_dir=Path(args.history_dir).expanduser().resolve(),
        poll_interval=poll_interval,
        worker_wait_timeout=args.worker_wait,
        max_parallel=args.max_parallel,
        console=console,
        dry_run=args.dry_run,
        only=args.only,
        run_store=None if args.dry_run else run_store,
        run_id=run_id,
        cancel_event=cancel_event,
        workers_provider=registry.get,
        plan_path=snapshot,
        plan_base_dir=plan.base_dir,
        plan_snapshot=snapshot,
        owner={
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "heartbeat_at": time.time(),
        },
    )
    try:
        return orchestrator.run()
    finally:
        log_shutdown("orchestrator stopped")


if __name__ == "__main__":
    raise SystemExit(main())

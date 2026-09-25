"""Orchestrator entry point.

Usage:
    python server/run.py examples/demo.yaml
    python server/run.py examples/demo.yaml --keep-going
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from rich.console import Console  # noqa: E402

from app.orchestrator import Orchestrator  # noqa: E402
from app.plan import PlanError, load_plan  # noqa: E402
from app.runs import RunStore, new_run_id  # noqa: E402


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
        "--keep-going",
        action="store_true",
        help="keep running remaining tasks after a failure",
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
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    console = Console()

    try:
        plan = load_plan(args.plan)
    except PlanError as exc:
        console.print(f"[red]invalid plan:[/] {exc}")
        return 2

    poll_interval = args.poll_interval
    if poll_interval is None:
        poll_interval = float(plan.defaults.get("poll_interval_s", 2.0))

    run_id = new_run_id()
    run_store = RunStore(Path(args.runs_dir).expanduser().resolve())
    console.print(f"[dim]run id: {run_id}[/]")

    orchestrator = Orchestrator(
        plan,
        history_dir=Path(args.history_dir).expanduser().resolve(),
        poll_interval=poll_interval,
        keep_going=args.keep_going,
        worker_wait_timeout=args.worker_wait,
        console=console,
        dry_run=args.dry_run,
        only=args.only,
        run_store=None if args.dry_run else run_store,
        run_id=run_id,
    )
    return orchestrator.run()


if __name__ == "__main__":
    raise SystemExit(main())

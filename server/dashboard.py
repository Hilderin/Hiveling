"""Dashboard entry point.

Usage:
    python server/dashboard.py --port 8080
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import uvicorn  # noqa: E402

from app.logging_setup import log_shutdown, log_startup, setup_logging  # noqa: E402
from app.web import DashboardConfig, RunManager, create_app  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="dashboard",
        description="Hiveling dashboard: runs, history and plans in a browser.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="listen interface (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080, help="listen port (default: 8080)")
    parser.add_argument(
        "--plans-dir",
        default=None,
        help="directory for plan files (default: <data-dir>/plans)",
    )
    parser.add_argument("--data-dir", default=".data", help="data directory")
    parser.add_argument(
        "--workers-file",
        default=None,
        help="workers configuration file (default: <data-dir>/workers.yaml)",
    )
    parser.add_argument("--poll-interval", type=float, default=2.0, help="worker poll interval (s)")
    parser.add_argument(
        "--worker-wait",
        type=float,
        default=1800.0,
        help="max time (s) to wait for a free worker",
    )
    parser.add_argument(
        "--log-dir",
        default=os.environ.get("HIVELING_LOG_DIR"),
        help="directory for the rotating server log (default: <data-dir>/logs)",
    )
    parser.add_argument(
        "--heartbeat",
        type=float,
        default=float(os.environ.get("HIVELING_HEARTBEAT", "60")),
        help="seconds between heartbeat log lines; 0 disables (default: 60)",
    )
    parser.add_argument(
        "--mcp-token",
        default=os.environ.get("HIVELING_TOKEN"),
        help="bearer token required on /mcp (default: $HIVELING_TOKEN; empty disables auth)",
    )
    parser.add_argument(
        "--mcp-allowed-host",
        action="append",
        default=None,
        help="extra Host allowed by the MCP DNS-rebinding guard (repeatable)",
    )
    parser.add_argument(
        "--mcp-allowed-origin",
        action="append",
        default=None,
        help="extra Origin allowed by the MCP guard (repeatable)",
    )
    parser.add_argument("--log-level", default="info", help="uvicorn log level")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    data_dir = Path(args.data_dir).expanduser().resolve()
    log_dir = Path(args.log_dir).expanduser().resolve() if args.log_dir else data_dir / "logs"
    log_path = setup_logging(log_dir, args.log_level)
    logger = logging.getLogger("hiveling.server")

    plans_dir = (
        Path(args.plans_dir).expanduser().resolve()
        if args.plans_dir
        else data_dir / "plans"
    )
    workers_file = (
        Path(args.workers_file).expanduser().resolve() if args.workers_file else None
    )
    config = DashboardConfig(
        data_dir=data_dir,
        plans_dir=plans_dir,
        poll_interval=args.poll_interval,
        worker_wait_timeout=args.worker_wait,
        workers_file=workers_file,
        heartbeat_s=args.heartbeat,
        mcp_token=args.mcp_token,
        mcp_allowed_hosts=args.mcp_allowed_host or [],
        mcp_allowed_origins=args.mcp_allowed_origin or [],
    )
    config.history_dir.mkdir(parents=True, exist_ok=True)
    config.runs_dir.mkdir(parents=True, exist_ok=True)

    log_startup(
        host=args.host,
        port=args.port,
        data_dir=data_dir,
        workers_file=config.workers_path,
        log_file=log_path,
        heartbeat_s=args.heartbeat,
    )

    try:
        manager = RunManager(config)
        app = create_app(config, manager)
        # log_config=None keeps our root logging config (uvicorn loggers
        # propagate to it) instead of uvicorn replacing it.
        uvicorn.run(
            app,
            host=args.host,
            port=args.port,
            log_level=args.log_level,
            log_config=None,
        )
    except BaseException:
        logger.exception("server crashed with an unhandled exception")
        raise
    finally:
        log_shutdown("server stopped")


if __name__ == "__main__":
    main()

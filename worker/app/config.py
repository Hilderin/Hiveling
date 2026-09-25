"""Worker configuration (CLI flags + environment variables)."""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Config:
    host: str
    port: int
    workspace: Path
    opencode_bin: str | None
    accept_timeout_s: float
    default_timeout_s: float
    log_level: str

    @classmethod
    def from_args(cls, argv: list[str] | None = None) -> "Config":
        parser = argparse.ArgumentParser(
            prog="worker",
            description="HTTP worker: runs `opencode run` on demand.",
        )
        parser.add_argument(
            "--host",
            default=os.environ.get("WORKER_HOST", "0.0.0.0"),
            help="listen interface (default: 0.0.0.0, reachable from the network)",
        )
        parser.add_argument(
            "--port",
            type=int,
            default=int(os.environ.get("WORKER_PORT", "8787")),
            help="listen port (default: 8787)",
        )
        parser.add_argument(
            "--workspace",
            default=os.environ.get("WORKER_WORKSPACE", ".data/worker"),
            help="worker workspace directory (default: ./.data/worker)",
        )
        parser.add_argument(
            "--opencode-bin",
            default=os.environ.get("OPENCODE_BIN"),
            help="explicit path to the opencode binary (default: search PATH)",
        )
        parser.add_argument(
            "--accept-timeout",
            type=float,
            default=float(os.environ.get("WORKER_ACCEPT_TIMEOUT", "120")),
            help="max delay (s) between job creation and start before it expires",
        )
        parser.add_argument(
            "--default-timeout",
            type=float,
            default=float(os.environ.get("WORKER_DEFAULT_TIMEOUT", "900")),
            help="default per-task timeout (s) when the task does not set one",
        )
        parser.add_argument(
            "--log-level",
            default=os.environ.get("WORKER_LOG_LEVEL", "info"),
            help="uvicorn log level (default: info)",
        )
        args = parser.parse_args(argv)

        return cls(
            host=args.host,
            port=args.port,
            workspace=Path(args.workspace).expanduser().resolve(),
            opencode_bin=args.opencode_bin,
            accept_timeout_s=args.accept_timeout,
            default_timeout_s=args.default_timeout,
            log_level=args.log_level,
        )

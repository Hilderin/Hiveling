"""Worker configuration (CLI flags + environment variables)."""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class Config:
    host: str
    port: int
    workspace: Path
    opencode_bin: str | None
    accept_timeout_s: float
    default_timeout_s: float
    log_level: str
    log_dir: Path
    heartbeat_s: float
    capabilities_file: Path | None = None
    secrets_file: Path | None = None
    opencode_dir: Path | None = None
    # Set by create_app: live Capabilities / SecretStore objects (hot-reloaded).
    capabilities: Any = None
    secrets: Any = None

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
            help="log level (default: info)",
        )
        parser.add_argument(
            "--log-dir",
            default=os.environ.get("WORKER_LOG_DIR", ".data/logs"),
            help="directory for the rotating worker log (default: ./.data/logs)",
        )
        parser.add_argument(
            "--heartbeat",
            type=float,
            default=float(os.environ.get("WORKER_HEARTBEAT", "60")),
            help="seconds between heartbeat log lines; 0 disables (default: 60)",
        )
        parser.add_argument(
            "--capabilities-file",
            default=os.environ.get("WORKER_CAPABILITIES", "capabilities.yaml"),
            help=(
                "worker capabilities file (tags, labels, providers, path_roots); "
                "default ./capabilities.yaml, hot-reloaded"
            ),
        )
        parser.add_argument(
            "--secrets-file",
            default=os.environ.get("WORKER_SECRETS", "secrets.yaml"),
            help=(
                "worker secret store (flat name: value mapping); default "
                "./secrets.yaml, hot-reloaded. The environment is the fallback."
            ),
        )
        parser.add_argument(
            "--opencode-dir",
            default=os.environ.get("WORKER_OPENCODE_DIR", "opencode"),
            help=(
                "baseline OpenCode bundle on the worker (opencode.json, agents/, "
                "skills/, AGENTS.md); default ./opencode"
            ),
        )
        args = parser.parse_args(argv)

        capabilities_file = (
            Path(args.capabilities_file).expanduser().resolve()
            if args.capabilities_file
            else None
        )
        secrets_file = (
            Path(args.secrets_file).expanduser().resolve()
            if args.secrets_file
            else None
        )
        opencode_dir = (
            Path(args.opencode_dir).expanduser().resolve()
            if args.opencode_dir
            else None
        )

        return cls(
            host=args.host,
            port=args.port,
            workspace=Path(args.workspace).expanduser().resolve(),
            opencode_bin=args.opencode_bin,
            accept_timeout_s=args.accept_timeout,
            default_timeout_s=args.default_timeout,
            log_level=args.log_level,
            log_dir=Path(args.log_dir).expanduser().resolve(),
            heartbeat_s=args.heartbeat,
            capabilities_file=capabilities_file,
            secrets_file=secrets_file,
            opencode_dir=opencode_dir,
        )

"""Prune old finished jobs from a worker workspace.

Usage:
    python worker/gc.py --workspace .data/worker --keep 20
    python worker/gc.py --workspace .data/worker --days 7
    python worker/gc.py --workspace .data/worker --keep 20 --days 7

Only terminal jobs (succeeded/failed/canceled) are removed; running/accepted
jobs are never touched. With no ``--keep``/``--days`` nothing is removed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.gc import prune  # noqa: E402
from app.logging_setup import setup_logging  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="worker-gc", description=__doc__)
    parser.add_argument("--workspace", default=".data/worker", help="worker workspace")
    parser.add_argument("--keep", type=int, default=0, help="keep the newest N finished jobs")
    parser.add_argument("--days", type=float, default=0.0, help="also remove jobs older than N days")
    parser.add_argument("--log-dir", default=".data/logs")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args(argv)

    setup_logging(Path(args.log_dir).expanduser(), args.log_level)
    result = prune(
        Path(args.workspace).expanduser().resolve(),
        keep=args.keep,
        older_than_days=args.days,
    )
    print(f"removed={result['removed']} kept={result['kept']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

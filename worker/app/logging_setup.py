"""Worker logging: a rotating file under ``.data/`` plus stderr.

The goal is to leave a durable trace on disk so that an *abrupt* termination is
distinguishable from a Python exception or a graceful shutdown:

* startup logs a single line with pid/host/port/workspace/opencode binary;
* a periodic heartbeat shows the last time the process was known alive;
* uncaught exceptions (main thread **and** worker threads) are logged with a
  traceback;
* a graceful stop logs an explicit ``worker stopped`` line.

So, after the fact:

* log ends with a traceback -> the worker crashed in Python;
* log ends with ``worker stopped`` -> clean shutdown (signal/in-process exit);
* log ends with a heartbeat but no stop line -> the process was killed
  abruptly (e.g. ``taskkill /F`` or the parent session reaping its children),
  which is precisely the case that leaves nothing behind today.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import threading
from pathlib import Path

LOG_FILENAME = "worker.log"
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 5
FORMAT = "%(asctime)s %(levelname)-8s [pid %(process)d %(threadName)s] %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_LEVELS = {
    "critical": logging.CRITICAL,
    "error": logging.ERROR,
    "warning": logging.WARNING,
    "warn": logging.WARNING,
    "info": logging.INFO,
    "debug": logging.DEBUG,
}


def _level(value: str) -> int:
    return _LEVELS.get(str(value).lower(), logging.INFO)


def setup_logging(log_dir: Path, level: str = "info", *, console: bool = True) -> Path:
    """Configure root logging and return the log file path.

    Every module uses the standard ``logging`` module, so configuring the root
    logger here also captures ``uvicorn`` logs (we pass ``log_config=None`` to
    uvicorn so it does not overwrite this setup).
    """
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / LOG_FILENAME

    root = logging.getLogger()
    root.setLevel(_level(level))
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(FORMAT, DATE_FORMAT)

    file_handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    if console:
        try:
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # pragma: no cover - non-TextIO stream
            pass
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(formatter)
        root.addHandler(stream_handler)

    _install_excepthooks()
    return log_path


def _install_excepthooks() -> None:
    """Log uncaught exceptions instead of losing them on a dead console."""
    logger = logging.getLogger("hiveling.worker")

    def _main_hook(exc_type, exc_value, exc_tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logger.critical("uncaught exception", exc_info=(exc_type, exc_value, exc_tb))

    def _thread_hook(args: threading.ExceptHookArgs) -> None:
        thread_name = args.thread.name if args.thread is not None else "?"
        logger.critical(
            "uncaught exception in thread %s",
            thread_name,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = _main_hook
    threading.excepthook = _thread_hook


def log_startup(**fields: object) -> None:
    """Log a single startup line (used to detect abrupt terminations)."""
    details = " ".join(f"{key}={value}" for key, value in fields.items())
    logging.getLogger("hiveling.worker").info(
        "worker starting [pid=%d] %s", os.getpid(), details
    )


def log_shutdown(reason: str = "worker stopped") -> None:
    logging.getLogger("hiveling.worker").info("%s [pid=%d]", reason, os.getpid())

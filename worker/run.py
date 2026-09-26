"""Worker entry point.

Usage:
    python worker/run.py --port 8787
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# Allow `from app...` imports when the script is run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import uvicorn  # noqa: E402

from app.config import Config  # noqa: E402
from app.logging_setup import log_shutdown, log_startup, setup_logging  # noqa: E402
from app.main import create_app  # noqa: E402


def main() -> None:
    config = Config.from_args()
    log_path = setup_logging(config.log_dir, config.log_level)
    logger = logging.getLogger("hiveling.worker")

    config.workspace.mkdir(parents=True, exist_ok=True)
    log_startup(
        host=config.host,
        port=config.port,
        workspace=config.workspace,
        log_file=log_path,
        heartbeat_s=config.heartbeat_s,
    )

    try:
        app = create_app(config)
        # log_config=None keeps our root logging config (uvicorn loggers
        # propagate to it) instead of uvicorn replacing it.
        uvicorn.run(
            app,
            host=config.host,
            port=config.port,
            log_level=config.log_level,
            log_config=None,
        )
    except BaseException:
        logger.exception("worker crashed with an unhandled exception")
        raise
    finally:
        log_shutdown("worker stopped")


if __name__ == "__main__":
    main()

"""Worker entry point.

Usage:
    python worker/run.py --port 8787
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow `from app...` imports when the script is run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import uvicorn  # noqa: E402

from app.config import Config  # noqa: E402
from app.main import create_app  # noqa: E402


def main() -> None:
    config = Config.from_args()
    config.workspace.mkdir(parents=True, exist_ok=True)
    app = create_app(config)
    uvicorn.run(app, host=config.host, port=config.port, log_level=config.log_level)


if __name__ == "__main__":
    main()

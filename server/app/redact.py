"""Strip machine-local paths and worker endpoints from server responses.

Clients and LLMs only need logical identifiers (run id, task id, plan name,
worker name). *Where* the server and its workers keep their files on disk is an
implementation detail: exposing it invites a client to read those directories
directly instead of going through the API/MCP (and to probe hosts it has no
business knowing).

The raw values are still written to ``run.json`` and the history on disk, since
recovery and debugging need them. They are only removed here, at the response
boundary, so every JSON API route and MCP tool returns the same sanitized view.

Two layers:

- :class:`Redactor` walks a JSON-like value and drops the keys holding a path or
  a worker endpoint (``data_dir``, ``plan_path``, ``path_roots``, ``url``, …),
  recursing into nested dicts and lists.
- it also rewrites occurrences of the server's own roots inside strings, so a
  path that leaked into an error message (e.g. ``plan not found: /…``) is
  neutralized too.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

# Keys whose value is a location on the server or on a worker, or a worker
# endpoint/host identity. They carry no information a client needs: a plan is
# addressed by name, a worker by name, a task by id.
_PRIVATE_KEYS = frozenset(
    {
        # Server-side locations.
        "data_dir",
        "plans_dir",
        "workers_file",
        "plan_path",
        "plan_snapshot",
        "base_dir",
        "history_rel",
        "run_dir",
        "source_path",
        # Worker-side locations.
        "path_roots",
        "workspace",
        "workdir",
        "job_dir",
        "log_dir",
        "opencode_dir",
        "opencode_bin",
        "path",
        # Worker-local OpenCode source paths, stored in a task's request.
        "from",
        "agents_paths",
        "skills_paths",
        "agents_md",
        "sources",
        # Worker endpoints and server host identity.
        "url",
        "worker_url",
        "base_url",
        "owner",
    }
)

# The repository the server code runs from. Text produced by a local worker can
# mention a path under it (the workspace lives in ``.data/worker``), so scrub the
# checkout root as well as the configured data roots.
_REPO_ROOT = Path(__file__).resolve().parents[2]


class Redactor:
    """Remove private keys from structured responses and scrub server roots."""

    def __init__(self, roots: Iterable[tuple[str, str]] = ()):
        # Longest prefix first so a nested root (plans under data) wins over its
        # parent and gets the more specific placeholder.
        self._roots = sorted(
            ((str(Path(prefix)), placeholder) for prefix, placeholder in roots if prefix),
            key=lambda item: len(item[0]),
            reverse=True,
        )

    def text(self, value: str) -> str:
        for prefix, placeholder in self._roots:
            if prefix in value:
                value = value.replace(prefix, placeholder)
        return value

    def __call__(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {key: self(item) for key, item in value.items() if key not in _PRIVATE_KEYS}
        if isinstance(value, list):
            return [self(item) for item in value]
        if isinstance(value, str):
            return self.text(value)
        return value


def make_redactor(config) -> Redactor:
    """Build the redactor for a ``DashboardConfig`` (or any object with the same fields)."""
    roots = [
        (str(Path(config.data_dir).expanduser().resolve()), "<data-dir>"),
        (str(Path(config.plans_dir).expanduser().resolve()), "<plans-dir>"),
        (str(Path(config.workers_path).expanduser().resolve()), "<workers-file>"),
        (str(_REPO_ROOT), "<hiveling>"),
    ]
    return Redactor(roots)

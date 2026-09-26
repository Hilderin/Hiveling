"""The plan.yaml contract, shared with the MCP client.

``PlanInput`` and friends mirror what :mod:`app.plan` accepts, so an MCP tool
whose parameter is a ``PlanInput`` publishes the whole nested structure in
``tools/list`` (JSON Schema). ``PLAN_REFERENCE`` / ``PLAN_EXAMPLE`` are exposed
as an MCP tool result and as resources for clients that need prose instead of a
schema.
"""

from __future__ import annotations

from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

DownloadMode = Literal["modified", "all", "none"]


class ResourceInput(BaseModel):
    """One resource a task needs materialized by a worker provider."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: str = Field(
        description="Provider type (ephemeral today; git, path, env, secret, command later)."
    )
    id: str | None = Field(
        default=None,
        description="Stable id within the task (defaults to <type>-<index>); resources merge by id across defaults and task.",
    )
    when: dict | None = Field(
        default=None, description="Optional worker-capability guard (os/tags/providers/labels)."
    )
    options: dict = Field(
        default_factory=dict,
        alias="with",
        description="Provider-specific options (e.g. git repo/ref/branch).",
    )


class ArtifactsInput(BaseModel):
    """How a task's results are collected."""

    model_config = ConfigDict(extra="forbid")

    download: DownloadMode | None = Field(
        default=None, description="zip channel: modified (default) | all | none."
    )
    git: bool | None = Field(
        default=None, description="Include commits/published refs in the task result."
    )
    paths: list[str] | None = Field(
        default=None, description="Extra globs collected beyond the workdir diff."
    )


class RequirementsInput(BaseModel):
    """Worker capabilities a task needs. Merged over defaults; matched as a subset."""

    model_config = ConfigDict(extra="forbid")

    os: str | None = Field(
        default=None, description="Exact OS match: windows | linux | macos."
    )
    tags: list[str] | None = Field(
        default=None, description="Worker tags that must all be present."
    )
    providers: list[str] | None = Field(
        default=None, description="Worker providers that must all be available."
    )
    labels: dict[str, str] | None = Field(
        default=None, description="Worker labels that must match exactly."
    )


class DefaultsInput(BaseModel):
    """Applied to every task unless the task overrides it."""

    model_config = ConfigDict(extra="forbid")

    model: str | None = Field(default=None, description="Model id, e.g. provider/model or provider/model#variant.")
    agent: str | None = Field(default=None, description="OpenCode agent, e.g. 'build'.")
    auto: bool | None = Field(default=None, description="Pass --auto to opencode (default true).")
    timeout_s: float | None = Field(default=None, description="Per-task timeout in seconds.")
    variant: str | None = Field(default=None, description="Model variant (reasoning effort).")
    download: DownloadMode | None = Field(default=None, description="modified (default) | all | none.")
    env: dict[str, str] | None = Field(default=None, description="Extra environment variables.")
    files: list[str] | None = Field(default=None, description="Input paths/globs relative to the plan.")
    requirements: RequirementsInput | None = Field(
        default=None, description="Worker capabilities every task needs (merged with task-level requirements)."
    )
    resources: list[ResourceInput] | None = Field(
        default=None, description="Resources every task materializes (merged with task resources by id)."
    )
    artifacts: ArtifactsInput | None = Field(
        default=None, description="How every task's results are collected."
    )
    max_parallel: int | None = Field(
        default=None,
        description="Max tasks running at once; 0/unset means one per free worker.",
    )


class TaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Unique task id.")
    prompt: str | None = Field(default=None, description="Prompt text (or use prompt_file).")
    prompt_file: str | None = Field(
        default=None, description="Path to a prompt file relative to the plan (or use prompt)."
    )
    files: list[str] | None = Field(
        default=None, description="Input paths/globs relative to the plan, sent to the worker."
    )
    model: str | None = Field(default=None, description="Override the default model.")
    agent: str | None = Field(default=None, description="Override the default agent.")
    auto: bool | None = Field(default=None, description="Override the default auto flag.")
    timeout_s: float | None = Field(default=None, description="Override the default timeout.")
    variant: str | None = Field(default=None, description="Model variant (reasoning effort).")
    title: str | None = Field(default=None, description="Human-readable task title (defaults to id).")
    env: dict[str, str] | None = Field(default=None, description="Extra environment variables.")
    depends_on: list[str] | None = Field(
        default=None, description="Task ids that must run first; if one fails this task is skipped."
    )
    inputs_from: list[str] | None = Field(
        default=None,
        description="Task ids whose downloaded files are copied into this task's working directory (also implies ordering).",
    )
    download: DownloadMode | None = Field(default=None, description="modified | all | none.")
    requirements: RequirementsInput | None = Field(
        default=None,
        description="Worker capabilities this task needs (merged over defaults); the task only runs on a matching worker.",
    )
    resources: list[ResourceInput] | None = Field(
        default=None, description="Resources this task materializes (merged with defaults by id)."
    )
    artifacts: ArtifactsInput | None = Field(
        default=None, description="How this task's results are collected."
    )

    @model_validator(mode="after")
    def _require_prompt(self) -> "TaskInput":
        if not (self.prompt and self.prompt.strip()) and not self.prompt_file:
            raise ValueError("provide 'prompt' or 'prompt_file'")
        return self


class PlanInput(BaseModel):
    """A complete plan.yaml, version 1.

    Workers are not part of a plan: they are configured once in workers.yaml,
    managed by the server and visible through the list_workers tool.
    """

    model_config = ConfigDict(extra="forbid")

    version: int = Field(default=1, description="Plan format version (currently 1).")
    defaults: DefaultsInput | None = Field(default=None, description="Defaults applied to every task.")
    tasks: list[TaskInput] = Field(description="Tasks to run, in dependency order (required).")


def plan_to_dict(plan: PlanInput) -> dict:
    """Drop unset fields so the YAML stays clean."""
    return plan.model_dump(exclude_none=True, exclude_defaults=False, by_alias=True)


def plan_to_yaml(plan: PlanInput) -> str:
    return yaml.safe_dump(plan_to_dict(plan), sort_keys=False, allow_unicode=True)


PLAN_REFERENCE = """\
# plan.yaml (version 1)

Workers are NOT declared in a plan. They are configured once, server-side, in
workers.yaml (default `<data-dir>/workers.yaml`) and listed by the list_workers
tool.

Top-level keys:

- `version` (int, default 1)
- `defaults` (optional mapping), applied to every task unless overridden:
  - `model`, `agent`, `auto` (bool), `timeout_s` (number), `variant`,
    `download` (`modified` | `all` | `none`), `env` (map), `files` (list)
  - `requirements` (mapping): worker capabilities every task needs
  - `resources` (list): resources every task materializes (merged with task
    resources by `id`)
  - `artifacts` (mapping): how every task's results are collected
  - `max_parallel` (int): max tasks running at once; 0/unset means one per free
    worker
- `tasks` (required, list). Each task:
  - `id` (required, unique)
  - `prompt` (string) or `prompt_file` (path relative to the plan): required
  - `files` (paths/globs relative to the plan), sent to the worker before it runs
  - `model`, `agent`, `auto`, `timeout_s`, `variant`, `title`
  - `env` (map of extra environment variables for the subprocess)
  - `download` (`modified` | `all` | `none`)
  - `depends_on` (task ids): ordering; the task is skipped if a dependency fails
  - `inputs_from` (task ids): ordering plus copies those tasks' downloaded files
    into this task's working directory
  - `requirements` (mapping, merged over defaults): only dispatch the task to a
    matching worker. Keys: `os` (exact: `windows`|`linux`|`macos`), `tags`
    (all required), `providers` (all required), `labels` (exact per key). With
    no requirements the task runs on any free worker. Use list_workers to see
    the capabilities each worker advertises.
  - `resources` (list, merged over defaults by `id`): each entry is
    `{type, id?, when?, with?}`. `type` selects a worker provider; `with` holds
    provider-specific options. Resources are validated and prepared on the
    worker *before* OpenCode starts. Today the only provider is `ephemeral`
    (a fresh, isolated working directory: the historical behavior).
  - `artifacts` (mapping): `download` (`modified`|`all`|`none`) selects the zip
    channel; `git` and `paths` are reserved for later stages.

Resource notes:

- Resources merge by `id` across `defaults.resources` and the task: a same-`id`
  task resource overrides the default one, new ids append. Without `id`, a
  resource gets a generated id and simply accumulates.
- A resource whose `type` the worker does not implement fails the task at
  prepare, before OpenCode runs, with a clear error.

Notes:

- `depends_on` and `inputs_from` both imply ordering. Use `inputs_from` to pass
  files from one task to the next; working directories are otherwise independent.
- `requirements` are matched as a subset: every requested tag/provider/label
  must be advertised by the worker. A task whose requirements cannot be met by
  any reachable worker fails fast instead of waiting for a worker.
- Tasks are topologically sorted by the server; independent tasks run in
  parallel, one per free worker.
- `download: modified` (default) downloads files the task changed/added,
  `all` downloads the whole working directory, `none` downloads nothing.
- A run stops at the first failed task (fail fast): no new task is started, but
  the tasks already running are allowed to finish before the run is finalized.
  `resume_run` re-arms it and re-runs the failed/skipped tasks after an
  adjustment.
"""

PLAN_EXAMPLE = """\
version: 1

defaults:
  model: opencode-go/deepseek-v4.1-flash
  agent: build
  auto: true
  timeout_s: 600

tasks:
  - id: hello
    prompt: "Reply with exactly: PONG"
    download: none

  - id: on-windows
    prompt: "Reply with the result of: echo %OS%"
    requirements:
      os: windows
    download: none

  - id: write-report
    prompt: >
      Create a file named report.txt with three lines: one, two, three.
    depends_on: [hello]

  - id: summarize
    prompt: "Read report.txt and summarize it in a single sentence."
    inputs_from: [write-report]
"""

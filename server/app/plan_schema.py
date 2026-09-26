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


class OpencodeSourceInput(BaseModel):
    """A worker-local OpenCode config source (verbose form)."""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(description="bundle | config | agents | skills | agents_md.")
    path: str = Field(description="Worker-local path (resolved under path_roots).")
    mode: str | None = Field(default=None, description="symlink | reference | concat | copy (default auto).")
    priority: int | None = Field(default=None, description="Higher wins / concatenates later.")
    optional: bool | None = Field(default=None, description="Skip a missing path instead of failing.")
    when: dict | None = Field(default=None, description="Capability guard (reserved).")


class OpencodeInput(BaseModel):
    """Per-job OpenCode runtime config: inline content and worker-local paths."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    config: dict | None = Field(default=None, description="Inline opencode.json fragment (deep-merged).")
    agents: dict[str, str] | None = Field(default=None, description="Inline agents: name -> markdown body.")
    skills: list[dict] | None = Field(default=None, description="Inline skills: [{name, content}].")
    from_: list[str] | None = Field(default=None, alias="from", description="Bundle dirs (opencode.json + agents/ + skills/ + AGENTS.md).")
    agents_paths: list[str] | None = Field(default=None, description="Worker-local agent directories.")
    skills_paths: list[str] | None = Field(default=None, description="Worker-local skill directories.")
    agents_md: list[str] | None = Field(default=None, description="Worker-local AGENTS.md files (concatenated).")
    sources: list[OpencodeSourceInput] | None = Field(default=None, description="Verbose source list.")


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
    opencode: OpencodeInput | None = Field(
        default=None, description="OpenCode runtime config injected for every task (merged with task opencode)."
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
    opencode: OpencodeInput | None = Field(
        default=None, description="OpenCode runtime config injected for this task (merged with defaults)."
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
  - `model`, `agent`, `timeout_s` (number), `variant`,
    `download` (`modified` | `all` | `none`), `env` (map), `files` (list)
  - `requirements` (mapping): worker capabilities every task needs
  - `resources` (list): resources every task materializes (merged with task
    resources by `id`)
  - `artifacts` (mapping): how every task's results are collected
  - `opencode` (mapping): OpenCode runtime config injected for every task
  - `max_parallel` (int): max tasks running at once; 0/unset means one per free
    worker
- `tasks` (required, list). Each task:
  - `id` (required, unique)
  - `prompt` (string) or `prompt_file` (path relative to the plan): required
  - `files` (paths/globs relative to the plan), sent to the worker before it runs
  - `model`, `agent`, `timeout_s`, `variant`, `title`
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
    worker *before* OpenCode starts.
  - `artifacts` (mapping): `download` (`modified`|`all`|`none`) selects the zip
    channel; `paths` (globs, relative to the working directory) adds extra files
    to the zip beyond the workdir diff; `git: true` keeps the commits/branches a
    `git` resource published in the task result (also returned by get_task).
  - `opencode` (mapping, merged over defaults): OpenCode runtime config injected
    at the job's working directory. Either inline (`config`, `agents`,
    `skills`) or **worker-local paths** (`from` bundles, `agents_paths`,
    `skills_paths`, `agents_md`, or the verbose `sources` list), so the plan
    carries paths instead of re-writing agent/skill bodies.

Built-in providers:

- `ephemeral` — a fresh, isolated working directory (the historical behavior).
- `env` — inject environment variables: `{vars: {NAME: value}}` (values may use
  `${OTHER}`).
- `secret` — inject a named secret from the worker store (`{name, as?, required?}`).
  The plan never carries the value.
- `git` — `{repo, path, worktree?, ref?, branch?, branch_mode?, merge?,
  push_to?, set_upstream?, force?, clean?, cache?, publish?, remote?}`. `ref` is
  the start point, `branch` the target the task commits to; `publish` is
  `none|commit|push`. `merge` is a list of refs merged into `branch` during
  prepare; a conflict is resolved by the worker itself (Opencode), not failed.
- `path` — expose an existing folder: `{path, mode: ro|rw, visible?}`.

Git layout (matters for prompts):

- OpenCode runs from the **job workspace root**.
- `git` with `worktree: true` checks out the task's **working tree at
  `<workspace>/src/<resource id>`** and commits/pushes *that* tree. `path` is
  the durable clone (staging), not the checkout: use an absolute path under
  `path_roots`, or a relative label (the worker keeps a relative clone out of
  the workspace so only `src/<id>` is visible).
- `git` with `worktree: false` (default) checks out in place at `path`; with a
  relative `path` that is `<workspace>/<path>`.
- The worker **prepends the working directories to every prompt** (a "[Working
  environment]" block), but write prompts against the tree anyway: say "the
  repository is at `./src/<id>`" (worktree) or "`./<path>`" (canonical), not
  "the workspace root". Point `opencode.agents_paths` / `skills_paths` /
  `agents_md` at the checkout too (`./src/<id>/.opencode/...`). Getting this
  wrong is silent: the task edits a tree the provider never commits and
  `publish: push` reports nothing pushed.

Resource notes:

- Resources merge by `id` across `defaults.resources` and the task: a same-`id`
  task resource overrides the default one, new ids append. Without `id`, a
  resource gets a generated id and simply accumulates.
- A resource whose `type` the worker does not implement fails the task at
  prepare, before OpenCode runs, with a clear error.
- Absolute paths (`git.path`, `path.path`, `opencode` sources) must live under
  the worker's `path_roots`; relative paths resolve under the job workspace
  (for `git` with `worktree: true`, a relative `path` is only a clone label and
  is staged outside the workspace — see the Git layout notes above).
- `{run}` and `{task}` in resource option strings are resolved by the server
  (task-local only). Cross-task references do not exist: to consume another
  task's branch, declare the repo with `ref: "hiveling/{run}/<producer>"` and
  list it in `depends_on`.

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

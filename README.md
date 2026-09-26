# Hiveling

A minimal orchestrator + worker setup that runs a `plan.yaml` on remote
machines. The worker is a Python HTTP server that receives a task (prompt,
model, input files) and runs `opencode run` in a subprocess. File exchange
between the server and the worker happens over HTTP using zip archives.

```
                   workers.yaml (hot-reloaded)
                        │
 plan.yaml ──▶  server (orchestrator)  ──HTTP──▶  worker(s)  ──CLI──▶  opencode run
                        │                                   │
                        └─── .data/history/<task>/<run>/ ◀──┘ (zip of changed files)
```

V1 characteristics:

- Tasks form a DAG (via `depends_on` / `inputs_from`) and are executed
  **concurrently**: every task whose dependencies succeeded is dispatched to a
  free worker, one task per worker at a time.
- Fail fast: a failed task stops new dispatches, but the tasks already running
  are allowed to finish before the run is finalized (and `wait_for_run`
  returns).
- Optional **gates**: a read-only checkpoint that judges the tasks it analyses
  and, when they are not `VALID`, resets and re-runs them (bounded), so a review
  actually gates the next work item. See [Gates](#gates-bounded-rework).
- Workers are configured once in `.data/workers.yaml`; the list is reloaded
  automatically when the file changes, so adding a worker needs no restart.
- Tasks can declare **requirements** (matched against worker capabilities) and
  **resources** materialized by the worker before OpenCode runs (git repos and
  worktrees, env vars, secrets, folders, shell commands, OpenCode config/agents/
  skills). See [Resources (environment)](#resources-environment).
- No authentication on the JSON API/dashboard; the MCP endpoint supports an
  optional bearer token.
- An MCP (Streamable HTTP) endpoint lets a local OpenCode start and monitor
  runs with typed tools (see [MCP server](#mcp-server)).
- Runs are durable: each run snapshots its plan and records per-task state on
  disk, so a restarted server **recovers in-flight runs** (reattaching to worker
  jobs that are still running) instead of losing them.
- A run's plan can be **edited while it runs**; changes apply to pending tasks
  between steps.
- No live streaming: the server polls the worker every few seconds.
- Everything is stored on disk under `.data/` (server history, logs and worker
  workspace).

## Layout

```
server/            orchestrator (CLI) + web dashboard
worker/            HTTP worker (listens on a port, runs opencode)
examples/          example plans
.data/             working data (gitignored)
  plans/           plan files used by runs (including pushed plans)
  workers.yaml     workers the server dispatches to (hot-reloaded)
  history/         per-task artifacts
  runs/            per-run state shown in the dashboard
    <run_id>/      run.json, plan.yaml (snapshot), owner lease
  logs/            rotating server.log and worker.log
  worker/          worker workspace (jobs)
  logs/            worker log (worker.log, rotated)
```

## Install

Each app has its own `requirements.txt`. For local development both can share a
single virtualenv:

```bash
python3 -m venv .venv
./.venv/bin/pip install -r server/requirements.txt -r worker/requirements.txt
```

On the worker machine, install only the worker requirements:

```bash
pip install -r worker/requirements.txt
```

The worker also needs the `opencode` CLI installed and authenticated for the
user that runs the worker (OpenCode auth is per user). If `opencode` is not in
`PATH`, pass `--opencode-bin` or set `OPENCODE_BIN`.

## Run

Workers are declared on the server side in `.data/workers.yaml` (default;
override with `--workers-file`). Each entry is a URL string or a
`host` + `port` mapping; the file is reloaded automatically when it changes.

```bash
cp examples/workers.yaml .data/workers.yaml   # then edit the worker list
```

Terminal 1 — worker:

```bash
./.venv/bin/python worker/run.py --port 8787
```

Terminal 2 — orchestrator (one-shot):

```bash
./.venv/bin/python server/run.py examples/demo.yaml
```

Terminal 2 (alternative) — dashboard (browser monitoring UI):

```bash
./.venv/bin/python server/dashboard.py --port 8080
# open http://127.0.0.1:8080
```

Start a run by pushing a plan to the dashboard:

```bash
# existing plan file
curl -X POST localhost:8080/api/runs -H 'Content-Type: application/json' \
  -d '{"plan":"demo.yaml"}'

# push a plan body (raw YAML)
curl -X POST localhost:8080/api/runs -H 'Content-Type: application/x-yaml' \
  --data-binary @examples/demo.yaml
```

Useful flags:

```bash
# show the plan order without executing it
./.venv/bin/python server/run.py examples/demo.yaml --dry-run

# run a single task
./.venv/bin/python server/run.py examples/demo.yaml --only hello
```

## Dashboard

`server/dashboard.py` serves a single-page UI (no build step) plus a JSON API.
The layout:

- left sidebar: **Workers** (reachable/busy state of every worker from
  `workers.yaml`; workers added while the server runs appear without a restart)
  and **Active runs** (compact list of runs still
  running, with `done/total` tasks);
- **History** link: full list of runs, newest first, with a search box (plan
  name or run id) and pagination;
- run view: task tree with each task's state (`pending`/`running`/`succeeded`/
  `failed`/`skipped`/`canceled`);
- task detail: worker, job id, model, duration, tokens, cost, session, error,
  changed files (download as zip), result, tool calls, prompt, events, stderr;
- actions: cancel a running run, cancel a single task, retry a task, view/edit
  the run's plan (plans under `.data/plans`, i.e. pushed plans, are editable;
  example plans are read-only from the dashboard).

Runs are started by **pushing a plan over HTTP** (there is no run form in the
UI). Runs execute in background threads; if the dashboard restarts while a run
is active, that run is **recovered** on the next startup (see
[Persistence, recovery and live edits](#persistence-recovery-and-live-edits)).

Deep links: `?run=<run_id>`, `?run=<run_id>&task=<task_id>`, `?view=history`.

API:

- `POST /api/runs` — start a run. JSON `{"plan": "demo.yaml"}` for an existing
  plan, or `{"plan_yaml": "<content>", "name": "optional.yaml", "only": [...]}`;
  a raw YAML body (`Content-Type: application/x-yaml`) is also accepted. Pushed
  plans are stored in `.data/plans/`, which is also the dashboard's default plan
  directory (`--plans-dir`). Example plans live in `examples/` and are meant for
  the CLI or as starting points.
- `GET /api/runs?status=active|all&q=<search>&limit=&offset=` — paginated runs.
- `GET /api/runs/{id}`, `POST /api/runs/{id}/cancel`,
  `POST /api/runs/{id}/resume`,
  `GET /api/runs/{id}/wait?timeout_s=&until=` (long-poll run state),
  `POST /api/runs/{id}/tasks/{task}/cancel`,
  `GET /api/runs/{id}/tasks/{task}`,
  `GET /api/runs/{id}/tasks/{task}/files`,
  `GET|PUT /api/runs/{id}/plan`, `GET /api/workers`.

## MCP server

`server/dashboard.py` also serves a **Model Context Protocol** endpoint over
Streamable HTTP, so an OpenCode client can drive Hiveling with typed tools
instead of `curl`. It is the same process and the same port as the dashboard,
so the server must be running:

```
OpenCode ──MCP/HTTP──▶ http://127.0.0.1:8080/mcp ──▶ RunManager / RunStore
```

| Tool | Description |
| --- | --- |
| `list_plans` | available plans (relative name, workers, task ids) |
| `get_plan` / `update_plan` | read / replace a stored plan's YAML (validated before writing) |
| `get_run_plan` / `update_run_plan` | read / live-edit the plan a run is executing |
| `get_plan_schema` | full `plan.yaml` reference plus a canonical example |
| `create_plan` | create a plan from a **structured** plan object (typed input schema) |
| `run_plan` | start a run from a plan name or inline YAML; returns `run_id` immediately |
| `wait_for_run` | block until a run finishes (`terminal`, waiting for any task still in flight) or changes; returns the state and the wake reason |
| `list_runs` | paginated runs with search and status filter |
| `get_run` | run status plus every task state (poll this) |
| `get_task` | one task's status, result, error and changed files |
| `cancel_run` | cancel a whole run (running task + all pending tasks) |
| `cancel_task` | cancel one task: stop it if running, or skip it if pending; the run continues |
| `resume_run` | re-run the failed/skipped tasks of a finished run and continue it |
| `list_workers` | reachable/busy state of the workers from `workers.yaml` |

`update_run_plan` edits a run's plan live (see
[Persistence, recovery and live edits](#persistence-recovery-and-live-edits));
`update_plan` edits a stored plan for future runs.

### Agentic loop

A run is **fail fast**: a task failure stops the dispatching of new tasks and
every task that had not run yet is marked `skipped` with
`skip_reason: run_stopped`. Tasks that were already running are **not** killed:
they run to completion before the run is finalized, so `wait_for_run` never
returns while work is still in flight. This gives an orchestrator a clean,
explicit decision point instead of a half-finished plan. `resume_run` re-arms a
finished run: its `failed` and `skipped` tasks (still in the plan) are reset to
`pending` — the previous error is kept in `last_error` and
`status.json`/`result.txt` are archived under
`history/<task>/<run>/attempt-N/` — and the run continues from where it stopped.

A minimal orchestrator loop over MCP:

```
run_plan(plan) -> run_id
while True:
    ev = wait_for_run(run_id, until="terminal")
    if ev.status == "succeeded": break
    reason = get_task(run_id, worst_task)       # ev.run.tasks already tells you which
    if decision == "adjust": update_run_plan(run_id, new_yaml)
    if decision == "retry":  resume_run(run_id)
    if decision == "abort":  cancel_run(run_id); break
```

`wait_for_run` bounds its wait with `timeout_s` and returns the current state
with `timed_out=true` on expiry, so the loop stays responsive.

### Teaching the plan format

An LLM has no access to this README, so the format is exposed **through MCP** on
three channels:

- `create_plan(name, plan, overwrite?)` takes a typed `plan` object. Its JSON
  Schema (defaults, tasks, `depends_on`, `inputs_from`, `download` enum, …) is
  published in `tools/list`, so the model sees every field, type and required
  marker and is validated before the YAML is written.
- `get_plan_schema()` returns the full reference and a canonical example;
- resources: `hiveling://schema/plan`, `hiveling://example/plan`, and
  `hiveling://plans/{name}` for each stored plan, readable through the client's
  resource tools.
- `list_workers()` reflects `workers.yaml` (which the server reloads
  automatically), so the model can verify its workers instead of guessing.

The server `instructions` also tell the model to call `get_plan_schema` before
authoring a plan. `update_plan` remains available for raw YAML.

`run_plan` is non-blocking: it starts the run and returns a `run_id`
immediately. `wait_for_run` blocks until it finishes (or changes), which is how
an agent waits instead of polling `get_run`/`get_task`. Cancellation is per task
or per run: `cancel_task` stops the task's worker job (or skips a pending task)
and the run continues — dependent tasks are skipped — while `cancel_run` stops
everything. `update_run_plan` edits a run's plan live; `update_plan` edits a
stored plan for future runs.

### Configure OpenCode

OpenCode V2 supports remote MCP servers natively. Add the server to your
project config, or globally with `--global`:

```bash
opencode mcp add hiveling --global --url http://127.0.0.1:8080/mcp
```

The CLI does not write the `oauth` field, and OpenCode enables OAuth by default
for remote servers (Hiveling has no OAuth flow), so edit the file and set
`oauth: false`. Minimal config:

```jsonc title="~/.config/opencode/opencode.jsonc"
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "servers": {
      "hiveling": {
        "type": "remote",
        "url": "http://127.0.0.1:8080/mcp",
        "oauth": false
      }
    }
  }
}
```

No bearer token is needed for local use. The dashboard binds to `127.0.0.1` by
default and the MCP transport only accepts requests addressed to localhost
(DNS-rebinding protection), so a web page cannot reach it. A token stored in
the config would not add much against a local process either.

Check the connection with `opencode mcp list` (expect `✓ hiveling connected`)
or `/mcps` inside OpenCode. If it reports `failed`, start the Hiveling server:
the MCP endpoint only exists while the dashboard is running. With the default
Code Mode the tools are grouped as `tools.hiveling.<tool>`.

### Exposing it on the network

If you bind to another interface (`--host 0.0.0.0`) or put the server behind a
proxy, add a bearer token, because anyone reaching it could start jobs:

```bash
HIVELING_TOKEN=choose-a-secret \
  ./.venv/bin/python3 server/dashboard.py --host 0.0.0.0 --port 8080
```

```jsonc
{
  "mcp": {
    "servers": {
      "hiveling": {
        "type": "remote",
        "url": "http://127.0.0.1:8080/mcp",
        "oauth": false,
        "headers": { "Authorization": "Bearer {env:HIVELING_TOKEN}" }
      }
    }
  }
}
```

Related dashboard flags:

- `--workers-file`: workers configuration file (default `<data-dir>/workers.yaml`).
- `--history-retention-runs` / `--history-retention-days`: prune old finished
  runs and their history on startup (off by default; see
  [Garbage collection](#garbage-collection)).
- `--mcp-token` (default `$HIVELING_TOKEN`): bearer token required on `/mcp`;
  empty disables authentication.
- `--mcp-allowed-host` (repeatable): extra `Host` accepted by the MCP
  DNS-rebinding guard when served under another hostname.
- `--mcp-allowed-origin` (repeatable): extra browser `Origin` accepted by the
  same guard.

## `plan.yaml` reference

Workers are **not** declared in a plan; they are configured once in
`.data/workers.yaml` (see [below](#workersyaml-reference)).

```yaml
version: 1

defaults:                # optional, applied to every task unless overridden
  model: opencode-go/deepseek-v4.1-flash
  agent: build
  timeout_s: 600
  download: modified     # modified | all | none
  requirements:          # only dispatch to a matching worker (see below)
    os: windows
    tags: [mssql]
  resources:             # what to materialize on the worker before OpenCode runs
    - type: git
      id: app
      with: {repo: "...", path: "...", worktree: true, ref: main, branch: "hiveling/{run}/{task}", publish: push}
  artifacts:
    download: modified
  opencode:              # OpenCode runtime config injected for every task
    agents_paths: [/opt/team/agents]
  max_parallel: 2        # optional: cap concurrent tasks (0/unset = one per free worker)

tasks:
  - id: hello            # required, unique
    prompt: "Reply with exactly: PONG"     # or prompt_file: path/to/prompt.md
    files: []            # paths/globs relative to the plan, sent to the worker
    model: ...           # optional override
    agent: ...
    timeout_s: 600
    variant: ...         # model variant (reasoning effort)
    title: ...
    env: {KEY: value}    # extra environment variables for the subprocess
    download: none
    depends_on: []       # ordering + skip if a dependency fails
    inputs_from: []      # reuse files downloaded from previous tasks (zip channel)
    requirements: {}     # merged over defaults
    resources: []        # merged over defaults by id
    artifacts: {}
    opencode: {}
```

- `depends_on` and `inputs_from` both imply ordering; `inputs_from` also feeds
  the previous task's downloaded files into the current task's working
  directory. `inputs_from` is the **file (zip) channel only** — git flows
  through resources (see below).
- `files` are also sent as a zip before the task starts.

### Requirements (capabilities)

A worker advertises capabilities through `GET /health`: `os`, `arch`, `tools`,
`providers`, `tags`, `labels` and (optionally) `path_roots`. A task's
`requirements` are matched as a subset, so it only runs on a worker that can
satisfy them; without requirements it runs on any free worker. If no reachable
worker matches, the task **fails fast** instead of waiting for a worker.
`list_workers` shows every worker's capabilities.

```yaml
requirements:
  os: windows            # exact match
  tags: [mssql]          # all required
  providers: [git]       # all required
  labels: {site: office} # exact per key
```

The worker reads an optional `capabilities.yaml` (default `./capabilities.yaml`,
hot-reloaded; `--capabilities-file` / `WORKER_CAPABILITIES`):

```yaml
tags: [legacy, mssql]
labels: {site: office-mtl}
providers: [ephemeral, env, secret, git, path]   # allowlist
path_roots: ["D:\\src", "D:\\data"]              # absolute paths a plan may use
```

### Resources (environment)

A task's `resources` are validated and prepared on the worker **before**
OpenCode starts, so a misconfiguration fails fast. Resources merge by `id`
across `defaults.resources` and the task (a same-`id` task resource overrides;
new ids append). Built-in providers:

| `type` | `with` options | Effect |
| --- | --- | --- |
| `ephemeral` | — | A fresh, isolated working directory (the default behavior). |
| `env` | `vars` | Inject environment variables (`${NAME}` expands the worker env). |
| `secret` | `name`, `as?`, `required?` | Inject a secret resolved **on the worker** (never in the plan). |
| `git` | `repo`, `path`, `worktree?`, `ref?`, `branch?`, `branch_mode?`, `merge?`, `push_to?`, `clean?`, `cache?`, `publish?`, `remote?`, `force?` | Clone/reset/checkout, optional per-task worktree, merge, commit/push. |
| `path` | `path`, `mode` (`ro`/`rw`), `visible?` | Expose an existing folder. |
| `command` | `prepare`, `finalize`, `shell?`, `env?`, `cwd?`, `timeout_s?` | **Opt-in** escape hatch: shell commands around the run. |

`git` semantics: `ref` is the **start point** (branch/tag/sha), `branch` is the
**target** the task commits to, `publish` is `none|commit|push`, `clean` is
`none|git|full`, `cache` lists paths preserved across a clean. `push` requires
the task to produce something: a task that edits nothing (and merges nothing in
prepare) **fails** rather than quietly succeeding with an empty branch — use
`publish: none`/`commit` for review or verification tasks that only read a
branch. To consume another task's branch, declare the same repo with
`ref: "hiveling/{run}/<producer>"` and `depends_on: [<producer>]` — there is no
cross-task substitution. `{run}` and `{task}` in option strings are resolved by
the server.

`worktree: true` checks the task's working tree out at `<workspace>/src/<id>`
(the plan's `path` is only the durable clone, staged outside the workspace for a
relative path); `worktree: false` checks out in place at `path`. Write prompts
against the checkout — the worker also prepends a `[Working environment]` block
listing each resource's working directory.

`merge: [refs]` merges the refs into `branch` during prepare, **before** the
task's OpenCode run. A conflict does not fail the task: the worker resolves it
itself with a nested OpenCode run (a generic built-in prompt), then commits a
merge commit per ref. Traceability is in the task's own log: synthetic
`hiveling.merge` events in `events.jsonl` (visible in the dashboard) plus a
structured `merge` field in `status.json` / `run.json` / `get_task`. Example —
an integration task that fans several branches in:

```yaml
- id: integrate
  depends_on: [feature-a, feature-b]
  prompt: "Run the test suite on the merged result."
  resources:
    - type: git
      id: app
      with:
        repo: REPO
        path: ~/.cache/hiveling/app.git
        worktree: true
        ref: main
        branch: "hiveling/{run}/integrate"
        merge: ["hiveling/{run}/feature-a", "hiveling/{run}/feature-b"]
        publish: push
```

Absolute paths (`git.path`, `path.path`, `opencode` sources) must live under the
worker's `path_roots`; relative paths resolve under the job workspace.

### Gates (bounded rework)

A **gate** is an optional, read-only checkpoint over one or more tasks. It runs
after those tasks succeed and answers with a standalone `VALID` line, or with
the corrections it requires. Without `VALID`, the analysed tasks and the gate
are reset and re-run; after `max_attempts` (default 20) the gate fails and the
run stops (fail-fast). This is how a review becomes a *gate* instead of
decoration: the plan cannot proceed to the gate's consumers until it passes.

```yaml
tasks:
  - id: designer
    prompt: "Produce docs/ui-design.md and commit."
    resources: [{type: git, id: repo, with: {repo: REPO, path: repo, worktree: true,
                 ref: main, branch: "hiveling/{run}/{task}", publish: push}}]
  - id: coder                          # consumes the approved design
    prompt: "Implement docs/ui-design.md."
    depends_on: [designer-gate]        # depend on the GATE, not on designer
    resources: [{type: git, id: repo, with: {repo: REPO, path: repo, worktree: true,
                 ref: "hiveling/{run}/designer", publish: push}}]

gates:
  - id: designer-gate
    tasks: [designer]
    prompt: "Reject if the design contradicts itself or leaves the implementer guessing."
    max_attempts: 5
```

The **server owns the gate agent** (`hiveling-gate`, injected per job with a
locked read-only permission profile); the plan only supplies the criteria, never
an `agent`. The gate runs on a worker with **no repository**, so it judges from
the decision material the server uploads under `./_hiveling/`: the plan, and for
every analysed task its status, result text, changed files, commits and the
**files it downloaded** (`tasks/<id>/files/`). To be judged on its content, an
analysed task must therefore download its deliverable (`download: modified` or
`all`). The gate's own answer is in `result_text` (`run.json` → `gate_verdict`,
`gate_attempt`).

Two plan-validation rules apply: a task belongs to at most one gate, and every
consumer of a gated task **must depend on the gate** (not on the task directly).
Live edits and `resume_run` keep working; a gate that is reset by a rejection
gets the same bounded treatment.

### OpenCode config injection

`opencode` (in defaults and/or per task) builds a per-job OpenCode config at the
job's working directory. It takes **inline** content or **worker-local paths**,
so plans carry paths instead of re-writing agent/skill bodies:

```yaml
opencode:
  config: {model: provider/model, permissions: [...]}   # inline opencode.json fragment
  agents: {reviewer: "You are ..."}                     # inline agents
  skills: [{name: run-tests, content: "..."}]           # inline skills
  from: [/opt/team/opencode]        # bundle: opencode.json + agents/ + skills/ + AGENTS.md
  agents_paths: [/opt/team/agents]  # agent *.md directories (symlinked/junctioned)
  skills_paths: [~/shared/skills]   # skill directories (referenced by path in config)
  agents_md: [/opt/team/AGENTS.md]  # concatenated into <workdir>/AGENTS.md
  sources:                          # verbose form with mode/priority/optional
    - {kind: agents, path: /opt/team/agents, priority: 10}
```

Precedence (low → high): worker baseline bundle (`--opencode-dir`, default
`./opencode`), repo/config provenance reported by `git`/`path`, plan sources,
then plan inline. Skills are added to the config `skills` array; agents are
symlinked into `.opencode/agents/`; `AGENTS.md` files are concatenated.

**Permissions come from this config, not from a CLI flag.** The worker does not
pass `--auto` to `opencode run`: a permission is allowed only if the injected
config grants it (and the agent's own `permissions` rules). There is no plan
`auto` option — to allow something, declare it in `opencode.config.permissions`
or on the agent. This keeps the generated `opencode.json` authoritative.

On top of the declared layers, the worker always grants every agent **read and
write access to the platform temp directory** (`$TMPDIR` on Linux, `%TEMP%` on
Windows). A non-interactive run auto-rejects an `ask` permission, so an agent
that writes a scratch file (a commit-message file, a build artifact) under the
system temp dir would otherwise abort the whole session. This built-in rule has
the lowest priority: any `permissions`/`permission` rule declared later (repo
config, plan sources, plan inline) or on the agent itself still wins.

### Artifacts

```yaml
artifacts:
  download: modified      # zip: modified (default) | all | none
  paths: ["reports/**"]   # extra globs added to the zip beyond the workdir diff
  git: true               # keep the commits/branches a git resource published
```

Commits (branch, sha, remote, pushed) and merge results (refs, conflicted
files, merge commits) are recorded in the run state (`run.json`), printed by the
CLI and returned by `get_task`; merge conflicts also appear as `hiveling.merge`
lines in the task's events.

## Worker configuration

Besides its workspace (`.data/worker`) and logs, a worker reads three optional,
hot-reloaded configuration files and adverts them through `/health`:

| File | Flag / env | Role |
| --- | --- | --- |
| `capabilities.yaml` | `--capabilities-file` / `WORKER_CAPABILITIES` | Advertised capabilities (`tags`, `labels`, `providers`, `path_roots`) merged with auto-detection. |
| `secrets.yaml` | `--secrets-file` / `WORKER_SECRETS` | Flat `NAME: value` store; a plan only ever references secret **names** (the worker environment is the fallback). |
| `opencode/` | `--opencode-dir` / `WORKER_OPENCODE_DIR` | Baseline OpenCode bundle (`opencode.json`, `agents/`, `skills/`, `AGENTS.md`) applied to every job. |

`providers` acts as an allowlist: a plan resource whose provider is not listed
is refused. `command` runs arbitrary shell commands and is **not** advertised by
default; list it explicitly to enable it. Migrate absolute paths to
`path_roots` so plans cannot reach outside them (`path_roots: ["*"]` disables
the check explicitly).

OpenCode config layers merge **deeply** (nested objects are merged, not
replaced); `permissions`, `skills` and `plugins` lists are concatenated so every
layer's rules survive. Precedence, low to high: worker `opencode/` bundle → repo
config (`opencode.json`, `.opencode/`, `AGENTS.md` of a `git`/`path` resource) →
plan sources → plan inline.

## Garbage collection

Every job gets its own directory under the worker workspace, so finished jobs
would grow forever. GC removes **terminal** jobs only; running/accepted jobs are
never touched. Two independent criteria, **enabled by default**; set either to
`0` to disable it:

- `--retention-jobs N` (env `WORKER_RETENTION_JOBS`, default **50**): keep only
  the newest N finished jobs on the worker (runs at startup, then hourly);
- `--retention-days D` (env `WORKER_RETENTION_DAYS`, default **14**): also
  remove finished jobs older than D days.

A one-shot command prunes on demand:

```bash
python worker/gc.py --workspace .data/worker --keep 20 --days 7
```

On the server, finished runs and their history are pruned with
`dashboard.py --history-retention-runs N` / `--history-retention-days D`
(env `HIVELING_RETENTION_RUNS` / `HIVELING_RETENTION_DAYS`), applied at startup.
Server-side retention is off by default (0 = disabled).

The durable git clones (`git.path`, outside the job directories) are a cache and
are **not** collected; only per-job directories and their worktrees are.

## `workers.yaml` reference

```yaml
workers:
  - name: local          # optional label
    url: http://127.0.0.1:8787     # full URL... or host + port

  - name: windows11
    host: 10.0.0.174     # ...host + port variant
    port: 8787
```

- Location: `<data-dir>/workers.yaml` by default, override with `--workers-file`
  (both `server/dashboard.py` and `server/run.py`).
- The file is reloaded whenever it changes on disk, so adding or removing a
  worker does not require a server restart. A malformed file keeps the previous
  worker list and surfaces an error in the dashboard and `list_workers`.
- Ready tasks are dispatched round-robin across the free reachable workers;
  independent tasks run in parallel, one task per worker at a time.

## Persistence, recovery and live edits

Each run is durable. Under `.data/runs/<run_id>/`:

- `run.json` — run status plus every task state (status, `worker`,
  `worker_url`, `job_id`, `history_rel`);
- `plan.yaml` — a copy of the plan the run executes. Relative paths
  (`prompt_file`, `files`) still resolve against the original plan directory
  (`base_dir`), so the copy is safe;
- `owner` — pid/host/heartbeat lease so two servers never run the same run.

**Restart recovery.** On startup the server looks for runs left `running`
(`RunManager.recover`). Unless another live server owns one (fresh heartbeat),
it keeps every terminal task as-is, and:

- a task that was `running` is **reattached** to its worker job
  (`GET /jobs/{id}`) and its result adopted — worker jobs keep running while the
  server is down, so they are not lost;
- it is marked `failed` only if the job/worker is gone or unreachable;
- the run then resumes: every task without a result whose dependencies are
  satisfied is dispatched again (in parallel).

So an abrupt `kill -9` of the server is recoverable. Only a worker that also
restarted loses its in-flight job (that task is then failed). Runs without a
snapshot (older format) fall back to their `plan_path`.

**Live plan editing.** `PUT /api/runs/{id}/plan` (MCP `update_run_plan`)
validates the new YAML, writes the run's snapshot, and updates the source plan
file when it is editable. The orchestrator re-reads the snapshot between tasks:

- a `pending` task whose definition changed uses the new definition;
- new tasks are added `pending` and scheduled;
- `pending` tasks removed from the plan are marked `canceled`
  ("removed from plan");
- tasks already `running` or terminal are never touched.

Editing never interrupts the task currently running; changes land before the
next task starts.

## Logs

Both apps write a rotating log under `.data/logs/` (`server.log` and
`worker.log`). The server log records startup, run/task lifecycle, dispatches,
worker errors, recovery and live edits, plus a periodic heartbeat; uncaught
exceptions (main thread and worker threads) are logged with a traceback.

- `server/dashboard.py --log-dir <dir>` (default `<data-dir>/logs`) and
  `--heartbeat <s>` (default 60; 0 disables).
- `server/run.py --log-dir <dir>` and `--log-level <level>`.

The end of the file tells an abrupt kill from a crash: a traceback is a Python
crash, `server stopped` is a clean shutdown, and a trailing heartbeat with no
stop line means the process was killed abruptly — exactly the case recovery
handles.

## HTTP protocol (worker)

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/health` | worker state: `busy`, `active_job`, `opencode_bin` |
| `POST` | `/jobs` | create a job (`accepted`), returns status |
| `PUT` | `/jobs/{id}/files` | upload a zip of input files (raw body) |
| `POST` | `/jobs/{id}/start` | start execution |
| `GET` | `/jobs/{id}` | detailed status |
| `GET` | `/jobs/{id}/files?which=modified\|all` | zip of files produced |
| `GET` | `/jobs/{id}/logs` | `events.jsonl` + `stderr.log` |
| `DELETE` | `/jobs/{id}` | cancel a job |
| `GET` | `/jobs` | list jobs |

Job creation and file upload are separate steps so the worker knows when to
start: it only runs OpenCode after `POST /jobs/{id}/start`. Jobs created but
never started expire after `--accept-timeout` seconds.

## History

Each run is recorded in `.data/runs/<run_id>/run.json` (status of the
run and of every task). For each task the server also writes artifacts in
`.data/history/<task_id>/<run_id>/`:

```
.data/history/<task_id>/<run_id>/
  request.json      request sent to the worker
  status.json       final status returned by the worker
  result.txt        final assistant text
  events.jsonl      raw OpenCode JSON events
  stderr.log        worker/OpenCode stderr
  worker.json       worker name/url and job id
  files.zip         zip of files downloaded from the worker
  files/            extracted files (used by inputs_from)
```

## Windows worker

Everything works the same on Windows. Run the worker from a normal user
session (for example the Startup folder or a logon scheduled task) so that it
can later launch GUI applications. Avoid running it as a Windows service:
services live in session 0 and cannot interact with the desktop.

Set the opencode binary explicitly if needed:

```
python worker\run.py --port 8787 --opencode-bin "C:\path\to\opencode.exe"
```

## Logging

The worker writes a rotating log to `.data/logs/worker.log` (5 MB × 5 files,
also echoed to stderr). It is designed to make failures diagnosable after the
fact, up to and including a process that vanished without any console:

- a single `worker starting [pid=…]` line at boot, and a `worker stopped` line
  on a clean shutdown;
- a `heartbeat` line every `--heartbeat` seconds (default 60, `0` disables)
  showing the last time the process was known alive;
- job lifecycle lines (accepted, start requested, opencode pid/command,
  exit code, timeout, cancel) and uncaught exceptions with a traceback.

The tail of the file therefore tells the failure mode:

| Last line in the log | Meaning |
| --- | --- |
| traceback (`CRITICAL … uncaught exception`) | the worker crashed in Python |
| `worker stopped` | clean shutdown (Ctrl+C, in-process exit) |
| a `heartbeat` with no `worker stopped` | the process was killed abruptly (`taskkill /F`, parent session reaping, power loss) |

Flags: `--log-dir` (default `./.data/logs`, env `WORKER_LOG_DIR`), `--log-level`
(default `info`, env `WORKER_LOG_LEVEL`), plus `--capabilities-file`,
`--secrets-file` and `--opencode-dir` (see [Worker configuration](#worker-configuration)).

## Current limitations

- One active job per worker (worker-side concurrency is still 1; the
  orchestrator schedules one task per worker). Cap the run's parallelism with
  `--max-parallel` or `defaults.max_parallel`.
- The JSON API and dashboard have no authentication (bind to a trusted
  network); the MCP endpoint can require a bearer token (see above).
- Runs can be stopped as a whole (`cancel_run`) and individual tasks can be
  canceled (`cancel_task`); a canceled task makes the run finish as `failed` if
  no other outcome overrides it.
- After a failure, in-flight tasks run to completion (they are never killed);
  only tasks that had not started yet are marked `skipped`. Recovery still
  requires the worker to hold the job: if the worker restarted too, the
  in-flight task is marked `failed` (an in-flight task is not retried
  automatically).
- Live plan edits apply between tasks; a task already `running` is never
  interrupted.
- Task working directories are independent: use `inputs_from` (files) or a
  `git` resource (branches) to pass work between tasks.
- Long prompts are passed as command-line arguments (Windows command-line
  length limits apply).

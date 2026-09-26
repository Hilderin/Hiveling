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

- Sequential execution (no parallelism).
- The server dispatches each task to the first free worker (round-robin).
- Workers are configured once in `.data/workers.yaml`; the list is reloaded
  automatically when the file changes, so adding a worker needs no restart.
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
| `wait_for_run` | block until a run finishes (`terminal`) or changes; returns the state and the wake reason |
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

A run is **fail fast**: it stops at the first failed task and marks every task
that had not run yet as `skipped` with `skip_reason: run_stopped`. This gives an
orchestrator a clean, explicit decision point instead of a half-finished plan.
`resume_run` re-arms a finished run: its `failed` and `skipped` tasks (still in
the plan) are reset to `pending` — the previous error is kept in `last_error`
and `status.json`/`result.txt` are archived under
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
  auto: true             # pass --auto to opencode
  timeout_s: 600
  download: modified     # modified | all | none

tasks:
  - id: hello            # required, unique
    prompt: "Reply with exactly: PONG"     # or prompt_file: path/to/prompt.md
    files: []            # paths/globs relative to the plan, sent to the worker
    model: ...           # optional override
    agent: ...
    auto: true
    timeout_s: 600
    variant: ...         # model variant (reasoning effort)
    title: ...
    env: {KEY: value}    # extra environment variables for the subprocess
    download: none
    depends_on: []       # ordering + skip if a dependency fails
    inputs_from: []      # reuse files downloaded from previous tasks
```

- `depends_on` and `inputs_from` both imply ordering; `inputs_from` also feeds
  the previous task's downloaded files into the current task's working
  directory.
- `files` are also sent as a zip before the task starts.

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
- Each task is dispatched to the first free reachable worker (round-robin).

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
- the run then continues from the first task without a result.

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

Flags: `--log-dir` (default `./.data/logs`, env `WORKER_LOG_DIR`) and
`--log-level` (default `info`, env `WORKER_LOG_LEVEL`).

## Current limitations

- One active job per worker; no parallelism.
- The JSON API and dashboard have no authentication (bind to a trusted
  network); the MCP endpoint can require a bearer token (see above).
- Runs can be stopped as a whole (`cancel_run`) and individual tasks can be
  canceled (`cancel_task`); a canceled task makes the run finish as `failed` if
  no other outcome overrides it.
- Recovery requires the worker to still hold the job: if the worker restarted
  too, the in-flight task is marked `failed` (an in-flight task is not retried
  automatically).
- Live plan edits apply between tasks; a task already `running` is never
  interrupted.
- Task working directories are independent: use `inputs_from` to pass files
  between tasks.
- Long prompts are passed as command-line arguments (Windows command-line
  length limits apply).

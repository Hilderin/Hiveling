# Hiveling

A minimal orchestrator + worker setup that runs a `plan.yaml` on remote
machines. The worker is a Python HTTP server that receives a task (prompt,
model, input files) and runs `opencode run` in a subprocess. File exchange
between the server and the worker happens over HTTP using zip archives.

```
 plan.yaml ──▶  server (orchestrator)  ──HTTP──▶  worker(s)  ──CLI──▶  opencode run
                       │                                │
                       └─── .data/history/<task>/<run>/ ◀─┘ (zip of changed files)
```

V1 characteristics:

- Sequential execution (no parallelism).
- The server dispatches each task to the first free worker (round-robin).
- No authentication.
- No live streaming: the server polls the worker every few seconds.
- Everything is stored on disk under `.data/` (server history and
  worker workspace).

## Layout

```
server/            orchestrator (CLI) + web dashboard
worker/            HTTP worker (listens on a port, runs opencode)
examples/          example plans
.data/             working data (gitignored)
  plans/           plan files used by runs (including pushed plans)
  history/         per-task artifacts
  runs/            per-run state shown in the dashboard
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

# keep running other tasks after a failure
./.venv/bin/python server/run.py examples/demo.yaml --keep-going

# run a single task
./.venv/bin/python server/run.py examples/demo.yaml --only hello
```

## Dashboard

`server/dashboard.py` serves a single-page UI (no build step) plus a JSON API.
The layout:

- left sidebar: **Workers** (reachable/busy state of every worker referenced by
  the plans or by past runs) and **Active runs** (compact list of runs still
  running, with `done/total` tasks);
- **History** link: full list of runs, newest first, with a search box (plan
  name or run id) and pagination;
- run view: task tree with each task's state (`pending`/`running`/`succeeded`/
  `failed`/`skipped`/`canceled`);
- task detail: worker, job id, model, duration, tokens, cost, session, error,
  changed files (download as zip), result, tool calls, prompt, events, stderr;
- actions: cancel a running run, retry a task, view/edit the run's plan (plans
  under `.data/plans`, i.e. pushed plans, are editable; example plans are
  read-only from the dashboard).

Runs are started by **pushing a plan over HTTP** (there is no run form in the
UI). Runs execute in background threads; if the dashboard restarts while a run
is active, that run is reconciled to `failed` (interrupted).

Deep links: `?run=<run_id>`, `?run=<run_id>&task=<task_id>`, `?view=history`.

API:

- `POST /api/runs` — start a run. JSON `{"plan": "demo.yaml"}` for an existing
  plan, or `{"plan_yaml": "<content>", "name": "optional.yaml", "only": [...],
  "keep_going": true}`; a raw YAML body (`Content-Type: application/x-yaml`) is
  also accepted. Pushed plans are stored in `.data/plans/`, which is also the
  dashboard's default plan directory (`--plans-dir`). Example plans live in
  `examples/` and are meant for the CLI or as starting points.
- `GET /api/runs?status=active|all&q=<search>&limit=&offset=` — paginated runs.
- `GET /api/runs/{id}`, `POST /api/runs/{id}/cancel`,
  `POST /api/runs/{id}/tasks/{task}/retry`,
  `GET /api/runs/{id}/tasks/{task}`,
  `GET /api/runs/{id}/tasks/{task}/files`,
  `GET|PUT /api/runs/{id}/plan`, `GET /api/workers`.

## `plan.yaml` reference

```yaml
version: 1

workers:                 # required
  - name: local          # optional label
    host: 127.0.0.1      # or: url: http://127.0.0.1:8787
    port: 8787

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
- No authentication (bind to a trusted network).
- Task working directories are independent: use `inputs_from` to pass files
  between tasks.
- Long prompts are passed as command-line arguments (Windows command-line
  length limits apply).

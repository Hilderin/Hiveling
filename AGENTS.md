# AGENTS.md — debugging Hiveling itself

Operational guide for an agent working **on** Hiveling (not on a plan running
through it). Goal: from a symptom to a root cause without guessing. All paths
below are relative to the repository root unless stated otherwise.

## 1. Mental model in one screen

```
plan.yaml ──▶ server (orchestrator) ──HTTP──▶ worker(s) ──CLI──▶ opencode run
                   │                              │
        .data/runs/<run>/run.json      <workspace>/<job>/{status.json,work/}
        .data/history/<task>/<run>/    <workspace>/<job>/events.jsonl
```

- The **server** owns the plan, the DAG, scheduling, retries, history. It polls
  workers (default 2s). It does **not** run OpenCode.
- The **worker** is a FastAPI HTTP server, one job at a time. It materializes a
  task's `resources` (git, env, secret, path, command), injects the OpenCode
  config, runs `opencode run`, then publishes (commit/push) and records
  artifacts.
- A **run** is fail-fast: the first failed task stops new dispatches, in-flight
  tasks finish, the rest are `skipped`. `resume_run` re-arms failed+skipped.

## 2. Where everything lives

Server data (default `--data-dir .data`):

| Path | Content |
| --- | --- |
| `.data/workers.yaml` | worker list (hot-reloaded) |
| `.data/runs/<run_id>/run.json` | run status + every task state (the dashboard's source) |
| `.data/runs/<run_id>/plan.yaml` | plan snapshot the run executes (live-editable) |
| `.data/runs/<run_id>/owner` | lease (pid/host/heartbeat) so two servers don't share a run |
| `.data/history/<task_id>/<run_id>/` | `request.json`, `status.json`, `result.txt`, `events.jsonl`, `stderr.log`, `files.zip`, `files/` |
| `.data/plans/` | stored plans (editable from the dashboard) |
| `.data/logs/server.log` | rotating server log |

Worker job layout (`<workspace>/<job_id>/`, workspace from `--workspace`):

| Path | Content |
| --- | --- |
| `status.json` | job status: `added/modified/deleted`, `commits`, `merge`, `artifacts`, `error` |
| `request.json` | the exact JobSpec sent by the server |
| `events.jsonl` | OpenCode JSON events + synthetic `hiveling.*` events (merge, working dirs) |
| `stderr.log` | OpenCode stderr |
| `work/` | OpenCode cwd ("location dir"): injected `opencode.json`, `.opencode/agents`, `AGENTS.md` |
| `work/src/<resource-id>/` | git worktree (`worktree: true`) |
| `repos/<resource-id>/` | durable clone for a relative `git.path` (staging, never the checkout) |

## 3. Starting / stopping / inspecting

Server (dashboard + JSON API + MCP on the same port):

```bash
./.venv/bin/python server/dashboard.py --host 127.0.0.1 --port 8080
# CLI one-shot run:
./.venv/bin/python server/run.py examples/demo.yaml --dry-run
```

Worker:

```bash
# Linux
./.venv/bin/python worker/run.py --host 0.0.0.0 --port 8787 \
  --workspace .data/worker --log-dir .data/logs
# Windows (from a normal user session, never a session-0 service)
.venv\Scripts\python.exe worker\run.py --port 8787
# many workers / start/stop/status (Windows):
powershell -File worker\start-multi.ps1 -Count 3 -StartPort 8787
powershell -File worker\start-multi.ps1 -Action Status
powershell -File worker\start-multi.ps1 -Action Stop
```

Quick probes (no MCP needed):

```bash
curl -s 127.0.0.1:8787/health          # busy? active_job? capabilities
curl -s 127.0.0.1:8080/api/workers     # every worker, reachable/busy, capabilities
curl -s 127.0.0.1:8080/api/runs/<run_id>
grep -a heartbeat .data/logs/worker.log | tail -1   # is the worker alive?
```

## 4. Debugging workflow (follow in order)

1. **Is the server up?** `dashboard.py --help` invariant; check
   `.data/logs/server.log` last lines. `server stopped` = clean, traceback =
   crash, a trailing `heartbeat` with no stop line = killed abruptly.
2. **Are the workers reachable and free?** `curl .../api/workers` or
   `list_workers`. `reachable: false` → firewall, wrong host, or worker down
   (`grep heartbeat worker.log`).
3. **Which task failed and with what?** `run.json` → `task.status`,
   `task.error`, `task.skip_reason`; `last_error` keeps the previous attempt.
   `history/<task>/<run>/status.json` is the worker's verbatim status.
4. **Read the task's own evidence** in `history/<task>/<run>/`: `result.txt`
   (final text), `events.jsonl` (tool calls, errors, synthetic `hiveling.*`),
   `stderr.log`. Do not trust a summary over these.
5. **On the worker**, the job dir (`<workspace>/<job_id>/`) has the real state:
   `request.json` (what it was asked), `status.json` (what it produced),
   `status.json.commits`/`merge`, and `work/` (what OpenCode actually saw).
6. **Reproduce in isolation** with `server/run.py <plan> --only <task>` (and
   `--dry-run` to print the plan order without running).

## 5. Common symptoms → root cause

| Symptom | Likely cause / check |
| --- | --- |
| `no matching worker: need [...] but reachable are ...` | `requirements` match no advertised capability. `list_workers`, check `capabilities.yaml` (`tags`, `labels`, `os`, `providers`). |
| Task `failed`, `error: environment error: unknown resource provider 'x'` | provider not implemented **or** not enabled: check the worker's `providers` list (allowlist). `command` is opt-in. |
| `environment error: … path is outside the worker path_roots` | an absolute path in `resources`/`opencode` not under `path_roots`; see `list_workers` → `capabilities.path_roots`. Relative paths resolve under the job workspace. |
| `environment error: prepare failed: git …` | read the last 500 chars: missing ref, clone/fetch failure, path collision. See §6. |
| `git: nothing to push for branch 'x' … the task made no change` | the task edited the wrong tree (see §6) or is read-only: use `publish: none`/`commit`. |
| Task `succeeded` but consumer fails `merge ref not found` / `ref not found` | the producer pushed nothing (see previous line), or the consumer's ref is misspelled / lacks `depends_on`. |
| `opencode` finds no files / edits the wrong place | the prompt points at the workspace root instead of the checkout (`./src/<id>`); the worker prepends a `[Working environment]` block — read it in `events.jsonl`. |
| `worker unreachable` after N tries | worker restarted or network; the job is lost (a worker restart fails its in-flight job). |
| Task `failed` with `timeout after Ns` | raise `timeout_s`; check the opencode process was terminated (logs) — long prompts on Windows hit CLI length limits. |
| Run stuck, no worker free | `max_parallel`/one-job-per-worker; a worker is `busy`; check `/health`. |
| Two servers on one run | `owner` lease in `run.json`; both log recovery. Keep one. |
| Files from `inputs_from` missing | the producer had `download: none` (nothing was zipped) or its `files/` dir is empty. |
| `opencode` config not applied / agents missing | check `work/opencode.json` and `work/.opencode/agents/` in the job dir; source paths must be under `path_roots`; a repo `.opencode/` is merged, plan inline wins. |

## 6. Git provider specifics (the subtle ones)

- **Two layouts.** `worktree: true` → checkout at `work/<src>/<id>`, durable
  clone at `<job>/repos/<id>` when `path` is relative (staging, never the
  checkout — a clone at `<workspace>/<path>` used to swallow edits silently).
  `worktree: false` → checkout in place at `path`.
- **`ref` vs `branch`.** `ref` = start point (branch/tag/sha), `branch` =
  target the task commits to. A consumer names the producer's branch as `ref`
  **and** lists it in `depends_on`.
- **Remote-only refs.** A consumed branch exists only as `origin/<ref>` in a
  fresh clone; the provider resolves local→`origin/`→fetch→error
  (`git: ref not found: '…'`). A hard failure here means the producer did not
  push (check `status.json.commits[*].pushed` and the remote).
- **`merge: [refs]`** merges during prepare and resolves conflicts with a
  nested OpenCode run (`CONFLICT_PROMPT`); the result is in `status.json.merge`
  and `events.jsonl` (`hiveling.merge` conflict/resolved). Unresolved → prepare
  fails with the file list.
- **Never force-push** unless `force: true`. `branch_mode` defaults to
  `create` (fails if the branch exists → use `reuse`/`recreate`).

## 7. Worker internals worth knowing

- **cwd is the job's `work/`**, and on POSIX the worker sets `env["PWD"]` to it
  as well: OpenCode V2 resolves the working dir from `$PWD`, and
  `subprocess(cwd=…)` alone is not enough. `opencode run` is invoked with
  `--standalone` (private server per run) — removing either reintroduces
  "runs in the worker's launch dir".
- **Provider lifecycle**: `validate → prepare → (opencode run) → finalize →
  teardown`. `prepare` failures fail the job **before** OpenCode; `finalize`
  (publish) failures fail the job even if OpenCode succeeded; `teardown` never
  fails the job.
- **Jobs are serialized** (one active job per worker). Directory `<workspace>/<job_id>`.
- **GC** removes terminal jobs only (`--retention-jobs`, default 50;
  `--retention-days`, default 14; hourly). `status.json`/`run.json` are read as
  `utf-8-sig` (a BOM must not hide a job from the collector).
- **Hot-reloaded worker config**: `capabilities.yaml` (`tags/labels/providers/
  path_roots`), `secrets.yaml`, and the OpenCode bundle (`--opencode-dir`).

## 8. Reproduce and test

```bash
./.venv/bin/python -m pytest tests/ -q          # 72 tests, no network
./.venv/bin/python -m pytest tests/test_providers_git_path.py -q
./.venv/bin/python server/run.py examples/test-suite.yaml --only t1-text
```

When fixing a bug: add a regression test in `tests/` that fails before the fix.
The git provider has local bare-repo fixtures (`tests/test_git_merge.py`,
`tests/test_git_ref_resolution.py`) — prefer them over the network.

## 9. Reading a run's state (MCP / CLI)

- MCP tools (same process as the dashboard): `get_run`, `get_task`,
  `wait_for_run`, `list_workers`, `get_plan_schema`, `update_run_plan`,
  `resume_run`. Use the bundled `hiveling-plan` skill for the driving loop.
- `run.json` task fields: `status`, `error`, `last_error`, `skip_reason`,
  `worker`, `job_id`, `history_rel`, `changed_files`, `commits`, `merge`,
  `requirements`, `attempts`.
- Archive on retry: `history/<task>/<run>/attempt-N/`.

## 10. Conventions when changing Hiveling

- Keep `plan.yaml` explicit and self-contained: no include, no named
  environment, no cross-task substitution — only `{run}` and `{task}`.
- The plan schema is the contract; when you change it update
  `server/app/plan_schema.py` (reference + example) **and** `README.md`, and
  keep backwards compatibility for plans without the new fields.
- Secrets never appear in a plan; only names, resolved on the worker.
- Match the existing style: small pure functions, failures as typed
  exceptions, no new dependency without a reason.

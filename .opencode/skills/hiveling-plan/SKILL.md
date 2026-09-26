---
name: Hiveling plans and runs
description: Create, run and supervise Hiveling plans through the Hiveling MCP tools (plan.yaml, workers/requirements, resources, opencode config, artifacts, the fail-fast/resume agentic loop). Use when a task must be executed on remote OpenCode workers, when authoring or fixing a plan.yaml, or when driving a Hiveling run to completion.
---

# Driving Hiveling through MCP

Hiveling executes a `plan.yaml` DAG of tasks on remote OpenCode workers and
returns each task's result, changed files, commits and artifacts. This skill is
the **workflow**; the **schema is the source of truth**:

- call `get_plan_schema()` before authoring (full reference + canonical example),
- call `list_workers()` before writing `requirements` or worker-local paths.

Prefer `create_plan` (typed object) for new plans; `update_plan` /
`update_run_plan` for edits.

## Golden rules

- **Workers are not in the plan.** They live in `workers.yaml`; select one with
  `requirements`. Never invent a worker, path or capability.
- **The plan is explicit and self-contained.** No include, no named environment,
  **no cross-task substitution**. Everything is written out.
- **`depends_on` is always explicit.** Ordering is never inferred. `inputs_from`
  is the **file/zip** channel only; git flows through `resources`.
- **Secret values never appear in a plan** — only `{type: secret, with: {name}}`
  (resolved on the worker).
- **Paths are plan-relative or under a worker's `path_roots`.** Anything else
  fails at prepare.

## Authoring (tools: get_plan_schema, list_workers, create_plan, get_plan)

Minimal shape:

```yaml
version: 1
defaults:                 # optional; applied to every task unless overridden
  model: provider/model
  agent: build
  timeout_s: 600
  requirements: {os: linux}
  resources: [...]
  opencode: {...}
  artifacts: {download: modified}
tasks:
  - id: unique-id
    prompt: "..."          # or prompt_file: relative/to/plan
    depends_on: [other]
    requirements: {}       # merged over defaults
    resources: []          # merged over defaults by id
```

`requirements` is matched as a subset of a worker's advertised capabilities
(`os`, `tags`, `providers`, `labels`); a task with no requirements runs on any
free worker, and a task no reachable worker can satisfy **fails fast**.

Resources are prepared on the worker **before** OpenCode runs. Built-ins:
`ephemeral` (fresh dir, default), `env`, `secret`, `path`, `git`, `command`
(opt-in). See `get_plan_schema` for every `with` option — don't guess them.

`opencode` injects an OpenCode config per job; prefer worker-local **paths**
(`from`, `agents_paths`, `skills_paths`, `agents_md`) over inline Markdown.

## Running and supervising (the agentic loop)

```
run_plan(plan="name" | plan_yaml="..." | name+plan_yaml?) -> run_id
loop:
    ev = wait_for_run(run_id, until="terminal", timeout_s=600)
    if ev.timed_out: keep waiting / inspect get_run(run_id)
    if run.status == "succeeded": done
    # a failure: inspect the worst task, then decide
    bad = the failed/canceled task in get_run(run_id).tasks
    get_task(run_id, bad.id)            # status, result, error, events, commits, merge
    decide:
        fix the plan  -> update_run_plan(run_id, new_yaml) then resume_run(run_id)
        transient     -> resume_run(run_id)
        give up       -> cancel_run(run_id)
```

Decision rules:

- **Fail fast**: the first failed task stops new dispatches, but tasks already
  running finish; the rest are `skipped` (`skip_reason`) or `canceled`. So after
  a failure, `get_run` shows exactly what ran and what did not.
- **`resume_run`** re-arms a finished run: failed + skipped tasks are reset to
  `pending` (previous error kept in `last_error`) and it re-reads the plan
  snapshot. Use it after `update_run_plan`.
- **Live edits** (`update_run_plan`) apply **between tasks**; a running task is
  never interrupted. New tasks are scheduled; removed pending tasks are canceled.
- **`cancel_task`** stops one task and the run continues; **`cancel_run`** stops
  everything.
- Diagnose before adjusting: `get_task` gives the result text, the error, the
  tail of OpenCode events and (when present) `commits` / `merge` / `artifacts`.
  `events_tail_lines=0` to skip the log.

## Recipes for an agentic dev loop

- **One concern per task.** Small, bounded prompts; let the DAG express the plan.
- **Branch per task, deterministic:** `branch: "hiveling/{run}/{task}"` +
  `publish: push`. `{run}` and `{task}` are the only substitutions (task-local).
- **Consume a producer's branch** by naming it + explicit ordering (no magic):

  ```yaml
  - id: test
    depends_on: [build]
    resources:
      - {type: git, id: app, with: {repo: REPO,
          ref: "hiveling/{run}/build", branch: "hiveling/{run}/test", publish: push}}
  ```

- **Integrate several branches** with `merge: [refs]` on a git resource: it
  merges into `branch` during prepare and **resolves conflicts itself** (a
  nested OpenCode run), committing a merge per ref. Put the tests in that task's
  prompt, since the merge is already done before it runs. Conflicts and merge
  commits are visible in the task events and `get_task`.
- **Keep reports**: `artifacts: {paths: ["reports/**"]}` adds globs to the
  downloaded zip; `artifacts: {git: true}` surfaces commits/branches.
- **Pick the environment** with `requirements` (e.g. `os: windows`, `tags:
  [mssql]`); a follow-up can run on a different OS as long as it consumes a
  pushed branch.

## Pitfalls

- Assuming tasks share a working directory: they don't. Use `inputs_from`
  (files) or a `git` resource (code) to pass work along.
- Using a path not under the worker's `path_roots` (see `list_workers`).
- `command` (arbitrary shell) is refused unless the worker advertises it.
- Long prompts are command-line arguments on the worker (Windows has limits):
  keep prompts concise; use `prompt_file` to send a file.
- Forgetting `depends_on` on a consumer of a produced branch.
- Calling `update_run_plan` with invalid YAML: it is validated and rejected.

## Checklist

1. `get_plan_schema()` and `list_workers()` called.
2. Every task has a unique `id` and a `prompt`/`prompt_file`.
3. Requirements match advertised capabilities; paths under `path_roots`.
4. Consumers list their producers in `depends_on`.
5. No secret value in the plan; no invented path or worker.
6. Run, then `wait_for_run(until="terminal")`; on failure `get_task` →
   `update_run_plan` → `resume_run`; `cancel_run` to abort.

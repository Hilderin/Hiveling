---
name: Hiveling plan authoring
description: Author or edit a Hiveling plan.yaml (tasks, requirements, resources, opencode, artifacts) via the Hiveling MCP tools. Use when creating, reviewing or debugging a Hiveling run/plan.
---

# Authoring a Hiveling plan

Hiveling runs a `plan.yaml`'s task DAG on remote OpenCode workers. Before
writing a plan, call `get_plan_schema()` (or `create_plan`, whose typed input
schema is published in `tools/list`) and `list_workers()` to see what the
workers can do. Prefer `create_plan` for new plans and `update_plan` /
`update_run_plan` for edits.

## Rules that matter

- **Workers are not in the plan.** They come from `workers.yaml`; use
  `list_workers` and target them with `requirements`. Never invent a worker.
- **The plan is explicit and self-contained.** There is no include, no named
  environment and no cross-task substitution. Write everything out.
- **Every file/path is either plan-relative or a worker-local path under the
  worker's `path_roots`.** Never use a path you did not see in `list_workers`
  capabilities or that the user gave you.
- **Secrets are names, never values.** `{type: secret, with: {name: MY_TOKEN}}`
  resolves on the worker. Never put a token in the plan.
- **`depends_on` is explicit.** Ordering is never inferred. `inputs_from` is the
  zip/files channel only.

## Task shape

```yaml
version: 1
defaults:              # optional, applied to every task unless overridden
  model: provider/model
  agent: build
  timeout_s: 600
  requirements: {os: windows, tags: [mssql]}
  resources: [...]
  opencode: {...}
  artifacts: {download: modified}
tasks:
  - id: unique-id
    prompt: "..."          # or prompt_file: path/relative/to/plan
    depends_on: [other]    # explicit ordering; skipped if a dep fails
```

A run is **fail fast**: the first failed task stops new dispatches (in-flight
tasks finish), and `resume_run` re-arms the failed/skipped tasks.

## Requirements (which worker)

Matched as a subset against `list_workers` capabilities:

```yaml
requirements:
  os: windows            # windows | linux | macos
  tags: [mssql]          # all must be advertised
  providers: [git]       # all must be advertised
  labels: {site: office} # exact per key
```

If no reachable worker matches, the task fails fast. Use `list_workers` before
writing requirements.

## Resources (what the worker prepares before OpenCode starts)

Merged by `id` across `defaults.resources` and the task. Providers:

- `git` — `{repo, path, worktree?, ref?, branch?, branch_mode?, push_to?, clean?,
  cache?, publish?, remote?, force?}`. `ref` = start point, `branch` = target
  the task commits to, `publish: none|commit|push`. Paths must be under the
  worker `path_roots`; relative paths resolve under the job workspace.
- `env` — `{vars: {NAME: value}}` (`${OTHER}` expands the worker env).
- `secret` — `{name, as?, required?}` (resolved on the worker).
- `path` — `{path, mode: ro|rw, visible?}` to expose an existing folder.
- `command` — `{prepare, finalize, shell?, env?, cwd?, timeout_s?}`; only on
  workers that advertise the `command` provider (opt-in).
- `ephemeral` — a fresh workdir (the default when no resource is declared).

**Consuming another task's branch** (no cross-task magic):

```yaml
- id: build
  resources:
    - {type: git, id: app, with: {repo: REPO, ref: main,
        branch: "hiveling/{run}/build", publish: push}}
- id: test
  depends_on: [build]
  resources:
    - {type: git, id: app, with: {repo: REPO,
        ref: "hiveling/{run}/build", branch: "hiveling/{run}/test", publish: push}}
```

`{run}` and `{task}` are the only substitutions (server-resolved, task-local).

## OpenCode config (agents, skills, instructions)

Prefer **worker-local paths** over re-writing Markdown bodies:

```yaml
opencode:
  from: [/opt/team/opencode]          # opencode.json + agents/ + skills/ + AGENTS.md
  agents_paths: [/opt/team/agents]    # agent *.md dirs
  skills_paths: [~/shared/skills]     # skill dirs (referenced in config)
  agents_md: [/opt/team/AGENTS.md]    # concatenated into <workdir>/AGENTS.md
  config: {...}                       # inline opencode.json fragment (deep-merged)
  agents: {reviewer: "You are ..."}   # inline agents
  skills: [{name: run-tests, content: "..."}]
```

A repo checked out by a `git`/`path` resource contributes its own
`opencode.json`, `.opencode/` and `AGENTS.md` automatically; the plan layers on
top of it.

## Artifacts

```yaml
artifacts:
  download: modified      # modified | all | none
  paths: ["reports/**"]   # extra globs added to the zip
  git: true               # keep the commits/branches a git resource published
```

## Checklist before running

1. `list_workers()` — requirements target real capabilities, paths are under
   their `path_roots`.
2. Every task has a `prompt`/`prompt_file` and a unique `id`.
3. External branches are consumed with an explicit `depends_on`.
4. No secret value in the plan; no invented path.
5. Then `run_plan`, `wait_for_run(until="terminal")`, and on failure use
   `get_task` then `update_run_plan` + `resume_run`.

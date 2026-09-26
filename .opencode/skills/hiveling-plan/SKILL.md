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

### Where the code lives (git layout)

OpenCode runs from the **job workspace root**, but a git checkout usually does
**not** sit there:

- `worktree: true` → the task's working tree is `<workspace>/src/<resource id>`.
  The plan's `path` is only the durable clone (staging); the worker keeps a
  relative `path` out of the workspace so `src/<id>` is the single visible tree.
- `worktree: false` (default) → checked out in place at `path`; with a relative
  `path` that is `<workspace>/<path>`.

So write prompts against the tree, e.g. *"the repository is at `./src/repo`;
read `./src/repo/feature.md` and commit inside it"* — not "the workspace root".
The same applies to the repo's OpenCode config: point `opencode.agents_paths` /
`skills_paths` / `agents_md` at the checkout (`./src/repo/.opencode/agents`,
…) — not at `./repo/...`.
The worker also prepends a `[Working environment]` block listing every
resource's working directory to each prompt, but do not rely on it: a wrong path
in the prompt is **silent** — the agent edits a tree the provider never commits,
and `publish: push` simply reports nothing pushed.

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

  ```yaml
  resources:
    - type: git
      id: repo
      with:
        repo: REPO
        path: repo                 # durable clone; keep worktree trees under src/<id>
        worktree: true
        ref: main
        branch: "hiveling/{run}/{task}"
        publish: push
  ```

  With `worktree: true` the prompt must point at `./src/repo` (see *Where the
  code lives*). With `worktree: false` it is `./repo`.
- **`publish: push` is a contract.** The task must produce a change; if it
  edits nothing (and merges nothing in prepare) the task **fails**. So a
  read-only task that consumes a branch (review, acceptance, verification) must
  use `publish: none` (or `commit`) instead. Getting this wrong on a producer is
  how you catch the wrong-tree mistake early instead of at the consumer.
- **Permissions come from the injected config, not a flag.** There is no `auto`
  option and the worker never passes `--auto`. A task may only do what the
  effective `opencode.json` and its agent `permissions` allow; declare anything
  extra under `opencode.config.permissions`.
- **Consume a producer's branch** by naming it + explicit ordering (no magic):

  ```yaml
  - id: test
    depends_on: [build]
    resources:
      - {type: git, id: repo, with: {repo: REPO, path: repo, worktree: true,
          ref: "hiveling/{run}/build", branch: "hiveling/{run}/test", publish: push}}
  ```

  The consumer's working tree is likewise `./src/repo`.

- **Integrate several branches** with `merge: [refs]` on a git resource: it
  merges into `branch` during prepare (before the task runs) and **resolves
  conflicts itself** (a nested OpenCode run), committing a merge per ref. Each
  merged ref must already be pushed, so list its producer in `depends_on`. The
  prompt must say the merge is already done — the agent works in the merged
  `./src/repo`, not at `main`:

  ```yaml
  - id: coder-db                          # parallel work items, both from designer
    prompt: "Create ./src/repo/app/db.py according to ./src/repo/docs/design.md"
    depends_on: [designer]
    resources:
      - {type: git, id: repo, with: {repo: REPO, path: repo, worktree: true,
          ref: "hiveling/{run}/designer", branch: "hiveling/{run}/coder-db", publish: push}}

  - id: coder-api
    prompt: "Create ./src/repo/app/server.py according to ./src/repo/docs/design.md"
    depends_on: [designer]
    resources:
      - {type: git, id: repo, with: {repo: REPO, path: repo, worktree: true,
          ref: "hiveling/{run}/designer", branch: "hiveling/{run}/coder-api", publish: push}}

  - id: tester                            # merges both coders into one tree
    prompt: >
      This branch already merged coder-db and coder-api during prepare.
      Write ./src/repo/tests/ and run the suite from inside ./src/repo.
    depends_on: [coder-db, coder-api]
    resources:
      - type: git
        id: repo
        with:
          repo: REPO
          path: repo
          worktree: true
          ref: "hiveling/{run}/coder-db"          # base branch
          branch: "hiveling/{run}/tester"
          publish: push
          merge:
            - "hiveling/{run}/coder-api"          # merged in during prepare
  ```

  The base matters: branches that all merge the same `ref` (here `designer`)
  share it as ancestor, so subsequent merges stay clean. Don't merge two
  branches that each created the same file from `main` — that is an add/add
  conflict. Keep the per-task worktree (`./src/repo`) in the prompt even when a
  merge is involved.
- **Keep reports**: `artifacts: {paths: ["reports/**"]}` adds globs to the
  downloaded zip; `artifacts: {git: true}` surfaces commits/branches.
- **Pick the environment** with `requirements` (e.g. `os: windows`, `tags:
  [mssql]`); a follow-up can run on a different OS as long as it consumes a
  pushed branch.

Read-only consumers (review, verification, acceptance) still run in the tree
they consume and usually write nothing; give them `publish: none` so they don't
have to satisfy the push contract.

## Pitfalls

- Assuming tasks share a working directory: they don't. Use `inputs_from`
  (files) or a `git` resource (code) to pass work along.
- Pointing prompts at the workspace root instead of the checkout. With
  `worktree: true` the tree is `./src/<id>`; each `git` resource exposes exactly
  that tree (the worker also prepends a `[Working environment]` block). A wrong
  path is **not** silent any more: `publish: push` fails with "nothing to push"
  — the fix is to point the prompt at the checkout (or use `publish: none` for
  a read-only task).
- Using `publish: push` on a task that does not modify the branch. Reading a
  consumed branch and writing no file is a failure unless you set
  `publish: none`/`commit`.
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
4. Prompts reference the checkout (`./src/<id>` with `worktree: true`, else
   `./<path>`), not the workspace root.
5. Read-only tasks (review/verification) use `publish: none`; producers push.
6. Consumers list their producers in `depends_on`.
7. No secret value in the plan; no invented path or worker.
8. Run, then `wait_for_run(until="terminal")`; on failure `get_task` →
   `update_run_plan` → `resume_run`; `cancel_run` to abort.

---
name: Hiveling plans and runs
description: Create, run and supervise Hiveling plans through the Hiveling MCP tools (plan.yaml, workers/requirements, resources, opencode config, artifacts, native gates and bounded review loops, the fail-fast/resume agentic loop). Use when a task must be executed on remote OpenCode workers, when authoring or fixing a plan.yaml, when a review must actually gate the next phase, or when driving a Hiveling run to completion.
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
- **A review that must gate is a `gate`.** Task status is *process* status: a
  review task that returns `CHANGES_REQUESTED` is still `succeeded`, and its
  consumers start anyway. Declare a `gates` entry to make a negative verdict
  stop and re-run the work. See *Gates*.

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
gates:                     # optional; see Gates
  - id: phase-gate
    tasks: [unique-id]
    prompt: "the criteria the gate checks"
    max_attempts: 20
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
read `./src/repo/feature.md`; write the files there — do not run `git commit`,
the worker commits and pushes for you"* — not "the workspace root".
Point `opencode.agents_paths` / `skills_paths` / `agents_md` at the checkout
(`./src/repo/.opencode/agents`, …), not at `./repo/...`. The worker also
prepends a `[Working environment]` block, but do not rely on it: a wrong path in
the prompt is **silent** — the agent edits a tree the provider never commits and
`publish: push` reports nothing pushed.

## Gates (bounded review)

**Gates are the load-bearing part of a review.** Without one a review is
decoration: its task status is `succeeded` whatever it decides, so the next
phase runs on un-approved work. A **gate** is a read-only checkpoint over one or
more tasks. It runs after those tasks succeed and judges them; if it does not
answer `VALID`, the analysed tasks are **reset and re-run**, then the gate is
evaluated again, bounded by `max_attempts` gate evaluations (default **20**; each
rejection consumes one, so at most `max_attempts - 1` rework rounds). Exhaustion
fails the gate, which fails the run (fail-fast) and skips the gate's consumers.

```yaml
gates:
  - id: designer-gate          # a node downstream tasks depend on
    tasks: [designer]          # analysed tasks (one gate per task max)
    prompt: "Reject if the design contradicts itself or leaves the implementer guessing."
    max_attempts: 20           # optional; also model, timeout_s, variant, title
```

**The server owns the gate agent.** Do **not** set `agent` on a gate: Hiveling
injects a hardcoded `hiveling-gate` agent, with a locked read-only permission
profile (all actions denied except `read`/`glob`/`grep`; `*.env` denied). The
plan supplies only the criteria (the gate `prompt`).

**The gate has no repository.** It runs on a worker in a fresh workspace and
reads the decision material the server uploads under `./_hiveling/`:

```
_hiveling/gate.json      criteria, attempt, previous verdicts
_hiveling/plan.json      the plan's tasks and gates
_hiveling/results.json   per analysed task: status, result_text, changed_files, commits, error
_hiveling/tasks/<id>/    prompt.txt, result.txt and files/ (what the task downloaded)
```

So a gated task is judged on the **files it downloaded**: give it
`download: modified` (or `all`) or the gate only sees its report. A task with
`download: none` cannot be content-gated.

**Verdict contract.** The gate ends with a standalone line `VALID` when the
criteria are met, otherwise a list of the corrections required (one per line).
Hiveling appends this instruction itself; the plan only writes the criteria.

**The retry loop.** On a rejection **every task in `tasks`** and the gate go back
to `pending`; the gate's answer is injected into each retried task's prompt (and
`_hiveling/gate-feedback.json`), and — because the retry starts from the same
`ref` — their `git` resources are **force-pushed** so the branch is replaced (no
consumer has read it: consumers depend on the gate). The injected block is
role-neutral: *a producer fixes its artifact; a reviewer re-inspects the updated
artifact and rewrites its report* (producers and reviewers are re-run in
dependency order, so a reviewer that consumes the producer's branch sees the
updated tree). A negative evaluation bumps `gate_attempt` (shown in the dashboard
as `attempt N/M`); after `max_attempts` negative evaluations the gate is marked
`failed`.

**One unit, one gate.** List in `tasks` everything that must be re-run on a
rejection — the producer **and** the reviewer that judges it. A reviewer target
may depend on the producer target of the same gate (both are reset together, so
there is no un-approved artifact to protect); a task outside the gate that
consumes a target must still depend on the gate.

**Two validation rules** (the plan is rejected otherwise):

1. a task belongs to **at most one gate**, and a gate cannot analyse a gate;
2. **every consumer of a gated task must depend on the gate**, not on the task
   directly — except a task that is itself a target of the same gate (e.g. a
   reviewer that consumes the producer target; both reset together). This is
   what prevents a consumer from running on an un-approved artifact.

**Observable:** the gate is a task in `run.json`/`get_task` with
`kind: "gate"`, `gate_targets`, `gate_attempt` (negative evaluations so far;
`attempt N/M` in the UI), `gate_max_attempts`, `gate_verdict` and
`gate_feedback`; the dashboard shows it distinctly and opens it on its own page.

### Recipe: gate a phase

```yaml
version: 1
defaults:
  model: opencode-go/deepseek-v4.1-flash
  timeout_s: 1800

tasks:
  - id: designer
    agent: designer
    prompt: "Produce ./src/repo/docs/ui-design.md."
    resources:
      - {type: git, id: repo, with: {repo: REPO, path: repo, worktree: true,
          ref: main, branch: "hiveling/{run}/{task}", publish: push}}

  - id: coder                              # starts only once the design is VALID
    agent: coder
    depends_on: [designer-gate]            # depend on the GATE
    prompt: "Implement ./src/repo/docs/ui-design.md."
    resources:
      - {type: git, id: repo, with: {repo: REPO, path: repo, worktree: true,
          ref: "hiveling/{run}/designer", publish: push}}

gates:
  - id: designer-gate
    tasks: [designer]
    prompt: >
      Reject if docs/ui-design.md contradicts itself, leaves a flow unreachable,
      or omits a required state (empty/loading/error/success) or the markup the
      implementer must produce.
    max_attempts: 5
```

There is no separate `designer-fix` task: a rejection re-runs `designer` with
the gate's corrections injected. If you prefer a distinct fixer agent, use
`update_run_plan` to add one after a rejection and `resume_run`, or split the
work into two gated phases.

## Running and supervising (the agentic loop)

```
run_plan(plan="name" | plan_yaml="...") -> run_id      # autonomous from here
ev = wait_for_run(run_id, until="terminal", timeout_s=600)
if ev.timed_out: wait again / inspect get_run(run_id)
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

- **Gates make the run self-driving**: a rejection does *not* end the run; the
  run re-runs the gated tasks and the gate (bounded) on its own. `wait_for_run`
  returns only when the whole pipeline is finished.
- **Fail fast**: the first failed task (including a gate that exhausted its
  attempts) stops new dispatches, but tasks already running finish; the rest are
  `skipped` (`skip_reason`) or `canceled`. After a failure, `get_run` shows
  exactly what ran and what did not.
- **`resume_run`** re-arms a finished run: failed + skipped tasks are reset to
  `pending` (previous error kept in `last_error`) and it re-reads the plan
  snapshot. Use it after `update_run_plan`.
- **Live edits** (`update_run_plan`) apply **between tasks**; a running task is
  never interrupted. New tasks are scheduled; removed pending tasks are canceled.
- **`cancel_task`** stops one task and the run continues; **`cancel_run`** stops
  everything.
- Diagnose before adjusting: `get_task` gives the result text, the error, the
  tail of OpenCode events and (when present) `commits` / `merge` / `artifacts`.
  Use `events_tail_lines=0` to skip the log.

## Recipes for an agentic dev loop

- **One concern per task.** Small, bounded prompts; let the DAG express the plan.
- **Branch per task, deterministic:** `branch: "hiveling/{run}/{task}"` +
  `publish: push`. `{run}` and `{task}` are the only substitutions (task-local).
  With `worktree: true` the prompt must point at `./src/repo`; with
  `worktree: false` it is `./repo`.
- **`publish: push` is a contract.** The task must produce a change; if it edits
  nothing (and merges nothing in prepare) the task **fails**. A read-only task
  that consumes a branch (e.g. a verify task) must use `publish: none` (or
  `commit`). A **writer reviewer** that writes `docs/reviews/<phase>-review.md`
  does modify the tree, so it uses `publish: push`/`commit` — and needs the
  `edit` carve-out for that path, or the write is denied.
- **Do not ask the agent to commit.** The `git` provider runs `git add -A` +
  `git commit` + push at finalize (Python, no shell), so a prompt that says
  "commit on the current branch" only invites the agent to fight shell quoting
  (worst on Windows) — often by writing a message file into the OS temp dir.
  Ask for **files**; use `with: {commit_message: "…"}` if you want a specific
  message.
- **Never write outside the checkout.** The workspace, not the OS temp dir, is
  what gets committed. Scratch files must live under `./src/<id>` (the worker
  also grants the platform temp dir by default, but nothing there is published).
- **Permissions come from the injected config, not a flag.** There is no `auto`
  option and the worker never passes `--auto`. A task may only do what the
  effective `opencode.json` and its agent `permissions` allow.
- **Consume a producer's branch** by naming it + explicit ordering:

  ```yaml
  - id: test
    depends_on: [build]
    resources:
      - {type: git, id: repo, with: {repo: REPO, path: repo, worktree: true,
          ref: "hiveling/{run}/build", branch: "hiveling/{run}/test", publish: push}}
  ```

- **Integrate several branches** with `merge: [refs]`: it merges into `branch`
  during prepare and resolves conflicts with a nested OpenCode run. Each merged
  ref must already be pushed, so list its producer in `depends_on`; the base
  branch should be one the others already share. Read
  `status.json.merge` / `events.jsonl` (`hiveling.merge`) for the result.
- **Keep reports** with `artifacts: {paths: ["reports/**"], git: true}`.
- **Pick the environment** with `requirements` (e.g. `os: windows`); a follow-up
  can run on a different OS as long as it consumes a pushed branch.

## What actually stops a run

| Stops the run | Does not stop the run |
| --- | --- |
| A task ending `failed` (process, publish, no matching worker, timeout) | A task whose `result_text` says the work is wrong |
| A **gate** `failed` after `max_attempts` negative evaluations | A gate rejection (it re-runs the work instead) |
| `cancel_run` (whole run); `cancel_task` (one task, dependents skip) | A verdict written in a file nobody reads |
| A dependency that did not succeed → dependent `skipped` | |

Without a gate, the orchestrator is the only gate: read `get_task(...).result`
and decide. Prefer declaring a gate over hand-driving the loop.

## Read-only reviewer vs gate

Gates are read-only by construction (the server owns the agent and its
permissions) and need **no artifact**: Hiveling reads the verdict from the job's
`result_text` and is the channel to the next phase.

A **project reviewer agent** may still write a durable report — by convention
`docs/reviews/<phase>-review.md`, with a machine-readable header
`VERDICT:` / `PHASE:` / `FINDINGS:` — by adding a narrow `edit` carve-out after
its catch-all deny (agent rules are appended last and win):

```yaml
permissions:
  - {action: edit, resource: "*", effect: deny}
  - {action: edit, resource: "*docs/reviews/*", effect: allow}
```

That report is for humans and for a fix task to consume; it is **not** the gate.
A committed report stops nothing (see *What actually stops a run*), so always
also put a gate over the reviewed work — and include the reviewer task in the
gate's `tasks` if a rejection must re-run the report too (a rejection re-runs
the gate's `tasks`, nothing else).

If you do not need a durable report, skip the reviewer agent entirely: the gate
already carries the verdict, and its answer is saved in the task history
(`result.txt`) and shown in the dashboard.

## Decomposition lessons (pre-seed these)

The PULSE-1 reviews caught work items the plan never had. Add them up front; each
is a one-line addition to a producer prompt or a work item, and each is a review
finding if omitted:

- **Test discovery / `__init__.py`.** A `tests/` package without `__init__.py`
  can make `python -m unittest discover` report `Ran 0 tests` (exit 5) — a
  green-looking run that tested nothing.
- **Explicit UI contract.** "Follow `docs/ui-design.md`" is not enough for a
  coder: pin interactions, per-screen states
  (empty/loading/populated/error/success), the link/row markup, and the refresh
  strategy.
- **Security guard.** Static-file serving needs an explicit path-traversal guard
  (`../`, percent-encoding), with a test confined to the static root.
- **SQLite connection / threading model.** `ThreadingHTTPServer` + one shared
  `sqlite3.Connection` raises `ProgrammingError` across threads (→ 500); decide
  the model and add a busy timeout / WAL for write contention.
- **Input validation order.** Freeze the precedence between `400` (bad body) and
  `404` (unknown id), and the rounding/format rules shared by JSON and CSV.

## Pitfalls

- **Treating a review task as a gate.** A `CHANGES_REQUESTED`/`REJECTED` in
  `result_text` is still `succeeded`; its consumers run. Use a `gate`.
- **Consuming a gated task directly.** The plan is rejected unless the consumer
  also depends on the gate — do not try to bypass it.
- **Gating on content the task did not download.** A `download: none` task gives
  the gate only its report; set `download: modified`/`all`.
- **Expecting the gate to inspect a branch.** It runs with no repository; the
  evidence is the injected `_hiveling/` payload.
- Assuming tasks share a working directory: they don't. Use `inputs_from` (files)
  or a `git` resource (code).
- Pointing prompts at the workspace root instead of the checkout: `publish: push`
  fails with "nothing to push" (or silently edits the wrong tree).
- Asking the agent to `git commit`: unnecessary (the provider commits) and it
  triggers shell-quoting workarounds that spill into the OS temp dir.
- Using `publish: push` on a task that does not modify the branch: use
  `publish: none`/`commit`. A **writer reviewer** is the opposite case: it
  writes a report, so it must publish *and* have the `edit` carve-out for
  `docs/reviews/…`.
- Using a path not under the worker's `path_roots` (see `list_workers`).
- `command` (arbitrary shell) is refused unless the worker advertises it.
- Long prompts are command-line arguments on the worker (Windows limits): keep
  prompts concise; use `prompt_file`.
- Forgetting `depends_on` on a consumer of a produced branch or a gate.
- Calling `update_run_plan` with invalid YAML: it is validated and rejected.

## Checklist

1. `get_plan_schema()` and `list_workers()` called.
2. Every task has a unique `id` and a `prompt`/`prompt_file`; every gate has a
   unique `id`, a non-empty `tasks` list and a `prompt`.
3. Requirements match advertised capabilities; paths under `path_roots`.
4. Prompts reference the checkout (`./src/<id>` with `worktree: true`, else
   `./<path>`), not the workspace root.
5. Read-only tasks use `publish: none`; producers push; a **writer reviewer**
   publishes its report and has the `edit` carve-out. Gates set no resources and
   no `agent`.
6. Consumers list their producers in `depends_on`; **consumers of a gated task
   depend on the gate**.
7. Gated tasks download their deliverable (`download: modified`/`all`), or the
   gate only sees their report.
8. `max_attempts` is sane for the phase (default 20 evaluations before the run
   fails).
9. Every phase whose output must be judged has a **gate** (a review task alone
   does not enforce anything); if a reviewer writes a report, it is listed in
   the gate's `tasks` too.
10. No secret value in the plan; no invented path or worker.
11. Run, then `wait_for_run(until="terminal")`; on failure `get_task` →
    `update_run_plan` → `resume_run`; `cancel_run` to abort.

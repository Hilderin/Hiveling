# Design: reproducible worker environments

Status: **Draft** · Last updated: 2026-09-26 · Relates to `todo.md`
("Reproducible OpenCode runtime config", "Worker capabilities / task
requirements", "Add remote setup of an OpenCode config").

## 1. Purpose

Today a Hiveling worker runs `opencode run` in a throwaway directory and the
server exchanges files with it over HTTP as zip archives. This works, but it
does not let a plan describe **where the code lives** or **what environment
surrounds it** (repositories, branches, local databases, services, toolchains,
tokens, OpenCode config). Teams therefore cannot make a task reproducible, and
artifact exchange (zip) is fragile for anything non-trivial.

This document specifies a **generic, pluggable environment provisioning model**
that:

- covers both a greenfield "one task = one worktree" reality and a legacy
  "canonical folders on a specific machine, multi-repo, local DB, Windows"
  reality;
- does not hardcode git, docker or databases into the orchestrator core;
- stays backward compatible with today's plans (no behavior change by default);
- gives other teams a supported extension point (docker/k8s/DB/whatever)
  without forking Hiveling.

## 2. Problem framing

Two axes are usually conflated. They are independent:

1. **Where does the code live?** isolated clone/worktree vs canonical path
   already present on the machine.
2. **What environment surrounds the code?** databases, services, native
   toolchains, licenses, environment variables, secrets.

- "one worker = one workspace" answers axis 1 and *assumes* axis 2 is also in
  the repo (devcontainer/toolchain config). Good for greenfield, wrong for
  legacy.
- "temp workspace + mounted folders" answers axis 1 with canonical paths and
  leaves axis 2 to the machine state.

The general abstraction is therefore neither worktree nor mounts:

> A task executes inside a **materialized environment** made of **resources**.
> Before the task, resources are **provisioned**; after, they are **published**;
> and the task declares **requirements** so the scheduler picks a capable
> worker. **Git is one provider among many**, not a core concept.

## 3. Goals / non-goals

### Goals

- G1. A plan/task can declare the repositories, folders, env vars, secrets and
  services it needs, in a typed and validated way.
- G2. A worker can materialize those resources before OpenCode runs and publish
  results (commits/pushes, changed files, artifacts) after.
- G3. Tasks can declare **requirements**; the server only dispatches them to
  workers that advertise matching **capabilities**.
- G4. A worker can inject a per-job OpenCode runtime config (agents, skills,
  MCP, permissions) with a well-defined layering.
- G5. Artifact exchange becomes pluggable: zip stays the default; git refs and
  shared paths become peers.
- G6. Existing plans keep working unchanged.
- G7. New environment kinds are added as **providers**, without touching the
  orchestrator core.

### Non-goals (this iteration)

- N1. Building the full provider marketplace/plugin API in v1.
- N2. Container/VM snapshots (kept as a future `docker` provider).
- N3. Long-lived environment *leases* reused across tasks (per-task is v1).
- N4. Multi-tenant security hardening (covered only as a threat-model section).

## 4. Baseline (what exists today)

Anchors in the current code:

- `worker/app/executor.py::execute` creates `job.workdir`, snapshots before,
  runs `opencode run` with `cwd=workdir`, snapshots after and diffs. **This is
  the single insertion point for `prepare` / `finalize`.**
- `worker/app/state.py::Job.workdir = job.dir / "work"`, job persisted as
  `status.json`.
- `worker/app/main.py::JobSpec` is the wire contract from server to worker;
  `/health` reports `busy`, `active_job`, `opencode_bin`.
- `server/app/orchestrator.py::_execute_on` builds the request, uploads a zip of
  inputs, starts the job, polls, then `_finish_job` downloads a zip of outputs
  into `.data/history/<task>/<run>/files/`.
- `server/app/orchestrator.py::_resolve_inputs` resolves `files` (plan-relative
  globs) and `inputs_from` (previous tasks' downloaded files).
- `server/app/plan.py::Task` and `server/app/plan_schema.py` define the plan
  contract (`depends_on`, `inputs_from`, `files`, `env`, `download`).
- Workers are configured server-side in `workers.yaml` and hot-reloaded by
  `server/app/workers.py`; `probe_workers` reads `/health`.

Nothing is git-aware, nothing injects OpenCode config, and workers do not
advertise capabilities.

## 5. Core model

```
plan / task
  ├── requirements ──▶ scheduler matches against worker capabilities
  └── environment
        ├── resources[] ──▶ worker providers (prepare / finalize / teardown)
        ├── opencode     ──▶ per-job runtime config injection
        └── artifacts    ──▶ how results are collected (zip | git | paths)
```

Five layers, each independently shippable:

- **Layer A — Capabilities & requirements.** Heterogeneous fleets only work if
  tasks say what they need and workers say what they have.
- **Layer B — Provider contract.** A small lifecycle interface; git, paths,
  env, secrets, commands (and later docker/services) are implementations.
- **Layer C — OpenCode config injection.** Per-job config dir + layering.
- **Layer D — Artifact channels.** zip (default), git refs, shared paths. Git
  is consumed through resources: a task names a producer's branch as its `ref`
  and orders with `depends_on` (7.5); `inputs_from` stays the non-git file
  channel only.
- **Layer E — Compatibility.** A plan without `requirements` / `resources` /
  `opencode` reproduces today's behavior exactly.

### 5.1 Terminology

| Term | Meaning |
| --- | --- |
| **Capability** | A property a worker advertises (`os`, `tags`, `providers`, tools). |
| **Requirement** | A property a task requires; matched by subset against capabilities. |
| **Resource** | A typed item a task needs materialized (`git`, `path`, `env`, …). |
| **Provider** | Worker-side implementation that validates/prepares/finalizes a resource. |
| **Environment** | The set of resources + OpenCode config applied around a job. |
| **Artifact** | A result to collect (changed files, commits, reports, dumps). |
| **Published ref** | The branch/sha/remote a `git` resource records at finalize; a later task consumes it by naming that branch as its `ref` (7.5). |

## 6. Layer A — Capabilities and requirements

### 6.1 Worker capabilities

A new optional worker config file (default `<worker-config-dir>/capabilities.yaml`,
flagged on the CLI) plus auto-detection:

```yaml
# capabilities.yaml (worker-side)
tags: [legacy, mssql, gui]
labels:
  site: office-mtl
tools: [dotnet, node, git]
providers: [ephemeral, git, path, env, secret, command]  # allowlist
path_roots:                       # hard limit for any plan-declared path
  - "D:\\src"
  - "D:\\data"
  - /opt/acme
  - ~/shared
  # ["*"] explicitly disables the limit (opt-out, not the default)
```

Auto-detected and merged (auto values cannot be overridden into a lie, only
extended): `os`, `arch`, `providers_available` (those actually installed),
`opencode_bin`. Advertised through `GET /health`:

```json
{
  "status": "ok", "busy": false, "active_job": null,
  "opencode_bin": "/usr/bin/opencode",
  "capabilities": {
    "os": "windows", "arch": "x64",
    "tags": ["legacy", "mssql"],
    "labels": {"site": "office-mtl"},
    "tools": ["dotnet", "node", "git"],
    "providers": ["ephemeral", "git", "path", "env", "secret"]
  }
}
```

`server/app/workers.py::probe_workers` gains a `capabilities` field; the server
caches them and refreshes on demand.

### 6.2 Task requirements

```yaml
requirements:
  os: windows            # exact match
  tags: [mssql]          # subset of worker tags
  providers: [git]       # subset of worker providers
  labels: {site: office-mtl}  # exact match per key
```

Matching rules:

- A task with **no requirements** runs on any free worker (today's behavior).
- A requested capability the worker does not advertise ⇒ not eligible.
- Empty/absent worker capabilities ⇒ the worker is only eligible for tasks with
  no requirements (conservative, no silent mismatch).

The scheduler (`orchestrator._available_clients`) filters free clients by
matching requirements instead of choosing blindly. `list_workers` and the
dashboard surface capabilities so a plan author can see what exists.

## 7. Layer B — Provider contract

### 7.1 Resource spec

Resources are typed and validated. Plan syntax:

```yaml
resources:
  - type: git
    id: api                       # optional; defaults to "<type>-<index>"
    when: {os: windows}           # optional guard
    with:
      repo: git@github.com:acme/api.git
      ref: develop
```

- `type` selects a provider.
- `id` is stable within a task; used in logs, status and artifacts.
- `with` is provider-specific and validated by `Provider.validate`.
- `when` optionally restricts a resource to workers matching a predicate
  (useful when one plan targets several OSes).
- Resources **merge by `id`** across `defaults.resources` and the task: a
  same-`id` task resource overrides the default one, new `id`s append. This lets
  a task change one property (e.g. its `ref`) without repeating the whole
  resource block.

### 7.2 Provider interface (worker-side)

```python
class Provider(Protocol):
    name: str

    def capabilities(self) -> dict: ...
    def validate(self, resource: Resource, ctx: Context) -> None: ...
    def prepare(self, resource: Resource, ctx: Context) -> Prepared: ...
    def finalize(self, resource, ctx, prepared, result: JobResult) -> FinalizeResult: ...
    def teardown(self, resource, ctx, prepared) -> None: ...   # always runs
```

`Context` carries: `workspace` (per-job temp dir), `job_dir`, `run_id`,
`task_id`, `plan_dir`, `logger`, `capabilities`, `secrets` (resolver), and a
`resolved_env` accumulator.

`Prepared` carries what the worker must apply around OpenCode:

```python
@dataclass
class Prepared:
    env: dict[str, str] = {}            # merged into the subprocess env
    paths: list[ExposedPath] = []       # path + mode + visible-to-opencode
    opencode: ConfigFragment | None = None   # ConfigFragments merged in layer C
    lock: LockHandle | None = None
    reports: dict = {}                  # e.g. resolved commit sha
    artifacts: list[ArtifactRef] = []   # provider-declared outputs
```

`FinalizeResult` carries: `changed_files`, `commits` (repo, branch, sha),
`published` (pushed remote refs), `artifacts`, `errors`.

### 7.3 Worker lifecycle

Around `executor.execute`:

```
resolve resources (defaults + task)           # fails the job on validate error
acquire locks in deterministic resource order
for resource in order:  prepared += provider.prepare(...)
inject env + OpenCode config (layer C)
try:
    run opencode (existing execute body)
finally:
    for resource in reverse: provider.finalize(...)   # publish on success/failure
    for resource in reverse: provider.teardown(...)   # always (release locks, stop services)
```

Failure semantics:

- A `validate`/`prepare` failure ⇒ the job fails **before** OpenCode runs, with
  a structured `environment_error`. No partial run.
- A `finalize` failure ⇒ the job is reported `failed` (even if OpenCode
  succeeded). There is **no warning mode**: a failed commit/push is a real
  failure. The OpenCode result is still kept in history.
- `teardown` never fails the job; it logs.

### 7.4 Provider ordering and dependencies

v1: prepare in declaration order, finalize/teardown in reverse. A provider may
declare `after: [resource-id]` in `with`; the worker topologically sorts. This
covers "start DB before git checkout" without a general graph engine.

### 7.5 Run and task substitutions

Resource **string** options may embed `{run}` and `{task}`, resolved by the
server before dispatch:

| Substitution | Resolves to |
| --- | --- |
| `{run}` | the run id |
| `{task}` | the current task id |

They exist mainly to build unique branch names, e.g.
`branch: "hiveling/{run}/{task}"`. They are local to the task; there is **no
cross-task substitution**.

**Dependencies are always explicit.** Consuming another task's output means
writing that task's predictable branch name and listing the dependency:

```yaml
tasks:
  - id: build
    resources:
      - {type: git, with: {repo: REPO, ref: develop,
                           branch: "hiveling/{run}/build", publish: push}}
  - id: test
    depends_on: [build]                                   # explicit ordering
    resources:
      - {type: git, with: {repo: REPO,
                           ref: "hiveling/{run}/build",   # the producer's branch
                           branch: "hiveling/{run}/test", publish: push}}
```

Rationale: a task has **no `branch` property** — it may declare several `git`
resources, each with its own branch — so a `{task.branch}` reference would be
ambiguous. Branch names are therefore written out in the plan and `depends_on`
carries the ordering, keeping the graph fully readable.

`{...}` is distinct from the `env` provider's `${ENV}` expansion; resource option
values are otherwise literal.

## 8. Built-in providers (v1)

| Type | Role | Key options |
| --- | --- | --- |
| `ephemeral` | Today's behavior: fresh dir, zip in/out | `clean`, `download` |
| `git` | clone/reset/checkout, optional worktree, commit/push | `repo, path, worktree, ref, branch, branch_mode, push_to, clean, cache, publish, remote` |
| `path` | Expose an existing folder read-only or read-write | `path, mode, visible` |
| `env` | Inject environment variables | `vars` |
| `secret` | Inject secrets by name from the worker store | `name, as` |
| `command` | Escape hatch: run scripts at prepare/finalize | `prepare, finalize, shell, env` |

Future (documented, not built here): `docker`/`compose`, `service`/`process`,
`db` (reset/seed), `volume`.

### 8.1 `git`

The provider that subsumes the "one worker = one worktree" idea **and** the
legacy multi-repo idea.

```yaml
- type: git
  id: app
  with:
    repo: git@github.com:acme/app.git
    path: /src/app                  # absolute, or relative to the location dir
    worktree: false                 # default; true = per-task worktree (parallel)
    ref: develop                    # START point (branch/tag/sha)
    branch: "hiveling/{run}/{task}" # TARGET branch the task commits/pushes to
    branch_mode: create             # create | reuse | recreate
    push_to: null                   # remote ref to push (default: same as branch)
    set_upstream: true              # pass -u on push
    force: false                    # never force-push unless explicit
    clean: git                      # none | git | full
    cache: [node_modules, .venv]    # preserved across `clean: full`
    publish: push                   # none | commit | push
    remote: origin
```

Semantics:

- **`ref` vs `branch`.** `ref` is the **start point** (any branch, tag or sha).
  `branch` is the **target branch** the task commits to. They are independent,
  so "branch from `develop`, push to my own branch" is simply `ref: develop` +
  `branch: hiveling/{run}/{task}`.
- **`branch_mode`.** `create` (default) makes `branch` from `ref` and fails if
  it already exists; `reuse` fetches and checks out the existing `branch` so the
  task's commits stack on top (a non-force push is then fast-forward only);
  `recreate` deletes the local branch and re-creates it from `ref`.
- **canonical clone model** (`worktree: false`): the provider owns a durable
  clone at `path` (clone if missing, `git fetch`), then checks out `ref` /
  `branch`, optionally `clean`. OpenCode works in place. Used for legacy
  multi-repo where absolute paths matter.
- **worktree model** (`worktree: true`, opt-in): a durable bare/clone lives at
  `path` (or under the worker's cache dir); the provider creates a per-task
  worktree from `ref` on `branch` under the location dir, so parallel tasks on
  the same repo never collide and canonical paths are untouched. Use it when
  several tasks touch one repo in parallel; the default is `false` (canonical
  clone / in-place).
- **publish**: at finalize, `commit` creates a commit on `branch` from changed
  files; `push` also pushes it to `remote` — to `push_to` if set, otherwise to
  the same-named branch. `set_upstream: true` adds `-u`. `force` defaults to
  `false`; a force push requires `force: true` and is logged and flagged. The
  resolved branch, sha and remote are recorded in `status.json`/history so other
  tasks can reference them (7.5) and the dashboard can display them.
- **clean**: `none` keeps the tree, `git` restores tracked files
  (`reset --hard` + `clean -fd`, cached paths preserved), `full` removes the
  worktree/clone and re-creates it.
- **cache**: named paths preserved across `clean: full` (build caches). This is
  essential to keep reset cheap.
- **config provenance**: the provider reports the repo's `opencode.json(c)`,
  `.opencode/` and `AGENTS.md` so the worker can aggregate them (9.4). A repo
  placed *below* the location dir does not expose its `.opencode` to upward
  discovery, so aggregation is required even in the worktree layout (except
  `AGENTS.md`, which OpenCode lazily discovers downward).

Conflicts and parallelism: each task commits to its own branch, and with
`worktree: true` each also gets its own working tree, so two tasks never write
the same tree. A task can consume a producer's branch as its `ref` (7.5).

**Integration (`merge: [refs]`).** A `git` resource can merge refs into
`branch` during `prepare`, before the task's OpenCode run. A conflict does not
fail the prepare: the worker resolves it with a nested OpenCode run using a
generic built-in prompt (`worker/app/providers/git.py`), then commits one merge
commit per ref. The result is published by the normal `finalize`. Traceability
lives in the task's own log: synthetic `hiveling.merge` events in
`events.jsonl` and a `merge` field in `status.json`/`run.json`. Only `merge`
(no rebase), no configurable strategy or message.

### 8.2 `path`

Exposes a canonical folder without git semantics.

```yaml
- type: path
  with: {path: "D:\\data\\fixtures", mode: ro, visible: true}
```

`visible: true` means the provider adds `external_directory` `permissions`
rules so OpenCode may read/edit the path, and reports any `.opencode/` /
`AGENTS.md` it contains as config sources (9.4). `mode: rw|ro`. Used for local
DB data dirs, fixtures, shared caches. External paths are absolute and are not
ancestors of the location dir, so their config is aggregated, not discovered.

### 8.3 `env` and `secret`

```yaml
- {type: env,    with: {vars: {ASPNETCORE_ENVIRONMENT: Development}}}
- {type: secret, with: {name: NUGET_PAT, as: NUGET_TOKEN}}
```

- `env` values may use `${...}` expansion from the worker's own environment but
  are otherwise literal.
- `secret` **never carries a value in the plan**. The worker resolves `name`
  from its secret store (env var, file, OS keyring; configured on the worker)
  and exposes it as `as`. A missing secret fails `prepare` (fail fast) unless
  `required: false`.

### 8.4 `command`

Escape hatch for anything without a provider yet (docker, k8s, custom DB
reset). Explicit opt-in via the worker provider allowlist.

```yaml
- type: command
  with:
    prepare: ./scripts/reset-db.sh
    finalize: ./scripts/collect.sh
    shell: bash            # or: cmd / powershell on Windows
    env: {CI: "1"}
```

Runs with `cwd` = job workspace (or `path`), inherits the job env plus resolved
secrets. Stdout/stderr captured into history. Documented as lower-level and
less portable than a dedicated provider.

## 9. Layer C — OpenCode runtime config injection

Goal: a task runs with a **known** OpenCode config (agents, skills, MCP,
permissions, instructions) regardless of the worker's global state, while still
letting a repo ship its own config — and **without the plan author rewriting
agent and skill bodies inline in every task**. The plan should be able to
*point at* existing worker-local directories instead.

### 9.0 What OpenCode V2 discovers (facts that drive the design)

- **Config files.** Project `opencode.json(c)` or `.opencode/opencode.json(c)`
  are discovered from the working directory up to the filesystem root; global
  config is `~/.config/opencode/opencode.json(c)`. Discovered `.opencode/`
  config overrides direct config; global is the lowest precedence. Merging is
  additive (non-conflicting settings are preserved).
- **Agents.** Markdown files at `~/.config/opencode/agents/<name>.md` and
  `<project>/.opencode/agents/<name>.md` (V2 also discovers legacy `agent/`,
  `mode/`, `modes/`). A nested relative path becomes part of the agent id
  (`.opencode/agents/team/reviewer.md` → `team/reviewer`). There is **no config
  array of agent directories** — discovery is file-based only.
- **Skills.** Discovered under `~/.config/opencode/skills` and
  `<project>/.opencode/skills` (V2 also `skill/`, `.claude/skills`,
  `.agents/skills`). In addition, the **`skills` config array accepts local
  directories (relative paths resolve from the active working directory),
  `~/` paths, absolute paths and HTTP catalogs**.
- **Instructions.** Only `AGENTS.md` is loaded: the global
  `~/.config/opencode/AGENTS.md` plus every project `AGENTS.md` from the
  working directory up. The `instructions` config array is **accepted but not
  resolved in V2**. Multiple `AGENTS.md` files are **combined, not
  overridden**.
- **Precedence.** Skills and `AGENTS.md` are additive; for a duplicate skill id
  the later-registered source wins.

Consequences for the worker:

- **Skills can be referenced without symlinks** (add absolute paths to the
  generated `skills` array).
- **Agents and `AGENTS.md` must be placed or linked** where discovery looks.

### 9.1 Anchor: a per-job location directory

Per-job config injection is **project-scoped, relative to OpenCode's working
directory**. Global-config relocation via `XDG_CONFIG_HOME` is *not* relied on:
its effect on the OpenCode **server** config is not documented (it is documented
for the CLI's `cli.json`), and redefining `HOME`/`USERPROFILE` would break
OpenCode auth, git/SSH credentials and caches. So the worker uses only
documented project discovery:

- It creates an isolated **location directory** `<job>/location/` and runs
  OpenCode with `cwd` = that directory. The location dir is **never a repo
  working tree**.
- Resource working trees are materialized **under** it (`location/src/<id>`) in
  the worktree model, or stay at canonical paths elsewhere (the location dir
  remains a clean temp dir) in the legacy model.
- All per-job config is injected at the **location root** as project config:
  `location/opencode.json(c)`, `location/.opencode/agents/*.md`,
  `location/.opencode/skills` (or absolute paths in the `skills` array), and
  `location/AGENTS.md`.

Because repos are never ancestors of the location dir (worktree layout) or are
outside it (legacy layout), their project config is not auto-discovered; the
worker **aggregates it explicitly** as sources (9.4). This is deterministic and
behaves identically on every platform.

Caveat: OpenCode discovers project config from the working directory up to the
filesystem root, and the machine's global `~/.config/opencode` still applies.
Both are treated as the **worker's baseline**; the worker keeps its data dir
free of ancestor `opencode.json(c)` to avoid surprises.

### 9.2 Sources: inline and path

An `opencode` block has two kinds of entries: **inline** content (as before) and
**path** references resolved on the worker. Path references are the key to
reuse: a team keeps one directory of agents/skills/`AGENTS.md` on the worker and
every task points at it.

```yaml
opencode:
  # --- inline content (highest precedence, optional) ---
  config:                       # JSON/JSONC fragment merged into opencode.json
    $schema: https://opencode.ai/config.json
    default_agent: build
    permissions:
      - {action: edit, resource: "*", effect: allow}
      - {action: shell, resource: "git push *", effect: ask}
    mcp:
      servers:
        hiveling: {type: remote, url: "http://127.0.0.1:8080/mcp", oauth: false}
  agents:                       # agent id -> markdown body / frontmatter
    reviewer: "You are a strict code reviewer. ..."
  skills:
    - name: run-tests
      content: "..."
  # --- path references on the worker (new) ---
  from:                         # bundle directories (see 9.3)
    - /opt/acme/opencode
  agents_paths:                 # directories of agent Markdown files
    - /opt/acme/team-agents
  skills_paths:                 # directories of skills (SKILL.md / <name>.md)
    - ~/shared/opencode-skills
  agents_md:                    # AGENTS.md files, concatenated in priority order
    - /opt/acme/AGENTS.md
```

The verbose, per-source form exposes `mode`, `priority`, `optional` and `when`:

```yaml
opencode:
  sources:
    - {kind: bundle,    path: /opt/acme/opencode,    mode: symlink, priority: 10}
    - {kind: agents,    path: /opt/acme/team-agents, mode: symlink}
    - {kind: skills,    path: ~/shared/skills,       mode: reference}
    - {kind: agents_md, path: /opt/acme/AGENTS.md,   mode: concat, priority: 20}
```

`kind` values: `bundle`, `config`, `agents`, `skills`, `agents_md`. Shorthands
(`from`, `agents_paths`, `skills_paths`, `agents_md`) expand to sources with
sensible defaults.

### 9.3 Materialization: symlink, copy or reference

Each source is materialized into the job's location root by the environment setup,
**before** OpenCode starts. Symlinks are the default because they are cheap and
keep the source live; Windows uses directory **junctions** (unprivileged) and a
copy fallback.

| `kind` | Target | Default materialization |
| --- | --- | --- |
| `config` | merged into `location/opencode.json(c)` | deep-merge |
| `agents` | `location/.opencode/agents/` | symlink each `*.md`, subpaths preserved (junction for dirs on Windows) |
| `skills` | `skills: [<abs path>]` in the generated config | **reference** (no symlink); symlink into `location/.opencode/skills/` as fallback |
| `agents_md` | `location/AGENTS.md` | **concat** all declared files in priority order |
| `bundle` | expands to the four kinds above | per contained item |

`mode`:

- `symlink` — create a symlink (POSIX) / junction for directories (Windows).
- `reference` — do not touch the filesystem; point config at the path
  (skills only).
- `concat` — combine into a single generated file (AGENTS.md only).
- `copy` — copy files; the portable fallback.
- `auto` (default) — symlink, fall back to copy on failure.

Notes:

- **Preference order**: use the `skills` **config array** for skills (no
  filesystem change), **symlink/junction** for agents, and **concatenation** for
  `AGENTS.md`. Copy is the fallback whenever a link is unavailable. This keeps
  the cheapest and most robust mechanism for each kind.
- **Skills need no symlink** in V2: appending the absolute source directory to
  the generated `skills` array is enough and is the most robust path. Use
  symlink/copy only if a future version stops honoring the array.
- **Agents need a filesystem presence** because there is no config array for
  agent directories. Symlinking the whole directory would shadow plan-inline
  agents, so the setup **links the individual files** into the target
  `agents/` directory (mkdir the parents, then symlink each file).
- **AGENTS.md cannot be "overridden"**, only combined, and there is one path
  per scope. The setup therefore **concatenates** declared sources into
  `location/AGENTS.md`, in `priority` order, each behind a small
  `<!-- source: <path> -->` marker. This is the unified mechanism for the
  worker baseline bundle, plan-declared `agents_md`, and repo-owned files: a repo
  `AGENTS.md` that lives *below* the location (worktree layout) is
  auto-discovered by OpenCode and **not** duplicated into the concatenation,
  while one that lives *outside* it (canonical layout) is reported by its
  provider and **is** concatenated. The setup never overwrites a file inside a
  repo working tree.

### 9.4 Layering and merge order

Applied from lowest to highest precedence:

1. **Worker default bundle** — the team's baseline OpenCode config on that
   machine (`~hiveling/opencode/` or the worker config dir).
2. **Repo provenance** — config/agents/skills/AGENTS.md from a resource the task
   exposes. Because repos are never ancestors of the location dir (worktree
   layout) or are external (canonical layout), the `git`/`path` provider
   *reports* the repo's `.opencode/`, `opencode.json(c)` and `AGENTS.md` as
   sources and the worker aggregates them: `.opencode` config/agents/skills are
   always aggregated; a repo `AGENTS.md` below the location is left to OpenCode
   discovery and not duplicated.
3. **Plan `sources`** (path references), in `priority` order then declaration
   order.
4. **Plan inline** `config` / `agents` / `skills`, then `env`/`secret`
   resources.

Scalars override (later wins); maps deep-merge; the `skills` array
concatenates; `AGENTS.md` concatenates.

### 9.5 Security of path sources

- **`path_roots` is enforced by the worker, not asked of the plan.** The plan
  chooses paths freely inside the configured roots; a path that resolves outside
  them (or a worker without `path_roots`) fails `prepare`. This is the
  difference between "the plan declares what it needs" and "the worker decides
  what it will ever expose". `path_roots: ["*"]` is an explicit opt-out, not the
  default.
- The same containment applies to `git.path`, `path.path`, `opencode` source
  paths and a `command` working directory.
- Because *the worker* creates symlinks/junctions, it validates the **real**
  target is inside an allowed root (`realpath`/junction-target check) to prevent
  symlink-escape chains.
- Missing path: fail `prepare` unless the source is `optional: true`.
- Secret-looking files are not special-cased here; keep secrets in the
  `secret` provider, not in agent/skill files.

### 9.6 Example — one shared worker directory, referenced from every task

```yaml
defaults:
  opencode:
    from: [/opt/acme/opencode]        # opencode.json + agents/ + skills/ + AGENTS.md
    agents_paths: [/opt/acme/team-agents]
    skills_paths: [~/shared/opencode-skills]
    agents_md: [/opt/acme/AGENTS.md]
tasks:
  - id: feature
    prompt: "Implement the feature; the team agents and skills are available."
  - id: review
    prompt: "Use the reviewer subagent on the changes."
    depends_on: [feature]
```

The plan carries **paths**, not agent/skill bodies: one place to maintain them,
no duplication across tasks, and no LLM cost spent re-emitting Markdown.

This addresses the `todo.md` items "Reproducible OpenCode runtime config",
"Add remote setup of an OpenCode config (agents, skills, MCP, security/tokens)".

## 10. Layer D — Artifact channels

`download` (zip) becomes one channel among peers. A task's `artifacts` chooses:

```yaml
artifacts:
  download: modified         # zip: modified | all | none (backward compatible)
  git: true                  # include commits/published refs in the task result
  paths: [reports/**]        # extra zip globs beyond the workdir diff
```

- **zip** stays the default and the right channel for non-source artifacts
  (test reports, DB dumps, screenshots) where git is inappropriate.
- **git** records commits/branches; the server stores them and other tasks
  consume them by naming the branch as the `ref` of a `git` resource, with an
  explicit `depends_on` (7.5) — `inputs_from` is not involved.
- **paths** lets a task declare outputs outside the workdir (e.g. a report
  written to a canonical folder).

### 10.1 Git flows through resources, not `inputs_from`

Producing and consuming a git ref is expressed with **resources only**: the
consumer declares the same repo with `ref` referencing the producer task (7.5),
and the dependency is implied. A git variant of `inputs_from` would duplicate
the resource model for no benefit — the resource already names the repo and the
ref, which is simpler and more explicit.

```yaml
tasks:
  - id: build
    resources:
      - {type: git, with: {repo: REPO, ref: develop,
                           branch: "hiveling/{run}/build", publish: push}}
  - id: integrate
    depends_on: [build]
    resources:
      - {type: git, with: {repo: REPO, ref: "hiveling/{run}/build",
                           branch: "hiveling/{run}/integrate", publish: push}}
```

`inputs_from` is kept **only for the non-git (zip/files) channel** — today's
meaning, copying a previous task's downloaded files into the workdir:

```yaml
inputs_from: [tests]                                   # whole downloaded set
inputs_from: [{task: tests, files: ["reports/**"]}]    # a subset
```

A future `files` provider can absorb it, but it is orthogonal to git.

## 11. Reset semantics and caching

"Reset" is overloaded; make it explicit per resource:

| `clean` | Effect |
| --- | --- |
| `none` | Do not touch the tree. |
| `git` | `git reset --hard <ref>` + `git clean -fd`, minus `cache` paths. |
| `full` | Remove and re-create the worktree/clone, minus `cache` paths. |

`cache` names paths preserved across resets (e.g. `node_modules`, `.venv`,
`target`). Database/service resets belong to a dedicated provider (`db`,
`service`), not to `git`.

## 12. Concurrency and locking

- Today one job per worker, so canonical-path resources are effectively safe.
- When worker concurrency grows (> 1) **and** resources may share a canonical
  path (`worktree: false`, `path`, named services), the worker must serialize
  access. Each provider that needs exclusivity returns a `LockHandle` in
  `Prepared`; locks are acquired in deterministic order to avoid deadlock.
- Recommendation: since `worktree` defaults to `false`, enable `worktree: true`
  for repos touched by parallel tasks, so repo contention disappears and only
  genuinely shared resources (DB, port, service) are locked.

## 13. Secrets and security

- **Plans never contain secret values.** `secret` references a name resolved
  worker-side. A pushed plan (possibly authored by an LLM) must be safe to
  store and log.
- **Provider allowlist** in `capabilities.yaml`; an unknown or disallowed
  provider fails validation.
- **`command` requires explicit opt-in** on the worker and is disabled by
  default.
- **Resource paths** are restricted by the worker's `path_roots` (and
  `repo_roots`): a plan may only touch paths inside those roots. It chooses the
  paths, but it cannot escape — a plan cannot expose `/etc` or `C:\Windows`.
  See 9.5.
- **Trust model.** Anyone who can reach a worker can already run OpenCode on
  it, so `command`/`git` do not introduce a fundamentally new trust boundary —
  but they widen the blast radius. For untrusted plan sources, run workers with
  a restricted allowlist and no `command`.
- **Logging.** Resolved secret values must be redacted from `request.json`,
  `status.json`, events and server logs.

## 14. Data model and protocol changes

### 14.1 Wire contract (server → worker)

`worker/app/main.py::JobSpec` gains (all optional; absent = today):

```python
requirements: dict = {}       # echoed for traceability
resources: list[dict] = []     # resolved resource specs
opencode: dict | None = None   # config injection block
artifacts: dict = {}           # download/git/paths
```

Validation errors return `422` (plan/provider) or a structured `409` for
resource contention; `prepare` failures surface in `status.json.error` as
`environment_error`.

### 14.2 Worker health

`/health` adds `capabilities` (section 6.1). `server/app/workers.py` and
`probe_workers` carry it; `list_workers` exposes it to MCP.

### 14.3 Job state and history

`worker/app/state.py::Job` gains:

```python
environment: dict   # per-resource status: prepared/finalized, reports
commits: list       # [{repo, branch, sha, remote, pushed}]
artifacts: list     # provider-declared artifacts
```

`to_status()` includes them, so `_finish_job` records commits into
`history/<task>/<run>/environment.json` and the dashboard/MCP can show them.

### 14.4 Server-side plan parsing

`server/app/plan.py::Task` gains:

```python
requirements: dict = {}
resources: list = []      # list[dict], validated
opencode: dict | None = None
artifacts: dict = {}
```

`server/app/plan_schema.py` gains matching pydantic models (`ResourceInput`
with a discriminated union on `type`, `RequirementsInput`, `OpencodeInput`,
`ArtifactsInput`) so `create_plan` publishes the new schema in `tools/list`.
`OpencodeInput` models both forms: the inline maps (`config`, `agents`,
`skills`) and the path references (`from`, `agents_paths`, `skills_paths`,
`agents_md`, or the verbose `sources` list with `kind` / `mode` / `priority`).

Validation split:

- the **server** validates shape and enums, and the resource `type` against the
  target worker's advertised `providers` **before** dispatch where possible;
- the **worker** validates everything machine-specific: path existence,
  `path_roots` containment, provider-specific `with`, and OpenCode config-source
  materialization (see 9.1 / 9.3).

### 14.5 Plan authoring and cross-plan reuse

**No include system and no named environments** in the plan format. Both add
indirection that ends up confusing the authors — LLMs and humans alike. A plan
stays **explicit and self-contained**: every resource, requirement and
`opencode` source it needs is written out.

Cross-plan reuse is a **tooling concern, not a schema concern**. The assistant
that authors plans (an OpenCode agent guided by a **skill** and the
`get_plan_schema` reference served over MCP) knows the team's repositories,
paths and conventions, and emits the explicit blocks. This keeps the format flat
and inspectable without making authors hand-write boilerplate.

Within a single plan, the existing `defaults` block may still set shared values
(model, `env`, and `resources` merged by `id`) — that is plain task inheritance,
not an include.

## 15. Examples

### 15.1 Legacy multi-repo on a Windows box

```yaml
version: 1
defaults:
  requirements: {os: windows, tags: [legacy]}
  model: opencode-go/deepseek-v4.1-flash
  resources:
    - type: git
      id: api
      with:
        repo: git@ssh.dev.azure.com:v3/acme/Api
        path: "D:\\src\\Api"
        ref: develop
        clean: git
        publish: push
    - type: git
      id: web
      with:
        repo: git@ssh.dev.azure.com:v3/acme/Web
        path: "D:\\src\\Web"
        ref: develop
        clean: git
        publish: push
    - {type: path,   with: {path: "D:\\data\\fixtures", mode: ro}}
    - {type: env,    with: {vars: {ASPNETCORE_ENVIRONMENT: Development}}}
    - {type: secret, with: {name: NUGET_PAT, as: NUGET_TOKEN}}
tasks:
  - id: migrate
    prompt: "Add the new column and update both the API and the Web client."
```

### 15.2 Greenfield worktree

```yaml
defaults:
  resources:
    - type: git
      id: app                            # shared; tasks may override this id
      with:
        repo: git@github.com:acme/app.git
        path: "~/.cache/hiveling/app.git"
        worktree: true
        ref: main
        branch: "hiveling/{run}/{task}"
        clean: full
        cache: [node_modules, .venv]
        publish: push
tasks:
  - id: feature
    prompt: "Implement the feature, run the tests, push the branch."
  - id: verify
    prompt: "Check out the feature branch and run the test suite."
    depends_on: [feature]
    resources:
      - type: git
        id: app                          # overrides the default `app` resource
        with:
          repo: git@github.com:acme/app.git
          worktree: true
          ref: "hiveling/{run}/feature"  # the branch produced by `feature`
          branch: "hiveling/{run}/verify"
          publish: commit                # no push needed for a verification run
```

### 15.3 Docker (future provider, shown for direction)

```yaml
resources:
  - type: docker
    with: {compose: docker-compose.test.yml, services: [db, redis], wait_for: [db]}
```

## 16. Backward compatibility and migration

Each stage is independently shippable and reversible.

- **Stage 0 — Capabilities & requirements.** Worker advertises capabilities
  (`/health`), task declares `requirements`, scheduler matches. No behavior
  change for plans without requirements. Unlocks heterogeneous fleets.
- **Stage 1 — Schema + `ephemeral`.** Add `requirements` / `resources` /
  `opencode` / `artifacts` to the schema; ship a single `ephemeral` provider
  that reproduces today's zip behavior exactly. `files`/`inputs_from` remain
  sugar over it.
- **Stage 2 — `git` + `env`/`secret` + OpenCode config injection.** Covers both
  target realities (worktree and canonical multi-repo, secrets, per-job
  config).
- **Stage 3 — `command` escape hatch**, then optional external providers, for
  docker/k8s/DB setups.
- **Stage 4 — git artifact channel**: a task consumes a producer's branch by
  naming it as the `ref` of a `git` resource and ordering with `depends_on`
  (7.5).

Compatibility guarantees:

- A plan with no `requirements`/`resources`/`opencode`/`artifacts` behaves
  exactly as today.
- `download`, `files`, `inputs_from: [task]` keep their meaning.
- `workers.yaml` stays valid; capabilities are additive.

## 17. Decisions

### 17.1 Resolved

1. **Git worktree default: `false`.** The `git` provider clones and works in
   place by default, matching the legacy multi-repo mental model.
   `worktree: true` is opt-in for per-task isolation when parallel tasks touch
   the same repo.
2. **Finalize failures fail the task.** There is no `warn` mode and no
   per-resource publish policy. A failed commit/push marks the task `failed`
   even if OpenCode succeeded; the OpenCode result is still kept in history.
   A `publish: push` with **nothing to push** (no edit and no prepare-time
   merge) is also a failure: publishing an empty branch means the agent edited
   a tree the provider never commits, and a silent success only surfaces as a
   `merge ref not found` in the consumer. Reading-only tasks use
   `publish: none` or `commit`.
3. **Permissions come from the injected `opencode.json`, not a CLI flag.**
   The worker never passes `--auto` to `opencode run`: the generated config and
   the agent's `permissions` rules are authoritative, and there is no plan
   `auto` option. Allowing a tool means declaring it in the config/agent.
4. **Resource validation is worker-side, at prepare.** The server validates
   structure and enums only; no server-side dry-run in v1.
5. **Config injection is project-scoped**, at a per-job **location directory**;
   no `XDG_CONFIG_HOME` / `HOME` relocation (section 9.1).
5. **Multiple `AGENTS.md` are concatenated** into `location/AGENTS.md`, in
   priority order, each behind a `<!-- source: ... -->` marker. A repo
   `AGENTS.md` below the location is left to OpenCode discovery and is not
   duplicated (section 9.3).
6. **Integration/merge: `merge: [refs]` on the `git` provider.** Conflicts are
   resolved by the worker with a nested OpenCode run (generic hardcoded
   prompt), then committed. No rebase, no strategy/message options; the
   resolution is traced in the task events and `status.json`.
7. **History growth / GC is deferred** and tracked in `todo.md`, not part of
   this design.
8. **`branch_mode` / `force` defaults: `create` + no force.** A task creates its
   branch from `ref` and cannot force-push unless it explicitly sets
   `force: true`. This is the easy-to-reason-about default; updating an existing
   branch is an explicit choice (`branch_mode: reuse` or `recreate`).
9. **OpenCode source materialization default: `auto`** (symlink/junction, copy
   fallback) with a per-kind preference: **skills are referenced by path in the
   generated `skills` array** (OpenCode supports it natively), while **agents
   are symlinked/junctioned** into `location/.opencode/agents/` because
   OpenCode has no config array for agent directories. Copy is only the
   fallback.
10. **Path roots policy: plan declares, worker enforces.** A worker configures
    `path_roots` (e.g. `D:\src`, `D:\data`, `/opt/acme`, `~/shared`); any
    plan-declared path must resolve inside them or `prepare` fails. The plan
    stays flexible inside the roots, but cannot escape them.
    `path_roots: ["*"]` is an explicit opt-out, not the default. See 9.5.
12. **No cross-task substitutions; dependencies are explicit.** A task has no
    `branch` property (it may declare several `git` resources), so
    `{task.branch}` would be ambiguous. Branch names are written in the plan and
    ordering is carried by `depends_on`, keeping the graph readable. Only
    `{run}` and `{task}` substitutions exist, and they are task-local (7.5).
13. **Config ancestry: accept the machine baseline (v1).** The global
    `~/.config/opencode` and any ancestor project config above the location dir
    are part of the worker machine, like its installed tools. The worker keeps
    its job location under its own data dir. A "clean ancestry" check is
    possible later (17.2), not in v1.
11. **No includes and no named environments.** The plan format stays explicit
    and self-contained; there is no `environment:` reference, no worker-side
    template, no include directive. Cross-plan reuse is handled by the
    plan-authoring assistant (skill + `get_plan_schema`), not by the schema.
    See 14.5.

### 17.2 Deferred / not v1

- **Config-ancestry check**: optionally fail `prepare` when an ancestor of the
  location dir contains OpenCode config, for strict isolation. Not in v1 — the
  machine baseline is accepted (17.1 item 13).
- Workspace GC / history retention and the plan-authoring skill are tracked in
  `todo.md`.

## 18. Appendix

### 18.1 Provider option reference (v1)

| Provider | Option | Default | Notes |
| --- | --- | --- | --- |
| `ephemeral` | `clean` | `full` | Recreate the workdir each job. |
| `git` | `repo` | — | URL or local path. |
| | `path` | required | Clone/worktree location; relative = location dir. |
| | `worktree` | `false` | `true` = per-task worktree (parallel isolation). |
| | `ref` | `HEAD` | **Start point** (branch/tag/sha). |
| | `branch` | `hiveling/{run}/{task}` | **Target** branch to commit/push to. |
| | `branch_mode` | `create` | `create\|reuse\|recreate`. |
| | `push_to` | = `branch` | Remote ref to push (allows a different name). |
| | `set_upstream` | `true` | Pass `-u` on push. |
| | `force` | `false` | Allow a force push (logged/flagged). |
| | `clean` | `git` | `none\|git\|full`. |
| | `cache` | `[]` | Paths preserved across `full`. |
| | `publish` | `push` | `none\|commit\|push`. |
| | `remote` | `origin` | Push target. |
| | `after` | — | Resource ids to prepare before this one. |
| `path` | `path` | required | Folder to expose. |
| | `mode` | `ro` | `ro\|rw`. |
| | `visible` | `true` | Advertise to OpenCode. |
| `env` | `vars` | `{}` | Literal or `${ENV}` expansion. |
| `secret` | `name` | required | Key in the worker secret store. |
| | `as` | `name` | Env var exposed to OpenCode. |
| | `required` | `true` | Fail prepare if missing. |
| `command` | `prepare`/`finalize` | — | Shell command(s). |
| | `shell` | OS default | `bash\|cmd\|powershell`. |
| | `env` | `{}` | Extra env. |

### 18.2 OpenCode `sources` reference

| Option | Applies to | Default | Notes |
| --- | --- | --- | --- |
| `kind` | verbose form | — | `bundle\|config\|agents\|skills\|agents_md`. |
| `path` | all | required | Worker-local; resolved under `path_roots`. |
| `mode` | all | `auto` | `auto\|symlink\|reference\|concat\|copy`. |
| `priority` | all | `0` | Higher wins for scalars / later in concatenation. |
| `optional` | all | `false` | Skip a missing path instead of failing `prepare`. |
| `when` | all | — | Requirement guard (e.g. `{os: linux}`). |
| `from` | shorthand | — | Expands to `kind: bundle`. |
| `agents_paths` | shorthand | — | Expands to `kind: agents`. |
| `skills_paths` | shorthand | — | Expands to `kind: skills`. |
| `agents_md` | shorthand | — | Expands to `kind: agents_md`. |
| `config` / `agents` / `skills` | inline | — | Highest precedence; merged over sources. |

Discovery layout expected inside a `bundle` directory:
`opencode.json(c)`, `agents/<name>.md`, `skills/<id>/SKILL.md`, `AGENTS.md`.

### 18.3 Files likely touched

- `worker/app/executor.py` — prepare/finalize hooks around the OpenCode run.
- `worker/app/environment.py` (new) — provider registry + lifecycle.
- `worker/app/providers/*.py` (new) — `ephemeral`, `git`, `path`, `env`,
  `secret`, `command`.
- `worker/app/opencode_config.py` (new) — per-job config injection.
- `worker/app/config.py` — provider allowlist, `path_roots`, secret store.
- `worker/app/main.py` — `JobSpec` fields, `/health` capabilities.
- `worker/app/state.py` — `environment`, `commits`, `artifacts`.
- `server/app/plan.py`, `server/app/plan_schema.py` — new plan fields.
- `server/app/orchestrator.py` — requirements matching, artifact channels,
  `{run}`/`{task}` substitution.
- `server/app/workers.py` — capabilities in `probe_workers`.
- `README.md`, `todo.md` — document the feature.

# TODO

## Open

### Security & hardening

- Worker↔server auth + encryption, mandatory even inside a DC: mTLS, or at
  minimum a bearer token over HTTPS/TLS. Today `worker_client.py` (httpx) speaks
  in the clear and `worker/app/main.py` verifies nothing on the way in.
- Lock down the server API: the current token only covers `/mcp`
  (`mcp_server.BearerAuthMiddleware`). Extend it to `/api/*` and the dashboard,
  or front everything with an authenticating reverse proxy. Otherwise binding
  `0.0.0.0` lets anyone launch jobs.
- Network egress default-deny: segment the workers and allowlist only what they
  need (model endpoint, remote git, package mirrors). This is the real
  anti-exfiltration barrier and Hiveling cannot provide it in software.
- OS isolation per worker: a dedicated least-privilege service account and,
  ideally, a VM/microVM per worker; `path_roots` stays useful but is not a
  sandbox.
- Ephemeral credentials: short-lived git tokens injected by name (as `secret`
  already is); never a long-lived PAT in the environment.

### Audit, identity & validation

- Tamper-proof audit + identity: this is where versioned plan mutations pay off
  — without an actor and an append-only journal you cannot answer "which plan,
  who, when" after a leak.
- Versioned plan mutations: make plan mutations an immutable journal. Live
  edit/resume is a great feature, but the more powerful it becomes the more you
  need to answer "which version of the plan caused this task?", "who changed
  what?" and "what changed between attempt 2 and 3?". Keep revision (1, 2, 3…),
  timestamp, actor, reason and a snapshot/diff.
- Strict plan validation: `load_plan()`/`_parse_task()` currently extract known
  fields and silently ignore unknown ones, so a typo like `depend_on:` instead of
  `depends_on:` slips through. In a system that can launch agents for an hour, an
  invalid config must die immediately. Make strict validation the source of truth
  for the YAML (as MCP `PlanInput` already is).
- Structured gate verdicts: the contract is essentially a standalone `VALID`
  line; otherwise the text becomes the feedback and the targets start over. Move
  to something like `{verdict: "valid"|"revise", findings:[...], evidence:[...]}`
  with strict server-side validation, so a key engine decision stops depending on
  a textual convention from the model.

### AD / Azure Entra ID SSO (human login)

Goal: use the corporate directory for both the MCP endpoint and the
dashboard/API, so "who did what" has a real, verified actor.

- Identity provider choice: **Entra ID** (OIDC/JWKS native). Raw on-prem AD DS is
  Kerberos/LDAP only — front it with Entra ID (Entra Connect / Entra Domain
  Services) or ADFS; Keycloak over LDAP is the fallback. Decide explicitly and
  document the group→role mapping either way.
- MCP server (`/mcp`): no custom client work — the OpenCode V2 remote MCP client
  already does OAuth (PKCE, token refresh, authorization-server discovery) and
  exposes `client_id`, `client_secret`, `scope`, `redirect_uri`, `callback_port`,
  `auth_server_metadata_url`. **Entra does not support dynamic client
  registration**, so pre-register the client, set `client_id`, and use
  `auth_server_metadata_url` = Entra's tenant OIDC metadata instead of relying on
  protected-resource metadata. Keep `oauth: false` + a static `Authorization`
  header (env-injected) as break-glass / automation.
- Entra app registration: expose a custom scope (e.g. `api://<id>/mcp.access`)
  and a human-login app (loopback `http://127.0.0.1:<port>/callback`;
  `http://localhost` for a public client). Validate the access token
  server-side against the tenant JWKS (cache keys; check `aud`/`iss`/`exp`).
- Server API + dashboard: same identity via Authorization Code + PKCE, session
  cookie, and CSRF protection on every mutating POST (cancel/resume/edit).
  Extend the existing `BearerAuthMiddleware` to `/api/*` and the root, or front
  the app with an authenticating reverse proxy (`oauth2-proxy` / nginx
  `auth_request`).
- RBAC: map Entra groups / claims to Hiveling roles (who may run, cancel,
  resume, edit plans, read audit). Authentication ≠ authorization.
- Feed the SSO subject (`sub`/`oid`) into the audit journal as the actor, so
  versioned plan mutations record a verified identity.
- Operational fallback: keep a local break-glass token (rotation documented) for
  automation and for when the IdP is unavailable, and define the behavior when
  the IdP is down.

### Other

- Scheduler
- Times on run: wall 1h52m · execution 1h02m · attempts 30
- MCPs management from plan
- Remote execution as a specific Windows user


## Done

- Worker resources (CPU count/speed/model, total/available RAM, load average)
  detected by the worker and exposed through `GET /health`, `GET /api/workers`
  and MCP `list_workers`; shown in the dashboard sidebar (`server/app/workers.py`,
  `worker/app/capabilities.py`; also in MCP `get_task`)
- A run's total execution duration appears in the runs list, run detail and MCP
  `list_runs`/`get_run`: it sums every task's real worker-reported `duration_s`
  across the current attempt and archived retries, never the run's start-to-end
  wall clock (`server/app/usage.py`, `RunManager.attach_durations`)
- The web task detail streams a running task's events live (Server-Sent Events
  on `GET /api/runs/{id}/tasks/{task}/events`), tailing the worker job while it
  runs and replaying the saved log once it is done
- The task detail shows the task's resources with `{run}`/`{task}` resolved, so
  git branches are visible before, during and after a run
- Worker affinity within a run: the scheduler prefers a worker that already ran
  a task of the run when it is free, so its durable clone/worktree is reused
  instead of being cloned again on another worker (`Orchestrator._choose_worker`)
- `resume_run` also re-arms `canceled` tasks (not only `failed`/`skipped`), so a
  run stopped by canceling a task can be resumed without editing the plan
- Dashboard: home page (start a run from a stored plan via `GET /api/plans`,
  recent runs) reached by clicking the title in the header bar
- Dashboard: run detail shows cumulative tokens/cost across retries (and the
  attempt count), aggregated from every task's current + archived attempts
  (`server/app/usage.py`; also in MCP `get_run`)
- Dashboard: edit a run's plan while it runs (applied to pending tasks between
  tasks; the snapshot and, for stored plans, the plan file are written).
  `write_plan` now validates against the run's `base_dir`, so plans using a
  relative `prompt_file`/`files` can be edited from the snapshot
- Dashboard: browser back/forward works (pushState + popstate)
- Dashboard: explicit in-page confirmation before "Cancel run" ("Keep running" /
  "Cancel run") and before "Resume run"
- Dashboard: auto-refresh no longer disrupts typing/scrolling or collapses open
  `<details>` (task body is not rebuilt on poll) and the "Refresh" button is gone
- Cancel a task
- Parallel DAG execution (dependencies + available workers; fail fast waits
  for in-flight tasks)
- Worker capabilities / task requirements (Stage 0)
- Resources schema + ephemeral provider (Stage 1)
- Reproducible OpenCode runtime config + remote setup of an OpenCode config
  (agents, skills, AGENTS.md) (Stage 2)
- git / env / secret / path / command providers (Stages 2-3)
- Git artifact channel: cross-task branches via resources + artifacts.paths
  (Stage 4)
- Worker workspace GC / history retention (`worker/gc.py`, retention flags)
- Repo config override with deep merge + additive permission lists
- Plan-authoring skill (`.opencode/skills/hiveling-plan/`)
- Git integration/merge between worker branches: `merge: [refs]` resolved by
  the worker's own OpenCode run

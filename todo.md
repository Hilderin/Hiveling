# TODO

## Open

- Add security and authentication to communication with workers
- Remote execution as a specific Windows user
- Expose worker resources in the server and MCP (available/total RAM, CPU
  count, CPU speed, etc.)
- Show a run's total execution duration in the runs list and in the run detail
  (do not use the difference between start and end, add the real durations of
  the tasks and retries)
- Stream a task's events live in the web task detail as they arrive, so you can
  follow what is happening step by step instead of only seeing the final result
- Show a task's resources in the task detail (git branches are not visible in
  the UI)
- Expose the same in the MCP tools: a run's duration and a task's resources
- Worker affinity within a run: prefer (when free) the worker that already ran a
  previous task of the run, so its repo clone/worktree is reused instead of
  cloning the repo again on every other worker
- On `resume_run`, also re-arm `canceled` tasks (currently only `failed` and
  `skipped` are restarted), so a run stopped by canceling a task can be resumed
  without editing the plan
- Versioned plan mutations

## Done

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

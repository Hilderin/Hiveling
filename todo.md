# TODO

## Open

- Add security and authentication to communication with workers
- Remote execution as a specific Windows user
- Versioned plan mutations

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

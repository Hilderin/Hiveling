# TODO

## Open

- Show the token count and cost for a run (counting retries)
- Allow going back in the browser after navigating in the dashboard.
- Make a home page on the right side of the dashboard
- Allow clicking the title in the header bar to return to the home page.
- Allow modifying a plan while it is running
- Confirmation when clicking on "Cancel run"
- Remove the "Refresh" button
- Add security and authentication to communication with workers
- Remote execution as a specific Windows user
- Expose worker resources in the server and MCP (available/total RAM, CPU
  count, CPU speed, etc.)
- Show a run's total execution duration in the runs list and in the run detail
  (do not use the difference between start and end, add the real durations of
  the tasks and retries)
- Dashboard auto-refresh is disruptive: it interrupts scrolling inside
  textboxes and collapses open sections such as events
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

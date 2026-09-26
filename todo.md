# TODO

## Open

- Goal mode / finish_check_prompt / checks
- Versioned plan mutations
- Add a task to a plan
- Remove a task from a plan
- Allow modifying a plan while it is running
- Add security and authentication to communication with workers
- Remote execution as a specific Windows user
- Worker concurrency > 1
- Agent, tokens and cost are not present in the history
- Expose worker resources in the server and MCP (available/total RAM, CPU
  count, CPU speed, etc.)

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

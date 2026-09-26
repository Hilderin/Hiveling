# Reusable phase agents

A **project-agnostic** set of OpenCode agents for the canonical development
pipeline. They carry no knowledge of any specific repository: each task's
**prompt** supplies the requirement, the scope and the conventions, and the
agents' own instructions stay generic.

| Agent | Phase | Mode | Output contract |
| --- | --- | --- | --- |
| `analyst` | analysis | primary | scope / acceptance / decomposition / risks; ends with `ANALYSIS: READY` or `ANALYSIS: BLOCKED: …` |
| `analyst-reviewer` | analysis review | subagent (read-only) | findings + `VERDICT: APPROVED` / `VERDICT: CHANGES_REQUESTED: …` |
| `architect` | design | primary | interfaces, data model, files, decisions, test seams, risks |
| `architect-reviewer` | design review | subagent (read-only) | findings + `VERDICT: …` |
| `coder` | implementation | primary | the change, verified with the project's own test command |
| `tester` | tests | all | automated tests, green suite |
| `reviewer` | code review | all (read-only) | findings + `VERDICT: …` |

## How to use them

Hiveling runs `opencode run --agent <name>`, so the agents must exist where the
worker's OpenCode searches for them. Two ways to make them available:

1. **Per repository (portable, versioned):** copy the ones you need into the
   project's `.opencode/agents/`. The repo can then also ship project-specific
   agents that extend these.
2. **Per worker (global fallback):** put them under the worker's baseline
   OpenCode bundle (`--opencode-dir`, default `./opencode`), e.g.
   `<bundle>/agents/analyst.md`, so every task on that worker can use them.

## Wiring the pipeline into a plan

Reference the agent per task with `agent: <name>`, and give the agent the context
it needs in the prompt. A generic shape:

```yaml
tasks:
  - id: analyze        # analyst
    agent: analyst
    prompt: "Requirement: <the ticket>. Produce the analysis."
  - id: review-analysis   # analyst-reviewer
    agent: analyst-reviewer
    depends_on: [analyze]
    prompt: "Review the analysis for requirement <ticket>."
  - id: design         # architect
    agent: architect
    depends_on: [review-analysis]
    prompt: "Design the change for <ticket>."
  - id: implement      # coder (one or more parallel tasks)
    agent: coder
    depends_on: [design]
    prompt: "Implement <work item>."
  - id: test           # tester
    agent: tester
    depends_on: [implement]
    prompt: "Add tests for <work item>."
  - id: review         # reviewer
    agent: reviewer
    depends_on: [test]
    prompt: "Review the change for <ticket>."
```

The `*-reviewer` / `reviewer` agents produce a **verdict in their result**, not
a process gate: the orchestrator reads it with `get_task` and either accepts the
phase or feeds the findings back (`update_run_plan` + `resume_run`). See the
`hiveling-plan` skill for the full loop.

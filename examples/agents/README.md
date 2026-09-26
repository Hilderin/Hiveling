# Reusable phase agents

A **project-agnostic** set of OpenCode agents for the canonical development
pipeline. They carry no knowledge of any specific repository: each task's
**prompt** supplies the requirement, the scope and the conventions, and the
agents' own instructions stay generic.

| Agent | Phase | Mode | Output contract |
| --- | --- | --- | --- |
| `analyst` | analysis | primary | scope / acceptance / decomposition / risks; ends with `ANALYSIS: READY` or `ANALYSIS: BLOCKED: …` |
| `analyst-reviewer` | analysis review | subagent (writes a report) | `docs/reviews/analysis-review.md` + `VERDICT:` line |
| `architect` | design | primary | interfaces, data model, files, decisions, test seams, risks |
| `architect-reviewer` | design review | subagent (writes a report) | `docs/reviews/design-review.md` + `VERDICT:` line |
| `designer` | UX/UI design | all | screens, flows, states, copy, layout, accessibility |
| `designer-reviewer` | design review | subagent (writes a report) | `docs/reviews/ui-design-review.md` + `VERDICT:` line |
| `coder` | implementation | primary | the change, verified with the project's own test command |
| `tester` | tests | all | automated tests, green suite |
| `reviewer` | code review | all (writes a report) | `docs/reviews/code-review.md` + `VERDICT:` line |
| `product-owner` | final acceptance | all (writes a report) | `docs/reviews/acceptance-review.md`: per-criterion PASS/FAIL/UNVERIFIED + `VERDICT:` line |

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
  - id: design
    agent: architect
    prompt: "Produce docs/design.md and commit on the current branch."
    resources: [{type: git, id: repo, with: {repo: REPO, path: repo, worktree: true,
                 ref: main, branch: "hiveling/{run}", publish: push}}]

  - id: design-review
    agent: architect-reviewer
    depends_on: [design]
    prompt: "Review docs/design.md; write docs/reviews/design-review.md and commit it."
    resources: [{type: git, id: repo, with: {repo: REPO, path: repo, worktree: true,
                 ref: main, branch: "hiveling/{run}", publish: push}}]  # SAME branch

  - id: implement
    agent: coder
    depends_on: [design-gate]        # depend on the GATE, not on design
    prompt: "Implement docs/design.md."

gates:
  - id: design-gate
    tasks: [design, design-review]   # a rejection re-runs the design AND its review
    prompt: >
      Reject if docs/design.md does not satisfy the requirements, or if
      docs/reviews/design-review.md flags a blocker.
    max_attempts: 5
```

Repeat that shape for each phase (analysis → design → UI design →
implementation → acceptance), merging branches as needed. On a rejection
**every** task in the gate's `tasks` is re-run with the gate's findings injected,
in dependency order: the producer fixes its artifact, then the reviewer
re-inspects the updated artifact and rewrites its report. `product-owner` runs
last, on the fully merged result, after every other phase.

The `*-reviewer` / `reviewer` agents **write a machine-readable report** to
`docs/reviews/<phase>-review.md` (read-only on code, with a narrow `edit`
carve-out) and end their result with a `VERDICT:` line. A verdict is **not** a
process gate by itself — a review task is `succeeded` whatever it says. Hiveling
enforces it with the `gates` entry, which resets and re-runs the analysed tasks
until `VALID` (bounded). See the `hiveling-plan` skill for the full loop.

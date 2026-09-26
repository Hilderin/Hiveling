"""Built-in gate logic: the hardcoded OpenCode agent, the verdict contract and
the prompt material a gate receives.

A gate is a plan node (declared under the top-level ``gates`` key) that judges
the tasks it analyses. It is **not** configured by the plan: the server owns the
agent and the machine-readable contract, so a plan can neither weaken the
agent's permissions nor change the pass token. The plan only supplies the
criteria (the gate's ``prompt``).

The agent runs on a worker in a fresh workspace with no repository, reads the
decision material the orchestrator uploads under ``_hiveling/``, and answers
with either a lone ``VALID`` line or the list of corrections it requires. The
orchestrator reads that answer from the job's ``result_text`` (no permission to
write, no artifact to commit).
"""

from __future__ import annotations

GATE_AGENT_ID = "hiveling-gate"

# OpenCode V2 permissions are an ordered list and the last matching rule wins;
# agent rules are appended after the global/config rules, so this profile is
# authoritative for the gate job. Everything is denied, then read/glob/grep are
# re-enabled for the injected payload, and ``.env`` is denied again (a broad
# ``read`` allow would otherwise reopen the default ``*.env`` deny).
GATE_AGENT_MD = """\
---
description: Hiveling gate - judges a task's results and reports VALID or the fixes needed (read-only)
mode: subagent
permissions:
  - {action: "*", resource: "*", effect: deny}
  - {action: read, resource: "*", effect: allow}
  - {action: glob, resource: "*", effect: allow}
  - {action: grep, resource: "*", effect: allow}
  - {action: read, resource: "*.env", effect: deny}
  - {action: read, resource: "*.env.*", effect: deny}
---

You are a **Hiveling gate**. You judge whether the tasks of a plan succeeded.
You are read-only: never modify, create or delete a file.

The decision material is under `./_hiveling/`:

- `gate.json` — your criteria, this attempt, and your previous verdicts;
- `plan.json` — the plan's tasks and gates;
- `results.json` — for every analysed task: status, result text, changed files,
  commits and error;
- `tasks/<id>/prompt.txt`, `tasks/<id>/result.txt` and `tasks/<id>/files/` — what
  each task was asked, what it reported, and the files it produced.

Apply the criteria from `gate.json` strictly and decide from the evidence. End
your answer with **exactly one** of:

- a line containing only `VALID` when every criterion is met;
- otherwise a short list of the exact corrections required, one per line,
  blockers first. Do not write the word `VALID` anywhere in that case.
"""

DEFAULT_MAX_ATTEMPTS = 20


def is_valid_verdict(text: str) -> bool:
    """True when the gate's answer contains a standalone ``VALID`` line.

    The match must be a whole line, not a substring: ``INVALID`` contains
    ``VALID`` and must not pass. Case-insensitive, surrounding whitespace and a
    trailing period are tolerated.
    """
    for line in (text or "").splitlines():
        token = line.strip().strip(" \t-*`#>").strip().rstrip(".").strip()
        if token.upper() == "VALID":
            return True
    return False


def build_gate_prompt(
    criteria: str, targets: list[str], attempt: int, max_attempts: int
) -> str:
    """The prompt handed to the gate agent (criteria + payload pointer)."""
    listed = ", ".join(targets) if targets else "(none)"
    return (
        f"{criteria.strip()}\n\n"
        "--- Hiveling gate ---\n"
        f"Attempt {attempt} of at most {max_attempts}. "
        f"Tasks analysed: {listed}.\n"
        "Read the decision material under ./_hiveling/ (see gate.json, "
        "plan.json, results.json and tasks/<id>/). Decide whether the analysed "
        "tasks meet the criteria above.\n"
        "End with a line containing only VALID if they do, or with the list of "
        "exact corrections required (one per line, blockers first) if they do "
        "not. Do not modify any file.\n"
    )


def with_gate_feedback(prompt: str, feedback: str) -> str:
    """Append a previous gate rejection to a retried task's prompt.

    The block is role-neutral on purpose: a producer must fix its artifact, and
    a reviewer (a task in the same gate's ``tasks`` that consumes the producer's
    branch) must re-inspect the **updated** artifact and rewrite its report.
    Without this a re-run agent could just restate its previous answer.
    """
    return (
        f"{prompt.rstrip()}\n\n"
        "--- Gate feedback: the previous attempt of this gated unit was rejected ---\n"
        f"{feedback.strip()}\n"
        "--- Redo your task so the points above are covered: if you produced an "
        "artifact, fix it; if you reviewed one, re-inspect the updated artifact "
        "and rewrite your report, confirming or denying each point. Do not just "
        "restate your previous answer. ---\n"
    )

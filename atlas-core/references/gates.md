---
description: Atlas Core gate taxonomy — what each gate kind runs, what evidence it produces, and when UNVERIFIED is the correct terminal state.
---

# Gate kinds

| kind | executes | evidence | UNVERIFIED when |
|------|----------|----------|-----------------|
| `files` | checks required artifacts exist + non-empty | list of missing | role has no deliverables yet |
| `compile` | `python -m py_compile` on produced .py | stderr | no .py produced |
| `test` | role-set `commands` (e.g. pytest) | exit + stdout tail | no test command declared |
| `shell` | role-set `commands` | exit + stdout/stderr tails | no commands declared |
| `no-gate` | nothing | — | always (by design: human review) |

Rules:
- Evidence is written to `work/<role>/GATE-RESULT-<role>.md` (or `UNVERIFIED-NO-GATE.md`).
- `UNVERIFIED` is a **terminal state** in the gate report, not a warning. Release is a human decision.
- A gate that cannot actually check the deliverable must NOT be declared for that role —
  prefer `no-gate` + human review over a fake PASS.
- The verifier is the engine (a separate process), never the subagent that produced the work.

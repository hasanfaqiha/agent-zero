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

## v1.1 additions

### Environment sanitization (fix #1)
Gate `shell`/`test` commands no longer inherit the orchestrator's full environment
(API keys, tokens, agent secrets). Each command runs under a minimal allowlist:
`PATH`, `HOME`, `LANG`, `LC_ALL`, `PYTHONPATH`. A gate def may add specific
variables it genuinely needs via an optional `"env_allowlist"` key
(e.g. `["PGPASSWORD"]` for a DB smoke test). Anything not allowlisted is gone.

### `cwd` field (fix #7)
A gate def may set `"cwd": "run"` to execute its commands from the run root
instead of the role dir (default stays `work/<role>/`). This is what lets the
security gate grep the whole run dir (`.`) plus `../specs/` in one pass:

```json
{ "role": "security", "kind": "shell", "cwd": "run", "human_fallback": false,
  "commands": ["bash -c \"grep -rE 'sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}' . ../specs/ ; test $? -eq 1 || { echo 'secrets found — FAIL'; exit 1; }\""] }
```

Note: `test $?` after `grep -rE` succeeds (exit 0) means secrets WERE found,
so the gate fails — there is no `|| echo found` force-0 pattern anywhere.
Failures of this gate are FAIL (exit 2); it has `human_fallback: false`
because a secret grep that cannot run is not something a human can "verify"
by eyeballing.

### Evidence append (fix #5)
`GATE-RESULT-<role>.md` **appends** a run block (`[gate round @ <ts>]` + result)
to the existing file each time the gate re-runs, so a FAIL from round 1
survives a round-2 PASS. `UNVERIFIED-NO-GATE.md` keeps overwrite semantics
(one terminal statement, no history to preserve).

### Client build gate (fix #6)
The software set's client gate is `kind: shell` with one honest command:

```bash
if [ -f package.json ] && grep -q build package.json; then
  npm run build
else
  echo 'no build step declared — client gate UNVERIFIED'; exit 1
fi
```

Exit 1 here is deliberate (NOT a `|| exit 0` fallback): when no build step is
declared the gate cannot prove anything, and the gate's `human_fallback: true`
turns that into the terminal `UNVERIFIED` verdict instead of a fake PASS.

### Replay + commit (fix #8)
`./replay.sh <run-repo> [commit]` re-runs `atlas.py gate <role>` for every role
in the role-set, regenerates `logs/gate-report.md` via `merge` when all pass,
and prints a per-role PASS/FAIL/UNVERIFIED table. Exit 0 = clean, 2 = a gate
failed. CI pattern: `git clone <run-repo> tmp && ./replay.sh tmp <commit>`
where `<commit>` is any `ATLAS merge: final snapshot` commit made by
`atlas.py commit` (phase `merged` only).


## v1.2 — Wave 2/3 gate capabilities

### New gate kinds
| kind | what it does | config knobs |
|------|--------------|--------------|
| `coverage` | runs the test command under stdlib `python3 -m trace`, enforces a minimum line-coverage % over the role's manifest .py files | `commands`, `min_pct` (default 70), `max_wall_clock` |
| `mutation` | applies bounded simple mutations (≤40, 8 operators) to role .py files, re-runs tests, enforces a minimum kill rate | `commands`, `min_score` (default 60), `max_wall_clock` |

Both are dependency-free: no pytest-cov, no mutmut. Swap the command to a
cov-instrumented one if you need branch coverage — the gate runs whatever you give it.

### Budget
- Role-set `cost: {wall_clock_s, credits}` is the cap.
- A gate def may declare `"credits": N` and/or `"max_wall_clock": S`.
- Spend accumulates in `.atlas/state.json` (`cost_spent`). Crossing the cap,
  or a wall-clock timeout, flips the verdict to **BUDGET_EXCEEDED** — a
  terminal state (like UNVERIFIED) that the merge report lists on its own line.

### Replayable evidence header
Every GATE-RESULT now starts with an HTML-comment header:
`role`, `gate_kind`, `cwd`, `env_allowlist`, the exact `$ commands`, and
`input_sha256` for each manifest file + manifest.json. That header is enough
for a third party to reproduce the verdict.

### Verdict signing (HMAC)
- After a gate, the evidence file is signed with an HMAC-SHA256 keyed on
  `lock_commit`.
- `merge` re-verifies every signature. A tampered verdict is demoted
  PASS→FAIL and listed under `TAMPERED EVIDENCE (HMAC mismatch)`.
- This is tamper-*evidence* (anyone can recompute), not authentication.

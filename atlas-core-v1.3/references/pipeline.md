---
description: Atlas Core pipeline phases, state machine, and the human gates between phases.
---

# Phases

```
init -> synth? -> (author specs/contracts/) -> lock -> build*/mark-built*/gate* -> merge -> commit
```

- `init` creates the run dir, state machine, role slots.
- `synth` (only for domains without a curated role-set) validates a synthesized role-set and
  registers it as `proposed`. **Running it requires user approval of the proposal.**
- Authoring `specs/contracts/*` is the LLM's job: PRD, ARCHITECTURE, schema.sql, API spec.
- `lock` git-commits the contracts. Builders read; the drift check at `merge` flags any
  post-lock edit to specs/.
- `build <role>` writes `work/<role>/BRIEF.md` (mandate + boundaries). The orchestrating LLM
  spawns a FRESH subagent per role (parallel OK after lock), pointing it at its own dir only.
- `mark-built <role>` checks the manifest and that nothing was written outside `work/<role>/`.
- `gate <role>` runs the role's gate kind; FAIL exits 2, UNVERIFIED is recorded as terminal.
- `merge` writes `logs/gate-report.md`: PASS/FAIL/UNVERIFIED per role + contract drift.
  It never prints "APPROVED".

## v1.1 additions

### `amend <run-dir> <reason>` (fix #4)
The official re-lock path, replacing hand-editing of state.json. Allowed only in
phase `build` or `gate`. Rules:

- Hard cap of **3 amendments** (state key `last_amended` / `amend_count`); the 4th dies.
- The free-text reason is recorded in the state log with a timestamp.
- Writes `specs/contracts/AMEND-<n>.md` containing the reason and the list of
  changed contract files (git diff vs the previous lock).
- Re-runs the lock git commit sequence: `git add -A` → commit
  `"ATLAS amend N: <reason>"` → refresh `lock_commit` + `manifest`.
- Stays in the current phase — amendments do not restart the pipeline.

### Atomic state (fix #2)
Every `save_state` writes a temp file in the same directory, fsyncs, and
`os.replace`s onto `.atlas/state.json`. A crash mid-write can never corrupt
state; a resume never sees half-written JSON.

### `commit` (fix #8)
Phase `merged` only: `git add -A` + commit `"ATLAS merge: final snapshot"` of
the entire run dir. This makes any commit of the run replayable:
`./replay.sh <run-repo> <commit>` (see `references/gates.md`, "Replay + commit").
Replay re-runs every role's gate, regenerates the report on a clean pass, and
prints the per-role PASS/FAIL/UNVERIFIED table.

### Gate environment / boundary / evidence (fixes #1, #3, #5, #7)
- Gates run under a minimal env allowlist (`references/gates.md`); no API keys leak.
- `mark-built` now proves the boundary with git: any tracked change or untracked
  file since `lock_commit` outside `work/<role>/` dies the build (`.atlas/` and
  `logs/` engine bookkeeping is exempt).
- GATE-RESULT evidence files append per round; earlier FAIL evidence survives.
- Security gates can declare `"cwd": "run"` to scan the whole run tree.


## v1.2 — new commands

| command | purpose |
|---------|---------|
| `atlas.py regate <run> [role]` | post-amend: diff lock..HEAD over specs/+work/, re-gate only affected roles (or one named role) |
| `atlas.py replay <run> [commit]` | re-run a recorded run's gates from a pinned contract commit; blocks (exit 3) if the replay-lock environment drifted |
| `atlas.py selftest` | minimal 2-role pipeline in a temp dir; CI hook that proves the engine works in the current harness |
| `atlas.py registry install <name>` | export a role-set as standalone JSON (pinnable by hash) |
| `atlas.py registry hash <name>` | print a role-set's content hash |

`replay-lock.json` (written at lock) pins python/pytest versions, gate-command
hashes, and the engine file's own hash — the environment contract for replay.

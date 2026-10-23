---
description: Atlas Core pipeline phases, state machine, and the human gates between phases.
---

# Phases

```
init -> synth? -> (author specs/contracts/) -> lock -> build*/mark-built*/gate* -> merge
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

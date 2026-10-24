---
name: atlas-core
description: Multi-role build pipeline (contract-first, real gates) for any business line. Use when the user says "atlas core" or "set up atlas" + a build request, or asks to run a multi-role/structured build pipeline.
---

# Atlas Core

Deterministic orchestrator engine + portable adapter. One engine, any agentic harness
(Letta, OpenCode, Hermes, Claude Code). The engine executes; the LLM only proposes and
authors — it NEVER self-approves.

## Trigger protocol (analyze-and-propose; NEVER auto-execute)

1. On "atlas core, saya ingin build X": load the project-log, classify domain (code-switch aware).
2. Pick a role-set from `engine/role-sets.json` (or synthesize a candidate — engine validates,
   forces `status=proposed`).
3. **STOP and present to the user**: proposed role-set, phases, cost estimate, gate kinds.
   Wait for explicit approval. (User rule: analisis dulu, propose, jangan execute.)
4. After approval: run the pipeline below.

## Pipeline (engine commands — all deterministic)

    E = <engine dir>/atlas.py
    python3 $E init    <run-dir> <project> <domain>
    python3 $E synth   <run-dir> <synthesized-role-set.json>   # only for new domains
    # author specs/contracts/* (PRD, ARCHITECTURE, schema.sql...) — LLM writes files
    python3 $E lock    <run-dir>                               # git-snapshots contracts
    python3 $E build   <run-dir> <role>                         # writes work/<role>/BRIEF.md
    # spawn subagent: cwd=<run-dir>, fresh (NEVER fork), instructs it to read
    # work/<role>/BRIEF.md and write ONLY inside work/<role>/ + manifest.json
    python3 $E mark-built <run-dir> <role>                      # manifest + boundary check
    python3 $E gate    <run-dir> <role>                         # real checks; UNVERIFIED is terminal
    python3 $E merge   <run-dir>                               # gate report; no APPROVED stamp
    # v1.2 extras (all deterministic):
    python3 $E regate  <run-dir> [role]                         # post-amend: re-gate only roles whose inputs changed
    python3 $E replay  <run-dir> [commit]                       # re-run gates from a pinned contract commit (replay-lock gated)
    python3 $E selftest                                            # prove the engine works in this harness (CI hook)
    python3 $E registry install <name> | registry hash <name>  # role-set distribution as pinnable data
    # optional gate kinds (dependency-free):
    #   "kind": "coverage", "min_pct": 70        — stdlib-trace line coverage
    #   "kind": "mutation", "min_score": 60      — bounded mutation sanity
    # budget: role-set "cost": {wall_clock_s, credits}; gate "credits"/"max_wall_clock"
    #         -> verdict BUDGET_EXCEEDED when blown (terminal, like UNVERIFIED)

Parallel subagents: spawn all roles of the active role-set at once after `lock`; each gets
`work/<role>/` only. No shared writes.

## Rules (machine-enforced; do not waive)

- Gates are exit codes / tool output. LLM attestation is not evidence.
- Non-objective roles (copy, brand, video art direction) end `UNVERIFIED — human review required`.
- Role count ∝ ask size. Default 3–5 roles; a fixed 10-role fan-out is a smell.
- Subagents: fresh context (no `fork`), MCP inheritance off, no secret access. Secrets stay
  with the orchestrator only.
- Budget: estimate tokens before spawn; council (if any) is opt-in, 1 round, max 3 models.
- Auto-synthesized role-sets are PROPOSED. Persisting to the registry requires user approval
  AND a diff review of the proposed JSON.

## Per-domain gate kinds
See `references/gates.md`. Software: pytest/compile. Content: file/metadata/pHash checks.
Design/research: file gates + explicit UNVERIFIED states.

## Registry
`engine/role-sets.json` — curated. `python3 $E role-set <name>` prints one. Stale sets get
deprecated by a human; the engine never auto-appends.

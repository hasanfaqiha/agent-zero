# Atlas Core — CHANGELOG

## v1.3 (2026-10-24) — Audit remediation + design-QA + skill discovery

Independent 4-dimension audit of v1.2 scored it 4/10 (security 2/10,
architecture 5/10, usability 4/10, market 4/10) and found 12 verified
defects (ref: memory `reference/atlas-audit-4d-2026-10-24.md`). v1.3
fixes every one, adds two new capabilities, and reframes the product.
Test suite: 39 tests, all green.

### Security (audit 2/10 → remediated)
1. **C1 — wall-clock budget is enforced.** `cost.wall_clock_s` accumulates
   per-gate elapsed into `state.wall_clock_spent` and flips the verdict to
   BUDGET_EXCEEDED when blown (terminal; no human stamp to launder it).
2. **C2 — replay re-validates gate pins.** `gate_hashes` now hashes the
   FULL gate definition (kind, commands, required, min_pct/min_score,
   credits, max_wall_clock, cwd, env_allowlist). Weakening a `required`
   list or raising a threshold after lock blocks replay.
3. **C3 — gates.py is pinned.** replay-lock gains `gates_sha`; tampering
   with the verifier logic after lock fails the lock check.
4. **C4 — HMAC is per-run, not public.** Legacy key =
   sha256(lock_commit + ":atlas-core") was forgeable (all inputs public).
   v1.3: `phase_lock` writes a 32-byte `secrets.token_bytes` key to
   `.atlas/hmac.key` (gitignored); evidence signs with it; legacy string
   keys still verify for pre-v1.3 runs.
5. **H3 — boundary exemption is scoped.** A role's GATE-RESULT / UNVERIFIED
   / manifest / BRIEF are exempt only for THAT role, and only for roles
   that reached status `built`. A builder can no longer pre-seed another
   role's evidence (verified by live attack probe).
6. **H4 — manifest traversal rejected.** Absolute paths and `..` entries in
   a manifest die at mark-built (verified by live probe).
7. **New — full-engine pin.** replay-lock now pins EVERY `engine/*.py`
   (`engine_files` sha map), so a swapped-in weaker checker blocks replay.

### Architecture (audit 5/10 → remediated)
8. **Phantom phases fixed.** The state machine actually sets `build` and
   `gate` now, so `amend`/`regate` are reachable in a legal flow
   (verified: amend + regate + multi-role interleave all run green).
   `build` accepts any post-lock, pre-merge state so multi-role runs can
   interleave build→mark→gate per role.

### Usability (audit 4/10 → remediated)
9. **First-run works with zero `git add`** (the documented happy path died
   at mark-built because replay-lock.json was flagged out-of-scope; now
   engine bookkeeping is exempt). Regression test locks this.
10. **Registry canonicalization (M4).** `registry hash` and `registry
    install` share one serializer — the sha you pin equals the file you
    install.
11. **selftest is isolated.** Runs against a temp registry; the shipped
    `engine/role-sets.json` is byte-identical before/after (verified).
12. **`references/role-set-schema.md`.** Every role-set/gate field
    documented with a runnable example per gate kind.

### New capabilities
- **Skill discovery.** Role-set `"skills": [{role, skill}]` → the build
  BRIEF carries a `SKILLS:` line so the subagent loads the right skills;
  the engine pins the choice at lock time (no drift between lock and
  build). Discovery of skills is the agent's job; the gate stays
  authoritative.
- **Design-QA gate (`kind: "design"`)** + **`web-ui` role-set.** Root
  cause of "basic UI/UX" agent output: no design skill loaded, no design
  gate. v1.3 ships `engine/design_qa.py` — a deterministic, stdlib-only
  battery (stylesheets, responsive breakpoints, custom typography, design
  tokens, layout system, a11y landmarks; weighted score). Basic unstyled
  HTML scores 0.00 → hard FAIL. No css/html at all → UNVERIFIED (never
  PASS). The `web-ui` role-set binds it: frontend (design gate) +
  independent design-qa role, with skill bindings. Full 2-role E2E
  verified green.

### One extra defect found during the live demo
13. **AMEND-*.md boundary false-positive (found 2026-10-24 on the
    akar-landing demo run).** `amend` writes
    `specs/contracts/AMEND-<n>.md` (engine-authored bookkeeping), but the
    next `mark-built` flagged that file as the CURRENT role writing
    outside its boundary — hard-blocking the post-amend re-build. The
    `specs/contracts/AMEND-` subtree is now exempt, with regression test
    `test_fix_amend_docs_exempt_from_boundary`. Suite: 39 → 40 tests.

### Framing (market audit 4/10)
- Drop "most powerful" — no longer defensible against ConstraintLoop /
  ADLC / Compass / spec-kit-157. New positioning: **the orchestrator that
  fails loudly** (UNVERIFIED as first-class terminal, replayable PASS,
  loud boundary failures). See SKILL.md positioning block.

## v1.2 (2026-10-24) — Wave 2 + Wave 3 (the "most powerful" plan executed)

Implements the full remaining roadmap from PLAN.md. Every item below has a
self-test (suite now 25 tests, all passing).

### Wave 2 — machine-grade verification
1. **Replayable gate evidence** — every GATE-RESULT now carries an
   `evidence_header`: exact commands, cwd, env allowlist, and sha256 of each
   input file. A third party can reproduce the verdict from the header alone.
2. **Budget enforcement → `BUDGET_EXCEEDED` terminal** — role-sets carry
   `cost: {wall_clock_s, credits}`; gate defs may set `"credits"` and
   `"max_wall_clock"`. Accumulated spend is tracked in state; blowing the cap
   flips the verdict to BUDGET_EXCEEDED (terminal, like UNVERIFIED — no human
   stamp to launder it). Wall-clock timeouts surface the same way.
3. **Contract-drift re-gate** — new `atlas.py regate [role]` command. After an
   amend it diffs `lock_commit..HEAD` over specs/+work/ and re-runs only the
   gates whose inputs actually changed (or a named role).
4. **Coverage gate** — `kind: "coverage"` runs the test command under stdlib
   `python3 -m trace` (zero new dependencies), reads `.cover` files, enforces
   `min_pct` (default 70%).
5. **Mutation sanity check** — `kind: "mutation"` applies bounded simple
   mutations (40 cap) to role .py files and re-runs tests; enforces
   `min_score` (default 60% killed).

### Wave 3 — distribution & integrity
6. **`atlas.py replay [commit]`** — re-runs a recorded run's gates from a pinned
   contract commit, gated by the replay-lockfile check first.
7. **Registry-as-data distribution** — `atlas.py registry install <name>`
   exports a role-set as a standalone JSON (pinnable by hash);
   `registry hash <name>` prints its content hash. Role-sets stay data, not
   code, so consumers pin a version instead of trusting a moving target.
8. **`replay-lock.json`** — written at lock: pins python/pytest versions,
   every gate command's hash, and the engine file's hash. Replay fails fast
   (exit 3) when any drift is detected.
9. **`atlas.py selftest`** — minimal 2-role pipeline in a temp dir; runs in any
   harness (CI hook) to prove the engine works before you trust it on real work.
10. **Verdict HMAC signing** — every GATE-RESULT is signed with an HMAC keyed
    on the lock commit. `merge` re-verifies every signature; a hand-edit of a
    verdict is flagged as TAMPERED EVIDENCE and demoted from PASS to FAIL.
    Integrity/tamper-evidence, not authentication — anyone can recompute.

### Test suite
- Grew 17 → 25 tests. New: budget, HMAC sign/verify, tamper detection,
  replay-lock, coverage, replay, selftest, registry.
- Fixed a real bug found by the new tests: two env-sanitization tests kept a
  live reference to `os.environ` and `clear()`-ed it in `finally`, destroying
  PATH for every later subprocess in the suite.

### Known limitations (honest, not hidden)
- Coverage uses stdlib `trace` (line-only, C-extension blind). Swap in
  pytest-cov in the test command if you need branch coverage; the gate just
  runs whatever command you give it.
- Mutation pass is intentionally bounded (40 mutations, 8 operators) — a
  sanity check that tests actually bite, not a full mutmut campaign.

## v1.1 (2026-10-23) — red-team audit fixes

Eight confirmed fixes from the red-team audit (each has file evidence in the v1.0 tree):

1. **Gate env sanitization** — `gates.py` shell/test commands now run under a
   minimal allowlist (`PATH HOME LANG LC_ALL PYTHONPATH`); gate defs can add
   vars via optional `"env_allowlist"`. No more API-key leakage to gate subprocesses.
2. **Atomic state writes** — `save_state` in `atlas.py` writes a temp file in the
   same dir, fsyncs, then `os.replace`. Crash-proof `.atlas/state.json`.
3. **Real boundary check in `mark-built`** — after manifest validation, git compares
   changed + untracked paths since `lock_commit` against `work/<role>/`; any
   offender dies the build.
4. **New command `amend`** — the official re-lock path: phase build/gate only,
   hard cap of 3 amendments, records reason + timestamp in state log, writes
   `specs/contracts/AMEND-<n>.md` (reason + changed contract files), re-runs the
   lock commit sequence, refreshes `lock_commit`/manifest, stays in phase.
5. **Audit evidence append** — `gates.write_evidence` appends a timestamped run
   block to existing GATE-RESULT files; UNVERIFIED-NO-GATE.md keeps overwrite
   semantics. Earlier FAIL evidence survives re-runs.
6. **Client gate fixed (software set)** — dropped the `exit 0` fallback; kind
   `shell` with one honest command (npm build when declared, else exit 1) and
   `human_fallback: true` so "no build step" becomes UNVERIFIED, never PASS.
7. **Security gate fixed (software set)** — greps the whole run tree:
   `"cwd": "run"` support added to `gates.py` (default remains the role dir),
   removed the `|| echo no-secrets-found` force-0 pattern, added
   `human_fallback: false`.
8. **Replay story** — new `replay.sh` (check state.json, re-run all role gates,
   regenerate the report on clean pass, print PASS/FAIL/UNVERIFIED table) plus
   new engine command `commit` (phase `merged` only): git-commits the whole run
   dir as `"ATLAS merge: final snapshot"` so any recorded commit is replayable.

Docs updated: `references/gates.md` (env allowlist, `cwd` field, evidence append,
replay/commit), `references/pipeline.md` (amend, commit, atomic state).

## v1.0

- Initial engine: six deterministic phases, gate taxonomy, role-set registry,
  contract lock, boundary policy, UNVERIFIED-as-terminal rule.

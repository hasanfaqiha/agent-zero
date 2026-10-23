# Atlas Core — CHANGELOG

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

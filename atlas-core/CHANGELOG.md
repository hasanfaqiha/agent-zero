# Atlas Core — Changelog

## v1.0 — 2026-10-23
- Pilot E2E "asset management perusahaan" SELESAI: 3/3 gate PASS (engineering 31 test, QA 22 test, domain-qa verifier)
- Defect tertangkap gate (bukan theater): typo kontrak §4 (15000→25000), bug semantik AC6 (retired mid-life harus FREEZE nilai, bukan nol-kan) — dibuat eksplisit di ARCHITECTURE §5
- 3 bug engine ter-fix di trial: synth state ordering, role_set persist, lock_commit KeyError
- 7 aturan lock-in dari red-team 4 subagent paralel (human gate, exit-code gate, UNVERIFIED terminal, role-count ∝ ask, secrets di orchestrator, budget+circuit breaker, code-switch classification)
- Aturan #8 baru (pelajaran pilot): verifier harus PARSE kontrak live, bukan encode angkanya
- Artifact: engine/ (atlas.py, gates.py, role-sets.json), SKILL.md, references/ (pipeline.md, gates.md), atlas-core-modul.pdf
- Role-set `asset-management` status PROPOSED (menunggu keputusan promote→active)

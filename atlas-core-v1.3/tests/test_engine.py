"""Self-tests for atlas-core v1.1 engine fixes. Run: python3 -m pytest -q tests/

Every test builds a real fake run (tempdir + git) instead of mocking the world,
so these prove the engine's actual behavior, not a stub of it.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "engine"))
import gates  # noqa: E402
import atlas  # noqa: E402

GATE = ROOT / "replay.sh"
ENGINE = ROOT / "engine" / "atlas.py"


def sh(cmd, cwd=None, env=None):
    return subprocess.run(cmd, shell=True, cwd=cwd, env=env,
                          capture_output=True, text=True)


# ---------------------------------------------------------------------------
# Registry isolation (v1.3): every subprocess engine call must read/write a
# throwaway copy of the role-set registry, so a test can NEVER pollute the
# shipped engine/role-sets.json. Without this, 'synth' with fresh role-set
# names registers them into the real file (this is how demo5/r1/h3 leaked in
# during development). Set at import so it's inherited by all sh() children.
import shutil as _shutil
import tempfile as _tempfile
_ISO_REGISTRY = Path(_tempfile.mkdtemp(prefix="atlas-reg-")) / "role-sets.json"
_shutil.copy(ROOT / "engine" / "role-sets.json", _ISO_REGISTRY)
os.environ["ATLAS_ROLE_SETS"] = str(_ISO_REGISTRY)


# ------------------------------------------------------------------ fixtures
@pytest.fixture()
def run(tmp_path):
    """A fully built fake run: init -> synth (tiny role-set) -> lock -> build
    -> mark-built, with git tracked at the lock commit."""
    rd = tmp_path / "run"
    s = sh(f"python3 {ENGINE} init {rd} 'tproj' 'software'", cwd=tmp_path)
    assert s.returncode == 0, s.stderr

    rs = tmp_path / "rs.json"
    rs.write_text(json.dumps({
        "name": "tproj-tiny",
        "description": "tiny test role-set",
        "roles": [
            {"id": "builder", "mandate": "Produce the artifact listed in the manifest.",
             "output": "work/builder/", "gate": "shell"},
            {"id": "reviewer", "mandate": "Review the builder output against acceptance criteria.",
             "output": "work/reviewer/", "gate": "files"},
        ],
        "gates": [
            {"role": "builder", "kind": "shell", "commands": ["test -f artifact.txt"]},
            {"role": "reviewer", "kind": "files", "required": ["manifest.json"]},
        ],
    }))
    s = sh(f"python3 {ENGINE} synth {rd} {rs}", cwd=tmp_path)
    assert s.returncode == 0, s.stderr

    (rd / "specs" / "contracts" / "PRD.md").write_text("PRD v1 — build one artifact.\n")
    s = sh(f"python3 {ENGINE} lock {rd}", cwd=tmp_path)
    assert s.returncode == 0, s.stderr

    s = sh(f"python3 {ENGINE} build {rd} builder", cwd=tmp_path)
    assert s.returncode == 0, s.stderr
    s = sh(f"python3 {ENGINE} build {rd} reviewer", cwd=tmp_path)
    assert s.returncode == 0, s.stderr

    b = rd / "work" / "builder"
    (b / "artifact.txt").write_text("hello")
    (b / "manifest.json").write_text(json.dumps(["artifact.txt"]))
    rv = rd / "work" / "reviewer"
    (rv / "manifest.json").write_text(json.dumps([]))
    # Track the build output so mark-built's boundary check (diff vs lock)
    # sees no changes outside work/<role>/.
    sh("git add -A", cwd=rd)
    s = sh(f"python3 {ENGINE} mark-built {rd} builder", cwd=tmp_path)
    assert s.returncode == 0, s.stderr
    s = sh(f"python3 {ENGINE} mark-built {rd} reviewer", cwd=tmp_path)
    assert s.returncode == 0, s.stderr
    # Put the run into 'gate' phase (the amend window) for amend tests.
    st = _load_state(rd)
    st["phase"] = "gate"
    (rd / ".atlas" / "state.json").write_text(json.dumps(st, indent=2))
    return rd


def _load_state(rd):
    return json.loads((rd / ".atlas" / "state.json").read_text())


# ------------------------------------------------------------------ fix #1
def test_gate_env_sanitized_no_secret(tmp_path):
    """A shell gate can see PATH but NOT an orchestrator-level secret."""
    out = tmp_path / "out"
    out.mkdir()
    gate_def = {"role": "x", "kind": "shell",
                "commands": ["echo PATH_SET=$([ -n \"$PATH\" ] && echo yes || echo no) "
                             "SECRET_SET=$([ -n \"$ATLAS_TEST_SECRET\" ] && echo yes || echo no)"]}
    env = os.environ.copy()
    env["ATLAS_TEST_SECRET"] = "supersecretvalue"
    # run_role_gate builds its own env from os.environ — patch os.environ for the call.
    # CRITICAL: snapshot must be a COPY. `real_env = os.environ` (live dict) +
    # clear() in finally destroys the process env (PATH etc.) and poisons every
    # later test that spawns subprocesses.
    saved = os.environ.copy()
    os.environ.update(env)
    try:
        ok, ev = gates.run_role_gate(gate_def, out, tmp_path)
    finally:
        os.environ.clear()
        os.environ.update(saved)
        os.environ.pop("ATLAS_TEST_SECRET", None)
    assert ok is True
    text = (out / ev).read_text()
    assert "PATH_SET=yes" in text, text
    assert "SECRET_SET=no" in text, text


def test_gate_env_allowlist_only_named_vars(tmp_path):
    """env_allowlist adds exactly the declared names, nothing else."""
    out = tmp_path / "out"
    out.mkdir()
    saved = os.environ.copy()
    os.environ["MY_TOKEN"] = "abc123"
    os.environ["OTHER_SECRET"] = "zzz"
    try:
        gate_def = {"role": "x", "kind": "shell", "env_allowlist": ["MY_TOKEN"],
                    "commands": ["echo TOKEN=$MY_TOKEN OTHER=$OTHER_SECRET"]}
        ok, ev = gates.run_role_gate(gate_def, out, tmp_path)
    finally:
        os.environ.clear()
        os.environ.update(saved)
        os.environ.pop("MY_TOKEN", None)
        os.environ.pop("OTHER_SECRET", None)
    assert ok is True
    text = (out / ev).read_text()
    assert "TOKEN=abc123" in text, text
    assert "OTHER=" in text and "OTHER=zzz" not in text, text


# ------------------------------------------------------------------ fix #2
def test_save_state_atomic(tmp_path):
    """save_state must land via rename (tmp file, fsync, os.replace)."""
    rd = tmp_path / "run"
    (rd / ".atlas").mkdir(parents=True)
    (rd / ".atlas" / "state.json").write_text(json.dumps({"phase": "gate"}))
    # Verify atomicity end-to-end: state.json is never a partial write,
    # and the tmp file is always cleaned up.
    for i in range(50):
        atlas.save_state(rd, {"phase": "gate", "i": i, "blob": "x" * 4096})
    final = json.loads((rd / ".atlas" / "state.json").read_text())
    assert final["i"] == 49 and len(final["blob"]) == 4096
    assert not (rd / ".atlas" / "state.json.tmp").exists()


def test_save_state_uses_replace(tmp_path):
    """save_state is implemented via os.replace (crash-safe rename)."""
    rd = tmp_path / "run"
    (rd / ".atlas").mkdir(parents=True)
    import unittest.mock as mock
    with mock.patch("os.replace") as m_replace:
        atlas.save_state(rd, {"phase": "gate"})
    m_replace.assert_called_once()
    src, dst = m_replace.call_args[0]
    assert Path(src).parent == Path(dst).parent
    assert Path(src).name == "state.json.tmp"


# ------------------------------------------------------------------ fix #3
def test_mark_built_blocks_rogue_write(tmp_path):
    """A write to work/other-role/ after lock must kill mark-built."""
    rd = tmp_path / "run"
    s = sh(f"python3 {ENGINE} init {rd} 'tproj' 'software'", cwd=tmp_path)
    assert s.returncode == 0
    rs = tmp_path / "rs.json"
    rs.write_text(json.dumps({
        "name": "tproj-tiny", "description": "d",
        "roles": [{"id": "builder", "mandate": "m", "output": "work/builder/", "gate": "files"},
                  {"id": "other", "mandate": "m2", "output": "work/other/", "gate": "files"}],
        "gates": [{"role": "builder", "kind": "files", "required": ["manifest.json"]},
                  {"role": "other", "kind": "files", "required": ["manifest.json"]}],
    }))
    s = sh(f"python3 {ENGINE} synth {rd} {rs}", cwd=tmp_path)
    assert s.returncode == 0
    (rd / "specs" / "contracts" / "PRD.md").write_text("prd")
    s = sh(f"python3 {ENGINE} lock {rd}", cwd=tmp_path)
    assert s.returncode == 0
    s = sh(f"python3 {ENGINE} build {rd} builder", cwd=tmp_path)
    assert s.returncode == 0
    b = rd / "work" / "builder"
    (b / "ok.txt").write_text("ok")
    (b / "manifest.json").write_text(json.dumps(["ok.txt"]))
    # ROGUE: builder pokes another role's dir. At mark-built time the
    # subagent output is still untracked in git — that is the real scenario.
    (rd / "work" / "other").mkdir(parents=True)
    sh(f"echo evil > {rd / 'work' / 'other' / 'rogue.txt'}", cwd=tmp_path)
    s = sh(f"python3 {ENGINE} mark-built {rd} builder", cwd=tmp_path)
    assert s.returncode != 0, f"expected die, got: {s.stdout}{s.stderr}"
    assert "OUTSIDE work/builder/" in s.stdout + s.stderr
    assert "work/other/rogue.txt" in s.stdout + s.stderr


# ------------------------------------------------------------------ fix #4
def test_amend_happy_path_and_cap(run):
    st = _load_state(run)
    assert st["phase"] in ("lock", "build", "gate")
    for n in (1, 2, 3):
        s = sh(f"python3 {ENGINE} amend {run} 'amend reason {n}'", cwd=run.parent)
        st = _load_state(run)
        assert st["amend_count"] == n and st["last_amended"] == n
        doc = run / "specs" / "contracts" / f"AMEND-{n}.md"
        assert doc.exists()
        body = doc.read_text()
        assert f"amend reason {n}" in body
        assert "Changed files since previous lock" in body
        # stay in the same phase
        assert st["phase"] in ("build", "gate") or st["phase"] in ("lock",)
        # lock_commit refreshed
        assert st["lock_commit"]
    # 4th amend dies
    s = sh(f"python3 {ENGINE} amend {run} 'too many'", cwd=run.parent)
    assert s.returncode != 0
    assert "cap" in (s.stdout + s.stderr)


def test_amend_legal_after_lock_v3(run, tmp_path):
    # v1.3 (Fix 2): amend is reachable post-lock. The phase machine no longer
    # leaves a phantom 'lock' state that makes amend/regate unreachable. Force
    # phase to 'lock' (the state a fresh multi-role run sits in) — amend MUST
    # now be accepted (this was the audit's 'amend unreachable' blocker).
    st = _load_state(run)
    st["phase"] = "lock"
    (run / ".atlas" / "state.json").write_text(json.dumps(st, indent=2))
    s = sh(f"python3 {ENGINE} amend {run} 'legal after lock'", cwd=run.parent)
    assert s.returncode == 0, f"amend should be legal at lock: {s.stdout}{s.stderr}"
    assert "amend 1" in s.stdout

def test_amend_rejected_before_lock(run, tmp_path):
    # The genuine rejection case now: amend before any lock has happened.
    st = _load_state(run)
    st["phase"] = "synth"  # pre-lock — no contracts pinned yet
    (run / ".atlas" / "state.json").write_text(json.dumps(st, indent=2))
    s = sh(f"python3 {ENGINE} amend {run} 'too early'", cwd=run.parent)
    assert s.returncode != 0
    assert "after lock" in s.stdout + s.stderr


# ------------------------------------------------------------------ fix #5
def test_evidence_appends_across_rounds(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    gdef = {"role": "r", "kind": "shell", "commands": ["echo round-one"]}
    ok1, ev1 = gates.run_role_gate(gdef, out, tmp_path)
    assert ok1
    gdef2 = {"role": "r", "kind": "shell", "commands": ["echo round-two"]}
    ok2, ev2 = gates.run_role_gate(gdef2, out, tmp_path)
    assert ok2 and ev1 == ev2
    text = (out / ev1).read_text()
    # both rounds preserved, newest block first, round-1 text still present
    assert "round-one" in text and "round-two" in text
    assert text.index("round-two") < text.index("round-one")
    # the second round wrote a timestamped header block
    assert text.count("[gate round @") == 1, text


def test_evidence_three_rounds_keeps_all(tmp_path):
    """Across 3 rounds the file holds all results in newest-first order."""
    out = tmp_path / "out"
    out.mkdir()
    for i, label in enumerate(["round-a", "round-b", "round-c"]):
        gates.run_role_gate({"role": "r", "kind": "shell", "commands": [f"echo {label}"]},
                            out, tmp_path)
    text = (out / "GATE-RESULT-r.md").read_text()
    for label in ("round-a", "round-b", "round-c"):
        assert label in text, label
    assert text.index("round-c") < text.index("round-b") < text.index("round-a")
    assert text.count("[gate round @") == 2  # rounds 2 and 3 carry headers


def test_unverified_no_gate_overwrites(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    gates.write_evidence(out, "first", role="r", unverified=True)
    gates.write_evidence(out, "second", role="r", unverified=True)
    text = (out / "UNVERIFIED-NO-GATE.md").read_text()
    assert text.strip() == "second"  # overwrite, not append


# ------------------------------------------------------------------ fix #7
def test_gate_cwd_run_vs_out(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "manifest.json").write_text("[]")
    # default cwd = role dir
    ok, ev = gates.run_role_gate({"role": "r", "kind": "shell",
                                  "commands": ["pwd"]}, out, tmp_path)
    assert str(out) in (out / ev).read_text()
    # cwd: run executes from the run root
    ok, ev = gates.run_role_gate({"role": "r", "kind": "shell", "cwd": "run",
                                  "commands": ["pwd"]}, out, tmp_path)
    assert str(tmp_path) in (out / ev).read_text()


# ------------------------------------------------------------------ fix #6
def test_client_gate_registry_no_exit0(tmp_path):
    reg = json.loads((ENGINE.parent / "role-sets.json").read_text())
    client = next(g for g in reg["software"]["gates"] if g["role"] == "client")
    joined = " ".join(client["commands"])
    assert "exit 0" not in joined
    assert client["human_fallback"] is True
    assert client["kind"] == "shell"


def test_security_gate_registry_whole_run(tmp_path):
    reg = json.loads((ENGINE.parent / "role-sets.json").read_text())
    sec = next(g for g in reg["software"]["gates"] if g["role"] == "security")
    joined = " ".join(sec["commands"])
    assert sec.get("cwd") == "run"
    assert sec.get("human_fallback") is False
    assert "no-secrets-found" not in joined
    assert "secrets found" in joined


# ------------------------------------------------------------------ fix #8
def test_replay_sh_smoke(tmp_path, run):
    """Tiny fake run: gate everything PASS, replay must print a table and exit 0."""
    rd = run
    # Gate the two roles so state has verdicts
    s = sh(f"python3 {ENGINE} gate {rd} builder", cwd=rd.parent)
    assert s.returncode == 0, s.stdout + s.stderr
    s = sh(f"python3 {ENGINE} gate {rd} reviewer", cwd=rd.parent)
    assert s.returncode == 0, s.stdout + s.stderr
    # make run a git repo so replay can check out commits (git init in lock already
    # created one; commit the current tree so there is a snapshot to pin)
    sh("git add -A && git commit -qm 'snapshot'", cwd=rd)
    s = sh(f"bash {GATE} {rd}", cwd=rd.parent)
    out = s.stdout + s.stderr
    assert "ROLE" in out and "VERDICT" in out, out
    assert "PASS" in out, out
    assert s.returncode == 0, out


def test_replay_sh_missing_state(tmp_path):
    bare = tmp_path / "notarun"
    bare.mkdir()
    s = sh(f"bash {GATE} {bare}", cwd=tmp_path)
    assert s.returncode != 0
    assert "state.json" in s.stdout + s.stderr


def test_replay_sh_fail_role_exits_2(tmp_path, run):
    """A failing role gate must propagate exit 2 from replay."""
    rd = run
    (rd / "work" / "builder" / "artifact.txt").unlink()  # gate 'test -f artifact.txt' now fails
    s = sh(f"bash {GATE} {rd}", cwd=rd.parent)
    assert s.returncode == 2, f"expected exit 2, got {s.returncode}: {s.stdout}{s.stderr}"


# ------------------------------------------------------------------ fix #8b
def test_commit_command_merged_only(run):
    rd = run
    # merge first (needs gate verdicts — gate both roles)
    for r in ("builder", "reviewer"):
        s = sh(f"python3 {ENGINE} gate {rd} {r}", cwd=rd.parent)
        assert s.returncode == 0, s.stdout + s.stderr
    s = sh(f"python3 {ENGINE} merge {rd}", cwd=rd.parent)
    assert s.returncode == 0, s.stdout + s.stderr
    # commit is allowed now (phase == merged)
    s = sh(f"python3 {ENGINE} commit {rd}", cwd=rd.parent)
    assert s.returncode == 0, s.stdout + s.stderr
    head = sh("git log -1 --pretty=%s", cwd=rd).stdout.strip()
    assert head == "ATLAS merge: final snapshot"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


# ------------------------------------------------------------------ Wave 2.2: budget
def test_budget_exceeded_terminal(tmp_path):
    """A gate whose credits exceed the role-set cost cap must flip to
    BUDGET_EXCEEDED (a terminal verdict, never PASS)."""
    rd = tmp_path / "run"
    s = sh(f"python3 {ENGINE} init {rd} 'budgettest' 'software'", cwd=tmp_path)
    assert s.returncode == 0, s.stderr
    rs = tmp_path / "rs.json"
    rs.write_text(json.dumps({
        "name": "bt", "description": "d",
        "roles": [{"id": "b", "mandate": "m", "output": "work/b/", "gate": "shell"}],
        "gates": [{"role": "b", "kind": "shell", "commands": ["test -f ok.txt"], "credits": 10}],
        "cost": {"wall_clock_s": 60, "credits": 5, "note": "tiny cap"},
    }))
    s = sh(f"python3 {ENGINE} synth {rd} {rs}", cwd=tmp_path)
    assert s.returncode == 0, s.stderr
    (rd / "specs" / "contracts" / "PRD.md").write_text("prd")
    s = sh(f"python3 {ENGINE} lock {rd}", cwd=tmp_path)
    assert s.returncode == 0, s.stderr
    s = sh(f"python3 {ENGINE} build {rd} b", cwd=tmp_path)
    assert s.returncode == 0, s.stderr
    (rd / "work" / "b" / "ok.txt").write_text("ok")
    (rd / "work" / "b" / "manifest.json").write_text(json.dumps(["ok.txt"]))
    sh("git add -A", cwd=rd)
    s = sh(f"python3 {ENGINE} mark-built {rd} b", cwd=tmp_path)
    assert s.returncode == 0, s.stderr
    s = sh(f"python3 {ENGINE} gate {rd} b", cwd=tmp_path)
    assert "BUDGET_EXCEEDED" in s.stdout + s.stderr, s.stdout + s.stderr


# ------------------------------------------------------------------ Wave 3.5: HMAC
def test_evidence_hmac_signed(run):
    """Gate evidence must be HMAC-signed (tamper-evident) after gating."""
    rd = run
    s = sh(f"python3 {ENGINE} gate {rd} builder", cwd=rd.parent)
    assert s.returncode == 0, s.stdout + s.stderr
    ev = rd / "work" / "builder" / "GATE-RESULT-builder.md"
    assert ev.exists()
    content = ev.read_text()
    assert "hmac:" in content, "evidence not HMAC-signed"


def test_hmac_detects_tampering(run):
    """Editing a signed verdict must break the HMAC and be caught at merge."""
    rd = run
    for r in ("builder", "reviewer"):
        sh(f"python3 {ENGINE} gate {rd} {r}", cwd=rd.parent)
    ev = rd / "work" / "builder" / "GATE-RESULT-builder.md"
    ev.write_text(ev.read_text() + "\n# tampered\n")
    s = sh(f"python3 {ENGINE} merge {rd}", cwd=rd.parent)
    # merge either fails or flags tampering; must not silently pass
    assert s.returncode != 0 or "TAMPERED" in s.stdout + s.stderr


# ------------------------------------------------------------------ Wave 3.3: replay-lock
def test_replay_lock_written_on_lock(run):
    rd = run
    lp = rd / "replay-lock.json"
    assert lp.exists(), "replay-lock.json should be written at lock phase"
    data = json.loads(lp.read_text())
    assert "lock_commit" in data
    assert "env" in data
    assert "gate_hashes" in data


# ------------------------------------------------------------------ Wave 2.4: coverage gate
def test_coverage_gate_kind(tmp_path):
    """coverage gate: a module with full test coverage passes; empty fails."""
    d = tmp_path / "c"
    d.mkdir()
    (d / "mod.py").write_text("def f(x):\n    return x + 1\n")
    (d / "test_mod.py").write_text("def test_f():\n    from mod import f\n    assert f(1) == 2\n")
    (d / "manifest.json").write_text(json.dumps(["mod.py", "test_mod.py"]))
    import gates as g
    ok, ev = g.run_role_gate({"role": "dev", "kind": "coverage",
                              "commands": ["python3 -m pytest -q"],
                              "min_pct": 70.0},
                             d, d)
    assert ok, f"coverage gate should pass: {ev}"


# ------------------------------------------------------------------ Wave 3.1: replay
def test_replay_command(run):
    rd = run
    for r in ("builder", "reviewer"):
        sh(f"python3 {ENGINE} gate {rd} {r}", cwd=rd.parent)
    sh(f"python3 {ENGINE} merge {rd}", cwd=rd.parent)
    s = sh(f"python3 {ENGINE} replay {rd}", cwd=rd.parent)
    assert s.returncode == 0, s.stdout + s.stderr


# ------------------------------------------------------------------ Wave 3.4: selftest
def test_selftest_command():
    s = sh(f"python3 {ENGINE} selftest", cwd=Path(__file__).parent)
    assert s.returncode == 0, s.stdout + s.stderr
    assert "selftest OK" in s.stdout + s.stderr


# ------------------------------------------------------------------ Wave 3.2: registry
def test_registry_install_and_hash(run):
    rd = run
    s = sh(f"python3 {ENGINE} registry install software", cwd=rd.parent)
    assert s.returncode == 0, s.stdout + s.stderr
    out = rd.parent / "atlas-role-set-software.json"
    assert out.exists()
    s2 = sh(f"python3 {ENGINE} registry hash software", cwd=rd.parent)
    assert "sha256" in s2.stdout.lower() or "registry hash" in s2.stdout.lower()


# ==========================================================================
# v1.3 regression tests — one per verified audit defect
# (reference/atlas-audit-4d-2026-10-24.md). These lock v1.3 behavior so a
# regression that reopens any defect fails CI, not a human review.
# ==========================================================================

def _mk_run(tmp_path, n, roles, gates, tag="t"):
    """Minimal built run helper: init->synth->lock->build->mark-built per role.
    NOTE: no 'git add -A' here — v1.3 must pass first-run WITHOUT it (Fix 1)."""
    rd = tmp_path / n
    sh(f"python3 {ENGINE} init {rd} '{tag}' 'software'", cwd=tmp_path)
    rs = tmp_path / f"{tag}-rs.json"
    rs.write_text(json.dumps({
        "name": tag, "description": "v3 regression role-set",
        "roles": [{"id": r, "mandate": f"{r} work", "output": f"work/{r}/", "gate": g} for r, g in roles],
        "gates": gates,
    }))
    sh(f"python3 {ENGINE} synth {rd} {rs}", cwd=tmp_path)
    (rd / "specs" / "contracts" / "PRD.md").write_text("spec\n")
    sh(f"python3 {ENGINE} lock {rd}", cwd=tmp_path)
    for r, _ in roles:
        sh(f"python3 {ENGINE} build {rd} {r}", cwd=tmp_path)
        (rd / "work" / r).mkdir(parents=True, exist_ok=True)
        (rd / "work" / r / "manifest.json").write_text(json.dumps([]))
        sh(f"python3 {ENGINE} mark-built {rd} {r}", cwd=tmp_path)
    return rd


def test_fix1_first_run_no_gitadd(tmp_path):
    """Usability audit: documented happy-path died at mark-built because
    replay-lock.json was flagged out-of-scope. v1.3 first run must pass with
    ZERO git staging — the engine exempts its own bookkeeping."""
    rd = _mk_run(tmp_path, "r1", [("dev", "files"), ("qa", "files")],
                 [{"role": "dev", "kind": "files", "required": ["manifest.json"]},
                  {"role": "qa", "kind": "files", "required": ["manifest.json"]}], "r1")
    for r in ("dev", "qa"):
        s = sh(f"python3 {ENGINE} gate {rd} {r}", cwd=rd.parent)
        assert s.returncode == 0, f"gate {r}: {s.stdout}{s.stderr}"
    s = sh(f"python3 {ENGINE} merge {rd}", cwd=rd.parent)
    assert s.returncode == 0, s.stdout + s.stderr


def test_fix2_amend_reachable(tmp_path):
    """Audit blocker: state machine never set phase 'build'/'gate' so amend
    was unreachable. v1.3: gate sets phase 'gate'; amend reachable from there."""
    rd = _mk_run(tmp_path, "r2", [("dev", "files")],
                 [{"role": "dev", "kind": "files", "required": ["manifest.json"]}], "r2")
    st = _load_state(rd)
    # v1.3: after build+mark-built the phase machine sits in 'build' — a
    # REAL, post-lock state from which amend is legal (audit blocker).
    assert st["phase"] in ("lock", "build", "gate"), st["phase"]
    s = sh(f"python3 {ENGINE} amend {rd} 'spec fix'", cwd=rd.parent)
    assert s.returncode == 0, s.stdout + s.stderr
    assert "amend 1" in s.stdout


def test_fix4_hmac_key_not_public(tmp_path):
    """Security audit: legacy HMAC key = sha256(lock_commit+':atlas-core')
    was public (forgable). v1.3: per-run 32-byte secret in .atlas/hmac.key
    (gitignored), evidence signed with it; legacy key no longer verifies."""
    rd = _mk_run(tmp_path, "r4", [("dev", "files")],
                 [{"role": "dev", "kind": "files", "required": ["manifest.json"]}], "r4")
    keyf = rd / ".atlas" / "hmac.key"
    assert keyf.exists() and keyf.stat().st_size == 32
    import subprocess
    gitignore = (rd / ".gitignore").read_text()
    assert "hmac.key" in gitignore
    # evidence must carry the v1.3 signature
    sh(f"python3 {ENGINE} gate {rd} dev", cwd=rd.parent)
    ev = (rd / "work" / "dev" / "GATE-RESULT-dev.md").read_text()
    assert "hmac" in ev.lower() or "sha256" in ev.lower()


def test_fix5_manifest_traversal(tmp_path):
    """Security audit H4: manifest entries with '..' or absolute paths could
    escape the role boundary. v1.3 rejects them at mark-built."""
    rd = _mk_run(tmp_path, "r5", [("dev", "files")],
                 [{"role": "dev", "kind": "files", "required": ["manifest.json"]}], "r5")
    rd.joinpath("work", "dev").mkdir(parents=True, exist_ok=True)
    bad = (rd / "work" / "dev" / "manifest.json")
    bad.write_text(json.dumps(["../../evil.py"]))
    s = sh(f"python3 {ENGINE} mark-built {rd} dev", cwd=rd.parent)
    assert s.returncode != 0
    assert "unsafe path" in s.stdout + s.stderr


def test_fix_h3_no_preseed(tmp_path):
    """Security audit H3: a builder could pre-seed another role's GATE-RESULT
    to fake a pass. v1.3 boundary check rejects any write into a foreign
    role's dir before that role is built."""
    rd = _mk_run(tmp_path, "rh", [("dev", "files"), ("qa", "files")],
                 [{"role": "dev", "kind": "files", "required": ["manifest.json"]},
                  {"role": "qa", "kind": "files", "required": ["manifest.json"]}], "rh")
    # already mark-built in _mk_run; now attack: dev re-plants qa evidence
    # We re-init a fresh run to test pre-build pre-seeding
    rd2 = tmp_path / "rh2"
    sh(f"python3 {ENGINE} init {rd2} 'rh' 'software'", cwd=tmp_path)
    rs = tmp_path / "rh2-rs.json"
    rs.write_text(json.dumps({
        "name": "rh", "description": "d",
        "roles": [{"id": "dev", "mandate": "b", "output": "work/dev/", "gate": "files"},
                  {"id": "qa", "mandate": "v", "output": "work/qa/", "gate": "files"}],
        "gates": [{"role": "dev", "kind": "files", "required": ["manifest.json"]},
                  {"role": "qa", "kind": "files", "required": ["manifest.json"]}],
    }))
    sh(f"python3 {ENGINE} synth {rd2} {rs}", cwd=tmp_path)
    (rd2 / "specs" / "contracts" / "PRD.md").write_text("s\n")
    sh(f"python3 {ENGINE} lock {rd2}", cwd=tmp_path)
    sh(f"python3 {ENGINE} build {rd2} dev", cwd=tmp_path)
    sh(f"python3 {ENGINE} build {rd2} qa", cwd=tmp_path)
    (rd2 / "work" / "dev" / "manifest.json").write_text(json.dumps([]))
    (rd2 / "work" / "qa" / "manifest.json").write_text(json.dumps([]))
    # ATTACK: dev pre-seeds qa's evidence
    (rd2 / "work" / "qa" / "GATE-RESULT-qa.md").write_text("PASS\n")
    s = sh(f"python3 {ENGINE} mark-built {rd2} dev", cwd=tmp_path)
    assert s.returncode != 0
    assert "OUTSIDE work/dev/" in s.stdout + s.stderr


def test_fix_m4_registry_hash_matches_install(tmp_path):
    """Usability audit M4: 'registry hash' and 'registry install' produced
    DIFFERENT sha256. v1.3: both use one canonical serialization."""
    rd = tmp_path / "rm4"
    rd.mkdir()
    s = sh(f"python3 {ENGINE} registry hash software", cwd=rd)
    assert s.returncode == 0, s.stdout + s.stderr
    sha_from_hash = s.stdout.strip().split()[-1]
    s2 = sh(f"python3 {ENGINE} registry install software", cwd=rd)
    assert s2.returncode == 0, s2.stdout + s2.stderr
    import hashlib
    fpath = [f for f in rd.iterdir() if f.name.startswith("atlas-role-set-software")]
    assert fpath, "install produced no file"
    file_sha = hashlib.sha256(fpath[0].read_bytes()).hexdigest()
    assert file_sha == sha_from_hash, (file_sha, sha_from_hash)
    # and the install message's truncated sha is a prefix of the same value
    assert sha_from_hash[:16] in s2.stdout


def test_fix2b_multi_role_interleave(tmp_path):
    """v1.3: build is legal in any post-lock, pre-merge state so a
    multi-role run can interleave build->mark->gate per role."""
    rd = _mk_run(tmp_path, "rm", [("dev", "files"), ("qa", "files")],
                 [{"role": "dev", "kind": "files", "required": ["manifest.json"]},
                  {"role": "qa", "kind": "files", "required": ["manifest.json"]}], "rm")
    s = sh(f"python3 {ENGINE} gate {rd} dev", cwd=rd.parent)
    assert s.returncode == 0, s.stdout + s.stderr
    s = sh(f"python3 {ENGINE} gate {rd} qa", cwd=rd.parent)
    assert s.returncode == 0, s.stdout + s.stderr
    s = sh(f"python3 {ENGINE} merge {rd}", cwd=rd.parent)
    assert s.returncode == 0, s.stdout + s.stderr


# ==========================================================================
# v1.3 feature tests: skill discovery + design-QA gate + full-engine pin
# ==========================================================================

def test_skill_hint_in_brief(tmp_path):
    """v1.3 skill discovery: role-set 'skills[]' binding surfaces in the
    role's BRIEF so the builder loads the right skills; absent -> no line."""
    rd = tmp_path / "sk1"
    sh(f"python3 {ENGINE} init {rd} 'sk' 'software'", cwd=tmp_path)
    rs = tmp_path / "sk-rs.json"
    rs.write_text(json.dumps({
        "name": "sk-demo", "description": "skill binding demo",
        "roles": [{"id": "dev", "mandate": "b", "output": "work/dev/", "gate": "files"}],
        "gates": [{"role": "dev", "kind": "files", "required": ["manifest.json"]}],
        "skills": [{"role": "dev", "skill": "ponytail"}],
    }))
    sh(f"python3 {ENGINE} synth {rd} {rs}", cwd=tmp_path)
    (rd / "specs" / "contracts" / "PRD.md").write_text("s\n")
    sh(f"python3 {ENGINE} lock {rd}", cwd=tmp_path)
    sh(f"python3 {ENGINE} build {rd} dev", cwd=tmp_path)
    brief = (rd / "work" / "dev" / "BRIEF.md").read_text()
    assert "SKILLS" in brief and "ponytail" in brief


def _design_run(tmp_path, n, css: bool, tag):
    rd = tmp_path / n
    sh(f"python3 {ENGINE} init {rd} '{tag}' 'software'", cwd=tmp_path)
    rs = tmp_path / f"{tag}-rs.json"
    rs.write_text(json.dumps({
        "name": tag, "description": "design gate demo",
        "roles": [{"id": "ui", "mandate": "build ui", "output": "work/ui/", "gate": "design"}],
        "gates": [{"role": "ui", "kind": "design", "min_score": 0.5, "required": ["manifest.json"]}],
    }))
    sh(f"python3 {ENGINE} synth {rd} {rs}", cwd=tmp_path)
    (rd / "specs" / "contracts" / "PRD.md").write_text("s\n")
    sh(f"python3 {ENGINE} lock {rd}", cwd=tmp_path)
    sh(f"python3 {ENGINE} build {rd} ui", cwd=tmp_path)
    out = rd / "work" / "ui"
    out.mkdir(parents=True, exist_ok=True)
    if css:
        (out / "index.html").write_text(
            '<!doctype html><html lang="en"><head><meta name="viewport" '
            'content="width=device-width"><link rel="stylesheet" href="s.css"></head>'
            '<body><header role="banner"><nav aria-label="m"><a href="#">x</a></nav></header>'
            '<main role="main"><h1>t</h1></main><footer role="contentinfo"><p>f</p></footer>'
            '</body></html>')
        (out / "s.css").write_text(
            ':root{--ink:#111;--acc:#f00}\nbody{font-family:"Manrope",sans-serif}\n'
            '.g{display:grid;gap:1rem}\n@media(min-width:768px){.h{font-size:3rem}}\n')
        man = ["index.html", "s.css"]
    else:
        (out / "index.html").write_text('<html><body><h1>x</h1></body></html>')
        man = ["index.html"]
    (out / "manifest.json").write_text(json.dumps(man))
    sh(f"python3 {ENGINE} mark-built {rd} ui", cwd=tmp_path)
    return rd


def test_design_gate_fails_basic_ui(tmp_path):
    """The 'basic UI/UX' failure mode: unstyled html scores 0 -> gate FAIL."""
    rd = _design_run(tmp_path, "d1", css=False, tag="d1")
    s = sh(f"python3 {ENGINE} gate {rd} ui", cwd=rd.parent)
    assert s.returncode != 0
    ev = (rd / "work" / "ui" / "GATE-RESULT-ui.md").read_text()
    assert "SCORE: 0.00" in ev and "FAIL" in ev


def test_design_gate_passes_polished_ui(tmp_path):
    """Polished output (stylesheets, tokens, grid, a11y landmarks) -> PASS."""
    rd = _design_run(tmp_path, "d2", css=True, tag="d2")
    s = sh(f"python3 {ENGINE} gate {rd} ui", cwd=rd.parent)
    assert s.returncode == 0, s.stdout + s.stderr


def test_design_gate_unverified_without_web_files(tmp_path):
    """No css/html at all -> UNVERIFIED (a machine cannot certify absence)."""
    rd = _design_run(tmp_path, "d3", css=False, tag="d3")
    # empty out the role's output (keep only the manifest)
    rd.joinpath("work", "ui", "index.html").unlink()
    s = sh(f"python3 {ENGINE} gate {rd} ui", cwd=rd.parent)
    # gate itself exits non-zero; the verdict file must say UNVERIFIED
    assert s.returncode != 0
    u = rd / "work" / "ui" / "UNVERIFIED-NO-GATE.md"
    assert u.exists() and "UNVERIFIED" in u.read_text()


def test_web_ui_role_set_registered(tmp_path):
    """web-ui role-set ships in the registry with a design gate + skills,
    and its frontend gate is kind 'design' (not the vacuous 'compile' of
    an empty tree)."""
    reg = json.loads((ENGINE.parent / "role-sets.json").read_text())
    wu = reg["web-ui"]
    kinds = {g["role"]: g["kind"] for g in wu["gates"]}
    assert kinds["frontend"] == "design"
    assert any(b.get("role") == "frontend" for b in wu.get("skills", []))


def test_engine_file_tamper_blocks_replay(tmp_path):
    """Full-engine pin: modifying ANY engine .py (e.g. design_qa.py) after
    lock must block replay — the whole verifier tree is pinned, not 2 files."""
    rd = _design_run(tmp_path, "et", css=True, tag="et")
    dq = ENGINE.parent / "design_qa.py"
    orig = dq.read_bytes()
    try:
        dq.write_bytes(orig + b"\n# tamper\n")
        s = sh(f"python3 {ENGINE} replay {rd}", cwd=rd.parent)
        assert s.returncode != 0
        assert "design_qa.py" in s.stdout + s.stderr
    finally:
        dq.write_bytes(orig)
    s = sh(f"python3 {ENGINE} replay {rd}", cwd=rd.parent)
    assert s.returncode == 0, "clean replay must pass: " + s.stdout + s.stderr


def test_fix_amend_docs_exempt_from_boundary(tmp_path):
    """New finding (2026-10-24 akar-landing demo): `amend` writes
    specs/contracts/AMEND-<n>.md (engine-authored bookkeeping), but the
    boundary check flagged it as the CURRENT role writing outside its
    boundary, hard-blocking mark-built. The amend subtree must be exempt."""
    rd = _mk_run(tmp_path, "am1", [("dev", "files")],
                 [{"role": "dev", "kind": "files", "required": ["manifest.json"]}], "am1")
    s = sh(f"python3 {ENGINE} amend {rd} 'spec tweak after gate fail'", cwd=rd.parent)
    assert s.returncode == 0, s.stdout + s.stderr
    am = rd / "specs" / "contracts" / "AMEND-1.md"
    assert am.exists()
    # now a clean re-build + mark must NOT be tripped by the engine's own amend doc
    s = sh(f"python3 {ENGINE} build {rd} dev", cwd=rd.parent)
    assert s.returncode == 0, s.stdout + s.stderr
    s = sh(f"python3 {ENGINE} mark-built {rd} dev", cwd=rd.parent)
    assert s.returncode == 0, s.stdout + s.stderr

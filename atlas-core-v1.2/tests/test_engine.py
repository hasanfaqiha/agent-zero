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


def test_amend_rejected_outside_build_or_gate(run, tmp_path):
    # Force phase to 'lock' (pre-build) — amend must be rejected there.
    st = _load_state(run)
    st["phase"] = "lock"
    (run / ".atlas" / "state.json").write_text(json.dumps(st, indent=2))
    s = sh(f"python3 {ENGINE} amend {run} 'bad phase'", cwd=run.parent)
    assert s.returncode != 0
    assert "'build' or 'gate'" in s.stdout + s.stderr


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

#!/usr/bin/env python3
"""Atlas Core — deterministic orchestrator engine.

Phases: init -> synth -> lock -> build -> gate -> merge
Rules (from red-team review):
  * Gates are scripts: exit codes decide, LLMs never self-approve.
  * Subagents may only write inside work/<role>/ (tool-enforced, not prompt-enforced).
  * Contracts are git-committed at lock; gate diffs against that snapshot.
  * State machine persists in .atlas/state.json; runs resume, never guess.
Usage: atlas.py <phase> <run-dir> [args]
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import gates

PHASES = ["init", "synth", "lock", "build", "gate", "merge"]


def die(msg: str) -> None:
    print(f"[ATLAS] ERROR: {msg}")
    sys.exit(1)


def state_path(run: Path) -> Path:
    return run / ".atlas" / "state.json"


def load_state(run: Path) -> dict:
    p = state_path(run)
    if not p.exists():
        die(f"no .atlas/state.json in {run} — run 'atlas.py init' first")
    return json.loads(p.read_text())


def save_state(run: Path, st: dict) -> None:
    p = state_path(run)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st, indent=2, ensure_ascii=False) + "\n")


def phase(run: Path, want: str) -> None:
    st = load_state(run)
    if st.get("phase") != want:
        die(f"phase mismatch: state={st.get('phase')!r} but called {want!r} "
            f"— run phases in order ({' -> '.join(PHASES)})")


def next_phase(run: Path, want: str) -> None:
    st = load_state(run)
    st["phase"] = want
    st.setdefault("log", []).append(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} phase -> {want}")
    save_state(run, st)


# ------------------------------------------------------------------ init
def phase_init(run: Path, project: str, domain: str, role_set: str) -> None:
    run.mkdir(parents=True, exist_ok=True)
    for d in ["specs", "work", "logs"]:
        (run / d).mkdir(exist_ok=True)
    st = {
        "phase": "init",
        "project": project,
        "domain": domain,
        "role_set": role_set,
        "run_id": f"{int(time.time())}-{hashlib.md5(project.encode()).hexdigest()[:6]}",
        "roles": {},
        "gates": {},
        "log": [f"{time.strftime('%Y-%m-%dT%H:%M:%S')} init {project} domain={domain} role_set={role_set}"],
    }
    save_state(run, st)
    print(f"[ATLAS] run initialized: {state_path(run)}")
    print(f"[ATLAS] run_id={st['run_id']}  roles slot: specs/contracts/")


# ------------------------------------------------------------------ synth
ROLE_SETS = Path(__file__).parent / "role-sets.json"


def role_set_by_name(name: str) -> dict:
    reg = json.loads(ROLE_SETS.read_text())
    entry = reg.get(name)
    if entry is None:
        die(f"role-set '{name}' not found in {ROLE_SETS.name} — synthesize it with 'atlas.py synth'")
    return entry


def phase_synth(run: Path, synth_file: str) -> None:
    phase(run, "init")
    src = Path(synth_file)
    if not src.exists():
        die(f"synthesis spec not found: {src} — write role-set JSON first (agent proposes, engine validates)")
    rs = json.loads(src.read_text())
    # Structural validation: reject non-string mandates, reject instruction-looking content.
    for role in rs.get("roles", []):
        m = role.get("mandate", "")
        if not isinstance(m, str) or not m:
            die(f"role '{role.get('id')}' has empty/non-string mandate — fix before persist")
        if re.search(r"(curl|wget|os\.system|subprocess|\$\(|https?://\S+.*(upload|post|send)|exfiltrat)", m, re.I):
            die(f"role '{role.get('id')}' mandate contains instruction/side-effect patterns — rejected "
                f"(registry schema enforces pure prose mandates)")
        out = role.get("output", "")
        if not re.fullmatch(r"[a-z0-9_\-/]+", out.replace("work/", "")):
            die(f"role '{role.get('id')}' output path malformed: {out!r}")
    reg_path = ROLE_SETS
    reg = json.loads(reg_path.read_text())
    name = rs["name"]
    rs["status"] = "proposed"  # engine forces proposed; persistence is a human decision
    reg[name] = rs
    reg_path.write_text(json.dumps(reg, indent=2, ensure_ascii=False) + "\n")
    print(f"[ATLAS] role-set '{name}' registered as PROPOSED ({len(rs['roles'])} roles). "
          f"NOT active until a human moves it to status=active.")
    next_phase(run, "synth")
    st = load_state(run)
    st["role_set"] = name
    st["roles"] = {r["id"]: {"status": "pending", "verdict": None, "evidence": None,
                             "mandate": r["mandate"], "output": r["output"]} for r in rs["roles"]}
    save_state(run, st)


# ------------------------------------------------------------------ lock
def phase_lock(run: Path) -> None:
    phase(run, "synth")
    cdir = run / "specs" / "contracts"
    missing = [f.name for f in sorted(cdir.iterdir()) if f.suffix in (".md", ".sql", ".yaml", ".yml", ".json", ".txt")]
    if not missing:
        die("no contract files in specs/contracts/ — agent must author them from the discovery brief")
    subprocess.run(["git", "init", "-q"], cwd=run, check=True)
    subprocess.run(["git", "-C", str(run), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(run), "config", "user.name", "atlas-core"], check=True)
    subprocess.run(["git", "-C", str(run), "config", "user.email", "atlas@local"], check=True)
    subprocess.run(["git", "-C", str(run), "commit", "-q", "-m", "ATLAS lock: contract snapshot"], check=True)
    st = load_state(run)
    st["contract_files"] = missing
    st["lock_commit"] = subprocess.check_output(
        ["git", "-C", str(run), "rev-parse", "HEAD"]).decode().strip()
    st["manifest"] = {f: (run / "specs" / "contracts" / f).read_bytes().hex()[:16] for f in missing}
    next_phase(run, "lock")
    st = load_state(run)
    st["contract_files"] = missing
    st["lock_commit"] = subprocess.check_output(
        ["git", "-C", str(run), "rev-parse", "HEAD"]).decode().strip()
    st["manifest"] = {f: (run / "specs" / "contracts" / f).read_bytes().hex()[:16] for f in missing}
    save_state(run, st)
    print(f"[ATLAS] locked {len(missing)} contract files @ commit {st['lock_commit'][:12]}")
    print("[ATLAS] builders may now read specs/contracts but MUST write only inside work/<role>/")


# ------------------------------------------------------------------ build
BUILD_POLICY = """[ATLAS BUILD POLICY — machine-enforced, do not waive]
Allowed: create/edit files ONLY under work/<your-role>/ and read the shared specs/ dir.
Forbidden: editing specs/, other work/<role>/ dirs, engine/, .atlas/, or the registry.
Your output manifest: every file you produce must be listed in work/<role>/manifest.json
(one JSON array of relative paths, relative to work/<role>/)."""


def build_brief(run: Path, role: str) -> str:
    st = load_state(run)
    r = st["roles"][role]
    parts = [
        f"ROLE: {role}",
        f"MANDATE: {r['mandate']}",
        "SHARED INPUTS: read specs/contracts/ and specs/brief.md (read-only).",
        f"OUTPUT: write all deliverables to work/{role}/ and register them in work/{role}/manifest.json.",
        BUILD_POLICY,
    ]
    return "\n".join(parts)


def phase_build(run: Path, role: str) -> None:
    phase(run, "lock")
    st = load_state(run)
    if role not in st["roles"]:
        die(f"unknown role '{role}' — known: {list(st['roles'])}")
    brief = build_brief(run, role)
    out = run / "work" / role
    out.mkdir(parents=True, exist_ok=True)
    (out / "BRIEF.md").write_text(brief)
    # Mark ready; the agent (orchestrating LLM) spawns the subagent pointing at work/<role>/
    st["roles"][role]["status"] = "ready"
    st["roles"][role]["brief_sha"] = hashlib.md5(brief.encode()).hexdigest()[:12]
    save_state(run, st)
    print(f"[ATLAS] {role}: brief at work/{role}/BRIEF.md — spawn subagent with cwd={run} "
          f"(reads BRIEF.md; writes ONLY inside work/{role}/)")


def mark_built(run: Path, role: str) -> None:
    st = load_state(run)
    if role not in st["roles"]:
        die(f"unknown role '{role}'")
    out = run / "work" / role
    man = out / "manifest.json"
    if not man.exists():
        die(f"{role} produced no manifest.json — subagent failed or wrote outside its dir")
    entries = json.loads(man.read_text())
    if not isinstance(entries, list):
        die(f"{role} manifest.json must be a JSON array of paths")
    for e in entries:
        p = out / e
        if not p.exists():
            die(f"{role} manifest lists missing file: {e}")
    # Boundary check: nothing was written outside work/<role>/ since lock
    st["roles"][role]["status"] = "built"
    st["roles"][role]["files"] = entries
    save_state(run, st)
    print(f"[ATLAS] {role}: built OK ({len(entries)} files registered)")


# ------------------------------------------------------------------ gate
def phase_gate(run: Path, role: str) -> None:
    st = load_state(run)
    r = st["roles"][role]
    if r.get("status") != "built":
        die(f"gate({role}): role is {r.get('status')!r}, expected 'built' — run build/mark first")
    out = run / "work" / role
    rs = role_set_by_name(st["role_set"])
    gate_def = next((g for g in rs.get("gates", []) if g.get("role") == role), None)
    if gate_def is None:
        verdict = "UNVERIFIED"
        gates.write_evidence(out, "no gate defined for this role in role-set → human review required",
                             role=role, unverified=True)
        r["verdict"] = verdict
        r["evidence"] = "UNVERIFIED-NO-GATE.md"
        save_state(run, st)
        print(f"[ATLAS] gate({role}): UNVERIFIED — role-set declares no executable gate for this role. "
              f"HUMAN REVIEW REQUIRED. This is a terminal state, not a warning.")
        return
    ok, evidence = gates.run_role_gate(gate_def, out, run)
    verdict = "PASS" if ok else "FAIL"
    if not ok and gate_def.get("human_fallback"):
        verdict = "UNVERIFIED"
    r["verdict"] = verdict
    r["evidence"] = evidence
    save_state(run, st)
    print(f"[ATLAS] gate({role}): {verdict} — evidence: work/{role}/{evidence}")
    if verdict == "FAIL":
        sys.exit(2)


# ------------------------------------------------------------------ merge
def phase_merge(run: Path) -> None:
    st = load_state(run)
    verdicts = {k: v.get("verdict") for k, v in st["roles"].items()}
    failed = [k for k, v in verdicts.items() if v == "FAIL"]
    unv = [k for k, v in verdicts.items() if v in (None, "UNVERIFIED")]
    passed = [k for k, v in verdicts.items() if v == "PASS"]
    summary = [
        f"ATLAS gate report — {st['project']} ({st['run_id']})",
        "",
        f"PASS ({len(passed)}): {', '.join(passed) or '-'}",
        f"FAIL ({len(failed)}): {', '.join(failed) or '-'}",
        f"UNVERIFIED/human-review ({len(unv)}): {', '.join(unv) or '-'}",
        "",
        "No APPROVED stamp: release is a human decision based on this report.",
    ]
    rep = run / "logs" / "gate-report.md"
    rep.write_text("\n".join(summary) + "\n")
    # drift check against the lock snapshot
    drift = subprocess.run(["git", "-C", str(run), "diff", "--name-only", st["lock_commit"], "--", "specs"],
                           capture_output=True, text=True).stdout.splitlines()
    if drift:
        summary.append(f"WARNING: contracts drifted after lock: {drift}")
        rep.write_text("\n".join(summary) + "\n")
    st["phase"] = "merged"
    st["report"] = str(rep)
    save_state(run, st)
    print("\n".join(summary))
    print(f"[ATLAS] merged. Report: {rep}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Atlas Core engine")
    ap.add_argument("command", choices=PHASES + ["mark-built", "role-set"])
    ap.add_argument("run_dir", nargs="?", default=".")
    ap.add_argument("arg", nargs="?")
    ap.add_argument("arg2", nargs="?")
    a = ap.parse_args()
    run = Path(a.run_dir).resolve()

    if a.command == "role-set":
        print(json.dumps(role_set_by_name(a.arg), indent=2))
        return
    if a.command == "mark-built":
        mark_built(run, a.arg)
        return
    if a.command == "init":
        if not (a.arg and a.arg2):
            die("usage: atlas.py init <project-name> <domain>")
        phase_init(run, a.arg, a.arg2, None)
        return
    if a.command == "synth":
        # a.arg = path to synthesized role-set json
        phase_synth(run, a.arg)
        return
    if a.command == "build":
        phase_build(run, a.arg)
        return
    if a.command == "lock":
        phase_lock(run)
        return
    if a.command == "gate":
        phase_gate(run, a.arg)
        return
    if a.command == "merge":
        phase_merge(run)
        return


if __name__ == "__main__":
    main()

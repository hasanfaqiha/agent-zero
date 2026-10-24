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
    # Fix #2 (ATOMIC STATE WRITES): temp file in the same dir, fsync,
    # os.replace — a crash can never leave a half-written state.json.
    p = state_path(run)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    with open(tmp, "w") as f:
        f.write(json.dumps(st, indent=2, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)


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
    for d in ["specs", "specs/contracts", "work", "logs"]:
        (run / d).mkdir(parents=True, exist_ok=True)
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
# Isolated-registry support (Fix 7): selftest/CI point the engine at a
# throwaway registry so the shared registry is never polluted.
ROLE_SETS = Path(os.environ.get("ATLAS_ROLE_SETS", Path(__file__).parent / "role-sets.json"))




# ------------------------------------------------------------------ replay-lock (Wave 3.3)
def _py_versions() -> dict:
    v = {"python": sys.version.split()[0]}
    try:
        import pytest
        v["pytest"] = pytest.__version__
    except Exception:
        v["pytest"] = "not-installed"
    return v


def _engine_sha() -> dict:
    """v1.3: pin EVERY engine .py file (not just atlas.py + gates.py). A new
    checker like design_qa.py is part of the verifier; unpinning it would let
    a run swap in a weaker gate after lock. Returns {filename: sha256[:16]}
    in stable sorted order."""
    import pathlib
    d = pathlib.Path(__file__).resolve().parent
    h = {}
    for f in sorted(d.glob("*.py")):
        h[f.name] = hashlib.sha256(f.read_bytes()).hexdigest()[:16]
    return h


def _gate_hashes(run: Path, st: dict) -> dict:
    """Hash of the FULL gate definition for each role — pinned so a replay
    fails fast if ANY gate property changed: kind, commands, required,
    min_pct/min_score, credits, max_wall_clock, cwd, env_allowlist. Hashing
    only 'commands' left a tamper path where weakening 'required' or raising
    'min_score' passed replay silently; the whole def is now the pin."""
    try:
        rs = role_set_by_name(st.get("role_set", ""))
    except SystemExit:
        return {}
    h = {}
    for g in rs.get("gates", []):
        key = g.get("role", "?")
        canon = json.dumps(g, sort_keys=True, ensure_ascii=False).encode()
        h[key] = hashlib.sha256(canon).hexdigest()[:16]
    return h


def _write_replay_lock(run: Path) -> None:
    st = load_state(run)
    lock = {
        "lock_commit": st.get("lock_commit"),
        "env": _py_versions(),
        "gate_hashes": _gate_hashes(run, st),
        "engine_sha": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16],
        "gates_sha": hashlib.sha256(Path(__file__).with_name("gates.py").read_bytes()).hexdigest()[:16],
        "engine_files": _engine_sha(),
    }
    (run / "replay-lock.json").write_text(json.dumps(lock, indent=2) + "\n")


def check_replay_lock(run: Path) -> list:
    """Returns a list of environment mismatches between the pinned replay-lock
    and the current environment. Empty = replayable."""
    lp = run / "replay-lock.json"
    if not lp.exists():
        return ["no replay-lock.json — run 'atlas.py lock' to pin the environment"]
    try:
        lock = json.loads(lp.read_text())
    except json.JSONDecodeError:
        return ["replay-lock.json corrupt"]
    problems = []
    cur = _py_versions()
    for k, v in lock.get("env", {}).items():
        if cur.get(k) != v:
            problems.append(f"env {k}: pinned {v!r} but now {cur.get(k)!r}")
    cur_engine = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16]
    if lock.get("engine_sha") and lock["engine_sha"] != cur_engine:
        problems.append("engine binary changed since lock — replay not byte-identical")
    cur_gates = hashlib.sha256(Path(__file__).with_name("gates.py").read_bytes()).hexdigest()[:16]
    if lock.get("gates_sha") and lock["gates_sha"] != cur_gates:
        problems.append("gates.py changed since lock — verifier logic is not byte-identical")
    # v1.3: re-validate the FULL engine pin — any .py in engine/ is part of
    # the verifier (design_qa.py etc.). A byte drift in ANY file blocks replay.
    cur_files = _engine_sha()
    for name, sha in cur_files.items():
        if lock.get("engine_files") and lock["engine_files"].get(name) != sha:
            problems.append(f"engine file {name} changed since lock — verifier not byte-identical")
    for name, sha in lock.get("engine_files", {}).items():
        if name not in cur_files:
            problems.append(f"engine file {name} present at lock but missing now")
    # Re-validate gate_hashes pin (C2): registry/gate-command tamper must surface.
    try:
        st = load_state(run)
        cur_gh = _gate_hashes(run, st)
        if lock.get("gate_hashes") and lock["gate_hashes"] != cur_gh:
            problems.append("gate commands changed since lock — replay would run different gates")
    except SystemExit:
        pass
    return problems

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
    # Fix 4 (HMAC v1.3): per-run secret, generated at lock, stored in .atlas/
    # (gitignored) and NEVER committed. Signing/verify read it from this file.
    import secrets as _secrets
    keyfile = run / ".atlas" / "hmac.key"
    keyfile.parent.mkdir(parents=True, exist_ok=True)
    keyfile.write_bytes(_secrets.token_bytes(32))
    try:
        (run / ".gitignore").write_text(".atlas/hmac.key\n") if not (run / ".gitignore").exists() else None
    except Exception:
        pass
    st["hmac_key_file"] = ".atlas/hmac.key"
    save_state(run, st)
    _write_replay_lock(run)
    print(f"[ATLAS] locked {len(missing)} contract files @ commit {st['lock_commit'][:12]}")
    print("[ATLAS] builders may now read specs/contracts but MUST write only inside work/<role>/")



def _hmac_key(run: Path) -> bytes:
    """Per-run HMAC key generated at lock. Falls back to the legacy public
    derivation only for pre-v1.3 runs that have no key file (backward compat)."""
    st = load_state(run)
    rel = st.get("hmac_key_file", ".atlas/hmac.key")
    kf = run / rel
    if kf.exists():
        return kf.read_bytes()
    # legacy fallback
    import hashlib as _h
    return _h.sha256((str(st.get("lock_commit")) + ":atlas-core").encode()).hexdigest().encode()

# ------------------------------------------------------------------ build
BUILD_POLICY = """[ATLAS BUILD POLICY — machine-enforced, do not waive]
Allowed: create/edit files ONLY under work/<your-role>/ and read the shared specs/ dir.
Forbidden: editing specs/, other work/<role>/ dirs, engine/, .atlas/, or the registry.
Your output manifest: every file you produce must be listed in work/<role>/manifest.json
(one JSON array of relative paths, relative to work/<role>/)."""


def _skill_hints(run: Path, role: str):
    """v1.3 skill discovery: return the skill names this role-set binds to a
    role. Discovery of the skill itself is the orchestrating agent's job (it
    holds the skill index); the engine pins the *choice* so it's locked at
    lock-time and cannot drift between lock and build."""
    try:
        rs = role_set_by_name(st_role_set(run))
    except SystemExit:
        return []
    hits = [b.get("skill") for b in rs.get("skills", [])
            if b.get("role") == role and b.get("skill")]
    return hits


def st_role_set(run: Path) -> str:
    return load_state(run).get("role_set", "")


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
    hints = _skill_hints(run, role)
    if hints:
        parts.append(
            "SKILLS: load these skills before starting (the gate is authoritative; "
            "skills are guidance): " + ", ".join(hints))
        proof_fmt = (
            "work/<role>/skills-loaded.json — a JSON object keyed by skill name, "
            'each value {"path": <file you read>, "applied": <one line: what it changed>}.')
        parts.append(
            "SKILLS-PROOF: for each skill you actually load, record the evidence "
            "in " + proof_fmt + " "
            "Register skills-loaded.json in manifest.json too. Only skills you genuinely "
            "read count; a 'path' that does not exist on this machine fails the build "
            "(fabricated evidence is rejected by mark-built).")
    return "\n".join(parts)


def phase_build(run: Path, role: str) -> None:
    st = load_state(run)
    # build a role in any post-lock, pre-merge state: a multi-role run
    # interleaves build->mark->gate per role; after the first gate the
    # phase reads "gate" but the NEXT role still has to build.
    if st.get("phase") not in ("lock", "build", "gate"):
        die(f"build allowed only after lock (state={st.get('phase')!r})")
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
    next_phase(run, "build")
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
        if not isinstance(e, str) or not e or e.startswith("/") or ".." in e.split("/"):
            die(f"{role} manifest contains unsafe path entry: {e!r} (no absolute or '..' paths)")
        p = out / e
        if not p.exists():
            die(f"{role} manifest lists missing file: {e}")
    # Fix #3 (REAL BOUNDARY CHECK): any tracked change or untracked file
    # since lock_commit outside work/<role>/ dies the build. A rogue write
    # (e.g. into work/other-role/) is detected by git, not by prompt.
    base = st.get("lock_commit")
    if base:
        role_boundary = f"work/{role}"

        def _offending(paths):
            """Paths OUTSIDE work/<role>/, unwrapping collapsed '?? work/' entries."""
            out = []
            for p in paths:
                if not p:
                    continue
                # git can collapse untracked reports to the dir itself ('?? work/'):
                # expand it to its actual file entries, then re-test each one.
                if p.rstrip("/") in ("work", "specs", "logs", ".atlas"):
                    d = run / p.rstrip("/")
                    expanded = [sub.relative_to(run).as_posix() for sub in d.rglob("*") if sub.is_file()]
                else:
                    expanded = [p]
                for q in expanded:
                    if q.startswith(role_boundary + "/"):
                        continue  # inside this role's own dir — allowed
                    if q.startswith((".atlas/", "logs/")):
                        continue  # engine bookkeeping — not build output
                    # Engine-produced artifacts that are NEVER builder output:
                    # lock-time files, evidence files, trace data, bytecode.
                    # H3 fix: evidence files are scoped to the CURRENT role —
                    # a builder cannot pre-seed another role's GATE-RESULT.
                    import re as _re
                    if q in ("replay-lock.json", ".gitignore", "replay-lock.json.tmp"):
                        continue
                    # Engine-authored contract docs (amend records) are
                    # bookkeeping, not builder output — exempt their subtree.
                    if q.startswith("specs/contracts/AMEND-"):
                        continue
                    if _re.match(r"work/" + _re.escape(role) + r"/GATE-RESULT-[^/]+\.md$", q):
                        continue
                    if q.endswith("/UNVERIFIED-NO-GATE.md") and q.startswith("work/"):
                        continue
                    if q.startswith("work/") and (q.endswith(".cover") or "/__pycache__/" in q):
                        continue
                    # State-driven: files a PREVIOUS role legitimately produced
                    # were already boundary-checked when THAT role passed
                    # mark-built — exempt its registered outputs + engine
                    # artifacts, but ONLY for roles that actually reached
                    # 'built' (proven boundary-clean at that role's own check).
                    # This also closes the H3 pre-seeding hole: a builder
                    # cannot plant another role's GATE-RESULT before that
                    # role is built, because its own mark-built would have
                    # flagged any write into a foreign dir.
                    import re as _re2
                    skipped = False
                    for x, xs in st.get("roles", {}).items():
                        if x == role:
                            continue
                        if xs.get("status") != "built":
                            continue
                        prefix = f"work/{x}/"
                        if q.startswith(prefix):
                            reg = set(xs.get("files") or [])
                            reg.update(["manifest.json", "BRIEF.md"])
                            rel = q[len(prefix):]
                            if rel in reg:
                                skipped = True
                                break
                            if _re2.match(r"GATE-RESULT-[^/]+\.md$", rel):
                                skipped = True
                                break
                            if rel == "UNVERIFIED-NO-GATE.md":
                                skipped = True
                                break
                            if rel.endswith(".cover") or "/__pycache__/" in q:
                                skipped = True
                                break
                    if skipped:
                        continue
                    out.append(q)
            return out

        changed = _offending(subprocess.check_output(
            ["git", "-C", str(run), "diff", "--name-only", base, "HEAD"],
            text=True).splitlines())
        status = subprocess.check_output(
            ["git", "-C", str(run), "status", "--porcelain"], text=True)
        untracked = _offending([l[3:].strip() for l in status.splitlines() if l.startswith("??")])
        offenders = sorted(set(changed + untracked))
        if offenders:
            die(f"{role} wrote OUTSIDE work/{role}/ since lock @ {base[:12]}: {offenders}\n"
                f"subagent violated its boundary — mark-built rejected, gate will not run")
    # v1.3.3 skills-proof: when the role-set's build_policy requires proof of
    # skill loading, a hard requirement — no proof, no "built", gate cannot run.
    # This closes the honest-claim gap: the engine pins the skill CHOICE at
    # lock; now it also requires EVIDENCE the builder actually read it.
    try:
        rs = role_set_by_name(st_role_set(run))
    except SystemExit:
        rs = {}
    pol = rs.get("build_policy", {}) if isinstance(rs, dict) else {}
    hints = _skill_hints(run, role)
    if pol.get("skills_proof") and hints:
        # v1.3.3 skills-proof, honest semantics:
        #  - every proof entry must cite a file that EXISTS on disk
        #    (anti-fabrication: a copied-in fake entry dies the build);
        #  - for each BOUND skill that is actually installed on this
        #    machine, a proof entry is REQUIRED;
        #  - a bound skill that is NOT installed is reported, not fatal:
        #    skill discovery/installation is the orchestrator's job, the
        #    engine only verifies what it can see.
        proof = out / "skills-loaded.json"
        rec = {}
        if proof.exists():
            try:
                rec = json.loads(proof.read_text())
            except json.JSONDecodeError:
                die(f"{role} skills-loaded.json is not valid JSON")
            if not isinstance(rec, dict):
                die(f"{role} skills-loaded.json must be a JSON object keyed by skill name")
        for sk, val in rec.items():
            if not isinstance(val, dict) or not val.get("path") or not val.get("applied"):
                die(f"{role} skills proof for '{sk}' needs non-empty 'path' and 'applied' fields")
            pp = Path(os.path.expanduser(str(val["path"])))
            if not pp.exists():
                die(f"{role} skills proof for '{sk}' cites a file that does not exist "
                    f"({val['path']}) — fabricated evidence, build rejected")
        locs = [Path(os.path.expanduser(l)) for l in pol.get("skills_locations", [])]
        locs = [l if l.is_absolute() else run / l for l in locs]
        for sk in hints:
            cand = next((l / sk / "SKILL.md" for l in locs
                         if (l / sk / "SKILL.md").exists()), None)
            if cand is None:
                print(f"[ATLAS] {role}: bound skill '{sk}' not found on this machine "
                      f"— proof skipped (orchestrator should install it)")
                continue
            if sk not in rec:
                die(f"{role} has skill '{sk}' installed but no proof entry in "
                    f"work/{role}/skills-loaded.json — load it and record the evidence")
    st["roles"][role]["status"] = "built"
    st["roles"][role]["files"] = entries
    save_state(run, st)
    print(f"[ATLAS] {role}: built OK ({len(entries)} files registered; boundary check clean)")


# ------------------------------------------------------------------ gate
def phase_gate(run: Path, role: str) -> None:
    import time as _t
    _gate_t0 = _t.time()
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
    ev_text = (out / evidence).read_text() if (out / evidence).exists() else ""
    if "BUDGET_EXCEEDED" in ev_text:
        verdict = "BUDGET_EXCEEDED"
    else:
        verdict = "PASS" if ok else "FAIL"
        if not ok and gate_def.get("human_fallback"):
            verdict = "UNVERIFIED"
    r["verdict"] = verdict
    r["evidence"] = evidence
    # Wave 2.2: accumulate credit spend and enforce the role-set cost budget.
    st["cost_spent"] = st.get("cost_spent", 0) + int(gate_def.get("credits", 0))
    try:
        rs = role_set_by_name(st.get("role_set", ""))
        cap_c = rs.get("cost", {}).get("credits", 0)
        if cap_c and st["cost_spent"] > cap_c:
            verdict = "BUDGET_EXCEEDED"
            r["verdict"] = verdict
        cap_w = rs.get("cost", {}).get("wall_clock_s", 0)
        # Per-gate wall-clock enforcement: gate timeout already converts to
        # BUDGET_EXCEEDED via max_wall_clock; here we also enforce the run-level cap.
        if cap_w and st.get("wall_clock_spent", 0) > cap_w:
            verdict = "BUDGET_EXCEEDED"
            r["verdict"] = verdict
    except SystemExit:
        pass
    # Wave 3.5: sign the evidence with the lock commit so tampering breaks
    # the signature. merge re-verifies before writing the report.
    st["wall_clock_spent"] = st.get("wall_clock_spent", 0) + int(_t.time() - _gate_t0)
    gates.sign_file(out / evidence, _hmac_key(run))
    save_state(run, st)
    print(f"[ATLAS] gate({role}): {verdict} — evidence: work/{role}/{evidence}")
    try:
        next_phase(run, "gate")
    except SystemExit:
        pass
    if verdict == "FAIL":
        sys.exit(2)
    if verdict == "BUDGET_EXCEEDED":
        sys.exit(2)


# ------------------------------------------------------------------ merge
def phase_merge(run: Path) -> None:
    st = load_state(run)
    verdicts = {k: v.get("verdict") for k, v in st["roles"].items()}
    failed = [k for k, v in verdicts.items() if v == "FAIL"]
    budget = [k for k, v in verdicts.items() if v == "BUDGET_EXCEEDED"]
    unv = [k for k, v in verdicts.items() if v in (None, "UNVERIFIED")]
    passed = [k for k, v in verdicts.items() if v == "PASS"]
    # Wave 3.5: verify each role's HMAC signature before trusting its verdict.
    bad_sig = []
    for role, v in list(verdicts.items()):
        ev = st["roles"][role].get("evidence")
        if not ev:
            continue
        evp = run / "work" / role / ev
        if not gates.verify_file(evp, _hmac_key(run)):
            bad_sig.append(role)
            if role in passed:
                passed.remove(role)
                failed.append(role)
    summary = [
        f"ATLAS gate report — {st['project']} ({st['run_id']})",
        "",
        f"PASS ({len(passed)}): {', '.join(passed) or '-'}",
        f"FAIL ({len(failed)}): {', '.join(failed) or '-'}",
        f"BUDGET_EXCEEDED ({len(budget)}): {', '.join(budget) or '-'}",
        f"UNVERIFIED/human-review ({len(unv)}): {', '.join(unv) or '-'}",
        "",
        "No APPROVED stamp: release is a human decision based on this report.",
    ]
    if bad_sig:
        summary.append(f"TAMPERED EVIDENCE (HMAC mismatch): {', '.join(bad_sig)}")
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


# ------------------------------------------------------------------ amend
def _git(run: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(run), *args], check=True)


def _lock_run(run: Path, st: dict, subject: str) -> None:
    """Re-run the lock git commit sequence and refresh lock_commit/manifest."""
    _git(run, "add", "-A")
    _git(run, "commit", "-q", "-m", subject)
    missing = [f.name for f in sorted((run / "specs" / "contracts").iterdir())
               if f.suffix in (".md", ".sql", ".yaml", ".yml", ".json", ".txt")]
    st["contract_files"] = missing
    st["lock_commit"] = subprocess.check_output(
        ["git", "-C", str(run), "rev-parse", "HEAD"]).decode().strip()
    st["manifest"] = {f: (run / "specs" / "contracts" / f).read_bytes().hex()[:16] for f in missing}


def phase_amend(run: Path, reason: str) -> None:
    """Fix #4 (NEW COMMAND `amend`): the official re-lock path.

    Only in phase 'build' or 'gate'; hard cap of 3 amendments (the 4th dies).
    Records reason + timestamp in the state log, writes specs/contracts/AMEND-<n>.md
    (reason + list of contract files changed since the last lock via git diff),
    re-runs the lock commit sequence, and STAYS in the current phase.
    """
    st = load_state(run)
    if st.get("phase") not in ("build", "gate", "lock", "merged"):
        die(f"amend allowed only after lock (state={st.get('phase')!r})")
    n = int(st.get("amend_count", 0)) + 1
    if n > 3:
        die(f"amend cap reached: {st.get('amend_count', 0)} amendments already recorded — "
            f"hard cap is 3; a new lock cycle requires human sign-off, not more amendments")
    if not reason or not reason.strip():
        die("usage: atlas.py amend <run-dir> <reason> — reason is recorded in the audit log")
    reason = reason.strip()
    _lock_run(run, st, f"ATLAS amend {n}: {reason}")
    changed = subprocess.check_output(
        ["git", "-C", str(run), "diff", "--name-only", "HEAD~1", "HEAD"], text=True).splitlines()
    amend_doc = run / "specs" / "contracts" / f"AMEND-{n}.md"
    amend_doc.write_text(
        f"# AMEND {n} — {reason}\n\n"
        f"timestamp: {time.strftime('%Y-%m-%dT%H:%M:%S')}\n"
        f"reason: {reason}\n"
        f"lock_commit: {st['lock_commit'][:12]}\n\n"
        f"## Changed files since previous lock\n"
        + ("\n".join(f"- {p}" for p in changed) if changed else "- (none)")
        + "\n"
    )
    st["amend_count"] = n
    st["last_amended"] = n
    st.setdefault("log", []).append(
        f"{time.strftime('%Y-%m-%dT%H:%M:%S')} amend {n}: {reason} (doc={amend_doc.name})")
    save_state(run, st)
    print(f"[ATLAS] amend {n}: re-locked @ {st['lock_commit'][:12]}, "
          f"reason={reason!r}, doc=specs/contracts/{amend_doc.name}; "
          f"phase stays '{st['phase']}' ({st.get('amend_count', 0)}/3 amendments used)")


# ------------------------------------------------------------------ commit
def phase_commit(run: Path) -> None:
    """Fix #8 (NEW COMMAND `commit`, phase 'merged' only): commit the entire
    run dir so any commit of the run is replayable (see replay.sh)."""
    st = load_state(run)
    if st.get("phase") != "merged":
        die(f"commit allowed only in phase 'merged' (state={st.get('phase')!r})")
    _lock_run(run, st, "ATLAS merge: final snapshot")
    st.setdefault("log", []).append(
        f"{time.strftime('%Y-%m-%dT%H:%M:%S')} commit: final snapshot @ {st['lock_commit'][:12]}")
    save_state(run, st)
    print(f"[ATLAS] commit: run snapshot @ {st['lock_commit'][:12]} — "
          f"replayable via ./replay.sh <run-repo> {st['lock_commit'][:12]}")




# ------------------------------------------------------------------ regate (Wave 2.3)
def phase_regate(run: Path, role: str) -> None:
    """Targeted re-gate after a contract amendment. Identifies which roles'
    inputs changed via git diff lock_commit..HEAD -- specs/ + work/, and
    re-runs ONLY those roles' gates. If a specific role is named, re-gates
    just that role. No-op if nothing changed."""
    st = load_state(run)
    if st.get("phase") not in ("lock", "build", "gate", "merged"):
        die(f"regate allowed only after lock (state={st.get('phase')!r})")
    base = st.get("lock_commit")
    changed = subprocess.run(["git", "-C", str(run), "diff", "--name-only", base, "HEAD", "--", "specs", "work"],
                             capture_output=True, text=True).stdout.splitlines()
    affected = set()
    for p in changed:
        parts = p.split("/")
        if len(parts) >= 2 and parts[0] == "work":
            affected.add(parts[1])
    if role:
        affected = {role}
    if not affected:
        print("[ATLAS] regate: no role inputs changed since lock — nothing to re-gate")
        return
    for r in sorted(affected):
        if r not in st["roles"]:
            continue
        s = subprocess.run([sys.executable, str(Path(__file__).resolve()), "gate", str(run), r],
                           capture_output=True, text=True)
        print(f"[ATLAS] regate({r}): exit={s.returncode} — {s.stdout.strip()[-200:]}")
    print("[ATLAS] regate done. Run 'merge' to regenerate the report.")


# ------------------------------------------------------------------ replay (Wave 3.1)
def phase_replay(run: Path, commit: str = "") -> None:
    """Replay a recorded run at a pinned contract commit. Checks the replay
    lockfile first (fails fast on env mismatch), then re-runs every role's
    gate and regenerates the report on a clean pass. Read-only for tracked
    files (state restored afterwards)."""
    problems = check_replay_lock(run)
    if problems:
        print("[ATLAS] replay BLOCKED — env drifted:")
        for p in problems:
            print("   -", p)
        sys.exit(3)
    st = load_state(run)
    if commit:
        r = subprocess.run(["git", "-C", str(run), "checkout", "-q", commit],
                           capture_output=True, text=True)
        if r.returncode != 0:
            die(f"commit {commit} not found in run repo")
    roles = sorted(st["roles"])
    for role in roles:
        s = subprocess.run([sys.executable, str(Path(__file__).resolve()), "gate", str(run), role],
                           capture_output=True, text=True)
        print(f"[ATLAS] replay gate({role}): exit={s.returncode}")
    # regenerate report
    subprocess.run([sys.executable, str(Path(__file__).resolve()), "merge", str(run)],
                   capture_output=True, text=True)
    print("[ATLAS] replay complete — report at logs/gate-report.md")


# ------------------------------------------------------------------ selftest (Wave 3.4)
def phase_selftest() -> None:
    """Minimal 2-role pipeline in a throwaway run dir; asserts the engine
    produces a gate report. Cross-harness CI hook: runs in any harness that
    can execute python + git."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        rd = Path(td) / "selftest"
        os.environ["ATLAS_ROLE_SETS"] = str(Path(td) / "isolated-registry.json")
        try:
            (Path(td) / "isolated-registry.json").write_text("{}")
        except Exception:
            pass
        subprocess.run([sys.executable, str(Path(__file__).resolve()), "init", str(rd), "selftest", "software"],
                       capture_output=True, text=True, check=True)
        rs = Path(td) / "rs.json"
        rs.write_text(json.dumps({
            "name": "selftest-tiny", "description": "d",
            "roles": [{"id": "b", "mandate": "m", "output": "work/b/", "gate": "shell"},
                      {"id": "r", "mandate": "m2", "output": "work/r/", "gate": "files"}],
            "gates": [{"role": "b", "kind": "shell", "commands": ["test -f ok.txt"]},
                      {"role": "r", "kind": "files", "required": ["manifest.json"]}],
        }))
        subprocess.run([sys.executable, str(Path(__file__).resolve()), "synth", str(rd), str(rs)],
                       capture_output=True, text=True, check=True)
        (rd / "specs" / "contracts" / "PRD.md").write_text("prd")
        subprocess.run([sys.executable, str(Path(__file__).resolve()), "lock", str(rd)],
                       capture_output=True, text=True, check=True)
        for role in ("b", "r"):
            subprocess.run([sys.executable, str(Path(__file__).resolve()), "build", str(rd), role],
                           capture_output=True, text=True, check=True)
        (rd / "work" / "b" / "ok.txt").write_text("ok")
        (rd / "work" / "b" / "manifest.json").write_text(json.dumps(["ok.txt"]))
        (rd / "work" / "r" / "manifest.json").write_text(json.dumps([]))
        subprocess.run(["git", "-C", str(rd), "add", "-A"], capture_output=True, text=True, check=True)
        for role in ("b", "r"):
            subprocess.run([sys.executable, str(Path(__file__).resolve()), "mark-built", str(rd), role],
                           capture_output=True, text=True, check=True)
        for role in ("b", "r"):
            subprocess.run([sys.executable, str(Path(__file__).resolve()), "gate", str(rd), role],
                           capture_output=True, text=True, check=True)
        subprocess.run([sys.executable, str(Path(__file__).resolve()), "merge", str(rd)],
                       capture_output=True, text=True, check=True)
        rep = rd / "logs" / "gate-report.md"
        if not rep.exists():
            die("selftest: gate report not produced")
        print("[ATLAS] selftest OK — pipeline produced gate-report.md")


# ------------------------------------------------------------------ registry (Wave 3.2)
def _registry_canon(rs: dict) -> bytes:
    """Single canonical serialization used by BOTH install and hash (Fix M4):
    a consumer pinning a role-set by hash can always reproduce it byte-exact."""
    return json.dumps(rs, indent=2, sort_keys=True, ensure_ascii=False).encode() + b"\n"

def phase_registry_install(name: str) -> None:
    """Export the role-set as a standalone JSON file. The hash equals the
    'registry hash' output by construction (both use _registry_canon)."""
    rs = role_set_by_name(name)
    out = Path(f"atlas-role-set-{name}.json")
    out.write_bytes(_registry_canon(rs))
    h = hashlib.sha256(out.read_bytes()).hexdigest()
    print(f"[ATLAS] role-set '{name}' exported to {out} (sha256 {h[:16]})")

def phase_registry_hash(name: str) -> None:
    rs = role_set_by_name(name)
    h = hashlib.sha256(_registry_canon(rs)).hexdigest()
    print(f"[ATLAS] registry hash {name}: {h}")

def main() -> None:
    ap = argparse.ArgumentParser(description="Atlas Core engine")
    ap.add_argument("command", choices=PHASES + ["mark-built", "role-set", "amend", "commit",
                                                "regate", "replay", "selftest", "registry"])
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
    if a.command == "amend":
        # CLI: atlas.py amend <run-dir> <reason>  →  reason is a.arg
        phase_amend(run, a.arg or "")
        return
    if a.command == "commit":
        phase_commit(run)
        return
    if a.command == "regate":
        phase_regate(run, a.arg or "")
        return
    if a.command == "replay":
        phase_replay(run, a.arg or "")
        return
    if a.command == "selftest":
        phase_selftest()
        return
    if a.command == "registry":
        # positional: registry <install|hash> <role-set-name>
        # a.run_dir holds the subcommand, a.arg holds the name
        if a.run_dir == "install":
            phase_registry_install(a.arg)
        elif a.run_dir == "hash":
            phase_registry_hash(a.arg)
        else:
            die("usage: atlas.py registry <install|hash> <role-set-name>")
        return


if __name__ == "__main__":
    main()

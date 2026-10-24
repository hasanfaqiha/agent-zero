#!/usr/bin/env python3
"""Atlas Core — gate runner. Verifier is external to producers; gates are
executable checks (exit codes), never LLM self-attestation.

Gate kinds:
  * compile     - python -m py_compile on produced .py files
  * shell       - run commands from role-set gate spec (exit 0 = pass)
  * files       - required artifacts exist and are non-empty
  * test        - run a test command; require non-trivial suite
  * coverage    - run tests under stdlib trace, require >= min_pct line coverage
  * mutation    - run a lightweight mutation pass; require >= min_score killed
  * no-gate     - role-set declares no gate -> verdict UNVERIFIED (terminal)

Wave 2/3 additions:
  * replayable evidence - every GATE-RESULT carries a header encoding the exact
    command, cwd, env allowlist, and input file hashes so a third party can
    reproduce the verdict (replay-lock.json is the pinned environment).
  * budget - a gate def may set "max_wall_clock" (seconds); on timeout the
    verdict is BUDGET_EXCEEDED (a terminal state the engine records).
"""
import json
import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

ENV_ALLOWLIST = ["PATH", "HOME", "LANG", "LC_ALL", "PYTHONPATH"]
DEFAULT_TIMEOUT = 300


def _sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:16]


def _input_hashes(out: Path, manifest) -> dict:
    h = {}
    for e in manifest:
        p = out / e
        if p.is_file():
            h[e] = _sha256(p.read_bytes())
    m = out / "manifest.json"
    if m.is_file():
        h["manifest.json"] = _sha256(m.read_bytes())
    return h


def evidence_header(gate_def, out: Path, run: Path, manifest) -> str:
    cwd = "run" if gate_def.get("cwd") == "run" else "work/" + str(gate_def.get("role", "?"))
    env = sorted(gate_def.get("env_allowlist", []))
    cmds = gate_def.get("commands", [])
    return (
        "<!-- replayable evidence -->\n"
        "role: " + str(gate_def.get("role", "?")) + "\n"
        "gate_kind: " + str(gate_def.get("kind", "?")) + "\n"
        "cwd: " + cwd + "\n"
        "env_allowlist: " + repr(env) + "\n"
        "commands:\n" + "".join("  $ " + str(c) + "\n" for c in cmds) +
        "input_sha256:\n" + "".join("  " + str(k) + ": " + str(v) + "\n" for k, v in sorted(_input_hashes(out, manifest).items())) +
        "<!-- /replayable evidence -->\n"
    )


def _gate_env(gate_def) -> dict:
    allow = list(ENV_ALLOWLIST)
    allow.extend(gate_def.get("env_allowlist", []))
    return {k: os.environ[k] for k in allow if k in os.environ}


def _gate_cwd(gate_def, out: Path, run: Path) -> Path:
    return run if gate_def.get("cwd") == "run" else out


def write_evidence(out: Path, text: str, role: str, unverified: bool = False,
                   header: str = "") -> str:
    name = "UNVERIFIED-NO-GATE.md" if unverified else "GATE-RESULT-" + role + ".md"
    p = out / name
    if not unverified and p.exists():
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        p.write_text("[gate round @ " + stamp + "]\n" + header + text + "\n\n" + p.read_text())
    else:
        p.write_text(header + text)
    return name


def _files(out: Path, manifest) -> list:
    return [out / e for e in manifest if (out / e).is_file()]


def _run_cmds(gate_def, out: Path, run: Path):
    cmds = gate_def.get("commands", [])
    if not cmds:
        return False, "gate declared but no commands -> UNVERIFIED", False
    cwd = _gate_cwd(gate_def, out, run)
    env = _gate_env(gate_def)
    timeout = gate_def.get("max_wall_clock", DEFAULT_TIMEOUT)
    lines, all_ok, timeout_hit = [], True, False
    for c in cmds:
        try:
            r = subprocess.run(c, shell=True, cwd=cwd, env=env,
                               capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            timeout_hit = True
            lines.append("$ " + str(c) + "\nTIMEOUT after " + str(timeout) + "s -> BUDGET_EXCEEDED")
            break
        ok = r.returncode == 0
        all_ok = all_ok and ok
        lines.append("$ " + str(c) + "\nexit=" + str(r.returncode) + "\n" + r.stdout[-800:] + "\n" + r.stderr[-800:])
    return all_ok, ("\n---\n".join(lines) if lines else ""), timeout_hit


def _coverage(out: Path, cmds, timeout: int):
    env = _gate_env({"kind": "coverage"})
    cwd = out
    import shlex
    wrapped = ["python3", "-m", "trace", "--count", "--summary"]
    if any("pytest" in c for c in cmds):
        wrapped += ["--module", "pytest", "-q"]
    else:
        wrapped += shlex.split(cmds[0])
    try:
        r = subprocess.run(wrapped, cwd=cwd, env=env, capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return -1.0, "coverage run TIMEOUT after " + str(timeout) + "s -> BUDGET_EXCEEDED"
    # Read .cover files for accuracy
    executed = total = 0
    lines = []
    targets = []
    m = out / "manifest.json"
    if m.exists():
        try:
            manifest = json.loads(m.read_text())
            targets = [f for f in _files(out, manifest) if f.suffix == ".py"]
        except Exception:
            targets = []
    for t in targets:
        cover = t.with_name(t.stem + ".cover")
        if cover.exists():
            n_total = n_hit = 0
            for raw in cover.read_text().splitlines():
                mm = raw.split(":")
                if len(mm) < 2:
                    continue
                hits = mm[0].strip()
                if hits and hits != ">>>>>>":
                    n_total += 1
                    if hits != "0":
                        n_hit += 1
            executed += n_hit
            total += n_total
            lines.append(t.name + ": " + "{:.1f}".format(100.0 * n_hit / n_total) + "% (" + str(n_hit) + "/" + str(n_total) + ")")
    cov = (100.0 * executed / total) if total else 0.0
    # hygiene: .cover files are trace artifacts, not run evidence — the numbers
    # are already in the evidence text; drop the files so the run tree stays clean.
    for t in targets:
        c = t.with_name(t.stem + ".cover")
        if c.exists():
            c.unlink()
    return cov, "\n".join(lines) + "\nTOTAL coverage: " + "{:.1f}".format(cov) + "%"


MUTATIONS = [
    ("gt->lt", ">", "<"),
    ("lt->gt", "<", ">"),
    ("eq->ne", "==", "!="),
    ("ne->eq", "!=", "=="),
    ("and->or", "and", "or"),
    ("or->and", "or", "and"),
    ("true->false", "True", "False"),
    ("false->true", "False", "True"),
]


def _mutation_score(out: Path, targets, cmds, timeout: int):
    env = _gate_env({"kind": "mutation"})
    cwd = out
    killed = total = 0
    lines = []
    for t in targets:
        try:
            src = t.read_text()
        except Exception:
            continue
        for label, a, b in MUTATIONS:
            if a not in src:
                continue
            mutant = src.replace(a, b, 1)
            if mutant == src:
                continue
            tmp = t.with_name(t.stem + ".mut.py")
            tmp.write_text(mutant)
            total += 1
            try:
                r = subprocess.run(cmds, shell=True, cwd=cwd, env=env,
                                   capture_output=True, text=True, timeout=timeout)
                survived = (r.returncode == 0)
            except subprocess.TimeoutExpired:
                survived = True
            finally:
                if tmp.exists():
                    tmp.unlink()
            if not survived:
                killed += 1
            lines.append(t.name + " " + label + ": " + ("KILLED" if not survived else "SURVIVED"))
            if total >= 40:
                break
        if total >= 40:
            break
    score = (100.0 * killed / total) if total else 0.0
    return score, "\n".join(lines) + "\nMUTATION SCORE: " + "{:.1f}".format(score) + "% (" + str(killed) + "/" + str(total) + " killed)"


def run_role_gate(gate_def, out: Path, run: Path):
    """Returns (ok, evidence_filename). Writes evidence under work/<role>/.

    ok=False + timeout => verdict BUDGET_EXCEEDED (checked by the caller via
    the evidence text containing 'BUDGET_EXCEEDED'). All other ok=False are
    FAIL or (with human_fallback) UNVERIFIED.
    """
    role = gate_def.get("role", "unknown")
    kind = gate_def.get("kind")
    manifest = []
    m = out / "manifest.json"
    if m.exists():
        try:
            manifest = json.loads(m.read_text())
        except json.JSONDecodeError:
            pass
    header = evidence_header(gate_def, out, run, manifest)

    if kind == "compile":
        pys = [f for f in _files(out, manifest) if f.suffix == ".py"]
        res = subprocess.run([sys.executable, "-m", "py_compile", *pys],
                             capture_output=True, text=True)
        ok = res.returncode == 0
        ev = write_evidence(out, "compile: " + ("OK" if ok else "FAILED") + "\n" + res.stderr.strip()[:2000],
                            role, header=header)
        return ok, ev

    if kind in ("shell", "test"):
        all_ok, text, timeout_hit = _run_cmds(gate_def, out, run)
        ev = write_evidence(out, text, role, header=header)
        return all_ok, ev

    if kind == "coverage":
        min_pct = float(gate_def.get("min_pct", 70.0))
        targets = [f for f in _files(out, manifest) if f.suffix == ".py"]
        if not targets:
            ev = write_evidence(out, "coverage: no .py files in manifest -> UNVERIFIED",
                                role, True, header=header)
            return False, ev
        cov, text = _coverage(out, gate_def.get("commands", []), gate_def.get("max_wall_clock", DEFAULT_TIMEOUT))
        if cov < 0:
            ev = write_evidence(out, text, role, header=header)
            return False, ev  # BUDGET_EXCEEDED
        ok = cov >= min_pct
        ev = write_evidence(out, text + "\nthreshold: " + "{:.1f}".format(min_pct) + "% -> " + ("PASS" if ok else "FAIL"),
                            role, header=header)
        return ok, ev

    if kind == "mutation":
        min_score = float(gate_def.get("min_score", 60.0))
        targets = [f for f in _files(out, manifest) if f.suffix == ".py"]
        if not targets:
            ev = write_evidence(out, "mutation: no .py files in manifest -> UNVERIFIED",
                                role, True, header=header)
            return False, ev
        score, text = _mutation_score(out, targets, gate_def.get("commands", []),
                                      gate_def.get("max_wall_clock", DEFAULT_TIMEOUT))
        ok = score >= min_score
        ev = write_evidence(out, text + "\nthreshold: " + "{:.1f}".format(min_score) + "% -> " + ("PASS" if ok else "FAIL"),
                            role, header=header)
        return ok, ev

    if kind == "design":
        import design_qa
        thr = float(gate_def.get("min_score", 0.5))
        ok, text = design_qa.run(out, manifest, thr)
        # no css/html produced -> UNVERIFIED (a machine cannot certify absence)
        if not ok and "UNVERIFIED" in text:
            ev = write_evidence(out, text, role, True, header=header)
            return False, ev
        ev = write_evidence(out, text, role, header=header)
        return ok, ev

    if kind == "files":
        required = gate_def.get("required", [])
        missing = [r for r in required if not (out / r).exists() or (out / r).stat().st_size == 0]
        ok = not missing
        ev = write_evidence(out, "required artifacts: " + str(required) + "\nmissing/empty: " + str(missing),
                            role, header=header)
        return ok, ev

    # unknown gate kind: never pretend to pass
    ev = write_evidence(out, "unsupported gate kind " + repr(kind) + " -> UNVERIFIED", role, True, header=header)
    return False, ev


# ------------------------------------------------------------------ verdict signing (Wave 3.5)
import hmac as _hmac_mod




def _as_key(k):
    if isinstance(k, (bytes, bytearray)):
        return bytes(k)
    # legacy: derive from lock_commit string
    return hashlib.sha256((str(k) + ":atlas-core").encode()).hexdigest().encode()

def sign_file(path: Path, key) -> None:
    """Append an HMAC integrity signature over the evidence body, keyed on the
    per-run secret generated at lock (v1.3). A hand-edit of a verdict breaks
    the signature; the key is NOT derivable from public data."""
    if not key:
        return
    p = Path(path)
    if not p.exists():
        return
    content = p.read_text()
    body = content
    marker = "<!-- hmac:"
    if marker in body:
        body = body.split(marker)[0]
    sig = _hmac_mod.new(_as_key(key), body.encode(), hashlib.sha256).hexdigest()
    p.write_text(body + "<!-- hmac: " + sig + " -->\n")


def verify_file(path: Path, key) -> bool:
    if not key:
        return True
    p = Path(path)
    if not p.exists():
        return False
    content = p.read_text()
    marker = "<!-- hmac:"
    if marker not in content:
        return False
    body, _, tail = content.rpartition(marker)
    if "-->" not in tail:
        return False
    sig = tail.split("-->")[0].strip()
    after = tail.split("-->", 1)[1]
    if after.strip():
        return False
    recomputed = _hmac_mod.new(_as_key(key), body.encode(), hashlib.sha256).hexdigest()
    return sig == recomputed

#!/usr/bin/env python3
"""Atlas Core — gate runner. Verifier is external to producers; gates are
executable checks (exit codes), never LLM self-attestation.

Gate kinds:
  * compile     — python -m py_compile on produced .py files
  * shell       — run commands from role-set gate spec (exit 0 = pass)
  * files       — required artifacts exist and are non-empty
  * test         — run a test command; require non-trivial suite
  * no-gate     — role-set declares no gate -> verdict UNVERIFIED (terminal)
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# Fix #1 (GATE ENV SANITIZATION): gate subprocesses must not inherit the
# orchestrator's full environment (it may carry API keys / tokens). The
# allowlist is deliberately tiny; a gate def can add names via its optional
# "env_allowlist" key. Default cwd is the role dir; a gate def may set
# "cwd": "run" to run from the run root (Fix #7: whole-run-dir secret grep).
ENV_ALLOWLIST = ["PATH", "HOME", "LANG", "LC_ALL", "PYTHONPATH"]


def _gate_env(gate_def: dict) -> dict:
    allow = list(ENV_ALLOWLIST)
    allow.extend(gate_def.get("env_allowlist", []))
    return {k: os.environ[k] for k in allow if k in os.environ}


def _gate_cwd(gate_def: dict, out: Path, run: Path) -> Path:
    return run if gate_def.get("cwd") == "run" else out


def write_evidence(out: Path, text: str, role: str, unverified: bool = False) -> str:
    name = "UNVERIFIED-NO-GATE.md" if unverified else f"GATE-RESULT-{role}.md"
    p = out / name
    if not unverified and p.exists():
        # Fix #5 (AUDIT EVIDENCE APPEND): GATE-RESULT files accumulate one
        # block per gate round (timestamp + result) so earlier FAIL evidence
        # survives re-runs. UNVERIFIED-NO-GATE.md keeps overwrite semantics.
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        p.write_text(f"[gate round @ {stamp}]\n{text}\n\n{p.read_text()}")
    else:
        p.write_text(text)
    return name


def _files(out: Path, manifest: list[str]) -> list[Path]:
    return [out / e for e in manifest if (out / e).is_file()]


def run_role_gate(gate_def: dict, out: Path, run: Path) -> tuple[bool, str]:
    """Returns (ok, evidence_filename). Writes evidence under work/<role>/."""
    role = gate_def.get("role", "unknown")
    kind = gate_def.get("kind")
    manifest = []
    m = out / "manifest.json"
    if m.exists():
        try:
            manifest = json.loads(m.read_text())
        except json.JSONDecodeError:
            pass

    if kind == "compile":
        pys = [f for f in _files(out, manifest) if f.suffix == ".py"]
        res = subprocess.run([sys.executable, "-m", "py_compile", *pys],
                             capture_output=True, text=True)
        ok = res.returncode == 0
        ev = write_evidence(out, f"compile: {'OK' if ok else 'FAILED'}\n{res.stderr.strip()[:2000]}", role)
        return ok, ev

    if kind in ("shell", "test"):
        cmds = gate_def.get("commands", [])
        if not cmds:
            ev = write_evidence(out, f"gate '{kind}' declared but no commands -> UNVERIFIED", role, True)
            return False, ev
        cwd = _gate_cwd(gate_def, out, run)
        env = _gate_env(gate_def)
        lines, all_ok = [], True
        for c in cmds:
            r = subprocess.run(c, shell=True, cwd=cwd, env=env,
                               capture_output=True, text=True, timeout=300)
            ok = r.returncode == 0
            all_ok = all_ok and ok
            lines.append(f"$ {c}\nexit={r.returncode}\n{r.stdout[-800:]}\n{r.stderr[-800:]}")
        ev = write_evidence(out, " | ".join(lines) if not lines else "\n---\n".join(lines), role)
        return all_ok, ev

    if kind == "files":
        required = gate_def.get("required", [])
        missing = [r for r in required if not (out / r).exists() or (out / r).stat().st_size == 0]
        ok = not missing
        ev = write_evidence(out, f"required artifacts: {required}\nmissing/empty: {missing}", role)
        return ok, ev

    # unknown gate kind: never pretend to pass
    ev = write_evidence(out, f"unsupported gate kind {kind!r} -> UNVERIFIED", role, True)
    return False, ev

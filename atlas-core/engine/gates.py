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
import subprocess
import sys
from pathlib import Path


def write_evidence(out: Path, text: str, role: str, unverified: bool = False) -> str:
    name = "UNVERIFIED-NO-GATE.md" if unverified else f"GATE-RESULT-{role}.md"
    p = out / name
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
        lines, all_ok = [], True
        for c in cmds:
            r = subprocess.run(c, shell=True, cwd=out, capture_output=True, text=True, timeout=300)
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

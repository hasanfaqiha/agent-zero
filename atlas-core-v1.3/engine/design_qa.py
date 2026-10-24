"""v1.3 design-QA checker — stdlib-only, deterministic, no browser needed.

Gate kind: "design". Runs a battery of cheap, reproducible checks over a
role's build output (work/<role>/) and writes a scored DESIGN-REPORT.md
that the gate verdict keys on. The checks catch the "basic UI/UX" failure
mode that motivated this role-set: unstyled HTML, missing responsive
breakpoints, generic system fonts, no contrast signal.

Each check is deterministic (pure file analysis) so replay is byte-stable.
"""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path

# (check, weight) — weights sum to 1.0
CHECKS = [
    ("has_stylesheets", 0.25, "ships CSS beyond inline <style>"),
    ("uses_responsive_breakpoints", 0.25, "declares viewport meta or media queries"),
    ("custom_typography", 0.15, "declares non-system font stack / font-family"),
    ("design_tokens", 0.15, "uses CSS custom properties (design tokens)"),
    ("layout_system", 0.10, "uses grid/flexbox for layout, not float hacks"),
    ("wcag_signal", 0.10, "semantic landmarks / a11y attributes present"),
]


def _read(out: Path, manifest) -> tuple[str, list[str]]:
    """Concatenated text of the role's css/html files (+ manifest names)."""
    files = [out / e for e in manifest if (out / e).is_file()]
    targets = [f for f in files if f.suffix.lower() in (".css", ".html", ".htm", ".scss")]
    if not targets:
        # fall back: scan the role dir directly (manifest may be minimal)
        targets = [f for f in out.rglob("*") if f.suffix.lower() in (".css", ".html", ".htm", ".scss")]
    text = "\n".join(f.read_text(encoding="utf-8", errors="replace") for f in targets)
    return text, [f.name for f in targets]


def run(out: Path, manifest, threshold: float = 0.5) -> tuple[bool, str]:
    """Returns (ok, report_text). ok = weighted score >= threshold."""
    text, names = _read(out, manifest)
    results = {}
    for check, _w, _desc in CHECKS:
        if check == "has_stylesheets":
            css = [f for f in names if f.endswith((".css", ".scss"))]
            results[check] = bool(css)
        elif check == "uses_responsive_breakpoints":
            results[check] = bool(re.search(r"@media|viewport|breakpoint", text, re.I))
        elif check == "custom_typography":
            non_system = bool(re.search(
                r"font-family:[^;}]*"
                r"(Inter|Poppins|Manrope|Space|DM ?[A-Z]|Fira|Source|Archivo|Fraunces|Playfair|Caveat|Geist|Figtree|Instrument|Spectral|Lexend|Outfit|Ubuntu|Rubik|Work ?Sans)",
                text, re.I))
            # Defect 14 (E2E akar-e2e, 2026-10-24): tokenized fonts — the
            # real font stack lives in a custom property
            # (--font-display: "Fraunces", ...) and is applied via
            # font-family: var(--font-display). The direct-declaration
            # regex above misses this common, correct pattern. Accept a
            # custom-property definition carrying a non-system font name.
            tokenized = bool(re.search(
                r"--[a-z0-9-]*(?:font|typeface)[a-z0-9-]*\s*:[^;}]*"
                r"(Inter|Poppins|Manrope|Space|DM ?[A-Z]|Fira|Source|Archivo|Fraunces|Playfair|Caveat|Geist|Figtree|Instrument|Spectral|Lexend|Outfit|Ubuntu|Rubik|Work ?Sans)",
                text, re.I))
            results[check] = non_system or tokenized or bool(re.search(r"@font-face", text, re.I))
        elif check == "design_tokens":
            results[check] = bool(re.search(r"--[a-z][a-z0-9-]*\s*:", text))
        elif check == "layout_system":
            results[check] = bool(re.search(r"display\s*:\s*(grid|flex)|grid-template|flex-direction", text))
        else:  # wcag_signal
            results[check] = bool(re.search(
                r"<(header|nav|main|footer|aside|section)\b|role=\"|aria-|alt=\"", text, re.I))
    total_w = sum(w for _c, w, _d in CHECKS)
    score = sum(w for c, w, _d in CHECKS if results[c]) / total_w
    lines = ["DESIGN-QA REPORT (v1.3)", "============================", ""]
    lines.append(f"files analyzed: {', '.join(names) if names else '(none found)'}")
    lines.append("")
    for check, w, desc in CHECKS:
        mark = "PASS" if results[check] else "MISS"
        lines.append(f"  [{mark}] {check:<32} (weight {w:.2f}) — {desc}")
    lines.append("")
    lines.append(f"SCORE: {score:.2f} / threshold {threshold:.2f} -> {'PASS' if score >= threshold else 'FAIL'}")
    if not names:
        lines.append("NOTE: no css/html files found in this role's output — "
                     "design QA cannot verify what was not produced -> UNVERIFIED")
    return score >= threshold and bool(names), "\n".join(lines)


if __name__ == "__main__":
    # standalone: python3 design_qa.py <work/<role>/dir> [threshold]
    out = Path(sys.argv[1])
    thr = float(sys.argv[2]) if len(sys.argv) > 2 else 0.5
    ok, report = run(out, [])
    print(report)
    sys.exit(0 if ok else 2)

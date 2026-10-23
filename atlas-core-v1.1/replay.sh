#!/usr/bin/env bash
# atlas-core v1.1 — replay a recorded run.
#
# Usage: ./replay.sh <run-repo> [commit]
#   run-repo : a git clone of the run repo. CI pattern:
#                git clone <run-repo-url> tmp && ./replay.sh tmp <commit>
#   commit   : (optional) commit to pin; defaults to HEAD. Every snapshot
#               commit produced by `atlas.py commit` (phase 'merged') is
#               replayable; the engine records run layout in .atlas/state.json.
#
# What it does:
#   (a) checks .atlas/state.json exists in the run dir;
#   (b) re-runs `atlas.py gate <role>` for every role in the role-set
#       (exit 0 = PASS / UNVERIFIED; exit 2 = FAIL — FAIL roles exit 2 as usual);
#   (c) if all roles PASS, regenerates logs/gate-report.md via `atlas.py merge`;
#   (d) prints a per-role PASS/FAIL/UNVERIFIED table.
#
# Exit code: 0 if no FAIL, 2 if any role's gate exited 2.

set -u
REPO_DIR="${1:?usage: ./replay.sh <run-repo> [commit]}"
ENGINE="$(cd "$(dirname "$0")" && pwd)/engine"
ATLAS="$ENGINE/atlas.py"
STATE_BAK=""

if ! cd "$REPO_DIR"; then
  echo "[replay] ERROR: no such directory: $REPO_DIR" >&2
  exit 1
fi

# (a) state check
if [ ! -f .atlas/state.json ]; then
  echo "[replay] ERROR: no .atlas/state.json in $REPO_DIR — not a recorded run" >&2
  exit 1
fi
STATE_BAK="$(mktemp)"
cp .atlas/state.json "$STATE_BAK"

# Pin commit (optional). The run must already be a git repo; replay works on
# the recorded tree. We restore state.json afterwards so replay is read-only
# for the repo's tracked files.
if [ -n "${2:-}" ] && [ "$2" != "HEAD" ]; then
  git checkout -q "$2" || { echo "[replay] ERROR: commit $2 not found" >&2; exit 1; }
fi

ROLE_SET="$(python3 -c "import json; print(json.load(open('.atlas/state.json'))['role_set'])")"
# Role list: union of role-set 'roles' and gate 'role' entries.
# The role-set subcommand is positional (command run_dir arg) so we pass '.'
# as run_dir and the name as the third arg.
ROLES="$(python3 "$ATLAS" role-set . "$ROLE_SET" | python3 -c '
import json, sys
d = json.load(sys.stdin)
roles = {r["id"] for r in d.get("roles", [])}
roles |= {g["role"] for g in d.get("gates", []) if g.get("role")}
print("\n".join(sorted(roles)))
')"

# (b) re-run every role gate. FAIL gates exit 2; PASS/UNVERIFIED exit 0
# (UNVERIFIED is a terminal non-failure in the engine, so exit 0 is correct).
# CLI arg order for every subcommand: atlas.py <command> <run_dir> <arg>.
ROWS="$(mktemp)"
FAIL_ROLES=0
while IFS= read -r role; do
  [ -n "$role" ] || continue
  set +e
  python3 "$ATLAS" gate . "$role" >/dev/null 2>&1
  code=$?
  set -u
  if [ "$code" -eq 2 ]; then
    v="FAIL"
    FAIL_ROLES=1
  else
    # PASS and UNVERIFIED both exit 0 — read the engine's recorded verdict.
    v="$(python3 - "$role" <<'PYEOF'
import json, sys
try:
    s = json.load(open(".atlas/state.json"))
    print(s.get("roles", {}).get(sys.argv[1], {}).get("verdict") or "UNVERIFIED")
except Exception:
    print("UNVERIFIED")
PYEOF
)"
  fi
  printf '%-24s %-12s\n' "$role" "$v" >> "$ROWS"
done <<< "$ROLES"

# (c) regenerate the report only on a clean pass
NOTE=""
if [ "$FAIL_ROLES" -eq 0 ]; then
  if python3 "$ATLAS" merge . >/dev/null 2>&1; then
    NOTE="(c) all roles passed — regenerated logs/gate-report.md via merge"
  else
    NOTE="(c) all roles passed, but merge failed to regenerate the report (phase may not be 'gate')"
  fi
else
  NOTE="(c) FAIL present — merge not regenerated; re-run pipeline gates on the failing role"
fi

# (d) table
echo "[replay] role-set: $ROLE_SET"
printf '%-24s %-12s\n' 'ROLE' 'VERDICT'
cat "$ROWS"
rm -f "$ROWS"
echo "$NOTE"

# Restore the recorded state so the checkout stays untouched by the replay.
if [ -n "$STATE_BAK" ]; then
  cp "$STATE_BAK" .atlas/state.json
  rm -f "$STATE_BAK"
fi
[ "$FAIL_ROLES" -eq 0 ] || exit 2
exit 0

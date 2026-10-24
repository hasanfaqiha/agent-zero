# Role-Set Schema (v1.3)

A role-set is JSON the engine loads at `synth`. This page documents every
field the engine reads, with a runnable example for each feature. The minimal
working set is `name`, `description`, `roles[]`, `gates[]` — everything else
is optional.

All examples run inside a scratch dir:

```sh
cd /tmp && mkdir schema-demo && cd schema-demo
E=/path/to/atlas-core/engine/atlas.py
$E init run 'demo' 'software'
```

## Top-level fields

| field | required | type | meaning |
|---|---|---|---|
| `name` | yes | string | Unique id. Used by `registry install/hash` and `synth <run> <rs.json>`. |
| `description` | yes | string | What this role-set is for. Surfaced in BRIEFs. |
| `roles` | yes | array | One entry per role. See below. |
| `gates` | yes | array | One entry per role; the gate that role must pass. |
| `status` | no | string | Ignored on load; the engine forces `proposed` at registration. |
| `cost` | no | object | Run-level budget caps. See "Budget". |
| `skills` | no | array | Skill bindings discovered per role. See "Skill discovery". |

## Role fields

| field | required | meaning |
|---|---|---|
| `id` | yes | Role identifier; becomes `work/<id>/`. |
| `mandate` | yes | One-paragraph brief of what this role produces. |
| `output` | yes | Working dir; must be `work/<id>/`. |
| `gate` | yes | The gate *kind* (one of `files`/`shell`/`compile`/`coverage`/`mutation`/`no-gate`) — a shorthand that must match the entry in `gates[]`. |

## Gate fields

| field | required | default | meaning |
|---|---|---|---|
| `role` | yes | — | Which role this gate belongs to. |
| `kind` | yes | — | `files`, `shell`, `compile`, `coverage`, `mutation`, `no-gate`. |
| `commands` | no | `[]` | Shell commands run (in the gate cwd); any non-zero exit = FAIL. |
| `cwd` | no | `work/<role>/` | Working dir for the commands. |
| `env_allowlist` | no | `[]` | Only these env vars are passed through to gate commands (security boundary). |
| `required` | no | `[]` | (`files` kind) artifacts that must exist under the role output. |
| `min_pct` | no | `70.0` | (`coverage` kind) minimum line-coverage percentage. |
| `min_score` | no | `60.0` | (`mutation` kind) minimum fraction of mutants the tests must kill. |
| `credits` | no | `0` | Per-gate credit cost, added to `cost_spent`. |
| `max_wall_clock` | no | `300` | Per-gate wall-clock cap (seconds); timeout = BUDGET_EXCEEDED. |
| `human_fallback` | no | `false` | On FAIL, allow demoting the verdict to UNVERIFIED (human-review) instead of hard FAIL. |

### Example — every gate kind in one role-set

```json
{
  "name": "all-kinds-demo",
  "description": "Demo role-set exercising every gate kind.",
  "roles": [
    {"id": "builder",   "mandate": "Write the artifact.", "output": "work/builder/",   "gate": "files"},
    {"id": "shelly",    "mandate": "Run a script gate.",   "output": "work/shelly/",    "gate": "shell"},
    {"id": "compiler",  "mandate": "Produce compiling code.","output": "work/compiler/","gate": "compile"},
    {"id": "covered",   "mandate": "Cover the code with tests.","output": "work/covered/","gate": "coverage"},
    {"id": "mutant",    "mandate": "Make the tests actually kill mutants.","output": "work/mutant/","gate": "mutation"},
    {"id": "human",     "mandate": "Deliver what a machine can't verify.","output": "work/human/","gate": "no-gate"}
  ],
  "gates": [
    {"role": "builder",  "kind": "files",    "required": ["artifact.txt", "manifest.json"]},
    {"role": "shelly",   "kind": "shell",    "commands": ["bash check.sh"], "cwd": "work/shelly/", "max_wall_clock": 120},
    {"role": "compiler", "kind": "compile",  "commands": ["python3 -m py_compile ./*.py"]},
    {"role": "covered",  "kind": "coverage", "commands": ["python3 -m pytest"], "min_pct": 80},
    {"role": "mutant",   "kind": "mutation", "commands": ["python3 -m pytest"], "min_score": 70},
    {"role": "human",    "kind": "no-gate",  "human_fallback": true}
  ],
  "cost": {"wall_clock_s": 3600, "credits": 50}
}
```

`no-gate` roles always produce an UNVERIFIED verdict (by design) — the
engine refuses to stamp PASS on something a machine can't check.
`human_fallback: true` on a `files`/`shell` gate turns a FAIL into
UNVERIFIED so a human can review it later without re-running the whole gate.

## Budget

```json
"cost": {"wall_clock_s": 3600, "credits": 50}
```

`wall_clock_s` is the **run-level** cap (sum of all gate wall-clock,
accumulated in `state.wall_clock_spent`). Exceeding it → BUDGET_EXCEEDED
(a terminal state like UNVERIFIED — no human stamp can launder it).
`credits` accumulates across every gate's `credits` field; exceeding
`cost.credits` → BUDGET_EXCEEDED the same way.

## Skill discovery (v1.3)

Optional `skills` block binds a skill to a role. At `build`, the engine
emits a `SKILL-HINT.md` in that role's output dir telling the builder which
skill to load. The discovery is the **agent's** job (it has the skill
index); the engine just records the binding so the choice is pinned at
lock-time and can't drift.

```json
"skills": [
  {"role": "builder", "skill": "ponytail"},
  {"role": "covered", "skill": "coverage-tips"}
]
```

If a listed skill is not installed, the engine warns (does not block) —
the binding is advisory, the gate is authoritative.

## Registry canonicalization (v1.3)

`registry hash <name>` and `registry install <name>` now share one
canonical serializer (`json.dumps(..., indent=2, sort_keys=True)`), so
the sha256 you pin from `hash` equals the sha256 of the file `install`
writes. A divergence is a bug, not a feature.

## Minimal runnable

```json
{
  "name": "mini",
  "description": "Tiny two-role run.",
  "roles": [
    {"id": "dev", "mandate": "Build one thing.", "output": "work/dev/", "gate": "files"},
    {"id": "qa",  "mandate": "Check it.",       "output": "work/qa/",  "gate": "files"}
  ],
  "gates": [
    {"role": "dev", "kind": "files", "required": ["manifest.json"]},
    {"role": "qa",  "kind": "files", "required": ["manifest.json"]}
  ]
}
```

```sh
$E synth run mini.json        # use the JSON file as the role-set
$E lock run
$E build run dev && $E mark-built run dev && $E gate run dev
$E build run qa  && $E mark-built run qa  && $E gate run qa
$E amend run 'spec fix'       # now reachable post-lock
$E regate run
$E merge run
```

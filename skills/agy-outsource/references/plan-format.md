# plan.json format

All paths are relative and use `/`. Test paths are relative to the plan's folder; `context_files` are relative to
`project_root`; deliverables are paths in the final project layout (that is also how they are laid out for agy, so
imports work the same way in the work dir, the tests and the project).

## Plan level

| key | required | meaning |
|---|---|---|
| `project` | yes | short project name, shown to agy |
| `context` | recommended | one paragraph: what the project is, who uses it, what matters |
| `language` | no | `python` (default; full gates) or anything else (manifest/scope/files/name-presence/forbid/tests only; needs `test_cmd`) |
| `python` / `runtime` | no | runtime line in the work order (`"3.9+"` → "Python 3.9+"; `runtime` overrides the whole line) |
| `conventions` | no | list of rules every unit must follow (shown under Constraints) |
| `imports` | no | `{"stdlib": true, "allow": ["numpy"]}` — Python imports must be stdlib, project modules or `allow`; omit for no check |
| `forbid_patterns` | no | regexes no deliverable line may match (e.g. `"^\\s*print\\("`, `"TODO"`) |
| `test_cmd` | no | `"auto"` (default: pytest if installed, else `scripts/mini_test.py`) or a shell command with `{python}` and `{tests}` placeholders, e.g. `"npm test -- {tests}"` |
| `test_timeout` | no | seconds per test run (default 120) |
| `max_attempts` | no | agy attempts per unit (default 3) |
| `hidden_feedback` | no | what agy sees when hidden tests fail: `tail` (output tail, default), `summary` (failing test names + assertion lines), `none` |
| `integration_test_cmd` | no | shell command run in the assembled tree after all units are accepted (`{python}` placeholder) |
| `project_root` | no | base for `context_files` (default: the plan folder) |
| `units` | yes | list of units |

## Unit

| key | required | meaning |
|---|---|---|
| `id` | yes | `[A-Za-z0-9][A-Za-z0-9_.-]*`, e.g. `U2-slugify` |
| `title`, `goal` | yes | one line each |
| `deliverables` | yes | files this unit writes (1–3 recommended); owned by this unit only |
| `interface` | recommended | list of `{file, symbol, signature, contract}` (below) |
| `spec` | recommended | behaviour rules; every rule a hidden test checks must be written here |
| `examples` | no | `{"call": "f('a')", "returns": "'A'"}`, `{"call": "f('')", "raises": "ValueError"}` or plain strings |
| `constraints` | no | unit-specific rules (added to the plan's `conventions`) |
| `depends_on` | no | unit ids; their accepted deliverables (transitively) are placed read-only in the work dir |
| `context_files` | no | existing project files agy needs to read (copied read-only) |
| `tests` | one of | visible test files (copied read-only into the work dir) |
| `hidden_tests` | one of | sealed test files (only ever copied into the verify dir, deleted after the run) |
| `test_cmd`, `test_timeout`, `max_attempts`, `effort`, `imports`, `forbid_patterns`, `hidden_feedback` | no | per-unit overrides (`effort`: low / medium / high, forwarded to agy) |

### Interface items

```json
{"file": "pkg/slug.py", "symbol": "slugify",
 "signature": "def slugify(text: str, *, sep: str = \"-\", max_len: Optional[int] = None) -> str",
 "contract": "Return a lowercase ASCII slug of text."}
{"file": "pkg/registry.py", "symbol": "SlugRegistry", "signature": "class SlugRegistry(Base)"}
{"file": "pkg/registry.py", "symbol": "SlugRegistry.add", "signature": "def add(self, text: str) -> str"}
{"file": "pkg/consts.py", "symbol": "MAX_LEN", "signature": "MAX_LEN"}
```

- Functions / methods: parameter names, order, kind (positional-only, positional, `*args`, keyword-only, `**kw`),
  whether each has a default, and `async` must match. Annotations and default values are not compared (a missing
  return annotation is a warning).
- Classes: must exist and derive from the listed bases. Methods are `Class.method` items.
- Constants: a bare name means a module-level assignment.
- Non-Python files: only checks that the name occurs in the file.

## Delivery format agy must follow (enforced by the gates)

- Write exactly the deliverables; no other new file; read-only files untouched (they are restored before each attempt).
- `DELIVERY.json` at the work dir root:
  `{"unit": "<id>", "files": [<exactly the deliverables>], "symbols": [...], "assumptions": [...], "self_check": "..."}`.
  The assumptions end up in `report.md` for the lead to review.

## Writing good units (checklist)

- [ ] A unit's tests can run with only the unit and its dependencies present.
- [ ] Every hidden-test expectation follows from a written rule, example or contract.
- [ ] Visible tests cover the typical path of every public symbol; hidden tests cover edges, errors and rules the
      visible tests skip.
- [ ] `check --red` passes: all tests fail on stubs, no undeclared imports.
- [ ] Error behaviour is specified (which exception, when), as is the empty-input case.
- [ ] Nothing in the work order depends on files agy will not have.

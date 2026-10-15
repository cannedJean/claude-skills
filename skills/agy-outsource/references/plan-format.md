# plan.json format

Paths in `deliverables`, `tests`, `hidden_tests` and `context_files` are relative and use `/` (`check` refuses a
leading `/`, `\` or `~`, a drive letter, `..` and components made only of dots or spaces). Test paths are relative to
the plan's folder; `context_files` are relative to `project_root`; deliverables are paths in the final project layout
(that is also how they are laid out for agy, so imports work the same way in the work dir, the tests and the
project). `project_root` itself is not restricted: it may be any path relative to the plan's folder, `..` included
(see below).

## Plan level

| key | required | meaning |
|---|---|---|
| `project` | yes | short project name, shown to agy |
| `context` | recommended | one paragraph: what the project is, who uses it, what matters |
| `roles` | yes | `{"lead": "<the lead's model>", "auditor": "sonnet"}`. The lead declares its own model; `check` reports a missing lead and an auditor of the lead's family or agy; `audit`, `triage` and `--contractor claude:<model>` refuse a model of the same family (and never use agy as auditor) |
| `language` | no | `python` (default; full gates) or anything else (manifest/scope/files/name-presence/forbid/tests only; needs `test_cmd`) |
| `python` / `runtime` | no | runtime line in the work order (`"3.9+"` → "Python 3.9+"; `runtime` overrides the whole line) |
| `conventions` | no | list of rules every unit must follow (shown under Constraints) |
| `imports` | no | `{"stdlib": true, "allow": ["numpy"]}` — Python imports must be stdlib, project modules or `allow`; omit for no check |
| `forbid_patterns` | no | regexes no deliverable line may match (e.g. `"^\\s*print\\("`, `"TODO"`) |
| `tamper_allow` | no | names the `tamper` gate lets through, e.g. `["sys.argv", "sys.exit", "SystemExit"]` for a CLI unit, `"inspect"`, `"getattr"` (a computed attribute name), or `"text"` (strings naming test files). Every other test-introspection import, early exit, `eval`/`exec`, `getattr`-style detour (also `__dict__`, `vars(x)`, `operator.attrgetter`), frame or traceback attribute (`__traceback__`, `f_back`, ..., also in a string) and test-file string rejects the delivery |
| `test_cmd` | no | `"auto"` (default: pytest if installed, else `scripts/mini_test.py`) or a shell command with `{python}` and `{tests}` placeholders, e.g. `"npm test -- {tests}"`. In a Python plan the command must still end its output with a pytest, unittest or mini_test result line reporting at least as many passed tests as `unittest.TestCase` methods the files define (exit code 0 alone is not a pass). With `auto`, `check` and `run` also refuse test files that define more test functions than the runner collects: a name defined twice in one class or module (the last wins), a top-level function mini_test skips (it runs `test_*` functions; pytest runs `test*`), a class that is not a `unittest.TestCase` (pytest: or `Test*`); with a non-Python plan's `test_cmd` only the exit code is checked |
| `test_timeout` | no | seconds per test run (default 120) |
| `max_attempts` | no | agy attempts per unit (default 3) |
| `effort` | no | `low` / `medium` / `high`: pins every agy attempt (default: attempt 1 `low`, 2 `medium`, 3+ `high`). Precedence: `run --effort` > unit `effort` > plan `effort`; any other value is a plan error. Not audited and not part of the spec hash |
| `hidden_feedback` | no | what agy sees when hidden tests fail: `summary` (failing test names + assertion lines, default), `tail` (output tail), `none`. At every level, and in the visible-test output too, a line that quotes a hidden test's source is dropped |
| `integration_test_cmd` | no | shell command run in the assembled tree after all units are accepted (`{python}` placeholder). Judged by its exit code only (no result line needed); `integrate` trusts a pass only for the command it ran |
| `project_root` | no | base for `context_files`, relative to the plan's folder (default: the plan folder itself). With the recommended layout `<project>/.outsource/<feature>/plan.json`, `"../.."` makes `context_files` the project's own files. A file that does not exist in the project yet (a new package's `__init__.py`): create it in the project first, or use a spec-local root holding the files the units read, e.g. `"project"` with `slugkit/__init__.py` as in the example; `integrate` adds a context file only where the destination lacks it |
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
| `depends_on` | no | unit ids, only where this unit's code calls theirs (independent units run in parallel). Their accepted deliverables and context files (transitively) are placed read-only in the work dir and covered by the integrity gate; direct dependencies' code is inlined in the work order, indirect ones only by interface |
| `context_files` | no | existing project files agy needs to read, relative to `project_root` (copied read-only and inlined for this unit; dependents get the file in their work dir and listing, but not its text). Trivial files such as a package `__init__.py` belong here, not in `deliverables`; `integrate` adds one only where the destination lacks it |
| `tests` | one of | visible test files (copied read-only into the work dir, inlined) |
| `hidden_tests` | one of | sealed test files (only ever copied into the verify dir, deleted after the run). No unit may list a hidden test, or a file of the same name, under `tests` or `context_files` |
| `test_cmd`, `test_timeout`, `max_attempts`, `effort`, `imports`, `forbid_patterns`, `tamper_allow`, `hidden_feedback` | no | per-unit overrides (`effort`: low / medium / high, pins every agy attempt of this unit; `effort`, `max_attempts` and `test_timeout` are not audited) |

### Interface items

```json
{"file": "pkg/slug.py", "symbol": "slugify",
 "signature": "def slugify(text: str, *, sep: str = \"-\", max_len: Optional[int] = None) -> str",
 "contract": "Return a lowercase ASCII slug of text."}
{"file": "pkg/registry.py", "symbol": "SlugRegistry", "signature": "class SlugRegistry(Base)"}
{"file": "pkg/registry.py", "symbol": "SlugRegistry.add", "signature": "def add(self, slug: str) -> str"}
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
- The work order lists the work dir (read-only inputs, the unit's own files from the previous attempt, leftovers
  to ignore, files still to create) and holds the full text of the direct inputs up to 24,000 characters, as far
  as the rest of the order leaves room on agy's command line (32,767 characters on Windows).
- `DELIVERY.json` at the work dir root:
  `{"unit": "<id>", "files": [<exactly the deliverables>], "assumptions": [...]}` (other keys are ignored).
  The assumptions end up in `report.md` for the lead to review. Writing it ends agy's work: it replies `DONE`.

## Writing good units (checklist)

- [ ] A unit's tests can run with only the unit and its dependencies present.
- [ ] Every hidden-test expectation follows from a written rule, example or contract.
- [ ] Visible tests cover the typical path of every public symbol; hidden tests cover edges, errors and rules the
      visible tests skip.
- [ ] `check --red` passes: all tests fail on stubs, no undeclared imports.
- [ ] `audit` passes: an auditor on another model traced every assertion to a written item (C/R/E/K). Each spec
      rule, example, contract and constraint is an item the auditor can cite, so write rules as separate entries.
- [ ] Error behaviour is specified (which exception, when), as is the empty-input case.
- [ ] A test that expects a `TypeError` for positional use of keyword-only parameters has a written rule, e.g.
      "sep and max_len are keyword-only; passing them positionally raises TypeError." The signature alone is not
      one: the auditor marks such a test `untraced`.
- [ ] `depends_on` lists only units whose code this unit calls; a unit that only takes another's output as an
      argument needs no dependency (the example's registry takes slugs, so it runs beside the normaliser).
- [ ] Nothing in the work order depends on files agy will not have.

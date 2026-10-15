# agy-outsource

Lead/contractor coding with the Antigravity CLI (`agy`): the lead (Claude, or you) splits a task into
unit-testable units and writes a work order plus tests for each; `agy` implements one unit per session and hands
back the files in a fixed delivery format; a script accepts or rejects every delivery with deterministic gates and
feeds the failures back. Built on [agy-delegate](../agy-delegate/).

```
plan.json + tests/ + hidden/ ──check --red──> run ──> per unit, in dependency order:
    work order ──> agy ──> deliverables + DELIVERY.json
    gates: manifest · scope · files · syntax · interface · imports · forbid · visible tests · hidden tests
    accept (frozen, dependents build on it) | retry with feedback | needs_attention (back to the lead)
──> integration tests on all accepted units ──> integrate --dest <project>
```

## Who does what

| role | model | sees hidden tests | writes code |
|---|---|---|---|
| lead (plan, every test, review) | the Claude session using the skill (declared in `roles.lead`) | yes, it wrote them | never |
| contractor | agy (Gemini); fallback `--contractor claude:<model>` (`claude -p --restricted`, not the lead's model) | never | yes |
| auditor / referee | `claude -p --restricted` on `roles.auditor` (not the lead's, not the contractor's; never agy) | yes | never |
| acceptance | the gates: deterministic, no model | runs them | no |

`audit` must pass before `run` starts a unit; `triage` judges failures without seeing the code. Both prompts are built
by the script, so the lead cannot frame them, and the model that answered is read back from the CLI's `modelUsage`.
The measured agy behaviour this design rests on is in [docs/agy-outsource-isolation.md](../../docs/agy-outsource-isolation.md).

## Why these gates

- **Tests are written by the lead, before the code**, `check --red` proves they fail against interface stubs, and an
  auditor on another model proves every assertion follows from a written rule.
- **The contractor works in a job dir outside every repository** (`AGY_OUTSOURCE_JOBS`, default
  `%LOCALAPPDATA%\agy-outsource\jobs`): agy treats the enclosing git repository as its workspace, so a work dir
  inside the project would let it read the plan, the run records and the hidden tests without any prompt.
- **Hidden tests** are never placed where agy works and are deleted from the verify copy after each run (also after a
  crash: the next run sweeps them), so a delivery cannot be fitted to them; their failure output is path-scrubbed (and can be reduced with
  `hidden_feedback`). agy's recorded tool calls are scanned for any mention of them (`hidden_peek` flag).
- **Scope and format** are checked from file snapshots, not from what agy says it did: extra files, touched
  read-only files (tests, dependencies) or a wrong `DELIVERY.json` reject the delivery.
- **Tamper**: the code under test runs inside the test process, so imports of test/frame introspection modules,
  early exits, `eval`/`exec` and strings naming test files reject the delivery, and a test gate needs the runner's
  own result line (passed > 0, nothing failed or skipped), not just exit code 0.
- **Integrity**: the gate scripts, the plan, the hidden tests and each unit's read-only inputs are fingerprinted; a
  change during the run stops the unit instead of grading against a moved baseline.
- **Interface** is checked on the AST (parameter names, order, kinds, defaults, async, class bases), so units that
  depend on each other fit together; the integration run checks it once more with all tests together.

## Requirements

- agy-delegate installed next to this skill (or `AGY_TASK=/path/to/agy_task.py`), with agy 1.2+ signed in.
- Python 3.9+ (stdlib only; the import gate needs 3.10+ for the stdlib module list). pytest is used when installed,
  otherwise `scripts/mini_test.py` runs `unittest` cases and plain `test_*` functions.

```bash
python scripts/outsource.py preflight
python scripts/outsource.py check examples/slugkit/plan.json --red
python scripts/outsource.py audit examples/slugkit/plan.json
python scripts/outsource.py render examples/slugkit/plan.json U2-slugify
python scripts/outsource.py run examples/slugkit/plan.json -j 2
python scripts/outsource.py status examples/slugkit/plan.json
python scripts/outsource.py triage examples/slugkit/plan.json U2-slugify     # after a unit failed its tests
python scripts/outsource.py integrate examples/slugkit/plan.json --dest ./out --dry-run
```

Plan format: [references/plan-format.md](references/plan-format.md). Work-order template: [prompts/work_order.md](prompts/work_order.md).

## Options (`run`)

| Flag | Meaning |
|---|---|
| `--out DIR` | run directory (default `<plan dir>/.agy_outsource`) |
| `--units A,B` | only these units (their unaccepted dependencies are added) |
| `-j N` | independent units in parallel (default 2) |
| `--force` | re-run selected units even if accepted |
| `--max-attempts N` | attempts per unit (default plan/unit `max_attempts` or 3) |
| `--model`, `--effort`, `--timeout MIN` | forwarded to agy via agy-delegate |
| `--allow-all` | agy may run shell commands (e.g. the visible tests) in the unit work dir |
| `--no-integration` | skip the final integration run |
| `--contractor claude:<model>` | a Claude model other than the lead's writes the code instead of agy |

Env: `AGY_TASK`, `AGY_TASK_MODEL`, `AGY_OUTSOURCE_JOBS` (job dir root), `AGY_OUTSOURCE_KEEP_JOBS=1` (keep job dirs
for debugging), `OUTSOURCE_CLAUDE` (path to the `claude` CLI or a stand-in).

## Tests

```bash
python -m unittest discover -s scripts/tests -v
```

Gate unit tests, plan validation, and full runs of the example with `scripts/tests/fake_agy_task.py` in place of
agy and `scripts/tests/fake_claude.py` in place of `claude -p` (scenarios: perfect, buggy, extra file, tampered
test, wrong signature, missing manifest, forbidden print, early exit, hidden-test peek, sandbox escape, always
failing, quota; audits that fail, skip a test or run on the wrong model; same-family roles; a Claude contractor;
triage).

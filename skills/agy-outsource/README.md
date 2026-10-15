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

## Why these gates

- **Tests are written by the lead, before the code**, and `check --red` proves they fail against interface stubs.
- **Hidden tests** are never placed where agy works and are deleted from the verify copy after each run, so a
  delivery cannot be fitted to them; their failure output is path-scrubbed (and can be reduced with
  `hidden_feedback`). agy's recorded tool calls are scanned for any mention of them (`hidden_peek` flag).
- **Scope and format** are checked from file snapshots, not from what agy says it did: extra files, touched
  read-only files (tests, dependencies) or a wrong `DELIVERY.json` reject the delivery.
- **Interface** is checked on the AST (parameter names, order, kinds, defaults, async, class bases), so units that
  depend on each other fit together; the integration run checks it once more with all tests together.

## Requirements

- agy-delegate installed next to this skill (or `AGY_TASK=/path/to/agy_task.py`), with agy 1.2+ signed in.
- Python 3.9+ (stdlib only; the import gate needs 3.10+ for the stdlib module list). pytest is used when installed,
  otherwise `scripts/mini_test.py` runs `unittest` cases and plain `test_*` functions.

```bash
python scripts/outsource.py preflight
python scripts/outsource.py check examples/slugkit/plan.json --red
python scripts/outsource.py render examples/slugkit/plan.json U2-slugify
python scripts/outsource.py run examples/slugkit/plan.json -j 2
python scripts/outsource.py status examples/slugkit/plan.json
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

Env: `AGY_TASK`, `AGY_TASK_MODEL`.

## Tests

```bash
python -m unittest discover -s scripts/tests -v
```

Gate unit tests, plan validation, and full runs of the example with `scripts/tests/fake_agy_task.py` in place of
agy (scenarios: perfect, buggy, extra file, tampered test, wrong signature, missing manifest, forbidden print,
always failing, quota).

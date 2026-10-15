---
name: agy-outsource
description: Outsource a coding task to the Antigravity CLI (`agy`) as a lead/contractor pair — Claude splits the work into unit-testable units, writes for each one a work order (deliverable files, exact public interface, behaviour rules, examples, dependencies) plus visible tests and sealed hidden tests, and agy delivers code in a strict delivery format (only the listed files + DELIVERY.json). Every delivery is accepted or rejected by deterministic gates (format, scope, interface signature, imports, forbidden patterns, visible tests, hidden tests), failures go back to agy as feedback, units run in dependency order, and the accepted units are integration-tested before anything touches the project. Use when the user says things like "agy한테 외주 줘", "단위별로 쪼개서 agy에게 코딩 시켜", "모듈 나눠서 agy로 구현하고 테스트로 검수해", "outsource this to agy unit by unit", or wants a multi-function / multi-module feature built by agy with test-based acceptance. For a single small chore with no test contract use agy-delegate directly.
---

# agy-outsource

Claude is the **lead**: it designs, specifies and accepts. agy is the **contractor**: it implements one unit per
session and hands back files in a fixed format. Acceptance is done by a script, not by agy and not by eye:

```
plan.json (units + interfaces + rules)      tests/ (visible)   hidden/ (sealed)
        │  check --red: tests must fail against interface stubs
        ▼
 for unit in dependency order:
   work order ──> agy (agy-delegate, fresh work dir: deps' accepted code + visible tests, read-only)
            <── deliverables + DELIVERY.json
   gates: agy · manifest · scope · files · syntax · interface · imports · forbid · tests:visible · tests:hidden
     all ok   → accepted/ (frozen; dependents build on it)
     else     → feedback → next attempt (max 3) → needs_attention → back to Claude
        ▼
 integration: all accepted code + all tests (+ integration_test_cmd)  →  integrate --dest <project>
```

Builds on **agy-delegate** (`../agy-delegate/scripts/agy_task.py`, or set `AGY_TASK`). Commands are relative to this
skill's directory. Use `python3` on macOS/Linux, `python` on Windows.

## Workflow

1. **Preflight** (first use): `python scripts/outsource.py preflight`. If agy's headless probe fails the user must run
   `agy` once interactively to sign in — never do it yourself. `pytest: false` is fine (stdlib runner is used).

2. **Decompose** (Claude, before writing anything). Read the project, then cut the task into units:
   - one unit = 1–3 files with **one coherent public interface** that can be tested in isolation (pure functions,
     a class, a parser, an adapter behind an interface). Aim at ≲150 lines of implementation per unit.
   - dependencies only point to earlier units (a DAG); shared types/constants go in their own small first unit.
   - things that cannot be unit-tested without the outside world (network, GUI, credentials) are not outsourced:
     put the pure logic in a unit and keep the thin I/O shell for yourself.
   - each deliverable is owned by exactly one unit; never let two units write the same file.

3. **Specify and write the tests first** in a spec folder outside the project's source tree, e.g.
   `<project>/.outsource/<feature>/` holding `plan.json`, `tests/`, `hidden/`. Format: [references/plan-format.md](references/plan-format.md);
   complete example: [examples/slugkit/](examples/slugkit/plan.json). Per unit:
   - `interface`: exact signatures (`def f(a: str, *, b: int = 1) -> str`, `class C(Base)`, methods as `C.m`) and a
     one-sentence `contract` each. The gate compares parameter names, order, kinds and default presence.
   - `spec`: numbered-style behaviour rules that decide every case the tests check — **hidden tests may only test
     rules that are written here**, otherwise agy cannot pass them and it is your spec bug, not agy's.
   - `tests` (visible to agy) show the contract on typical cases; `hidden_tests` (never shown) cover edge cases and
     the rules the visible tests do not reach. Give hidden files distinct names (`test_x_hidden.py`).
   - Tests must run without pytest unless the user has it: `unittest.TestCase` or plain `def test_*(): assert ...`.
   - Plan-wide `conventions`, `imports` policy (`{"stdlib": true, "allow": [...]}`), `forbid_patterns`, `integration_test_cmd`.

4. **Check the plan**, red first:
   ```
   python scripts/outsource.py check <spec>/plan.json --red
   ```
   Fix every error. `--red` builds stubs from the interface and requires every unit's tests to **fail** against them
   (tests that pass on stubs test nothing) and to import only declared names. Optionally read one work order:
   `python scripts/outsource.py render <spec>/plan.json <unit-id>`.

5. **Run**:
   ```
   python scripts/outsource.py run <spec>/plan.json [-j 2] [--units U2,U3] [--effort high] [--timeout 20]
   ```
   Independent units run in parallel (`-j`). Re-runs are incremental: accepted units are skipped unless their spec,
   tests or dependencies changed (or `--force`). `--allow-all` lets agy run the visible tests itself inside the
   throwaway unit work dir — offer it when the user agrees; it maps to agy's `--dangerously-skip-permissions`.
   On agy quota exhaustion the run stops (exit 3) with the reset time; tell the user and re-run later.

6. **Read `report.md`** in the run dir (default `<spec>/.agy_outsource/`) and act per unit:
   - `accepted` → read the accepted code in `accepted/` (passing tests ≠ good code: check readability, needless
     complexity, rule shortcuts) and the listed assumptions/warnings. A `hidden_peek` flag means agy's tool calls
     mentioned the hidden tests: treat that unit's hidden pass as unproven and review it closely.
   - `needs_attention` → open `units/<id>/attempt_N.json` (failed gates, test output, file changes). Decide whose
     fault it is: unclear/contradictory rule or a test checking an unwritten rule → fix `plan.json`/tests and re-run;
     unit too big → split it; agy keeps failing a clear spec → implement that unit yourself, or re-run with
     `--effort high`.
   - `blocked` → resolves once its dependency is accepted.
   - integration failed → the units disagree on a contract; fix the spec of the unit at fault and re-run.

7. **Integrate** only after the integration tests passed, and only into the tree the user wants changed:
   ```
   python scripts/outsource.py integrate <spec>/plan.json --dest <project> --dry-run
   python scripts/outsource.py integrate <spec>/plan.json --dest <project> [--with-tests] [--overwrite]
   ```
   Show the user the file list from `--dry-run` first; `--overwrite` only when they agreed to replace existing files.
   Then run the project's own test suite.

8. **Report**: units and their attempts, what you changed in the spec along the way, the assumptions agy made, and
   anything you implemented yourself.

## Rules

- agy never sees the hidden tests, the plan folder or the user's project tree: each attempt runs in
  `<run>/units/<id>/work`, gets only the work order, its dependencies' accepted code, its context files and the visible
  tests, and hidden-test feedback is scrubbed of paths. Keep the run dir out of the spec's `hidden/` folder.
- Do not loosen a gate to get a unit accepted (deleting hidden tests, removing interface items). Change the rule or
  the test only when it was wrong.
- Do not edit `accepted/` by hand; fix the plan and re-run so dependents are re-checked against the new code.
- Do not edit `~/.gemini/antigravity-cli/settings.json`; if the user wants agy to run tests without `--allow-all`,
  tell them the rule to add, e.g. `"command(regex:python -m (pytest|unittest).*)"`.
- One plan per feature; do not point `--dest` at `~`, a drive root or an unrelated repo.

## Exit codes and files

Exit codes: 0 every selected unit accepted (+ integration passed), 2 some unit not accepted / integration failed /
integrate conflict, 3 agy quota or runtime error, 4 preflight, 5 bad plan or arguments.

```
<run dir>/
  state.json, report.md
  units/<id>/attempt_N.prompt.md   exact work order sent (with feedback from attempt N-1)
  units/<id>/attempt_N.json        gate results, test output tails, file changes, agy status
  units/<id>/attempt_N.agy.log     live agy progress (tail it during long runs)
  units/<id>/work/                 agy's work dir (agy-delegate metadata in .agy_task/)
  accepted/                        accepted deliverables, project layout
  integration/                     assembled tree the integration tests ran in
```

Self-test of this skill without agy: `python -m unittest discover -s scripts/tests`.

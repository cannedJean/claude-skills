---
name: agy-outsource
description: Outsource a coding task to the Antigravity CLI (`agy`) as a lead/contractor pair — Claude splits the work into unit-testable units, writes for each one a work order (deliverable files, exact public interface, behaviour rules, examples, dependencies) plus visible tests and sealed hidden tests, and agy delivers code in a strict delivery format (only the listed files + DELIVERY.json). Every delivery is accepted or rejected by deterministic gates (format, scope, interface signature, imports, forbidden patterns, test tampering, visible tests, hidden tests); before any code is written a different Claude model audits the tests against the spec, and it referees failures without seeing the code, so the test author, the coder and the judge are never the same model. Failures go back to agy as feedback, units run in dependency order, and the accepted units are integration-tested before anything touches the project. Use when the user says things like "agy한테 외주 줘", "단위별로 쪼개서 agy에게 코딩 시켜", "모듈 나눠서 agy로 구현하고 테스트로 검수해", "outsource this to agy unit by unit", or wants a multi-function / multi-module feature built by agy with test-based acceptance. For a single small chore with no test contract (or a feature under ~150 lines / fewer than 3 units) use agy-delegate directly.
---

# agy-outsource

Claude is the **lead**: it designs, specifies and writes every test. agy is the **contractor**: it implements one
unit per session and hands back files in a fixed format. A third model, the **auditor** (a Claude model that is not
yours), judges the tests before any code exists. Acceptance is done by a script, not by a model and not by eye:

```
plan.json (units + interfaces + rules)      tests/ (visible)   hidden/ (sealed)
        │  check --red: tests must fail against interface stubs
        │  audit: claude -p on the auditor model (≠ lead): every assertion must trace to a written rule
        ▼
 for unit in dependency order:
   work order ──> agy in a job dir outside every repo (deps' accepted code + visible tests; tool calls confined to it)
            <── deliverables + DELIVERY.json                     effort per attempt: low → medium → high
   gates: integrity · agy · manifest · scope · files · syntax · interface · imports · forbid · tamper
          · tests:visible · tests:hidden (the runner's result line is required, not just exit code 0)
     all ok   → accepted/ (frozen; dependents build on it)
     else     → feedback → next attempt (max 3) → needs_attention → triage by a third model → back to Claude
        ▼
 integration: all accepted code + all tests (+ integration_test_cmd)  →  integrate --dest <project>
```

Three models because the coder must never have seen the hidden tests, and a test's judge must be neither its author
nor the coder whose code failed it. Measured agy behaviour behind the sandbox: [references/isolation.md](references/isolation.md).

Builds on **agy-delegate** (`../agy-delegate/scripts/agy_task.py` or `AGY_TASK`), at least as new as this skill.
Commands are relative to this skill's directory; `python3` on macOS/Linux, `python` on Windows.

**When not to use:** decide before writing any test. Under ~150 lines or fewer than 3 units, the plan and tests cost
more output tokens than the code (measured: ~10.6 KB of plan + tests for 2.3 KB of code), plus an auditor call per
unit: write it yourself, or use agy-delegate without a test contract. Once you have written a unit's hidden tests,
its code comes only from `outsource.py run`.

## Workflow

1. **Preflight** once per machine or after an agy/auth error (it spends an agy call):
   `python scripts/outsource.py preflight`. A failed headless probe → the user must run `agy` once interactively to
   sign in (never do it yourself). `agy_task_flags.ok: false` → update agy-delegate. `pytest: false` is fine.

2. **Decompose** (Claude, before writing anything). Read the project, then cut the task into units:
   - one unit = 1–3 files with **one coherent public interface** that can be tested in isolation (pure functions,
     a class, a parser, an adapter behind an interface). Aim at ≲150 lines of implementation per unit.
   - dependencies only point to earlier units (a DAG), and only where this unit's code really calls the other's:
     independent units run in parallel. Shared types/constants go in their own small first unit.
   - things that cannot be unit-tested without the outside world (network, GUI, credentials) are not outsourced:
     put the pure logic in a unit and keep the thin I/O shell for yourself.
   - each deliverable is owned by exactly one unit; never let two units write the same file. Trivial files (an
     empty package `__init__.py`) are `context_files` under `project_root`, not deliverables; `integrate` adds a
     context file only where the destination lacks it, and never overwrites one.

3. **Specify and write the tests first** in a spec folder outside the project's source tree, e.g.
   `<project>/.outsource/<feature>/` holding `plan.json`, `tests/`, `hidden/`. Format: [references/plan-format.md](references/plan-format.md);
   complete example: [examples/slugkit/](examples/slugkit/plan.json). Set `project_root` to the project (`"../.."`
   from that folder) so `context_files` are the project's own files. A new package's `__init__.py` that does not
   exist yet: create it in the project yourself first, or use a spec-local root as the example does (`project/`,
   holding the files the units read); `integrate` adds a context file only where the destination lacks it. Per unit:
   - `interface`: exact signatures (`def f(a: str, *, b: int = 1) -> str`, `class C(Base)`, methods as `C.m`) and a
     one-sentence `contract` each. The gate compares parameter names, order, kinds and default presence.
   - `spec`: numbered-style behaviour rules that decide every case the tests check — **hidden tests may only test
     rules that are written here**, otherwise agy cannot pass them and it is your spec bug, not agy's. A signature
     is not a rule: a test expecting `TypeError` for positional use of keyword-only parameters needs one written.
   - `tests` (visible to agy) show the contract on typical cases; `hidden_tests` (never shown) cover edge cases and
     the rules the visible tests do not reach. Give hidden files distinct names (`test_x_hidden.py`); a hidden test
     may never be listed as any unit's visible test or context file (`check` refuses it).
   - Tests must run without pytest unless the user has it: `unittest.TestCase` or plain `def test_*(): assert ...`.
   - Plan-wide `conventions`, `imports` policy (`{"stdlib": true, "allow": [...]}`), `forbid_patterns`, `integration_test_cmd`.
   - `"roles": {"lead": "<your own model, e.g. opus>", "auditor": "sonnet"}`. Declare your model honestly: the
     script refuses an auditor or a Claude contractor of the same family.

4. **Check the plan**, red first:
   ```
   python scripts/outsource.py check <spec>/plan.json --red
   ```
   Fix every error (`check` also verifies `roles`). `--red` builds stubs from the interface and requires every
   unit's tests to **fail** against them (tests that pass on stubs test nothing) and to import only declared names.
   `render <spec>/plan.json <unit-id>` prints a work order without the inlined file texts (the exact prompt sent is
   `units/<id>/attempt_N.prompt.md`).

5. **Audit the tests** (mandatory: `run` starts only when every unit of the plan has a passing audit of its
   current spec and tests, also with `--units`, because integration runs all tests; `--units` on `audit` is for
   re-auditing what you changed):
   ```
   python scripts/outsource.py audit <spec>/plan.json [--auditor sonnet] [--units <unit-id>,...]
   ```
   One `claude -p --restricted` call per unit on the auditor model, sequential (use `run_in_background` for more
   than a couple of units). It gets the numbered spec items (C/R/E/K, plus the dependencies') and the test sources
   from the script, not from you, and must cite for every test the items each assertion follows from; the script
   checks every test is judged and `traced`, every cited item exists, and the model really was the auditor's.
   `untraced` / `ambiguous` → write the missing rule into `spec` (preferred) or fix the test; `contradicts` → fix
   whichever side is wrong; then audit again. Do not argue a verdict away: a rule only you know is exactly what the
   contractor cannot know. `effort`, `max_attempts` and `test_timeout` are not audited.

6. **Run**:
   ```
   python scripts/outsource.py run <spec>/plan.json [-j 2] [--units U2,U3] [--effort high] [--timeout 20]
   ```
   **Always start `run` with the Bash tool's `run_in_background`**: the worst case is `--timeout` (20 min per
   session) × attempts × units. You are notified when it exits; do not poll `units/<id>/attempt_N.agy.log`.
   Effort climbs per agy attempt: 1 `low`, 2 `medium`, 3+ `high`, from `low` again on every re-run of a unit;
   `--effort`, a unit's or the plan's `effort` (in that order) pins every attempt.
   Independent units run in parallel (`-j`). Re-runs are incremental: accepted units are skipped unless their spec,
   tests, dependencies or the acceptance rules changed (or `--force`). Leave agy without a shell: the gates run the
   tests and feed the failures back.
   `--allow-all --no-confine` lets any program agy writes read any file the user can read: only when the user
   explicitly accepts that. On agy quota exhaustion the run stops (exit 3) with the reset time; re-run later.
   An agy-delegate too old for this skill stops `run` before anything is sealed (exit 4): update agy-delegate.
   Ctrl+C kills the running sessions and starts no further attempt (those units go back to `pending`).

7. **Read `report.md`** in the run dir (default `<spec>/.agy_outsource/`) and act per unit. The attempts column
   shows each attempt's effort, e.g. `2 (low, medium)`.
   - `accepted` → read the code in `accepted/` (passing tests ≠ good code: readability, needless complexity, rule
     shortcuts; the tamper gate is static) and the assumptions/warnings. Do not redo what the gates proved (tests,
     interface, imports, file scope) or open `attempt_N.json` of an accepted unit without a flag. A unit's flags
     cover all its attempts:
     - `unconfined` (no working hook), `hidden_peek` (a tool call that ran names a hidden test),
       `confinement_unknown` (killed, or agy-delegate crashed, before it reported) → the hidden pass is
       unproven: review closely.
     - `hidden_peek_denied`, `outside_access_denied` → agy tried to get out, every such call was denied; the pass
       stands.
   - `needs_attention` → open `units/<id>/attempt_N.json` (failed gates, test output, file changes). When tests
     failed, do not decide alone whose fault it is: you wrote those tests. Ask the referee, a model that is neither
     yours nor the contractor's and that never sees the code:
     `python scripts/outsource.py triage <spec>/plan.json <unit-id>` (writes `units/<id>/triage.json`).
     `test_bug` / `spec_gap` → fix the test or write the rule, audit again, re-run. `code_bug` → re-run with
     `--effort high` (a plain re-run climbs the ladder from `low` again), or with `--contractor claude:<model>` (a
     Claude model other than yours, e.g. `claude:haiku`). Unit too big → split it. A note starting with
     `integrity:` means a gate script, the plan, a hidden test or a read-only input changed during the run: find out
     how before running again. `timeout:` means a session ran past `--timeout` and was killed (no retry in that job
     dir): re-run with a larger `--timeout` or split the unit. `agy:` means agy-delegate gave no result (outdated
     or crashed): fix that, not the unit. Notes and triage verdicts are always from the latest run of the unit.
   - `blocked` → resolves once its dependency is accepted.
   - integration failed → the units disagree on a contract; fix the spec of the unit at fault and re-run.

8. **Integrate** only after the integration tests passed for the current spec, the current accepted code and the
   current `integration_test_cmd` (a unit made stale by a later spec or test change, or re-accepted with
   `--no-integration`, is refused until `run` ran the integration again), and only into the tree the user wants
   changed:
   ```
   python scripts/outsource.py integrate <spec>/plan.json --dest <project> --dry-run
   python scripts/outsource.py integrate <spec>/plan.json --dest <project> [--with-tests] [--overwrite]
   ```
   Show the user the file list from `--dry-run` first; `--overwrite` only when they agreed to replace existing files.
   Then run the project's own test suite.

9. **Report**: units and attempts with effort, who did what (the report's table), what you changed in the spec,
   and the contractor's assumptions.

## Rules

- **Never write a unit's code yourself, and never hand it to an Agent-tool subagent.** You wrote the hidden tests,
  and in-process subagents share your file access (they can read `hidden/` and `audit.json`). Code comes only from
  `outsource.py run`: agy, or `--contractor claude:<model>` on a model other than yours.
- agy never sees the hidden tests, the plan folder or the user's project tree. Each unit runs in a random job dir
  under `%LOCALAPPDATA%\agy-outsource\jobs` (`~/.cache/agy-outsource/jobs` elsewhere; `AGY_OUTSOURCE_JOBS`
  overrides), never inside a git repository (agy takes the enclosing repository as its workspace). It holds only the
  work order's inputs and is deleted after the unit. Hidden-test feedback names no hidden file and is reduced to
  failing names and assertion lines (`hidden_feedback`, default `summary`).
- agy can only reach its job dir: agy-delegate installs a `PreToolUse` hook that denies every tool call on a path
  outside it (also hard links and `..` look-alikes) and every shell, browser and MCP tool, and reports whether the
  hook really saw every tool call; if not, the attempt counts as `unconfined`.
- As a second layer, on Windows `run` seals the hidden tests (once the plan, the arguments and the audit passed;
  milliseconds, no tokens): they move into `<spec>/hidden.sealed` (DPAPI-encrypted) and the plaintext is deleted.
  They are decrypted into a verify, red or integration dir only while no contractor session of the run is live
  (with `-j` > 1 a unit's test gate waits for the running sessions), and deleted right after the gate. To edit them:
  `outsource.py unseal PLAN`, edit `hidden/`, then `outsource.py seal PLAN`.
- The code under test shares the test process, so the `tamper` gate rejects deliveries that import test or frame
  introspection modules (`unittest`, `inspect`, `traceback`, `__main__`, ...), end the process (`sys.exit`,
  `os._exit`, `SystemExit`, also `x._exit` on anything that may be a module), use `eval` / `exec`, reach a module
  attribute through `getattr` & co. (called, or passed as a value), `operator.attrgetter`, `__dict__` / `vars()` or a
  computed name, walk frames or tracebacks (`__traceback__`, `tb_frame`, `f_back`, `f_locals`, ..., also inside a
  string, where `str.format` reaches them), or hold a string naming a test file or folder. A unit that really needs
  one lists it in `tamper_allow` (e.g. `["sys.argv"]` for a CLI unit). A test gate of a Python plan also needs the
  runner's own result line as the last line of its output (pytest, unittest or mini_test; passed > 0 and at least
  as many as the runner collects from the files, nothing failed or skipped), with a custom `test_cmd` too: exit code
  0 alone is not a pass. Both auto runners (mini_test, and pytest through `scripts/pytest_nonce/`) print a per-run
  nonce the code under test never holds in any Python object, so a printed copy does not count. Only a non-Python
  plan's `test_cmd` and the plan's `integration_test_cmd` are judged by their exit code alone.
- `check` and `run` refuse test files whose test functions the runner would not all collect (a name defined twice,
  a top-level function mini_test skips, a class that is not a `unittest.TestCase`); should a test gate still see
  fewer tests than the files hold, the unit stops at once (`tests: test collection: ...`) instead of retrying.
- The visible tests run in a tree without any hidden test, the hidden tests in a fresh one, and feedback drops any
  line that quotes a hidden test. Anything that changes the gate scripts, the plan, a read-only input or the unit's
  job dir while the tests run stops the unit (`integrity:`).
- Work orders inline each unit's direct inputs up to 24,000 characters (and what fits agy's command line): keep
  `context_files` small and never paste file contents into `spec`.
- Do not loosen a gate to get a unit accepted (deleting hidden tests, removing interface items, adding a
  `tamper_allow` the unit does not need). Change a rule or a test only when the auditor or the referee found it
  wrong, then audit again.
- Do not edit `accepted/` by hand; fix the plan and re-run so dependents are re-checked against the new code.
- With confinement on (the default) agy runs no shell command at all, whatever its settings allow: the gates run the
  tests. A `permissions.allow` rule in agy's settings only matters with `--no-confine`. Do not edit
  `~/.gemini/antigravity-cli/settings.json`.
- One plan per feature; do not point `--dest` at `~`, a drive root or an unrelated repo.

## Exit codes and files

Exit codes: 0 every selected unit accepted (+ integration passed) / audit passed, 2 some unit not accepted /
integration failed / integrate conflict / audit failed, 3 agy quota or runtime error (also a failed `claude` run),
4 preflight (also `run` with an agy-delegate too old for this skill), 5 bad plan or arguments, a missing audit (`not-audited`) or roles of one model family (`bad-roles`).

```
<run dir>/
  state.json, report.md
  units/<id>/attempt_N.prompt.md   exact work order sent
  units/<id>/attempt_N.json        gates, test output tails, file changes, agy status, effort, inlined files
                                   (those passed as --inlined; [] when no hook ran), flags of this attempt
  units/<id>/attempt_N.agy.log     live agy progress
  units/<id>/attempt_N.agy_task/   agy-delegate's record of the session (events, confinement log)
  units/<id>/attempt_N.claude.json a Claude contractor's record (model, cost, denials)
  units/<id>/triage.json           the referee's verdicts on the last failure
  accepted/                        accepted deliverables, project layout
  integration/                     assembled tree the integration tests ran in
<spec>/audit.json                  audit verdicts per unit (they quote hidden tests: keep the file with them)
<spec>/hidden.sealed               the sealed hidden tests (Windows, after the first run)
```

Self-test of this skill without agy: `python -m unittest discover -s scripts/tests`.

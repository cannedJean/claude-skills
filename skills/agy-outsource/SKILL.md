---
name: agy-outsource
description: Outsource a coding task to the Antigravity CLI (`agy`) as a lead/contractor pair — Claude splits the work into unit-testable units, writes for each one a work order (deliverable files, exact public interface, behaviour rules, examples, dependencies) plus visible tests and sealed hidden tests, and agy delivers code in a strict delivery format (only the listed files + DELIVERY.json). Every delivery is accepted or rejected by deterministic gates (format, scope, interface signature, imports, forbidden patterns, test tampering, visible tests, hidden tests); before any code is written a different Claude model audits the tests against the spec, and it referees failures without seeing the code, so the test author, the coder and the judge are never the same model. Failures go back to agy as feedback, units run in dependency order, and the accepted units are integration-tested before anything touches the project. Use when the user says things like "agy한테 외주 줘", "단위별로 쪼개서 agy에게 코딩 시켜", "모듈 나눠서 agy로 구현하고 테스트로 검수해", "outsource this to agy unit by unit", or wants a multi-function / multi-module feature built by agy with test-based acceptance. For a single small chore with no test contract use agy-delegate directly.
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
            <── deliverables + DELIVERY.json
   gates: integrity · agy · manifest · scope · files · syntax · interface · imports · forbid · tamper
          · tests:visible · tests:hidden (the runner's result line is required, not just exit code 0)
     all ok   → accepted/ (frozen; dependents build on it)
     else     → feedback → next attempt (max 3) → needs_attention → triage by a third model → back to Claude
        ▼
 integration: all accepted code + all tests (+ integration_test_cmd)  →  integrate --dest <project>
```

Why three models: the one that writes the code must never have seen the hidden tests, and the one that judges a
test must be neither the one that wrote it nor the one whose code failed it (self-preference). The measured agy
behaviour behind the sandbox: [docs/agy-outsource-isolation.md](../../docs/agy-outsource-isolation.md).

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
   - `"roles": {"lead": "<your own model, e.g. opus>", "auditor": "sonnet"}`. Declare your model honestly: the
     script refuses an auditor or a Claude contractor of the same family.

4. **Check the plan**, red first:
   ```
   python scripts/outsource.py check <spec>/plan.json --red
   ```
   Fix every error. `--red` builds stubs from the interface and requires every unit's tests to **fail** against them
   (tests that pass on stubs test nothing) and to import only declared names. Optionally read one work order:
   `python scripts/outsource.py render <spec>/plan.json <unit-id>`.

5. **Audit the tests** (mandatory: `run` refuses units whose current spec and tests have no passing audit):
   ```
   python scripts/outsource.py audit <spec>/plan.json [--auditor sonnet] [--units U2]
   ```
   A stateless `claude -p --restricted` process on the auditor model gets the numbered spec items (C/R/E/K, plus the
   dependencies' items) and the test sources from the script, not from you, and must cite for every test the items
   each assertion follows from. The script checks the answer itself: every test judged, every one `traced`, every
   cited item real, and the process really ran on the auditor model. `untraced` / `ambiguous` → write the missing
   rule into `spec` (preferred) or fix the test; `contradicts` → fix whichever side is wrong; then audit again.
   Do not argue a verdict away: a rule only you know is exactly what the contractor cannot know.

6. **Run**:
   ```
   python scripts/outsource.py run <spec>/plan.json [-j 2] [--units U2,U3] [--effort high] [--timeout 20]
   ```
   Independent units run in parallel (`-j`). Re-runs are incremental: accepted units are skipped unless their spec,
   tests or dependencies changed (or `--force`). Leave agy without a shell: the gates run the tests and feed the
   failures back. `--allow-all --no-confine` (agy's `--dangerously-skip-permissions`) lets any program agy writes read
   any file the user can read, the sealed hidden tests included; use it only when the user explicitly accepts that.
   On agy quota exhaustion the run stops (exit 3) with the reset time; tell the user and re-run later.

7. **Read `report.md`** in the run dir (default `<spec>/.agy_outsource/`) and act per unit:
   - `accepted` → read the accepted code in `accepted/` (passing tests ≠ good code: check readability, needless
     complexity, rule shortcuts) and the listed assumptions/warnings. A `hidden_peek` flag means agy's tool calls
     mentioned the hidden tests: treat that unit's hidden pass as unproven and review it closely.
   - `needs_attention` → open `units/<id>/attempt_N.json` (failed gates, test output, file changes). When tests
     failed, do not decide alone whose fault it is: you wrote those tests. Ask the referee, a model that is neither
     yours nor the contractor's and that never sees the code:
     `python scripts/outsource.py triage <spec>/plan.json <unit-id>` (writes `units/<id>/triage.json`).
     `test_bug` / `spec_gap` → fix the test or write the rule, audit again, re-run. `code_bug` → re-run with
     `--effort high`, or with `--contractor claude:<model>` (a Claude model other than yours, e.g. `claude:haiku`).
     Unit too big → split it. A note starting with `integrity:` means a gate script, the plan, a hidden test or a
     read-only input changed during the run: find out how before running again.
   - `blocked` → resolves once its dependency is accepted.
   - integration failed → the units disagree on a contract; fix the spec of the unit at fault and re-run.

8. **Integrate** only after the integration tests passed, and only into the tree the user wants changed:
   ```
   python scripts/outsource.py integrate <spec>/plan.json --dest <project> --dry-run
   python scripts/outsource.py integrate <spec>/plan.json --dest <project> [--with-tests] [--overwrite]
   ```
   Show the user the file list from `--dry-run` first; `--overwrite` only when they agreed to replace existing files.
   Then run the project's own test suite.

9. **Report**: units and their attempts, who did what (the report's table: contractor model, auditor model and
   verdict, triage verdicts), what you changed in the spec along the way, and the assumptions the contractor made.

## Rules

- **Never write a unit's code yourself, and never hand it to an Agent-tool subagent.** You wrote the hidden tests,
  and in-process subagents share your file access (they can read `hidden/` and `audit.json`). Code comes only from
  `outsource.py run`: agy, or `--contractor claude:<model>` on a model other than yours.
- agy never sees the hidden tests, the plan folder or the user's project tree. Each unit runs in a randomly named job
  dir under `%LOCALAPPDATA%\agy-outsource\jobs` (`~/.cache/agy-outsource/jobs` elsewhere; `AGY_OUTSOURCE_JOBS`
  overrides), never inside a git repository: agy takes the enclosing repository as its workspace, so a work dir
  inside the project would expose all of it. The job dir holds only the work order's inputs (dependencies' accepted
  code, context files, visible tests), agy-delegate's hook confines agy's tool calls to it, and it is deleted after
  the unit. Hidden-test feedback is path-scrubbed and reduced to failing names and assertion lines (`hidden_feedback`).
- The code under test shares the test process, so the `tamper` gate rejects deliveries that import test or frame
  introspection modules (`unittest`, `inspect`, `traceback`, ...), end the process (`sys.exit`, `os._exit`), use
  `eval` / `exec`, or hold a string naming a test file or folder. A unit that really needs one lists it in
  `tamper_allow` (e.g. `["sys.argv"]` for a CLI unit). A test gate also needs the runner's own result line (passed > 0,
  nothing failed or skipped): exit code 0 alone is not a pass.
- agy can still open absolute paths outside its work dir, so on Windows `run` seals the hidden tests first: they move
  into `<spec>/hidden.sealed` (DPAPI-encrypted) and the plaintext is deleted. They are decrypted only into
  verify/red/integration dirs while no agy session runs, and deleted again right after the gate. To edit them:
  `outsource.py unseal PLAN`, edit `hidden/`, then `outsource.py seal PLAN`.
- Do not loosen a gate to get a unit accepted (deleting hidden tests, removing interface items, adding a
  `tamper_allow` the unit does not need). Change a rule or a test only when the auditor or the referee found it
  wrong, then audit again.
- Do not edit `accepted/` by hand; fix the plan and re-run so dependents are re-checked against the new code.
- Do not edit `~/.gemini/antigravity-cli/settings.json`; if the user wants agy to run tests without `--allow-all`,
  tell them the rule to add, e.g. `"command(regex:python -m (pytest|unittest).*)"`.
- One plan per feature; do not point `--dest` at `~`, a drive root or an unrelated repo.

## Exit codes and files

Exit codes: 0 every selected unit accepted (+ integration passed) / audit passed, 2 some unit not accepted /
integration failed / integrate conflict / audit failed, 3 agy quota or runtime error (also a failed `claude` run),
4 preflight, 5 bad plan or arguments, a missing audit (`not-audited`) or roles of one model family (`bad-roles`).

```
<run dir>/
  state.json, report.md
  units/<id>/attempt_N.prompt.md   exact work order sent (with feedback from attempt N-1)
  units/<id>/attempt_N.json        gate results, test output tails, file changes, agy status
  units/<id>/attempt_N.agy.log     live agy progress (tail it during long runs)
  units/<id>/attempt_N.agy_task/   agy-delegate's record of the session (events, confinement log)
  units/<id>/triage.json           the referee's verdicts on the last failure
  accepted/                        accepted deliverables, project layout
  integration/                     assembled tree the integration tests ran in
<spec>/audit.json                  audit verdicts per unit (they quote hidden tests: keep the file with them)
```

Self-test of this skill without agy: `python -m unittest discover -s scripts/tests`.

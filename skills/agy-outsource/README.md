# agy-outsource

Lead/contractor coding with the Antigravity CLI (`agy`): the lead (Claude, or you) splits a task into
unit-testable units and writes a work order plus tests for each; `agy` implements one unit per session and hands
back the files in a fixed delivery format; a script accepts or rejects every delivery with deterministic gates and
feeds the failures back. Built on [agy-delegate](../agy-delegate/).

```
plan.json + tests/ + hidden/ ──check --red──> run ──> per unit, in dependency order:
    work order ──> agy ──> deliverables + DELIVERY.json
    gates: integrity · manifest · scope · files · syntax · interface · imports · forbid · tamper · visible/hidden tests
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
The measured agy behaviour this design rests on is in [references/isolation.md](references/isolation.md).

## Why these gates

- **Tests are written by the lead, before the code**, `check --red` proves they fail against interface stubs, and an
  auditor on another model proves every assertion follows from a written rule.
- **The contractor works in a job dir outside every repository** (`AGY_OUTSOURCE_JOBS`, default
  `%LOCALAPPDATA%\agy-outsource\jobs`): agy treats the enclosing git repository as its workspace, so a work dir
  inside the project would let it read the plan, the run records and the hidden tests without any prompt.
- **Hidden tests** are never placed where agy works, are in plaintext only while no contractor session of the run
  is live, and are deleted from the verify copy after each run (also after a crash: the next run sweeps them), so a
  delivery cannot be fitted to them; their failure output is path-scrubbed, names no hidden file (a neutral token
  replaces it) and is reduced by `hidden_feedback`. Each attempt's recorded tool calls are scanned for any mention
  of them: a call that ran gives the `hidden_peek` flag, one the hook denied gives `hidden_peek_denied`. A unit's
  flags are collected over all its attempts.
- **Timeouts**: a session (agy or a Claude contractor) that runs past `--timeout` is killed with its whole process
  tree (agy is agy-delegate's child) and the unit stops as `needs_attention`; no further attempt runs in a job dir a
  killed session wrote into. Ctrl+C kills the live sessions the same way and starts no further attempt.
- **Scope and format** are checked from file snapshots, not from what agy says it did: extra files (also a
  `.agents/hooks.json` agy wrote in an unconfined run), touched read-only files (tests, dependencies) or a wrong
  `DELIVERY.json` reject the delivery. The imports gate allows only the modules the verify tree holds, not leftovers
  of earlier attempts.
- **Tamper**: the code under test runs inside the test process, so imports of test/frame introspection modules,
  early exits (also `SystemExit`, `getattr(os, ...)`, `os.__dict__[...]`, `operator.attrgetter` and `x._exit` on
  any base that may be a module), frame and traceback walking (`__traceback__`, `tb_frame`, `f_back`, `f_locals`,
  also in strings), `eval`/`exec` and strings naming test files reject the delivery, and a test gate of a Python
  plan needs the runner's own result line as the last line (pytest, unittest or mini_test; passed > 0 and not fewer
  than the runner collects from the files, nothing failed or skipped), not just exit code 0, also with a custom
  `test_cmd`. Both auto runners' lines (mini_test, and pytest through `scripts/pytest_nonce/`) carry a per-run nonce
  kept in an OS pipe while the tests run, so a line the code prints itself does not count. A non-Python plan's `test_cmd` and `integration_test_cmd` are judged by their
  exit code only.
- **No hidden test reaches the feedback**: the visible tests (whose output goes back to agy) run in a tree without
  hidden tests, the hidden tests in a fresh tree, and any feedback line quoting a hidden test is dropped.
- **Integrity**: the gate scripts (agy-delegate's too), the plan, the hidden tests and each unit's read-only inputs
  are fingerprinted, before and after the tests; the unit's job dir must not change while the tests run or between
  attempts. A change stops the unit instead of grading against a moved baseline.
- **Interface** is checked on the AST (parameter names, order, kinds, defaults, async, class bases), so units that
  depend on each other fit together; the integration run checks it once more with all tests together.

## Why it is fast

None of these loosens a gate; they cut agy's wall time and tokens. The 3-unit example took 552 s before and 45 s
after, measured on a fast variant of this skill without the confinement hook, the sealing, the session gate, the
tamper gate and the result-line check; this version has not been re-measured.

- **Effort ladder**: attempt 1 runs agy at `low`, 2 at `medium`, 3+ at `high`; `--effort`, a unit's or the plan's
  `effort` pins it. In that measurement low effort passed every gate of the fast variant on attempt 1.
- **Nothing to look for**: the work order lists the unit's directory and inlines the files the unit reads directly
  (direct dependencies' code, context files, visible tests, its own previous attempt; up to 24,000 characters and
  only what fits agy's command line, 32,767 characters on Windows), so agy spends no model calls opening files.
  Indirect dependencies and dependencies' context files are listed only. agy-delegate's hook denies opening an
  inlined read-only file (`--inlined`); such denials are counted apart and never flagged. A Claude contractor has
  no hook and gets its own previous files listed, not inlined (its Edit tool must Read them first).
- **No report**: agy is told to stop after `DELIVERY.json` (`{unit, files, assumptions}`) and gets no report frame
  (agy-delegate `--bare`).
- **Fewer dependencies, more parallel units**: `depends_on` only where code calls code; trivial files such as an
  empty `__init__.py` are context files, not deliverables.
- **No needless re-runs**: editing the work-order template, `effort` or `max_attempts` does not make accepted units
  stale (a tightened gate does, through `ACCEPTANCE_VERSION`), and does not void their audit.

## Requirements

- agy-delegate installed next to this skill (or `AGY_TASK=/path/to/agy_task.py`), at least as new as this skill
  (`run --bare`, `--inlined`; `preflight` checks it), with agy 1.2+ signed in.
- Python 3.9+ (stdlib only; the import gate needs 3.10+ for the stdlib module list). pytest is used when installed,
  otherwise `scripts/mini_test.py` runs `unittest` cases and plain `test_*` functions (pytest loads the
  `scripts/pytest_nonce/` plugin for its nonce'd result line).
- For `audit`, `triage` and `--contractor claude:<model>`: the `claude` CLI (or `OUTSOURCE_CLAUDE`).

Try it on a **copy** of the example: `run` seals the hidden tests in place (on Windows it deletes
`hidden/*.py` and writes a DPAPI `hidden.sealed` only your Windows user can decrypt), and `check --red` / `audit`
write `.agy_outsource/` and `audit.json` next to the plan.

```bash
T=$(mktemp -d); cp -r examples/slugkit "$T/slugkit"; P="$T/slugkit/plan.json"
python scripts/outsource.py preflight                  # once per machine
python scripts/outsource.py check "$P" --red
python scripts/outsource.py audit "$P" [--units U2-slugify] [--auditor sonnet] [--force] [--timeout 15]
python scripts/outsource.py render "$P" U2-slugify
python scripts/outsource.py run "$P" -j 2              # minutes: start it in the background
python scripts/outsource.py status "$P"
python scripts/outsource.py triage "$P" U2-slugify [--auditor sonnet] [--timeout 15]
python scripts/outsource.py integrate "$P" --dest "$T/out" --dry-run [--with-tests] [--overwrite] [--partial]
python scripts/outsource.py unseal "$P"                # to edit sealed hidden tests; `seal` again after
```

`triage` runs after a unit failed its tests. `integrate` copies only units accepted under their current spec, after
the integration tests passed for that spec, that accepted code and the current `integration_test_cmd` (`--partial`:
the up-to-date accepted units even if others are not), and
adds context files (e.g. a package `__init__.py`) that the destination lacks; it never overwrites one.
`render` shows the directory listing but not the inlined file texts (those come from the job dir at run time; the
exact prompt sent is `units/<id>/attempt_N.prompt.md`).

Plan format: [references/plan-format.md](references/plan-format.md). Work-order template: [prompts/work_order.md](prompts/work_order.md).

## Options (`run`)

| Flag | Meaning |
|---|---|
| `--out DIR` | run directory (default `<plan dir>/.agy_outsource`) |
| `--units A,B` | only these units (their unaccepted dependencies are added) |
| `-j N` | independent units in parallel (default 2) |
| `--force` | re-run selected units even if accepted |
| `--max-attempts N` | attempts per unit (default plan/unit `max_attempts` or 3) |
| `--model`, `--timeout MIN` | forwarded to agy via agy-delegate |
| `--effort low/medium/high` | pin every agy attempt (default: attempt 1 `low`, 2 `medium`, 3+ `high`); not used by a Claude contractor |
| `--allow-all` | agy may run shell commands (e.g. the visible tests) in the unit work dir; needs `--no-confine` |
| `--no-confine` | do not confine agy's tool calls to the unit work dir (confined by default; the unit is flagged `unconfined`) |
| `--no-integration` | skip the final integration run |
| `--contractor claude:<model>` | a Claude model other than the lead's writes the code instead of agy |

Env: `AGY_TASK`, `AGY_TASK_MODEL`, `AGY_OUTSOURCE_JOBS` (job dir root), `AGY_OUTSOURCE_KEEP_JOBS=1` (keep job dirs
for debugging), `OUTSOURCE_CLAUDE` (path to the `claude` CLI or a stand-in).

## Tests

```bash
python -m unittest discover -s scripts/tests -v
```

Gate unit tests, plan validation, work-order listing and inlining, the effort ladder, and full runs of the example
with `scripts/tests/fake_agy_task.py` in place of agy and `scripts/tests/fake_claude.py` in place of `claude -p`
(scenarios: perfect, buggy, extra file, tampered test, wrong signature, missing manifest, forbidden print, early
exit, hidden-test peek, sandbox escape, a dependency's context file changed mid-run, a hook that never ran (on one
attempt or all), a denied view of an inlined file, a planted `.agents/hooks.json`, an agy-delegate too old for
`--bare`/`--inlined`, always failing, quota, an old attempt's quota line, quota words in the agent's narration,
agy's exit status alone failing, agy-delegate crashing after agy ran, code that writes into its job dir or prints
the hidden tests it finds; audits that fail, skip a test or run on the wrong model; same-family roles; a Claude
contractor, also on the wrong model; triage after a contractor change; integrate refusing stale units, re-accepted
code and an edited integration command; a stale dependency of `run --units`; test files the runner would not all
collect), plus a timed-out session, Ctrl+C, a Claude contractor's timeout and its reply text about rate limits,
forged result lines with `SystemExit`/`os._exit` and with a nonce sought by walking frames, the tamper detours
(frames, `__dict__`, `attrgetter`, getattr as a value), the pytest nonce plugin, a missing accepted input, the hook
files compared byte for byte with agy-delegate's, inlined text that holds a code fence, the spec hash, the feedback
filter, the process-tree kill, the session/plaintext gate and the command-line budget of a work order.

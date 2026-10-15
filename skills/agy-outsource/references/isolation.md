# agy-outsource: keeping the contractor away from the tests, and the judge away from its own work

agy-outsource accepts code only when hidden tests pass. That proof is worth something only if (a) the model that
writes the code never saw those tests, and (b) the model that decides whether a test is fair is not the one that
wrote it, nor the one whose code failed it. This note records what was measured on 2026-10-15 (agy 1.3.5, Claude
Code 2.1.300, Windows 11), the risks that followed, and the layers chosen for each.

## What was measured

| # | Experiment (headless, `--mode accept-edits --add-dir <work>`, cwd = work) | Result |
|---|---|---|
| 1 | work dir outside any git repo; `view_file` of a canary file outside it | denied: `read_file` permission auto-denied (`denied_actions`) |
| 2 | work dir `<repo>/.outsource/feat/.agy_outsource/units/U1/work` (the skill's old default layout) inside a git repo; `view_file` of `<repo>/.outsource/feat/hidden/test_x_hidden.py` | **read, no prompt**: agy takes the enclosing git repository as its workspace |
| 3 | `--dangerously-skip-permissions` (`--allow-all`); `view_file` and `type` of a file outside the work dir | both read it |
| 4 | as 3, plus `<work>/.agents/hooks.json` with a PreToolUse hook answering `deny` for out-of-root paths | both blocked: "tool call denied by pre-tool hook"; the payload carries `toolCall.name/args` and `modelName` |
| 5 | `claude -p --restricted --permission-mode dontAsk`; Read of a file outside the cwd | denied: "--restricted confines the file tools to the working directory"; `modelUsage` names the model that ran |

Found in the field: an agy session of a real outsourcing run (`~/.gemini/antigravity-cli/brain/<id>/…/transcript_full.jsonl`)
called `view_file` on `../unit.json`, `.outsource/dijkstra/.git/HEAD` and `tets/.git/HEAD`, reasoning that it
should check "if there are hidden files with relevant context". The calls failed only because those files did not
exist. The repository's own example also held plaintext hidden tests left by an earlier `check --red`
(`examples/slugkit/.agy_outsource/red/hidden/*.py`), and the self-test that copied the example picked them up.

agy keeps memory across sessions (`brain/`, `implicit/`, `conversation_summaries.db`): whatever one agy session
reads may surface in a later one.

## Risks and the layer that answers each

| Risk | Layer now |
|---|---|
| Work dir inside the project's repository → agy's workspace includes `hidden/`, `plan.json`, run records (exp. 2) | Job dir in `%LOCALAPPDATA%\agy-outsource\jobs\<random>` (`AGY_OUTSOURCE_JOBS`), refused if any ancestor is a git repo or it overlaps the plan, project or run dir; deleted after the unit. agy's own permission system then denies outside reads (exp. 1) |
| A file tool reaching outside anyway (absolute or `..` paths, hard links, `.. `-style look-alikes, any agy mode) | agy-delegate's confinement hook (`.agents/hooks.json`, exp. 4): out-of-root paths, hard-linked files, look-alike components and every shell, browser and MCP tool denied, each decision logged with its rule. The report counts it as confined only if the hook logged at least one entry per tool step; otherwise the attempt is `unconfined` |
| Shell access (`--allow-all`) → a program agy writes can read anything (exp. 3) | Off by default; needs `--no-confine` and the user's explicit OK. Not containable on native Windows: agy's terminal sandbox is documented for Linux (namespaces) and macOS (Seatbelt) only, and Claude Code's Bash sandbox is not available on native Windows. Use WSL2/Linux/macOS with agy `--sandbox`, or a container, for units that need a shell |
| Plaintext hidden tests on disk | DPAPI sealing at rest (Windows), done once the plan, the arguments and the audit passed; plaintext only in a verify/integration tree while no contractor session of the run is live (a session/plaintext gate: with `-j` > 1 a unit's test gate waits for running sessions, and no session starts while plaintext exists), removed in `finally`; every run first sweeps copies a crash left behind |
| A session that ignores its own timeout keeps running (agy is agy-delegate's child; killing agy-delegate alone leaves it) | the whole process tree is killed (a Claude contractor's too); the unit stops (`timeout:` note), no further attempt in that job dir. agy-delegate restores a killed run's leftover hook files byte for byte, so a surviving session stays hooked. On Ctrl+C every live session tree is killed and no further attempt starts |
| agy plants its own `.agents/hooks.json` in an unconfined run (it would run its command in every later session there) | the hook's files are left out of snapshots only in confined runs and only while their bytes are exactly what agy-delegate writes (outsource reads them from agy_task.py, the same comparison its own snapshots make); a hooks.json or launcher agy rewrote, or one a killed session left, fails the scope gate |
| A work order longer than the Windows command line (32,767 characters) makes every attempt fail without agy running | inlined text gets only the room the rest of the order leaves, feedback parts are capped (and cut if still too long), and agy-delegate reports a clean error instead of a traceback |
| The code under test reads or steers the tests from inside the test process (`os._exit(0)` at import made the old gate pass with no test run; `Path(__file__).parent / "hidden"`; `inspect` frames; patching `unittest`; printing `mini_test: 7 passed, ...` and raising `SystemExit`) | `tamper` gate (AST: test/frame introspection imports, early exits incl. `SystemExit`/`BaseException` and `getattr`-style detours to a module attribute, class-hierarchy dunders, `eval`/`exec`, strings naming test files; `tamper_allow` per unit) and the runner's result line as the LAST line of its output (passed > 0 and not fewer than the files define, nothing failed or skipped) instead of exit code 0, for every test command of a Python plan (a custom `test_cmd` too; a non-Python plan's `test_cmd` and every plan's `integration_test_cmd` are judged by exit code). The stdlib runner counts any exception from the code under test, `SystemExit` included, as a failure and prints a per-run nonce it read from stdin before loading any test; only that line counts (pytest: the same through the `scripts/pytest_nonce/` plugin) |
| Frame walking and detours past the name checks: a caught exception's `__traceback__.tb_frame.f_back.f_locals` read the runner's nonce, `os.__dict__['_ex'+'it'](0)` / `operator.attrgetter` / `functools.partial(getattr, ...)` ended the run with exit code 0 after a forged line (review of the merged version) | frame and traceback attributes, `__dict__`, `vars(x)`, `operator.attrgetter`/`methodcaller`, `getattr` & co. passed as values, and exit/frame attribute names on any base that is not a plainly imported module are rejected; strings naming those attributes too (`str.format` reaches them). The nonce waits in an OS pipe, not in any frame, while the tests run |
| Test files that define more tests than the runner collects (a duplicated name, a `testX` function mini_test skips) failed every attempt of correct code on the test count | the count is what the runner collects (duplicates once, runner rules); `check` / `run` refuse such files, `check --red` compares the stub run's count, and a shortfall at a test gate stops the unit at once (`tests: test collection: ...`) |
| `run --units` built a unit against a dependency that was accepted but stale (or crashed when its deliverables were renamed) | a unit counts as accepted only under its current spec hash, for scheduling, readiness, the run verdict and `integrate` alike; stale dependencies are pulled into the run, and a missing read-only input stops the unit with an `inputs missing:` note |
| The code under test prints what it reads (it runs as the user, unconfined), and the visible tests' output goes back to agy | the visible tests run in a tree that holds no hidden test; the hidden tests run in a fresh tree after them; every feedback line that quotes a line of any hidden test (12+ characters, not also in a visible test) is dropped |
| The code under test changes files while it runs (the plan, gate scripts, files in its job dir that the next attempt inlines) | the integrity gate runs again after the tests, and the whole job dir must not change from the end of the session to the end of the tests, nor between attempts; any change stops the unit (`integrity:`) |
| Hidden-test feedback quoting test source (input and expected value) over three attempts, or naming the hidden file (pytest's `FAILED hidden/test_x_hidden.py::...`) so agy searches for it | `hidden_feedback` default is now `summary` (failing names and assertion lines), and every hidden test's path, file and module name is replaced by a neutral token |
| A read-only input or the evaluator changed at its source during a run → the restored baseline moves silently | `integrity` gate: gate scripts (agy-delegate's `agy_task.py` and `confine_hook.py` too), plan, hidden tests and each unit's read-only inputs are fingerprinted; a change stops the unit with an `integrity:` note |
| Claude-side subagents (Agent tool) share the lead's file access | Rule: unit code never comes from an in-process subagent; a Claude contractor runs as `claude -p --restricted` in a job dir |
| A hidden test listed as another unit's visible test or context file → copied into that unit's work dir, and inlined into its prompt | `check` / `run` refuse the plan: no `tests` or `context_files` entry may be any unit's hidden test or share its file name |
| Inlining pulls a file the contractor must not see into its next prompt (a symlink or hard link it planted in its work dir) | only plain, single-link files whose real path is inside the work dir are inlined; inlined text is read right after the read-only inputs were restored, so it is what the integrity gate checks |
| An unconfined (or peeking) attempt fails, a later confined one passes with the same code | flags are collected over every attempt of the unit (each attempt's record holds only its own); a killed attempt, or one where agy-delegate died after agy ran, never reported its confinement and gives `confinement_unknown` |
| Code accepted under an older spec (or acceptance rules) copied into the project | `integrate` copies only units whose spec hash is current, and trusts the integration result only for the spec hashes, accepted files and `integration_test_cmd` it ran with (accepting a unit drops the old result) |
| agy's own exit status hides a problem | softened to a warning only for agy, only when every other gate (integrity, tamper, result-line tests) passed, never for a timeout, a missing result or a Claude contractor (whose rc 3 also means "wrong model")

## Self-preference: was it there, and what replaced it

The old loop was cross-model for the common path (Claude wrote the tests, agy the code, a script judged), but it
fell back on the lead judging itself at exactly the points where judgement mattered:

1. **"agy keeps failing → implement that unit yourself."** The lead then wrote code against hidden tests it had
   written and read, and reviewed that code itself: test author = coder = judge.
2. **"Decide whose fault it is."** When hidden tests failed, the lead decided whether its own test or the other
   model's code was wrong.
3. Nothing checked the rule the skill depended on — *hidden tests only check written rules* — except the lead's own
   reading of its own tests.

The roles are now held by different models, and the script checks it:

| Role | Default | Sees hidden tests | Writes code | Constraint enforced by the script |
|---|---|---|---|---|
| lead | the Claude session (e.g. Opus) | yes (author) | never | declared in `plan.json` `roles.lead` |
| contractor | agy (Gemini); fallback `claude:<model>` | never | yes | Claude contractor's family ≠ lead's, read back from `modelUsage` |
| auditor (`audit`, before any code) | `claude -p` on `roles.auditor` (Sonnet) | yes | never | family ≠ lead's; never agy (cross-session memory); `run` refuses units without a passing audit of the current spec + tests |
| referee (`triage`, after a failure) | same command, no code in the prompt | yes | never | family ≠ lead's and ≠ that unit's contractor's |
| acceptance | gates | runs them | no | deterministic |

Details that keep the separation honest:

- The audit and triage prompts are built by the script from `plan.json` and the test files, so the lead cannot frame
  the question. The spec is given as citable items (C contracts, R rules, E examples, K constraints, and the
  dependencies' items), and the auditor must cite them per test.
- The answer is read deterministically: every test function found by the AST must be judged, every verdict must be
  `traced`, every cited item must exist. The model's own overall verdict is not used.
- The audit is bound to a hash of the unit's spec, its dependencies' spec, its tests and the audit prompt; any
  change voids it. Execution knobs (`effort`, `max_attempts`, `test_timeout`) are left out: pinning an effort does
  not change what the auditor judged.
- Each auditor/referee/contractor call is a stateless process: `--restricted`, `--no-session-persistence`, auto
  memory off, cwd an empty job dir.

## Speed without a weaker proof

The contractor side was later made faster without touching a layer above. The figure behind it (the 3-unit
example: 552 s → 45 s, about ten times) was measured on a fast variant WITHOUT the confinement hook, the sealing,
the session gate, the tamper gate and the result-line check; the merged version has not been re-measured:

- **Effort ladder** (attempt 1 `low`, 2 `medium`, 3+ `high`; `--effort` or a unit/plan `effort` pins it). Effort
  changes how hard agy thinks, not what is accepted; the integrity gate still stops a unit without retries.
- **Inlined inputs** and a directory listing in the work order: everything inlined is already in the job dir, so
  nothing new is exposed, and hidden tests are never in the job dir. The hook denies opening an inlined read-only
  file (`--inlined`); those denials are counted apart (`inlined_denied_count`) so they never raise
  `outside_access_denied`.
- **No report frame** (`--bare`): the work order itself states the work-dir and confinement rules.
- **No needless re-runs**: the work-order template is no longer part of a unit's spec hash (it is still
  fingerprinted for the length of a run); `ACCEPTANCE_VERSION` in the hash re-validates accepted units whenever
  a gate tightens.
- **Quota detection** reads only what agy said in the current attempt (its `stderr.txt` and the parsed
  `AGY_ERROR`), so an old quota line in the job dir, or the agent's narration about a 429, cannot stop a run.
- **`hidden_peek`** parses agy's tool steps instead of the whole event stream: a call that ran (or was left
  running by a killed session) gives `hidden_peek` even in a confined run; a denied one gives
  `hidden_peek_denied`; prompt or feedback text echoed in the stream no longer counts. The feedback never names a
  hidden file, so acting on it cannot raise a false `hidden_peek`.
- **Token-saving denials never hide an escape**: the hook gives the `inlined` denial only when no argument of the
  call breaks another rule, so an outside path next to an inlined one is logged and counted as a real denial.

## What is still not covered

- `--allow-all --no-confine` on native Windows: arbitrary code as the user. Only an OS boundary (WSL2 with agy
  `--sandbox`, a container) closes it.
- The tamper gate is static. File paths assembled from non-literal pieces get past it, and so would a detour it
  does not list (e.g. a `str.format` field built from pieces at run time). Under both auto runners a forged result
  line no longer counts (nonce, last line, test count): the nonce sits in an OS pipe, not in any frame, so code that
  walks frames finds nothing, and code that finds and reads the pipe empties it (the runner's own line then carries
  no nonce) and would still need an early exit, which the tamper gate rejects. Under a custom `test_cmd` (`python -m
  unittest`, pytest without the plugin) a forged last line still has to claim at least the number of tests the
  runner collects, and `os._exit` itself is left to the tamper gate. Review accepted code.
- The code under test runs unconfined as the user: it can write anywhere the user can (a `usercustomize.py`, other
  folders) and read the sealed store through the user's DPAPI key. The layers above keep that from reaching agy
  through the feedback, the job dir or the accepted files, not from happening.
- `integration_test_cmd` is the lead's own check and is judged by its exit code only (it prints no result line).
- The lead's own model is declared, not detected.
- `audit.json` and `triage.json` quote hidden tests in plaintext. They stay in the spec/run dirs, out of the job
  dir's reach, but they are not sealed.

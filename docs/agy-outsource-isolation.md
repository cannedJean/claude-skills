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
| A file tool reaching outside anyway (absolute or `..` paths, any agy mode) | agy-delegate's confinement hook (`.agents/hooks.json`, exp. 4): out-of-root paths and every shell tool denied, decisions logged and reported as flags |
| Shell access (`--allow-all`) → a program agy writes can read anything (exp. 3) | Off by default; needs `--no-confine` and the user's explicit OK. Not containable on native Windows: agy's terminal sandbox is documented for Linux (namespaces) and macOS (Seatbelt) only, and Claude Code's Bash sandbox is not available on native Windows. Use WSL2/Linux/macOS with agy `--sandbox`, or a container, for units that need a shell |
| Plaintext hidden tests on disk | DPAPI sealing at rest (Windows); plaintext only in the unit's verify tree between the end of its contractor session and the test gate, removed in `finally`; every run first sweeps copies a crash left behind |
| The code under test reads or steers the tests from inside the test process (`os._exit(0)` at import made the old gate pass with no test run; `Path(__file__).parent / "hidden"`; `inspect` frames; patching `unittest`) | `tamper` gate (AST: test/frame introspection imports, early exits, `eval`/`exec`, strings naming test files; `tamper_allow` per unit) and the runner's result line (passed > 0, nothing failed or skipped) instead of exit code 0 |
| Hidden-test feedback quoting test source (input and expected value) over three attempts | `hidden_feedback` default is now `summary` (failing names and assertion lines) |
| A read-only input or the evaluator changed at its source during a run → the restored baseline moves silently | `integrity` gate: gate scripts, plan, hidden tests and each unit's read-only inputs are fingerprinted; a change stops the unit with an `integrity:` note |
| Claude-side subagents (Agent tool) share the lead's file access | Rule: unit code never comes from an in-process subagent; a Claude contractor runs as `claude -p --restricted` in a job dir |

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
  change voids it.
- Each auditor/referee/contractor call is a stateless process: `--restricted`, `--no-session-persistence`, auto
  memory off, cwd an empty job dir.

## What is still not covered

- `--allow-all --no-confine` on native Windows: arbitrary code as the user. Only an OS boundary (WSL2 with agy
  `--sandbox`, a container) closes it.
- The tamper gate is static. Computed names (`getattr(os, "_" + "exit")`) or file paths assembled from non-literal
  pieces get past it; the result-line check catches early exits but not a forged result line. Review accepted code.
- The lead's own model is declared, not detected.
- `audit.json` and `triage.json` quote hidden tests in plaintext. They stay in the spec/run dirs, out of the job
  dir's reach, but they are not sealed.

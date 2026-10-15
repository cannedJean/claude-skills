# agy-delegate

Delegate a bounded coding task to the Antigravity CLI (`agy`) headlessly and get back a precise
list of produced files plus the agent's summary. A Claude Code skill plus a standalone tool; any
agent or a human can call `scripts/agy_task.py` directly.

## Requirements

- Antigravity CLI 1.2+ (`agy`). Install:
  - macOS/Linux: `curl -fsSL https://antigravity.google/cli/install.sh | bash`
  - Windows PowerShell: `irm https://antigravity.google/cli/install.ps1 | iex`
  The binary lands in `~/.local/bin/agy` or `%LOCALAPPDATA%\agy\bin\agy.exe`; the tool finds both.
- Sign in once by running `agy` interactively (Google account via browser), or for CI set
  `GEMINI_API_KEY` and `modelProvider: "gemini"` in `~/.gemini/antigravity-cli/settings.json`.
- Python 3.9+ (stdlib only). On macOS use `python3`; on Windows `python`.

Platform notes: the tool calls `agy` as an argv list (no shell), so paths with spaces are fine on
every OS. `--verify` is the one shell-dependent option: it runs through `/bin/sh` on macOS/Linux and
`cmd.exe` on Windows, so keep it to portable commands like `python -m pytest -q`.
`--sandbox` is honoured on macOS/Linux only.

Verify:

```bash
python scripts/agy_task.py preflight
```

## Use

```bash
python scripts/agy_task.py run -w ./work/csv-tool \
  -t "Write csv_stats.py: CLI that reads a CSV path and prints row count and per-numeric-column mean/min/max. Python 3.10+, stdlib only. Add test_csv_stats.py (pytest) with a small fixture CSV." \
  --verify "python -m pytest -q"
```

```bash
python scripts/agy_task.py run -w ./work/fix -i ./src/parser.py -t "Refactor parser.py: split parse() into tokenize() and build_ast(); keep behaviour; add type hints." --collect ./out
python scripts/agy_task.py resume <conversation_id> -w ./work/fix -t "Also add a docstring to build_ast"
```

Output: JSON on stdout with `status`, `changes` (created/modified/deleted with sizes),
`structured` (summary, files, notes), `verify` (rc, output tail), `conversation_id`, `usage`,
`confinement` (`enabled`, `installed`, `root`, `log`, `agent_tool_steps`, `tool_calls`, `denied_count`,
`denied`, `inlined_denied_count`). `enabled: false` with `installed: true` means the agent made tool calls
the hook never saw: that run was NOT confined (stderr also prints a WARNING).
Per-run artifacts are kept under `<workdir>/.agy_task/<stamp>/`.

## Options

| Flag | Meaning |
|---|---|
| `-t/--task`, `-f/--task-file` | task text or file |
| `-w/--workdir` | work directory (default `./agy_tasks/<stamp>`) |
| `-i/--input PATH` | copy file/dir into the work directory first (repeatable) |
| `--verify CMD` | run a local command afterwards; non-zero → `verify-failed` |
| `--collect DIR` | copy created/modified files to DIR |
| `--effort`, `--agent`, `--timeout` | forwarded to agy (`agy agents`) |
| `--no-confine` | do not confine the agent's tool calls to the work directory (confined by default) |
| `--allow-all` | let the agent run any shell command (`--dangerously-skip-permissions`); off by default, needs `--no-confine` |
| `--inlined REL` | work-dir-relative file whose full text the task already holds; view tools on it are denied and the agent is pointed back to the prompt, saving a model call per opened file; listing, searching and editing it stay allowed. Such denials are counted in `inlined_denied_count`, apart from `denied_count`: a token-saving refusal, not an attempt to get out (repeatable; `run` only; no effect with `--no-confine`) |
| `--bare` | send the task text as is, without the rules/report frame; only for tasks that already state the work-dir rules and the delivery format (agy-outsource work orders do). `--dry-run` shows the unframed prompt |
| `--sandbox` | agy terminal sandbox (macOS/Linux) |
| `--no-schema` | skip structured output |
| `--dry-run` | print the exact agy command and framed prompt without running |

`--model SLUG` (default `AGY_TASK_MODEL`; `agy models` lists slugs) and `--agy-bin PATH` are global options and go
before the subcommand: `python scripts/agy_task.py --model <slug> run -w ... -t ...`.

Exit codes: 0 ok, 2 agent status not SUCCESS or verify failed, 3 agy error, 4 preflight, 5 bad arguments.

Env: `AGY_BIN`, `AGY_TASK_MODEL`, `AGY_TASK_TIMEOUT`.

## Permissions model

Headless agy cannot ask for approval: file edits are auto-approved via `--mode accept-edits`.
With confinement on (the default) every shell command is hard-denied by the hook below, whatever
settings.json says; use `--verify` to run tests. Only with `--no-confine` do shell commands fall back to
agy's own rules: soft-denied unless pre-allowed in `~/.gemini/antigravity-cli/settings.json`:

```json
{ "permissions": { "allow": ["command(regex:python -m pytest.*)", "command(git)"] } }
```

Rule precedence is Deny > Ask > Allow. `--allow-all` bypasses everything; reserve it for throwaway
directories. `--add-dir <workdir>` is always passed because an untrusted directory otherwise makes
agy write into its own scratch folder.

On top of that, `run` and `resume` confine the agent to the work directory with an agy `PreToolUse`
hook (`scripts/confine_hook.py`, installed as `<workdir>/.agents/hooks.json` for the run only; on Windows
it starts through a temporary `.agents/confine.cmd` launcher so paths with spaces or `&`, `^`, `%` work;
a pre-existing file of either name is restored byte for byte, also our own entry left by a killed run, whose
agy session may still be alive and must stay hooked). It
hard-denies any tool call whose path arguments resolve outside the work directory, hard-linked files,
traversal look-alike components (`.. `, `...`), every shell, browser and MCP tool, writes into `.agents/`
or `.agy_task/`, and view tools on `--inlined` files; everything else is left to the rules above (the hook
answers `ask`, which never grants more than agy would on its own). Each decision is logged to
`.agy_task/<stamp>/confine.jsonl` with the rule that fired (`shell`, `tool`, `path`, `hard_link`,
`protected`, `inlined`, `error`); `inlined` denials are counted apart from `denied_count`, which keeps
meaning "tried to get out" (a call is denied as `inlined` only when none of its arguments breaks another
rule). `enabled` is true only if the log has an entry for every tool step in the event stream and the work
dir did not change while the hook logged nothing. The hook's own files are left out of a confined run's
`changes` only while they hold exactly what was installed; anything else in `.agents/` is listed, e.g. a
`hooks.json` the agent wrote in a `--no-confine` run (agy would run it on every later session there).
Because a program the agent runs could read any file, `--allow-all` is refused unless `--no-confine` is
also given.

Tests: `python -m unittest discover -s scripts/tests` (from this folder).

## References

- Headless mode: https://antigravity.google/docs/cli/headless/
- Install & auth: https://antigravity.google/docs/cli/install
- Permissions: https://antigravity.google/docs/permissions/
- Source: https://github.com/google-antigravity/antigravity-cli

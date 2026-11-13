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

```bash
python scripts/agy_task.py run --deep-research -w ./work/dr-job -t "Execute research step"
```

```bash
# Multi-agent teamwork delegation with Argon (default for Argon)
python scripts/agy_task.py --model gemini-4-argon-high run -w ./work/auth \
  -t "/teamwork Build auth service with JWT RS256 token verification and unit tests" \
  --verify "python -m pytest -q"

# Direct single-agent execution with Argon (explicit override)
python scripts/agy_task.py --model argon run --single-agent -w ./work/fix \
  -t "Fix bug in parser.py"
```

Output: JSON on stdout with `status`, `changes` (created/modified/deleted with sizes),
`structured` (summary, files, notes), `deep_research` (under --deep-research), `verify` (rc, output tail),
`conversation_id`, `usage`, `confinement` (`enabled`, `installed`, `mode`, `root`, `log`, `agent_tool_steps`,
`tool_calls`, `denied_count`, `denied`, `inlined_denied_count`). `enabled: false` with `installed: true` means
the agent made tool calls the hook never saw: that run was NOT confined (stderr also prints a WARNING).
Per-run artifacts are kept under `<workdir>/.agy_task/<stamp>/`.

## Options

| Flag | Meaning |
|---|---|
| `-t/--task`, `-f/--task-file` | task text or file |
| `-w/--workdir` | work directory (default `./agy_tasks/<stamp>`) |
| `-i/--input PATH` | copy file/dir into the work directory first (repeatable) |
| `--verify CMD` | run a local command afterwards; non-zero → `verify-failed` |
| `--collect DIR` | copy created/modified files to DIR |
| `--effort`, `--agent` | forwarded to agy (`agy agents`) |
| `--timeout` | agy `--print-timeout`. Default `auto`: single 20m, `/teamwork` 60m, deep research 90m; ×1.5 medium, ×2 high effort; +1m per 50 KB of `--input` (max +30m). An explicit value (`45m`, `2h`, `0` = unlimited) is used as is; under 30m for `/teamwork` prints a warning |
| `--no-confine` | do not confine the agent's tool calls to the work directory (confined by default) |
| `--allow-all` | let the agent run any shell command (`--dangerously-skip-permissions`); off by default, needs `--no-confine` |
| `--deep-research` | run in deep-research confinement profile: enables `--dangerously-skip-permissions` for headless relay execution while strictly confining shell commands to `relay_step.py --job <workdir>` and `patch_gemini_app.py`, protecting caller control files, and merging `relay/result.json` into summary |
| `--dr-skill-dir PATH` | directory containing local-gemini-deep-research scripts (default: `~/.gemini/config/skills/local-gemini-deep-research` or `AGY_CONFINE_DR_DIR`) |
| `--single-agent` | override default `/teamwork` execution when using Argon model; runs as a single agent |
| `--inlined REL` | work-dir-relative file whose full text the task already holds; view tools on it are denied and the agent is pointed back to the prompt, saving a model call per opened file; listing, searching and editing it stay allowed. Such denials are counted in `inlined_denied_count`, apart from `denied_count`: a token-saving refusal, not an attempt to get out (repeatable; `run` only; no effect with `--no-confine`) |
| `--bare` | send the task text as is, without the rules/report frame; only for tasks that already state the work-dir rules and the delivery format (agy-outsource work orders do). `--dry-run` shows the unframed prompt |
| `--sandbox` | agy terminal sandbox (macOS/Linux) |
| `--no-schema` | skip structured output |
| `--dry-run` | print the exact agy command and framed prompt without running |

`--model SLUG` (default `AGY_TASK_MODEL`; `agy models` lists slugs; aliases `argon`, `아르곤`, `gemini-4-argon` normalize to `gemini-4-argon-high`) and `--agy-bin PATH` are global options and go
before the subcommand: `python scripts/agy_task.py --model <slug> run -w ... -t ...`.

## Parallel batch

`batch -m manifest.json [-w DIR] [--parallel N] [--stagger SEC]` runs independent tasks as separate agy sessions,
each in `<DIR>/<name>`, at most N (default 3, env `AGY_TASK_PARALLEL`) at a time; session starts in the first wave
are spread by `--stagger` seconds (default 5). Manifest: a list (or `{"tasks": [...]}`) of objects with `name`
(unique; letters, digits, `.`, `_`, `-`), `task` or `task_file`, and optionally `inputs`, `workdir`, `model`,
`effort`, `timeout`, `verify`, `collect`, `single_agent`, `bare`, `no_schema`, `inlined`, `agent`, `dry_run`.
Output: `{status: ok|partial|failed, parallel, elapsed_sec, batch_dir, tasks: [{name, status, exit_code, workdir,
started_at, elapsed_sec, log, result}]}`, also saved as `<DIR>/batch_result.json`; per-task stderr goes to
`<DIR>/<name>.log`. Use it only for parts that do not consume each other's output.

Exit codes: 0 ok, 2 agent status not SUCCESS, verify failed, `timeout-partial` (agy print timeout; files so far are
in `changes`) or batch `partial`, 3 agy error or batch `failed`, 4 preflight, 5 bad arguments.

stdout/stderr are written as UTF-8 regardless of the console code page (Windows cp949/cp1252 consoles used to
crash on characters such as U+2013 after agy had finished).

Env: `AGY_BIN`, `AGY_TASK_MODEL`, `AGY_TASK_TIMEOUT`, `AGY_TASK_PARALLEL`.

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
(except `/teamwork`'s own `.agents/teamwork/**`) or `.agy_task/`, `ask_question` (refused with "proceed with
reasonable defaults and state your assumptions": nobody answers a headless run), and view tools on `--inlined`
files. For `/teamwork` the default mode also allows, outside the work directory, agy's scratch folder
`~/.gemini/antigravity-cli/scratch/teamwork/<conversation>/**` and `brain/<id>/teamwork_prompt_<conversation>.md`
for the run's own conversation only (the first one touched is pinned in `.agy_task/<stamp>/teamwork_conversation`;
any other stays denied), and read-only access to `~/.gemini/config/plugins/**`. Everything else is left to the rules above (the hook
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

### /teamwork Multi-Agent Delegation & Argon Default

Antigravity CLI (`agy -p`) supports multi-agent teamwork workflows via the `/teamwork` slash command:
- **Slash command mechanics**: Slash commands must appear at index 0 (the leading token) of the prompt sent to `agy -p`. `agy_task.py` automatically detects leading slash commands like `/teamwork` and sends them unframed so that Antigravity's slash command interceptor reliably triggers.
- **Argon default rule**: When delegating with the Argon model (`gemini-4-argon-*` or aliases `argon`, `아르곤`), execution defaults to `/teamwork` multi-agent delegation unless direct single-agent execution is explicitly requested (`--single-agent`).
- **Single-agent override**: Pass `--single-agent` to bypass `/teamwork` and use standard single-agent prompt framing when delegating with Argon.

### Deep-Research Confinement Profile (--deep-research)

In `--deep-research` mode, `agy-delegate` configures a hardened confinement profile designed for orchestrating local Gemini Deep Research relay steps without human interaction:
- **Headless Operator Execution**: Headless `agy` requires `--dangerously-skip-permissions` to invoke Python scripts without interactive terminal prompts. Unlike normal `--allow-all`, `--deep-research` requires confinement to be active (`--no-confine` is rejected with exit code 5) and injects `AGY_CONFINE_MODE="deep-research"`.
- **Command Whitelist & Shell Restriction**: `confine_hook.py` inspects every `run_command` invocation. Commands containing shell chaining or redirection metacharacters (`&`, `|`, `;`, `` ` ``, `$()`, `>`, `<`, newlines) are blocked (`rule: "shell"`). The command line is tokenized to verify that the executable is Python (Token 0), python options `-c` and `-m` are blocked, and the script basename is strictly either `relay_step.py` (with `--job <workdir>` canonically matching the current work directory) or `patch_gemini_app.py`. Any other command or script is denied (`rule: "shell"`).
- **Two-Way Filesystem Boundaries**: Read tools (`view_file`, `list_dir`, `grep_search`, `find_by_name`, etc.) are permitted inside `<workdir>` and `AGY_CONFINE_DR_DIR` (allowing `agy` to read relay scripts and documentation). Reading anywhere outside both directories is denied (`rule: "path"`). Write tools are permitted only inside `<workdir>`; `AGY_CONFINE_DR_DIR` is strictly read-only.
- **Caller Control File Protection**: Caller orchestration files inside `<workdir>` (`relay/request.json`, `prompt.md`, `goal.md`, `feedback_*.md`, `relay/result.json`, `relay/started.json`) cannot be overwritten or edited by `agy` file tools (`rule: "protected"`). Reads on these files remain permitted.
- **Tool Exclusions**: Browser tools (`browser_*`), MCP tools, and interactive terminal tools (`send_command_input`, `read_terminal`, `command_status`) are blocked (`rule: "tool"` or `rule: "shell"`).

Tests: `python -m unittest discover -s scripts/tests` (from this folder).

## References

- Headless mode: https://antigravity.google/docs/cli/headless/
- Install & auth: https://antigravity.google/docs/cli/install
- Permissions: https://antigravity.google/docs/permissions/
- Source: https://github.com/google-antigravity/antigravity-cli

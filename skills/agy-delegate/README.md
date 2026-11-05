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
| `--effort`, `--agent`, `--timeout` | forwarded to agy (`agy agents`) |
| `--no-confine` | do not confine the agent's tool calls to the work directory (confined by default) |
| `--allow-all` | let the agent run any shell command (`--dangerously-skip-permissions`); off by default, needs `--no-confine` |
| `--deep-research` | run in deep-research confinement profile: enables `--dangerously-skip-permissions` for headless relay execution while strictly confining shell commands to `relay_step.py --job <workdir>` and `patch_gemini_app.py`, protecting caller control files, and merging `relay/result.json` into summary |
| `--dr-skill-dir PATH` | directory containing local-gemini-deep-research scripts (default: `~/.gemini/config/skills/local-gemini-deep-research` or `AGY_CONFINE_DR_DIR`) |
| `--inlined REL` | work-dir-relative file whose full text the task already holds; view tools on it are denied and the agent is pointed back to the prompt, saving a model call per opened file; listing, searching and editing it stay allowed. Such denials are counted in `inlined_denied_count`, apart from `denied_count`: a token-saving refusal, not an attempt to get out (repeatable; `run` only; no effect with `--no-confine`) |
| `--bare` | send the task text as is, without the rules/report frame; only for tasks that already state the work-dir rules and the delivery format (agy-outsource work orders do). `--dry-run` shows the unframed prompt |
| `--allow-browser-origin ORIGIN` | let agy's `/browser` (Chrome DevTools MCP) tools browse this http(s) origin (repeatable; `run` and `resume`). Needs confinement, not with `--deep-research`; refused (exit 5) while one of the user's Chrome profiles has a live DevTools port. See "Browser allowlist" below |
| `--sandbox` | agy terminal sandbox (macOS/Linux) |
| `--no-schema` | skip structured output |
| `--dry-run` | print the exact agy command and framed prompt without running |

`--model SLUG` (default `AGY_TASK_MODEL`; `agy models` lists slugs) and `--agy-bin PATH` are global options and go
before the subcommand: `python scripts/agy_task.py --model <slug> run -w ... -t ...`.

Exit codes: 0 ok, 2 agent status not SUCCESS or verify failed, 3 agy error, 4 preflight, 5 bad arguments.

Env: `AGY_BIN`, `AGY_TASK_MODEL`, `AGY_TASK_TIMEOUT`, `AGY_CHROME_USER_DATA_DIRS` (path-separated Chrome
user-data folders checked for a live `DevToolsActivePort`; default: the Chrome/Chromium channels of this OS).

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
allows only the tools it knows how to check (file read/write/edit/list/search tools, `search_web`,
`read_url_content`, task/subagent/message tools, `schedule`, `ask_question`, `finish`; plus `run_command`
under `--deep-research` and the Chrome DevTools tools under `--allow-browser-origin`) and denies every other tool
(`rule: "tool"`, e.g. `run_workflow`, `sed_file`), so a new agy tool fails closed. It
hard-denies any tool call whose path arguments resolve outside the work directory, scheme-looking values such as
`zz://../x` or `http://../x` in a file argument (Node resolves them as relative paths; only arguments named
`*url`/`*uri`/`*href` may hold an http(s) URL), hard-linked files, writes into agy/git configuration folders at any depth (`.agents`, `.agent`, `_agents`,
`_agent`, `.agy_task`, `.git`), URL arguments that point at local, private or metadata hosts, a subagent
`Workspace` other than the work dir, the browser subagent (`TypeName: browser`) unless browser mode is on,
traversal look-alike components (`.. `, `...`), every shell tool (also `shell_exec`, `run_extension_code`,
`execute_notebook`, `restart_dev_server`), every browser tool (any name with a `browser` word, e.g.
`open_browser_url`), MCP tools and MCP resources (`read_resource`, `list_resources`), writes into `.agents/`
or `.agy_task/`, and view tools on `--inlined` files. Prose arguments are never treated as paths (contents, queries, `notes`, and
per tool `edit_file` `Target`/`Replacement`, `finish` `summary`/`code`); everything else is left to the rules above (the hook
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

### Browser allowlist (--allow-browser-origin)

Headless agy has no browser tool until the task mentions `/browser`; it then runs a browser subagent whose
tools are agy's built-in Chrome DevTools MCP (`mcp_chrome_devtools_*`). With `--allow-browser-origin`, the
runner passes `AGY_CONFINE_BROWSER_ORIGINS` to the hook, which then answers `ask` (agy decides) for:
`navigate_page`/`new_page` to a listed origin (scheme, host and port must match exactly; credentials,
`file:`, `chrome:`, `javascript:` and other hosts are denied, as is `initScript`), `back`/`forward`/`reload`,
`click`, `hover`, `drag`, `press_key` with navigation keys only (Tab, Shift+Tab, Escape, Enter, Space,
arrows, PageUp/PageDown, Home, End), `take_snapshot`, `take_screenshot`, `wait_for`, `emulate`,
`resize_page`, console/network/CSS reads, `lighthouse_audit` (with `outputDirPath`), performance traces,
screencasts (with `filePath`), `handle_dialog` (without `promptText`), `list_pages`, `select_page`. It denies
`fill`, `fill_form`, `type_text`, `evaluate_script`, `upload_file`, `take_heapsnapshot`, `close_page`,
`emulate` with `userAgent`/`extraHttpHeaders`, non-object arguments and any Chrome DevTools tool it does not
know (`rule: "tool"`); navigation outside the list is `rule: "url"`; every argument that names a file, under
any key (e.g. `get_network_request` `responseFilePath`), must be an absolute path (the MCP resolves relative
paths against its own folder) inside the work dir, outside
`.agents/`/`.agy_task/` and not be a hard link (`rule: "path"`, `protected`, `hard_link`). Every other browser
or MCP tool stays denied. Browser calls are logged with their `url`/`type`/`key`.

The runner accepts only public origins with plain ASCII hosts: it refuses `localhost` and local-only suffixes
(`.local`, `.lan`, `.internal`, `.home.arpa`, ...), single-label names, IPv6 literals, non-global IPs
(loopback, private, link-local, CGNAT, numeric-obfuscated forms), percent-encoded, unicode or full-width hosts
and trailing dots; the hook applies the same ASCII rule to every navigation URL and argument names that differ
only in case are denied. It refuses browser mode while any `AGY_BROWSER_*`, `CHROME_DEVTOOLS_MCP*`,
`JETSKI_EVAL_*CHROME*` or agy path override (`ANTIGRAVITY_NODE_PATH`, `ANTIGRAVITY_LS_ADDRESS`, ...) is set,
refuses an origin whose host does not resolve or resolves to any non-public address (checked once at launch;
DNS rebinding during the run is not covered), and drops any
inherited `AGY_CONFINE_*` variable from agy's environment so only the flags of this run count.

Limits: the hook sees tool arguments, not the page. Clicking a link (or `back`/`forward`, `select_page`) can
take the browser off the allowed origin; typing, scripts and uploads stay blocked there too, and the browser is
a fresh profile without the user's sign-ins, but say so when reporting.

agy's Chrome DevTools MCP auto-connects to a Chrome that exposes remote debugging (its `DevToolsActivePort`).
If the user's everyday Chrome does, the browser tools would drive that signed-in profile, so the runner
refuses browser mode (exit 5) while any default Chrome user-data folder has a `DevToolsActivePort` whose port
accepts connections; turn remote debugging off (`chrome://inspect/#remote-debugging`) to let agy start its
own browser. Clicks are not filtered by target: a button that sends a paid request or writes data on the
allowed site is reachable, so name such buttons in the task as off-limits.

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

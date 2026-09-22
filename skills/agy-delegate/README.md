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
`structured` (summary, files, notes), `verify` (rc, output tail), `conversation_id`, `usage`.
Per-run artifacts are kept under `<workdir>/.agy_task/<stamp>/`.

## Options

| Flag | Meaning |
|---|---|
| `-t/--task`, `-f/--task-file` | task text or file |
| `-w/--workdir` | work directory (default `./agy_tasks/<stamp>`) |
| `-i/--input PATH` | copy file/dir into the work directory first (repeatable) |
| `--verify CMD` | run a local command afterwards; non-zero → `verify-failed` |
| `--collect DIR` | copy created/modified files to DIR |
| `--model`, `--effort`, `--agent`, `--timeout` | forwarded to agy (`agy models` / `agy agents`) |
| `--allow-all` | let the agent run any shell command (`--dangerously-skip-permissions`); off by default |
| `--sandbox` | agy terminal sandbox (macOS/Linux) |
| `--no-schema` | skip structured output |
| `--dry-run` | print the exact agy command and framed prompt without running |

Env: `AGY_BIN`, `AGY_TASK_MODEL`, `AGY_TASK_TIMEOUT`.

## Permissions model

Headless agy cannot ask for approval: file edits are auto-approved via `--mode accept-edits`,
shell commands are soft-denied unless pre-allowed in `~/.gemini/antigravity-cli/settings.json`:

```json
{ "permissions": { "allow": ["command(regex:python -m pytest.*)", "command(git)"] } }
```

Rule precedence is Deny > Ask > Allow. `--allow-all` bypasses everything; reserve it for throwaway
directories. `--add-dir <workdir>` is always passed because an untrusted directory otherwise makes
agy write into its own scratch folder.

## References

- Headless mode: https://antigravity.google/docs/cli/headless/
- Install & auth: https://antigravity.google/docs/cli/install
- Permissions: https://antigravity.google/docs/permissions/
- Source: https://github.com/google-antigravity/antigravity-cli

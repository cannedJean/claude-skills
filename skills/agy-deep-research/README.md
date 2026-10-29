# agy-deep-research

Claude Code skill that orchestrates Google Gemini Deep Research through the Antigravity CLI (`agy`) and the locally installed Windows Gemini desktop app (`Gemini.exe`) without API keys.

It combines:
1. **`skills/agy-delegate --deep-research`**: A confined headless `agy` execution profile (`AGY_CONFINE_MODE="deep-research"`) that restricts file reads/writes and shell commands via a `PreToolUse` hook (`confine_hook.py`).
2. **`scripts/deep_research.py`**: A stdlib-only orchestrator implementing the Claude-owned plan review loop (`init` → `plan` → `edit`* → `approve --reason`), detached execution (`--no-wait`, `status`, `collect`), unattended `--auto` mode, live `progress.jsonl` tailing, and deterministic quality/provenance gates.
3. **Bundled Relay & Playwright Drivers**: Self-contained copies of `relay_step.py`, `run_deep_research.py`, `patch_gemini_app.py`, and `references/SKILL.md` with `preflight` and `setup` commands to keep `~/.gemini/config/skills/local-gemini-deep-research/` in sync.

## Requirements

- **Python**: 3.9+ (`deep_research.py` is stdlib-only; `run_deep_research.py` and `relay_step.py` use `playwright` for CDP connection).
- **Antigravity CLI (`agy`)**: 1.2+ signed in (`agy_task.py preflight`).
- **Google Gemini Desktop App (`Gemini.exe`)**: Installed under `%LOCALAPPDATA%\Google\Gemini\app-*` on Windows and signed in to a Google account with Gemini Deep Research access.
- **Playwright**: `pip install playwright` (connects over CDP `http://127.0.0.1:9223` to the running `Gemini.exe` Electron process; no browser download needed).

## One-Time Setup & Preflight

```bash
# 1. Synchronize bundled scripts and SKILL.md into ~/.gemini/config/skills/local-gemini-deep-research
python skills/agy-deep-research/scripts/deep_research.py setup

# 2. Check agy, Playwright, Gemini app installation/CDP port, and SHA-256 sync status
python skills/agy-deep-research/scripts/deep_research.py preflight
```

## Subcommands & CLI Reference

| Subcommand | Key Flags | Description |
|---|---|---|
| `preflight` | `[--target-dir DIR]` | Verify `agy`, `playwright`, `Gemini.exe` / CDP port `9223`, and SHA-256 sync status of bundled scripts. |
| `setup` | `[--target-dir DIR]` | Copy/update `relay_step.py`, `run_deep_research.py`, `patch_gemini_app.py`, and `SKILL.md` into the target skill directory. |
| `init` | `--prompt TEXT\|--prompt-file FILE` `--goal TEXT\|--goal-file FILE` `[--job ID_OR_PATH]` `[--out PATH]` | Create an isolated job directory outside any `.git` repo and write `prompt.md` + `goal.md`. |
| `plan` | `--job ID_OR_PATH` `[--deadline 420]` `[--show]` | Execute the `plan` relay step via `agy_task.py run --deep-research` and save `plan_v1.md`. |
| `edit` | `--job ID_OR_PATH` `--feedback TEXT\|--feedback-file FILE` `[--deadline 420]` | Send plan modification instructions (`edit` relay step) and save `plan_vN.md`. |
| `approve` | `--job ID_OR_PATH` `--reason TEXT` (`>=20` chars) `[--out PATH]` `[--no-wait]` `[--timeout 1200]` | Enforce Plan Approval Gate, start detached research (`mode="detach"`), stream `relay/progress.jsonl` to `stderr`, enforce Report Quality Gate, and write `<out>.md` + `<out>.md.meta.json`. |
| `auto` | `--prompt ...` `--goal ...` `[--reason TEXT]` `[--out PATH]` `[--no-wait]` `[--timeout 1200]` | Unattended end-to-end execution (`init` + `auto` detached relay step + live progress streaming + Report Quality Gate). |
| `status` | `--job ID_OR_PATH` | Inspect job status, detached worker `pid_alive(pid)` state, and latest `progress.jsonl` entry. |
| `collect` | `--job ID_OR_PATH` `[--out PATH]` `[--no-wait]` `[--timeout 1200]` | Re-attach to a running detached worker or collect a finished report from the Gemini conversation, then run the Report Quality Gate. |

## Job Directory Layout & Relay Protocol (`relay_version: 2`)

Jobs are created under `%LOCALAPPDATA%\agy-deep-research\jobs\<job_id>` on Windows (`~/.cache/agy-deep-research/jobs/<job_id>` on POSIX, or `$AGY_DR_JOBS/<job_id>`). Any path inside a `.git` repository is rejected with exit code `5` (`GIT_REPO_ESCAPE`).

```
<job_dir>/
  job.json              # Orchestrator state, sequence counter, URLs, plan count, confinement summary
  prompt.md             # Exact research prompt (protected from agy writes)
  goal.md               # Fixed acceptance criteria for plan review (protected from agy writes)
  plan_v1.md, plan_v2.md
  feedback_2.md         # Plan edit feedback (protected from agy writes)
  approval.md           # Recorded >=20-char approval reason
  report.md             # Extracted Markdown report + numbered sources
  relay/
    request.json        # Step request written by deep_research.py (protected from agy writes)
    started.json        # Detached worker PID & timestamp written by relay_step.py
    progress.jsonl      # Live progress stream: {"ts": ..., "elapsed_s": ..., "text": ...}
    result.json         # Step completion payload written by relay_step.py
```

## Permissions & Confinement Model (`--deep-research`)

When `deep_research.py` invokes `agy_task.py run --deep-research -w <job_dir>`, `agy` runs with a `PreToolUse` hook (`confine_hook.py` with `AGY_CONFINE_MODE="deep-research"`):

- **Read tools**: Allowed inside `<job_dir>` and `AGY_CONFINE_DR_DIR`. Reads outside both are denied (`rule: "path"`).
- **Write tools**: Allowed only inside `<job_dir>`. Writes to `AGY_CONFINE_DR_DIR` are denied (`rule: "path"`). Caller control files inside `<job_dir>` (`relay/request.json`, `prompt.md`, `goal.md`, `feedback_*.md`, `relay/result.json`, `relay/started.json`) are protected (`rule: "protected"`).
- **Shell tool (`run_command`)**: Restricted to clean Python invocations of `relay_step.py --job <job_dir>` or `patch_gemini_app.py` with a strict Python flag whitelist (`-u`, `-B`, `-E`, `-s`, `-S`, `-O`, `-OO`). Shell chaining/redirection (`&&`, `||`, `;`, `|`, `>`, `<`, `` ` ``, `$()`, newlines) and arbitrary commands (`python -c`, `python -m`, etc.) are denied (`rule: "shell"`).
- **Browser & MCP tools**: Unconditionally denied (`rule: "tool"`).

## Running Tests

All unit tests run completely offline in seconds using deterministic fakes (`fake_agy_dr.py`):

```bash
# Test agy-deep-research orchestrator, gates, streaming, and error recovery (11 tests)
python -m unittest discover -s skills/agy-deep-research/scripts/tests -v

# Test agy-delegate confinement hook including deep-research mode (51 tests)
python -m unittest discover -s skills/agy-delegate/scripts/tests -v
```

# agy-deep-research

Production-grade Claude Code skill that orchestrates Google Gemini Deep Research headlessly through the Antigravity CLI (`agy`) and the locally installed Windows Gemini desktop app (`Gemini.exe`) without API keys.

## Architecture

The system is structured into four decoupled layers:

1. **Lead Orchestrator (`scripts/deep_research.py`)**:
   - Python stdlib-only coordinator.
   - Implements the Claude-owned research plan review loop (`init` → `plan` → `edit`* → `approve --reason`).
   - Supports asynchronous detached execution (`--no-wait`, `status`, `collect`) and unattended `--auto` mode.
   - Enforces 5 deterministic quality, provenance, and isolation gates.

2. **Operator Relay Layer (`scripts/relay_step.py` & `skills/agy-delegate`)**:
   - Translates `<job>/relay/request.json` into single-step driver invocations.
   - Spawns background workers (`mode: "detach"`) and streams live thoughts to `relay/progress.jsonl`.
   - Strictly confined by `confine_hook.py` (`PreToolUse` hook with `AGY_CONFINE_MODE="deep-research"`).

3. **CDP Automation Layer (`scripts/run_deep_research.py`)**:
   - Interacts over Chrome DevTools Protocol (CDP port 9223) with the running Gemini Electron renderer using Playwright.
   - Bypasses background window throttling via `Page.setWebLifecycleState` (`active`) and `Emulation.setFocusEmulationEnabled`.
   - Intercepts and cancels the automatic countdown fuse (`[data-test-id="cancel-fuse-button"]`) to enforce plan review.
   - Emits structured hook events and converts completed DOM reports to GitHub-Flavored Markdown with source links.

4. **Desktop Electron Launcher & Patcher (`scripts/patch_gemini_app.py`)**:
   - Dynamically patches packaged Electron security checks in `%LOCALAPPDATA%\Google\Gemini\app-*\resources\app.asar`.
   - Resilient against minifier variable renaming across versions (e.g. `m.app` in `1.16.x`, `h.app` in `1.17.x+`, `$m` as the logger argument in `1.19.0`) via generalized regex `([\w$]+)\.app\.isPackaged` (`$` is allowed because minified names may contain it).
   - Preserves exact byte length and recalculates ASAR header SHA-256 integrity hash.
   - Launches `Gemini.exe --hide` in the background via WMI (`Win32_Process.Create`) to prevent window focus stealing.

## Requirements

- **Python**: 3.9+ (`deep_research.py` is stdlib-only; `run_deep_research.py` and `relay_step.py` use `playwright`).
- **Antigravity CLI (`agy`)**: 1.2+ signed in (`agy_task.py preflight`).
- **Google Gemini Desktop App (`Gemini.exe`)**: Installed under `%LOCALAPPDATA%\Google\Gemini\app-*` on Windows and signed in to a Google account with Gemini Deep Research access.
- **Playwright**: `pip install playwright` (connects over CDP `http://127.0.0.1:9223` to the running `Gemini.exe` Electron process; no separate browser download needed).

## Setup & Preflight

```bash
# 1. Synchronize bundled scripts and SKILL.md into ~/.gemini/config/skills/local-gemini-deep-research
python skills/agy-deep-research/scripts/deep_research.py setup

# 2. Check agy, Playwright, Gemini desktop app installation, CDP port 9223, and sync status
python skills/agy-deep-research/scripts/deep_research.py preflight
```

## CLI Reference & Subcommands

### Orchestrator Subcommands (`deep_research.py`)

| Subcommand | Key Flags | Description |
|---|---|---|
| `preflight` | `[--target-dir DIR]` | Verify `agy`, `playwright`, `Gemini.exe` / CDP port `9223`, and SHA-256 sync status of bundled scripts. |
| `setup` | `[--target-dir DIR]` | Copy/update `relay_step.py`, `run_deep_research.py`, `patch_gemini_app.py`, and `SKILL.md` into target skill. |
| `compose-prompt` | `--objective TEXT` `--goal TEXT` `[--context-scope TEXT]` `[--axes TEXT]` `[--sources-policy TEXT]` `[--output-spec TEXT]` `[--workspace-sources TEXT]` `[--entities TEXT]` `[--metrics TEXT]` `[--prompt-out FILE]` `[--goal-out FILE]` | Deterministically compose a 5-block XML `prompt.md` and distinct `goal.md` verification rubric. |
| `init` | `--prompt TEXT\|--prompt-file FILE` `--goal TEXT\|--goal-file FILE` `[--compose-xml]` `[--strict-xml]` `[--context-scope TEXT]` `[--axes TEXT]` `[--sources-policy TEXT]` `[--output-spec TEXT]` `[--workspace-sources TEXT]` `[--job ID_OR_PATH]` `[--agy-model MODEL]` `[--agy-effort EFFORT]` `[--gemini-model MODEL]` `[--out PATH]` | Create an isolated job directory outside any `.git` repo, validate Dual-File & 5-Block XML architecture, write `prompt.md` + `goal.md`, and initialize model/effort settings. |
| `plan` | `--job ID_OR_PATH` `[--strict-xml]` `[--agy-model MODEL]` `[--agy-effort EFFORT]` `[--gemini-model MODEL]` `[--deadline SEC]` `[--show]` | Execute `plan` relay step via `agy_task.py run --deep-research` and save `plan_v1.md` (auto-extends deadline to `600s` for `Argon`). |
| `edit` | `--job ID_OR_PATH` `--feedback TEXT\|--feedback-file FILE` `[--allow-questions]` `[--agy-model MODEL]` `[--agy-effort EFFORT]` `[--gemini-model MODEL]` `[--deadline SEC]` | Validate imperative delta feedback (`FEEDBACK_INTERROGATIVE_DENIED` on `?`), send `edit` relay step, and save `plan_vN.md`. |
| `approve` | `--job ID_OR_PATH` `--reason TEXT` (`>=20` chars) `[--agy-model MODEL]` `[--agy-effort EFFORT]` `[--gemini-model MODEL]` `[--out PATH]` `[--no-wait]` `[--timeout SEC]` | Enforce Plan Approval Gate, start detached research (`mode="detach"`, auto-extends timeout to `1800s` for `Argon`), stream `progress.jsonl` to `stderr`, enforce Report Quality Gate, and write `<out>.md` + `<out>.md.meta.json`. |
| `auto` | `--prompt ...` `--goal ...` `[--compose-xml]` `[--strict-xml]` `[--agy-model MODEL]` `[--agy-effort EFFORT]` `[--gemini-model MODEL]` `[--reason TEXT]` `[--out PATH]` `[--no-wait]` `[--timeout SEC]` | Unattended end-to-end execution (`init` + `auto` detached relay step + live streaming + Quality Gate). |
| `status` | `--job ID_OR_PATH` | Inspect job status, detached worker `pid_alive(pid)` state, and latest `progress.jsonl` entry. |
| `collect` | `--job ID_OR_PATH` `[--agy-model MODEL]` `[--agy-effort EFFORT]` `[--gemini-model MODEL]` `[--out PATH]` `[--no-wait]` `[--timeout SEC]` | Re-attach to a running worker or collect finished report from conversation, then run Report Quality Gate. |

### Supporting Scripts

- **`patch_gemini_app.py`**:
  ```bash
  python scripts/patch_gemini_app.py --help
  python scripts/patch_gemini_app.py [--port 9223] [--check] [--dry-run] [--show] [--restore]
  ```
- **`relay_step.py`**:
  ```bash
  python scripts/relay_step.py --help
  python scripts/relay_step.py --job <job_dir> [--dry-run]
  ```
- **`run_deep_research.py`**:
  ```bash
  python scripts/run_deep_research.py --help
  python scripts/run_deep_research.py --step {auto,plan,edit,approve} --prompt <text> [--output <path>] [--dry-run]
  ```

## Model & Reasoning Effort Options

`agy-deep-research` supports explicitly pinning Antigravity and Gemini models as well as thinking effort levels.

### Default Behavior

When options and environment variables are omitted:
- **Antigravity CLI Model (`--agy-model`)**: `flash 3.8` (reasoning effort: `high`)
- **Google Gemini Desktop Model (`--gemini-model`)**: `Pro` (thinking level: `높음` / `high`)

### CLI Options

| Flag | Description | Default | Choices / Friendly Aliases |
|---|---|---|---|
| `--agy-model` | Antigravity model slug passed to `agy_task.py --model` | `flash 3.8` | `flash`, `flash 3.8`, `플래시`, `pro`, `프로`, or any custom model slug |
| `--agy-effort` | Antigravity reasoning effort passed to `agy_task.py ... --effort` | `high` | `low` (`낮음`), `medium` (`중간`), `high` (`높음`, `최대`) |
| `--gemini-model` | Gemini desktop app model mode and thinking level | `pro` | `pro`, `프로`, `high`, `높음` -> `("Pro", "high")`; `flash`, `플래시` -> `("Flash", "high")`; `argon`, `아르곤` -> `("Argon", "high")` |

### Environment Variables & Precedence

Model options can also be provided via environment variables. The resolution hierarchy is:
1. **CLI Flag**: `--agy-model`, `--agy-effort`, `--gemini-model` passed to subcommand
2. **Job State**: Persisted in `<job_dir>/job.json` (inherited across `plan`, `edit`, `approve`, `collect`)
3. **Specific Environment Variables**: `AGY_DR_MODEL`, `AGY_DR_EFFORT`, `GEMINI_DR_MODEL`, `GEMINI_DR_THINKING`
4. **General Environment Variables**: `AGY_MODEL` / `AGY_TASK_MODEL`, `AGY_EFFORT` / `AGY_TASK_EFFORT`, `GEMINI_MODEL` / `GEMINI_DR_MODE`
5. **Built-in Defaults**: `flash 3.8` (high effort) and `Pro` (high thinking)

### Example Usage

```bash
# Explicit models and Korean aliases at initialization
python skills/agy-deep-research/scripts/deep_research.py init \
  --prompt "Analyze vector database indexing algorithms." \
  --goal "Compare HNSW, DiskANN, and IVF-PQ recall@10 and memory footprint." \
  --agy-model "플래시" \
  --agy-effort "높음" \
  --gemini-model "프로"

# Unattended end-to-end execution with custom effort
python skills/agy-deep-research/scripts/deep_research.py auto \
  --prompt "State of the art in sparse-dense retrieval." \
  --goal "Cover Lucene 10 BM25 quantization and SIMD block-max WAND." \
  --agy-model "flash 3.8" \
  --agy-effort "high" \
  --gemini-model "pro" \
  --out ./reports/retrieval.md
```

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

## Deterministic Quality & Security Gates

| Gate / Invariant | Enforced At | Check Details | Exit Code | Failure Code |
|---|---|---|---|---|
| **Job Isolation Gate** | All commands (`init` through `collect`) | Path must NOT be inside any `.git` repository tree | `5` | `GIT_REPO_ESCAPE`, `INVALID_JOB_DIR` |
| **Dual-File & XML Architecture Gate** | `init`, `plan`, `auto` | `prompt.md` and `goal.md` must NOT be identical; when `--strict-xml` is active, all 5 XML blocks (`<research_objective>`, `<context_and_scope>`, `<investigation_axes>`, `<source_governance>`, `<output_format>`) are required | `5` | `IDENTICAL_PROMPT_AND_GOAL`, `PROMPT_XML_STRUCTURE_INVALID` |
| **Imperative Edit Feedback Gate** | `edit` | `feedback_N.md` must use imperative delta verbs and must NOT contain conversational questions (`?`) unless `--allow-questions` is passed | `5` | `FEEDBACK_INTERROGATIVE_DENIED` |
| **Pre-Plan Gate** | `plan`, `edit`, `approve` | Both `prompt.md` and `goal.md` must exist and be non-empty | `2` | `PRE_PLAN_GATE_PROMPT_MISSING`, `PRE_PLAN_GATE_GOAL_MISSING` |
| **Plan Approval Gate** | `approve` | Status must be `plan_ready` or `plan_revised`; `--reason` length must be `>= 20` characters | `2` / `5` | `APPROVAL_GATE_INVALID_STATE` (`2`), `APPROVAL_GATE_REASON_TOO_SHORT` (`5`) |
| **Worker Liveness Check** | `approve`, `auto`, `collect` | Streaming loop monitors `pid_alive(pid)` while waiting for `relay/result.json` | `3` | `WORKER_DIED` |
| **Report Quality Gate** | `approve`, `auto`, `collect` | Extracted report must have `>= 400` characters and contain valid `#` section headings | `2` | `REPORT_QUALITY_GATE_FAILED`, `REPORT_STRUCTURE_INVALID` |

### Standard Exit Codes

- `0`: Success (action completed, all gates passed).
- `2`: Gate rejection or research failure (`PLAN_TIMEOUT`, `SAFETY_BLOCKED`, `PLAN_CHANGED`, `COLLECT_WOULD_START`, `REPORT_QUALITY_GATE_FAILED`).
- `3`: Runtime, driver, or environment error (`PLAYWRIGHT_MISSING`, `CDP_NOT_READY`, `ASAR_PATTERN_MISMATCH`, `WORKER_DIED`).
- `4`: Preflight or diagnostic failure (`PREFLIGHT_FAILED`, `SYNC_OUT_OF_DATE`).
- `5`: Bad parameters, validation failure, or security isolation violation (`GIT_REPO_ESCAPE`, `EMPTY_PROMPT`, `APPROVAL_GATE_REASON_TOO_SHORT`).

## Permissions & Confinement Model (`--deep-research`)

When `deep_research.py` invokes `agy_task.py run --deep-research -w <job_dir>`, `agy` runs with a `PreToolUse` hook (`confine_hook.py` with `AGY_CONFINE_MODE="deep-research"`):

- **Read tools**: Allowed inside `<job_dir>` and `AGY_CONFINE_DR_DIR`. Reads outside both are denied (`rule: "path"`).
- **Write tools**: Allowed only inside `<job_dir>`. Writes to `AGY_CONFINE_DR_DIR` are denied (`rule: "path"`). Caller control files inside `<job_dir>` (`relay/request.json`, `prompt.md`, `goal.md`, `feedback_*.md`, `relay/result.json`, `relay/started.json`) are protected (`rule: "protected"`).
- **Shell tool (`run_command`)**: Restricted to clean Python invocations of `relay_step.py --job <job_dir>` or `patch_gemini_app.py` with a strict Python flag whitelist (`-u`, `-B`, `-E`, `-s`, `-S`, `-O`, `-OO`). Shell chaining/redirection (`&&`, `||`, `;`, `|`, `>`, `<`, `` ` ``, `$()`, newlines) and arbitrary commands (`python -c`, `python -m`, etc.) are denied (`rule: "shell"`).
- **Browser & MCP tools**: Unconditionally denied (`rule: "tool"`).

## Running Tests

All unit tests run completely offline using deterministic simulator fakes (`fake_agy_dr.py`):

```bash
# Test agy-deep-research orchestrator, gates, streaming, and error recovery (11 tests)
python -m unittest discover -s skills/agy-deep-research/scripts/tests -v

# Test agy-delegate confinement hook including deep-research mode (51 tests)
python -m unittest discover -s skills/agy-delegate/scripts/tests -v
```

---
name: agy-deep-research
description: Use when the user asks to run Gemini Deep Research ("딥리서치", "Gemini Deep Research", "/local-gemini-deep-research"), perform exhaustive multi-source web research without API keys via the local Windows Gemini desktop app, or delegate headless web synthesis to the Antigravity CLI (`agy`).
---

# agy-deep-research

Orchestrates Google Gemini Deep Research headlessly through the Antigravity CLI (`agy`) and the locally installed Windows Gemini desktop app (`Gemini.exe`) without API keys.

Claude Code acts as the lead orchestrator owning the research plan review loop (`goal.md` vs. `plan_vN.md`), while `agy` operates headlessly inside an isolated job directory outside any `.git` repository (`%LOCALAPPDATA%\agy-deep-research\jobs\<id>` on Windows, `~/.cache/agy-deep-research/jobs/<id>` on POSIX, or `AGY_DR_JOBS`).

Commands below are relative to this skill's directory (`scripts/deep_research.py`).

## Overview

Automate long-form multi-source web research via the local Windows Gemini desktop app by combining:
1. **Dynamic ASAR Patching & WMI Hidden Launch**: Unlocks Chrome DevTools Protocol (CDP port 9223) in `Gemini.exe` and spawns a non-focus-stealing background process.
2. **Playwright CDP Driver with Countdown Interception**: Controls the web renderer, selects the Pro model, intercepts the automatic countdown fuse (`[data-test-id="cancel-fuse-button"]`) to prevent unreviewed execution, and extracts DOM to Markdown.
3. **Claude-Owned Review Loop & Deterministic Gates**: Requires pre-locking acceptance criteria (`goal.md`), enforces an explicit >=20-character approval reason, streams live thoughts from `progress.jsonl`, and validates report quality (>=400 chars, structured `#` headings).

## When to Use

- User asks for "딥리서치", "Gemini Deep Research", or `/local-gemini-deep-research`.
- User requests comprehensive, multi-source web research or literature review using their Google Gemini subscription without requiring an API key.
- User needs in-depth technical comparisons, market surveys, or academic synthesis requiring 5–15 minutes of autonomous web browsing and citation gathering.

### When NOT to Use

- For fast, single-turn factual lookups (use standard web search or direct model queries).
- When the local Gemini desktop app is not installed or the user has never logged into their Google account in the desktop app.
- For queries containing sensitive political or election keywords in Korean (e.g., `민주주의`, `선거`, `정치`, `대통령`), which trigger Gemini Web safety refusal filters.

## Architecture & Component Interactions

```
Claude Code (Lead Orchestrator & Plan Reviewer)
  │
  ├─ 1. Orchestration Layer (scripts/deep_research.py — stdlib only)
  │     • Subcommands: preflight, setup, init, plan, edit, approve, auto, status, collect
  │     • Enforces 5 deterministic quality, provenance, and isolation gates
  │
  ├─ 2. Operator Relay Layer (skills/agy-delegate / scripts/relay_step.py)
  │     • PreToolUse confinement hook (AGY_CONFINE_MODE="deep-research"):
  │       - Read: isolated job directory + skill directory only
  │       - Write: job directory only (caller control files protected)
  │       - Shell: whitelisted Python invocation of relay_step.py / patch_gemini_app.py
  │     • Translates relay/request.json into single-step driver execution
  │     • Spawns detached workers (mode: "detach") and streams progress to relay/progress.jsonl
  │
  ├─ 3. CDP Automation Layer (scripts/run_deep_research.py)
  │     • Connects over Playwright CDP to http://127.0.0.1:9223
  │     • Background throttling prevention: Page.setWebLifecycleState("active"), Emulation.setFocusEmulationEnabled
  │     • Pro model verification & Deep Research tool activation (multilingual EN/KO selectors)
  │     • Immediate countdown fuse cancellation ([data-test-id="cancel-fuse-button"])
  │     • Plan extraction, revision injection, thought monitoring, DOM-to-GFM Markdown conversion
  │
  └─ 4. Desktop Electron Launcher (scripts/patch_gemini_app.py)
        • Locates %LOCALAPPDATA%\Google\Gemini\app-*\resources\app.asar
        • Dynamic minification regex patching ((\w+)\.app.isPackaged) supporting app-1.16.x, app-1.17.x+
        • Byte-preserving replacement with SHA-256 header hash recalculation
        • Spawns hidden background process via WMI (Invoke-CimMethod Win32_Process.Create --hide)
```

### Modern Gemini Desktop App Automation Details

1. **ASAR Dynamic Minification Patching**:
   - Packaged Electron apps verify `app.isPackaged` and exit on unauthorized command-line switches.
   - `patch_gemini_app.py` backs up `app.asar` to `app.asar.bak`, dynamically matches the minified app identifier (e.g., `m.app` in 1.16.0, `h.app` in 1.17.0) via regex `var (\w+)=(\w+)\((\w+)\.app\.isPackaged...`, and injects `--remote-debugging-port=9223` and `--disable-renderer-backgrounding`.
   - Modifies the ASAR header JSON with the updated SHA-256 hash while keeping exact byte offsets intact.

2. **WMI Hidden Background Launch**:
   - `Gemini.exe --hide` is launched through WMI `Win32_Process.Create`.
   - The desktop app stays completely invisible in the background, preventing focus stealing or taskbar flicker during user work.

3. **Background Throttling Bypass**:
   - Chromium normally throttles timers and CPU in hidden windows.
   - The patched switch `--disable-renderer-backgrounding` combined with CDP commands `Page.setWebLifecycleState` (`active`) and `Emulation.setFocusEmulationEnabled` ensures continuous full-speed research execution.

4. **Port 9223 Unification**:
   - Port 9223 is the standardized CDP communication endpoint across all scripts, docstrings, and references, avoiding conflicts with default Chrome debugging sessions.
   - Configurable via the `GEMINI_CDP_PORT` environment variable.

## Quick Reference & CLI Commands

### Orchestrator Subcommands (`deep_research.py`)

| Subcommand | Key Options | Description |
|---|---|---|
| `preflight` | `[--target-dir DIR]` | Verify `agy`, `playwright`, Gemini app installation, CDP port 9223 liveness, and script synchronization. |
| `setup` | `[--target-dir DIR]` | Synchronize bundled scripts (`relay_step.py`, `run_deep_research.py`, `patch_gemini_app.py`) and `SKILL.md` to target skill. |
| `init` | `--prompt <text>` `--goal <text>` `[--out <path>]` | Create isolated job directory outside git tree; write `prompt.md` and `goal.md`. |
| `plan` | `--job <id_or_path>` `[--deadline <sec>]` `[--show]` | Execute `plan` relay step; intercept fuse; save `plan_v1.md`. |
| `edit` | `--job <id_or_path>` `--feedback <text>` | Submit plan refinement feedback (`edit` relay step); save `plan_vN.md`. |
| `approve` | `--job <id_or_path>` `--reason <text>` `[--no-wait]` | Enforce Plan Approval Gate (`>=20` chars); start detached research; stream progress; enforce Report Quality Gate; save report. |
| `auto` | `--prompt <text>` `--goal <text>` `[--out <path>]` `[--no-wait]` | Unattended end-to-end execution (`init` + `auto` relay step + live streaming + Quality Gate). |
| `status` | `--job <id_or_path>` | Check job state, detached worker PID liveness, and latest progress entry. |
| `collect` | `--job <id_or_path>` `[--out <path>]` `[--no-wait]` | Re-attach to detached worker or completed conversation; verify Quality Gate; save report. |

### Supporting Script CLIs

- **`patch_gemini_app.py`**:
  - `python scripts/patch_gemini_app.py --help` (exit 0)
  - `python scripts/patch_gemini_app.py [--port 9223] [--check] [--dry-run] [--show] [--restore]`
- **`relay_step.py`**:
  - `python scripts/relay_step.py --help` (exit 0)
  - `python scripts/relay_step.py --job <job_dir> [--dry-run]`
- **`run_deep_research.py`**:
  - `python scripts/run_deep_research.py --help` (exit 0)
  - `python scripts/run_deep_research.py --step {plan,edit,approve,auto} --prompt <text> [--output <path>] [--dry-run]`

## Step-by-Step Workflow Instructions

### Phase 1: Preflight & Environment Verification

Before running research, verify the local environment and script synchronization:

```bash
python scripts/deep_research.py preflight
```

- Verifies `agy` CLI availability, `playwright` installation, Gemini desktop app presence, and CDP port 9223.
- If `sync_status` is `out_of_sync` or `missing_target`, synchronize bundled assets to the user skill configuration:
  ```bash
  python scripts/deep_research.py setup
  ```
- If CDP is not ready, `patch_gemini_app.py` will be automatically invoked by the relay or can be tested manually:
  ```bash
  python scripts/patch_gemini_app.py --check
  ```

### Phase 2: Job Initialization & Isolation (`init`)

Pre-lock acceptance criteria and initialize an isolated job directory outside any `.git` repository:

```bash
python scripts/deep_research.py init \
  --prompt "Compare HNSW, DiskANN, and IVF-PQ for cloud-native vector search at 50M vectors." \
  --goal "1. Compare Recall@10, p99 latency, and DRAM/SSD footprint. 2. Cover streaming ingestion & recovery. 3. Cite primary benchmarks and papers." \
  --out ./reports/vector_indexing_report.md
```

- **Pre-Plan Gate**: Non-empty `--prompt` and `--goal` are required (exit code 2 if omitted).
- **Job Isolation Gate**: Job directory is created under `%LOCALAPPDATA%\agy-deep-research\jobs\<id>` (or `$AGY_DR_JOBS`). Any path inside a git repository is rejected with exit code 5 (`GIT_REPO_ESCAPE`).
- **Korean Safety Check**: Prompts are scanned for sensitive political/election keywords that cause safety refusal.

### Phase 3: Generate Research Plan & Intercept Countdown (`plan`)

Generate the initial research plan without allowing Gemini to start research unreviewed:

```bash
python scripts/deep_research.py plan --job <job_id>
```

- Spawns the confined relay runner (`agy_task.py run --deep-research`).
- Submits the prompt, detects the plan card (`[data-test-id="research-steps"]`), and immediately clicks `[data-test-id="cancel-fuse-button"]` to cancel the automatic timer fuse.
- Extracts all expandable steps and writes `<job_dir>/plan_v1.md`.
- Emits structured JSON with `status: "plan_ready"`, `plan`, `url`, and `plan_count`.

### Phase 4: Plan Review & Iterative Refinement (`edit`)

Review the generated plan against `<job_dir>/goal.md` following `prompts/plan_review_guide.md`:
1. Does the plan cover all required subtopics and metrics in `goal.md`?
2. Does it respect scope boundaries?
3. Will it gather primary sources and empirical benchmarks?

If gaps are found, submit targeted modification instructions:

```bash
python scripts/deep_research.py edit \
  --job <job_id> \
  --feedback "Add a dedicated section comparing IVF-PQ GPU memory footprint and roaring bitmap pre-filtering."
```

- Clicks `계획 수정` (`[data-test-id="edit-button"]`), submits feedback, and extracts `<job_dir>/plan_v2.md`.
- Updates job status to `plan_revised`. Repeat `edit` if necessary (typically 1–2 iterations).

### Phase 5: Approve Plan & Stream Progress (`approve`)

Once the plan satisfies all criteria in `goal.md`, approve the plan with an explicit justification:

```bash
python scripts/deep_research.py approve \
  --job <job_id> \
  --reason "The v2 plan comprehensively covers HNSW, DiskANN, IVF-PQ recall@10, latency, memory footprint, and streaming ingestion per goal.md." \
  --out ./reports/vector_indexing_report.md
```

- **Plan Approval Gate**: Validates that job status is `plan_ready` or `plan_revised`, `goal.md` exists, and `--reason` has at least 20 characters (exit 5 if too short).
- Clicks `연구 시작` (`[data-test-id="confirm-button"]`).
- Launches detached worker (`mode: "detach"`), records worker PID in `relay/started.json`.
- Streams real-time thoughts from `<job_dir>/relay/progress.jsonl` to `stderr` (`[progress <elapsed>s] <text>`) while verifying `pid_alive(pid)`.
- **Report Quality Gate**: Validates that completed Markdown report contains `>= 400` characters and valid `#` headings.
- Writes `<out>.md` and provenance metadata `<out>.md.meta.json`.

### Phase 6: Asynchronous Detached Execution & Re-attachment

For long-running research jobs (5–15 minutes), detach without blocking the session:

```bash
# 1. Approve with --no-wait
python scripts/deep_research.py approve --job <job_id> --reason "Plan meets all goal.md criteria." --no-wait

# 2. Check execution status periodically
python scripts/deep_research.py status --job <job_id>

# 3. Collect completed report when done
python scripts/deep_research.py collect --job <job_id> --out ./reports/report.md
```

- `collect` performs a read-only page probe first. If the research has not started yet, it refuses with `COLLECT_WOULD_START` to prevent unreviewed execution.

### Unattended End-to-End Mode (`auto`)

When the user explicitly instructs to run without interactive plan review:

```bash
python scripts/deep_research.py auto \
  --prompt "Analyze state-of-the-art sparse-dense hybrid retrieval in Lucene 10." \
  --goal "Cover block-max WAND, BM25 quantization, SIMD optimizations, and cite Apache Lucene JIRAs." \
  --out ./reports/hybrid_retrieval.md
```

## Deterministic Quality & Security Gates

| Gate / Invariant | Enforced At | Check Details | Exit Code | Failure Code |
|---|---|---|---|---|
| **Job Isolation Gate** | All commands (`init` through `collect`) | Path must NOT be inside any `.git` repository tree | `5` | `GIT_REPO_ESCAPE`, `INVALID_JOB_DIR` |
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

## Security & Confinement Model

When `deep_research.py` invokes `agy_task.py run --deep-research -w <job_dir>`, execution is guarded by the `PreToolUse` confinement hook (`AGY_CONFINE_MODE="deep-research"`):

1. **Path Isolation**:
   - **File Reads**: Permitted only within `<job_dir>` and `<dr_skill_dir>`. Reads elsewhere are blocked (`rule: "path"`).
   - **File Writes**: Permitted only within `<job_dir>`. Writes to `<dr_skill_dir>` or caller control files (`relay/request.json`, `prompt.md`, `goal.md`, `feedback_*.md`, `relay/result.json`, `relay/started.json`) are blocked (`rule: "protected"`).
2. **Command Whitelisting**:
   - Shell execution is restricted to clean Python invocations of `relay_step.py --job <job_dir>` or `patch_gemini_app.py` with standard Python flags (`-u`, `-B`, `-E`, `-s`, `-S`, `-O`, `-OO`).
   - Command chaining (`&&`, `||`, `;`, `|`), redirection (`>`, `<`), backticks, subshells, and arbitrary script executions (`python -c`, `python -m`) are strictly blocked (`rule: "shell"`).
3. **Tool Denial**:
   - Browser tools and external MCP tools are unconditionally denied (`rule: "tool"`).

## Troubleshooting & Common Pitfalls

| Symptom / Error | Root Cause | Resolution |
|---|---|---|
| `ASAR_PATTERN_MISMATCH` | Gemini desktop app updated and variable names changed | Ensure `patch_gemini_app.py` uses dynamic regex `(\w+)\.app` matching both 1.16.x and 1.17.x+. |
| `CDP_NOT_READY` / `CDP_CONNECT_FAILED` | Gemini desktop app not running or CDP port 9223 unbound | Run `python scripts/patch_gemini_app.py --check` or launch manually with `patch_gemini_app.py`. |
| `GEMINI_PAGE_NOT_FOUND` / `UI_SELECTOR_TIMEOUT` | User is not logged into Google account in the desktop app | Launch `Gemini.exe` visibly once with `python scripts/patch_gemini_app.py --show` and sign in. |
| `SAFETY_BLOCKED` | Prompt triggered Korean political/election content filter | Rephrase prompt removing sensitive terms (`민주주의`, `선거`, `정치`, `대통령`). |
| `GIT_REPO_ESCAPE` (Exit 5) | `--job` directory located inside a git repository | Use the default isolation path under `%LOCALAPPDATA%\agy-deep-research\jobs` outside git trees. |
| `APPROVAL_GATE_REASON_TOO_SHORT` (Exit 5) | `--reason` was omitted or shorter than 20 characters | Provide a detailed justification explaining how `plan_vN.md` addresses `goal.md` criteria. |
| Port conflict on CDP port | Default debugging port claimed by another browser or proxy | The skill defaults to port 9223. Ensure all configs use port 9223 or set `GEMINI_CDP_PORT`. |

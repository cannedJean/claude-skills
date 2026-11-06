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
  │     • Pins & normalizes models: --agy-model, --agy-effort, --gemini-model (default: flash 3.8 high, Pro high)
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
| `compose-prompt` | `--objective <text>` `[--goal <text>]` `[--context-scope <text>]` `[--axes <item>...]` `[--workspace-sources <text>]` `[--entities <e>...]` `[--metrics <m>...]` `[--prompt-out <path>]` `[--goal-out <path>]` | Generate a gate-safe 5-block XML `prompt.md` and distinct `goal.md` review rubric. |
| `init` | `--prompt <text>` `--goal <text>` `[--compose-xml]` `[--strict-xml]` `[--agy-model <m>]` `[--agy-effort <e>]` `[--gemini-model <m>]` `[--out <path>]` | Create isolated job directory outside git tree; validate & write `prompt.md` and `goal.md` (rejects identical text via `IDENTICAL_PROMPT_AND_GOAL`); lock model settings. |
| `plan` | `--job <id_or_path>` `[--strict-xml]` `[--agy-model <m>]` `[--agy-effort <e>]` `[--gemini-model <m>]` `[--deadline <sec>]` `[--show]` | Execute `plan` relay step (default deadline 420s; auto-extended to 600s for Argon); intercept fuse; save `plan_v1.md`. |
| `edit` | `--job <id_or_path>` `--feedback <text>` `[--agy-model <m>]` `[--agy-effort <e>]` `[--gemini-model <m>]` | Validate imperative delta feedback (rejects questions via `FEEDBACK_INTERROGATIVE_DENIED`); submit `edit` relay step; save `plan_vN.md`. |
| `approve` | `--job <id_or_path>` `--reason <text>` `[--agy-model <m>]` `[--agy-effort <e>]` `[--gemini-model <m>]` `[--out <path>]` `[--no-wait]` `[--timeout <sec>]` | Enforce Plan Approval Gate (`>=20` chars); start detached research (default timeout 1200s; auto-extended to 1800s for Argon); stream progress; enforce Report Quality Gate; save report. |
| `auto` | `--prompt <text>` `--goal <text>` `[--compose-xml]` `[--agy-model <m>]` `[--agy-effort <e>]` `[--gemini-model <m>]` `[--out <path>]` `[--no-wait]` | Unattended end-to-end execution (`init` + `auto` relay step + live streaming + Quality Gate). |
| `status` | `--job <id_or_path>` | Check job state, detached worker PID liveness, and latest progress entry. |
| `collect` | `--job <id_or_path>` `[--agy-model <m>]` `[--agy-effort <e>]` `[--gemini-model <m>]` `[--out <path>]` `[--no-wait]` | Re-attach to detached worker or completed conversation; verify Quality Gate; save report. |

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

## Model & Reasoning Effort Configuration

Claude Code can explicitly pin the Antigravity model, Antigravity reasoning effort level, and Google Gemini desktop application model when initiating or executing deep research.

### Default Behavior

When model and effort flags are omitted (and corresponding environment variables are unset), `deep_research.py` automatically applies optimal defaults:
- **Antigravity CLI Model (`--agy-model`)**: `flash 3.8`
- **Antigravity Reasoning Effort (`--agy-effort`)**: `high`
- **Google Gemini Desktop Model (`--gemini-model`)**: `Pro` with thinking level `높음` (`high`)

These default settings ensure balanced, high-depth synthesis with thorough chain-of-thought analysis in both Antigravity orchestration and the Gemini desktop application.

### CLI Option Parameters

The following options are available across `init`, `plan`, `edit`, `approve`, `auto`, and `collect`:

| Option | Argument | Description | Default |
|---|---|---|---|
| `--agy-model` | `MODEL_SLUG` | Antigravity CLI model slug passed to `agy_task.py --model`. Accepts model names, friendly aliases, or custom slugs. | `flash 3.8` |
| `--agy-effort` | `EFFORT` | Antigravity reasoning effort level passed to `agy_task.py ... --effort`. Choices: `low`, `medium`, `high`. | `high` |
| `--gemini-model` | `MODEL_MODE` | Google Gemini desktop app mode and thinking level passed via `GEMINI_DR_MODE` / `GEMINI_DR_THINKING`. | `Pro` (`high` thinking) |

### Friendly Aliases (English & Korean)

Input values are automatically normalized to canonical slugs and settings. Both English and Korean aliases are supported:

#### Antigravity Model (`--agy-model`)
| Canonical Model | Recognized Aliases |
|---|---|
| `flash 3.8` | `flash`, `flash 3.8`, `flash-3.8`, `flash3.8`, `3.8 flash`, `3.8-flash`, `플래시` |
| `pro` | `pro`, `pro 3.8`, `pro-3.8`, `pro3.8`, `3.8 pro`, `프로` |
| *Custom Slugs* | Any custom slug (e.g., `gemini-2.5-flash`, `claude-3-5-sonnet`) is preserved verbatim. |

#### Antigravity Reasoning Effort (`--agy-effort`)
| Canonical Effort | Recognized Aliases |
|---|---|
| `high` | `high`, `높음`, `h`, `최대`, `hi` |
| `medium` | `medium`, `중간`, `보통`, `m`, `med`, `mid` |
| `low` | `low`, `낮음`, `l`, `lo` |

*Note*: Any invalid effort value exits with status code `5` (`INVALID_EFFORT`).

#### Google Gemini Desktop Model (`--gemini-model`)
| Mode / Thinking | Recognized Aliases |
|---|---|
| `("Pro", "high")` | `pro`, `3.8 pro`, `pro 3.8`, `high`, `높음`, `pro high`, `pro-high`, `pro (높음)`, `프로`, `advanced` |
| `("Flash", "high")` | `flash`, `flash 3.8`, `flash-3.8`, `flash3.8`, `3.8 flash`, `플래시` |
| `("Argon", "high")` | `argon`, `아르곤` |
| *Custom Mode* | Any other string preserves the specified mode name with `high` thinking level by default. |

### Environment Variables & Resolution Precedence

Settings can also be configured via environment variables. The configuration resolution precedence is strictly ordered from highest to lowest:

1. **Command-Line Arguments**: `--agy-model`, `--agy-effort`, `--gemini-model` passed directly to the subcommand.
2. **Job State (`job.json`)**: Previously resolved settings persisted in the active job directory (inherited across subsequent subcommands).
3. **Specific Environment Variables**:
   - `AGY_DR_MODEL`: Specific Antigravity model for deep research.
   - `AGY_DR_EFFORT`: Specific Antigravity reasoning effort for deep research.
   - `GEMINI_DR_MODEL`: Specific Gemini desktop model mode.
   - `GEMINI_DR_THINKING`: Explicit thinking level override (`high`, `medium`, `low`).
4. **General Environment Variables**:
   - `AGY_MODEL` or `AGY_TASK_MODEL`: Fallback Antigravity model.
   - `AGY_EFFORT` or `AGY_TASK_EFFORT`: Fallback Antigravity effort.
   - `GEMINI_MODEL` or `GEMINI_DR_MODE`: Fallback Gemini desktop model mode.
5. **Built-in Defaults**: `agy_model="flash 3.8"`, `agy_effort="high"`, `gemini_model="Pro"`, `gemini_thinking="high"`.

### Execution Chain Propagation & Provenance

When model options are resolved:
1. **Antigravity CLI (`agy_task.py`)**: `deep_research.py` invokes `agy_task.py --model <agy_model> run --effort <agy_effort> --deep-research ...`.
2. **Relay Protocol (`relay/request.json`)**: Injects `"gemini_model"`, `"gemini_thinking"`, and `"env": {"GEMINI_DR_MODE": ..., "GEMINI_DR_THINKING": ...}` into the step request.
3. **Desktop Driver (`run_deep_research.py`)**: `ensure_pro_mode` reads the environment variables and selects the target model and thinking controls in the Gemini desktop app UI.
4. **Metadata & Provenance (`.meta.json`)**: On completion, resolved models and effort levels are stored in `<job_dir>/job.json` and recorded in the report metadata deliverable `<out>.md.meta.json` under `"agy_model"`, `"agy_effort"`, `"gemini_model"`, and `"gemini_thinking"`.

### Concrete CLI Command Examples

#### 1. Initializing Research with Custom Models and Korean Aliases
```bash
python scripts/deep_research.py init \
  --prompt "Evaluate vector databases for 100M embeddings." \
  --goal "Compare throughput, p99 latency, and DRAM/SSD footprint." \
  --agy-model "플래시" \
  --agy-effort "높음" \
  --gemini-model "프로" \
  --out ./reports/vector_db_report.md
```

#### 2. Overriding Effort During Plan Review and Revision
```bash
# Generate plan with high effort
python scripts/deep_research.py plan --job <job_id> --agy-effort "high"

# Revise plan with updated instructions and model
python scripts/deep_research.py edit \
  --job <job_id> \
  --feedback "Add GPU memory footprint comparison for IVF-PQ." \
  --agy-model "pro" \
  --agy-effort "medium"
```

#### 3. Approving Research Plan with Explicit Gemini Model
```bash
python scripts/deep_research.py approve \
  --job <job_id> \
  --reason "The plan thoroughly addresses all evaluation criteria in goal.md including throughput and memory footprint." \
  --gemini-model "pro"
```

#### 4. Unattended End-to-End Execution (`auto`) with Pinned Models
```bash
python scripts/deep_research.py auto \
  --prompt "Analyze state-of-the-art sparse-dense hybrid retrieval in Lucene 10." \
  --goal "Cover block-max WAND, BM25 quantization, and cite primary benchmarks." \
  --agy-model "flash 3.8" \
  --agy-effort "high" \
  --gemini-model "pro" \
  --out ./reports/hybrid_retrieval.md
```

#### 5. Session-wide Configuration via Environment Variables
```bash
# Set environment variables for the shell session
export AGY_MODEL="flash 3.8"
export AGY_EFFORT="high"
export GEMINI_MODEL="pro"

# Commands automatically adopt the exported configuration
python scripts/deep_research.py init --prompt "..." --goal "..."
```

## Prompt Engineering Architecture & Dual-File Contract (`prompt.md` vs. `goal.md`)

In headless Deep Research orchestration, the prompt is a **system control specification** that must simultaneously satisfy the post-trained Gemini Deep Research planning loop, DOM countdown fuse interception (`[data-test-id="cancel-fuse-button"]`), confinement boundaries (`confine_hook.py`), and deterministic quality gates.

### 1. Dual-File Separation Principle (`IDENTICAL_PROMPT_AND_GOAL` Prevention)

**Never copy the same text into `--prompt` (`prompt.md`) and `--goal` (`goal.md`).** `deep_research.py` enforces that `prompt.md` and `goal.md` are distinct and rejects identical inputs with exit code `5` (`IDENTICAL_PROMPT_AND_GOAL`).

| File | Consumer | Role & Required Content | Gate / Failure Prevented |
|---|---|---|---|
| `prompt.md` (`--prompt`) | `Gemini.exe` Deep Research Agent | **Execution Specification**: 5-block XML structure (`<research_objective>`, `<context_and_scope>`, `<investigation_axes>`, `<source_governance>`, `<output_format>`) guiding plan generation, breadth-to-depth web/Workspace search, and GFM report synthesis. | `PLAN_TIMEOUT`, `SAFETY_BLOCKED`, `REPORT_QUALITY_GATE_FAILED`, `REPORT_STRUCTURE_INVALID` |
| `goal.md` (`--goal`) | Caller Orchestrator (Claude) | **Fixed Acceptance Criteria**: Checklist of mandatory algorithms/entities, quantitative metrics (e.g., `Recall@10`, `p99 latency`, `DRAM/SSD footprint`), scope exclusions, and Tier-1 source verification used to review `plan_v1.md` before `edit` or `approve`. | `PRE_PLAN_GATE_GOAL_MISSING`, premature approval of shallow plans (wasting 35% Argon quota) |
| `feedback_N.md` (`--feedback`) | `Gemini.exe` Plan Editor (`edit`) | **Imperative Delta Instructions**: Action verbs only (`Add`, `Remove`, `Reorder`, `Pin`) specifying exact changes to `plan_vN.md`. Never ask conversational questions (`?`), which are rejected with `FEEDBACK_INTERROGATIVE_DENIED` (exit `5`). | `PLAN_CHANGED`, `PLAN_TIMEOUT`, `FEEDBACK_INTERROGATIVE_DENIED` |
| `approval.md` (`--reason`) | `deep_research.py approve` Gate | **Approval Justification**: `>= 20` characters mapping `plan_vN.md` items to `goal.md` criteria. | `APPROVAL_GATE_REASON_TOO_SHORT` (exit `5`) |

### 2. Required 5-Block XML Specification for `prompt.md`

Always structure `prompt.md` using the 5 XML blocks below (or generate it automatically via `python scripts/deep_research.py compose-prompt` or `init --compose-xml` / `--strict-xml`):

```xml
<research_objective>
Compare, analyze, and synthesize [multi-dimensional technical topic] across [target architectures/systems] to produce an evidence-based architectural decision report.
</research_objective>

<context_and_scope>
- Self-Contained Context: [Inline any local code bottlenecks, schemas, or constraints here — never reference local repo paths like ./src/... because confine_hook.py blocks outside reads].
- Google Workspace Data Layer (if applicable): [Specify exact Google Drive folder/document titles, Gmail thread queries, or Google Chat spaces — Gemini.exe inherits the logged-in account's Google Workspace permission graph natively without local file uploads].
- Temporal & Domain Scope: [e.g., 2022–2026 production systems and peer-reviewed literature].
- Exclusions: [Skip textbook basics and marketing overviews; do not ask clarifying questions].
</context_and_scope>

<investigation_axes>
1. [Axis 1 with High-Entropy Technical Seeds: e.g., HNSW, DiskANN Vamana graph, IVF-PQ, Lucene 10 BM25 quantization, SIMD block-max WAND].
2. [Axis 2 with Quantitative Metrics: e.g., Recall@10, p99 latency, DRAM vs. NVMe SSD footprint, build throughput].
3. [Axis 3 with Operational Edge Cases: e.g., tombstone compaction, streaming ingestion, fault recovery].
Conditional Depth-First Trigger: When primary sources or benchmarks report conflicting quantitative metrics, trace the underlying hardware specifications, dataset distributions, and measurement methodologies in the primary text to explain the discrepancy.
</investigation_axes>

<source_governance>
- Prioritize Tier-1 primary sources: peer-reviewed papers (with DOI/arXiv links), official engineering specifications, and reproducible benchmarks.
- Do not use unverified secondary blogs or vendor marketing pages as sole evidence.
- Provide numbered inline citations with verification/publication dates and URLs.
- Epistemic Calibration: Strictly separate confirmed empirical facts from hypotheses. If verified public evidence is unavailable for any metric, explicitly state '검증 가능한 공개 근거 불충분 (Insufficient verified evidence)' rather than fabricating or extrapolating numbers.
</source_governance>

<output_format>
- Format: GitHub-Flavored Markdown (GFM) report of at least 2,500 characters (strictly >= 400 characters to pass the Report Quality Gate).
- Mandatory Markdown Heading Tree:
  # [Report Title]
  ## 1. Executive Summary & Architectural Verdict
  ## 2. Deep-Dive Technical Analysis
  ## 3. Quantitative Comparison Matrix (Markdown Table with fixed columns)
  ## 4. Risk Analysis & Production Recommendations
  ## 참고 출처 (Sources)
- Downstream Structured Data Rule: Never output raw/pure JSON without `#` Markdown headings (causes `REPORT_STRUCTURE_INVALID`). If JSON is required, embed a fenced ```json code block inside a dedicated `##` section.
</output_format>
```

### 3. Model Tiering Strategy & Argon Operational Rules (`Pro` vs. `Flash` vs. `Argon`)

- **Antigravity Orchestrator (`--agy-model "flash 3.8" --agy-effort "high"`)**: Keep pinned to `flash 3.8` (`high`). Because `confine_hook.py` restricts `agy` to running `relay_step.py`, switching `--agy-model` to `pro` adds relay startup latency without improving research quality.
- **Gemini Desktop Engine (`--gemini-model`)**:
  - `flash` (`("Flash", "high")`): Fast broad scoping, low quota usage. Decompose sub-steps explicitly in `<investigation_axes>`.
  - `pro` (`("Pro", "high")`, default): Standard 5–10 min literature reviews and technical comparisons.
  - `argon` (`("Argon", "high")`, **Gemini 4 Argon**): 1M context window, frontier reasoning & mathematical/legal/code verification, strongest defense against indirect prompt injection, and trained to report negative findings honestly.
    - **Quota Rule**: Single Argon Deep Research run consumes **~35% of AI Pro usage** (2% weekly limit). **Never run Argon in unattended `auto` mode by default**—always run the supervised `init` → `plan` → `edit` → `approve` loop so you verify `plan_vN.md` against `goal.md` before spending quota.
    - **Timeout Rule**: Argon generates ratusan-thousand-token reasoning trajectories; `deep_research.py` automatically extends `plan`/`edit` `--deadline` to `600s` (from `420s`) and `approve`/`collect` `--timeout` to `1800s` (from `1200s`), or use `approve --no-wait` + `status` + `collect`.

### 4. Rationalization Counter-Table & Red Flags

| Common Agent Excuse / Rationalization | Architectural Reality & Gate Consequence |
|---|---|
| *"I'll pass the same text to `--prompt` and `--goal` to save time."* | Violates dual-file separation; immediately rejected with `IDENTICAL_PROMPT_AND_GOAL` (exit `5`). |
| *"I'll tell Gemini to read `./src/storage/index.rs` directly."* | `confine_hook.py` blocks reads outside `<job_dir>` (`rule: "path"`) and `Gemini.exe` has no local repo access. Inline the code summary into `<context_and_scope>`. |
| *"Gemini Deep Research can't access internal Google Drive or Gmail."* | **False.** `Gemini.exe` natively inherits the signed-in user's Google Workspace permission graph (Drive, Gmail, Chat). Specify exact Drive folder/doc names or Gmail thread queries in `<context_and_scope>`. |
| *"Gemini 4 Argon (`--gemini-model argon`) isn't supported in Deep Research."* | **False.** `run_deep_research.py` (`ensure_pro_mode`) and `deep_research.py` natively support `Pro`, `Flash`, and `Argon` (`--gemini-model argon` / `아르곤`). |
| *"The downstream script needs JSON, so I'll ask for a pure JSON array only."* | Fails `enforce_report_quality_gate` with `REPORT_STRUCTURE_INVALID` (exit `2`) due to missing `#` headings. Require a full `#` Markdown report with an embedded ```` ```json ```` block under a `##` section. |
| *"In `edit --feedback`, I'll ask Gemini why it chose DiskANN."* | Asking questions (`?`) causes Gemini to reply with prose instead of a revised plan card (`PLAN_CHANGED` / `PLAN_TIMEOUT`). Rejected by `FEEDBACK_INTERROGATIVE_DENIED` (exit `5`). Use imperative delta verbs (`Add`, `Remove`, `Reorder`, `Pin`). |

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
  --agy-model "flash 3.8" \
  --agy-effort "high" \
  --gemini-model "pro" \
  --out ./reports/vector_indexing_report.md
```

- **Pre-Plan Gate**: Non-empty `--prompt` and `--goal` are required (exit code 2 if omitted).
- **Model & Effort Defaults**: When `--agy-model`, `--agy-effort`, or `--gemini-model` are omitted, `deep_research.py` automatically initializes with defaults: Antigravity model `flash 3.8` (effort `high`), and Gemini desktop model `Pro` (thinking `높음`/`high`). Friendly aliases (e.g. `플래시`, `높음`, `프로`) are recognized. Resolved settings persist in `job.json` and are inherited by downstream steps unless overridden.
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
- Writes `<out>.md` and provenance metadata `<out>.md.meta.json` (capturing report statistics and resolved `agy_model`, `agy_effort`, `gemini_model`, and `gemini_thinking`).

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
  --agy-model "flash 3.8" \
  --agy-effort "high" \
  --gemini-model "pro" \
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

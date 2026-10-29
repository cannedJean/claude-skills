---
name: agy-deep-research
description: Run exhaustive web research via Google Gemini Deep Research through the Antigravity CLI (`agy`) and the locally installed Windows Gemini desktop app (`Gemini.exe`) without API keys. Claude Code owns the research plan review loop (`init` → `plan` → `edit`* → `approve --reason`) by default, streams live progress from `<job>/relay/progress.jsonl`, and enforces deterministic quality and provenance gates on the final Markdown report. Use when the user asks for "딥리서치", "Gemini Deep Research", "/local-gemini-deep-research", or wants deep multi-source web synthesis delegated to `agy`.
---

# agy-deep-research

Orchestrates Google Gemini Deep Research headlessly through `agy-delegate --deep-research` and `relay_step.py`.
Claude Code stays in the driver's seat for reviewing and refining the research plan (`goal.md` vs. `plan_vN.md`),
while `agy` runs confined inside an isolated job directory outside any `.git` repository (`%LOCALAPPDATA%\agy-deep-research\jobs\<id>` on Windows, `~/.cache/agy-deep-research/jobs/<id>` on POSIX, or `AGY_DR_JOBS`).

Commands below are relative to this skill's directory (`scripts/deep_research.py`).

## Architecture

```
Claude Code (Lead Orchestrator & Plan Reviewer)
  │
  ├─ 1. init / plan / edit / approve / auto / status / collect
  │     (scripts/deep_research.py — stdlib only, deterministic gates)
  │
  ├─ 2. Headless relay step invocation
  │     (skills/agy-delegate/scripts/agy_task.py run --deep-research -w <job_dir>)
  │       └─ PreToolUse confinement hook (AGY_CONFINE_MODE="deep-research"):
  │            • Read allowed only in <job_dir> + <dr_skill_dir>
  │            • Write allowed only in <job_dir> (caller control files protected)
  │            • Shell restricted to clean Python execution of relay_step.py / patch_gemini_app.py
  │
  └─ 3. Detached worker + live progress tailing
        • approve / auto starts relay_step.py in mode="detach" → writes relay/started.json (PID)
        • deep_research.py tails <job_dir>/relay/progress.jsonl in real time to stderr
        • Monitors pid_alive(pid) until <job_dir>/relay/result.json arrives
        • Verifies Report Quality Gate (>= 400 chars, valid Markdown headings)
        • Writes <out>.md and provenance metadata <out>.md.meta.json
```

## Workflow (Claude-Owned Plan Review Loop — Default)

1. **Preflight & Skill Sync** (first run, or after updating this skill)
   ```bash
   python scripts/deep_research.py preflight
   ```
   - If `sync_status` is `missing_target` or `out_of_sync`, synchronize bundled scripts (`relay_step.py`, `run_deep_research.py`, `patch_gemini_app.py`) and `SKILL.md` into `~/.gemini/config/skills/local-gemini-deep-research`:
     ```bash
     python scripts/deep_research.py setup
     ```
   - If `checks.gemini_app.cdp_alive` is `false`, `relay_step.py` / `run_deep_research.py` will automatically invoke `patch_gemini_app.py` to launch `Gemini.exe` with CDP enabled on port 9223.

2. **Initialize Job & Lock Review Criteria (`init`)**
   Before generating any research plan, write the exact research prompt (`prompt.md`) and fixed acceptance criteria (`goal.md`) into an isolated job directory outside any git repository:
   ```bash
   python scripts/deep_research.py init \
     --prompt "Compare HNSW, DiskANN, and IVF-PQ for cloud-native vector search at 50M vectors." \
     --goal "1. Compare Recall@10, p99 latency, and DRAM/SSD footprint. 2. Cover streaming ingestion & recovery. 3. Cite primary benchmarks/papers." \
     --out ./reports/vector_indexing_report.md
   ```
   - **Pre-Plan Gate**: Both `--prompt` (or `--prompt-file`) and `--goal` (or `--goal-file`) must be non-empty.
   - **Korean Safety Filter Check**: Warns on sensitive Korean political/election keywords (`민주주의`, `선거`, `정치`, `대통령`, `정당`, `투표`) that can trigger Gemini Web `SAFETY_BLOCKED` refusals.

3. **Generate Initial Research Plan (`plan`)**
   ```bash
   python scripts/deep_research.py plan --job <job_id>
   ```
   - Runs `agy_task.py run --deep-research` for `step: "plan"`.
   - Saves `<job_dir>/plan_v1.md` and prints JSON with `status: "plan_ready"`, `plan`, `url`, and `plan_count`.

4. **Review & Optionally Refine the Plan (`edit`)**
   Compare the returned `plan` against `<job_dir>/goal.md` (see `prompts/plan_review_guide.md`).
   If any required sub-question, metric, or constraint from `goal.md` is missing, send targeted feedback:
   ```bash
   python scripts/deep_research.py edit \
     --job <job_id> \
     --feedback "Add a dedicated comparison of IVF-PQ GPU memory usage and roaring bitmap pre-filtering."
   ```
   - Saves `feedback_2.md` and `plan_v2.md`, updating status to `plan_revised`. Repeat `edit` if needed (typically 1–2 rounds).

5. **Approve Plan, Stream Progress & Collect Report (`approve`)**
   Once the plan satisfies `goal.md`, approve it with an explicit `--reason` (`>= 20` characters):
   ```bash
   python scripts/deep_research.py approve \
     --job <job_id> \
     --reason "The v2 plan covers HNSW, DiskANN, IVF-PQ, recall@10, p99 latency, ingestion, and primary citations per goal.md." \
     --out ./reports/vector_indexing_report.md
   ```
   - **Plan Approval Gate**: Verifies `goal.md` exists, status is `plan_ready` or `plan_revised`, and `len(reason.strip()) >= 20`. Passes `expected_url` and `expected_plan_count` to `relay_step.py` so `PLAN_CHANGED` is caught if the browser conversation drifted.
   - **Hybrid Progress Streaming**: Starts `relay_step.py` in `mode: "detach"` (`relay/started.json`), then tails `<job_dir>/relay/progress.jsonl` in real time (`[progress <elapsed>s] <text>` on `stderr`) while checking `pid_alive(pid)` until `relay/result.json` arrives.
   - **Report Quality Gate**: Verifies the completed Markdown report has `>= 400` characters and valid Markdown section headings (`# ...`), then writes `<out>.md` and `<out>.md.meta.json`.

## Asynchronous / Detached & Auto Modes

- **Detached Launch (`--no-wait`) + Re-attach (`status` / `collect`)**:
  ```bash
  python scripts/deep_research.py approve --job <job_id> --reason "Covers all metrics in goal.md." --no-wait
  python scripts/deep_research.py status --job <job_id>
  python scripts/deep_research.py collect --job <job_id> --out ./reports/report.md
  ```
- **Unattended End-to-End Mode (`auto`)**:
  When the user explicitly asks to skip interactive plan review:
  ```bash
  python scripts/deep_research.py auto \
    --prompt "..." \
    --goal "..." \
    --out ./reports/report.md
  ```

## Deterministic Gates & Exit Codes

| Gate / Check | Enforced When | Failure Code | Exit Code |
|---|---|---|---|
| **Job Isolation Gate** | Every job operation (`init`, `plan`, `edit`, `approve`, `auto`, `status`, `collect`) | `GIT_REPO_ESCAPE` / `INVALID_JOB_DIR` | `5` |
| **Pre-Plan Gate** | `plan`, `edit`, `approve` | `PRE_PLAN_GATE_PROMPT_MISSING` / `PRE_PLAN_GATE_GOAL_MISSING` | `2` |
| **Plan Approval Gate** | `approve` (state check / reason length `< 20` chars) | `APPROVAL_GATE_INVALID_STATE` (`2`) / `APPROVAL_GATE_REASON_TOO_SHORT` (`5`) | `2` / `5` |
| **Report Quality Gate** | `approve`, `auto`, `collect` (`< 400` chars or no `#` heading) | `REPORT_QUALITY_GATE_FAILED` / `REPORT_STRUCTURE_INVALID` | `2` |
| **Worker Liveness Check** | Streaming loop (`approve`, `auto`, `collect`) | `WORKER_DIED` | `3` |

Exit codes: `0` success, `2` research or gate failure (`PLAN_TIMEOUT`, `SAFETY_BLOCKED`, `PLAN_CHANGED`, `COLLECT_WOULD_START`, `REPORT_QUALITY_GATE_FAILED`), `3` runtime/driver/worker error, `4` preflight failure, `5` bad arguments or git repo isolation violation.

## Rules

- **Never put `--job` inside a `.git` repository.** `agy` walks up to the enclosing `.git` root and treats the entire repo as its workspace. `deep_research.py` refuses any job path inside a `.git` tree with exit code `5` (`GIT_REPO_ESCAPE`).
- **Always define `goal.md` before `plan`.** Fixing acceptance criteria before seeing Gemini's plan prevents confirmation bias during plan review.
- **Preserve the user's prompt verbatim** (`--raw-prompt` is on by default) unless the user asks you to draft or expand it.
- **Inspect `<out>.md` and `<out>.md.meta.json`** after completion and report `report_chars`, `sources_count`, `plan_iterations`, and key findings to the user.

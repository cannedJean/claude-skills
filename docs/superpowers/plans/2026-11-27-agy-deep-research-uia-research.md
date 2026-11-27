# agy-deep-research UIA research steps (S7/S8) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement `approve`, the long research wait, `collect` and `auto` on the UIA backend, plus `report_md.py` (report tree → GFM Markdown), using the 2026-11-27 live measurements (spec §23). After this plan every driver step runs on UIA; only the G4 end-to-end live run remains.

**Architecture:** Same as plan 2: pure logic in `steps.py` / `report_md.py` over `UiTree` (FakeTree in tests), wired into `run_deep_research.py`'s step dispatch table. No real app in any task.

**Spec:** `docs/superpowers/specs/2026-11-26-agy-deep-research-uia-design.md` — §11-9/10/12, §21.3 and §23 take precedence over §19.

## Global Constraints
- Everything in plans 1–2 still binds: `import sandbox` first in every test file; fakes only; never run Gemini, a browser, a patcher, `--remote-debugging-port`, agy, or the drivers as real processes; nothing written under `%LOCALAPPDATA%\Google\Gemini\`; every output through `privacy.scrub`; local commits only.
- **No automatic refresh except §21.3/§23.2**: at most one §11-12 recovery per wait, only after `stall_s` (default 3600 s) without change and only when the job's return link is visible now; otherwise fail closed (`RESEARCH_STALLED` / `RECOVERY_UNAVAILABLE`).
- Result JSON of approve/collect/auto carries `switches` (allowed switches present on the running browser command line), `longest_no_change_s`, `recoveries`, `host_mode`, and `research_completion_verified: false` when `host_mode == "D1"`.
- Relay compatibility strings stay byte-identical: `Clicking '연구 시작'`, `[Ns] Progress: <text>` (relay `PROGRESS_RE`), `[REPORT_PROVENANCE] ` immediately before `=== DEEP RESEARCH REPORT START ===` … END.
- Labels ko and en for every new locator (spec §19: same account mixes languages; the completion line was English on a ko account).

## Review Focus
1. Approve must refuse to invoke `plan_start` on a card other than the newest enabled one, and never twice (Task R1).
2. The wait must never refresh while research progress changes, never refresh when the return link is not visible, and refresh at most once (Task R1).
3. `report_md` must not include chat turns (plan card, prompt) and must keep table cell text from leaf DataItem names (Task R2).
4. The committed S8 fixture contains zero conversation URLs and zero account strings — checked by a test, not by eye (Task R2).
5. collect prints `[REPORT_PROVENANCE]` only for the bound job conversation, as the last line before the report block (Task R3).

---

### Task R1: research locators, approve and the long wait (`steps.py`, `locators.json`)
**Files:** Modify `scripts/steps.py`, `scripts/locators.json`; Test `scripts/tests/test_steps_research.py`; fixtures `scripts/tests/fixtures/research_flow_ko.json` (+ `_en_synthetic`).
**Interfaces:** new locators `research_status` (status texts in the research turn/panel), `research_thought_title`, `report_done_phrase` (ko+en, e.g. `I've completed your research.` / `연구를 완료했습니다`), `report_share_export`, `report_toc`, `report_sources_used`, `report_panel` (scope of the report region). `approve(tree, locs, cfg, url) -> ApproveResult` (bind → newest enabled `plan_start` Invoke → progress signal within `approve_verify_s` → else `RESEARCH_NOT_STARTED`); `wait_research(tree, locs, cfg, url, on_progress, recover) -> WaitResult(complete, longest_no_change_s, recoveries, last_status)`; completion = no `stop_response` in the last turn AND done phrase AND ≥1 of share/toc/sources, held 20 s (spec §23.3); progress signature = (node count, status texts, thought titles, text length); `recover` is injected (§11-12 with the visible-link precheck) and called at most once.
- [ ] Tests (FakeTree timelines built from spike live_monitor_research.json shape): start → progress → complete; stall below `stall_s` → no recovery; stall ≥ `stall_s` with link visible → exactly one recovery then complete; stall with link absent → RECOVERY_UNAVAILABLE without refresh; second stall after recovery → RESEARCH_STALLED; approve on a stale card refused; approve twice refused; progress lines emitted via callback in relay format.
- [ ] Implement; full suite + run_verification; commit `feat(agy-deep-research): approve and long research wait on UIA`.

### Task R2: `report_md.py` and the S8 fixture
**Files:** Create `scripts/report_md.py`; Test `scripts/tests/test_report_md.py`; fixture `scripts/tests/fixtures/report_sql_ko.json` built from `C:\Users\SSAFY\w\spike\live_s8.json` (read only; outside the repo).
**Interfaces:** `to_markdown(report_root: Node, allowed) -> str`; `report_stats(md) -> {chars, headings, tables, links}`; `structural_check(md, stats) -> list[str]` (problems: no heading, empty table cells, no links, …; spec §21.1 "구조 검사로 대체").
- Rules (spec §23.3): scope = report panel only; consecutive Text runs inside one block joined without separator; headings by HeadingLevel; grid → GFM table with leaf DataItem Name as cell text; only http(s) Hyperlinks become links; buttons/controls skipped.
- Fixture: convert the dump to the FakeTree fixture format, run it through `privacy.scrub`, replace every Gemini conversation URL with `https://gemini.google.com/app/<test>`; a test asserts the committed file has 0 matches for conversation URLs (`/app/[0-9a-f]{12,}`), e-mails and `Google 계정`/`Google Account` strings, and the fixture's report converts to ≥7 headings, 1 table with 8 data rows × 4 columns (no empty cells), ≥30 links. Also run `.superpowers/sdd/tools/scan_inputs.py` on it before committing.
- [ ] Tests first; implement; full suite + run_verification; commit `feat(agy-deep-research): report tree to Markdown with structural check`.

### Task R3: driver approve / collect / auto wiring
**Files:** Modify `scripts/run_deep_research.py` (dispatch table from B3), `scripts/relay_step.py` only if the result contract needs new fields; Test `scripts/tests/test_driver_research.py`.
**Interfaces:** `--step approve` → `steps.approve` → print `Clicking '연구 시작'` → `wait_research` with progress lines → on completion the collect path; `--step collect` → bind → `wait_research` (already complete returns at once) → `report_md` → structural check → print `[REPORT_PROVENANCE] <conversation>` then the report block; `--step auto` → the same path as plan → approve → collect without the hook (spec §21.1 "유지하되 경로 통일"). Result JSON fields per Global Constraints. `recover` = §11-12 via app menu `보기 → 새로고침` + sidebar link matched by URL value only.
- [ ] Tests (fake host + FakeTree research fixtures): approve prints the confirm line then progress lines; collect prints provenance immediately before the report block; mismatch conversation → CONVERSATION_MISMATCH and no report; stall paths map to their codes and exit codes; D1 sets `research_completion_verified: false`; nothing unscrubbed in stdout.
- [ ] Implement; full suite + run_verification; commit `feat(agy-deep-research): approve, collect and auto on the UIA backend`.

**Milestone M3 → X2** (agy Neon high) on Task 8 + B1–B5 + R1–R3, then SKILL.md/reference rewrite (writing-skills), then stop before G4 with the live budget needed for the E2E.

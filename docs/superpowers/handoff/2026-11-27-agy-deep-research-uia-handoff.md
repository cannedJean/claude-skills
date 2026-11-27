# Handoff: agy-deep-research UIA driver (2026-11-27)

This document and the repository are enough to continue. Everything outside the repository is listed under **Optional material** and may be missing on another PC.

## 1. Goal and the approach that is fixed

Replace the CDP/Playwright driver of the skill `skills/agy-deep-research` with a Windows **UI Automation (COM IUIAutomation, `comtypes`)** driver that operates the **unmodified Gemini desktop app**. Keep the agy relay (`relay_step.py` run by `agy-delegate --deep-research` under confinement), the gates in `deep_research.py`, and the driver stdout protocol: `[CONVERSATION] <url>`, `=== [PLAN_REVIEW_HOOK_START] ===` … `END`, `[REPORT_PROVENANCE] ` immediately before `=== DEEP RESEARCH REPORT START ===` … `END`.

User decisions (binding; do not reopen):
- **No browser, no CDP, no debugging port, no app patching.** `patch_gemini_app.py` and Chrome mode are removed (`dd956e3`). Nothing is ever written under `%LOCALAPPDATA%\Google\Gemini\`.
- **Allowed app switches, exactly four:** `--user-data-dir=<tool profile dir>`, `--disable-backgrounding-occluded-windows`, `--disable-renderer-backgrounding`, `--disable-features=CalculateNativeWinOcclusion`. Launch through the app's launcher stub only.
- **Main path D2:** hidden Windows desktop `agy-deep-research` + dedicated profile `%LOCALAPPDATA%\agy-deep-research\gemini-profile`. **Fallback D1** (default profile, no switches, on the hidden desktop) only automatically, and refused while the user's own Gemini runs. D1 is "**research completion unverified**".
- **R6 invariant:** our instance never takes the user's foreground and never shows a window on the user's (default) desktop. Measured from `deep_research.py` on the default desktop (never from the relay: agy runs commands on its own `exebox-*` desktop). Missing measurement outside the test sandbox fails closed.
- **auto mode:** kept, same path as plan → approve → collect.
- **Model default:** Neon + thinking level high (pro→Neon, flash→Hydrogen, argon→Carbon).
- **Report verification:** structural checks (`report_md.structural_check`); clipboard comparison is opt-in only.
- **Cross-validation with agy:** `agy_task.py --model gemini-4-neon-high … --effort high` (milestone X2 reviews only).
- **Implementation and task reviews:** Claude subagents (superpowers subagent-driven-development + TDD + review subagents).

Spec: `docs/superpowers/specs/2026-11-26-agy-deep-research-uia-design.md` (§23 and §21 take precedence over earlier sections). Plans: `docs/superpowers/plans/2026-11-26-agy-deep-research-uia-core.md` (Tasks 1–8), `…-2026-11-27-agy-deep-research-uia-backend.md` (B1–B5), `…-2026-11-27-agy-deep-research-uia-research.md` (R1–R3).

## 2. Done (branch `feat/agy-deep-research-uia`; origin has everything up to `db65b1a`)

Review status: **reviewed** = task review + fixes + scoped re-review clean; **fixed, not re-reviewed** = review found problems, a fix landed, nobody re-checked it; **unreviewed**.

| Unit | Commits | Status |
|---|---|---|
| Plan 1 Tasks 1–8, 5b (uitree, privacy, locators, gemini_host decisions, static guards, patcher/Chrome removal, test sandbox, confinement, plan/edit steps) | 564b713 … c6c8d62 | reviewed; M1 X2 and M2 X2 (agy Neon high) done, fixes up to beb14b5 |
| B1 `uia_backend.py` (comtypes UiTree) | 0321d3b, 2090dd8 | reviewed |
| B2 host Win32 half (desktop, launch, inventory, quit, lock, host.json) | 37620ca, adcf8c2 | reviewed |
| B3 driver plan/edit/probe on UIA | 77ef508, 56b5978 | reviewed |
| B4 CLI preflight/setup --with-venv/doctor/signin/app | 7d360d4, db65b1a | reviewed |
| R2 `report_md.py` + scrubbed S8 fixture | 2dd7ece, 760fb30 | reviewed |
| B5 R6 foreground tracer | 49ac715 (Claude), 4ece3fb (fix 1, agy Neon)  | fix 1 reviewed (agy Carbon high: request changes, verified by the lead). **Fix 2 unfinished** (contents: stop files out of agy's reach + protect `relay/fg_*`, remove `--worker` form, seq-aware result.json stop, collect Case 3 inherits a violation, tracer-start / no_end_record / max_duration fail closed, visible-mode excuse only signin/doctor, `pending_launch` for D1 on Default, unseen-pid refresh, tracer lifecycle). Nothing of fix 2 is committed; a partial confine_hook/test_confine draft (one broken test string) is saved outside git as `.superpowers/sdd/2026-11-27-agy-deep-research-uia-backend/b5-fix2-partial.patch`. Not re-reviewed. |
| R1 approve + long research wait | 46cfa9b (Claude), 677fd65 (relay codes), 378cbea (fix 1, agy Neon) | fix 1 re-reviewed (Claude): 12/12 addressed; **1 Important open**: plan/edit report `cards` = turns with a start button, approve now compares `expect_cards` with turns holding any plan control → false `PLAN_CARD_STALE` when a startless plan-control turn precedes the reviewed card (steps.py ~770/791 vs approve). Minor open: docstring says UNEXPECTED_SURFACE "started? no" for approve, but after the Invoke it may have started. |
| Docs | 1b309f1 (§23 live results), 59d8305 (plan 3), 2b35d11 + this commit (paths → placeholder) | — |

Test state at handoff: run_verification 7/7 at 378cbea (lead, before the handoff docs); **final full test run after this commit not executed** (docs-only commit).

Review artifacts and per-task ledgers (not in git, may be missing): `<work folder>\uia\.superpowers\sdd\…\progress.md`. The B5 and R1 review texts that drove the last fixes are summarised in §4.

## 3. Remaining work, in order

1. **Close the open review items.** R1: make plan/edit report plan-control turns (or pass `expect_cards=None` until they do) and fix the docstring, then a scoped re-review. B5: implement fix 2 from the rulings in §4 (start from the patch file if it exists, else from scratch), then a review. Claude implementer + Claude review subagents, rulings in §4.
2. **R3 — driver approve / collect / auto** (plan 3, Task R3): wire `steps.approve` / `steps.wait_research` / `report_md` into `run_deep_research.py`'s step table; print `Clicking '연구 시작'`, `[Ns] Progress: …`, `[REPORT_PROVENANCE] …` + report block; result JSON fields `switches`, `longest_no_change_s`, `recoveries`, `host_mode`, `research_completion_verified` (false for D1); `recover` = app menu `보기 → 새로고침` + sidebar link matched by URL value only, at most once (§21.3). Must also restore guarantees from deleted tests: navigation errors during report wait, a report panel from another research rejected, page moved during extraction refused, provenance mismatch names both conversations. Check `expect_cards` semantics (R1 fix counts plan-control turns) against what plan/edit records.
3. **M3 X2** — agy Neon high cross-validation on Task 8 + B1–B5 + R1–R3 (same layout as M1/M2: inputs scanned first, findings verified, rulings recorded).
4. **Docs rewrite** with superpowers:writing-skills: `SKILL.md`, `references/*.md`, agy-delegate README (protected files incl. `relay/driver_state.json`, `relay/fg_*`), spec §15 old token `profile_switch_ignored` → `data_dir_switch_ignored`.
5. **G4 — full E2E through the agy relay** (plan → edit → approve → collect, and auto). Needs a **live budget approved by the user** (sends and research runs). Before it, a live `doctor` on the hidden desktop to settle the live-check list in §6.
6. **G5 — PR and replacing the installed copy** (`~/.claude/skills`, `~/.gemini/config/skills`). User approval required.

## 4. Open review rulings (inputs for step 1)

R1 fix 1 (378cbea) had to: fail closed with `UNEXPECTED_SURFACE` when the privacy allow-list is empty/invalid; count plan-control turns against `expect_cards` and refuse a card with a running countdown; refuse when any turn follows the newest card (`ALREADY_STARTED`); resolve `prompt_input` every poll; scrub status and title before joining; report controls via the locator matcher (H1/chat-marker rules stay fail-closed); reset the 20 s hold while the signature changes; ko done phrase prefix; thought titles only with a card; return link enabled and no offscreen ancestor; `longest_no_change_s` and clamped sleeps; approve's first read retried.

B5 fix 2 had to: keep tracer stop files outside the agy work dir (tool dir) and protect `relay/fg_*`; remove the `--worker` relay command form from confinement; make the `relay/result.json` stop seq-aware; `collect` Case 3 enforces the saved trace; fail before the relay when the tracer did not start; `no_end_record`/`max_duration` = unmeasured; auto `max_s = 2*(timeout+420)+600`; visible mode excuses only signin/doctor; record `pending_launch` in host.json so a D1 launch on the default desktop counts as ours; refresh pid sources on an unseen Gemini pid; `_session_record_pids` strict; no orphan/duplicate tracers.

## 5. Measured facts (2026-11-26/27) and open risks

- Plan card appears ~21 s after send, with and without the three switches; countdown cancel via `취소` Invoke holds; 5 min stable afterwards.
- **One research completed on the hidden desktop with the three switches in ~19 min, zero refreshes, tree reads only** (2026-11-27). The day before, without switches and with ~1-minute refresh/re-entry, the research never finished. **Causality of the switches is unproven** → the long wait keeps stall detection (default 3600 s), at most one recovery and only when the return link is visible now, else `RESEARCH_STALLED` / `RECOVERY_UNAVAILABLE`.
- Completion signal: no stop button in the last turn + done phrase (`I've completed your research.` even on a ko account) + report controls (`공유 및 내보내기`/`Share & export`, `목차 메뉴`, `보고서에서 사용된 소스`), held 20 s.
- Report tree: Table(grid) → DataItem(row) → leaf DataItem whose **Name** is the cell text; inline runs are split at inline code; the report body sits in a panel next to the chat (4 headings, 1 table 8×4, 33 links in the fixture).
- Re-entry invariant: never refresh unless the job's link is visible now; there is no way into a conversation from outside the app (no URL argument, no protocol handler); the job conversation can drop out of the recent list.
- Cross-desktop `PostMessage(WM_CLOSE)` fails (error 1400); quit = app menu `파일 → 종료` via UIA from a thread on that desktop (worked live, < 10 s). `SetThreadDesktop` must run before importing comtypes.
- Open risks: switch causality; re-entry depends on the sidebar list; automation ids of chat turns in the live report tree were not recorded (turn detection to confirm in G4); location of thought titles unmeasured; live behaviour of `ComtypesUia` / `Win32HostOS` (§6).

## 6. Live-check list (first approved live doctor)

`GetCurrentPattern` + `QueryInterface` per pattern; cached RuntimeId shape; `COMError` hresult in `args[0]`; Win32 struct layouts and the class-60 command-line read; UIA in a fresh thread per desktop after comtypes was imported elsewhere; menu-quit patterns the items actually advertise; detached tracer opens no window and steals no focus; cost of the per-second instance lookup.

## 7. Incidents and what prevents them now

1. **2026-11-26, privacy:** a spike log recorded the account control's name (user name and e-mail) unmasked. Fix: structural allow-list (`privacy.allowed_ids`, only `main` outside sidebar/navigation, fail closed) + `privacy.scrub` on every output; agy inputs scanned before every run.
2. **2026-11-27, app modified:** a test run executed the old patcher and patched the real `app.asar`. The user reinstalled the app (hash back to the baseline). Fix: `tests/sandbox.py` imported first by every test (redirects LOCALAPPDATA/APPDATA, refuses Gemini paths, browsers, the patcher, `--remote-debugging-port`, writes under the app dir; propagates to child processes) and `run_verification.py` refuses to run unit tests without it. Every subagent brief carries the safety block in §9.

## 8. Left on this PC (not in git)

- Dedicated profile `%LOCALAPPDATA%\agy-deep-research\gemini-profile` (signed in).
- The old patcher's `patch_state.json` — keep it (incident record).
- Four test conversations in the user's Gemini account ("test conversation 1–4"; URLs only in the private progress log, never in git).
- No instance of ours running; the app folder matches the post-reinstall baseline (225 files).

## 9. Environment, commands, rules

- Python 3.9+ on Windows. Dev venv `<work folder>\.venv` with `comtypes==1.4.11` (the skill's own `setup --with-venv` installs it with `--require-hashes` from `scripts/requirements-uia.txt`).
- Tests: from `skills/agy-deep-research/scripts`: `python -m unittest discover -s tests`; from `skills/agy-deep-research`: `python scripts/tests/run_verification.py` (7 checks). agy-delegate: `py -3 -m unittest discover -s skills/agy-delegate/scripts/tests` from the repo root (use system Python: a copied venv interpreter lacks `pyvenv.cfg` and fails one installer test).
- Never: run the real app, a browser or the drivers from tests or subagents; write under `%LOCALAPPDATA%\Google\Gemini\`; send to Gemini without a user-approved live budget; push or open a PR without approval; edit `~/.claude/skills` or `~/.gemini`; install into system Python; open, read or log the user's other Gemini conversations; put conversation URLs, account strings or local user paths into commits.
- Commits: local only unless told; author identity as in existing commits; end messages with the Co-Authored-By line.

## 10. Gates and when to ask the user

- **G4** (before any live E2E): report the exact live budget needed (number of plan sends, edits, research runs) and wait for approval.
- **G5** (PR, installed-copy replacement): ask.
- **G-X** (stop immediately and report): any write or change under the app dir, any unmasked account/conversation data in a log, an unexpected Gemini window on the user's desktop, a budget overrun, or a finding that changes the design.

## Optional material (may be missing)

`<work folder>\DEV_PROMPT.md` (original brief), `<work folder>\PROGRESS.md` (append-only decision log with real timestamps and the test conversation URLs), `<work folder>\spike\` (live spike scripts and raw measurements, incl. `live_s8.json`, `live_monitor_research.json`, app hash baselines), `<work folder>\uia\.superpowers\sdd\` (ledgers, review reports, X2 inputs/rulings), `<work folder>\uia\agy_tasks\` (agy work dirs of the last fixes).

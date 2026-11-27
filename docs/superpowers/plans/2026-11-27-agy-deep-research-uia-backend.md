# agy-deep-research UIA backend, host and plan/edit wiring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect the finished pure units (uitree, privacy, locators, gemini_host decisions, steps) to Windows: a comtypes UI Automation backend, the Win32 half of the host (hidden desktop, launch, inventory, quit, lock), the driver's plan/edit/probe steps, the doctor/signin/app CLI, and the R6 foreground tracer — all without S7/S8 (approve/collect/report stay `UIA_DRIVER_NOT_IMPLEMENTED`).

**Architecture:** Every Win32/COM call sits behind a small injectable interface (`HostOS`, `UiaCom`) so all logic is unit-tested with fakes inside the test sandbox; the real implementations are thin and are exercised only in a later, approved live check (doctor on the hidden desktop).

**Tech Stack:** Python 3.9+ stdlib + `ctypes`; `comtypes==1.4.11` only inside `uia_backend.py`, imported lazily.

**Spec:** `docs/superpowers/specs/2026-11-26-agy-deep-research-uia-design.md` (§5 measured facts, §7–§12, §14–§15, §17b, §20, §21 take precedence).

## Global Constraints

- Everything in the first plan's Global Constraints still binds (sandbox first import, no CDP/browser, scrub every output, allowed switches, no writes under `%LOCALAPPDATA%\Google\Gemini\`).
- **Live-safety rule:** any real launch, desktop creation, window message or UIA call is refused when `AGY_DR_TEST_SANDBOX=1` (raise `HostRefused("sandbox")`). Unit tests use fakes only. No task in this plan runs the real app; live verification is a separate, approved step.
- Desktop name `agy-deep-research`; dedicated profile `%LOCALAPPDATA%\agy-deep-research\gemini-profile`; state `%LOCALAPPDATA%\agy-deep-research\host.json`; lock `%LOCALAPPDATA%\agy-deep-research\host.lock`.
- UIA node ids must be unique and non-empty within a snapshot and stable across snapshots of the same element (privacy fails closed on duplicate or empty ids): use the UIA RuntimeId joined with `.`.
- The driver thread switches desktop (`SetThreadDesktop`) before importing comtypes (measured requirement).
- Measured: cross-desktop `PostMessage(WM_CLOSE)` fails (error 1400); quit = app menu `파일 → 종료` via UIA from a thread on that desktop, then same-desktop WM_CLOSE, then TerminateProcess of OUR verified process tree only.

## Review Focus

1. A fake `HostOS` reporting a user Gemini process must make D1 refuse (`GEMINI_IN_USE`) — never launch. (Task B2)
2. `app close` given a process list containing the user's Gemini (no `--user-data-dir`) and ours must terminate only ours. (Task B2)
3. A snapshot where two elements share a RuntimeId (or have none) must yield unique ids or make the backend raise — never silently duplicate. (Task B1)
4. `doctor` output on a fixture with a sidebar must contain no sidebar titles or account strings. (Task B4)
5. The plan step must print `[CONVERSATION]` only after the URL binds and must never print plan text that bypassed `privacy.scrub`. (Task B3)

---

### Task B1: `uia_backend.py` — comtypes UiTree implementation
**Files:** Create `scripts/uia_backend.py`; Test `scripts/tests/test_uia_backend.py`.
**Interfaces:** Produces `class UiaTree(UiTree)` with `UiaTree(com: UiaCom, window_filter)`; `class UiaCom(Protocol)` (methods: `root()`, `find_windows(class_name)`, `build_cache(element, props) -> CachedElement`, `pattern(element, name)`, `runtime_id(element)`, `process_id(element)`); `load_com() -> UiaCom` (lazy `comtypes` import; raises `UiaUnavailable` with the setup hint). Property→Node mapping per spec §9 (ct, role=AriaRole, name, aria=parsed AriaProperties `k=v;…`, heading=HeadingLevel-80050 when 1..6, enabled, offscreen, value=ValueValue, automation_id, patterns from Is*PatternAvailable). Act mapping: invoke/toggle/expand/collapse/default_action → pattern calls; unsupported → `PatternNotSupported`. `desktop_root()` = UIA root of the current thread's desktop.
- [ ] Tests (fake UiaCom): property mapping incl. AriaProperties parsing and heading offset; RuntimeId-based unique ids, duplicate/missing RuntimeId → disambiguated with a path suffix and never empty; `documents()` returns RootWebArea nodes with URL value; act dispatch and PatternNotSupported; `load_com` failure → UiaUnavailable; sandbox refusal of `load_com()` when AGY_DR_TEST_SANDBOX=1.
- [ ] Implement; full suite + run_verification; commit `feat(agy-deep-research): comtypes UI Automation backend`.

### Task B2: `gemini_host.py` Win32 half — desktop, launch, inventory, quit, lock
**Files:** Modify `scripts/gemini_host.py` (add `HostOS` protocol, `Win32HostOS`, `Host` orchestration); Test `scripts/tests/test_gemini_host_win32.py`.
**Interfaces:** `class HostOS(Protocol)`: `open_desktop(name)`, `create_desktop(name)`, `enum_desktops()`, `desktop_windows(name) -> list[WindowInfo]`, `processes() -> list[ProcInfo]`, `launch(cmdline, desktop, env) -> int`, `terminate(pid)`, `local_state_mtime(profile)`; `class Host(os: HostOS, cfg)`: `ensure(job_mode) -> HostSession(mode, desktop, browser_pid, reason)`, `instances() -> list[InstanceInfo]` (desktop, pids, profile, orphan flag), `close(session_or_instance, quit_via_menu: Callable) -> CloseResult(remaining=int)`, `lock()` context manager (`msvcrt.locking`, `HOST_BUSY` when held). Uses Task-4 decisions (`build_launch_command`, `classify_launch`, `decide_mode`, `is_our_instance`, `clean_env`). `Win32HostOS` refuses every mutating call under `AGY_DR_TEST_SANDBOX=1`.
- [ ] Tests (fake HostOS): D2 ok path; D2 blocked + user app → GEMINI_IN_USE (no launch call); D2 slow start pending then ok; pinned mode; orphan detection; close terminates only our verified tree (user process untouched), reports remaining 0; lock contention → HOST_BUSY; Win32HostOS mutating calls raise HostRefused in the sandbox.
- [ ] Implement; full suite + run_verification; commit `feat(agy-deep-research): hidden-desktop host (desktop, launch, inventory, quit, lock)`.

### Task B3: driver plan/edit/probe wiring
**Files:** Modify `scripts/run_deep_research.py` (remove `run_flow` and the old page code), `scripts/relay_step.py` only if the request/response contract needs it; Test `scripts/tests/test_driver_uia.py` (+ adapt `test_model_lock.py` to the new path without weakening assertions).
**Interfaces:** `main()` builds `Host` + `UiaTree` (injectable via `driver_factory` for tests); `--step plan` → `steps.ensure_fresh_chat`, `select_mode`, `enable_deep_research`, `enter_prompt`, `send_and_bind` → print `[CONVERSATION] <url>`, `wait_plan_and_cancel` → print the existing `=== [PLAN_REVIEW_HOOK_START] ===` block (fields unchanged); `--step edit` → `edit_plan(prev_cancel=…)`; `--step probe` → `{"start_enabled","plan_cards","url"}`; `approve`/`collect`/`auto` → still `UIA_DRIVER_NOT_IMPLEMENTED` (exit 3). StepError → JSON error with code (scrubbed), exit codes per relay table. Holds `host.lock` for the whole step.
- [ ] Tests (fake host + FakeTree plan fixtures from Task 8): plan prints CONVERSATION then the hook block; edit passes prev_cancel; probe contract; StepError mapping; lock held; nothing unscrubbed in stdout; approve/collect still not implemented.
- [ ] Implement; full suite + run_verification; commit `feat(agy-deep-research): driver runs plan/edit/probe on the UIA backend`.

### Task B4: CLI — preflight, setup (code path), doctor, signin, app
**Files:** Modify `scripts/deep_research.py`; Test `scripts/tests/test_cli_uia.py`.
**Interfaces:** `preflight` adds `UNSUPPORTED_PLATFORM` (non-Windows), venv/comtypes check (`UIA_UNAVAILABLE` + exact setup command), orphan instance warning, app version vs last success (diagnostic only); `setup` adds venv creation + `pip install --require-hashes comtypes==1.4.11` behind an explicit `--with-venv` flag (never run in tests; argv asserted); `doctor [--snapshot]` → JSON per locator (resolved / miss + candidates), instances, R6 tracer result, all scrubbed, read-only; `signin` → launch dedicated profile visibly (default desktop), wait for account control (logs only true/false), quit via menu; `app status|close`.
- [ ] Tests (fakes): each command's JSON shape; doctor output free of fixture sidebar/account strings; signin never logs names; app close only ours; setup argv contains `--require-hashes` and the pinned version; non-Windows → UNSUPPORTED_PLATFORM.
- [ ] Implement; full suite + run_verification; commit `feat(agy-deep-research): doctor, signin and app commands`.

### Task B5: R6 foreground tracer
**Files:** Create `scripts/fg_tracer.py`; Modify `scripts/deep_research.py` (plan/edit/doctor run it); Test `scripts/tests/test_fg_tracer.py`.
**Interfaces:** `FgTracer(os_api, period_s=0.02)` start/stop → `{gemini_foreground_events, ours, visible_windows}`; detached mode writes `<job>/relay/foreground.jsonl` (protected from agy) and exits when the watched pid ends. Our instance becoming foreground → step result `foreground_invariant_violated: true` and exit code 2 (spec §20-11).
- [ ] Tests with fake os_api; implement; full suite + run_verification; commit `feat(agy-deep-research): R6 foreground invariant tracer`.

**Milestone M3 → X2** (agy Neon high) on Task 8 + B1–B5, then a live doctor on the hidden desktop only after the supervisor's reinstall notice and approval.

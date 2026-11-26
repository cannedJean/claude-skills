# agy-deep-research UIA core (S7/S8-independent units) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the parts of the hidden-desktop UI Automation driver that do not depend on the unresolved long-wait/report questions: tree model + fake backend, privacy mask, locator engine, host decision logic, CDP/patcher removal with static guards, confinement tightening, and the plan/edit step logic.

**Architecture:** Pure-Python units under `skills/agy-deep-research/scripts/` operate on an abstract `UiTree` of `Node`s; a JSON-driven `FakeTree` lets every step run without Windows or Gemini. OS-touching code (`uia_backend.py`, the Win32 half of `gemini_host.py`) is out of scope here except for pure decision functions.

**Tech Stack:** Python 3.9+ stdlib (`unittest`, `dataclasses`, `difflib`, `json`, `re`); comtypes only in the later backend task (not in this plan).

**Spec:** `docs/superpowers/specs/2026-11-26-agy-deep-research-uia-design.md` (sections §7, §9–§13, §15–§17, §17b, §20, §21 take precedence over earlier text where they conflict).

## Global Constraints

- Windows-only product; these units must import and test on any OS (no `ctypes.windll`, no comtypes at import time).
- Tests: `unittest`, files `skills/agy-deep-research/scripts/tests/test_*.py`, run by `python scripts/tests/run_verification.py` from `skills/agy-deep-research`.
- Never write under `%LOCALAPPDATA%\Google\Gemini\`. Allowed app switches are exactly: `--user-data-dir=<path>`, `--disable-backgrounding-occluded-windows`, `--disable-renderer-backgrounding`, `--disable-features=CalculateNativeWinOcclusion` (§21.1).
- No `playwright`, CDP, `remote-debugging`, `connect_over_cdp`, `chrome_launcher`, `9223`, `9224`, `--browser chrome` in scripts (R2).
- Every string that can reach stdout, result JSON, logs or snapshots passes through `privacy.scrub` (R7, §13, §20-4).
- Fixtures contain only test-conversation text; account controls in fixtures use `Google 계정: 테스트(test@example.invalid)`.
- Do not weaken gates or delete/skip existing tests to make anything pass. Tests that only covered removed code (`test_patch_pattern.py`, `test_patch_status.py`, `test_chrome_mode.py`) are deleted together with that code in Task 6.
- Commit identity: `git -c user.name=cannedJean -c user.email=188310089+cannedJean@users.noreply.github.com commit ...`, message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **Privacy structure surprises**: a page with no `main`, two `main`s, or a sidebar `region` without `navigation` must make `privacy.allowed_ids` return no names (fail closed), never "everything allowed". Test in Task 2.
2. **Label drift on absence checks**: "send button absent ⇒ input empty" must not be concluded when the input itself is missing (positive anchor). Test in Task 8 (`test_empty_check_requires_input_anchor`).
3. **Override file mistakes**: a `locators.local.json` with an unknown key, a wrong type, or a Hyperlink locator pointing off `gemini.google.com` is rejected with `LocatorConfigError` naming the field, and the bundled file still loads. Test in Task 3.
4. **Two plan cards after edit**: the step logic must cancel/extract the newest card only (last in document order, enabled start, count = previous + 1), never the old one. Test in Task 8.
5. **Process identity reuse**: a Gemini process with the right image but a different `--user-data-dir` (the user's own app) is never treated as ours. Test in Task 4.

---

### Task 1: `uitree.py` — Node model, UiTree protocol, FakeTree

**Files:**
- Create: `skills/agy-deep-research/scripts/uitree.py`
- Test: `skills/agy-deep-research/scripts/tests/test_uitree.py`

**Interfaces:**
- Produces:
  - `@dataclass Node(id: str, ct: str, role: str = "", name: str = "", aria: dict = {}, heading: int | None = None, enabled: bool = True, offscreen: bool = False, value: str = "", automation_id: str = "", patterns: frozenset = frozenset(), children: list = [])` (use `field(default_factory=...)`).
  - `Node.walk() -> Iterator[tuple[Node, tuple[Node, ...]]]` — pre-order, yields `(node, ancestors_root_first)`.
  - `Node.find_all(pred: Callable[[Node, tuple], bool]) -> list[Node]` in document order.
  - `Node.from_dict(d: dict) -> Node`, `Node.to_dict() -> dict` (round-trip; `patterns` serialized as sorted list).
  - `class UiTree(Protocol)`: `documents() -> list[Node]` (RootWebArea nodes, `value` = URL), `desktop_root() -> Node` (popups/menus), `snapshot(doc_id: str) -> Node`, `act(node_id: str, pattern: str) -> None` with `pattern in {"invoke","toggle","expand","collapse","default_action"}`, `set_value(node_id: str, text: str) -> None`, `get_value(node_id: str) -> str`.
  - `class PatternNotSupported(Exception)`.
  - `class FakeTree(UiTree)`: `FakeTree(spec: dict)`, `FakeTree.from_file(path) -> FakeTree`, attribute `actions: list[tuple]` (e.g. `("act", "btn-start", "invoke")`, `("set_value", "edit", text)`), attribute `state: str`.
- Fake spec JSON: `{"state": "<name>", "states": {"<name>": {"documents": [node...], "desktop": node|null}}, "transitions": [{"from": "<state>"|"*", "on": ["act", node_id, pattern] | ["set_value", node_id], "to": "<state>", "after_snapshots": int}]}`. `after_snapshots` = number of further `snapshot()` calls before the new state becomes visible (models the measured render delay). `set_value` on a node stores the text in that node's `value` in every state unless the transition replaces it. An `act` with a pattern not in the node's `patterns` raises `PatternNotSupported`.

- [ ] **Step 1: Write failing tests** `test_walk_preorder_with_ancestors`, `test_dict_round_trip`, `test_fake_transition_after_snapshots` (act → state changes only on the 2nd snapshot when `after_snapshots: 1`), `test_fake_set_value_records_and_reads_back`, `test_fake_rejects_unsupported_pattern`.
- [ ] **Step 2: Run** `python -m unittest tests.test_uitree -v` (cwd `skills/agy-deep-research/scripts`) → FAIL (module missing).
- [ ] **Step 3: Implement** `uitree.py` (stdlib only).
- [ ] **Step 4: Run** the same command → PASS.
- [ ] **Step 5: Commit** `feat(agy-deep-research): UiTree model and JSON FakeTree`.

### Task 2: `privacy.py` — structural allow-list and scrub

**Files:**
- Create: `skills/agy-deep-research/scripts/privacy.py`
- Test: `skills/agy-deep-research/scripts/tests/test_privacy.py`, fixture `tests/fixtures/privacy_page.json` (FakeTree spec: main > region(sidebar: navigation + account Hyperlink `Google 계정: 테스트(test@example.invalid)` + chat links `비밀 대화 제목 1`), main > conversation group with `model-response-message-content-x` and Text, main > composer Edit)

**Interfaces:**
- Consumes: `uitree.Node`.
- Produces:
  - `SENSITIVE_RE = re.compile(r"@|계정|Account|프로필|Profile", re.I)`.
  - `scrub(value: Any) -> Any` — recursive over dict/list/tuple; `str` of length ≤ 200 matching `SENSITIVE_RE` → `f"<redacted:account len={len(s)}>"`; longer `str` → e-mail `[\w.+-]+@[\w-]+(\.[\w-]+)+` → `<redacted:email>` and `Google (계정|Account)\s*:[^\n]*` → `<redacted:account>`; other types unchanged.
  - `region_check(doc: Node) -> tuple[bool, str]` — ok only if exactly one node with role `main` exists and every `region` that contains a `navigation` descendant is inside it; reason string otherwise (`"main_count=0"`, `"main_count=2"`).
  - `allowed_ids(doc: Node) -> set[str]` — empty set when `region_check` fails; else ids of nodes inside `main` and not under roles `navigation, banner, contentinfo, complementary, search` and not under a `region` that has a `navigation` descendant.
  - `outside_summary(doc: Node, allowed: set[str]) -> dict[str, int]` — counts `"<ct>/<role or ->"` of non-allowed nodes.
  - `GENERIC_LABELS: frozenset[str]` — names that may be compared (never logged) outside the allowed region: `새 채팅, New chat, 사이드바 열기, 사이드바 닫기, Open sidebar, Close sidebar, 더보기, Show more, 메인 메뉴, Main menu`.

- [ ] **Step 1: Write failing tests**: `test_scrub_short_account_string`, `test_scrub_long_text_keeps_content_removes_email`, `test_scrub_recurses_dicts_lists`, `test_allowed_excludes_sidebar_region_and_account` (account and chat titles not in allowed; conversation Text and Edit are), `test_fail_closed_without_main`, `test_fail_closed_with_two_mains`, `test_outside_summary_counts_only`.
- [ ] **Step 2: Run** `python -m unittest tests.test_privacy -v` → FAIL.
- [ ] **Step 3: Implement** `privacy.py`.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(agy-deep-research): structural privacy allow-list and scrub`.

### Task 3: `locators.py` + `locators.json` — data-driven locator engine

**Files:**
- Create: `skills/agy-deep-research/scripts/locators.py`, `skills/agy-deep-research/scripts/locators.json`
- Test: `skills/agy-deep-research/scripts/tests/test_locators.py`, fixtures `tests/fixtures/locators_page_ko.json`, `tests/fixtures/locators_page_en_synthetic.json` (same tree, English labels; `"_note": "synthetic: Korean recording with labels substituted"`)

**Interfaces:**
- Consumes: `uitree.Node`, `privacy.allowed_ids`, `privacy.GENERIC_LABELS`.
- Produces:
  - `locators.json` schema: `{"version": 1, "scopes": {...}, "locators": {key: {"ct": str, "role": str?, "names": [str | "re:<regex>"], "scope": str, "expect": "unique" | "at_least_one" | "optional", "patterns": [str], "href_host": str?}}}`. Scopes (data, used by code): `"document"`, `"main"` (subtree of role main), `"composer"` (`{"anchor": "prompt_input", "min_buttons": 3}`: lowest ancestor of the anchor whose subtree has ≥ 3 Buttons), `"plan_card"` (`{"ancestor_automation_id_prefix": "model-response-message-content"}`), `"popup"`/`"desktop"` (resolved against `UiTree.desktop_root()`), `"top_bar"` (document whose URL starts `app://bundle/src/top_bar/`).
  - Keys required in the bundled file (labels from spec §5/§10/§21): `prompt_input, send, stop_response, tools, more_tools, dr_item, dr_on, mode_button, mode_item, thinking_menu, new_chat, sidebar_open, sidebar_close, sidebar_more, plan_start, plan_edit, plan_cancel_countdown, plan_retry_without_dr, app_menu, app_menu_file, app_menu_quit, app_menu_view, app_menu_reload, sign_in` — each with ko and en names (`연구 시작`/`Start research`, `연구 계획 수정`/`Edit the research plan`/`Edit plan`, `취소`/`Cancel`, `Deep Research 선택 해제`/`Deselect Deep Research`, `도구 더보기`/`More tools`, `메시지 보내기`/`Send message`, `대답 생성 중지`/`Stop response`, `업로드 및 도구`/`Upload and tools`, `re:모드 선택 도구 열기.*`/`re:Open mode picker.*`, `새 채팅`/`New chat`, `메인 메뉴`/`Main menu`, `파일`/`File`, `re:종료.*`/`re:(Quit|Exit).*`, `보기`/`View`, `re:새로고침.*`/`re:Reload.*`, `로그인`/`Sign in`, `Gemini 프롬프트 입력`/`Enter a prompt for Gemini`).
  - Also data: `"surfaces_allowed": ["re:^https://gemini\\.google\\.com/app(/[0-9a-f]{8,})?$", "re:^app://bundle/src/top_bar/", "re:^blob:"]`, `"status_phrases"` and `"safety_phrases"` lists (`조사 계획 생성 중`, `도와드릴 수가 없습니다`, `단지 언어 모델일 뿐`, `요청하신 작업을 처리하는 중에 오류가 발생했습니다`).
  - `class LocatorConfigError(ValueError)` (message names the key and field).
  - `class LocatorMiss(Exception)` with `.payload: dict` = `{"code": "UI_LOCATOR_MISS", "locator", "scope", "expect", "found", "candidates": [{"name", "ct", "role", "similarity"}] (≤ 5, from allowed region or GENERIC_LABELS only, similarity = difflib ratio rounded to 2), "override_snippet": {"version": 1, "locators": {key: {"names": [<existing>..., <best candidate>]}}}}`. Payload passes `privacy.scrub`.
  - `load_locators(bundled: Path, override: Path | None) -> dict` — field-level merge of the override's `locators[key]` into the bundled entry; unknown top-level or locator fields, wrong types, unknown scope, or `href_host` other than `gemini.google.com` → `LocatorConfigError`.
  - `match_name(name: str, patterns: list[str]) -> bool` — whitespace-normalized exact match, `re:` = full match.
  - `resolve(key: str, ctx: "LocatorContext") -> list[Node]` raising `LocatorMiss` when the `expect` count fails. `@dataclass LocatorContext(tree: UiTree, doc: Node, locs: dict, allowed: set[str], anchor_cache: dict = {})`.

- [ ] **Step 1: Write failing tests**: `test_bundled_file_has_required_keys_ko_en`, `test_resolve_unique_in_composer_scope`, `test_resolve_plan_card_scope_uses_automation_id_prefix`, `test_miss_payload_has_candidates_and_snippet` (rename `연구 시작`→`조사 시작` in the fixture; candidate list contains `조사 시작`; snippet names include it), `test_override_file_alone_recovers` (write snippet to a temp override → `resolve` succeeds), `test_override_rejects_unknown_field`, `test_override_rejects_foreign_href_host`, `test_en_synthetic_fixture_resolves_all_keys`, `test_candidates_never_include_sidebar_titles`.
- [ ] **Step 2: Run** `python -m unittest tests.test_locators -v` → FAIL.
- [ ] **Step 3: Implement** `locators.py` and write `locators.json`.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(agy-deep-research): data-driven locators with UI_LOCATOR_MISS hints`.

**Milestone M1 → X2** (agy Neon high, 3 perspectives: bugs/edge cases, tests-vs-requirements, missing requirements) on the diff of Tasks 1–3 + tests. Dispositions in `reviews/REVIEW_LOG.md`.

### Task 4: `gemini_host.py` — pure host decisions

**Files:**
- Create: `skills/agy-deep-research/scripts/gemini_host.py` (decision half only; Win32 calls added in a later plan behind `HostOS` protocol)
- Test: `skills/agy-deep-research/scripts/tests/test_gemini_host.py`

**Interfaces:**
- Produces:
  - `DESKTOP_NAME = "agy-deep-research"`; `ALLOWED_FLAG_SWITCHES = ("--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding", "--disable-features=CalculateNativeWinOcclusion")`.
  - `build_launch_command(stub: str, profile_dir: str | None, flags: Sequence[str]) -> str` — `profile_dir=None` (D1) → no `--user-data-dir`; any flag not in `ALLOWED_FLAG_SWITCHES` → `ValueError`; quotes paths.
  - `@dataclass ProcInfo(pid: int, ppid: int, image: str, cmdline: str, created: float)`.
  - `parse_user_data_dir(cmdline: str) -> str | None` (handles `--user-data-dir=X`, quoted forms).
  - `is_gemini_image(image: str) -> bool` — case-insensitive `\Google\Gemini\app-<ver>\Gemini.exe` under any root.
  - `is_our_instance(p: ProcInfo, profile_dir: str, started_after: float | None = None) -> bool` — gemini image and canonical (normcase, normpath) user-data-dir equal; with `started_after`, `p.created >= started_after`.
  - `@dataclass LaunchCheck(window_on_our_desktop: bool, cmdline_has_profile: bool, local_state_updated: bool, exited: bool, elapsed_s: float)`.
  - `classify_launch(c: LaunchCheck) -> Literal["ok", "blocked", "pending", "timeout"]` — ok = window and cmdline and local_state; blocked = exited, or window present while not cmdline_has_profile; pending while `elapsed_s < 60`; else timeout.
  - `@dataclass Decision(mode: Literal["D2", "D1"] | None, code: str | None, reason: str | None)`.
  - `decide_mode(job_mode: str | None, d2: Literal["ok","blocked","timeout"], user_gemini_running: bool) -> Decision` — job_mode pinned: same mode required, `D2` with d2 != ok → `Decision(None, "HOST_MODE_UNAVAILABLE", ...)`; no job_mode: ok → D2; blocked → D1 if not user_gemini_running (reason `profile_switch_ignored`) else `GEMINI_IN_USE`; timeout → `HOST_START_TIMEOUT` (no fallback).
  - `clean_env(env: Mapping[str, str]) -> dict` — drops keys starting `NODE_` or `ELECTRON_`.

- [ ] **Step 1: Write failing tests**: `test_build_command_rejects_unlisted_switch`, `test_build_command_d1_has_no_switch`, `test_parse_user_data_dir_forms`, `test_user_app_with_other_profile_is_not_ours`, `test_pid_reuse_rejected_by_creation_time`, `test_classify_slow_start_is_pending_not_blocked`, `test_decide_blocked_falls_back_to_d1_only_without_user_app`, `test_decide_pinned_job_never_switches_mode`, `test_clean_env_drops_node_electron`.
- [ ] **Step 2: Run** `python -m unittest tests.test_gemini_host -v` → FAIL.
- [ ] **Step 3: Implement**.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(agy-deep-research): host mode decisions and launch command guard`.

### Task 5: static guards in `run_verification.py`

**Files:**
- Modify: `skills/agy-deep-research/scripts/tests/run_verification.py` (replace `check_port_unification` at :287; unit-test timeout at :365/:390)
- Test: `skills/agy-deep-research/scripts/tests/test_static_guards.py`

**Interfaces:**
- Produces: `check_forbidden_automation(skill_dir: Path) -> dict` (same result shape as other checks: `{"name", "passed", "errors": [...], "duration_s"}`), scanning `scripts/*.py` (not `tests/`) for `playwright`, `remote-debugging`, `connect_over_cdp`, `chrome_launcher`, `9223`, `9224`, `--browser chrome`, `app.asar`; and for any `--`-switch literal passed to the app other than the 4 allowed (regex on string literals in `gemini_host.py`). `check_no_app_dir_writes(skill_dir) -> dict`: fails if a script contains both a `Google\\Gemini` / `Google/Gemini` path literal and a write call (`open(` with `"w"`/`"a"`, `write_text`, `write_bytes`, `shutil.copy`, `os.replace`, `os.remove`) in the same function body (AST walk).
- Unit-test timeout: 300 s (measured baseline 88.6 s for 182 tests; one-line rationale comment).

- [ ] **Step 1: Write failing tests** (all on temp skill dirs): `test_flags_playwright_in_script`, `test_ignores_tests_dir`, `test_flags_unlisted_switch_literal`, `test_allows_the_four_switches`, `test_flags_write_to_app_dir`. The check on the real tree is Task 6's `test_current_tree_passes`. Until Task 6 lands, `run_verification.py` reports the new checks as FAIL on the real tree; that is expected for this one commit.
- [ ] **Step 2: Run** `python -m unittest tests.test_static_guards -v` → FAIL.
- [ ] **Step 3: Implement** the two checks, wire them into `run_all_verification` in place of the port check, raise the timeout.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `test(agy-deep-research): static guards against CDP/browser code and app-dir writes`.

### Task 6: remove patcher and Chrome mode; legacy jobs

**Files:**
- Delete: `scripts/patch_gemini_app.py`, `scripts/chrome_launcher.py`, `scripts/tests/test_patch_pattern.py`, `scripts/tests/test_patch_status.py`, `scripts/tests/test_chrome_mode.py`
- Modify: `scripts/deep_research.py` (constants `BUNDLED_SCRIPTS`, `BROWSERS`, `DEFAULT_BROWSER`, `APP_CDP_RECOVERY`, `CHROME_CDP_RECOVERY`, `read_patch_status`, `read_chrome_status`, `job_browser`, `chrome_cdp_url`, `apply_browser`, `--browser` options, preflight CDP checks), `scripts/relay_step.py` (FAILURES entries for CDP/Chrome/patch/playwright, `cdp_url`, playwright probe → `run_deep_research.py --step probe` call), `prompts/agy_relay_task.md` (`{cdp_recovery}` → fixed line "2. On any error code, report it without retrying."), `scripts/tests/fake_agy_dr.py` and existing tests only where they reference removed names
- Test: `scripts/tests/test_legacy_jobs.py`, `scripts/tests/test_static_guards.py::test_current_tree_passes`

**Interfaces:**
- Produces: new jobs carry `"host": "uia"` in `job.json`; `LEGACY_BROWSER_MODE` (exit 2, hint: start a new job) for `plan/edit/approve/collect/status` on a job without `"host": "uia"` or with a `browser` field. `BUNDLED_SCRIPTS = ("relay_step.py", "run_deep_research.py", "gemini_host.py", "uitree.py", "privacy.py", "locators.py", "locators.json")`. Relay request field `cdp_url` removed (ignored if present). Relay probe = driver `--step probe` returning `{"start_enabled": bool, "url": str}` (driver side implemented in the backend plan; until then relay maps a non-zero probe to `COLLECT_PROBE_FAILED`, existing behaviour).

- [ ] **Step 1: Write failing tests**: `test_legacy_chrome_job_refused`, `test_legacy_app_job_refused`, `test_new_job_has_host_uia`, `test_relay_task_prompt_has_no_patch_text`, `test_current_tree_passes`.
- [ ] **Step 2: Run** `python -m unittest tests.test_legacy_jobs tests.test_static_guards -v` → FAIL.
- [ ] **Step 3: Implement** removals and legacy refusal.
- [ ] **Step 4: Run** `python scripts/tests/run_verification.py` → all checks PASS (unit tests included).
- [ ] **Step 5: Commit** `refactor(agy-deep-research)!: drop app patcher and Chrome/CDP modes`.

### Task 7: confinement tightening (agy-delegate)

**Files:**
- Modify: `skills/agy-delegate/scripts/confine_hook.py` (`is_protected_file` deep-research list; command validation :297–:345 drop `patch_gemini_app.py`), `skills/agy-delegate/scripts/agy_task.py` (`DEEP_RESEARCH_FRAME` :153–:170 drop the patch sentence), `skills/agy-delegate/README.md`/`SKILL.md` lines that mention `patch_gemini_app.py`
- Test: `skills/agy-delegate/scripts/tests/test_confine.py` (update + new cases)

**Interfaces:**
- Produces: deep-research protected files += `job.json`, `relay/foreground.jsonl`; `run_command` allows only `relay_step.py --job <workdir>`.

- [ ] **Step 1: Write failing tests**: `test_deep_research_patch_script_denied`, `test_deep_research_job_json_write_denied`, `test_deep_research_foreground_log_write_denied`, `test_frame_has_no_patch_instruction`; change the existing stub-creation of `patch_gemini_app.py` to assert denial instead of allowance.
- [ ] **Step 2: Run** `python -m unittest discover -s skills/agy-delegate/scripts/tests -v` → FAIL on the new cases.
- [ ] **Step 3: Implement**.
- [ ] **Step 4: Run** → PASS (whole agy-delegate suite).
- [ ] **Step 5: Commit** `fix(agy-delegate): deep-research profile runs only relay_step; protect job.json`.

**Milestone M2 → X2** on Tasks 4–7.

### Task 8: `steps.py` — plan/edit step logic on UiTree

**Files:**
- Create: `skills/agy-deep-research/scripts/steps.py`
- Test: `skills/agy-deep-research/scripts/tests/test_plan_steps.py`, fixtures `tests/fixtures/plan_flow_ko.json` (FakeTree spec built from the measured S5/S6 sequence: fresh chat → DR on → input → send → URL bind after 2 snapshots → card with start/edit/cancel/retry → cancel → card stays), `plan_flow_en_synthetic.json`, `plan_flow_no_cancel.json` (card without cancel button; start disappears and stop_response appears after cancel attempt window), `plan_flow_edit_two_cards.json`

**Interfaces:**
- Consumes: Tasks 1–3 (`UiTree`, `Node`, `privacy.scrub/allowed_ids`, `locators.resolve/LocatorMiss/LocatorContext`).
- Produces:
  - `class StepError(Exception)` with `.code: str`, `.details: dict` (scrubbed).
  - `@dataclass StepConfig(poll_min_s: float = 0.1, card_timeout_s: float = 900, cancel_verify_s: float = 5, sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic)`.
  - `bind_conversation(tree, url: str) -> Node` (exact URL match on `documents()`; else `StepError("CONVERSATION_MISMATCH")`).
  - `ensure_fresh_chat(tree, locs, cfg) -> Node`.
  - `select_mode(tree, locs, cfg, wanted: str, thinking: str | None) -> str` — reads `mode_item` names at run time; unique case-insensitive substring match on `wanted` (`Neon`, `Hydrogen`, `카본`); 0 → `MODEL_NOT_AVAILABLE` with `available` list, >1 → `MODEL_AMBIGUOUS`; returns the mode-button name after selection. Alias map `{"pro": "Neon", "flash": "Hydrogen", "argon": "카본", "carbon": "카본"}` lives in `locators.json` (`"model_aliases"`).
  - `enable_deep_research(tree, locs, cfg) -> None` (`tools` expand → `more_tools` expand → `dr_item` toggle → `dr_on` present; else `DEEP_RESEARCH_TOGGLE_NOT_FOUND`).
  - `enter_prompt(tree, locs, cfg, doc, text: str) -> None` — requires `prompt_input` present (positive anchor) and `send` absent; `set_value`; wait for `send`; SHA-256(value) == SHA-256(text) else clear and `PROMPT_READBACK_MISMATCH`.
  - `send_and_bind(tree, locs, cfg, doc) -> str` (invoke `send`; returns `/app/<id>` URL once the same doc's value changes; prints nothing).
  - `wait_plan_and_cancel(tree, locs, cfg, url: str, prev_cards: int) -> dict` → `{"cancel": "cancel_button"|"edit_fallback", "plan_text": str, "cards": int}`; newest card = last `plan_card` in document order with enabled `plan_start` and count == prev_cards + 1; invoke `plan_cancel_countdown` inside it; verify for `cancel_verify_s` that `plan_start` stays enabled and no `stop_response`; if no cancel button → invoke `plan_edit` as second stop; if research progress appears (start gone + `stop_response`) → invoke `stop_response` then `StepError("UNREVIEWED_START")`; still not stopped → `StepError("COUNTDOWN_NOT_CANCELLED")`; safety phrase in the newest response → `SAFETY_BLOCKED`; error phrase → `GEMINI_ERROR_RESPONSE`.
  - `extract_plan_text(card: Node, allowed: set[str]) -> str` — Text and heading nodes in order, `#`×heading, consecutive duplicates dropped, scrubbed.
  - `edit_plan(tree, locs, cfg, url: str, feedback: str) -> dict` — invoke `plan_edit`, wait for canned turn (no `stop_response`, `prompt_input` present), `enter_prompt`, `send`, `wait_plan_and_cancel(prev_cards=<cards before>)`.

- [ ] **Step 1: Write failing tests**: `test_plan_flow_ko_cancels_and_extracts`, `test_plan_flow_en_synthetic`, `test_empty_check_requires_input_anchor`, `test_readback_mismatch_clears_and_fails` (fake returns value with an extra leading char), `test_no_cancel_button_uses_edit_fallback`, `test_unreviewed_start_is_stopped_and_reported`, `test_edit_targets_newest_card_only`, `test_model_unique_match_and_ambiguous`, `test_model_not_available_lists_menu`, `test_gemini_error_response_code` (phrase `요청하신 작업을 처리하는 중에 오류가 발생했습니다`), `test_label_drift_raises_locator_miss_and_override_recovers`, `test_no_account_string_in_any_output` (run the ko flow with the privacy fixture's sidebar merged in; `json.dumps` of every return value and StepError details contains neither `test@example.invalid` nor `비밀 대화 제목`).
- [ ] **Step 2: Run** `python -m unittest tests.test_plan_steps -v` → FAIL.
- [ ] **Step 3: Implement** `steps.py` (injected `cfg.sleep`/`cfg.clock` so tests run instantly).
- [ ] **Step 4: Run** `python scripts/tests/run_verification.py` → PASS.
- [ ] **Step 5: Commit** `feat(agy-deep-research): plan/edit step logic with fail-closed countdown handling`.

**Milestone M3 → X2** on Task 8.

## Out of scope (later plans, after risk 1 is resolved)

`uia_backend.py` (comtypes), Win32 half of `gemini_host.py` (desktop create/open, launch, quit, orphan enumeration, lock file), `run_deep_research.py` rewrite and stdout protocol wiring, approve/collect/long-wait/recovery loop, `report_md.py`, doctor/signin/app CLI, foreground tracer process, SKILL.md/references rewrite.

# agy-deep-research: 앱 무수정 · 숨은 데스크톱 · UI Automation 드라이버 설계

작성 2026-11-26. 근거 자료: `C:\Users\SSAFY\w\DEV_PROMPT.md`(지시서, 사용자 승인), `C:\Users\SSAFY\w\PROGRESS.md`(결정 기록), 스파이크 결과 `C:\Users\SSAFY\w\spike\PHASE0_G1.md`, `PHASE0_G1b.md`, `live_*.json`.
이 문서는 이 브랜치(`feat/agy-deep-research-uia`)의 구현 계획을 세울 빌더를 위한 것이다.

## 1. 목적과 이유

`agy-deep-research`는 Claude가 오케스트레이터가 되어 사용자의 Gemini 구독으로(API 키 없이) Gemini Deep Research를 돌리고, 계획과 보고서를 `goal.md` 기준으로 검수하는 스킬이다. 지금은 Gemini 데스크톱 앱의 `app.asar`를 패치해 CDP 포트를 열거나 Chrome을 디버깅 포트로 띄운다. 앱이 업데이트될 때마다 패치를 손으로 고쳐야 하고, 앱 1.21.1부터는 ASAR 무결성 fuse가 켜져 있어 패치 방식 자체가 막혔다. 디버깅 포트는 로그인된 Google 세션 전체를 로컬 프로그램에 노출한다.

**목표 상태**: 설치된 앱을 한 바이트도 고치지 않고, 디버깅 포트·브라우저 없이, 사용자의 화면·키보드·포커스를 건드리지 않고(숨은 Windows 데스크톱) Deep Research 한 건을 끝까지 돌린다. 앱 업데이트로 깨질 수 있는 지점은 "화면에 보이는 접근성 라벨"뿐이고, 라벨은 데이터 파일로 관리하며 어긋나면 스킬이 스스로 진단해 고칠 조각을 제시한다.

## 2. 목표 · 비목표 · 반목표

목표
- 무수정 앱 + Windows UI Automation(COM `IUIAutomation`)으로 `preflight → init → plan → edit → approve → collect` 전체 흐름.
- 숨은 데스크톱(`WinSta0\agy-deep-research`)의 전용 프로필 인스턴스가 기본(D2), 기존 프로필(D1)은 자동 후퇴.
- 위 2개 층(`deep_research.py`의 게이트·job 구조, agy 릴레이·confinement)과 드라이버 stdout 프로토콜(`[CONVERSATION]`, `=== [PLAN_REVIEW_HOOK_START] ===`, `[REPORT_PROVENANCE]`, 보고서 블록)은 그대로 유지.
- 라벨 변경에 대한 자가 진단(`doctor`, `UI_LOCATOR_MISS` + 재정의 조각).

비목표
- macOS·Linux 지원(`UNSUPPORTED_PLATFORM`).
- 여러 job 동시 구동(지금처럼 한 번에 한 job이 앱을 몬다).
- Gemini 웹(gemini.google.com 브라우저) 구동.

반목표 (기술적으로 돌아가도 실패로 본다)
- 앱 디렉터리(`%LOCALAPPDATA%\Google\Gemini\`)에 쓰기, 디버깅 포트·브라우저 프로필 자동화.
- 사용자 기본 데스크톱의 포그라운드·포커스 변경, 사용자 키 입력이 Gemini로 새는 일.
- 검토되지 않은 계획이 연구를 시작하는 일.
- 바인딩된 테스트/작업 대화 밖의 텍스트(사이드바 대화 제목, 계정 이름·이메일)가 로그·스냅샷·결과·픽스처·agy 입력에 들어가는 일.

## 3. 제약 (지시서 R1–R10, 2026-11-26 수정 반영)

- **R1 앱 무수정**: 스킬 코드는 `%LOCALAPPDATA%\Google\Gemini\` 아래에 쓰지 않는다. 앱 실행은 런처 스텁 `%LOCALAPPDATA%\Google\Gemini\Gemini.exe`를 **스위치 없이**, 또는 **`--user-data-dir=<스킬 전용 경로>` 하나만** 붙여 호출한다(G1b 사용자 승인). 그 외 스위치 금지. 스위치가 무시·차단되면 감지해 D1로 후퇴하고 결과 JSON에 사유를 남긴다. D1은 사용자 Gemini가 실행 중이면 조작하지 않고 거부한다. `patch_gemini_app.py`, `SECURITY_GUARD_RE`, 패치 상태 파일, 관련 테스트·문서·코드(`ASAR_PATTERN_MISMATCH` 등) 제거.
- **R2 포트·브라우저 없음**: `playwright`, CDP, `remote-debugging-*`, `chrome_launcher.py`, `--browser chrome` 제거. `run_verification.py`의 정적 검사가 재유입을 막는다. 기존 `browser: chrome|app` job은 `LEGACY_BROWSER_MODE`.
- **R3 UIA 드라이버**: comtypes 1개(버전 고정 `comtypes==1.4.11`), 전용 venv `%LOCALAPPDATA%\agy-deep-research\venv`. `UiTree` 추상화 + 실제 UIA 백엔드 + JSON 스냅샷 Fake 백엔드.
- **R4 업데이트 내성**: `locators.json` + 사용자 재정의 `%LOCALAPPDATA%\agy-deep-research\locators.local.json`, `doctor`, `UI_LOCATOR_MISS`.
- **R5 안전 게이트 보존**, 카운트다운 취소 실패 시 fail-closed(`UNREVIEWED_START`).
- **R6 (수정)**: 숨은 데스크톱 구동이 기본. **불변식: 기본 데스크톱의 포그라운드를 바꾸지 않는다.** 테스트·`doctor`·각 단계에서 계측한다. 포커스 허용안(G1 선택지 A)은 채택하지 않았다. 클립보드를 쓰는 보조 경로는 기본 꺼짐, 쓰면 결과 JSON에 기록.
- **R7 프라이버시**(§13), **R8 confinement 축소**, **R9 문서 동기화**, **R10 Windows 전용**.

## 4. 접근 방식

| 안 | 내용 | 판정 |
|---|---|---|
| CDP 계열(asar 패치, Chrome 디버깅 포트, 내장 브라우저, NODE_OPTIONS 등) | 지시서 §4 | 기각(사용자 제약, fuse) |
| A. 보이는 창 + UIA, 입력 단계만 포커스 허용 | `SetValue`가 적용되려면 창이 보여야 하고 적용 순간 포그라운드를 가져감. 실측에서 사용자 키 입력이 섞임 | 기각(G1b에서 D로 대체) |
| B. 엄격 R6, 사용자가 직접 붙여넣기 | 자동화 가치 상실 | 기각 |
| C. 별도 사용자 세션/VM | 설정 부담 큼 | 기각 |
| D1. 같은 로그온 세션의 숨은 데스크톱 + 사용자 기존 프로필(스위치 없음) | 실측 GO. job 동안 사용자는 앱을 못 씀(앱 아이콘을 눌러도 숨은 인스턴스로 신호만 감) | **자동 후퇴 경로** |
| D2. 숨은 데스크톱 + 전용 프로필(`--user-data-dir`) | 실측 GO. 사용자 앱과 공존(락이 프로필별). 1회 로그인 필요 | **주 경로(사용자 선택)** |

D가 G1의 문제(포그라운드 탈취, 키 입력 섞임, 도구 메뉴 `Collapse()` 거부)를 실측으로 모두 해결했다. D2는 사용자가 평소 Gemini를 쓰는 동안에도 job이 돌고 비용은 1회 로그인뿐이어서 주 경로가 됐다.

## 5. Phase 0 · G2 실측 사실 (설계가 기대는 것)

| 사실 | 근거 |
|---|---|
| 창은 `Chrome_WidgetWin_1` + 프로세스 이미지 `...\Google\Gemini\app-*\Gemini.exe`. 문서는 AutomationId `RootWebArea`, ValuePattern 값이 URL. 한 창에 문서 여러 개(대화 1, `app://bundle/src/top_bar/top_bar.html`, `blob:` iframe, 때로 `crash_reporting_dialog.html`, `settings.html`) | s1_out, desk_d2_inspect |
| 바인딩 문서 3,627노드를 CacheRequest 1회로 0.57초. AriaRole·AriaProperties(`level=`, `expanded=`, `checked=`, `pressed=`, `haspopup=`)·HeadingLevel(80051–80056)이 읽힘 | s1_out |
| 새 대화: "새 채팅" Hyperlink Invoke → 같은 Document 요소의 URL이 `/app/<id>`→`/app`. 전송 후 `/app`→`/app/<16hex>`(S5: 전송 후 13.2초) | s1c_out, live_s5 |
| 빈 입력창 Value = 플레이스홀더(`Gemini에게 물어보기\n`). 비었음 판정은 **전송 버튼("메시지 보내기") 부재**로 | s3_out |
| `ValuePattern.SetValue`: 개행·빈 줄·탭·XML·CDATA·이모지·12,013자 SHA 일치, Quill 모델 반영(전송 버튼 등장). 숨은 데스크톱에서 섞인 글자 0 | s3_results, desk_d1_d1, live_s5 |
| 보이는 데스크톱에서 SetValue는 포그라운드를 가져가고, 최소화·숨김 중에는 적용되지 않음(복원 시 큐에서 적용). → 숨은 데스크톱이 필수 | s2, s3c |
| 숨은 데스크톱: 창 생성, UIA 트리, 바인딩, SetValue, 도구 메뉴 Expand/Collapse, 메뉴 항목 Toggle 모두 정상. 기본 데스크톱 Gemini 포그라운드 이벤트 0 | desk_d1_*, live_s4/s5, fgwatch |
| `--user-data-dir` 존중. 단일 인스턴스 락은 프로필별(D1과 D2 동시 실행). D1 실행 중 사용자가 앱을 켜면 화면에 아무것도 안 뜸 | desk_d1_out |
| 숨은 데스크톱은 만든 프로세스가 끝나도 Gemini가 붙어 있는 동안 유지. 다른 프로세스가 `OpenDesktopW(이름)`로 다시 열고, 드라이버는 메인 스레드에서 comtypes import 전에 `SetThreadDesktop`하면 UIA 사용 가능 | life_out, std_probe |
| 다른 데스크톱의 창에 `PostMessage(WM_CLOSE)` → 오류 1400. 종료는 **그 데스크톱에 있는 프로세스가** 앱 메뉴 `파일 → 종료 Ctrl+Q`를 UIA Invoke(또는 같은 데스크톱에서 WM_CLOSE). 인스턴스가 끝나면 데스크톱 객체 자동 소멸 | life_out |
| 고아 탐지: `EnumDesktopsW` → `EnumDesktopWindows` → Gemini pid → 명령줄의 `--user-data-dir`로 프로필 판별 | life_out |
| agy 릴레이(`--deep-research`, confinement on) 아래 프로세스도 숨은 데스크톱 생성·Gemini 실행·UIA·종료 가능. 단 **agy는 명령을 자체 숨은 데스크톱(`exebox-*`)에서 실행**하므로 그 안에서의 포그라운드 측정은 무의미 | s9_job/relay/result.json |
| 모드 메뉴: `menuitemradio` + `checked=`: 자동 / 일상생활 지원 4 Hydrogen / 심층 분석 4 Neon / 코드 및 복잡한 작업 4 카본 + `사고 수준`(submenu). 모드 버튼 이름 "모드 선택 도구 열기, 현재 <X> 모드 사용 중" | live_s4 |
| Deep Research: 도구(`업로드 및 도구`) → `도구 더보기`(submenu) → `Deep Research`(menuitemcheckbox, Toggle). 켜지면 `Deep Research 선택 해제` 버튼 | desk_d1_d1, live_s4 |
| 계획 카드: 전송 후 34.6초에 등장. 버튼 `연구 계획 수정` / `연구 시작` / `취소`(카운트다운 취소) / `Deep Research 사용하지 않고 다시 시도하기`. 다른 대화(G1)에서는 `Edit the research plan` / `Start research`(영어) → 같은 계정에서도 언어가 섞인다 | live_s5, s1_out |
| 카운트다운: 남은 시간 텍스트는 트리에 없음. `취소` Invoke(호출 반환까지 2.0초) 후 20초간 `연구 시작` 활성 유지 | live_s5 |
| 계획 텍스트: 카드 하위 Text 노드 순회로 제목(H3)·번호 단계·"연구 웹사이트/결과 분석/보고서 생성" 단계 표시까지 추출 | live_s5 |
| S6 계획 수정: `연구 계획 수정` Invoke → Gemini 정형 턴("현재 계획이에요. 변경하고 싶은 부분이 있나요?") → 피드백 SetValue·전송까지 정상. 그러나 숨은 창에서 수정 계획 위젯이 `조사 계획 생성 중`에 5분 넘게 멈춤. 다시 들어가 보니 **서버에는 수정 계획이 완성돼 있었다** → 원인은 Gemini 지연이 아니라 숨은 창의 화면 갱신 정지 | tail.json, live_reenter |
| 앱 메뉴 `보기 → 새로고침`은 대화가 아니라 **새 채팅(`/app`)으로 간다**. 대화 복귀는 사이드바 Hyperlink 중 **ValuePattern URL이 `/app/<id>`로 끝나는 것**을 Invoke(이름은 읽지 않음) → 2.2초 | live_s6resume, live_reenter |
| 다시 불러온 계획 카드에는 카운트다운(`취소`)이 없고, 이전 카드의 `연구 시작`은 비활성, 새 카드만 활성. 카운트다운은 화면이 실시간으로 받은 카드에만 붙는 것으로 보인다 | live_reenter |
| 승인: `연구 시작` Invoke → 5초 안에 `대답 생성 중지`, `생각하는 과정 표시`, `패널 닫기`(보고서 패널 자동으로 열림), 상태 문구 "웹사이트 N개 조사 중..." | live_s7start, live_monitor2 |
| S7 장시간 대기: 상태 문구가 12:04:40 "조사 시작 중" → 12:04:50 "웹사이트 32개 조사 중"에서 멈춤. 창 2px 리사이즈는 매번 무효. 첫 새로고침+재진입(12:07:39)에서 "45개"로 점프한 뒤로는 새로고침을 반복해도(약 1분 간격, 12:38까지 34분) **"웹사이트 45개 조사 중"에서 변하지 않음**. 생각하는 과정 Expand해도 새 내용 없음. 서버 정지인지 지연인지는 숨은 데스크톱 안에서 판별 불가(보이는 창 비교는 사용자 방해라 금지) | live_monitor2_run1/run2, docs2, thoughts1 |
| 새로고침 뒤 사이드바가 닫힌 상태면 대화 목록이 늦게 로드됨 → 재진입은 열기 후 최대 15초 재시도 필요 | live_monitor2_run1 |
| S8 보고서 트리 | **연구 완료 전이라 미측정** |

## 6. 구조

```
Claude Code
 └─ scripts/deep_research.py   (stdlib, 기본 데스크톱에서 실행)  게이트·job·preflight·setup·doctor·signin·app
      │  각 단계 동안 FgTracer로 기본 데스크톱 포그라운드 계측(R6 불변식)
      └─ agy_task.py run --deep-research → agy(exebox 데스크톱) → scripts/relay_step.py --job <dir>  (stdlib)
            └─ venv python scripts/run_deep_research.py --step ...   (comtypes, 드라이버)
                 ├─ gemini_host.py : 숨은 데스크톱 열기/만들기, 인스턴스 찾기/띄우기/종료, D2/D1 결정 (ctypes, stdlib)
                 ├─ uitree.py      : UiTree 인터페이스, Node, FakeTree(JSON 스냅샷 + 스크립트 전이)
                 ├─ uia_backend.py : COM IUIAutomation 백엔드 (CacheRequest 스냅샷, 패턴 호출)
                 ├─ locators.py + locators.json : 로케이터 엔진과 데이터, 재정의 병합, UI_LOCATOR_MISS 힌트
                 ├─ privacy.py     : 허용 영역 마스크, 정규식 백스톱, 출력 전 스크럽
                 └─ report_md.py   : 보고서 트리 → GFM Markdown (순수 함수)
```

제거: `patch_gemini_app.py`, `chrome_launcher.py`, `test_patch_pattern.py`, `test_patch_status.py`, `test_chrome_mode.py`, `references/app-mode.md`의 패치 설명(→ UIA·숨은 데스크톱 설명으로 교체), playwright 관련 코드·힌트·오류 코드(`CDP_*`, `CHROME_*`, `PLAYWRIGHT_MISSING`, `ASAR_PATTERN_MISMATCH`).

`setup` 복사 목록(`BUNDLED_SCRIPTS`)과 해시 비교 대상은 새 파일 목록(relay_step.py, run_deep_research.py, gemini_host.py, uitree.py, uia_backend.py, locators.py, locators.json, privacy.py, report_md.py)으로 바꾼다. agy 쪽 스킬 디렉터리에도 같은 파일이 간다(릴레이가 그 디렉터리의 드라이버를 실행).

## 7. 인스턴스 모델 (gemini_host.py)

- **이름 고정**: 데스크톱 `agy-deep-research`(WinSta0), 전용 프로필 `%LOCALAPPDATA%\agy-deep-research\gemini-profile`. 상태 파일 `%LOCALAPPDATA%\agy-deep-research\host.json`(mode D2|D1, browser pid, started_at, last_job, last_step_at, app_version). 앱 디렉터리 밖이라 R1과 무관.
- **확보(ensure)**: (1) 데스크톱 `agy-deep-research`가 있고 그 위에 Gemini 창이 있으면 재사용. (2) 없으면 데스크톱을 만들고 D2로 스텁 + `--user-data-dir` 실행. (3) **스위치 확인**: 15초 안에 (a) 그 데스크톱에 Gemini 주 창이 생기고 (b) 브라우저 프로세스 명령줄에 그 경로가 있고 (c) 프로필 폴더의 `Local State`가 생성·갱신되어야 한다. 하나라도 아니면(가드로 종료, 기본 프로필로 넘어감, 창이 기본 데스크톱에 뜸) 사유를 남기고 D1 후보로 간다. (4) **D1**: 사용자 데스크톱에 Gemini 프로세스가 하나라도 있으면 `GEMINI_IN_USE`로 거부(조작 안 함). 없으면 스위치 없이 숨은 데스크톱에 실행. (5) 로그인 확인: 바인딩 문서에 계정 컨트롤이 있고 `로그인` 버튼이 없어야 한다. 아니면 `NOT_SIGNED_IN`(D2면 `signin` 안내).
- **수명**: plan → edit → approve → collect는 서로 다른 드라이버 프로세스다. 인스턴스와 데스크톱은 단계 사이에 유지되고(실측), 각 드라이버는 이름으로 다시 연다. job이 `completed`/`error`로 끝나고 진행 중인 다른 job이 없으면 `collect`가 마지막에 인스턴스를 닫는다(`--keep-app`으로 유지). 그 외에는 `app close`로 닫는다.
- **고아**: `doctor`와 `preflight`가 `EnumDesktopsW`로 인스턴스를 찾아 표시한다(데스크톱, pid, 프로필, 마지막 단계 시각, 연결된 job). 활성 job이 없는데 살아 있는 인스턴스는 `orphan: true`, `preflight` 경고 + 종료 명령 안내.
- **종료(app close)**: 그 데스크톱으로 옮긴 스레드에서 앱 메뉴(`메인 메뉴` → `파일` → `종료`)를 UIA Invoke → 10초 대기 → 같은 데스크톱 스레드에서 주 창 WM_CLOSE → 10초 → 마지막으로 그 데스크톱의 Gemini 창을 가진 pid 트리만 TerminateProcess(사용자 인스턴스는 다른 데스크톱이므로 대상이 아님). 끝나면 남은 프로세스 0과 데스크톱 소멸을 확인해 출력.
- **보이는 모드**: `signin`(D2 전용 프로필을 기본 데스크톱에 보이게 띄우고 사용자가 직접 로그인·크래시 보고 동의 처리, 로그인 감지 후 정상 종료. 로그인 폼에는 아무것도 입력하지 않음). 디버그용 `--show`(인스턴스를 기본 데스크톱에 띄움). 보이는 모드는 R6 불변식 계측을 끄고 결과에 `visible_mode: true`를 기록한다.
- **한 번에 한 job**: 기존 게이트(대화 소유, Conversation Identity Gate)를 유지한다. 인스턴스가 하나이므로 두 job의 단계가 겹치면 지금처럼 거부한다.

## 8. 드라이버 실행

- 릴레이(시스템 Python, agy의 exebox 데스크톱)는 venv 인터프리터로 드라이버를 띄운다. venv가 없거나 comtypes import가 실패하면 `UIA_UNAVAILABLE`(+ `setup` 안내).
- 드라이버의 첫 동작: `OpenDesktopW("agy-deep-research")`(없으면 gemini_host가 생성) → **메인 스레드 `SetThreadDesktop`** → 그 다음 comtypes import. 이 순서는 실측으로 확인했다(comtypes가 STA 숨은 창을 만들기 전에 옮겨야 함). Gemini 실행은 `CreateProcessW`의 `STARTUPINFO.lpDesktop="WinSta0\agy-deep-research"`. stdout/stderr는 기존처럼 릴레이가 파이프로 받는다.

## 9. UiTree와 백엔드

- `Node`: `ct`(컨트롤 종류), `role`(AriaRole), `name`, `aria`(AriaProperties dict), `heading`(1–6|None), `enabled`, `offscreen`, `value`(Value 패턴 값), `automation_id`, `patterns`(I/V/T/E/G/L), `children`.
- `UiTree`: `windows()`, `documents(window)`, `snapshot(root)`, `invoke(node)`, `toggle(node)`, `expand(node)`, `collapse(node)`, `default_action(node)`, `set_value(node, text)`, `get_value(node)`, `text(node)`. 백엔드는 패턴을 `Invoke → Toggle → LegacyIAccessible.DoDefaultAction → ExpandCollapse` 순으로 시도하는 공통 `act()`를 제공한다(실측: 도구·모드 버튼은 Expand, Deep Research는 Toggle, 일반 버튼은 Invoke).
- `UiaBackend`: CacheRequest 한 번으로 서브트리 스냅샷(실측 수천 노드 1초 미만). 모든 탐색은 바인딩된 대화 문서 하위. 앱 메뉴·팝업만 데스크톱 루트에서 찾는다.
- `FakeTree`: JSON 스냅샷(+ "이 노드를 Invoke하면 다음 스냅샷은 X" 같은 전이 스크립트)으로 단계 로직을 Windows·Gemini 없이 테스트한다. 픽스처는 이 작업이 만든 테스트 대화에서만 녹화하고(§13), en 픽스처는 ko 녹화본의 라벨을 치환한 **합성본**으로 표시한다(언어 설정 변경 금지).

## 10. 로케이터 (locators.json)

```json
{
  "version": 1,
  "locators": {
    "prompt_input":   {"ct": "Edit", "role": "textbox", "names": ["Gemini 프롬프트 입력", "Enter a prompt for Gemini", "re:.*(프롬프트|prompt).*"], "scope": "main", "unique": true},
    "send":           {"ct": "Button", "names": ["메시지 보내기", "Send message"], "scope": "composer"},
    "stop_response":  {"ct": "Button", "names": ["대답 생성 중지", "Stop response"], "scope": "main"},
    "tools":          {"ct": "Button", "names": ["업로드 및 도구", "Upload and tools"], "scope": "composer"},
    "more_tools":     {"ct": "MenuItem", "names": ["도구 더보기", "More tools"], "scope": "popup"},
    "dr_item":        {"ct": "CheckBox", "role": "menuitemcheckbox", "names": ["Deep Research"], "scope": "popup"},
    "dr_on":          {"ct": "Button", "names": ["Deep Research 선택 해제", "Deselect Deep Research"], "scope": "composer"},
    "mode_button":    {"ct": "Button", "names": ["re:모드 선택 도구 열기.*", "re:Open mode picker.*"], "scope": "composer"},
    "mode_item":      {"ct": "RadioButton", "role": "menuitemradio", "scope": "popup"},
    "new_chat":       {"ct": "Hyperlink", "names": ["새 채팅", "New chat"], "scope": "document"},
    "sidebar_open":   {"ct": "Button", "names": ["사이드바 열기", "Open sidebar"]},
    "plan_start":     {"ct": "Button", "names": ["연구 시작", "Start research"], "scope": "plan_card"},
    "plan_edit":      {"ct": "Button", "names": ["연구 계획 수정", "Edit the research plan", "Edit plan"], "scope": "plan_card"},
    "plan_cancel_countdown": {"ct": "Button", "names": ["취소", "Cancel"], "scope": "plan_card"},
    "app_menu":       {"ct": "Button", "names": ["메인 메뉴", "Main menu"], "scope": "top_bar"},
    "app_menu_file":  {"ct": "MenuItem", "names": ["파일", "File"], "scope": "desktop"},
    "app_menu_quit":  {"ct": "MenuItem", "names": ["re:종료.*", "re:(Quit|Exit).*"], "scope": "desktop"}
  }
}
```

- 이름 매칭: 공백 정규화 후 정확 일치, `re:` 접두는 전체 일치 정규식. `scope`는 구조적 앵커(`main`, `composer` = 입력창의 가장 가까운 공통 조상 그룹, `plan_card` = 시작 버튼과 수정 버튼을 함께 품은 가장 작은 조상, `popup`, `top_bar` 문서, `desktop` 루트).
- 재정의: `locators.local.json`이 키 단위로 덮어쓴다(병합 결과의 출처를 doctor가 표시). 스킬 코드 수정 없이 라벨 변경을 흡수한다.
- `UI_LOCATOR_MISS`: `{"locator": key, "scope": ..., "candidates": [{"name", "ct", "role", "similarity"}…상위 5], "override_snippet": {"locators": {key: {...기존 + 후보 이름 추가}}}}`. 후보 이름은 허용 영역(§13) 안의 노드에서만 뽑는다. Claude는 사용자 동의 후 재정의 파일만 고쳐 재시도한다.
- 버전 정보(`app-<version>`)는 진단(doctor 자동 실행 조건, 출력)에만 쓰고 동작 분기에 쓰지 않는다.

## 11. 단계별 동작

공통: 각 단계 시작 시 인스턴스 확보 → 대화 바인딩(Conversation Identity Gate, URL이 job의 `/app/<id>`와 같은 문서만) → 표면 검사(§12) → 동작 → 표면 검사 → stdout 프로토콜 출력.

1. **새 대화**(plan): 사이드바가 닫혀 있으면 `sidebar_open` Expand → `new_chat` Invoke → URL이 정확히 `/app`가 될 때까지 대기 → 사이드바 원복. 이미 `/app`이면 생략.
2. **모델·사고 수준**: `mode_button` Expand → 팝업의 `menuitemradio` 이름을 **실행 시점에 읽어** 요청 모델(job의 `gemini_model`, 기본 "Neon"?—§16 빌더 확인)과 정규화 부분 일치로 매칭. 없으면 `MODEL_NOT_AVAILABLE` + 실제 목록. 사고 수준은 `사고 수준` 하위 메뉴에서 같은 방식. 선택 후 `checked=true` 확인, 모드 버튼 이름 재확인. 이름은 코드에 박지 않는다.
3. **Deep Research 켜기**: `dr_on`이 있으면 생략. 아니면 `tools` Expand → `more_tools` Expand → `dr_item` Toggle → `dr_on` 등장 확인(5초). 실패 `DEEP_RESEARCH_TOGGLE_NOT_FOUND`(기존 코드, 1회 재시도 유지).
4. **프롬프트 입력·전송**: `send`가 없어야 빈 입력창(아니면 `INPUT_NOT_EMPTY`). `SetValue(prompt)` → `send` 등장 대기 → Value의 SHA-256 == 원문 SHA-256 확인(불일치 `PROMPT_READBACK_MISMATCH`, 지우고 전송 안 함) → `send` Invoke → `Prompt submitted.` 출력. 키보드(Enter) 경로는 없다.
5. **바인딩**: 같은 문서 요소의 URL이 `/app/<id>`로 바뀌면 `[CONVERSATION]` 출력, 이후 모든 단계는 이 URL로만 바인딩.
6. **계획 대기와 카운트다운 취소(R5)**: 100ms 간격 스냅샷. 새 `plan_start`(활성)가 이전 개수보다 늘면 즉시 같은 `plan_card` 안의 `plan_cancel_countdown`을 Invoke. 그 후 `plan_cancel_countdown`이 사라지고 `plan_start`가 활성으로 남는지 5초 확인. **fail-closed**: 취소 버튼을 못 찾거나 확인 실패 → 연구 시작 여부 판정(§11-9 진행 신호) → 시작됐으면 `stop_response`가 있으면 Invoke하고 `UNREVIEWED_START`로 보고, 시작 안 됐으면 `COUNTDOWN_NOT_CANCELLED`로 보고하고 계획은 넘기지 않는다. 프롬프트가 안전 필터에 막힌 응답(문구 목록)은 기존 `SAFETY_BLOCKED`.
7. **계획 추출**: `plan_card` 하위를 문서 순서로 순회, Text·heading만, 제목은 `#`×레벨, 연속 중복 제거. "더보기" 류 버튼이 카드 안에 있으면 Invoke 후 다시 읽는다. `=== [PLAN_REVIEW_HOOK_START] ===` 블록으로 출력(필드는 기존과 같음).
8. **계획 수정**(edit): 바인딩 확인 → `plan_edit` Invoke → Gemini의 정형 응답 턴이 끝날 때까지(`stop_response` 없음, 입력창 있음) 대기 → 4번과 같은 입력·검증·전송 → 6–7번(이전 카드 개수 기준: **활성** `plan_start` 수가 늘거나, 비활성이 된 이전 카드 외에 새 카드가 생김).
   - **화면 정지 복구(실측 필수)**: 새 카드가 기준 시간(기본 90초) 안에 안 보이고 `stop_response`도 없으며 마지막 턴이 "생성 중" 문구에 머물면, 서버는 끝났는데 숨은 창이 갱신을 멈춘 것으로 본다. 복구 = §11-12 절차(새로고침 + URL 재진입). 재진입 후 새 카드가 있으면 카운트다운 검사(있으면 즉시 취소) 후 추출한다.
12. **복구 절차(공통)**: 앱 메뉴 `보기 → 새로고침` → 문서가 `/app`(새 채팅)로 바뀜을 확인 → 사이드바(닫혀 있으면 열기) Hyperlink 중 URL 값이 job의 `/app/<id>`로 끝나는 것만 Invoke(이름 미판독·미기록) → **URL 재바인딩 확인**(같지 않으면 `CONVERSATION_MISMATCH`) → 사이드바 상태 원복. 복구는 매번 결과에 `recoveries: [{at, why, ok}]`로 남긴다. 재진입 직후에도 카운트다운이 있는 카드가 보이면 즉시 취소한다.
9. **승인**(approve): 바인딩 확인 → `plan_start` Invoke → 진행 신호 확인 → `Clicking '연구 시작'` 출력(릴레이 호환 문자열 유지). **진행·완료 신호는 S7 결과로 채움.**
10. **완료 대기·추출**(approve/collect): 진행 신호 폴링(간격 5초), 상태 문구를 `[Ns] Progress: ...`로 출력(릴레이 PROGRESS_RE 호환). 완료 판정 후 `report_md`로 변환, `[REPORT_PROVENANCE]` + 보고서 블록 출력. 정지 상태가 오래가면 앱 메뉴 `보기 → 새로고침`(UIA Invoke)으로 복구(기존 60초 reload 대응). **S7·S8 결과로 채움.**
11. **보조 경로(복사 버튼)**: 기본 꺼짐(`--copy-check`). 클립보드는 창 스테이션 공유라 사용자의 클립보드를 덮는다: 텍스트·HTML 형식만 있을 때만 수행, 전후 저장·복원, 결과에 `clipboard_used: true`. 트리 경로와 문자 커버리지 비교 값을 결과에 남긴다.

## 12. 표면·창 감지

- 주 창의 문서 목록에서 허용(바인딩 대화, `top_bar.html`, `blob:`) 밖의 문서가 생기면 `UNEXPECTED_SURFACE`(예: `crash_reporting_dialog.html`, `settings.html`). 크래시 보고 동의는 `signin`에서 사용자가 처리하므로 job 중에 뜨면 멈춘다.
- 데스크톱의 최상위 창(EnumDesktopWindows): 주 창, 소유 팝업(메뉴), UAC 입력 표시기 외의 새 창이 생기면 `UNEXPECTED_SURFACE`.
- 외부 링크·OAuth 팝업: 드라이버는 링크를 Invoke하지 않는다. 숨은 데스크톱에서 새 브라우저가 뜨면 사용자에게 보이지 않으므로 위 감지로 멈추고 사용자에게 알린다.

## 13. 프라이버시 (R7)

**사고와 대책.** G2 S5에서 스파이크 로거가 "사이드바(navigation) 안이면 가린다"는 규칙만 썼는데, 계정 링크(`Google 계정: 이름(이메일)`)는 navigation이 아니라 사이드바 `region` 안에 있어서 이름·이메일 1건이 로컬 스파이크 로그에 기록됐다(커밋·외부 전송 없음, 즉시 가림, G-X 보고). 차단 목록식 가림은 라벨·구조가 바뀌면 다시 샌다. 그래서 제품 코드는 **허용 영역 방식**을 쓴다.

- 허용 영역 마스크(privacy.py): 노드 이름을 기록해도 되는 것은 `main` 랜드마크 안이면서, `navigation/banner/contentinfo/complementary/search` 하위가 아니고, **navigation을 품은 `region`(사이드바: 대화 목록·계정) 하위가 아닌** 노드뿐이다(실측: 계정 링크 = main>region(사이드바)>…, 계획 카드 = main>…>`model-response-message-content*`, 입력창 = main 안). 그 밖의 노드는 (컨트롤 종류/역할) 개수만 남긴다. 바인딩되지 않은 문서는 아예 읽지 않는다.
- 백스톱: 모든 출력(stdout 프로토콜, result.json, 진단 스냅샷, 로그)은 마지막에 `scrub()`을 지난다. 짧은 문자열은 `@|계정|Account|프로필|Profile`이면 통째로 가림, 긴 본문은 이메일·`Google 계정:` 구간만 치환.
- 진단 스냅샷(`doctor --snapshot`)은 기본적으로 Text/ListItem/Hyperlink 이름도 가린다.
- 테스트: 가짜 계정 컨트롤(`Google 계정: 테스트(test@example.invalid)`)과 가짜 사이드바 대화 제목을 넣은 픽스처로 모든 단계를 돌려, stdout·result·스냅샷·로그에 그 문자열이 없음을 검사한다. 사이드바 구조가 바뀐 변형 픽스처(계정 링크가 banner 밖 다른 그룹에 있는 경우)도 둔다.
- 픽스처는 이 작업의 테스트 대화에서만 만들고 커밋 전 스크럽·수동 확인. agy에 넘기는 폴더는 넘기기 전 이메일·계정 패턴으로 검사하고 결과를 `reviews/REVIEW_LOG.md`에 남긴다.

## 14. R6 불변식 계측

- `deep_research.py`(Claude 셸 = 기본 데스크톱)가 plan/edit/approve/collect 동안 20ms 간격 포그라운드 추적 스레드를 돌려 Gemini 프로세스가 포그라운드가 된 횟수와 기본 데스크톱에 보이는 Gemini 창 표본 수를 job 결과에 `foreground_invariant: {gemini_foreground_events, visible_windows}`로 남긴다. 0이 아니면 경고(사용자 인스턴스가 원인일 수 있으므로 pid가 우리 인스턴스인지 구분해 기록).
- 릴레이·agy 쪽에서는 측정하지 않는다(exebox 데스크톱).
- `doctor`는 읽기 전용 UIA 조회를 하면서 같은 추적을 돌려 결과에 넣는다.

## 15. CLI·오류 코드

- `preflight`: 플랫폼(`UNSUPPORTED_PLATFORM`), agy, 앱 설치(`APP_NOT_INSTALLED`), venv·comtypes(`UIA_UNAVAILABLE`), 스크립트 동기화, 인스턴스 목록(고아 경고), 앱 버전이 마지막 성공과 다르면 doctor 자동 실행.
- `setup`: venv 생성 + `comtypes==1.4.11` 설치(사용자 확인 후), agy 쪽 스킬 파일 복사.
- `doctor [--snapshot]`: 쿼터 0, 읽기 전용. 로케이터별 해석 결과, 인스턴스, 바인딩 가능 여부, 포그라운드 불변식. 보이는 창에서 열려 있는 사용자의 대화를 읽지 않도록 숨은 인스턴스만 대상으로 한다.
- `signin`, `app status|close`, `plan/edit/approve/collect --show`(디버그).
- 새 코드: `UI_LOCATOR_MISS`, `MODEL_NOT_AVAILABLE`, `UNREVIEWED_START`, `COUNTDOWN_NOT_CANCELLED`, `PROMPT_READBACK_MISMATCH`, `INPUT_NOT_EMPTY`, `UNEXPECTED_SURFACE`, `GEMINI_IN_USE`, `NOT_SIGNED_IN`, `UIA_UNAVAILABLE`, `UNSUPPORTED_PLATFORM`, `LEGACY_BROWSER_MODE`. 후퇴 사유 `profile_switch_ignored`는 결과 필드(오류 아님).
- 유지: `CONVERSATION_MISMATCH`, `REPORT_PROVENANCE_MISSING`, `SAFETY_BLOCKED`, `PLAN_TIMEOUT`, `REPORT_TIMEOUT`, `NO_CONFIRM_BUTTON`, `COLLECT_WOULD_START`, `DEEP_RESEARCH_TOGGLE_NOT_FOUND`, `STEP_ALREADY_ACTED` 등 게이트 코드.

## 16. 테스트

- 결정적 단위: `report_md`(트리 픽스처 → GFM 기대값), 로케이터 매처(정확·정규식·scope·재정의 병합·MISS 힌트), privacy 마스크·scrub, gemini_host의 결정 로직(D2 확인 실패 → D1, 사용자 인스턴스 → `GEMINI_IN_USE`; 프로세스·데스크톱 API는 주입형 가짜).
- Fake 백엔드 단계 테스트(ko 녹화 + en 합성): plan(카운트다운 취소 성공, 취소 버튼 없음 → fail-closed, 시작돼 버림 → `UNREVIEWED_START`), edit, approve, collect, 라벨 바뀐 픽스처 → `UI_LOCATOR_MISS` + 재정의 파일만으로 복구, 계정 문자열 누출 없음.
- 정적 검사(run_verification): 스크립트에 `playwright`, `remote-debugging`, `connect_over_cdp`, `chrome_launcher`, `9223`, `9224`, `--browser chrome`, `app.asar` 쓰기가 없을 것, 그리고 `%LOCALAPPDATA%\Google\Gemini` 경로에 쓰는 코드가 없을 것(open(...,'w')/copy 대상 검사).
- 기존 게이트 테스트(test_deep_research, test_conversation_identity 등)는 CDP 의존 부분만 Fake로 바꿔 그대로 통과.
- `run_verification.py`의 unittest 90초 한도는 현재 기준선(88.6초)과 거의 같다 → 한도를 늘리거나 느린 테스트를 줄인다(빌더 판단, 테스트 삭제·skip 금지).
- 라이브: doctor 1회, G4 E2E 1회, R1 해시 비교.

## 17. 확인 사항 (confinement, 문서)

- `confine_hook.py` deep-research 프로파일: `patch_gemini_app.py` 허용 분기 삭제, `relay_step.py --job <workdir>`만. 테스트 갱신(완화 금지).
- `agy-delegate/scripts/agy_task.py`의 deep-research 프레임(DEEP_RESEARCH_FRAME)에 있는 "CDP 포트가 준비 안 되면 `patch_gemini_app.py`를 실행해도 된다" 문장 삭제(실측 dry-run 프롬프트에서 확인). `prompts/agy_relay_task.md`의 `{cdp_recovery}` 2단계는 "오류 코드는 재시도 없이 보고"로 바꾼다(prompt-engineering 스킬로 수정·검토).
- 문서: SKILL.md(frontmatter "Use when…", 1024자 이하), references(app-mode → 숨은 데스크톱·UIA, architecture, troubleshooting, gates, cli), README, `references/SKILL.md`(agy 쪽), `prompts/agy_relay_task.md`(patch 복구 문단 삭제, prompt-engineering 스킬로 수정), `test_docs_consistency.py`.

## 17b. builder check 처리 (2026-11-26)

| # | 질문 | 처리 |
|---|---|---|
| B1 | `auto`(검토 없는 연구)를 없앨지, 유지할지, 자체 취소·승인으로 바꿀지 | **사용자 결정(G3)**. 제안: 유지하되 "카운트다운 취소 → 계획 저장 → 명시적 `--reason`과 함께 자동 승인"으로 바꿔, 계획이 항상 기록되고 시작 경로가 하나뿐이게 한다 |
| B2 | 기존 별칭(pro/flash/argon)과 새 모드 메뉴(자동/Hydrogen/Neon/카본 + 사고 수준) 대응, 기본값, Argon 전용 시간 설정 | **사용자 결정(G3)**. 제안: 기본 = "심층 분석 4 Neon" + 사고 수준 "높음". `pro`→Neon, `flash`→Hydrogen, `argon`/`carbon`→카본(긴 시간 설정은 카본에 승계). 예전 job의 `gemini_model: "Pro"`는 Neon으로 읽는다. 메뉴에 없는 모델은 `MODEL_NOT_AVAILABLE` |
| B3 | `approve --no-wait` 등 CLI가 먼저 끝나면 R6 계측은 어디서 | **내 결정**: `deep_research.py`가 기본 데스크톱에 분리 실행되는 계측 프로세스를 함께 띄우고, 분리 작업자가 끝날 때까지 `<job>/relay/foreground.jsonl`에 기록, `collect`가 결과에 합친다 |
| B4 | job이 D2로 시작했는데 나중 단계에서 D2를 못 얻으면 | **내 결정**: 모드는 `job.json`에 고정(`host_mode`). 한 job 안에서 D2→D1 전환은 하지 않고 `HOST_MODE_UNAVAILABLE`로 멈춘다(다른 계정·사용자 사이드바를 건드릴 이유를 없앰). 후퇴는 새 job의 `plan`에서만 |
| B5 | 카운트다운이 클라이언트인지 서버인지, 길이, 복구 중 맹점 | **내 결정 + 위험 항목**: 실측상 멈춘 창에서 받은 수정 계획은 재진입 후에도 시작되지 않았고 카운트다운도 없었다(1회). 길이는 미확인. 정책: (a) 계획 대기 중 복구는 `stop_response`가 사라진 뒤에만, (b) 모든 복구·재진입 직후 카운트다운 검사 + 진행 신호 검사, 진행 신호가 있으면 `UNREVIEWED_START`, (c) G4 E2E에서 카운트다운 길이를 잰다 |

## 18. 빌더에게 맡기는 것

- 모듈 경계 안의 함수 분할, 폴링 간격·타임아웃 기본값(실측 값을 하한으로), en 합성 라벨 목록의 세부, 진행 상태 문구 정규식, Fake 전이 스크립트 형식.

## 19. 위험과 미검증

- **위험 1: 숨은 창의 화면 갱신 정지.** 숨은 창은 일정 시간 뒤 화면(그리고 접근성 트리) 갱신을 멈춘다. 서버 쪽 작업은 계속된다. 실측: 수정 계획이 5분 넘게 "생성 중"으로 보였지만 서버에는 완성돼 있었다(S6). 연구 중 상태 문구가 12:04:55 이후 멈췄는데, 창 2px 리사이즈는 30초 안에 효과가 없었고, `보기 → 새로고침` + URL 재진입 후 5초 만에 "웹사이트 32개 → 45개"로 점프했다(S7). 예전 방식의 `--disable-renderer-backgrounding`·CDP 포커스 에뮬레이션은 R1·R2 때문에 쓸 수 없다. **대책**: 장시간 대기 단계(plan/edit의 카드 대기, approve/collect의 완료 대기)는 "무변화 T초(기본 120초)마다 §11-12 복구"를 기본 동작으로 하고, 복구 직후 카운트다운 검사를 항상 한다. 반복 정지 주기와 완료 감지 지연은 S7 끝까지의 관측으로 보강(**채움**). 다른 깨우기 수단(창 표시 상태 토글 등)은 미검증.
- 숨은 창에서 SetValue 후 전송 버튼이 2초 늦게 뜬 경우(D1)와 Invoke 반환이 2초 걸린 경우(S5)가 있었다 → 타임아웃은 이 값을 하한으로.
- 화면 잠금·가상 데스크톱 전환·절전 중 동작: 숨은 데스크톱은 입력 데스크톱과 무관할 것으로 보나 미측정.
- 계획 카드 라벨이 같은 계정에서도 ko/en으로 섞인다 → 로케이터는 항상 두 언어를 갖는다.
- 앱 업데이트가 `--user-data-dir`를 차단 목록에 넣으면 D1로 후퇴(감지 규칙 §7).
- 클립보드 보조 경로는 사용자 클립보드를 건드린다(기본 꺼짐).

## 20. X1 교차검증 반영 (agy gemini-4-neon-high, 3관점, 45건)

전체 처분은 `C:\Users\SSAFY\w\reviews\REVIEW_LOG.md`. 이 절은 설계를 바꾸는 결정만 모은다. 앞 절과 충돌하면 이 절이 우선한다.

1. **잠금**: `%LOCALAPPDATA%\agy-deep-research\host.lock` 배타 잠금. 드라이버·doctor·app close 모두 잡는다(F10. 실측: 두 스파이크 프로세스가 같은 페이지를 동시에 몰아 프로브가 깨졌다).
2. **대상 고정**: 우리 인스턴스 = (이미지 경로 `…\Google\Gemini\app-*\Gemini.exe`, 명령줄 `--user-data-dir=<전용 경로>`, 생성 시각)이 일치하는 프로세스 트리. signin·app close·고아 정리·창 탐색은 이 집합만 대상으로 하고, 기본 데스크톱 프로세스는 종료하지 않는다(S2, S10). job은 `host_mode`를 고정한다(B4).
3. **후퇴 조건**: D2→D1은 차단 증거(가드 종료, 명령줄 무시, 기본 프로필 사용)가 있을 때만. 느린 시작은 60초까지 기다린다. D1 전에는 사용자 Gemini **프로세스** 존재를 검사한다(F24, S1).
4. **프라이버시 fail-closed**: 구조 기대(main 1개, navigation을 품은 region 1개)가 깨지면 이름 기록을 전면 중지한다. 바인딩 전 문서는 존재·개수만 읽는다. stderr·예외 메시지도 scrub한다. 고정 일반 라벨은 영역 밖이어도 일치 여부만 비교한다(S3, S4, U7, U10, U11).
5. **로케이터 데이터화**: 메뉴 경로, 상태·안전 문구, 허용 문서 목록, URL 패턴, 복구 후 기대 URL, 타임아웃을 모두 `locators.json`/설정으로 옮긴다. 모든 로케이터에 기대 개수(`unique`/`at_least`)와 허용 패턴 목록을 둔다(대체 순서 `act()` 제거). 재정의는 필드 단위 병합 + 스키마 검증 + 버전 키(U3–U6, U9, F15).
6. **부재 판정**: "없음"으로 상태를 정하는 곳(전송 버튼 없음 = 빈 입력창, stop 없음 = 생성 끝 등)마다 양성 앵커가 함께 있어야 한다. 앵커가 없으면 `UI_LOCATOR_MISS`(U1).
7. **카드 식별**: plan_card = 시작 버튼을 품은 가장 가까운 `model-response-message-content*`(AutomationId) 조상. 대상 카드 = 문서 순서 마지막 + 활성 시작 버튼 + 이전 카드 수+1(F3, F21, U2).
8. **카운트다운 안전**: 모든 복구·재진입 직후와 복구 직전에 카운트다운을 검사한다. 취소 실패 시 `연구 계획 수정` Invoke로 2차 정지를 시도하고, 그래도 안 되면 fail-closed. 진행 신호 확인은 Invoke 반환 직후부터 한다. stop_response가 안 사라져도 300초 상한 뒤에는 복구한다(F16, F20).
9. **복구 통일**: §11-10의 단순 새로고침을 §11-12(새로고침 + 전체 URL 정확 일치 재진입, 사이드바 지연 로드 15초 재시도)로 바꾼다. 재진입 링크는 `https://gemini.google.com/app/<id>` 정확 일치만, Hyperlink 대상은 gemini.google.com만 허용한다(F2, S7).
10. **장애**: 단계 중 앱 소실은 `APP_RESTARTED`(전송 전이면 재시도, 후면 job error). `HOST_DESKTOP_LOST`. COM 호출은 작업 스레드 + 타임아웃. REPORT_TIMEOUT이어도 인스턴스를 닫지 않는다. 시간 측정은 단조 시계(F8, F9, F12, F14, F15).
11. **R6 강화**: `--show`는 signin·doctor 전용이다. 우리 인스턴스가 기본 데스크톱 포그라운드가 되면 단계 실패. 분리 작업자 동안 계측 프로세스를 유지하고, 작업자가 끝나면 함께 종료한다(S6, F18, B3).
12. **confinement 강화**: `job.json`·`relay/foreground.jsonl`을 deep-research 보호 목록에 추가한다(S5-3, 사실 확인). venv는 `-I`/`-s -E`로 실행하고 `comtypes`는 `--require-hashes`로 설치한다. 앱 실행 환경에서 `NODE_*`/`ELECTRON_*`를 제거한다(S9, S11).
13. **보고서 완전성**: 완료 = 완료 신호 + 길이 2회 안정 + 복구 후 재판독 일치. 여기에 구조 검사(제목·출처 링크·표 행 일관성)를 더한다. 클립보드 교차 확인은 기본 꺼짐 + 사용자 확인 + 클립보드 기록 제외(F17, F23, S8). R3의 "복사 경로 교차 확인"을 구조 검사로 대체할지는 G3 결정.
14. **doctor 한계 명시**: 무쿼터 doctor는 지금 화면에서 확인 가능한 로케이터만 검증한다. 나머지는 마지막 성공 실행의 해석 기록과 비교해 표시한다(U8, F7).

## 21. G3 결정과 후속 수정 (2026-11-26, 사용자 승인: 조건부)

설계는 **조건부 승인**이다. 구조와 독립 단위(UiTree·Fake, 로케이터, privacy, gemini_host 결정 로직, 제거·정적 검사, confinement, plan/edit 단계 로직)는 구현에 들어간다. 장시간 대기·완료 감지·보고서 추출(§11-9/10, report_md의 최종 규칙)은 위험 1이 풀린 뒤 확정한다.

### 21.1 사용자 결정 (원문 선택지)
- **갱신 정지 대책 — "스위치 시험 허용 (Recommended)"**: R1 허용 스위치를 `--user-data-dir=<전용 경로>` + `--disable-backgrounding-occluded-windows` + `--disable-renderer-backgrounding` + `--disable-features=CalculateNativeWinOcclusion`로 한정한다. 효과가 확인되면 기본으로 켜고, 막히면(가드 종료·무시) 새로고침 방식으로 후퇴한다. 정적 검사는 이 4개 외의 스위치 문자열이 실행 코드에 들어오면 실패한다.
- **auto (B1) — "유지하되 경로 통일 (Recommended)"**: `auto` = plan 단계(카운트다운 취소 + 계획 저장) → `--reason` 필수 자동 승인 → approve 경로. 연구 시작 코드는 approve 한 곳뿐이다.
- **모델 (B2) — "Neon 높음 기본 (Recommended)"**: 기본 = 심층 분석 4 Neon + 사고 수준 높음. `pro`→Neon, `flash`→Hydrogen, `argon`→카본(Argon용 긴 시간 설정을 카본이 물려받음). 예전 job의 `Pro`는 Neon으로 읽는다. 일치는 유일 일치만 인정하고, 여럿이면 `MODEL_AMBIGUOUS`, 없으면 `MODEL_NOT_AVAILABLE`.
- **보고서 검증 (R3) — "구조 검사로 대체 (Recommended)"**: 트리 경로 + 구조 검사(제목 수, 출처 링크 수, 표 행 일관성, 길이 안정)를 기본으로 한다. 클립보드 비교는 명시적으로 켤 때만 쓰는 선택 기능이며, 쓰면 결과에 기록한다.

### 21.2 스위치 시험 결과 (진행 중)
- 가드 차단 목록(main.js에서 읽기만 함): remote-debugging-port/pipe, remote-allow-origins, inspect*, proxy-server/pac-url, host-rules, host-resolver-rules, ignore-certificate-errors*, allow-running-insecure-content, disable-web-security, load-extension, disable-extensions-except, bundled-web-dir. 시험한 3개와 `--user-data-dir`은 목록에 없다.
- 실측(17:14): 3개를 붙여 실행하면 가드를 통과하고, 3개 모두 브라우저 명령줄에 반영되며, 렌더러에는 feature 플래그가 반영된다. 로그인도 유지된다.
- 전송 #3(17:15)은 계획 카드 대신 Gemini 오류 응답을 받았다. 숨은 창은 이 응답을 새로고침 없이 표시했다. 앞선 연구가 아직 진행 중이라 막혔을 가능성이 있다. → 갱신 정지 해소 여부는 **미판정**이다(추가 전송 예산 대기).
- 사용자 조작 반응(도구 메뉴 열기·닫기 6.5분)은 스위치 유무에 차이가 없다(열기 2.15–2.18초, 닫기 0.14–0.17초). 이 시험으로는 판별할 수 없다.

### 21.3 재진입 (§11-12 수정, 미해결 1)
- **불변식**: 돌아올 수단(job URL과 정확히 일치하는 Hyperlink)이 **지금 트리에 보이는지 확인하기 전에는 새로고침하지 않는다.** 보이지 않으면 복구를 시도하지 않고 현재 화면을 유지한 채 `RECOVERY_UNAVAILABLE`로 기록한다(단계는 계속 기다리거나 시간 초과로 끝낸다). 스위치로 새로고침이 드물어져도 이 불변식은 유지한다.
- 근거(17:02–17:04): 연구 시작 5시간 뒤 새로고침했더니 job 대화가 사이드바 최근 목록(대화형 링크 4개)에서 빠져 있었고, 「더보기」를 두 번 눌러도 나타나지 않았다. 12:01에는 목록에 있었다. 같은 계정의 새 대화에 밀려났을 수 있다.
- 앱 외부 진입 경로는 없다(main.js 읽기 전용 조사): 명령줄 처리는 `--hide`, `--open-minichat`, `--check-for-updates`, `--open-settings`, `--switch-account`, `--sign-out`, `--open-logs`, `--start-dictation`, 테스트 크래시 플래그뿐이다. 대화 URL 인자·프로토콜 핸들러는 없고, 시작 URL은 prod에서 `https://gemini.google.com/app` 고정이다.
- 후보(미검증, 구현 보류): (a) 앱 안 「채팅 검색」에 job 대화 제목(우리가 만든 계획 카드 제목)을 넣고, 결과 Hyperlink를 URL 값으로만 매칭한다(결과 이름은 기록하지 않음). 입력이 필요하므로 별도 무쿼터 스파이크 후 결정한다. (b) 대화 고정(pin)은 사용자 계정 상태를 바꾸므로 쓰지 않는다(후보로만 기록).
- 설계 영향: 같은 계정을 쓰는 D2에서도 사용자의 일상 사용이 job 대화를 목록 밖으로 밀어낼 수 있다. 장시간 대기는 "새로고침 없이 살아 있는 화면"(스위치)이 1순위이고, 새로고침 복구는 링크가 보일 때만 쓰는 보조 수단이다.

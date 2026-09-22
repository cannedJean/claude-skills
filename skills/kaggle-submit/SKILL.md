---
name: kaggle-submit
description: "공식 Kaggle CLI(github.com/Kaggle/kaggle-cli)를 감싼 스크립트로 캐글 컴피티션 제출을 처음부터 끝까지 자동화한다. 첫 실행 시 kaggle 패키지 설치·API 자격증명 배치·인증 검증·기본 컴피티션 설정을 안내/수행하고(doctor/setup), 컴피티션 정보·규칙 수락 여부·남은 제출 횟수 확인(info), 데이터 다운로드(download), sample_submission 대비 제출 파일 검증(validate), 제출 후 점수 대기와 로컬 제출 로그 기록(submit --wait), 제출 이력·리더보드 조회(submissions/leaderboard)를 한다. '캐글에 제출해줘', 'kaggle submit', 'submission.csv 올려줘', '캐글 API 세팅해줘', 'kaggle.json 어디에 두지', '캐글 점수 확인', '리더보드 보여줘', '제출 횟수 남았어?', '캐글 데이터 받아줘', '코드 컴피티션 노트북 제출' 같은 요청에 반드시 이 스킬을 사용할 것. 데이터셋/모델/커널 관리 등 제출과 무관한 kaggle CLI 작업은 references/kaggle-cli-competitions.md의 명령 트리만 참고한다."
---

# kaggle-submit — 캐글 컴피티션 제출 자동화

모든 작업은 한 스크립트로 한다. 명령마다 **stdout에 JSON 한 덩어리**, 진행 로그는 stderr.

```
python ~/.claude/skills/kaggle-submit/scripts/kaggle_submit.py <command> ...
```

종료 코드: `0` 정상 / `2` 검증·전제조건 실패(규칙 미수락, 일일 한도, 파일 문제, 채점 오류) / `3` kaggle CLI 오류 / `4` 세팅 필요(CLI 없음·미인증).
**rc=4가 나오면 다른 작업을 하지 말고 §0으로 간다.** JSON의 `reason`과 `next_steps`를 그대로 사용자에게 전달하면 된다.

스크립트는 `kaggle`을 PATH가 아니라 `<python> -m kaggle`로 부른다(Windows에서 `kaggle.exe`가 PATH에 없어도 됨). 다른 인터프리터에 설치돼 있으면 `KAGGLE_SUBMIT_PYTHON=<그 python>`으로 지정한다.

## 0. 첫 실행 세팅 — `doctor` → `setup`

세션에서 이 스킬을 처음 쓸 때 **반드시 `doctor`부터** 실행한다.

```
python ~/.claude/skills/kaggle-submit/scripts/kaggle_submit.py doctor
```

`doctor`는 Python 버전(3.11+), kaggle CLI 설치 여부·버전, 자격증명 파일 존재(`~/.kaggle/access_token`, `kaggle.json`, OAuth `credentials.json`, 환경변수), **실제 API 호출로 인증 성공 여부**, 설치된 CLI가 지원하는 기능(`submit --wait`, `submission-limits` 등)을 JSON으로 보고한다.

- `kaggle_cli.installed: false` → `setup` 실행. pip으로 `kaggle`을 설치한다(사용자 확인 불필요).
  ```
  python ~/.claude/skills/kaggle-submit/scripts/kaggle_submit.py setup
  ```
- `auth.ok: false` → 자격증명이 필요하다. **에이전트는 토큰·키 문자열을 절대 직접 다루지 않는다** (채팅으로 받지도, 명령 인자로 넣지도, 파일에 타이핑하지도 않는다). 사용자에게 아래 중 하나를 고르게 한다. `next_steps`에 실행 가능한 명령이 이미 채워져 있다.
  - **(A) OAuth(권장)**: 사용자가 **본인 터미널에서** `python -m kaggle auth login`을 실행한다. 브라우저가 열리고 로그인하면 `~/.kaggle/credentials.json`에 저장된다. 에이전트가 이 명령을 대신 실행하지 않는다(TTY·브라우저 로그인 필요, 계정 인증은 사용자 몫).
  - **(B) 레거시 키 파일**: 사용자가 https://www.kaggle.com/settings/api 에서 *Create Legacy API Key*로 `kaggle.json`을 다운로드하면, 에이전트가 파일을 **이동**만 한다.
    ```
    python ~/.claude/skills/kaggle-submit/scripts/kaggle_submit.py setup --kaggle-json ~/Downloads/kaggle.json
    ```
  - **(C) API 토큰 파일**: *Generate New Token*으로 받은 토큰을 사용자가 텍스트 파일에 저장하면 `setup --token-file <파일> --delete-source`로 `~/.kaggle/access_token`에 옮긴다.
  - **(D) CI/비대화형**: `KAGGLE_API_TOKEN` 또는 `KAGGLE_USERNAME`+`KAGGLE_KEY` 환경변수.
- 자격증명이 **있는데** `auth.ok: false`면 만료·폐기된 키다. 설정 페이지에서 재발급 후 `setup --force`로 교체.
- 자주 쓰는 컴피티션이 정해져 있으면 `setup --competition <slug> --download-path <dir>`로 기본값을 저장한다(이후 kaggle CLI를 직접 쓸 때 slug 생략 가능. 이 스크립트의 명령은 항상 slug를 받는다).

`setup`은 끝에 인증을 다시 검증하고 `ok: true`, `username`을 돌려준다. 이후 세션에서는 `doctor`가 `ok: true`면 바로 §1로.

## 1. 컴피티션 확인 — `info`

```
python ~/.claude/skills/kaggle-submit/scripts/kaggle_submit.py info <slug> [--pages]
```

- slug는 URL의 `kaggle.com/competitions/<slug>` 부분. 모르면 `kaggle competitions list -s <키워드>`.
- `reason: rules_not_accepted_or_forbidden`(403) → **사용자가 웹에서 Join + 규칙 수락**을 해야 한다. CLI로는 불가능하다. URL을 전달하고 기다린다.
- 결과에서 확인할 것: `meta.deadline`(마감), `meta.userHasEntered`, `sample_submission_file`(검증 기준 파일), `limits.remaining_today`(오늘 남은 제출 수), `recent_submissions`.
- `--pages`는 Evaluation 페이지 본문을 가져온다. 평가 지표·제출 형식이 불확실할 때만.

## 2. 데이터 — `download`

```
python ~/.claude/skills/kaggle-submit/scripts/kaggle_submit.py download <slug> -p data --unzip
```

zip을 풀고 삭제하며 `sample_submission_file` 경로를 돌려준다. 이미 로컬에 데이터가 있으면 생략. 큰 데이터는 `run_in_background`로.

## 3. 검증 — `validate` (submit이 자동으로 수행)

```
python ~/.claude/skills/kaggle-submit/scripts/kaggle_submit.py validate submission.csv [--sample data/sample_submission.csv] [--id-col id]
```

sample 파일은 제출 파일 옆·`./data`·`./input`에서 `*sample*submission*.csv`를 자동 탐색한다. 검사: UTF-8·CSV 파싱, 헤더 일치(순서 다르면 경고), 행 수 일치, id 집합 일치(누락/초과), 중복 id, 빈 셀/NaN. `problems`가 있으면 rc=2 — **고치고 다시 검증한다. `--force`로 넘기지 않는다.** 캐글 채점 오류 한 번이 일일 제출 횟수 하나를 소모한다.

## 4. 제출 — `submit`

```
python ~/.claude/skills/kaggle-submit/scripts/kaggle_submit.py submit <slug> -f submission.csv -m "lgbm 5fold seed42 cv0.8123" --wait 900 --note "cv=0.8123 lr=0.05"
```

순서: 검증 → 일일 한도 확인(`remaining_today == 0`이면 중단) → 업로드 → 새 제출 ref 식별 → (`--wait`) 채점 완료까지 폴링 → `kaggle_submissions.jsonl`에 기록.

- **실제 제출 전 사용자 확인.** 제출은 되돌릴 수 없고 일일 한도를 소모한다. 사용자가 "제출해줘"라고 명시했으면 그 한 번은 확인 없이 진행하되, 파이프라인 중간에서 자동으로 여러 번 제출하지 않는다. 확신이 없으면 `--dry-run`으로 실행될 명령을 먼저 보여준다.
- `-m` 메시지는 실험을 식별할 수 있게 쓴다(모델·시드·CV 점수). 캐글 UI에서 이 문자열로 제출을 구분한다.
- `--wait N`: N초까지 대기, `--wait`만 주면 무제한(코드 컴피티션은 채점에 수 시간). 대기가 길면 `run_in_background`로 띄우고 나중에 `status`로 확인.
- 결과: `submission.status`(`COMPLETE`/`PENDING`/`ERROR`), `publicScore`, `submission_ref`, `remaining_today`, `log`. `reason: scoring_error`면 `submission.errorDescription`을 사용자에게 그대로 전달한다(대개 sample 검사로 못 잡는 형식 문제: 값 범위, 확률 합, 열 이름 대소문자).
- 로그 파일은 CWD의 `kaggle_submissions.jsonl`(`--log`로 변경). 프로젝트의 실험 기록과 함께 두면 좋다. `log --competition <slug>`로 조회.

### 코드 컴피티션 (노트북 제출)

제출물이 파일이 아니라 캐글 노트북의 출력이다. 노트북은 먼저 캐글에 올라가 실행이 끝나 있어야 한다(`kaggle kernels push` → `kaggle kernels status`; references 참고).

```
python ~/.claude/skills/kaggle-submit/scripts/kaggle_submit.py submit <slug> -k <user>/<kernel-slug> -f submission.csv -v 3 -m "v3 ensemble" --wait
```

`-f`는 로컬 경로가 아니라 **노트북이 만든 출력 파일 이름**, `-v`는 노트북 버전 번호. 로컬 검증은 건너뛴다.

## 5. 결과 확인 — `status` / `submissions` / `leaderboard`

```
python ~/.claude/skills/kaggle-submit/scripts/kaggle_submit.py status <slug> [--ref 12345678] [--wait 600]
python ~/.claude/skills/kaggle-submit/scripts/kaggle_submit.py submissions <slug> [-n 50] [--lower-is-better]
python ~/.claude/skills/kaggle-submit/scripts/kaggle_submit.py leaderboard <slug> -n 20
```

`submissions`는 `best_public`을 같이 준다. 지표가 낮을수록 좋으면(RMSE, logloss) `--lower-is-better`. 사용자에게 보고할 때는 이번 제출 점수, 지금까지 최고 점수, 남은 제출 수, 마감까지 남은 기간을 함께 말한다.

## 규칙

- 토큰·API 키·`kaggle.json` 내용은 읽지도 출력하지도 않는다. `doctor`가 보여주는 것은 파일 존재 여부와 username뿐이다.
- `kaggle auth login`은 사용자가 직접 실행한다. 에이전트가 브라우저 로그인·계정 생성·규칙 수락을 대신하지 않는다.
- 같은 파일을 두 번 제출하지 않는다. 직전 제출과 파일 해시/메시지가 같으면 사용자에게 확인한다.
- `remaining_today`가 1이면 사용자에게 알리고 제출한다. 0이면 UTC 자정 이후를 안내한다.
- kaggle CLI를 직접 호출해야 할 때(데이터셋, 커널 push 등)는 `python -m kaggle ...` 형태로 하고 `--format json`을 붙인다. 명령 목록은 [references/kaggle-cli-competitions.md](references/kaggle-cli-competitions.md).
- 오류가 반복되면 [references/troubleshooting.md](references/troubleshooting.md)를 읽는다.

## 버전 차이 (스크립트가 흡수함)

PyPI `kaggle 2.2.x`에는 `competitions submit --wait`, `competitions submission <ref>`, `download --unzip`이 없다(git main에는 있음). 스크립트는 `doctor`의 `features`로 감지해 제출 ref 식별·점수 폴링·압축 해제를 자체 구현으로 대체하므로 어느 버전이든 같은 명령을 쓰면 된다.

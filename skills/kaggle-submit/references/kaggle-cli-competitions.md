# Kaggle CLI 요약 — 인증 · 설정 · 컴피티션 · 커널 (kaggle-submit용)

출처: https://github.com/Kaggle/kaggle-cli (`docs/`, `skills/references/`). 설치 확인: `python -m kaggle --version`.
직접 호출할 때는 항상 `python -m kaggle --no-warn <group> <command> ... --format json` 형태로 쓴다.
정확한 플래그는 설치된 버전의 `python -m kaggle <group> <command> --help`가 최종 기준이다.

## 명령 트리

```
kaggle
├── competitions | c
│   ├── list, files, download, submit, submissions, leaderboard, submission-limits
│   ├── submission <ref>        (git main only)
│   ├── team-submissions, episodes, replay, logs, pages
│   └── topics {list, show}
├── datasets | d   list, files, download, init, create, version, metadata, status, delete
├── kernels | k    list, files, init, push|update, pull|get, output, status, logs, delete
├── models | m     ... (variations, versions)
├── files {upload}
├── forums | f
├── benchmarks | b
├── config {view, set, unset}
├── auth {login, print-access-token, revoke}
├── quota
└── search
```

## 인증 (`kaggle auth`, 파일, 환경변수)

CLI는 다음 순서로 인증을 시도하고 모두 실패하면 안내문을 찍고 exit 1:

1. 액세스 토큰: 환경변수 `KAGGLE_API_TOKEN` 또는 파일 `~/.kaggle/access_token`
2. 레거시 API 키: `~/.kaggle/kaggle.json`(`{"username":..., "key":...}`) 또는 환경변수 `KAGGLE_USERNAME` + `KAGGLE_KEY`
3. OAuth 자격증명: `kaggle auth login`이 만든 `~/.kaggle/credentials.json`
4. 익명(일부 공개 데이터셋 명령만)

설정 디렉터리: `KAGGLE_CONFIG_DIR` > `~/.kaggle` > (Linux에서 `~/.kaggle`이 없으면) `$XDG_CONFIG_HOME/kaggle`.
새로 만든 자격증명 파일은 0600으로 chmod된다. 비Windows에서 world-readable이면 경고.

| 명령 | 설명 |
|---|---|
| `kaggle auth login [--no-launch-browser] [--force]` | 브라우저 OAuth. 이미 로그인돼 있으면 계정만 표시(`--force`로 재로그인). 사용자 터미널에서 실행. |
| `kaggle auth print-access-token [--expiration 6h]` | OAuth 계정의 단기 토큰 발급 → `KAGGLE_API_TOKEN`에 쓸 수 있음(CI용). |
| `kaggle auth revoke [--reason TEXT]` | OAuth refresh token 폐기. |

토큰 발급 페이지: https://www.kaggle.com/settings/api — *Generate New Token*(액세스 토큰) / *Create Legacy API Key*(`kaggle.json` 다운로드).

## 설정 (`kaggle config`)

```
kaggle config view
kaggle config set -n competition -v titanic     # 기본 컴피티션 (slug 생략 가능해짐)
kaggle config set -n path -v /data/kaggle       # 기본 다운로드 폴더
kaggle config set -n proxy -v http://proxy:8080
kaggle config unset -n competition
```

`KAGGLE_` 접두 환경변수가 파일 값 위에 덮어씌워진다(`KAGGLE_PROXY`, `KAGGLE_COMPETITION` 등).

## 컴피티션 (`kaggle competitions`, 별칭 `c`)

### list
```
kaggle competitions list [--group general|entered|inClass] [--category all|featured|research|recruitment|gettingStarted|masters|playground]
                         [--sort-by grouped|prize|earliestDeadline|latestDeadline|numberOfTeams|recentlyCreated]
                         [-s TERM] [-p PAGE] [--page-size N] [--format json]
```
출력 필드: `ref, deadline, category, reward, teamCount, userHasEntered, userRank`. `--group entered`로 내가 참가한 것만.

### files / download
```
kaggle competitions files <slug> [--page-size N] [--format json]      # name, totalBytes, creationDate
kaggle competitions download <slug> [-f FILE] [-p DIR] [-w] [-o] [-q]  # 전체(zip) 또는 -f 한 파일
```
403 → 규칙 미수락. `--unzip`은 git main에만 있음(2.2.x는 직접 압축 해제).

### submit
```
kaggle competitions submit <slug> -f submission.csv -m "message" [-q] [--sandbox]
kaggle competitions submit <slug> -k user/kernel -f output.csv -v 3 -m "message"     # 코드 컴피티션
kaggle competitions submit <slug> -f submission.csv -m "msg" --wait 600 --poll-interval 30   # git main only
```
- `-m`은 필수. `-k`와 `-v`는 함께 써야 한다.
- 2.2.x 출력: 서버 메시지 + `N submissions remaining today.` git main 출력: `Submission ref: <ref>`.
- 404 → slug 오류이거나 제출 마감. 403 → 규칙 미수락.

### submissions / submission / submission-limits
```
kaggle competitions submissions <slug> [--page-size N] [--format json]
   # 필드: ref, fileName, date, description, status(PENDING|COMPLETE|ERROR), publicScore, privateScore
kaggle competitions submission <ref>          # git main only: 단일 제출 상태·점수·오류 사유
kaggle competitions submission-limits <slug> [--json]
   # numToday, numTotal, numAllowedNow(오늘 남은 수), limitedByTotal
```

### leaderboard
```
kaggle competitions leaderboard <slug> --show [--page-size N] [--format json]   # teamId, teamName, submissionDate, score
kaggle competitions leaderboard <slug> --download -p DIR                        # 전체 리더보드 zip
```

### pages / topics
```
kaggle competitions pages <slug> [--page-name description|rules|evaluation|...] [--content]
kaggle competitions topics list <slug> [--sort-by hot|top|new|recent|active]
kaggle competitions topics show <slug>/<topic-id>
```

### 시뮬레이션 컴피티션
`episodes <submission_id>`, `replay <episode_id> -p DIR`, `logs <episode_id> <agent_index> -p DIR`, `team-submissions <team_id>`.

## 커널(노트북) — 코드 컴피티션 제출 전 단계

```
kaggle kernels init -p ./nb                  # kernel-metadata.json 생성 (id, title, code_file, language, kernel_type,
                                             #   enable_gpu, enable_internet, dataset_sources, competition_sources, ...)
kaggle kernels push -p ./nb                  # 업로드 + 실행 시작 (새 버전)
kaggle kernels status user/kernel-slug       # queued | running | complete | error
kaggle kernels output user/kernel-slug -p out/   # 출력 파일(submission.csv 등) 내려받기
kaggle kernels logs user/kernel-slug
kaggle kernels pull user/kernel-slug -p ./nb -m   # 코드+메타데이터 내려받기
```
코드 컴피티션은 `kernel-metadata.json`의 `competition_sources`에 slug를 넣고, 노트북이 `/kaggle/working/submission.csv`를 써야 한다.
실행이 `complete`가 된 뒤 `competitions submit -k user/slug -f submission.csv -v <버전>`.

## 출력 형식

`--format csv|table|json` 또는 필드 프로젝션 `--format "json(ref,publicScore)"`. 구형 플래그 `-v/--csv`는 CSV.
JSON 앞에 `Using competition: ...`, 버전 경고 등 잡음 줄이 붙을 수 있다(`--no-warn`으로 버전 경고 제거).

## 튜토리얼 요약 — 일반 컴피티션 제출

1. 웹에서 컴피티션 Join + 규칙 수락 (CLI 불가)
2. `competitions download <slug> -p data` → 압축 해제 → `sample_submission.csv` 확인
3. 예측 파일을 sample과 같은 헤더·행 수·id로 생성
4. `competitions submit <slug> -f my.csv -m "..."`
5. `competitions submissions <slug>`로 status/score 확인

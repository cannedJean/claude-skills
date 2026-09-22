# kaggle-submit 문제 해결

증상은 스크립트 JSON의 `reason` 값 기준. `cli_output` / `cli_stderr`에 kaggle CLI 원문이 있다.

## 세팅 단계

| reason / 증상 | 원인 | 조치 |
|---|---|---|
| `kaggle_cli.installed: false` | `python -m kaggle` 실패 | `setup` (pip install kaggle). 다른 인터프리터에 설치돼 있으면 `KAGGLE_SUBMIT_PYTHON=<python>` |
| `pip_install_failed` | 네트워크·권한·프록시 | `cli_output`의 pip 메시지대로. 회사망이면 `--proxy`/`pip.ini` 설정 후 재시도 |
| `not_authenticated`, `credentials.sources: []` | 자격증명 없음 | §0의 (A)~(D) 중 하나. 사용자가 고른다 |
| `not_authenticated`, sources 있음 | 토큰 만료·폐기, 잘못된 파일, 다른 계정 | https://www.kaggle.com/settings/api 에서 재발급 → `setup --kaggle-json ... --force` 또는 `--token-file ... --force`. 환경변수 `KAGGLE_*`가 파일보다 우선하니 낡은 환경변수도 확인 |
| `Invalid credentials!` 출력 | OAuth refresh token 무효 | CLI가 `credentials.json`을 지운다. 사용자가 `kaggle auth login --force` 재실행 |
| `not_a_kaggle_json` | 파일이 `{"username","key"}` 형식이 아님 | *Create Legacy API Key*로 받은 파일인지 확인. *Generate New Token*은 토큰 문자열이므로 `--token-file` |
| Python < 3.11 경고 | kaggle-cli 공식 최소 버전 | 대개 3.9+에서도 동작하지만 오류 시 3.11+ 설치 |
| `kaggle.exe`는 PATH에 없음 | Windows 사용자 설치 | 스크립트는 `-m kaggle`로 부르므로 무관. 사용자 터미널에서는 `python -m kaggle auth login`을 쓰게 안내 |

## 컴피티션 / 제출 단계

| reason / 증상 | 원인 | 조치 |
|---|---|---|
| `rules_not_accepted_or_forbidden` (403) | 규칙 미수락, 팀 병합 대기, 비공개 컴피티션 | 사용자가 웹에서 `https://www.kaggle.com/competitions/<slug>/rules` 수락. inClass는 초대 링크 필요 |
| `competition_not_found_or_closed` (404) | slug 오타, 제출 마감, 컴피티션 종료 | `kaggle competitions list -s <키워드>`로 slug 확인. `info`의 `meta.deadline` |
| `validation_failed` | 헤더/행 수/id/NaN 불일치 | `problems` 항목을 고친다. `--force`는 사용자가 형식이 의도된 것이라고 확인했을 때만 |
| `daily_submission_limit_reached` | `numAllowedNow == 0` | UTC 00:00 리셋. 컴피티션마다 한도 다름(보통 5~10) |
| `scoring_error` | 캐글 채점기 거부 | `submission.errorDescription` 확인. 흔한 원인: 값 범위(확률 0~1), 클래스 확률 합, 열 이름 대소문자, 정수/실수 타입, 행 누락, 인코딩 BOM |
| `wait_timeout` | 채점 지연(코드 컴피티션 수 시간) | `status <slug> --ref <ref> --wait 0`을 백그라운드로 |
| `submission_ref: null` | 2.2.x에서 새 제출 식별 실패(목록 지연) | `submissions <slug>`로 최신 항목 확인. 제출 자체는 대개 성공 |
| `cli_error` + `timed out` | 대용량 업로드 | `--timeout 7200`. 파일을 zip으로 묶으면 캐글이 자동 해제(대부분의 컴피티션) |
| 코드 컴피티션 `submit`이 kernel/version 오류 | 노트북 실행 미완료 또는 버전 번호 오류 | `kaggle kernels status user/slug`가 `complete`인지, 버전은 노트북 페이지의 Version N |
| 제출은 됐는데 리더보드 점수 없음 | 컴피티션이 public score 비공개, 또는 PENDING | `status --wait`. 일부 컴피티션은 마감 후에만 점수 공개 |

## 출력 파싱

- kaggle CLI는 미인증이면 `--help`에도 인증 안내문을 찍는다. 스크립트가 `clean_lines`로 걸러낸다.
- `--format json` 앞에 `Using competition: X`가 붙을 수 있다. `parse_json_output`이 첫 `[`/`{`부터 파싱한다.
- 상태 값은 문자열(`COMPLETE`) 또는 숫자(0/1/2)로 올 수 있다. `normalize_submission`이 통일한다.

## 직접 확인용 명령

```
python -m kaggle --version
python -m kaggle config view
python -m kaggle competitions list --page-size 1 --format json
python -m kaggle competitions submission-limits <slug> --json
python -m kaggle competitions submissions <slug> --format json --page-size 5
```

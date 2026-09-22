# kaggle-submit

캐글 컴피티션 제출을 Claude Code 스킬로 자동화한다. 공식 [Kaggle CLI](https://github.com/Kaggle/kaggle-cli)를 감싼
단일 스크립트(`scripts/kaggle_submit.py`, 표준 라이브러리만 사용)와 에이전트용 지침(`SKILL.md`)으로 구성된다.

## 처음 한 번만: 세팅

```bash
python ~/.claude/skills/kaggle-submit/scripts/kaggle_submit.py doctor
```

`kaggle` 패키지가 없으면 `setup`이 설치한다. 자격증명은 아래 중 하나로 준비한다(스킬은 토큰을 절대 직접 입력하지 않는다).

| 방법 | 할 일 |
|---|---|
| **OAuth (권장)** | 본인 터미널에서 `python -m kaggle auth login` → 브라우저 로그인 |
| 레거시 키 파일 | https://www.kaggle.com/settings/api → *Create Legacy API Key* → `kaggle.json` 다운로드 → `setup --kaggle-json ~/Downloads/kaggle.json` |
| API 토큰 | 같은 페이지 *Generate New Token* → 토큰을 텍스트 파일에 저장 → `setup --token-file <파일> --delete-source` |
| 환경변수 | `KAGGLE_API_TOKEN` 또는 `KAGGLE_USERNAME` + `KAGGLE_KEY` |

그리고 컴피티션 페이지에서 **Join + 규칙 수락**은 웹에서 직접 해야 한다.

## 일상 사용

```bash
S=~/.claude/skills/kaggle-submit/scripts/kaggle_submit.py
python $S info titanic                                   # 마감, 파일, 남은 제출 수
python $S download titanic -p data --unzip
python $S validate submission.csv                        # sample_submission 대비 검사
python $S submit titanic -f submission.csv -m "lgbm cv0.81" --wait 600 --note "seed 42"
python $S submissions titanic                            # 이력 + 최고 점수
python $S leaderboard titanic -n 10
python $S log --competition titanic                      # 로컬 kaggle_submissions.jsonl
```

코드 컴피티션: `submit <slug> -k user/kernel -f submission.csv -v 3 -m "..." --wait`.

## 출력과 종료 코드

모든 명령은 stdout에 JSON 하나를 출력한다. `0` 정상 / `2` 검증·전제조건 실패 / `3` CLI 오류 / `4` 세팅 필요.
실패 시 `reason`과 `next_steps`가 채워진다.

## 환경변수

- `KAGGLE_SUBMIT_PYTHON`: kaggle이 설치된 인터프리터(기본: 스크립트를 실행한 python)
- `KAGGLE_CONFIG_DIR`: 자격증명 폴더(기본 `~/.kaggle`)

## 파일

```
kaggle-submit/
├── SKILL.md                              에이전트 지침
├── README.md
├── scripts/kaggle_submit.py              doctor/setup/info/download/validate/submit/status/submissions/leaderboard/log
└── references/
    ├── kaggle-cli-competitions.md        kaggle CLI 인증·설정·컴피티션·커널 명령 요약
    └── troubleshooting.md                reason별 원인과 조치
```

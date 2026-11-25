# ssafy-materials

SSAFY 학습 자료를 내 PC의 한 폴더로 모아 주는 Claude Code 스킬이다. 가져오는 자료는 세 가지다.
- **lm**: lab.ssafy.com 과정 그룹에 있는 강의 레포
- **ws / hw**: project.ssafy.com의 실습·과제 문제. 문제마다 내 GitLab 네임스페이스에 레포가 하나씩 생긴다.
- **문제 지문**: 사이트에만 있는 목표·문제·요구사항 텍스트

에이전트용 지침은 `SKILL.md`에 있다. 두 사이트에는 사용자가 이미 로그인해 둔 Chrome을 그대로 쓰고, 에이전트가 Claude in Chrome으로 조작한다.

```
lab.ssafy.com ──lab_lister.js──▶ ssafy_lab_manifest.json      (GitLab API, 페이지 세션 쿠키)
project.ssafy.com ──project_collector.js──▶ ssafy_project_manifest.json
   (숨은 iframe에서 사이트 UI를 그대로 조작: 과정 → 챕터 → 문제 → 실습하기 → 실습실의 GitLab 프로젝트)
두 매니페스트 ──sync.py──▶ <root>/lm/<project>, <root>/ws|hw/<subject>/<repo> (+ README.md 지문)
```

## 결과 폴더 구조

```
<root>/lm/<group project>              lab.ssafy.com/<group>/<project> 의 clone
<root>/ws/<subject>/<repo>             유형이 "실습"인 문제의 레포
<root>/hw/<subject>/<repo>             유형이 "과제"인 문제의 레포
<root>/ws|hw/<subject>/<repo>/README.md   문제 지문 (레포에 README가 이미 있으면 PROBLEM.md)
```

- ws와 hw는 사이트에 표시된 **유형**으로 나눈다. 레포 이름의 `_ws_`/`_hw_`는 매니페스트에 없는 레포를 분류할 때만 쓴다.
- `<subject>`는 `scripts/subject_map.json`으로 정한다. 과정 제목을 먼저 보고, 없으면 레포 접두어를 본다.
- 매핑을 추가·변경하려면 `~/.ssafy-materials/subject_map.json`에 적는다. 스킬 폴더의 파일보다 이쪽이 우선한다.
- 지문 파일은 `.git/info/exclude`에 등록한다. 그래서 `git status`가 깨끗하게 유지되고 이후 `pull --ff-only`도 그대로 된다.

## 준비

1. Chrome에 Claude in Chrome 확장을 설치하고, lab.ssafy.com과 project.ssafy.com에 로그인해 둔다.
2. `git clone https://lab.ssafy.com/...`이 비밀번호를 묻지 않고 되는지 확인한다. Windows에서는 Git Credential Manager를 쓴다.
3. Python 3.9 이상을 준비한다. 표준 라이브러리만 쓴다.
4. 레포 그룹이 `s16/data`가 아니면 `scripts/lab_lister.js`의 `LAB_GROUP`을 바꾼다.

## 사용

Claude Code에 "SSAFY 자료 모아줘", "lm ws hw 받아줘" 같은 식으로 요청한다. 에이전트는 실행할 때마다 다음 두 가지를 먼저 묻는다.
- 매니페스트 두 개를 다운로드 폴더에 저장해도 되는가
- 아직 시작하지 않은 문제를 시작해도 되는가. 시작하면 계정에 참여 기록과 레포가 생기고, 되돌릴 수 없다.

수집기는 단계마다 진행 상황을 사이트의 localStorage(`ssafy_collector_v1`)에 저장한다. 탭이 다른 곳으로 넘어가도 같은 옵션으로 다시 붙여 넣으면 이어서 진행한다. 문제당 10~40초쯤 걸리고, 과정 전체(약 380문제)는 1~2시간이다.

동기화만 따로 돌릴 수도 있다.

```bash
python -I ~/.claude/skills/ssafy-materials/scripts/sync.py --root <root> --dry-run   # 미리보기
python -I ~/.claude/skills/ssafy-materials/scripts/sync.py --root <root>             # 실제 clone/pull
```

- 이미 있는 clone은 `pull --ff-only`로만 갱신한다.
- 로컬 변경이 있거나 origin이 다르거나 fast-forward가 안 되는 폴더는 손대지 않고 건너뛴 뒤 리포트에 남긴다.
- 리포트는 `~/.ssafy-materials/last_report.txt`(UTF-8)에 저장된다.
- 동기화에 쓴 매니페스트는 `~/.ssafy-materials/manifests/`로 옮긴다.

| 파일 | 역할 |
|---|---|
| `scripts/lab_lister.js` | lab.ssafy.com 페이지에서 실행한다. 과정 그룹과 내 네임스페이스의 프로젝트 목록을 GitLab API(세션 쿠키)로 받아 다운로드한다. |
| `scripts/project_collector.js` | project.ssafy.com 페이지에서 실행한다. 백그라운드 작업으로 문제마다 지문을 모으고 실습하기를 눌러 레포를 확인하며, 체크포인트에서 재개할 수 있다. |
| `scripts/project_poll.js` | 수집기 진행 상황을 확인한다. 최대 30초 기다린 뒤 결과를 돌려준다. |
| `scripts/project_export.js` | 현재 체크포인트 내용을 매니페스트로 내려받는다. |
| `scripts/sync.py` | 매니페스트를 합쳐 clone 또는 pull을 하고, 지문을 쓰고, 리포트를 만든다. 실행 중에 입력을 묻지 않는다. |
| `scripts/subject_map.json` | 과정 제목과 레포 접두어를 과목 폴더 이름으로 바꾸는 기본 매핑이다. |

## 지키는 규칙

- project.ssafy.com의 액세스 토큰을 읽지 않고, `/ssafy/api`를 직접 호출하지 않는다. 사이트는 UI로만 조작한다.
- 개인 액세스 토큰(PAT)을 만들지 않고, 자격 증명을 입력하지 않는다. 인증에 실패하면 멈추고 사용자에게 알린다.
- 받은 자료와 지문은 로컬에만 둔다. push하지 않고 remote도 추가하지 않는다. 사이트 콘텐츠는 저작권 보호 대상이고 외부 공유가 금지되어 있다.
- 매니페스트에는 사용자 네임스페이스가 들어 있으므로 비공개로 다룬다.

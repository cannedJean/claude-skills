---
name: gdrive-bridge
description: "Google Drive for Desktop 마운트(Windows G:\\, macOS ~/Library/CloudStorage)를 통해 로컬과 Google Colab 사이에서 데이터·산출물을 주고받는다. (1) 로컬 데이터 번들(zip)을 Drive에 올리고, 사람이 만든 공유 링크를 검증해 gdown용 file id를 확정한다 — 새 VM을 만드는 Colab CLI 러너용. (2) Colab UI 노트북용 drive.mount 셀(업로드 완료를 매니페스트로 검증 후 압축 해제)과 산출물 export 셀을 생성하고, Colab이 Drive에 남긴 결과(submission·체크포인트·노트북)를 로컬로 회수·검증한다. '구글 드라이브에 올려줘', 'Drive에 data.zip 배치', '번들 올려', '공유 링크 확인해줘', 'gdown id 등록', '코랩 산출물 로컬로 가져와', '드라이브에 뭐 있는지 확인', '코랩에서 저장한 결과 확인', 'drive.mount 셀 만들어줘' 같은 요청에 반드시 이 스킬을 사용할 것. 노트북을 Colab에서 실행하는 일은 colab-notebook-runner가 담당한다. Drive API·공유 설정 변경은 다루지 않는다(사람이 Drive 웹에서 한다)."
---

# gdrive-bridge — Drive를 통한 로컬 ↔ Colab 파일 브리지

Google Drive for Desktop이 이 PC에 Drive를 로컬 폴더로 마운트하고, Colab은 같은 Drive를 `/content/drive`에 마운트한다.
**로컬 마운트 폴더에 쓰면 Colab에 나타나고, Colab이 Drive에 쓰면 로컬에 나타난다.** 이 스킬은 그 사이의
경로 변환·복사·검증·대기를 표준 라이브러리만 쓰는 스크립트 하나로 제공한다. 별도 인증·API 키가 필요 없다.

```
python ~/.claude/skills/gdrive-bridge/scripts/gdrive_bridge.py <command> ...
```

모든 명령은 stdout에 JSON 하나를 출력하고 진행 로그는 stderr로 보낸다. 종료 코드: `0` 정상 / `2` 검증 실패·마커 타임아웃 / `3` I/O 오류 / `4` 마운트 없음.

## 경로 표기

| 뷰 | 예 |
|---|---|
| **Drive 경로** (이 스킬의 인자) | `MyDrive/proj/bundles/x.zip`, `Shareddrives/팀/x.csv` — 접두사 없으면 `MyDrive/` |
| 로컬 (Drive for Desktop) | `G:\내 드라이브\proj\bundles\x.zip` (`status`가 알려준다) |
| Colab | `/content/drive/MyDrive/proj/bundles/x.zip` |

## 두 가지 Colab 실행 방식과 각각의 데이터 경로

| 실행 방식 | 데이터 넣기 | 산출물 꺼내기 |
|---|---|---|
| **CLI 러너** (colab-notebook-runner; 매 실행 새 VM, Drive 마운트 불가) | `push` → 사람이 링크 공유 → `share-id` → 노트북 첫 셀이 **gdown** (`snippet gdown`) | 러너가 `/content/outputs`를 직접 회수 |
| **Colab UI 노트북** (사람이 브라우저에서 실행, `drive.mount` 가능) | `push` → 노트북 첫 셀 `snippet fetch` (매니페스트 검증 후 압축 해제) | 마지막 셀 `snippet export` → 로컬 `pull --wait` |

Colab CLI의 `drivemount`는 세션마다 브라우저 동의가 필요해 자동화되지 않는다. 그래서 러너 경로는 gdown이다.

## 워크플로우

### 0. `status` — 세션당 1회

```
python ~/.claude/skills/gdrive-bridge/scripts/gdrive_bridge.py status
```

- rc=4 → Drive for Desktop이 꺼져 있거나 없다. 사용자에게 실행·설치를 요청한다. 대신 로그인하지 않는다.
- `my_drive_root` 목록으로 **Colab이 쓰는 계정과 같은 Drive인지** 확인한다. Colab 노트북이 참조하던 파일이 보이면 같은 계정이다. 다른 계정의 Drive에 올리면 Colab에서 영원히 안 보인다.

### A. 데이터 번들 올리기 (`push`)

폴더 여러 개면 zip 하나로 묶는다. Drive는 파일 수에 비례해 느려지고, Colab의 Drive FUSE는 작은 파일 수천 개를 읽을 때 매우 느리다.

```
python ~/.claude/skills/gdrive-bridge/scripts/gdrive_bridge.py push data/splits train_subset --to MyDrive/proj/bundles --zip holdout_min.zip
python ~/.claude/skills/gdrive-bridge/scripts/gdrive_bridge.py push _workspace/cache/lora200.zip --to MyDrive/proj/bundles
```

- 이미 만들어 둔 zip은 그대로 push한다(`--zip` 없이). `--zip NAME`은 소스들을 묶어 새 zip을 만든다.
- 기본 제외: `.git`, `__pycache__`, `.ipynb_checkpoints`, `*.pyc` 등. `--exclude GLOB`로 추가.
- 이미 있으면 에러(rc=3). 정말 교체할 때만 `--overwrite`. 같은 파일을 덮어쓰면 Drive file id는 유지된다.
- `.part`로 쓰고 마지막에 이름을 바꾸므로 반쯤 쓰인 파일이 업로드되지 않는다. 완료 후 `<이름>.manifest.json`(단일 파일) 또는 `<폴더>/_manifest.json`(트리)에 size·sha256을 남긴다.
- **업로드 완료는 로컬에서 관측할 수 없다.** Drive for Desktop이 백그라운드로 올리며 진행률 API가 없다. 사용자에게 "push 완료, 업로드 진행 중, PC 절전 금지"라고 알리고, 완료 확인은 아래 B 또는 C에서 한다.

### B. CLI 러너 경로 — 공유 링크 → gdown id

1. 파일이 Drive 웹에 보이면(업로드 후 수 분) **사람에게 요청한다**: Drive 웹(Colab 계정)에서 그 zip 우클릭 → 공유 → "링크가 있는 모든 사용자"(뷰어) → 링크 복사. 파일 id는 Drive 웹에서만 얻을 수 있고, 공유 설정을 에이전트가 바꾸려 하지 않는다.
2. 받은 링크를 검증한다. gdown이 쓰는 URL로 접근해 공개 여부·파일명·크기를 읽고 로컬 zip과 대조한다.
   ```
   python ~/.claude/skills/gdrive-bridge/scripts/gdrive_bridge.py share-id "https://drive.google.com/open?id=1-A2...&usp=drive_fs" --local _workspace/cache/lora200.zip
   ```
   `ok: false`면 `problems`를 그대로 사용자에게 전달한다(대개 공유 범위가 "제한됨", 다른 파일의 링크, 아직 업로드 중). 통과한 `file_id`를 프로젝트 설정(예: `configs/data_source.yaml`)에 기록한다.
3. 노트북 첫 셀은 `snippet gdown`이 만든다(gdown → 크기·sha256 검사 → 압축 해제 → `DATA_ROOT`). 프로젝트에 이미 gdown 셀이 있으면 id만 넘긴다.
   ```
   python ~/.claude/skills/gdrive-bridge/scripts/gdrive_bridge.py snippet gdown 1-A2... --size 148361935 --unzip-to /content/data --env DATA_ROOT
   ```
4. 실행 직전에 `share-id`를 다시 돌려 id가 아직 공개·같은 크기인지 본다. 실패하면 그 실행은 러너의 `-u <로컬 zip>` 폴백으로 가고 사유를 보고한다.

### C. Colab UI 경로 — drive.mount 셀

```
python ~/.claude/skills/gdrive-bridge/scripts/gdrive_bridge.py snippet fetch MyDrive/proj/bundles/holdout_min.zip --unzip-to /content/data --env DATA_ROOT
```

출력된 셀을 사용자가 노트북 첫 셀에 붙여 넣는다. 셀은 `drive.mount` → 매니페스트와 size·sha256이 일치할 때까지 30초 간격 대기(`--wait` 초, 기본 3600) → 압축 해제 → `DATA_ROOT` 설정. 업로드 완료 검증이 이 셀에서 일어난다.

- 수 GB 파일은 `--no-hash`(크기만 비교). 사람이 손으로 올린 파일(매니페스트 없음)은 `--no-manifest`.
- 산출물 회수: 노트북 마지막에 `snippet export MyDrive/proj/runs/exp-003 --src /content/outputs` 셀을 넣게 한다. 산출물을 `<runs>/<타임스탬프>/`로 복사하고 `_manifest.json`을 쓴 뒤 **맨 마지막에 `_DONE` 마커**를 만들고 `drive.flush_and_unmount()`한다. 로컬에서:
  ```
  python ~/.claude/skills/gdrive-bridge/scripts/gdrive_bridge.py pull MyDrive/proj/runs/exp-003/20260922-1530 --to _workspace/04_runs/exp-003 --wait --timeout 21600
  ```
  학습이 길면 백그라운드로 띄운다. 마커가 마지막이므로 마커가 보이면 나머지는 이미 다 올라가 있다. 매니페스트가 있으면 파일마다 sha256을 대조해 `verified`/`mismatched`/`listed_in_manifest_but_absent`로 보고한다. 재실행 시 크기·시각이 같은 파일은 건너뛴다.

### D. Drive에 이미 있는 것 확인·회수

```
python ~/.claude/skills/gdrive-bridge/scripts/gdrive_bridge.py ls MyDrive/ssafy_ai_out_v7
python ~/.claude/skills/gdrive-bridge/scripts/gdrive_bridge.py pull MyDrive/ssafy_ai_out_v7 --to downloads/v7 --include "*.csv" --include "*.npy"
python ~/.claude/skills/gdrive-bridge/scripts/gdrive_bridge.py pull "MyDrive/Colab Notebooks/v5.ipynb" --to notebooks/from_colab
```

`ls`는 기본 비재귀다. DriveFS 디렉터리 나열은 서버 메타데이터를 당겨 오므로 깊은 트리를 `-R`로 훑으면 분 단위로 걸린다. 매니페스트 없는 폴더도 pull은 되며 `manifest_found: false`로 표시된다. **어댑터·체크포인트는 필요할 때만** — `--exclude "*.safetensors"`로 submission·metrics·로그를 먼저 받고, 가중치는 별도 pull. 읽기가 곧 다운로드다.

## 규칙

- **Drive의 파일을 삭제·이동·공유 설정 변경하지 않는다.** 스크립트에 그런 명령이 없다. 팀 공유 계정일 수 있다.
- **`--overwrite`는 사용자가 교체를 원한다고 말했을 때만.** Colab이 참조 중인 zip을 덮어쓰면 실행 중인 노트북이 깨진다.
- **로컬에서 "업로드 끝났다"고 단정하지 않는다.** 근거는 `share-id`의 크기 일치 또는 Colab 쪽 fetch 셀의 검증뿐이다.
- **push 전에 크기를 본다.** `local_cache_free`(status)보다 큰 것을 올리지 않는다. Drive for Desktop은 로컬 캐시를 거친다.
- 프로젝트별 번들 정의(어떤 csv의 이미지를 담을지, 어느 yaml에 id를 적을지)는 프로젝트 쪽 얇은 래퍼에 둔다. 이 스킬은 옮기고 검증하는 일만 한다.

## 결과 보고 형식

- push: `pushed[].colab` 경로와 `size`, 그리고 다음 단계(사람의 링크 공유 또는 fetch 셀).
- share-id: `file_id`, `probe.name`, `probe.size_bytes_approx`, `problems`. `ok: false`면 기록하지 않는다.
- pull: `dest`, `copied`/`skipped_up_to_date`, `verified`, 문제 목록(`mismatched`, `unreadable`, `listed_in_manifest_but_absent`)을 원문 그대로.

## 문제 해결

| 증상 | 원인·조치 |
|---|---|
| `status` rc=4 | Drive for Desktop 미실행. 사용자가 실행/로그인. 다른 경로면 `GDRIVE_BRIDGE_ROOT=G:\` 로 지정 |
| `share-id`가 `login required` | 공유 범위가 "제한됨". 사용자에게 "링크가 있는 모든 사용자"로 바꿔 달라고 요청 |
| `share-id` 크기 불일치 | 아직 업로드 중이거나 다른 파일의 링크. 몇 분 뒤 재시도, 파일명 확인 |
| Colab fetch 셀이 `not visible in Drive yet`에서 계속 대기 | 로컬 업로드 진행 중, 다른 계정의 Drive, 또는 PC 절전. `status`의 루트 목록과 계정을 확인 |
| pull에서 `not found on Drive (local view)` | Colab이 방금 썼다면 메타데이터 동기화 지연. 1~2분 후 재시도. Colab 쪽에서 `flush_and_unmount()`를 안 했으면 캐시에 남아 있을 수 있다 |
| `mismatched`에 파일이 남음 | 동기화 중이거나 Colab이 같은 이름으로 다시 썼다. 잠시 후 `--include`로 그 파일만 `--force` 재pull |
| Git Bash에서 `/content/...` 인자가 `C:/Program Files/Git/content/...`로 바뀜 | 스크립트가 되돌린다. 다른 도구에 넘길 땐 `MSYS_NO_PATHCONV=1` |

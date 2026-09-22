# gdrive-bridge

로컬 PC와 Google Colab이 같은 Google Drive를 공유한다는 점을 이용해 데이터와 산출물을 주고받는 Claude Code 스킬.
에이전트용 지침은 `SKILL.md`, 도구는 `scripts/gdrive_bridge.py` 하나(표준 라이브러리만 사용).

```
로컬 zip ──push──▶ G:\내 드라이브\proj\bundles\x.zip ═══ Drive for Desktop 업로드 ═══▶ Drive
     CLI 러너(새 VM):  사람이 링크 공유 → share-id 검증 → 노트북 첫 셀 gdown(file id)        (snippet gdown)
     Colab UI 노트북:  drive.mount → 매니페스트로 업로드 완료 검증 → 압축 해제                (snippet fetch)
로컬 폴더 ◀──pull --wait── G:\내 드라이브\proj\runs\<run_id>\ ◀═══ Colab export 셀(_manifest.json + _DONE)  (snippet export)
```

## 처음 한 번만: 세팅

1. **Google Drive for Desktop** 설치 후 Colab에서 쓰는 Google 계정으로 로그인.
   - Windows: 드라이브 문자(예: `G:\`) 아래 `내 드라이브`/`My Drive`가 보이면 준비 완료.
   - macOS: `~/Library/CloudStorage/GoogleDrive-<email>/My Drive`.
   - 스트리밍 모드(기본)는 로컬 캐시를 거쳐 업로드하므로 디스크 여유가 파일 크기 이상 있어야 한다.
2. 확인:
   ```bash
   python ~/.claude/skills/gdrive-bridge/scripts/gdrive_bridge.py status
   ```
   `ok: true`이고 루트 목록에 Colab에서 쓰던 파일이 보이면 같은 계정이다.

마운트 경로가 특이하면 `GDRIVE_BRIDGE_ROOT`로 마운트 루트(또는 My Drive 폴더)를 지정한다.

## 일상 사용

```bash
S=~/.claude/skills/gdrive-bridge/scripts/gdrive_bridge.py
python $S push _workspace/cache/lora200.zip --to MyDrive/proj/bundles          # 번들 업로드 (+ manifest)
python $S share-id "https://drive.google.com/open?id=..." --local _workspace/cache/lora200.zip   # 공유 링크 검증 → file id
python $S snippet gdown <file_id> --size <bytes>                                # 새 VM용 첫 셀 (gdown)
python $S snippet fetch MyDrive/proj/bundles/lora200.zip                        # Colab UI용 첫 셀 (drive.mount)
python $S snippet export MyDrive/proj/runs/exp-003                              # Colab UI용 마지막 셀
python $S pull MyDrive/proj/runs/exp-003/<run_id> --to _workspace/04_runs/exp-003 --wait   # 산출물 회수
python $S ls MyDrive/proj/runs
```

| 명령 | 역할 |
|---|---|
| `status` | 마운트 감지, 로컬↔Colab 경로 매핑, My Drive 루트 목록 |
| `ls DRIVE_PATH [-R] [--depth N]` | 목록 (기본 비재귀 — DriveFS 재귀 나열은 느리다) |
| `push LOCAL... --to DIR [--zip NAME.zip] [--overwrite]` | 업로드 + 매니페스트 |
| `share-id LINK [--local FILE]` | 공유 링크가 gdown으로 받아지는지(공개·이름·크기) 검증 |
| `pull DRIVE_PATH [--to DIR] [--wait] [--include/--exclude GLOB]` | 회수 + sha256 검증 |
| `snippet fetch\|gdown\|export ...` | Colab 셀 생성 |

## 알아둘 것

- 업로드 진행률은 로컬에서 볼 수 없다. 완료 확인은 `share-id`(크기 일치) 또는 Colab 쪽 fetch 셀이 한다.
- 파일 id는 Drive 웹에서만 얻을 수 있다. "링크가 있는 모든 사용자" 공유는 사람이 한다.
- Colab CLI의 `drivemount`는 세션마다 브라우저 동의가 필요해 자동화할 수 없다. 새 VM에서는 gdown을 쓴다.
- 스크립트는 Drive의 파일을 삭제·이동하지 않는다.

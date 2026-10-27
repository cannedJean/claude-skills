# 개념 병합 판단 (main session)

`merge`가 exit 2를 내면 `.kpack/merge_queue.md`**만** 열고 `.kpack/merge_reads.json`을 쓴 뒤 `merge`를 다시 실행한다. 덱, 카드, 이미지는 열지 않는다.

판단 기준:
- **same**: 두 개념이 같은 대상을 가리킬 때. 예: 표기만 다른 경우(기본 키 / 기본키 / PK), 같은 개념을 다른 강의에서 다시 정의한 경우. `[남길 id, 합칠 id]` 순서로 적고, 이미 registry에 있는 id나 먼저 정의된 쪽을 남긴다.
- **distinct**: 이름은 비슷하지만 다른 개념일 때. 예: 후보 키 / 기본 키, 2NF / 3NF, 정규화 / 비정규화.
- 근거가 `slug ... also used for a different term`인 쌍은 두 강의가 같은 slug를 서로 다른 용어에 쓴 경우다. 다른 개념이면 distinct로 두고(뒤쪽은 `-2` 같은 id로 남는다), 같은 개념이면 same으로 합친다.
- 강의 칸이 `registry (...)`인 개념은 이번에 어느 덱도 만들지 않은 기존 registry 개념이다. 덱의 새 개념과 같은 개념이면 `[registry id, 새 id]` 순서로 same에 넣어 기존 id를 지킨다.
- 폐기된 id는 큐에 나오지 않고 되살리지 않는다.
- 확신이 없으면 distinct로 둔다. 잘못 합치면 두 개념의 학습 기록이 섞이고, 그건 되돌리기 어렵다.
- **cross_prerequisites**(선택): 서로 다른 강의의 개념 사이에 선수관계가 분명한 경우만 넣는다. 예: 외래 키는 기본 키를 요구한다. 근거 슬라이드를 모르면 `"kind": "inferred"`와 `"evidence": []`로 둔다.

큐에 나온 모든 쌍을 same이나 distinct 중 한쪽에 넣어야 한다. 이미 답한 쌍은 `.kpack/merge_decisions.json`에 누적되므로 다시 묻지 않는다.

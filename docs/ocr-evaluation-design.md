# agy 이미지 분석 결과를 "이미지를 다시 보지 않고" 검증하는 설계

`agy-slide-analyzer` / `agy-pdf-analyzer`가 agy(Gemini)에 위임한 OCR·구조화 결과를, 수행자와 다른 회사의
LLM(Claude CLI)이 검수하되 이미지를 다시 분석하지 않도록 만든 평가 로직의 설계 근거와 동작을 정리한다.

## 1. 문제

| | 기존 구조 | 문제 |
|---|---|---|
| 수행 | agy 분석가 세션(슬라이드당 1회) | - |
| 검수 | agy 검수자 세션이 **이미지를 다시 보고** 판정(슬라이드당 +1회, 재시도 시 +2회) | 분석을 한 번 더 하는 비용. 쿼터(`RESOURCE_EXHAUSTED`) 두 배 소모 |
| 편향 | 수행자와 검수자가 같은 회사 모델 | 자기선호(self-preference): 같은 계열 모델은 서로의 오류를 비슷하게 놓친다 |

검수를 Claude로 옮기면 편향은 해결되지만, Claude가 이미지를 전부 다시 보면(장당 800~1,600 토큰 + 추론)
토큰 절약을 위해 agy에 위임한 의미가 사라진다. 그래서 질문은 이것이다.

> **이미지를 이해하지 않고도 "이미지를 제대로 설명했는가"를 판정할 수 있는가?**

## 2. 원칙

### 2.1 평가 기준은 작업 전에 봉인한다 (사전 등록)

이미지를 이해하지 않고도 **작업 전에** 만들 수 있는 것이 두 가지 있다.

- **독립 참조(reference)**: 로컬 OCR 엔진이 뽑은 텍스트·단어 박스·신뢰도, 잉크 밀도, 비텍스트 잉크 비율,
  괘선 수, 격자 정렬 단어(표 후보), 제목 띠, 퍼셉추얼 해시. agy를 호출하기 전에 계산해 이미지 sha256과 함께
  `ref/<stem>.json`에 저장한다. agy의 출력은 참조에 영향을 줄 수 없다.
- **고정 규칙(rubric)**: 스키마, 근거성(grounding), few-shot 누출, 임계값. 저장소에 버전 관리되고 골드셋으로
  캘리브레이션된다.

### 2.2 서로 다르게 틀리는 두 관측을 교차한다

로컬 OCR은 틀린다. 그러나 LLM과 **다른 방식으로** 틀린다.

| | 로컬 OCR | 멀티모달 LLM |
|---|---|---|
| 전형적 오류 | 폰트·노이즈로 글자 오독(`NULL`→`NULLS`, `DBMS`→`08146`), 음절 단위 분절(`테 이 블 당`), 구두점 혼동(`1,024`→`1.024`) | 문장 누락, 환각(없는 문장 추가), 조용한 "교정"(`2024`→`2023`, 오타 수정), 표를 평문으로 눌러 씀, 페이지 섞임 |
| 오류의 분포 | 단어 단위로 흩어짐 | 문장·필드 단위로 뭉침 |
| 비용 | 장당 1초 미만, 토큰 0 | 장당 수천 토큰 |

오류가 비상관이므로 **두 관측이 일치하는 부분은 신뢰**하고, **불일치하는 부분만** 제3자에게 보낸다. 이것이
"이미지를 다시 분석하지 않고 검증"의 핵심이다. 이미지 전체를 보는 대신 분쟁 지점만 본다.

### 2.3 점수가 아니라 결정을 낸다

평가의 소비자는 사람이 아니라 파이프라인이다. 0.87이라는 점수보다 "끝났다 / agy에 다시 시켜라 / Claude가 이
줄만 봐라"가 필요하다. 결정은 네 가지다.

```
accept    참조가 동의 → 종료. 검수 토큰 0
retry     명백한 누락(recall < 0.6) 또는 스키마/누출 오류 → agy 재실행. 피드백에 "OCR이 읽었는데 빠진 줄" 목록을 그대로 넣는다
escalate  텍스트로 결판나지 않는 불일치 → Claude에게 분쟁 크롭(또는 축소본)만 제시
unverified 참조를 만들 수 없고 보여줄 것도 없음 → 검증 안 됨을 명시
```
여기에 accept 중 무작위 표본(기본 10%, 최소 2장)을 Claude가 블라인드로 읽는 **감사(audit)** 를 더해 전체
실행의 잔여 오류율을 추정한다.

### 2.4 Claude의 검수는 블라인드 크롭 판독이다

- 스크립트가 **불일치한 OCR 줄의 박스 영역만 크롭**해 저장하고 `review_queue.md`에 크롭 ID만 나열한다.
  agy의 전사문도, OCR의 텍스트도 보여주지 않는다.
- Claude는 크롭을 보고 읽은 글자를 `crop_reads.json`에 적는다. 스크립트가 agy 값과 대조한다(숫자·식별자는
  정확 일치 필수). 검수자가 수행자의 답을 먼저 보지 않으므로 앵커링도 자기선호도 없다.
- 구조 질문(표/도식 미선언)과 감사 표본은 640px 축소본으로 처리한다. 감사는 "읽을 수 있는 줄을 적어라"는
  블라인드 질문이고, 스크립트가 (1) Claude가 읽은 줄이 분석에 있는지(누락), (2) OCR도 Claude도 못 본 분석
  문장이 있는지(환각)를 계산한다. 후자는 Claude가 OCR 고신뢰 줄의 70% 이상을 읽어낸 경우에만 판정한다
  (축소본이 너무 작아 못 읽은 것과 구분하기 위해).

## 3. 메트릭

정규화: NFKC → 소문자 → 공백·구두점 제거. 한국어 OCR이 음절 단위로 끊기므로 단어 경계를 쓰지 않고 **문자
바이그램 집합 겹침**(기본 0.75)으로 매칭한다.

| 실패 유형 | 메트릭 | 정의 |
|---|---|---|
| 누락 | `recall` | 참조 단어(가중치 high=1, mid=0.5, low=0) 중 분석 코퍼스에서 찾은 비율 |
| 환각 | `unsupported_ratio` | 분석 전사 텍스트(title/verbatim/tables/code) 중 참조에 없는 글자 비율. 도식 요소는 설명일 수 있어 제외 |
| 환각(문장 단위) | `unsupported_fields` | 10자 이상이면서 70% 이상 확인되지 않는 verbatim 항목. OCR 노이즈는 흩어지고 환각은 뭉친다는 점을 이용 |
| 숫자 교정 | `digit_recall` | 참조 high 단어의 숫자열(구분자 제거: `1,024`=`1.024`=`1024`) 중 분석에 있는 비율. 빠진 숫자의 줄은 크롭 큐로 |
| 식별자 교정 | `latin_recall` | 참조 high 단어의 영문 식별자(코드·약어) 중 분석에 있는 비율 |
| 표 평문화 | `table_candidate_undeclared` | 격자 정렬 단어(≥3행×≥2열) 또는 괘선 표가 있는데 `tables`가 비고 3줄 이상 코드도 없음 |
| 도식 무시 | `graphics_undeclared` | 텍스트 박스 밖 잉크 비율 ≥ 6%인데 `diagrams`가 빔 |
| 참조 부실 | `ref_thin`, `ref_unreliable`, `agy_rich_ref_empty` | 가중 단어 < 5, 평균 신뢰도 < 55, 참조는 비었는데 분석은 풍부 |
| 해석 과장 | grounding(기존 검증기) + 텍스트 전용 큐 | summary/key_concepts/exam_points가 전사문에서 도출되는지 Claude가 **텍스트만으로** 판정 |

단어 가중치: Tesseract 신뢰도 ≥80 → high, ≥60 → mid, 미만 → low. Windows OCR은 신뢰도가 없으므로 mid.
두 엔진이 모두 읽은 단어는 high. 즉 두 엔진을 함께 쓰면 Windows OCR의 신뢰도 부재를 교차 일치로 보완한다.

결정 규칙(기본 임계값, `calibration.json`으로 대체 가능):

```
validator error                                  → retry
ref thin  & corpus ≥ 40자                        → escalate (thumbnail)
ref thin  & corpus < 40자                        → accept  (양쪽 다 글자가 거의 없음)
recall < 0.60                                    → retry
recall ≥ 0.90 & unsupported ≤ 0.15 & unsupported_fields = 0
  & digit ≥ 0.90 & latin ≥ 0.85 & 구조 트리거 없음  → accept
그 외                                            → escalate (분쟁 줄에 박스가 있으면 crops, 구조·환각 의심이면 thumbnail)
```
`unsupported`는 참조 평균 신뢰도가 55 미만이면 판단에 쓰지 않는다(OCR이 못 읽은 것을 agy 환각으로 몰지 않기 위해).

## 4. 토큰 비용 모델

| 자료 | 크기 | 대략 토큰 |
|---|---|---|
| 한 줄 크롭 | ≈ 800×60 px | ≈ 70 |
| 축소본 | 640×360 px | ≈ 300 |
| 원본 슬라이드 전체 | 1280×720 ~ 1920×1080 | 800 ~ 1,600 + 긴 추론 |

슬라이드의 20%가 에스컬레이션되고 10%가 감사된다고 하면 100장 덱의 Claude 이미지 토큰은 대략
20×(2~4 크롭 또는 축소본 1) + 10×300 ≈ 6k~9k. 전체를 다시 보는 방식(100×1,200 + 추론)의 약 1/10이다.
agy 세션은 슬라이드당 1회(재시도 시 2회)로, 기존 2~4회의 절반 이하다. `report.md`가 실제 큐의 픽셀 합으로
추정 토큰을 표시한다.

## 5. 골드셋: 게이트 캘리브레이션과 벤치마크를 한 번에

사용자가 결과 JSON 10~30장을 교정해 `gold/<stem>.json`으로 저장하면,

- `calibrate`: 골드 대비 **진짜** recall/정밀도와 OCR 기반 **대리** 지표를 함께 계산하고, false-accept(진짜
  recall 또는 정밀도 < 0.9인 슬라이드를 accept한 비율) ≤ 목표(5%)인 조합 중 가장 많이 accept하는
  `recall_accept`/`unsupported_accept`를 격자 탐색으로 고른다(동률이면 더 엄격한 쪽). 엔진별로 "골드 텍스트
  중 그 엔진이 읽은 비율"도 보고해 엔진 선택 근거를 준다. 결과는 `calibration.json`에 쓰이고 이후 실행이
  자동으로 읽는다.
- `bench`: 임의 실행의 진짜 recall/정밀도/문자 유사도/숫자·셀 정확도/환각률을 `bench.json`에 누적해 프롬프트·
  모델 버전을 비교한다.

10장 미만이면 경고가 붙는다. 골드셋 없이도 기본 임계값으로 동작하지만 그때의 임계값은 경험치다.

## 6. 파이프라인

```
slide.png ─┬─> [0] ocr_ref.build_reference  (agy 호출 전) ──> ref/<stem>.json
           └─> [1] agy analyst ──> slide_analysis.json
[2] slide_validate.validate_analysis (스키마/근거성/누출)
[3] ocr_score.score(analysis, ref, thresholds, validator_errors) ──> decision
      retry    → feedback_text(): 빠진 줄·숫자·확인 안 된 조각을 한국어로 → analyst 재실행(1회)
      escalate → crops.prepare(): 분쟁 줄 크롭 / 축소본 → results/<stem>.json의 queue
[4] build_report: 감사 표본 선택(stem 해시로 결정적) → review_queue.md / grounding_queue.md / report.md
[5] Claude: 큐만 열어 crop_reads.json 작성 → resolve → crops.judge() → accepted | needs_attention
```
PDF 변형은 페이지에 텍스트 레이어가 있으면 그것을 **최고 신뢰 참조**로(`primary: pdf_text`), 없으면 pypdf로
내장 페이지 이미지를 꺼내(실패 시 pymupdf 렌더) 같은 OCR 참조를 만든다. 둘 다 안 되면 `unverified`.
report/resolve/calibrate/bench는 `slide_analyze.py`의 코드를 그대로 재사용한다(페이지 이미지를 `ref/page_XXXX.png`
라는 "슬라이드"로 취급).

## 7. 검증된 동작 (합성 슬라이드 + 가짜 agy)

`scripts/tests/synth.py`가 정답을 100% 아는 슬라이드를 렌더하고, `tests/fake_agy_task.py`가 agy 대신 시나리오별
분석을 쓴다. Tesseract 5.5(kor+eng) 기준:

| 시나리오 | 결정 | 비고 |
|---|---|---|
| 완벽한 전사 | accept | OCR이 `NULL`→`NULLS`, `DBMS`→`08146`로 오독해도 가중치/퍼지 매칭으로 흡수 |
| 문장 1개 + 코드 누락 | retry → 재시도 accept | 피드백에 빠진 두 줄이 그대로 들어감 |
| 없는 문장 추가 | escalate (thumbnail) | 전역 비율(0.17)과 필드 단위 신호 모두 반응; 축소본 블라인드 읽기로 환각 의심 확정 |
| 숫자 2개 변경 | escalate (crops, ≈126토큰) | Claude 판독 `2024`/`1,024` vs 분석 `2023`/`1,042` → `agy_wrong` → needs_attention |
| 표를 평문으로 | escalate (thumbnail) | `table_candidate_undeclared` |
| 거의 빈 분석 | retry | 6개 줄 누락 지적 |
| few-shot 누출 | retry → accept | 검증기 error |
| 제목만 있는 슬라이드에 풍부한 분석 | escalate (thumbnail) | `agy_rich_ref_empty` |
| agy 쿼터 소진 | 중단, 슬라이드 pending, exit 3 | 다음 실행이 이어서 처리 |

## 7.1 실제 슬라이드 파일럿 (SVM 강의 3장, agy gemini-3.8-flash-high, Windows OCR + Tesseract)

첫 실행은 3장 모두 escalate(큐 14건, ≈1,165토큰)였다. 원인은 전부 **참조 쪽 잡음**이었고, 아래 네 가지를 고친
뒤 `rescore`(agy 재호출 없이 재채점)하자 2장 accept, 1장 escalate(크롭 2건)로 줄었다. Claude의 블라인드
판독과 감사 2장 결과 3장 모두 accepted, 큐 4건 ≈654토큰. agy 세션은 4회(슬라이드 3 첫 시도에서 agy가 셸
명령을 시도하다 파일을 못 써 재시도).

| 잡음 | 증상 | 조치 |
|---|---|---|
| 불릿 점·산점도 점을 `0`/`5`로 읽음 | 숫자 분쟁 → 불필요한 크롭 | 1글자 OCR "단어"는 가중치 low, 숫자 검사는 2자리 이상만 |
| `초평면(Hyperplane)`을 `초평면대yperplane`으로 붙여 읽음 | 식별자 누락 | 식별자는 포함 관계(4자 이상)도 일치로 인정 |
| 불릿 목록의 정렬을 표 후보로 오인 | 모든 텍스트 슬라이드가 `table_candidate_undeclared` | 짧은 행(2~6단어)이고 단어 대부분이 공유 열 위에 있을 때만 표 후보 |
| 상단 배너 장식이 비텍스트 잉크로 집계 | 순수 텍스트 슬라이드도 `graphics_undeclared` | 상단 13%·하단 7% 띠는 그래픽 통계에서 제외 |

남은 크롭 2건은 Windows OCR이 도식의 `y_i = +1` 기호를 `사도 BF시`로, `Hyperplane`을 `Hyverplane`으로 읽은
것이었다. 둘 다 Claude가 70토큰짜리 크롭을 읽어 agy가 맞았음을 확인했다. 단일 엔진의 오독이 크롭으로
흡수되는 설계대로다.

## 8. 한계와 열어 둔 것

- **OCR이 못 보는 것**: 도식의 관계(화살표 방향, 카디널리티), 색·강조, 레이아웃 의미. 축소본 질문과 감사
  표본으로만 다루며, 도식 정확도 자체는 측정하지 않는다. 도식이 중요한 덱이라면 `--audit-rate`를 올리거나
  해당 슬라이드만 `--force`로 agy에 재분석을 시키는 것이 낫다.
- **표 격자/비텍스트 감지는 휴리스틱**이다. 자동 reject의 근거로 쓰지 않고 축소본 질문으로만 쓴다.
- **단일 엔진의 오독**은 high 가중치로 분쟁 줄이 되어 크롭이 늘어난다(비용은 작다). 두 엔진을 함께 쓰면
  줄어든다.
- **블라인드의 한계**: 구조 질문(`thumb_struct`)은 분석가의 선언을 보여줘야 비교가 가능하므로 블라인드가
  아니다. 닫힌 질문으로 제한했다.
- **임계값은 자료 종류마다 다르다**. 손글씨·저해상도 스캔은 OCR 신뢰도가 낮아 `ref_unreliable`이 자주 켜지고
  escalate가 늘어난다. 그런 자료는 골드셋 캘리브레이션 없이는 수치 게이트를 믿지 말 것.
- 기존 agy 검수자 모드는 `--reviewer agy`로 남겨 두었다. 같은 덱을 두 모드로 돌려 `bench`로 비교하면 이
  설계의 효과를 수치로 확인할 수 있다.

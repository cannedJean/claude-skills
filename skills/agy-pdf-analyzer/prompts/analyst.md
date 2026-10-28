# 역할 (Persona)
당신은 **{subject} 과목 전문 교수설계자이자 강의자료 디지털 아카이빙 전문가**입니다.
- 10년 이상 {subject} 과목을 가르쳐 온 강사 경력이 있고,
- 강의 PDF의 각 페이지를 **한 글자도 놓치지 않고 전사(OCR 교정)** 한 뒤, 학습자가 원본 없이도 내용을 복원할 수 있을 만큼 **구조화된 데이터**로 기록하는 일을 합니다.
- 당신의 산출물은 사람이 아니라 **다른 AI 분석 파이프라인의 입력**으로 쓰입니다. 따라서 추측보다 **정확성·근거·일관성**이 훨씬 중요합니다.

# 맥락 (Context)
- 자료: {deck_context} (원본 PDF `{pdf_name}`, 총 {total}쪽. 실제 내용은 PDF 페이지 화면으로만 판단하세요.)
- 작업 디렉터리의 **`{chunk_file}`** 은 {chunk_desc}이며, PDF 페이지가 **{chunk_pages}개** 들어 있습니다.
- 파일 보기 도구로 `{chunk_file}`을 **직접** 여세요. 페이지들이 순서대로 렌더링되어 보입니다. 셸 명령을 실행하거나 PDF를 변환하는 코드를 작성하지 마세요.
- 이번에 분석할 페이지는 아래 표의 **{n_targets}개**입니다. 표에 없는 페이지는 앞뒤 맥락 파악에만 쓰고, 분석 파일을 만들지 마세요.

| `{chunk_file}` 안에서의 순서 | 전체 쪽 번호 (`page`) | 작성할 파일 |
| --- | --- | --- |
{page_table}

- **페이지를 서로 섞지 마세요.** 각 JSON에는 해당 페이지 화면에 실제로 보이는 내용만 담습니다. 앞뒤 페이지의 내용을 끌어오지 마세요.
- 슬라이드 뷰어 UI(예: 좌우 가운데의 작은 `←`, `→` 화살표, 재생 버튼)는 페이지 내용이 아닙니다. 전사하지 마세요.
- 모든 페이지에 반복되는 로고/슬로건/워터마크는 `region: "header/footer"`로만 기록하고 핵심 내용으로 취급하지 마세요. `header/footer`에는 여러 쪽에 반복되는 로고·슬로건·단원 띠·Confidential 표시만 넣고, 이 페이지에만 있는 문장은 넣지 마세요.
- 재분석 요청에는 **로컬 OCR과 대조한 결과**(빠진 줄, 확인되지 않은 숫자/조각)가 피드백으로 붙을 수 있습니다. OCR은 글자를 오독하거나 음절 단위로 끊을 수 있으므로, 피드백의 문자열을 그대로 베끼지 말고 **해당 페이지를 다시 보고 실제 글자를** 전사하세요. 페이지에 없는 내용은 피드백에 있어도 넣지 마세요.

# 작업 절차 (Chain-of-Thought — 페이지마다 반드시 이 순서로 생각하고, 생각의 결과를 그 페이지의 `reasoning`에 간결히 남기세요)
1. **관찰(observation)**: 페이지의 영역을 위→아래, 왼→오른쪽 순서로 훑습니다. 어떤 영역(제목, 본문, 표, 코드, 도식, 화면 캡처, 강조 표시, 각주, 페이지 번호)이 있는지 나열합니다.
2. **전사(transcription)**: 각 영역의 텍스트를 **원문 그대로** 옮깁니다. 맞춤법을 고치거나 번역·요약하지 마세요. 줄바꿈이 의미 단위면 `\n`으로 보존합니다.
   - 글머리 기호(•, –, ➢, ❖, 1.)와 특수 문자(…, ‘ ’, “ ”, –)는 보이는 그대로 적습니다. 코드 안의 스마트 따옴표·긴 대시·띄어쓰기도 문법에 맞게 고치지 말고 보이는 그대로 적습니다(고친 것이 있으면 `uncertain`에 `원문: …`으로 남기세요).
   - 작은 글자(화면 캡처 속 UI 글자 포함)는 생략하지 말고 읽은 만큼 적습니다. 읽기 어려운 글자는 추측하지 말고 `[?]`로 표시하고 `uncertain`에 기록합니다.
3. **구조화(structure)**: 표는 `tables`에 행/열 그대로, 코드(SQL, HTML, JavaScript 등)는 `code_blocks`에 들여쓰기·대소문자·세미콜론까지 그대로, 도식(ERD, 흐름도, 계층도, 벤다이어그램, 문법 분해도 등)은 `diagrams`에 구성요소와 관계(화살표 방향, 카디널리티 표기 포함)로 기록합니다.
   - **표·코드·도식으로 옮긴 텍스트는 `verbatim_text`에 다시 쓰지 않습니다.** `verbatim_text`에는 제목·본문 문장·캡션·주석·쪽 번호처럼 표/코드/도식 밖에 있는 글자만 남깁니다. 코드 상자 안의 주석(`<!-- -->`, `//`)과 렌더링 결과·콘솔 출력 상자도 `code_blocks`에만 적습니다(language: html | javascript | text).
   - 3-1. 화면 캡처(브라우저 렌더링 결과, 개발자 도구, 콘솔, IDE·앱 화면)는 `diagrams`에 `kind: "screenshot"`으로 캡처마다 하나씩 선언합니다. `elements`에는 캡처 안에 보이는 글자를 보이는 그대로 나열합니다(탭·메뉴 이름, 입력창 안내 문구, 배지, 잘린 단어는 잘린 그대로). 너무 작아 읽을 수 없으면 생략하지 말고 `[?]`로 적습니다. 캡처 속 코드·DOM 트리·콘솔 출력 줄은 `code_blocks`(language: html | text)에 한 번만 적습니다.
   - 3-2. 강조 표시(빨간 박스, ✕/✓ 아이콘, 색 밑줄, 형광펜, 콜아웃 화살표)는 `diagrams`에 `kind: "annotation"`으로 선언하고 `relations`에 `표시 -> 대상 : 의미`로 적습니다(예: `빨간 박스 -> count++ : 클릭할 때 실행되는 줄`, `보라색 밑줄 -> onSubmit : 이벤트 처리 함수`). `reasoning.observation`에 적은 캡처·강조 표시는 모두 `diagrams`에도 있어야 합니다.
   - 3-3. `relations`가 하나라도 있으면 `illustration`을 쓰지 마세요. 코드·식의 부분마다 이름표를 붙인 그림은 `syntax_breakdown`입니다. 색·밑줄·괄호로 대응을 표시했으면 그 표시도 `relations`에 적습니다.
4. **해석(interpretation)**: 이 페이지가 가르치려는 개념, 강의 흐름에서의 역할, 시험/실습에서 중요해 보이는 포인트를 정리합니다. 해석은 반드시 전사한 내용에 근거해야 하며, 페이지에 없는 지식을 사실처럼 덧붙이지 마세요.
   - 제목(과 머리글·로고)만 있는 표지·구분·휴식 페이지(`title`, `section_divider`)는 `exam_points: []`로 두고, `key_concepts`는 제목 용어 1개 이하(설명: '단원 제목'), `summary`는 어떤 단원이 시작되는지 1문장만 적습니다. 다음 페이지에 나올 내용을 예측하거나 일반 지식으로 채우지 마세요.
   - 어떤 페이지든 `exam_points`·`key_concepts`·`summary`에 전사문에 없는 API·수식어·옵션 이름을 새로 쓰지 마세요.
5. **자기검증(self_check)**: 그 페이지를 **한 번 더** 훑어 누락된 텍스트, 숫자·기호·코드 키워드 오타, 표의 행/열 수, 캡처·강조 표시 선언이 맞는지, 코드 텍스트를 `verbatim_text`에 중복으로 적지 않았는지, 그리고 **다른 페이지의 내용이 섞이지 않았는지** 확인하고 결과를 적습니다. `key_concepts`의 `term`은 **페이지에 적힌 표기 그대로**여야 합니다(번역 금지). `confidence`를 0.9 이상으로 주려면 `uncertain`이 비어 있고 `[?]`가 없어야 합니다.

# 출력 형식 (엄격)
위 표의 페이지마다 JSON 파일 **하나씩, 총 {n_targets}개**를 작업 디렉터리에 UTF-8로 작성하세요. 파일 이름은 표의 "작성할 파일" 그대로입니다. 한 페이지 분석이 끝나면 바로 그 파일을 저장하고 다음 페이지로 넘어가세요. 다른 파일은 만들지 말고, PDF는 수정하지 마세요.
각 파일의 JSON 스키마:
```json
{{
  "page": <정수, 표의 "전체 쪽 번호">,
  "reasoning": {{
    "observation": "영역 목록과 배치 (1-3문장)",
    "structure": "표/코드/도식/캡처/강조 표시의 존재와 구성 (1-3문장)",
    "interpretation": "이 페이지의 교육적 의도 (1-3문장)",
    "self_check": "재확인 결과: 누락/오타/페이지 혼동 점검 내역 (1-2문장)"
  }},
  "slide_type": "title | toc | section_divider | concept | diagram | table | code | example | practice | summary | other 중 하나",
  "title": "페이지 제목 원문 또는 null",
  "printed_page_number": "페이지에 인쇄된 페이지 번호 문자열 또는 null",
  "verbatim_text": [{{"region": "title | body | header/footer | caption | note | diagram_label | page_number", "text": "원문"}}],
  "tables": [{{"caption": "표 제목 또는 null", "headers": ["..."], "rows": [["..."]]}}],
  "code_blocks": [{{"language": "sql | html | javascript | python | text 등", "code": "원문 코드"}}],
  "diagrams": [{{"kind": "erd | flowchart | hierarchy | architecture | venn | syntax_breakdown | screenshot | annotation | illustration | other", "description": "무엇을 그렸는지", "elements": ["구성요소"], "relations": ["A -> B : 관계 설명"]}}],
  "key_concepts": [{{"term": "페이지 표기 그대로", "explanation": "페이지 근거 설명(한국어)"}}],
  "summary": "페이지 핵심 내용 한국어 요약 (2-4문장)",
  "lecture_role": "강의 흐름에서의 역할 (1문장)",
  "exam_points": ["시험/실습에서 중요해 보이는 포인트"],
  "uncertain": [{{"item": "불확실한 부분", "reason": "이유"}}],
  "confidence": <0.0~1.0, 전사 정확도에 대한 자기 평가>
}}
```
- 해당 요소가 없으면 빈 배열 `[]` 또는 `null`을 쓰세요. 키를 생략하지 마세요.
- `slide_type`: `section_divider`는 단원 이름만 크게 있는 페이지(정의 문장이 있으면 `concept`), `other`는 휴식·인사·'이어서' 페이지, `example`은 앞 개념을 적용한 예제·실행 결과, `code`는 새 문법을 코드로 제시하는 페이지입니다.
- `diagrams.kind`: `syntax_breakdown`은 코드·식의 부분마다 이름표를 붙인 분해도, `screenshot`은 화면 캡처, `annotation`은 강조 표시, `illustration`은 사진·장식 그림처럼 구성요소 사이 관계가 없는 그림에만 씁니다.
- `{chunk_file}`에서 보이는 페이지 수가 {chunk_pages}개가 아니면, 보이지 않는 페이지의 파일은 만들지 말고 최종 응답에 그 사실을 적으세요. 내용을 지어내지 마세요.
- 최종 응답에는 JSON을 반복하지 말고 작성한 파일 이름과 각 confidence만 적으세요.

# 예시 (Few-shot) — 형식과 상세도만 참고하세요. 아래 예시의 내용은 이번 PDF와 **무관한 가상의 페이지**이므로 절대 복사하지 마세요.

## 예시 A: 개념 + 표 페이지 (가상의 901쪽)
```json
{{
  "page": 901,
  "reasoning": {{
    "observation": "상단에 제목, 중앙에 2열 비교표, 하단에 회색 각주, 우하단 페이지 번호가 있다.",
    "structure": "표 1개(3행 x 3열). 코드와 도식 없음.",
    "interpretation": "파일 시스템 대비 DBMS의 장점을 비교해 DBMS 도입 동기를 설명한다.",
    "self_check": "표 3행 모두 전사 확인, 각주의 '무결성' 철자 재확인. 누락 없음. 앞뒤 페이지 내용 혼입 없음."
  }},
  "slide_type": "table",
  "title": "EXAMPLE 파일 시스템 vs DBMS",
  "printed_page_number": "901",
  "verbatim_text": [
    {{"region": "title", "text": "EXAMPLE 파일 시스템 vs DBMS"}},
    {{"region": "note", "text": "※ DBMS는 데이터 무결성을 제약 조건으로 보장한다."}},
    {{"region": "page_number", "text": "901"}}
  ],
  "tables": [{{"caption": null, "headers": ["구분", "파일 시스템", "DBMS"], "rows": [["중복", "높음", "최소화"], ["동시 접근", "어려움", "지원"], ["무결성", "응용 프로그램 책임", "제약 조건"]]}}],
  "code_blocks": [],
  "diagrams": [],
  "key_concepts": [
    {{"term": "DBMS", "explanation": "중복 최소화, 동시 접근 지원, 제약 조건으로 무결성을 보장하는 관리 시스템으로 제시됨"}},
    {{"term": "무결성", "explanation": "파일 시스템에서는 응용 프로그램이, DBMS에서는 제약 조건이 책임진다고 비교됨"}}
  ],
  "summary": "파일 시스템과 DBMS를 중복, 동시 접근, 무결성 세 기준으로 비교한다. DBMS는 중복을 최소화하고 동시 접근을 지원하며 제약 조건으로 무결성을 보장한다는 점을 강조한다.",
  "lecture_role": "DBMS가 필요한 이유를 제시하는 동기 부여 페이지",
  "exam_points": ["파일 시스템 대비 DBMS 장점 3가지", "무결성 보장 주체의 차이"],
  "uncertain": [],
  "confidence": 0.95
}}
```

## 예시 B: SQL 코드 페이지 (가상의 902쪽)
```json
{{
  "page": 902,
  "reasoning": {{
    "observation": "제목, 좌측 코드 박스, 우측 실행 결과 캡처, 하단 설명 문장이 있다.",
    "structure": "SQL 코드 블록 1개, 결과 표 1개(2행). 결과 캡처의 셋째 열 머리글이 흐릿하다.",
    "interpretation": "WHERE 절로 조건 조회하는 기본 SELECT 사용법을 실습 형태로 보여준다.",
    "self_check": "코드의 세미콜론·따옴표 재확인. 결과표 셋째 열 머리글은 판독 불가로 [?] 처리."
  }},
  "slide_type": "code",
  "title": "EXAMPLE SELECT 기본",
  "printed_page_number": "902",
  "verbatim_text": [
    {{"region": "title", "text": "EXAMPLE SELECT 기본"}},
    {{"region": "body", "text": "WHERE 절은 조건을 만족하는 행만 반환한다."}},
    {{"region": "page_number", "text": "902"}}
  ],
  "tables": [{{"caption": "실행 결과", "headers": ["id", "name", "[?]"], "rows": [["1", "kim", "A"], ["3", "lee", "A"]]}}],
  "code_blocks": [{{"language": "sql", "code": "SELECT id, name, grade\nFROM example_students\nWHERE grade = 'A';"}}],
  "diagrams": [{{"kind": "screenshot", "description": "실행 결과 화면 캡처 (결과 표는 tables[0]에 기록)", "elements": [], "relations": []}}],
  "key_concepts": [
    {{"term": "WHERE", "explanation": "조건을 만족하는 행만 반환하는 절로 설명됨"}},
    {{"term": "SELECT", "explanation": "조회할 컬럼을 지정하는 구문으로 예제에 사용됨"}}
  ],
  "summary": "SELECT와 WHERE를 사용해 grade가 'A'인 학생만 조회하는 예제를 보여준다. 결과로 2개 행이 반환된다.",
  "lecture_role": "조건 조회 문법을 예제로 익히는 실습 페이지",
  "exam_points": ["WHERE 절의 역할", "문자열 비교 시 작은따옴표 사용"],
  "uncertain": [{{"item": "결과표 셋째 열 머리글", "reason": "캡처 해상도가 낮아 판독 불가 (코드상 grade로 추정되나 확인 불가)"}}],
  "confidence": 0.85
}}
```

## 예시 C: 코드 + 실행 화면 캡처 + 강조 표시 페이지 (가상의 903쪽)
```json
{{
  "page": 903,
  "reasoning": {{
    "observation": "상단에 제목, 왼쪽에 코드 상자 2개, 오른쪽에 브라우저 실행 화면 캡처와 개발자 도구 콘솔 캡처, 코드 한 줄을 감싼 빨간 박스, 하단 캡션, 우하단 페이지 번호가 있다.",
    "structure": "코드 블록 2개(javascript, html)와 콘솔 출력 1개(text), 화면 캡처 2개(screenshot), 빨간 박스 강조 1개(annotation). 표 없음.",
    "interpretation": "버튼 클릭으로 반응형 값을 1씩 올리고 그 값이 화면과 콘솔에 반영되는 흐름을 보여준다.",
    "self_check": "++ 연산자와 따옴표 재확인. 콘솔 캡처의 잘린 탭 이름은 보이는 그대로 'Netw'로 적음. 코드 텍스트를 verbatim_text에 다시 적지 않음. 앞뒤 페이지 내용 혼입 없음."
  }},
  "slide_type": "code",
  "title": "EXAMPLE 카운터 버튼",
  "printed_page_number": "903",
  "verbatim_text": [
    {{"region": "title", "text": "EXAMPLE 카운터 버튼"}},
    {{"region": "caption", "text": "버튼을 누를 때마다 숫자가 1씩 증가한다."}},
    {{"region": "page_number", "text": "903"}}
  ],
  "tables": [],
  "code_blocks": [
    {{"language": "javascript", "code": "const exampleCount = ref(0)\nfunction addOne() {{\n  exampleCount.value++\n}}"}},
    {{"language": "html", "code": "<button @click=\"addOne\">+1</button>\n<p>{{{{ exampleCount }}}}</p>"}},
    {{"language": "text", "code": "exampleCount: 3"}}
  ],
  "diagrams": [
    {{"kind": "screenshot", "description": "브라우저 실행 화면 캡처", "elements": ["+1", "3"], "relations": []}},
    {{"kind": "screenshot", "description": "개발자 도구 콘솔 캡처", "elements": ["Elements", "Console", "Netw", "[?]"], "relations": []}},
    {{"kind": "annotation", "description": "코드 한 줄을 감싼 빨간 박스", "elements": ["빨간 박스"], "relations": ["빨간 박스 -> exampleCount.value++ : 클릭할 때 실행되는 줄"]}}
  ],
  "key_concepts": [
    {{"term": "ref", "explanation": "exampleCount를 반응형 값으로 만들어 값이 바뀌면 화면의 숫자도 바뀐다."}},
    {{"term": "@click", "explanation": "버튼을 클릭하면 addOne 함수가 실행되도록 연결한다."}}
  ],
  "summary": "버튼의 @click에 addOne 함수를 연결해 클릭할 때마다 exampleCount를 1 올리는 예제다. 실행 화면에는 3이 표시되고 콘솔에도 같은 값이 출력된다.",
  "lecture_role": "이벤트 연결과 반응형 값 변경을 함께 보여 주는 코드 예제 페이지",
  "exam_points": ["@click으로 버튼 클릭과 함수를 연결하는 방법", "exampleCount.value++로 반응형 값을 바꾸는 코드"],
  "uncertain": [{{"item": "콘솔 캡처 오른쪽 끝의 작은 글자", "reason": "너무 작아 판독 불가([?])"}}],
  "confidence": 0.85
}}
```

{feedback}
이제 `{chunk_file}`을 열어 위 절차대로 표의 {n_targets}개 페이지를 분석하고 페이지별 JSON 파일을 작성하세요.

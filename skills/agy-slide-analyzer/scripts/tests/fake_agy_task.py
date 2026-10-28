#!/usr/bin/env python3
"""fake_agy_task.py - stand-in for agy-delegate's agy_task.py so the pipeline can be exercised without agy.

Usage: set AGY_TASK=<this file> before running slide_analyze.py / pdf_analyze.py. It accepts the same
`run -w WORKDIR -f PROMPT ...` arguments, looks for an image in WORKDIR and writes slide_analysis.json
(or page_XXXX.json files for the PDF analyzer) from a canned analysis chosen by FAKE_AGY_SCENARIO:

  perfect     the full synthetic slide transcription (tests/synth.py's slide_001-003 each get their own)
  omit        drops the second body sentence and the code block (-> retry; the retry is perfect when the
              prompt carries feedback, mimicking an analyst that fixes what it is told)
  hallucinate adds a sentence the image does not contain (-> escalate, thumbnail)
  digits      changes two numbers (-> escalate, crops)
  leak        copies a few-shot title (validator error -> retry; the retry is clean)
  quota       prints agy's RESOURCE_EXHAUSTED line and writes nothing
  quota_on_retry  first attempt leaks (-> retry), the retry hits RESOURCE_EXHAUSTED

omit / hallucinate / digits change only the main slide (slide_002 / the PDF pages). FAKE_CALL_LOG, when set,
gets one line per call ("<file> <attempt>") so tests can count agy calls.
The canned content matches tests/synth.py's slides; anything else is just a smoke test of plumbing.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

CANNED = {
    "slide_type": "concept", "title": "3장. 기본 키와 외래 키", "printed_page_number": "12",
    "verbatim_text": [
        {"region": "title", "text": "3장. 기본 키와 외래 키"},
        {"region": "body", "text": "기본 키는 NULL을 허용하지 않는다."},
        {"region": "body", "text": "기본 키는 테이블당 하나만 존재한다. (2024년 기준 1,024개 행)"},
        {"region": "page_number", "text": "12"}],
    "tables": [{"caption": None, "headers": ["구분", "파일 시스템", "DBMS"],
                "rows": [["중복", "높음", "최소화"], ["무결성", "응용 책임", "제약 조건"]]}],
    "code_blocks": [{"language": "sql", "code": "SELECT id, name FROM students WHERE grade = 'A';"}],
    "diagrams": [{"kind": "other", "description": "원과 사각형이 선으로 연결된 도식", "elements": ["원", "사각형"],
                  "relations": ["원 -> 사각형 : 연결"]}],
    "key_concepts": [{"term": "기본 키", "explanation": "NULL 불허, 테이블당 하나"}],
    "summary": "기본 키의 두 성질(NULL 불허, 테이블당 하나)을 설명하고 파일 시스템과 DBMS를 비교하는 표와 SELECT 예제를 보여준다.",
    "lecture_role": "기본 키 개념 도입", "exam_points": ["기본 키는 NULL을 허용하지 않는다"], "uncertain": [],
    "reasoning": {"observation": "제목, 본문 2문장, 코드, 표, 도식", "structure": "표 1개, 코드 1개, 도식 1개",
                  "interpretation": "기본 키 개념", "self_check": "확인"},
    "confidence": 0.9,
}

TITLE_ONLY = {
    "slide_type": "section_divider", "title": "2부. 관계형 모델", "printed_page_number": None,
    "verbatim_text": [{"region": "title", "text": "2부. 관계형 모델"}],
    "tables": [], "code_blocks": [], "diagrams": [],
    "key_concepts": [{"term": "관계형 모델", "explanation": "단원 제목"}],
    "summary": "관계형 모델 단원이 시작되는 구분 슬라이드다.", "lecture_role": "2부 시작", "exam_points": [], "uncertain": [],
    "reasoning": {"observation": "가운데 큰 제목만 있다", "structure": "표·코드·도식 없음",
                  "interpretation": "단원 구분", "self_check": "제목 철자 확인"},
    "confidence": 0.95,
}

DIAGRAM = {
    "slide_type": "diagram", "title": "ERD 예시", "printed_page_number": None,
    "verbatim_text": [{"region": "title", "text": "ERD 예시"}],
    "tables": [], "code_blocks": [],
    "diagrams": [{"kind": "erd", "description": "학생, 수강, 과목 상자가 화살표로 이어진 ERD",
                  "elements": ["학생", "수강", "과목"], "relations": ["학생 -> 수강", "수강 -> 과목"]}],
    "key_concepts": [{"term": "ERD", "explanation": "학생-수강-과목 관계를 상자와 화살표로 나타낸 도식"}],
    "summary": "학생, 수강, 과목 세 개체를 화살표로 이은 ERD 예시를 보여준다.", "lecture_role": "ERD 표기 예시",
    "exam_points": [], "uncertain": [],
    "reasoning": {"observation": "제목과 상자 3개, 화살표 2개", "structure": "도식 1개(ERD)",
                  "interpretation": "개체 관계 표현", "self_check": "상자 이름 확인"},
    "confidence": 0.95,
}

BY_FILE = {"slide_001.png": TITLE_ONLY, "slide_003.png": DIAGRAM}


def variant(scenario: str, retry: bool, base: dict = None) -> dict:
    main = base is None or base is CANNED
    d = json.loads(json.dumps(CANNED if base is None else base))
    if retry:  # an analyst that applies the feedback
        return d
    if scenario == "omit" and main:
        del d["verbatim_text"][2]
        d["code_blocks"] = []
    elif scenario == "hallucinate" and main:
        d["verbatim_text"].append({"region": "body", "text": "외래 키는 참조 무결성을 보장하며 복합 키로 구성될 수 있다."})
    elif scenario == "digits" and main:
        d["verbatim_text"][2]["text"] = "기본 키는 테이블당 하나만 존재한다. (2023년 기준 1,042개 행)"
    elif scenario in ("leak", "quota_on_retry"):
        d["title"] = "EXAMPLE 파일 시스템 vs DBMS"
    return d


def log_call(path: str, line: str) -> None:
    """Append under a lock file: parallel sessions append at once and Windows appends are not atomic."""
    lock, fd = path + ".lock", None
    for _ in range(2000):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except OSError:
            time.sleep(0.005)
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    finally:
        if fd is not None:
            os.close(fd)
            os.remove(lock)


def quota() -> int:
    sys.stderr.write('AGY_ERROR: {"code": 429, "message": "RESOURCE_EXHAUSTED: quota reached. Resets in 2h 10m."}\n')
    print(json.dumps({"status": "error"}))
    return 3


def main(argv) -> int:
    args = argv[1:]
    if args and args[0] == "preflight":
        print(json.dumps({"ok": True, "checks": {"agy": "fake"}}))
        return 0
    wd = Path(args[args.index("-w") + 1])
    prompt = Path(args[args.index("-f") + 1]).read_text(encoding="utf-8")
    scenario = os.environ.get("FAKE_AGY_SCENARIO", "perfect")
    retry = "이전 시도에 대한 피드백" in prompt
    m = re.search(r"이번에 분석할 파일: \*\*`([^`]+)`\*\* \(전체 (\d+)장 중 \*\*(\d+)번째\*\*", prompt)
    if os.environ.get("FAKE_CALL_LOG"):
        log_call(os.environ["FAKE_CALL_LOG"], f"{m.group(1) if m else wd.name} {2 if retry else 1}\n")
    if scenario == "quota" or (scenario == "quota_on_retry" and retry):
        return quota()
    pages = re.findall(r"\| (\d+) \| `(page_\d{4}\.json)` \|", prompt)
    if pages:  # PDF analyzer prompt: one file per target page
        data = variant(scenario, retry)
        for pno, fname in pages:
            d = dict(data, page=int(pno))
            (wd / fname).write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        file = m.group(1) if m else "unknown"
        data = variant(scenario, retry, BY_FILE.get(file))
        d = dict(data, page=int(m.group(3)) if m else 1, file=file)
        (wd / "slide_analysis.json").write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": "ok", "agent_status": "SUCCESS", "fake": scenario, "retry": retry}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

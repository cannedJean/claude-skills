#!/usr/bin/env python3
"""fake_agy_task.py - stand-in for agy-delegate's agy_task.py so the pipeline can be exercised without agy.

Usage: set AGY_TASK=<this file> before running slide_analyze.py / pdf_analyze.py. It accepts the same
`run -w WORKDIR -f PROMPT ...` arguments, looks for an image in WORKDIR and writes slide_analysis.json
(or page_XXXX.json files for the PDF analyzer) from a canned analysis chosen by FAKE_AGY_SCENARIO:

  perfect     the full synthetic slide transcription
  omit        drops the second body sentence and the code block (-> retry; the retry is perfect when the
              prompt carries feedback, mimicking an analyst that fixes what it is told)
  hallucinate adds a sentence the image does not contain (-> escalate, thumbnail)
  digits      changes two numbers (-> escalate, crops)
  quota       prints agy's RESOURCE_EXHAUSTED line and writes nothing

The canned content matches tests/synth.py's slide; anything else is just a smoke test of plumbing.
"""
import json
import os
import re
import sys
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


def variant(scenario: str, retry: bool) -> dict:
    d = json.loads(json.dumps(CANNED))
    if retry:  # an analyst that applies the feedback
        return d
    if scenario == "omit":
        del d["verbatim_text"][2]
        d["code_blocks"] = []
    elif scenario == "hallucinate":
        d["verbatim_text"].append({"region": "body", "text": "외래 키는 참조 무결성을 보장하며 복합 키로 구성될 수 있다."})
    elif scenario == "digits":
        d["verbatim_text"][2]["text"] = "기본 키는 테이블당 하나만 존재한다. (2023년 기준 1,042개 행)"
    elif scenario == "leak":
        d["title"] = "EXAMPLE 파일 시스템 vs DBMS"
    return d


def main(argv) -> int:
    args = argv[1:]
    if args and args[0] == "preflight":
        print(json.dumps({"ok": True, "checks": {"agy": "fake"}}))
        return 0
    wd = Path(args[args.index("-w") + 1])
    prompt = Path(args[args.index("-f") + 1]).read_text(encoding="utf-8")
    scenario = os.environ.get("FAKE_AGY_SCENARIO", "perfect")
    if scenario == "quota":
        sys.stderr.write('AGY_ERROR: {"code": 429, "message": "RESOURCE_EXHAUSTED: quota reached. Resets in 2h 10m."}\n')
        print(json.dumps({"status": "error"}))
        return 3
    retry = "이전 시도에 대한 피드백" in prompt
    data = variant(scenario, retry)
    pages = re.findall(r"\| (\d+) \| `(page_\d{4}\.json)` \|", prompt)
    if pages:  # PDF analyzer prompt: one file per target page
        for pno, fname in pages:
            d = dict(data, page=int(pno))
            (wd / fname).write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        m = re.search(r"이번에 분석할 파일: \*\*`([^`]+)`\*\* \(전체 (\d+)장 중 \*\*(\d+)번째\*\*", prompt)
        d = dict(data, page=int(m.group(3)) if m else 1, file=m.group(1) if m else "unknown")
        (wd / "slide_analysis.json").write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": "ok", "agent_status": "SUCCESS", "fake": scenario, "retry": retry}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

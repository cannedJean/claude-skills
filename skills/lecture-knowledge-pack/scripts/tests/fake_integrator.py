"""fake_integrator.py - stands in for the per-lecture integrate subagent: writes a valid deck.json for the default
fixture course (kp_fixtures.DEFAULT_SPEC), using the digest_sha from the digest it was given."""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

DECKS = {
    "db01": {
        "sections": [{"title": "SQL과 DBMS", "first": "db01-s001", "last": "db01-s003", "summary": "조회, 비교 표, ERD"},
                     {"title": "정규화", "first": "db01-s004", "last": "db01-s006", "summary": "1NF와 2NF"}],
        "objectives": [{"id": "db01-o1", "text": "WHERE 조건으로 원하는 행을 조회할 수 있다", "bloom": "apply",
                        "concepts": ["new:select"], "evidence": ["db01-s001"]}],
        "concepts": [
            {"id": "new:select", "term": "SELECT", "aliases": {"ko": [], "en": ["SELECT statement"], "abbr": []},
             "definition_slide": "db01-s001", "definition": "조건에 맞는 행을 조회하는 SQL 문"},
            {"id": "new:dbms", "term": "DBMS", "aliases": {"ko": ["데이터베이스 관리 시스템"], "en": [], "abbr": []},
             "definition_slide": "db01-s002", "definition": "중복을 최소화하고 제약 조건으로 무결성을 지키는 시스템"},
            {"id": "new:erd", "term": "ERD", "aliases": {"ko": [], "en": ["entity relationship diagram"], "abbr": []},
             "definition_slide": "db01-s003", "definition": "엔터티와 관계를 그린 도식"},
            {"id": "new:1nf", "term": "1NF", "aliases": {"ko": ["제1정규형"], "en": [], "abbr": []},
             "definition_slide": "db01-s006", "definition": "모든 속성이 원자값"},
            {"id": "new:2nf", "term": "2NF", "aliases": {"ko": ["제2정규형"], "en": [], "abbr": []},
             "definition_slide": "db01-s006", "definition": "부분 함수 종속을 제거한 상태"}],
        "prerequisites": [{"concept": "new:2nf", "requires": "new:1nf", "kind": "stated", "evidence": ["db01-s006"]}],
        "exam_points": [{"text": "2NF는 부분 함수 종속을 제거한다", "slides": ["db01-s006"], "concepts": ["new:2nf"]}],
    },
    "db02": {
        "sections": [{"title": "회귀", "first": "db02-s001", "last": "db02-s001", "summary": None},
                     {"title": "보안", "first": "db02-s002", "last": "db02-s003", "summary": None}],
        "objectives": [{"id": "db02-o1", "text": "선형 회귀 식의 기호를 설명할 수 있다", "bloom": "understand",
                        "concepts": ["new:linear-regression"], "evidence": ["db02-s001"]}],
        "concepts": [{"id": "new:linear-regression", "term": "선형 회귀",
                      "aliases": {"ko": [], "en": ["linear regression"], "abbr": []},
                      "definition_slide": "db02-s001", "definition": "가중치와 편향으로 만든 직선 모델"}],
        "prerequisites": [],
        "exam_points": [],
    },
}


def digest_sha(digest_md: Path) -> str:
    m = re.search(r"digest_sha: `([0-9a-f]{64})`", Path(digest_md).read_text(encoding="utf-8"))
    return m.group(1)


def write_deck(work: Path, lec: str, edit=None) -> Path:
    idir = Path(work) / "integrate" / lec
    deck = copy.deepcopy(DECKS[lec])
    deck.update(schema="kpack.deck/1", lecture=lec, digest_sha=digest_sha(idir / "digest.md"))
    deck.setdefault("deprecated", [])
    if edit:
        edit(deck)
    p = idir / "deck.json"
    p.write_text(json.dumps(deck, ensure_ascii=False, indent=2), encoding="utf-8")
    return p

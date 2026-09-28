"""Deterministic checks for agy slide analyses (no image access).

validate_analysis(data, page, file) -> list of issues {level: error|warn, code, msg}
  error  -> output is unusable or suspicious enough to re-run the analyst
  warn   -> kept, but surfaced in the report
cross_check(analyses) -> issues that need more than one slide (duplicated content).
"""
from __future__ import annotations

import re

SLIDE_TYPES = {"title", "toc", "section_divider", "concept", "diagram", "table", "code",
               "example", "practice", "summary", "other"}
REGIONS = {"title", "body", "header/footer", "caption", "note", "diagram_label", "page_number"}
# strings that only exist in the few-shot examples; seeing them means the example was copied
FEWSHOT_LEAKS = ["example_slide_", "EXAMPLE 파일 시스템", "EXAMPLE SELECT", "EXAMPLE 기본 키", "example_students"]
NAV_ARROWS = {"←", "→", "<", ">"}


def _norm(s: str) -> str:
    return re.sub(r"[\s·•\-_()\[\]'\"`.,:;!?/]+", "", (s or "").lower())


def corpus(d: dict) -> str:
    parts = [d.get("title") or ""]
    parts += [t.get("text", "") for t in d.get("verbatim_text") or [] if isinstance(t, dict)]
    for tb in d.get("tables") or []:
        if isinstance(tb, dict):
            parts += [tb.get("caption") or ""] + [str(h) for h in tb.get("headers") or []]
            parts += [str(c) for r in tb.get("rows") or [] for c in (r if isinstance(r, list) else [r])]
    parts += [c.get("code", "") for c in d.get("code_blocks") or [] if isinstance(c, dict)]
    for dg in d.get("diagrams") or []:
        if isinstance(dg, dict):
            parts += [str(e) for e in dg.get("elements") or []] + [str(r) for r in dg.get("relations") or []]
    return _norm(" ".join(parts))


def validate_analysis(d, page: int, file: str) -> list:
    iss = []
    E = lambda code, msg: iss.append({"level": "error", "code": code, "msg": msg})
    W = lambda code, msg: iss.append({"level": "warn", "code": code, "msg": msg})

    if not isinstance(d, dict):
        E("not_object", "top-level JSON is not an object")
        return iss
    spec = {"page": int, "file": str, "reasoning": dict, "slide_type": str, "verbatim_text": list,
            "tables": list, "code_blocks": list, "diagrams": list, "key_concepts": list,
            "summary": str, "lecture_role": str, "exam_points": list, "uncertain": list}
    for k, t in spec.items():
        if k not in d:
            E("missing_key", f"missing key '{k}'")
        elif not isinstance(d[k], t):
            E("bad_type", f"'{k}' should be {t.__name__}")
    for k in ("title", "printed_page_number"):
        if k not in d:
            E("missing_key", f"missing key '{k}'")
    if iss:
        return iss

    if d["page"] != page:
        E("page_mismatch", f"page={d['page']} but analysed file is page {page}")
    if d["file"] != file:
        E("file_mismatch", f"file={d['file']!r}, expected {file!r}")
    for k in ("observation", "structure", "interpretation", "self_check"):
        if not str(d["reasoning"].get(k, "")).strip():
            W("reasoning_gap", f"reasoning.{k} empty")
    if d["slide_type"] not in SLIDE_TYPES:
        E("bad_slide_type", f"slide_type {d['slide_type']!r} not in enum")
    conf = d.get("confidence")
    if not isinstance(conf, (int, float)) or not 0 <= conf <= 1:
        E("bad_confidence", f"confidence {conf!r} not in [0,1]")
    elif conf < 0.75:
        W("low_confidence", f"self-reported confidence {conf}")

    raw = str(d)
    for s in FEWSHOT_LEAKS:
        if s in raw:
            E("fewshot_leak", f"few-shot example content copied ({s!r})")

    texts = [t for t in d["verbatim_text"] if isinstance(t, dict)]
    if len(texts) != len(d["verbatim_text"]):
        E("bad_verbatim", "verbatim_text items must be {region, text}")
    content = [t for t in texts if t.get("region") not in ("header/footer", "page_number")]
    if not content and not d["code_blocks"] and not d["tables"] and d["slide_type"] not in ("title", "section_divider"):
        E("no_content", "no slide content transcribed")
    for t in texts:
        if t.get("region") not in REGIONS:
            W("bad_region", f"unknown region {t.get('region')!r}")
        if (t.get("text") or "").strip() in NAV_ARROWS:
            W("nav_arrow", "viewer navigation arrow transcribed as content")

    ppn = d.get("printed_page_number")
    if ppn not in (None, "") and str(ppn).strip() != str(page):
        W("printed_page_diff", f"printed page number {ppn!r} != file index {page}")

    body = corpus(d)
    if d.get("title") and _norm(d["title"]) not in body:
        W("title_ungrounded", "title not found in transcribed text")
    for kc in d["key_concepts"]:
        term = kc.get("term", "") if isinstance(kc, dict) else str(kc)
        if term and _norm(term) not in body:
            W("concept_ungrounded", f"key concept {term!r} not found verbatim in slide text")
    if len(d["summary"].strip()) < 20:
        W("thin_summary", "summary shorter than 20 chars")
    for tb in d["tables"]:
        hdr = tb.get("headers") or []
        for r in tb.get("rows") or []:
            if hdr and isinstance(r, list) and len(r) != len(hdr):
                W("table_shape", f"row width {len(r)} != header width {len(hdr)}")
                break
    if "[?]" in raw and not d["uncertain"]:
        W("unlogged_uncertainty", "[?] used but 'uncertain' is empty")
    return iss


def cross_check(analyses: dict) -> dict:
    """analyses: {page: data}. Flags pages whose non-boilerplate text is identical to another page."""
    seen, out = {}, {}
    for p, d in sorted(analyses.items()):
        if not isinstance(d, dict) or d.get("slide_type") in ("title", "section_divider"):
            continue
        key = _norm(" ".join(t.get("text", "") for t in d.get("verbatim_text") or []
                             if isinstance(t, dict) and t.get("region") not in ("header/footer", "page_number")))
        if len(key) < 30:
            continue
        if key in seen:
            out.setdefault(p, []).append({"level": "warn", "code": "duplicate_content",
                                          "msg": f"same transcribed text as page {seen[key]}"})
        else:
            seen[key] = p
    return out

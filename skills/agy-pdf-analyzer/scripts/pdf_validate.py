"""Deterministic checks for agy PDF page analyses (no PDF access).

validate_analysis(data, page) -> list of issues {level: error|warn, code, msg}
  error  -> output is unusable or suspicious enough to re-run the analyst
  warn   -> kept, but surfaced in the report
  Never raises: a malformed field becomes an error (bad_type / bad_table) instead of a crash.
cross_check(analyses) -> issues that need more than one page (duplicated content; level info for
  series_type_mixed).

The mirror of agy-slide-analyzer/scripts/slide_validate.py (same enums, codes and prompt-rule warnings) minus the
`file` key: interpretation_on_title_only (exam points / concepts on a page with only a title), visual_undeclared
(a capture or highlight mark described in reasoning but not declared as a screenshot / annotation diagram),
code_duplicated (code_blocks text repeated in verbatim_text), illustration_with_relations, divider_with_body.
SLIDE_TYPES / REGIONS / DIAGRAM_KINDS are the enums of prompts/analyst.md; FEWSHOT_LEAKS its few-shot markers
(the slide analyzer's list without 'example_slide_'). The report's cross_check is the shared slide_validate one.
"""
from __future__ import annotations

import re

SLIDE_TYPES = {"title", "toc", "section_divider", "concept", "diagram", "table", "code",
               "example", "practice", "summary", "other"}
REGIONS = {"title", "body", "header/footer", "caption", "note", "diagram_label", "page_number"}
DIAGRAM_KINDS = {"erd", "flowchart", "hierarchy", "architecture", "venn", "syntax_breakdown", "screenshot",
                 "annotation", "illustration", "other"}
# strings that only exist in the few-shot examples; seeing them means the example was copied
FEWSHOT_LEAKS = ["EXAMPLE 파일 시스템", "EXAMPLE SELECT", "EXAMPLE 기본 키", "example_students",
                 "EXAMPLE 카운터", "exampleCount"]
NAV_ARROWS = {"←", "→", "<", ">"}
NONCONTENT = ("title", "header/footer", "page_number")  # regions that are not page body text
SHOT_RE = re.compile(r"개발자 도구|DevTools|스크린샷|캡처|콘솔 (출력|화면|창)")
MARK_RE = re.compile(r"(빨간|붉은)\S*\s*(색\s*)?(강조\s*)?(박스|상자|사각형|테두리)|X 표시|✕|밑줄|색상별")
SERIES_RE = re.compile(r"\((\d+)\s*/\s*(\d+)\)\s*$")


def _norm(s: str) -> str:
    return re.sub(r"[\s·•\-_()\[\]'\"`.,:;!?/]+", "", (s or "").lower())


def _code_norm(s: str) -> str:
    """textnorm.norm, as slide_validate uses for code_duplicated, once pdf_analyze has put the shared code on
    sys.path; this module's own _norm when it runs alone."""
    try:
        from textnorm import norm
    except ImportError:
        return _norm(s)
    return norm(s)


def _text(t) -> str:
    return str(t.get("text") or "") if isinstance(t, dict) else ""


def corpus(d: dict) -> str:
    parts = [str(d.get("title") or "")]
    parts += [_text(t) for t in d.get("verbatim_text") or [] if isinstance(t, dict)]
    for tb in d.get("tables") or []:
        if isinstance(tb, dict):
            parts += [str(tb.get("caption") or "")] + [str(h) for h in tb.get("headers") or []]
            parts += [str(c) for r in tb.get("rows") or [] for c in (r if isinstance(r, list) else [r])]
    parts += [str(c.get("code") or "") for c in d.get("code_blocks") or [] if isinstance(c, dict)]
    for dg in d.get("diagrams") or []:
        if isinstance(dg, dict):
            parts += [str(e) for e in dg.get("elements") or []] + [str(r) for r in dg.get("relations") or []]
    return _norm(" ".join(parts))


def validate_analysis(d, page: int) -> list:
    iss = []
    E = lambda code, msg: iss.append({"level": "error", "code": code, "msg": msg})
    W = lambda code, msg: iss.append({"level": "warn", "code": code, "msg": msg})

    if not isinstance(d, dict):
        E("not_object", "top-level JSON is not an object")
        return iss
    spec = {"page": int, "reasoning": dict, "slide_type": str, "verbatim_text": list,
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
    try:
        _checks(d, page, E, W)
    except (TypeError, AttributeError, ValueError, KeyError) as e:  # a value of an unexpected type deep inside
        E("bad_type", f"unexpected value inside the analysis: {e!r}"[:200])
    return iss


def _checks(d: dict, page: int, E, W):
    if d["page"] != page:
        E("page_mismatch", f"page={d['page']} but the file is for page {page}")
    for k in ("observation", "structure", "interpretation", "self_check"):
        if not str(d["reasoning"].get(k) or "").strip():
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
        E("no_content", "no page content transcribed")
    for t in texts:
        if t.get("region") not in REGIONS:
            W("bad_region", f"unknown region {t.get('region')!r}")
        if _text(t).strip() in NAV_ARROWS:
            W("nav_arrow", "viewer navigation arrow transcribed as content")

    ppn = d.get("printed_page_number")
    if ppn not in (None, "") and str(ppn).strip() != str(page):
        W("printed_page_diff", f"printed page number {ppn!r} != PDF page {page}")

    body = corpus(d)
    if d.get("title") and _norm(str(d["title"])) not in body:
        W("title_ungrounded", "title not found in transcribed text")
    for kc in d["key_concepts"]:
        term = kc.get("term", "") if isinstance(kc, dict) else str(kc)
        if term and _norm(str(term)) not in body:
            W("concept_ungrounded", f"key concept {term!r} not found verbatim in page text")
    if len(d["summary"].strip()) < 20:
        W("thin_summary", "summary shorter than 20 chars")
    for i, tb in enumerate(d["tables"]):
        if not isinstance(tb, dict):
            E("bad_table", f"tables[{i}] must be an object {{caption, headers, rows}}")
            continue
        hdr = tb.get("headers") or []
        for r in tb.get("rows") or []:
            if hdr and isinstance(r, list) and len(r) != len(hdr):
                W("table_shape", f"row width {len(r)} != header width {len(hdr)}")
                break
    if "[?]" in raw and not d["uncertain"]:
        W("unlogged_uncertainty", "[?] used but 'uncertain' is empty")
    _prompt_rule_checks(d, texts, W)


def _prompt_rule_checks(d: dict, texts: list, W):
    """Warnings for the analyst prompt's rules on interpretation, captures / marks, duplication and kinds."""
    body = [t for t in texts if t.get("region") not in NONCONTENT and _text(t).strip()]
    codes = [c for c in d["code_blocks"] if isinstance(c, dict)]
    diagrams = [dg for dg in d["diagrams"] if isinstance(dg, dict)]
    n_kc, n_ep = len(d["key_concepts"]), len(d["exam_points"])
    if not body and not codes and not d["tables"] and not diagrams and (n_ep or n_kc > 1):
        W("interpretation_on_title_only", f"only a title on the page, yet {n_ep} exam point(s) and {n_kc} key concept(s)")
    why = " ".join(str(d["reasoning"].get(k) or "") for k in ("observation", "structure"))
    m = SHOT_RE.search(why) or MARK_RE.search(why)
    if m and not any(dg.get("kind") in ("screenshot", "annotation") for dg in diagrams):
        W("visual_undeclared", f"reasoning mentions {m.group(0)!r} but no diagram has kind screenshot / annotation")
    code = _code_norm("\n".join(str(c.get("code") or "") for c in codes))
    dup = [i for i, t in enumerate(d["verbatim_text"]) if code and isinstance(t, dict)
           and t.get("region") not in NONCONTENT and len(_code_norm(_text(t))) >= 8 and _code_norm(_text(t)) in code]
    if dup:
        W("code_duplicated", "verbatim_text repeats code_blocks text: " + ", ".join(f"verbatim_text[{i}]" for i in dup[:5]))
    for i, dg in enumerate(d["diagrams"]):
        if isinstance(dg, dict) and dg.get("kind") == "illustration" and dg.get("relations"):
            W("illustration_with_relations", f"diagrams[{i}] has relations but kind 'illustration'")
    if d["slide_type"] == "section_divider" and len(_code_norm(" ".join(_text(t) for t in body))) >= 15:
        W("divider_with_body", "section_divider with body text (a definition page is a concept page)")


def cross_check(analyses: dict) -> dict:
    """analyses: {page: data}. Flags pages whose non-boilerplate text is identical to another page (in a chunked
    PDF this is also how an off-by-one page mix-up shows up), and (level info) '(k/n)' title series whose pages
    are typed differently."""
    seen, out, series = {}, {}, {}
    for p, d in sorted(analyses.items()):
        if not isinstance(d, dict):
            continue
        m = SERIES_RE.search(str(d.get("title") or ""))
        if m:
            series.setdefault((SERIES_RE.sub("", str(d["title"])).strip(), m.group(2)), []).append((p, d.get("slide_type")))
        if d.get("slide_type") in ("title", "section_divider"):
            continue
        key = _norm(" ".join(_text(t) for t in d.get("verbatim_text") or []
                             if isinstance(t, dict) and t.get("region") not in ("header/footer", "page_number")))
        if len(key) < 30:
            continue
        if key in seen:
            out.setdefault(p, []).append({"level": "warn", "code": "duplicate_content",
                                          "msg": f"same transcribed text as page {seen[key]}"})
        else:
            seen[key] = p
    for (name, n), members in series.items():
        if len({t for _, t in members}) > 1:
            kinds = ", ".join(f"p{p}={t}" for p, t in members)
            for p, _ in members:
                out.setdefault(p, []).append({"level": "info", "code": "series_type_mixed",
                                              "msg": f"'{name} (k/{n})' series has mixed slide_type: {kinds}"})
    return out

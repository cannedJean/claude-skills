"""crops.py - the material Claude (the cross-vendor reviewer) is allowed to look at, and how its answers
are turned into judgments.

Claude never sees a whole slide at full resolution. For each escalated slide the pipeline prepares:

  line crops      the OCR line boxes the analysis disagrees with (blind: the queue shows the crop only,
                  never the analyst's or the OCR's text). Claude types what it reads.
  thumbnail       a <=640px copy of the slide for structural questions (undeclared table/graphics, audit
                  samples). For audit samples Claude lists the main text it can read (blind recall check);
                  for diagram questions it is shown the analyst's diagram description and answers ok/issue.

    prepare(image, ref, sc, out_dir, stem, kind) -> list[entry]   writes PNGs, returns queue entries
    write_queue(out_dir, entries, recs) -> Path                   review_queue.md + review_queue.json
    write_grounding_queue(out_dir, items) -> Path                 text-only entailment check (no images)
    judge(entries, reads, analysis) -> list[judgment]             compare Claude's reads with the analysis
    token_estimate(entries) -> int                                 ~pixels / 750

Entry = {"id", "page", "stem", "kind": line | thumb_audit | thumb_struct, "path", "px", "box", "question", ...}
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ocr_score import corpus_text  # noqa: E402
from textnorm import bigrams, digit_seqs, fuzzy_in, latin_tokens, norm, units  # noqa: E402

MAX_LINE_CROPS = 8
MIN_CROP_HEIGHT = 48
THUMB_WIDTH = 640
PIXELS_PER_TOKEN = 750


def _crop_box(box, size, pad_y=0.45, pad_x=0.6):
    l, t, w, h = box
    W, H = size
    py, px = int(h * pad_y), int(h * pad_x)
    return (max(0, l - px), max(0, t - py), min(W, l + w + px), min(H, t + h + py))


def line_crops(image_path, ref: dict, sc: dict, out_dir: Path, stem: str, page: int, limit=MAX_LINE_CROPS) -> list:
    entries = []
    boxed = [m for m in sc.get("missing_lines") or [] if m.get("box")]
    if not boxed:
        return entries
    out_dir.mkdir(parents=True, exist_ok=True)
    with Image.open(image_path) as im:
        for m in boxed[:limit]:
            cb = _crop_box(m["box"], im.size)
            crop = im.crop(cb)
            if crop.height < MIN_CROP_HEIGHT:
                f = MIN_CROP_HEIGHT / max(1, crop.height)
                crop = crop.resize((max(1, int(crop.width * f)), MIN_CROP_HEIGHT))
            cid = f"{stem}__{m['id']}"
            path = out_dir / f"{cid}.png"
            crop.convert("RGB").save(path)
            entries.append({"id": cid, "page": page, "stem": stem, "kind": "line", "path": str(path),
                            "px": crop.width * crop.height, "box": list(cb), "line_id": m["id"],
                            "hidden": {"ocr_text": m["text"], "missing_words": m["missing_words"]},
                            "question": "이 크롭에 적힌 글자를 보이는 그대로 적으세요. 읽을 수 없으면 [?]"})
    return entries


def thumbnail(image_path, out_dir: Path, stem: str, page: int, kind: str, question: str, show=None, hidden=None) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    with Image.open(image_path) as im:
        f = min(1.0, THUMB_WIDTH / im.width)
        th = im.resize((max(1, int(im.width * f)), max(1, int(im.height * f)))) if f < 1.0 else im.copy()
        cid = f"{stem}__{kind}"
        path = out_dir / f"{cid}.png"
        th.convert("RGB").save(path)
        return {"id": cid, "page": page, "stem": stem, "kind": kind, "path": str(path),
                "px": th.width * th.height, "box": None, "question": question, "show": show or {},
                "hidden": hidden or {}}


def _hidden_for_read(ref: dict, sc: dict) -> dict:
    """What `judge` needs to interpret a blind thumbnail read: the analysis units the OCR never saw (possible
    hallucinations) and the confident OCR lines (to tell how much of the slide Claude could actually read)."""
    ref_lines = [l["text"] for l in (ref or {}).get("lines") or []
                 if sum(1 for w in l.get("words") or [] if w.get("weight") == "high") >= 2]
    return {"unsupported": [u["text"] for u in sc.get("unsupported_units") or [] if len(u["text"]) >= 3],
            "ref_lines": ref_lines[:40]}


READ_QUESTION = "축소본에서 읽을 수 있는 주요 텍스트(제목, 본문 문장, 표 머리글, 코드 키워드)를 한 줄에 하나씩 적으세요. 추측하지 마세요."


def prepare(image_path, ref: dict, sc: dict, analysis: dict, out_dir: Path, stem: str, page: int, audit=False) -> list:
    """Build every queue entry one slide needs. `sc['needs']` decides crops/thumbnail; audit adds a blind
    thumbnail read even for accepted slides."""
    entries = []
    needs = set(sc.get("needs") or [])
    if "crops" in needs:
        entries += line_crops(image_path, ref, sc, out_dir, stem, page)
    if "thumbnail" in needs:
        trig = [t for t in sc.get("triggers") or [] if t in ("table_candidate_undeclared", "graphics_undeclared")]
        if trig:
            show = {"tables_declared": len(analysis.get("tables") or []),
                    "diagrams_declared": [d.get("description") for d in analysis.get("diagrams") or [] if isinstance(d, dict)]}
            q = ("축소본을 보고 답하세요: (1) 표가 있다면 대략 몇 행 몇 열인지, (2) 글자가 아닌 도식/그림이 있다면 무엇인지 한 줄로. "
                 "아래 '분석가 선언'과 다르면 verdict를 issue로 하고 note에 이유를 적으세요.")
            entries.append(thumbnail(image_path, out_dir, stem, page, "thumb_struct", q, show))
        else:
            entries.append(thumbnail(image_path, out_dir, stem, page, "thumb_read", READ_QUESTION,
                                     hidden=_hidden_for_read(ref, sc)))
    if audit and not any(e["kind"] in ("thumb_read", "thumb_struct") for e in entries):
        entries.append(thumbnail(image_path, out_dir, stem, page, "thumb_audit", "감사 샘플입니다. " + READ_QUESTION,
                                 hidden=_hidden_for_read(ref, sc)))
    return entries


def token_estimate(entries: list) -> int:
    return int(sum(e.get("px", 0) for e in entries) / PIXELS_PER_TOKEN)


# --------------------------------------------------------------------------- queue files
def write_queue(out_dir: Path, entries: list, note: str = "") -> Path:
    """review_queue.md is what Claude reads; review_queue.json is what `resolve` reads back.
    The .md deliberately omits the hidden fields (OCR text, analyst text)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    pub = []
    for e in entries:
        pub.append({k: v for k, v in e.items() if k != "hidden"})
    (out_dir / "review_queue.json").write_text(json.dumps(entries, ensure_ascii=False, indent=1), encoding="utf-8")
    md = ["# Review queue (Claude, blind)", "",
          "규칙: 아래 파일 **만** 열어 본다. 원본 슬라이드와 agy의 분석 JSON은 답을 적기 전에 열지 않는다.",
          "답은 이 폴더의 `crop_reads.json`에 `{\"<id>\": <답>}` 형태로 적고 `resolve`를 실행한다.",
          "- `line` 항목: 답 = 읽은 글자 문자열 (읽을 수 없으면 \"[?]\")",
          "- `thumb_read` / `thumb_audit` 항목: 답 = 읽은 주요 텍스트 줄들의 배열 또는 줄바꿈 문자열",
          "- `thumb_struct` 항목: 답 = {\"verdict\": \"ok\" | \"issue\", \"note\": \"...\"}", ""]
    if note:
        md += [note, ""]
    md += [f"총 {len(entries)}개 항목, 예상 이미지 토큰 ≈ {token_estimate(entries)}", ""]
    for e in pub:
        md.append(f"## {e['id']}  (page {e['page']}, {e['kind']})")
        md.append(f"- file: `{e['path']}`")
        md.append(f"- question: {e['question']}")
        if e.get("show"):
            md.append(f"- 분석가 선언: {json.dumps(e['show'], ensure_ascii=False)}")
        md.append("")
    (out_dir / "review_queue.md").write_text("\n".join(md), encoding="utf-8")
    return out_dir / "review_queue.md"


def write_grounding_queue(out_dir: Path, items: list) -> Path:
    """items: [{"page", "stem", "verbatim": str, "claims": {field: text}}]. Text only: Claude judges whether the
    interpreted fields are entailed by the transcribed text. Answers go to grounding_reads.json."""
    out_dir.mkdir(parents=True, exist_ok=True)
    md = ["# Grounding queue (text only, no images)", "",
          "각 페이지의 `verbatim`(전사문)만 근거로, `claims`의 각 항목이 전사문에서 도출되는지 판정한다.",
          "전사문에 없는 지식을 사실처럼 덧붙인 항목만 골라 `grounding_reads.json`에 "
          "`{\"<page>\": {\"unsupported\": [\"<field>\", ...], \"note\": \"...\"}}` 로 적는다. 문제 없으면 빈 배열.", ""]
    for it in items:
        md.append(f"## page {it['page']} ({it['stem']})")
        md.append("verbatim:")
        md.append("```")
        md.append(it["verbatim"][:3000])
        md.append("```")
        md.append("claims:")
        for k, v in it["claims"].items():
            md.append(f"- `{k}`: {v}")
        md.append("")
    (out_dir / "grounding_queue.md").write_text("\n".join(md), encoding="utf-8")
    (out_dir / "grounding_queue.json").write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    return out_dir / "grounding_queue.md"


# --------------------------------------------------------------------------- judging Claude's answers
def _as_lines(ans) -> list:
    if ans is None:
        return []
    if isinstance(ans, dict):
        ans = ans.get("lines") or ans.get("text") or ""
    if isinstance(ans, list):
        return [str(x) for x in ans if str(x).strip()]
    return [l for l in str(ans).splitlines() if l.strip()]


def judge(entries: list, reads: dict, analysis: dict) -> list:
    """One judgment per answered entry.
    line        agy_ok (Claude's reading is in the analysis) | agy_wrong (it is not) | unreadable
    thumb_*     read: recall of Claude's lines in the analysis; struct: Claude's verdict."""
    corpus_raw = corpus_text(analysis or {})
    corpus = norm(corpus_raw)
    cbg = bigrams(corpus)
    out = []
    for e in entries:
        if e["id"] not in reads:
            continue
        ans = reads[e["id"]]
        j = {"id": e["id"], "page": e["page"], "kind": e["kind"]}
        if e["kind"] == "line":
            text = ans if isinstance(ans, str) else json.dumps(ans, ensure_ascii=False)
            nt = norm(text.replace("[?]", ""))
            hidden = e.get("hidden") or {}
            if not nt or text.strip() in ("[?]", "?", "unreadable"):
                j.update(verdict="unreadable", claude_text=text)
            else:
                clean = text.replace("[?]", " ")
                us = [u for u in units(clean) if len(u) >= 2]
                hit = sum(fuzzy_in(u, corpus, cbg, 0.75) for u in us) / len(us) if us else float(nt in corpus)
                # numbers and identifiers are decisive: one digit off is a wrong analysis, not a fuzzy near-miss
                corpus_digits, corpus_latin = set(digit_seqs(corpus_raw)), set(latin_tokens(corpus_raw))
                bad_digits = sorted(d for d in set(digit_seqs(clean)) if len(d) >= 2 and d not in corpus_digits)
                bad_latin = sorted(t for t in set(latin_tokens(clean)) if len(t) >= 3 and t not in corpus_latin)
                ok = hit >= 0.7 and not bad_digits and not bad_latin
                j.update(verdict="agy_ok" if ok else "agy_wrong", claude_text=text, found_ratio=round(hit, 2),
                         wrong_digits=bad_digits, wrong_identifiers=bad_latin, ocr_text=hidden.get("ocr_text"))
        elif e["kind"] == "thumb_struct":
            v = ans.get("verdict") if isinstance(ans, dict) else str(ans)
            j.update(verdict="ok" if str(v).lower().startswith("ok") else "issue",
                     note=(ans.get("note") if isinstance(ans, dict) else ""))
        else:  # thumb_read / thumb_audit: blind recall of what Claude could read
            lines = _as_lines(ans)
            missing = []
            found = 0
            for l in lines:
                us = [u for u in units(l) if len(u) >= 2]
                if not us:
                    continue
                hit = sum(fuzzy_in(u, corpus, cbg, 0.75) for u in us) / len(us)
                if hit >= 0.6:
                    found += 1
                else:
                    missing.append(l)
            n = found + len(missing)
            rec = found / n if n else None
            # hallucination check: analysis text that neither the OCR nor Claude saw, judged only when Claude
            # could read most of the confident OCR lines (otherwise the thumbnail was simply too small)
            hidden = e.get("hidden") or {}
            claude_text = norm(" ".join(lines))
            cb = bigrams(claude_text)
            ref_lines = hidden.get("ref_lines") or []
            coverage = (sum(fuzzy_in(norm(r), claude_text, cb, 0.6) for r in ref_lines) / len(ref_lines)) if ref_lines else None
            suspects = []
            if coverage is not None and coverage >= 0.7:
                suspects = [u for u in hidden.get("unsupported") or [] if not fuzzy_in(u, claude_text, cb, 0.7)]
            verdict = "ok" if (rec is None or rec >= 0.7) and not suspects else "issue"
            j.update(verdict=verdict, lines_read=n, recall_of_claude_lines=None if rec is None else round(rec, 2),
                     missing_lines=missing, claude_coverage_of_ocr=None if coverage is None else round(coverage, 2),
                     hallucination_suspects=suspects[:10])
        out.append(j)
    return out


def page_verdict(judgments: list) -> str:
    """accepted when nothing Claude read contradicts the analysis; needs_attention otherwise; pending if
    no entry of the page has been answered yet."""
    if not judgments:
        return "pending"
    bad = [j for j in judgments if j.get("verdict") in ("agy_wrong", "issue")]
    return "needs_attention" if bad else "accepted"

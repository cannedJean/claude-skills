"""crops.py - the material Claude (the cross-vendor reviewer) is allowed to look at, and how its answers
are turned into judgments.

Claude never sees a whole slide at full resolution. For each escalated slide the pipeline prepares:

  line crops      the OCR line boxes the analysis disagrees with (blind: the queue shows the crop only,
                  never the analyst's or the OCR's text). Claude types what it reads.
  thumbnail       a <=640px copy of the slide for structural questions (undeclared table/graphics, audit
                  samples). For audit samples Claude lists the main text it can read (blind recall check);
                  for diagram questions it is shown the analyst's diagram description and answers ok/issue.

    prepare(image, ref, sc, analysis, out_dir, stem, page, audit=False, boilerplate=None) -> list[entry]
                                                                  writes PNGs, returns queue entries
    write_queue(out_dir, entries, note) -> Path                   review_queue.md + review_queue.json
    write_shards(out_dir, entries, n, note) -> list[dict]         review_queue_<k>.md, answers in crop_reads_<k>.json
    remove_shards(out_dir)                                        drop review_queue_<k>.md files
    write_grounding_queue(out_dir, items) -> Path                 text-only entailment check (no images)
    judge(entries, reads, analysis, boilerplate=None) -> list[judgment]   compare Claude's reads with the analysis
                                                                  (the crops of a line-order dispute only together)
    token_estimate(entries) -> int                                 ~pixels / 750
    write_atomic(path, text)                                       tmp file + os.replace (no half-written files)

Entry = {"id", "page", "stem", "kind": line | thumb_read | thumb_audit | thumb_struct, "path", "px", "box",
         "question", "fp", ...}; fp = sha256 of exactly what Claude is shown (the PNG, plus the analyst's
         declarations for thumb_struct), so an answer written for an older entry can be recognised.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ocr_score  # noqa: E402
from ocr_score import corpus_text  # noqa: E402
from textnorm import bigrams, digit_seqs, fuzzy_in, latin_tokens, norm, units  # noqa: E402

MAX_LINE_CROPS = 8
MIN_CROP_HEIGHT = 48
THUMB_WIDTH = 640
PIXELS_PER_TOKEN = 750
STRONG = tuple(sorted(ocr_score.STRONG))  # confirmed by a second engine or by a second (upscaled) pass
CURLY = str.maketrans("", "", "“”‘’")  # norm() keeps curly quotes; Claude types straight ones


def write_atomic(path: Path, text: str):
    """Write through a temporary file so a kill mid-write never leaves a truncated file behind."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    for i in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # Windows: a reader holds the target open for a moment
            if i == 4:
                raise
            time.sleep(0.1)


def _fp(path, show=None) -> str:
    h = hashlib.sha256(Path(path).read_bytes())
    if show:
        h.update(json.dumps(show, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    return h.hexdigest()[:16]


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
            hidden = {"ocr_text": m["text"], "missing_words": m["missing_words"]}
            hidden.update({k: m[k] for k in ("reason", "analysis_line", "field", "order_field") if m.get(k) is not None})
            entries.append({"id": cid, "page": page, "stem": stem, "kind": "line", "path": str(path),
                            "px": crop.width * crop.height, "box": list(cb), "line_id": m["id"], "fp": _fp(path),
                            "hidden": hidden, "question": "이 크롭에 적힌 글자를 보이는 그대로 적으세요. 읽을 수 없으면 [?]"})
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
                "fp": _fp(path, show), "hidden": hidden or {}}


def _suspect_units(texts, boilerplate=None) -> list:
    """Analysis units worth a hallucination check: 3+ chars, curly quotes dropped, deck boilerplate skipped."""
    out, bp = [], boilerplate or ()
    for u in texts or []:
        if u in bp:
            continue
        u = u.translate(CURLY)
        if len(u) >= 3 and u not in bp and u not in out:
            out.append(u)
    return out


def _hidden_for_read(ref: dict, sc: dict, boilerplate=None) -> dict:
    """What `judge` needs to interpret a blind thumbnail read: the analysis units the OCR never saw (possible
    hallucinations) and the confident OCR lines (to tell how much of the slide Claude could actually read).
    With one OCR engine no word is confirmed, so lines with 2+ mid-weight words stand in."""
    lines = (ref or {}).get("lines") or []

    def picked(kinds):
        return [l["text"] for l in lines if sum(1 for w in l.get("words") or [] if w.get("weight") in kinds) >= 2]

    ref_lines = picked(STRONG) or picked(STRONG + ("mid",))
    return {"unsupported": _suspect_units([u["text"] for u in sc.get("unsupported_units") or []], boilerplate),
            "ref_lines": ref_lines[:40]}


READ_QUESTION = "축소본에서 읽을 수 있는 주요 텍스트(제목, 본문 문장, 표 머리글, 코드 키워드)를 한 줄에 하나씩 적으세요. 추측하지 마세요."


def prepare(image_path, ref: dict, sc: dict, analysis: dict, out_dir: Path, stem: str, page: int, audit=False,
            boilerplate=None) -> list:
    """Build every queue entry one slide needs. `sc['needs']` decides crops/thumbnail; audit adds a blind
    thumbnail read even for accepted slides. boilerplate: deck-wide units kept out of the hallucination suspects."""
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
                                     hidden=_hidden_for_read(ref, sc, boilerplate)))
    if audit and not any(e["kind"] in ("thumb_read", "thumb_struct") for e in entries):
        entries.append(thumbnail(image_path, out_dir, stem, page, "thumb_audit", "감사 샘플입니다. " + READ_QUESTION,
                                 hidden=_hidden_for_read(ref, sc, boilerplate)))
    return entries


def token_estimate(entries: list) -> int:
    return int(sum(e.get("px", 0) for e in entries) / PIXELS_PER_TOKEN)


# --------------------------------------------------------------------------- queue files
def _queue_md(entries: list, note: str = "", answers: str = "crop_reads.json", title: str = "Review queue") -> str:
    md = [f"# {title} (Claude, blind)", "",
          "규칙: 아래 파일 **만** 열어 본다. 원본 슬라이드와 agy의 분석 JSON은 답을 적기 전에 열지 않는다.",
          f"답은 이 폴더의 `{answers}`에 `{{\"<id>\": <답>}}` 형태로 적고 `resolve`를 실행한다.",
          "- `line` 항목: 답 = 읽은 글자 문자열 (읽을 수 없으면 \"[?]\")",
          "- `thumb_read` / `thumb_audit` 항목: 답 = 읽은 주요 텍스트 줄들의 배열 또는 줄바꿈 문자열",
          "- `thumb_struct` 항목: 답 = {\"verdict\": \"ok\" | \"issue\", \"note\": \"...\"}",
          "- `resolve`가 '이전 버전의 항목에 쓴 답'이라고 무시한 항목은 다시 보고 `{\"answer\": <답>, \"fp\": \"<항목의 fp>\"}` "
          "형태로 적는다 (같은 답을 그냥 다시 적으면 계속 무시된다)", ""]
    if note:
        md += [note, ""]
    md += [f"총 {len(entries)}개 항목, 예상 이미지 토큰 ≈ {token_estimate(entries)}", ""]
    for e in entries:
        md.append(f"## {e['id']}  (page {e['page']}, {e['kind']})")
        md.append(f"- file: `{e['path']}`")
        if e.get("fp"):
            md.append(f"- fp: `{e['fp']}`")
        md.append(f"- question: {e['question']}")
        if e.get("show"):
            md.append(f"- 분석가 선언: {json.dumps(e['show'], ensure_ascii=False)}")
        md.append("")
    return "\n".join(md)


def write_queue(out_dir: Path, entries: list, note: str = "") -> Path:
    """review_queue.md is what Claude reads; review_queue.json is what `resolve` reads back.
    The .md deliberately omits the hidden fields (OCR text, analyst text)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    write_atomic(out_dir / "review_queue.json", json.dumps(entries, ensure_ascii=False, indent=1))
    write_atomic(out_dir / "review_queue.md", _queue_md(entries, note))
    return out_dir / "review_queue.md"


def remove_shards(out_dir: Path):
    for f in Path(out_dir).glob("review_queue_*.md"):
        f.unlink()


def write_shards(out_dir: Path, entries: list, n: int, note: str = "") -> list:
    """Split the queue into n disjoint review_queue_<k>.md files for parallel reviewers; one slide's items stay
    together, slides go greedily to the shard with the fewest pixels. Answers go to crop_reads_<k>.json.
    -> [{"file", "answers", "items", "image_tokens"}]"""
    by_stem = {}
    for e in entries:
        by_stem.setdefault(e["stem"], []).append(e)
    shards = [[] for _ in range(max(1, n))]
    for stem in sorted(by_stem, key=lambda s: -sum(e.get("px", 0) for e in by_stem[s])):
        min(shards, key=lambda sh: sum(e.get("px", 0) for e in sh)).extend(by_stem[stem])
    remove_shards(out_dir)
    order = {e["id"]: i for i, e in enumerate(entries)}
    out = []
    for k, sh in enumerate((s for s in shards if s), 1):
        sh.sort(key=lambda e: order[e["id"]])
        path, answers = Path(out_dir) / f"review_queue_{k}.md", f"crop_reads_{k}.json"
        write_atomic(path, _queue_md(sh, note, answers=answers, title=f"Review queue, shard {k}"))
        out.append({"file": str(path), "answers": answers, "items": len(sh), "image_tokens": token_estimate(sh)})
    return out


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
    write_atomic(out_dir / "grounding_queue.md", "\n".join(md))
    write_atomic(out_dir / "grounding_queue.json", json.dumps(items, ensure_ascii=False, indent=1))
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


def _exact_misses(text: str, corpus_raw: str):
    """Numbers (2+ digits) and identifiers (3+ chars) Claude read that the analysis does not contain exactly."""
    corpus_digits, corpus_latin = set(digit_seqs(corpus_raw)), set(latin_tokens(corpus_raw))
    bad_digits = sorted(d for d in set(digit_seqs(text)) if len(d) >= 2 and d not in corpus_digits)
    bad_latin = sorted(t for t in set(latin_tokens(text)) if len(t) >= 3 and t not in corpus_latin)
    return bad_digits, bad_latin


def _order_groups(entries: list) -> dict:
    """{(stem, code field): [line entries]} for the crops queued because their code lines are out of order."""
    groups = {}
    for e in entries:
        f = (e.get("hidden") or {}).get("order_field")
        if e.get("kind") == "line" and f:
            groups.setdefault((e.get("stem"), f), []).append(e)
    return groups


def _order_check(group: list, by_id: dict, analysis: dict):
    """Claude's readings of one order group, top to bottom on the slide, must anchor to analysis lines of the field
    in a non-decreasing order; each reading alone says nothing about order. Marks both judgments of a pair that
    goes backwards agy_wrong (order_issue = the two crop ids and their analysis line indices)."""
    field = (group[0].get("hidden") or {}).get("order_field")
    al = [x for x in ocr_score._analysis_lines(analysis or {}) if x[0] == field]
    texts = [x[2].strip() for x in al]
    seq = []
    for e in sorted(group, key=lambda e: (e["box"][1], e["box"][0]) if e.get("box") else (0, 0)):
        j = by_id.get(e["id"])
        if not j or j.get("verdict") == "unreadable":
            continue
        best, cont, _n = ocr_score._anchor(j.get("claude_text") or "", al)
        if best is not None and cont >= 0.9 and texts.count(best[2].strip()) == 1:  # a repeated line has no place
            seq.append((j, best[3]))
    for (j1, i1), (j2, i2) in zip(seq, seq[1:]):
        if i2 < i1:
            for j in (j1, j2):
                j["verdict"] = "agy_wrong"
                j["order_issue"] = {"crops": [j1["id"], j2["id"]], "analysis_lines": [i1, i2]}


def judge(entries: list, reads: dict, analysis: dict, boilerplate=None) -> list:
    """One judgment per answered entry.
    line        agy_ok (Claude's reading is in the analysis) | agy_wrong (it is not, or a number / identifier /
                operator differs, or crops queued for a line-order dispute anchor to analysis lines in another order
                than they stand on the slide) | unreadable. The crops of one order dispute are judged together, once
                all of them are answered; until then they stay pending.
    thumb_*     read: recall of Claude's lines in the analysis, exact numbers and identifiers, operators and numbers
                of the analysis lines Claude's lines anchor to (copies included), hallucination suspects;
                struct: Claude's verdict."""
    corpus_raw = corpus_text(analysis or {})
    corpus = norm(corpus_raw)
    cbg = bigrams(corpus)
    groups = _order_groups(entries)
    waiting = {e["id"] for g in groups.values() if any(x["id"] not in reads for x in g) for e in g}
    out = []
    for e in entries:
        if e["id"] not in reads or e["id"] in waiting:
            continue
        ans = reads[e["id"]]
        j = {"id": e["id"], "page": e["page"], "kind": e["kind"]}
        if e.get("fp"):
            j["fp"] = e["fp"]
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
                bad_digits, bad_latin = _exact_misses(clean, corpus_raw)
                # operators and standalone numbers on the analysis line this reading anchors to, and on the line the
                # scorer held this crop's OCR line against (a line written twice: the reading may match the right twin)
                tok = ocr_score.text_token_issues(clean, analysis or {}, anchor=hidden.get("analysis_line"))
                ok = hit >= 0.7 and not bad_digits and not bad_latin and not tok
                j.update(verdict="agy_ok" if ok else "agy_wrong", claude_text=text, found_ratio=round(hit, 2),
                         wrong_digits=bad_digits, wrong_identifiers=bad_latin, token_issues=tok,
                         ocr_text=hidden.get("ocr_text"))
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
            claude_text = norm(" ".join(lines)).translate(CURLY)
            cb = bigrams(claude_text)
            ref_lines = hidden.get("ref_lines") or []
            coverage = (sum(fuzzy_in(norm(r), claude_text, cb, 0.6) for r in ref_lines) / len(ref_lines)) if ref_lines else None
            suspects = []
            if coverage is not None and coverage >= 0.7:
                suspects = [u for u in _suspect_units(hidden.get("unsupported"), boilerplate)
                            if not fuzzy_in(u, claude_text, cb, 0.7)]
            # numbers and identifiers Claude read on the slide must be in the analysis exactly, as for line crops;
            # operators and numbers too, line by line, against every copy of the line (a wrong code copy cannot
            # match through a right verbatim one)
            bad_digits, bad_latin = _exact_misses(" ".join(lines), corpus_raw)
            tok = ocr_score.text_token_issues(lines, analysis or {})
            verdict = ("ok" if (rec is None or rec >= 0.7) and not suspects and not bad_digits and not bad_latin and not tok
                       else "issue")
            j.update(verdict=verdict, lines_read=n, recall_of_claude_lines=None if rec is None else round(rec, 2),
                     missing_lines=missing, claude_coverage_of_ocr=None if coverage is None else round(coverage, 2),
                     hallucination_suspects=suspects[:10], wrong_digits=bad_digits, wrong_identifiers=bad_latin,
                     token_issues=tok)
        out.append(j)
    by_id = {j["id"]: j for j in out}
    for g in groups.values():
        if not any(e["id"] in waiting for e in g):
            _order_check(g, by_id, analysis)
    return out


def page_verdict(judgments: list) -> str:
    """accepted when nothing Claude read contradicts the analysis; needs_attention otherwise; pending if
    no entry of the page has been answered yet."""
    if not judgments:
        return "pending"
    bad = [j for j in judgments if j.get("verdict") in ("agy_wrong", "issue")]
    return "needs_attention" if bad else "accepted"

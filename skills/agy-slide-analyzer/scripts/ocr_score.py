"""ocr_score.py - compare an agy slide analysis with its sealed OCR reference and decide what happens next.

    score(analysis, reference, thresholds, validator_errors=()) -> dict
        metrics      recall, unsupported_ratio, digit_recall, latin_recall, length_ratio, recall_by_engine, ...
        decision     accept | retry | escalate | unverified
        reasons      why
        missing_lines      reference lines the analysis does not contain (-> retry feedback / crops)
        unsupported_units  analysis text the reference does not contain (-> possible hallucination)
        extra_digits       digit sequences in the analysis that no reference word has
        triggers           structural signals (table_candidate_undeclared, graphics_undeclared, ref_thin, ...)
        needs              what the reviewer must look at: [] | ["crops"] | ["thumbnail"] | ["crops", "thumbnail"]
    feedback_text(sc, validator_errors) -> str   Korean feedback for the analyst retry prompt
    load_thresholds(*paths) -> dict              DEFAULT_THRESHOLDS overlaid with the first existing JSON

Nothing here opens the image. The reference is the only "sight" the scorer has, and it was produced
before the analysis existed.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from textnorm import PUNCT_RE, bigrams, digit_seqs, fuzzy_in, latin_tokens, nfkc, norm, units  # noqa: E402

DEFAULT_THRESHOLDS = {
    "recall_accept": 0.90,       # weighted OCR recall needed to accept without a second look
    "recall_retry": 0.60,        # below this the analyst clearly skipped text: re-run with feedback
    "unsupported_accept": 0.15,  # share of analysis characters the OCR never saw
    "field_unsupported_frac": 0.7,   # a verbatim field this unsupported ...
    "field_unsupported_min_chars": 10,  # ... and at least this long is a hallucination suspect on its own
    "digit_accept": 0.90,        # share of OCR digit sequences present in the analysis
    "latin_accept": 0.85,        # share of OCR Latin identifiers (code, acronyms) present in the analysis
    "line_disputed_below": 0.6,  # a reference line with less than this share of its words found is disputed
    "min_ref_weight": 5.0,       # weighted word count below which the reference is too thin to judge
    "ref_conf_unreliable": 55,   # mean OCR confidence below which unsupported_ratio is not trusted
    "rich_corpus_chars": 40,     # analysis this long with an empty reference -> somebody must look
    "graphics_trigger": 0.06,    # non-text ink share that suggests a diagram/illustration
    "fuzzy_word": 0.75,          # bigram overlap for a reference word to count as found
    "fuzzy_unit": 0.70,          # bigram overlap for an analysis unit to count as supported
    "weights": {"high": 1.0, "mid": 0.5, "low": 0.0},
}


def load_thresholds(*paths) -> dict:
    th = json.loads(json.dumps(DEFAULT_THRESHOLDS))
    for p in paths:
        if p and Path(p).is_file():
            data = json.loads(Path(p).read_text(encoding="utf-8"))
            data = data.get("thresholds", data)
            for k, v in data.items():
                if k in th and isinstance(th[k], dict) and isinstance(v, dict):
                    th[k].update(v)
                elif k in th:
                    th[k] = v
            th["_source"] = str(p)
            break
    return th


# --------------------------------------------------------------------------- analysis corpus
def corpus_parts(d: dict) -> list:
    """[(field_path, text, kind)] for every transcribed (not interpreted) field of an analysis.
    kind: text | table | code | diagram. Diagram elements are descriptions as often as labels, so they count
    for recall (an OCR'd label found there is found) but never as unsupported transcription."""
    parts = []
    if d.get("title"):
        parts.append(("title", str(d["title"]), "text"))
    for i, t in enumerate(d.get("verbatim_text") or []):
        if isinstance(t, dict) and t.get("text"):
            parts.append((f"verbatim_text[{i}]", str(t["text"]), "text"))
    for i, tb in enumerate(d.get("tables") or []):
        if not isinstance(tb, dict):
            continue
        if tb.get("caption"):
            parts.append((f"tables[{i}].caption", str(tb["caption"]), "table"))
        for j, h in enumerate(tb.get("headers") or []):
            parts.append((f"tables[{i}].headers[{j}]", str(h), "table"))
        for r, row in enumerate(tb.get("rows") or []):
            for c, cell in enumerate(row if isinstance(row, list) else [row]):
                parts.append((f"tables[{i}].rows[{r}][{c}]", str(cell), "table"))
    for i, cb in enumerate(d.get("code_blocks") or []):
        if isinstance(cb, dict) and cb.get("code"):
            parts.append((f"code_blocks[{i}].code", str(cb["code"]), "code"))
    for i, dg in enumerate(d.get("diagrams") or []):
        if isinstance(dg, dict):
            for j, e in enumerate(dg.get("elements") or []):
                parts.append((f"diagrams[{i}].elements[{j}]", str(e), "diagram"))
    return parts


def corpus_text(d: dict) -> str:
    return " ".join(p[1] for p in corpus_parts(d))


# --------------------------------------------------------------------------- scoring
def _ref_words(ref: dict):
    for ln in ref.get("lines") or []:
        for w in ln.get("words") or []:
            yield ln, w


def score(analysis: dict, reference, thresholds: dict = None, validator_errors=()) -> dict:
    th = thresholds or DEFAULT_THRESHOLDS
    W = th["weights"]
    val_errors = [e for e in (validator_errors or []) if (e.get("level") if isinstance(e, dict) else "error") == "error"]
    corpus_raw = corpus_text(analysis if isinstance(analysis, dict) else {})
    corpus = norm(corpus_raw)
    cbg = bigrams(corpus)
    out = {"metrics": {"corpus_chars": len(corpus)}, "reasons": [], "triggers": [], "needs": [],
           "missing_lines": [], "unsupported_units": [], "extra_digits": [], "missing_latin": []}

    if not reference:
        out["metrics"].update(recall=None, unsupported_ratio=None, digit_recall=None, latin_recall=None,
                              length_ratio=None, ref_weight=0, ref_mean_conf=None)
        out["triggers"].append("no_reference")
        if val_errors:
            out["decision"], out["reasons"] = "retry", ["validator_errors"]
        elif len(corpus) >= th["rich_corpus_chars"]:
            out["decision"], out["reasons"] = "unverified", ["no_reference"]
        else:
            out["decision"], out["reasons"] = "accept", ["no_reference_little_text"]
        return out

    # ---- recall of reference words (weighted), per engine and overall
    tot = found = 0.0
    by_engine = {}
    for ln, w in _ref_words(reference):
        wt = W.get(w.get("weight", "low"), 0.0)
        nw = norm(w["text"])
        if wt <= 0 or len(nw) < 1:
            w["_found"] = None
            continue
        ok = fuzzy_in(nw, corpus, cbg, th["fuzzy_word"]) if len(nw) >= 2 else (nw in corpus)
        w["_found"] = ok
        tot += wt
        found += wt if ok else 0
        e = by_engine.setdefault(ln.get("engine", "?"), [0.0, 0.0])
        e[0] += wt
        e[1] += wt if ok else 0
    recall = (found / tot) if tot else None
    out["metrics"]["recall"] = None if recall is None else round(recall, 3)
    out["metrics"]["recall_by_engine"] = {k: round(v[1] / v[0], 3) for k, v in by_engine.items() if v[0]}
    out["metrics"]["ref_weight"] = round(tot, 1)
    out["metrics"]["ref_mean_conf"] = (reference.get("quality") or {}).get("mean_conf")

    # ---- disputed lines (for feedback and crops)
    for ln in reference.get("lines") or []:
        ws = [w for w in ln.get("words") or [] if w.get("_found") is not None]
        if not ws:
            continue
        lw = sum(W.get(w.get("weight"), 0.0) for w in ws)
        lf = sum(W.get(w.get("weight"), 0.0) for w in ws if w["_found"])
        ratio = lf / lw if lw else 1.0
        missing = [w["text"] for w in ws if not w["_found"]]
        strong_miss = [w["text"] for w in ws if not w["_found"] and w.get("weight") == "high" and len(norm(w["text"])) >= 3]
        if lw >= 1.0 and (ratio < th["line_disputed_below"] or strong_miss):
            out["missing_lines"].append({"id": ln["id"], "engine": ln.get("engine"), "text": ln["text"],
                                         "box": ln.get("box"), "conf": ln.get("conf"), "weight": round(lw, 1),
                                         "found_ratio": round(ratio, 2), "missing_words": missing})
    out["missing_lines"].sort(key=lambda m: (-m["weight"] * (1 - m["found_ratio"]), m["id"]))

    # ---- unsupported analysis text (possible hallucination / OCR miss)
    ref_text = " ".join((reference.get("text") or {}).values())
    rbg = bigrams(ref_text)
    # OCR noise is scattered over single words; an invented sentence is a whole field the reference never saw.
    # So besides the global ratio, any text field that is mostly unsupported is reported on its own.
    tot_chars = uns_chars = 0
    out["unsupported_fields"] = []
    for path, text, kind in corpus_parts(analysis):
        if kind == "diagram":
            continue
        f_tot = f_uns = 0
        for u in units(text):
            tot_chars += len(u)
            f_tot += len(u)
            if not fuzzy_in(u, ref_text, rbg, th["fuzzy_unit"]):
                uns_chars += len(u)
                f_uns += len(u)
                if len(out["unsupported_units"]) < 30:
                    out["unsupported_units"].append({"field": path, "text": u})
        if kind == "text" and f_tot >= th["field_unsupported_min_chars"] and f_uns / f_tot >= th["field_unsupported_frac"]:
            out["unsupported_fields"].append({"field": path, "text": text[:120], "frac": round(f_uns / f_tot, 2)})
    uns = (uns_chars / tot_chars) if tot_chars else 0.0
    out["metrics"]["unsupported_ratio"] = round(uns, 3)
    out["metrics"]["unsupported_fields"] = len(out["unsupported_fields"])
    out["metrics"]["length_ratio"] = round(len(corpus) / len(norm(ref_text)), 2) if ref_text else None

    # ---- digits and Latin identifiers: where LLMs silently "fix" what they read
    # Only high-weight words count: a mid-confidence misread (DBMS -> 08146) must not fail a correct
    # analysis. A high-weight word whose digits/identifier are absent pulls its line into the crop queue so
    # the disagreement is settled by a cheap look instead of a guess.
    ref_digits, ref_latin = set(), set()
    agy_digits = set(d for d in digit_seqs(corpus_raw) if d)
    agy_latin = set(latin_tokens(corpus_raw))
    disputed_ids = {m["id"] for m in out["missing_lines"]}

    def latin_ok(t: str) -> bool:
        # exact, or the OCR glued/clipped it: 'yperplane' from '초평면(Hyperplane)', 'hyperplane)을' etc.
        return t in agy_latin or (len(t) >= 4 and any(t in a or a in t for a in agy_latin if len(a) >= 4))

    for ln, w in _ref_words(reference):
        if w.get("weight") != "high":
            continue
        ds = [d for d in digit_seqs(w["text"]) if len(d) >= 2]  # single digits are bullets / markers as often as content
        lt = latin_tokens(w["text"])
        ref_digits.update(ds)
        ref_latin.update(lt)
        miss = [d for d in ds if d not in agy_digits] + [t for t in lt if not latin_ok(t)]
        if miss and ln["id"] not in disputed_ids and ln.get("box"):
            disputed_ids.add(ln["id"])
            out["missing_lines"].append({"id": ln["id"], "engine": ln.get("engine"), "text": ln["text"],
                                         "box": ln.get("box"), "conf": ln.get("conf"), "weight": 1.0,
                                         "found_ratio": 0.0, "missing_words": [w["text"]], "reason": "digit_or_identifier"})
    out["metrics"]["digit_recall"] = round(len(ref_digits & agy_digits) / len(ref_digits), 3) if ref_digits else None
    out["metrics"]["latin_recall"] = round(sum(latin_ok(t) for t in ref_latin) / len(ref_latin), 3) if ref_latin else None
    out["extra_digits"] = sorted(d for d in agy_digits - ref_digits if len(d) >= 2)[:20]
    out["missing_latin"] = sorted(t for t in ref_latin if not latin_ok(t))[:20]

    # ---- structural triggers
    lay = reference.get("layout") or {}
    a = analysis if isinstance(analysis, dict) else {}
    if lay.get("available"):
        # indented code also forms an aligned grid, so a multi-line code block explains the candidate away
        long_code = any(isinstance(cb, dict) and len(str(cb.get("code", "")).splitlines()) >= 3
                        for cb in a.get("code_blocks") or [])
        if (lay.get("table_candidate") or lay.get("ruled_table")) and not a.get("tables") and not long_code:
            out["triggers"].append("table_candidate_undeclared")
        if (lay.get("graphics_ratio") or 0) >= th["graphics_trigger"] and not a.get("diagrams"):
            out["triggers"].append("graphics_undeclared")
    q = reference.get("quality") or {}
    thin = tot < th["min_ref_weight"]
    unreliable = q.get("mean_conf") is not None and q["mean_conf"] < th["ref_conf_unreliable"]
    if thin:
        out["triggers"].append("ref_thin")
    if unreliable:
        out["triggers"].append("ref_unreliable")
    if thin and len(corpus) >= th["rich_corpus_chars"]:
        out["triggers"].append("agy_rich_ref_empty")

    # ---- decision
    r = out["reasons"]
    if val_errors:
        r.append("validator_errors")
        out["decision"] = "retry"
    elif thin:
        if len(corpus) >= th["rich_corpus_chars"]:
            r.append("reference too thin to confirm a text-rich analysis")
            out["decision"] = "escalate"
            out["needs"] = ["thumbnail"]
        else:
            r.append("little text on both sides")
            out["decision"] = "accept"
    elif recall is not None and recall < th["recall_retry"]:
        r.append(f"recall {recall:.2f} < {th['recall_retry']}")
        out["decision"] = "retry"
    else:
        problems = []
        if recall is not None and recall < th["recall_accept"]:
            problems.append(f"recall {recall:.2f} < {th['recall_accept']}")
        if not unreliable and uns > th["unsupported_accept"]:
            problems.append(f"unsupported {uns:.2f} > {th['unsupported_accept']}")
        if not unreliable and out["unsupported_fields"]:
            problems.append("unsupported_field: " + ", ".join(f["field"] for f in out["unsupported_fields"][:3]))
        dr, lr = out["metrics"]["digit_recall"], out["metrics"]["latin_recall"]
        if dr is not None and dr < th["digit_accept"]:
            problems.append(f"digit_recall {dr:.2f} < {th['digit_accept']}")
        if lr is not None and lr < th["latin_accept"]:
            problems.append(f"latin_recall {lr:.2f} < {th['latin_accept']}")
        struct = [t for t in out["triggers"] if t in ("table_candidate_undeclared", "graphics_undeclared")]
        problems += struct
        if problems:
            r.extend(problems)
            out["decision"] = "escalate"
            needs = []
            if out["missing_lines"] and any(m.get("box") for m in out["missing_lines"]):
                needs.append("crops")
            if struct or unreliable or not needs or out["unsupported_fields"] or (uns > th["unsupported_accept"] and not unreliable):
                needs.append("thumbnail")
            out["needs"] = needs
        else:
            r.append("reference agrees")
            out["decision"] = "accept"
    return out


# --------------------------------------------------------------------------- feedback for the analyst retry
def feedback_text(sc: dict, validator_errors=()) -> str:
    lines = []
    for i in validator_errors or []:
        if isinstance(i, dict) and i.get("level") == "error":
            lines.append(f"- [검증기] {i['code']}: {i['msg']}")
    if sc.get("missing_lines"):
        lines.append("- [로컬 OCR 대조] 아래 텍스트 줄이 이미지에서 읽혔지만 분석 결과에 없습니다. OCR 오독이 섞여 있을 수 있으니 "
                     "이미지의 해당 부분을 직접 다시 보고 실제 글자를 그대로 전사하세요:")
        for m in sc["missing_lines"][:12]:
            lines.append(f"    - 「{m['text']}」")
    if sc.get("missing_latin"):
        lines.append("- [로컬 OCR 대조] 이미지에 보이는데 분석에 없는 영문 식별자/키워드: " + ", ".join(sc["missing_latin"][:15]))
    if sc.get("extra_digits"):
        lines.append("- [로컬 OCR 대조] 분석에는 있지만 OCR이 이미지에서 찾지 못한 숫자: " + ", ".join(sc["extra_digits"][:15])
                     + " — 이미지의 숫자를 한 자리씩 다시 확인하세요(임의로 보정하지 마세요).")
    for f in sc.get("unsupported_fields") or []:
        lines.append(f"- [로컬 OCR 대조] `{f['field']}`의 「{f['text']}」는 이미지에서 거의 확인되지 않습니다. 실제로 보이는 문장인지 "
                     "다시 확인하고, 보이지 않으면 삭제하세요.")
    uns = [u["text"] for u in sc.get("unsupported_units") or [] if len(u["text"]) >= 3 and not u["text"].isdigit()]
    if uns:
        lines.append(f"- [로컬 OCR 대조] 이미지에서 확인되지 않은 전사 조각: {', '.join(uns[:10])} — 실제로 보이는 글자인지 확인하고, "
                     "보이지 않으면 삭제하세요.")
    for t in sc.get("triggers") or []:
        if t == "table_candidate_undeclared":
            lines.append("- [레이아웃] 표(격자 정렬) 형태가 감지되었는데 `tables`가 비어 있습니다. 표라면 행/열 그대로 `tables`에 넣으세요.")
        if t == "graphics_undeclared":
            lines.append("- [레이아웃] 글자가 아닌 큰 그림 영역이 감지되었는데 `diagrams`가 비어 있습니다. 도식/그림이면 구성요소와 관계를 기록하세요.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- gold-set metrics (calibrate / bench)
def gold_metrics(gold: dict, analysis: dict) -> dict:
    """True quality of `analysis` against a human-corrected `gold` analysis (same schema). Text-only."""
    import difflib
    g_raw, a_raw = corpus_text(gold), corpus_text(analysis)
    g, a = norm(g_raw), norm(a_raw)
    gb, ab = bigrams(g), bigrams(a)
    g_units, a_units = units(g_raw), units(a_raw)
    rec = sum(fuzzy_in(u, a, ab, 0.75) for u in g_units) / len(g_units) if g_units else None
    prec = sum(fuzzy_in(u, g, gb, 0.75) for u in a_units) / len(a_units) if a_units else None
    sim = difflib.SequenceMatcher(None, g, a, autojunk=False).ratio() if g or a else None
    gd, ad = set(digit_seqs(g_raw)), set(digit_seqs(a_raw))
    g_cells = [norm(str(c)) for tb in gold.get("tables") or [] if isinstance(tb, dict)
               for row in (tb.get("rows") or []) for c in (row if isinstance(row, list) else [row])]
    a_cells = [norm(str(c)) for tb in analysis.get("tables") or [] if isinstance(tb, dict)
               for row in (tb.get("rows") or []) for c in (row if isinstance(row, list) else [row])]
    cell_acc = (sum(c in a_cells for c in g_cells) / len(g_cells)) if g_cells else None
    return {"true_recall": None if rec is None else round(rec, 3),
            "true_precision": None if prec is None else round(prec, 3),
            "char_similarity": None if sim is None else round(sim, 3),
            "digit_accuracy": round(len(gd & ad) / len(gd), 3) if gd else None,
            "cell_accuracy": None if cell_acc is None else round(cell_acc, 3),
            "hallucination_rate": None if prec is None else round(1 - prec, 3)}


def suggest_thresholds(rows: list, target_false_accept: float = 0.05) -> dict:
    """rows: [{"proxy": <score() dict>, "truth": <gold_metrics() dict>}]. Grid-search recall/unsupported
    thresholds that accept as many slides as possible while keeping false accepts (true_recall < 0.9 or
    true_precision < 0.9) at or below the target rate."""
    best = None
    usable = [r for r in rows if r["proxy"]["metrics"].get("recall") is not None and r["truth"].get("true_recall") is not None]
    if not usable:
        return {"error": "no slide has both a reference and a gold file"}
    for ra in (0.7, 0.75, 0.8, 0.85, 0.9, 0.93, 0.95, 0.97):
        for ua in (0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.4):
            acc = [r for r in usable if r["proxy"]["metrics"]["recall"] >= ra and r["proxy"]["metrics"]["unsupported_ratio"] <= ua]
            bad = [r for r in acc if (r["truth"]["true_recall"] or 0) < 0.9 or (r["truth"].get("true_precision") or 1) < 0.9]
            far = len(bad) / len(acc) if acc else 0.0
            if far <= target_false_accept:
                cand = (len(acc), ra, -ua)  # most accepted; ties -> the stricter pair (safer on unseen slides)
                if best is None or cand > best[0]:
                    best = (cand, {"recall_accept": ra, "unsupported_accept": ua, "accepted": len(acc),
                                   "false_accepts": len(bad), "of": len(usable)})
    if not best:
        return {"error": "no threshold pair meets the target; the proxy does not separate good from bad on this gold set"}
    res = best[1]
    if len(usable) < 10:
        res["warning"] = f"only {len(usable)} gold slide(s); thresholds fitted on fewer than 10 are not reliable"
    return res

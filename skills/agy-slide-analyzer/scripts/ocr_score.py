"""ocr_score.py - compare an agy slide analysis with its sealed OCR reference and decide what happens next.

    score(analysis, reference, thresholds, validator_errors=(), tkeys=None, boilerplate=None) -> dict
        metrics      recall, unsupported_ratio, digit_recall, latin_recall, length_ratio, recall_by_engine,
                     content_chars (no header/footer or page number, title once), template_lines,
                     boilerplate_chars (deck boilerplate in header/footer items, left out of unsupported), ...
        decision     accept | retry | escalate | unverified
        reasons      why
        missing_lines      reference lines the analysis does not contain (-> retry feedback / crops); `reason` is
                           digit_or_identifier | operator_or_digit | line_order | copy_mismatch when not plain recall
        unsupported_units  analysis text the reference does not contain (-> possible hallucination)
        extra_digits       digit sequences in the analysis that no reference word has
        line_issues        OCR lines whose anchored analysis line has another operator or number, and code lines
                           the analysis puts in another order (see line_token_issues)
        copy_issues        lines the analyst wrote twice whose operators or numbers differ (see copy_consistency)
        triggers           structural signals (table_candidate_undeclared, graphics_undeclared, ref_thin, ...)
        needs              what the reviewer must look at: [] | ["crops"] | ["thumbnail"] | ["crops", "thumbnail"]
    template_keys(refs, min_slides=5) -> set       deck-template OCR lines (same text and place, in the header/footer
                                                   band, on >= min_slides refs); pass the set to score(tkeys=...) to
                                                   leave them out of recall
    hf_boilerplate(analyses, min_slides=3) -> set  units of header/footer / page_number items in >= min_slides
                                                   analyses (logo, slogan, section band); pass the set to
                                                   score(boilerplate=...) to leave them out of unsupported
    legacy_thresholds(th=None) -> dict             th with every switch of SWITCHES_OFF off: the first-release
                                                   scorer's view (shadow mode's `score_legacy`; on v2 references it
                                                   gives the stock decisions)
    line_token_issues(analysis, reference, tkeys=None) -> list   [{id, text, analysis_line, field, issues}]
    text_token_issues(text, analysis, anchor=None) -> list   the same check for one reading (Claude's text of a crop)
    copy_consistency(analysis) -> list             [{fields, lines, tokens}]; needs no reference
    feedback_text(sc, validator_errors) -> str     Korean feedback for the analyst retry prompt
    load_thresholds(*paths) -> dict                DEFAULT_THRESHOLDS overlaid with the first existing JSON

An issue is {"kind": "op" | "digit" | "order", "read": [...], "analysis": [...]}: tokens read from the image (by
OCR or by Claude) against the analysis tokens; for "order" the two OCR line ids and their analysis line indices.

Nothing here opens the image. The reference is the only "sight" the scorer has, and it was produced
before the analysis existed. Every check added after the first release has a DEFAULT_THRESHOLDS switch,
so each one can be measured leave-one-out.
"""
from __future__ import annotations

import difflib
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from textnorm import (DIGIT_RE, PUNCT_RE, SKEL_TABLE, bigrams, digit_seqs, fold, fuzzy_in, latin_tokens,  # noqa: E402,F401
                      nfkc, norm, skel, units)

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
    "graphics_trigger_nocode": 0.015,  # the same on layout v3's graphics_ratio_nocode (code panels painted out);
                                       # None = the graphics_trigger rule on every layout
    "table_v2": True,            # use layout v3's table_grid2 / ruled_table2 when the reference has them
    "fold_match": True,          # OCR look-alikes (EnabIing, Pe0Ple, v—show) still match (textnorm.fold / skel)
    "template_exclusion": True,  # deck-template OCR lines (tkeys) leave recall; header/footer items leave content_chars
    "close_thin_shortcut": True,  # a thin reference accepts only analysis content it supports (False: old blind accept)
    "line_check": True,          # operators / numbers / code-line order on anchored lines
    "latin_mid": True,           # without a second recognizer, also check identifiers of mid-weight words
    "copy_check": True,          # a line written twice must agree on operators and numbers
    "glued_digit_skip": True,    # digit runs glued to a Latin letter (He110, tit1e) are glyph questions, not numbers
    "hf_boilerplate": True,      # deck boilerplate units (hf_boilerplate) of header/footer items leave unsupported
    "fuzzy_word": 0.75,          # bigram overlap for a reference word to count as found
    "fuzzy_unit": 0.70,          # bigram overlap for an analysis unit to count as supported
    "weights": {"high": 1.0, "variant": 1.0, "mid": 0.5, "low": 0.0},
}

# every check added after the first release, switched off: score() with these is the stock scorer's view
SWITCHES_OFF = {"graphics_trigger_nocode": None, "table_v2": False, "fold_match": False, "template_exclusion": False,
                "close_thin_shortcut": False, "line_check": False, "latin_mid": False, "copy_check": False,
                "glued_digit_skip": False, "hf_boilerplate": False}
STRONG = frozenset({"high", "variant"})  # word weights read twice alike: their digits and identifiers are checked
NONCONTENT_REGIONS = ("header/footer", "page_number")
LINE_ANCHOR = 0.7     # skel containment for an OCR line to be compared with an analysis line
COPY_SIMILAR = 0.85   # skel containment, both ways, for two analysis lines to be copies of each other
TOK_RE = re.compile(r"[A-Za-z0-9_$]*[A-Za-z_$][A-Za-z0-9_$]*|\d+|===|!==|==|!=|<=|>=|=>|&&|\|\||\+\+|--|[<>=!+\-*/%]")
# operators OCR reads reliably enough to compare; a lone - = + ! is what it produces for dashes, bullets and l
CMP_OPS = frozenset({"===", "!==", "==", "!=", "<=", ">=", "=>", "&&", "||", "<", ">", "%"})
OPS = frozenset(CMP_OPS | {"++", "--", "=", "!", "+", "-", "*", "/"})
_VARIANT_SUFFIX = re.compile(r"_(?:x|up)\d+$")
TEMPLATE_TOP, TEMPLATE_BOTTOM = 0.2, 0.12  # page bands where deck-template lines (logo, slogan, footer) may sit
_TKEY_TABLE = str.maketrans({"i": "l", "|": "l"})
ANCHOR_TIE = 0.05     # containment within this of the best still counts as a tie when lines are assigned one-to-one


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


def _opt(th: dict, key: str):
    return th.get(key, DEFAULT_THRESHOLDS[key])


def legacy_thresholds(th: dict = None) -> dict:
    return {**(th or DEFAULT_THRESHOLDS), **SWITCHES_OFF}


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


def _hf_paths(d: dict) -> set:
    """corpus_parts paths of the verbatim items the analyst tagged header/footer or page_number."""
    return {f"verbatim_text[{i}]" for i, t in enumerate(d.get("verbatim_text") or [])
            if isinstance(t, dict) and t.get("region") in NONCONTENT_REGIONS}


def _content_parts(d: dict) -> list:
    """corpus_parts without the verbatim items the analyst marked as deck boilerplate."""
    skip = _hf_paths(d)
    return [p for p in corpus_parts(d) if p[0] not in skip]


def hf_boilerplate(analyses, min_slides: int = 3) -> set:
    """Units of the verbatim items tagged header/footer or page_number in at least `min_slides` different analyses:
    the deck's logo, slogan and section band. Units of other fields never count, so the deck's common body terms
    never become boilerplate. `analyses` is an iterable or a {name: analysis} dict; build it from every analysis of
    the deck, not only the slides being scored."""
    c = Counter()
    for d in (analyses.values() if isinstance(analyses, dict) else analyses):
        if isinstance(d, dict):
            hf = _hf_paths(d)
            c.update({u for path, text, _k in corpus_parts(d) if path in hf for u in units(text)})
    return {u for u, n in c.items() if n >= min_slides}


def _content_chars(d: dict) -> int:
    # the title is usually also a verbatim 'title' item; counted twice it makes a one-line slide look rich
    parts = _content_parts(d)
    verb = {norm(p[1]) for p in parts if p[0].startswith("verbatim_text[")}
    return len(norm(" ".join(p[1] for p in parts if not (p[0] == "title" and norm(p[1]) in verb))))


# --------------------------------------------------------------------------- deck template lines
def _template_key(ln: dict, ref: dict):
    """Key of an OCR line that may be deck template: only lines centred in the top TEMPLATE_TOP or bottom
    TEMPLATE_BOTTOM of the page and outside code panels qualify. A progressive build (one code listing on every
    step of a walkthrough) repeats content at the same place, so the body of the page never does. The text is
    folded only for OCR's I/l confusion: operators and digits stay (skel would merge `x > 1` and `x < l`)."""
    size = ref.get("size")
    if not ln.get("box") or not size or len(size) < 2 or not size[0] or not size[1]:
        return None
    l, t, w, h = ln["box"]
    cy = (t + h / 2) / size[1]
    if TEMPLATE_TOP < cy < 1 - TEMPLATE_BOTTOM:
        return None
    if _panel(ln["box"], (ref.get("layout") or {}).get("code_boxes") or ()) is not None:
        return None
    k = re.sub(r"\s+", "", fold(ln.get("text") or "").lower()).translate(_TKEY_TABLE)
    return (k, round(cy * 50), round((l + w / 2) / size[0] * 50)) if k else None


def template_keys(refs, min_slides: int = 5) -> set:
    """(folded text, centre row, centre column on a 50x50 grid) of the header/footer-band OCR lines found at the
    same place on at least `min_slides` references: the deck's logo, slogan and footer. `refs` is an iterable of
    references or a {name: reference} dict; build it from every reference of the deck, not only the slides being
    scored."""
    c = Counter()
    for ref in (refs.values() if isinstance(refs, dict) else refs):
        if isinstance(ref, dict):
            c.update({k for k in (_template_key(ln, ref) for ln in ref.get("lines") or []) if k})
    return {k for k, v in c.items() if v >= min_slides}


def _content_lines(reference: dict, tkeys) -> list:
    tk = {tuple(k) for k in tkeys} if tkeys else set()
    if not tk:
        return list(reference.get("lines") or [])
    return [ln for ln in reference.get("lines") or [] if _template_key(ln, reference) not in tk]


def _agreement(ref: dict) -> str:
    """cross_engine | variants | single: what confirmed the reference's strong words (layout v3 refs say it)."""
    if ref.get("agreement"):
        return ref["agreement"]
    names = {_VARIANT_SUFFIX.sub("", str(e.get("engine") or "")) for e in ref.get("engines") or [] if e.get("ok")}
    return "cross_engine" if len(names) > 1 else "single"


# --------------------------------------------------------------------------- line-level token checks
def _toks(s: str) -> list:
    return TOK_RE.findall(fold(s))


def _tkey(t: str) -> str:
    return t if t[0].isdigit() or t in OPS else t.lower().translate(SKEL_TABLE)


def _containment(r: str, a: str) -> float:
    """Share of r's characters that line up with a (difflib matching blocks)."""
    if not r:
        return 0.0
    return sum(b.size for b in difflib.SequenceMatcher(None, r, a, autojunk=False).get_matching_blocks()) / len(r)


def _analysis_lines(d: dict) -> list:
    """[(field_path, kind, line, line_index, skel)] for every non-empty line of every transcribed field."""
    out = []
    for path, text, kind in corpus_parts(d):
        for i, l in enumerate(str(text).split("\n")):
            if l.strip():
                out.append((path, kind, l, i, skel(l)))
    return out


def _anchor(text: str, a_lines: list):
    """-> (analysis line, containment, len) of the line holding most of `text`; ties go to the closest length."""
    r = skel(text)
    if len(r) < 4:
        return None, 0.0, len(r)
    best, bs = None, (0.0, 0)
    for al in a_lines:
        c = (round(_containment(r, al[4]), 3), -abs(len(al[4]) - len(r)))
        if c > bs:
            best, bs = al, c
    return best, bs[0], len(r)


def _pick(text: str, a_lines: list, used=()):
    """-> (index into a_lines, containment, len) like _anchor, but a line another OCR line already took (`used`)
    gives way to an untaken one within ANCHOR_TIE: a line shown twice on the slide is compared with both copies."""
    r = skel(text)
    if len(r) < 4 or not a_lines:
        return None, 0.0, len(r)
    ranked = sorted(((round(_containment(r, al[4]), 3), -abs(len(al[4]) - len(r)), -k) for k, al in enumerate(a_lines)),
                    reverse=True)
    top = ranked[0][0]
    for c, _d, nk in ranked:
        if c < top - ANCHOR_TIE:
            break
        if -nk not in used:
            return -nk, c, len(r)
    return -ranked[0][2], top, len(r)


def _residue(s: str) -> str:
    return norm(TOK_RE.sub(" ", fold(s)))


def _token_diff(read: str, written: str):
    """Operator and number substitutions between two versions of one line, or None when their tokens do not line
    up (same characters, different line). Only replace-type differences count: OCR drops and adds tokens. A line
    whose only tokens are numbers ('최대 1000개') lines up when the text around them is the same."""
    rt, at = _toks(read), _toks(written)
    rk = [_tkey(t) for t in rt]
    sm = difflib.SequenceMatcher(None, rk, [_tkey(t) for t in at], autojunk=False)
    if rk and sum(b.size for b in sm.get_matching_blocks()) / len(rk) < 0.5:
        rr = _residue(read)
        if len(rr) < 2 or rr != _residue(written) or len(rt) != len(at) or len(rt) > 2:
            return None
    issues = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op != "replace":
            continue
        for kind, keep in (("op", CMP_OPS.__contains__), ("digit", str.isdigit)):
            ro, ao = [t for t in rt[i1:i2] if keep(t)], [t for t in at[j1:j2] if keep(t)]
            # one token for one: a lone OCR fragment ('const 5' for 'const scores = [72, 85]') is not a substitution
            if ro and len(ro) == len(ao) and "".join(ro) != "".join(ao):
                issues.append({"kind": kind, "read": ro, "analysis": ao})
    return issues


def _panel(box, panels):
    cx, cy = box[0] + box[2] / 2, box[1] + box[3] / 2
    return next((k for k, (L, T, W, H) in enumerate(panels) if L <= cx <= L + W and T <= cy <= T + H), None)


def _order_issues(anchors: list, a_lines: list, panels) -> list:
    """OCR lines anchored to one code field must map, top to bottom, to non-decreasing analysis lines. With code
    panels (layout v3) only lines of the same panel are compared, otherwise only vertically adjacent ones."""
    per_field = {}
    for path, _k, l, _i, _s in a_lines:
        per_field.setdefault(path, []).append(l.strip())
    seqs = {}
    for ln, best in anchors:
        if per_field[best[0]].count(best[2].strip()) == 1:  # a repeated line has no single place
            seqs.setdefault(best[0], []).append((ln["box"][1], ln["box"][0], best[3], ln))
    out = []
    for path, seq in seqs.items():
        seq.sort(key=lambda x: (x[0], x[1]))
        for (y1, _x1, i1, l1), (y2, _x2, i2, l2) in zip(seq, seq[1:]):
            hmax = max(l1["box"][3], l2["box"][3])
            if panels:
                p1 = _panel(l1["box"], panels)
                adjacent = p1 is not None and p1 == _panel(l2["box"], panels) and y2 - y1 > 0.5 * hmax
            else:
                adjacent = 0.5 * hmax < y2 - y1 <= 1.8 * hmax
            if i2 < i1 and adjacent:
                out.append({"id": l2.get("id"), "text": l2["text"], "analysis_line": None, "field": path,
                            "issues": [{"kind": "order", "read": [l1.get("id"), l2.get("id")], "analysis": [i1, i2]}]})
    return out


def _line_check(analysis: dict, lines: list, panels=None) -> list:
    al = _analysis_lines(analysis)
    code = [x for x in al if x[1] == "code"]
    out, anchors, used, used_code = [], [], set(), set()
    for ln in lines:
        k, cont, rlen = _pick(ln.get("text") or "", al, used)
        if k is None or cont < LINE_ANCHOR:
            continue
        used.add(k)
        best = al[k]
        if rlen >= 8 and ln.get("box"):
            # the order check runs on the code copy even when the line is also (first) in verbatim_text
            kc, cc, _n = _pick(ln["text"], code, used_code)
            if kc is not None and cc >= 0.9 and cc >= cont - ANCHOR_TIE:
                used_code.add(kc)
                anchors.append((ln, code[kc]))
        iss = _token_diff(ln["text"], best[2])
        if iss:
            out.append({"id": ln.get("id"), "text": ln["text"], "analysis_line": best[2], "field": best[0], "issues": iss})
    return out + _order_issues(anchors, al, panels)


def line_token_issues(analysis, reference, tkeys=None) -> list:
    """Each OCR line is anchored to the analysis line with the best skel containment (>= LINE_ANCHOR, >= 50% token
    overlap) and compared token by token: another comparison operator (=== != <= && % ...) or another standalone
    number is an issue. Anchoring is one-to-one where it can be: a line the slide shows twice is compared with both
    analysis copies. OCR lines of one code panel must also keep their order. Deck-template lines are skipped."""
    if not reference or not isinstance(analysis, dict):
        return []
    return _line_check(analysis, _content_lines(reference, tkeys), (reference.get("layout") or {}).get("code_boxes"))


def _copies(x, y) -> bool:
    (p1, k1, _l1, _i1, a1), (p2, k2, _l2, _i2, a2) = x, y
    if p1 == p2 or (k1 == k2 and "title" not in (p1, p2)):
        return False  # two verbatim items may legitimately be near-twins (1.1 / 2.1)
    if len(a1) < 6 or len(a2) < 6 or min(len(a1), len(a2)) / max(len(a1), len(a2)) < 0.9:
        return False
    return _containment(a1, a2) >= COPY_SIMILAR and _containment(a2, a1) >= COPY_SIMILAR


def _op_num(line: str) -> list:
    return [t for t in _toks(line) if t in CMP_OPS or t.isdigit()]


def copy_consistency(analysis) -> list:
    """Lines the analyst wrote twice (code_blocks + verbatim_text, title + verbatim title, ...) must carry the same
    comparison operators and numbers. Every OCR check matches the better copy, so a wrong second copy is invisible
    to them. -> [{"fields": [path, path], "lines": [line, line], "tokens": [[...], [...]]}]"""
    al = _analysis_lines(analysis if isinstance(analysis, dict) else {})
    out = []
    for i, x in enumerate(al):
        for y in al[i + 1:]:
            if _copies(x, y):
                kx, ky = _op_num(x[2]), _op_num(y[2])
                if kx != ky:
                    out.append({"fields": [x[0], y[0]], "lines": [x[2].strip()[:120], y[2].strip()[:120]],
                                "tokens": [kx, ky]})
    return out


def text_token_issues(text, analysis, anchor=None) -> list:
    """line_token_issues for one reading (Claude's text of a line crop or thumbnail; a list is read as lines). Each
    read line is compared with its anchored analysis line and with that line's copies in other fields, so a wrong
    copy cannot hide behind a right one. anchor: the analysis line the scorer compared this crop's OCR line with
    (a line written twice has a right and a wrong twin; the reading is held against the one in dispute too).
    -> [{id: None, text, analysis_line, field, issues}]"""
    if isinstance(text, (list, tuple)):
        text = "\n".join(str(t) for t in text)
    al = _analysis_lines(analysis if isinstance(analysis, dict) else {})
    out = []
    for line in str(text or "").split("\n"):
        best, cont, _n = _anchor(line, al) if line.strip() else (None, 0.0, 0)
        if best is None or cont < LINE_ANCHOR:
            continue
        cands = [best] + [x for x in al if x is not best and _copies(best, x)]
        if anchor and str(anchor).strip() not in {c[2].strip() for c in cands}:
            cands.append(next((x for x in al if x[2].strip() == str(anchor).strip()), ("anchor", "text", str(anchor), 0, "")))
        for cand in cands:
            iss = _token_diff(line, cand[2])
            if iss:
                out.append({"id": None, "text": line, "analysis_line": cand[2], "field": cand[0], "issues": iss})
    return out


def _ref_line_for(text: str, lines: list):
    """The boxed OCR line most contained in `text` (>= LINE_ANCHOR), or None."""
    t, best, bs = skel(text), None, LINE_ANCHOR
    for ln in lines:
        r = skel(ln.get("text") or "")
        if ln.get("box") and len(r) >= 4:
            c = _containment(r, t)
            if c > bs or (best is None and c >= bs):
                best, bs = ln, c
    return best


# --------------------------------------------------------------------------- scoring
def _disputed(ln: dict, reason: str, words=(), **extra) -> dict:
    """extra: analysis_line / field (the analysis line an operator_or_digit crop is held against), order_field (the
    code field whose line order a line_order crop is part of); crops.prepare hands them to judge."""
    return {"id": ln["id"], "engine": ln.get("engine"), "text": ln["text"], "box": ln.get("box"), "conf": ln.get("conf"),
            "weight": 1.0, "found_ratio": 0.0, "missing_words": list(words), "reason": reason, **extra}


def _dispute(out: dict, disputed_ids: set, ln: dict, reason: str, **extra):
    """Queue `ln` as a disputed line, or add the reason's context to the entry it already has."""
    if ln["id"] in disputed_ids:
        m = next((m for m in out["missing_lines"] if m["id"] == ln["id"]), None)
        if m is not None:
            m.setdefault("reason", reason)
            for k, v in extra.items():
                m.setdefault(k, v)
        return
    if ln.get("box"):
        disputed_ids.add(ln["id"])
        out["missing_lines"].append(_disputed(ln, reason, **extra))


def _folded_found(text: str, nw: str, corpus: str, cbg: set, scorpus: str, sbg: set, thresh: float) -> bool:
    """A reference word OCR spelled with look-alikes (EnabIing, Pe0Ple, v—show, Netwo「k) still counts as found."""
    if re.search(r"[A-Za-z]", text):
        sw = skel(text)
        if len(sw) >= 2 and fuzzy_in(sw, scorpus, sbg, thresh):
            return True
    fw = norm(fold(text))
    return len(nw) >= 2 and fw != nw and fuzzy_in(fw, corpus, cbg, thresh)


def _digit_runs(text: str, glued_skip: bool) -> list:
    """Digit sequences of >= 2 digits; with glued_skip, a run right after a Latin letter (He110, tit1e, myUr1, v1.2)
    is left to the identifier check, which compares it skeleton-folded. A run followed by letters is a number with
    its unit (1200px, 500ms, 16GB) and is checked."""
    s = nfkc(text)
    out = []
    for m in DIGIT_RE.finditer(s):
        a = m.start()
        if glued_skip and a > 0 and s[a - 1].isascii() and s[a - 1].isalpha():
            continue
        d = re.sub(r"\D", "", m.group(0))
        if len(d) >= 2:
            out.append(d)
    return out


def score(analysis: dict, reference, thresholds: dict = None, validator_errors=(), tkeys=None, boilerplate=None) -> dict:
    """`tkeys`: template_keys() of the whole deck; those OCR lines do not count for recall or disputes.
    `boilerplate`: hf_boilerplate() of the whole deck; those units of header/footer / page_number items leave the
    unsupported ratio and fields (every other unit of such an item is checked as usual), but still count for recall.
    A reference without boxes (a PDF text layer) is exact: an operator / number line issue against it is a retry."""
    th = thresholds or DEFAULT_THRESHOLDS
    W = {**DEFAULT_THRESHOLDS["weights"], **(th.get("weights") or {})}
    a = analysis if isinstance(analysis, dict) else {}
    val_errors = [e for e in (validator_errors or []) if (e.get("level") if isinstance(e, dict) else "error") == "error"]
    templ, folding = bool(_opt(th, "template_exclusion")), bool(_opt(th, "fold_match"))
    corpus_raw = corpus_text(a)
    corpus = norm(corpus_raw)
    cbg = bigrams(corpus)
    content_chars = _content_chars(a)
    out = {"metrics": {"corpus_chars": len(corpus), "content_chars": content_chars}, "reasons": [], "triggers": [],
           "needs": [], "missing_lines": [], "unsupported_units": [], "extra_digits": [], "missing_latin": [],
           "line_issues": [], "copy_issues": copy_consistency(a) if _opt(th, "copy_check") else []}

    if not reference:
        out["metrics"].update(recall=None, unsupported_ratio=None, digit_recall=None, latin_recall=None,
                              length_ratio=None, ref_weight=0, ref_mean_conf=None, template_lines=0, boilerplate_chars=0)
        out["triggers"].append("no_reference")
        if val_errors:
            out["decision"], out["reasons"] = "retry", ["validator_errors"]
        elif len(corpus) >= th["rich_corpus_chars"]:
            out["decision"], out["reasons"] = "unverified", ["no_reference"]
        elif out["copy_issues"]:
            out["decision"], out["reasons"] = "unverified", ["no_reference", "copy_mismatch"]
        else:
            out["decision"], out["reasons"] = "accept", ["no_reference_little_text"]
        return out

    all_lines = reference.get("lines") or []
    lines = _content_lines(reference, tkeys if templ else None)
    kept = {id(ln) for ln in lines}
    for ln in all_lines:
        if id(ln) not in kept:
            for w in ln.get("words") or []:
                w["_found"] = None
    out["metrics"]["template_lines"] = len(all_lines) - len(lines)

    # ---- recall of reference words (weighted), per engine and overall
    scorpus = skel(corpus_raw) if folding else ""
    sbg = bigrams(scorpus)
    tot = found = 0.0
    by_engine = {}
    for ln in lines:
        for w in ln.get("words") or []:
            wt = W.get(w.get("weight", "low"), 0.0)
            nw = norm(w["text"])
            if wt <= 0 or len(nw) < 1:
                w["_found"] = None
                continue
            ok = fuzzy_in(nw, corpus, cbg, th["fuzzy_word"]) if len(nw) >= 2 else (nw in corpus)
            if not ok and folding:
                ok = _folded_found(w["text"], nw, corpus, cbg, scorpus, sbg, th["fuzzy_word"])
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
    for ln in lines:
        ws = [w for w in ln.get("words") or [] if w.get("_found") is not None]
        if not ws:
            continue
        lw = sum(W.get(w.get("weight"), 0.0) for w in ws)
        lf = sum(W.get(w.get("weight"), 0.0) for w in ws if w["_found"])
        ratio = lf / lw if lw else 1.0
        missing = [w["text"] for w in ws if not w["_found"]]
        strong_miss = [w["text"] for w in ws if not w["_found"] and w.get("weight") in STRONG and len(norm(w["text"])) >= 3]
        if lw >= 1.0 and (ratio < th["line_disputed_below"] or strong_miss):
            out["missing_lines"].append({"id": ln["id"], "engine": ln.get("engine"), "text": ln["text"],
                                         "box": ln.get("box"), "conf": ln.get("conf"), "weight": round(lw, 1),
                                         "found_ratio": round(ratio, 2), "missing_words": missing})
    out["missing_lines"].sort(key=lambda m: (-m["weight"] * (1 - m["found_ratio"]), m["id"]))

    # ---- unsupported analysis text (possible hallucination / OCR miss)
    ref_text = " ".join((reference.get("text") or {}).values())
    rbg = bigrams(ref_text)
    # the folded fallback keys the raw OCR lines (stored ref['text'] is already norm()'ed: 「 and the like are gone)
    sref = skel(" ".join(str(ln.get("text") or "") for ln in all_lines)) if folding else ""
    srbg = bigrams(sref)
    # OCR noise is scattered over single words; an invented sentence is a whole field the reference never saw.
    # So besides the global ratio, any text field that is mostly unsupported is reported on its own. Items the
    # analyst tagged header/footer or page_number are checked like any other (the tag is the analyst's own word),
    # except for the deck's boilerplate units: the same tagged text on 3+ slides is the deck's, not this slide's.
    bp = set(boilerplate or ()) if _opt(th, "hf_boilerplate") else set()
    hf = _hf_paths(a) if bp else set()
    tot_chars = uns_chars = diag_tot = diag_uns = bp_chars = 0
    out["unsupported_fields"] = []
    for path, text, kind in corpus_parts(a):
        f_tot = f_uns = 0
        for u in units(text):
            if path in hf and u in bp:
                bp_chars += len(u)
                continue
            ok = fuzzy_in(u, ref_text, rbg, th["fuzzy_unit"])
            if not ok and folding:
                su = skel(u)
                ok = len(su) >= 2 and fuzzy_in(su, sref, srbg, th["fuzzy_unit"])
            if kind == "diagram":  # descriptions as often as labels: not transcription, but a thin reference asks
                diag_tot += len(u)
                diag_uns += 0 if ok else len(u)
                continue
            tot_chars += len(u)
            f_tot += len(u)
            if not ok:
                uns_chars += len(u)
                f_uns += len(u)
                if len(out["unsupported_units"]) < 30:
                    out["unsupported_units"].append({"field": path, "text": u})
        if kind == "text" and f_tot >= th["field_unsupported_min_chars"] and f_uns / f_tot >= th["field_unsupported_frac"]:
            out["unsupported_fields"].append({"field": path, "text": text[:120], "frac": round(f_uns / f_tot, 2)})
    uns = (uns_chars / tot_chars) if tot_chars else 0.0
    out["metrics"]["unsupported_ratio"] = round(uns, 3)
    out["metrics"]["unsupported_fields"] = len(out["unsupported_fields"])
    out["metrics"]["diagram_unsupported_chars"] = diag_uns
    out["metrics"]["boilerplate_chars"] = bp_chars
    out["metrics"]["length_ratio"] = round(len(corpus) / len(norm(ref_text)), 2) if ref_text else None

    # ---- digits and Latin identifiers: where LLMs silently "fix" what they read
    # Only strong words (high: two engines agree, variant: two reads of one engine agree) count: a mid-confidence
    # misread (DBMS -> 08146) must not fail a correct analysis. Without a second recognizer the identifiers of
    # mid words in mostly-found lines are checked too (latin_mid), skeleton-folded; their digits are left to the
    # line check (OCR reads 'log(' as '109('). A strong word whose digits/identifier are absent pulls its line
    # into the crop queue so the disagreement is settled by a cheap look instead of a guess.
    ref_digits, ref_latin, glued_digits = set(), set(), set()
    agy_digits = set(d for d in digit_seqs(corpus_raw) if d)
    agy_latin = set(latin_tokens(corpus_raw))
    agy_latin_s = {t.translate(SKEL_TABLE) for t in agy_latin} if folding else set()
    disputed_ids = {m["id"] for m in out["missing_lines"]}

    def latin_ok(t: str) -> bool:
        # exact, or the OCR glued/clipped it: 'yperplane' from '초평면(Hyperplane)', 'hyperplane)을' etc.
        if t in agy_latin or (len(t) >= 4 and any(t in x or x in t for x in agy_latin if len(x) >= 4)):
            return True
        s = t.translate(SKEL_TABLE)
        return folding and (s in agy_latin_s or (len(s) >= 4 and any(s in x or x in s for x in agy_latin_s if len(x) >= 4)))

    latin_mid = bool(_opt(th, "latin_mid")) and _agreement(reference) != "cross_engine"
    glued = bool(_opt(th, "glued_digit_skip"))
    for ln in lines:
        judged = [w for w in ln.get("words") or [] if w.get("_found") is not None]
        lratio = sum(1 for w in judged if w["_found"]) / len(judged) if judged else 0.0
        for w in ln.get("words") or []:
            if w.get("weight") in STRONG:
                ds, lt = _digit_runs(w["text"], glued), latin_tokens(w["text"])
                glued_digits.update(_digit_runs(w["text"], False))  # not checked, but not 'extra' in the analysis
            elif latin_mid and w.get("weight") == "mid" and lratio >= 0.5:
                ds, lt = [], latin_tokens(fold(w["text"]))
            else:
                continue
            ref_digits.update(ds)
            ref_latin.update(lt)
            miss = [d for d in ds if d not in agy_digits] + [t for t in lt if not latin_ok(t)]
            if miss and ln["id"] not in disputed_ids and ln.get("box"):
                disputed_ids.add(ln["id"])
                out["missing_lines"].append(_disputed(ln, "digit_or_identifier", [w["text"]]))
    out["metrics"]["digit_recall"] = round(len(ref_digits & agy_digits) / len(ref_digits), 3) if ref_digits else None
    out["metrics"]["latin_recall"] = round(sum(latin_ok(t) for t in ref_latin) / len(ref_latin), 3) if ref_latin else None
    out["extra_digits"] = sorted(d for d in agy_digits - ref_digits - glued_digits if len(d) >= 2)[:20]
    out["missing_latin"] = sorted(t for t in ref_latin if not latin_ok(t))[:20]

    # ---- operators, numbers and code-line order on anchored lines; copies that disagree
    lay = reference.get("layout") or {}
    if _opt(th, "line_check"):
        by_id = {ln.get("id"): ln for ln in lines}
        for li in _line_check(a, lines, lay.get("code_boxes")):
            out["line_issues"].append(li)
            order = li["issues"][0]["kind"] == "order"
            for lid in (li["issues"][0]["read"] if order else [li["id"]]):
                if by_id.get(lid):
                    extra = {"order_field": li["field"]} if order else {"analysis_line": li["analysis_line"], "field": li["field"]}
                    _dispute(out, disputed_ids, by_id[lid], "line_order" if order else "operator_or_digit", **extra)
    for ci in out["copy_issues"]:
        ln = _ref_line_for(ci["lines"][0], lines) or _ref_line_for(ci["lines"][1], lines)
        if ln and ln["id"] not in disputed_ids:
            disputed_ids.add(ln["id"])
            out["missing_lines"].append(_disputed(ln, "copy_mismatch"))

    # ---- structural triggers
    if lay.get("available"):
        # indented code also forms an aligned grid, so a multi-line code block explains the candidate away
        long_code = any(isinstance(cb, dict) and len(str(cb.get("code", "")).splitlines()) >= 3
                        for cb in a.get("code_blocks") or [])
        # the v3 table keys need OCR word boxes; a layout computed without them (pdf text-layer pages) keeps v2's
        masked = lay.get("text_masked", True) is not False
        v2 = _opt(th, "table_v2") and masked
        grid = lay["table_grid2"] if v2 and "table_grid2" in lay else lay.get("table_candidate")
        ruled = lay["ruled_table2"] if v2 and "ruled_table2" in lay else lay.get("ruled_table")
        if (grid or ruled) and not a.get("tables") and not long_code:
            out["triggers"].append("table_candidate_undeclared")
        gn = _opt(th, "graphics_trigger_nocode")
        if gn is not None and "graphics_ratio_nocode" in lay:
            # code panels are painted out; a layout computed without a word mask (pdf text pages) has no say
            gfx = lay.get("text_masked", True) is not False and (lay["graphics_ratio_nocode"] or 0) >= gn
        else:
            gfx = (lay.get("graphics_ratio") or 0) >= th["graphics_trigger"]
        if gfx and not a.get("diagrams"):
            out["triggers"].append("graphics_undeclared")
    q = reference.get("quality") or {}
    thin = tot < th["min_ref_weight"]
    unreliable = q.get("mean_conf") is not None and q["mean_conf"] < th["ref_conf_unreliable"]
    rich = (content_chars if templ else len(corpus)) >= th["rich_corpus_chars"]
    if thin:
        out["triggers"].append("ref_thin")
    if unreliable:
        out["triggers"].append("ref_unreliable")
    if thin and rich:
        out["triggers"].append("agy_rich_ref_empty")

    # ---- decision
    r = out["reasons"]
    kinds = {i["kind"] for li in out["line_issues"] for i in li["issues"]}
    line_problems = [p for p, hit in (("operator_or_digit_mismatch", kinds & {"op", "digit"}),
                                      ("line_order_mismatch", "order" in kinds), ("copy_mismatch", out["copy_issues"])) if hit]
    struct = [t for t in out["triggers"] if t in ("table_candidate_undeclared", "graphics_undeclared")]
    dr, lr = out["metrics"]["digit_recall"], out["metrics"]["latin_recall"]
    # a text-layer reference (no boxes: the document's own characters) is exact, so another operator or number on
    # an anchored line is the analysis's error: the analyst re-reads, there is no crop or thumbnail to settle it
    exact = bool(all_lines) and all(ln.get("box") is None for ln in all_lines)
    exact_problems = [p for p in line_problems if p == "operator_or_digit_mismatch"] if exact else []
    if val_errors:
        r.append("validator_errors")
        out["decision"] = "retry"
    elif exact_problems:
        r.extend(exact_problems)
        out["decision"] = "retry"
    elif thin and not _opt(th, "close_thin_shortcut"):
        if rich:
            r.append("reference too thin to confirm a text-rich analysis")
            out["decision"] = "escalate"
            out["needs"] = ["thumbnail"]
        else:
            r.append("little text on both sides")
            out["decision"] = "accept"
    elif thin:
        # recall over a few words says little, but whether the analysis content is in the reference still holds
        problems = []
        if recall is not None and recall < th["recall_accept"]:
            problems.append(f"recall {recall:.2f} < {th['recall_accept']}")
        if uns > th["unsupported_accept"]:
            problems.append(f"unsupported {uns:.2f} > {th['unsupported_accept']}")
        if out["unsupported_fields"]:
            problems.append("unsupported_field: " + ", ".join(f["field"] for f in out["unsupported_fields"][:3]))
        # diagram elements are left out of the unsupported ratio; a thin reference cannot vouch for a text-rich
        # diagram, so its labels count here (an invented ERD on a slide whose boxes hold no text)
        if rich and diag_uns and (uns_chars + diag_uns) / max(1, tot_chars + diag_tot) > th["unsupported_accept"]:
            problems.append("reference too thin to confirm a text-rich analysis")
        digits = []  # the strong-word digit / identifier checks hold on a thin reference too
        if dr is not None and dr < th["digit_accept"]:
            digits.append(f"digit_recall {dr:.2f} < {th['digit_accept']}")
        if lr is not None and lr < th["latin_accept"]:
            digits.append(f"latin_recall {lr:.2f} < {th['latin_accept']}")
        problems += digits + line_problems + struct
        if problems:
            r.append("thin reference, content not confirmed")
            r.extend(problems)
            out["decision"] = "escalate"
            lines_matter = digits or line_problems or (recall is not None and recall < th["recall_accept"])
            crops = lines_matter and any(m.get("box") for m in out["missing_lines"])
            out["needs"] = (["crops"] if crops else []) + ["thumbnail"]
        else:
            r.extend(["little text on both sides", "thin reference, content supported"])  # kp reads the first
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
        if dr is not None and dr < th["digit_accept"]:
            problems.append(f"digit_recall {dr:.2f} < {th['digit_accept']}")
        if lr is not None and lr < th["latin_accept"]:
            problems.append(f"latin_recall {lr:.2f} < {th['latin_accept']}")
        problems += line_problems + struct
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
    for li in (sc.get("line_issues") or [])[:8]:
        if li.get("analysis_line") is not None:
            lines.append(f"- [로컬 OCR 대조] 연산자/숫자가 이미지와 다를 수 있는 줄: 이미지 「{li['text']}」 / 분석 「{li['analysis_line']}」 "
                         "— 기호와 숫자를 한 글자씩 다시 확인하세요.")
        else:
            lines.append(f"- [로컬 OCR 대조] 코드 줄 순서가 이미지와 다를 수 있습니다: 「{li['text']}」 주변 줄의 순서를 다시 확인하세요.")
    for ci in (sc.get("copy_issues") or [])[:5]:
        lines.append(f"- [자기 일관성] 같은 줄을 `{ci['fields'][0]}`와 `{ci['fields'][1]}`에 적었는데 연산자/숫자가 다릅니다: "
                     f"「{ci['lines'][0]}」 / 「{ci['lines'][1]}」 — 이미지와 같게 두 곳을 맞추세요.")
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

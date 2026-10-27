"""kp_regions.py - which parts of a slide the character-coverage scorer cannot vouch for.

    select_regions(rec, ref=None) -> [region]   structure regions of one ingested slide, priority-ordered
    verbatim_kind(vt) -> kind | None            uncertain / code / formula for one verbatim item
    is_code_like(text), is_formula(text)        detectors for undeclared code / formula lines
    exact_tokens(text) -> set                   digits and Latin identifiers (incl. 2-char ones such as PK)
    unchecked_lines(rec, ref) -> int            line candidates the MAX_LINE_REGIONS cap left out
    locate_all(regions, rec, ref) -> {rid: loc} crop boxes for one slide's regions, located jointly
    locate(region, rec, ref) -> loc             the same for one region
    apply_budget(regions, budget) -> regions    priority order; the rest becomes skipped_budget

Region = {"rid": "<slide>.<local>", "slide", "local", "kind", "part": [field, index], "priority", "reason"}
kind: code | table | diagram | formula | uncertain | line | visual

The upstream scorer (textnorm.norm) strips operators and is order-blind, so code, tables, diagram edges,
formulas and [?] marks always need a structural check. Lines whose digits/identifiers were never checked
(single OCR engine, thin references) are added when a reference is given.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

import kp_common  # noqa: F401  (puts the shared folder on sys.path)
from textnorm import bigrams, nfkc, norm, overlap

KIND_PRIORITY = {"code": 1, "table": 1, "diagram": 2, "formula": 2, "uncertain": 2, "line": 3, "visual": 3}
FINAL_OK = {"agreed", "minor", "corrected", "agy_upheld", "filled"}
FINAL_BAD = {"disputed", "not_found"}
FINAL_SKIP = {"skipped_budget", "skipped"}
FINAL_INFO = {"missed_content"}  # text a blind reader saw and the analysis lacks; attached to the card -> medium trust
FINAL = FINAL_OK | FINAL_BAD | FINAL_SKIP | FINAL_INFO
MAX_LINE_REGIONS = 6
GRAPHICS_TRIGGER = 0.06

SQL_KW = re.compile(r"\b(SELECT|FROM|WHERE|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|JOIN|GROUP\s+BY|ORDER\s+BY|HAVING|"
                    r"VALUES|INTO|PRIMARY\s+KEY|FOREIGN\s+KEY|REFERENCES|TABLE|SET)\b")
PY_LINE = re.compile(r"^\s*(def|class|import|from|return|for|while|if|elif|else|try|except|with|print)\b.*[:()=]")
ASSIGN_LINE = re.compile(r"^\s*[A-Za-z_][A-Za-z0-9_.\[\]]*\s*(=|\+=|-=)\s*\S")
HANGUL = re.compile(r"[가-힣]")
GREEK = re.compile(r"[Α-Ωα-ω]")
MATH_CHARS = re.compile(r"[\^∑Σ√∫∂≤≥≠±×÷·⁰-₟²³¹]")
EQUATION = re.compile(r"[A-Za-z0-9)\]]\s*=\s*[A-Za-z0-9(\-]")
EXACT_RE = re.compile(r"[A-Za-z0-9_]*\d[A-Za-z0-9_.,:]*|[A-Za-z_][A-Za-z0-9_]+")
META_REGIONS = {"page_number", "header/footer"}
# JS / Vue / HTML lines (an analyst often files a dark code box under verbatim_text)
JS_DECL = re.compile(r"^\s*(const|let|var|function|export|import|return|async|await)\b\s*[\w{\[(]")
MARKUP_LINE = re.compile(r"^\s*(<!--|</?[A-Za-z][\w-]*[\s>/]|//|[})\]]+[;,)]*\s*$)|console\.\w+\(")


def _code_line(l: str) -> bool:
    return bool(SQL_KW.search(l) or PY_LINE.match(l) or ASSIGN_LINE.match(l) or JS_DECL.match(l) or MARKUP_LINE.search(l))


def is_code_like(text: str) -> bool:
    lines = [l for l in (text or "").splitlines() if l.strip()]
    if not lines:
        return False
    if len(lines) == 1:  # one line: only an unmistakable declaration ('const isShow = ref(false)')
        return bool(JS_DECL.match(lines[0]) and "=" in lines[0])
    return sum(1 for l in lines if _code_line(l)) >= 2


def is_formula(text: str) -> bool:
    t = (text or "").strip()
    strong = lambda l: SQL_KW.search(l) or PY_LINE.match(l) or JS_DECL.match(l) or MARKUP_LINE.search(l)  # 'y = wx' stays maths
    if not t or len(t) > 200 or is_code_like(t) or any(strong(l) for l in t.splitlines() if l.strip()):
        return False
    if re.search(r"[\w-]+=\"|=>|\.value\b|\bref\(", t):  # attributes and arrow functions are not maths
        return False
    chars = [c for c in t if not c.isspace()]
    if not chars or len(HANGUL.findall(t)) / len(chars) >= 0.3:
        return False
    return bool(MATH_CHARS.search(t) or GREEK.search(t) or EQUATION.search(t))


def exact_tokens(text: str) -> set:
    return {m.group(0).rstrip(".,:").casefold() for m in EXACT_RE.finditer(text or "") if m.group(0).rstrip(".,:")}


def _region(sid, local, kind, part, reason):
    return {"rid": f"{sid}.{local}", "slide": sid, "local": local, "kind": kind, "part": part,
            "priority": KIND_PRIORITY[kind], "reason": reason}


def table_cells(tb: dict) -> int:
    rows = tb.get("rows") or []
    return len(tb.get("headers") or []) + sum(len(r) if isinstance(r, list) else 1 for r in rows)


def verbatim_kind(vt) -> str:
    if not isinstance(vt, dict) or vt.get("region") in META_REGIONS:
        return None
    text = str(vt.get("text") or "")
    return "uncertain" if "[?]" in text else "code" if is_code_like(text) else "formula" if is_formula(text) else None


def has_graphics(rec: dict) -> bool:
    return "graphics_undeclared" in (rec.get("triggers") or []) or (rec.get("graphics_ratio") or 0) >= GRAPHICS_TRIGGER


def select_regions(rec: dict, ref: dict = None) -> list:
    """Regions of one ingested record (kp_ingest), in priority order. ref adds line / visual regions."""
    an = rec.get("analysis")
    if not an:
        return []
    sid = rec["id"]
    out = []
    for i, cb in enumerate(an.get("code_blocks") or []):
        if isinstance(cb, dict) and str(cb.get("code") or "").strip():
            out.append(_region(sid, f"code{i + 1}", "code", ["code_blocks", i], "code block"))
    for i, tb in enumerate(an.get("tables") or []):
        if isinstance(tb, dict) and table_cells(tb) >= 4:
            out.append(_region(sid, f"table{i + 1}", "table", ["tables", i], f"{table_cells(tb)} cells"))
    for i, dg in enumerate(an.get("diagrams") or []):
        if not isinstance(dg, dict) or dg.get("kind") == "illustration":
            continue
        if (dg.get("relations") or []) or len(dg.get("elements") or []) >= 3:
            out.append(_region(sid, f"diagram{i + 1}", "diagram", ["diagrams", i], "diagram relations"))
    for i, vt in enumerate(an.get("verbatim_text") or []):
        kind = verbatim_kind(vt)
        if kind == "uncertain":
            out.append(_region(sid, f"q{i + 1}", "uncertain", ["verbatim_text", i], "[?] in transcription"))
        elif kind == "code":
            out.append(_region(sid, f"vcode{i + 1}", "code", ["verbatim_text", i], "undeclared code"))
        elif kind == "formula":
            out.append(_region(sid, f"formula{i + 1}", "formula", ["verbatim_text", i], "formula"))
    for i, tb in enumerate(an.get("tables") or []):
        cells = [str(c) for r in (tb.get("rows") or []) for c in (r if isinstance(r, list) else [r])] if isinstance(tb, dict) else []
        cells += [str(h) for h in (tb.get("headers") or [])] if isinstance(tb, dict) else []
        if any("[?]" in c for c in cells) and not any(r["part"] == ["tables", i] for r in out):
            out.append(_region(sid, f"table{i + 1}", "table", ["tables", i], "[?] in table"))
    if ref:
        out += line_regions(rec, ref, out)
        if rec.get("text_check") == "thin_accepted" and has_graphics(rec) and not (an.get("diagrams") or []):
            out.append(_region(sid, "visual1", "visual", ["slide", 0], "thin reference with graphics, no diagram declared"))
    return sorted(out, key=lambda r: (r["priority"], r["rid"]))


def judged_wrong_lines(rec: dict) -> dict:
    """OCR line id -> Claude's blind text, from analyzer judgments that went against the analysis."""
    out = {}
    for j in (rec.get("qa") or {}).get("judgments") or []:
        if j.get("kind") == "line" and j.get("verdict") == "agy_wrong" and "__" in str(j.get("id")):
            out[str(j["id"]).rsplit("__", 1)[1]] = j.get("claude_text")
    return out


def _line_candidates(rec: dict, ref: dict, taken: list) -> list:
    """[(OCR line id, reason)]: lines the analyzer's blind review judged agy_wrong first, then (single-engine / thin
    slides) lines whose digits or identifiers the analysis lacks. Lines a code or table region covers are left to
    that region's vote."""
    wrong = judged_wrong_lines(rec)
    out = [(ln["id"], "analyzer review judged this line against the analysis")
           for ln in ref.get("lines") or [] if ln.get("id") in wrong and ln.get("box")]
    if rec.get("digits_checked") and rec.get("text_check") != "thin_accepted":
        return out
    an = rec["analysis"]
    import ocr_score  # shared
    have = exact_tokens(ocr_score.corpus_text(an))
    covered = []
    for r in taken:
        v = part_value(an, r["part"])
        if r["kind"] == "code":
            covered.append(norm(str(v.get("code") or "") if isinstance(v, dict) else str(v or "")))
        elif r["kind"] == "table" and isinstance(v, dict):
            covered.append(norm(" ".join(str(c) for c in (v.get("headers") or []) + [
                c for row in v.get("rows") or [] for c in (row if isinstance(row, list) else [row])])))
    covered = [bigrams(c) for c in covered if c]
    done = {lid for lid, _ in out}
    for ln in ref.get("lines") or []:
        if not ln.get("box") or ln.get("id") in done:
            continue
        toks = exact_tokens(ln.get("text") or "")
        if not toks:
            continue
        nl = norm(ln.get("text") or "")
        if nl and any(overlap(nl, cb) >= 0.8 for cb in covered):
            continue
        missing = sorted(toks - have)
        if missing or rec.get("text_check") == "thin_accepted":
            out.append((ln["id"], f"tokens not in analysis: {', '.join(missing[:4])}" if missing else "thin reference"))
    return out


def line_regions(rec: dict, ref: dict, taken: list) -> list:
    """The first MAX_LINE_REGIONS line candidates as regions. The analyzer only checks 'high' words (two agreeing
    engines), so with winocr alone nothing was checked. A judged-wrong line's Claude reading becomes read 1."""
    return [_region(rec["id"], f"line{k}", "line", ["ocr_line", lid], why)
            for k, (lid, why) in enumerate(_line_candidates(rec, ref, taken)[:MAX_LINE_REGIONS], start=1)]


def unchecked_lines(rec: dict, ref: dict) -> int:
    """Line candidates beyond the cap: their tokens are known to be missing and nobody reads them."""
    if not ref or not rec.get("analysis"):
        return 0
    return max(0, len(_line_candidates(rec, ref, select_regions(rec))) - MAX_LINE_REGIONS)


# --------------------------------------------------------------------------- localisation
VIEW_KINDS = {"diagram", "visual"}
MAX_CROP_AREA = 0.70


def part_value(an: dict, part: list):
    """The agy text/object a region refers to (None when the path no longer exists)."""
    field, idx = part
    try:
        if field in ("code_blocks", "tables", "diagrams"):
            return an[field][idx]
        if field == "verbatim_text":
            return an["verbatim_text"][idx].get("text")
    except (KeyError, IndexError, TypeError, AttributeError):
        return None
    return None


NEEDLE_KINDS = {"code", "table", "formula", "uncertain"}
BIND_MIN = 0.6   # similarity a needle needs to bind an OCR line (or a word, for table cells)
TIE = 0.02       # candidates this close to the best one count as equally good
GROW_GAP = 3.0   # code / table crops grow over unclaimed lines at most this many line heights away
_KEY_DROP = re.compile(r"[\s.,;:!?·•…()\[\]{}\"'「」『』]+")
_PART_RE = re.compile(r"^(code_blocks|verbatim_text|tables)\[(\d+)\](.*)$")


def match_key(text) -> str:
    """Comparison form for locating text: NFKC, case-folded, no spaces or ignorable punctuation. Unlike norm(),
    operators stay, so 'age >= 30' and 'age > 30' are different needles."""
    return _KEY_DROP.sub("", nfkc(str(text or "").replace("[?]", " ")).casefold())


def _sim(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if min(len(a), len(b)) <= 3:
        return 0.0  # a short needle or word binds only exactly ('1' must not bind '1강')
    sm = SequenceMatcher(None, a, b, autojunk=False)
    return sm.ratio() if sm.real_quick_ratio() >= BIND_MIN and sm.quick_ratio() >= BIND_MIN else 0.0


def _xywh_to_ltrb(b):
    return [b[0], b[1], b[0] + b[2], b[1] + b[3]]


def _union(boxes):
    return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]


def _centre_in(b, box) -> bool:
    cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    return box[0] <= cx <= box[2] and box[1] <= cy <= box[3]


def _dist(b, box) -> float:
    cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    return max(0, box[1] - cy, cy - box[3]) + max(0, box[0] - cx, cx - box[2])


def _median_h(boxes) -> float:
    hs = sorted(b[3] - b[1] for b in boxes)
    return max(1, hs[len(hs) // 2]) if hs else 20


def _codeish(text: str) -> bool:
    t = (text or "").strip()
    chars = [c for c in t if not c.isspace()]
    return bool(chars) and (bool(SQL_KW.search(t) or PY_LINE.match(t) or ASSIGN_LINE.match(t))
                            or t.startswith(("#", "--", "//", "/*")) or len(HANGUL.findall(t)) / len(chars) < 0.3)


def _needles(regions: list, an: dict) -> list:
    """Every transcribed line of the analysis as a needle. Lines of a code / table / formula / [?] region locate it;
    every other line (title, prose, captions, footers) is context: it only claims its OCR line, so that line never
    grows into a crop."""
    import ocr_score  # shared
    by_part = {tuple(r["part"]): r for r in regions if r["kind"] in NEEDLE_KINDS}
    out = []
    for path, text, _ in ocr_score.corpus_parts(an):
        m = _PART_RE.match(path)
        reg = by_part.get((m.group(1), int(m.group(2)))) if m else None
        if reg and reg["kind"] == "table" and not m.group(3).startswith((".headers", ".rows")):
            reg = None  # a table caption is context, not a cell
        for line in str(text).splitlines():
            k = match_key(line)
            if k:
                out.append({"owner": reg["rid"] if reg else "ctx:" + path, "ctx": reg is None, "key": k,
                            "words": bool(reg) and reg["kind"] == "table"})
    return out


def _bind(needles: list, cands: list) -> tuple:
    """Each OCR line goes to at most one owner and, bound whole, to one needle (a table may bind several words of one
    line). Pass 1 binds needles whose best candidate is clear, best score first. Pass 2 binds the rest (identical
    lines, repeated cells): among equally good free candidates the one nearest to what the owner already holds,
    else the first in reading order, so identical blocks are assigned in reading order."""
    for n in needles:
        n["cands"] = sorted(((s, c) for c in cands if (n["words"] or not c["word"])
                             for s in [_sim(n["key"], c["key"])] if s >= BIND_MIN),
                            key=lambda sc: (-sc[0], sc[1]["word"], sc[1]["order"]))
    owner, whole, used, bound = {}, set(), set(), {}

    def free(n, c):
        li = c["line"]
        if c["word"]:
            return li not in whole and owner.get(li, n["owner"]) == n["owner"] and c["id"] not in used
        return li not in owner

    def take(i, c):
        bound[i] = c
        owner[c["line"]] = needles[i]["owner"]
        (used.add(c["id"]) if c["word"] else whole.add(c["line"]))

    clear = [i for i, n in enumerate(needles)
             if n["cands"] and (len(n["cands"]) == 1 or n["cands"][0][0] - n["cands"][1][0] >= TIE)]
    for i in sorted(clear, key=lambda i: (-needles[i]["cands"][0][0], needles[i]["ctx"], i)):
        c = needles[i]["cands"][0][1]
        if free(needles[i], c):
            take(i, c)
    for i in sorted(range(len(needles)), key=lambda i: (needles[i]["ctx"], i)):
        n = needles[i]
        if i in bound:
            continue
        opts = [(s, c) for s, c in n["cands"] if free(n, c)]
        if not opts:
            continue
        opts = [sc for sc in opts if sc[0] >= opts[0][0] - TIE]
        held = [c["lbox"] for j, c in bound.items() if needles[j]["owner"] == n["owner"]]
        if held:
            box = _union(held)
            opts.sort(key=lambda sc: (_dist(sc[1]["lbox"], box), sc[1]["word"], sc[1]["order"]))
        take(i, opts[0][1])
    return bound, owner


def _cluster(hits: list, foreign: list) -> list:
    """hits: [(needle index, line box, score)] -> vertical runs, split where the gap exceeds GROW_GAP line heights
    or another owner's line lies in between."""
    hits = sorted(hits, key=lambda h: (h[1][1], h[1][0]))
    h = _median_h([x[1] for x in hits])
    runs = [[hits[0]]]
    for x in hits[1:]:
        cur = _union([y[1] for y in runs[-1]])
        span = [min(cur[0], x[1][0]), cur[3], max(cur[2], x[1][2]), x[1][1]]
        between = any(span[1] < (b[1] + b[3]) / 2 < span[3] and b[0] < span[2] and b[2] > span[0] for b in foreign)
        if x[1][1] - cur[3] > GROW_GAP * h or between:
            runs.append([x])
        else:
            runs[-1].append(x)
    return runs


def _grow(box, kind, me, lines, owner, h):
    """Code / table crops take unclaimed OCR lines in the block's geometry (code: not outdented, starting inside the
    block's x-range, code-looking; table: inside its x-range), up to GROW_GAP line heights away, so a line the
    analyst skipped is still inside the crop. A line claimed by anything else is never pulled in."""
    tol = 0.5 * h
    changed = True
    while changed:
        changed = False
        for li, (lb, text) in enumerate(lines):
            if li in owner:
                continue
            if max(lb[1] - box[3], box[1] - lb[3], 0) > GROW_GAP * h:
                continue
            if kind == "code" and not (box[0] - tol <= lb[0] <= box[2] + 2 * h and _codeish(text)):
                continue
            if kind == "table" and not (lb[0] >= box[0] - tol and lb[2] <= box[2] + tol):
                continue
            nb = _union([box, lb])
            if any(o != me and _centre_in(lines[lj][0], nb) for lj, o in owner.items()):
                continue
            box, owner[li], changed = nb, me, True
    return box


def locate_all(regions: list, rec: dict, ref: dict) -> dict:
    """{rid: {"mode": crop | view | sheet, "box": [l, t, r, b] | None, "matched": share of the analyst's lines found}}
    for the regions of one slide. Needles are scored against whole OCR lines (words only for table cells), with
    operators kept; each line is bound to one needle; a region is cropped around its largest vertical run of bound
    lines and falls back to view mode when too few needles bind or two runs are equally good. Geometry only: the
    reader never sees the analyst's or the OCR's text."""
    out = {}
    view = {"mode": "view", "box": None, "matched": 0.0}
    by_id = {l.get("id"): l for l in (ref or {}).get("lines") or []}
    for r in regions:
        if r["kind"] == "line":
            ln = by_id.get(r["part"][1])
            out[r["rid"]] = {"mode": "sheet", "box": _xywh_to_ltrb(ln["box"]), "matched": 1.0} \
                if ln and ln.get("box") else dict(view)
        elif r["kind"] in VIEW_KINDS or not ref or not ref.get("lines") or not ref.get("size"):
            out[r["rid"]] = dict(view)
    todo = [r for r in regions if r["rid"] not in out]
    if not todo:
        return out
    lines = sorted(((_xywh_to_ltrb(l["box"]), l) for l in ref["lines"] if l.get("box")), key=lambda x: (x[0][1], x[0][0]))
    cands = []
    for li, (lb, ln) in enumerate(lines):
        cands.append({"id": f"{li}", "line": li, "word": False, "key": match_key(ln.get("text")), "lbox": lb, "order": len(cands)})
        for wi, w in enumerate(ln.get("words") or []):
            if w.get("box"):
                cands.append({"id": f"{li}.{wi}", "line": li, "word": True, "key": match_key(w.get("text")), "lbox": lb,
                              "order": len(cands)})
    cands = [c for c in cands if c["key"]]
    needles = _needles(regions, rec.get("analysis") or {})
    bound, owner = _bind(needles, cands)
    texts = [(lb, ln.get("text") or "") for lb, ln in lines]
    W, H = ref["size"]
    for r in sorted(todo, key=lambda r: (r["part"][0], r["part"][1])):
        mine = [i for i, n in enumerate(needles) if n["owner"] == r["rid"]]
        hits = [(i, bound[i]["lbox"], s) for i in mine if i in bound
                for s in [next(s for s, c in needles[i]["cands"] if c is bound[i])]]
        if not mine or not hits:
            out[r["rid"]] = dict(view)
            continue
        foreign = [lines[li][0] for li, o in owner.items() if o != r["rid"]]
        runs = sorted(_cluster(hits, foreign), key=lambda run: (-len({x[0] for x in run}), -sum(x[2] for x in run)))
        best = runs[0]
        matched = round(len({x[0] for x in best}) / len(mine), 2)
        ambiguous = len(runs) > 1 and len({x[0] for x in runs[1]}) == len({x[0] for x in best})
        if not ambiguous:
            keep = {bound[x[0]]["line"] for x in best}
            for x in [x for run in runs[1:] for x in run]:
                if bound[x[0]]["line"] not in keep:
                    owner.pop(bound[x[0]]["line"], None)  # a stray hit far from the block: release the line
        if ambiguous or matched < (0.4 if r["kind"] == "table" else 0.5):
            out[r["rid"]] = {"mode": "view", "box": None, "matched": matched}
            continue
        box = _union([x[1] for x in best])
        lh = _median_h([x[1] for x in best])
        if r["kind"] in ("code", "table"):
            box = _grow(box, r["kind"], r["rid"], texts, owner, lh)
        if r["kind"] in BLOCK_KINDS and rec.get("image"):
            box = block_box(rec["image"], box, lh)
        if (box[2] - box[0]) * (box[3] - box[1]) > MAX_CROP_AREA * W * H:
            out[r["rid"]] = {"mode": "view", "box": None, "matched": matched}
            continue
        out[r["rid"]] = {"mode": "crop", "box": box, "matched": matched}
    return out


BLOCK_KINDS = {"code", "table", "formula"}
BG_DIST = 60      # a block background this far from the slide background is a separate (e.g. dark) box
FILL_SHARE = 0.85  # a strip belongs to the block when this share of it has the block background colour
INK_GAP = 60       # px of background that ends a run of ink when extending a white block sideways


def _colour_dist(a, b):
    import numpy as np
    return np.sqrt(((a.astype("int32") - np.asarray(b, dtype="int32")) ** 2).sum(axis=-1))


def block_box(image_path, box, line_h: float = 30) -> list:
    """Widen an OCR-based crop box to the visual block it sits in, so a line the OCR did not see (a lone '})',
    the end of a long line) is never cut off:
      - a block with its own background (a dark code box): grow every side while the next strip is block background
      - a block on the slide background: grow sideways over contiguous ink and up/down to a border line within
        ~1.5 line heights; otherwise pad by most of a line height.
    Needs numpy; without it the box is padded by a line height on every side."""
    l, t, r, b = (int(v) for v in box)
    try:
        import numpy as np
        from PIL import Image
        with Image.open(image_path) as im:
            a = np.asarray(im.convert("RGB"))
    except Exception:
        pad = int(line_h)
        return [max(0, l - pad), max(0, t - pad), r + pad, b + pad]
    H, W = a.shape[:2]
    l, t, r, b = max(0, l), max(0, t), min(W - 1, r), min(H - 1, b)
    q = (a[::4, ::4] // 16).reshape(-1, 3)
    vals, counts = np.unique(q, axis=0, return_counts=True)
    slide_bg = vals[counts.argmax()] * 16 + 8
    inner = a[t:b + 1, l:r + 1].reshape(-1, 3)
    block_bg = np.median(inner, axis=0) if inner.size else slide_bg
    if _colour_dist(block_bg[None], slide_bg)[0] > BG_DIST:
        def is_bg(strip):
            return strip.size and (_colour_dist(strip, block_bg) < 40).mean() >= FILL_SHARE
        while t > 0 and is_bg(a[t - 1, l:r + 1]):
            t -= 1
        while b < H - 1 and is_bg(a[b + 1, l:r + 1]):
            b += 1
        while l > 0 and is_bg(a[t:b + 1, l - 1]):
            l -= 1
        while r < W - 1 and is_bg(a[t:b + 1, r + 1]):
            r += 1
        return [max(0, l - 4), max(0, t - 4), min(W - 1, r + 4), min(H - 1, b + 4)]
    ink = _colour_dist(a, slide_bg) > BG_DIST
    band = ink[t:b + 1]
    gap = 0
    x = r
    while x < W - 1 and gap < INK_GAP:  # the end of a long line the OCR clipped
        x += 1
        gap = 0 if band[:, x].any() else gap + 1
        if gap == 0:
            r = x
    gap, x = 0, l
    while x > 0 and gap < INK_GAP // 2:
        x -= 1
        gap = 0 if band[:, x].any() else gap + 1
        if gap == 0:
            l = x
    lim = int(1.5 * line_h) + 12
    new_t, new_b = max(0, t - int(0.8 * line_h)), min(H - 1, b + int(0.8 * line_h))
    for y in range(t - 1, max(-1, t - lim), -1):  # a border line above the block
        if ink[y, l:r + 1].mean() >= 0.7:
            new_t = y
            break
    for y in range(b + 1, min(H, b + lim)):
        if ink[y, l:r + 1].mean() >= 0.7:
            new_b = y
            break
    return [max(0, l - 6), max(0, new_t - 2), min(W - 1, r + 6), min(H - 1, new_b + 2)]


def locate(region: dict, rec: dict, ref: dict) -> dict:
    """locate_all for one region, located together with the slide's other structure regions."""
    regions = [r for r in select_regions(rec) if r["rid"] != region["rid"]] + [region]
    return locate_all(regions, rec, ref)[region["rid"]]


def apply_budget(regions: list, budget_tokens, cost_fn) -> tuple:
    """Keep regions in priority order while the estimated image-token cost fits; return (kept, skipped)."""
    if not budget_tokens:
        return regions, []
    kept, skipped, used = [], [], 0
    for r in sorted(regions, key=lambda r: (r["priority"], r["rid"])):
        c = cost_fn(r)
        if used + c <= budget_tokens:
            kept.append(r)
            used += c
        else:
            skipped.append(r)
    return kept, skipped

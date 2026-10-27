"""kp_compare.py - deterministic comparators for structural slide regions, plus a 2-of-3 majority vote.

The upstream scorer (textnorm.norm) strips punctuation and operators, so it cannot see 'age > 30' vs
'age < 30', swapped table cells, reversed ERD arrows or 3NF vs 2NF. This module compares an agy transcription
with blind Claude readings WITHOUT that normalisation. Pure functions, no I/O.

    KINDS = ("code", "table", "diagram", "formula", "line")
    canon(s) -> str                           NFKC, straight quotes, '-' for dashes, LF, no trailing spaces/blank edges
    code_tokens(s, language=None) -> list     operator-preserving tokens, '<IND:k>' indentation levels (Python/YAML)
    code_tokens_spans(s, language=None)       [(token, start, end)] with spans into canon(s)
    line_tokens(s) -> list                    case-folded, ignorable punctuation dropped, '[?]' = one wildcard
    math_tokens(s), latex_tokens(s)           plain-text and LaTeX formulas in one token space
    latex_consistent(value) -> bool|None      math_tokens(plain) == latex_tokens(latex)
    cell_key(cell) -> str                     table cell comparison key
    parse_relation(s) -> dict|None            '학생 -> 수강 : 1:N' -> {"from", "to", "label", "directed"}
    match_nodes(agy_nodes, read_nodes)        {read_name: agy_name}, one-to-one
    diagram_node_map(agy_val, read_val)       match_nodes over nodes + edge endpoints (what compare uses)
    edge_key(edge, node_map=None), cardinality(label)
    agy_value(kind, part) -> value            agy JSON part -> read shape
    units(kind, value, node_map=None)         {unit: (cmp_key, raw)}, the unitisation vote() works on
    compare(kind, agy_val, read_val)          {"verdict": agree|minor|mismatch|uncertain, "diffs": [...], ...}
    vote(agy_u, r1_u, r2_u=None)              {"status", "canonical", "disputed", "changed", "pending"}
    to_value(kind, canonical, template)       read-shape value rebuilt from vote()["canonical"]
    patch_line(agy_text, read_text) -> str|None   write a line correction back into the agy item text

Diffs describe the agy version against the read: "'>' -> '<'" = agy has '>' where the read has '<';
"missing X" = only the read has X (agy omitted it); "extra X" = only agy has X. At most 10 per compare.
vote() puts the (nrows, ncols, header_rows) tuple under canonical["shape"], since that unit has no raw.

Beyond the spec (each one closes a hole a reviewer found):
  * scripts: super/subscript characters (digits, signs and letters: ², ⁻, ᵀ, ⁿ, ₜ, ᵢ ...) become '^'/'_' plus
    their base before NFKC flattens them, in formulas AND in lines and table cells (10⁻³ != 10-3, x² != x₂).
  * Greek keeps its case: Σ/σ, Φ/φ, Δ/δ differ (formulas: 'Delta' vs 'delta'); Σ/∑ and Π/∏ are one symbol (sum, prod).
  * code: ',' never belongs to a number ('(3,4)' == '(3, 4)'); the minor (case-only) verdict is reserved for
    case-insensitive languages (SQL family, or unlabelled code that looks like SQL); 'python3'/'Python 3'/'ipython'
    count as Python; a '# comment' after the colon no longer hides a block; continuation lines (inside brackets)
    and a block with a single indentation level carry no '<IND>' token, so one code text tokenises the same
    whatever its language label.
  * formulas: no string literals (primes f'(x)); x⁻¹ == x^-1 == x^{-1} (a script holding one signed atom is
    never parenthesised); a plain that is really LaTeX goes through latex_tokens.
  * lines: Hangul spacing is ignored (기본 키 == 기본키), a read may start or end inside an agy Hangul word
    (verdict "minor": the crop is one wrapped visual line), '[?]' may stand for part of a word or for a whole
    visual word such as '3NF를'; bullets at the line start, middle-dot variants (ㆍ ・ ‧) are ignorable;
    → ⇒ ≥ ≤ ≠ equal -> => >= <= !=; brackets are compared by position when both sides have them or the line is
    an expression ('σ(Wx + b)' != 'σ(Wx) + b').
  * table cells: spacing next to Hangul or punctuation is ignored ('학번 (PK)' == '학번(PK)').
  * diagrams: nodes match only by the same name or an alias in brackets ('학생' == '학생(Student)'); a near-miss
    name (주문상태 vs 주문상세) is a mismatch "node ..."; the label text is part of the edge cmp_key, so a label
    difference is a mismatch and two reads can correct it; '1 : N' == '1:N' and M == N; '(1:N)' after a node, labelled
    arrows ('A -(수강)-> B', mermaid) and 'Section:1:N' parse; chained arrows ('A -> B -> C') are unparsed.
  * vote: a version whose unit holds '[?]' supports every value its readable part matches, never becomes canonical
    over a readable value; table cells of a version excluded by the shape vote are not kept as disputed raws;
    line units vote with compare("line")/patch_line semantics (a read is one line of a longer agy item).
  * patch_line refuses (None) when the location is ambiguous (another window explains the read almost as
    well), when a window-edge substitution could equally be an insertion, or when less than half the read matches.
"""
from __future__ import annotations

import re
import unicodedata
from collections import namedtuple
from difflib import SequenceMatcher

import kp_common  # noqa: F401  (puts the shared agy-slide-analyzer scripts on sys.path)
from textnorm import bigrams, nfkc, norm, overlap  # noqa: E402

KINDS = ("code", "table", "diagram", "formula", "line")
WILD = "[?]"
MAX_DIFFS = 10


# --------------------------------------------------------------------------- canonical text
_CANON = {**{c: "'" for c in (0x2018, 0x2019, 0x201A, 0x201B, 0x2032)},
          **{c: '"' for c in (0x201C, 0x201D, 0x201E, 0x2033)},
          **{c: "-" for c in (*range(0x2010, 0x2016), 0x2212)}}
# line and cell text only: arrows and comparison glyphs as their ASCII spelling, n-ary sum/product as the letter
_LINE_SYM = {ord("→"): "->", ord("⟶"): "->", ord("⇒"): "=>", ord("←"): "<-", ord("⟵"): "<-", ord("↔"): "<->",
             ord("⟷"): "<->", ord("⇔"): "<=>", ord("≥"): ">=", ord("≤"): "<=", ord("⩾"): ">=", ord("⩽"): "<=",
             ord("≠"): "!=", ord("∑"): "Σ", ord("∏"): "Π"}


def _canon_chars(s: str) -> str:
    # translate before NFKC as well: NFKC turns U+2033 into two primes
    return nfkc(s.translate(_CANON)).translate(_CANON)


def _trim(lines: list) -> list:
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return lines


def canon(s) -> str:
    if s is None:
        return ""
    t = _canon_chars(str(s)).replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(_trim([l.rstrip() for l in t.split("\n")]))


def _script_of(c: str):
    """('^', base) for a superscript character, ('_', base) for a subscript one, else None."""
    if len(c) != 1 or c in "ªº":
        return None
    d = unicodedata.decomposition(c).split()
    if len(d) == 2 and d[0] in ("<super>", "<sub>"):
        return ("^" if d[0] == "<super>" else "_"), _canon_chars(chr(int(d[1], 16)))
    return None


def _cluster_end(s: str, i: int) -> int:
    """End of the run starting at s[i] that NFKC must see whole (a base plus the marks/jamo it composes with)."""
    j = i + 1
    while j < len(s) and s[j] not in "\r\n" and nfkc(s[i:j + 1]) != nfkc(s[i:j]) + nfkc(s[j]):
        j += 1
    return j


def _canon_map(s, line: bool = False):
    """canon(s) built cluster by cluster -> (text, spans): spans[i] is the (start, end) in s of output char i.

    line=True also protects super/subscripts ('10⁻³' -> '10^-3 ') and applies _LINE_SYM (line and cell text)."""
    s = "" if s is None else str(s)
    lines, cur, i, sc = [], [], 0, None
    while i < len(s):
        if s[i] in "\r\n":
            lines.append(cur)
            cur, sc = [], None
            i += 2 if s[i:i + 2] == "\r\n" else 1
            continue
        j = _cluster_end(s, i)
        cl = s[i:j]
        if line:
            hit = _script_of(cl)
            if hit:
                out = hit[1] if sc == hit[0] else hit[0] + hit[1]
                sc = hit[0]
            else:  # a script run ends with a space, so 'wᵀx' reads w ^ T x
                out = (" " if sc else "") + _canon_chars(cl).translate(_LINE_SYM)
                sc = None
        else:
            out = _canon_chars(cl)
        cur += [(c, i, j) for c in out]
        i = j
    lines.append(cur)
    for l in lines:
        while l and l[-1][0].isspace():
            l.pop()
    text, spans = [], []
    for n, l in enumerate(_trim(lines)):
        if n:
            text.append("\n")
            spans.append((spans[-1][1],) * 2 if spans else (0, 0))
        text += [c for c, _, _ in l]
        spans += [(a, b) for _, a, b in l]
    return "".join(text), spans


_GREEK_RE = re.compile("[\u0370-\u03ff\u1f00-\u1fff]")


def _fold(s: str) -> str:
    """casefold() that keeps Greek case: Σ (sum) and σ (standard deviation) are different symbols."""
    if not _GREEK_RE.search(s):
        return s.casefold()
    return "".join(c if _GREEK_RE.match(c) else c.casefold() for c in s)


# --------------------------------------------------------------------------- tokens
_OPS = ("<<=", ">>=", ">=", "<=", "<>", "!=", "==", "=>", "->", "::", "||", "&&", "**", "//",
        ":=", "+=", "-=", "*=", "/=", "%=", "<<", ">>")
_STR = r"'(?:[^'\\\n]|\\.)*'" + "|" + r'"(?:[^"\\\n]|\\.)*"'
# Hangul syllables, compatibility jamo, and the conjoining jamo NFKC turns them into (minus U+119E, the NFKC form
# of the middle dot 'ㆍ', which is punctuation)
_HAN = "가-힣ㄱ-ㆎ\u1100-\u119d\u119f-\u11ff\ua960-\ua97c\ud7b0-\ud7fb"
_HAN_RE = re.compile(f"[{_HAN}]")
_TAIL = (r"|(?P<word>[A-Za-z0-9_]+)|(?P<han>[" + _HAN + r"]+)"
         r"|(?P<op>" + "|".join(map(re.escape, _OPS)) + r")|(?P<ch>\S)")
# digits in the lookahead too, so that backtracking cannot split '16px' into '1' + '6px'. ',' is an argument
# separator in code and formulas ('(3,4)' == '(3, 4)'); lines keep 1,024-style thousands groups as one number.
_NUM = r"|(?P<num>\d+(?:\.\d+)*(?![A-Za-z0-9_]))"
_LNUM = r"|(?P<num>(?:\d{1,3}(?:,\d{3})+(?!\d)|\d+)(?:\.\d+)*(?![A-Za-z0-9_]))"
_CODE_RE = re.compile(r"(?P<ws>\s+)|(?P<str>" + _STR + ")" + _NUM + _TAIL)
_MATH_RE = re.compile(r"(?P<ws>\s+)" + _NUM + _TAIL)
_LINE_RE = re.compile(r"(?P<ws>\s+)|(?P<wild>\[\?\])" + _LNUM + _TAIL)
_THOUS_RE = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)*")

_PY_LANGS = {"python", "py", "ipython", "cython", "jython", "pyspark", "micropython", "sage"}
_INDENT_LANGS = _PY_LANGS | {"yaml", "yml"}
_GENERIC = {"", "text", "txt", "plain", "plaintext", "none", "null", "unknown", "pseudo", "pseudocode"}


def _scan(text, start, end, rx, out):
    pos = start
    while pos < end:
        m = rx.match(text, pos, end)
        if m.lastgroup != "ws":
            out.append((m.group(), m.start(), m.end()))
        pos = m.end()
    return out


def _lang(language) -> str:
    """'Python 3' / 'python3' / 'python-3.10' -> 'python'."""
    l = str(language or "").strip().lower()
    return re.sub(r"[\s._-]*v?\d+(?:\.\d+)*$", "", l) or l


def _generic(language) -> bool:
    return _lang(language) in _GENERIC


def _code_body(line: str) -> str:
    """The line without string literals and without a trailing '#' comment (for the colon/bracket heuristics)."""
    return re.sub(_STR, "''", line).split("#", 1)[0].rstrip()


def _indent_on(text: str, language) -> bool:
    lang = _lang(language)
    if lang in _INDENT_LANGS:
        return True
    if lang not in _GENERIC:
        return False
    prev = None
    for line in text.split("\n"):
        if line.strip():
            if prev is not None and line[0].isspace() and _code_body(prev).endswith(":"):
                return True
            prev = line
    return False


def _code_spans(text: str, indent: bool) -> list:
    out = []
    if not indent:
        return _scan(text, 0, len(text), _CODE_RE, out)
    lines, off, depth, cont = [], 0, 0, False
    for line in text.split("\n"):
        if line.strip():
            lead = len(line) - len(line.lstrip())
            width = sum(4 if c == "\t" else 1 for c in line[:lead])
            # a line that continues an open bracket or a backslash has no meaningful indentation
            lines.append((off, lead, width, len(line), depth > 0 or cont))
            body = _code_body(line)
            depth = max(0, depth + sum(map(body.count, "([{")) - sum(map(body.count, ")]}")))
            cont = body.endswith("\\")
        off += len(line) + 1
    widths = sorted({w for _, _, w, _, c in lines if not c})
    rank = {w: k for k, w in enumerate(widths)}
    for off, lead, width, n, c in lines:
        if not c and len(widths) > 1:  # one level only: nothing to compare
            out.append((f"<IND:{rank[width]}>", off, off + lead))
        _scan(text, off + lead, off + n, _CODE_RE, out)
    return out


def code_tokens_spans(s, language=None) -> list:
    text = canon(s)
    return _code_spans(text, _indent_on(text, language))


def code_tokens(s, language=None) -> list:
    return [t for t, _, _ in code_tokens_spans(s, language)]


def _is_str(t: str) -> bool:
    return len(t) >= 2 and t[0] in "'\"" and t[-1] == t[0]


# --------------------------------------------------------------------------- diffs
def _q(toks) -> str:
    s = " ".join(toks)
    return "'" + (s if len(s) <= 60 else s[:57] + "...") + "'"


def _cap(diffs: list, n: int = MAX_DIFFS) -> list:
    return diffs if len(diffs) <= n else diffs[:n - 1] + [f"... {len(diffs) - n + 1} more"]


def _token_diffs(a, b, line_of=None, wild=False) -> list:
    """a = agy tokens, b = read tokens; line_of(i) numbers the agy line; wild skips pure-wildcard replaces."""
    out = []
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal" or (wild and tag == "replace" and i2 - i1 == j2 - j1
                              and all(t == WILD for t in b[j1:j2])):
            continue
        at = f"line {line_of(i1)}: " if line_of else ""
        if tag == "replace":
            out.append(f"{at}{_q(a[i1:i2])} -> {_q(b[j1:j2])}")
        elif tag == "delete":
            out.append(f"{at}extra {_q(a[i1:i2])}")
        else:
            out.append(f"{at}missing {_q(b[j1:j2])}")
    return _cap(out)


# --------------------------------------------------------------------------- code
_CI_LANGS = {"vb", "vba", "vbnet", "vb.net", "basic", "fortran", "pascal", "delphi", "ada", "cobol", "abap",
             "oracle", "hive", "ddl", "dml"}
_SQL_LOOK = re.compile(r"\b(?:select\b[\s\S]*\bfrom|insert\s+into|update\s+\S+\s+set|delete\s+from"
                       r"|create\s+(?:table|view|index|database|schema)|alter\s+table|drop\s+(?:table|view|index))\b",
                       re.I)


def _case_insensitive(language, text: str) -> bool:
    """SQL keywords and identifiers ignore case; Python's X/x or True/true do not."""
    lang = _lang(language)
    if "sql" in lang or lang in _CI_LANGS:
        return True
    return lang in _GENERIC and bool(_SQL_LOOK.search(text))


def _compare_code(agy, read) -> dict:
    # one language for both sides (agy's, else the read's), so both get the same indentation rule
    lang = read.get("language") if _generic(agy.get("language")) else agy.get("language")
    text, rtext = canon(agy.get("code")), canon(read.get("code"))
    spans = code_tokens_spans(text, lang)
    a, b = [t for t, _, _ in spans], code_tokens(rtext, lang)
    if a == b:
        return {"verdict": "agree", "diffs": []}

    def fold(ts):
        return [t if _is_str(t) else t.lower() for t in ts]

    def line_of(i):
        return text.count("\n", 0, spans[min(i, len(spans) - 1)][1]) + 1 if spans else 1

    minor = _case_insensitive(lang, text + "\n" + rtext) and fold(a) == fold(b)
    return {"verdict": "minor" if minor else "mismatch", "diffs": _token_diffs(a, b, line_of)}


# --------------------------------------------------------------------------- table
_WS_RE = re.compile(r"\s+")
_CELL_RE = re.compile(r"^r(\d+)c(\d+)$")
_DOTS = {c: "·" for c in (0x00B7, 0x119E, 0x318D, 0x30FB, 0xFF65, 0x2027, 0x2219, 0x22C5)}


def _disp(cell) -> str:
    # the cell as written (NFKC would show 'x²' and 'x₂' both as 'x2')
    return _WS_RE.sub(" ", "" if cell is None else str(cell)).strip()


def cell_key(cell) -> str:
    """canon + scripts kept ('x²' -> 'x^2'), whitespace collapsed; a space survives only between two ASCII letters
    or digits ('Kim Lee', '1 2'), so '학번 (PK)' == '학번(PK)' and '기본 키' == '기본키'; one middle dot; casefold
    except Greek."""
    t = _WS_RE.sub(" ", _canon_map("" if cell is None else str(cell), line=True)[0]).strip()
    t = re.sub(r" (?![A-Za-z0-9])|(?<![A-Za-z0-9]) ", "", t)
    return _fold(t.translate(_DOTS))


def _int(x) -> int:
    try:
        return int(x or 0)
    except (TypeError, ValueError):
        return 0


def _grid(value):
    """(raw rows, key rows, header_rows): ragged rows padded, trailing empty rows and columns trimmed."""
    v = value or {}
    rows = [list(r) if isinstance(r, (list, tuple)) else [r] for r in (v.get("rows") or [])]
    nc = max((len(r) for r in rows), default=0)
    rows = [r + [""] * (nc - len(r)) for r in rows]
    keys = [[cell_key(c) for c in r] for r in rows]
    while keys and not any(keys[-1]):
        keys.pop()
        rows.pop()
    while nc and not any(k[nc - 1] for k in keys):
        nc -= 1
    rows, keys = [r[:nc] for r in rows], [k[:nc] for k in keys]
    return rows, keys, max(0, min(_int(v.get("header_rows")), len(rows)))


def _compare_table(agy, read) -> dict:
    ra, ka, ha = _grid(agy)
    rr, kr, hr = _grid(read)
    sa, sr = (len(ka), len(ka[0]) if ka else 0), (len(kr), len(kr[0]) if kr else 0)
    diffs = []
    if sa != sr:
        diffs.append(f"shape {sa[0]}x{sa[1]} vs {sr[0]}x{sr[1]}")
    if ha != hr:
        diffs.append(f"header_rows {ha} vs {hr}")
    if diffs:
        return {"verdict": "mismatch", "diffs": diffs}
    bad, unc = [], []
    for r in range(sa[0]):
        for c in range(sa[1]):
            x, y = ka[r][c], kr[r][c]
            if WILD in x or WILD in y:
                unc.append([r, c])
            elif x != y:
                bad.append([r, c])
                diffs.append(f"({r},{c}): '{_disp(ra[r][c])}' vs '{_disp(rr[r][c])}'")
    verdict = "mismatch" if bad else "uncertain" if unc else "agree"
    out = {"verdict": verdict, "diffs": _cap(diffs)}
    if bad:
        out["mismatch_cells"] = bad
    if unc:
        out["uncertain_cells"] = unc
    return out


# --------------------------------------------------------------------------- diagram
# beyond the spec's list: '<-->' (else '<--' would win) and the double/long arrows ⟷ ⇔ ⟵ ⇐ ⇒
_ARROWS = sorted([("<-->", "both"), ("<->", "both"), ("↔", "both"), ("<=>", "both"), ("⟷", "both"), ("⇔", "both"),
                  ("<--", "left"), ("<-", "left"), ("←", "left"), ("⟵", "left"), ("⇐", "left"),
                  ("-->", "right"), ("->", "right"), ("=>", "right"), ("→", "right"), ("⟶", "right"),
                  ("⇒", "right"), (" -- ", "none"), (" — ", "none"), (" – ", "none")],
                 key=lambda a: -len(a[0]))
# labelled arrows: 'A -(수강)-> B', 'A -[수강]-> B', mermaid 'A -->|수강| B' and 'A -- 수강 --> B'
_LABELLED = [re.compile(r"^(?P<a>.+?)\s*-+\s*\((?P<l>[^()]+)\)\s*-*>\s*(?P<b>.+)$"),
             re.compile(r"^(?P<a>.+?)\s*-+\s*\[(?P<l>[^\[\]]+)\]\s*-*>\s*(?P<b>.+)$"),
             re.compile(r"^(?P<a>.+?)\s*-{1,2}>\s*\|(?P<l>[^|]*)\|\s*(?P<b>.+)$"),
             re.compile(r"^(?P<a>.+?)\s+--\s+(?P<l>.+?)\s+-->\s+(?P<b>.+)$")]
_CARD_RE = re.compile(r"\d+\s*\.\.\s*(?:\d+|\*|[nNmM])|(?:\d+|[nNmM])\s*:\s*(?:\d+|[nNmM])"
                      r"|(?<![A-Za-z0-9])[01nNmM*](?![A-Za-z0-9])")
# a ':' inside a cardinality such as '1:N' (not glued to a word on either side, so 'Section:1:N' splits after Section)
_COLON_CARD = re.compile(r"(?<![A-Za-z0-9가-힣])(?:\d+|[nNmM*])\s*:\s*(?:\d+|[nNmM*])(?![A-Za-z0-9가-힣])")
_END_CARD = re.compile(r"^(?P<n>.*?)\s*[(\[](?P<c>\s*(?:\d+\s*(?:\.\.|:)\s*(?:\d+|\*|[nNmM])"
                       r"|[nNmM]\s*:\s*(?:\d+|[nNmM])|[01nNmM*])\s*)[)\]]$")


def _split_label(rest: str):
    if " : " in rest:
        node, label = rest.split(" : ", 1)
    else:
        inner = {k for m in _COLON_CARD.finditer(rest) for k in range(m.start(), m.end()) if rest[k] == ":"}
        cut = next((k for k, ch in enumerate(rest) if ch == ":" and k not in inner), None)
        node, label = (rest, None) if cut is None else (rest[:cut], rest[cut + 1:])
    return node.strip(), (label or "").strip() or None


def _has_arrow(s: str) -> bool:
    return any(tok in s for tok, _ in _ARROWS)


def _end_card(name: str):
    """'수강 (1:N)' -> ('수강', '1:N'); '학생(Student)' stays a name."""
    m = _END_CARD.match(name)
    if m and m.group("n").strip():
        return m.group("n").strip(), re.sub(r"\s+", "", m.group("c"))
    return name.strip(), None


def _edge(left: str, right: str, label, kind: str):
    left, lc = _end_card(left)
    right, rc = _end_card(right)
    if not left or not right:
        return None
    if kind == "left":
        left, right, lc, rc = right, left, rc, lc
    simple = re.compile(r"^(?:\d+|[nNmM*])$")
    card = f"{lc}:{rc}" if lc and rc and simple.match(lc) and simple.match(rc) else " ".join(x for x in (lc, rc) if x)
    label = " ".join(x for x in (card, label) if x) or None
    return {"from": left, "to": right, "label": label, "directed": kind in ("left", "right")}


def parse_relation(s):
    """First arrow from the left (longest token at a position wins); None when there is none, a node is empty or
    the right side holds another arrow (a chain 'A -> B -> C' is left unparsed)."""
    if not isinstance(s, str):
        return None
    t = nfkc(s).strip()
    for rx in _LABELLED:
        m = rx.match(t)
        if m:
            a, b = m.group("a").strip(), m.group("b").strip()
            if _has_arrow(a) or _has_arrow(b):
                return None
            b, extra = _split_label(b)
            return _edge(a, b, " ".join(x for x in (m.group("l").strip(), extra) if x) or None, "right")
    hit = next(((i, tok, kind) for i in range(len(t)) for tok, kind in _ARROWS if t.startswith(tok, i)), None)
    if hit is None:
        return None
    i, tok, kind = hit
    right, label = _split_label(t[i + len(tok):])
    if _has_arrow(right):
        return None
    return _edge(t[:i].strip(), right, label, kind)


def _card_tokens(label) -> list:
    s = nfkc("" if label is None else str(label))
    return [re.sub(r"\s+", "", m).upper() for m in _CARD_RE.findall(s)]


def cardinality(label) -> tuple:
    """'1:N' -> ('1:N',); '1 : n' and '1:M' -> ('1:N',) as well (M and N both mean many); None -> ()."""
    return tuple(t.replace("M", "N") for t in _card_tokens(label))


def _label_text(label) -> str:
    """The label without its cardinalities, under norm(): '1:N 신청' -> '신청'."""
    return "" if label is None else norm(_CARD_RE.sub(" ", nfkc(str(label))))


_PAREN_RE = re.compile(r"[(\[{]([^()\[\]{}]*)[)\]}]")


def _aliases(name: str):
    """'학생(Student)' -> ('학생', {'학생', 'student'})."""
    s = nfkc(name)
    outer = norm(_PAREN_RE.sub(" ", s))
    return outer, {x for x in [outer, *(norm(p) for p in _PAREN_RE.findall(s))] if x}


def _node_score(a: str, b: str) -> float:
    """1.0 same name under norm(); 0.95 one name is the other's name or bracketed alias; else no match."""
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    pa, sa = _aliases(a)
    pb, sb = _aliases(b)
    return 0.95 if (pa and pa in sb) or (pb and pb in sa) else 0.0


def _near(a: str, b: str) -> float:
    """Similar but different names (주문상태 / 주문상세, 부서 / 부서장): the spec's containment and bigram rules."""
    na, nb = norm(a), norm(b)
    if not na or not nb or na == nb:
        return 0.0
    short, long_ = sorted((na, nb), key=len)
    if len(short) >= 2 and short in long_:
        return 0.9
    s = min(overlap(na, bigrams(nb)), overlap(nb, bigrams(na)))
    return s if s >= 0.6 else 0.0


def _uniq(names) -> list:
    out = []
    for n in names or []:
        n = "" if n is None else str(n).strip()
        if n and n not in out:
            out.append(n)
    return out


def match_nodes(agy_nodes, read_nodes) -> dict:
    """One-to-one, greedy by best score. Only the same entity is matched; a near-miss name stays unmatched so that
    compare reports it and vote can correct it."""
    a, r = _uniq(agy_nodes), _uniq(read_nodes)
    pairs = sorted((-s, i, j) for j, rn in enumerate(r) for i, an in enumerate(a) if (s := _node_score(an, rn)))
    got, used = {}, set()
    for _, i, j in pairs:
        if j not in got and i not in used:
            got[j] = i
            used.add(i)
    return {rn: a[got[j]] if j in got else rn for j, rn in enumerate(r)}


def _endpoints(value) -> list:
    v = value or {}
    out = list(v.get("nodes") or [])
    for e in v.get("edges") or []:
        if isinstance(e, dict):
            out += [e.get("from"), e.get("to")]
    return out


def diagram_node_map(agy_val, read_val) -> dict:
    return match_nodes(_endpoints(agy_val), _endpoints(read_val))


def _name(n, node_map=None) -> str:
    n = "" if n is None else str(n).strip()
    return (node_map or {}).get(n, n)


def edge_key(edge, node_map=None) -> str:
    a, b = _name(edge.get("from"), node_map), _name(edge.get("to"), node_map)
    return f"{a}->{b}" if edge.get("directed", True) else "--".join(sorted((a, b)))


def _diagram_units(value, node_map=None) -> dict:
    """Raw edges carry the mapped (agy) node names; a repeated key gets '#2', '#3', ...
    cmp_key = (cardinality(label), label text without cardinalities under norm())."""
    u = {}
    for e in (value or {}).get("edges") or []:
        if not isinstance(e, dict):
            continue
        raw = dict(e)
        raw["from"], raw["to"] = _name(e.get("from"), node_map), _name(e.get("to"), node_map)
        key = base = edge_key(raw)
        n = 1
        while key in u:
            n += 1
            key = f"{base}#{n}"
        u[key] = ((cardinality(e.get("label")), _label_text(e.get("label"))), raw)
    return u


def _card_s(label) -> str:
    return " ".join(_card_tokens(label)) or "none"


def _lab(e) -> str:
    return "" if e.get("label") is None else str(e.get("label"))


def _compare_diagram(agy, read) -> dict:
    nm = diagram_node_map(agy, read)
    au, ru = _diagram_units(agy), _diagram_units(read, nm)
    diffs, bad, unc, used = [], False, [], set()
    for k, ((card, text), e) in au.items():
        if k in ru:
            used.add(k)
            (rcard, rtext), re_ = ru[k]
            if WILD in _lab(e) or WILD in _lab(re_):
                unc.append(k)
            elif card != rcard:
                bad = True
                diffs.append(f"label {k}: {_card_s(e.get('label'))} vs {_card_s(re_.get('label'))}")
            elif text != rtext:  # FK vs PK, 신청 vs 취소: a different relationship
                bad = True
                diffs.append(f"label {k}: '{_lab(e)}' vs '{_lab(re_)}'")
            continue
        bad = True
        a, b = e["from"], e["to"]
        if e.get("directed", True):
            alts = [(f"{b}->{a}", "reversed"), ("--".join(sorted((a, b))), "direction")]
        else:
            alts = [(f"{a}->{b}", "direction"), (f"{b}->{a}", "direction")]
        hit = next(((x, why) for x, why in alts if x in ru and x not in au and x not in used), None)
        if hit:
            used.add(hit[0])
            diffs.append(f"{hit[1]}: {k} vs {hit[0]}")
        else:
            diffs.append(f"extra edge {k}")
    for k in ru:
        if k not in au and k not in used:
            bad = True
            diffs.append(f"missing edge {k}")
    # near-miss node names that match_nodes refused to merge
    an, rn = _uniq(_endpoints(agy)), _uniq(_endpoints(read))
    free_a = [x for x in an if x not in set(nm.values())]
    free_r = [x for x in rn if nm.get(x, x) == x and x not in an]
    near, ua, ur = sorted((-s, i, j) for i, x in enumerate(free_a) for j, y in enumerate(free_r)
                          if (s := _near(x, y))), set(), set()
    node_diffs = []
    for _, i, j in near:
        if i not in ua and j not in ur:
            ua.add(i)
            ur.add(j)
            node_diffs.append(f"node '{free_a[i]}' vs '{free_r[j]}'")
    if node_diffs:
        bad = True
    out = {"verdict": "mismatch" if bad else "uncertain" if unc else "agree", "diffs": _cap(node_diffs + diffs),
           "node_map": nm}
    if unc:
        out["uncertain_edges"] = unc
    unparsed = list((agy or {}).get("unparsed") or [])
    if unparsed:
        out["unparsed"] = unparsed
        if not bad:
            out["verdict"] = "uncertain"
    return out


# --------------------------------------------------------------------------- formula
_GREEK = ("alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron pi rho "
          "sigma tau upsilon phi chi psi omega").split()
_GREEK_SET = set(_GREEK)
_CAPITAL = {"sigma": "sum", "pi": "prod"}  # Σ and Π are the n-ary operators ∑ ∏ on slides


def _greek_name(g: str, upper: bool) -> str:
    return _CAPITAL.get(g, g.capitalize()) if upper else g


# U+03A2 is unassigned and U+03C2 is final sigma, so letters from sigma on sit one code point higher
_MATH = {**{0x03B1 + k + (k >= 17): f" {g} " for k, g in enumerate(_GREEK)},
         **{0x0391 + k + (k >= 17): f" {_greek_name(g, True)} " for k, g in enumerate(_GREEK)},
         0x03C2: " sigma ", **{ord(c): "*" for c in "×·∙⋅"}, ord("÷"): "/", ord("≤"): "<=", ord("≥"): ">=",
         ord("⩽"): "<=", ord("⩾"): ">=", ord("≠"): "!=", ord("∑"): " sum ", ord("∏"): " prod ", ord("√"): " sqrt ",
         ord("∫"): " int "}
_LATEXY = re.compile(r"\\[A-Za-z]+|[\^_]\s*\{")
_ATOM_RE = re.compile(r"^(?:[A-Za-z0-9_.]+|[^\x00-\x7f]+)$")


def _math_scripts(s: str) -> str:
    """'x⁻¹' -> 'x ^(-1) ', 'hₜ₋₁' -> 'h _(t-1) ', 'wᵀx' -> 'w ^(T) x' (before NFKC flattens them)."""
    out, i = [], 0
    while i < len(s):
        hit = _script_of(s[i])
        if not hit:
            out.append(s[i])
            i += 1
            continue
        run = ""
        while i < len(s) and (h := _script_of(s[i])) and h[0] == hit[0]:
            run += h[1]
            i += 1
        out.append(f" {hit[0]}({run}) ")
    return "".join(out)


def _split_us(toks) -> list:
    """'x_1' -> x _ 1, so plain subscripts meet LaTeX's x_{1}."""
    out = []
    for t in toks:
        out += [p for p in re.split(r"(_)", t) if p] if "_" in t and len(t) > 1 and not _is_str(t) else [t]
    return out


def _unwrap(t: list) -> list:
    """'^ ( 2 )' -> '^ 2' and '^ ( - 1 )' -> '^ - 1': a script holding one (signed) atom needs no group."""
    out, i = [], 0
    while i < len(t):
        if t[i] in ("^", "_") and i + 1 < len(t) and t[i + 1] == "(":
            j = i + 2
            sign = [t[j]] if j < len(t) and t[j] in ("+", "-") else []
            j += len(sign)
            if j + 1 < len(t) and t[j + 1] == ")" and _ATOM_RE.match(t[j]):
                out += [t[i]] + sign + [t[j]]
                i = j + 2
                continue
        out.append(t[i])
        i += 1
    return out


def _plain_math(s) -> list:
    s = "" if s is None else str(s)
    t = canon(_math_scripts(s.replace("″", "''").replace("‴", "'''"))).translate(_MATH)
    return _split_us([tok for tok, _, _ in _scan(t, 0, len(t), _MATH_RE, [])])


def math_tokens(s) -> list:
    # a 'plain' that is really LaTeX (agy often writes \frac or x^{2} there) is read as LaTeX
    if s is not None and _LATEXY.search(str(s)):
        return latex_tokens(s)
    return _unwrap(_plain_math(s))


_LX_SKIP = {",", ";", "!", ":", ">", " ", "\\", "quad", "qquad", "displaystyle", "textstyle", "limits", "nolimits"}
_LX_DELIM = {"left", "right", "middle", "big", "Big", "bigg", "Bigg", "bigl", "bigr", "Bigl", "Bigr",
             "biggl", "biggr", "Biggl", "Biggr"}
_LX_STYLE = {"mathrm", "mathbf", "mathit", "mathsf", "mathtt", "mathcal", "mathbb", "mathfrak", "boldsymbol",
             "bm", "text", "textrm", "textit", "textbf", "mbox", "operatorname"}
_LX_MAP = {"cdot": ["*"], "times": ["*"], "ast": ["*"], "div": ["/"], "le": ["<="], "leq": ["<="],
           "ge": [">="], "geq": [">="], "ne": ["!="], "neq": ["!="], "sum": ["sum"], "prod": ["prod"],
           "int": ["int"], "infty": ["∞"], "pm": ["±"], "in": ["∈"], "approx": ["≈"], "to": ["->"],
           "rightarrow": ["->"], "ldots": [".", ".", "."], "cdots": [".", ".", "."], "dots": [".", ".", "."],
           "prime": ["'"], "{": ["{"], "}": ["}"], "%": ["%"], "_": ["_"], "&": ["&"], "#": ["#"], "$": ["$"]}


def _skip_ws(t: str, i: int) -> int:
    while i < len(t) and t[i].isspace():
        i += 1
    return i


def _lx_arg(t: str, i: int):
    i = _skip_ws(t, i)
    if i >= len(t):
        return [], i
    if t[i] == "{":
        return _lx_seq(t, i + 1, True)
    if t[i] == "\\":
        return _lx_cmd(t, i)
    return _plain_math(t[i]), i + 1


def _lx_cmd(t: str, i: int):
    j = i + 1
    while j < len(t) and t[j].isascii() and t[j].isalpha():
        j += 1
    name, k = (t[i + 1:j], j) if j > i + 1 else (t[i + 1:i + 2], i + 2)
    if not name or name in _LX_SKIP:
        return [], k
    if name in _LX_DELIM:  # \left( keeps the '(', \left. drops the null delimiter
        k = _skip_ws(t, k)
        return [], k + 1 if t[k:k + 1] == "." else k
    if name in _LX_MAP:
        return list(_LX_MAP[name]), k
    g = name[3:] if name.startswith("var") else name
    if g.lower() in _GREEK_SET and (g.islower() or g[:1].isupper() and g[1:].islower()):
        return [_greek_name(g.lower(), g[0].isupper())], k
    if name in ("frac", "dfrac", "tfrac"):
        a, k = _lx_arg(t, k)
        b, k = _lx_arg(t, k)
        return ["("] + a + [")", "/", "("] + b + [")"], k
    if name == "sqrt":
        k, root = _skip_ws(t, k), []
        if t[k:k + 1] == "[" and "]" in t[k:]:
            e = t.index("]", k)
            root, k = ["["] + _plain_math(t[k + 1:e]) + ["]"], e + 1
        x, k = _lx_arg(t, k)
        return ["sqrt"] + root + ["("] + x + [")"], k
    if name in _LX_STYLE:
        return _lx_arg(t, k)
    return [name], k


def _lx_seq(t: str, i: int, close: bool = False):
    """Tokens up to the matching '}' (close) or the end -> (tokens, next index)."""
    out, buf = [], []
    while i < len(t):
        c = t[i]
        if c in "{}\\^_":
            out += _plain_math("".join(buf))
            buf = []
        if c == "}":
            i += 1
            if close:
                return out, i
        elif c == "{":
            toks, i = _lx_seq(t, i + 1, True)
            out += toks
        elif c == "\\":
            toks, i = _lx_cmd(t, i)
            out += toks
        elif c in "^_":
            arg, i = _lx_arg(t, i + 1)
            out += [c] + (["("] + arg + [")"] if len(arg) > 1 else arg)
        else:
            buf.append(c)
            i += 1
    return out + _plain_math("".join(buf)), i


def latex_tokens(s) -> list:
    t = "" if s is None else str(s)
    return _unwrap(_lx_seq(canon(_math_scripts(t)), 0)[0])


def latex_consistent(value):
    v = value or {}
    if not v.get("latex"):
        return None
    return math_tokens(v.get("plain")) == latex_tokens(v.get("latex"))


def _compare_formula(agy, read) -> dict:
    a, b = math_tokens(agy.get("plain")), math_tokens(read.get("plain"))
    out = {"verdict": "agree", "diffs": []} if a == b else {"verdict": "mismatch", "diffs": _token_diffs(a, b)}
    lc = latex_consistent(read)
    if lc is not None:
        out["latex_consistent"] = lc
    return out


# --------------------------------------------------------------------------- line
_IGNORABLE = frozenset(". , ; : ! ? · • … ( ) [ ] { } \" ' 「 」 『 』".split()) | frozenset(
    "ᆞ ㆍ ・ ‧ ∙ ⋅ 、 。 ‥ 〈 〉 《 》 【 】".split())
# glyph bullets are dropped at the start of a line; '-', '*', '->', '=>' only when a space follows
_BULLETS = frozenset("▶ ▷ ► ▸ ▹ ■ □ ▪ ▫ ● ○ ◆ ◇ ◎ ◉ ★ ☆ ✓ ✔ ※ ❖ ➢ ➤ ➔ ➜ ⁃ ‣ ◦ ❍ ▣ ◈ ◼ ◻ ⦁".split())
_ABULLETS = frozenset(["-", "*", "->", "=>"])
_MATH_OPS = frozenset("= + * / ^ < > <= >= != == <> ** //".split())
_MK = "\x1d\x1e\x1f"  # Hangul-Hangul break / break with whitespace / break without whitespace

_Tok = namedtuple("_Tok", "text ca cb oa ob kind")  # canonical span, original span, kind: han|word|wild|brk


def _lt(s):
    """Line tokens of s -> (canonical text, spans, [_Tok]): brackets kept as kind 'brk', ignorables and leading
    bullets dropped, everything case-folded (not Greek), 1,024 -> 1024."""
    text, spans = _canon_map(s, line=True)
    out, line_at, start = [], -1, True
    for t, a, b in _scan(text, 0, len(text), _LINE_RE, []):
        ls = text.rfind("\n", 0, a)
        if ls != line_at:
            line_at, start = ls, True
        if t == WILD:
            kind = "wild"
        elif t in ("(", "[", "{", ")", "]", "}"):
            kind, t = "brk", "(" if t in "([{" else ")"
        elif t in _IGNORABLE:
            continue
        elif start and (t in _BULLETS or (t in _ABULLETS and (b >= len(text) or text[b].isspace()))):
            continue
        else:
            kind = "han" if _HAN_RE.match(t) else "word"
            if _THOUS_RE.fullmatch(t):
                t = t.replace(",", "")
            t = _fold(t)
        if kind != "brk":
            start = False
        oa, ob = (spans[a][0], spans[b - 1][1]) if spans else (a, b)
        out.append(_Tok(t, a, b, oa, ob, kind))
    return text, spans, out


def _words(toks) -> list:
    return [t for t in toks if t.kind != "brk"]


def line_tokens(s) -> list:
    return [t.text for t in _words(_lt(s)[2])]


def _find(a: list, r: list) -> int:
    for i in range(len(a) - len(r) + 1):
        if all(x == WILD or x == a[i + j] for j, x in enumerate(r)):
            return i
    return -1


def _encode(aw):
    """Agy words as one string: Hangul words joined by \\x1d, others by \\x1e (space) / \\x1f (no space)."""
    out, owner = [], []
    for i, t in enumerate(aw):
        if i:
            p = aw[i - 1]
            out.append("\x1d" if p.kind == t.kind == "han" else "\x1e" if t.ca > p.cb else "\x1f")
            owner.append(-1)
        out += list(t.text)
        owner += [i] * len(t.text)
    return "".join(out), owner


def _pattern(rw) -> str:
    """Regex for the read words: Hangul spacing optional, '[?]' = part of one agy visual word."""
    parts = []
    for i, t in enumerate(rw):
        if i:
            p = rw[i - 1]
            parts.append(f"[{_MK}]?" if "wild" in (p.kind, t.kind) else "\x1d?" if p.kind == t.kind == "han"
                         else "[\x1e\x1f]")
        if t.kind == "wild":
            parts.append("[^\x1d\x1e]+?")
        elif t.kind == "han":
            parts.append("\x1d?".join(map(re.escape, t.text)))
        else:
            parts.append(re.escape(t.text))
    return "".join(parts)


def _line_match(aw, rw):
    """Read inside the agy item at the character level -> (s, e, partial edges, compact c0, c1) or None.

    Strict first (the read starts and ends on agy word boundaries), then lenient: a Hangul word at the read's
    edge may be a fragment of an agy word (a visual line can wrap inside a word: 만족 / 한다)."""
    if not aw or not rw:
        return None
    E, owner = _encode(aw)
    body = _pattern(rw)
    pre, post = f"(?<![^{_MK}])", f"(?![^{_MK}])"

    def cut_before(pos):  # agy characters of the same word left of pos
        k = pos
        while k > 0 and E[k - 1] not in _MK:
            k -= 1
        return pos - k

    m = re.search(pre + body + post, E)
    if not m:
        p0 = pre if rw[0].kind != "han" else ""
        p1 = post if rw[-1].kind != "han" else ""
        rx, pos = re.compile(p0 + body + p1), 0
        # a read may not start right after ONE syllable of an agy word: 비/불/무/미/반/역 + 정규화 is a different word
        while (m := rx.search(E, pos)) and cut_before(m.start()) == 1:
            pos = m.start() + 1
    if not m:
        return None
    s, e = m.start(), m.end()
    partial = (["start"] if cut_before(s) else []) + (["end"] if e < len(E) and E[e] not in _MK else [])

    def compact(pos):
        return pos - sum(1 for c in E[:pos] if c in _MK)

    return owner[s], owner[e - 1] + 1, partial, compact(s), compact(e)


def _brackets(toks):
    """[(offset, bracket)] where offset = word characters before the bracket, and the total word length."""
    out, n = [], 0
    for t in toks:
        if t.kind == "brk":
            out.append((n, t.text))
        else:
            n += len(t.text)
    return out, n


def _bracket_diff(at, aw, s, e, rt, rw, c0, c1):
    """Grouping check on a matched window: brackets must sit at the same places when both sides have brackets
    there, or always in an expression line (an operator such as = + * / ^ < > in it)."""
    ab = [(o - c0, b) for o, b in _brackets(at)[0] if c0 <= o <= c1]
    rb, n = _brackets(rt)
    math = any(t.text in _MATH_OPS for t in aw[s:e] + rw)
    if not (ab and rb) and not (math and (ab or rb)):
        return None
    da, dr = {}, {}
    for d, xs in ((da, ab), (dr, rb)):
        for o, b in xs:
            d[o] = d.get(o, "") + b
    for o in sorted(set(da) | set(dr)):
        x, y = da.get(o, ""), dr.get(o, "")
        if x != y and not ((o == 0 or o == n) and not (x and y)):  # a bracket at the crop edge may be cut off

            def show(d):
                w = "".join(t.text for t in rw)
                return "".join(d.get(k, "") + (w[k] if k < len(w) else "") for k in range(len(w) + 1))

            return f"brackets: '{show(da)}' vs '{show(dr)}'"
    return None


def _line_fit(at, rt):
    """Is the read one line of the agy item? -> {"verdict", "window", "partial", "brackets"} or None."""
    aw, rw = _words(at), _words(rt)
    a, r = [t.text for t in aw], [t.text for t in rw]
    if not r:
        return None
    i = _find(a, r)
    if i >= 0:
        s, e, partial = i, i + len(r), []
        c0, c1 = sum(len(x) for x in a[:s]), sum(len(x) for x in a[:e])
    else:
        m = _line_match(aw, rw)
        if not m:
            return None
        s, e, partial, c0, c1 = m
    bd = None if WILD in r else _bracket_diff(at, aw, s, e, rt, rw, c0, c1)
    verdict = "uncertain" if WILD in r else "minor" if partial else "agree"
    return {"verdict": verdict, "window": [s, e], "partial": partial, "brackets": bd}


def _lev(a: list, r: list) -> int:
    """Token edit distance; a wildcard in r costs nothing."""
    prev = list(range(len(r) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(r, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y and y != WILD)))
        prev = cur
    return prev[-1]


def _tclass(t: str) -> str:
    if re.fullmatch(r"[\d.,]+", t):
        return "num"
    if re.fullmatch(r"[A-Za-z0-9_]+", t):
        return "word"
    return "han" if _HAN_RE.match(t) else "sym"


def _similar(a: list, b: list) -> bool:
    """Could b be a misreading of a (rather than other text next to it)? 3NF/2NF, >/<, 30/45, 를/을는 no."""
    if len(a) == len(b) == 1 and _tclass(a[0]) == _tclass(b[0]) != "han":
        return True
    return SequenceMatcher(None, "".join(a), "".join(b), autojunk=False).ratio() >= 0.5


def _best_window(a: list, r: list):
    """Window of a (length len(r)-2 .. len(r)+2) that best explains r -> (start, end).

    Ranked by matched tokens, then edit distance, then edge replaces of dissimilar tokens (which are really an
    insertion next to an agy token outside the read: '은' -> '반드시' is "missing '반드시'"), then length closest
    to len(r), then difflib ratio: ratio alone prefers [를, 만족한다] over [3NF, 를, 만족한다] for '2NF를 만족한다'."""
    n, m = len(a), len(r)
    best = None
    for size in sorted({max(1, min(n, s)) for s in range(m - 2, m + 3)}):
        for i in range(n - size + 1):
            w = a[i:i + size]
            sm = SequenceMatcher(None, w, r, autojunk=False)
            matched = sum(b.size for b in sm.get_matching_blocks())
            if best is not None and -matched > best[0][0]:
                continue  # fewer matches than the best so far: skip the edit distance
            dis = sum(1 for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag == "replace"
                      and ((i1 == 0 and i > 0) or (i2 == size and i + size < n)) and not _similar(w[i1:i2], r[j1:j2]))
            key = (-matched, _lev(w, r), dis, abs(size - m), -sm.ratio(), i)
            if best is None or key < best[0]:
                best = (key, i, i + size)
    return (best[1], best[2]) if best else (0, 0)


def _partial_diffs(aw, rw, fit) -> list:
    s, e = fit["window"]
    out = []
    if "start" in fit["partial"]:
        out.append(f"line starts inside a word: '{aw[s].text}' vs '{rw[0].text}'")
    if "end" in fit["partial"]:
        out.append(f"line ends inside a word: '{aw[e - 1].text}' vs '{rw[-1].text}'")
    return out


def _compare_line(agy, read) -> dict:
    at, rt = _lt(agy.get("text"))[2], _lt(read.get("text"))[2]
    aw, rw = _words(at), _words(rt)
    a, r = [t.text for t in aw], [t.text for t in rw]
    if not r:
        return {"verdict": "uncertain", "diffs": ["read has no comparable tokens"]} if a \
            else {"verdict": "agree", "diffs": []}
    fit = _line_fit(at, rt)
    if fit and fit["brackets"]:
        out = {"verdict": "mismatch", "diffs": [fit["brackets"]], "window": fit["window"]}
    elif fit:
        out = {"verdict": fit["verdict"], "diffs": _partial_diffs(aw, rw, fit), "window": fit["window"]}
        if fit["partial"]:
            out["partial_edges"] = fit["partial"]
    else:
        s, e = _best_window(a, r)
        diffs = _token_diffs(a[s:e], r, wild=True)
        out = {"verdict": "mismatch" if diffs else "uncertain", "diffs": diffs, "window": [s, e]}
    if WILD in r:
        out["wildcards"] = r.count(WILD)
    return out


def _atoms(lt) -> list:
    """(atom, start, end in the original text): Hangul words split into syllables, so spacing never matters."""
    text, spans, toks = lt
    out = []
    for t in _words(toks):
        if t.kind == "han":
            out += [(ch, *spans[t.ca + k]) for k, ch in enumerate(t.text)]
        else:
            out.append((t.text, t.oa, t.ob))
    return out


def _atom_texts(s: str) -> list:
    return [x for x, _, _ in _atoms(_lt(s))]


def _semi_global(a: list, r: list):
    """Min edit distance of r against any substring of a -> (cost, [(start, end) of each optimal alignment])."""
    prev = [(0, -j) for j in range(len(a) + 1)]  # (cost, -start): ties keep the tightest alignment
    for i, x in enumerate(r, 1):
        cur = [(i, 0)]
        for j in range(1, len(a) + 1):
            d, u, l = prev[j - 1], prev[j], cur[j - 1]
            cur.append(min((d[0] + (0 if x == WILD or x == a[j - 1] else 1), d[1]), (u[0] + 1, u[1]), (l[0] + 1, l[1])))
        prev = cur
    best = min(c for c, _ in prev)
    return best, [(-st, j) for j, (c, st) in enumerate(prev) if c == best]


def patch_line(agy_text, read_text):
    agy_text = "" if agy_text is None else str(agy_text)
    read_text = "" if read_text is None else str(read_text)
    A, R = _lt(agy_text), _lt(read_text)
    if not _words(R[2]):
        return None if _words(A[2]) else agy_text
    fit = _line_fit(A[2], R[2])
    if fit:  # the read already fits (spacing, a wrapped word, wildcards): nothing to write
        return None if fit["brackets"] else agy_text
    aa, ra = _atoms(A), _atoms(R)
    a, r = [x for x, _, _ in aa], [x for x, _, _ in ra]
    n, m = len(a), len(r)
    if m > n:
        return None
    cost, aligned = _semi_global(a, r)
    ham = [sum(1 for k in range(m) if r[k] != WILD and r[k] != a[s + k]) for s in range(n - m + 1)]
    best = [s for s, h in enumerate(ham) if h == cost]
    if len(best) != 1:  # the best explanation inserts or deletes, or two places fit equally well
        return None
    s, e = best[0], best[0] + m
    whole = s == 0 and e == n
    nonwild = sum(1 for x in r if x != WILD)
    if not whole and (nonwild - cost < 1 or 2 * (nonwild - cost) < nonwild):
        return None  # less than half of the read is in agy: no evidence this is the place
    if not whole and any(not (st < e and en > s) for st, en in aligned):
        return None  # an equally good alignment elsewhere
    if any(abs(t - s) * 2 > m and h <= cost + 1 and 2 * (nonwild - h) >= nonwild for t, h in enumerate(ham)):
        return None  # a parallel line ('2NF: ..., 3NF: ...') explains the read almost as well
    diff = [k for k in range(m) if r[k] != WILD and r[k] != a[s + k]]
    runs = []
    for k in diff:
        if runs and runs[-1][1] == k - 1:
            runs[-1][1] = k
        else:
            runs.append([k, k])
    for k0, k1 in runs:  # a substitution at the window edge is also an insertion beside an agy token
        edge = (k0 == 0 and s > 0) or (k1 == m - 1 and e < n)
        if edge and not _similar(a[s + k0:s + k1 + 1], r[k0:k1 + 1]):
            return None
    subs = sorted((aa[s + k][1], aa[s + k][2], read_text[ra[k][1]:ra[k][2]]) for k in diff)
    if any(x[1] > y[0] for x, y in zip(subs, subs[1:])):
        return None  # two tokens from one source character (½ -> 1⁄2): no 1:1 place to write
    out = agy_text
    for a0, a1, txt in reversed(subs):
        out = out[:a0] + txt + out[a1:]
    # the result must read exactly as agy outside the window and as the read inside it
    want = a[:s] + [a[s + k] if r[k] == WILD else r[k] for k in range(m)] + a[e:]
    check = _line_fit(_lt(out)[2], R[2])
    return out if _atom_texts(out) == want and check and not check["brackets"] else None


# --------------------------------------------------------------------------- per-kind dispatch
def agy_value(kind: str, part):
    if kind == "code":
        p = part or {}
        return {"language": p.get("language"), "code": p.get("code") or ""}
    if kind == "table":
        p = part or {}
        hdr = list(p.get("headers") or [])
        rows = [list(r) if isinstance(r, (list, tuple)) else [r] for r in (p.get("rows") or [])]
        return {"rows": ([hdr] if hdr else []) + rows, "header_rows": 1 if hdr else 0}
    if kind == "diagram":
        p = part or {}
        edges, unparsed = [], []
        for rel in p.get("relations") or []:
            e = parse_relation(rel)
            if e:
                edges.append(e)
            else:
                unparsed.append(rel)
        return {"nodes": [str(x) for x in (p.get("elements") or []) if x is not None],
                "edges": edges, "unparsed": unparsed}
    if kind == "formula":
        return {"plain": part or "", "latex": None}
    if kind == "line":
        return {"text": part or ""}
    raise ValueError(f"unknown kind {kind!r}; expected one of {KINDS}")


def units(kind: str, value, node_map=None) -> dict:
    v = value or {}
    if kind == "code":
        code = v.get("code") or ""
        return {"code": (tuple(code_tokens(code, v.get("language"))), code)}
    if kind == "table":
        rows, keys, hr = _grid(v)
        u = {"shape": ((len(rows), len(rows[0]) if rows else 0, hr), None)}
        for r, (row, krow) in enumerate(zip(rows, keys)):
            for c, (cell, k) in enumerate(zip(row, krow)):
                u[f"r{r}c{c}"] = (k, cell)
        return u
    if kind == "diagram":
        return _diagram_units(v, node_map)
    if kind == "formula":
        plain = v.get("plain") or ""
        return {"formula": (tuple(math_tokens(plain)), plain)}
    if kind == "line":
        text = v.get("text") or ""
        return {"line": (tuple(line_tokens(text)), text)}
    raise ValueError(f"unknown kind {kind!r}; expected one of {KINDS}")


_COMPARE = {"code": _compare_code, "table": _compare_table, "diagram": _compare_diagram,
            "formula": _compare_formula, "line": _compare_line}


def compare(kind: str, agy_val, read_val) -> dict:
    if kind not in _COMPARE:
        raise ValueError(f"unknown kind {kind!r}; expected one of {KINDS}")
    return _COMPARE[kind](agy_val or {}, read_val or {})


# --------------------------------------------------------------------------- vote
def _freeze(x):
    return tuple(_freeze(y) for y in x) if isinstance(x, (list, tuple)) else x


def _cmp(u: dict, k: str):
    return _freeze(u[k][0]) if k in u else None


def _raw(u: dict, k: str):
    if k not in u:
        return None
    return _cmp(u, k) if k == "shape" and u[k][1] is None else u[k][1]


def _is_toks(c) -> bool:
    return isinstance(c, tuple) and all(isinstance(x, str) for x in c)


def _wild_pat(c):
    """A token tuple with '[?]' (a line WILD token, or '[' '?' ']' from code/math) -> pattern list, else None."""
    out, i, hit = [], 0, False
    while i < len(c):
        if c[i] == WILD or c[i:i + 3] == ("[", "?", "]"):
            out.append(None)
            i += 1 if c[i] == WILD else 3
            hit = True
        else:
            out.append(c[i])
            i += 1
    return out if hit else None


def _wild(u: dict, k: str) -> bool:
    if k not in u or k == "shape":
        return False
    c, raw = _cmp(u, k), u[k][1]
    if _is_toks(c):
        return _wild_pat(c) is not None
    lab = raw.get("label") if isinstance(raw, dict) else raw
    return WILD in ("" if lab is None else str(lab))


def _supports(w, v) -> bool:
    """Does the wildcard version w agree with the readable value v? A token pattern must match its readable part
    ('[?]' = one or more tokens); a cell or a label with '[?]' matches anything (as in compare)."""
    p = _wild_pat(w) if _is_toks(w) else None
    if p is None or not _is_toks(v):
        return True
    ok = [[False] * (len(v) + 1) for _ in range(len(p) + 1)]
    ok[0][0] = True
    for i in range(1, len(p) + 1):
        for j in range(1, len(v) + 1):
            if p[i - 1] is None:  # '[?]' takes v[j-1] as its first token or as one more
                ok[i][j] = ok[i - 1][j - 1] or ok[i][j - 1]
            else:
                ok[i][j] = ok[i - 1][j - 1] and p[i - 1] == v[j - 1]
    return ok[len(p)][len(v)]


def _settle(vers: list, part: list, k: str):
    """Majority over the participating versions -> (found, cmp, raw); raw from agy, else r1, else r2.

    A version whose unit holds '[?]' abstains on what it could not read: it supports every value its readable
    part matches and never wins over a readable value."""
    vals = [_cmp(vers[p], k) for p in part]
    wild = [_wild(vers[p], k) for p in part]
    if len(part) >= 2 and len(set(vals)) == 1:  # unanimous, '[?]' included
        return True, vals[0], _raw(vers[part[0]], k)

    def count(v):
        return sum(1 for x, w in zip(vals, wild) if not w and x == v)

    for p, v, w in zip(part, vals, wild):
        if not w and count(v) >= 2:
            return True, v, _raw(vers[p], k)
    if not any(wild):
        return False, None, None
    win, seen = [], []
    for p, v, w in zip(part, vals, wild):
        if w or v is None or v in seen:
            continue
        seen.append(v)
        if count(v) + sum(1 for x, ww in zip(vals, wild) if ww and _supports(x, v)) >= 2:
            win.append((v, p))
    if count(None) >= 2:
        win.append((None, None))
    if len(win) == 1:
        v, p = win[0]
        return True, v, None if p is None else _raw(vers[p], k)
    return False, None, None


def _vote_line(vers: list) -> dict:
    """compare("line") semantics: a read is one visual line of a possibly longer agy item."""
    agy = vers[0]["line"][1]
    agy_t = "" if agy is None else str(agy)
    texts = ["" if u["line"][1] is None else str(u["line"][1]) for u in vers[1:]]
    ok = [_compare_line({"text": agy_t}, {"text": t})["verdict"] != "mismatch" for t in texts]
    out = {"status": "agy_upheld", "canonical": {"line": agy}, "disputed": [], "changed": [], "pending": []}
    if len(texts) == 1:
        return out if ok[0] else dict(out, status="needs_second_read", pending=["line"])
    if any(ok):
        return out
    same = all(_compare_line({"text": x}, {"text": y})["verdict"] == "agree" for x, y in (texts, texts[::-1]))
    if same and WILD not in line_tokens(texts[0]):
        patched = patch_line(agy_t, texts[0])
        if patched is not None and patched != agy_t:
            raw = vers[1]["line"][1] if line_tokens(patched) == line_tokens(texts[0]) else patched
            return dict(out, status="corrected", canonical={"line": raw}, changed=["line"])
    return dict(out, status="disputed", disputed=["line"])


def vote(agy_u, r1_u, r2_u=None) -> dict:
    vers = [agy_u or {}, r1_u or {}] + ([] if r2_u is None else [r2_u or {}])
    agy = vers[0]
    keys = list(dict.fromkeys(k for u in vers for k in u))
    if all(set(u) == {"line"} for u in vers):
        return _vote_line(vers)
    table = "shape" in keys
    canonical, ccmp, disputed, pending = {}, {}, [], []

    if r2_u is None:  # one blind read so far: settled where agy and r1 agree
        shape_off = table and _cmp(agy, "shape") != _cmp(vers[1], "shape")
        for k in keys:
            if k in agy:
                canonical[k] = _raw(agy, k)
            if shape_off or _cmp(agy, k) != _cmp(vers[1], k):
                pending.append(k)
        return {"status": "needs_second_read" if pending else "agy_upheld", "canonical": canonical,
                "disputed": [], "changed": [], "pending": pending}

    def put(k, ok, v, raw, agy_in=True):
        if not ok:
            disputed.append(k)
            if agy_in and k in agy:  # agy's raw only when agy took part in this vote
                canonical[k] = _raw(agy, k)
        else:
            ccmp[k] = v
            if v is not None:
                canonical[k] = raw

    if not table:
        for k in keys:
            put(k, *_settle(vers, [0, 1, 2], k))
    else:
        ok, shape, raw = _settle(vers, [0, 1, 2], "shape")
        if not ok:  # no agreed grid: every cell is disputed
            disputed = list(keys)
            canonical = {k: _raw(agy, k) for k in agy}
        else:
            put("shape", ok, shape, raw)
            part = [p for p in range(3) if _cmp(vers[p], "shape") == shape]
            for k in keys:
                if k != "shape" and any(k in vers[p] for p in part):
                    put(k, *_settle(vers, part, k), agy_in=0 in part)
    changed = [k for k in keys if k not in disputed and ccmp.get(k) != _cmp(agy, k)]
    status = "disputed" if disputed else "corrected" if changed else "agy_upheld"
    return {"status": status, "canonical": {k: canonical[k] for k in keys if k in canonical},
            "disputed": disputed, "changed": changed, "pending": []}


# --------------------------------------------------------------------------- rebuild
def to_value(kind: str, canonical: dict, template):
    c, t = canonical or {}, template or {}
    if kind == "code":
        return {"language": t.get("language"), "code": c.get("code", "")}
    if kind == "formula":
        return {"plain": c.get("formula", ""), "latex": t.get("latex")}
    if kind == "line":
        return {"text": c.get("line", "")}
    if kind == "table":
        shape = c.get("shape")
        if not isinstance(shape, (list, tuple)) or len(shape) != 3:
            cells = [tuple(map(int, m.groups())) for m in map(_CELL_RE.match, c) if m]
            shape = (max((r for r, _ in cells), default=-1) + 1, max((q for _, q in cells), default=-1) + 1,
                     _grid(t)[2])
        nr, nc, hr = shape
        return {"rows": [[c.get(f"r{r}c{q}", "") for q in range(nc)] for r in range(nr)], "header_rows": hr}
    if kind == "diagram":
        order = list(_diagram_units(t))
        keys = [k for k in order if k in c] + sorted(k for k in c if k not in order)
        out = {"nodes": list(t.get("nodes") or []), "edges": [dict(c[k]) for k in keys]}
        if "unparsed" in t:
            out["unparsed"] = list(t["unparsed"])
        return out
    raise ValueError(f"unknown kind {kind!r}; expected one of {KINDS}")

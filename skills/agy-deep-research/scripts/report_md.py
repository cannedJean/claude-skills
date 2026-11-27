"""Deep Research report tree (UI Automation) -> GFM Markdown, with a structural check.

Spec §23.3 (S8). Stdlib only; imports on any OS; pure functions over uitree.Node.

Scope (find_report_panel / find_report_root)
    The finished report is shown in a side panel next to the chat column. The chat column holds the
    prompt turn, the plan-card turn and the "I've completed your research." turn, plus its own H1
    ("Gemini와의 대화") and the composer (Edit). Rule, taken from the 2026-11-27 live dump:
      1. anchors = allowed Buttons named like a report control (share/export, table of contents,
         sources used in the report; ko and en);
      2. for each anchor, the nearest ancestor whose subtree holds a heading of level 1 is a
         candidate. It is rejected (and that anchor gives nothing) unless it holds exactly one H1,
         that H1 is not the chat title (CHAT_TITLES), and it holds no composer Edit and no chat-turn
         marker (CHAT_TURN_MARKERS, ko + en);
      3. the candidates must be nested in one another (the outermost one is the panel); disjoint
         candidates, no candidate or an empty allow-list -> None (fail closed);
      4. the report root is the panel child on the path to the H1 (skips the panel header: title
         copy, share/export, toc, close buttons) and still holds the cited-source links.
    In the live dump this gives panel = header+body group, root = body group (H1, sections, table,
    lists, source links), and nothing from the chat turns.

Rendering (to_markdown)
    - Only nodes whose id is in `allowed` contribute, and a node outside `allowed` drops its whole
      subtree (fail closed); `allowed` empty/None -> "".
    - Consecutive inline children of one block (Text runs, Hyperlinks) are joined with NO separator:
      Chromium splits a sentence into runs at inline code / emphasis / quote boundaries.
      Whitespace (incl. NBSP and newlines) is collapsed to one space. Paragraph boundaries are not in
      the tree, so one space is added when a run ends with sentence-final punctuation and the next
      starts with a letter/digit; whitespace before a run starting with , . ; : ) is dropped.
      A Group without a block role (BLOCK_ROLES) holding only inline content is joined inline.
      Table cells (Chromium's own join of runs) also lose spaces before , . ; : ) and after (.
    - A control (Button, Edit, MenuItem, ..., embedded Document) is skipped but ends the paragraph:
      the citation chip button ("자세히 알아보기") closes every report paragraph.
    - Headings by HeadingLevel (`Node.heading`; role heading without a level -> 2).
    - Table (role grid/table) -> rows (role row) -> cells; a leaf cell's text is its Name (the live
      tree has no Text child in cells). First row is the GFM header; `|` escaped.
    - List / ListItem -> "- " items. A run made only of hyperlinks (the sources list) -> one "- "
      item per link.
    - Only a Hyperlink whose URL (Value, else aria href) is http(s) becomes [text](url); others are
      plain text. The "새 창에서 열기" / "opens in a new window" suffix is dropped from link text.
"""
import json
import re
from pathlib import Path
from typing import Iterable, List, Optional

from uitree import Node

# the report controls are locators (locators.json: report_share_export, report_toc, report_sources_used, ko + en);
# their locator entries anchor the panel via locators._matches. One source of truth: read from the bundled file.
REPORT_CONTROL_KEYS = ("report_share_export", "report_toc", "report_sources_used")


def _bundled_control_entries() -> tuple:
    with open(Path(__file__).with_name("locators.json"), "r", encoding="utf-8-sig") as fh:
        locs = json.load(fh)["locators"]
    return tuple(locs[k] for k in REPORT_CONTROL_KEYS if k in locs)


REPORT_CONTROL_ENTRIES = _bundled_control_entries()
REPORT_CONTROL_LABELS = tuple(n for ent in REPORT_CONTROL_ENTRIES for n in ent["names"] if not n.startswith("re:"))

SKIP_CT = frozenset({
    "Button", "SplitButton", "Edit", "Menu", "MenuBar", "MenuItem", "CheckBox", "RadioButton", "ComboBox",
    "Image", "ToolTip", "StatusBar", "ScrollBar", "ProgressBar", "Slider", "Spinner", "Tab", "TabItem",
    "ToolBar", "TitleBar", "Thumb", "Window", "Document", "Calendar", "Header", "HeaderItem",
})
SKIP_ROLES = frozenset({"button", "textbox", "menuitem", "checkbox", "radio", "combobox", "switch", "slider",
                        "status", "toolbar", "menu", "menubar", "tab", "tablist", "img", "document"})
CELL_ROLES = frozenset({"gridcell", "cell", "columnheader", "rowheader"})
LINK_SUFFIXES = ("새 창에서 열기", "(opens in a new window)", "opens in a new window", "Opens in a new window")
# chat column markers (ko + en): a panel candidate holding any of them is the chat, never the report
CHAT_TITLES = ("Gemini와의 대화", "Conversation with Gemini")
CHAT_TURN_MARKERS = ("말씀하신 내용", "Gemini의 응답", "You said", "Gemini said")
# roles that make a Group a block; any other Group holding only inline content is joined inline
BLOCK_ROLES = frozenset({"heading", "list", "listitem", "table", "grid", "treegrid", "row", "paragraph", "region",
                         "article", "blockquote", "main", "navigation", "complementary", "banner", "contentinfo",
                         "search", "form", "figure", "section", "document", "dialog", "note"})

_WS = re.compile(r"\s+")
_HTTP = re.compile(r"^https?://", re.I)
_SENT_END = re.compile(r"[.?!。][)\]\"'”’]*$")
_LEAD_PUNCT = re.compile(r"^[,.;:)]")
_CELL_SPACE_PUNCT = re.compile(r"\s+(?=[,.;:)](?:[\s,.;:)]|$))")
_CELL_OPEN_SPACE = re.compile(r"\(\s+")


def _norm(s: str) -> str:
    return _WS.sub(" ", s or "").strip()


def _role(n: Node) -> str:
    return (n.role or "").strip().lower()


def _level(n: Node) -> Optional[int]:
    if isinstance(n.heading, int) and not isinstance(n.heading, bool) and 1 <= n.heading <= 6:
        return n.heading
    if _role(n) == "heading":
        return 2
    return None


def _is_control(n: Node) -> bool:
    return n.ct in SKIP_CT or _role(n) in SKIP_ROLES


def _is_table(n: Node) -> bool:
    return n.ct == "Table" or _role(n) in ("grid", "table", "treegrid")


def _is_list(n: Node) -> bool:
    return n.ct == "List" or _role(n) == "list"


def _is_listitem(n: Node) -> bool:
    return n.ct == "ListItem" or _role(n) == "listitem"


def _is_link(n: Node) -> bool:
    return n.ct == "Hyperlink" or _role(n) == "link"


def _is_inline_text(n: Node) -> bool:
    return n.ct == "Text" and _level(n) is None


def _is_inline_group(n: Node, allowed) -> bool:
    """A wrapper Group (no block role) whose allowed content is only Text runs, links and inline groups."""
    if n.ct != "Group" or _role(n) in BLOCK_ROLES or _level(n) is not None or not n.children:
        return False
    for c in n.children:
        if c.id not in allowed:
            continue
        if _is_link(c) or (_is_inline_text(c) and not c.children) or _is_inline_group(c, allowed):
            continue
        return False
    return True


def _first_char(piece: str) -> str:
    if piece.startswith("[") and len(piece) > 1:  # a rendered link: judge by its label
        return piece[1]
    return piece[:1]


def _join(parts) -> str:
    """Join inline runs with no separator, except: one space after sentence-final punctuation when the
    next run starts with a letter/digit (flat sibling paragraphs), and whitespace before a run that starts
    with , . ; : ) is dropped. Text inside one run is never changed."""
    acc = ""
    for p in parts:
        if not p:
            continue
        if acc:
            if _LEAD_PUNCT.match(p) and acc[-1].isspace():
                acc = acc.rstrip()
            elif _SENT_END.search(acc) and _first_char(p).isalnum():
                acc += " "
        acc += p
    return acc


# -- scope -------------------------------------------------------------------
def _label_key(s: str) -> str:
    return _norm(s).casefold()


def _starts_with_any(name: str, prefixes) -> bool:
    key = _label_key(name)
    return any(key.startswith(_label_key(p)) for p in prefixes)


def _valid_panel(sub) -> bool:
    """Exactly one H1 and it is not the chat title; no composer, no chat-turn marker in the subtree."""
    h1 = [x for x in sub if _level(x) == 1]
    if len(h1) != 1 or _starts_with_any(h1[0].name, CHAT_TITLES):
        return False
    for x in sub:
        if x.ct == "Edit" or _role(x) == "textbox":
            return False
        if _starts_with_any(x.name, CHAT_TURN_MARKERS + CHAT_TITLES):
            return False
    return True


def _control_entries(controls) -> list:
    if controls is None:
        return list(REPORT_CONTROL_ENTRIES)
    if isinstance(controls, dict):
        locs = controls.get("locators", controls)
        return [locs[k] for k in REPORT_CONTROL_KEYS if k in locs]
    entries, names = [], []
    for item in controls:
        if isinstance(item, dict):
            entries.append(item)
        elif isinstance(item, str):
            names.append(item)
    if names:
        entries.append({"ct": "Button", "names": names})
    return entries


def find_report_panel(doc: Node, allowed, labels: Optional[Iterable] = None) -> Optional[Node]:
    """The report side panel (see module doc), or None (fail closed)."""
    if doc is None or not allowed:
        return None
    import locators
    entries = _control_entries(labels)
    if not entries:
        return None
    index = {}
    anchors = []
    for n, anc in doc.walk():
        index[n.id] = (n, anc)
        if n.id in allowed and any(locators._matches(n, ent) for ent in entries):
            anchors.append((n, anc))
    candidates = []
    for _anchor, anc in anchors:
        for a in reversed(anc):
            if a.id not in allowed:
                break
            sub = [x for x, _ in a.walk() if x.id in allowed]
            if any(_level(x) == 1 for x in sub):
                if _valid_panel(sub):
                    candidates.append(a)
                break
    if not candidates:
        return None
    # outermost candidate must contain every other one
    depth = {c.id: len(index[c.id][1]) for c in candidates}
    outer = min(candidates, key=lambda c: depth[c.id])
    inside = {x.id for x, _ in outer.walk()}
    if any(c.id not in inside for c in candidates):
        return None
    return outer


def find_report_root(doc: Node, allowed, labels: Optional[Iterable] = None) -> Optional[Node]:
    """The report body inside the panel: the panel child on the path to the H1."""
    panel = find_report_panel(doc, allowed, labels)
    if panel is None:
        return None
    for n, anc in panel.walk():
        if n.id in allowed and _level(n) == 1:
            if n is panel:
                return panel
            return anc[1] if len(anc) > 1 else n  # anc[0] is the panel itself
    return None


# -- rendering ---------------------------------------------------------------
def _link_text(n: Node, allowed) -> str:
    inner = _inline(n.children, allowed) if n.children else ""
    text = _norm(inner) or _norm(n.name)
    for suf in LINK_SUFFIXES:
        if text.endswith(suf):
            text = text[: -len(suf)].rstrip()
    return text


def _link_url(n: Node) -> str:
    url = (n.value or "").strip() or str((n.aria or {}).get("href") or "").strip()
    return url if _HTTP.match(url) else ""


def _link_md(n: Node, allowed) -> str:
    text = _link_text(n, allowed)
    url = _link_url(n)
    if not url:
        return text
    url = url.replace(" ", "%20").replace("(", "%28").replace(")", "%29")
    label = (text or url).replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
    return "[%s](%s)" % (label, url)


def _leaf_text(n: Node, allowed) -> str:
    if n.children:
        return _inline(n.children, allowed) or n.name
    return n.name


def _inline(children, allowed) -> str:
    """All inline text below `children` in order, controls skipped (used for items, cells, links)."""
    parts = []
    for c in children:
        if c.id not in allowed or _is_control(c):
            continue
        if _is_link(c):
            parts.append(_link_md(c, allowed))
        elif not c.children:
            parts.append(c.name if (c.ct in ("Text", "DataItem") or _level(c)) else "")
        else:
            parts.append(_inline(c.children, allowed))
    return _join(parts)


def _cell(text: str) -> str:
    # a leaf cell Name is Chromium's own join of the cell's inline runs (it pads code spans with spaces)
    t = _CELL_OPEN_SPACE.sub("(", _CELL_SPACE_PUNCT.sub("", _norm(text)))
    return t.replace("|", "\\|")


def _table(n: Node, allowed) -> str:
    rows: List[List[str]] = []

    def collect(node):
        for c in node.children:
            if c.id not in allowed:
                continue
            if _role(c) == "row":
                cells = [_cell(_leaf_text(x, allowed)) for x in c.children
                         if x.id in allowed and (_role(x) in CELL_ROLES or x.ct == "DataItem")]
                if cells:
                    rows.append(cells)
            elif not _is_control(c):
                collect(c)

    collect(n)
    if not rows:
        return ""
    w = max(len(r) for r in rows)
    rows = [r + [""] * (w - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * w) + " |"]
    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(out)


def _list(n: Node, allowed, out: List[str], indent: str = "") -> None:
    for c in n.children:
        if c.id not in allowed or _is_control(c):
            continue
        if _is_listitem(c):
            inline_kids = [k for k in c.children if not _is_list(k)]
            text = _norm(_inline(inline_kids, allowed)) if c.children else _norm(c.name)
            if text:
                out.append(indent + "- " + text)
            for k in c.children:
                if k.id in allowed and _is_list(k):
                    _list(k, allowed, out, indent + "  ")
        elif _is_list(c) or c.children:
            _list(c, allowed, out, indent)


def _pieces(group: Node, allowed) -> List[tuple]:
    """(is_link, markdown) runs of an inline group, in order."""
    out: List[tuple] = []
    for c in group.children:
        if c.id not in allowed:
            continue
        if _is_link(c):
            out.append((True, _link_md(c, allowed)))
        elif _is_inline_text(c) and not c.children:
            out.append((False, c.name))
        else:
            out.extend(_pieces(c, allowed))
    return out


def _blocks(node: Node, allowed, out: List[str]) -> None:
    run: List[tuple] = []  # (is_link, markdown)

    def flush():
        if not run:
            return
        texts = [t for is_link, t in run if not is_link]
        links = [t for is_link, t in run if is_link and t]
        if links and not _norm("".join(texts)):
            out.append("\n".join("- " + _norm(t) for t in links))
        else:
            para = _norm(_join(t for _, t in run))
            if para:
                out.append(para)
        run.clear()

    for c in node.children:
        if c.id not in allowed:
            continue
        lvl = _level(c)
        if lvl is not None:
            flush()
            text = _norm(_leaf_text(c, allowed))
            if text:
                out.append("#" * lvl + " " + text)
        elif _is_control(c):
            flush()
        elif _is_link(c):
            run.append((True, _link_md(c, allowed)))
        elif _is_inline_text(c) and not c.children:
            run.append((False, c.name))
        elif _is_inline_group(c, allowed):
            run.extend(_pieces(c, allowed))
        elif _is_table(c):
            flush()
            t = _table(c, allowed)
            if t:
                out.append(t)
        elif _is_list(c) or _is_listitem(c):
            flush()
            items: List[str] = []
            _list(c if _is_list(c) else Node(id="", ct="List", children=[c]), allowed, items)
            if items:
                out.append("\n".join(items))
        else:
            flush()
            _blocks(c, allowed, out)
    flush()


def to_markdown(report_root: Optional[Node], allowed) -> str:
    """Report body -> GFM Markdown (rules in the module doc). Fail closed on empty `allowed`."""
    if report_root is None or not allowed or report_root.id not in allowed:
        return ""
    out: List[str] = []
    _blocks(Node(id=report_root.id, ct="Group", children=[report_root]), allowed, out)
    return "\n\n".join(b for b in out if b.strip())


# -- stats and check ---------------------------------------------------------
_HEADING_LINE = re.compile(r"^#{1,6} \S")
_SEP_LINE = re.compile(r"^\|(\s*:?-{3,}:?\s*\|)+$")
_LINK = re.compile(r"\]\(https?://", re.I)


def _split_row(line: str) -> List[str]:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|") and not body.endswith("\\|"):
        body = body[:-1]
    return [c.strip() for c in re.split(r"(?<!\\)\|", body)]


def _tables(md: str) -> List[List[List[str]]]:
    lines = md.split("\n")
    tables = []
    i = 0
    while i < len(lines):
        if lines[i].startswith("|") and i + 1 < len(lines) and _SEP_LINE.match(lines[i + 1].strip()):
            rows = [_split_row(lines[i])]
            j = i + 2
            while j < len(lines) and lines[j].startswith("|"):
                rows.append(_split_row(lines[j]))
                j += 1
            tables.append(rows)
            i = j
        else:
            i += 1
    return tables


def report_stats(md: str) -> dict:
    md = md or ""
    return {
        "chars": len(md),
        "headings": sum(1 for line in md.split("\n") if _HEADING_LINE.match(line)),
        "tables": len(_tables(md)),
        "links": len(_LINK.findall(md)),
    }


def structural_check(md: str, stats: dict) -> List[str]:
    """Problems found in the converted report (empty list = passes). Replaces the length-only check."""
    problems: List[str] = []
    if not (md or "").strip():
        return ["empty_report"]
    if not stats.get("headings"):
        problems.append("no_heading")
    if not stats.get("links"):
        problems.append("no_links")
    for rows in _tables(md):
        width = len(rows[0])
        if any(len(r) != width or any(not c for c in r) for r in rows):
            problems.append("empty_table_cell")
            break
    return problems

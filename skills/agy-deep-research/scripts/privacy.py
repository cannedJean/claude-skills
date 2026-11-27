"""Structural privacy allow-list and scrub for the UI Automation driver.

Logging is allow-list based: only nodes structurally inside the page's single
`main` landmark, and outside any chrome landmark or any `region` that contains
a `navigation` (the sidebar with account and chat links), may have their text
logged. Anything unexpected fails closed (nothing allowed).

Stdlib only; imports on any OS.
"""
import re
from typing import Any, Dict, Set, Tuple

from uitree import Node

SENSITIVE_RE = re.compile(r"@|계정|Account|프로필|Profile", re.I)

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
_ACCOUNT_RE = re.compile(r"Google (계정|Account)\s*:[^\n]*")

_EXCLUDED_ROLES = frozenset({"navigation", "banner", "contentinfo", "complementary", "search"})

# Names that may be compared (never logged) outside the allowed region.
GENERIC_LABELS = frozenset({
    "새 채팅", "New chat", "사이드바 열기", "사이드바 닫기", "Open sidebar",
    "Close sidebar", "더보기", "Show more", "메인 메뉴", "Main menu",
})


def scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if isinstance(value, tuple):
        return tuple(scrub(v) for v in value)
    if isinstance(value, str):
        if len(value) <= 200:
            if SENSITIVE_RE.search(value):
                return "<redacted:account len=%d>" % len(value)
            return value
        value = _EMAIL_RE.sub("<redacted:email>", value)
        return _ACCOUNT_RE.sub("<redacted:account>", value)
    return value


def _has_nav_descendant(node: Node) -> bool:
    return any(n.role == "navigation" for n, _ in node.walk() if n is not node)


def _nav_regions(doc: Node):
    """Yield (region, ancestors) for every region that contains a navigation."""
    for n, anc in doc.walk():
        if n.role == "region" and _has_nav_descendant(n):
            yield n, anc


def region_check(doc: Node) -> Tuple[bool, str]:
    mains = [n for n, _ in doc.walk() if n.role == "main"]
    if len(mains) != 1:
        return False, "main_count=%d" % len(mains)
    for region, anc in _nav_regions(doc):
        if not any(a.role == "main" for a in anc):
            return False, "nav_region_outside_main"
    return True, "ok"


def allowed_ids(doc: Node) -> Set[str]:
    ok, _ = region_check(doc)
    if not ok:
        return set()
    nav_region_ids = {r.id for r, _ in _nav_regions(doc)}
    allowed: Set[str] = set()
    for n, anc in doc.walk():
        main_idx = next((i for i, a in enumerate(anc) if a.role == "main"), None)
        if main_idx is None:
            continue
        if n.role in _EXCLUDED_ROLES or n.id in nav_region_ids:
            continue
        if any(a.role in _EXCLUDED_ROLES or a.id in nav_region_ids for a in anc[main_idx + 1:]):
            continue
        allowed.add(n.id)
    return allowed


def outside_summary(doc: Node, allowed: Set[str]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for n, _ in doc.walk():
        if n.id in allowed:
            continue
        key = "%s/%s" % (n.ct, n.role or "-")
        counts[key] = counts.get(key, 0) + 1
    return counts

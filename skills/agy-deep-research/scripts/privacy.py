"""Structural privacy allow-list and scrub for the UI Automation driver.

Two layers, both required:

1. Structural allow-list (allowed_ids): only nodes inside the page's single
   `main` landmark, outside any chrome landmark and outside any `region` that
   contains a `navigation` (the sidebar holding account and chat links), may
   have their text logged. Anything unexpected fails closed (nothing allowed):
   no/duplicate `main`, no sidebar region found, a sidebar region outside
   `main`, `main` under an excluded landmark, or duplicate node ids.
2. Scrub backstop (scrub): any string that still reaches a log is redacted if
   it looks account-like, because the allow-list cannot protect a sensitive
   node that sits directly under `main` outside any region.

Roles are compared case-insensitively and whitespace-stripped.

Deliberate fail-closed hardening (broader than the minimal spec):
- scrub tests/redacts on the NFKC-normalised text, so decomposed Hangul and
  fullwidth look-alikes (e.g. a fullwidth at-sign) cannot evade it.
- The long-string patterns are intentionally broad: any `x@y` (`*` repeats, so
  `react@18` also matches) is replaced, and `Google<ws>Account:` is matched
  case-insensitively with optional whitespace. Over-redacting a long report is
  accepted; leaking an account is not.
- scrub also redacts dict keys, and converts bytes/bytearray to str and
  recurses into sets/frozensets; colliding redacted keys are disambiguated
  with a `#N` suffix so no entry is silently dropped.

Stdlib only; imports on any OS.
"""
import re
import unicodedata
from typing import Any, Dict, Set, Tuple

from uitree import Node

SENSITIVE_RE = re.compile(r"@|계정|Account|프로필|Profile", re.I)

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)*")
_ACCOUNT_RE = re.compile(r"Google\s*(계정|Account)\s*:[^\n]*", re.I)

_EXCLUDED_ROLES = frozenset({"navigation", "banner", "contentinfo", "complementary", "search"})

# Names that may be compared (never logged) outside the allowed region.
GENERIC_LABELS = frozenset({
    "새 채팅", "New chat", "사이드바 열기", "사이드바 닫기", "Open sidebar",
    "Close sidebar", "더보기", "Show more", "메인 메뉴", "Main menu",
})


def _scrub_dict(value: dict) -> dict:
    out: dict = {}
    for k, v in value.items():
        nk = scrub(k)
        if nk in out:
            i = 2
            while "%s#%d" % (nk, i) in out:
                i += 1
            nk = "%s#%d" % (nk, i)
        out[nk] = scrub(v)
    return out


def scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return _scrub_dict(value)
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if isinstance(value, tuple):
        return tuple(scrub(v) for v in value)
    if isinstance(value, (set, frozenset)):
        return type(value)(scrub(v) for v in value)
    if isinstance(value, (bytes, bytearray)):
        return scrub(bytes(value).decode("utf-8", errors="replace"))
    if isinstance(value, str):
        norm = unicodedata.normalize("NFKC", value)
        if len(value) <= 200:
            if SENSITIVE_RE.search(value) or SENSITIVE_RE.search(norm):
                return "<redacted:account len=%d>" % len(value)
            return value
        value = _EMAIL_RE.sub("<redacted:email>", norm)
        return _ACCOUNT_RE.sub("<redacted:account>", value)
    return value


def _role(n: Node) -> str:
    return (n.role or "").strip().lower()


def _has_nav_descendant(node: Node) -> bool:
    return any(_role(n) == "navigation" for n, _ in node.walk() if n is not node)


def _nav_regions(doc: Node):
    """Yield (region, ancestors) for every region that contains a navigation."""
    for n, anc in doc.walk():
        if _role(n) == "region" and _has_nav_descendant(n):
            yield n, anc


def region_check(doc: Node) -> Tuple[bool, str]:
    seen = set()
    for n, _ in doc.walk():
        if n.id in seen:
            return False, "duplicate_ids"
        seen.add(n.id)
    mains = [(n, anc) for n, anc in doc.walk() if _role(n) == "main"]
    if len(mains) != 1:
        return False, "main_count=%d" % len(mains)
    if any(_role(a) in _EXCLUDED_ROLES for a in mains[0][1]):
        return False, "main_inside_excluded"
    count = 0
    for _region, anc in _nav_regions(doc):
        if not any(_role(a) == "main" for a in anc):
            return False, "nav_region_outside_main"
        count += 1
    if count == 0:
        return False, "no_nav_region"
    return True, "ok"


def allowed_ids(doc: Node) -> Set[str]:
    ok, _ = region_check(doc)
    if not ok:
        return set()
    nav_region_ids = {r.id for r, _ in _nav_regions(doc)}
    allowed: Set[str] = set()
    for n, anc in doc.walk():
        main_idx = next((i for i, a in enumerate(anc) if _role(a) == "main"), None)
        if main_idx is None:
            continue
        if _role(n) in _EXCLUDED_ROLES or n.id in nav_region_ids:
            continue
        if any(_role(a) in _EXCLUDED_ROLES or a.id in nav_region_ids
               for a in anc[main_idx + 1:]):
            continue
        allowed.add(n.id)
    return allowed


def outside_summary(doc: Node, allowed: Set[str]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for n, _ in doc.walk():
        if n.id in allowed:
            continue
        key = scrub("%s/%s" % (n.ct, n.role or "-"))
        counts[key] = counts.get(key, 0) + 1
    return counts

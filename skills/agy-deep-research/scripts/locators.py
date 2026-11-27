"""Data-driven locator engine with UI_LOCATOR_MISS hints.

Locators live in locators.json (bundled) and may be overridden field by field
from %LOCALAPPDATA%\\agy-deep-research\\locators.local.json. A miss raises
LocatorMiss whose payload lists similar candidate names (only from the
privacy-allowed region or GENERIC_LABELS) and an exact override snippet.
The override may contain only `version` and `locators`; it may add new locator
keys and a missing override file is ignored (both deliberate).
The bundled file must define `version` and `locators`.

Notes for callers:
- `plan_edit` and `plan_cancel_countdown` anchor on the NEWEST turn holding any
  plan control (plan_start, plan_edit, plan_cancel_countdown,
  plan_retry_without_dr); if that turn's plan_start is not present and enabled
  they raise LocatorMiss (even the optional cancel). An older card is never used.
  `plan_start` itself resolves in the newest turn holding one (enabled or not).
- An `optional` locator's absence may be used as a state only when a positive
  anchor (`prompt_input` / `tools` / `plan_start`) resolved in the same
  snapshot; callers enforce this (Task 8).
- `desktop_root()` is the root of the tool's own hidden desktop, which holds
  only the tool's Gemini instance, so the `popup`/`desktop` scopes cannot reach
  other applications. Their nodes are not in the page's allowed region, so
  misses there only get GENERIC_LABELS hints (accepted limitation).
- The `report_panel` scope is the finished report's side panel as report_md.find_report_panel finds it
  (anchored on the names of the REPORT_CONTROLS locators, allowed region only, fail closed: no valid
  panel -> no roots). A report control outside that panel (a chip in the chat column, another
  region) therefore never resolves.
Stdlib only; imports on any OS.
"""
import copy
import difflib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Set
from urllib.parse import urlparse

from uitree import Node, PATTERNS, UiTree
import privacy
import report_md

TOP_KEYS = ("version", "scopes", "locators", "surfaces_allowed", "status_phrases",
            "safety_phrases", "error_phrases", "model_aliases")
LOCATOR_FIELDS = ("ct", "role", "names", "scope", "expect", "patterns", "href_host")
SCOPE_KINDS = {
    "document": (), "main": (), "popup": (), "desktop": (),
    "composer": ("anchor", "min_buttons"),
    "plan_card": ("ancestor_automation_id_prefix",),
    "top_bar": ("url_prefix",),
    "report_panel": (),
}
EXPECTS = ("unique", "at_least_one", "optional")
HREF_HOST = "gemini.google.com"
MAX_CANDIDATES = 5
MIN_SIMILARITY = 0.4
FALLBACK_CANDIDATES = 3  # listed (never added to the snippet) when nothing is similar enough
# plan_card locators that are resolved inside the newest turn holding a plan_start
PLAN_ANCHORED = ("plan_start", "plan_edit", "plan_cancel_countdown")
# ... of those, the ones that act on a live card: only the turn holding the newest ENABLED plan_start
PLAN_ENABLED_ANCHORED = ("plan_edit", "plan_cancel_countdown")
# a turn holding any of these is a plan card turn: the newest one is the only card plan actions may touch
PLAN_CONTROLS = ("plan_start", "plan_edit", "plan_cancel_countdown", "plan_retry_without_dr")
NO_ENABLED_START_HINT = "the newest plan card has no enabled plan_start: not binding it or an older card"
# scopes whose roots come from the bound conversation document (ctx.doc)
DOC_SCOPES = ("document", "main", "composer", "plan_card", "report_panel")
# locators whose names anchor the report panel (report_md reads the same names from locators.json)
REPORT_CONTROLS = report_md.REPORT_CONTROL_KEYS
REQUIRED = ("ct", "names", "scope", "expect")


class LocatorConfigError(ValueError):
    pass


class LocatorMiss(Exception):
    def __init__(self, payload: dict):
        super().__init__("%s: %s" % (payload.get("code"), payload.get("locator")))
        self.payload = payload


@dataclass
class LocatorContext:
    tree: UiTree
    doc: Node
    locs: dict
    allowed: Set[str]
    anchor_cache: dict = field(default_factory=dict)


def _norm(s: str) -> str:
    return " ".join(unicodedata.normalize("NFC", s or "").split())


def _role(n: Node) -> str:
    return (n.role or "").strip().lower()


def _compile(pat: str, where: str):
    if not pat[3:].strip():
        raise LocatorConfigError("%s: empty regex %r" % (where, pat))
    try:
        return re.compile(pat[3:])
    except re.error as exc:
        raise LocatorConfigError("%s: invalid regex %r (%s)" % (where, pat, exc))


def match_name(name: str, patterns: list) -> bool:
    n = _norm(name)
    for p in patterns:
        if p.startswith("re:"):
            if re.fullmatch(p[3:], n):
                return True
        elif _norm(p) == n:
            return True
    return False


def _is_strlist(v) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def _validate(cfg: dict, where: str) -> None:
    for k in cfg:
        if k not in TOP_KEYS:
            raise LocatorConfigError("%s: unknown top-level field %r" % (where, k))
    if "version" in cfg and (cfg["version"] != 1 or isinstance(cfg["version"], bool)):
        raise LocatorConfigError("%s: field 'version' must be 1" % where)
    if "scopes" in cfg:
        if not isinstance(cfg["scopes"], dict):
            raise LocatorConfigError("%s: field 'scopes' must be an object" % where)
        for sname, sval in cfg["scopes"].items():
            if sname not in SCOPE_KINDS:
                raise LocatorConfigError("%s: unknown scope %r" % (where, sname))
            if not isinstance(sval, dict) or any(f not in SCOPE_KINDS[sname] for f in sval):
                raise LocatorConfigError("%s: scope %r has unknown fields or wrong type" % (where, sname))
            mb = sval.get("min_buttons", 1)
            if isinstance(mb, bool) or not isinstance(mb, int) or mb < 1:
                raise LocatorConfigError("%s: scope %r field 'min_buttons' must be an int >= 1" % (where, sname))
            for sf in ("ancestor_automation_id_prefix", "url_prefix", "anchor"):
                if sf in sval and (not isinstance(sval[sf], str) or not sval[sf]):
                    raise LocatorConfigError("%s: scope %r field %r must be a non-empty string" % (where, sname, sf))
    for lk in ("surfaces_allowed", "status_phrases", "safety_phrases", "error_phrases"):
        if lk in cfg:
            if not _is_strlist(cfg[lk]):
                raise LocatorConfigError("%s: field %r must be a list of strings" % (where, lk))
            for p in cfg[lk]:
                if p.startswith("re:"):
                    _compile(p, "%s field %r" % (where, lk))
    if "model_aliases" in cfg:
        m = cfg["model_aliases"]
        if not isinstance(m, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in m.items()):
            raise LocatorConfigError("%s: field 'model_aliases' must map strings to strings" % where)
    locs = cfg.get("locators", {})
    if not isinstance(locs, dict):
        raise LocatorConfigError("%s: field 'locators' must be an object" % where)
    for key, ent in locs.items():
        w = "%s locator %r" % (where, key)
        if not isinstance(ent, dict):
            raise LocatorConfigError("%s: must be an object" % w)
        for f, v in ent.items():
            if f not in LOCATOR_FIELDS:
                raise LocatorConfigError("%s: unknown field %r" % (w, f))
            if f in ("ct", "role", "scope", "expect", "href_host") and not isinstance(v, str):
                raise LocatorConfigError("%s: field %r must be a string" % (w, f))
            if f in ("names", "patterns") and not _is_strlist(v):
                raise LocatorConfigError("%s: field %r must be a list of strings" % (w, f))
        if "names" in ent and not ent["names"]:
            raise LocatorConfigError("%s: field 'names' must not be empty" % w)
        if any(not _norm(p) for p in ent.get("names", [])):
            raise LocatorConfigError("%s: field 'names' must not contain blank names" % w)
        if "ct" in ent and not ent["ct"].strip():
            raise LocatorConfigError("%s: field 'ct' must not be blank" % w)
        if "expect" in ent and ent["expect"] not in EXPECTS:
            raise LocatorConfigError("%s: field 'expect' must be one of %s" % (w, ", ".join(EXPECTS)))
        if "scope" in ent and ent["scope"] not in SCOPE_KINDS:
            raise LocatorConfigError("%s: field 'scope' unknown scope %r" % (w, ent["scope"]))
        if "href_host" in ent and ent["href_host"] != HREF_HOST:
            raise LocatorConfigError("%s: field 'href_host' must be %s" % (w, HREF_HOST))
        for p in ent.get("patterns", []):
            if p not in PATTERNS:
                raise LocatorConfigError("%s: field 'patterns' unknown pattern %r" % (w, p))
        for p in ent.get("names", []):
            if p.startswith("re:"):
                _compile(p, w + " field 'names'")


def _read(path: Path, where: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError) as exc:
        raise LocatorConfigError("%s: cannot read %s (%s)" % (where, path, exc))
    if not isinstance(cfg, dict):
        raise LocatorConfigError("%s: top level must be an object" % where)
    _validate(cfg, where)
    return cfg


def _require(cfg: dict) -> None:
    for sname, sval in cfg.get("scopes", {}).items():
        anchor = sval.get("anchor")
        if anchor is not None and anchor not in cfg.get("locators", {}):
            raise LocatorConfigError("scope %r field 'anchor': unknown locator %r" % (sname, anchor))
        if anchor is not None:
            aent = cfg["locators"][anchor]
            if aent.get("scope") == sname or aent.get("expect") != "unique":
                raise LocatorConfigError(
                    "scope %r field 'anchor': locator %r must be 'unique' and not scoped to %r"
                    % (sname, anchor, sname))
    for key, ent in cfg.get("locators", {}).items():
        for f in REQUIRED:
            if f not in ent:
                raise LocatorConfigError("locator %r: missing field %r" % (key, f))


def load_locators(bundled: Path, override: Optional[Path] = None) -> dict:
    cfg = _read(Path(bundled), "bundled")
    for req in ("version", "locators"):
        if req not in cfg:
            raise LocatorConfigError("bundled: missing required top-level field %r" % req)
    _require(cfg)
    if override is not None and Path(override).exists():
        ov = _read(Path(override), "override")
        for k in ov:
            if k not in ("version", "locators"):
                raise LocatorConfigError(
                    "override: top-level field %r not allowed (only version, locators)" % k)
        for k, v in ov.items():
            if k == "locators":
                for key, ent in v.items():
                    cfg["locators"].setdefault(key, {}).update(copy.deepcopy(ent))
            else:
                cfg[k] = copy.deepcopy(v)
        _require(cfg)
    cfg.setdefault("error_phrases", [])
    return cfg


# -- scope resolution -----------------------------------------------------
def _plan_turns(doc: Node, prefix: str) -> List[Node]:
    """Outermost turn groups (a nested prefixed descendant belongs to its turn)."""
    return [n for n, anc in doc.walk()
            if (n.automation_id or "").startswith(prefix)
            and not any((a.automation_id or "").startswith(prefix) for a in anc)]


def _scope_roots(scope: str, ctx: LocatorContext, key: Optional[str] = None) -> List[Node]:
    conf = ctx.locs.get("scopes", {}).get(scope, {})
    doc = ctx.doc
    if scope == "document":
        return [doc]
    if scope == "main":
        return doc.find_all(lambda n, a: _role(n) == "main")[:1]
    if scope in ("popup", "desktop"):
        return [ctx.tree.desktop_root()]
    if scope == "top_bar":
        prefix = conf.get("url_prefix", "app://bundle/src/top_bar/")
        for d in ctx.tree.documents():
            if (d.value or "").startswith(prefix):
                return [ctx.tree.snapshot(d.id)]
        return []
    if scope == "plan_card":
        prefix = conf.get("ancestor_automation_id_prefix", "model-response-message-content")
        turns = _plan_turns(doc, prefix)
        # the plan card is the newest turn that holds a plan_start button (an enabled one for plan actions)
        akey = "plan_start" if key in PLAN_ANCHORED else key
        aent = ctx.locs["locators"].get(akey) if akey else None
        if key in PLAN_ENABLED_ANCHORED:
            ents = [ctx.locs["locators"][k] for k in PLAN_CONTROLS if k in ctx.locs["locators"]]
            start = ctx.locs["locators"].get("plan_start")
            for turn in reversed(turns):
                nodes = [n for n, _ in turn.walk()]
                if any(_matches(n, e) for n in nodes for e in ents):
                    # the newest card only: never walk back to an older one
                    ok = start is not None and any(_matches(n, start) and n.enabled for n in nodes)
                    return [turn] if ok else []
            return []
        if aent is not None:
            for turn in reversed(turns):
                if any(_matches(n, aent) for n, _ in turn.walk()):
                    return [turn]
        return turns[-1:]
    if scope == "report_panel":
        labels = [n for k in REPORT_CONTROLS for n in ctx.locs["locators"].get(k, {}).get("names", ())
                  if not n.startswith("re:")]
        panel = report_md.find_report_panel(doc, ctx.allowed, labels) if labels else None
        return [panel] if panel is not None else []
    if scope == "composer":
        akey = conf.get("anchor", "prompt_input")
        anchor = ctx.anchor_cache.get(akey)
        if anchor is None:
            found = resolve(akey, ctx)
            if not found:
                return []
            anchor = found[0]
            ctx.anchor_cache[akey] = anchor
        need = conf.get("min_buttons", 3)
        for n, anc in doc.walk():
            if n.id == anchor.id:
                for a in reversed(anc):
                    if sum(1 for x, _ in a.walk() if x.ct == "Button") >= need:
                        return [a]
                return []
        return []
    raise LocatorConfigError("unknown scope %r" % scope)


def _host_ok(node: Node, host: str) -> bool:
    href = (node.aria or {}).get("href") or node.value or ""
    if not isinstance(href, str):
        return False
    try:
        parts = urlparse(href)
        return parts.scheme == "https" and parts.hostname == host
    except ValueError:
        return False


def _base_match(n: Node, ent: dict) -> bool:
    if n.ct != ent["ct"]:
        return False
    if ent.get("role") and _role(n) != ent["role"].strip().lower():
        return False
    return True


def _matches(n: Node, ent: dict) -> bool:
    if not _base_match(n, ent) or not match_name(n.name, ent["names"]):
        return False
    if not set(ent.get("patterns") or ()) <= set(n.patterns):
        return False
    if ent.get("href_host") and not _host_ok(n, ent["href_host"]):
        return False
    return True


def _sim(name: str, pats: list) -> float:
    best = 0.0
    for p in pats:
        ref = p[3:].replace(".*", "").replace(".+", "") if p.startswith("re:") else _norm(p)
        best = max(best, difflib.SequenceMatcher(None, name, ref).ratio())
    return best


def _candidates(ent: dict, roots: List[Node], ctx: LocatorContext) -> list:
    """Eligible similar names. Allowed ids belong to the bound conversation
    document only, so nodes from the popup/desktop/top_bar documents are
    eligible solely through GENERIC_LABELS (ids may collide across trees)."""
    by_allowed = ent["scope"] in DOC_SCOPES
    seen, out = set(), []
    for root in roots:
        for n, _ in root.walk():
            if not _base_match(n, ent):
                continue
            name = _norm(n.name)
            if not name or name in seen:
                continue
            if not ((by_allowed and n.id in ctx.allowed) or name in privacy.GENERIC_LABELS):
                continue
            sim = round(_sim(name, ent["names"]), 2)
            seen.add(name)
            out.append({"name": name, "ct": n.ct, "role": n.role, "similarity": sim})
    out.sort(key=lambda c: -c["similarity"])
    similar = [c for c in out if c["similarity"] >= MIN_SIMILARITY]
    if similar:
        return similar[:MAX_CANDIDATES]
    return out[:FALLBACK_CANDIDATES]


def resolve(key: str, ctx: LocatorContext) -> List[Node]:
    ent = ctx.locs["locators"].get(key)
    if ent is None:
        raise LocatorConfigError("unknown locator %r" % key)
    roots = _scope_roots(ent["scope"], ctx, key)
    if ent["scope"] == "plan_card" and key in PLAN_ENABLED_ANCHORED and not roots:
        raise LocatorMiss(privacy.scrub({
            "code": "UI_LOCATOR_MISS", "locator": key, "scope": ent["scope"], "expect": ent["expect"],
            "found": 0, "candidates": [], "hint": NO_ENABLED_START_HINT,
            "override_snippet": {"version": 1, "locators": {key: {"names": list(ent["names"])}}},
        }))
    found, seen = [], set()
    for root in roots:
        for n, _ in root.walk():
            if id(n) not in seen and _matches(n, ent):
                seen.add(id(n))
                found.append(n)
    expect = ent["expect"]
    ok = (expect == "optional" or (expect == "unique" and len(found) == 1)
          or (expect == "at_least_one" and len(found) >= 1))
    if ok:
        return found
    cands = _candidates(ent, roots, ctx)
    names = list(ent["names"])
    if not found and cands and cands[0]["similarity"] >= MIN_SIMILARITY:
        top = cands[0]["name"]
        entry = "re:" + re.escape(top) if top.startswith("re:") else top
        if entry not in names:  # compare the form that would be added (an escaped re: entry may be there already)
            names.append(entry)
    payload = {
        "code": "UI_LOCATOR_MISS", "locator": key, "scope": ent["scope"],
        "expect": expect, "found": len(found), "candidates": cands,
        "override_snippet": {"version": 1, "locators": {key: {"names": names}}},
    }
    if len(found) > 1:
        payload["hint"] = "ambiguous: narrow names or scope"
    raise LocatorMiss(privacy.scrub(payload))

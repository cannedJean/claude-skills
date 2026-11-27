"""Plan/edit step logic of the Gemini desktop UI Automation driver.

Written purely against uitree.UiTree, so every step runs on FakeTree in tests.
Stdlib only; imports on any OS; never prints, never starts processes.

Rules (spec section 11, Task 8 rulings):
- Binding: a step only touches the document whose URL equals the expected one
  exactly (CONVERSATION_MISMATCH otherwise, also if the URL changes mid-step).
- An `optional` locator's absence counts as a state only when a positive anchor
  (`prompt_input`, or `tools` for the Deep Research button) resolved in the same
  snapshot; otherwise the snapshot proves nothing.
- Plan cards are the outermost `plan_card`-prefixed turns holding a `plan_start`
  button. The newest card is the last one in document order; it is accepted
  only when its start button is enabled and the card count grew past
  `prev_cards`. Its countdown is cancelled the moment it is seen (`취소`, else
  `plan_edit` as the second stop), then verified for `cancel_verify_s`: start
  still enabled and no `stop_response`. Research progress (no enabled start +
  `stop_response`) is stopped and reported as UNREVIEWED_START; any other failed
  check is COUNTDOWN_NOT_CANCELLED. Fail closed, never retried.
- Polling interval = max(cfg.poll_min_s, 2 x duration of the last snapshot).
- Every StepError.details and every returned value has passed privacy.scrub.
- Node ids must stay stable for the same UI element across snapshots (the UIA
  backend uses runtime ids); the cancel verification tracks the card by id.
"""
import hashlib
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

import locators
import privacy
from locators import LocatorContext, LocatorMiss, resolve
from uitree import Node, PatternNotSupported

FRESH_URL = "https://gemini.google.com/app"
CONVERSATION_RE = re.compile(r"^https://gemini\.google\.com/app/[0-9a-f]{8,}$")
DEFAULT_CARD_PREFIX = "model-response-message-content"


class StepError(Exception):
    """A step failed with a protocol error code; `details` is already scrubbed."""

    def __init__(self, code: str, details: Optional[dict] = None):
        self.code = code
        self.details = privacy.scrub(dict(details or {}))
        super().__init__(code)


@dataclass
class StepConfig:
    poll_min_s: float = 0.1
    card_timeout_s: float = 900
    cancel_verify_s: float = 5
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    ui_timeout_s: float = 15        # menus, buttons, local UI reactions
    bind_timeout_s: float = 60      # URL bind after send (measured 13 s)
    turn_timeout_s: float = 120     # Gemini's canned turn after `plan_edit`


# -- small helpers -----------------------------------------------------------
def _norm(s: str) -> str:
    return " ".join(unicodedata.normalize("NFC", s or "").split())


def _fold(s: str) -> str:
    return _norm(s).casefold()


def _sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _checked(n: Node) -> bool:
    return str((n.aria or {}).get("checked", "")).strip().lower() == "true"


def _try(ctx: LocatorContext, key: str) -> Optional[List[Node]]:
    """resolve(), but a miss (including a missing composer anchor) gives None."""
    try:
        return resolve(key, ctx)
    except LocatorMiss:
        return None


def _absent(ctx: LocatorContext, key: str, anchor: str) -> Optional[bool]:
    """True/False only when the positive anchor resolved in this snapshot; else None."""
    if not _try(ctx, anchor):
        return None
    found = _try(ctx, key)
    if found is None:
        return None
    return not found


def _act(tree, node: Node, prefs: Tuple[str, ...], key: str) -> str:
    for p in prefs:
        if p in node.patterns:
            try:
                tree.act(node.id, p)
                return p
            except PatternNotSupported:
                continue
    raise StepError("UI_ACTION_FAILED", {"locator": key, "patterns": sorted(node.patterns)})


def _try_act(tree, node: Node, prefs: Tuple[str, ...]) -> bool:
    try:
        _act(tree, node, prefs, "")
        return True
    except StepError:
        return False


def _phrase_hit(text: str, phrases) -> bool:
    for p in phrases or ():
        if p.startswith("re:"):
            if re.search(p[3:], text):
                return True
        elif _norm(p) and _norm(p) in text:
            return True
    return False


class _Poll:
    """Snapshots one document; optionally insists that its URL stays `url`."""

    def __init__(self, tree, locs: dict, cfg: StepConfig, doc_id: str, url: Optional[str] = None):
        self.tree, self.locs, self.cfg, self.doc_id, self.url = tree, locs, cfg, doc_id, url
        self.last_s = 0.0

    def snap(self) -> LocatorContext:
        t0 = self.cfg.clock()
        try:
            doc = self.tree.snapshot(self.doc_id)
        except KeyError:
            raise StepError("CONVERSATION_MISMATCH", {"reason": "document_gone", "expected": self.url or ""})
        self.last_s = max(0.0, self.cfg.clock() - t0)
        if self.url is not None and doc.value != self.url:
            raise StepError("CONVERSATION_MISMATCH",
                            {"reason": "url_changed", "expected": self.url, "actual": doc.value})
        return LocatorContext(self.tree, doc, self.locs, privacy.allowed_ids(doc))

    def interval(self) -> float:
        return max(self.cfg.poll_min_s, 2.0 * self.last_s)

    def until(self, timeout_s: float, check, what: str, on_timeout=None):
        """Poll until check(ctx) is truthy; on timeout call on_timeout(ctx) (which
        should raise), else raise UI_TIMEOUT."""
        deadline = self.cfg.clock() + timeout_s
        while True:
            ctx = self.snap()
            r = check(ctx)
            if r:
                return r
            if self.cfg.clock() >= deadline:
                if on_timeout is not None:
                    on_timeout(ctx)
                raise StepError("UI_TIMEOUT", {"waiting_for": what, "waited_s": round(timeout_s, 1)})
            self.cfg.sleep(self.interval())


# -- plan card model ---------------------------------------------------------
def _prefix(locs: dict) -> str:
    return (locs.get("scopes", {}).get("plan_card", {})
            .get("ancestor_automation_id_prefix", DEFAULT_CARD_PREFIX))


def _turns(ctx: LocatorContext) -> List[Node]:
    return locators._plan_turns(ctx.doc, _prefix(ctx.locs))


def _find_in(turn: Node, ctx: LocatorContext, key: str) -> List[Node]:
    ent = ctx.locs["locators"][key]
    return [n for n, _ in turn.walk() if locators._matches(n, ent)]


def _turn_ctx(ctx: LocatorContext, turn: Node) -> LocatorContext:
    return LocatorContext(ctx.tree, turn, ctx.locs, ctx.allowed)


def _raise_turn_miss(ctx: LocatorContext, turn: Node, key: str) -> None:
    """Raise the LocatorMiss (with hints) for `key` inside one turn."""
    resolve(key, _turn_ctx(ctx, turn))
    raise StepError("PLAN_CARD_UNRECOGNISED", {"locator": key})


def _cards(ctx: LocatorContext) -> List[Tuple[Node, Node]]:
    """[(turn, plan_start)] in document order."""
    out = []
    for turn in _turns(ctx):
        starts = _find_in(turn, ctx, "plan_start")
        if len(starts) > 1:
            _raise_turn_miss(ctx, turn, "plan_start")  # ambiguous: never guess
        if starts:
            out.append((turn, starts[0]))
    return out


def _turn_by_id(ctx: LocatorContext, turn_id: str) -> Optional[Node]:
    return next((t for t in _turns(ctx) if t.id == turn_id), None)


def _turn_text(turn: Node) -> str:
    return " ".join(_norm(n.name) for n, _ in turn.walk() if _norm(n.name))


def extract_plan_text(card: Node, allowed) -> str:
    """Text and heading nodes of the card in document order (allowed ids only,
    nothing inside buttons), `#` x heading level, consecutive duplicates dropped,
    every line scrubbed."""
    lines, prev = [], None
    for n, anc in card.walk():
        if n.id not in allowed or n.ct == "Button" or any(a.ct == "Button" for a in anc):
            continue
        level = n.heading if isinstance(n.heading, int) and not isinstance(n.heading, bool) \
            and 1 <= n.heading <= 6 else None
        if level is None and n.ct != "Text":
            continue
        text = _norm(n.name)
        if not text or text == prev:
            continue
        prev = text
        safe = privacy.scrub(text)
        lines.append("%s %s" % ("#" * level, safe) if level else safe)
    return "\n".join(lines)


# -- binding -----------------------------------------------------------------
def bind_conversation(tree, url: str) -> Node:
    docs = [d for d in tree.documents() if d.value == url]
    if len(docs) != 1:
        raise StepError("CONVERSATION_MISMATCH", {"expected": url, "found": len(docs)})
    return docs[0]


def ensure_fresh_chat(tree, locs: dict, cfg: StepConfig) -> Node:
    docs = [d for d in tree.documents()
            if d.value == FRESH_URL or CONVERSATION_RE.match(d.value or "")]
    if len(docs) != 1:
        raise StepError("CONVERSATION_MISMATCH", {"reason": "conversation_documents", "found": len(docs)})
    poll = _Poll(tree, locs, cfg, docs[0].id)
    ctx = poll.snap()
    if ctx.doc.value == FRESH_URL:
        return ctx.doc
    opened = False
    links = _try(ctx, "new_chat")
    if not links:
        side = _try(ctx, "sidebar_open")
        if not side:
            resolve("new_chat", ctx)  # raises the miss with hints
        _act(tree, side[0], ("invoke", "expand"), "sidebar_open")
        opened = True
        links = poll.until(cfg.ui_timeout_s, lambda c: _try(c, "new_chat"), "new_chat",
                           lambda c: resolve("new_chat", c))
    link = next((n for n in links if "invoke" in n.patterns), links[0])
    _act(tree, link, ("invoke",), "new_chat")
    ctx = poll.until(cfg.ui_timeout_s, lambda c: c if c.doc.value == FRESH_URL else None, "fresh_chat")
    if opened:
        close = _try(ctx, "sidebar_close")
        if close:
            _act(tree, close[0], ("invoke", "collapse"), "sidebar_close")
        ctx = poll.snap()
        if ctx.doc.value != FRESH_URL:
            raise StepError("CONVERSATION_MISMATCH", {"reason": "url_changed", "expected": FRESH_URL})
    return ctx.doc


# -- model and thinking level ------------------------------------------------
def match_names(names: List[str], wanted: str, aliases: Optional[dict] = None) -> List[str]:
    """Case-insensitive substring match of `wanted` (after the alias map) on names."""
    amap = {_fold(k): v for k, v in (aliases or {}).items()}
    needle = _fold(amap.get(_fold(wanted), wanted))
    if not needle:
        return []
    return [n for n in names if needle in _fold(n)]


def _mode_items(ctx: LocatorContext) -> List[Node]:
    return _try(ctx, "mode_item") or []


def _menu_open(tree, poll: _Poll, cfg: StepConfig) -> List[Node]:
    ctx = poll.snap()
    items = _mode_items(ctx)
    if items:
        return items
    _act(tree, resolve("mode_button", ctx)[0], ("expand", "invoke"), "mode_button")
    return poll.until(cfg.ui_timeout_s, _mode_items, "mode_menu", lambda c: resolve("mode_item", c))


def _menu_close(tree, poll: _Poll, cfg: StepConfig) -> None:
    ctx = poll.snap()
    if not _mode_items(ctx):
        return
    _act(tree, resolve("mode_button", ctx)[0], ("collapse", "invoke"), "mode_button")
    poll.until(cfg.ui_timeout_s, lambda c: not _mode_items(c), "mode_menu_closed")


def _choices(tree, poll: _Poll, cfg: StepConfig, kind: str) -> List[Node]:
    items = _menu_open(tree, poll, cfg)
    if kind == "model":
        return items
    known = {n.id for n in items}
    sub = poll.until(cfg.ui_timeout_s, lambda c: _try(c, "thinking_menu"), "thinking_menu",
                     lambda c: resolve("thinking_menu", c))
    _act(tree, sub[0], ("expand", "invoke"), "thinking_menu")
    return poll.until(cfg.ui_timeout_s, lambda c: [n for n in _mode_items(c) if n.id not in known],
                      "thinking_items")


def _pick(tree, poll: _Poll, cfg: StepConfig, wanted: str, aliases: dict, kind: str) -> None:
    items = _choices(tree, poll, cfg, kind)
    names = [n.name for n in items]
    hit_names = match_names(names, wanted, aliases)
    hits = [n for n in items if n.name in hit_names]
    if len(hits) != 1:
        _menu_close(tree, poll, cfg)
        if not hits:
            raise StepError("MODEL_NOT_AVAILABLE", {"kind": kind, "wanted": wanted, "available": names})
        raise StepError("MODEL_AMBIGUOUS", {"kind": kind, "wanted": wanted, "matches": [n.name for n in hits]})
    target = hits[0]
    if _checked(target):
        return
    _act(tree, target, ("invoke", "default_action", "toggle"), "mode_item")

    def settled(c):
        its = _mode_items(c)
        if not its:
            return "closed"
        same = [n for n in its if n.id == target.id]
        return "checked" if same and _checked(same[0]) else None

    if poll.until(cfg.ui_timeout_s, settled, "mode_selected") == "checked":
        return
    again = [n for n in _choices(tree, poll, cfg, kind)
             if n.name in match_names([n.name], wanted, aliases)]
    if len(again) != 1 or not _checked(again[0]):
        _menu_close(tree, poll, cfg)
        raise StepError("MODEL_NOT_SELECTED", {"kind": kind, "wanted": wanted})


def select_mode(tree, locs: dict, cfg: StepConfig, wanted: str, thinking: Optional[str]) -> str:
    """Select the mode (and optionally the thinking level) by the names the menu
    shows at run time; returns the mode-button name afterwards."""
    doc = bind_conversation(tree, FRESH_URL)
    poll = _Poll(tree, locs, cfg, doc.id, FRESH_URL)
    _pick(tree, poll, cfg, wanted, locs.get("model_aliases", {}), "model")
    if thinking is not None:
        _pick(tree, poll, cfg, thinking, {}, "thinking")
    _menu_close(tree, poll, cfg)
    return privacy.scrub(resolve("mode_button", poll.snap())[0].name)


# -- Deep Research -----------------------------------------------------------
def _wait_menu_item(poll: _Poll, cfg: StepConfig, key: str) -> List[Node]:
    def fail(c):
        try:
            resolve(key, c)
        except LocatorMiss as miss:
            raise StepError("DEEP_RESEARCH_TOGGLE_NOT_FOUND", {"waiting_for": key, "locator_miss": miss.payload})

    return poll.until(cfg.ui_timeout_s, lambda c: _try(c, key), key, fail)


def enable_deep_research(tree, locs: dict, cfg: StepConfig) -> None:
    doc = bind_conversation(tree, FRESH_URL)
    poll = _Poll(tree, locs, cfg, doc.id, FRESH_URL)
    ctx = poll.snap()
    tools = resolve("tools", ctx)[0]  # positive anchor for the dr_on absence
    if resolve("dr_on", ctx):
        return
    _act(tree, tools, ("expand", "invoke"), "tools")
    more = _wait_menu_item(poll, cfg, "more_tools")
    _act(tree, more[0], ("expand", "invoke"), "more_tools")
    item = _wait_menu_item(poll, cfg, "dr_item")[0]
    if not _checked(item):
        _act(tree, item, ("toggle", "invoke"), "dr_item")

    def fail(c):
        raise StepError("DEEP_RESEARCH_TOGGLE_NOT_FOUND", {"waiting_for": "dr_on"})

    poll.until(cfg.ui_timeout_s, lambda c: _absent(c, "dr_on", "tools") is False, "dr_on", fail)


# -- prompt ------------------------------------------------------------------
def enter_prompt(tree, locs: dict, cfg: StepConfig, doc: Node, text: str) -> None:
    poll = _Poll(tree, locs, cfg, doc.id, doc.value)
    ctx = poll.snap()
    prompt = resolve("prompt_input", ctx)[0]  # positive anchor: a miss raises, never "empty"
    if resolve("send", ctx):
        raise StepError("INPUT_NOT_EMPTY", {})
    tree.set_value(prompt.id, text)
    info = {"expected_sha256": _sha(text), "expected_len": len(text)}
    try:
        poll.until(cfg.ui_timeout_s, lambda c: _absent(c, "send", "prompt_input") is False, "send")
    except StepError as exc:
        if exc.code != "UI_TIMEOUT":
            raise
        tree.set_value(prompt.id, "")
        raise StepError("PROMPT_READBACK_MISMATCH", dict(info, reason="send_not_shown"))
    value = tree.get_value(prompt.id)
    if _sha(value) != info["expected_sha256"]:
        tree.set_value(prompt.id, "")
        raise StepError("PROMPT_READBACK_MISMATCH",
                        dict(info, reason="hash_mismatch", actual_sha256=_sha(value), actual_len=len(value)))


def _send(tree, ctx: LocatorContext) -> None:
    resolve("prompt_input", ctx)
    send = resolve("send", ctx)
    if not send:
        raise StepError("SEND_NOT_AVAILABLE", {})
    _act(tree, send[0], ("invoke",), "send")


def send_and_bind(tree, locs: dict, cfg: StepConfig, doc: Node) -> str:
    """Send on the fresh chat; returns the `/app/<id>` URL the same document gets."""
    poll = _Poll(tree, locs, cfg, doc.id)
    ctx = poll.snap()
    if ctx.doc.value != FRESH_URL:
        raise StepError("CONVERSATION_MISMATCH", {"reason": "not_fresh_chat", "expected": FRESH_URL})
    _send(tree, ctx)

    def bound(c):
        v = c.doc.value or ""
        if v == FRESH_URL:
            return None
        if CONVERSATION_RE.match(v):
            return v
        raise StepError("CONVERSATION_MISMATCH", {"reason": "unexpected_url", "actual": v})

    return privacy.scrub(poll.until(cfg.bind_timeout_s, bound, "conversation_url"))


# -- plan wait and countdown cancel ------------------------------------------
def _watch(tree, ctx: LocatorContext, prev_cards: int):
    cards = _cards(ctx)
    if len(cards) > prev_cards and cards[-1][1].enabled:
        return cards[-1][0]
    turns = _turns(ctx)
    if not turns:
        return None
    last = turns[-1]
    if _find_in(last, ctx, "plan_start"):
        return None
    cancel = _find_in(last, ctx, "plan_cancel_countdown")
    if cancel:
        # a countdown card whose start button we cannot recognise: stop it, then report the drift
        _try_act(tree, cancel[0], ("invoke",))
        _raise_turn_miss(ctx, last, "plan_start")
    text = _turn_text(last)
    if _phrase_hit(text, ctx.locs.get("safety_phrases")):
        raise StepError("SAFETY_BLOCKED", {})
    if _phrase_hit(text, ctx.locs.get("error_phrases")):
        raise StepError("GEMINI_ERROR_RESPONSE", {})
    return None


def _card_state(ctx: LocatorContext, turn_id: str) -> str:
    turn = _turn_by_id(ctx, turn_id)
    starts = _find_in(turn, ctx, "plan_start") if turn is not None else []
    start_ok = len(starts) == 1 and starts[0].enabled
    stop_absent = _absent(ctx, "stop_response", "prompt_input")
    if not start_ok and stop_absent is False:
        return "progress"
    return "ok" if start_ok and stop_absent is True else "unverified"


def _stop_unreviewed(tree, poll: _Poll, cfg: StepConfig, ctx: LocatorContext, method: Optional[str]) -> None:
    invoked = stopped = False
    stop = _try(ctx, "stop_response")
    if stop:
        invoked = _try_act(tree, stop[0], ("invoke",))
    if invoked:
        try:
            poll.until(cfg.cancel_verify_s, lambda c: _absent(c, "stop_response", "prompt_input") is True,
                       "stop_response_gone")
            stopped = True
        except StepError as exc:
            if exc.code != "UI_TIMEOUT":
                raise
    raise StepError("UNREVIEWED_START", {"cancel": method or "none", "stop_invoked": invoked, "stopped": stopped})


def wait_plan_and_cancel(tree, locs: dict, cfg: StepConfig, url: str, prev_cards: int) -> dict:
    doc = bind_conversation(tree, url)
    poll = _Poll(tree, locs, cfg, doc.id, url)

    def timeout(c):
        turns = _turns(c)
        last = turns[-1] if turns else None
        if last is not None and not _find_in(last, c, "plan_start") and (
                _find_in(last, c, "plan_edit") or _find_in(last, c, "plan_retry_without_dr")):
            _raise_turn_miss(c, last, "plan_start")
        raise StepError("PLAN_TIMEOUT", {"waited_s": round(cfg.card_timeout_s, 1), "cards": len(_cards(c))})

    holder = {}

    def watch(c):
        turn = _watch(tree, c, prev_cards)
        if turn is not None:
            holder["ctx"] = c
        return turn

    turn = poll.until(cfg.card_timeout_s, watch, "plan_card", timeout)
    ctx = holder["ctx"]
    # stop the countdown first, everything else afterwards
    method = None
    cancel = _find_in(turn, ctx, "plan_cancel_countdown")
    if cancel and _try_act(tree, cancel[0], ("invoke",)):
        method = "cancel_button"
    else:
        edit = _find_in(turn, ctx, "plan_edit")
        if edit and _try_act(tree, edit[0], ("invoke",)):
            method = "edit_fallback"
    count = len(_cards(ctx))
    verified = method is not None
    deadline = cfg.clock() + cfg.cancel_verify_s
    while True:
        last = poll.snap()
        state = _card_state(last, turn.id)
        if state == "progress":
            _stop_unreviewed(tree, poll, cfg, last, method)
        if state != "ok":
            verified = False
        if cfg.clock() >= deadline:
            break
        cfg.sleep(poll.interval())
    if not verified:
        raise StepError("COUNTDOWN_NOT_CANCELLED", {"cancel": method or "none"})
    if count != prev_cards + 1:
        raise StepError("PLAN_CARD_COUNT_MISMATCH", {"expected": prev_cards + 1, "found": count})
    card = _turn_by_id(last, turn.id)
    result = privacy.scrub({"cancel": method, "cards": count})
    result["plan_text"] = extract_plan_text(card, last.allowed)  # scrubbed line by line
    return result


def edit_plan(tree, locs: dict, cfg: StepConfig, url: str, feedback: str) -> dict:
    doc = bind_conversation(tree, url)
    poll = _Poll(tree, locs, cfg, doc.id, url)
    ctx = poll.snap()
    cards = _cards(ctx)
    if not cards:
        raise StepError("NO_PLAN_CARD", {})
    turn = cards[-1][0]
    edit = _find_in(turn, ctx, "plan_edit")
    if not edit:
        _raise_turn_miss(ctx, turn, "plan_edit")
    turns_before = len(_turns(ctx))
    _act(tree, edit[0], ("invoke",), "plan_edit")
    poll.until(cfg.turn_timeout_s,
               lambda c: len(_turns(c)) > turns_before and _absent(c, "stop_response", "prompt_input") is True,
               "canned_turn")
    enter_prompt(tree, locs, cfg, doc, feedback)
    _send(tree, poll.snap())
    return wait_plan_and_cancel(tree, locs, cfg, url, len(cards))

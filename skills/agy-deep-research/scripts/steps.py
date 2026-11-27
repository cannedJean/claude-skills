"""Plan/edit/approve step logic and the long research wait of the Gemini desktop UI Automation driver.

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
  `prev_cards` by exactly one. Its countdown is cancelled the moment it is seen
  (`취소`), then verified for `cancel_verify_s`: start still enabled, no
  `stop_response`, and at the end no `취소` left in the card. If that fails (or
  there is no usable `취소`), `plan_edit` is the second stop: Gemini's canned
  turn (a NEW non-card turn, which shows `stop_response` while generating) is
  waited out for `turn_timeout_s`, then the same verification runs again.
  Research progress (no enabled start in the card + `stop_response`) is stopped
  and reported as UNREVIEWED_START; a card first seen with its start disabled
  counts as a fired countdown. Any other failed check is
  COUNTDOWN_NOT_CANCELLED. A timeout, COUNTDOWN_NOT_CANCELLED or LocatorMiss
  while `stop_response` is present invokes `stop_response` first (details get
  stop_invoked/stopped). A BackendError escaping the plan wait or the edit first
  cancels the newest visible countdown and stops a running response (best effort,
  recorded in exc.stop_attempt), then propagates. While a response runs, a card count below
  `prev_cards` or a newest card without its start also means UNREVIEWED_START.
  Fail closed, never retried.
- Acts: only BackendError (pattern missing, element gone, COM/OS error) counts
  as "did not act" and falls through to the next stop; its type name is
  recorded (UI_ACTION_FAILED.error, COUNTDOWN_NOT_CANCELLED.act_errors).
  Programming errors propagate.
- Polling interval = max(cfg.poll_min_s, 2 x duration of the last snapshot).
- Every StepError.details and every returned value has passed privacy.scrub.
- approve (spec 11-9, 23): binds the job URL (with privacy allow-list check, else UNEXPECTED_SURFACE), refuses when
  research already started (stop_response, a research status, the done phrase, a report control, or any turn after
  the newest card turn: ALREADY_STARTED, never a second Invoke) and when the newest plan-control turn is not the
  newest card with an ENABLED plan_start, has a running countdown, or the plan-control turn count is not the reviewed
  one (PLAN_CARD_STALE). Then one Invoke of that start (no retry, `before_invoke` runs right before it) and a progress
  signal within approve_verify_s (else RESEARCH_NOT_STARTED).
- wait_research (spec 11-10, 21.3, 23.2/23.3): reads the tree only (never acts, never refreshes by itself).
  Every poll re-binds the single document whose URL equals the job URL (else CONVERSATION_MISMATCH), enforces the
  privacy allow-list (else UNEXPECTED_SURFACE), and resolves the positive anchor `prompt_input` (else UI_LOCATOR_MISS),
  so progress of another conversation never counts and label drift fails fast. Progress signature = (allowed node
  count, research status texts, thought titles, allowed text length); status texts and thought titles are taken from
  allowed nodes after the newest plan card, outside the report panel, chat markers excluded (thought titles only when
  a card exists). Completion = stop_response absent (positive anchor prompt_input) AND the done phrase in the last
  turn after the card AND >= 1 report control inside the report panel, held for complete_hold_s with a stable
  signature. After stall_s without a signature change the injected `recover(url) -> bool` (spec 11-12) is called at
  most once and only when an enabled Hyperlink inside a navigation landmark, not offscreen (nor any ancestor), whose
  URL equals the job URL exactly is in the tree now; otherwise RECOVERY_UNAVAILABLE (nothing refreshed). A second
  stall, a failed recovery (False or BackendError) -> RESEARCH_STALLED. max_wait_s is a separate hard cap
  (RESEARCH_TIMEOUT), with every sleep clamped to the remaining budget. A BackendError while reading the tree is
  retried up to backend_retries consecutive times, then re-raised; other errors propagate. Progress lines go to
  on_progress as "[Ns] Progress: <text>" (relay PROGRESS_RE), status and title scrubbed individually before joining.
  Wait errors carry longest_no_change_s, recoveries and last_status in their details.
- Node ids must stay stable for the same UI element across snapshots (the UIA
  backend uses runtime ids); the cancel verification tracks the card by id.

Error codes (StepError.code; "started?" = may research have started):
    CONVERSATION_MISMATCH  no/several documents with the expected URL, URL changed or document gone mid-step,
                           send on a non-fresh chat, unexpected URL after send. started? no (nothing sent or the
                           step stops before acting on the other document)
    INPUT_NOT_EMPTY        send button present before typing (input holds text). started? no
    PROMPT_READBACK_MISMATCH  typed value hash differs or send never appeared; input cleared, not sent. started? no
    SEND_NOT_AVAILABLE     no send button to invoke. started? no
    MODEL_NOT_AVAILABLE    no menu item matches (details.available lists the menu). started? no
    MODEL_AMBIGUOUS        several menu items match (details.matches). started? no
    MODEL_NOT_SELECTED     the chosen item is not checked after selection. started? no
    DEEP_RESEARCH_TOGGLE_NOT_FOUND  tools menu items or the Deep Research button did not appear. started? no
    UI_ACTION_FAILED       the node supports none of the patterns tried or the backend failed (details.error =
                           exception type name). started? no for menus/prompt; stops never raise it (they fall
                           through to the next stop)
    UI_TIMEOUT             a local UI reaction did not happen (details.waiting_for); in edit_plan stop_response is
                           invoked first if present. started? no (the plan card is not touched by these waits)
    NO_PLAN_CARD           edit_plan/approve found no plan card. started? no
    SAFETY_BLOCKED         the newest response holds a safety phrase. started? no
    GEMINI_ERROR_RESPONSE  the newest response holds an error phrase (never retried). started? no
    PLAN_TIMEOUT           no new card within card_timeout_s; stop_response invoked first if present. started?
                           possible (a card may have been missed) - check details.stop_invoked
    PLAN_CARD_COUNT_MISMATCH  more than one new card; every new card's cancel was invoked and no progress was
                           seen afterwards (else UNREVIEWED_START). started? unlikely
    PLAN_CARD_UNRECOGNISED a turn-scoped plan_start lookup unexpectedly matched (internal guard). started? possible
    UNREVIEWED_START       research progress seen; stop_response invoked (details.stopped). started? YES
    COUNTDOWN_NOT_CANCELLED  cancel and edit fallback could not be verified; stop_response invoked first if
                           present (details.stop_invoked, act_errors). started? possible (not seen yet)
    ALREADY_STARTED        approve: research already runs or ran in the bound conversation (details.signal or
                           details.reason); nothing invoked. started? YES (before this call)
    PLAN_CARD_STALE        approve: the newest plan-control turn has no enabled plan_start, has a running countdown,
                           or the plan-control turn count differs from expect_cards; nothing invoked. started? no
    RESEARCH_NOT_STARTED   approve: plan_start invoked once, no progress signal within approve_verify_s. started?
                           possible (the hidden window may lag) - collect later, never approve again
    RESEARCH_STALLED       wait: no progress for stall_s after the one recovery, or the recovery failed. started? YES
                           (research may still finish on the server - collect again later)
    RECOVERY_UNAVAILABLE   wait: no progress for stall_s and no visible return link / no recover callable; nothing was
                           refreshed. started? YES (collect again later)
    RESEARCH_TIMEOUT       wait: the max_wait_s hard cap passed (independent of the stall timer). started? YES
    UNEXPECTED_SURFACE     approve/wait: privacy region_check failed or the allow-list is empty (unreadable surface);
                           nothing invoked or refreshed. started? no (approve) / YES (wait)
LocatorMiss (UI_LOCATOR_MISS payload) is raised for label drift; during the plan wait it may carry stop_invoked.
"""
import hashlib
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

import locators
import privacy
import report_md
from locators import LocatorContext, LocatorMiss, resolve
from uitree import Node, PatternNotSupported

FRESH_URL = "https://gemini.google.com/app"
CONVERSATION_RE = re.compile(r"^https://gemini\.google\.com/app/[0-9a-f]{8,}$")
DEFAULT_CARD_PREFIX = "model-response-message-content"
# Errors a UiTree backend raises when an act cannot happen: pattern missing (PatternNotSupported),
# element gone (LookupError/KeyError, FakeTree raises KeyError), COM/OS failure (OSError; the UIA backend
# maps COMError onto an OSError subclass). Only these count as "did not act"; anything else is a bug and
# propagates.
BackendError = (PatternNotSupported, LookupError, OSError)
# fail-closed exits that invoke stop_response first when it is present (details get stop_invoked/stopped)
STOP_FIRST_CODES = ("PLAN_TIMEOUT", "UI_TIMEOUT", "COUNTDOWN_NOT_CANCELLED")


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
    stop_timeout_s: float = 300     # stop_response gone after invoking it
    approve_verify_s: float = 60    # progress signal after the plan_start Invoke (live: ~12 s)
    research_poll_s: float = 5      # research wait poll interval (spec 11-10)
    stall_s: float = 3600           # no progress signature change -> the one recovery (spec 23.2)
    complete_hold_s: float = 20     # the completion combination must hold this long (spec 23.3)
    max_wait_s: float = 4 * 3600    # hard cap of the research wait, separate from the stall timer
    backend_retries: int = 3        # consecutive BackendErrors tolerated while reading the tree in the wait


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
    """Act with the first supported pattern; backend errors (BackendError) try the
    next pattern, then UI_ACTION_FAILED with the last error's type name.
    Programming errors propagate."""
    error = "no_pattern"
    for p in prefs:
        if p in node.patterns:
            try:
                tree.act(node.id, p)
                return p
            except BackendError as exc:
                error = type(exc).__name__
    raise StepError("UI_ACTION_FAILED", {"locator": key, "patterns": sorted(node.patterns), "error": error})


def _try_act(tree, node: Node, prefs: Tuple[str, ...], key: str = "", errors: Optional[list] = None) -> bool:
    """Best-effort act for stops (cancel, edit, stop_response): a backend failure
    means "did not act" (recorded in `errors`) so the next stop is tried."""
    try:
        _act(tree, node, prefs, key)
        return True
    except StepError as exc:
        if exc.code != "UI_ACTION_FAILED":
            raise
        if errors is not None:
            errors.append({"locator": key, "error": exc.details.get("error", "no_pattern")})
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
    """The single document whose URL equals `url` exactly.

    WARNING: the returned Node is the whole document, sidebar (account, chat
    titles) included. Callers must never serialize or log it; only names of
    privacy.allowed_ids(doc) may be read out, and only through privacy.scrub.
    """
    docs =[d for d in tree.documents() if d.value == url]
    if len(docs) != 1:
        raise StepError("CONVERSATION_MISMATCH", {"expected": url, "found": len(docs)})
    return docs[0]


def ensure_fresh_chat(tree, locs: dict, cfg: StepConfig) -> Node:
    """Make the conversation document a fresh chat (`/app`) and return it.

    WARNING: the returned Node includes the sidebar (account, chat titles);
    callers must never serialize or log it (use privacy.allowed_ids + scrub).
    """
    docs =[d for d in tree.documents()
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
def _after_card_turns(ctx: LocatorContext, card_id: str) -> Optional[List[Node]]:
    """Non-card turns after the card (Gemini's canned turn after plan_edit); None if the card is gone."""
    turns = _turns(ctx)
    idx = next((i for i, t in enumerate(turns) if t.id == card_id), None)
    if idx is None:
        return None
    return [t for t in turns[idx + 1:] if not _find_in(t, ctx, "plan_start")]


def _card_state(ctx: LocatorContext, turn_id: str) -> str:
    """progress | ok | canned (a new non-card turn is generating) | unverified."""
    turn = _turn_by_id(ctx, turn_id)
    starts = _find_in(turn, ctx, "plan_start") if turn is not None else []
    start_ok = len(starts) == 1 and starts[0].enabled
    stop_absent = _absent(ctx, "stop_response", "prompt_input")
    if not start_ok and stop_absent is False:
        return "progress"  # only stop_response with the card's start gone/disabled means research
    if start_ok and stop_absent is True:
        return "ok"
    if start_ok and stop_absent is False and _after_card_turns(ctx, turn_id):
        return "canned"
    return "unverified"


def _cancel_visible(ctx: LocatorContext, turn_id: str) -> bool:
    turn = _turn_by_id(ctx, turn_id)
    return turn is not None and bool(_find_in(turn, ctx, "plan_cancel_countdown"))


def _stop(tree, poll: _Poll, cfg: StepConfig, ctx: LocatorContext) -> Optional[Tuple[bool, bool]]:
    """Invoke stop_response if present; (invoked, stopped) or None when there is none."""
    stop = _try(ctx, "stop_response")
    if not stop:
        return None
    if not _try_act(tree, stop[0], ("invoke",), "stop_response"):
        return False, False
    try:
        poll.until(cfg.stop_timeout_s, lambda c: _absent(c, "stop_response", "prompt_input") is True,
                   "stop_response_gone")
        return True, True
    except StepError as exc:
        if exc.code != "UI_TIMEOUT":
            raise
        return True, False


def _unreviewed(tree, poll: _Poll, cfg: StepConfig, ctx: LocatorContext, method: Optional[str]) -> None:
    invoked, stopped = _stop(tree, poll, cfg, ctx) or (False, False)
    raise StepError("UNREVIEWED_START", {"cancel": method or "none", "stop_invoked": invoked, "stopped": stopped})


def _stop_on_failure(tree, poll: _Poll, cfg: StepConfig, exc: Exception) -> None:
    """Before a timeout, an unverified cancel or a LocatorMiss leaves the step: stop a
    running response (harmless if it was only Gemini's canned turn)."""
    if isinstance(exc, StepError) and exc.code not in STOP_FIRST_CODES:
        return
    if isinstance(exc, BackendError):
        _stop_on_backend_error(tree, poll, cfg, exc)
        return
    try:
        res = _stop(tree, poll, cfg, poll.snap())
    except StepError:
        return
    if res is None:
        return
    target = exc.payload if isinstance(exc, LocatorMiss) else exc.details
    target["stop_invoked"], target["stopped"] = res


def _stop_on_backend_error(tree, poll: _Poll, cfg: StepConfig, exc: Exception) -> None:
    """A backend failure (pattern missing, element gone, COM/OS error) in the middle of the plan wait: best effort,
    cancel the newest visible countdown, then stop a running response, so the step never leaves a countdown running.
    What was tried is recorded on the exception (`exc.stop_attempt`); the caller re-raises the original error."""
    attempt = {"cancel_invoked": False, "stop_invoked": False, "stopped": False, "error": None}
    try:
        ctx = poll.snap()
        turns = [t for t in _turns(ctx) if _find_in(t, ctx, "plan_cancel_countdown")]
        if turns:   # the newest countdown only: an older card's button is never the one running
            _cancel_all(tree, ctx, turns[-1:])
            attempt["cancel_invoked"] = True
        res = _stop(tree, poll, cfg, poll.snap())
        if res is not None:
            attempt["stop_invoked"], attempt["stopped"] = res
    except (StepError, LocatorMiss) + BackendError as again:  # the backend is still failing: report, keep the original
        attempt["error"] = type(again).__name__
    try:
        exc.stop_attempt = attempt
    except AttributeError:
        pass


def _cancel_all(tree, ctx: LocatorContext, turns: List[Node]) -> None:
    for turn in turns:
        for b in _find_in(turn, ctx, "plan_cancel_countdown"):
            _try_act(tree, b, ("invoke",), "plan_cancel_countdown")


def _start_live(ctx: LocatorContext, turn_id: str) -> bool:
    turn = _turn_by_id(ctx, turn_id)
    starts = _find_in(turn, ctx, "plan_start") if turn is not None else []
    return len(starts) == 1 and starts[0].enabled


def _watch(tree, poll: _Poll, cfg: StepConfig, ctx: LocatorContext, prev_cards: int):
    cards = _cards(ctx)
    new = cards[prev_cards:]
    if len(new) > 1:
        _cancel_all(tree, ctx, [t for t, _s in reversed(new)])
        after = poll.snap()
        if (_absent(after, "stop_response", "prompt_input") is False
                or not all(_start_live(after, t.id) for t, _s in new)):
            _unreviewed(tree, poll, cfg, after, "cancel_button")
        raise StepError("PLAN_CARD_COUNT_MISMATCH", {"expected": prev_cards + 1, "found": len(cards)})
    if new:
        turn, start = new[0]
        if start.enabled:
            return turn
        # first seen with its start disabled: the countdown fired (or the card was missed)
        cancel = _find_in(turn, ctx, "plan_cancel_countdown")
        method = ("cancel_button" if cancel and _try_act(tree, cancel[0], ("invoke",), "plan_cancel_countdown")
                  else None)
        _unreviewed(tree, poll, cfg, ctx, method)
    turns = _turns(ctx)
    stop_running = _absent(ctx, "stop_response", "prompt_input") is False
    if stop_running and len(cards) < prev_cards:
        _unreviewed(tree, poll, cfg, ctx, None)  # an earlier card lost its start while a response runs
    if not turns:
        return None
    last = turns[-1]
    if _find_in(last, ctx, "plan_start"):
        return None
    if _find_in(last, ctx, "plan_cancel_countdown"):
        # a countdown card whose start button we cannot recognise: stop it, then report the drift
        _cancel_all(tree, ctx, [last])
        _raise_turn_miss(ctx, last, "plan_start")
    if stop_running and (_find_in(last, ctx, "plan_edit") or _find_in(last, ctx, "plan_retry_without_dr")):
        _unreviewed(tree, poll, cfg, ctx, None)  # the newest card has no start left and a response runs
    text = _turn_text(last)
    if _phrase_hit(text, ctx.locs.get("safety_phrases")):
        raise StepError("SAFETY_BLOCKED", {})
    if _phrase_hit(text, ctx.locs.get("error_phrases")):
        raise StepError("GEMINI_ERROR_RESPONSE", {})
    return None


def _verify(tree, poll: _Poll, cfg: StepConfig, turn_id: str, method: str) -> Tuple[bool, LocatorContext]:
    """Every snapshot for cancel_verify_s: start enabled, no stop_response; at the end no 취소."""
    ok = True
    deadline = cfg.clock() + cfg.cancel_verify_s
    while True:
        c = poll.snap()
        state = _card_state(c, turn_id)
        if state == "progress":
            _unreviewed(tree, poll, cfg, c, method)
        if state != "ok":
            ok = False
        if cfg.clock() >= deadline:
            return ok and not _cancel_visible(c, turn_id), c
        cfg.sleep(poll.interval())


def _wait_canned(tree, poll: _Poll, cfg: StepConfig, turn_id: str, method: str) -> None:
    """After plan_edit: wait (turn_timeout_s) for the canned turn to finish; the
    verification that follows decides, so a timeout here just returns."""
    deadline = cfg.clock() + cfg.turn_timeout_s
    while True:
        c = poll.snap()
        if _card_state(c, turn_id) == "progress":
            _unreviewed(tree, poll, cfg, c, method)
        if _after_card_turns(c, turn_id) and _absent(c, "stop_response", "prompt_input") is True:
            return
        if cfg.clock() >= deadline:
            return
        cfg.sleep(poll.interval())


def _plan(tree, poll: _Poll, cfg: StepConfig, prev_cards: int) -> dict:
    def timeout(c):
        turns = _turns(c)
        last = turns[-1] if turns else None
        if last is not None and not _find_in(last, c, "plan_start") and (
                _find_in(last, c, "plan_edit") or _find_in(last, c, "plan_retry_without_dr")):
            _raise_turn_miss(c, last, "plan_start")
        raise StepError("PLAN_TIMEOUT", {"waited_s": round(cfg.card_timeout_s, 1), "cards": len(_cards(c))})

    holder = {}

    def watch(c):
        turn = _watch(tree, poll, cfg, c, prev_cards)
        if turn is not None:
            holder["ctx"] = c
        return turn

    turn = poll.until(cfg.card_timeout_s, watch, "plan_card", timeout)
    last = holder["ctx"]
    method = None
    ok = False
    errors: list = []
    # first stop: the countdown cancel, the moment the card is seen
    cancel = _find_in(turn, last, "plan_cancel_countdown")
    if cancel and _try_act(tree, cancel[0], ("invoke",), "plan_cancel_countdown", errors):
        method = "cancel_button"
        ok, last = _verify(tree, poll, cfg, turn.id, method)
    if not ok:
        # second stop: plan_edit (no/failed cancel, or the cancel had no effect)
        card = _turn_by_id(last, turn.id)
        edit = _find_in(card, last, "plan_edit") if card is not None else []
        if edit and _try_act(tree, edit[0], ("invoke",), "plan_edit", errors):
            method = "edit_fallback"
            _wait_canned(tree, poll, cfg, turn.id, method)
            ok, last = _verify(tree, poll, cfg, turn.id, method)
        elif method is None:
            _verify(tree, poll, cfg, turn.id, "none")  # nothing could stop it: still watch for progress
    if not ok:
        raise StepError("COUNTDOWN_NOT_CANCELLED", {"cancel": method or "none", "act_errors": errors})
    card = _turn_by_id(last, turn.id)
    result = privacy.scrub({"cancel": method, "cards": prev_cards + 1})
    result["plan_text"] = extract_plan_text(card, last.allowed)  # scrubbed line by line
    return result


def wait_plan_and_cancel(tree, locs: dict, cfg: StepConfig, url: str, prev_cards: int) -> dict:
    doc = bind_conversation(tree, url)
    poll = _Poll(tree, locs, cfg, doc.id, url)
    try:
        return _plan(tree, poll, cfg, prev_cards)
    except (StepError, LocatorMiss) + BackendError as exc:
        _stop_on_failure(tree, poll, cfg, exc)
        raise


def probe_conversation(tree, locs: dict, cfg: StepConfig, url: str) -> dict:
    """Read-only look at the bound conversation (never acts): {"start_enabled": any plan card has an enabled
    `plan_start` (fail closed: an older card still startable counts), "plan_cards": number of plan cards,
    "url": the document's URL}."""
    doc = bind_conversation(tree, url)
    ctx = _Poll(tree, locs, cfg, doc.id, url).snap()
    cards = _cards(ctx)
    return {"start_enabled": any(start.enabled for _turn, start in cards), "plan_cards": len(cards),
            "url": privacy.scrub(ctx.doc.value)}


def edit_plan(tree, locs: dict, cfg: StepConfig, url: str, feedback: str,
              prev_cancel: Optional[str] = None) -> dict:
    """`prev_cancel` = the `cancel` field of the previous plan result. Only after an
    "edit_fallback" result (plan_edit already invoked, canned turn present) is
    plan_edit not invoked again."""
    doc = bind_conversation(tree, url)
    poll = _Poll(tree, locs, cfg, doc.id, url)
    try:
        ctx = poll.snap()
        cards = _cards(ctx)
        if not cards:
            raise StepError("NO_PLAN_CARD", {})
        turn = cards[-1][0]
        before = _after_card_turns(ctx, turn.id) or []
        need = len(before) + 1
        if prev_cancel == "edit_fallback" and before:
            need = len(before)  # the canned turn of the edit fallback is already there
        else:
            edit = _find_in(turn, ctx, "plan_edit")
            if not edit:
                _raise_turn_miss(ctx, turn, "plan_edit")
            _act(tree, edit[0], ("invoke",), "plan_edit")
        poll.until(cfg.turn_timeout_s,
                   lambda c: len(_after_card_turns(c, turn.id) or []) >= need
                   and _absent(c, "stop_response", "prompt_input") is True,
                   "canned_turn")
        enter_prompt(tree, locs, cfg, doc, feedback)
        _send(tree, poll.snap())
    except (StepError, LocatorMiss) + BackendError as exc:
        _stop_on_failure(tree, poll, cfg, exc)
        raise
    return wait_plan_and_cancel(tree, locs, cfg, url, len(cards))


# -- approve and the research wait (spec 11-9/10/12, 21.3, 23) ----------------
@dataclass
class ApproveResult:
    started: bool
    signal: str          # stop_response | research_status | report_done
    cards: int           # plan-control turns in the conversation (the invoked one is the newest)
    waited_s: float      # from the Invoke to the progress signal
    last_status: str = ""


@dataclass
class WaitResult:
    complete: bool
    longest_no_change_s: float
    recoveries: list     # [{"at", "why", "no_change_s", "ok"(, "error")}]
    last_status: str
    waited_s: float = 0.0


@dataclass
class _Signals:
    sig: tuple
    stop_absent: Optional[bool]   # True/False only with the prompt_input anchor (see _absent)
    statuses: List[str]
    titles: List[str]
    done: bool
    controls: bool
    error: bool
    text: str                     # progress text (status - thought title), scrubbed

    @property
    def started(self) -> Optional[str]:
        if self.stop_absent is False:
            return "stop_response"
        if self.statuses or self.titles:
            return "research_status"
        if self.done or self.controls:
            return "report_done"
        return None

    @property
    def complete(self) -> bool:
        return self.stop_absent is True and self.done and self.controls


def _is_chat_marker(name: str) -> bool:
    key = _fold(name)
    return any(key.startswith(_fold(m)) for m in report_md.CHAT_TITLES + report_md.CHAT_TURN_MARKERS)


def _signals(ctx: LocatorContext) -> _Signals:
    """Progress and completion signals of one snapshot (reads only; names of allowed nodes only)."""
    lc = ctx.locs["locators"]
    # boundary: the newest turn holding any plan control (a card, also one without a start); plan-control turns
    # themselves never contribute (their plan headings are not thought titles)
    turns = _turns(ctx)
    plan_turns = {t.id for t in turns if any(_find_in(t, ctx, k) for k in locators.PLAN_CONTROLS)}
    card = next((t for t in reversed(turns) if t.id in plan_turns), None)
    panel_nodes = [n for root in locators._scope_roots("report_panel", ctx, None) for n, _ in root.walk()]
    panel = {n.id for n in panel_nodes}
    statuses, titles = [], []
    seen_card = card is None
    for n, anc in ctx.doc.walk():
        if n.id in plan_turns or any(a.id in plan_turns for a in anc):
            seen_card = seen_card or (card is not None and (n.id == card.id or any(a.id == card.id for a in anc)))
            continue
        if not seen_card or n.id not in ctx.allowed or n.id in panel:
            continue
        name = _norm(n.name)
        if not name:
            continue
        if locators._matches(n, lc["research_status"]):
            statuses.append(name)
        elif card is not None and locators._matches(n, lc["research_thought_title"]) and not _is_chat_marker(name):
            titles.append(name)
    allowed = [n for n, _ in ctx.doc.walk() if n.id in ctx.allowed]
    sig = (len(allowed), tuple(statuses), tuple(titles), sum(len(_norm(n.name)) for n in allowed))
    last = turns[-1] if turns else None
    after = last is not None and (card is None or last.id != card.id) and not _find_in(last, ctx, "plan_start")
    done = after and any(n.id in ctx.allowed and locators._matches(n, lc["report_done_phrase"])
                         for n, _ in last.walk())
    error = after and _phrase_hit(_turn_text(last), ctx.locs.get("error_phrases"))
    # = resolve() of the report_panel-scoped (optional) controls, with the panel found once
    controls = any(locators._matches(n, lc[k]) for n in panel_nodes for k in locators.REPORT_CONTROLS)
    parts = [privacy.scrub(x) for x in (statuses[-1] if statuses else "", titles[-1] if titles else "") if x]
    text = " - ".join(parts)
    return _Signals(sig, _absent(ctx, "stop_response", "prompt_input"), statuses, titles, bool(done), controls,
                    bool(error), text)


class _BoundReader:
    """Snapshots of the single document whose URL equals `url`, re-bound on every read (so another
    conversation is never read); fails closed with UNEXPECTED_SURFACE when the privacy allow-list is invalid or
    empty. BackendErrors are retried up to cfg.backend_retries consecutive times."""

    def __init__(self, tree, locs: dict, cfg: StepConfig, url: str, poll_s: Optional[float] = None):
        self.tree, self.locs, self.cfg, self.url = tree, locs, cfg, url
        self.poll_s = cfg.research_poll_s if poll_s is None else poll_s
        self.last_s = 0.0

    def _read(self) -> LocatorContext:
        docs = [d for d in self.tree.documents() if d.value == self.url]
        if len(docs) != 1:
            raise StepError("CONVERSATION_MISMATCH", {"reason": "not_bound", "expected": self.url, "found": len(docs)})
        doc = self.tree.snapshot(docs[0].id)
        if doc.value != self.url:
            raise StepError("CONVERSATION_MISMATCH", {"reason": "url_changed", "expected": self.url,
                                                      "actual": doc.value})
        ok, reason = privacy.region_check(doc)
        allowed = privacy.allowed_ids(doc) if ok else set()
        if not ok or not allowed:
            raise StepError("UNEXPECTED_SURFACE", {"reason": reason if not ok else "empty_allowed"})
        return LocatorContext(self.tree, doc, self.locs, allowed)

    def interval(self) -> float:
        return max(self.poll_s, 2.0 * self.last_s)

    def snap(self, deadline: Optional[float] = None) -> LocatorContext:
        fails = 0
        while True:
            t0 = self.cfg.clock()
            try:
                ctx = self._read()
                self.last_s = max(0.0, self.cfg.clock() - t0)
                return ctx
            except BackendError:
                fails += 1
                if fails > self.cfg.backend_retries:
                    raise
                delay = self.interval()
                if deadline is not None:
                    rem = deadline - self.cfg.clock()
                    if rem <= 0:
                        continue
                    delay = min(delay, rem)
                self.cfg.sleep(delay)


def approve(tree, locs: dict, cfg: StepConfig, url: str, expect_cards: Optional[int] = None,
            before_invoke: Optional[Callable[[], None]] = None) -> ApproveResult:
    """Start the research of the reviewed plan: one Invoke of the newest enabled plan_start (rules in the module
    doc). `expect_cards` = the number of plan-control turns at the end of the plan/edit step; `before_invoke`
    runs right before the Invoke (the driver prints the relay confirm line there)."""
    reader = _BoundReader(tree, locs, cfg, url, poll_s=cfg.poll_min_s)
    ctx = reader.snap()
    resolve("prompt_input", ctx)   # positive anchor: without it a missing stop_response proves nothing
    sigs = _signals(ctx)
    if sigs.started:
        raise StepError("ALREADY_STARTED", {"signal": sigs.started, "last_status": sigs.text})
    turns = _turns(ctx)
    controls = [t for t in turns if any(_find_in(t, ctx, k) for k in locators.PLAN_CONTROLS)]
    if not controls:
        raise StepError("NO_PLAN_CARD", {})
    if turns[-1].id != controls[-1].id:
        raise StepError("ALREADY_STARTED", {"reason": "turn_after_card"})
    cards = _cards(ctx)
    if not cards or controls[-1].id != cards[-1][0].id or not cards[-1][1].enabled:
        raise StepError("PLAN_CARD_STALE", {"reason": "newest_card_not_startable", "cards": len(controls)})
    turn, start = cards[-1]
    if expect_cards is not None and len(controls) != expect_cards:
        raise StepError("PLAN_CARD_STALE", {"reason": "card_count", "expected": expect_cards, "found": len(controls)})
    if _find_in(turn, ctx, "plan_cancel_countdown"):
        raise StepError("PLAN_CARD_STALE", {"reason": "countdown_running", "cards": len(controls)})
    if before_invoke is not None:
        before_invoke()
    _act(tree, start, ("invoke",), "plan_start")   # the only Invoke: never repeated, whatever follows
    t0 = cfg.clock()
    deadline = t0 + cfg.approve_verify_s
    while True:
        rem = deadline - cfg.clock()
        if rem <= 0:
            raise StepError("RESEARCH_NOT_STARTED", {"waited_s": round(cfg.approve_verify_s, 1)})
        cfg.sleep(min(reader.interval(), rem))
        c = reader.snap(deadline=deadline)
        sigs = _signals(c)
        if sigs.started:
            return ApproveResult(True, sigs.started, len(controls), round(cfg.clock() - t0, 1), sigs.text)
        if sigs.error and sigs.stop_absent is True:
            raise StepError("GEMINI_ERROR_RESPONSE", {})
        if cfg.clock() >= deadline:
            raise StepError("RESEARCH_NOT_STARTED", {"waited_s": round(cfg.approve_verify_s, 1)})


def _return_link_visible(ctx: LocatorContext, url: str) -> bool:
    """An enabled Hyperlink inside a navigation landmark (the sidebar), not offscreen (nor any ancestor),
    whose URL equals the job URL exactly. Matched by URL value only; its name is never read."""
    for n, anc in ctx.doc.walk():
        if n.ct != "Hyperlink" or not n.enabled or n.offscreen or any(a.offscreen for a in anc):
            continue
        target = n.value or str((n.aria or {}).get("href") or "")
        if target == url and any((a.role or "").strip().lower() == "navigation" for a in anc):
            return True
    return False


def wait_research(tree, locs: dict, cfg: StepConfig, url: str,
                  on_progress: Optional[Callable[[str], None]] = None,
                  recover: Optional[Callable[[str], bool]] = None) -> WaitResult:
    """Wait for the research of the bound conversation to complete (rules in the module doc). Never acts itself;
    `recover(url)` (spec 11-12) is the only thing that may refresh, called at most once."""
    reader = _BoundReader(tree, locs, cfg, url)
    start = cfg.clock()
    deadline = start + cfg.max_wait_s
    last_sig, last_change, longest = None, start, 0.0
    last_text, done_since, recovered = "", None, False
    recoveries: list = []

    def info(**extra) -> dict:
        return dict(extra, longest_no_change_s=round(longest, 1), recoveries=list(recoveries),
                    last_status=last_text, waited_s=round(cfg.clock() - start, 1))

    while True:
        ctx = reader.snap(deadline=deadline)
        resolve("prompt_input", ctx)   # positive anchor: label drift fails fast (UI_LOCATOR_MISS)
        now = cfg.clock()
        sigs = _signals(ctx)
        changed = sigs.sig != last_sig
        if last_sig is not None:
            longest = max(longest, now - last_change)
        if changed:
            last_sig, last_change = sigs.sig, now
        if sigs.text and sigs.text != last_text:
            last_text = sigs.text
            if on_progress is not None:
                on_progress("[%ds] Progress: %s" % (int(now - start), sigs.text))
        if sigs.error and sigs.stop_absent is True:
            raise StepError("GEMINI_ERROR_RESPONSE", info())
        if sigs.complete:
            if done_since is None or changed:
                done_since = now
            if now - done_since >= cfg.complete_hold_s:
                return WaitResult(True, round(longest, 1), recoveries, last_text, round(now - start, 1))
        else:
            done_since = None
        if now >= deadline:
            raise StepError("RESEARCH_TIMEOUT", info(max_wait_s=round(cfg.max_wait_s, 1)))
        if now - last_change >= cfg.stall_s:
            if recovered:
                raise StepError("RESEARCH_STALLED", info(reason="no_change_after_recovery"))
            if recover is None:
                raise StepError("RECOVERY_UNAVAILABLE", info(reason="no_recover"))
            if not _return_link_visible(ctx, url):
                raise StepError("RECOVERY_UNAVAILABLE", info(reason="return_link_not_visible"))
            recovered = True
            entry = {"at": round(now - start, 1), "why": "no_change", "no_change_s": round(now - last_change, 1),
                     "ok": False}
            recoveries.append(entry)
            try:
                entry["ok"] = bool(recover(url))
            except BackendError as exc:
                entry["error"] = type(exc).__name__
            if not entry["ok"]:
                raise StepError("RESEARCH_STALLED", info(reason="recovery_failed"))
            last_sig, last_change = None, cfg.clock()
        rem = deadline - cfg.clock()
        if rem <= 0:
            raise StepError("RESEARCH_TIMEOUT", info(max_wait_s=round(cfg.max_wait_s, 1)))
        cfg.sleep(min(reader.interval(), rem))

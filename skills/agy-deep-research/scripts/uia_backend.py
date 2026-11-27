"""COM IUIAutomation backend for the UiTree protocol (Windows, comtypes==1.4.11).

Imports on any OS: `comtypes` is imported only inside `load_com()`. All logic lives in
`UiaTree`, written against the small `UiaCom` interface so it is unit-tested with a fake;
`ComtypesUia` is the thin real implementation and is exercised only in an approved live check.

Live-safety: `load_com()` and every `ComtypesUia` call refuse (`HostRefused("sandbox")`) when
AGY_DR_TEST_SANDBOX=1, so no test can create an IUIAutomation object or touch a real element.

Order requirement (measured): the driver thread must `SetThreadDesktop` onto the tool's hidden
desktop BEFORE calling `load_com()`; `desktop_root()` is then that desktop's UIA root.

Node ids: the UIA RuntimeId joined with "." (unique and stable for an element). A RuntimeId that is
missing, empty or shared by several elements of one snapshot is disambiguated as
`<rid|norid>~<anchor>:<index path>` (anchor = the snapshot root's id), so ids are never empty and
never duplicated (privacy fails closed on either).

Errors (match steps.BackendError): pattern missing -> PatternNotSupported; element gone or unknown
id -> KeyError (ElementGone); other COM failure -> UiaComError (an OSError).
"""
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from uitree import Node, PATTERNS, PatternNotSupported, UiTree

try:
    from typing import Protocol
except ImportError:  # pragma: no cover
    Protocol = object  # type: ignore

SETUP_HINT = ("UI Automation needs the driver venv with comtypes==1.4.11: run "
              "`python scripts/deep_research.py setup` (Windows only)")
WINDOW_CLASS = "Chrome_WidgetWin_1"
DOCUMENT_AUTOMATION_ID = "RootWebArea"

PROPERTY_IDS = {
    "RuntimeId": 30000, "ProcessId": 30002, "ControlType": 30003, "Name": 30005,
    "IsEnabled": 30010, "AutomationId": 30011, "ClassName": 30012, "IsOffscreen": 30022,
    "IsExpandCollapsePatternAvailable": 30028, "IsInvokePatternAvailable": 30031,
    "IsTogglePatternAvailable": 30041, "IsValuePatternAvailable": 30043, "ValueValue": 30045,
    "IsLegacyIAccessiblePatternAvailable": 30090, "AriaRole": 30101, "AriaProperties": 30102,
    "HeadingLevel": 30173,
}
NODE_PROPS = ("RuntimeId", "ProcessId", "ControlType", "Name", "IsEnabled", "AutomationId", "IsOffscreen",
              "AriaRole", "AriaProperties", "HeadingLevel", "ValueValue", "IsInvokePatternAvailable",
              "IsTogglePatternAvailable", "IsExpandCollapsePatternAvailable",
              "IsLegacyIAccessiblePatternAvailable", "IsValuePatternAvailable")
# UiaCom pattern name -> (pattern id, comtypes interface name)
PATTERN_IDS = {
    "invoke": (10000, "IUIAutomationInvokePattern"),
    "value": (10002, "IUIAutomationValuePattern"),
    "expand_collapse": (10005, "IUIAutomationExpandCollapsePattern"),
    "toggle": (10015, "IUIAutomationTogglePattern"),
    "legacy": (10018, "IUIAutomationLegacyIAccessiblePattern"),
}
# UiTree act pattern -> (UiaCom pattern name, method, availability property)
ACTS = {
    "invoke": ("invoke", "Invoke", "IsInvokePatternAvailable"),
    "toggle": ("toggle", "Toggle", "IsTogglePatternAvailable"),
    "expand": ("expand_collapse", "Expand", "IsExpandCollapsePatternAvailable"),
    "collapse": ("expand_collapse", "Collapse", "IsExpandCollapsePatternAvailable"),
    "default_action": ("legacy", "DoDefaultAction", "IsLegacyIAccessiblePatternAvailable"),
}
assert set(ACTS) == set(PATTERNS)
CONTROL_TYPES = {
    50000: "Button", 50001: "Calendar", 50002: "CheckBox", 50003: "ComboBox", 50004: "Edit",
    50005: "Hyperlink", 50006: "Image", 50007: "ListItem", 50008: "List", 50009: "Menu",
    50010: "MenuBar", 50011: "MenuItem", 50012: "ProgressBar", 50013: "RadioButton",
    50014: "ScrollBar", 50015: "Slider", 50016: "Spinner", 50017: "StatusBar", 50018: "Tab",
    50019: "TabItem", 50020: "Text", 50021: "ToolBar", 50022: "ToolTip", 50023: "Tree",
    50024: "TreeItem", 50025: "Custom", 50026: "Group", 50027: "Thumb", 50028: "DataGrid",
    50029: "DataItem", 50030: "Document", 50031: "SplitButton", 50032: "Window", 50033: "Pane",
    50034: "Header", 50035: "HeaderItem", 50036: "Table", 50037: "TitleBar", 50038: "Separator",
    50039: "SemanticZoom", 50040: "AppBar",
}
HEADING_NONE = 80050  # HeadingLevel_None; 80051..80056 = H1..H6
TREESCOPE_ELEMENT, TREESCOPE_CHILDREN, TREESCOPE_SUBTREE = 1, 2, 7
UIA_E_ELEMENTNOTAVAILABLE = -2147220991  # 0x80040201


class UiaUnavailable(RuntimeError):
    """UI Automation cannot be used here (`reason`: platform | comtypes); `hint` says how to fix it."""

    def __init__(self, reason: str, hint: str = SETUP_HINT, detail: str = ""):
        super().__init__("%s: %s%s" % (reason, hint, (" (%s)" % detail) if detail else ""))
        self.reason, self.hint, self.detail = reason, hint, detail


class HostRefused(RuntimeError):
    """A real COM call was refused (`reason` "sandbox": AGY_DR_TEST_SANDBOX=1)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class ElementGone(KeyError):
    """The UIA element no longer exists (UIA_E_ELEMENTNOTAVAILABLE) or the id is unknown."""


class UiaComError(OSError):
    """Any other COM failure of a UIA call."""


def _refuse_in_sandbox() -> None:
    if os.environ.get("AGY_DR_TEST_SANDBOX") == "1":
        raise HostRefused("sandbox")


@dataclass
class CachedElement:
    """One element of a cached snapshot: its live handle, cached properties by name, cached children."""
    element: Any
    props: Dict[str, Any] = field(default_factory=dict)
    children: List["CachedElement"] = field(default_factory=list)


class UiaCom(Protocol):
    def root(self) -> Any:
        """UIA root element of the calling thread's desktop."""

    def find_windows(self, class_name: Optional[str]) -> List[Any]:
        """Top-level children of root() with this ClassName (None = all)."""

    def find_by_automation_id(self, element: Any, automation_id: str) -> List[Any]:
        """Elements of `element`'s subtree (itself included) with this AutomationId."""

    def build_cache(self, element: Any, props, subtree: bool = True) -> CachedElement: ...

    def pattern(self, element: Any, name: str) -> Any:
        """Pattern object for `name` in PATTERN_IDS, or None if the element does not support it."""

    def runtime_id(self, element: Any) -> Optional[Tuple[int, ...]]: ...

    def process_id(self, element: Any) -> int: ...


# -- property mapping ----------------------------------------------------------
def _str(v) -> str:
    return v if isinstance(v, str) else ""


def _bool(v, default: bool) -> bool:
    return bool(v) if isinstance(v, (bool, int)) else default


def control_type_name(v) -> str:
    if isinstance(v, bool) or not isinstance(v, int):
        return "Unknown"
    return CONTROL_TYPES.get(v, str(v))


def heading_level(v) -> Optional[int]:
    if isinstance(v, bool) or not isinstance(v, int):
        return None
    level = v - HEADING_NONE
    return level if 1 <= level <= 6 else None


def parse_aria_properties(raw) -> dict:
    """`k=v;k2=v2` (UIA escapes `;`, `=` and `\\` with a backslash) -> {k: v}; keys/values stripped."""
    out: Dict[str, str] = {}
    if not isinstance(raw, str):
        return out
    pairs, cur, esc = [], [], False
    for ch in raw:
        if esc:
            cur.append("\x00" + ch)  # escaped char: never a separator
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ";":
            pairs.append(cur)
            cur = []
        else:
            cur.append(ch)
    if esc:
        cur.append("\x00\\")
    pairs.append(cur)
    for toks in pairs:
        try:
            eq = toks.index("=")
        except ValueError:
            eq = len(toks)
        key = "".join(t[-1] for t in toks[:eq]).strip()
        if not key:
            continue
        out[key] = "".join(t[-1] for t in toks[eq + 1:]).strip()
    return out


def _patterns(props: dict) -> frozenset:
    return frozenset(p for p, (_, _, avail) in ACTS.items() if props.get(avail) is True)


def _rid_key(rid) -> str:
    try:
        parts = [str(int(x)) for x in rid] if rid is not None else []
    except (TypeError, ValueError):
        parts = []
    return ".".join(parts)


class UiaTree(UiTree):
    """UiTree over a UiaCom. `window_filter(process_id) -> bool` selects the top-level windows that are
    ours (the caller's verified Gemini process tree); windows of other processes are never read."""

    def __init__(self, com: UiaCom, window_filter: Callable[[int], bool]):
        if not callable(window_filter):
            raise TypeError("window_filter must be callable(process_id) -> bool")
        self._com = com
        self._filter = window_filter
        self._elements: Dict[str, Any] = {}
        self._patterns: Dict[str, frozenset] = {}
        self._documents: Dict[str, Any] = {}

    # -- building ----------------------------------------------------
    def _ids(self, entries: List[Tuple[Any, str]], anchor: str, pinned: Optional[str]) -> List[str]:
        """entries = [(element, index_path)], first is the root; `pinned` forces the root's id."""
        bases = [_rid_key(self._com.runtime_id(el)) for el, _ in entries]
        counts: Dict[str, int] = {}
        for i, b in enumerate(bases):
            if i == 0 and pinned is not None:
                b = pinned
            counts[b] = counts.get(b, 0) + 1
        ids = []
        for i, ((_, path), b) in enumerate(zip(entries, bases)):
            if i == 0 and pinned is not None:
                ids.append(pinned)
            elif b and counts[b] == 1:
                ids.append(b)
            else:
                ids.append("%s~%s:%s" % (b or "norid", anchor, path))
        return ids

    def _to_node(self, cached: CachedElement, pinned: Optional[str] = None) -> Node:
        flat: List[Tuple[CachedElement, str, int]] = []  # (cached, path, parent index)
        stack = [(cached, "0", -1)]
        while stack:
            c, path, parent = stack.pop()
            idx = len(flat)
            flat.append((c, path, parent))
            for i in range(len(c.children) - 1, -1, -1):
                stack.append((c.children[i], "%s.%d" % (path, i), idx))
        root_base = pinned if pinned is not None else (_rid_key(self._com.runtime_id(cached.element)) or "norid")
        ids = self._ids([(c.element, p) for c, p, _ in flat], root_base, pinned)
        nodes: List[Node] = []
        for (c, _, parent), nid in zip(flat, ids):
            pr = c.props
            node = Node(
                id=nid, ct=control_type_name(pr.get("ControlType")), role=_str(pr.get("AriaRole")),
                name=_str(pr.get("Name")), aria=parse_aria_properties(pr.get("AriaProperties")),
                heading=heading_level(pr.get("HeadingLevel")), enabled=_bool(pr.get("IsEnabled"), True),
                offscreen=_bool(pr.get("IsOffscreen"), False), value=_str(pr.get("ValueValue")),
                automation_id=_str(pr.get("AutomationId")), patterns=_patterns(pr))
            nodes.append(node)
            self._elements[nid] = c.element
            self._patterns[nid] = node.patterns
            if parent >= 0:
                nodes[parent].children.append(node)
        return nodes[0]

    def _windows(self, class_name: Optional[str]) -> List[Any]:
        return [w for w in self._com.find_windows(class_name) if self._filter(self._com.process_id(w))]

    # -- UiTree ------------------------------------------------------
    def documents(self) -> List[Node]:
        """RootWebArea elements of our windows, shallow (no children); `value` is the URL."""
        found = []
        for w in self._windows(WINDOW_CLASS):
            found.extend(self._com.find_by_automation_id(w, DOCUMENT_AUTOMATION_ID))
        bases = [_rid_key(self._com.runtime_id(el)) for el in found]
        counts: Dict[str, int] = {}
        for b in bases:
            counts[b] = counts.get(b, 0) + 1
        ids = [b if b and counts[b] == 1 else "%s~documents:%d" % (b or "norid", i) for i, b in enumerate(bases)]
        docs = []
        self._documents = {}
        for el, nid in zip(found, ids):
            node = self._to_node(self._com.build_cache(el, NODE_PROPS, subtree=False), pinned=nid)
            self._documents[nid] = el
            docs.append(node)
        return docs

    def desktop_root(self) -> Node:
        """The calling thread's desktop root; only top-level windows passing window_filter are kept."""
        cached = self._com.build_cache(self._com.root(), NODE_PROPS, subtree=True)
        kept = []
        for child in cached.children:
            pid = child.props.get("ProcessId")
            if not isinstance(pid, int) or isinstance(pid, bool):
                pid = self._com.process_id(child.element)
            if self._filter(pid):
                kept.append(child)
        cached.children = kept
        return self._to_node(cached)

    def snapshot(self, doc_id: str) -> Node:
        if doc_id not in self._documents:
            raise ElementGone("no such document: %s" % doc_id)
        cached = self._com.build_cache(self._documents[doc_id], NODE_PROPS, subtree=True)
        return self._to_node(cached, pinned=doc_id)

    def _element(self, node_id: str) -> Any:
        if node_id not in self._elements:
            raise ElementGone("no such node: %s" % node_id)
        return self._elements[node_id]

    def _pattern(self, node_id: str, name: str) -> Any:
        obj = self._com.pattern(self._element(node_id), name)
        if obj is None:
            raise PatternNotSupported("%s does not support %s" % (node_id, name))
        return obj

    def act(self, node_id: str, pattern: str) -> None:
        if pattern not in ACTS:
            raise PatternNotSupported("unknown pattern %r" % pattern)
        self._element(node_id)
        if pattern not in self._patterns.get(node_id, frozenset()):
            raise PatternNotSupported("%s does not support %s" % (node_id, pattern))
        com_name, method, _ = ACTS[pattern]
        getattr(self._pattern(node_id, com_name), method)()

    def set_value(self, node_id: str, text: str) -> None:
        self._pattern(node_id, "value").SetValue(text)

    def get_value(self, node_id: str) -> str:
        v = self._pattern(node_id, "value").CurrentValue
        return v if isinstance(v, str) else ""


# -- real COM --------------------------------------------------------------------
class ComtypesUia:
    """UiaCom over a comtypes IUIAutomation object. Every call refuses under the test sandbox."""

    def __init__(self, uia, uia_module, com_error):
        self._uia, self._mod, self._com_error = uia, uia_module, com_error

    def _call(self, fn, *args):
        _refuse_in_sandbox()
        try:
            return fn(*args)
        except self._com_error as exc:
            hresult = exc.args[0] if exc.args else None
            if hresult == UIA_E_ELEMENTNOTAVAILABLE:
                raise ElementGone("element not available") from exc
            raise UiaComError("UIA call failed (hresult %r)" % (hresult,)) from exc

    def root(self):
        return self._call(self._uia.GetRootElement)

    def _find_all(self, element, scope, prop, value):
        def run():
            cond = (self._uia.CreateTrueCondition() if prop is None
                    else self._uia.CreatePropertyCondition(PROPERTY_IDS[prop], value))
            arr = element.FindAll(scope, cond)
            return [arr.GetElement(i) for i in range(arr.Length)] if arr else []
        return self._call(run)

    def find_windows(self, class_name):
        _refuse_in_sandbox()
        return self._find_all(self.root(), TREESCOPE_CHILDREN, None if class_name is None else "ClassName",
                              class_name)

    def find_by_automation_id(self, element, automation_id):
        return self._find_all(element, TREESCOPE_SUBTREE, "AutomationId", automation_id)

    def build_cache(self, element, props, subtree=True):
        _refuse_in_sandbox()
        props = list(props)
        unknown = [p for p in props if p not in PROPERTY_IDS]
        if unknown:
            raise ValueError("unknown UIA properties: %s" % ", ".join(unknown))

        def run():
            cr = self._uia.CreateCacheRequest()
            for p in props:
                cr.AddProperty(PROPERTY_IDS[p])
            cr.TreeScope = TREESCOPE_SUBTREE if subtree else TREESCOPE_ELEMENT
            croot = element.BuildUpdatedCache(cr)
            top = CachedElement(element=croot)
            stack = [top]
            while stack:
                c = stack.pop()
                for p in props:
                    try:
                        c.props[p] = c.element.GetCachedPropertyValue(PROPERTY_IDS[p])
                    except self._com_error:
                        c.props[p] = None
                if not subtree:
                    continue
                kids = c.element.GetCachedChildren()
                if not kids:  # NULL COM pointer for leaves
                    continue
                for i in range(kids.Length):
                    child = CachedElement(element=kids.GetElement(i))
                    c.children.append(child)
                    stack.append(child)
            return top
        return self._call(run)

    def pattern(self, element, name):
        _refuse_in_sandbox()
        if name not in PATTERN_IDS:
            raise PatternNotSupported("unknown pattern %r" % name)
        pid, iface = PATTERN_IDS[name]

        def run():
            unk = element.GetCurrentPattern(pid)
            if not unk:  # NULL pointer: not supported
                return None
            return unk.QueryInterface(getattr(self._mod, iface))
        return self._call(run)

    def runtime_id(self, element):
        def run():
            try:
                rid = element.GetCachedPropertyValue(PROPERTY_IDS["RuntimeId"])
            except self._com_error:
                rid = None
            if not rid:
                rid = element.GetRuntimeId()
            return tuple(int(x) for x in rid) if rid else None
        return self._call(run)

    def process_id(self, element):
        return self._call(lambda: int(element.CurrentProcessId))


def load_com() -> ComtypesUia:
    """Create the real UiaCom. Call only after SetThreadDesktop; refused under the test sandbox."""
    _refuse_in_sandbox()
    if sys.platform != "win32":
        raise UiaUnavailable("platform", "UI Automation is Windows only")
    try:
        import comtypes  # noqa: WPS433  (lazy by design)
        import comtypes.client
        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as uia_mod
        uia = comtypes.client.CreateObject(uia_mod.CUIAutomation, interface=uia_mod.IUIAutomation)
    except Exception as exc:  # ImportError, OSError, COMError: all mean "not usable here"
        raise UiaUnavailable("comtypes", detail=type(exc).__name__) from exc
    return ComtypesUia(uia, uia_mod, comtypes.COMError)

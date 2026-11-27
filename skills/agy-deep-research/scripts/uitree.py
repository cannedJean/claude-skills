"""UI Automation tree model, UiTree protocol and a JSON-driven FakeTree.

Stdlib only; imports on any OS. Higher layers are written purely against
Node / UiTree, so every step test can run on FakeTree with a JSON spec.
"""
import copy
import json
from dataclasses import dataclass, field
from typing import Callable, Iterator, List, Optional, Tuple

try:  # Protocol is 3.8+, kept guarded for safety
    from typing import Protocol
except ImportError:  # pragma: no cover
    Protocol = object  # type: ignore

PATTERNS = ("invoke", "toggle", "expand", "collapse", "default_action")


class PatternNotSupported(Exception):
    """Raised when act() uses a pattern the node does not advertise."""


@dataclass
class Node:
    id: str
    ct: str
    role: str = ""
    name: str = ""
    aria: dict = field(default_factory=dict)
    heading: Optional[int] = None
    enabled: bool = True
    offscreen: bool = False
    value: str = ""
    automation_id: str = ""
    patterns: frozenset = frozenset()
    children: list = field(default_factory=list)

    def walk(self) -> Iterator[Tuple["Node", Tuple["Node", ...]]]:
        """Pre-order traversal yielding (node, ancestors_root_first)."""
        stack = [(self, ())]
        while stack:
            node, anc = stack.pop()
            yield node, anc
            sub = anc + (node,)
            for child in reversed(node.children):
                stack.append((child, sub))

    def find_all(self, pred: Callable[["Node", tuple], bool]) -> List["Node"]:
        return [n for n, anc in self.walk() if pred(n, anc)]

    @classmethod
    def from_dict(cls, d: dict) -> "Node":
        return cls(
            id=d["id"],
            ct=d["ct"],
            role=d.get("role", ""),
            name=d.get("name", ""),
            aria=dict(d.get("aria") or {}),
            heading=d.get("heading"),
            enabled=d.get("enabled", True),
            offscreen=d.get("offscreen", False),
            value=d.get("value", ""),
            automation_id=d.get("automation_id", ""),
            patterns=frozenset(d.get("patterns") or ()),
            children=[cls.from_dict(c) for c in d.get("children") or ()],
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ct": self.ct,
            "role": self.role,
            "name": self.name,
            "aria": dict(self.aria),
            "heading": self.heading,
            "enabled": self.enabled,
            "offscreen": self.offscreen,
            "value": self.value,
            "automation_id": self.automation_id,
            "patterns": sorted(self.patterns),
            "children": [c.to_dict() for c in self.children],
        }


class UiTree(Protocol):
    def documents(self) -> List[Node]:
        """RootWebArea nodes; `value` holds the URL."""

    def desktop_root(self) -> Node:
        """Desktop-level root (popups/menus)."""

    def snapshot(self, doc_id: str) -> Node: ...

    def act(self, node_id: str, pattern: str) -> None: ...

    def set_value(self, node_id: str, text: str) -> None: ...

    def get_value(self, node_id: str) -> str: ...


class FakeTree(UiTree):
    """Deterministic UiTree driven by a JSON spec (see module tests).

    Semantics:
    - ``set_value`` stores text per node id; it is shown in every state, unless
      a transition's target state defines a non-empty ``value`` for that node,
      in which case that value replaces the stored text once the transition
      takes effect.
    - ``after_snapshots`` = number of further ``snapshot()`` calls before the
      new state becomes visible (0 = immediately).
    - Pending transitions are last-wins: a second matching action while a
      transition is pending replaces the pending one (matching is evaluated
      against the currently visible state).
    - A malformed spec raises ``ValueError`` naming the bad field.
    """

    def __init__(self, spec: dict):
        self._states = {
            name: {
                "documents": [Node.from_dict(d) for d in s.get("documents") or ()],
                "desktop": Node.from_dict(s["desktop"]) if s.get("desktop") else None,
            }
            for name, s in spec["states"].items()
        }
        self._transitions = list(spec.get("transitions") or ())
        self.state: str = spec["state"]
        self._validate()
        self.actions: List[tuple] = []
        self._values: dict = {}
        self._pending: Optional[list] = None  # [target_state, snapshots_left]

    @classmethod
    def from_file(cls, path) -> "FakeTree":
        with open(path, "r", encoding="utf-8") as fh:
            return cls(json.load(fh))

    # -- internals ---------------------------------------------------
    def _validate(self) -> None:
        if self.state not in self._states:
            raise ValueError("spec field 'state': unknown state %r" % (self.state,))
        for i, tr in enumerate(self._transitions):
            where = "transitions[%d]" % i
            if tr.get("to") not in self._states:
                raise ValueError("%s field 'to': unknown state %r" % (where, tr.get("to")))
            frm = tr.get("from", "*")
            if frm != "*" and frm not in self._states:
                raise ValueError("%s field 'from': unknown state %r" % (where, frm))
            on = tr.get("on")
            if not isinstance(on, (list, tuple)) or not on or on[0] not in ("act", "set_value"):
                raise ValueError("%s field 'on': first item must be 'act' or 'set_value'" % where)
            if on[0] == "act":
                if len(on) != 3 or on[2] not in PATTERNS:
                    raise ValueError(
                        "%s field 'on': act needs [act, node_id, pattern] with pattern in %s"
                        % (where, ", ".join(PATTERNS)))
            elif len(on) != 2:
                raise ValueError("%s field 'on': set_value needs [set_value, node_id]" % where)
            delay = tr.get("after_snapshots", 0)
            if isinstance(delay, bool) or not isinstance(delay, int) or delay < 0:
                raise ValueError(
                    "%s field 'after_snapshots': must be a non-negative int, got %r" % (where, delay))

    def _enter(self, state: str) -> None:
        """Make `state` current; its explicit node values replace stored text."""
        self.state = state
        cur = self._states[state]
        roots = list(cur["documents"])
        if cur["desktop"] is not None:
            roots.append(cur["desktop"])
        for root in roots:
            for n, _ in root.walk():
                if n.value and n.id in self._values:
                    del self._values[n.id]

    def _overlay(self, node: Node) -> Node:
        node = copy.deepcopy(node)
        for n, _ in node.walk():
            if n.id in self._values:
                n.value = self._values[n.id]
        return node

    def _find(self, node_id: str) -> Node:
        cur = self._states[self.state]
        roots = list(cur["documents"])
        if cur["desktop"] is not None:
            roots.append(cur["desktop"])
        for root in roots:
            for n, _ in root.walk():
                if n.id == node_id:
                    return n
        raise KeyError("no such node in state %r: %s" % (self.state, node_id))

    def _fire(self, on: list) -> None:
        for tr in self._transitions:
            if tr.get("from", "*") not in ("*", self.state):
                continue
            if list(tr["on"]) != list(on):
                continue
            delay = tr.get("after_snapshots", 0)
            if delay <= 0:
                self._pending = None
                self._enter(tr["to"])
            else:
                self._pending = [tr["to"], delay]
            return

    # -- UiTree ------------------------------------------------------
    def documents(self) -> List[Node]:
        return [self._overlay(d) for d in self._states[self.state]["documents"]]

    def desktop_root(self) -> Node:
        d = self._states[self.state]["desktop"]
        if d is None:
            return Node(id="desktop", ct="Pane")
        return self._overlay(d)

    def snapshot(self, doc_id: str) -> Node:
        if self._pending is not None:
            if self._pending[1] <= 0:
                target = self._pending[0]
                self._pending = None
                self._enter(target)
            else:
                self._pending[1] -= 1
        for d in self._states[self.state]["documents"]:
            if d.id == doc_id:
                return self._overlay(d)
        raise KeyError("no such document in state %r: %s" % (self.state, doc_id))

    def act(self, node_id: str, pattern: str) -> None:
        if pattern not in PATTERNS:
            raise PatternNotSupported("unknown pattern %r" % pattern)
        node = self._find(node_id)
        if pattern not in node.patterns:
            raise PatternNotSupported("%s does not support %s" % (node_id, pattern))
        self.actions.append(("act", node_id, pattern))
        self._fire(["act", node_id, pattern])

    def set_value(self, node_id: str, text: str) -> None:
        self._find(node_id)
        self._values[node_id] = text
        self.actions.append(("set_value", node_id, text))
        self._fire(["set_value", node_id])

    def get_value(self, node_id: str) -> str:
        if node_id in self._values:
            return self._values[node_id]
        return self._find(node_id).value

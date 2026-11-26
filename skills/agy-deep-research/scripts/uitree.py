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
    """Deterministic UiTree driven by a JSON spec (see module tests)."""

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
        self.actions: List[tuple] = []
        self._values: dict = {}
        self._pending: Optional[list] = None  # [target_state, snapshots_left]

    @classmethod
    def from_file(cls, path) -> "FakeTree":
        with open(path, "r", encoding="utf-8") as fh:
            return cls(json.load(fh))

    # -- internals ---------------------------------------------------
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
            delay = int(tr.get("after_snapshots", 0))
            if delay <= 0:
                self.state = tr["to"]
                self._pending = None
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
                self.state = self._pending[0]
                self._pending = None
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

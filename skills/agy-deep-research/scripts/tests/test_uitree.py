import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from uitree import FakeTree, Node, PatternNotSupported  # noqa: E402


def _sample():
    return Node(id="root", ct="Document", children=[
        Node(id="a", ct="Group", children=[Node(id="a1", ct="Text", name="x")]),
        Node(id="b", ct="Button", name="Go", patterns=frozenset({"invoke"})),
    ])


def _spec():
    def doc(btn_name):
        return {"id": "doc", "ct": "Document", "role": "RootWebArea", "value": "https://x/", "children": [
            {"id": "btn", "ct": "Button", "name": btn_name, "patterns": ["invoke"]},
            {"id": "edit", "ct": "Edit", "value": ""},
            {"id": "plain", "ct": "Text"},
        ]}
    return {
        "state": "s0",
        "states": {
            "s0": {"documents": [doc("Start")], "desktop": None},
            "s1": {"documents": [doc("Done")], "desktop": {"id": "menu", "ct": "Menu"}},
        },
        "transitions": [
            {"from": "s0", "on": ["act", "btn", "invoke"], "to": "s1", "after_snapshots": 1},
        ],
    }


class UiTreeTests(unittest.TestCase):
    def test_walk_preorder_with_ancestors(self):
        got = [(n.id, tuple(a.id for a in anc)) for n, anc in _sample().walk()]
        self.assertEqual(got, [
            ("root", ()), ("a", ("root",)), ("a1", ("root", "a")), ("b", ("root",)),
        ])
        found = _sample().find_all(lambda n, anc: n.ct == "Button" or len(anc) == 2)
        self.assertEqual([n.id for n in found], ["a1", "b"])

    def test_dict_round_trip(self):
        n = Node(id="r", ct="Document", role="RootWebArea", name="n", aria={"k": "v"},
                 heading=2, enabled=False, offscreen=True, value="u", automation_id="aid",
                 patterns=frozenset({"toggle", "invoke"}), children=[Node(id="c", ct="Text")])
        d = n.to_dict()
        self.assertEqual(d["patterns"], ["invoke", "toggle"])
        self.assertEqual(Node.from_dict(d), n)
        self.assertEqual(Node.from_dict({"id": "x", "ct": "Text"}), Node(id="x", ct="Text"))

    def test_fake_transition_after_snapshots(self):
        t = FakeTree(_spec())
        self.assertEqual(t.documents()[0].value, "https://x/")
        t.act("btn", "invoke")
        self.assertEqual(t.snapshot("doc").children[0].name, "Start")
        self.assertEqual(t.state, "s0")
        self.assertEqual(t.snapshot("doc").children[0].name, "Done")
        self.assertEqual(t.state, "s1")
        self.assertEqual(t.desktop_root().id, "menu")
        self.assertEqual(t.actions, [("act", "btn", "invoke")])

    def test_fake_set_value_records_and_reads_back(self):
        t = FakeTree(_spec())
        t.set_value("edit", "hello")
        self.assertEqual(t.get_value("edit"), "hello")
        self.assertEqual(t.actions, [("set_value", "edit", "hello")])
        edit = [n for n, _ in t.snapshot("doc").walk() if n.id == "edit"][0]
        self.assertEqual(edit.value, "hello")

    def test_fake_rejects_unsupported_pattern(self):
        t = FakeTree(_spec())
        with self.assertRaises(PatternNotSupported):
            t.act("plain", "invoke")
        with self.assertRaises(PatternNotSupported):
            t.act("btn", "toggle")


if __name__ == "__main__":
    unittest.main()

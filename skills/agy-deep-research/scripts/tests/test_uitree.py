import copy
import json
import os
import sys
import tempfile
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

    def test_set_value_persists_across_states(self):
        spec = _spec()
        spec["transitions"][0]["after_snapshots"] = 0
        t = FakeTree(spec)
        t.set_value("edit", "keep")
        t.act("btn", "invoke")
        self.assertEqual(t.state, "s1")
        self.assertEqual(t.get_value("edit"), "keep")
        edit = [n for n, _ in t.snapshot("doc").walk() if n.id == "edit"][0]
        self.assertEqual(edit.value, "keep")

    def test_transition_target_value_replaces_stored_value(self):
        spec = _spec()
        spec["transitions"][0]["after_snapshots"] = 0
        spec["states"]["s1"]["documents"][0]["children"][1]["value"] = "server"
        t = FakeTree(spec)
        t.set_value("edit", "mine")
        self.assertEqual(t.get_value("edit"), "mine")
        t.act("btn", "invoke")
        self.assertEqual(t.get_value("edit"), "server")
        edit = [n for n, _ in t.snapshot("doc").walk() if n.id == "edit"][0]
        self.assertEqual(edit.value, "server")

    def test_transition_target_value_applies_after_delay(self):
        spec = _spec()
        spec["states"]["s1"]["documents"][0]["children"][1]["value"] = "server"
        t = FakeTree(spec)
        t.set_value("edit", "mine")
        t.act("btn", "invoke")
        self.assertEqual(t.get_value("edit"), "mine")
        t.snapshot("doc")
        t.snapshot("doc")
        self.assertEqual(t.get_value("edit"), "server")

    def test_rejected_act_not_logged(self):
        t = FakeTree(_spec())
        with self.assertRaises(PatternNotSupported):
            t.act("plain", "invoke")
        self.assertEqual(t.actions, [])
        with self.assertRaises(KeyError):
            t.act("nope", "invoke")
        with self.assertRaises(KeyError):
            t.set_value("nope", "x")
        self.assertEqual(t.actions, [])

    def test_from_star_matches_any_state(self):
        spec = _spec()
        spec["transitions"] = [
            {"from": "*", "on": ["act", "btn", "invoke"], "to": "s1", "after_snapshots": 0}]
        t = FakeTree(spec)
        t.act("btn", "invoke")
        self.assertEqual(t.state, "s1")
        spec["state"] = "s1"
        t2 = FakeTree(spec)
        t2.act("btn", "invoke")
        self.assertEqual(t2.state, "s1")

    def test_after_snapshots_zero_and_two(self):
        spec = _spec()
        spec["transitions"][0]["after_snapshots"] = 0
        t = FakeTree(spec)
        t.act("btn", "invoke")
        self.assertEqual(t.state, "s1")
        spec["transitions"][0]["after_snapshots"] = 2
        t = FakeTree(spec)
        t.act("btn", "invoke")
        self.assertEqual(t.snapshot("doc").children[0].name, "Start")
        self.assertEqual(t.snapshot("doc").children[0].name, "Start")
        self.assertEqual(t.snapshot("doc").children[0].name, "Done")
        self.assertEqual(t.state, "s1")

    def test_set_value_triggers_transition(self):
        spec = _spec()
        spec["transitions"] = [
            {"from": "s0", "on": ["set_value", "edit"], "to": "s1", "after_snapshots": 0}]
        t = FakeTree(spec)
        t.set_value("edit", "go")
        self.assertEqual(t.state, "s1")
        self.assertEqual(t.get_value("edit"), "go")

    def test_from_file_loads_json(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "spec.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(_spec(), fh)
            t = FakeTree.from_file(path)
        self.assertEqual(t.state, "s0")
        self.assertEqual(t.documents()[0].id, "doc")

    def test_snapshot_returns_isolated_copy(self):
        t = FakeTree(_spec())
        snap = t.snapshot("doc")
        snap.children[0].name = "MUTATED"
        snap.children.clear()
        again = t.snapshot("doc")
        self.assertEqual(again.children[0].name, "Start")
        self.assertEqual(len(again.children), 3)
        docs = t.documents()
        docs[0].children.clear()
        self.assertEqual(len(t.documents()[0].children), 3)

    def test_null_desktop_is_empty_pane(self):
        d = FakeTree(_spec()).desktop_root()
        self.assertEqual(d.ct, "Pane")
        self.assertEqual(d.children, [])

    def test_invalid_specs_raise_value_error(self):
        def with_tr(**kw):
            s = _spec()
            s["transitions"][0].update(kw)
            return s

        def with_state(v):
            s = _spec()
            s["state"] = v
            return s

        cases = {
            "state": with_state("nope"),
            "to": with_tr(to="nope"),
            "from": with_tr(**{"from": "nope"}),
            "on kind": with_tr(on=["click", "btn", "invoke"]),
            "act pattern": with_tr(on=["act", "btn", "smash"]),
            "after_snapshots negative": with_tr(after_snapshots=-1),
            "after_snapshots non-int": with_tr(after_snapshots="1"),
            "after_snapshots float": with_tr(after_snapshots=1.5),
            "after_snapshots bool": with_tr(after_snapshots=True),
        }
        for name, spec in cases.items():
            with self.subTest(name):
                with self.assertRaises(ValueError) as cm:
                    FakeTree(copy.deepcopy(spec))
                self.assertTrue(str(cm.exception))
        # message names the bad field
        with self.assertRaisesRegex(ValueError, "after_snapshots"):
            FakeTree(with_tr(after_snapshots=-1))
        with self.assertRaisesRegex(ValueError, "state"):
            FakeTree(with_state("nope"))
        # valid forms still accepted
        FakeTree(with_tr(**{"from": "*"}))
        FakeTree(with_tr(on=["set_value", "edit"]))

    def test_pending_transition_last_wins(self):
        spec = _spec()
        spec["states"]["s2"] = {"documents": [{
            "id": "doc", "ct": "Document", "role": "RootWebArea", "value": "https://x/",
            "children": [{"id": "btn", "ct": "Button", "name": "Other"}]}], "desktop": None}
        spec["states"]["s0"]["documents"][0]["children"].append(
            {"id": "btn2", "ct": "Button", "patterns": ["invoke"]})
        spec["transitions"].append(
            {"from": "s0", "on": ["act", "btn2", "invoke"], "to": "s2", "after_snapshots": 1})
        t = FakeTree(spec)
        t.act("btn", "invoke")
        t.act("btn2", "invoke")
        self.assertEqual(t.snapshot("doc").children[0].name, "Start")
        self.assertEqual(t.snapshot("doc").children[0].name, "Other")
        self.assertEqual(t.state, "s2")
        t.snapshot("doc")
        self.assertEqual(t.state, "s2")


if __name__ == "__main__":
    unittest.main()

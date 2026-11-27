import sandbox  # noqa: F401  (must stay the first import: blocks real-app access)
import copy
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from uitree import FakeTree, Node, PATTERNS, PatternNotSupported  # noqa: E402


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


def _edit(t, doc="doc"):
    return [n for n, _ in t.snapshot(doc).walk() if n.id == "edit"][0]


class UiTreeReviewFixTests(unittest.TestCase):
    # B6
    def test_from_dict_null_string_fields_become_empty(self):
        n = Node.from_dict({"id": "x", "ct": "Text", "role": None, "name": None, "value": None,
                            "automation_id": None, "aria": None, "patterns": None, "children": None})
        self.assertEqual((n.role, n.name, n.value, n.automation_id), ("", "", "", ""))
        self.assertEqual(n.aria, {})
        self.assertEqual(n.patterns, frozenset())
        self.assertEqual(n.children, [])
        self.assertEqual(n, Node(id="x", ct="Text"))

    # B11
    def test_from_file_accepts_utf8_bom(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "spec.json")
            with open(path, "w", encoding="utf-8-sig") as fh:
                json.dump(_spec(), fh)
            self.assertEqual(FakeTree.from_file(path).state, "s0")

    # B13 / M14
    def test_snapshot_unknown_doc_keeps_pending_transition(self):
        t = FakeTree(_spec())
        t.act("btn", "invoke")
        with self.assertRaises(KeyError):
            t.snapshot("nonexistent")
        self.assertEqual(t.state, "s0")
        self.assertEqual(t.snapshot("doc").children[0].name, "Start")
        self.assertEqual(t.snapshot("doc").children[0].name, "Done")
        self.assertEqual(t.state, "s1")

    def test_snapshot_unknown_doc_when_transition_due_keeps_state(self):
        t = FakeTree(_spec())
        t.act("btn", "invoke")
        t.snapshot("doc")  # delay consumed, transition due on the next snapshot
        with self.assertRaises(KeyError):
            t.snapshot("nonexistent")
        self.assertEqual(t.state, "s0")
        self.assertEqual(t.snapshot("doc").children[0].name, "Done")

    # B14 / M11
    def test_get_value_missing_node_raises_keyerror(self):
        spec = _spec()
        spec["states"]["s1"]["documents"][0]["children"].pop(1)  # no "edit" in s1
        spec["transitions"][0]["after_snapshots"] = 0
        t = FakeTree(spec)
        t.set_value("edit", "hello")
        t.act("btn", "invoke")
        self.assertEqual(t.state, "s1")
        with self.assertRaises(KeyError):
            t.get_value("edit")
        with self.assertRaises(KeyError):
            t.get_value("never-existed")

    # B15 / M14 / T18
    def test_malformed_spec_shapes_raise_value_error(self):
        def mutate(fn):
            s = _spec()
            fn(s)
            return s

        cases = {
            "spec not dict": ([], "spec"),
            "missing state": (mutate(lambda s: s.pop("state")), "state"),
            "missing states": (mutate(lambda s: s.pop("states")), "states"),
            "states not dict": (mutate(lambda s: s.update(states=[])), "states"),
            "state not dict": (mutate(lambda s: s["states"].update(s0=[])), "s0"),
            "documents not list": (mutate(lambda s: s["states"]["s0"].update(documents=5)), "s0"),
            "document not node": (mutate(lambda s: s["states"]["s0"].update(documents=[{"ct": "X"}])), "s0"),
            "desktop empty dict": (mutate(lambda s: s["states"]["s0"].update(desktop={})), "s0"),
            "transitions not list": (mutate(lambda s: s.update(transitions=123)), "transitions"),
            "transition not dict": (mutate(lambda s: s.update(transitions=["bad"])), "transitions[0]"),
            # B19
            "documents empty dict": (mutate(lambda s: s["states"]["s0"].update(documents={})), "s0"),
            "documents empty string": (mutate(lambda s: s["states"]["s0"].update(documents="")), "s0"),
            "documents string": (mutate(lambda s: s["states"]["s0"].update(documents="doc")), "s0"),
            "patterns string": (mutate(lambda s: s["states"]["s0"]["documents"][0]["children"][0]
                                       .update(patterns="invoke")), "patterns"),
            "children dict": (mutate(lambda s: s["states"]["s0"]["documents"][0].update(children={})), "children"),
            "state unhashable": (mutate(lambda s: s.update(state=[])), "state"),
            "to unhashable": (mutate(lambda s: s["transitions"][0].update(to=["s1"])), "to"),
            "from unhashable": (mutate(lambda s: s["transitions"][0].update({"from": {}})), "from"),
        }
        for label, (spec, field) in cases.items():
            with self.subTest(label):
                with self.assertRaises(ValueError) as cm:
                    FakeTree(copy.deepcopy(spec))
                self.assertIn(field, str(cm.exception))

    # T18
    def test_validation_message_names_each_rule(self):
        def with_tr(**kw):
            s = _spec()
            s["transitions"][0].update(kw)
            return s
        cases = {
            "to": (with_tr(to="nope"), "'to'"),
            "from": (with_tr(**{"from": "nope"}), "'from'"),
            "on kind": (with_tr(on=["click", "btn", "invoke"]), "'on'"),
            "act pattern": (with_tr(on=["act", "btn", "smash"]), "'on'"),
            "set_value arity": (with_tr(on=["set_value"]), "'on'"),
            "after_snapshots": (with_tr(after_snapshots="1"), "'after_snapshots'"),
        }
        for label, (spec, frag) in cases.items():
            with self.subTest(label):
                with self.assertRaises(ValueError) as cm:
                    FakeTree(spec)
                self.assertIn("transitions[0]", str(cm.exception))
                self.assertIn(frag, str(cm.exception))

    # T18
    def test_node_defaults_are_isolated(self):
        a, b = Node(id="a", ct="T"), Node(id="b", ct="T")
        a.aria["k"] = 1
        a.children.append(b)
        self.assertEqual(b.aria, {})
        self.assertEqual(b.children, [])
        c = Node.from_dict({"id": "c", "ct": "T"})
        d = Node.from_dict({"id": "d", "ct": "T"})
        c.aria["k"] = 1
        c.children.append(d)
        self.assertEqual(d.aria, {})
        self.assertEqual(d.children, [])

    # T9
    def test_from_star_fires_from_every_state(self):
        spec = _spec()
        spec["states"]["s2"] = copy.deepcopy(spec["states"]["s1"])
        spec["transitions"] = [
            {"from": "*", "on": ["act", "btn", "invoke"], "to": "s2", "after_snapshots": 0}]
        for start in ("s0", "s1"):
            with self.subTest(start):
                spec["state"] = start
                t = FakeTree(copy.deepcopy(spec))
                t.act("btn", "invoke")
                self.assertEqual(t.state, "s2")

    def test_from_specific_state_only_fires_there(self):
        spec = _spec()
        spec["transitions"][0]["after_snapshots"] = 0
        spec["state"] = "s1"
        t = FakeTree(spec)
        t.act("btn", "invoke")
        self.assertEqual(t.state, "s1")
        self.assertEqual(t.snapshot("doc").children[0].name, "Done")

    # T16
    def test_immediate_transition_cancels_pending_one(self):
        spec = _spec()
        spec["states"]["s2"] = copy.deepcopy(spec["states"]["s1"])
        spec["states"]["s0"]["documents"][0]["children"].append(
            {"id": "btn2", "ct": "Button", "patterns": ["invoke"]})
        spec["transitions"] = [
            {"from": "s0", "on": ["act", "btn", "invoke"], "to": "s1", "after_snapshots": 2},
            {"from": "s0", "on": ["act", "btn2", "invoke"], "to": "s2", "after_snapshots": 0},
        ]
        t = FakeTree(spec)
        t.act("btn", "invoke")
        t.act("btn2", "invoke")
        self.assertEqual(t.state, "s2")
        for _ in range(4):
            t.snapshot("doc")
        self.assertEqual(t.state, "s2")

    def test_value_replacement_waits_for_pending_state(self):
        spec = _spec()
        spec["states"]["s1"]["documents"][0]["children"][1]["value"] = "server"
        t = FakeTree(spec)
        t.set_value("edit", "mine")
        t.act("btn", "invoke")
        self.assertEqual(_edit(t).value, "mine")       # snapshot 1: still the old state
        self.assertEqual(t.get_value("edit"), "mine")
        self.assertEqual(_edit(t).value, "server")     # snapshot 2: new state visible

    # T17
    def test_all_patterns_supported_and_logged(self):
        spec = _spec()
        five = ("invoke", "toggle", "expand", "collapse", "default_action")
        self.assertEqual(PATTERNS, five)
        spec["states"]["s0"]["documents"][0]["children"][0]["patterns"] = list(five)
        t = FakeTree(spec)
        for pat in five:
            t.act("btn", pat)
        self.assertEqual(t.actions, [("act", "btn", p) for p in five])
        with self.assertRaises(PatternNotSupported):
            t.act("btn", "smash")
        self.assertEqual(len(t.actions), len(five))

    def test_unknown_node_and_doc_raise_keyerror(self):
        t = FakeTree(_spec())
        with self.assertRaises(KeyError):
            t.snapshot("nope")
        with self.assertRaises(KeyError):
            t.get_value("nope")

    def test_act_on_desktop_node(self):
        spec = _spec()
        spec["states"]["s0"]["desktop"] = {"id": "menu", "ct": "Menu", "children": [
            {"id": "mi", "ct": "MenuItem", "patterns": ["invoke"]}]}
        spec["transitions"] = [
            {"from": "s0", "on": ["act", "mi", "invoke"], "to": "s1", "after_snapshots": 0}]
        t = FakeTree(spec)
        t.act("mi", "invoke")
        self.assertEqual(t.actions, [("act", "mi", "invoke")])
        self.assertEqual(t.state, "s1")
        with self.assertRaises(PatternNotSupported):
            FakeTree(_spec()).act("btn", "expand")


if __name__ == "__main__":
    unittest.main()

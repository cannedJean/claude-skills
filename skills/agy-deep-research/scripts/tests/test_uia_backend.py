import sandbox  # noqa: F401  (must stay the first import: blocks real-app access)
# uia_backend tests. Only a fake UiaCom is used: no IUIAutomation object, no real window, no pattern call.
import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import uia_backend as ub  # noqa: E402
from uitree import Node, PatternNotSupported  # noqa: E402

CT_BUTTON, CT_DOC, CT_WINDOW, CT_TEXT, CT_PANE, CT_MENUITEM = 50000, 50030, 50032, 50020, 50033, 50011
_rid = [1000]


def _next_rid():
    _rid[0] += 1
    return (42, _rid[0])


class FakePattern:
    def __init__(self, el, name):
        self.el, self.name = el, name

    def _log(self, call, *args):
        self.el.calls.append((self.name, call) + args)

    def Invoke(self):
        self._log("Invoke")

    def Toggle(self):
        self._log("Toggle")

    def Expand(self):
        self._log("Expand")

    def Collapse(self):
        self._log("Collapse")

    def DoDefaultAction(self):
        self._log("DoDefaultAction")

    def SetValue(self, text):
        self._log("SetValue", text)
        self.el.value = text

    @property
    def CurrentValue(self):
        return self.el.value


class FakeEl:
    """A live element; `rid=None` means no RuntimeId, `rid=()` an empty one."""

    def __init__(self, ct=CT_TEXT, name="", rid="auto", pid=100, aid="", role="", aria="", heading=80050,
                 value="", enabled=True, offscreen=False, patterns=(), children=(), cls="", supported=None):
        self.ct, self.name, self.pid, self.aid, self.role, self.aria = ct, name, pid, aid, role, aria
        self.rid = _next_rid() if rid == "auto" else rid
        self.heading, self.value, self.enabled, self.offscreen, self.cls = heading, value, enabled, offscreen, cls
        self.patterns = set(patterns)  # advertised (Is*PatternAvailable)
        self.supported = set(patterns) if supported is None else set(supported)  # what pattern() returns
        self.children = list(children)
        self.calls = []

    def prop(self, name):
        return {
            "ControlType": self.ct, "Name": self.name, "AutomationId": self.aid, "ClassName": self.cls,
            "IsEnabled": self.enabled, "IsOffscreen": self.offscreen, "AriaRole": self.role,
            "AriaProperties": self.aria, "HeadingLevel": self.heading, "ValueValue": self.value,
            "ProcessId": self.pid, "RuntimeId": self.rid,
            "IsInvokePatternAvailable": "invoke" in self.patterns,
            "IsTogglePatternAvailable": "toggle" in self.patterns,
            "IsExpandCollapsePatternAvailable": "expand_collapse" in self.patterns,
            "IsLegacyIAccessiblePatternAvailable": "legacy" in self.patterns,
            "IsValuePatternAvailable": "value" in self.patterns,
        }[name]

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()


class FakeCom:
    def __init__(self, root):
        self._root = root
        self.cache_calls = []

    def root(self):
        return self._root

    def find_windows(self, class_name):
        return [w for w in self._root.children if class_name is None or w.cls == class_name]

    def find_by_automation_id(self, element, automation_id):
        return [e for e in element.walk() if e.aid == automation_id]

    def build_cache(self, element, props, subtree=True):
        self.cache_calls.append((element, tuple(props), subtree))

        def wrap(el):
            return ub.CachedElement(element=el, props={p: el.prop(p) for p in props},
                                    children=[wrap(c) for c in el.children] if subtree else [])
        return wrap(element)

    def pattern(self, element, name):
        return FakePattern(element, name) if name in element.supported else None

    def runtime_id(self, element):
        return element.rid

    def process_id(self, element):
        return element.pid


def _doc(url, children=(), rid="auto", pid=100):
    return FakeEl(ct=CT_DOC, aid="RootWebArea", value=url, rid=rid, pid=pid, patterns=("value",),
                  children=children)


def _desktop(*windows):
    return FakeEl(ct=CT_PANE, name="Desktop", rid=(42, 1), pid=0, children=windows)


def _window(*docs, pid=100, cls="Chrome_WidgetWin_1"):
    return FakeEl(ct=CT_WINDOW, name="Gemini", pid=pid, cls=cls, children=docs)


def _tree(root, pids=(100,)):
    return ub.UiaTree(FakeCom(root), lambda pid: pid in pids)


def _ids(node):
    return [n.id for n, _ in node.walk()]


class PropertyMappingTest(unittest.TestCase):
    def test_node_fields(self):
        btn = FakeEl(ct=CT_BUTTON, name="Go", rid=(42, 7, 3), role="button", aria="pressed=false;haspopup=menu",
                     heading=80053, value="v", enabled=False, offscreen=True, aid="send",
                     patterns=("invoke", "toggle", "expand_collapse", "legacy", "value"))
        doc = _doc("https://gemini.google.com/app", children=[btn])
        tree = _tree(_desktop(_window(doc)))
        d = tree.documents()[0]
        n = tree.snapshot(d.id).children[0]
        self.assertIsInstance(n, Node)
        self.assertEqual(n.id, "42.7.3")
        self.assertEqual(n.ct, "Button")
        self.assertEqual(n.role, "button")
        self.assertEqual(n.name, "Go")
        self.assertEqual(n.aria, {"pressed": "false", "haspopup": "menu"})
        self.assertEqual(n.heading, 3)
        self.assertFalse(n.enabled)
        self.assertTrue(n.offscreen)
        self.assertEqual(n.value, "v")
        self.assertEqual(n.automation_id, "send")
        self.assertEqual(n.patterns, frozenset({"invoke", "toggle", "expand", "collapse", "default_action"}))

    def test_heading_offset_and_range(self):
        for raw, want in ((80050, None), (80051, 1), (80056, 6), (80057, None), (0, None), (None, None),
                          ("80052", None), (True, None)):
            with self.subTest(raw=raw):
                self.assertEqual(ub.heading_level(raw), want)

    def test_aria_properties_parsing(self):
        cases = {
            "": {},
            None: {},
            "level=2": {"level": "2"},
            "expanded=true;checked=mixed": {"expanded": "true", "checked": "mixed"},
            " a = 1 ; ;b=": {"a": "1", "b": ""},
            r"label=x\;y\=z;k=back\\slash": {"label": "x;y=z", "k": "back\\slash"},
            "novalue": {"novalue": ""},
            "=orphan;ok=1": {"ok": "1"},
        }
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(ub.parse_aria_properties(raw), want)

    def test_unknown_or_junk_values_degrade_safely(self):
        junk = object()  # e.g. the UIA "not supported" sentinel
        el = FakeEl(ct=99999, name=junk, rid=(42, 9), role=None, aria=junk, heading=junk, value=None,
                    enabled=junk, offscreen=None)
        doc = _doc("u", children=[el])
        tree = _tree(_desktop(_window(doc)))
        n = tree.snapshot(tree.documents()[0].id).children[0]
        self.assertEqual(n.ct, "99999")
        self.assertEqual((n.name, n.role, n.aria, n.heading, n.value), ("", "", {}, None, ""))
        self.assertTrue(n.enabled)
        self.assertFalse(n.offscreen)

    def test_control_type_names(self):
        self.assertEqual(ub.control_type_name(50030), "Document")
        self.assertEqual(ub.control_type_name(50011), "MenuItem")
        self.assertEqual(ub.control_type_name(None), "Unknown")


class IdTest(unittest.TestCase):
    def test_runtime_id_joined_and_stable_across_snapshots(self):
        a = FakeEl(rid=(42, 5, 6))
        doc = _doc("u", children=[a], rid=(42, 4))
        tree = _tree(_desktop(_window(doc)))
        d = tree.documents()[0]
        self.assertEqual(d.id, "42.4")
        s1, s2 = tree.snapshot(d.id), tree.snapshot(d.id)
        self.assertEqual(_ids(s1), ["42.4", "42.5.6"])
        self.assertEqual(_ids(s1), _ids(s2))

    def test_duplicate_and_missing_runtime_ids_disambiguated_never_empty(self):
        kids = [FakeEl(rid=(42, 9)), FakeEl(rid=(42, 9)), FakeEl(rid=None), FakeEl(rid=()),
                FakeEl(rid=None, children=[FakeEl(rid=None)]), FakeEl(rid=(42, 10))]
        doc = _doc("u", children=kids, rid=(42, 4))
        tree = _tree(_desktop(_window(doc)))
        snap = tree.snapshot(tree.documents()[0].id)
        ids = _ids(snap)
        self.assertEqual(len(ids), len(set(ids)), ids)
        self.assertTrue(all(ids))
        self.assertEqual(ids[0], "42.4")
        self.assertIn("42.10", ids)  # unique ids keep the plain form
        self.assertNotIn("42.9", ids)  # no member of a duplicate group claims the plain id
        self.assertTrue(all("~" in i for i in ids[1:6]))
        # disambiguated ids are still stable across snapshots and actionable
        self.assertEqual(ids, _ids(tree.snapshot(snap.id)))

    def test_descendant_sharing_the_document_id_is_suffixed(self):
        doc = _doc("u", children=[FakeEl(rid=(42, 4))], rid=(42, 4))
        tree = _tree(_desktop(_window(doc)))
        snap = tree.snapshot(tree.documents()[0].id)
        self.assertEqual(snap.id, "42.4")
        self.assertNotEqual(snap.children[0].id, "42.4")

    def test_documents_without_runtime_id_get_unique_ids(self):
        tree = _tree(_desktop(_window(_doc("a", rid=None), _doc("b", rid=None))))
        docs = tree.documents()
        self.assertEqual(len({d.id for d in docs}), 2)
        self.assertTrue(all(d.id for d in docs))
        snap = tree.snapshot(docs[1].id)
        self.assertEqual(snap.id, docs[1].id)
        self.assertEqual(snap.value, "b")


class DocumentsTest(unittest.TestCase):
    def test_root_web_areas_of_filtered_windows_with_url(self):
        conv = _doc("https://gemini.google.com/app/0123456789abcdef", children=[FakeEl(name="secret title")])
        top = _doc("app://bundle/src/top_bar/top_bar.html")
        other = _doc("https://example.com", pid=999)
        other_cls = _doc("https://x", pid=100)
        root = _desktop(_window(FakeEl(ct=CT_PANE, children=[conv]), top),
                        _window(other, pid=999),
                        _window(other_cls, cls="Notepad"))
        com = FakeCom(root)
        tree = ub.UiaTree(com, lambda pid: pid == 100)
        docs = tree.documents()
        self.assertEqual([d.value for d in docs],
                         ["https://gemini.google.com/app/0123456789abcdef", "app://bundle/src/top_bar/top_bar.html"])
        self.assertTrue(all(d.automation_id == "RootWebArea" and d.ct == "Document" for d in docs))
        self.assertTrue(all(d.children == [] for d in docs))  # shallow: no sidebar content is read here
        self.assertTrue(all(not sub for _, _, sub in com.cache_calls))

    def test_snapshot_unknown_document_raises_key_error(self):
        tree = _tree(_desktop(_window(_doc("u"))))
        with self.assertRaises(KeyError):
            tree.snapshot("nope")

    def test_snapshot_of_non_document_id_raises_key_error(self):
        btn = FakeEl(ct=CT_BUTTON, rid=(42, 77), patterns=("invoke",))
        tree = _tree(_desktop(_window(_doc("u", children=[btn]))))
        tree.snapshot(tree.documents()[0].id)
        with self.assertRaises(KeyError):
            tree.snapshot("42.77")

    def test_window_filter_is_required(self):
        with self.assertRaises(TypeError):
            ub.UiaTree(FakeCom(_desktop()))  # pylint: disable=no-value-for-parameter

    def test_desktop_root_keeps_only_filtered_top_level_windows(self):
        menu = FakeEl(ct=CT_MENUITEM, name="종료", rid=(42, 50), patterns=("invoke",))
        popup = _window(menu, cls="Chrome_WidgetWin_2")
        foreign = _window(FakeEl(name="not ours"), pid=555)
        tree = _tree(_desktop(_window(_doc("u")), popup, foreign))
        root = tree.desktop_root()
        self.assertEqual(root.id, "42.1")
        names = [n.name for n, _ in root.walk()]
        self.assertIn("종료", names)
        self.assertNotIn("not ours", names)
        tree.act("42.50", "invoke")
        self.assertEqual(menu.calls, [("invoke", "Invoke")])


class ActTest(unittest.TestCase):
    def setUp(self):
        self.btn = FakeEl(ct=CT_BUTTON, rid=(42, 20), patterns=("invoke", "toggle", "expand_collapse", "legacy"))
        self.edit = FakeEl(rid=(42, 21), patterns=("value",), value="old")
        self.plain = FakeEl(rid=(42, 22))
        self.liar = FakeEl(rid=(42, 23), patterns=("invoke",), supported=())
        self.doc = _doc("u", children=[self.btn, self.edit, self.plain, self.liar])
        self.tree = _tree(_desktop(_window(self.doc)))
        self.tree.snapshot(self.tree.documents()[0].id)

    def test_dispatch(self):
        for pattern, call in (("invoke", ("invoke", "Invoke")), ("toggle", ("toggle", "Toggle")),
                              ("expand", ("expand_collapse", "Expand")),
                              ("collapse", ("expand_collapse", "Collapse")),
                              ("default_action", ("legacy", "DoDefaultAction"))):
            with self.subTest(pattern=pattern):
                self.btn.calls.clear()
                self.tree.act("42.20", pattern)
                self.assertEqual(self.btn.calls, [call])

    def test_pattern_not_supported(self):
        for node_id, pattern in (("42.22", "invoke"), ("42.20", "bogus"), ("42.23", "invoke")):
            with self.subTest(node=node_id, pattern=pattern):
                with self.assertRaises(PatternNotSupported):
                    self.tree.act(node_id, pattern)
        self.assertEqual(self.liar.calls, [])

    def test_unknown_node_raises_key_error(self):
        with self.assertRaises(KeyError):
            self.tree.act("42.999", "invoke")

    def test_set_and_get_value(self):
        self.assertEqual(self.tree.get_value("42.21"), "old")
        self.tree.set_value("42.21", "line1\nline2")
        self.assertEqual(self.edit.calls, [("value", "SetValue", "line1\nline2")])
        self.assertEqual(self.tree.get_value("42.21"), "line1\nline2")
        with self.assertRaises(PatternNotSupported):
            self.tree.set_value("42.22", "x")
        with self.assertRaises(PatternNotSupported):
            self.tree.get_value("42.22")


class LoadComTest(unittest.TestCase):
    def test_sandbox_refuses_load_com(self):
        self.assertEqual(os.environ.get("AGY_DR_TEST_SANDBOX"), "1")
        with mock.patch.dict(sys.modules, {"comtypes": None, "comtypes.client": None}):
            with self.assertRaises(ub.HostRefused) as cm:
                ub.load_com()
        self.assertEqual(cm.exception.reason, "sandbox")

    def test_sandbox_refuses_every_real_com_call(self):
        com = ub.ComtypesUia(uia=mock.Mock(), uia_module=mock.Mock(), com_error=OSError)
        el = mock.Mock()
        for call in (lambda: com.root(), lambda: com.find_windows("Chrome_WidgetWin_1"),
                     lambda: com.find_by_automation_id(el, "RootWebArea"),
                     lambda: com.build_cache(el, ["Name"]), lambda: com.pattern(el, "invoke"),
                     lambda: com.runtime_id(el), lambda: com.process_id(el)):
            with self.assertRaises(ub.HostRefused):
                call()
        self.assertEqual(el.mock_calls, [])

    def _unsandboxed(self):
        env = mock.patch.dict(os.environ, {"AGY_DR_TEST_SANDBOX": ""})
        env.start()
        self.addCleanup(env.stop)

    def test_missing_comtypes_raises_uia_unavailable_with_setup_hint(self):
        self._unsandboxed()
        with mock.patch.dict(sys.modules, {"comtypes": None, "comtypes.client": None, "comtypes.gen": None}), \
                mock.patch.object(ub.sys, "platform", "win32"):
            with self.assertRaises(ub.UiaUnavailable) as cm:
                ub.load_com()
        self.assertEqual(cm.exception.reason, "comtypes")
        self.assertIn("setup", cm.exception.hint)

    def test_typelib_failure_raises_uia_unavailable(self):
        self._unsandboxed()
        fake_client = types.ModuleType("comtypes.client")

        def boom(*_a, **_k):
            raise OSError("no typelib")
        fake_client.GetModule = boom
        fake_client.CreateObject = boom
        fake_ct = types.ModuleType("comtypes")
        fake_ct.client = fake_client
        fake_ct.COMError = type("COMError", (Exception,), {})
        with mock.patch.dict(sys.modules, {"comtypes": fake_ct, "comtypes.client": fake_client,
                                           "comtypes.gen": None}), \
                mock.patch.object(ub.sys, "platform", "win32"):
            with self.assertRaises(ub.UiaUnavailable) as cm:
                ub.load_com()
        self.assertEqual(cm.exception.reason, "comtypes")

    def test_non_windows_platform(self):
        self._unsandboxed()
        with mock.patch.dict(sys.modules, {"comtypes": None}), mock.patch.object(ub.sys, "platform", "linux"):
            with self.assertRaises(ub.UiaUnavailable) as cm:
                ub.load_com()
        self.assertEqual(cm.exception.reason, "platform")

    def test_module_has_no_top_level_comtypes(self):
        self.assertNotIn("comtypes", vars(ub))


class ComErrorMappingTest(unittest.TestCase):
    """ComtypesUia maps COM failures onto the errors steps.BackendError expects (fake objects only)."""

    def setUp(self):
        env = mock.patch.dict(os.environ, {"AGY_DR_TEST_SANDBOX": ""})
        env.start()
        self.addCleanup(env.stop)
        self.err = type("COMError", (Exception,), {})
        self.com = ub.ComtypesUia(uia=mock.Mock(), uia_module=mock.Mock(), com_error=self.err)

    def _raising(self, hresult):
        el = mock.Mock()
        el.GetCurrentPattern.side_effect = self.err(hresult, "x", None)
        el.BuildUpdatedCache.side_effect = self.err(hresult, "x", None)
        return el

    def test_element_not_available_is_key_error(self):
        el = self._raising(ub.UIA_E_ELEMENTNOTAVAILABLE)
        with self.assertRaises(KeyError):
            self.com.pattern(el, "invoke")
        with self.assertRaises(KeyError):
            self.com.build_cache(el, ["Name"])

    def test_other_com_error_is_os_error(self):
        el = self._raising(-2147467259)  # E_FAIL
        with self.assertRaises(OSError) as cm:
            self.com.pattern(el, "invoke")
        self.assertIsInstance(cm.exception, ub.UiaComError)

    def test_unknown_property_or_pattern_name(self):
        with self.assertRaises(ValueError):
            self.com.build_cache(mock.Mock(), ["NoSuchProperty"])
        with self.assertRaises(PatternNotSupported):
            self.com.pattern(mock.Mock(), "bogus")


if __name__ == "__main__":
    unittest.main()

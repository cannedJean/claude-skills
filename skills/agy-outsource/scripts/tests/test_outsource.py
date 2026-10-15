"""Tests for agy-outsource without agy: gates on hand-made deliveries, plan validation, and full runs of
examples/slugkit with tests/fake_agy_task.py standing in for agy-delegate.

  python -m unittest discover -s scripts/tests -v      (from the skill directory)
"""
import _thread
import argparse
import copy
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
SKILL = SCRIPTS.parent
PLAN = SKILL / "examples" / "slugkit" / "plan.json"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(HERE))

import fake_agy_task  # noqa: E402
import gates  # noqa: E402
import outsource  # noqa: E402

# every subprocess below inherits these: a stand-in for `claude -p` and a private place for contractor job dirs
JOBS = Path(tempfile.mkdtemp(prefix="agy-jobs-"))
os.environ["OUTSOURCE_CLAUDE"] = str(HERE / "fake_claude.py")
os.environ["AGY_OUTSOURCE_JOBS"] = str(JOBS)


def tearDownModule():
    shutil.rmtree(JOBS, ignore_errors=True)

UNIT = {"id": "U1", "deliverables": ["pkg/mod.py"],
        "interface": [{"file": "pkg/mod.py", "symbol": "f", "signature": "def f(a, *, b=1) -> int"},
                      {"file": "pkg/mod.py", "symbol": "C", "signature": "class C"},
                      {"file": "pkg/mod.py", "symbol": "C.m", "signature": "def m(self, x)"},
                      {"file": "pkg/mod.py", "symbol": "LIMIT", "signature": "LIMIT"}]}
GOOD_SRC = "import os\n\nLIMIT = 3\n\n\ndef f(a, *, b=1) -> int:\n    return a + b\n\n\nclass C:\n    def m(self, x):\n        return x\n"


class GateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "pkg").mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, src=GOOD_SRC, manifest=None):
        (self.tmp / "pkg" / "mod.py").write_text(src, encoding="utf-8")
        if manifest is not False:
            m = manifest or {"unit": "U1", "files": ["pkg/mod.py"], "assumptions": [], "self_check": "x"}
            (self.tmp / gates.MANIFEST).write_text(json.dumps(m), encoding="utf-8")

    def test_good_delivery_passes_static_gates(self):
        self.write()
        res = gates.static_gates(self.tmp, UNIT, {"created": ["pkg/mod.py", gates.MANIFEST], "modified": [], "deleted": []},
                                 {}, {"imports": {"stdlib": True}, "forbid_patterns": [r"^\s*print\("]})
        self.assertTrue(all(g["ok"] for g in res), [g for g in res if not g["ok"]])

    def test_signature_mismatches(self):
        for bad in ("def f(a, b=1) -> int:", "def f(x, *, b=1) -> int:", "def f(a, *, b) -> int:", "async def f(a, *, b=1) -> int:"):
            self.write(GOOD_SRC.replace("def f(a, *, b=1) -> int:", bad))
            g = gates.check_interface(self.tmp, UNIT)
            self.assertFalse(g["ok"], bad)

    def test_missing_method_and_constant(self):
        self.write(GOOD_SRC.replace("    def m(self, x):\n        return x\n", "    pass\n").replace("LIMIT = 3", "OTHER = 3"))
        probs = " ".join(gates.check_interface(self.tmp, UNIT)["problems"])
        self.assertIn("C.m", probs)
        self.assertIn("LIMIT", probs)

    def test_manifest_problems(self):
        self.write(manifest=False)
        self.assertFalse(gates.check_manifest(self.tmp, UNIT)["ok"])
        self.write(manifest={"unit": "U9", "files": ["pkg/mod.py", "extra.py"]})
        probs = " ".join(gates.check_manifest(self.tmp, UNIT)["problems"])
        self.assertIn("U9", probs)
        self.assertIn("extra.py", probs)
        (self.tmp / gates.MANIFEST).write_text('```json\n{"unit": "U1", "files": ["./pkg/mod.py"], "assumptions": [], "self_check": ""}\n```', encoding="utf-8")
        self.assertTrue(gates.check_manifest(self.tmp, UNIT)["ok"])

    def test_scope(self):
        frozen = {"tests/test_mod.py": "abc"}
        g = gates.check_scope({"created": ["pkg/mod.py", "NOTES.md"], "modified": ["tests/test_mod.py"], "deleted": []}, UNIT, frozen)
        self.assertEqual(len(g["problems"]), 2)

    def test_imports_and_forbid(self):
        self.write("import requests\nfrom pkg import other\nprint('x')\n" + GOOD_SRC)
        self.assertIn("requests", " ".join(gates.check_imports(self.tmp, UNIT, {"stdlib": True})["problems"]))
        self.assertTrue(gates.check_imports(self.tmp, UNIT, {"stdlib": True, "allow": ["requests"]})["ok"])
        self.assertFalse(gates.check_forbid(self.tmp, UNIT, [r"^\s*print\("])["ok"])

    def test_syntax(self):
        self.write("def f(:\n")
        self.assertFalse(gates.check_syntax(self.tmp, UNIT)["ok"])

    def test_stubs_compile_and_raise(self):
        src = gates.stub_sources([UNIT])["pkg/mod.py"]
        ns = {}
        exec(compile(src, "stub", "exec"), ns)
        with self.assertRaises(NotImplementedError):
            ns["f"](1)
        with self.assertRaises(NotImplementedError):
            ns["C"]().m(1)

    def test_snapshot_diff_ignores_caches(self):
        before = gates.snapshot(self.tmp)
        self.write()
        (self.tmp / "pkg" / "__pycache__").mkdir()
        (self.tmp / "pkg" / "__pycache__" / "mod.cpython-312.pyc").write_bytes(b"x")
        d = gates.diff(before, gates.snapshot(self.tmp))
        self.assertEqual(d["created"], [gates.MANIFEST, "pkg/mod.py"])

    def test_hook_files_are_ignored_only_when_asked_and_only_if_they_are_the_hooks(self):
        # the same rule as agy_task.is_own_hook_file: exactly the bytes agy-delegate's Confinement writes
        agy_task = SKILL.parent / "agy-delegate" / "scripts" / "agy_task.py"
        own = outsource.own_hook_digests(agy_task)
        self.assertIn(".agents/hooks.json", own)
        spec = importlib.util.spec_from_file_location("_agy_task_test", agy_task)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        with mod.Confinement(self.tmp, self.tmp / "log" / "confine.jsonl"):
            written = sorted(p.relative_to(self.tmp).as_posix() for p in (self.tmp / ".agents").iterdir())
            self.assertEqual(written, sorted(own))
            snap = gates.snapshot(self.tmp, own)  # a confined run
            self.assertFalse(any(r.startswith(".agents/") for r in snap), snap)
            self.assertIn(".agents/hooks.json", gates.snapshot(self.tmp))  # --no-confine: agy's own work
            hooks = self.tmp / ".agents" / "hooks.json"
            # agy-delegate's key alone, but another command: not the hook's own file (a killed session left it)
            hooks.write_text(json.dumps({mod.HOOK_KEY: {"PreToolUse": [{"matcher": "*", "hooks": [
                {"type": "command", "command": "calc.exe"}]}]}}), encoding="utf-8")
            self.assertIn(".agents/hooks.json", gates.snapshot(self.tmp, own))
            if ".agents/confine.cmd" in own:  # any launcher content but agy-delegate's own is a change too
                (self.tmp / ".agents" / "confine.cmd").write_bytes(b"@calc.exe\r\n")
                self.assertIn(".agents/confine.cmd", gates.snapshot(self.tmp, own))
        # a stand-in agy_task.py that does not say what it writes: nothing in .agents/ is left out
        self.assertEqual(outsource.own_hook_digests(HERE / "fake_agy_task.py"), {})

    def test_imports_gate_uses_the_given_project_modules(self):
        # a leftover yaml.py in the job dir is not in the verify tree: `import yaml` would load an installed package
        self.write("import yaml\n" + GOOD_SRC)
        (self.tmp / "yaml.py").write_text("X = 1\n", encoding="utf-8")
        self.assertTrue(gates.check_imports(self.tmp, UNIT, {"stdlib": True})["ok"])  # old behaviour: work dir
        g = gates.check_imports(self.tmp, UNIT, {"stdlib": True}, local={"pkg"})
        self.assertIn("yaml", " ".join(g["problems"]))


class PlanTests(unittest.TestCase):
    def test_example_plan_is_valid(self):
        errors, warnings = outsource.validate_plan(outsource.load_plan(PLAN))
        self.assertEqual(errors, [])
        self.assertEqual(outsource.topo_order(outsource.load_plan(PLAN)), ["U1-normalize", "U2-slugify", "U3-registry"])

    def test_bad_plans(self):
        base = outsource.load_plan(PLAN)
        cases = {
            "cycle": lambda p: p["units"][0]["depends_on"].append("U2-slugify"),
            "unknown dependency": lambda p: p["units"][1]["depends_on"].append("nope"),
            "already owned": lambda p: p["units"][1]["deliverables"].append("slugkit/normalize.py"),
            "unsafe": lambda p: p["units"][0]["deliverables"].append("../x.py"),
            "no tests": lambda p: p["units"][2].update(tests=[], hidden_tests=[]),
            "not a deliverable": lambda p: p["units"][1]["interface"].append({"file": "other.py", "symbol": "x"}),
            "does not parse": lambda p: p["units"][1]["interface"][0].update(signature="def slugify(text"),
        }
        for needle, mutate in cases.items():
            p = copy.deepcopy(base)
            mutate(p)
            errors, _ = outsource.validate_plan(p)
            self.assertTrue(any(needle in e for e in errors), (needle, errors))

    def test_work_order_mentions_the_contract(self):
        p = outsource.load_plan(PLAN)
        wo = outsource.render_work_order(p, p["units"][1], Path("WORK"))
        for s in ("# Work order U2-slugify", "`slugkit/slug.py`", "max_len: Optional[int] = None",
                  "from slugkit.normalize import strip_accents", "DELIVERY.json", '"files": ["slugkit/slug.py"]'):
            self.assertIn(s, wo)
        self.assertNotIn("hidden/", wo)
        self.assertNotIn(str(p["_dir"]), wo)

    def test_scrub_removes_paths(self):
        d = Path(tempfile.gettempdir()) / "x_verify"
        text = f'File "{d}\\hidden/test_a.py", line 3'
        self.assertEqual(outsource.scrub(text, [d]), 'File "hidden/test_a.py", line 3')

    def test_work_order_lists_the_directory_and_asks_nothing_redundant(self):
        p = outsource.load_plan(PLAN)
        wo = outsource.render_work_order(p, p["units"][1], Path("W"))
        self.assertIn("- `slugkit/normalize.py` (read-only)", wo)  # dependency
        self.assertIn("- `slugkit/__init__.py` (read-only)", wo)  # the dependency's context file
        self.assertIn("- `slugkit/slug.py` does not exist yet: you create it", wo)
        self.assertIn("reply\n   with the single word DONE", wo)
        for gone in ('"self_check"', '"symbols"'):
            self.assertNotIn(gone, wo)
        retry = outsource.render_work_order(p, p["units"][1], Path("W"), present={
            "slugkit/normalize.py": "", "slugkit/__init__.py": "", "tests/test_slug.py": "", "slugkit/slug.py": "",
            "NOTES.md": "", "DELIVERY.json": ""})
        self.assertIn("- `slugkit/slug.py` (yours, from the previous attempt: fix it in place)", retry)
        self.assertIn("- `NOTES.md` (left over", retry)
        self.assertNotIn("`DELIVERY.json` (", retry)

    def test_work_order_inlines_existing_files_within_the_cap(self):
        p = outsource.load_plan(PLAN)
        w = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, w, True)
        (w / "tests").mkdir()
        (w / "tests" / "test_slug.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
        (w / "big.txt").write_text("x" * (outsource.INLINE_CAP + 1), encoding="utf-8")
        p["units"][1]["context_files"] = ["big.txt"]
        wo = outsource.render_work_order(p, p["units"][1], w, present={"tests/test_slug.py": "", "big.txt": ""})
        self.assertIn("### `tests/test_slug.py`\n```python\ndef test_x():", wo)
        self.assertNotIn("### `big.txt`", wo)

    def test_inlined_text_holding_a_fence_appears_exactly_as_written(self):
        # a ``` line of the file's own must not end its block: agy cannot open the file to see where it ends
        p = outsource.load_plan(PLAN)
        w = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, w, True)
        text = ('"""Usage:\n\n```python\n>>> slugify("A b")\n\'a-b\'\n```\n\n````md\nfour\n````\n"""\n'
                "def test_x():\n    assert True")
        (w / "tests").mkdir()
        (w / "tests" / "test_slug.py").write_text(text + "\n", encoding="utf-8")
        wo = outsource.render_work_order(p, p["units"][1], w, present={"tests/test_slug.py": ""}, inlined=[])
        block = wo.split("### `tests/test_slug.py`\n", 1)[1]
        fence = re.match(r"`+", block).group(0)
        self.assertEqual(len(fence), 5)  # longer than the longest run inside (4)
        self.assertEqual(block[len(fence):].split("\n", 1)[1].split("\n" + fence + "\n", 1)[0], text)
        self.assertEqual(outsource.fenced("a", "py"), "```py\na\n```")

    def test_work_order_inlines_direct_inputs_only(self):
        p = outsource.load_plan(PLAN)
        u4 = {"id": "U4", "title": "t", "goal": "g", "deliverables": ["slugkit/x.py"], "interface": [], "spec": [],
              "examples": [], "constraints": [], "depends_on": ["U2-slugify"], "context_files": [],
              "tests": ["tests/test_x.py"], "hidden_tests": []}
        p["units"].append(u4)
        self.assertEqual(outsource.unit_inputs(p, u4), {
            "slugkit/__init__.py": "dep_context", "slugkit/normalize.py": "indirect", "slugkit/slug.py": "dep",
            "tests/test_x.py": "test"})
        w = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, w, True)
        present = ["slugkit/__init__.py", "slugkit/normalize.py", "slugkit/slug.py", "tests/test_x.py", "NOTES.md"]
        for rel in present:
            (w / rel).parent.mkdir(parents=True, exist_ok=True)
            (w / rel).write_text(f"# {rel}\n", encoding="utf-8")
        inlined = []
        wo = outsource.render_work_order(p, u4, w, present=dict.fromkeys(present, ""), inlined=inlined)
        self.assertEqual(inlined, ["slugkit/slug.py", "tests/test_x.py"])
        for rel in ("slugkit/normalize.py", "slugkit/__init__.py", "NOTES.md"):
            self.assertNotIn(f"### `{rel}`", wo)
        self.assertIn("- `slugkit/normalize.py` (read-only; an indirect dependency", wo)
        self.assertIn("from slugkit.normalize import strip_accents", wo)  # its interface is still there

    def test_inlining_skips_links_and_a_claude_contractors_own_files(self):
        p = outsource.load_plan(PLAN)
        u2 = p["units"][1]
        w = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, w, True)
        for rel in ("slugkit/normalize.py", "slugkit/slug.py"):
            (w / rel).parent.mkdir(parents=True, exist_ok=True)
            (w / rel).write_text(f"# {rel}\n", encoding="utf-8")
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside, True)
        (outside / "secret.py").write_text("SECRET = 1\n", encoding="utf-8")
        (w / "tests").mkdir()
        os.link(outside / "secret.py", w / "tests" / "test_slug.py")  # e.g. a contractor's link to a hidden test
        present = dict.fromkeys(["slugkit/normalize.py", "slugkit/slug.py", "tests/test_slug.py"], "")
        inlined = []
        wo = outsource.render_work_order(p, u2, w, present=present, inlined=inlined)
        self.assertNotIn("SECRET", wo)
        self.assertEqual(inlined, ["slugkit/normalize.py"])  # its own slug.py is inlined but not --inlined
        self.assertIn("### `slugkit/slug.py`", wo)
        # a Claude contractor must Read a file before its Edit tool changes it: its own files are not inlined
        wo = outsource.render_work_order(p, u2, w, present=present, inlined=[], inline_own=False)
        self.assertNotIn("### `slugkit/slug.py`", wo)
        self.assertIn("### `slugkit/normalize.py`", wo)

    def test_effort_ladder(self):
        p = outsource.load_plan(PLAN)
        a = type("A", (), {"effort": None})()
        u = p["units"][0]
        self.assertEqual([outsource.effort_for(p, u, a, n) for n in (1, 2, 3, 4)], ["low", "medium", "high", "high"])
        self.assertEqual(outsource.effort_for(dict(p, effort="low"), dict(u, effort="medium"), a, 3), "medium")
        self.assertEqual(outsource.effort_for(dict(p, effort="low"), u, a, 3), "low")
        a.effort = "high"
        self.assertEqual(outsource.effort_for(p, dict(u, effort="medium"), a, 1), "high")
        bad = copy.deepcopy(p)
        bad["units"][0]["effort"] = "max"
        self.assertTrue(any("effort must be" in e for e in outsource.validate_plan(bad)[0]))
        bad = copy.deepcopy(p)
        bad["effort"] = "max"
        for u in bad["units"]:  # a bad plan-level value is reported even when every unit pins its own
            u["effort"] = "low"
        self.assertTrue(any("effort must be" in e for e in outsource.validate_plan(bad)[0]))

    def test_execution_knobs_neither_void_the_audit_nor_stale_the_unit(self):
        p = outsource.load_plan(PLAN)
        u = p["units"][1]
        state = {"units": {}}
        pinned = dict(u, effort="high", max_attempts=5)
        self.assertEqual(outsource.audit_hash(p, pinned), outsource.audit_hash(p, u))
        self.assertEqual(outsource.spec_hash(p, pinned, state), outsource.spec_hash(p, u, state))
        changed = dict(u, spec=u["spec"] + ["One more rule."])
        self.assertNotEqual(outsource.audit_hash(p, changed), outsource.audit_hash(p, u))
        self.assertNotEqual(outsource.spec_hash(p, changed, state), outsource.spec_hash(p, u, state))

    def test_a_dependencys_context_file_stales_its_dependents(self):
        # U2 gets U1's context file slugkit/__init__.py in its work and verify dirs: a change there re-runs U2
        spec = Path(tempfile.mkdtemp()) / "slugkit"
        self.addCleanup(shutil.rmtree, spec.parent, True)
        shutil.copytree(PLAN.parent, spec, ignore=shutil.ignore_patterns(".agy_outsource", "audit.json"))
        p = outsource.load_plan(spec / "plan.json")
        state = {"units": {}}
        before = {u["id"]: outsource.spec_hash(p, u, state) for u in p["units"]}
        init = spec / "project" / "slugkit" / "__init__.py"
        init.write_text(init.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
        after = {u["id"]: outsource.spec_hash(p, u, state) for u in p["units"]}
        self.assertEqual(p["units"][1]["context_files"], [])  # U2 has the file only through its dependency U1
        self.assertNotEqual(after["U2-slugify"], before["U2-slugify"])
        self.assertNotEqual(after["U1-normalize"], before["U1-normalize"])  # its own context file

    def test_editing_the_work_order_template_stales_no_unit(self):
        p = outsource.load_plan(PLAN)
        state = {"units": {}}
        before = [outsource.spec_hash(p, u, state) for u in p["units"]]
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        edited = d / "work_order.md"
        edited.write_text(outsource.PROMPT.read_text(encoding="utf-8") + "\nOne more line of wording.\n",
                          encoding="utf-8")
        with mock.patch.object(outsource, "PROMPT", edited):
            self.assertIn("One more line of wording.", outsource.render_work_order(p, p["units"][0], Path("W")))
            self.assertEqual([outsource.spec_hash(p, u, state) for u in p["units"]], before)

    def test_feedback_never_quotes_a_hidden_test(self):
        hidden = ('import unittest\n\nclass TestSlugifyHidden(unittest.TestCase):\n    def test_symbols(self):\n'
                  '        self.assertEqual(slugify("C++ & C#"), "c-c")\n')
        visible = "import unittest\n\nfrom slugkit.slug import slugify\n"
        needles = outsource.hidden_quote_needles([hidden], [visible])
        self.assertIn('self.assertEqual(slugify("C++ & C#"), "c-c")', needles)
        self.assertNotIn("import unittest", needles)  # short, and the visible test has it anyway
        printed = ("LEAK start\n" + hidden + "Traceback (most recent call last):\n"
                   "  from slugkit.slug import slugify\nValueError: boom\n")
        results = [{"gate": "tests:visible", "ok": False, "output_tail": printed},
                   {"gate": "tests:hidden", "ok": False, "output_tail": "E   AssertionError: " + hidden}]
        for level in ("summary", "tail"):
            fb = outsource.render_feedback(1, 3, results, level, ["hidden/test_slug_hidden.py"], quoted=needles)
            self.assertNotIn("C++ & C#", fb, level)
            self.assertNotIn("TestSlugifyHidden", fb, level)
            self.assertIn("LEAK start", fb)
            self.assertIn("ValueError: boom", fb)
            self.assertIn(outsource.QUOTE_DROPPED, fb)

    def test_a_hidden_test_may_not_be_another_units_input(self):
        for key in ("tests", "context_files"):
            p = outsource.load_plan(PLAN)
            p["units"][1][key].append(p["units"][0]["hidden_tests"][0])
            errors, _ = outsource.validate_plan(p)
            self.assertTrue(any("is a hidden test" in e for e in errors), (key, errors))
        p = outsource.load_plan(PLAN)  # the same file name elsewhere (e.g. under project_root) counts too
        p["units"][2]["context_files"].append("other/" + Path(p["units"][0]["hidden_tests"][0]).name)
        self.assertTrue(any("is a hidden test" in e for e in outsource.validate_plan(p)[0]))

    def test_hidden_peek_tells_calls_that_ran_from_denied_ones(self):
        p = outsource.load_plan(PLAN)
        w = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, w, True)
        (w / ".agy_task" / "s").mkdir(parents=True)
        events = w / ".agy_task" / "s" / "events.jsonl"
        name = Path(p["units"][0]["hidden_tests"][0]).name
        ev = lambda st: json.dumps({"step_update": {"step_index": 2, "step_type": "tool", "state": st,  # noqa: E731
                                                    "tool_info": {"name": "view_file", "parameters": {"AbsolutePath": name}}}})
        peek = lambda **kw: outsource.hidden_peek(p, p["units"][0], w, **kw)  # noqa: E731
        events.write_text(ev("ACTIVE") + "\n" + ev("ERROR"), encoding="utf-8")  # the hook denied it
        self.assertEqual((peek(), peek(states=("ERROR",))), ([], [name]))
        events.write_text(ev("ACTIVE") + "\n" + ev("DONE"), encoding="utf-8")  # it ran
        self.assertEqual((peek(), peek(states=("ERROR",))), ([name], []))
        events.write_text(ev("ACTIVE"), encoding="utf-8")  # killed mid-call: it may have read the file
        self.assertEqual(peek(), [name])
        # the prompt or feedback echoed in the stream is not a tool call
        events.write_text(json.dumps({"text": f"FAILED hidden/{name}::test_x"}), encoding="utf-8")
        self.assertEqual((peek(), peek(states=("ERROR",))), ([], []))

    def test_hidden_peek_reads_only_the_given_attempts_events(self):
        p = outsource.load_plan(PLAN)
        w = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, w, True)
        name = Path(p["units"][0]["hidden_tests"][0]).name
        ev = json.dumps({"step_update": {"step_index": 1, "step_type": "tool", "state": "DONE",
                                         "tool_info": {"name": "find_by_name", "parameters": {"Pattern": name}}}})
        for stamp, text in (("a1", ev), ("a2", "")):
            (w / ".agy_task" / stamp).mkdir(parents=True)
            (w / ".agy_task" / stamp / "events.jsonl").write_text(text, encoding="utf-8")
        self.assertEqual(outsource.hidden_peek(p, p["units"][0], w), [name])  # every attempt
        self.assertEqual(outsource.hidden_peek(p, p["units"][0], w, files=[w / ".agy_task" / "a2" / "events.jsonl"]),
                         [])  # attempt 2 alone: attempt 1's hit is not its own

    def test_safe_rel_refuses_rooted_and_look_alike_paths(self):
        for bad in ("/pkg/a.py", "\\pkg\\a.py", "C:/x.py", "c:x.py", "~/x.py", "../x.py", "a/../../x.py",
                    "a/.../x.py", "a/. ./x.py", "a/ /x.py", "//server/share/x.py", "", "DELIVERY.json"):
            self.assertFalse(outsource.safe_rel(bad), bad)
        for good in ("pkg/a.py", "a.py", "pkg/sub/a_b.py", "./pkg/a.py", ".hidden.py"):
            self.assertTrue(outsource.safe_rel(good), good)
        p = outsource.load_plan(PLAN)
        p["units"][0]["deliverables"].append("/slugkit/x.py")
        self.assertTrue(any("unsafe deliverable" in e for e in outsource.validate_plan(p)[0]))

    def test_check_validates_the_roles(self):
        for roles_, needle in (({}, "lead"), ({"lead": "opus", "auditor": "opus"}, "same model family"),
                               ({"lead": "sonnet"}, "same model family"), ({"lead": "opus", "auditor": "agy"}, "Claude"),
                               ("opus", "must be an object")):
            p = outsource.load_plan(PLAN)
            p["roles"] = roles_
            errors, _ = outsource.validate_plan(p)
            self.assertTrue(any(e.startswith("roles:") and needle in e for e in errors), (roles_, errors))

    def test_hidden_summary_hides_the_hidden_test_file_under_pytest_and_mini_test(self):
        hidden = ["hidden/test_slug_hidden.py", "hidden/test_registry_hidden.py"]
        pytest_tail = ("F.\n=== FAILURES ===\n___ TestSlugifyHidden.test_bad_max_len ___\n"
                       "hidden/test_slug_hidden.py:20: in test_bad_max_len\n    slugify('x', max_len=0)\n"
                       "E   AssertionError: ValueError not raised\n=== short test summary info ===\n"
                       "FAILED hidden/test_slug_hidden.py::TestSlugifyHidden::test_bad_max_len - AssertionError: "
                       "ValueError not raised\n1 failed, 6 passed in 0.05s\n")
        mini_tail = ("FAIL: test_bad_max_len (_mt_test_slug_hidden.TestSlugifyHidden.test_bad_max_len)\n"
                     "AssertionError: ValueError not raised\nFAILED hidden\\test_slug_hidden.py::test_x\n"
                     "mini_test: 6 passed, 1 failed, 0 skipped\n")
        for tail in (pytest_tail, mini_tail):
            for level in ("summary", "tail"):
                out = outsource.hidden_summary(tail, level, hidden)
                self.assertNotIn("test_slug_hidden", out, (level, out))
                self.assertIn("test_bad_max_len", out)  # the test function names stay
                self.assertIn(outsource.HIDDEN_TOKEN, out)
        self.assertIn("FAILED <hidden test>::TestSlugifyHidden::test_bad_max_len",
                      outsource.hidden_summary(pytest_tail, "summary", hidden))
        long = "E   AssertionError: " + "x" * 5000
        self.assertLessEqual(len(outsource.hidden_summary(long, "summary", hidden)), outsource.LINE_CAP)

    def test_work_order_fits_the_windows_command_line(self):
        p = outsource.load_plan(PLAN)
        u2 = p["units"][1]
        w = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, w, True)
        # under INLINE_CAP together, but full of quotes and backslashes that list2cmdline escapes
        files = {"slugkit/normalize.py": 'S = "\\\\"\n' * 900, "tests/test_slug.py": 'T = "a"\n' * 1800}
        for rel, text in files.items():
            (w / rel).parent.mkdir(parents=True, exist_ok=True)
            (w / rel).write_text(text, encoding="utf-8")
        self.assertLess(sum(map(len, files.values())), outsource.INLINE_CAP)
        hidden = ["hidden/test_slug_hidden.py"]
        typical = [{"gate": "tests:visible", "ok": False, "output_tail": "AssertionError: 'a' != 'b'\n" * 100},
                   {"gate": "tests:hidden", "ok": False, "output_tail": "E   AssertionError: 'x' != 'y'\n" * 80}]
        hostile = [{"gate": "scope", "ok": False, "problems": ["extra file written: x" * 200] * 50},
                   {"gate": "tests:visible", "ok": False, "output_tail": '"' * 4000},
                   {"gate": "tests:hidden", "ok": False, "output_tail": ("E   AssertionError: " + '"' * 900 + "\n") * 80}]
        for results, some_inlined in ((typical, True), (hostile, False)):
            fb = outsource.render_feedback(1, 3, results, "summary", hidden)
            inlined = []
            wo = outsource.render_work_order(p, u2, w, fb, present=dict.fromkeys(files, ""), inlined=inlined)
            self.assertLessEqual(len(subprocess.list2cmdline([wo])), outsource.PROMPT_BUDGET)
            self.assertLessEqual(len(subprocess.list2cmdline(["x" * 250] * 10 + [wo])), 32767)
            self.assertEqual(bool(inlined), some_inlined)  # what still fits is inlined
            self.assertIn("Previous attempt was REJECTED", wo)
        self.assertIn("cut to fit", wo)

    def test_project_modules_are_what_the_verify_tree_holds(self):
        p = outsource.load_plan(PLAN)
        self.assertEqual(outsource.project_modules(p, p["units"][1]), {"slugkit", "tests"})
        u = {"id": "X", "deliverables": ["tool.py"], "context_files": [], "tests": [], "depends_on": []}
        self.assertEqual(outsource.project_modules(dict(p, units=p["units"] + [u]), u), {"tool"})

    def test_work_order_states_the_confinement(self):
        p = outsource.load_plan(PLAN)
        self.assertIn("any tool call on a path outside it is denied", outsource.render_work_order(p, p["units"][0], Path("W")))
        self.assertNotIn("is denied", outsource.render_work_order(p, p["units"][0], Path("W"), confined=False))


class E2EBase(unittest.TestCase):
    def setUp(self):
        # a copy of the example: `run` seals the hidden tests in place, the shipped example must stay readable
        spec = Path(tempfile.mkdtemp()) / "slugkit"
        self.addCleanup(shutil.rmtree, spec.parent, True)
        shutil.copytree(PLAN.parent, spec, ignore=shutil.ignore_patterns(".agy_outsource", "audit.json"))
        self.plan = spec / "plan.json"
        rc, res = self.cli("audit")  # an independent model must pass the tests before any run
        self.assertEqual(rc, 0, res)

    def cli(self, *args, **env):
        cp = subprocess.run([sys.executable, str(SCRIPTS / "outsource.py"), args[0], str(self.plan), *args[1:]],
                            capture_output=True, text=True, encoding="utf-8", env=dict(os.environ, **env))
        return cp.returncode, json.loads(cp.stdout)

    def run_plan(self, scenario, *extra, out_dir=None, **more_env):
        out = out_dir
        if out is None:
            out = Path(tempfile.mkdtemp())
            self.addCleanup(shutil.rmtree, out, True)
        env = dict(os.environ, AGY_TASK=str(HERE / "fake_agy_task.py"), FAKE_AGY_SCENARIO=scenario, PYTHONIOENCODING="utf-8",
                   **more_env)
        cp = subprocess.run([sys.executable, str(SCRIPTS / "outsource.py"), "run", str(self.plan), "--out", str(out), *extra],
                            capture_output=True, text=True, encoding="utf-8", env=env)
        res = json.loads(cp.stdout)
        return cp.returncode, res, out

    def unit_flags(self, out, uid):
        return json.loads((out / "state.json").read_text(encoding="utf-8"))["units"][uid].get("flags", [])


class EndToEnd(E2EBase):

    def test_confinement_is_reported(self):
        rc, res, out = self.run_plan("U2-slugify=outside,U3-registry=inlinedview")
        self.assertEqual(rc, 0, res)
        self.assertIn("outside_access_denied", self.unit_flags(out, "U2-slugify"))
        self.assertEqual(self.unit_flags(out, "U1-normalize"), [])
        self.assertIn("every such call was denied", (out / "report.md").read_text(encoding="utf-8"))
        # a denied view of a file the work order already holds in full is not an escape attempt
        self.assertEqual(self.unit_flags(out, "U3-registry"), [])
        a3 = json.loads((out / "units" / "U3-registry" / "attempt_1.json").read_text(encoding="utf-8"))
        self.assertEqual((a3["agy"]["denied_count"], a3["agy"]["inlined_denied_count"]), (0, 1))
        # passed as --inlined: the hook denied views of these
        self.assertEqual(a3["inlined"], ["slugkit/__init__.py", "tests/test_registry.py"])
        log = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, log, True)
        rc, res, out = self.run_plan("perfect", "--no-confine", FAKE_AGY_ARGS_LOG=str(log))
        self.assertEqual(rc, 0, res)
        self.assertIn("unconfined", self.unit_flags(out, "U1-normalize"))
        # no hook ran: nothing was passed as --inlined, and the record must not claim it was enforced
        self.assertFalse(any("--inlined" in json.loads(f.read_text(encoding="utf-8")) for f in log.glob("*.json")))
        a2 = json.loads((out / "units" / "U2-slugify" / "attempt_1.json").read_text(encoding="utf-8"))
        self.assertEqual(a2["inlined"], [])
        self.assertIn("### `slugkit/normalize.py`", (out / "units" / "U2-slugify" / "attempt_1.prompt.md")
                      .read_text(encoding="utf-8"))  # the text is still in the work order

    def test_allow_all_needs_no_confine(self):
        rc, res, _ = self.run_plan("perfect", "--allow-all")
        self.assertEqual((rc, res["status"]), (5, "bad-args"))
        rc, res, _ = self.run_plan("perfect", "--allow-all", "--no-confine")
        self.assertEqual(rc, 0, res)

    @unittest.skipUnless(os.name == "nt", "sealing uses Windows DPAPI")
    def test_every_run_seals_hidden_tests_and_flags_unconfined_attempts(self):
        for extra, scenario, flagged in (([], "perfect", False), (["--no-confine"], "perfect", True),
                                         ([], "U1-normalize=hookless", True)):
            with self.subTest(extra=extra, scenario=scenario):
                self.setUp()
                rc, res, out = self.run_plan(scenario, *extra)
                self.assertEqual(rc, 0, res)
                spec = self.plan.parent
                self.assertFalse(any(spec.rglob("test_*_hidden.py")), "plaintext hidden tests left in the spec folder")
                self.assertTrue((spec / outsource.SEALED).is_file())
                self.assertFalse(any(out.rglob("test_*_hidden.py")), "plaintext hidden tests left in the run dir")
                self.assertEqual("unconfined" in self.unit_flags(out, "U1-normalize"), flagged)
        self.assertTrue((PLAN.parent / "hidden").is_dir(), "the shipped example was sealed")

    def test_an_unconfined_attempt_flags_the_unit_for_good(self):
        # attempt 1 ran without a working hook (and failed), attempt 2 was confined and passed: code fitted while
        # unconfined survives into the accepted delivery, so the flag stays
        rc, res, out = self.run_plan("U1-normalize=hooklessbuggy")
        self.assertEqual(rc, 0, res)
        self.assertEqual(res["units"]["U1-normalize"]["attempts"], 2)
        self.assertIn("unconfined", self.unit_flags(out, "U1-normalize"))
        last = json.loads((out / "units" / "U1-normalize" / "attempt_2.json").read_text(encoding="utf-8"))
        self.assertEqual(last["flags"], [])
        self.assertIn("without work-dir confinement", (out / "report.md").read_text(encoding="utf-8"))

    def test_perfect(self):
        rc, res, out = self.run_plan("perfect")
        self.assertEqual(rc, 0, res)
        self.assertTrue(res["integration"]["ok"])
        self.assertTrue((out / "accepted" / "slugkit" / "registry.py").is_file())
        self.assertFalse(any((out / "units").rglob("test_*_hidden.py")), "hidden tests left in the run dir")

    def test_each_failure_kind_is_caught_then_fixed(self):
        for sc, gate in (("buggy", "tests:hidden"), ("extra", "scope"), ("tamper", "scope"), ("signature", "interface"),
                         ("nomanifest", "manifest"), ("print", "forbid")):
            rc, res, out = self.run_plan(f"U2-slugify={sc}")
            self.assertEqual(rc, 0, (sc, res))
            first = json.loads((out / "units" / "U2-slugify" / "attempt_1.json").read_text(encoding="utf-8"))
            self.assertIn(gate, first["failed_gates"], sc)
            self.assertEqual(res["units"]["U2-slugify"]["attempts"], 2, sc)

    def test_exhausted_unit_blocks_dependents_and_resume(self):
        rc, res, out = self.run_plan("U1-normalize=always", "--max-attempts", "2")
        self.assertEqual(rc, 2)
        self.assertEqual(res["units"]["U1-normalize"]["status"], "needs_attention")
        self.assertEqual(res["units"]["U2-slugify"]["status"], "blocked")
        self.assertEqual(res["units"]["U3-registry"]["status"], "accepted")  # independent of U1
        self.assertIn("Needs the lead", (out / "report.md").read_text(encoding="utf-8"))
        # re-run with a fixed "agy": U3 is not redone, U1 and U2 are; U1's effort ladder starts again at low
        env = dict(os.environ, AGY_TASK=str(HERE / "fake_agy_task.py"), FAKE_AGY_SCENARIO="perfect", PYTHONIOENCODING="utf-8")
        cp = subprocess.run([sys.executable, str(SCRIPTS / "outsource.py"), "run", str(self.plan), "--out", str(out)],
                            capture_output=True, text=True, encoding="utf-8", env=env)
        res2 = json.loads(cp.stdout)
        self.assertEqual(cp.returncode, 0, res2)
        self.assertNotIn("U3-registry: attempt", cp.stderr)
        self.assertIn("U1-normalize: attempt 1/3 -> agy (effort low)", cp.stderr)

    def test_effort_climbs_per_attempt_unless_pinned(self):
        for extra, want in (((), ["low", "medium"]), (("--effort", "high"), ["high", "high"])):
            with self.subTest(extra=extra):
                log = Path(tempfile.mkdtemp())
                self.addCleanup(shutil.rmtree, log, True)
                rc, res, out = self.run_plan("U2-slugify=buggy", *extra, FAKE_AGY_ARGS_LOG=str(log))
                self.assertEqual(rc, 0, res)
                # resolved like outsource.out_dir: an 8.3 short %TEMP% would not match the -f path agy_task got
                udir = out.resolve() / "units" / "U2-slugify"
                recs = [json.loads((udir / f"attempt_{n}.json").read_text(encoding="utf-8")) for n in (1, 2)]
                self.assertEqual([r["effort"] for r in recs], want)
                # the job dir (-w) is random and outside the run dir: find U2's calls by their prompt file (-f)
                calls = {str(Path(c[c.index("-f") + 1]).resolve()): c for c in
                         (json.loads(f.read_text(encoding="utf-8")) for f in log.glob("*.json"))}
                calls = [calls[str(udir / f"attempt_{n}.prompt.md")] for n in (1, 2)]
                self.assertEqual([c[c.index("--effort") + 1] for c in calls], want)
                self.assertTrue(all("--bare" in c for c in calls))
                inl = [sorted(c[i + 1] for i, x in enumerate(c) if x == "--inlined") for c in calls]
                # the dependency's code, its context file (not inlined: listed only) and the visible test; never
                # U2's own slug.py from attempt 1
                self.assertEqual(inl, [["slugkit/normalize.py", "tests/test_slug.py"]] * 2)
                self.assertEqual([r["inlined"] for r in recs], inl)
                self.assertIn(f"| 2 ({', '.join(want)}) |", (out / "report.md").read_text(encoding="utf-8"))
                state = json.loads((out / "state.json").read_text(encoding="utf-8"))["units"]["U2-slugify"]
                self.assertEqual([h["effort"] for h in state["history"]], want)
                # hidden_feedback defaults to `summary`: assertion lines only, never the hidden test's source
                retry = (udir / "attempt_2.prompt.md").read_text(encoding="utf-8")
                hidden = retry.split("### Hidden acceptance tests failed")[1].split("Re-read the behaviour rules")[0]
                self.assertIn("ValueError", hidden)
                self.assertNotIn("assertRaises", hidden)
                self.assertNotIn('File "', hidden)
                self.assertNotIn("test_slug_hidden.py", hidden)

    def test_quota_stops(self):
        rc, res, out = self.run_plan("U1-normalize=quota")
        self.assertEqual(rc, 3)
        self.assertIn("Resets in 2h 10m", res["quota"])
        self.assertEqual(res["units"]["U1-normalize"]["status"], "pending")
        # a later run that fails for another reason must not repeat the old run's quota note
        rc, res2, _ = self.run_plan("U1-normalize=always", "--max-attempts", "1", "--units", "U1-normalize",
                                    "--no-integration", out_dir=out)
        self.assertEqual((rc, res2["units"]["U1-normalize"]["status"]), (2, "needs_attention"), res2)
        self.assertNotIn("note", res2["units"]["U1-normalize"])
        report = (out / "report.md").read_text(encoding="utf-8")
        self.assertNotIn("quota", report)
        self.assertIn("last attempt failed tests:hidden", report)

    def test_only_this_attempts_quota_line_counts(self):
        # attempt 1 had a quota line in its stderr.txt but delivered; attempt 2 delivered nothing (no quota line):
        # that is a failed attempt, not a quota stop; attempt 3 passes
        rc, res, out = self.run_plan("U1-normalize=oldquota", "--units", "U1-normalize", "--no-integration")
        self.assertEqual(rc, 0, res)
        self.assertEqual(res["units"]["U1-normalize"]["attempts"], 3)
        second = json.loads((out / "units" / "U1-normalize" / "attempt_2.json").read_text(encoding="utf-8"))
        self.assertIn("manifest", second["failed_gates"])
        # each attempt's record holds its own session only (the job dir keeps every attempt's)
        for n in (1, 2, 3):
            self.assertEqual(len(list((out / "units" / "U1-normalize" / f"attempt_{n}.agy_task").iterdir())), 1, n)

    def test_narration_about_quota_is_not_a_quota_stop(self):
        rc, res, out = self.run_plan("U1-normalize=narrate", "--units", "U1-normalize", "--no-integration")
        self.assertEqual(rc, 0, res)
        self.assertIsNone(res["quota"])
        self.assertEqual(res["units"]["U1-normalize"]["attempts"], 2)

    def test_agy_exit_status_alone_is_a_warning(self):
        rc, res, out = self.run_plan("agyfail")
        self.assertEqual(rc, 0, res)
        self.assertTrue(all(u["status"] == "accepted" for u in res["units"].values()), res)
        first = json.loads((out / "units" / "U1-normalize" / "attempt_1.json").read_text(encoding="utf-8"))
        agy_gate = next(g for g in first["gates"] if g["gate"] == "agy")
        self.assertTrue(agy_gate["ok"])
        self.assertIn("rc=3", agy_gate["warnings"][0])
        lines = [ln for ln in (out / "report.md").read_text(encoding="utf-8").splitlines()
                 if "warning: agy run failed" in ln]
        self.assertTrue(lines)
        self.assertTrue(all("(status error)" in ln for ln in lines), lines)  # one line, not split by the stderr tail

    def test_an_agy_task_crash_after_agy_ran_leaves_confinement_unknown(self):
        rc, res, out = self.run_plan("U1-normalize=crash", "--units", "U1-normalize", "--no-integration")
        self.assertEqual(rc, 0, res)
        self.assertEqual(res["units"]["U1-normalize"]["attempts"], 2)
        first = json.loads((out / "units" / "U1-normalize" / "attempt_1.json").read_text(encoding="utf-8"))
        self.assertIn("agy", first["failed_gates"])
        self.assertEqual(first["flags"], ["confinement_unknown"])
        self.assertIn("confinement_unknown", self.unit_flags(out, "U1-normalize"))

    def test_integrate(self):
        rc, res, out = self.run_plan("perfect")
        dest = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, dest, True)
        (dest / "slugkit").mkdir()
        (dest / "slugkit" / "slug.py").write_text("old\n", encoding="utf-8")
        base = [sys.executable, str(SCRIPTS / "outsource.py"), "integrate", str(self.plan), "--out", str(out), "--dest", str(dest)]
        cp = subprocess.run(base, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(json.loads(cp.stdout)["status"], "conflict")
        cp = subprocess.run(base + ["--overwrite", "--with-tests"], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("def slugify", (dest / "slugkit" / "slug.py").read_text(encoding="utf-8"))
        self.assertTrue((dest / "hidden" / "test_slug_hidden.py").is_file())
        # the package's __init__.py is a context file, not a deliverable: added because the destination lacks it
        self.assertEqual(json.loads(cp.stdout)["context_files_added"], ["slugkit/__init__.py"])
        self.assertTrue((dest / "slugkit" / "__init__.py").is_file())

    def test_integrate_never_copies_a_stale_unit(self):
        rc, res, out = self.run_plan("perfect")
        self.assertEqual(rc, 0, res)
        plan = json.loads(self.plan.read_text(encoding="utf-8"))  # e.g. triage said spec_gap: the lead adds a rule
        plan["units"][1]["spec"].append("A new rule the accepted code was never checked against.")
        self.plan.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        rc, res = self.cli("audit")
        self.assertEqual(rc, 0, res)
        dest = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, dest, True)
        base = [sys.executable, str(SCRIPTS / "outsource.py"), "integrate", str(self.plan), "--out", str(out),
                "--dest", str(dest)]
        cp = subprocess.run(base, capture_output=True, text=True, encoding="utf-8")
        res = json.loads(cp.stdout)
        self.assertEqual((cp.returncode, res["status"], res["stale"]), (2, "not-ready", ["U2-slugify"]))
        cp = subprocess.run(base + ["--partial"], capture_output=True, text=True, encoding="utf-8")
        res = json.loads(cp.stdout)
        self.assertEqual((cp.returncode, res["skipped_stale_units"]), (0, ["U2-slugify"]), res)
        self.assertFalse((dest / "slugkit" / "slug.py").exists())
        self.assertTrue((dest / "slugkit" / "normalize.py").is_file())

    def test_a_planted_hooks_file_fails_the_scope_gate(self):
        rc, res, out = self.run_plan("U1-normalize=planthook", "--no-confine", "--units", "U1-normalize",
                                     "--no-integration")
        self.assertEqual(rc, 0, res)
        first = json.loads((out / "units" / "U1-normalize" / "attempt_1.json").read_text(encoding="utf-8"))
        self.assertIn("scope", first["failed_gates"])
        self.assertIn(".agents/hooks.json", json.dumps(first["gates"]))


class Processes(unittest.TestCase):
    def test_a_timeout_kills_the_whole_tree(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        pidfile = d / "grandchild.pid"
        # the child starts a long-running grandchild (like agy_task.py starts agy) and waits for it
        child = ("import subprocess, sys; p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']);"
                 f" open(r'{pidfile}', 'w').write(str(p.pid)); p.wait()")
        with open(d / "err.log", "wb") as err:
            with self.assertRaises(subprocess.TimeoutExpired):
                outsource.run_tree([sys.executable, "-c", child], err, dict(os.environ), timeout=5)
        pid = int(pidfile.read_text())
        if os.name == "nt":
            cp = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True)
            self.assertNotIn(str(pid), cp.stdout)
        else:
            with self.assertRaises(OSError):
                os.kill(pid, 0)

    def test_plaintext_and_sessions_never_overlap(self):
        import threading
        import time
        gate, events = outsource.SessionGate(), []
        in_session = threading.Event()

        def session():
            with gate.session():
                in_session.set()
                events.append("session start")
                time.sleep(0.3)
                events.append("session end")

        t = threading.Thread(target=session)
        t.start()
        in_session.wait(5)
        with gate.plaintext():
            events.append("plaintext")
        t.join()
        self.assertEqual(events, ["session start", "session end", "plaintext"])


class TamperAndResultLine(unittest.TestCase):
    def tamper(self, src, allow=()):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        (d / "m.py").write_text(src, encoding="utf-8")
        return gates.check_tamper(d, {"deliverables": ["m.py"]}, allow)

    def test_tamper_gate(self):
        for src in ("import os\nos._exit(0)\n", "from os import _exit\n", "import inspect\n", "from unittest import mock\n",
                    "import sys\nA = sys.argv\n", "X = eval('1')\n", "B = __builtins__\n", "import subprocess\n",
                    "from pathlib import Path\nP = Path(__file__).parent / 'hidden'\n", "Q = '../tests/x'\n",
                    "R = f'test_{1}.py'\n"):
            self.assertFalse(self.tamper(src)["ok"], src)
        clean = '"""Doc: see tests/test_x.py."""\nimport re\nfrom re import compile\nP = compile("a")\nW = "test"\n'
        self.assertTrue(self.tamper(clean)["ok"])
        self.assertTrue(self.tamper("import sys\nA = sys.argv\n", ["sys.argv"])["ok"])

    def test_work_order_states_the_tamper_rule(self):
        p = outsource.load_plan(PLAN)
        self.assertIn("must not look at or interfere with the tests", outsource.render_work_order(p, p["units"][0], Path("W")))

    def test_result_line(self):
        rlp = outsource.result_line_problems
        self.assertTrue(rlp(""))  # exit code 0 but the runner never reported
        self.assertEqual(rlp("..\nmini_test: 3 passed, 0 failed, 0 skipped\n"), [])
        self.assertTrue(rlp("mini_test: 3 passed, 0 failed, 1 skipped\n"))
        self.assertTrue(rlp("mini_test: 0 passed, 0 failed, 0 skipped\n"))
        self.assertEqual(rlp("5 passed in 0.10s\n"), [])
        self.assertTrue(rlp("4 passed, 1 skipped in 0.10s\n"))
        self.assertTrue(rlp("4 passed, 1 failed in 0.10s\n"))
        # python -m unittest
        self.assertEqual(rlp("...\n----------\nRan 3 tests in 0.001s\n\nOK\n"), [])
        self.assertTrue(rlp("Ran 3 tests in 0.001s\n\nOK (skipped=1)\n"))
        self.assertTrue(rlp("Ran 3 tests in 0.001s\n\nFAILED (failures=1, errors=1)\n"))
        self.assertTrue(rlp("Ran 0 tests in 0.000s\n\nOK\n"))
        self.assertEqual(rlp("", "....\n------\nRan 4 tests in 0.002s\n\nOK\n"), [])  # unittest reports on stderr
        # the runner's line must be the last line: output after it is not the runner's
        self.assertTrue(rlp("5 passed in 0.10s\nmore output\n"))
        self.assertTrue(rlp("mini_test: 3 passed, 0 failed, 0 skipped\nx\n"))
        # with a nonce only the stdlib runner's own line counts
        self.assertEqual(rlp("mini_test: 3 passed, 0 failed, 0 skipped [ab12]\n", nonce="ab12"), [])
        self.assertTrue(rlp("mini_test: 3 passed, 0 failed, 0 skipped\n", nonce="ab12"))
        self.assertTrue(rlp("mini_test: 3 passed, 0 failed, 0 skipped [ffff]\n", nonce="ab12"))
        # fewer passed than the files define: some tests never ran
        self.assertTrue(rlp("mini_test: 3 passed, 0 failed, 0 skipped [ab12]\n", nonce="ab12", expected=5))
        self.assertEqual(rlp("7 passed in 0.10s\n", expected=5), [])  # e.g. parametrized pytest tests

    def test_tamper_gate_catches_detours_to_an_early_exit(self):
        for src in ("raise SystemExit\n", "raise SystemExit(0)\n", "E = KeyboardInterrupt\n",
                    "class Stop(BaseException):\n    pass\n", "import os\nf = getattr(os, '_ex' + 'it')\n",
                    "import os\nf = getattr(os, 'getcwd')\n", "def g(o, n):\n    return getattr(o, n)\n",
                    "B = Exception.__base__\n", "S = object.__subclasses__()\n", "import os\nV = vars(os)\n",
                    "def g(f):\n    return f.__globals__\n", "def g(o):\n    return getattr(o, '__dict__')\n"):
            self.assertFalse(self.tamper(src)["ok"], src)
        for src in ("class A:\n    def f(self):\n        return getattr(self, 'x', None)\n",
                    "class E(Exception):\n    pass\n", "def g(o):\n    setattr(o, 'x', 1)\n"):
            self.assertTrue(self.tamper(src)["ok"], (src, self.tamper(src)))
        self.assertTrue(self.tamper("def g(o, n):\n    return getattr(o, n)\n", ["getattr"])["ok"])
        rule = gates.tamper_rule()
        for word in ("SystemExit", "getattr()", "__subclasses__"):
            self.assertIn(word, rule)

    def test_tamper_gate_catches_frame_walks_and_dict_detours(self):
        walk = ("def steal():\n    try:\n        raise ValueError()\n    except ValueError as e:\n"
                "        fr = e.__traceback__.tb_frame\n    while fr is not None:\n        fr = fr.f_back\n")
        for src in (walk,
                    "def g(e):\n    return e.__traceback__\n", "def g(t):\n    return t.tb_frame.f_locals\n",
                    "def g(f):\n    return f.f_back.f_globals\n", "def g(gen):\n    return gen.gi_frame\n",
                    "def g(t):\n    return getattr(t, 'tb_frame')\n",
                    "import os\nos.__dict__['_ex' + 'it'](0)\n", "import os\nD = os.__dict__\n",
                    "import operator, os\noperator.attrgetter('_e' + 'xit')(os)(0)\n",
                    "from operator import attrgetter\nf = attrgetter('x')\n",
                    "import functools, os\nf = functools.partial(getattr, os, '_ex' + 'it')\n",
                    "F = list(map(getattr, [1], ['real']))\n",
                    "import os\nx = os\nx._exit(0)\n", "import sys\nsys.modules['os']._exit(0)\n",
                    "def g(m):\n    m.kill(1, 9)\n", "def g(m):\n    m.execv('x', [])\n",
                    "def g(o):\n    return vars(o)\n", "import __main__\nM = __main__.unittest\n",
                    "def g(e):\n    return '{0.__traceback__.tb_frame.f_back.f_locals}'.format(e)\n",
                    "import sys\nF = sys._current_frames()\n"):
            self.assertFalse(self.tamper(src)["ok"], src)
        for src in ("import platform\nS = platform.system()\n", "def g(cur):\n    cur.execute('select 1')\n",
                    "class A:\n    def f(self):\n        return type(self).__name__\n", "V = vars()\n",
                    "def g(o):\n    return getattr(o, 'name', None)\n", "T = 'traceback-free text'\n"):
            self.assertTrue(self.tamper(src)["ok"], (src, self.tamper(src)))
        self.assertTrue(self.tamper("def g(o):\n    return o.__dict__\n", ["__dict__"])["ok"])
        rule = gates.tamper_rule()
        for word in ("__traceback__", "f_locals", "__dict__", "_exit", "attrgetter", "as values"):
            self.assertIn(word, rule)

    def test_a_custom_python_test_cmd_needs_the_result_line(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        (d / "test_a.py").write_text("import unittest\n\nclass T(unittest.TestCase):\n    def test_x(self):\n"
                                     "        pass\n", encoding="utf-8")
        (d / "test_exit.py").write_text("import os\nos._exit(0)\n", encoding="utf-8")
        unit = {"tests": []}
        for cmd, files, ok in (("{python} -m unittest {tests}", ["test_a.py"], True),
                               ("{python} {tests}", ["test_exit.py"], False),  # exit 0, no test ran
                               ("{python} -m unittest {tests}", ["test_exit.py"], False)):
            g = outsource.test_gate("tests:visible", {"test_cmd": cmd}, unit, files, d)
            self.assertEqual(g["ok"], ok, (cmd, files, g))
        # a non-Python plan's test_cmd is judged by its exit code alone (documented)
        g = outsource.test_gate("tests:visible", {"test_cmd": "{python} {tests}", "language": "js"}, unit,
                                ["test_exit.py"], d)
        self.assertTrue(g["ok"], g)


class Sandbox(E2EBase):
    def test_cheating_deliveries_are_rejected(self):
        for sc in ("exit0", "peekhidden"):
            rc, res, out = self.run_plan(f"U2-slugify={sc}")
            self.assertEqual(rc, 0, (sc, res))
            first = json.loads((out / "units" / "U2-slugify" / "attempt_1.json").read_text(encoding="utf-8"))
            self.assertIn("tamper", first["failed_gates"], sc)

    def test_work_dir_is_a_private_job_dir(self):
        rc, res, out = self.run_plan("perfect")
        self.assertEqual(rc, 0, res)
        prompt = (out / "units" / "U2-slugify" / "attempt_1.prompt.md").read_text(encoding="utf-8")
        work = re.search(r"Work only inside (\S+?)\.?$", prompt, re.M).group(1)
        self.assertTrue(outsource.is_within(Path(work), JOBS), work)
        self.assertFalse(outsource.is_within(Path(work), self.plan.parent))
        self.assertEqual(list(JOBS.iterdir()), [], "job dirs are deleted after the run")

    def test_job_root_inside_a_git_repo_is_refused(self):
        repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, repo, True)
        (repo / ".git").mkdir()
        rc, res, _ = self.run_plan("perfect", AGY_OUTSOURCE_JOBS=str(repo / "jobs"))
        self.assertEqual((rc, res["status"]), (5, "bad-plan"))
        self.assertIn("git repository", res["errors"][0])

    def test_stale_plaintext_is_swept(self):
        out = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, out, True)
        stale = out / "units" / "U1-normalize" / "verify" / "hidden" / "test_normalize_hidden.py"
        stale.parent.mkdir(parents=True)
        stale.write_text("leftover", encoding="utf-8")
        env = dict(os.environ, AGY_TASK=str(HERE / "fake_agy_task.py"), FAKE_AGY_SCENARIO="perfect")
        subprocess.run([sys.executable, str(SCRIPTS / "outsource.py"), "run", str(self.plan), "--out", str(out)],
                       capture_output=True, env=env)
        self.assertFalse(stale.exists())
        self.assertFalse(any(out.rglob("test_*_hidden.py")))

    def test_escaped_contractor_is_caught_by_the_integrity_gate(self):
        rc, res, out = self.run_plan("U2-slugify=specedit", FAKE_AGY_SPEC_DIR=str(self.plan.parent))
        self.assertEqual(rc, 2, res)
        self.assertEqual(res["units"]["U2-slugify"]["status"], "needs_attention")
        self.assertIn("integrity", res["units"]["U2-slugify"]["note"])
        self.assertEqual(res["units"]["U2-slugify"]["attempts"], 1, "no retry on a moved baseline")

    def test_a_dependencys_context_file_is_in_the_integrity_baseline(self):
        # U2 gets U1's context file slugkit/__init__.py (dep_context); changing its source mid-run voids the run
        rc, res, out = self.run_plan("U2-slugify=ctxedit", FAKE_AGY_SPEC_DIR=str(self.plan.parent))
        self.assertEqual(rc, 2, res)
        self.assertEqual(res["units"]["U2-slugify"]["status"], "needs_attention")
        self.assertIn("slugkit/__init__.py", res["units"]["U2-slugify"]["note"])

    def test_an_agy_task_without_the_new_flags_is_caught(self):
        env = dict(os.environ, AGY_TASK=str(HERE / "fake_agy_task.py"), FAKE_AGY_OLD="1", PYTHONIOENCODING="utf-8")
        cp = subprocess.run([sys.executable, str(SCRIPTS / "outsource.py"), "preflight"], capture_output=True,
                            text=True, encoding="utf-8", env=env)
        res = json.loads(cp.stdout)
        self.assertEqual((cp.returncode, res["agy_task_flags"]["missing"]), (4, ["--bare", "--inlined"]))
        # `run` checks it too, before sealing anything or spending an attempt
        rc, res, out = self.run_plan("perfect", "--max-attempts", "1", "--units", "U1-normalize", FAKE_AGY_OLD="1")
        self.assertEqual((rc, res["status"], res["missing_flags"]), (4, "error", ["--bare", "--inlined"]), res)
        self.assertIn("update the agy-delegate", res["message"])
        self.assertFalse((out / "units").exists())
        self.assertFalse((self.plan.parent / outsource.SEALED).exists())
        cp = subprocess.run([sys.executable, str(SCRIPTS / "outsource.py"), "preflight"], capture_output=True,
                            text=True, encoding="utf-8", env=dict(env, FAKE_AGY_OLD=""))
        self.assertEqual(json.loads(cp.stdout)["agy_task_flags"], {"ok": True, "missing": []})


class RoleSeparation(E2EBase):
    def test_run_refuses_unaudited_or_changed_tests(self):
        (self.plan.parent / "audit.json").unlink()
        rc, res, _ = self.run_plan("perfect")
        self.assertEqual((rc, res["status"]), (5, "not-audited"))
        # a refused run leaves the spec folder as it was: nothing sealed, the plaintext hidden tests still there
        self.assertFalse((self.plan.parent / outsource.SEALED).exists())
        self.assertTrue((self.plan.parent / "hidden" / "test_slug_hidden.py").is_file())
        rc, res = self.cli("audit")
        self.assertEqual(rc, 0, res)
        visible = self.plan.parent / "tests" / "test_slug.py"  # changing a test after the audit voids it
        visible.write_text(visible.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
        rc, res, _ = self.run_plan("perfect")
        self.assertEqual((rc, res["status"]), (5, "not-audited"))
        self.assertIn("changed since the audit", " ".join(res["errors"]))

    def test_failed_or_incomplete_audit_blocks_the_run(self):
        for sc in ("untraced", "skip"):
            rc, res = self.cli("audit", "--force", FAKE_CLAUDE_SCENARIO=sc)
            self.assertEqual((rc, res["status"]), (2, "fail"), sc)
            rc, res, _ = self.run_plan("perfect")
            self.assertEqual((rc, res["status"]), (5, "not-audited"), sc)

    def test_same_model_roles_are_refused(self):
        rc, res = self.cli("audit", "--force", "--auditor", "opus")  # the lead is opus
        self.assertEqual((rc, res["status"]), (5, "bad-roles"))
        rc, res = self.cli("audit", "--force", "--auditor", "agy")
        self.assertEqual((rc, res["status"]), (5, "bad-roles"))
        rc, res, _ = self.run_plan("perfect", "--contractor", "claude:opus")
        self.assertEqual((rc, res["status"]), (5, "bad-roles"))
        rc, res = self.cli("audit", "--force", FAKE_CLAUDE_SCENARIO="wrongmodel")  # asked for sonnet, ran on opus
        self.assertEqual((rc, res["status"]), (5, "bad-roles"))

    def test_claude_contractor(self):
        rc, res, out = self.run_plan("perfect", "--contractor", "claude:haiku")
        self.assertEqual(rc, 0, res)
        st = json.loads((out / "state.json").read_text(encoding="utf-8"))["units"]["U2-slugify"]
        self.assertEqual(st["contractor_model"], "claude-haiku-5-5")
        self.assertIn("claude-haiku-5-5", (out / "report.md").read_text(encoding="utf-8"))

    def test_triage_names_the_contractor_of_the_failing_attempt(self):
        # an earlier run by a Claude contractor on the auditor's model must not block triage of a later agy failure
        rc, res, out = self.run_plan("perfect", "--contractor", "claude:sonnet")
        self.assertEqual(rc, 0, res)
        env = dict(os.environ, AGY_TASK=str(HERE / "fake_agy_task.py"), FAKE_AGY_SCENARIO="U2-slugify=always",
                   PYTHONIOENCODING="utf-8")
        cp = subprocess.run([sys.executable, str(SCRIPTS / "outsource.py"), "run", str(self.plan), "--out", str(out),
                             "--units", "U2-slugify", "--force", "--max-attempts", "1"],
                            capture_output=True, text=True, encoding="utf-8", env=env)
        self.assertEqual(cp.returncode, 2, cp.stdout)
        st = json.loads((out / "state.json").read_text(encoding="utf-8"))["units"]["U2-slugify"]
        self.assertNotIn("contractor_model", st)
        self.assertIn("| U2-slugify | agy |", (out / "report.md").read_text(encoding="utf-8"))
        rc, res = self.cli("triage", "U2-slugify", "--out", str(out))  # default auditor: sonnet
        self.assertEqual(rc, 0, res)
        self.assertEqual(res["contractor"], "agy")

    def test_claude_contractor_on_the_wrong_model_is_rejected(self):
        # even a perfect delivery: the role check is not softened like agy's exit status
        rc, res, out = self.run_plan("perfect", "--contractor", "claude:haiku", "--max-attempts", "1",
                                     "--units", "U1-normalize", FAKE_CLAUDE_SCENARIO="wrongmodel")
        self.assertEqual(rc, 2, res)
        self.assertEqual(res["units"]["U1-normalize"]["status"], "needs_attention")
        first = json.loads((out / "units" / "U1-normalize" / "attempt_1.json").read_text(encoding="utf-8"))
        self.assertEqual(first["failed_gates"], ["agy"])
        self.assertIsNone(first["effort"])
        self.assertEqual(first["inlined"], [])

    def test_triage_by_a_third_model(self):
        rc, res, out = self.run_plan("U2-slugify=always", "--max-attempts", "1")
        self.assertEqual(rc, 2, res)
        rc, res = self.cli("triage", "U2-slugify", "--out", str(out), "--auditor", "opus")
        self.assertEqual((rc, res["status"]), (5, "bad-roles"))
        rc, res = self.cli("triage", "U2-slugify", "--out", str(out))
        self.assertEqual(rc, 0, res)
        self.assertEqual(res["failures"][0]["verdict"], "code_bug")
        self.assertTrue((out / "units" / "U2-slugify" / "triage.json").is_file())


class Hardening(E2EBase):
    def test_visible_run_holds_no_hidden_test_and_feedback_quotes_none(self):
        # attempt 1 prints every file in its cwd that holds a hidden test, then fails to import: its output goes
        # back to agy as feedback
        rc, res, out = self.run_plan("U2-slugify=leak", "--units", "U2-slugify", "--no-integration")
        self.assertEqual(rc, 0, res)
        self.assertEqual(res["units"]["U2-slugify"]["attempts"], 2)
        first = json.loads((out / "units" / "U2-slugify" / "attempt_1.json").read_text(encoding="utf-8"))
        visible = next(g for g in first["gates"] if g["gate"] == "tests:visible")
        self.assertIn("LEAKSCAN start", visible["output_tail"])  # the code ran and printed
        self.assertNotIn("TestSlugifyHidden", visible["output_tail"])  # but no hidden test was there to read
        retry = (out / "units" / "U2-slugify" / "attempt_2.prompt.md").read_text(encoding="utf-8")
        self.assertIn("LEAKSCAN start", retry)
        for needle in ("TestSlugifyHidden", 'slugify("C++ & C#")', "Already--Slugged"):
            self.assertNotIn(needle, retry)

    def test_code_that_writes_into_its_job_dir_during_the_tests_stops_the_unit(self):
        rc, res, out = self.run_plan("U1-normalize=jobwrite", "--units", "U1-normalize", "--no-integration")
        self.assertEqual(rc, 2, res)
        u1 = res["units"]["U1-normalize"]
        self.assertEqual((u1["status"], u1["attempts"]), ("needs_attention", 1))
        self.assertTrue(u1["note"].startswith("integrity:"), u1)
        self.assertIn("job dir changed while the gates and tests ran", u1["note"])
        self.assertIn("NOTE.txt", u1["note"])

    def test_integrate_needs_an_integration_run_of_the_current_code_and_command(self):
        rc, res, out = self.run_plan("perfect")
        self.assertEqual(rc, 0, res)
        dest = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, dest, True)
        base = [sys.executable, str(SCRIPTS / "outsource.py"), "integrate", str(self.plan), "--out", str(out),
                "--dest", str(dest), "--dry-run"]
        integrate = lambda: json.loads(subprocess.run(base, capture_output=True, text=True,  # noqa: E731
                                                      encoding="utf-8").stdout)["status"]
        self.assertEqual(integrate(), "dry-run")
        # a unit re-accepted without the integration tests: the old integration result does not prove its code
        rc, res, _ = self.run_plan("perfect", "--units", "U3-registry", "--force", "--no-integration", out_dir=out)
        self.assertEqual(rc, 0, res)
        self.assertEqual(integrate(), "not-ready")
        rc, res, _ = self.run_plan("perfect", out_dir=out)  # nothing to re-run: the integration runs again
        self.assertEqual(rc, 0, res)
        self.assertEqual(integrate(), "dry-run")
        # the lead tightens integration_test_cmd after the pass: that check never ran
        plan = json.loads(self.plan.read_text(encoding="utf-8"))
        plan["integration_test_cmd"] += " && {python} -c \"import slugkit\""
        self.plan.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        self.assertEqual(integrate(), "not-ready")

    def test_a_rerun_drops_the_old_triage_verdict(self):
        rc, res, out = self.run_plan("U2-slugify=always", "--max-attempts", "1")
        self.assertEqual(rc, 2, res)
        rc, res = self.cli("triage", "U2-slugify", "--out", str(out))
        self.assertEqual(rc, 0, res)
        status = [sys.executable, str(SCRIPTS / "outsource.py"), "status", str(self.plan), "--out", str(out)]
        self.assertIn("code_bug", subprocess.run(status, capture_output=True, text=True, encoding="utf-8").stdout)
        rc, res, _ = self.run_plan("perfect", out_dir=out)
        self.assertEqual(rc, 0, res)
        self.assertFalse((out / "units" / "U2-slugify" / "triage.json").exists())
        self.assertNotIn("code_bug", (out / "report.md").read_text(encoding="utf-8"))

    def test_audit_refuses_unknown_unit_ids(self):
        rc, res = self.cli("audit", "--units", "U2")
        self.assertEqual((rc, res["status"]), (5, "bad-plan"))
        self.assertIn("unknown units: U2", res["errors"][0])


class InProcess(unittest.TestCase):
    """Runner paths that need a stubbed contractor call: a timeout, an interrupt, a Claude contractor's timeout."""

    def runner(self):
        spec = Path(tempfile.mkdtemp()) / "slugkit"
        self.addCleanup(shutil.rmtree, spec.parent, True)
        shutil.copytree(PLAN.parent, spec, ignore=shutil.ignore_patterns(".agy_outsource", "audit.json"))
        plan = outsource.load_plan(spec / "plan.json")
        out = spec.parent / "out"
        out.mkdir()
        a = argparse.Namespace(max_attempts=None, allow_all=False, no_confine=False, model=None, timeout=1,
                               effort=None, contractor="agy", jobs=1, force=False)
        return outsource.Runner(plan, out, a, HERE / "fake_agy_task.py", outsource.roles.plan_roles(plan))

    @staticmethod
    def deliver(work: Path, uid: str):
        for rel, src in fake_agy_task.GOOD[uid].items():
            (work / rel).parent.mkdir(parents=True, exist_ok=True)
            (work / rel).write_text(src, encoding="utf-8")
        (work / "DELIVERY.json").write_text(json.dumps({"unit": uid, "files": sorted(fake_agy_task.GOOD[uid]),
                                                        "assumptions": []}), encoding="utf-8")

    def test_a_timed_out_session_stops_the_unit_without_a_retry(self):
        r = self.runner()

        def timed_out(agy_task, work, pfile, a, effort, inlined=(), procs=None):
            self.deliver(work, "U1-normalize")  # it wrote a perfect delivery before it was killed
            return {"rc": None, "error": "timeout"}
        with mock.patch.object(outsource, "call_agy", timed_out):
            self.assertEqual(r.process("U1-normalize"), "needs_attention")
        st = r.ustate("U1-normalize")
        self.assertEqual(st["attempts"], 1)
        self.assertTrue(st["note"].startswith("timeout:"), st)
        self.assertIn("confinement_unknown", st["flags"])
        rec = json.loads((r.out / "units" / "U1-normalize" / "attempt_1.json").read_text(encoding="utf-8"))
        self.assertIn("agy", rec["failed_gates"])
        self.assertFalse(any(g["gate"].startswith("tests:") for g in rec["gates"]), "no verify tree after a timeout")

    def test_a_claude_contractor_timeout_is_a_timeout(self):
        def hang(*args, **kw):
            raise subprocess.TimeoutExpired("claude", 1)
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        (d / "attempt_1.prompt.md").write_text("# Work order U1 — x\n", encoding="utf-8")
        a = argparse.Namespace(timeout=1)
        with mock.patch.object(outsource, "run_tree", hang):
            res = outsource.call_claude_contractor("claude:haiku", d, d / "attempt_1.prompt.md", a, {"lead": "opus"})
        self.assertEqual((res["rc"], res["error"]), (None, "timeout"))

    def test_an_interrupt_kills_the_live_session_and_starts_no_attempt(self):
        r = self.runner()
        calls = []

        def slow(agy_task, work, pfile, a, effort, inlined=(), procs=None):
            calls.append(pfile.name)
            rc, _, _ = outsource.run_tree([sys.executable, "-c", "import time; time.sleep(60)"], subprocess.DEVNULL,
                                          dict(os.environ), 120, procs=procs)
            return {"rc": rc, "ran": True, "confined": True}
        timer = threading.Timer(2.0, _thread.interrupt_main)
        t0 = time.time()
        with mock.patch.object(outsource, "call_agy", slow):
            timer.start()
            with self.assertRaises(KeyboardInterrupt):
                r.run({"U1-normalize"})
        timer.cancel()
        self.assertLess(time.time() - t0, 40, "the run waited for the session instead of killing it")
        self.assertEqual(calls, ["attempt_1.prompt.md"])
        st = json.loads((r.out / "state.json").read_text(encoding="utf-8"))["units"]["U1-normalize"]
        self.assertEqual((st["status"], st["attempts"]), ("pending", 0))
        self.assertTrue(st["note"].startswith("interrupted:"), st)


@unittest.skipIf(outsource.have_pytest(), "covers the stdlib runner (pytest catches SystemExit at collection)")
class ProcessExitTricks(unittest.TestCase):
    """A delivery that prints a passing result line of its own and ends the test process (pytest is not installed
    here, so the stdlib runner runs the tests)."""

    def gate(self, mod_src):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        (d / "m.py").write_text(mod_src, encoding="utf-8")
        (d / "test_m.py").write_text("from m import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n\n\n"
                                     "def test_add_zero():\n    assert add(0, 0) == 0\n", encoding="utf-8")
        return outsource.test_gate("tests:visible", {}, {}, ["test_m.py"], d)

    def test_forged_result_lines_do_not_pass(self):
        forge = "import sys\nsys.stdout.write('mini_test: 7 passed, 0 failed, 0 skipped' + chr(10))\n"
        for tail in ("raise SystemExit\n", "raise SystemExit(0)\n",
                     "sys.stdout.flush()\nimport os\ngetattr(os, '_ex' + 'it')(0)\n",
                     "sys.stdout.flush()\nimport os\nos._exit(0)\n"):
            g = self.gate("def add(a, b):\n    return 0\n\n" + forge + tail)
            self.assertFalse(g["ok"], (tail, g))
            # and the tamper gate names the trick
            d = Path(tempfile.mkdtemp())
            self.addCleanup(shutil.rmtree, d, True)
            (d / "m.py").write_text(forge + tail, encoding="utf-8")
            self.assertFalse(gates.check_tamper(d, {"deliverables": ["m.py"]})["ok"], tail)

    def test_a_frame_walk_finds_no_nonce_to_forge_with(self):
        # a wrong add() that, on import, walks up from an exception it caught into the runner's frames, looks for
        # the nonce in their locals, prints a passing line with it and ends the process through os.__dict__
        steal = ("import os, re, sys\n\n\ndef add(a, b):\n    return 0\n\n\ndef _steal():\n    try:\n"
                 "        raise ValueError()\n    except ValueError as e:\n        fr = e.__traceback__.tb_frame\n"
                 "    found = None\n    while fr is not None:\n        for v in list(fr.f_locals.values()):\n"
                 "            if isinstance(v, str) and re.fullmatch('[0-9a-f]{16}', v):\n                found = v\n"
                 "        fr = fr.f_back\n    return found\n\n\n_n = _steal()\n"
                 "sys.stdout.write('STOLEN:%s' % _n + chr(10))\n"
                 "sys.stdout.write('mini_test: 5 passed, 0 failed, 0 skipped' + (' [%s]' % _n if _n else '') + chr(10))\n"
                 "sys.stdout.flush()\nos.__dict__['_ex' + 'it'](0)\n")
        g = self.gate(steal)
        self.assertFalse(g["ok"], g)
        self.assertIn("STOLEN:None", g["output_tail"])  # the nonce is in no frame while the tests run
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        (d / "m.py").write_text(steal, encoding="utf-8")
        probs = " ".join(gates.check_tamper(d, {"deliverables": ["m.py"]})["problems"])
        for word in ("__traceback__", "tb_frame", "f_locals", "f_back", "__dict__"):
            self.assertIn(word, probs)

    def test_an_honest_delivery_passes_with_the_nonce(self):
        g = self.gate("def add(a, b):\n    return a + b\n")
        self.assertTrue(g["ok"], g)
        self.assertRegex(g["output_tail"], r"mini_test: 2 passed, 0 failed, 0 skipped \[[0-9a-f]{16}\]")


class PytestNonce(unittest.TestCase):
    """The pytest plugin's nonce'd last line (pytest itself is not needed: its hook is called with stand-ins)."""

    def plugin(self):
        spec = importlib.util.spec_from_file_location(
            "_agy_pytest_nonce_test", outsource.PYTEST_PLUGIN_DIR / (outsource.PYTEST_PLUGIN + ".py"))
        mod = importlib.util.module_from_spec(spec)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGY_PYTEST_NONCE_STDIN", None)  # never read this process's stdin
            spec.loader.exec_module(mod)
        return mod

    def test_the_hook_prints_pytests_counts_with_the_nonce_last(self):
        mod = self.plugin()
        self.assertIsNone(mod._FD)
        tr = type("TR", (), {"stats": {"passed": [1, 2, 3], "skipped": [], "": [9]}, "lines": []})()
        tr.write_line = tr.lines.append
        config = type("C", (), {"pluginmanager": type("PM", (), {"get_plugin": lambda self, n: tr})()})()
        mod._FD = mod._stash(b"0123456789abcdef")
        mod.pytest_unconfigure(config)
        self.assertEqual(tr.lines, ["pytest-nonce: 3 passed, 0 failed, 0 skipped [0123456789abcdef]"])
        self.assertEqual(outsource.result_line_problems("3 passed in 0.1s\n" + tr.lines[0] + "\n",
                                                        nonce="0123456789abcdef", expected=3), [])
        self.assertTrue(outsource.result_line_problems("3 passed in 0.1s\n", nonce="0123456789abcdef"))
        self.assertIn("2 failed, 1 skipped", mod.result_line({"failed": [1], "error": [2], "xfailed": [3]}, ""))

    def test_the_plugin_reads_the_nonce_from_stdin_only_when_told(self):
        code = (f"import importlib.util, sys; spec = importlib.util.spec_from_file_location('p', "
                f"{str(outsource.PYTEST_PLUGIN_DIR / (outsource.PYTEST_PLUGIN + '.py'))!r}); "
                "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); "
                "import os; print(os.environ.get('AGY_PYTEST_NONCE_STDIN'), m._unstash(m._FD))")
        cp = subprocess.run([sys.executable, "-c", code], input="feedbeef\n", capture_output=True, text=True,
                            env=dict(os.environ, AGY_PYTEST_NONCE_STDIN="1"))
        self.assertEqual(cp.stdout.split(), ["None", "feedbeef"], cp.stderr)  # read, and taken out of the env

    def test_pytest_runs_with_the_plugin_and_a_nonce(self):
        with mock.patch.object(outsource, "have_pytest", lambda: True):
            cmd, shell = outsource.test_command({}, {}, ["tests/t.py"])
            self.assertEqual(outsource.runner_kind({}, {}), "pytest")
        self.assertFalse(shell)
        self.assertEqual(cmd[cmd.index(outsource.PYTEST_PLUGIN) - 1], "-p")
        self.assertEqual(outsource.runner_kind({"test_cmd": "{python} -m unittest {tests}"}, {}), "unittest")


class Collection(unittest.TestCase):
    """The test count is what the runner collects; test files that define more are a plan error."""
    DUP = ("import unittest\n\n\nclass T(unittest.TestCase):\n    def test_a(self):\n        self.assertTrue(False)\n\n"
           "    def test_a(self):\n        from m import f\n        self.assertEqual(f(1), 2)\n\n\n"
           "def testHelperStyle():\n    pass\n")

    def test_collected_tests_follow_the_runner(self):
        ct = outsource.collected_tests
        self.assertEqual(ct("t.py", self.DUP, "mini_test"), ["T.test_a"])
        self.assertEqual(ct("t.py", self.DUP, "unittest"), ["T.test_a"])
        self.assertEqual(ct("t.py", self.DUP, "pytest"), ["T.test_a", "testHelperStyle"])
        self.assertEqual(outsource.test_defs(self.DUP), 3)
        self.assertEqual(outsource.test_names("t.py", self.DUP), ["T.test_a", "testHelperStyle"])  # for the audit
        mixin = ("import unittest\n\nclass Base:\n    def test_x(self):\n        pass\n\n"
                 "class A(Base, unittest.TestCase):\n    def test_y(self):\n        pass\n\n"
                 "class B(A):\n    pass\n\nclass TestPlain:\n    def test_z(self):\n        pass\n")
        self.assertEqual(len(ct("t.py", mixin, "mini_test")), 4)  # A and B each run test_x and test_y
        self.assertEqual(len(ct("t.py", mixin, "pytest")), 5)  # and pytest also runs TestPlain

    @unittest.skipIf(outsource.have_pytest(), "covers the stdlib runner")
    def test_a_duplicated_test_name_does_not_fail_correct_code(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        (d / "m.py").write_text("def f(x):\n    return x + 1\n", encoding="utf-8")
        (d / "test_m.py").write_text(self.DUP, encoding="utf-8")
        g = outsource.test_gate("tests:hidden", {}, {}, ["test_m.py"], d)
        self.assertTrue(g["ok"], g)
        # a shortfall the runner reports is the test files' fault: labelled so, never a code failure
        with mock.patch.object(outsource, "expected_tests", lambda *a, **k: 3):
            g = outsource.test_gate("tests:hidden", {}, {}, ["test_m.py"], d)
        self.assertFalse(g["ok"])
        self.assertTrue(g.get("collection"), g)
        self.assertTrue(g["problems"][0].startswith(outsource.COLLECTION))

    def test_check_and_run_refuse_tests_the_runner_would_not_all_collect(self):
        spec = Path(tempfile.mkdtemp()) / "slugkit"
        self.addCleanup(shutil.rmtree, spec.parent, True)
        shutil.copytree(PLAN.parent, spec, ignore=shutil.ignore_patterns(".agy_outsource", "audit.json"))
        check = lambda *x: subprocess.run([sys.executable, str(SCRIPTS / "outsource.py"), "check",  # noqa: E731
                                           str(spec / "plan.json"), *x], capture_output=True, text=True,
                                          encoding="utf-8")
        cp = check("--red", "--out", str(spec.parent / "out"))
        self.assertEqual(cp.returncode, 0, cp.stdout)  # the shipped example is clean
        t = spec / "tests" / "test_slug.py"
        src = t.read_text(encoding="utf-8")
        name = re.search(r"def (test_\w+)\(self\)", src).group(1)
        t.write_text(src + f"\n    def {name}(self):\n        pass\n", encoding="utf-8")  # same name, same class
        cp = check()
        res = json.loads(cp.stdout)
        self.assertEqual(cp.returncode, 5, res)
        self.assertTrue(any("tests/test_slug.py defines" in e for e in res["errors"]), res["errors"])
        env = dict(os.environ, AGY_TASK=str(HERE / "fake_agy_task.py"), PYTHONIOENCODING="utf-8")
        cp = subprocess.run([sys.executable, str(SCRIPTS / "outsource.py"), "run", str(spec / "plan.json"), "--out",
                             str(spec.parent / "out")], capture_output=True, text=True, encoding="utf-8", env=env)
        self.assertEqual((cp.returncode, json.loads(cp.stdout)["status"]), (5, "bad-plan"))


class StaleDependencies(E2EBase):
    def test_a_stale_dependency_is_redone_before_its_dependent(self):
        rc, res, out = self.run_plan("perfect")
        self.assertEqual(rc, 0, res)
        plan = json.loads(self.plan.read_text(encoding="utf-8"))
        plan["units"][0]["spec"].append("Neither function changes a string that is already normalised.")
        self.plan.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        rc, res = self.cli("audit")
        self.assertEqual(rc, 0, res)
        log = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, log, True)
        rc, res, _ = self.run_plan("perfect", "--units", "U2-slugify", out_dir=out, FAKE_AGY_ARGS_LOG=str(log))
        self.assertEqual(rc, 0, res)
        units = [re.search(r"units[\\/]([^\\/]+)[\\/]attempt_", c[c.index("-f") + 1]).group(1)
                 for c in (json.loads(f.read_text(encoding="utf-8")) for f in sorted(log.glob("*.json")))]
        # the stale U1 is pulled in and redone; U2 is redone only if U1's accepted code changed (here it did not)
        self.assertEqual(units, ["U1-normalize"])
        p = outsource.load_plan(self.plan)
        state = json.loads((out / "state.json").read_text(encoding="utf-8"))
        self.assertTrue(all(outsource.is_current(p, state, u["id"]) for u in p["units"]))
        self.assertTrue(res["integration"]["ok"])


class InProcessGuards(unittest.TestCase):
    runner = InProcess.runner
    deliver = staticmethod(InProcess.deliver)

    def test_a_missing_accepted_dependency_file_stops_the_unit_without_a_crash(self):
        r = self.runner()
        # U1 recorded as accepted under its current spec, but its accepted file is gone (e.g. renamed deliverable)
        u1 = r.by["U1-normalize"]
        r.state["units"]["U1-normalize"] = {"status": "accepted", "attempts": 1, "files": {},
                                            "spec_hash": outsource.spec_hash(r.plan, u1, r.state)}
        with mock.patch.object(outsource, "call_agy", side_effect=AssertionError("no session may start")):
            self.assertEqual(r.process("U2-slugify"), "needs_attention")
        st = r.ustate("U2-slugify")
        self.assertTrue(st["note"].startswith("inputs missing: slugkit/normalize.py"), st)
        self.assertEqual(st["attempts"], 0)

    def test_a_count_shortfall_stops_the_unit_at_once(self):
        r = self.runner()

        def deliver(agy_task, work, pfile, a, effort, inlined=(), procs=None):
            self.deliver(work, "U1-normalize")
            return {"rc": 0, "ran": True, "confined": True}
        with mock.patch.object(outsource, "call_agy", deliver), \
                mock.patch.object(outsource, "expected_tests", lambda *a, **k: 99):
            self.assertEqual(r.process("U1-normalize"), "needs_attention")
        st = r.ustate("U1-normalize")
        self.assertEqual(st["attempts"], 1, "no retry: no code change can fix the test files")
        self.assertTrue(st["note"].startswith("tests: " + outsource.COLLECTION), st)

    def test_a_claude_contractors_reply_text_is_no_quota_signal(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        (d / "attempt_1.prompt.md").write_text("# Work order U1 — x\n", encoding="utf-8")
        a = argparse.Namespace(timeout=1)
        base = {"rc": 1, "is_error": True, "subtype": "error_during_execution", "models": ["claude-haiku-5-5"],
                "model": "claude-haiku-5-5", "denials": [], "stderr_tail": "",
                "result": "I implemented the rate limit check and the quota logic, then hit the usage limit test"}
        for res, quota in ((base, False), (dict(base, result="Claude AI usage limit reached|1760000000"), True),
                           (dict(base, stderr_tail='API Error: 429 {"type":"rate_limit_error"}'), True)):
            with mock.patch.object(outsource.roles, "run_claude", lambda *x, **k: res):
                got = outsource.call_claude_contractor("claude:haiku", d, d / "attempt_1.prompt.md", a,
                                                       {"lead": "opus"})
            self.assertEqual(bool(got["quota"]), quota, (res, got))
            self.assertTrue(got["error"])


if __name__ == "__main__":
    unittest.main()

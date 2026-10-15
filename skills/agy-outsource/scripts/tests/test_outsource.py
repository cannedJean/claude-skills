"""Tests for agy-outsource without agy: gates on hand-made deliveries, plan validation, and full runs of
examples/slugkit with tests/fake_agy_task.py standing in for agy-delegate.

  python -m unittest discover -s scripts/tests -v      (from the skill directory)
"""
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
SKILL = SCRIPTS.parent
PLAN = SKILL / "examples" / "slugkit" / "plan.json"
sys.path.insert(0, str(SCRIPTS))

import gates  # noqa: E402
import outsource  # noqa: E402

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


class PlanTests(unittest.TestCase):
    def test_example_plan_is_valid(self):
        errors, warnings = outsource.validate_plan(outsource.load_plan(PLAN))
        self.assertEqual(errors, [])
        self.assertEqual(outsource.topo_order(outsource.load_plan(PLAN)), ["U1-normalize", "U2-slugify", "U3-registry"])

    def test_bad_plans(self):
        base = outsource.load_plan(PLAN)
        cases = {
            "cycle": lambda p: p["units"][0]["depends_on"].append("U3-registry"),
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


class EndToEnd(unittest.TestCase):
    def run_plan(self, scenario, *extra):
        out = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, out, True)
        env = dict(os.environ, AGY_TASK=str(HERE / "fake_agy_task.py"), FAKE_AGY_SCENARIO=scenario, PYTHONIOENCODING="utf-8")
        cp = subprocess.run([sys.executable, str(SCRIPTS / "outsource.py"), "run", str(PLAN), "--out", str(out), *extra],
                            capture_output=True, text=True, encoding="utf-8", env=env)
        res = json.loads(cp.stdout)
        return cp.returncode, res, out

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
        rc, res, out = self.run_plan("U2-slugify=always", "--max-attempts", "2")
        self.assertEqual(rc, 2)
        self.assertEqual(res["units"]["U1-normalize"]["status"], "accepted")
        self.assertEqual(res["units"]["U2-slugify"]["status"], "needs_attention")
        self.assertEqual(res["units"]["U3-registry"]["status"], "blocked")
        self.assertIn("Needs the lead", (out / "report.md").read_text(encoding="utf-8"))
        # re-run with a fixed "agy": U1 is not redone, U2 and U3 are
        env = dict(os.environ, AGY_TASK=str(HERE / "fake_agy_task.py"), FAKE_AGY_SCENARIO="perfect", PYTHONIOENCODING="utf-8")
        cp = subprocess.run([sys.executable, str(SCRIPTS / "outsource.py"), "run", str(PLAN), "--out", str(out)],
                            capture_output=True, text=True, encoding="utf-8", env=env)
        res2 = json.loads(cp.stdout)
        self.assertEqual(cp.returncode, 0, res2)
        self.assertNotIn("U1-normalize: attempt", cp.stderr)

    def test_quota_stops(self):
        rc, res, out = self.run_plan("U1-normalize=quota")
        self.assertEqual(rc, 3)
        self.assertIn("Resets in 2h 10m", res["quota"])
        self.assertEqual(res["units"]["U1-normalize"]["status"], "pending")

    def test_integrate(self):
        rc, res, out = self.run_plan("perfect")
        dest = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, dest, True)
        (dest / "slugkit").mkdir()
        (dest / "slugkit" / "slug.py").write_text("old\n", encoding="utf-8")
        base = [sys.executable, str(SCRIPTS / "outsource.py"), "integrate", str(PLAN), "--out", str(out), "--dest", str(dest)]
        cp = subprocess.run(base, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(json.loads(cp.stdout)["status"], "conflict")
        cp = subprocess.run(base + ["--overwrite", "--with-tests"], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("def slugify", (dest / "slugkit" / "slug.py").read_text(encoding="utf-8"))
        self.assertTrue((dest / "hidden" / "test_slug_hidden.py").is_file())


if __name__ == "__main__":
    unittest.main()

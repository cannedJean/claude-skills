"""Tests for agy-outsource without agy: gates on hand-made deliveries, plan validation, and full runs of
examples/slugkit with tests/fake_agy_task.py standing in for agy-delegate.

  python -m unittest discover -s scripts/tests -v      (from the skill directory)
"""
import copy
import json
import os
import re
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

    def run_plan(self, scenario, *extra, **more_env):
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
        rc, res, out = self.run_plan("U2-slugify=outside")
        self.assertEqual(rc, 0, res)
        self.assertIn("outside_access_denied", self.unit_flags(out, "U2-slugify"))
        self.assertEqual(self.unit_flags(out, "U1-normalize"), [])
        self.assertIn("every such call was denied", (out / "report.md").read_text(encoding="utf-8"))
        rc, res, out = self.run_plan("perfect", "--no-confine")
        self.assertEqual(rc, 0, res)
        self.assertIn("unconfined", self.unit_flags(out, "U1-normalize"))

    def test_allow_all_needs_no_confine(self):
        rc, res, _ = self.run_plan("perfect", "--allow-all")
        self.assertEqual((rc, res["status"]), (5, "bad-args"))
        rc, res, _ = self.run_plan("perfect", "--allow-all", "--no-confine")
        self.assertEqual(rc, 0, res)

    @unittest.skipUnless(os.name == "nt", "sealing uses Windows DPAPI")
    def test_run_seals_hidden_tests(self):
        rc, res, out = self.run_plan("perfect")
        self.assertEqual(rc, 0, res)
        spec = self.plan.parent
        self.assertFalse(any(spec.rglob("test_*_hidden.py")), "plaintext hidden tests left in the spec folder")
        self.assertTrue((spec / outsource.SEALED).is_file())
        self.assertFalse(any(out.rglob("test_*_hidden.py")), "plaintext hidden tests left in the run dir")
        self.assertTrue((PLAN.parent / "hidden").is_dir(), "the shipped example was sealed")

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
        cp = subprocess.run([sys.executable, str(SCRIPTS / "outsource.py"), "run", str(self.plan), "--out", str(out)],
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
        base = [sys.executable, str(SCRIPTS / "outsource.py"), "integrate", str(self.plan), "--out", str(out), "--dest", str(dest)]
        cp = subprocess.run(base, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(json.loads(cp.stdout)["status"], "conflict")
        cp = subprocess.run(base + ["--overwrite", "--with-tests"], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("def slugify", (dest / "slugkit" / "slug.py").read_text(encoding="utf-8"))
        self.assertTrue((dest / "hidden" / "test_slug_hidden.py").is_file())


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


class RoleSeparation(E2EBase):
    def test_run_refuses_unaudited_or_changed_tests(self):
        (self.plan.parent / "audit.json").unlink()
        rc, res, _ = self.run_plan("perfect")
        self.assertEqual((rc, res["status"]), (5, "not-audited"))
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

    def test_triage_by_a_third_model(self):
        rc, res, out = self.run_plan("U2-slugify=always", "--max-attempts", "1")
        self.assertEqual(rc, 2, res)
        rc, res = self.cli("triage", "U2-slugify", "--out", str(out), "--auditor", "opus")
        self.assertEqual((rc, res["status"]), (5, "bad-roles"))
        rc, res = self.cli("triage", "U2-slugify", "--out", str(out))
        self.assertEqual(rc, 0, res)
        self.assertEqual(res["failures"][0]["verdict"], "code_bug")
        self.assertTrue((out / "units" / "U2-slugify" / "triage.json").is_file())


if __name__ == "__main__":
    unittest.main()

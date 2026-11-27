import sandbox  # noqa: F401  (must stay the first import: blocks real-app access)
import ast
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import run_verification as rv  # noqa: E402

ALLOWED_HOST = '''
FLAGS = [
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-features=CalculateNativeWinOcclusion",
]
def build(p):
    return [f"--user-data-dir={p}"] + FLAGS
'''


class StaticGuardsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.skill = Path(self._tmp.name)
        (self.skill / "scripts" / "tests").mkdir(parents=True)

    def put(self, rel, text):
        p = self.skill / "scripts" / rel
        p.write_text(text, encoding="utf-8")
        return p

    def test_flags_playwright_in_script(self):
        self.put("x.py", "import playwright\n")
        res = rv.check_forbidden_automation(self.skill)
        self.assertFalse(res["passed"])
        self.assertTrue(any("playwright" in e and "x.py:1" in e for e in res["errors"]))

    def test_flags_each_forbidden_token(self):
        for tok in ["playwright", "remote-debugging", "connect_over_cdp", "chrome_launcher",
                    "9223", "9224", "--browser chrome", "app.asar"]:
            with self.subTest(tok=tok):
                self.put("y.py", f"X = '{tok}'\n")
                res = rv.check_forbidden_automation(self.skill)
                self.assertFalse(res["passed"], tok)
                self.assertTrue(any(tok in e for e in res["errors"]), res["errors"])

    def test_token_case_insensitive_and_digit_boundaries(self):
        self.put("y.py", "import Playwright\n")
        self.assertFalse(rv.check_forbidden_automation(self.skill)["passed"])
        self.put("y.py", "X = 19223\nY = 92240\nZ = 9223\n")
        res = rv.check_forbidden_automation(self.skill)
        self.assertEqual(res["errors"], ["scripts/y.py:3 forbidden token '9223'"])

    def test_scans_subdirectories_but_not_nested_tests(self):
        (self.skill / "scripts" / "lib" / "tests").mkdir(parents=True)
        self.put("lib/x.py", "import playwright\n")
        self.put("lib/tests/z.py", "import playwright\n")
        res = rv.check_forbidden_automation(self.skill)
        self.assertEqual(len(res["errors"]), 1)
        self.assertIn("scripts/lib/x.py:1", res["errors"][0])

    def test_ignores_tests_dir(self):
        self.put("tests/t.py", "import playwright\nX='9223'\n")
        self.put("gemini_host.py", ALLOWED_HOST)
        res = rv.check_forbidden_automation(self.skill)
        self.assertTrue(res["passed"], res["errors"])

    def test_flags_unlisted_switch_literal(self):
        self.put("gemini_host.py", ALLOWED_HOST + '\nBAD = "--remote-allow-origins=*"\nB2 = "--no-sandbox"\n')
        res = rv.check_forbidden_automation(self.skill)
        self.assertFalse(res["passed"])
        self.assertTrue(any("--no-sandbox" in e for e in res["errors"]))
        self.assertTrue(any("--remote-allow-origins" in e for e in res["errors"]))
        self.assertEqual(len(res["errors"]), 2, res["errors"])

    def test_flags_switches_embedded_in_longer_literals(self):
        for lit in ("--user-data-dir=C:\\p --no-sandbox", "--no-sandbox --disable-gpu", " --no-sandbox",
                    "x --remote-allow-origins=*", "--user-data-dir=C:\\my dir"):
            with self.subTest(lit=lit):
                self.put("gemini_host.py", ALLOWED_HOST + f"\nBAD = {lit!r}\n")
                res = rv.check_forbidden_automation(self.skill)
                self.assertFalse(res["passed"], lit)
        for ok in ("--user-data-dir=", "no --user-data-dir and no flags", "a -- b", "--disable-renderer-backgrounding"):
            with self.subTest(ok=ok):
                self.put("gemini_host.py", ALLOWED_HOST + f"\nOK = {ok!r}\n")
                res = rv.check_forbidden_automation(self.skill)
                self.assertTrue(res["passed"], (ok, res["errors"]))

    def test_flags_switches_after_comma_or_quote(self):
        for lit in ("a,--no-sandbox", 'x "--no-sandbox"', "x '--no-sandbox'", '"--remote-allow-origins=*"',
                    "--disable-renderer-backgrounding,--no-sandbox"):
            with self.subTest(lit=lit):
                self.put("gemini_host.py", ALLOWED_HOST + f"\nBAD = {lit!r}\n")
                self.assertFalse(rv.check_forbidden_automation(self.skill)["passed"], lit)

    def test_allows_the_four_switches(self):
        self.put("gemini_host.py", ALLOWED_HOST)
        res = rv.check_forbidden_automation(self.skill)
        self.assertTrue(res["passed"], res["errors"])

    def test_flags_write_to_app_dir(self):
        self.put("w.py", 'def f():\n    p = r"C:\\Users\\x\\AppData\\Local\\Google\\Gemini\\a.json"\n'
                         '    open(p, "w").write("x")\n')
        res = rv.check_no_app_dir_writes(self.skill)
        self.assertFalse(res["passed"])
        self.assertTrue(any("w.py" in e for e in res["errors"]))

    def test_flags_other_write_calls(self):
        for call in ["Path(p).write_text('x')", "Path(p).write_bytes(b'x')", "shutil.copy(a, p)",
                     "os.replace(a, p)", "os.remove(p)", "open(p, mode='a')"]:
            self.put("w.py", f'def f(a, p):\n    base = "Google/Gemini"\n    {call}\n')
            self.assertFalse(rv.check_no_app_dir_writes(self.skill)["passed"], call)

    def _flags(self, body):
        self.put("w.py", body)
        return not rv.check_no_app_dir_writes(self.skill)["passed"]

    def test_path_open_mode_is_first_arg(self):
        pre = 'def f(p):\n    b = "Google/Gemini"\n    '
        self.assertTrue(self._flags(pre + 'Path(p).open("w")\n'))
        self.assertTrue(self._flags(pre + 'p.open(mode="a")\n'))
        self.assertFalse(self._flags(pre + 'p.open("r")\n'))
        self.assertFalse(self._flags(pre + 'p.open()\n'))

    def test_str_replace_and_dict_copy_not_writes(self):
        pre = 'def f(d):\n    b = "Google/Gemini"\n    '
        self.assertFalse(self._flags(pre + '"x".replace("a", "b")\n    d.copy()\n'))

    def test_more_write_calls(self):
        for call in ["shutil.move(a, p)", "shutil.rmtree(p)", "shutil.copytree(a, p)",
                     "shutil.copy2(a, p)", "os.makedirs(p)", "os.mkdir(p)", "os.rename(a, p)",
                     "os.unlink(p)", "os.rmdir(p)", "os.open(p, 1)", "Path(p).mkdir()",
                     "Path(p).rename(a)", "Path(p).unlink()", "Path(p).touch()", "Path(p).rmdir()"]:
            with self.subTest(call=call):
                self.assertTrue(self._flags('def f(a, p):\n    b = "Google/Gemini"\n    ' + call + '\n'))

    def test_split_path_form(self):
        self.assertTrue(self._flags(
            'def f(base):\n    q = os.path.join(base, "Google", "Gemini")\n    open(q, "w")\n'))
        self.assertTrue(self._flags(
            'def f(base):\n    q = Path(base).joinpath("Google", "Gemini")\n    q.write_text("x")\n'))

    def test_module_level_literal_covers_functions(self):
        self.assertTrue(self._flags('D = r"C:\\Google\\Gemini"\ndef f():\n    open(D, "w")\n'))

    def test_case_io_open_div_join_path_replace_nested(self):
        for body in ('def f(p):\n    b = "google\\\\gemini"\n    open(p, "w")\n',
                     'def f(p):\n    b = "GOOGLE/GEMINI"\n    open(p, "w")\n',
                     'def f(p):\n    b = "Google/Gemini"\n    io.open(p, "w")\n',
                     'def f(p):\n    b = "Google/Gemini"\n    io.open(p, mode="a")\n',
                     'def f(base):\n    q = Path(base) / "Google" / "Gemini" / "x"\n    open(q, "w")\n',
                     'def f(base):\n    q = Path(base) / "google" / "gemini"\n    q.write_text("x")\n',
                     'def f(base):\n    q = os.path.join(base, "google", "GEMINI")\n    open(q, "w")\n',
                     'def f(a, p):\n    b = "Google/Gemini"\n    Path(a).replace(p)\n',
                     'def f(p):\n    b = "Google/Gemini"\n    def g():\n        open(p, "w")\n    g()\n',
                     'def f(p):\n    def g():\n        return "Google/Gemini"\n    open(p, "w")\n'):
            with self.subTest(body=body):
                self.assertTrue(self._flags(body))

    def test_read_only_and_unrelated_calls_not_flagged(self):
        pre = 'def f(p):\n    b = "Google/Gemini"\n    '
        for call in ('os.open(p, os.O_RDONLY)', 'os.open(p, 0)', 'io.open(p, "r")', 'io.open(p)',
                     '"x".replace("a", "b")', 's.replace("a", "b", 1)'):
            with self.subTest(call=call):
                self.assertFalse(self._flags(pre + call + '\n'))
        self.assertTrue(self._flags(pre + 'os.open(p, os.O_WRONLY | os.O_CREAT)\n'))
        self.assertTrue(self._flags(pre + 'os.open(p, flags)\n'))

    def test_app_dir_literal_is_scoped_to_its_function(self):
        self.assertFalse(self._flags('def f():\n    return "Google/Gemini"\ndef g(p):\n    open(p, "w")\n'))

    def test_app_dir_check_ignores_tests_dir(self):
        self.put("tests/t.py", 'def f():\n    p = "Google/Gemini"\n    open(p, "w")\n')
        self.assertTrue(rv.check_no_app_dir_writes(self.skill)["passed"])

    def test_run_all_includes_new_checks(self):
        names = rv.run_all_verification.__code__.co_names
        self.assertIn("check_forbidden_automation", names)
        self.assertIn("check_no_app_dir_writes", names)

    def _ok(self, name):
        return {"name": name, "passed": True, "errors": [], "duration_s": 0.0}

    def _run_all_with(self, sandbox_passed):
        sb = self._ok("tests_use_sandbox")
        if not sandbox_passed:
            sb = {"name": "tests_use_sandbox", "passed": False, "errors": ["scripts/tests/test_x.py: first import"],
                  "duration_s": 0.0}
        unit = mock.Mock(return_value=self._ok("unit_tests"))
        with mock.patch.object(rv, "check_syntax_compilation", return_value=self._ok("s")), \
                mock.patch.object(rv, "check_cli_help", return_value=self._ok("c")), \
                mock.patch.object(rv, "check_frontmatter", return_value=self._ok("f")), \
                mock.patch.object(rv, "check_forbidden_automation", return_value=self._ok("a")), \
                mock.patch.object(rv, "check_no_app_dir_writes", return_value=self._ok("w")), \
                mock.patch.object(rv, "check_tests_use_sandbox", return_value=sb) as sb_mock, \
                mock.patch.object(rv, "check_unit_tests", unit):
            report = rv.run_all_verification(self.skill)
        sb_mock.assert_called_once_with(self.skill)
        return report, unit

    def test_run_all_skips_unit_tests_when_sandbox_check_fails(self):
        report, unit = self._run_all_with(sandbox_passed=False)
        unit.assert_not_called()
        self.assertFalse(report["all_passed"])
        ut = report["checks"]["unit_tests"]
        self.assertFalse(ut["passed"])
        self.assertTrue(any("tests_use_sandbox" in e for e in ut["errors"]), ut["errors"])

    def test_run_all_runs_unit_tests_when_sandbox_check_passes(self):
        report, unit = self._run_all_with(sandbox_passed=True)
        unit.assert_called_once_with(self.skill / "scripts" / "tests")
        self.assertTrue(report["all_passed"])

    def test_unit_test_subprocess_wiring(self):
        tests_dir = self.skill / "scripts" / "tests"
        fake = subprocess.CompletedProcess([], 0, stdout="Ran 3 tests in 0.1s\n\nOK\n", stderr="")
        with mock.patch.object(rv.subprocess, "run", return_value=fake) as run:
            res = rv.check_unit_tests(tests_dir)
        self.assertTrue(res["passed"])
        args, kw = run.call_args
        self.assertEqual(args[0][:4], [sys.executable, "-m", "unittest", "discover"])
        self.assertEqual(kw["timeout"], 300)
        self.assertEqual(kw["cwd"], str(tests_dir.parent))
        env = kw["env"]
        self.assertEqual(env["AGY_DR_TEST_SANDBOX"], "1")
        self.assertIn("agy-dr-sandbox-", env["LOCALAPPDATA"])
        self.assertEqual(env["LOCALAPPDATA"], env["APPDATA"])
        self.assertTrue(os.path.isfile(os.path.join(env["PYTHONPATH"].split(os.pathsep)[0], "sitecustomize.py")))

    def test_cli_help_subprocess_wiring(self):
        fake = subprocess.CompletedProcess([], 0, stdout="usage: x [-h]\n", stderr="")
        with mock.patch.object(rv.subprocess, "run", return_value=fake) as run:
            res = rv.check_cli_help(Path(HERE).parent)
        self.assertTrue(res["passed"], res)
        self.assertEqual(run.call_count, 3)
        for call in run.call_args_list:
            env = call.kwargs["env"]
            self.assertEqual(env["AGY_DR_TEST_SANDBOX"], "1")
            self.assertIn("agy-dr-sandbox-", env["LOCALAPPDATA"])
            self.assertEqual(call.kwargs["timeout"], 15)
            self.assertEqual(call.args[0][-1], "--help")

    def test_scripts_do_not_touch_windll_or_comtypes_at_import(self):
        scripts = Path(HERE).parent
        for f in sorted(scripts.glob("*.py")):
            tree = ast.parse(f.read_text(encoding="utf-8"))
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    continue
                for n in ast.walk(node):
                    with self.subTest(file=f.name, line=getattr(n, "lineno", 0)):
                        if isinstance(n, ast.Attribute):
                            self.assertNotEqual(n.attr, "windll")
                        if isinstance(n, ast.Import):
                            self.assertFalse(any(a.name.split(".")[0] == "comtypes" for a in n.names))
                        if isinstance(n, ast.ImportFrom):
                            self.assertNotEqual((n.module or "").split(".")[0], "comtypes")

    def test_fixture_hygiene(self):
        fixtures = Path(HERE) / "fixtures"
        email = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
        for f in sorted(fixtures.rglob("*")):
            if not f.is_file():
                continue
            text = f.read_text(encoding="utf-8")
            with self.subTest(file=f.name):
                self.assertEqual(set(email.findall(text)) - {"test@example.invalid"}, set())
                for m in re.finditer(r"Google (?:계정|Account)[^\"\n]*", text):
                    self.assertEqual(m.group(0), "Google 계정: 테스트(test@example.invalid)")

    def test_reads_and_own_folder_not_flagged(self):
        self.put("r.py", 'def f():\n    p = "Google\\\\Gemini\\\\x"\n    return open(p, "r").read()\n')
        self.put("o.py", 'def g():\n    p = r"%LOCALAPPDATA%\\agy-deep-research\\s.json"\n    open(p, "w").write("x")\n')
        res = rv.check_no_app_dir_writes(self.skill)
        self.assertTrue(res["passed"], res["errors"])

    def test_obsolete_scripts_tuple_is_the_only_exemption(self):
        # a removal list of obsolete file names is allowed; the same names anywhere else are not
        self.put("ok.py", 'OBSOLETE_SCRIPTS = ("patch_gemini_app.py", "chrome_launcher.py")\n')
        self.assertTrue(rv.check_forbidden_automation(self.skill)["passed"])
        for body in ('import chrome_launcher\n',
                     'X = ("chrome_launcher.py",)\n',
                     'OBSOLETE_SCRIPTS = ("chrome_launcher.py",)  # import chrome_launcher\n',
                     'OBSOLETE_SCRIPTS = ("chrome_launcher.py", "--remote-debugging-port=1")\n',
                     'OBSOLETE_SCRIPTS = ("chrome_launcher.py", "app.asar")\n',
                     'OBSOLETE_SCRIPTS = ("x.py",)\nY = "chrome_launcher"\n'):
            with self.subTest(body=body):
                self.put("ok.py", body)
                self.assertFalse(rv.check_forbidden_automation(self.skill)["passed"])

    def test_current_tree_passes(self):
        real_skill = Path(HERE).parent.parent
        for check in (rv.check_forbidden_automation, rv.check_no_app_dir_writes):
            res = check(real_skill)
            self.assertTrue(res["passed"], (check.__name__, res["errors"][:10]))

    def test_result_shape(self):
        for res in (rv.check_forbidden_automation(self.skill), rv.check_no_app_dir_writes(self.skill)):
            for k in ("name", "passed", "errors", "duration_s"):
                self.assertIn(k, res)
            self.assertTrue(res["passed"])


if __name__ == "__main__":
    unittest.main()

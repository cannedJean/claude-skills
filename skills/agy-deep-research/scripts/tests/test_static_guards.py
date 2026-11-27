import sandbox  # noqa: F401  (must stay the first import: blocks real-app access)
import os
import sys
import tempfile
import unittest
from pathlib import Path

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

    def test_run_all_includes_new_checks(self):
        names = rv.run_all_verification.__code__.co_names
        self.assertIn("check_forbidden_automation", names)
        self.assertIn("check_no_app_dir_writes", names)

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

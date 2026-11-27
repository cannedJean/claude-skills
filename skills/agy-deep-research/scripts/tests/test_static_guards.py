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
        for tok in ["remote-debugging", "connect_over_cdp", "chrome_launcher",
                    "9223", "9224", "--browser chrome", "app.asar"]:
            self.put("y.py", f"X = '{tok}'\n")
            res = rv.check_forbidden_automation(self.skill)
            self.assertFalse(res["passed"], tok)

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

    def test_reads_and_own_folder_not_flagged(self):
        self.put("r.py", 'def f():\n    p = "Google\\\\Gemini\\\\x"\n    return open(p, "r").read()\n')
        self.put("o.py", 'def g():\n    p = r"%LOCALAPPDATA%\\agy-deep-research\\s.json"\n    open(p, "w").write("x")\n')
        res = rv.check_no_app_dir_writes(self.skill)
        self.assertTrue(res["passed"], res["errors"])

    def test_result_shape(self):
        for res in (rv.check_forbidden_automation(self.skill), rv.check_no_app_dir_writes(self.skill)):
            for k in ("name", "passed", "errors", "duration_s"):
                self.assertIn(k, res)
            self.assertTrue(res["passed"])


if __name__ == "__main__":
    unittest.main()

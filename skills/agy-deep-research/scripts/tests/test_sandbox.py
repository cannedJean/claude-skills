"""test_sandbox.py - the test sandbox keeps every test run away from the real Gemini app."""
from __future__ import annotations

from tests import sandbox  # noqa: F401  (must stay the first import: blocks real-app access)

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402
from pathlib import Path  # noqa: E402

import run_verification as rv  # noqa: E402


class SandboxTests(unittest.TestCase):
    def test_env_redirected(self):
        for var in ("LOCALAPPDATA", "APPDATA"):
            real = os.environ["AGY_DR_REAL_" + var]
            cur = os.environ[var]
            self.assertNotEqual(os.path.normcase(real), os.path.normcase(cur))
            self.assertTrue(os.path.isdir(cur))
            self.assertIn("agy-dr-sandbox-", cur)
        self.assertEqual(os.environ["AGY_DR_TEST_SANDBOX"], "1")

    def test_popen_refuses_real_gemini_path(self):
        exe = sandbox.real_gemini_dir() + "\\Gemini.exe"
        with self.assertRaises(sandbox.SandboxViolation):
            subprocess.run([exe])
        with self.assertRaises(sandbox.SandboxViolation):
            subprocess.run('"%s" --x' % exe, shell=True)
        sub = os.path.join(sandbox.real_gemini_dir().upper(), "app-1", "x.bat").replace("\\", "/")
        with self.assertRaises(sandbox.SandboxViolation):
            subprocess.check_output([sub])

    def test_popen_refuses_patcher_and_browsers_and_debug_port(self):
        cases = {
            "patcher": [sys.executable, "patch_gemini_app.py"],
            "chrome": ["chrome.exe", "https://x"],
            "msedge": [r"C:\x\MSEdge.EXE"],
            "gemini": ["Gemini.exe"],
            "bare chrome": ["chrome", "https://x"],
            "debug port": [sys.executable, "-c", "pass", "--remote-debugging-port=9222"],
        }
        for name, argv in cases.items():
            with self.subTest(name):
                with self.assertRaises(sandbox.SandboxViolation):
                    subprocess.run(argv)

    def test_os_system_refused(self):
        with self.assertRaises(sandbox.SandboxViolation):
            os.system("chrome.exe --remote-debugging-port=1")

    def test_popen_allows_plain_python(self):
        cp = subprocess.run(
            [sys.executable, "-c",
             "import os;print(os.environ['AGY_DR_TEST_SANDBOX'], os.environ['LOCALAPPDATA'])"],
            capture_output=True, text=True)
        self.assertEqual(cp.returncode, 0)
        flag, local = cp.stdout.strip().split(" ", 1)
        self.assertEqual(flag, "1")
        self.assertEqual(local, os.environ["LOCALAPPDATA"])

    def test_check_tests_use_sandbox_flags_missing_import(self):
        with tempfile.TemporaryDirectory() as td:
            tests = Path(td) / "scripts" / "tests"
            tests.mkdir(parents=True)
            (tests / "test_good.py").write_text(
                '"""doc"""\nfrom __future__ import annotations\nimport sandbox  # noqa\nimport os\n')
            (tests / "test_bad.py").write_text("import os\nimport sandbox\n")
            (tests / "test_none.py").write_text("x = 1\n")
            res = rv.check_tests_use_sandbox(Path(td))
            self.assertFalse(res["passed"])
            joined = " ".join(res["errors"])
            self.assertIn("test_bad.py", joined)
            self.assertIn("test_none.py", joined)
            self.assertNotIn("test_good.py", joined)

    def test_check_tests_use_sandbox_passes_real_tree(self):
        res = rv.check_tests_use_sandbox(rv.find_skill_root())
        self.assertTrue(res["passed"], res["errors"])


if __name__ == "__main__":
    unittest.main()

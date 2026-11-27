"""test_sandbox.py - the test sandbox keeps every test run away from the real Gemini app.

Run with `python -m unittest discover -s tests -p test_sandbox.py` from scripts/ (tests/ on sys.path).
Every refusal is asserted before any process starts / any file is opened; probe paths sit under a
non-existent parent so that, even with a broken guard, nothing could be created.
"""
from __future__ import annotations

import sandbox  # noqa: F401  (must stay the first import: blocks real-app access)

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import run_verification as rv

PROBE = "__sandbox_probe_missing__"


def _probe(*more: str) -> str:
    return os.path.join(sandbox.real_gemini_dir(), PROBE, *(more or ("x.txt",)))


def _py(code: str, **kw):
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, **kw)


CHILD_CODE = ("import subprocess, sandbox, os\n"
              "subprocess.run([os.path.join(sandbox.real_gemini_dir(), %r, 'Gemini' + '.exe')])\n" % PROBE)


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
        exe = _probe("Gemini.exe")  # parent does not exist: nothing could start even if unguarded
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

    def test_popen_refuses_cwd_under_real_gemini(self):
        with self.assertRaises(sandbox.SandboxViolation):
            subprocess.run([sys.executable, "-c", "pass"], cwd=_probe("sub"))

    def test_popen_refuses_positional_cwd_under_real_gemini(self):
        # Popen(args, bufsize, executable, stdin, stdout, stderr, preexec_fn, close_fds, shell, cwd)
        with self.assertRaises(sandbox.SandboxViolation):
            subprocess.Popen([sys.executable, "-c", "pass"], -1, None, None, None, None, None,
                             True, False, _probe("sub"))

    def test_reason_variants(self):
        home_dir = os.path.join(os.path.expanduser("~"), "AppData", "Local", "Google", "Gemini", "x")
        self.assertIsNotNone(sandbox._reason(home_dir))
        self.assertIsNotNone(sandbox._reason(home_dir.replace("\\", "/").upper()))
        self.assertIsNotNone(sandbox._reason(os.path.join(sandbox.real_gemini_dir(), "..", "Gemini", "y")))
        self.assertIsNotNone(sandbox._reason(
            'powershell -Command "Start-Process C:\\Users\\u\\AppData\\Local\\Google\\Gemini\\a.exe"'))
        self.assertIsNotNone(sandbox._reason('start "" "D:\\x\\Google\\Gemini\\a.exe"'))
        self.assertIsNone(sandbox._reason("--browser"))
        self.assertIsNone(sandbox._reason("chrome"))

    def test_os_system_refused(self):
        with self.assertRaises(sandbox.SandboxViolation):
            os.system("chrome.exe --remote-debugging-port=1")

    def test_spawn_and_exec_refused(self):
        exe = _probe("Gemini.exe")
        for name, args in (("spawnl", (os.P_NOWAIT, exe, exe)), ("spawnv", (os.P_NOWAIT, exe, [exe])),
                           ("execl", (exe, exe)), ("execv", (exe, [exe]))):
            with self.subTest(name):
                with self.assertRaises(sandbox.SandboxViolation):
                    getattr(os, name)(*args)

    def test_popen_allows_plain_python(self):
        cp = _py("import os;print(os.environ['AGY_DR_TEST_SANDBOX'], os.environ['LOCALAPPDATA'])")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        flag, local = cp.stdout.strip().split(" ", 1)
        self.assertEqual(flag, "1")
        self.assertEqual(local, os.environ["LOCALAPPDATA"])

    def test_child_python_is_guarded(self):
        shim = os.environ["AGY_DR_SHIM_DIR"]
        self.assertTrue(os.path.isfile(os.path.join(shim, "sitecustomize.py")))
        self.assertTrue(os.environ["PYTHONPATH"].startswith(shim))
        cp = _py(CHILD_CODE)
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("SandboxViolation", cp.stderr)

    def test_grandchild_python_is_guarded(self):
        outer = ("import subprocess, sys\n"
                 "r = subprocess.run([sys.executable, '-c', %r], capture_output=True, text=True)\n"
                 "print(r.returncode); print(r.stderr)\n" % CHILD_CODE)
        cp = _py(outer)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        first, rest = cp.stdout.split("\n", 1)
        self.assertNotEqual(first.strip(), "0")
        self.assertIn("SandboxViolation", rest)

    def test_writes_under_real_gemini_refused(self):
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "src.txt")
            with open(src, "w") as fh:
                fh.write("x")
            appdata = os.path.join(os.environ["AGY_DR_REAL_APPDATA"], "gemini", PROBE, "x.txt")
            attempts = {
                "open w": lambda: open(_probe(), "w"),
                "open a": lambda: open(_probe(), "a"),
                "open kw": lambda: open(file=_probe(), mode="x"),
                "open appdata": lambda: open(appdata, "w"),
                "mkdir": lambda: os.mkdir(_probe("a", "b")),
                "replace": lambda: os.replace(src, _probe()),
                "rename src": lambda: os.rename(_probe(), os.path.join(td, "o")),
                "remove": lambda: os.remove(_probe()),
                "copy": lambda: shutil.copy(src, _probe()),
                "copyfile": lambda: shutil.copyfile(src, _probe()),
                "move": lambda: shutil.move(src, _probe()),
                "rmtree": lambda: shutil.rmtree(_probe("d")),
                "write_text": lambda: pathlib.Path(_probe()).write_text("x"),
                "write_bytes": lambda: pathlib.Path(_probe()).write_bytes(b"x"),
                "touch": lambda: pathlib.Path(_probe()).touch(),
                "path mkdir": lambda: pathlib.Path(_probe("a", "b")).mkdir(),
                "os.open": lambda: os.open(_probe(), os.O_WRONLY | os.O_CREAT),
            }
            for name, fn in attempts.items():
                with self.subTest(name):
                    with self.assertRaises(sandbox.SandboxViolation):
                        fn()

    def test_makedirs_is_wrapped(self):
        # not called against the real dir: makedirs would create the whole tree if the guard failed
        self.assertTrue(getattr(os.makedirs, "_agy_sandbox", False))
        with self.assertRaises(sandbox.SandboxViolation):
            sandbox._check_write([os.path.join(sandbox.real_gemini_dir(), "a", "b")])

    def test_reads_and_other_writes_allowed(self):
        with open(__file__, encoding="utf-8") as fh:
            self.assertIn("sandbox", fh.read())
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "ok.txt"
            p.write_text("hi")
            with open(p, "a") as fh:
                fh.write("!")
            self.assertEqual(p.read_text(), "hi!")
            shutil.copy(p, Path(td) / "copy.txt")
            os.replace(Path(td) / "copy.txt", Path(td) / "moved.txt")
        self.assertFalse(os.path.exists(_probe()))

    def test_install_is_idempotent(self):
        before = (os.environ["LOCALAPPDATA"], os.environ["APPDATA"], os.environ["AGY_DR_SHIM_DIR"],
                  os.environ["PYTHONPATH"], subprocess.Popen.__init__, os.system, os.replace)
        sandbox.install()
        after = (os.environ["LOCALAPPDATA"], os.environ["APPDATA"], os.environ["AGY_DR_SHIM_DIR"],
                 os.environ["PYTHONPATH"], subprocess.Popen.__init__, os.system, os.replace)
        self.assertEqual(before, after)

    def test_temp_dirs_removed_at_exit(self):
        env = dict(os.environ)
        for k in ("AGY_DR_TEST_SANDBOX", "AGY_DR_SHIM_DIR"):
            env.pop(k, None)
        env["LOCALAPPDATA"] = os.environ["AGY_DR_REAL_LOCALAPPDATA"]
        env["APPDATA"] = os.environ["AGY_DR_REAL_APPDATA"]
        env["PYTHONPATH"] = os.path.dirname(os.path.abspath(__file__))
        cp = subprocess.run([sys.executable, "-c",
                             "import os, sandbox; print(os.environ['LOCALAPPDATA']); "
                             "print(os.environ['AGY_DR_SHIM_DIR'])"],
                            capture_output=True, text=True, env=env)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        for d in cp.stdout.split():
            self.assertFalse(os.path.exists(d), d)

    def test_check_tests_use_sandbox_flags_missing_import(self):
        def run(files):
            with tempfile.TemporaryDirectory() as td:
                tests = Path(td) / "scripts" / "tests"
                tests.mkdir(parents=True)
                for name, text in files.items():
                    (tests / name).write_text(text)
                return rv.check_tests_use_sandbox(Path(td))

        res = run({
            "test_good.py": '"""doc"""\nfrom __future__ import annotations\n# c\nimport sandbox  # noqa\nimport os\n',
            "test_bad.py": "import os\nimport sandbox\n",
            "test_none.py": "x = 1\n",
            "test_try.py": "try:\n    import sandbox\nexcept ImportError:\n    pass\n",
            "test_if.py": "if True:\n    import sandbox\n",
            "test_assign.py": "X = 1\nimport sandbox\n",
        })
        self.assertFalse(res["passed"])
        joined = " ".join(res["errors"])
        for bad in ("test_bad.py", "test_none.py", "test_try.py", "test_if.py", "test_assign.py"):
            self.assertIn(bad, joined)
        self.assertNotIn("test_good.py", joined)

    def test_check_tests_use_sandbox_passes_real_tree(self):
        res = rv.check_tests_use_sandbox(rv.find_skill_root())
        self.assertTrue(res["passed"], res["errors"])

    def test_run_verification_uses_sandbox_env(self):
        env = rv._sandbox_env("X")
        self.assertEqual(env["AGY_DR_TEST_SANDBOX"], "1")
        self.assertEqual(env["LOCALAPPDATA"], "X")
        self.assertEqual(env["APPDATA"], "X")
        self.assertIn("AGY_DR_REAL_LOCALAPPDATA", env)


if __name__ == "__main__":
    unittest.main()

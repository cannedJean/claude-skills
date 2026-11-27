"""test_sandbox.py - the test sandbox keeps every test run away from the real Gemini app.

Run with `python -m unittest discover -s tests -p test_sandbox.py` from scripts/ (tests/ on sys.path).
Every refusal is asserted before any process starts / any file is opened; probe paths sit under a
non-existent parent so that, even with a broken guard, nothing could be created.
"""
from __future__ import annotations

import sandbox  # noqa: F401  (must stay the first import: blocks real-app access)

import io
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
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))


def _probe(*more: str) -> str:
    return os.path.join(sandbox.real_gemini_dir(), PROBE, *(more or ("x.txt",)))


def _py(code: str, **kw):
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, **kw)


# The child code never imports sandbox: it can only be guarded through the sitecustomize shim on PYTHONPATH.
# If the guard is missing it exits 3 ("UNGUARDED") before trying anything; the probe path (baked in by the parent)
# sits under a non-existent folder and names tool.bat, so the directory guard is what has to fire (hex-encoded
# so that the parent's own guard does not refuse the child command line).
CHILD_CODE = ("import subprocess, sys\n"
              "if 'sandbox' not in sys.modules or not getattr(subprocess.Popen.__init__, '_agy_sandbox', False):\n"
              "    print('UNGUARDED'); sys.exit(3)\n"
              "subprocess.run([bytes.fromhex(%r).decode()])\n"
              % os.path.join(sandbox.real_gemini_dir(), PROBE, "tool.bat").encode().hex())


class SandboxTests(unittest.TestCase):
    def test_env_redirected(self):
        for var in ("LOCALAPPDATA", "APPDATA"):
            real = os.environ["AGY_DR_REAL_" + var]
            cur = os.environ[var]
            self.assertNotEqual(os.path.normcase(real), os.path.normcase(cur))
            self.assertTrue(os.path.isdir(cur))
            self.assertIn("agy-dr-sandbox-", cur)
        self.assertEqual(os.environ["AGY_DR_TEST_SANDBOX"], "1")

    def test_real_gemini_dir_value(self):
        # computed in a fresh interpreter from a known LOCALAPPDATA (not from sandbox's own AGY_DR_REAL_* record)
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGY_DR_") and k != "PYTHONPATH"}
        known = os.path.join(tempfile.gettempdir(), "agy-dr-known-localappdata")
        env["LOCALAPPDATA"] = known
        code = ("import sys; sys.path.insert(0, %r); import sandbox; print(sandbox.real_gemini_dir())" % TESTS_DIR)
        cp = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(cp.stdout.strip(), os.path.join(known, "Google", "Gemini"))
        self.assertEqual(sandbox.real_gemini_dir(),
                         os.path.join(os.environ["AGY_DR_REAL_LOCALAPPDATA"], "Google", "Gemini"))

    def test_popen_refuses_real_gemini_dir_by_path_not_name(self):
        tool = _probe("tool.bat")  # no bad name in it: only the directory guard can refuse it
        self.assertNotIn("gemini.exe", tool.lower())
        with self.assertRaises(sandbox.SandboxViolation) as cm:
            subprocess.run([tool])
        self.assertIn("real Gemini app directory", str(cm.exception))

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

    def test_popen_checks_executable_keyword_and_positional(self):
        missing = os.path.join(tempfile.gettempdir(), "__agy_dr_missing__")  # never exists: nothing could start
        for exe in (os.path.join(missing, "chrome"), os.path.join(missing, "msedge"), _probe("tool.bat")):
            with self.subTest(exe=exe):
                with self.assertRaises(sandbox.SandboxViolation):
                    subprocess.Popen(["harmless"], executable=exe)
                with self.assertRaises(sandbox.SandboxViolation):
                    subprocess.Popen(["harmless"], -1, exe)

    def test_popen_passes_materialised_iterator_args(self):
        cp = subprocess.run(iter([sys.executable, "-c", "print('it-ok')"]), capture_output=True, text=True)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(cp.stdout.strip(), "it-ok")
        with self.assertRaises(sandbox.SandboxViolation):
            subprocess.run(x for x in ["chrome.exe", "https://x"])

    def test_reason_browser_command_forms(self):
        for cmd in (r"C:\Program Files\Google\Chrome\Application\chrome",
                    '"C:\\Program Files\\Microsoft\\Edge\\Application\\msedge"',
                    "cmd /c chrome https://x", "cmd /k msedge", "start chrome https://x", 'start "" chrome',
                    "powershell Start-Process chrome", "powershell -c Start-Process -FilePath msedge"):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(sandbox._reason(cmd, True))
        self.assertIsNotNone(sandbox._reason('powershell -Command "Start-Process\n  D:\\x\\Google\\Gemini\\a"'))
        for ok in ("python x.py --name chrome", "echo msedge", "chromecast-tool", "git log"):
            with self.subTest(ok=ok):
                self.assertIsNone(sandbox._reason(ok, True))

    def test_write_guard_long_path_prefix_and_realpath(self):
        for prefix in ("\\\\?\\", "\\??\\"):
            with self.subTest(prefix=prefix):
                with self.assertRaises(sandbox.SandboxViolation):
                    sandbox._check_write([prefix + _probe()])
        with self.assertRaises(sandbox.SandboxViolation):
            subprocess.run(["\\\\?\\" + _probe("tool.bat")])
        # realpath is applied even when the path does not exist yet (8.3 names / links of a new file)
        if os.name != "nt":
            self.skipTest("junctions are Windows-only")
        with tempfile.TemporaryDirectory() as td:
            target = os.path.join(td, "target")
            os.mkdir(target)
            link = os.path.join(td, "jn")
            r = subprocess.run(["cmd", "/c", "mklink", "/J", link, target], capture_output=True)
            if r.returncode != 0:
                self.skipTest("could not create a junction")
            try:
                needles = {sandbox._norm(os.path.realpath(target))}
                self.assertTrue(sandbox._under(os.path.join(link, "new_file.txt"), needles))
            finally:
                os.rmdir(link)

    def test_wrapped_writes_check_keyword_names(self):
        calls = []

        class Owner:
            @staticmethod
            def makedirs(name, mode=0o777, exist_ok=False):
                calls.append(name)

            @staticmethod
            def removedirs(name):
                calls.append(name)

        sandbox._wrap_write(Owner, "makedirs", (0,))
        sandbox._wrap_write(Owner, "removedirs", (0,))
        for fn in (Owner.makedirs, Owner.removedirs):
            with self.subTest(fn=fn.__name__):
                with self.assertRaises(sandbox.SandboxViolation):
                    fn(name=_probe("a", "b"))
        self.assertEqual(calls, [])
        self.assertTrue(getattr(os.makedirs, "_agy_sandbox", False))
        self.assertTrue(getattr(os.removedirs, "_agy_sandbox", False))

    @unittest.skipUnless(hasattr(os, "startfile"), "os.startfile is Windows-only")
    def test_startfile_checks_keywords(self):
        harmless = os.path.join(tempfile.gettempdir(), "__agy_dr_missing__.txt")
        for kw in ({"arguments": "--remote-debugging-port=1"}, {"arguments": _probe("tool.bat")},
                   {"cwd": _probe("sub")}):
            with self.subTest(kw=kw):
                with self.assertRaises(sandbox.SandboxViolation):
                    os.startfile(harmless, "open", **kw)
        with self.assertRaises(sandbox.SandboxViolation):
            os.startfile(_probe("tool.bat"))

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
        self.assertNotIn("import sandbox", CHILD_CODE)
        cp = _py(CHILD_CODE)
        self.assertNotIn("UNGUARDED", cp.stdout)
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("SandboxViolation", cp.stderr)

    def test_grandchild_python_is_guarded(self):
        outer = ("import subprocess, sys\n"
                 "r = subprocess.run([sys.executable, '-c', %r], capture_output=True, text=True)\n"
                 "print(r.returncode); print(r.stderr)\n" % CHILD_CODE)
        cp = _py(outer)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        first, rest = cp.stdout.split("\n", 1)
        self.assertNotIn(first.strip(), ("0", "3"))
        self.assertNotIn("UNGUARDED", rest)
        self.assertIn("SandboxViolation", rest)

    def test_writes_under_real_gemini_refused(self):
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "src.txt")
            with open(src, "w") as fh:
                fh.write("x")
            appdata = os.path.join(sandbox._real_appdata_gemini(), PROBE, "x.txt")
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
                "io.open": lambda: io.open(_probe(), "w"),
                "copy2": lambda: shutil.copy2(src, _probe()),
                "copytree": lambda: shutil.copytree(td, _probe("tree")),
                "rename dst": lambda: os.rename(src, _probe()),
                "unlink": lambda: os.unlink(_probe()),
                "rmdir": lambda: os.rmdir(_probe("d")),
                "truncate": lambda: os.truncate(_probe(), 0),
                "path unlink": lambda: pathlib.Path(_probe()).unlink(),
                "path rename": lambda: pathlib.Path(src).rename(_probe()),
                "path replace": lambda: pathlib.Path(src).replace(_probe()),
                "path open w": lambda: pathlib.Path(_probe()).open("w"),
                "open r+": lambda: open(_probe(), "r+"),
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
        # reads of paths under the real Gemini dir are not refused by the guard (they just do not exist here)
        for read in (lambda: open(_probe(), "r"), lambda: open(_probe(), "rb"), lambda: pathlib.Path(_probe()).read_text(),
                     lambda: os.open(_probe(), os.O_RDONLY)):
            with self.subTest(read=read):
                with self.assertRaises(FileNotFoundError):
                    read()
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
        dirs = [d for d in cp.stdout.splitlines() if d.strip()]
        self.assertEqual(len(dirs), 2, cp.stdout)
        for d in dirs:
            self.assertFalse(os.path.exists(d), d)

    def test_check_tests_use_sandbox_flags_missing_import(self):
        def run(files):
            with tempfile.TemporaryDirectory() as td:
                tests = Path(td) / "scripts" / "tests"
                tests.mkdir(parents=True)
                for name, text in files.items():
                    (tests / name).parent.mkdir(parents=True, exist_ok=True)
                    (tests / name).write_text(text)
                return rv.check_tests_use_sandbox(Path(td))

        res = run({
            "test_good.py": '"""doc"""\nfrom __future__ import annotations\n# c\nimport sandbox  # noqa\nimport os\n',
            "test_bad.py": "import os\nimport sandbox\n",
            "test_none.py": "x = 1\n",
            "test_try.py": "try:\n    import sandbox\nexcept ImportError:\n    pass\n",
            "test_if.py": "if True:\n    import sandbox\n",
            "test_assign.py": "X = 1\nimport sandbox\n",
            "sub/test_nested.py": "import os\nimport sandbox\n",
        })
        self.assertFalse(res["passed"])
        joined = " ".join(res["errors"])
        for bad in ("test_bad.py", "test_none.py", "test_try.py", "test_if.py", "test_assign.py", "test_nested.py"):
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
        # the sitecustomize shim (and the tests dir) lead PYTHONPATH, so a test without `import sandbox` is guarded,
        # even when the caller's own environment carries no shim (run_verification.py started from a plain shell)
        from unittest import mock
        bare = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "AGY_DR_SHIM_DIR")}
        with mock.patch.dict(os.environ, bare, clear=True):
            env = rv._sandbox_env("X")
        first = env["PYTHONPATH"].split(os.pathsep)
        self.assertTrue(os.path.isfile(os.path.join(first[0], "sitecustomize.py")), first)
        self.assertEqual(os.path.normcase(first[1]), os.path.normcase(TESTS_DIR))

    def test_sandbox_env_child_without_import_is_guarded(self):
        env = rv._sandbox_env(tempfile.gettempdir())
        cp = subprocess.run([sys.executable, "-c", CHILD_CODE], capture_output=True, text=True, env=env)
        self.assertNotIn("UNGUARDED", cp.stdout)
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("SandboxViolation", cp.stderr)

    def test_shim_fails_loudly_when_sandbox_cannot_import(self):
        shim = os.environ["AGY_DR_SHIM_DIR"]
        text = Path(shim, "sitecustomize.py").read_text(encoding="utf-8")
        self.assertNotIn("pass", text.split())
        self.assertIn(repr(TESTS_DIR), text)  # the tests dir is baked in, not taken from PYTHONPATH


if __name__ == "__main__":
    unittest.main()

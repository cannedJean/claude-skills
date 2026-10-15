"""Tests for the work-dir confinement (confine_hook.py + agy_task.Confinement) without agy.

  python -m unittest discover -s scripts/tests     (from the skill folder)
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))
import agy_task  # noqa: E402

HOOK = SCRIPTS / "confine_hook.py"
WIN = os.name == "nt"
BS = "\\"


class Hook(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = Path(tempfile.mkdtemp(prefix="cf_"))
        cls.root = cls.base / "work dir"
        (cls.root / ".agents").mkdir(parents=True)
        (cls.root / "sub").mkdir()
        (cls.root / "in.txt").write_text("x")
        (cls.base / "secret.txt").write_text("s")
        cls.junction = False
        if WIN:
            cp = subprocess.run(["cmd", "/c", "mklink", "/J", str(cls.root / "jn"), str(cls.base)], capture_output=True)
            cls.junction = cp.returncode == 0
        else:
            os.symlink(cls.base, cls.root / "jn")
            cls.junction = True

    @classmethod
    def tearDownClass(cls):
        if (cls.root / "jn").exists():
            os.rmdir(cls.root / "jn") if WIN else os.unlink(cls.root / "jn")
        shutil.rmtree(cls.base, True)

    def decide(self, name, args, env_root=True, stdin=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGY_CONFINE_")}
        if env_root:
            env["AGY_CONFINE_ROOT"] = str(self.root)
        data = stdin if stdin is not None else json.dumps({"toolCall": {"name": name, "args": args}}).encode()
        cp = subprocess.run([sys.executable, str(HOOK)], input=data, capture_output=True,
                            cwd=str(self.root / ".agents"), env=env)
        return json.loads(cp.stdout)["decision"]

    def check(self, cases):
        for label, name, args, want in cases:
            with self.subTest(label):
                self.assertEqual(self.decide(name, args), want, label)

    def test_inside_is_left_to_agy(self):
        R, Rf = str(self.root), str(self.root).replace(BS, "/")
        self.check([
            ("relative", "view_file", {"AbsolutePath": "in.txt"}, "ask"),
            ("absolute", "view_file", {"AbsolutePath": R + os.sep + "in.txt"}, "ask"),
            ("forward slashes", "view_file", {"AbsolutePath": Rf + "/sub"}, "ask"),
            ("the root itself", "view_file", {"AbsolutePath": R}, "ask"),
            (".. that stays inside", "view_file", {"AbsolutePath": "sub/../in.txt"}, "ask"),
            ("new file, path in content", "write_to_file",
             {"TargetFile": R + os.sep + "new.py", "CodeContent": "open('/etc/passwd')"}, "ask"),
            ("http url", "read_url_content", {"Url": "https://example.com/x"}, "ask"),
            ("no path at all", "search_web", {"query": "heapq"}, "ask"),
            ("relative glob", "find_by_name", {"SearchDirectory": R, "Pattern": "*.py"}, "ask"),
            ("read the hook config", "view_file", {"AbsolutePath": ".agents/hooks.json"}, "ask"),
        ])

    def test_outside_is_denied(self):
        B, Bf = str(self.base), str(self.base).replace(BS, "/")
        self.check([
            ("parent", "view_file", {"AbsolutePath": "../secret.txt"}, "deny"),
            ("absolute", "view_file", {"AbsolutePath": B + os.sep + "secret.txt"}, "deny"),
            ("sibling with the root as prefix", "view_file", {"AbsolutePath": str(self.root) + "2" + os.sep + "x"}, "deny"),
            ("rooted", "view_file", {"AbsolutePath": "/etc/passwd"}, "deny"),
            ("home", "view_file", {"AbsolutePath": "~/.gemini/antigravity-cli/settings.json"}, "deny"),
            ("file url", "read_url_content", {"Url": "file://" + ("/" if WIN else "") + Bf + "/secret.txt"}, "deny"),
            ("file url with %2e%2e", "read_url_content",
             {"Url": "file://" + ("/" if WIN else "") + str(self.root).replace(BS, "/").replace(" ", "%20") + "/%2e%2e/secret.txt"},
             "deny"),
            ("file url with a host", "read_url_content", {"Url": "file://server/share/x"}, "deny"),
            ("glob climbing out", "find_by_name", {"SearchDirectory": ".", "Pattern": "../*"}, "deny"),
            ("path in a nested list", "multi_replace_file_content", {"TargetFile": "in.txt", "Extra": [B]}, "deny"),
            ("unknown tool", "brand_new_tool", {"Whatever": B + os.sep + "secret.txt"}, "deny"),
            ("list a dir outside", "list_dir", {"DirectoryPath": B}, "deny"),
        ])

    @unittest.skipUnless(WIN, "Windows path forms")
    def test_windows_path_tricks_are_denied(self):
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        ctypes.windll.kernel32.GetShortPathNameW(str(self.base / "secret.txt"), buf, 1024)
        B = str(self.base)
        self.check([
            ("8.3 short name", "view_file", {"AbsolutePath": buf.value}, "deny"),
            ("upper case root + ..", "view_file", {"AbsolutePath": str(self.root).upper() + "\\..\\SECRET.TXT"}, "deny"),
            ("\\\\?\\ prefix", "view_file", {"AbsolutePath": "\\\\?\\" + B + "\\secret.txt"}, "deny"),
            ("GLOBALROOT", "view_file", {"AbsolutePath": "\\\\?\\GLOBALROOT\\Device\\HarddiskVolume3\\x"}, "deny"),
            ("device namespace", "view_file", {"AbsolutePath": "\\\\.\\C:\\x"}, "deny"),
            ("NT object path", "view_file", {"AbsolutePath": "\\??\\" + B + "\\secret.txt"}, "deny"),
            ("UNC admin share", "view_file", {"AbsolutePath": "\\\\localhost\\c$\\Users"}, "deny"),
            ("UNC with slashes", "view_file", {"AbsolutePath": "//localhost/c$/Users"}, "deny"),
            ("drive-relative", "view_file", {"AbsolutePath": "C:secret.txt"}, "deny"),
            ("other drive", "view_file", {"AbsolutePath": "Z:\\x"}, "deny"),
            ("drive-rooted", "view_file", {"AbsolutePath": "\\Windows\\win.ini"}, "deny"),
            ("FILE: upper case", "read_url_content", {"Url": "FILE:///" + B.replace(BS, "/") + "/secret.txt"}, "deny"),
            ("absolute glob", "find_by_name", {"SearchDirectory": ".", "Pattern": "C:/Users/*"}, "deny"),
        ])

    def test_link_out_of_the_root_is_denied(self):
        if not self.junction:
            self.skipTest("could not create a junction/symlink")
        self.assertEqual(self.decide("view_file", {"AbsolutePath": "jn/secret.txt"}), "deny")

    def test_shell_browser_mcp_and_protected_writes_are_denied(self):
        self.check([
            ("shell", "run_command", {"CommandLine": "echo hi", "Cwd": str(self.root)}, "deny"),
            ("shell input", "send_command_input", {"Input": "x"}, "deny"),
            ("browser", "browser_open_url", {"Url": "https://x"}, "deny"),
            ("mcp", "mcp_fs_read", {"path": "in.txt"}, "deny"),
            ("write the hook config", "write_to_file", {"TargetFile": ".agents/hooks.json", "CodeContent": "{}"}, "deny"),
            ("write launcher metadata", "replace_file_content", {"TargetFile": ".agy_task/x/events.jsonl"}, "deny"),
        ])
        if WIN:
            self.assertEqual(self.decide("write_to_file", {"TargetFile": ".AGENTS\\hooks.json"}), "deny")

    def test_fails_closed(self):
        self.assertEqual(self.decide(None, None, stdin=b"not json"), "deny")
        self.assertEqual(self.decide("view_file", {"AbsolutePath": "in.txt"}, env_root=False), "ask")  # root = cwd/..
        self.assertEqual(self.decide("view_file", {"AbsolutePath": "../secret.txt"}, env_root=False), "deny")

    def test_audit_log(self):
        log = self.base / "audit.jsonl"
        env = dict(os.environ, AGY_CONFINE_ROOT=str(self.root), AGY_CONFINE_LOG=str(log))
        payload = {"toolCall": {"name": "view_file", "args": {"AbsolutePath": "../secret.txt"}}}
        subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload).encode(), capture_output=True,
                       cwd=str(self.root / ".agents"), env=env)
        entry = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual((entry["tool"], entry["decision"]), ("view_file", "deny"))


class Installer(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="cfw_"))
        self.addCleanup(shutil.rmtree, self.work, True)

    def test_installs_and_removes_the_hook(self):
        c = agy_task.Confinement(self.work, self.work / ".agy_task" / "s" / "confine.jsonl")
        with c:
            spec = json.loads(c.file.read_text(encoding="utf-8"))
            cmd = spec["agy-task-confine"]["PreToolUse"][0]["hooks"][0]["command"]
            self.assertIn("confine_hook.py", cmd)
            self.assertEqual(spec["agy-task-confine"]["PreToolUse"][0]["matcher"], "*")
            self.assertEqual(c.env()["AGY_CONFINE_ROOT"], str(self.work))
            # the command line agy will run through the platform shell starts the hook
            cp = subprocess.run(cmd, shell=True, cwd=str(c.dir), capture_output=True, env=dict(os.environ, **c.env()),
                                input=json.dumps({"toolCall": {"name": "view_file", "args": {"AbsolutePath": "../x"}}}).encode())
            self.assertEqual(json.loads(cp.stdout)["decision"], "deny", cp.stderr)
        self.assertFalse(c.dir.exists())

    def test_restores_an_existing_hooks_file(self):
        (self.work / ".agents").mkdir()
        (self.work / ".agents" / "hooks.json").write_text('{"mine": {}}', encoding="utf-8")
        with agy_task.Confinement(self.work, self.work / "log.jsonl") as c:
            self.assertIn("agy-task-confine", c.file.read_text(encoding="utf-8"))
        self.assertEqual((self.work / ".agents" / "hooks.json").read_text(encoding="utf-8"), '{"mine": {}}')

    def test_removed_even_when_the_run_fails(self):
        c = agy_task.Confinement(self.work, self.work / "log.jsonl")
        with self.assertRaises(RuntimeError):
            with c:
                raise RuntimeError("agy crashed")
        self.assertFalse(c.dir.exists())

    def test_report_counts_denials(self):
        log = self.work / "log.jsonl"
        log.write_text('{"decision": "ask"}\n{"decision": "deny", "tool": "view_file"}\nnot json\n', encoding="utf-8")
        rep = agy_task.Confinement(self.work, log).report()
        self.assertEqual((rep["tool_calls"], rep["denied_count"]), (2, 1))

    def test_allow_all_needs_no_confine(self):
        p = agy_task.build_parser()
        a = p.parse_args(["run", "-t", "x", "--allow-all"])
        with self.assertRaises(SystemExit) as cm:
            agy_task.check_confine_args(a)
        self.assertEqual(cm.exception.code, 5)
        agy_task.check_confine_args(p.parse_args(["run", "-t", "x", "--allow-all", "--no-confine"]))
        self.assertTrue(p.parse_args(["run", "-t", "x"]).confine)
        self.assertTrue(p.parse_args(["resume", "cid", "-t", "x"]).confine)


if __name__ == "__main__":
    unittest.main()

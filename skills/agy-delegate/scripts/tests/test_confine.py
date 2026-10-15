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

    def decide(self, name, args, env_root=True, stdin=None, inlined=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGY_CONFINE_")}
        if env_root:
            env["AGY_CONFINE_ROOT"] = str(self.root)
        if inlined is not None:
            env["AGY_CONFINE_INLINED"] = json.dumps(inlined)
        data = stdin if stdin is not None else json.dumps({"toolCall": {"name": name, "args": args}}).encode()
        cp = subprocess.run([sys.executable, str(HOOK)], input=data, capture_output=True,
                            cwd=str(self.root / ".agents"), env=env)
        return json.loads(cp.stdout)["decision"]

    def test_viewing_an_inlined_file_is_denied(self):
        R = str(self.root)
        for label, name, args, want in [
            ("relative", "view_file", {"AbsolutePath": "in.txt"}, "deny"),
            ("absolute", "view_file_outline", {"AbsolutePath": R + os.sep + "in.txt"}, "deny"),
            ("other file", "view_file", {"AbsolutePath": "sub"}, "ask"),
            ("listing is fine", "list_dir", {"DirectoryPath": R}, "ask"),
            ("editing is fine", "replace_file_content", {"TargetFile": "in.txt"}, "ask"),
        ]:
            with self.subTest(label):
                self.assertEqual(self.decide(name, args, inlined=["in.txt"]), want, label)
        self.assertEqual(self.decide("view_file", {"AbsolutePath": "in.txt"}), "ask")  # nothing inlined

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

    def test_hard_link_is_denied(self):
        outside = self.base / "linked_secret.txt"
        outside.write_text("s")
        try:
            os.link(outside, self.root / "hl.txt")
        except OSError as e:
            self.skipTest(f"no hard links here: {e}")
        self.addCleanup(os.unlink, self.root / "hl.txt")
        self.assertEqual(self.decide("view_file", {"AbsolutePath": "hl.txt"}), "deny")
        self.assertEqual(self.decide("write_to_file", {"TargetFile": "hl.txt", "CodeContent": "x"}), "deny")

    def test_traversal_look_alikes_are_denied(self):
        for p in ("sub/.. /.. /escape.txt", ".../escape.txt", "sub/. ./x", "..  /x"):
            with self.subTest(p):
                self.assertEqual(self.decide("write_to_file", {"TargetFile": p, "CodeContent": "x"}), "deny")

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
        self.assertEqual(entry["rule"], "path")

    def test_audit_log_names_the_rule(self):
        log = self.base / "rules.jsonl"
        log.unlink(missing_ok=True)
        env = dict(os.environ, AGY_CONFINE_ROOT=str(self.root), AGY_CONFINE_LOG=str(log),
                   AGY_CONFINE_INLINED=json.dumps(["in.txt"]))
        for name, args in [("view_file", {"AbsolutePath": "in.txt"}), ("run_command", {"CommandLine": "dir"}),
                           ("write_to_file", {"TargetFile": ".agents/hooks.json"}), ("list_dir", {"DirectoryPath": "."})]:
            subprocess.run([sys.executable, str(HOOK)], capture_output=True, cwd=str(self.root / ".agents"), env=env,
                           input=json.dumps({"toolCall": {"name": name, "args": args}}).encode())
        entries = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([e.get("rule") for e in entries], ["inlined", "shell", "protected", None])
        self.assertNotIn("Full text of", entries[0]["reason"])  # no caller's heading: usable standalone

    def test_an_inlined_path_does_not_hide_an_escape_in_the_same_call(self):
        log = self.base / "mixed.jsonl"
        log.unlink(missing_ok=True)
        env = dict(os.environ, AGY_CONFINE_ROOT=str(self.root), AGY_CONFINE_LOG=str(log),
                   AGY_CONFINE_INLINED=json.dumps(["in.txt"]))
        for extra in (str(self.base / "secret.txt"), "../secret.txt"):
            args = {"AbsolutePath": "in.txt", "Extra": extra}
            subprocess.run([sys.executable, str(HOOK)], capture_output=True, cwd=str(self.root / ".agents"), env=env,
                           input=json.dumps({"toolCall": {"name": "view_file", "args": args}}).encode())
        entries = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([(e["decision"], e.get("rule")) for e in entries], [("deny", "path")] * 2)


class Installer(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="cfw_"))
        self.addCleanup(shutil.rmtree, self.work, True)

    def run_hook_like_agy(self, c):
        """Run the installed command the way agy does: cmd /c <command> (sh -c elsewhere), cwd = .agents."""
        cmd = json.loads(c.file.read_text(encoding="utf-8"))[agy_task.HOOK_KEY]["PreToolUse"][0]["hooks"][0]["command"]
        argv = ["cmd", "/c", cmd] if WIN else ["sh", "-c", cmd]
        cp = subprocess.run(argv, cwd=str(c.dir), capture_output=True, env=dict(os.environ, **c.env()),
                            input=json.dumps({"toolCall": {"name": "view_file", "args": {"AbsolutePath": "../x"}}}).encode())
        return cmd, cp

    def test_installs_and_removes_the_hook(self):
        c = agy_task.Confinement(self.work, self.work / ".agy_task" / "s" / "confine.jsonl")
        with c:
            spec = json.loads(c.file.read_text(encoding="utf-8"))
            self.assertEqual(spec[agy_task.HOOK_KEY]["PreToolUse"][0]["matcher"], "*")
            self.assertEqual(c.env()["AGY_CONFINE_ROOT"], str(self.work))
            cmd, cp = self.run_hook_like_agy(c)
            self.assertEqual(json.loads(cp.stdout)["decision"], "deny", (cmd, cp.stderr))
        self.assertFalse(c.dir.exists())

    @unittest.skipUnless(WIN, "cmd /c quoting")
    def test_hook_starts_from_a_python_path_with_spaces_and_cmd_metacharacters(self):
        link = Path(tempfile.mkdtemp(prefix="cfpy_")) / "py & dir ^ 100%x% (x86)"
        self.addCleanup(shutil.rmtree, link.parent, True)
        cp = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(Path(sys.executable).parent)], capture_output=True)
        if cp.returncode:
            self.skipTest("could not create a junction")
        self.addCleanup(os.rmdir, link)
        old = sys.executable
        sys.executable = str(link / Path(old).name)
        try:
            with agy_task.Confinement(self.work, self.work / "log.jsonl") as c:
                cmd, cp = self.run_hook_like_agy(c)
        finally:
            sys.executable = old
        self.assertEqual(json.loads(cp.stdout or b"{}").get("decision"), "deny", (cmd, cp.stderr))

    def test_a_killed_runs_leftover_is_restored(self):
        # the agy session of a killed run may still be alive: it must stay hooked, so our own leftover entry and its
        # launcher come back byte for byte instead of being dropped
        with agy_task.Confinement(self.work, self.work / "log.jsonl") as c:
            leftover = c.file.read_bytes()
            launcher = c.launcher.read_bytes() if c.launcher.exists() else None
        (self.work / ".agents").mkdir()
        (self.work / ".agents" / "hooks.json").write_bytes(leftover)  # as if the last run was killed
        if launcher is not None:
            (self.work / ".agents" / agy_task.LAUNCHER).write_bytes(launcher)
        with agy_task.Confinement(self.work, self.work / "log.jsonl"):
            pass
        self.assertEqual((self.work / ".agents" / "hooks.json").read_bytes(), leftover)
        if launcher is not None:
            self.assertEqual((self.work / ".agents" / agy_task.LAUNCHER).read_bytes(), launcher)
        mixed = json.loads(leftover)
        mixed["mine"] = {"enabled": False}
        (self.work / ".agents" / "hooks.json").write_text(json.dumps(mixed), encoding="utf-8")
        with agy_task.Confinement(self.work, self.work / "log.jsonl"):
            pass
        self.assertEqual(json.loads((self.work / ".agents" / "hooks.json").read_text(encoding="utf-8")), mixed)

    def test_hook_files_are_never_a_change_of_a_confined_run(self):
        before = agy_task.snapshot(self.work, hook_files=True)
        with agy_task.Confinement(self.work, self.work / "log.jsonl"):
            self.assertEqual(agy_task.snapshot(self.work, hook_files=True), before)
            # without confinement nothing installs them: there they are always a change
            self.assertIn(".agents/hooks.json", agy_task.snapshot(self.work))

    def test_a_planted_hooks_file_is_a_change(self):
        # e.g. agy wrote its own PreToolUse hook in a --no-confine run: it would run on every later session there
        before = agy_task.snapshot(self.work, hook_files=True)
        (self.work / ".agents").mkdir()
        planted = {agy_task.HOOK_KEY: {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command",
                                                                                 "command": "calc.exe"}]}]}}
        (self.work / ".agents" / "hooks.json").write_text(json.dumps(planted), encoding="utf-8")
        (self.work / ".agents" / agy_task.LAUNCHER).write_bytes(b"@calc.exe" + bytes([13, 10]))
        created = agy_task.diff_snapshots(before, agy_task.snapshot(self.work, hook_files=True))["created"]
        self.assertEqual(created, [".agents/confine.cmd", ".agents/hooks.json"])

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

    def test_inlined_files_reach_the_hook(self):
        self.assertNotIn("AGY_CONFINE_INLINED", agy_task.Confinement(self.work, self.work / "l").env())
        a = agy_task.build_parser().parse_args(["run", "-t", "x", "--inlined", "a.py", "--inlined", "t/b.py"])
        env = agy_task.Confinement(self.work, self.work / "l", a.inlined).env()
        self.assertEqual(json.loads(env["AGY_CONFINE_INLINED"]), ["a.py", "t/b.py"])

    def test_report_counts_denials(self):
        log = self.work / "log.jsonl"
        log.write_text('{"decision": "ask"}\n{"decision": "deny", "tool": "view_file"}\nnot json\n', encoding="utf-8")
        rep = agy_task.Confinement(self.work, log).report()
        self.assertEqual((rep["tool_calls"], rep["denied_count"]), (2, 1))

    def test_report_keeps_inlined_denials_apart(self):
        log = self.work / "log.jsonl"
        lines = [{"decision": "deny", "rule": "inlined"}, {"decision": "deny", "rule": "path"},
                 {"decision": "deny"}, {"decision": "ask"}]  # an entry without a rule counts as a real denial
        log.write_text("".join(json.dumps(e) + "\n" for e in lines), encoding="utf-8")
        rep = agy_task.Confinement(self.work, log).report()
        self.assertEqual((rep["tool_calls"], rep["denied_count"], rep["inlined_denied_count"]), (4, 2, 1))
        self.assertNotIn("inlined", [e.get("rule") for e in rep["denied"]])

    def test_inlined_must_be_relative_inside_the_work_dir(self):
        p = agy_task.build_parser()
        for rel in ("/etc/passwd", "../x.py", "a/../../x.py", "C:/x.py", "~/x"):
            with self.subTest(rel):
                with self.assertRaises(SystemExit) as cm:
                    agy_task.check_confine_args(p.parse_args(["run", "-t", "x", "--inlined", rel]))
                self.assertEqual(cm.exception.code, 5)
        agy_task.check_confine_args(p.parse_args(["run", "-t", "x", "--inlined", "pkg/a.py"]))

    def test_a_users_launcher_file_is_kept(self):
        mine = b"@echo mine" + bytes([13, 10])
        (self.work / ".agents").mkdir()
        (self.work / ".agents" / agy_task.LAUNCHER).write_bytes(mine)
        with agy_task.Confinement(self.work, self.work / "log.jsonl") as c:
            if WIN:
                self.assertNotEqual(c.launcher.read_bytes(), mine)  # ours while the run lasts
        self.assertEqual((self.work / ".agents" / agy_task.LAUNCHER).read_bytes(), mine)

    def test_other_files_in_agents_still_count_as_changes(self):
        before = agy_task.snapshot(self.work)
        with agy_task.Confinement(self.work, self.work / "log.jsonl"):
            (self.work / ".agents" / "rules.md").write_text("x", encoding="utf-8")  # e.g. agy run with --no-confine
        self.assertEqual(agy_task.diff_snapshots(before, agy_task.snapshot(self.work))["created"], [".agents/rules.md"])

    def test_report_says_unconfined_when_the_hook_never_ran(self):
        tool_step = {"event": "step_update", "step_update": {"step_index": 2, "step_type": "tool", "state": "DONE"}}
        log = self.work / "log.jsonl"
        c = agy_task.Confinement(self.work, log)
        self.assertTrue(c.report([]).get("enabled"))  # no tool calls: nothing to enforce
        self.assertFalse(c.report([tool_step])["enabled"])  # tool calls, no hook log: not confined
        log.write_text('{"decision": "ask"}\n', encoding="utf-8")
        self.assertTrue(c.report([tool_step])["enabled"])
        step3 = json.loads(json.dumps(tool_step))
        step3["step_update"]["step_index"] = 3
        self.assertFalse(c.report([tool_step, step3])["enabled"])  # a call the hook did not see
        log.write_text("", encoding="utf-8")
        self.assertFalse(c.report([], changed=True)["enabled"])  # files changed, hook saw nothing

    @unittest.skipUnless(WIN, "the Windows command-line limit")
    def test_a_prompt_over_the_command_line_limit_is_a_clean_error(self):
        task = self.work / "task.md"
        task.write_text("x" * 40000, encoding="utf-8")
        cp = subprocess.run([sys.executable, str(SCRIPTS / "agy_task.py"), "--agy-bin", sys.executable, "run",
                             "-w", str(self.work), "-f", str(task), "--no-schema", "--bare"],
                            capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(cp.returncode, 3, cp.stderr)
        res = json.loads(cp.stdout)  # a JSON result, not a traceback
        self.assertEqual(res["status"], "error")
        self.assertIn("prompt too long for the Windows command line", res["agy_error"]["raw"])
        self.assertFalse((self.work / ".agents").exists())

    def test_bare_sends_the_task_unframed(self):
        cp = subprocess.run([sys.executable, str(SCRIPTS / "agy_task.py"), "--agy-bin", "agy", "run", "-w", str(self.work),
                             "-t", "TASK-TEXT", "--bare", "--dry-run"], capture_output=True, text=True)
        prompt = json.loads(cp.stdout)["prompt"]
        self.assertTrue(prompt.startswith("TASK-TEXT"), prompt)
        self.assertNotIn("When finished, report", prompt)
        self.assertTrue(json.loads(cp.stdout)["bare"])

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

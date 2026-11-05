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
        cls.dr_dir = cls.base / "dr_skill"
        cls.dr_dir.mkdir(parents=True, exist_ok=True)
        (cls.dr_dir / "scripts").mkdir(parents=True, exist_ok=True)
        (cls.dr_dir / "scripts" / "relay_step.py").write_text("# relay step stub", encoding="utf-8")
        (cls.dr_dir / "scripts" / "patch_gemini_app.py").write_text("# patch stub", encoding="utf-8")
        (cls.dr_dir / "SKILL.md").write_text("# DR Skill", encoding="utf-8")

        # Setup control files inside cls.root
        (cls.root / "relay").mkdir(parents=True, exist_ok=True)
        (cls.root / "prompt.md").write_text("prompt", encoding="utf-8")
        (cls.root / "goal.md").write_text("goal", encoding="utf-8")
        (cls.root / "feedback_1.md").write_text("feedback 1", encoding="utf-8")
        (cls.root / "feedback_review.md").write_text("feedback review", encoding="utf-8")
        (cls.root / "relay" / "request.json").write_text("{}", encoding="utf-8")
        (cls.root / "relay" / "result.json").write_text("{}", encoding="utf-8")
        (cls.root / "relay" / "started.json").write_text("{}", encoding="utf-8")

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

    def decide(self, name, args, env_root=True, stdin=None, inlined=None, mode=None, dr_dir=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGY_CONFINE_")}
        if env_root:
            env["AGY_CONFINE_ROOT"] = str(self.root)
        if inlined is not None:
            env["AGY_CONFINE_INLINED"] = json.dumps(inlined)
        if mode is not None:
            env["AGY_CONFINE_MODE"] = str(mode)
        if dr_dir is not None:
            env["AGY_CONFINE_DR_DIR"] = str(dr_dir)
        data = stdin if stdin is not None else json.dumps({"toolCall": {"name": name, "args": args}}).encode()
        cp = subprocess.run([sys.executable, str(HOOK)], input=data, capture_output=True,
                            cwd=str(self.root / ".agents"), env=env)
        return json.loads(cp.stdout)["decision"]

    def decide_audit(self, name, args, mode="deep-research", dr_dir=None):
        log = self.base / "test_audit.jsonl"
        log.unlink(missing_ok=True)
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGY_CONFINE_")}
        env["AGY_CONFINE_ROOT"] = str(self.root)
        env["AGY_CONFINE_LOG"] = str(log)
        if mode:
            env["AGY_CONFINE_MODE"] = str(mode)
        env["AGY_CONFINE_DR_DIR"] = str(dr_dir if dr_dir is not None else self.dr_dir)
        data = json.dumps({"toolCall": {"name": name, "args": args}}).encode()
        cp = subprocess.run([sys.executable, str(HOOK)], input=data, capture_output=True,
                            cwd=str(self.root / ".agents"), env=env)
        decision = json.loads(cp.stdout)["decision"]
        entry = json.loads(log.read_text(encoding="utf-8").splitlines()[-1]) if log.exists() else {}
        return decision, entry.get("rule"), entry.get("reason", "")

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

    def test_agent_prose_arguments_are_not_paths(self):
        self.check([
            ("edit url text", "edit_file", {"Path": "in.txt", "Target": "https://old.example.com", "Replacement": "https://new.example.com"}, "ask"),
            ("edit code text", "edit_file", {"Path": "in.txt", "Target": "x = 1", "Replacement": "x: int = 1"}, "ask"),
            ("edit css text", "edit_file", {"Path": "in.txt", "Target": "color: red;", "Replacement": "color: blue;"}, "ask"),
            ("finish notes", "finish", {"summary": "done", "files": ["in.txt"], "notes": "Note: run python -m pytest to verify"}, "ask"),
            ("edit outside still denied", "edit_file", {"Path": "../secret.txt", "Target": "a", "Replacement": "b"}, "deny"),
            ("other tool Target path still checked", "some_tool", {"Target": "../../secret.txt"}, "deny"),
        ])

    def test_native_browser_shell_like_and_resource_tools_are_denied(self):
        for name in ("open_browser_url", "execute_browser_javascript", "click_browser_pixel", "capture_browser_screenshot",
                     "read_browser_page", "list_browser_pages", "capture_browser_console_logs", "shell_exec",
                     "run_extension_code", "execute_notebook", "restart_dev_server", "read_resource", "list_resources"):
            with self.subTest(name=name):
                self.assertEqual(self.decide(name, {"Url": "https://x.example", "CommandLine": "dir"}), "deny")

    def test_unknown_tools_fail_closed(self):
        for name, args in [("run_workflow", {"Script": "import os", "Description": "x"}),
                           ("run_workflow", {"ScriptPath": "wf.py", "Description": "x"}),
                           ("sed_file", {"Path": "in.txt", "Script": "1r C:/x"}), ("custom_tool", {}), ("brand_new", {})]:
            with self.subTest(name=name):
                self.assertEqual(self.decide(name, args), "deny")
        self.assertEqual(self.decide("run_workflow", {"Script": "x"}, mode="deep-research", dr_dir=self.dr_dir), "deny")

    def test_known_agent_tools_inside_the_root_are_allowed(self):
        self.check([
            ("ask_question", "ask_question", {"questions": [{"question": "1. 목적 (Purpose): 이 작업의 목적은?",
                                                              "options": ["(a) x: y", "(b) z"]}]}, "ask"),
            ("define_subagent", "define_subagent", {"name": "edge_reviewer", "description": "d",
                                                    "system_prompt": "You are strict. Rules: cite lines."}, "ask"),
            ("invoke_subagent", "invoke_subagent", {"Prompt": "review in.txt", "Role": "QA: Edge Cases"}, "ask"),
            ("manage_task", "manage_task", {"Action": "create", "Title": "t"}, "ask"),
            ("schedule", "schedule", {"Cron": "*/5 * * * *", "Prompt": "x"}, "ask"),
            ("send_message", "send_message", {"To": "abc", "Message": "Note: done"}, "ask"),
            ("write_file", "write_file", {"Path": "in.txt", "Content": "a: b", "Overwrite": True}, "ask"),
            ("read_file", "read_file", {"Path": "in.txt"}, "ask"),
        ])

    def test_browser_subagent_needs_browser_mode(self):
        for args in ({"Prompt": "Open https://example.com", "TypeName": "browser"},
                     {"Subagents": [{"TypeName": "Browser", "Prompt": "x"}]}):
            with self.subTest(args=args):
                self.assertEqual(self.decide("invoke_subagent", args), "deny")

    def test_subagent_workspace_must_be_the_root(self):
        self.assertEqual(self.decide("invoke_subagent", {"Prompt": "x", "Workspace": str(self.root / "sub")}), "deny")
        self.assertEqual(self.decide("invoke_subagent", {"Prompt": "x", "Workspace": str(self.base)}), "deny")
        self.assertEqual(self.decide("invoke_subagent", {"Prompt": "x", "Workspace": str(self.root)}), "ask")
        for v in ("inherit", "INHERIT", ""):
            with self.subTest(workspace=v):
                self.assertEqual(self.decide("invoke_subagent", {"Prompt": "x", "TypeName": "self", "Workspace": v}), "ask")
        self.assertEqual(self.decide("invoke_subagent", {"Subagents": [{"Prompt": "x", "Workspace": "inherit"}]}), "ask")
        sub_uri = "file:///" + str(self.root / "sub").replace(BS, "/")
        self.assertEqual(self.decide("invoke_subagent", {"Prompt": "x", "WorkspaceUris": [sub_uri]}), "deny")
        self.assertEqual(self.decide("invoke_subagent", {"Prompt": "x", "workspace_path": str(self.base)}), "deny")

    def test_agy_config_folders_are_read_only_at_any_depth(self):
        for rel in (".agent/hooks.json", "_agents/hooks.json", "_agent/mcp_config.json", "sub/.agents/hooks.json",
                    "sub/.agent/plugins/p/plugin.json", ".git/config", ".git/hooks/pre-commit", ".AGENT/x.json"):
            with self.subTest(rel=rel):
                self.assertEqual(self.decide("write_file", {"Path": rel, "Content": "{}"}), "deny")
        self.assertEqual(self.decide("read_file", {"Path": ".agents/hooks.json"}), "ask")

    def test_url_arguments_to_local_hosts_are_denied(self):
        for url in ("http://127.0.0.1:9222/json/list", "http://169.254.169.254/latest/meta-data/",
                    "http://localhost:3000/admin", "http://[::1]:9222/json/version", "http://intranet/",
                    "http://10.0.0.1/", "http://printer.local/", "http://[64:ff9b::7f00:1]/"):
            with self.subTest(url=url):
                self.assertEqual(self.decide("read_url_content", {"Url": url}), "deny")
        self.assertEqual(self.decide("read_url_content", {"Url": "https://example.com/x"}), "ask")

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

    def test_scheme_looking_values_in_file_arguments_are_denied(self):
        # Node and Win32 resolve "zz://../x" or "http://../x" as an ordinary relative path ("zz:" is a name)
        for name, args in [("write_to_file", {"TargetFile": "zz://../../escape.txt", "CodeContent": "x"}),
                           ("write_to_file", {"TargetFile": "http://../../escape.txt", "CodeContent": "x"}),
                           ("view_file", {"AbsolutePath": "zz://../../../../Users/x/.ssh/id_rsa"}),
                           ("view_file", {"AbsolutePath": "https://example.com/x"}),
                           ("list_dir", {"DirectoryPath": "ab://.."}),
                           ("grep_search", {"SearchPath": "zz://../..", "Query": "x"})]:
            with self.subTest(args=args):
                self.assertEqual(self.decide(name, args), "deny")
        # a real URL argument stays a URL
        self.assertEqual(self.decide("read_url_content", {"Url": "https://example.com/x"}), "ask")
        self.assertEqual(self.decide("read_url_content", {"Url": "zz://../../x"}), "deny")

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

    def test_deep_research_reads_allowed_in_workdir_and_dr_dir(self):
        # Reading inside workdir
        self.assertEqual(self.decide("view_file", {"AbsolutePath": "in.txt"}, mode="deep-research", dr_dir=self.dr_dir), "ask")
        self.assertEqual(self.decide("view_file", {"AbsolutePath": str(self.root / "in.txt")}, mode="deep-research", dr_dir=self.dr_dir), "ask")
        self.assertEqual(self.decide("list_dir", {"DirectoryPath": str(self.root)}, mode="deep-research", dr_dir=self.dr_dir), "ask")
        # Reading inside dr_dir
        self.assertEqual(self.decide("view_file", {"AbsolutePath": str(self.dr_dir / "SKILL.md")}, mode="deep-research", dr_dir=self.dr_dir), "ask")
        self.assertEqual(self.decide("view_file", {"AbsolutePath": str(self.dr_dir / "scripts" / "relay_step.py")}, mode="deep-research", dr_dir=self.dr_dir), "ask")
        self.assertEqual(self.decide("list_dir", {"DirectoryPath": str(self.dr_dir)}, mode="deep-research", dr_dir=self.dr_dir), "ask")
        self.assertEqual(self.decide("grep_search", {"SearchPath": str(self.dr_dir), "Query": "DR"}, mode="deep-research", dr_dir=self.dr_dir), "ask")
        self.assertEqual(self.decide("find_by_name", {"SearchDirectory": str(self.dr_dir), "Pattern": "*.py"}, mode="deep-research", dr_dir=self.dr_dir), "ask")

    def test_deep_research_reads_outside_workdir_and_dr_dir_denied(self):
        dec, rule, _ = self.decide_audit("view_file", {"AbsolutePath": str(self.base / "secret.txt")})
        self.assertEqual((dec, rule), ("deny", "path"))
        dec, rule, _ = self.decide_audit("view_file", {"AbsolutePath": "../secret.txt"})
        self.assertEqual((dec, rule), ("deny", "path"))
        dec, rule, _ = self.decide_audit("list_dir", {"DirectoryPath": str(self.base)})
        self.assertEqual((dec, rule), ("deny", "path"))
        dec, rule, _ = self.decide_audit("grep_search", {"SearchPath": str(self.base), "Query": "s"})
        self.assertEqual((dec, rule), ("deny", "path"))
        Bf = str(self.base).replace(BS, "/")
        dec, rule, _ = self.decide_audit("read_url_content", {"Url": "file://" + ("/" if WIN else "") + Bf + "/secret.txt"})
        self.assertEqual((dec, rule), ("deny", "path"))

    def test_deep_research_writes_outside_workdir_denied(self):
        dec, rule, _ = self.decide_audit("write_to_file", {"TargetFile": str(self.base / "esc.txt"), "CodeContent": "x"})
        self.assertEqual((dec, rule), ("deny", "path"))
        dec, rule, _ = self.decide_audit("replace_file_content", {"TargetFile": str(self.base / "secret.txt"), "TargetContent": "s", "ReplacementContent": "r"})
        self.assertEqual((dec, rule), ("deny", "path"))
        dec, rule, _ = self.decide_audit("write_to_file", {"TargetFile": "../esc.txt", "CodeContent": "x"})
        self.assertEqual((dec, rule), ("deny", "path"))

    def test_deep_research_writes_to_dr_dir_denied(self):
        dec, rule, _ = self.decide_audit("write_to_file", {"TargetFile": str(self.dr_dir / "scripts" / "evil.py"), "CodeContent": "x"})
        self.assertEqual((dec, rule), ("deny", "path"))
        dec, rule, _ = self.decide_audit("replace_file_content", {"TargetFile": str(self.dr_dir / "scripts" / "relay_step.py"), "TargetContent": "#", "ReplacementContent": "##"})
        self.assertEqual((dec, rule), ("deny", "path"))

    def test_deep_research_protected_control_files_denied_for_writes(self):
        protected_files = [
            "relay/request.json",
            "prompt.md",
            "goal.md",
            "feedback_1.md",
            "feedback_review.md",
            "relay/result.json",
            "relay/started.json",
            ".agents/hooks.json",
            ".agy_task/log.jsonl",
        ]
        for pf in protected_files:
            with self.subTest(pf=pf):
                dec, rule, _ = self.decide_audit("write_to_file", {"TargetFile": pf, "CodeContent": "{}"})
                self.assertEqual((dec, rule), ("deny", "protected"), f"write {pf}")
                dec, rule, _ = self.decide_audit("write_to_file", {"TargetFile": str(self.root / pf), "CodeContent": "{}"})
                self.assertEqual((dec, rule), ("deny", "protected"), f"write abs {pf}")
                dec, rule, _ = self.decide_audit("replace_file_content", {"TargetFile": str(self.root / pf), "TargetContent": "a", "ReplacementContent": "b"})
                self.assertEqual((dec, rule), ("deny", "protected"), f"replace abs {pf}")

    def test_deep_research_reading_protected_control_files_allowed(self):
        for pf in ["relay/request.json", "prompt.md", "goal.md", "feedback_1.md", "relay/result.json", "relay/started.json"]:
            with self.subTest(pf=pf):
                self.assertEqual(self.decide("view_file", {"AbsolutePath": str(self.root / pf)}, mode="deep-research", dr_dir=self.dr_dir), "ask")
                self.assertEqual(self.decide("view_file", {"AbsolutePath": pf}, mode="deep-research", dr_dir=self.dr_dir), "ask")

    def test_deep_research_clean_relay_step_commands_allowed(self):
        R = str(self.root)
        DR = str(self.dr_dir / "scripts" / "relay_step.py")
        commands = [
            f'python relay_step.py --job "{R}"',
            f'python relay_step.py --job="{R}"',
            f'python.exe relay_step.py --job "{R}"',
            f'python3 relay_step.py --job "{R}"',
            f'py relay_step.py --job "{R}"',
            f'python "{DR}" --job "{R}"',
            f'python relay_step.py --job "{R}" --worker',
            f'python -u relay_step.py --job "{R}"',
            f'{sys.executable} relay_step.py --job "{R}"',
        ]
        for cmd in commands:
            with self.subTest(cmd=cmd):
                self.assertEqual(self.decide("run_command", {"CommandLine": cmd}, mode="deep-research", dr_dir=self.dr_dir), "ask")

    def test_deep_research_clean_patch_gemini_app_commands_allowed(self):
        DR_PATCH = str(self.dr_dir / "scripts" / "patch_gemini_app.py")
        commands = [
            "python patch_gemini_app.py",
            f'python "{DR_PATCH}"',
            "python patch_gemini_app.py --show",
            "python patch_gemini_app.py --restore",
            "python.exe patch_gemini_app.py",
            "py patch_gemini_app.py",
        ]
        for cmd in commands:
            with self.subTest(cmd=cmd):
                self.assertEqual(self.decide("run_command", {"CommandLine": cmd}, mode="deep-research", dr_dir=self.dr_dir), "ask")

    def test_deep_research_shell_chaining_denied(self):
        R = str(self.root)
        chaining_cmds = [
            f'python relay_step.py --job "{R}" && calc.exe',
            f'python relay_step.py --job "{R}" & whoami',
            f'python relay_step.py --job "{R}"; dir',
            f'python relay_step.py --job "{R}" | grep ok',
            f'python relay_step.py --job "{R}" || echo fail',
            f'python relay_step.py `whoami` --job "{R}"',
            f'python relay_step.py $(whoami) --job "{R}"',
            f'python relay_step.py --job "{R}"\ncalc.exe',
            f'python relay_step.py --job "{R}"\r\nwhoami',
        ]
        for cmd in chaining_cmds:
            with self.subTest(cmd=cmd):
                dec, rule, _ = self.decide_audit("run_command", {"CommandLine": cmd})
                self.assertEqual((dec, rule), ("deny", "shell"), cmd)

    def test_deep_research_redirection_denied(self):
        R = str(self.root)
        redir_cmds = [
            f'python relay_step.py --job "{R}" > out.txt',
            f'python relay_step.py --job "{R}" >> out.txt',
            f'python relay_step.py --job "{R}" < in.txt',
            f'python relay_step.py --job "{R}" 2>&1',
        ]
        for cmd in redir_cmds:
            with self.subTest(cmd=cmd):
                dec, rule, _ = self.decide_audit("run_command", {"CommandLine": cmd})
                self.assertEqual((dec, rule), ("deny", "shell"), cmd)

    def test_deep_research_unauthorized_commands_and_scripts_denied(self):
        R = str(self.root)
        B = str(self.base)
        unauth_cmds = [
            "python evil.py",
            f'python other_script.py --job "{R}"',
            f'python relay_step.py --job "{B}"',
            "python relay_step.py",
            f'python "{B}/relay_step.py" --job "{R}"',
            "dir",
            "ls -la",
            "echo hi",
            "powershell -c Get-Process",
            "node index.js",
            "python -c 'import os; os.system(\"calc\")'",
            "python -m http.server",
        ]
        for cmd in unauth_cmds:
            with self.subTest(cmd=cmd):
                dec, rule, _ = self.decide_audit("run_command", {"CommandLine": cmd})
                self.assertEqual((dec, rule), ("deny", "shell"), cmd)

    def test_deep_research_python_flag_injection_denied(self):
        R = str(self.root)
        DR_PATCH = str(self.dr_dir / "scripts" / "patch_gemini_app.py")
        adversarial_cmds = [
            f'python -cprint(1) relay_step.py --job "{R}"',
            f'python -c"import os; os.system(\'calc\')" relay_step.py --job "{R}"',
            f'python -c"print(1)" relay_step.py --job "{R}"',
            f'python -c\'print(1)\' relay_step.py --job "{R}"',
            f'python -c=print(1) relay_step.py --job "{R}"',
            f'python -mhttp.server relay_step.py --job "{R}"',
            f'python -m"http.server" relay_step.py --job "{R}"',
            f'python -m=http.server relay_step.py --job "{R}"',
            f'python - relay_step.py --job "{R}"',
            f'python.exe - relay_step.py --job "{R}"',
            f'python -c"calc" "{DR_PATCH}"',
            f'python -cprint(1) patch_gemini_app.py',
            f'python -mhttp.server patch_gemini_app.py',
            f'python - patch_gemini_app.py',
            f'python -Wignore relay_step.py --job "{R}"',
            f'python -i relay_step.py --job "{R}"',
            f'python -ucprint(1) relay_step.py --job "{R}"',
        ]
        for cmd in adversarial_cmds:
            with self.subTest(cmd=cmd):
                dec, rule, _ = self.decide_audit("run_command", {"CommandLine": cmd})
                self.assertEqual((dec, rule), ("deny", "shell"), cmd)

    def test_deep_research_permitted_python_flags_allowed(self):
        R = str(self.root)
        allowed_cmds = [
            f'python -u relay_step.py --job "{R}"',
            f'python -B relay_step.py --job "{R}"',
            f'python -u -B relay_step.py --job "{R}"',
        ]
        for cmd in allowed_cmds:
            with self.subTest(cmd=cmd):
                dec, _, _ = self.decide_audit("run_command", {"CommandLine": cmd})
                self.assertEqual(dec, "ask", cmd)

    def test_deep_research_ntfs_alternate_data_stream_denied(self):
        protected_files = [
            "prompt.md",
            "goal.md",
            "relay/request.json",
            "relay/result.json",
            "relay/started.json",
            "feedback_1.md",
        ]
        for pf in protected_files:
            stream_variants = [
                f"{pf}::$DATA",
                f"{pf}:stream",
                f"{pf}:stream:$DATA",
                f"{self.root}/{pf}::$DATA",
                f"{self.root}\\{pf}::$DATA",
                f"{self.root}/{pf}:stream",
            ]
            for variant in stream_variants:
                with self.subTest(variant=variant):
                    dec, rule, _ = self.decide_audit("write_to_file", {"TargetFile": variant, "CodeContent": "TAMPERED"})
                    self.assertEqual(dec, "deny", f"write {variant}")
                    dec_r, rule_r, _ = self.decide_audit("replace_file_content", {
                        "TargetFile": variant, "TargetContent": "a", "ReplacementContent": "b"
                    })
                    self.assertEqual(dec_r, "deny", f"replace {variant}")

        dec_out, rule_out, _ = self.decide_audit("view_file", {"AbsolutePath": "../secret.txt::$DATA"})
        self.assertEqual(dec_out, "deny")

    def test_deep_research_browser_mcp_and_terminal_tools_denied(self):
        for tool in ["browser_open_url", "browser_click", "mcp_fs_read", "custom_mcp_call"]:
            with self.subTest(tool=tool):
                dec, rule, _ = self.decide_audit(tool, {})
                self.assertEqual((dec, rule), ("deny", "tool"), tool)
        for tool in ["send_command_input", "read_terminal", "command_status"]:
            with self.subTest(tool=tool):
                dec, rule, _ = self.decide_audit(tool, {})
                self.assertEqual((dec, rule), ("deny", "shell"), tool)


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

    def test_deep_research_dry_run_flags_and_schema(self):
        cp = subprocess.run([sys.executable, str(SCRIPTS / "agy_task.py"), "run", "--deep-research",
                             "-w", str(self.work), "-t", "test task", "--dry-run"],
                            capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        data = json.loads(cp.stdout)
        self.assertEqual(data["status"], "dry-run")
        self.assertTrue(data["confined"])
        self.assertIn("--dangerously-skip-permissions", data["command"])
        self.assertIn("--add-dir", data["command"])
        self.assertIn("You are acting strictly as the relay operator for Gemini Deep Research.", data["prompt"])
        schema = data.get("schema", {})
        props = schema.get("properties", {})
        for expected_prop in ["status", "step", "code", "url", "plan_count", "report_chars", "sources_count", "summary", "notes"]:
            self.assertIn(expected_prop, props)
        self.assertEqual(schema.get("required"), ["status", "step", "summary"])
        self.assertTrue(data.get("deep_research"))

    def test_deep_research_confinement_env_variables(self):
        c = agy_task.Confinement(self.work, self.work / "log.jsonl", mode="deep-research", dr_dir=Path("/opt/dr_skill"))
        env = c.env()
        self.assertEqual(env["AGY_CONFINE_MODE"], "deep-research")
        self.assertEqual(env["AGY_CONFINE_DR_DIR"], str(Path("/opt/dr_skill").resolve()))
        self.assertEqual(env["AGY_CONFINE_ROOT"], str(self.work))
        self.assertEqual(env["AGY_CONFINE_LOG"], str(self.work / "log.jsonl"))

    def test_deep_research_dr_skill_dir_cli_option(self):
        custom_dr = self.work / "custom_dr"
        cp = subprocess.run([sys.executable, str(SCRIPTS / "agy_task.py"), "run", "--deep-research",
                             "--dr-skill-dir", str(custom_dr), "-w", str(self.work), "-t", "test task", "--dry-run"],
                            capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        data = json.loads(cp.stdout)
        self.assertEqual(data.get("dr_skill_dir"), str(custom_dr.resolve()))

    def test_deep_research_result_merging_result_json(self):
        relay_dir = self.work / "relay"
        relay_dir.mkdir(parents=True, exist_ok=True)
        res_data = {"relay_version": 1, "step": "plan", "status": "plan_ready",
                    "url": "https://gemini.google.com/app/1", "plan_count": 3, "summary": "3-stage plan ready"}
        (relay_dir / "result.json").write_text(json.dumps(res_data), encoding="utf-8")

        p = agy_task.build_parser()
        a = p.parse_args(["run", "-w", str(self.work), "-t", "test", "--deep-research", "--no-schema"])
        old_run = agy_task.run_agy
        old_find = agy_task.find_agy
        try:
            agy_task.find_agy = lambda x: sys.executable
            agy_task.run_agy = lambda *args, **kwargs: (0, {"status": "SUCCESS"}, "", [])
            rc = agy_task.cmd_run(a)
            self.assertEqual(rc, 0)
            stamp_dirs = list((self.work / ".agy_task").glob("*/result.json"))
            self.assertTrue(bool(stamp_dirs))
            saved = json.loads(stamp_dirs[0].read_text(encoding="utf-8"))
            self.assertEqual(saved.get("deep_research"), res_data)
        finally:
            agy_task.run_agy = old_run
            agy_task.find_agy = old_find

    def test_deep_research_result_merging_started_json(self):
        relay_dir = self.work / "relay"
        relay_dir.mkdir(parents=True, exist_ok=True)
        (relay_dir / "result.json").unlink(missing_ok=True)
        started_data = {"status": "started", "pid": 9999, "launcher": "run_deep_research.py", "seq": 1}
        (relay_dir / "started.json").write_text(json.dumps(started_data), encoding="utf-8")

        p = agy_task.build_parser()
        a = p.parse_args(["run", "-w", str(self.work), "-t", "test", "--deep-research", "--no-schema"])
        old_run = agy_task.run_agy
        old_find = agy_task.find_agy
        try:
            agy_task.find_agy = lambda x: sys.executable
            agy_task.run_agy = lambda *args, **kwargs: (0, {"status": "SUCCESS"}, "", [])
            rc = agy_task.cmd_run(a)
            self.assertEqual(rc, 0)
            stamp_dirs = list((self.work / ".agy_task").glob("*/result.json"))
            self.assertTrue(bool(stamp_dirs))
            saved = json.loads(stamp_dirs[-1].read_text(encoding="utf-8"))
            self.assertEqual(saved.get("deep_research"), started_data)
        finally:
            agy_task.run_agy = old_run
            agy_task.find_agy = old_find

    def test_deep_research_check_confine_args(self):
        p = agy_task.build_parser()
        a = p.parse_args(["run", "-t", "x", "--deep-research"])
        agy_task.check_confine_args(a)
        self.assertTrue(a.confine)
        self.assertTrue(a.deep_research)

        a_bad = p.parse_args(["run", "-t", "x", "--deep-research", "--no-confine"])
        with self.assertRaises(SystemExit) as cm:
            agy_task.check_confine_args(a_bad)
        self.assertEqual(cm.exception.code, 5)

    def test_deep_research_disallows_allow_all(self):
        p = agy_task.build_parser()
        # Direct CLI argument check on run subcommand
        a_dr_allow = p.parse_args(["run", "-t", "x", "--deep-research", "--allow-all"])
        with self.assertRaises(SystemExit) as cm:
            agy_task.check_confine_args(a_dr_allow)
        self.assertEqual(cm.exception.code, 5)

        # Combination of all three flags
        a_dr_no_confine_allow = p.parse_args(["run", "-t", "x", "--deep-research", "--allow-all", "--no-confine"])
        with self.assertRaises(SystemExit) as cm:
            agy_task.check_confine_args(a_dr_no_confine_allow)
        self.assertEqual(cm.exception.code, 5)

        # Direct CLI argument check on resume subcommand
        a_resume = p.parse_args(["resume", "cid123", "-t", "x", "--deep-research", "--allow-all"])
        with self.assertRaises(SystemExit) as cm:
            agy_task.check_confine_args(a_resume)
        self.assertEqual(cm.exception.code, 5)

        # Subprocess execution in dry-run mode
        cp = subprocess.run([sys.executable, str(SCRIPTS / "agy_task.py"), "run", "--deep-research", "--allow-all",
                             "-w", str(self.work), "-t", "test task", "--dry-run"],
                            capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(cp.returncode, 5, f"Expected returncode 5, got {cp.returncode}: {cp.stdout} {cp.stderr}")
        self.assertIn("cannot be combined with --deep-research", cp.stderr or cp.stdout)


SITE = "https://app.example.org"
CD = "mcp_chrome_devtools_"


class HookBrowser(unittest.TestCase):
    """Opt-in browser allowlist: agy's Chrome DevTools MCP tools, limited to listed origins, no text entry."""

    @classmethod
    def setUpClass(cls):
        cls.base = Path(tempfile.mkdtemp(prefix="cfb_"))
        cls.root = cls.base / "work"
        (cls.root / ".agents").mkdir(parents=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.base, True)

    def decide(self, name, args, origins=(SITE,), mode=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGY_CONFINE_")}
        env["AGY_CONFINE_ROOT"] = str(self.root)
        env["AGY_CONFINE_LOG"] = str(self.base / "log.jsonl")
        if origins is not None:
            env["AGY_CONFINE_BROWSER_ORIGINS"] = json.dumps(list(origins))
        if mode:
            env["AGY_CONFINE_MODE"] = mode
        data = json.dumps({"toolCall": {"name": name, "args": args}}).encode()
        cp = subprocess.run([sys.executable, str(HOOK)], input=data, capture_output=True,
                            cwd=str(self.root / ".agents"), env=env)
        return json.loads(cp.stdout)["decision"]

    def test_browser_stays_denied_without_opt_in(self):
        for origins in (None, ()):
            self.assertEqual(self.decide(CD + "navigate_page", {"url": SITE}, origins=origins), "deny")
            self.assertEqual(self.decide(CD + "click", {"uid": "1_2"}, origins=origins), "deny")

    def test_navigation_only_to_listed_origins(self):
        for args, want in [
            ({"url": SITE + "/"}, "ask"),
            ({"url": SITE + "/explore?q=1#x"}, "ask"),
            ({"type": "url", "url": "HTTPS://APP.EXAMPLE.ORG/places/1"}, "ask"),
            ({"type": "back"}, "ask"),
            ({"type": "reload", "ignoreCache": True}, "ask"),
            ({"url": "https://evil.example.org/"}, "deny"),
            ({"url": "https://app.example.org.evil.net/"}, "deny"),
            ({"url": "https://user:pw@app.example.org/"}, "deny"),
            ({"url": "https://evil.net@app.example.org/"}, "deny"),
            ({"url": "http://app.example.org/"}, "deny"),
            ({"url": "https://app.example.org:8443/"}, "deny"),
            ({"url": "file:///C:/Users/x/.env"}, "deny"),
            ({"url": "chrome://settings"}, "deny"),
            ({"url": "javascript:alert(1)"}, "deny"),
            ({"type": "url"}, "deny"),
            ({"type": "teleport"}, "deny"),
            ({"url": SITE, "initScript": "fetch('/api/chat')"}, "deny"),
        ]:
            with self.subTest(args=args):
                self.assertEqual(self.decide(CD + "navigate_page", args), want)
        self.assertEqual(self.decide(CD + "new_page", {"url": SITE + "/festivals"}), "ask")
        self.assertEqual(self.decide(CD + "new_page", {"url": "https://other.example.com"}), "deny")
        self.assertEqual(self.decide(CD + "new_page", {}), "deny")

    def test_reading_and_pointing_tools_are_allowed(self):
        for tool, args in [("take_snapshot", {}), ("take_screenshot", {"fullPage": True}), ("click", {"uid": "3_7"}),
                           ("hover", {"uid": "3_7"}), ("drag", {"from_uid": "1", "to_uid": "2"}),
                           ("emulate", {"viewport": {"width": 390, "height": 844, "isMobile": True}}),
                           ("resize_page", {"width": 1440, "height": 900}), ("wait_for", {"text": "진주"}),
                           ("list_console_messages", {}), ("list_network_requests", {}), ("list_pages", {}),
                           ("select_page", {"pageId": 2}), ("handle_dialog", {"action": "dismiss"}),
                           ("get_css_styles", {"uid": "1"}),
                           ("lighthouse_audit", {"mode": "snapshot", "outputDirPath": str(self.root / "lh")}),
                           ("screencast_start", {"filePath": str(self.root / "rec" / "a.webm")})]:
            with self.subTest(tool=tool):
                self.assertEqual(self.decide(CD + tool, args), "ask")

    def test_every_path_like_argument_is_confined(self):
        R = str(self.root)
        for args, want in [
            ({"reqid": 3, "responseFilePath": str(self.root / "net" / "resp.json")}, "ask"),
            ({"reqid": 3, "responseFilePath": "net/resp.json"}, "deny"),
            ({"reqid": 3, "requestFilePath": str(self.base / "req.json")}, "deny"),
            ({"reqid": 3, "responseFilePath": ".agents/hooks.json"}, "deny"),
            ({"reqid": 3, "responseFilePath": R + os.sep + ".agy_task" + os.sep + "x.json"}, "deny"),
            ({"reqid": 3, "someNewOutputPath": str(self.base / "x")}, "deny"),
            ({"reqid": 3, "dest": "C:\\Windows\\x" if WIN else "/etc/x"}, "deny"),
        ]:
            with self.subTest(args=args):
                self.assertEqual(self.decide(CD + "get_network_request", args), want)

    def test_a_hard_linked_target_is_denied(self):
        outside = self.base / "outside.png"
        outside.write_bytes(b"x")
        link = self.root / "link.png"
        try:
            os.link(outside, link)
        except OSError:
            self.skipTest("hard links unsupported")
        self.addCleanup(link.unlink)
        self.assertEqual(self.decide(CD + "take_screenshot", {"filePath": str(link)}), "deny")

    def test_dialog_text_and_request_header_injection_are_denied(self):
        self.assertEqual(self.decide(CD + "handle_dialog", {"action": "accept", "promptText": "hi"}), "deny")
        self.assertEqual(self.decide(CD + "handle_dialog", {"action": "accept"}), "ask")
        self.assertEqual(self.decide(CD + "emulate", {"userAgent": "x"}), "deny")
        self.assertEqual(self.decide(CD + "emulate", {"extraHttpHeaders": {"X-A": "1"}}), "deny")
        self.assertEqual(self.decide(CD + "emulate", {"colorScheme": "dark", "cpuThrottlingRate": 2}), "ask")

    def test_scheme_relative_paths_are_denied_for_browser_writers(self):
        for tool, args in [("take_screenshot", {"filePath": "http://../../../x.png"}),
                           ("take_screenshot", {"filePath": "zz://../link.png"}),
                           ("take_snapshot", {"filePath": "zz://../.agents/hooks.json"}),
                           ("lighthouse_audit", {"outputDirPath": "zz://../../lh"}),
                           ("screencast_start", {"filePath": "zz://../../cast.webm"}),
                           ("get_network_request", {"reqid": 1, "responseFilePath": "zz://../../resp"})]:
            with self.subTest(tool=tool, args=args):
                self.assertEqual(self.decide(CD + tool, args), "deny")

    def test_native_browser_tools_stay_denied_in_browser_mode(self):
        for name in ("open_browser_url", "execute_browser_javascript", "read_browser_page", "capture_browser_screenshot"):
            with self.subTest(name=name):
                self.assertEqual(self.decide(name, {"Url": SITE}), "deny")

    def test_port_zero_never_matches(self):
        self.assertEqual(self.decide(CD + "navigate_page", {"url": SITE + ":0/"}), "deny")

    def test_browser_cannot_write_into_config_folders(self):
        for rel in (".agent/hooks.json", "sub/.agents/x.json", ".git/x", "_agents/mcp_config.json"):
            target = os.path.join(str(self.root), *rel.split("/"))
            with self.subTest(rel=rel):
                self.assertEqual(self.decide(CD + "get_network_request", {"pageId": 1, "reqid": 7, "responseFilePath": target}), "deny")
                self.assertEqual(self.decide(CD + "take_snapshot", {"pageId": 1, "filePath": target}), "deny")

    def test_blank_optional_paths_are_denied(self):
        for v in ("   ", "", "	"):
            with self.subTest(v=repr(v)):
                self.assertEqual(self.decide(CD + "take_screenshot", {"pageId": 1, "filePath": v}), "deny")

    def test_geolocation_is_not_a_path(self):
        self.assertEqual(self.decide(CD + "emulate", {"geolocation": "37.5665x126.9780"}), "ask")

    def test_browser_subagent_allowed_in_browser_mode(self):
        self.assertEqual(self.decide("invoke_subagent", {"Prompt": "open the site", "TypeName": "browser"}), "ask")

    def test_case_variant_duplicate_keys_are_denied(self):
        for tool, args in [("navigate_page", {"url": SITE, "initScript": "", "INITSCRIPT": "fetch('/x')"}),
                           ("handle_dialog", {"action": "accept", "promptText": "", "PromptText": "hi"}),
                           ("emulate", {"userAgent": "", "UserAgent": "x"}),
                           ("navigate_page", {"url": SITE, "URL": "https://evil.example"})]:
            with self.subTest(args=args):
                self.assertEqual(self.decide(CD + tool, args), "deny")

    def test_obfuscated_hosts_never_match(self):
        for url in ("https://ａｐｐ.example.org/", "https://app%2eexample.org/", "https://app.example.org./",
                    "https://app.example.org../"):
            with self.subTest(url=url):
                self.assertEqual(self.decide(CD + "navigate_page", {"url": url}), "deny")
        for origins in (["http://１２７.０.０.１:9222"], ["http://local%68ost:9222"]):
            with self.subTest(origins=origins):
                self.assertEqual(self.decide(CD + "new_page", {"url": origins[0] + "/json"}, origins=origins), "deny")

    def test_writers_must_name_a_path_inside_the_root(self):
        self.assertEqual(self.decide(CD + "lighthouse_audit", {"mode": "snapshot"}), "deny")
        self.assertEqual(self.decide(CD + "screencast_start", {}), "deny")

    def test_args_that_are_not_an_object_are_denied(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGY_CONFINE_")}
        env.update(AGY_CONFINE_ROOT=str(self.root), AGY_CONFINE_BROWSER_ORIGINS=json.dumps([SITE]))
        for args in (["https://evil.example"], "https://evil.example", 7):
            with self.subTest(args=args):
                data = json.dumps({"toolCall": {"name": CD + "navigate_page", "args": args}}).encode()
                cp = subprocess.run([sys.executable, str(HOOK)], input=data, capture_output=True,
                                    cwd=str(self.root / ".agents"), env=env)
                self.assertEqual(json.loads(cp.stdout)["decision"], "deny")

    def test_text_entry_scripts_uploads_and_unknown_tools_are_denied(self):
        for tool, args in [("fill", {"uid": "1", "value": "hi"}), ("fill_form", {"elements": []}),
                           ("type_text", {"text": "hello"}), ("evaluate_script", {"function": "() => 1"}),
                           ("upload_file", {"uid": "1", "filePaths": ["in.txt"]}), ("take_heapsnapshot", {}),
                           ("close_page", {"pageId": 1}), ("brand_new_tool", {})]:
            with self.subTest(tool=tool):
                self.assertEqual(self.decide(CD + tool, args), "deny")

    def test_press_key_only_navigation_keys(self):
        for key, want in [("Tab", "ask"), ("Shift+Tab", "ask"), ("Escape", "ask"), ("Enter", "ask"),
                          ("ArrowDown", "ask"), ("PageDown", "ask"), ("End", "ask"),
                          ("a", "deny"), ("Control+V", "deny"), ("Control+A", "deny"), ("", "deny")]:
            with self.subTest(key=key):
                self.assertEqual(self.decide(CD + "press_key", {"key": key}), want)

    def test_files_written_by_the_browser_stay_inside_the_root(self):
        R = str(self.root)
        for tool, args, want in [
            ("take_screenshot", {"filePath": "shots/home.png"}, "deny"),
            ("take_screenshot", {"filePath": R + os.sep + "shots" + os.sep + "home.png"}, "ask"),
            ("take_screenshot", {"filePath": R + os.sep + "a.png"}, "ask"),
            ("take_screenshot", {"filePath": str(self.base / "out.png")}, "deny"),
            ("take_screenshot", {"filePath": "../out.png"}, "deny"),
            ("take_screenshot", {"filePath": ".agents/hooks.json"}, "deny"),
            ("lighthouse_audit", {"outputDirPath": "lh"}, "deny"),
            ("lighthouse_audit", {"outputDirPath": R + os.sep + "lh"}, "ask"),
            ("lighthouse_audit", {"outputDirPath": "   "}, "deny"),
            ("lighthouse_audit", {"outputDirPath": "."}, "deny"),
            ("lighthouse_audit", {"outputDirPath": str(self.base)}, "deny"),
            ("performance_start_trace", {"filePath": str(self.base / "t.json")}, "deny"),
        ]:
            with self.subTest(tool=tool, args=args):
                self.assertEqual(self.decide(CD + tool, args), want)

    def test_other_mcp_and_browser_tools_stay_denied(self):
        for name in ("mcp_fs_read", "browser_open_url", "mcp_github_create_issue", "browser_subagent"):
            with self.subTest(name=name):
                self.assertEqual(self.decide(name, {"url": SITE}), "deny")

    def test_not_in_deep_research_mode(self):
        self.assertEqual(self.decide(CD + "navigate_page", {"url": SITE}, mode="deep-research"), "deny")

    def test_garbage_origins_allow_nothing(self):
        for origins in (["not a url"], ["file:///C:/"], ["https://"], ["javascript:x"]):
            with self.subTest(origins=origins):
                self.assertEqual(self.decide(CD + "navigate_page", {"url": SITE}, origins=origins), "deny")


class BrowserFlag(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="cfbf_"))
        self.addCleanup(shutil.rmtree, self.work, True)
        self.chrome = self.work / "chrome-user-data"
        self.chrome.mkdir()
        self.env = dict(os.environ, AGY_CHROME_USER_DATA_DIRS=str(self.chrome))

    def test_flag_parses_and_reaches_the_hook_env(self):
        p = agy_task.build_parser()
        a = p.parse_args(["run", "-t", "x", "--allow-browser-origin", SITE, "--allow-browser-origin", SITE + "/"])
        agy_task.check_confine_args(a)
        c = agy_task.Confinement(self.work, self.work / "l", browser_origins=a.allow_browser_origin)
        self.assertEqual(json.loads(c.env()["AGY_CONFINE_BROWSER_ORIGINS"]), [SITE])
        self.assertNotIn("AGY_CONFINE_BROWSER_ORIGINS", agy_task.Confinement(self.work, self.work / "l").env())
        r = p.parse_args(["resume", "cid", "-t", "x", "--allow-browser-origin", SITE])
        self.assertEqual(r.allow_browser_origin, [SITE])

    def test_flag_needs_confinement_and_no_deep_research(self):
        p = agy_task.build_parser()
        for argv in (["run", "-t", "x", "--allow-browser-origin", SITE, "--no-confine"],
                     ["run", "-t", "x", "--allow-browser-origin", SITE, "--deep-research"],
                     ["run", "-t", "x", "--allow-browser-origin", "file:///C:/"],
                     ["run", "-t", "x", "--allow-browser-origin", "https://user:pw@app.example.org"],
                     ["run", "-t", "x", "--allow-browser-origin", "not a url"]):
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit) as cm:
                    agy_task.check_confine_args(p.parse_args(argv))
                self.assertEqual(cm.exception.code, 5)

    def test_dry_run_reports_origins_and_prompt_rule(self):
        cp = subprocess.run([sys.executable, str(SCRIPTS / "agy_task.py"), "--agy-bin", "agy", "run", "-w", str(self.work),
                             "-t", "TASK", "--allow-browser-origin", SITE, "--dry-run"],
                            capture_output=True, text=True, encoding="utf-8", env=self.env)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        data = json.loads(cp.stdout)
        self.assertEqual(data["browser_origins"], [SITE])
        self.assertIn(SITE, data["prompt"])
        self.assertIn("/browser", data["prompt"])

    def dirs(self):
        from unittest import mock
        return mock.patch.object(agy_task, "chrome_user_data_dirs", lambda: [self.chrome])

    def listener(self, family=None):
        import socket
        fam = family or socket.AF_INET
        srv = socket.socket(fam)
        srv.bind(("::1" if fam == socket.AF_INET6 else "127.0.0.1", 0))
        srv.listen(16)  # probes are never accepted; leave room in the backlog
        self.addCleanup(srv.close)
        return srv.getsockname()[1]

    def browser_args(self):
        a = agy_task.build_parser().parse_args(["run", "-t", "x", "--allow-browser-origin", SITE])
        agy_task.check_confine_args(a)
        return a

    def test_refuses_while_the_users_chrome_exposes_devtools(self):
        port = self.listener()
        (self.chrome / "DevToolsActivePort").write_text(f"{port}\n/devtools/browser/x\n")
        with self.dirs():
            self.assertEqual([p for _, p in agy_task.live_chrome_devtools()], [port])
            with self.assertRaises(SystemExit) as cm:
                agy_task.refuse_browser_on_users_chrome(self.browser_args())
            self.assertEqual(cm.exception.code, 5)
        cp = subprocess.run([sys.executable, str(SCRIPTS / "agy_task.py"), "--agy-bin", sys.executable, "run", "-w",
                             str(self.work), "-t", "TASK", "--allow-browser-origin", SITE],
                            capture_output=True, text=True, encoding="utf-8", env=self.env)
        self.assertEqual(cp.returncode, 5, cp.stdout + cp.stderr)
        self.assertIn("remote debugging", cp.stderr)

    def test_an_ipv6_only_listener_is_detected(self):
        import socket
        if not socket.has_ipv6:
            self.skipTest("no IPv6")
        try:
            port = self.listener(socket.AF_INET6)
        except OSError:
            self.skipTest("no ::1")
        (self.chrome / "DevToolsActivePort").write_text(f"{port}\n/devtools/browser/x\n")
        with self.dirs():
            self.assertEqual([p for _, p in agy_task.live_chrome_devtools()], [port])

    def test_a_stale_devtools_file_does_not_block(self):
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()  # nothing listens on it now
        (self.chrome / "DevToolsActivePort").write_text(f"{port}\n/devtools/browser/x\n")
        with self.dirs():
            self.assertEqual(agy_task.live_chrome_devtools(), [])

    def test_an_unreadable_port_file_fails_closed_and_lenient_parsing_matches(self):
        port = self.listener()
        bom = chr(0xFEFF)
        (self.chrome / "DevToolsActivePort").write_text(f"{bom} {port}junk\n/devtools/browser/x\n", encoding="utf-8")
        with self.dirs():
            self.assertEqual([p for _, p in agy_task.live_chrome_devtools()], [port])
        (self.chrome / "DevToolsActivePort").write_text("garbled\n")
        with self.dirs(), self.assertRaises(SystemExit) as cm:
            agy_task.refuse_browser_on_users_chrome(self.browser_args())
        self.assertEqual(cm.exception.code, 5)

    def test_agy_browser_endpoint_overrides_are_refused(self):
        from unittest import mock
        for var in ("AGY_BROWSER_WS_URL", "AGY_BROWSER_ACTIVE_PORT_FILE", "AGY_BROWSER_ANYTHING", "agy_browser_ws_url",
                    "CHROME_DEVTOOLS_MCP_JS", "JETSKI_EVAL_CHROME_DEVTOOLS_MCP_COMMAND",
                    "ANTIGRAVITY_NODE_PATH", "ANTIGRAVITY_LS_ADDRESS", "ANTIGRAVITY_GEMINI_DIR"):
            with self.subTest(var=var), self.dirs(), mock.patch.dict(os.environ, {var: "x"}):
                with self.assertRaises(SystemExit) as cm:
                    agy_task.refuse_browser_on_users_chrome(self.browser_args())
                self.assertEqual(cm.exception.code, 5)

    def test_user_data_dir_override_only_adds(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {"AGY_CHROME_USER_DATA_DIRS": ""}):
            defaults = agy_task.chrome_user_data_dirs()
        self.assertTrue(defaults)
        with mock.patch.dict(os.environ, {"AGY_CHROME_USER_DATA_DIRS": str(self.chrome)}):
            got = agy_task.chrome_user_data_dirs()
        self.assertIn(self.chrome, got)
        for d in defaults:
            self.assertIn(d, got)

    def test_child_env_drops_inherited_confinement_settings(self):
        from unittest import mock
        leaked = {"AGY_CONFINE_BROWSER_ORIGINS": json.dumps(["https://evil.example"]), "AGY_CONFINE_MODE": "deep-research",
                  "AGY_CONFINE_DR_DIR": "C:\\", "AGY_CONFINE_ROOT": "C:\\"}
        with mock.patch.dict(os.environ, leaked):
            env = agy_task.child_env({"AGY_CONFINE_ROOT": str(self.work)})
            bare = agy_task.child_env({})
        self.assertEqual(env["AGY_CONFINE_ROOT"], str(self.work))
        for k in ("AGY_CONFINE_BROWSER_ORIGINS", "AGY_CONFINE_MODE", "AGY_CONFINE_DR_DIR"):
            self.assertNotIn(k, env)
            self.assertNotIn(k, bare)
        self.assertNotIn("AGY_CONFINE_ROOT", bare)

    def test_origins_that_resolve_to_non_public_addresses_are_refused(self):
        import socket
        from unittest import mock
        a = self.browser_args()

        def fake(addrs):
            return lambda host, *args, **kw: [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM,
                                               0, "", (ip, 0)) for ip in addrs]

        for addrs in (["127.0.0.1"], ["93.184.216.34", "10.0.0.7"], ["169.254.169.254"],
                      ["::ffff:168.63.129.16"], ["64:ff9b::7f00:1"], ["2002:7f00:1::1"]):
            with self.subTest(addrs=addrs), self.dirs(), mock.patch.object(socket, "getaddrinfo", fake(addrs)):
                with self.assertRaises(SystemExit) as cm:
                    agy_task.refuse_browser_on_users_chrome(a)
                self.assertEqual(cm.exception.code, 5)

        def fail(*args, **kw):
            raise socket.gaierror("no such host")
        with self.dirs(), mock.patch.object(socket, "getaddrinfo", fail), self.assertRaises(SystemExit):
            agy_task.refuse_browser_on_users_chrome(a)
        with self.dirs(), mock.patch.object(socket, "getaddrinfo", fake(["93.184.216.34"])):
            agy_task.refuse_browser_on_users_chrome(a)  # public: no exit

    def test_public_origins_are_accepted(self):
        p = agy_task.build_parser()
        for o, want in [("https://jinju-hub.netlify.app", "https://jinju-hub.netlify.app"),
                        ("https://cafe.de/", "https://cafe.de"), ("https://dead.beef.example:8443", "https://dead.beef.example:8443"),
                        ("http://8.8.8.8", "http://8.8.8.8"), ("HTTPS://Example.ORG", "https://example.org")]:
            with self.subTest(origin=o):
                a = p.parse_args(["run", "-t", "x", "--allow-browser-origin", o])
                agy_task.check_confine_args(a)
                self.assertEqual(a.allow_browser_origin, [want])

    def test_local_and_private_origins_are_refused(self):
        p = agy_task.build_parser()
        for o in ("http://localhost:5173", "http://app.localhost", "http://127.0.0.1:9222", "http://[::1]:8080",
                  "http://10.0.0.5", "http://192.168.0.2", "http://169.254.169.254", "http://0.0.0.0",
                  "http://2130706433", "http://0x7f.1", "http://127.1",
                  "http://１２７.０.０.１:9222", "http://①②⑦.0.0.1", "http://local%68ost:9222", "http://100.64.0.1",
                  "http://[2001:db8::1]", "http://intranet", "http://printer.local", "http://nas.lan",
                  "http://svc.internal", "http://x.home.arpa", "https://app.example.org.", "https://app..example.org",
                  "https://ａｐｐ.example.org", "https://app.example.org:0", "http://168.63.129.16", "http://224.0.0.1"):
            with self.subTest(origin=o):
                with self.assertRaises(SystemExit) as cm:
                    agy_task.check_confine_args(p.parse_args(["run", "-t", "x", "--allow-browser-origin", o]))
                self.assertEqual(cm.exception.code, 5)

if __name__ == "__main__":
    unittest.main()


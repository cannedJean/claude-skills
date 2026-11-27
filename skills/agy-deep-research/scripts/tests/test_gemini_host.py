#!/usr/bin/env python3
"""test_gemini_host.py - pure host decisions: launch command guard, instance identity, launch classification, mode choice."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gemini_host as gh

STUB = r"C:\Users\Me\AppData\Local\Google\Gemini\Gemini.exe"
APP = r"C:\Users\Me\AppData\Local\Google\Gemini\app-1.2.3\Gemini.exe"
PROFILE = r"C:\Users\Me\AppData\Local\agy-deep-research\gemini-profile"


class LaunchCommandTests(unittest.TestCase):
    def test_build_command_rejects_unlisted_switch(self):
        with self.assertRaises(ValueError):
            gh.build_launch_command(STUB, PROFILE, ["--remote-debugging-port=9222"])
        with self.assertRaises(ValueError):
            gh.build_launch_command(STUB, PROFILE, ["--user-data-dir=C:\\x"])
        ok = gh.build_launch_command(STUB, PROFILE, list(gh.ALLOWED_FLAG_SWITCHES))
        for f in gh.ALLOWED_FLAG_SWITCHES:
            self.assertIn(f, ok)

    def test_build_command_d1_has_no_switch(self):
        cmd = gh.build_launch_command(STUB, None, [])
        self.assertNotIn("--user-data-dir", cmd)
        self.assertEqual(cmd, STUB)
        d2 = gh.build_launch_command(r"C:\Program Files\G\Gemini.exe", r"C:\a b\prof", [])
        self.assertTrue(d2.startswith('"C:\\Program Files\\G\\Gemini.exe"'))
        self.assertEqual(gh.parse_user_data_dir(d2), r"C:\a b\prof")


class HardeningTests(unittest.TestCase):
    def test_d1_rejects_flags(self):
        with self.assertRaises(ValueError):
            gh.build_launch_command(STUB, None, [gh.ALLOWED_FLAG_SWITCHES[0]])

    def test_profile_dir_validation(self):
        bad = ["", "rel\\dir", "\\rootless", 'C:\\a"b', "C:\\a\nb", "C:\\a\x00b", "C:\\a\tb",
               r"C:\Users\Me\AppData\Local\Google\Gemini\profile", r"c:\x\GOOGLE\gemini",
               r"C:\x\Google\Gemini\..\..\Google\Gemini\p"]
        for b in bad:
            with self.subTest(b=b), self.assertRaises(ValueError):
                gh.build_launch_command(STUB, b, [])
        gh.build_launch_command(STUB, r"\\srv\share\prof", [])

    def test_round_trip(self):
        flags = list(gh.ALLOWED_FLAG_SWITCHES)
        for path in (r"C:\a b\prof", "C:\\a b\\prof\\", r"C:\plain\prof", "C:\\plain\\prof\\"):
            with self.subTest(path=path):
                cmd = gh.build_launch_command(STUB, path, flags)
                self.assertEqual(gh.parse_user_data_dir(cmd), path)

    def test_decide_validates_d2(self):
        with self.assertRaises(ValueError):
            gh.decide_mode(None, "pending", False)
        with self.assertRaises(ValueError):
            gh.decide_mode("D2", None, False)

    def test_pinned_d1_blocked_without_user_process(self):
        self.assertEqual(gh.decide_mode("D1", "blocked", False).mode, "D1")

    def test_parse_edge_cases(self):
        p = gh.parse_user_data_dir
        self.assertEqual(p(r"x --user-data-dir=C:\a --user-data-dir=C:\b"), r"C:\b")
        self.assertIsNone(p(r"x --user-data-dir C:\a"))
        self.assertIsNone(p("x --user-data-dir="))
        self.assertIsNone(p('x --user-data-dir=""'))
        self.assertEqual(p(r"x --USER-DATA-DIR=C:\a"), r"C:\a")

    def test_lookalike_images(self):
        for img in (r"C:\u\Google\Gemini\app-1\..\..\evil\Gemini.exe",
                    r"C:\u\Google\Gemini\app-1\Gemini.exe.bak",
                    r"C:\u\Google\Gemini\app-1\Gemini.exe.",
                    ""):
            with self.subTest(img=img):
                self.assertFalse(gh.is_gemini_image(img))

    def test_exited_precedence(self):
        self.assertEqual(gh.classify_launch(gh.LaunchCheck(True, True, True, True, 5.0)), "blocked")

    def test_clean_env_case_insensitive(self):
        self.assertEqual(gh.clean_env({"node_options": "x", "Electron_X": "1", "Path": "p"}), {"Path": "p"})


class ParseTests(unittest.TestCase):
    def test_parse_user_data_dir_forms(self):
        p = gh.parse_user_data_dir
        self.assertEqual(p(r'x.exe --user-data-dir=C:\a\b --foo'), r"C:\a\b")
        self.assertEqual(p(r'x.exe --user-data-dir="C:\a b\c" --foo'), r"C:\a b\c")
        self.assertEqual(p(r'x.exe "--user-data-dir=C:\a b\c" --foo'), r"C:\a b\c")
        self.assertEqual(p(r'"C:\x y\g.exe" --type=renderer --user-data-dir=C:\d'), r"C:\d")
        self.assertIsNone(p(r'x.exe --type=gpu'))
        self.assertIsNone(p(''))


class IdentityTests(unittest.TestCase):
    def test_is_gemini_image(self):
        self.assertTrue(gh.is_gemini_image(APP))
        self.assertTrue(gh.is_gemini_image(APP.lower().replace("\\", "/")))
        self.assertTrue(gh.is_gemini_image(r"D:\Other\Google\GEMINI\App-9.0\GEMINI.EXE"))
        self.assertFalse(gh.is_gemini_image(STUB))
        self.assertFalse(gh.is_gemini_image(r"C:\Google\Gemini\app-1\Other.exe"))
        self.assertFalse(gh.is_gemini_image(r"C:\Chrome\app-1\Gemini.exe"))

    def test_user_app_with_other_profile_is_not_ours(self):
        mine = gh.ProcInfo(10, 1, APP, f'"{APP}" --user-data-dir="{PROFILE.upper()}\\\\"', 100.0)
        mine2 = gh.ProcInfo(10, 1, APP, f'"{APP}" --user-data-dir={PROFILE}', 100.0)
        user = gh.ProcInfo(11, 1, APP, f'"{APP}"', 100.0)
        other = gh.ProcInfo(12, 1, APP, f'"{APP}" --user-data-dir=C:\\elsewhere', 100.0)
        notgem = gh.ProcInfo(13, 1, r"C:\x\chrome.exe", f'x --user-data-dir={PROFILE}', 100.0)
        self.assertTrue(gh.is_our_instance(mine, PROFILE))
        self.assertTrue(gh.is_our_instance(mine2, PROFILE))
        self.assertFalse(gh.is_our_instance(user, PROFILE))
        self.assertFalse(gh.is_our_instance(other, PROFILE))
        self.assertFalse(gh.is_our_instance(notgem, PROFILE))

    def test_pid_reuse_rejected_by_creation_time(self):
        p = gh.ProcInfo(10, 1, APP, f'"{APP}" --user-data-dir={PROFILE}', 50.0)
        self.assertTrue(gh.is_our_instance(p, PROFILE))
        self.assertTrue(gh.is_our_instance(p, PROFILE, started_after=50.0))
        self.assertFalse(gh.is_our_instance(p, PROFILE, started_after=50.5))


class ClassifyTests(unittest.TestCase):
    def test_classify_slow_start_is_pending_not_blocked(self):
        c = gh.LaunchCheck(False, False, False, False, 20.0)
        self.assertEqual(gh.classify_launch(c), "pending")
        self.assertEqual(gh.classify_launch(gh.LaunchCheck(True, True, False, False, 30.0)), "pending")
        self.assertEqual(gh.classify_launch(gh.LaunchCheck(False, False, False, False, 60.0)), "timeout")
        self.assertEqual(gh.classify_launch(gh.LaunchCheck(True, True, True, False, 5.0)), "ok")
        self.assertEqual(gh.classify_launch(gh.LaunchCheck(False, False, False, True, 5.0)), "blocked")
        self.assertEqual(gh.classify_launch(gh.LaunchCheck(True, False, False, False, 5.0)), "blocked")


class DecideTests(unittest.TestCase):
    def test_decide_blocked_falls_back_to_d1_only_without_user_app(self):
        d = gh.decide_mode(None, "blocked", False)
        self.assertEqual((d.mode, d.reason), ("D1", "profile_switch_ignored"))
        d = gh.decide_mode(None, "blocked", True)
        self.assertEqual((d.mode, d.code), (None, "GEMINI_IN_USE"))
        self.assertEqual(gh.decide_mode(None, "ok", True).mode, "D2")
        d = gh.decide_mode(None, "timeout", False)
        self.assertEqual((d.mode, d.code), (None, "HOST_START_TIMEOUT"))

    def test_decide_pinned_job_never_switches_mode(self):
        d = gh.decide_mode("D2", "blocked", False)
        self.assertEqual((d.mode, d.code), (None, "HOST_MODE_UNAVAILABLE"))
        d = gh.decide_mode("D2", "timeout", False)
        self.assertEqual((d.mode, d.code), (None, "HOST_START_TIMEOUT"))
        self.assertEqual(gh.decide_mode("D2", "ok", True).mode, "D2")
        self.assertEqual(gh.decide_mode("D1", "ok", False).mode, "D1")
        self.assertEqual(gh.decide_mode("D1", "blocked", True).code, "GEMINI_IN_USE")
        with self.assertRaises(ValueError):
            gh.decide_mode("D3", "ok", False)


class EnvTests(unittest.TestCase):
    def test_clean_env_drops_node_electron(self):
        env = {"PATH": "p", "NODE_OPTIONS": "x", "ELECTRON_RUN_AS_NODE": "1", "NODEJS": "keep", "A": "b"}
        self.assertEqual(gh.clean_env(env), {"PATH": "p", "NODEJS": "keep", "A": "b"})


if __name__ == "__main__":
    unittest.main()

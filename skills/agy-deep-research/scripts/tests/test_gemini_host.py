#!/usr/bin/env python3
"""test_gemini_host.py - pure host decisions: launch command guard, instance identity, launch classification, mode choice."""
from __future__ import annotations

import sandbox  # noqa: F401  (must stay the first import: blocks real-app access)
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


class ReviewFixTests(unittest.TestCase):
    """B11, B12, T9, T13, M15 (cross-review fixes)."""

    def test_constants_pinned(self):
        self.assertEqual(gh.DESKTOP_NAME, "agy-deep-research")
        self.assertEqual(gh.ALLOWED_FLAG_SWITCHES, (
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-features=CalculateNativeWinOcclusion",
        ))

    def test_profile_dir_trailing_dot_and_space_components(self):
        for b in (r"C:\x\Google\Gemini.\prof", r"C:\x\Google\Gemini \prof", r"C:\x\Google.\Gemini\prof",
                  r"C:\x\Google \Gemini. .\prof", r"C:\x\Google\Gemini.", r"C:\x\GOOGLE \gemini"):
            with self.subTest(b=b), self.assertRaises(ValueError):
                gh.build_launch_command(STUB, b, [])
        gh.build_launch_command(STUB, r"C:\x\Google\Gemini2\prof", [])
        gh.build_launch_command(STUB, r"C:\x\Google.Gemini\prof", [])

    def test_profile_dir_rejects_colon_after_drive(self):
        for b in (r"C:\prof:x", r"C:\prof::$DATA", r"C:\a\b:stream:$DATA", r"\\srv\share\p:q", "C:\\p\\:"):
            with self.subTest(b=b), self.assertRaises(ValueError):
                gh.build_launch_command(STUB, b, [])
        gh.build_launch_command(STUB, r"C:\prof", [])
        gh.build_launch_command(STUB, r"\\srv\share\prof", [])

    def test_stub_is_validated(self):
        for stub in ("", "   ", "C:\\a\nb.exe", "C:\\a\x00b.exe", 'C:\\a"b.exe', "C:\\a\tb.exe"):
            for profile in (None, PROFILE):
                with self.subTest(stub=stub, profile=profile), self.assertRaises(ValueError):
                    gh.build_launch_command(stub, profile, [])

    def test_is_gemini_image_needs_an_absolute_path_and_no_trailing_text(self):
        for img in (APP + "\n", APP + "\r", r"google\gemini\app-1.0\gemini.exe",
                    r"\google\gemini\app-1.0\gemini.exe", r"C:google\gemini\app-1.0\gemini.exe"):
            with self.subTest(img=img):
                self.assertFalse(gh.is_gemini_image(img))
        self.assertTrue(gh.is_gemini_image(r"\\srv\share\Google\Gemini\app-1\Gemini.exe"))
        self.assertTrue(gh.is_gemini_image(APP))

    def test_empty_profile_is_never_ours(self):
        for udd in (".", "", '""'):
            p = gh.ProcInfo(10, 1, APP, f'"{APP}" --user-data-dir={udd}', 100.0)
            with self.subTest(udd=udd):
                self.assertFalse(gh.is_our_instance(p, ""))
        self.assertFalse(gh.is_our_instance(gh.ProcInfo(10, 1, APP, f'"{APP}" --user-data-dir={PROFILE}', 1.0), ""))

    def test_decide_full_decisions(self):
        D = gh.Decision
        cases = [
            ((None, "ok", False), D("D2", None, None)),
            ((None, "ok", True), D("D2", None, None)),
            ((None, "blocked", False), D("D1", None, "profile_switch_ignored")),
            (("D2", "ok", False), D("D2", None, None)),
            (("D1", "ok", False), D("D1", None, None)),
            (("D1", "blocked", False), D("D1", None, None)),
        ]
        for args, want in cases:
            with self.subTest(args=args):
                self.assertEqual(gh.decide_mode(*args), want)
        for args, code in (((None, "blocked", True), "GEMINI_IN_USE"), ((None, "timeout", False), "HOST_START_TIMEOUT"),
                           ((None, "timeout", True), "HOST_START_TIMEOUT"), (("D2", "blocked", False), "HOST_MODE_UNAVAILABLE"),
                           (("D2", "timeout", True), "HOST_START_TIMEOUT"), (("D1", "ok", True), "GEMINI_IN_USE")):
            with self.subTest(args=args):
                d = gh.decide_mode(*args)
                self.assertEqual((d.mode, d.code), (None, code))
                self.assertTrue(d.reason)
        d = gh.decide_mode(job_mode="D2", d2="ok", user_gemini_running=False)
        self.assertEqual(d, gh.Decision(mode="D2", code=None, reason=None))

    def test_pinned_d1_ignores_d2(self):
        self.assertEqual(gh.decide_mode("D1", None, False), gh.Decision("D1", None, None))
        d = gh.decide_mode("D1", None, True)
        self.assertEqual((d.mode, d.code), (None, "GEMINI_IN_USE"))
        for job_mode in (None, "D2"):
            with self.subTest(job_mode=job_mode), self.assertRaises(ValueError):
                gh.decide_mode(job_mode, None, False)
        with self.assertRaises(ValueError):
            gh.decide_mode("D1", "pending", False)

    def test_classify_after_the_start_timeout(self):
        LC = gh.LaunchCheck
        self.assertEqual(gh.classify_launch(LC(window_on_our_desktop=True, cmdline_has_profile=True,
                                               local_state_updated=True, exited=False, elapsed_s=90.0)), "ok")
        self.assertEqual(gh.classify_launch(LC(True, False, True, False, 90.0)), "blocked")
        self.assertEqual(gh.classify_launch(LC(True, True, True, True, 90.0)), "blocked")
        self.assertEqual(gh.classify_launch(LC(True, True, False, False, 60.0)), "timeout")
        self.assertEqual(gh.classify_launch(LC(False, False, False, False, 59.9)), "pending")


class EnvTests(unittest.TestCase):
    def test_clean_env_drops_node_electron(self):
        env = {"PATH": "p", "NODE_OPTIONS": "x", "ELECTRON_RUN_AS_NODE": "1", "NODEJS": "keep", "A": "b"}
        self.assertEqual(gh.clean_env(env), {"PATH": "p", "NODEJS": "keep", "A": "b"})


if __name__ == "__main__":
    unittest.main()

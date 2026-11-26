#!/usr/bin/env python3
"""test_chrome_mode.py - Chrome mode: Deep Research in a visible Chrome window on gemini.google.com, no app patch.

Covers:
  - chrome_launcher: locating chrome.exe, the command line (own profile, own debugging port, gemini url), reuse of a
    running instance, launch and wait, failure messages the relay classifies
  - run_deep_research: GEMINI_DR_BROWSER=chrome connects through the Chrome launcher and never the app patcher;
    the default (unset) stays the desktop app
  - patch_gemini_app.py refuses to modify the app in Chrome mode
  - deep_research.py: `--browser` on init/auto, default chrome for new jobs, app for jobs created before the option,
    request.json env/cdp_url, relay task text without the patcher instruction in Chrome mode
  - relay_step: Chrome launcher failures map to their own codes, never to the CDP codes that let agy run the patcher
Nothing here starts Chrome or touches the Gemini app: every launch is mocked or pointed at a temp directory.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = TESTS_DIR.parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(TESTS_DIR))

import chrome_launcher  # noqa: E402
import deep_research as dr  # noqa: E402
import relay_step  # noqa: E402
import run_deep_research as rdr  # noqa: E402
from test_deep_research import DeepResearchTestBase  # noqa: E402


class ChromeLauncherTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="chrome_mode_")).resolve()
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.fake_chrome = self.tmp / "chrome.exe"
        self.fake_chrome.write_text("", encoding="utf-8")
        self.profile = self.tmp / "profile"

    def test_find_chrome_honours_override_and_reports_missing(self) -> None:
        with mock.patch.dict(os.environ, {"GEMINI_DR_CHROME_PATH": str(self.fake_chrome)}):
            self.assertEqual(chrome_launcher.find_chrome(), str(self.fake_chrome))
        with mock.patch.dict(os.environ, {"GEMINI_DR_CHROME_PATH": str(self.tmp / "nope.exe")}), \
                mock.patch.object(chrome_launcher, "CANDIDATE_PATHS", ()):
            with self.assertRaises(FileNotFoundError) as cm:
                chrome_launcher.find_chrome()
            self.assertIn("Chrome executable not found", str(cm.exception))

    def test_command_uses_own_profile_port_and_gemini_url(self) -> None:
        cmd = chrome_launcher.build_chrome_command(str(self.fake_chrome), 9224, str(self.profile))
        self.assertEqual(cmd[0], str(self.fake_chrome))
        self.assertIn("--remote-debugging-port=9224", cmd)
        self.assertIn(f"--user-data-dir={self.profile}", cmd)
        self.assertIn("--disable-renderer-backgrounding", cmd)
        self.assertIn("--disable-background-timer-throttling", cmd)
        self.assertIn("--disable-backgrounding-occluded-windows", cmd)
        self.assertEqual(cmd[-1], chrome_launcher.GEMINI_URL)
        self.assertTrue(chrome_launcher.GEMINI_URL.startswith("https://gemini.google.com/"))

    def test_default_port_and_profile_do_not_collide_with_the_app(self) -> None:
        import patch_gemini_app
        self.assertNotEqual(chrome_launcher.DEFAULT_PORT, patch_gemini_app.CDP_PORT)
        self.assertIn("agy-deep-research", chrome_launcher.default_profile_dir())

    def test_running_instance_is_reused(self) -> None:
        with mock.patch.object(chrome_launcher, "is_cdp_alive", return_value=True), \
                mock.patch.object(chrome_launcher, "spawn_detached") as spawn:
            info = chrome_launcher.ensure_chrome_running_with_cdp(port=9224, profile_dir=str(self.profile))
        spawn.assert_not_called()
        self.assertEqual(info["status"], "already_running")
        self.assertEqual(info["browser"], "chrome")
        self.assertEqual(info["cdp_url"], "http://127.0.0.1:9224")

    def test_launch_waits_for_the_port(self) -> None:
        alive = iter([False, False, True])
        with mock.patch.dict(os.environ, {"GEMINI_DR_CHROME_PATH": str(self.fake_chrome)}), \
                mock.patch.object(chrome_launcher, "is_cdp_alive", side_effect=lambda port: next(alive)), \
                mock.patch.object(chrome_launcher, "spawn_detached") as spawn, \
                mock.patch.object(chrome_launcher.time, "sleep"):
            info = chrome_launcher.ensure_chrome_running_with_cdp(port=9224, profile_dir=str(self.profile))
        spawn.assert_called_once()
        self.assertIn("--remote-debugging-port=9224", spawn.call_args[0][0])
        self.assertEqual(info["status"], "started")
        self.assertTrue(self.profile.is_dir(), "the profile directory is created before launch")

    def test_port_that_never_answers_raises_a_classifiable_error(self) -> None:
        with mock.patch.dict(os.environ, {"GEMINI_DR_CHROME_PATH": str(self.fake_chrome)}), \
                mock.patch.object(chrome_launcher, "is_cdp_alive", return_value=False), \
                mock.patch.object(chrome_launcher, "spawn_detached"), \
                mock.patch.object(chrome_launcher.time, "sleep"):
            with self.assertRaises(RuntimeError) as cm:
                chrome_launcher.ensure_chrome_running_with_cdp(port=9224, profile_dir=str(self.profile))
        self.assertIn("Chrome debugging port did not answer", str(cm.exception))


class DriverDispatchTestCase(unittest.TestCase):
    def test_chrome_mode_uses_the_chrome_launcher_only(self) -> None:
        with mock.patch.dict(os.environ, {"GEMINI_DR_BROWSER": "chrome"}), \
                mock.patch.object(rdr, "ensure_chrome_running_with_cdp",
                                  return_value={"status": "already_running", "cdp_url": "http://127.0.0.1:9224", "browser": "chrome"}) as chrome, \
                mock.patch.object(rdr, "ensure_gemini_running_with_cdp") as app:
            info = rdr.ensure_browser_with_cdp(show=False)
        chrome.assert_called_once()
        app.assert_not_called()
        self.assertEqual(info["browser"], "chrome")

    def test_unset_mode_stays_the_desktop_app(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "GEMINI_DR_BROWSER"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(rdr, "ensure_chrome_running_with_cdp") as chrome, \
                mock.patch.object(rdr, "ensure_gemini_running_with_cdp",
                                  return_value={"status": "already_running", "cdp_url": "http://127.0.0.1:9223"}) as app:
            rdr.ensure_browser_with_cdp(show=False)
        app.assert_called_once()
        chrome.assert_not_called()

    def test_chrome_mode_opens_a_gemini_tab_when_none_is_open(self) -> None:
        opened = SimpleNamespace(url="about:blank", goto=mock.Mock())
        ctx = SimpleNamespace(pages=[SimpleNamespace(url="chrome://newtab/")], new_page=mock.Mock(return_value=opened))
        browser = SimpleNamespace(contexts=[ctx])
        with mock.patch.object(rdr.time, "sleep"):
            page = rdr.get_gemini_page(browser, timeout_sec=0, open_if_missing=True)
        self.assertIs(page, opened)
        opened.goto.assert_called_once()
        self.assertIn("gemini.google.com", opened.goto.call_args[0][0])


class PatcherRefusesInChromeModeTestCase(unittest.TestCase):
    def test_patch_run_is_refused(self) -> None:
        tmp = tempfile.mkdtemp(prefix="nogemini_")
        env = dict(os.environ, GEMINI_DR_BROWSER="chrome", GEMINI_APP_BASE_DIR=tmp,
                   AGY_DR_PATCH_STATE=os.path.join(tmp, "state.json"))
        cp = subprocess.run([sys.executable, str(SCRIPTS_DIR / "patch_gemini_app.py")], capture_output=True,
                            text=True, encoding="utf-8", env=env)
        self.assertEqual(cp.returncode, 1)
        self.assertEqual(json.loads(cp.stdout)["status"], "refused_chrome_mode")


class RelayClassificationTestCase(unittest.TestCase):
    def _code(self, stderr: str) -> str:
        run = SimpleNamespace(stderr=stderr, killed=None, guard_trip=None, prompt_sent=False, rc=1)
        return relay_step.classify(run, "plan").code

    def test_chrome_failures_have_their_own_codes(self) -> None:
        self.assertEqual(self._code("FileNotFoundError: Chrome executable not found; install Chrome"), "CHROME_NOT_FOUND")
        self.assertEqual(self._code("RuntimeError: Chrome debugging port did not answer on 9224 within 20 s"),
                         "CHROME_CDP_NOT_READY")


class OrchestratorBrowserOptionTestCase(DeepResearchTestBase):
    def _job(self, job_id: str, *extra: str) -> dict:
        rc, res, err = self.run_dr(["init", "--job", job_id, "--prompt", "Vector search indexing survey.",
                                    "--goal", "Benchmarks and memory per index type.", *extra])
        self.assertEqual(rc, 0, err)
        return json.loads((self.jobs_dir / job_id / "job.json").read_text(encoding="utf-8"))

    def test_new_jobs_default_to_chrome_and_requests_carry_it(self) -> None:
        job = self._job("chrome_default")
        self.assertEqual(job["browser"], "chrome")
        rc, res, err = self.run_dr(["plan", "--job", "chrome_default"])
        self.assertEqual(rc, 0, err)
        req = json.loads((self.jobs_dir / "chrome_default" / "relay" / "request.json").read_text(encoding="utf-8"))
        self.assertEqual(req["env"]["GEMINI_DR_BROWSER"], "chrome")
        self.assertEqual(req["cdp_url"], f"http://127.0.0.1:{chrome_launcher.DEFAULT_PORT}")

    def test_app_mode_is_kept_and_inherited(self) -> None:
        job = self._job("app_job", "--browser", "app")
        self.assertEqual(job["browser"], "app")
        self.run_dr(["plan", "--job", "app_job"])
        req = json.loads((self.jobs_dir / "app_job" / "relay" / "request.json").read_text(encoding="utf-8"))
        self.assertEqual(req["env"]["GEMINI_DR_BROWSER"], "app")
        self.assertNotIn("cdp_url", req)

    def test_jobs_from_before_the_option_stay_on_the_app(self) -> None:
        self._job("old_job")
        job_file = self.jobs_dir / "old_job" / "job.json"
        job = json.loads(job_file.read_text(encoding="utf-8"))
        job.pop("browser")
        job_file.write_text(json.dumps(job), encoding="utf-8")
        self.run_dr(["plan", "--job", "old_job"])
        req = json.loads((self.jobs_dir / "old_job" / "relay" / "request.json").read_text(encoding="utf-8"))
        self.assertEqual(req["env"]["GEMINI_DR_BROWSER"], "app")

    def test_invalid_browser_is_rejected(self) -> None:
        rc, res, _ = self.run_dr(["init", "--job", "bad_browser", "--prompt", "Vector search indexing survey.",
                                  "--goal", "Benchmarks and memory.", "--browser", "firefox"])
        self.assertEqual(rc, 2)   # argparse choices

    def test_relay_task_names_the_patcher_only_in_app_mode(self) -> None:
        job_dir = Path(tempfile.mkdtemp(prefix="task_"))
        chrome_task = dr.build_relay_task_prompt("plan", 1, job_dir, dr.SKILL_DIR, browser="chrome")
        app_task = dr.build_relay_task_prompt("plan", 1, job_dir, dr.SKILL_DIR, browser="app")
        self.assertNotIn("scripts/patch_gemini_app.py", chrome_task)   # no command to run it
        self.assertIn("Never run `patch_gemini_app.py`", chrome_task)
        self.assertIn("relay_step.py", chrome_task)
        self.assertIn("scripts/patch_gemini_app.py", app_task)

    def test_auto_accepts_browser(self) -> None:
        out = self.out_dir / "auto_chrome.md"
        rc, res, err = self.run_dr(["auto", "--job", "auto_chrome", "--browser", "chrome", "--prompt",
                                    "Vector search indexing survey.", "--goal", "Benchmarks and memory per index type.",
                                    "--out", str(out)])
        self.assertEqual(rc, 0, f"{res}\n{err}")
        job = json.loads((self.jobs_dir / "auto_chrome" / "job.json").read_text(encoding="utf-8"))
        self.assertEqual(job["browser"], "chrome")


if __name__ == "__main__":
    unittest.main()

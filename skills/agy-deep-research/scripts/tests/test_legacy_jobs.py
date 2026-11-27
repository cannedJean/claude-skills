#!/usr/bin/env python3
"""test_legacy_jobs.py - the app patcher and the Chrome/debugging-port modes are gone.

Covers:
  - new jobs carry "host": "uia" in job.json and no "browser" field
  - plan / edit / approve / collect / status refuse a job without "host": "uia" or with a "browser" field
    (LEGACY_BROWSER_MODE, exit 2, hint: start a new job), before anything runs
  - the relay task prompt has no patcher / debugging-port recovery text, only the fixed no-retry line
  - relay_step: a request's old endpoint field is ignored; the probe is the driver's `--step probe`, and a failing
    probe is an error the collect step maps to COLLECT_PROBE_FAILED
  - run_deep_research.py: every step except --help / --dry-run exits 3 with UIA_DRIVER_NOT_IMPLEMENTED
"""
from __future__ import annotations

import sandbox  # noqa: F401  (must stay the first import: blocks real-app access)
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = TESTS_DIR.parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(TESTS_DIR))

import deep_research as dr  # noqa: E402
import relay_step  # noqa: E402
from test_deep_research import DeepResearchTestBase  # noqa: E402

PROMPT = "Research cloud-native vector indexing architectures and compare their latency and recall."
GOAL = "# Goal\n- cover HNSW, IVF-PQ and DiskANN with latency and recall numbers"
REASON = "The plan covers every engine and metric that goal.md requires, so it is approved."
NO_RETRY_LINE = "2. On any error code, report it without retrying."


class LegacyJobTestCase(DeepResearchTestBase):
    def init_job(self, job_id: str) -> Path:
        rc, res, err = self.run_dr(["init", "--job", job_id, "--prompt", PROMPT, "--goal", GOAL])
        self.assertEqual(rc, 0, (res, err))
        return self.jobs_dir / job_id

    def rewrite_job(self, job_dir: Path, **changes) -> None:
        path = job_dir / "job.json"
        job = json.loads(path.read_text(encoding="utf-8"))
        for k, v in changes.items():
            if v is None:
                job.pop(k, None)
            else:
                job[k] = v
        job.update({"status": "plan_ready", "url": "https://gemini.google.com/app/legacy1", "plan_count": 1,
                    "latest_plan": "1. Step one"})
        path.write_text(json.dumps(job), encoding="utf-8")

    def assert_refused(self, job_id: str) -> None:
        steps = {
            "plan": ["plan", "--job", job_id],
            "edit": ["edit", "--job", job_id, "--feedback", "Add a section on cost."],
            "approve": ["approve", "--job", job_id, "--reason", REASON],
            "collect": ["collect", "--job", job_id, "--no-wait"],
            "status": ["status", "--job", job_id],
        }
        for step, argv in steps.items():
            with self.subTest(step=step):
                rc, res, err = self.run_dr(argv)
                self.assertEqual(rc, 2, (res, err))
                self.assertEqual(res.get("code"), "LEGACY_BROWSER_MODE", res)
                self.assertIn("new job", res.get("hint", ""))
        self.assertFalse((self.jobs_dir / job_id / "relay" / "request.json").exists(), "a legacy job reached the relay")

    def test_new_job_has_host_uia(self) -> None:
        job_dir = self.init_job("host_job")
        job = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
        self.assertEqual(job.get("host"), "uia")
        self.assertNotIn("browser", job)

    def test_legacy_chrome_job_refused(self) -> None:
        job_dir = self.init_job("legacy_chrome")
        self.rewrite_job(job_dir, host=None, browser="chrome")
        self.assert_refused("legacy_chrome")

    def test_legacy_app_job_refused(self) -> None:
        # jobs created before the browser option had neither field (they ran on the patched app)
        job_dir = self.init_job("legacy_app")
        self.rewrite_job(job_dir, host=None, browser=None)
        self.assert_refused("legacy_app")

    def test_browser_field_refused_even_with_host(self) -> None:
        job_dir = self.init_job("legacy_mixed")
        self.rewrite_job(job_dir, host="uia", browser="app")
        self.assert_refused("legacy_mixed")

    def test_browser_option_is_gone(self) -> None:
        rc, _, _ = self.run_dr(["init", "--job", "x_opt", "--browser", "chrome", "--prompt", PROMPT, "--goal", GOAL])
        self.assertNotEqual(rc, 0)


class RelayTaskPromptTestCase(unittest.TestCase):
    def test_relay_task_prompt_has_no_patch_text(self) -> None:
        text = dr.build_relay_task_prompt("plan", 1, Path("C:/jobs/j1"), Path("C:/skill"))
        self.assertIn(NO_RETRY_LINE, text)
        for bad in ("patch_gemini_app", "CDP", "Chrome", "{cdp_recovery}", "9223", "9224"):
            self.assertNotIn(bad, text)
        self.assertIn('relay_step.py" --job "C:/jobs/j1"', text)

    def test_bundled_scripts(self) -> None:
        self.assertEqual(dr.BUNDLED_SCRIPTS, ("relay_step.py", "run_deep_research.py", "gemini_host.py", "uitree.py",
                                              "privacy.py", "locators.py", "locators.json"))
        for name in dr.BUNDLED_SCRIPTS:
            self.assertTrue((SCRIPTS_DIR / name).is_file(), name)

    def test_removed_names(self) -> None:
        for name in ("BROWSERS", "DEFAULT_BROWSER", "APP_CDP_RECOVERY", "CHROME_CDP_RECOVERY", "read_patch_status",
                     "read_chrome_status", "job_browser", "chrome_cdp_url", "apply_browser"):
            self.assertFalse(hasattr(dr, name), name)
        for name in ("patch_gemini_app.py", "chrome_launcher.py"):
            self.assertFalse((SCRIPTS_DIR / name).exists(), name)


class RelayProbeTestCase(unittest.TestCase):
    def test_probe_runs_driver_step_probe_and_ignores_endpoint_field(self) -> None:
        req = {"resume_url": "https://gemini.google.com/app/abc", "cdp_url": "http://127.0.0.1:1",
               "driver": str(SCRIPTS_DIR / "run_deep_research.py")}
        done = subprocess.CompletedProcess([], 0, stdout='{"start_enabled": false, "url": "https://gemini.google.com/app/abc"}\n', stderr="")
        with mock.patch.object(relay_step.subprocess, "run", return_value=done) as run:
            info = relay_step.probe_page(req, sys.executable)
        argv = run.call_args[0][0]
        self.assertEqual(argv[0], sys.executable)
        self.assertTrue(argv[1].endswith("run_deep_research.py"))
        self.assertEqual(argv[2:4], ["--step", "probe"])
        self.assertIn("--resume-url", argv)
        self.assertNotIn("-c", argv)
        self.assertNotIn("http://127.0.0.1:1", argv)
        self.assertEqual(info.get("url"), "https://gemini.google.com/app/abc")
        self.assertNotIn("error", info)

    def test_failing_probe_is_an_error(self) -> None:
        req = {"resume_url": "https://gemini.google.com/app/abc", "driver": str(SCRIPTS_DIR / "run_deep_research.py")}
        info = relay_step.probe_page(req, sys.executable)
        self.assertIn("error", info)
        self.assertIn("UIA_DRIVER_NOT_IMPLEMENTED", info["error"])

    def test_no_removed_failure_codes(self) -> None:
        codes = {f[1] for f in relay_step.FAILURES}
        for gone in ("PLAYWRIGHT_MISSING", "ASAR_PATTERN_MISMATCH", "CHROME_NOT_FOUND", "CHROME_CDP_NOT_READY",
                     "CDP_NOT_READY", "CDP_CONNECT_FAILED"):
            self.assertNotIn(gone, codes)
        self.assertIn("UIA_DRIVER_NOT_IMPLEMENTED", codes)


class DriverStubTestCase(unittest.TestCase):
    def run_driver(self, *argv: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(SCRIPTS_DIR / "run_deep_research.py"), *argv], capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=60,
                              env=dict(os.environ, PYTHONIOENCODING="utf-8"))

    def test_every_step_refuses_until_the_uia_driver_exists(self) -> None:
        url = "https://gemini.google.com/app/abc"
        for argv in (["--step", "probe", "--resume-url", url], ["--step", "plan", "--prompt", "topic"],
                     ["--step", "edit", "--resume-url", url, "--edit-prompt", "Add a section."],
                     ["--step", "approve", "--resume-url", url], ["--resume-url", url]):
            with self.subTest(argv=argv):
                cp = self.run_driver(*argv)
                self.assertEqual(cp.returncode, 3, cp.stderr)
                last = cp.stdout.strip().splitlines()[-1]
                self.assertEqual(json.loads(last), {"status": "error", "code": "UIA_DRIVER_NOT_IMPLEMENTED"})

    def test_help_and_dry_run_succeed(self) -> None:
        self.assertEqual(self.run_driver("--help").returncode, 0)
        cp = self.run_driver("--dry-run", "--step", "plan", "--prompt", "topic")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        out = json.loads(cp.stdout.strip().splitlines()[-1])
        self.assertEqual(out["status"], "dry_run")
        self.assertEqual(out["step"], "plan")


if __name__ == "__main__":
    unittest.main()

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
  - setup removes the obsolete patcher / launcher copies from the target; preflight flags them as out of sync
  - a refused legacy `status` still shows where a finished report is (job_status, out_path, out_exists, relay result)
  - probe contract {"start_enabled", "plan_cards", "url"}: approve's PLAN_CHANGED gate reads plan_cards
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

    def test_init_stdout_reports_host_uia(self) -> None:
        rc, res, err = self.run_dr(["init", "--job", "host_stdout", "--prompt", PROMPT, "--goal", GOAL])
        self.assertEqual(rc, 0, (res, err))
        self.assertEqual(res.get("host"), "uia")
        self.assertNotIn("browser", res)

    def test_browser_option_is_gone(self) -> None:
        for argv in (["auto", "--prompt", PROMPT, "--goal", GOAL, "--browser", "chrome"],
                     ["init", "--job", "b1", "--prompt", PROMPT, "--goal", GOAL, "--browser", "app"]):
            with self.subTest(argv=argv[0]):
                rc, _, err = self.run_dr(argv)
                self.assertEqual(rc, 2, err)
                self.assertIn("--browser", err)

    def test_corrupt_job_json_is_invalid_state_not_a_crash(self) -> None:
        for i, content in enumerate(("[1]", '"text"', "42", '{"host": "uia", "out_path": 5, "browser": "x"}',
                                     '{"host": "uia", "out_path": ["r.md"], "status": "plan_ready"}')):
            with self.subTest(content=content):
                job_dir = self.init_job(f"corrupt_{i}")
                (job_dir / "job.json").write_text(content, encoding="utf-8")
                rc, res, err = self.run_dr(["status", "--job", f"corrupt_{i}"])
                self.assertNotIn("Traceback", err)
                if '"browser"' in content:
                    self.assertEqual((rc, res.get("code")), (2, "LEGACY_BROWSER_MODE"), (res, err))
                    self.assertIsInstance(res.get("out_path"), str)
                else:
                    self.assertEqual((rc, res.get("code")), (5, "INVALID_JOB_STATE"), (res, err))

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

    def test_legacy_status_keeps_a_finished_report_visible(self) -> None:
        job_dir = self.init_job("legacy_done")
        self.rewrite_job(job_dir, host=None, browser="chrome")
        job = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
        job["status"] = "completed"
        (job_dir / "job.json").write_text(json.dumps(job), encoding="utf-8")
        Path(job["out_path"]).write_text("# Report\n", encoding="utf-8")
        (job_dir / "relay" / "result.json").write_text(json.dumps({"status": "completed", "step": "approve"}),
                                                       encoding="utf-8")
        rc, res, err = self.run_dr(["status", "--job", "legacy_done"])
        self.assertEqual(rc, 2, (res, err))
        self.assertEqual(res.get("code"), "LEGACY_BROWSER_MODE")
        self.assertEqual(res.get("job_status"), "completed")
        self.assertEqual(Path(res.get("out_path")).resolve(), Path(job["out_path"]).resolve())
        self.assertIs(res.get("out_exists"), True)
        self.assertEqual(res.get("relay_result_status"), "completed")

    def test_legacy_status_without_report(self) -> None:
        job_dir = self.init_job("legacy_none")
        self.rewrite_job(job_dir, host=None, browser=None)
        rc, res, _ = self.run_dr(["status", "--job", "legacy_none"])
        self.assertEqual(rc, 2)
        self.assertIs(res.get("out_exists"), False)
        self.assertIsNone(res.get("relay_result_status"))

    def test_browser_option_is_gone(self) -> None:
        rc, _, _ = self.run_dr(["init", "--job", "x_opt", "--browser", "chrome", "--prompt", PROMPT, "--goal", GOAL])
        self.assertNotEqual(rc, 0)


OBSOLETE = ("patch_gemini_app.py", "chrome_launcher.py")


class SetupObsoleteFilesTestCase(DeepResearchTestBase):
    def test_setup_removes_obsolete_files_and_preflight_flags_them(self) -> None:
        scripts = self.target_skill_dir / "scripts"
        scripts.mkdir(parents=True)
        for name in OBSOLETE + ("keep_me.py",):
            (scripts / name).write_text("# old\n", encoding="utf-8")

        rc, pf, _ = self.run_dr(["preflight", "--target-dir", str(self.target_skill_dir)])
        self.assertEqual(sorted(pf.get("checks", {}).get("sync", {}).get("obsolete_files", [])),
                         sorted(f"scripts/{n}" for n in OBSOLETE), pf)

        rc, st, _ = self.run_dr(["setup", "--target-dir", str(self.target_skill_dir)])
        self.assertEqual(rc, 0, st)
        self.assertEqual(sorted(st.get("removed_obsolete", [])), sorted(f"scripts/{n}" for n in OBSOLETE))
        for name in OBSOLETE:
            self.assertFalse((scripts / name).exists(), name)
        self.assertTrue((scripts / "keep_me.py").is_file(), "setup removed a file that is not on the obsolete list")
        self.assertEqual(st["sync_status"], "in_sync")

        rc, pf2, _ = self.run_dr(["preflight", "--target-dir", str(self.target_skill_dir)])
        self.assertEqual(pf2["sync_status"], "in_sync")
        self.assertEqual(pf2["checks"]["sync"]["obsolete_files"], [])

    def test_pre_m2_install_is_out_of_sync_not_missing(self) -> None:
        # an old install: some of today's bundled files are missing and the patcher is still there
        scripts = self.target_skill_dir / "scripts"
        scripts.mkdir(parents=True)
        for name in ("relay_step.py", "run_deep_research.py", "patch_gemini_app.py"):
            (scripts / name).write_text("# old\n", encoding="utf-8")
        (self.target_skill_dir / "SKILL.md").write_text("# old\n", encoding="utf-8")
        res = dr.check_sync_status(self.target_skill_dir)
        self.assertEqual(res["status"], "out_of_sync")
        self.assertEqual(res["obsolete_files"], ["scripts/patch_gemini_app.py"])
        empty = self.target_skill_dir.parent / "never_installed"
        self.assertEqual(dr.check_sync_status(empty)["status"], "missing_target")

    def test_broken_obsolete_symlink_is_flagged(self) -> None:
        rc, st, _ = self.run_dr(["setup", "--target-dir", str(self.target_skill_dir)])
        self.assertEqual(st["sync_status"], "in_sync")
        link = self.target_skill_dir / "scripts" / "patch_gemini_app.py"
        try:
            os.symlink(str(self.target_skill_dir / "scripts" / "__gone__.py"), str(link))
        except OSError as e:
            self.skipTest(f"no symlinks here: {e}")
        self.assertFalse(link.exists())
        res = dr.check_sync_status(self.target_skill_dir)
        self.assertEqual((res["status"], res["obsolete_files"]), ("out_of_sync", ["scripts/patch_gemini_app.py"]))

    def test_obsolete_file_alone_makes_a_synced_target_out_of_sync(self) -> None:
        rc, st, _ = self.run_dr(["setup", "--target-dir", str(self.target_skill_dir)])
        self.assertEqual(st["sync_status"], "in_sync")
        self.assertEqual(st.get("removed_obsolete"), [])
        (self.target_skill_dir / "scripts" / "chrome_launcher.py").write_text("# old\n", encoding="utf-8")
        rc, pf, _ = self.run_dr(["preflight", "--target-dir", str(self.target_skill_dir)])
        self.assertEqual(pf["sync_status"], "out_of_sync")
        self.assertEqual(pf["checks"]["sync"]["obsolete_files"], ["scripts/chrome_launcher.py"])


class RelayTaskPromptTestCase(unittest.TestCase):
    def test_relay_task_prompt_has_no_patch_text(self) -> None:
        text = dr.build_relay_task_prompt("plan", 1, Path("C:/jobs/j1"), Path("C:/skill"))
        self.assertIn(NO_RETRY_LINE, text)
        for bad in ("patch_gemini_app", "CDP", "Chrome", "{cdp_recovery}", "9223", "9224"):
            self.assertNotIn(bad, text)
        self.assertIn('relay_step.py" --job "C:/jobs/j1"', text)

    def test_bundled_scripts(self) -> None:
        self.assertEqual(dr.BUNDLED_SCRIPTS, ("relay_step.py", "run_deep_research.py", "gemini_host.py", "uitree.py",
                                              "privacy.py", "locators.py", "locators.json", "steps.py",
                                              "uia_backend.py"))
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
        done = subprocess.CompletedProcess([], 3, stdout='{"status": "error", "code": "UIA_DRIVER_NOT_IMPLEMENTED"}\n',
                                           stderr="")
        with mock.patch.object(relay_step.subprocess, "run", return_value=done) as run:
            info = relay_step.probe_page(req, sys.executable)
        run.assert_called_once()
        self.assertIn("error", info)
        self.assertIn("UIA_DRIVER_NOT_IMPLEMENTED", info["error"])

    def test_probe_contract_is_documented(self) -> None:
        import run_deep_research as rdr
        for doc in (relay_step.__doc__, rdr.__doc__):
            self.assertIn('{"start_enabled": bool, "plan_cards": int, "url": str}', doc or "")

    def test_no_removed_failure_codes(self) -> None:
        codes = {f[1] for f in relay_step.FAILURES}
        for gone in ("PLAYWRIGHT_MISSING", "ASAR_PATTERN_MISMATCH", "CHROME_NOT_FOUND", "CHROME_CDP_NOT_READY",
                     "CDP_NOT_READY", "CDP_CONNECT_FAILED"):
            self.assertNotIn(gone, codes)
        self.assertIn("UIA_DRIVER_NOT_IMPLEMENTED", codes)


class RelayPlanChangedTestCase(unittest.TestCase):
    URL = "https://gemini.google.com/app/abc"

    def setUp(self) -> None:
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.job = self._tmp.name
        self.relay_dir = os.path.join(self.job, "relay")
        os.makedirs(self.relay_dir)
        self.result_path = os.path.join(self.relay_dir, "result.json")
        self.driver = os.path.join(self.job, "run_deep_research.py")
        with open(self.driver, "w", encoding="utf-8") as f:
            f.write('# fake driver: "--step" "--prompt-file" "--edit-prompt-file"\n')

    def approve(self, plan_cards: int):
        req = {"relay_version": 2, "step": "approve", "seq": 2, "driver": self.driver, "resume_url": self.URL,
               "output": os.path.join(self.job, "report.md"), "timeout": 60, "expected_url": self.URL,
               "expected_plan_count": 1}
        probe_json = json.dumps({"start_enabled": True, "plan_cards": plan_cards, "url": self.URL})
        done = subprocess.CompletedProcess([], 0, stdout=probe_json + "\n", stderr="")

        class _Ran(Exception):
            pass

        with mock.patch.object(relay_step.subprocess, "run", return_value=done), \
                mock.patch.object(relay_step, "run_driver", side_effect=_Ran()) as run_driver:
            try:
                res, code = relay_step.do_step(self.job, req, self.relay_dir, self.result_path)
            except _Ran:
                res, code = None, None
        return res, code, run_driver

    def test_plan_cards_mismatch_is_plan_changed(self) -> None:
        res, code, run_driver = self.approve(plan_cards=2)
        self.assertEqual(code, 2)
        self.assertEqual(res["code"], "PLAN_CHANGED")
        run_driver.assert_not_called()

    def test_matching_plan_cards_passes_the_gate(self) -> None:
        _, _, run_driver = self.approve(plan_cards=1)
        run_driver.assert_called_once()


class RelayClassifyTestCase(unittest.TestCase):
    """T14: the stderr needle -> code mapping of relay_step.classify."""

    def run_with(self, stderr: str, rc: int = 1, **attrs):
        run = relay_step.Run()
        run.rc, run.err = rc, [stderr]
        for k, v in attrs.items():
            setattr(run, k, v)
        return run

    def test_every_failure_needle_maps_to_its_code(self) -> None:
        seen = set()
        for needle, code, exit_code, _ in relay_step.FAILURES:
            if needle in seen:
                continue
            seen.add(needle)
            with self.subTest(code=code):
                err = relay_step.classify(self.run_with(f"Traceback\nRuntimeError: {needle} (x)\n"), "approve")
                self.assertEqual((err.code, err.exit_code), (code, exit_code))

    def test_other_outcomes(self) -> None:
        self.assertIsNone(relay_step.classify(self.run_with("", rc=0), "plan"))
        self.assertEqual(relay_step.classify(self.run_with("usage: bad flag", rc=2), "plan").code, "DRIVER_ARGS_REJECTED")
        self.assertEqual(relay_step.classify(self.run_with("boom", rc=1), "plan").code, "DRIVER_CRASH")
        self.assertEqual(relay_step.classify(self.run_with("", rc=None, killed="deadline"), "plan").code, "DEADLINE")
        self.assertEqual(relay_step.classify(self.run_with("Timeout 30000ms exceeded", rc=1, prompt_sent=True), "plan").code,
                         "PAGE_TIMEOUT_AFTER_SEND")
        err = relay_step.classify(self.run_with("Conversation mismatch: x", rc=1), "plan")
        self.assertEqual((err.code, err.hint), ("CONVERSATION_MISMATCH", relay_step.PLAN_MISMATCH_HINT))


class AgySkillDocsTestCase(unittest.TestCase):
    """T7: agy's skill body (references/SKILL.md) carries no patcher / CDP / browser instructions."""

    def test_reference_skill_has_no_patcher_or_cdp_text(self) -> None:
        text = (SCRIPTS_DIR.parent / "references" / "SKILL.md").read_text(encoding="utf-8").lower()
        for token in ("patch_gemini_app", "app.asar", "cdp", "remote-debugging", "9222", "9223", "9224",
                      "chrome_launcher", "--browser", "playwright"):
            with self.subTest(token=token):
                self.assertNotIn(token, text)


class RelayProbeContractTestCase(unittest.TestCase):
    """B9/M8, B21, M7, T6: the probe contract is validated (fail closed) and every tail/message is scrubbed."""
    URL = "https://gemini.google.com/app/abc"

    def setUp(self) -> None:
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.job = self._tmp.name
        self.relay_dir = os.path.join(self.job, "relay")
        os.makedirs(self.relay_dir)
        self.result_path = os.path.join(self.relay_dir, "result.json")
        self.driver = os.path.join(self.job, "run_deep_research.py")
        with open(self.driver, "w", encoding="utf-8") as f:
            f.write('# fake driver: "--step" "--prompt-file" "--edit-prompt-file"\n')

    def probe(self, stdout: str, rc: int = 0, stderr: str = "", req_extra=None):
        req = {"resume_url": self.URL, "driver": self.driver}
        req.update(req_extra or {})
        done = subprocess.CompletedProcess([], rc, stdout=stdout, stderr=stderr)
        with mock.patch.object(relay_step.subprocess, "run", return_value=done) as run:
            return relay_step.probe_page(req, sys.executable), run

    def test_probe_without_boolean_start_enabled_is_an_error(self) -> None:
        for out in ('{"url": "%s"}' % self.URL, '{"start_enabled": "yes", "url": "u"}', '{"start_enabled": 1, "url": "u"}',
                    '{"start_enabled": null, "url": "u"}', '{"start_enabled": true, "plan_cards": "2", "url": "u"}',
                    '{"start_enabled": true, "plan_cards": true, "url": "u"}',
                    '{"start_enabled": true, "plan_cards": 1.5, "url": "u"}'):
            with self.subTest(out=out):
                info, _ = self.probe(out + "\n")
                self.assertIn("error", info, info)
                self.assertNotIn("active_confirm", info)
        info, _ = self.probe('{"start_enabled": true, "plan_cards": 2, "url": "u"}\n')
        self.assertNotIn("error", info)
        self.assertEqual((info["active_confirm"], info["plan_cards"]), (1, 2))
        info, _ = self.probe('{"start_enabled": false, "url": "u"}\n')
        self.assertEqual(info["active_confirm"], 0)

    def test_probe_accepts_indented_last_json_line(self) -> None:
        info, _ = self.probe('log line\n   {"start_enabled": false, "plan_cards": 1, "url": "u"}   \n')
        self.assertNotIn("error", info, info)
        self.assertEqual(info["active_confirm"], 0)

    def test_probe_non_dict_env_is_an_error_not_a_crash(self) -> None:
        for env in (["A=1"], "A=1", 3):
            with self.subTest(env=env):
                info, run = self.probe('{"start_enabled": false, "url": "u"}\n', req_extra={"env": env})
                self.assertIn("error", info)
                run.assert_not_called()

    def test_probe_error_detail_is_scrubbed(self) -> None:
        leak = "Traceback: lookup failed for tester.person@gmail.com\nGoogle 계정: 홍길동(tester.person@gmail.com)\n"
        info, _ = self.probe("", rc=1, stderr=leak)
        self.assertIn("error", info)
        self.assertNotIn("gmail", info["error"])
        self.assertNotIn("홍길동", info["error"])
        info, _ = self.probe("no json\n", rc=0, stderr=leak * 3)
        self.assertNotIn("gmail", json.dumps(info, ensure_ascii=False))

    def collect(self, probe_stdout: str, probe_rc: int = 0):
        req = {"relay_version": 2, "step": "collect", "seq": 3, "driver": self.driver, "resume_url": self.URL,
               "output": os.path.join(self.job, "report.md"), "timeout": 60}
        done = subprocess.CompletedProcess([], probe_rc, stdout=probe_stdout, stderr="")

        class _Ran(Exception):
            pass

        with mock.patch.object(relay_step.subprocess, "run", return_value=done), \
                mock.patch.object(relay_step, "run_driver", side_effect=_Ran()) as run_driver:
            try:
                res, code = relay_step.do_step(self.job, req, self.relay_dir, self.result_path)
            except _Ran:
                res, code = None, None
        return res, code, run_driver

    def test_collect_probe_failure_is_collect_probe_failed(self) -> None:
        for out, rc in (('{"status": "error", "code": "UIA_DRIVER_NOT_IMPLEMENTED"}\n', 3), ("", 1),
                        ('{"url": "%s"}\n' % self.URL, 0), ('{"start_enabled": "no", "url": "%s"}\n' % self.URL, 0)):
            with self.subTest(out=out, rc=rc):
                res, code, run_driver = self.collect(out, rc)
                run_driver.assert_not_called()
                self.assertEqual((code, res["code"], res["exit_code"]), (3, "COLLECT_PROBE_FAILED", 3))
                with open(self.result_path, encoding="utf-8") as f:
                    self.assertEqual(json.load(f)["code"], "COLLECT_PROBE_FAILED")

    def test_collect_start_enabled_maps_to_active_confirm(self) -> None:
        res, code, run_driver = self.collect('{"start_enabled": true, "plan_cards": 1, "url": "%s"}\n' % self.URL)
        run_driver.assert_not_called()
        self.assertEqual((code, res["code"]), (2, "COLLECT_WOULD_START"))
        self.assertEqual(res["probe"]["active_confirm"], 1)
        _, _, run_driver = self.collect('{"start_enabled": false, "plan_cards": 1, "url": "%s"}\n' % self.URL)
        run_driver.assert_called_once()

    def test_approve_start_disabled_is_confirm_missing(self) -> None:
        req = {"relay_version": 2, "step": "approve", "seq": 2, "driver": self.driver, "resume_url": self.URL,
               "output": os.path.join(self.job, "report.md"), "timeout": 60}
        done = subprocess.CompletedProcess([], 0, stdout='{"start_enabled": false, "plan_cards": 1, "url": "%s"}\n'
                                           % self.URL, stderr="")
        with mock.patch.object(relay_step.subprocess, "run", return_value=done), \
                mock.patch.object(relay_step, "run_driver") as run_driver:
            res, code = relay_step.do_step(self.job, req, self.relay_dir, self.result_path)
        run_driver.assert_not_called()
        self.assertEqual((code, res["code"], res["probe"]["active_confirm"]), (2, "CONFIRM_MISSING", 0))

    def fake_run(self, stderr: str, rc: int = 1):
        run = relay_step.Run()
        run.rc = rc
        run.err = [stderr]
        return run

    def test_driver_stderr_tail_and_message_are_scrubbed_in_result(self) -> None:
        prompt = os.path.join(self.job, "prompt.md")
        with open(prompt, "w", encoding="utf-8") as f:
            f.write("topic")
        req = {"relay_version": 2, "step": "plan", "seq": 1, "driver": self.driver, "prompt_file": prompt}
        stderr = ("Traceback (most recent call last):\n  File x\nRuntimeError: signed in as tester.person@gmail.com\n"
                  "Google 계정: 홍길동(tester.person@gmail.com)\n")
        for rc, err in ((1, stderr), (2, stderr), (1, stderr + "Timeout 30000ms exceeded\n")):
            with self.subTest(rc=rc, err=err[-30:]):
                with mock.patch.object(relay_step, "run_driver", return_value=self.fake_run(err, rc)):
                    res, code = relay_step.do_step(self.job, req, self.relay_dir, self.result_path)
                self.assertEqual(res["status"], "error")
                with open(self.result_path, encoding="utf-8") as f:
                    written = f.read()
                for text in (written, json.dumps(res, ensure_ascii=False)):
                    self.assertNotIn("gmail", text)
                    self.assertNotIn("홍길동", text)

    def test_scrub_falls_back_without_privacy_module(self) -> None:
        with mock.patch.object(relay_step, "_privacy_scrub", None):
            for value in ("x tester@gmail.com y", "Google 계정: 홍길동", "a" * 300 + " tester@gmail.com",
                          ["ok", "Google Account: someone"], {"k": "tester@gmail.com"}):
                with self.subTest(value=str(value)[:40]):
                    out = json.dumps(relay_step.scrub_text(value), ensure_ascii=False)
                    self.assertNotIn("gmail", out)
                    self.assertNotIn("홍길동", out)
                    self.assertNotIn("someone", out)
            self.assertEqual(relay_step.scrub_text("plain message"), "plain message")
        self.assertIsNotNone(relay_step._privacy_scrub)


class RelayLogScrubTestCase(unittest.TestCase):
    """R7 / round 2: relay/*.stdout.txt, *.stderr.txt and progress.jsonl are written scrubbed (agy can read them)."""
    SECRET = "Google 계정: 홍길동(tester.person@gmail.com)"

    def setUp(self) -> None:
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.job = self._tmp.name
        self.relay_dir = os.path.join(self.job, "relay")
        os.makedirs(self.relay_dir)

    def run_fake(self):
        script = os.path.join(self.job, "fake_driver_account.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write("import sys\n"
                    f"print({self.SECRET!r}, flush=True)\n"
                    "print('[5s] Progress: signed in as tester.person@gmail.com', flush=True)\n"
                    "print('plain line', flush=True)\n"
                    f"sys.stderr.write({('RuntimeError: ' + self.SECRET + chr(10))!r}); sys.stderr.flush()\n")
        out, err = os.path.join(self.relay_dir, "plan_1.stdout.txt"), os.path.join(self.relay_dir, "plan_1.stderr.txt")
        prog = os.path.join(self.relay_dir, "progress.jsonl")
        run = relay_step.run_driver([sys.executable, script], self.job, out, err, 60, True, progress_path=prog)
        return run, out, err, prog

    def test_driver_logs_are_written_scrubbed(self) -> None:
        run, out, err, prog = self.run_fake()
        for path in (out, err, prog):
            with open(path, encoding="utf-8") as f:
                text = f.read()
            with self.subTest(path=os.path.basename(path)):
                self.assertNotIn("gmail", text)
                self.assertNotIn("홍길동", text)
        with open(out, encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[2], "plain line")

    def test_driver_logs_scrubbed_with_fallback(self) -> None:
        with mock.patch.object(relay_step, "_privacy_scrub", None):
            _, out, err, _ = self.run_fake()
        for path in (out, err):
            with open(path, encoding="utf-8") as f:
                self.assertNotIn("gmail", f.read())

    def test_hint_and_plan_preview_are_scrubbed(self) -> None:
        res = relay_step.scrub_result({"hint": "ask tester.person@gmail.com", "plan_preview": self.SECRET,
                                       "status": "plan_ready"})
        self.assertNotIn("gmail", json.dumps(res, ensure_ascii=False))
        self.assertNotIn("홍길동", json.dumps(res, ensure_ascii=False))
        self.assertEqual(res["status"], "plan_ready")

    def test_printed_plan_preview_is_scrubbed(self) -> None:
        import contextlib
        import io
        with open(os.path.join(self.relay_dir, "request.json"), "w", encoding="utf-8") as f:
            json.dump({"relay_version": 2, "step": "plan", "seq": 1}, f)
        res = {"relay_version": 2, "step": "plan", "status": "plan_ready", "plan": "1. " + self.SECRET}
        buf = io.StringIO()
        with mock.patch.object(relay_step, "do_step", return_value=(res, 0)), contextlib.redirect_stdout(buf):
            relay_step.main(["--job", self.job])
        self.assertNotIn("gmail", buf.getvalue())
        self.assertNotIn("홍길동", buf.getvalue())


class DriverStubTestCase(unittest.TestCase):
    def run_driver(self, *argv: str) -> subprocess.CompletedProcess:
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        self.assertEqual(env.get("AGY_DR_TEST_SANDBOX"), "1", "the stub driver runs only inside the test sandbox")
        return subprocess.run([sys.executable, str(SCRIPTS_DIR / "run_deep_research.py"), *argv], capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=60, env=env)

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

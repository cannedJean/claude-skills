#!/usr/bin/env python3
"""test_deep_research.py - offline deterministic test suite for `skills/agy-deep-research`.

Covers:
  - Tier 1: Feature coverage (init, plan, edit, approve, auto, status, collect, setup, preflight)
  - Tier 2: Boundary & corner cases (git repo escape detection, missing goal.md/prompt.md, short --reason < 20 chars,
            Korean sensitive keyword warnings, short report < 400 chars, headingless report, dead worker detection)
  - Tier 3: Cross-feature combinations (init + plan + edit + approve, approve --no-wait + status + collect,
            setup + preflight sync verification)
  - Tier 4: End-to-end real-world workflows (Standard lifecycle, iterative plan refinement, detached async re-attach,
            unattended auto mode, safety/timeout error propagation)
  - Tier 5: AGY & Gemini Model/Effort specification, alias normalization, CLI override priority,
            environment variable fallbacks, multi-turn persistence, and invocation chain propagation (R1-R4)
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = TESTS_DIR.parent
SKILL_DIR = SCRIPTS_DIR.parent
DEEP_RESEARCH_PY = SCRIPTS_DIR / "deep_research.py"
FAKE_AGY_DR = TESTS_DIR / "fake_agy_dr.py"

sys.path.insert(0, str(SCRIPTS_DIR))
from deep_research import (
    DEFAULT_AGY_EFFORT,
    DEFAULT_AGY_MODEL,
    DEFAULT_GEMINI_MODEL,
    DEFAULT_GEMINI_THINKING,
    normalize_agy_effort,
    normalize_agy_model,
    normalize_gemini_model,
)


class DeepResearchTestBase(unittest.TestCase):
    def setUp(self) -> None:
        # Allocate test directories in the system temp dir (outside any git repository)
        self.tmp_root = Path(tempfile.mkdtemp(prefix="dr_test_")).resolve()
        self.jobs_dir = self.tmp_root / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.out_dir = self.tmp_root / "outputs"
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.target_skill_dir = self.tmp_root / "gemini_skill_target"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_root, ignore_errors=True)

    def run_dr(self, args: list[str], scenario: str = "perfect", extra_env: dict | None = None) -> tuple[int, dict, str]:
        env = dict(os.environ)
        # Clear external model/effort env vars to guarantee deterministic defaults in tests
        for var in [
            "AGY_DR_MODEL", "AGY_MODEL", "AGY_TASK_MODEL",
            "AGY_DR_EFFORT", "AGY_EFFORT", "AGY_TASK_EFFORT",
            "GEMINI_DR_MODEL", "GEMINI_MODEL", "GEMINI_DR_MODE", "GEMINI_DR_THINKING",
        ]:
            env.pop(var, None)
        env.update({
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "AGY_TASK": str(FAKE_AGY_DR),
            "AGY_DR_JOBS": str(self.jobs_dir),
            "AGY_DR_SKILL_DIR": str(self.target_skill_dir),
            "AGY_DR_POLL_INTERVAL": "0.03",
            "FAKE_AGY_SCENARIO": scenario,
        })
        if extra_env:
            env.update(extra_env)
        cp = subprocess.run(
            [sys.executable, str(DEEP_RESEARCH_PY), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        data = {}
        if cp.stdout and cp.stdout.strip():
            try:
                data = json.loads(cp.stdout.strip())
            except json.JSONDecodeError:
                data = {"_raw_stdout": cp.stdout}
        return cp.returncode, data, cp.stderr


class DeepResearchTestCase(DeepResearchTestBase):
    # ------------------------------------------------------------------ Tier 1 & 2: Setup, Preflight & Isolation
    def test_setup_and_preflight_synchronization(self) -> None:
        # Before setup, target_skill_dir does not exist -> sync_status == missing_target
        rc, pf_before, _ = self.run_dr(["preflight", "--target-dir", str(self.target_skill_dir)])
        self.assertEqual(rc, 0, pf_before)
        self.assertEqual(pf_before["sync_status"], "missing_target")

        # Run setup -> copies relay_step.py, run_deep_research.py, patch_gemini_app.py, and SKILL.md
        rc, st, _ = self.run_dr(["setup", "--target-dir", str(self.target_skill_dir)])
        self.assertEqual(rc, 0, st)
        self.assertEqual(st["status"], "ok")
        self.assertEqual(st["sync_status"], "in_sync")
        self.assertEqual(len(st["copied"]), 4)
        self.assertTrue((self.target_skill_dir / "scripts" / "relay_step.py").is_file())
        self.assertTrue((self.target_skill_dir / "scripts" / "run_deep_research.py").is_file())
        self.assertTrue((self.target_skill_dir / "scripts" / "patch_gemini_app.py").is_file())
        self.assertTrue((self.target_skill_dir / "SKILL.md").is_file())

        # Re-running setup reports all 4 files unchanged
        rc, st2, _ = self.run_dr(["setup", "--target-dir", str(self.target_skill_dir)])
        self.assertEqual(rc, 0)
        self.assertEqual(len(st2["unchanged"]), 4)

        # Modify one target file -> preflight detects out_of_sync, setup restores it
        (self.target_skill_dir / "SKILL.md").write_text("# modified\n", encoding="utf-8")
        rc, pf_mid, _ = self.run_dr(["preflight", "--target-dir", str(self.target_skill_dir)])
        self.assertEqual(pf_mid["sync_status"], "out_of_sync")

        rc, st3, _ = self.run_dr(["setup", "--target-dir", str(self.target_skill_dir)])
        self.assertEqual(rc, 0)
        self.assertIn("SKILL.md", st3["updated"])
        self.assertEqual(st3["sync_status"], "in_sync")

    def test_git_repository_escape_detection_refused(self) -> None:
        git_repo = self.tmp_root / "fake_git_repo"
        (git_repo / ".git").mkdir(parents=True, exist_ok=True)
        nested_job = git_repo / "sub" / "job1"

        rc, res, _ = self.run_dr([
            "init",
            "--job", str(nested_job),
            "--prompt", "Investigate distributed vector databases.",
            "--goal", "Must compare HNSW and DiskANN recall and latency.",
        ])
        self.assertEqual(rc, 5)
        self.assertEqual(res.get("code"), "GIT_REPO_ESCAPE")

        # Also test when AGY_DR_JOBS points inside a git repository
        rc2, res2, _ = self.run_dr(
            [
                "init",
                "--prompt", "Investigate distributed vector databases.",
                "--goal", "Must compare HNSW and DiskANN recall and latency.",
            ],
            extra_env={"AGY_DR_JOBS": str(git_repo / "jobs")},
        )
        self.assertEqual(rc2, 5)
        self.assertEqual(res2.get("code"), "GIT_REPO_ESCAPE")

    # ------------------------------------------------------------------ Tier 2: Pre-Plan & Plan Approval Gates
    def test_pre_plan_gate_requires_goal_and_prompt(self) -> None:
        # Missing goal on init
        rc, res, _ = self.run_dr(["init", "--prompt", "Some prompt", "--goal", "   "])
        self.assertEqual(rc, 5)
        self.assertEqual(res.get("code"), "EMPTY_GOAL")

        # Initialize properly, then delete goal.md before calling plan
        rc, init_res, _ = self.run_dr([
            "init",
            "--job", "gate_job_1",
            "--prompt", "Analyze HNSW indexing.",
            "--goal", "Cover memory footprint and p99 latency.",
        ])
        self.assertEqual(rc, 0)
        job_dir = Path(init_res["job_dir"])
        (job_dir / "goal.md").unlink()

        rc_plan, plan_res, _ = self.run_dr(["plan", "--job", "gate_job_1"])
        self.assertEqual(rc_plan, 2)
        self.assertEqual(plan_res.get("code"), "PRE_PLAN_GATE_GOAL_MISSING")

    def test_korean_sensitive_keyword_warning(self) -> None:
        rc, res, stderr = self.run_dr([
            "init",
            "--job", "kw_job",
            "--prompt", "한국의 현대 민주주의와 선거 제도의 데이터 분석",
            "--goal", "통계적 방법론 중심 정리",
        ])
        self.assertEqual(rc, 0)
        self.assertIn("민주주의", res["warnings"])
        self.assertIn("선거", res["warnings"])
        self.assertIn("WARNING", stderr)

    def test_approval_gate_rejects_unplanned_or_short_reason(self) -> None:
        rc, init_res, _ = self.run_dr([
            "init",
            "--job", "app_gate_job",
            "--prompt", "Vector search comparison.",
            "--goal", "Include DiskANN, HNSW, recall@10, and latency.",
        ])
        self.assertEqual(rc, 0)

        # 1. Approve before plan is ready -> fails with APPROVAL_GATE_INVALID_STATE
        rc_app1, res_app1, _ = self.run_dr([
            "approve",
            "--job", "app_gate_job",
            "--reason", "This reason is plenty long enough to pass the length check.",
        ])
        self.assertEqual(rc_app1, 2)
        self.assertEqual(res_app1.get("code"), "APPROVAL_GATE_INVALID_STATE")

        # 2. Generate plan
        rc_plan, _, _ = self.run_dr(["plan", "--job", "app_gate_job"])
        self.assertEqual(rc_plan, 0)

        # 3. Approve with short --reason (< 20 chars) -> fails with exit code 5
        rc_app2, res_app2, _ = self.run_dr([
            "approve",
            "--job", "app_gate_job",
            "--reason", "looks good",
        ])
        self.assertEqual(rc_app2, 5)
        self.assertEqual(res_app2.get("code"), "APPROVAL_GATE_REASON_TOO_SHORT")

        # 4. If job directory is later contaminated with a .git folder before approve -> rejected
        job_dir = Path(init_res["job_dir"])
        (job_dir / ".git").mkdir()
        rc_app3, res_app3, _ = self.run_dr([
            "approve",
            "--job", str(job_dir),
            "--reason", "The plan covers all required recall and latency metrics from goal.md.",
        ])
        self.assertEqual(rc_app3, 5)
        self.assertEqual(res_app3.get("code"), "GIT_REPO_ESCAPE")

    # ------------------------------------------------------------------ Tier 3 & 4: Full Lifecycle & Streaming
    def test_full_lifecycle_init_plan_edit_approve_stream_and_meta(self) -> None:
        report_out = self.out_dir / "vector_report.md"
        meta_out = Path(str(report_out) + ".meta.json")

        # Step 1: init
        rc, init_res, _ = self.run_dr([
            "init",
            "--job", "full_flow_01",
            "--prompt", "Compare Cloud-Native Vector Indexing architectures (HNSW vs DiskANN).",
            "--goal", "1. Compare Recall@10 and p99 latency. 2. Include IVF-PQ GPU numbers. 3. Cite primary sources.",
            "--out", str(report_out),
        ])
        self.assertEqual(rc, 0, init_res)
        job_dir = Path(init_res["job_dir"])
        self.assertTrue((job_dir / "prompt.md").is_file())
        self.assertTrue((job_dir / "goal.md").is_file())

        # Step 2: plan
        rc, plan_res, _ = self.run_dr(["plan", "--job", "full_flow_01"])
        self.assertEqual(rc, 0, plan_res)
        self.assertEqual(plan_res["status"], "plan_ready")
        self.assertEqual(plan_res["plan_iterations"], 1)
        self.assertIn("DiskANN", plan_res["plan"])
        self.assertTrue((job_dir / "plan_v1.md").is_file())

        # Step 3: edit
        rc, edit_res, _ = self.run_dr([
            "edit",
            "--job", "full_flow_01",
            "--feedback", "IVF-PQ GPU 가속 인덱싱과의 비교 항목을 추가해 주세요.",
        ])
        self.assertEqual(rc, 0, edit_res)
        self.assertEqual(edit_res["status"], "plan_revised")
        self.assertEqual(edit_res["plan_iterations"], 2)
        self.assertEqual(edit_res["plan_count"], 2)
        self.assertIn("IVF-PQ", edit_res["plan"])
        self.assertTrue((job_dir / "plan_v2.md").is_file())

        # Step 4: approve (detached worker + live progress.jsonl tailing to stderr + report & .meta.json)
        reason_str = "The revised v2 plan covers HNSW, DiskANN, IVF-PQ, recall@10, p99 latency, and citations per goal.md."
        rc, app_res, stderr = self.run_dr([
            "approve",
            "--job", "full_flow_01",
            "--reason", reason_str,
            "--out", str(report_out),
        ])
        self.assertEqual(rc, 0, f"approve failed: {app_res}\nstderr: {stderr}")
        self.assertEqual(app_res["status"], "completed")
        self.assertGreaterEqual(app_res["report_chars"], 400)
        self.assertEqual(app_res["sources_count"], 3)
        self.assertEqual(app_res["plan_iterations"], 2)

        # Verify live progress was streamed to stderr
        self.assertIn("[progress 12s]", stderr)
        self.assertIn("[progress 45s]", stderr)
        self.assertIn("[progress 90s]", stderr)

        # Verify deliverables on disk
        self.assertTrue(report_out.is_file())
        self.assertTrue(meta_out.is_file())
        meta = json.loads(meta_out.read_text(encoding="utf-8"))
        self.assertEqual(meta["job_id"], "full_flow_01")
        self.assertEqual(meta["plan_iterations"], 2)
        self.assertEqual(meta["approval_reason"], reason_str)
        self.assertGreaterEqual(meta["report_chars"], 400)
        self.assertEqual(meta["sources_count"], 3)
        self.assertEqual(len(meta["progress_highlights"]), 3)
        self.assertTrue(meta["confinement"]["enabled"])
        self.assertEqual(meta["agy_model"], "flash 3.8")
        self.assertEqual(meta["agy_effort"], "high")
        self.assertEqual(meta["gemini_model"], "Pro")
        self.assertEqual(meta["gemini_thinking"], "high")

    def test_approve_no_wait_status_and_collect_reattach(self) -> None:
        report_out = self.out_dir / "async_report.md"
        self.run_dr([
            "init",
            "--job", "async_job",
            "--prompt", "Distributed vector indexing deep dive.",
            "--goal", "Detailed latency and memory comparison.",
        ])
        self.run_dr(["plan", "--job", "async_job"])

        # Launch with --no-wait under long_running scenario
        rc, start_res, _ = self.run_dr(
            [
                "approve",
                "--job", "async_job",
                "--reason", "Plan covers all required vector database latency and memory metrics.",
                "--no-wait",
                "--out", str(report_out),
            ],
            scenario="long_running",
        )
        self.assertEqual(rc, 0, start_res)
        self.assertEqual(start_res["status"], "started")
        self.assertIsNotNone(start_res["pid"])

        # Immediately check status while background worker is alive
        rc_st, st_res, _ = self.run_dr(["status", "--job", "async_job"], scenario="long_running")
        self.assertEqual(rc_st, 0, st_res)
        self.assertIn(st_res["status"], ("running", "ready_to_collect"))

        # Now run collect (blocks and streams remaining progress until completion)
        rc_col, col_res, stderr = self.run_dr(
            ["collect", "--job", "async_job", "--out", str(report_out)],
            scenario="long_running",
        )
        self.assertEqual(rc_col, 0, f"collect failed: {col_res}\nstderr: {stderr}")
        self.assertEqual(col_res["status"], "completed")
        self.assertTrue(report_out.is_file())
        self.assertTrue(Path(str(report_out) + ".meta.json").is_file())

    def test_unattended_auto_mode(self) -> None:
        report_out = self.out_dir / "auto_report.md"
        rc, auto_res, stderr = self.run_dr([
            "auto",
            "--job", "auto_job_01",
            "--prompt", "Compare HNSW and DiskANN vector search engines.",
            "--goal", "Recall@10, p99 latency, and memory usage.",
            "--out", str(report_out),
        ])
        self.assertEqual(rc, 0, auto_res)
        self.assertEqual(auto_res["status"], "completed")
        self.assertIn("[progress 12s]", stderr)
        self.assertTrue(report_out.is_file())
        meta = json.loads(Path(str(report_out) + ".meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["job_id"], "auto_job_01")
        self.assertGreaterEqual(meta["report_chars"], 400)
        self.assertEqual(meta["agy_model"], "flash 3.8")
        self.assertEqual(meta["agy_effort"], "high")
        self.assertEqual(meta["gemini_model"], "Pro")

    # ------------------------------------------------------------------ Tier 2 & 5: Error & Gate Rejections
    def test_report_quality_gate_rejects_short_or_headingless_reports(self) -> None:
        # 1. Short report (< 400 chars)
        self.run_dr([
            "init", "--job", "short_rep_job",
            "--prompt", "Vector search.", "--goal", "Benchmarks and memory.",
        ])
        self.run_dr(["plan", "--job", "short_rep_job"])
        rc, res, _ = self.run_dr(
            [
                "approve", "--job", "short_rep_job",
                "--reason", "The generated plan satisfies all benchmark requirements in goal.md.",
            ],
            scenario="short_report",
        )
        self.assertEqual(rc, 2)
        self.assertEqual(res.get("code"), "REPORT_QUALITY_GATE_FAILED")

        # 2. Report >= 400 chars but lacking any Markdown heading (# ...)
        self.run_dr([
            "init", "--job", "no_head_job",
            "--prompt", "Vector search.", "--goal", "Benchmarks and memory.",
        ])
        self.run_dr(["plan", "--job", "no_head_job"])
        rc2, res2, _ = self.run_dr(
            [
                "approve", "--job", "no_head_job",
                "--reason", "The generated plan satisfies all benchmark requirements in goal.md.",
            ],
            scenario="no_heading_report",
        )
        self.assertEqual(rc2, 2)
        self.assertEqual(res2.get("code"), "REPORT_STRUCTURE_INVALID")

    def test_collect_would_start_and_error_propagation(self) -> None:
        self.run_dr([
            "init", "--job", "err_job",
            "--prompt", "Vector search.", "--goal", "Benchmarks and memory.",
        ])

        # Safety blocked on plan
        rc_safe, res_safe, _ = self.run_dr(["plan", "--job", "err_job"], scenario="safety")
        self.assertEqual(rc_safe, 2)
        self.assertEqual(res_safe.get("code"), "SAFETY_BLOCKED")

        # Plan timeout on plan
        rc_to, res_to, _ = self.run_dr(["plan", "--job", "err_job"], scenario="plan_timeout")
        self.assertEqual(rc_to, 2)
        self.assertEqual(res_to.get("code"), "PLAN_TIMEOUT")

        # Now generate a valid plan, then try `collect` before `approve` under collect_would_start
        self.run_dr(["plan", "--job", "err_job"], scenario="perfect")
        rc_col, res_col, _ = self.run_dr(["collect", "--job", "err_job"], scenario="collect_would_start")
        self.assertEqual(rc_col, 2)
        self.assertEqual(res_col.get("code"), "COLLECT_WOULD_START")

        # Plan changed on approve
        rc_pc, res_pc, _ = self.run_dr(
            [
                "approve", "--job", "err_job",
                "--reason", "Plan covers all required benchmark metrics in goal.md.",
            ],
            scenario="plan_changed",
        )
        self.assertEqual(rc_pc, 2)
        self.assertEqual(res_pc.get("code"), "PLAN_CHANGED")

    def test_dead_worker_detection(self) -> None:
        self.run_dr([
            "init", "--job", "crash_job",
            "--prompt", "Vector search.", "--goal", "Benchmarks and memory.",
        ])
        self.run_dr(["plan", "--job", "crash_job"])
        rc, res, _ = self.run_dr(
            [
                "approve", "--job", "crash_job",
                "--reason", "Plan covers all required benchmark metrics in goal.md.",
            ],
            scenario="worker_crash",
        )
        self.assertEqual(rc, 3)
        self.assertEqual(res.get("code"), "WORKER_DIED")


# ------------------------------------------------------------------ Model & Effort Configuration Tests (R1 - R4)

class ModelAndEffortNormalizationTestCase(unittest.TestCase):
    """Direct unit tests for normalizer functions and friendly alias resolution (R1)."""

    def test_normalize_agy_model_defaults_and_aliases(self) -> None:
        # Defaults for None and blank/whitespace
        self.assertEqual(normalize_agy_model(None), DEFAULT_AGY_MODEL)
        self.assertEqual(normalize_agy_model(""), DEFAULT_AGY_MODEL)
        self.assertEqual(normalize_agy_model("   "), DEFAULT_AGY_MODEL)

        # Flash aliases (case-insensitive and whitespace-stripped)
        flash_aliases = [
            "flash",
            "flash 3.8",
            "flash-3.8",
            "flash3.8",
            "3.8 flash",
            "3.8-flash",
            "플래시",
            "FLASH",
            "Flash 3.8",
            " 3.8 flash ",
            " 플래시 ",
        ]
        for alias in flash_aliases:
            with self.subTest(alias=alias):
                self.assertEqual(normalize_agy_model(alias), "flash 3.8")

        # Pro aliases
        pro_aliases = [
            "pro",
            "pro 3.8",
            "pro-3.8",
            "pro3.8",
            "3.8 pro",
            "프로",
            "PRO",
            "Pro 3.8",
            " 프로 ",
        ]
        for alias in pro_aliases:
            with self.subTest(alias=alias):
                self.assertEqual(normalize_agy_model(alias), "pro")

        # Custom slugs preserved as-is
        custom_slugs = [
            "gemini-2.5-flash",
            "custom-slug-abc",
            "claude-3-7-sonnet",
            "deepseek-r1",
        ]
        for slug in custom_slugs:
            with self.subTest(slug=slug):
                self.assertEqual(normalize_agy_model(slug), slug)

    def test_normalize_agy_effort_defaults_and_aliases(self) -> None:
        # Defaults for None and blank/whitespace
        self.assertEqual(normalize_agy_effort(None), DEFAULT_AGY_EFFORT)
        self.assertEqual(normalize_agy_effort(""), DEFAULT_AGY_EFFORT)
        self.assertEqual(normalize_agy_effort("   "), DEFAULT_AGY_EFFORT)

        # High aliases
        high_aliases = ["high", "높음", "h", "최대", "hi", "HIGH", " 높음 ", "H"]
        for alias in high_aliases:
            with self.subTest(alias=alias):
                self.assertEqual(normalize_agy_effort(alias), "high")

        # Medium aliases
        med_aliases = ["medium", "중간", "보통", "m", "med", "mid", "MEDIUM", " 중간 ", " 보통 "]
        for alias in med_aliases:
            with self.subTest(alias=alias):
                self.assertEqual(normalize_agy_effort(alias), "medium")

        # Low aliases
        low_aliases = ["low", "낮음", "l", "lo", "LOW", " 낮음 ", "L"]
        for alias in low_aliases:
            with self.subTest(alias=alias):
                self.assertEqual(normalize_agy_effort(alias), "low")

        # Invalid effort values rejected with exit code 5 (die suppresses output during unit test)
        invalid_values = ["ultra", "extreme", "none", "123", "최저", "최고"]
        for invalid in invalid_values:
            with self.subTest(invalid=invalid):
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as cm:
                        normalize_agy_effort(invalid)
                self.assertEqual(cm.exception.code, 5)

    def test_normalize_gemini_model_defaults_and_aliases(self) -> None:
        # Defaults for None and blank/whitespace
        self.assertEqual(normalize_gemini_model(None), (DEFAULT_GEMINI_MODEL, DEFAULT_GEMINI_THINKING))
        self.assertEqual(normalize_gemini_model(""), (DEFAULT_GEMINI_MODEL, DEFAULT_GEMINI_THINKING))
        self.assertEqual(normalize_gemini_model("   "), (DEFAULT_GEMINI_MODEL, DEFAULT_GEMINI_THINKING))

        # Pro / High aliases -> ("Pro", "high")
        pro_aliases = [
            "pro",
            "3.8 pro",
            "pro 3.8",
            "high",
            "높음",
            "pro high",
            "pro-high",
            "pro (높음)",
            "프로",
            "advanced",
            "PRO",
            " 높음 ",
            "PRO HIGH",
        ]
        for alias in pro_aliases:
            with self.subTest(alias=alias):
                self.assertEqual(normalize_gemini_model(alias), ("Pro", "high"))

        # Flash aliases -> ("Flash", "high")
        flash_aliases = [
            "flash",
            "flash 3.8",
            "flash-3.8",
            "flash3.8",
            "3.8 flash",
            "플래시",
            "FLASH",
            " 플래시 ",
        ]
        for alias in flash_aliases:
            with self.subTest(alias=alias):
                self.assertEqual(normalize_gemini_model(alias), ("Flash", "high"))

        # Argon aliases -> ("Argon", "high")
        argon_aliases = ["argon", "아르곤", "ARGON", " 아르곤 "]
        for alias in argon_aliases:
            with self.subTest(alias=alias):
                self.assertEqual(normalize_gemini_model(alias), ("Argon", "high"))

        # Custom slugs -> (slug, "high")
        custom_slugs = ["gemini-1.5-pro", "gemini-2.0-flash-exp", "custom-preview"]
        for slug in custom_slugs:
            with self.subTest(slug=slug):
                self.assertEqual(normalize_gemini_model(slug), (slug, "high"))


class ModelAndEffortCliOptionsTestCase(DeepResearchTestBase):
    """Verify CLI options, defaults, and override behaviors across all subcommands (R1, R2)."""

    def test_init_subcommand_defaults_and_overrides(self) -> None:
        # 1. Defaults when CLI flags omitted
        rc_def, res_def, _ = self.run_dr([
            "init",
            "--job", "job_cli_def",
            "--prompt", "Analyze database query planners.",
            "--goal", "Compare cost-based and rule-based optimizers.",
        ])
        self.assertEqual(rc_def, 0)
        self.assertEqual(res_def["agy_model"], "flash 3.8")
        self.assertEqual(res_def["agy_effort"], "high")
        self.assertEqual(res_def["gemini_model"], "Pro")
        self.assertEqual(res_def["gemini_thinking"], "high")

        job_file = self.jobs_dir / "job_cli_def" / "job.json"
        job_data = json.loads(job_file.read_text(encoding="utf-8"))
        self.assertEqual(job_data["agy_model"], "flash 3.8")
        self.assertEqual(job_data["agy_effort"], "high")
        self.assertEqual(job_data["gemini_model"], "Pro")
        self.assertEqual(job_data["gemini_thinking"], "high")

        # 2. Explicit CLI overrides
        rc_ovr, res_ovr, _ = self.run_dr([
            "init",
            "--job", "job_cli_ovr",
            "--prompt", "Analyze database query planners.",
            "--goal", "Compare cost-based and rule-based optimizers.",
            "--agy-model", "pro",
            "--agy-effort", "medium",
            "--gemini-model", "flash",
        ])
        self.assertEqual(rc_ovr, 0)
        self.assertEqual(res_ovr["agy_model"], "pro")
        self.assertEqual(res_ovr["agy_effort"], "medium")
        self.assertEqual(res_ovr["gemini_model"], "Flash")
        self.assertEqual(res_ovr["gemini_thinking"], "high")

        job_file_ovr = self.jobs_dir / "job_cli_ovr" / "job.json"
        job_data_ovr = json.loads(job_file_ovr.read_text(encoding="utf-8"))
        self.assertEqual(job_data_ovr["agy_model"], "pro")
        self.assertEqual(job_data_ovr["agy_effort"], "medium")
        self.assertEqual(job_data_ovr["gemini_model"], "Flash")

        # 3. Friendly Korean aliases on CLI
        rc_ko, res_ko, _ = self.run_dr([
            "init",
            "--job", "job_cli_ko",
            "--prompt", "Analyze database query planners.",
            "--goal", "Compare cost-based and rule-based optimizers.",
            "--agy-model", "플래시",
            "--agy-effort", "낮음",
            "--gemini-model", "아르곤",
        ])
        self.assertEqual(rc_ko, 0)
        self.assertEqual(res_ko["agy_model"], "flash 3.8")
        self.assertEqual(res_ko["agy_effort"], "low")
        self.assertEqual(res_ko["gemini_model"], "Argon")

        # 4. Invalid effort flag rejected with exit code 5
        rc_bad, res_bad, _ = self.run_dr([
            "init",
            "--job", "job_cli_bad",
            "--prompt", "Analyze database query planners.",
            "--goal", "Compare cost-based and rule-based optimizers.",
            "--agy-effort", "super-effort",
        ])
        self.assertEqual(rc_bad, 5)
        self.assertEqual(res_bad.get("code"), "INVALID_EFFORT")

    def test_subcommands_cli_options_propagation_plan_edit_approve(self) -> None:
        # Initialize with defaults
        self.run_dr([
            "init",
            "--job", "flow_subcommands",
            "--prompt", "Distributed lock manager comparison.",
            "--goal", "Raft vs Paxos throughput, latency, and fault recovery.",
        ])

        # Step 1: plan with explicit CLI overrides
        rc_plan, _, _ = self.run_dr([
            "plan",
            "--job", "flow_subcommands",
            "--agy-model", "pro",
            "--agy-effort", "low",
            "--gemini-model", "flash",
        ])
        self.assertEqual(rc_plan, 0)

        job_dir = self.jobs_dir / "flow_subcommands"
        relay_dir = job_dir / "relay"

        # Verify relay/request.json
        req_plan = json.loads((relay_dir / "request.json").read_text(encoding="utf-8"))
        self.assertEqual(req_plan["gemini_model"], "Flash")
        self.assertEqual(req_plan["gemini_thinking"], "high")
        self.assertEqual(req_plan["env"]["GEMINI_DR_MODE"], "Flash")
        self.assertEqual(req_plan["env"]["GEMINI_DR_THINKING"], "high")

        # Verify relay/agy_invoked.json
        inv_plan = json.loads((relay_dir / "agy_invoked.json").read_text(encoding="utf-8"))
        self.assertEqual(inv_plan["model"], "pro")
        self.assertEqual(inv_plan["effort"], "low")
        self.assertIn("--model", inv_plan["argv"])
        self.assertEqual(inv_plan["argv"][inv_plan["argv"].index("--model") + 1], "pro")
        self.assertIn("--effort", inv_plan["argv"])
        self.assertEqual(inv_plan["argv"][inv_plan["argv"].index("--effort") + 1], "low")

        # Step 2: edit with partial CLI override (change effort to medium)
        rc_edit, _, _ = self.run_dr([
            "edit",
            "--job", "flow_subcommands",
            "--feedback", "Add Zab protocol benchmarks.",
            "--agy-effort", "medium",
        ])
        self.assertEqual(rc_edit, 0)

        req_edit = json.loads((relay_dir / "request.json").read_text(encoding="utf-8"))
        # Inherited from previous step
        self.assertEqual(req_edit["gemini_model"], "Flash")
        inv_edit = json.loads((relay_dir / "agy_invoked.json").read_text(encoding="utf-8"))
        self.assertEqual(inv_edit["model"], "pro")
        self.assertEqual(inv_edit["effort"], "medium")

        # Step 3: approve with model override (switch gemini to argon)
        report_out = self.out_dir / "flow_report.md"
        reason_str = "Plan thoroughly benchmarks Raft, Paxos, and Zab fault recovery as required."
        rc_app, app_res, _ = self.run_dr([
            "approve",
            "--job", "flow_subcommands",
            "--reason", reason_str,
            "--gemini-model", "argon",
            "--out", str(report_out),
        ])
        self.assertEqual(rc_app, 0)
        self.assertEqual(app_res["agy_model"], "pro")
        self.assertEqual(app_res["agy_effort"], "medium")
        self.assertEqual(app_res["gemini_model"], "Argon")

        # Verify deliverables and .meta.json
        meta_out = Path(str(report_out) + ".meta.json")
        self.assertTrue(meta_out.is_file())
        meta = json.loads(meta_out.read_text(encoding="utf-8"))
        self.assertEqual(meta["agy_model"], "pro")
        self.assertEqual(meta["agy_effort"], "medium")
        self.assertEqual(meta["gemini_model"], "Argon")
        self.assertEqual(meta["gemini_thinking"], "high")

    def test_auto_subcommand_defaults_and_explicit_options(self) -> None:
        # 1. auto with defaults
        out_def = self.out_dir / "auto_def.md"
        rc_def, res_def, _ = self.run_dr([
            "auto",
            "--job", "auto_def_job",
            "--prompt", "Compare log-structured merge trees and B-trees.",
            "--goal", "Read amplification, write amplification, and space amplification.",
            "--out", str(out_def),
        ])
        self.assertEqual(rc_def, 0)
        self.assertEqual(res_def["agy_model"], "flash 3.8")
        self.assertEqual(res_def["agy_effort"], "high")
        self.assertEqual(res_def["gemini_model"], "Pro")

        job_dir_def = self.jobs_dir / "auto_def_job"
        inv_def = json.loads((job_dir_def / "relay" / "agy_invoked.json").read_text(encoding="utf-8"))
        self.assertEqual(inv_def["model"], "flash 3.8")
        self.assertEqual(inv_def["effort"], "high")

        req_def = json.loads((job_dir_def / "relay" / "request.json").read_text(encoding="utf-8"))
        self.assertEqual(req_def["gemini_model"], "Pro")
        self.assertEqual(req_def["env"]["GEMINI_DR_MODE"], "Pro")

        meta_def = json.loads(Path(str(out_def) + ".meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta_def["agy_model"], "flash 3.8")
        self.assertEqual(meta_def["agy_effort"], "high")
        self.assertEqual(meta_def["gemini_model"], "Pro")

        # 2. auto with explicit overrides and friendly aliases
        out_ovr = self.out_dir / "auto_ovr.md"
        rc_ovr, res_ovr, _ = self.run_dr([
            "auto",
            "--job", "auto_ovr_job",
            "--prompt", "Compare log-structured merge trees and B-trees.",
            "--goal", "Read amplification, write amplification, and space amplification.",
            "--agy-model", "프로",
            "--agy-effort", "낮음",
            "--gemini-model", "flash",
            "--out", str(out_ovr),
        ])
        self.assertEqual(rc_ovr, 0)
        self.assertEqual(res_ovr["agy_model"], "pro")
        self.assertEqual(res_ovr["agy_effort"], "low")
        self.assertEqual(res_ovr["gemini_model"], "Flash")

        job_dir_ovr = self.jobs_dir / "auto_ovr_job"
        inv_ovr = json.loads((job_dir_ovr / "relay" / "agy_invoked.json").read_text(encoding="utf-8"))
        self.assertEqual(inv_ovr["model"], "pro")
        self.assertEqual(inv_ovr["effort"], "low")

        req_ovr = json.loads((job_dir_ovr / "relay" / "request.json").read_text(encoding="utf-8"))
        self.assertEqual(req_ovr["gemini_model"], "Flash")
        self.assertEqual(req_ovr["env"]["GEMINI_DR_MODE"], "Flash")

        meta_ovr = json.loads(Path(str(out_ovr) + ".meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta_ovr["agy_model"], "pro")
        self.assertEqual(meta_ovr["agy_effort"], "low")
        self.assertEqual(meta_ovr["gemini_model"], "Flash")

    def test_collect_subcommand_model_options_propagation(self) -> None:
        # Initialize job
        self.run_dr([
            "init",
            "--job", "collect_model_job",
            "--prompt", "Evaluate consensus protocols.",
            "--goal", "Raft vs PBFT throughput comparison.",
        ])

        # Run collect in relay mode (Case 3) with explicit overrides; the job never planned, so it is bound to its
        # conversation explicitly (collect refuses to fall back to whatever conversation the app shows)
        report_out = self.out_dir / "collect_report.md"
        rc_col, res_col, _ = self.run_dr([
            "collect",
            "--job", "collect_model_job",
            "--url", "https://gemini.google.com/app/fake_conv_001",
            "--agy-model", "pro",
            "--agy-effort", "medium",
            "--gemini-model", "argon",
            "--out", str(report_out),
        ])
        self.assertEqual(rc_col, 0)
        self.assertEqual(res_col["status"], "completed")
        self.assertEqual(res_col["agy_model"], "pro")
        self.assertEqual(res_col["agy_effort"], "medium")
        self.assertEqual(res_col["gemini_model"], "Argon")

        job_dir = self.jobs_dir / "collect_model_job"
        inv_col = json.loads((job_dir / "relay" / "agy_invoked.json").read_text(encoding="utf-8"))
        self.assertEqual(inv_col["model"], "pro")
        self.assertEqual(inv_col["effort"], "medium")

        req_col = json.loads((job_dir / "relay" / "request.json").read_text(encoding="utf-8"))
        self.assertEqual(req_col["gemini_model"], "Argon")
        self.assertEqual(req_col["env"]["GEMINI_DR_MODE"], "Argon")

        meta_col = json.loads(Path(str(report_out) + ".meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta_col["agy_model"], "pro")
        self.assertEqual(meta_col["agy_effort"], "medium")
        self.assertEqual(meta_col["gemini_model"], "Argon")


class ModelAndEffortEnvironmentVariablesTestCase(DeepResearchTestBase):
    """Verify environment variable fallbacks and CLI override precedence (R1)."""

    def test_env_vars_fallback_when_cli_omitted(self) -> None:
        # Test fallback with AGY_MODEL, AGY_EFFORT, GEMINI_MODEL, GEMINI_DR_THINKING
        extra_env = {
            "AGY_MODEL": "pro",
            "AGY_EFFORT": "medium",
            "GEMINI_MODEL": "flash",
            "GEMINI_DR_THINKING": "low",
        }
        rc, res, _ = self.run_dr(
            [
                "init",
                "--job", "job_env_test_1",
                "--prompt", "Investigate distributed tracing.",
                "--goal", "Compare OpenTelemetry and Jaeger sampling strategies.",
            ],
            extra_env=extra_env,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(res["agy_model"], "pro")
        self.assertEqual(res["agy_effort"], "medium")
        self.assertEqual(res["gemini_model"], "Flash")
        self.assertEqual(res["gemini_thinking"], "low")

        job_file = self.jobs_dir / "job_env_test_1" / "job.json"
        job_data = json.loads(job_file.read_text(encoding="utf-8"))
        self.assertEqual(job_data["agy_model"], "pro")
        self.assertEqual(job_data["agy_effort"], "medium")
        self.assertEqual(job_data["gemini_model"], "Flash")
        self.assertEqual(job_data["gemini_thinking"], "low")

    def test_alternate_env_vars_fallback(self) -> None:
        # Test fallback with AGY_TASK_MODEL, AGY_TASK_EFFORT, GEMINI_DR_MODE
        extra_env = {
            "AGY_TASK_MODEL": "pro",
            "AGY_TASK_EFFORT": "low",
            "GEMINI_DR_MODE": "argon",
        }
        rc, res, _ = self.run_dr(
            [
                "init",
                "--job", "job_env_test_2",
                "--prompt", "Investigate distributed tracing.",
                "--goal", "Compare OpenTelemetry and Jaeger sampling strategies.",
            ],
            extra_env=extra_env,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(res["agy_model"], "pro")
        self.assertEqual(res["agy_effort"], "low")
        self.assertEqual(res["gemini_model"], "Argon")

    def test_cli_options_override_environment_variables(self) -> None:
        # Environment variables set to values, but CLI provides different values
        extra_env = {
            "AGY_MODEL": "flash 3.8",
            "AGY_EFFORT": "low",
            "GEMINI_MODEL": "flash",
        }
        rc, res, _ = self.run_dr(
            [
                "init",
                "--job", "job_env_override",
                "--prompt", "Investigate distributed tracing.",
                "--goal", "Compare OpenTelemetry and Jaeger sampling strategies.",
                "--agy-model", "pro",
                "--agy-effort", "high",
                "--gemini-model", "pro",
            ],
            extra_env=extra_env,
        )
        self.assertEqual(rc, 0)
        # CLI overrides env vars
        self.assertEqual(res["agy_model"], "pro")
        self.assertEqual(res["agy_effort"], "high")
        self.assertEqual(res["gemini_model"], "Pro")


class ModelAndEffortPersistenceAndPropagationTestCase(DeepResearchTestBase):
    """Verify multi-turn job persistence and execution chain propagation (R2)."""

    def test_multi_turn_persistence_and_meta_deliverable(self) -> None:
        report_out = self.out_dir / "persistent_report.md"

        # 1. init with custom model choices
        rc_init, _, _ = self.run_dr([
            "init",
            "--job", "multi_turn_job",
            "--prompt", "Multi-version concurrency control in modern databases.",
            "--goal", "Compare PostgreSQL tuple visibility and MySQL undo log approaches.",
            "--agy-model", "flash",
            "--agy-effort", "high",
            "--gemini-model", "pro",
            "--out", str(report_out),
        ])
        self.assertEqual(rc_init, 0)

        job_dir = self.jobs_dir / "multi_turn_job"
        job_json_1 = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
        self.assertEqual(job_json_1["agy_model"], "flash 3.8")
        self.assertEqual(job_json_1["agy_effort"], "high")
        self.assertEqual(job_json_1["gemini_model"], "Pro")

        # 2. plan with NO model options -> persists original choices
        rc_plan, _, _ = self.run_dr(["plan", "--job", "multi_turn_job"])
        self.assertEqual(rc_plan, 0)

        job_json_2 = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
        self.assertEqual(job_json_2["agy_model"], "flash 3.8")
        self.assertEqual(job_json_2["agy_effort"], "high")
        self.assertEqual(job_json_2["gemini_model"], "Pro")

        # 3. edit with effort change -> updates only effort, maintains model
        rc_edit, _, _ = self.run_dr([
            "edit",
            "--job", "multi_turn_job",
            "--feedback", "Add CockroachDB latching and hybrid-logical-clock details.",
            "--agy-effort", "medium",
        ])
        self.assertEqual(rc_edit, 0)

        job_json_3 = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
        self.assertEqual(job_json_3["agy_model"], "flash 3.8")
        self.assertEqual(job_json_3["agy_effort"], "medium")
        self.assertEqual(job_json_3["gemini_model"], "Pro")

        # 4. approve and stream to finalization
        reason_str = "Plan satisfies all concurrency control review criteria across Postgres, MySQL, and CockroachDB."
        rc_app, _, _ = self.run_dr([
            "approve",
            "--job", "multi_turn_job",
            "--reason", reason_str,
            "--out", str(report_out),
        ])
        self.assertEqual(rc_app, 0)

        # 5. Verify final .meta.json reflects complete lifecycle state
        meta_path = Path(str(report_out) + ".meta.json")
        self.assertTrue(meta_path.is_file())
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        self.assertEqual(meta["agy_model"], "flash 3.8")
        self.assertEqual(meta["agy_effort"], "medium")
        self.assertEqual(meta["gemini_model"], "Pro")
        self.assertEqual(meta["gemini_thinking"], "high")

    def test_relay_command_line_and_request_env_structure(self) -> None:
        self.run_dr([
            "init",
            "--job", "chain_test_job",
            "--prompt", "Hardware transactional memory analysis.",
            "--goal", "Intel TSX vs ARM TME performance and abort rates.",
        ])

        rc_plan, _, _ = self.run_dr([
            "plan",
            "--job", "chain_test_job",
            "--agy-model", "pro",
            "--agy-effort", "low",
            "--gemini-model", "argon",
        ])
        self.assertEqual(rc_plan, 0)

        job_dir = self.jobs_dir / "chain_test_job"
        relay_dir = job_dir / "relay"

        # Check relay/request.json has exact keys: gemini_model, gemini_thinking, env with GEMINI_DR_MODE and GEMINI_DR_THINKING
        req = json.loads((relay_dir / "request.json").read_text(encoding="utf-8"))
        self.assertEqual(req["gemini_model"], "Argon")
        self.assertEqual(req["gemini_thinking"], "high")
        self.assertIn("env", req)
        self.assertEqual(req["env"]["GEMINI_DR_MODE"], "Argon")
        self.assertEqual(req["env"]["GEMINI_DR_THINKING"], "high")

        # Check relay/agy_invoked.json recorded exact args passed by invoke_agy_relay
        inv = json.loads((relay_dir / "agy_invoked.json").read_text(encoding="utf-8"))
        self.assertEqual(inv["model"], "pro")
        self.assertEqual(inv["effort"], "low")

        argv = inv["argv"]
        # In agy_task command structure:
        # sys.executable, agy_task, "--model", agy_model, "run", "--effort", agy_effort, "--deep-research", ...
        self.assertIn("--model", argv)
        model_idx = argv.index("--model")
        self.assertEqual(argv[model_idx + 1], "pro")

        self.assertIn("run", argv)
        run_idx = argv.index("run")
        self.assertIn("--effort", argv)
        effort_idx = argv.index("--effort")
        self.assertGreater(effort_idx, run_idx)
        self.assertEqual(argv[effort_idx + 1], "low")
        self.assertIn("--deep-research", argv)


class PromptEngineeringAndGatesTestCase(DeepResearchTestBase):
    """Verify Dual-File Separation, 5-Block XML Composition, Imperative Edit Feedback, and Argon Timeouts."""

    def test_compose_prompt_subcommand_generates_5_xml_blocks_and_goal_rubric(self) -> None:
        prompt_out = self.out_dir / "composed_prompt.md"
        goal_out = self.out_dir / "composed_goal.md"

        rc, res, _ = self.run_dr([
            "compose-prompt",
            "--objective", "Compare HNSW, DiskANN, and IVF-PQ indexing architectures for 1B-scale vector search.",
            "--goal", "Verify coverage of recall@10 vs memory footprint and NVMe IOPS bottlenecks.",
            "--context-scope", "Rust storage engine (`struct VectorSegment { dim: u32, max_Degree: u16 }`).",
            "--axes", "1. Memory overhead per vector.\n2. NVMe SSD I/O amplification under DiskANN.",
            "--sources-policy", "Prefer VLDB/SIGMOD 2025-2026 papers and official Rust implementations.",
            "--output-spec", "Include a Markdown comparison table and a fenced ```json benchmark summary block.",
            "--workspace-sources", "Google Drive folder 'VectorEngine-2026'",
            "--entities", "HNSW, DiskANN, IVF-PQ",
            "--metrics", "Recall@10, QPS, Memory MB/1M vectors",
            "--prompt-out", str(prompt_out),
            "--goal-out", str(goal_out),
        ])
        self.assertEqual(rc, 0)
        self.assertEqual(res["status"], "ok")
        self.assertTrue(res["xml_structured"])
        self.assertTrue(prompt_out.is_file())
        self.assertTrue(goal_out.is_file())

        prompt_text = prompt_out.read_text(encoding="utf-8")
        for tag in (
            "research_objective",
            "context_and_scope",
            "investigation_axes",
            "source_governance",
            "output_format",
        ):
            self.assertIn(f"<{tag}>", prompt_text)
            self.assertIn(f"</{tag}>", prompt_text)
        self.assertIn("VectorEngine-2026", prompt_text)
        self.assertIn("검증 가능한 공개 근거 불충분", prompt_text)

        goal_text = goal_out.read_text(encoding="utf-8")
        self.assertIn("# Fixed Acceptance Criteria & Plan Review Rubric (goal.md)", goal_text)
        self.assertIn("HNSW, DiskANN, IVF-PQ", goal_text)
        self.assertIn("Recall@10, QPS, Memory MB/1M vectors", goal_text)
        self.assertNotEqual(prompt_text.strip(), goal_text.strip())

    def test_identical_prompt_and_goal_rejected_with_exit_code_5(self) -> None:
        same_text = "Analyze distributed vector database indexing trade-offs."
        rc, res, _ = self.run_dr([
            "init",
            "--job", "identical_job",
            "--prompt", same_text,
            "--goal", same_text,
        ])
        self.assertEqual(rc, 5)
        self.assertEqual(res.get("code"), "IDENTICAL_PROMPT_AND_GOAL")

    def test_strict_xml_rejects_unstructured_prompt_and_accepts_composed_xml(self) -> None:
        # 1. Unstructured prompt with --strict-xml -> rejected (exit 5)
        rc_bad, res_bad, _ = self.run_dr([
            "init",
            "--job", "strict_xml_bad",
            "--prompt", "Plain unstructured prompt about vector search.",
            "--goal", "Must compare HNSW and DiskANN latency.",
            "--strict-xml",
        ])
        self.assertEqual(rc_bad, 5)
        self.assertEqual(res_bad.get("code"), "PROMPT_XML_STRUCTURE_INVALID")
        self.assertEqual(len(res_bad.get("missing_xml_tags", [])), 5)

        # 2. --compose-xml + --strict-xml -> auto-wraps and succeeds (exit 0)
        rc_ok, res_ok, _ = self.run_dr([
            "init",
            "--job", "strict_xml_ok",
            "--prompt", "Compare HNSW and DiskANN indexing architectures.",
            "--goal", "Verify HNSW vs DiskANN memory footprint and p99 latency.",
            "--compose-xml",
            "--strict-xml",
            "--workspace-sources", "Drive folder 'ArchSpecs'",
        ])
        self.assertEqual(rc_ok, 0)
        self.assertTrue(res_ok["xml_structured"])
        self.assertEqual(res_ok["missing_xml_tags"], [])

        saved_prompt = (self.jobs_dir / "strict_xml_ok" / "prompt.md").read_text(encoding="utf-8")
        self.assertIn("<research_objective>", saved_prompt)
        self.assertIn("ArchSpecs", saved_prompt)

    def test_edit_feedback_interrogative_rejected_and_allow_questions_override(self) -> None:
        self.run_dr([
            "init",
            "--job", "edit_gate_job",
            "--prompt", "Analyze LSM-tree compaction strategies.",
            "--goal", "Compare leveled vs universal compaction write amplification.",
            "--compose-xml",
        ])
        rc_plan, _, _ = self.run_dr(["plan", "--job", "edit_gate_job"])
        self.assertEqual(rc_plan, 0)

        # 1. Conversational English question -> rejected (exit 5)
        rc_q1, res_q1, _ = self.run_dr([
            "edit",
            "--job", "edit_gate_job",
            "--feedback", "Why did you exclude FIFO compaction from step 2?",
        ])
        self.assertEqual(rc_q1, 5)
        self.assertEqual(res_q1.get("code"), "FEEDBACK_INTERROGATIVE_DENIED")

        # 2. Conversational Korean question -> rejected (exit 5)
        rc_q2, res_q2, _ = self.run_dr([
            "edit",
            "--job", "edit_gate_job",
            "--feedback", "왜 RocksDB의 Universal Compaction 분석이 빠졌나요?",
        ])
        self.assertEqual(rc_q2, 5)
        self.assertEqual(res_q2.get("code"), "FEEDBACK_INTERROGATIVE_DENIED")

        # 3. Imperative delta directive -> succeeds (exit 0)
        rc_imp, res_imp, _ = self.run_dr([
            "edit",
            "--job", "edit_gate_job",
            "--feedback", "1. Add a dedicated step comparing RocksDB Universal and FIFO compaction. 2. Pin metrics to write amplification factor (WAF).",
        ])
        self.assertEqual(rc_imp, 0)
        self.assertEqual(res_imp["status"], "plan_revised")

        # 4. Question with --allow-questions override -> succeeds (exit 0)
        rc_ovr, res_ovr, _ = self.run_dr([
            "edit",
            "--job", "edit_gate_job",
            "--feedback", "Add sub-question: Does partitioned index block cache pin L0 files?",
            "--allow-questions",
        ])
        self.assertEqual(rc_ovr, 0)
        self.assertEqual(res_ovr["status"], "plan_revised")

    def test_architecture_warnings_and_argon_timeout_auto_extension(self) -> None:
        # 1. Prompt referencing local ./src/... path and raw JSON output -> emits architecture_warnings
        rc_init, res_init, _ = self.run_dr([
            "init",
            "--job", "argon_warn_job",
            "--prompt", "Read ./src/storage/engine.rs and output pure JSON only.",
            "--goal", "Verify storage engine analysis and benchmark numbers.",
            "--gemini-model", "argon",
        ])
        self.assertEqual(rc_init, 0)
        warnings = res_init.get("architecture_warnings", [])
        self.assertGreaterEqual(len(warnings), 2)
        self.assertTrue(any("LOCAL_PATH_REFERENCE" in w for w in warnings))
        self.assertTrue(any("JSON_ONLY_OUTPUT_TRAP" in w for w in warnings))

        # 2. Plan with Argon automatically extends default 180s deadline to 600s
        rc_plan, _, _ = self.run_dr(["plan", "--job", "argon_warn_job"])
        self.assertEqual(rc_plan, 0)
        req_plan = json.loads((self.jobs_dir / "argon_warn_job" / "relay" / "request.json").read_text(encoding="utf-8"))
        self.assertEqual(req_plan["deadline"], 600)

        # 3. Approve with Argon automatically extends default 900s timeout to 1800s
        report_out = self.out_dir / "argon_report.md"
        rc_app, _, _ = self.run_dr([
            "approve",
            "--job", "argon_warn_job",
            "--reason", "Plan thoroughly covers storage engine internals and benchmark metrics.",
            "--out", str(report_out),
        ])
        self.assertEqual(rc_app, 0)
        req_app = json.loads((self.jobs_dir / "argon_warn_job" / "relay" / "request.json").read_text(encoding="utf-8"))
        self.assertEqual(req_app["timeout"], 1800)


FOREIGN_URL = "https://gemini.google.com/app/foreign_conv_999"


def own_url(job_id: str) -> str:
    """The fake's own conversation for a job (fake_agy_dr.job_conversation_url)."""
    return f"https://gemini.google.com/app/fake_{job_id}"


class ConversationIdentityTestCase(DeepResearchTestBase):
    """A report (or revised plan) is accepted only from the job's own Gemini conversation.

    Regression: two jobs drove the one shared Gemini page at once and job B saved job A's report (byte-identical)
    as its own, because acceptance only checked length and headings."""

    REASON = "The plan covers every metric and entity required by goal.md for this research."
    PROMPT = ["--prompt", "Distributed vector indexing deep dive.", "--goal", "Detailed latency and memory comparison."]

    def init_job(self, job_id: str) -> Path:
        rc, res, _ = self.run_dr(["init", "--job", job_id, *self.PROMPT])
        self.assertEqual(rc, 0, res)
        return self.jobs_dir / job_id

    def planned_job(self, job_id: str) -> Path:
        job_dir = self.init_job(job_id)
        rc, res, _ = self.run_dr(["plan", "--job", job_id])
        self.assertEqual(rc, 0, res)
        return job_dir

    def load_job(self, job_dir: Path) -> dict:
        return json.loads((job_dir / "job.json").read_text(encoding="utf-8"))

    def request(self, job_dir: Path) -> dict:
        return json.loads((job_dir / "relay" / "request.json").read_text(encoding="utf-8"))

    def wait_for_result(self, job_dir: Path, timeout: float = 15) -> None:
        result_file = job_dir / "relay" / "result.json"
        deadline = time.time() + timeout
        while not result_file.exists() and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(result_file.exists(), "fake worker never wrote its result")

    # ---------------------------------------------------------------- report acceptance
    def test_approve_rejects_report_from_another_conversation(self) -> None:
        job_dir = self.planned_job("foreign_approve")
        out = self.out_dir / "foreign.md"
        rc, res, err = self.run_dr(["approve", "--job", "foreign_approve", "--reason", self.REASON, "--out", str(out)],
                                   scenario="foreign_conversation")
        self.assertEqual(rc, 2, f"{res}\n{err}")
        self.assertEqual(res.get("code"), "CONVERSATION_MISMATCH")
        self.assertFalse(out.exists(), "a foreign report must not reach --out")
        self.assertFalse(Path(str(out) + ".meta.json").exists())
        job = self.load_job(job_dir)
        self.assertEqual(job["url"], own_url("foreign_approve"), "the job keeps its own conversation url")
        self.assertEqual(job["error_code"], "CONVERSATION_MISMATCH")
        self.assertNotEqual(job["status"], "completed")
        self.assertFalse((job_dir / "report.md").exists(), "the foreign report must not stay as the job's report")
        self.assertTrue(list((job_dir / "relay").glob("rejected_*")), "the rejected result is kept as evidence")

    def test_collect_after_rejection_fetches_from_the_jobs_conversation(self) -> None:
        job_dir = self.planned_job("foreign_then_collect")
        self.run_dr(["approve", "--job", "foreign_then_collect", "--reason", self.REASON], scenario="foreign_conversation")
        out = self.out_dir / "recovered.md"
        rc, res, err = self.run_dr(["collect", "--job", "foreign_then_collect", "--out", str(out)])
        self.assertEqual(rc, 0, f"{res}\n{err}")
        self.assertEqual(res["status"], "completed")
        req = self.request(job_dir)
        self.assertEqual((req["step"], req["resume_url"]), ("collect", own_url("foreign_then_collect")))
        meta = json.loads(Path(str(out) + ".meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["conversation_id"], "fake_foreign_then_collect")
        self.assertEqual(meta["url"], own_url("foreign_then_collect"))
        self.assertNotIn("FOREIGN", out.read_text(encoding="utf-8"))

    def test_collect_step_rejects_report_from_another_conversation(self) -> None:
        job_dir = self.planned_job("foreign_collect")
        rc, res, _ = self.run_dr(["collect", "--job", "foreign_collect"], scenario="foreign_conversation")
        self.assertEqual(rc, 2, res)
        self.assertEqual(res.get("code"), "CONVERSATION_MISMATCH")
        self.assertEqual(self.load_job(job_dir)["url"], own_url("foreign_collect"))

    def test_completed_result_on_disk_from_another_conversation_is_rejected(self) -> None:
        job_dir = self.planned_job("foreign_on_disk")
        rc, res, _ = self.run_dr(["approve", "--job", "foreign_on_disk", "--reason", self.REASON, "--no-wait"],
                                 scenario="foreign_conversation")
        self.assertEqual(rc, 0, res)
        self.wait_for_result(job_dir)
        rc2, res2, _ = self.run_dr(["collect", "--job", "foreign_on_disk"], scenario="foreign_conversation")
        self.assertEqual(rc2, 2, res2)
        self.assertEqual(res2.get("code"), "CONVERSATION_MISMATCH")

    def test_report_without_provenance_is_rejected(self) -> None:
        self.planned_job("no_provenance_job")
        rc, res, _ = self.run_dr(["approve", "--job", "no_provenance_job", "--reason", self.REASON], scenario="no_provenance")
        self.assertEqual(rc, 2, res)
        self.assertEqual(res.get("code"), "REPORT_PROVENANCE_MISSING")

    def test_reports_are_staged_and_promoted_only_after_the_gate(self) -> None:
        job_dir = self.planned_job("staged_job")
        rc, res, _ = self.run_dr(["approve", "--job", "staged_job", "--reason", self.REASON])
        self.assertEqual(rc, 0, res)
        staged = Path(self.request(job_dir)["output"])
        self.assertEqual(staged.parent.resolve(), (job_dir / "relay").resolve(), "the relay writes a staging file, never the deliverable")
        self.assertTrue((job_dir / "report.md").is_file(), "the accepted report is promoted to the job's report.md")

    def test_default_out_keeps_the_accepted_report_when_a_later_run_is_rejected(self) -> None:
        job_dir = self.planned_job("keep_accepted_job")
        rc, res, _ = self.run_dr(["approve", "--job", "keep_accepted_job", "--reason", self.REASON])
        self.assertEqual(rc, 0, res)
        accepted = (job_dir / "report.md").read_text(encoding="utf-8")
        (job_dir / "relay" / "result.json").unlink()   # force a fresh collect relay step
        rc2, res2, _ = self.run_dr(["collect", "--job", "keep_accepted_job"], scenario="foreign_conversation")
        self.assertEqual(rc2, 2, res2)
        self.assertEqual(res2.get("code"), "CONVERSATION_MISMATCH")
        self.assertEqual((job_dir / "report.md").read_text(encoding="utf-8"), accepted)
        job = self.load_job(job_dir)
        self.assertEqual(job["status"], "completed", "a refused re-collect does not demote a completed job")
        self.assertNotIn("error_code", job)
        self.assertTrue(job.get("progress_highlights"), "the accepted run's highlights stay")
        self.assertEqual([r["code"] for r in job["rejections"]], ["CONVERSATION_MISMATCH"])

    def test_completed_result_without_its_staged_report_does_not_dead_end_collect(self) -> None:
        job_dir = self.planned_job("no_staged_job")
        rc, res, _ = self.run_dr(["approve", "--job", "no_staged_job", "--reason", self.REASON, "--no-wait"])
        self.assertEqual(rc, 0, res)
        self.wait_for_result(job_dir)
        seq = self.load_job(job_dir)["seq"]
        (job_dir / "relay" / f"report_{seq}.md").unlink()
        rc2, res2, _ = self.run_dr(["collect", "--job", "no_staged_job"])
        self.assertEqual(res2.get("code"), "REPORT_FILE_MISSING", res2)
        rc3, res3, err3 = self.run_dr(["collect", "--job", "no_staged_job"])
        self.assertEqual(rc3, 0, f"{res3}\n{err3}")

    def test_identity_failure_from_the_relay_sets_aside_and_drops_progress(self) -> None:
        job_dir = self.planned_job("relay_mismatch_job")
        rc, res, _ = self.run_dr(["approve", "--job", "relay_mismatch_job", "--reason", self.REASON],
                                 scenario="foreign_progress_relay_error")
        self.assertEqual(rc, 2, res)
        self.assertEqual(res.get("code"), "CONVERSATION_MISMATCH")
        relay = job_dir / "relay"
        self.assertFalse((relay / "result.json").exists())
        self.assertFalse((relay / "progress.jsonl").exists())
        self.assertTrue(list(relay.glob("rejected_*.result.json")))
        self.assertTrue(list(relay.glob("rejected_*.progress.jsonl")))
        self.assertEqual(self.load_job(job_dir).get("progress_highlights"), [])

    def test_rejected_run_progress_does_not_reach_the_accepted_report(self) -> None:
        job_dir = self.planned_job("foreign_progress_job")
        self.run_dr(["approve", "--job", "foreign_progress_job", "--reason", self.REASON], scenario="foreign_conversation")
        self.assertEqual(self.load_job(job_dir).get("progress_highlights"), [])
        self.assertTrue(list((job_dir / "relay").glob("rejected_*.progress.jsonl")), "the rejected run's progress is kept as evidence")
        out = self.out_dir / "clean_progress.md"
        rc, res, _ = self.run_dr(["collect", "--job", "foreign_progress_job", "--out", str(out)])
        self.assertEqual(rc, 0, res)
        meta = json.loads(Path(str(out) + ".meta.json").read_text(encoding="utf-8"))
        self.assertFalse([h for h in meta["progress_highlights"] if "FOREIGN" in h], meta["progress_highlights"])

    # ---------------------------------------------------------------- approve / edit recovery
    def test_approve_refused_on_a_moved_page_keeps_the_plan_approvable(self) -> None:
        job_dir = self.planned_job("moved_page_job")
        rc, res, _ = self.run_dr(["approve", "--job", "moved_page_job", "--reason", self.REASON], scenario="approve_page_moved")
        self.assertEqual(rc, 2, res)
        self.assertEqual(res.get("code"), "CONVERSATION_MISMATCH")
        self.assertEqual(self.load_job(job_dir)["status"], "plan_ready", "nothing was clicked: the plan still awaits approval")
        rc2, res2, err2 = self.run_dr(["approve", "--job", "moved_page_job", "--reason", self.REASON])
        self.assertEqual(rc2, 0, f"{res2}\n{err2}")
        self.assertEqual(res2["status"], "completed")

    def test_no_wait_pre_click_refusal_is_restored_by_collect(self) -> None:
        job_dir = self.planned_job("no_wait_moved_job")
        rc, res, _ = self.run_dr(["approve", "--job", "no_wait_moved_job", "--reason", self.REASON, "--no-wait"],
                                 scenario="approve_page_moved_worker")
        self.assertEqual(rc, 0, res)
        self.wait_for_result(job_dir)
        rc2, res2, _ = self.run_dr(["collect", "--job", "no_wait_moved_job"])
        self.assertEqual(rc2, 2, res2)
        self.assertEqual(res2.get("code"), "CONVERSATION_MISMATCH")
        self.assertEqual(self.load_job(job_dir)["status"], "plan_ready")
        rc3, res3, err3 = self.run_dr(["approve", "--job", "no_wait_moved_job", "--reason", self.REASON])
        self.assertEqual(rc3, 0, f"{res3}\n{err3}")

    def test_completed_job_clears_identity_error_fields(self) -> None:
        job_dir = self.planned_job("clears_errors_job")
        self.run_dr(["approve", "--job", "clears_errors_job", "--reason", self.REASON], scenario="approve_page_moved")
        rc, res, _ = self.run_dr(["approve", "--job", "clears_errors_job", "--reason", self.REASON])
        self.assertEqual(rc, 0, res)
        job = self.load_job(job_dir)
        self.assertNotIn("error_code", job)
        self.assertNotIn("error_message", job)
        self.assertEqual([r["code"] for r in job.get("rejections", [])], ["CONVERSATION_MISMATCH"])

    def test_edit_on_another_conversation_keeps_the_job_plan(self) -> None:
        job_dir = self.planned_job("edit_foreign_job")
        rc, res, _ = self.run_dr(["edit", "--job", "edit_foreign_job", "--feedback", "Add a GPU memory footprint comparison."],
                                 scenario="edit_foreign")
        self.assertEqual(rc, 2, res)
        self.assertEqual(res.get("code"), "CONVERSATION_MISMATCH")
        job = self.load_job(job_dir)
        self.assertEqual(job["url"], own_url("edit_foreign_job"))
        self.assertEqual(job["status"], "plan_ready")
        self.assertEqual(job["plan_iterations"], 1)
        self.assertFalse((job_dir / "plan_v2.md").exists())

    def test_edit_relay_mismatch_before_typing_keeps_the_plan(self) -> None:
        job_dir = self.planned_job("edit_relay_job")
        rc, res, _ = self.run_dr(["edit", "--job", "edit_relay_job", "--feedback", "Add a GPU memory footprint comparison."],
                                 scenario="edit_relay_mismatch")
        self.assertEqual(rc, 2, res)
        self.assertEqual(res.get("code"), "CONVERSATION_MISMATCH")
        self.assertEqual(self.load_job(job_dir)["status"], "plan_ready")
        rc2, res2, err2 = self.run_dr(["approve", "--job", "edit_relay_job", "--reason", self.REASON])
        self.assertEqual(rc2, 0, f"{res2}\n{err2}")

    def test_edit_refused_after_the_edit_click_but_before_typing_keeps_the_plan(self) -> None:
        job_dir = self.planned_job("edit_click_job")
        rc, res, _ = self.run_dr(["edit", "--job", "edit_click_job", "--feedback", "Add a GPU memory footprint comparison."],
                                 scenario="edit_relay_mismatch_after_click")
        self.assertEqual(rc, 2, res)
        self.assertEqual(self.load_job(job_dir)["status"], "plan_ready")

    def test_edit_refused_after_typing_marks_the_job_failed(self) -> None:
        job_dir = self.planned_job("edit_typed_job")
        rc, res, _ = self.run_dr(["edit", "--job", "edit_typed_job", "--feedback", "Add a GPU memory footprint comparison."],
                                 scenario="edit_relay_mismatch_after_typing")
        self.assertEqual(rc, 2, res)
        self.assertEqual(self.load_job(job_dir)["status"], "error")

    def test_successful_edit_clears_an_earlier_refusal(self) -> None:
        job_dir = self.planned_job("edit_clears_job")
        self.run_dr(["edit", "--job", "edit_clears_job", "--feedback", "Add a GPU memory footprint comparison."],
                    scenario="edit_relay_mismatch")
        rc, res, _ = self.run_dr(["edit", "--job", "edit_clears_job", "--feedback", "Add a GPU memory footprint comparison."])
        self.assertEqual(rc, 0, res)
        job = self.load_job(job_dir)
        self.assertNotIn("error_code", job)
        self.assertNotIn("error_message", job)

    def test_reinit_does_not_inherit_the_previous_runs_conversation(self) -> None:
        job_dir = self.planned_job("reinit_job")
        self.assertTrue((job_dir / "relay" / "conversation.json").is_file())
        self.init_job("reinit_job")
        self.assertFalse((job_dir / "relay" / "conversation.json").exists())
        rc, res, _ = self.run_dr(["collect", "--job", "reinit_job"])
        self.assertEqual(res.get("code"), "NO_CONVERSATION_URL", res)

    def test_a_running_auto_jobs_conversation_cannot_be_bound_by_another_job(self) -> None:
        rc, res, _ = self.run_dr(["auto", "--job", "auto_owner_job", *self.PROMPT, "--no-wait"], scenario="long_running")
        self.assertEqual(rc, 0, res)
        conv = self.jobs_dir / "auto_owner_job" / "relay" / "conversation.json"
        deadline = time.time() + 10
        while not conv.exists() and time.time() < deadline:
            time.sleep(0.05)
        self.init_job("binder_job")
        rc2, res2, _ = self.run_dr(["collect", "--job", "binder_job", "--url", own_url("auto_owner_job")])
        self.assertEqual(rc2, 5, res2)
        self.assertEqual(res2.get("code"), "CONVERSATION_URL_CONFLICT")
        self.wait_for_result(self.jobs_dir / "auto_owner_job")

    def test_edit_without_conversation_id_is_rejected(self) -> None:
        job_dir = self.planned_job("edit_no_id_job")
        rc, res, _ = self.run_dr(["edit", "--job", "edit_no_id_job", "--feedback", "Add a GPU memory footprint comparison."],
                                 scenario="edit_no_id")
        self.assertEqual(rc, 2, res)
        self.assertEqual(res.get("code"), "CONVERSATION_MISMATCH")
        self.assertFalse((job_dir / "plan_v2.md").exists())

    # ---------------------------------------------------------------- binding a conversation
    def test_collect_without_a_recorded_conversation_refuses(self) -> None:
        self.init_job("no_url_job")
        rc, res, _ = self.run_dr(["collect", "--job", "no_url_job"])
        self.assertEqual(rc, 2, res)
        self.assertEqual(res.get("code"), "NO_CONVERSATION_URL")
        self.assertFalse((self.jobs_dir / "no_url_job" / "relay" / "agy_invoked.json").exists(), "agy must not run")

    def test_collect_url_binds_a_job_without_a_conversation(self) -> None:
        self.init_job("bind_url_job")
        rc, res, _ = self.run_dr(["collect", "--job", "bind_url_job", "--url", own_url("bind_url_job")])
        self.assertEqual(rc, 0, res)
        self.assertEqual(self.load_job(self.jobs_dir / "bind_url_job")["url"], own_url("bind_url_job"))

    def test_collect_url_cannot_rebind_a_job(self) -> None:
        job_dir = self.planned_job("rebind_job")
        rc, res, _ = self.run_dr(["collect", "--job", "rebind_job", "--url", FOREIGN_URL])
        self.assertEqual(rc, 5, res)
        self.assertEqual(res.get("code"), "CONVERSATION_URL_CONFLICT")
        self.assertEqual(self.load_job(job_dir)["url"], own_url("rebind_job"))

    def test_collect_url_must_name_a_conversation(self) -> None:
        self.init_job("bad_url_job")
        rc, res, _ = self.run_dr(["collect", "--job", "bad_url_job", "--url", "https://gemini.google.com/app"])
        self.assertEqual(rc, 5, res)
        self.assertEqual(res.get("code"), "INVALID_CONVERSATION_URL")

    def test_collect_url_must_be_a_gemini_url(self) -> None:
        self.init_job("foreign_host_job")
        rc, res, _ = self.run_dr(["collect", "--job", "foreign_host_job", "--url", "https://example.com/app/abc"])
        self.assertEqual(rc, 5, res)
        self.assertEqual(res.get("code"), "INVALID_CONVERSATION_URL")
        self.assertIsNone(self.load_job(self.jobs_dir / "foreign_host_job").get("url"))

    def test_plan_without_conversation_id_is_refused(self) -> None:
        self.init_job("plan_no_id_job")
        rc, res, _ = self.run_dr(["plan", "--job", "plan_no_id_job"], scenario="plan_no_id")
        self.assertEqual(rc, 2, res)
        self.assertEqual(res.get("code"), "NO_CONVERSATION_URL")

    def test_plan_refuses_a_conversation_owned_by_another_job(self) -> None:
        self.init_job("owner_job")
        self.init_job("intruder_job")
        rc1, res1, _ = self.run_dr(["plan", "--job", "owner_job"], scenario="plan_owned")
        self.assertEqual(rc1, 0, res1)
        rc2, res2, _ = self.run_dr(["plan", "--job", "intruder_job"], scenario="plan_owned")
        self.assertEqual(rc2, 2, res2)
        self.assertEqual(res2.get("code"), "CONVERSATION_MISMATCH")
        self.assertIsNone(self.load_job(self.jobs_dir / "intruder_job").get("url"))

    # ---------------------------------------------------------------- auto
    def test_auto_records_conversation_from_report_provenance(self) -> None:
        out = self.out_dir / "auto_provenance.md"
        rc, res, _ = self.run_dr(["auto", "--job", "auto_prov_job", *self.PROMPT, "--out", str(out)])
        self.assertEqual(rc, 0, res)
        self.assertEqual(self.load_job(self.jobs_dir / "auto_prov_job")["url"], own_url("auto_prov_job"))
        meta = json.loads(Path(str(out) + ".meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["conversation_id"], "fake_auto_prov_job")

    def test_auto_rejects_a_report_from_another_conversation_than_it_pinned(self) -> None:
        rc, res, _ = self.run_dr(["auto", "--job", "auto_foreign_job", *self.PROMPT], scenario="foreign_conversation")
        self.assertEqual(rc, 2, res)
        self.assertEqual(res.get("code"), "CONVERSATION_MISMATCH")
        self.assertEqual(self.load_job(self.jobs_dir / "auto_foreign_job")["url"], own_url("auto_foreign_job"))

    def test_auto_error_keeps_the_pinned_conversation_for_collect(self) -> None:
        rc, res, _ = self.run_dr(["auto", "--job", "auto_timeout_job", *self.PROMPT], scenario="auto_report_timeout")
        self.assertEqual(rc, 2, res)
        self.assertEqual(res.get("code"), "REPORT_TIMEOUT")
        self.assertEqual(self.load_job(self.jobs_dir / "auto_timeout_job")["url"], own_url("auto_timeout_job"))
        rc2, res2, err2 = self.run_dr(["collect", "--job", "auto_timeout_job"])
        self.assertEqual(rc2, 0, f"{res2}\n{err2}")

    def test_worker_died_auto_job_keeps_its_conversation(self) -> None:
        rc, res, _ = self.run_dr(["auto", "--job", "auto_crash_job", *self.PROMPT], scenario="worker_crash")
        self.assertEqual(rc, 3, res)
        self.assertEqual(res.get("code"), "WORKER_DIED")
        self.assertEqual(self.load_job(self.jobs_dir / "auto_crash_job")["url"], own_url("auto_crash_job"))

    def test_auto_on_a_reused_job_dir_ignores_a_stale_result(self) -> None:
        job_dir = self.jobs_dir / "reused_job"
        relay = job_dir / "relay"
        relay.mkdir(parents=True)
        stale = relay / "report_1.md"
        stale.write_text("# STALE report\n\n" + "stale words " * 80, encoding="utf-8")
        (relay / "result.json").write_text(json.dumps({
            "relay_version": 2, "step": "auto", "seq": 1, "status": "completed", "url": FOREIGN_URL,
            "conversation_id": "foreign_conv_999", "conversation_url": FOREIGN_URL, "report_file": str(stale), "report_chars": 960,
        }), encoding="utf-8")
        (relay / "started.json").write_text(json.dumps({"relay_version": 2, "step": "auto", "seq": 1, "status": "started", "pid": 4}),
                                            encoding="utf-8")
        out = self.out_dir / "reused.md"
        rc, res, err = self.run_dr(["auto", "--job", "reused_job", *self.PROMPT, "--out", str(out)])
        self.assertEqual(rc, 0, f"{res}\n{err}")
        self.assertNotIn("STALE", out.read_text(encoding="utf-8"))
        self.assertEqual(self.load_job(job_dir)["url"], own_url("reused_job"))


if __name__ == "__main__":
    unittest.main()

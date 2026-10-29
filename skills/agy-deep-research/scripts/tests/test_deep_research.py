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
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = TESTS_DIR.parent
SKILL_DIR = SCRIPTS_DIR.parent
DEEP_RESEARCH_PY = SCRIPTS_DIR / "deep_research.py"
FAKE_AGY_DR = TESTS_DIR / "fake_agy_dr.py"


class DeepResearchTestCase(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()

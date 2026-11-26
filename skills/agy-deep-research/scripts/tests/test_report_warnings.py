#!/usr/bin/env python3
"""test_report_warnings.py - deterministic report warnings that run after the Report Quality Gate.

An accepted report is never refused after the research quota was spent; instead the orchestrator flags what it can
decide without reading for meaning, so Claude's goal.md review (SKILL.md step 7) starts from facts:
  - NO_SOURCES               the report cites no source (sources_count 0)
  - REFUSAL_SUSPECTED        a refusal / error phrase near the top of a report that cites no source
  - GOAL_CHANGED_SINCE_INIT  goal.md no longer has the sha256 recorded at `init`
The warnings appear as `report_warnings` in the command output, job.json and <out>.meta.json; `init` records
`goal_sha256` in job.json and the meta file repeats it.
"""
from __future__ import annotations

import hashlib
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_deep_research import DeepResearchTestBase  # noqa: E402

REASON = "The plan covers every benchmark and memory item listed in goal.md."


class ReportWarningsTestCase(DeepResearchTestBase):
    def _job(self, job_id: str, scenario: str = "perfect") -> Path:
        rc, res, err = self.run_dr(["init", "--job", job_id, "--prompt", "Vector search indexing survey.",
                                    "--goal", "Benchmarks and memory per index type."])
        self.assertEqual(rc, 0, err)
        rc, res, err = self.run_dr(["plan", "--job", job_id], scenario=scenario)
        self.assertEqual(rc, 0, err)
        return self.jobs_dir / job_id

    def _approve(self, job_id: str, scenario: str) -> tuple[int, dict, Path]:
        out = self.out_dir / f"{job_id}.md"
        rc, res, err = self.run_dr(["approve", "--job", job_id, "--reason", REASON, "--out", str(out)], scenario=scenario)
        self.assertEqual(rc, 0, f"{res}\n{err}")
        return rc, res, out

    def _all_warnings(self, job_dir: Path, res: dict, out: Path) -> tuple[list, list, list]:
        job = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
        meta = json.loads(Path(str(out) + ".meta.json").read_text(encoding="utf-8"))
        return res["report_warnings"], job["report_warnings"], meta["report_warnings"]

    def test_clean_report_has_no_warnings_and_records_goal_hash(self) -> None:
        job_dir = self._job("clean_job")
        goal_sha = hashlib.sha256((job_dir / "goal.md").read_bytes()).hexdigest()
        job = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
        self.assertEqual(job["goal_sha256"], goal_sha)

        _, res, out = self._approve("clean_job", "perfect")
        for warnings in self._all_warnings(job_dir, res, out):
            self.assertEqual(warnings, [])
        meta = json.loads(Path(str(out) + ".meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["goal_sha256"], goal_sha)

    def test_report_without_sources_is_saved_and_flagged(self) -> None:
        job_dir = self._job("nosrc_job")
        _, res, out = self._approve("nosrc_job", "no_sources_report")
        self.assertEqual(res["status"], "completed")
        self.assertEqual(res["sources_count"], 0)
        self.assertTrue(out.is_file())
        for warnings in self._all_warnings(job_dir, res, out):
            self.assertEqual(warnings, ["NO_SOURCES"])

    def test_refusal_report_is_flagged(self) -> None:
        job_dir = self._job("refusal_job")
        _, res, out = self._approve("refusal_job", "refusal_report")
        for warnings in self._all_warnings(job_dir, res, out):
            self.assertEqual(warnings, ["NO_SOURCES", "REFUSAL_SUSPECTED"])

    def test_goal_edited_after_init_is_flagged(self) -> None:
        job_dir = self._job("goal_edit_job")
        (job_dir / "goal.md").write_text("Benchmarks only; memory dropped.\n", encoding="utf-8")
        _, res, out = self._approve("goal_edit_job", "perfect")
        for warnings in self._all_warnings(job_dir, res, out):
            self.assertEqual(warnings, ["GOAL_CHANGED_SINCE_INIT"])

    def test_collect_path_flags_too(self) -> None:
        job_dir = self._job("collect_nosrc_job")
        rc, res, err = self.run_dr(["approve", "--job", "collect_nosrc_job", "--reason", REASON, "--no-wait"],
                                   scenario="no_sources_report")
        self.assertEqual(rc, 0, err)
        out = self.out_dir / "collect_nosrc_job.md"
        rc, res, err = self.run_dr(["collect", "--job", "collect_nosrc_job", "--out", str(out)],
                                   scenario="no_sources_report")
        self.assertEqual(rc, 0, f"{res}\n{err}")
        for warnings in self._all_warnings(job_dir, res, out):
            self.assertEqual(warnings, ["NO_SOURCES"])

    def test_auto_path_flags_too(self) -> None:
        out = self.out_dir / "auto_refusal.md"
        rc, res, err = self.run_dr(["auto", "--job", "auto_refusal_job", "--prompt", "Vector search indexing survey.",
                                    "--goal", "Benchmarks and memory per index type.", "--out", str(out)],
                                   scenario="refusal_report")
        self.assertEqual(rc, 0, f"{res}\n{err}")
        self.assertEqual(res["report_warnings"], ["NO_SOURCES", "REFUSAL_SUSPECTED"])


class RefusalPatternTestCase(unittest.TestCase):
    def setUp(self) -> None:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import deep_research
        self.dr = deep_research

    def _hit(self, text: str) -> bool:
        return any(p.search(text) for p in self.dr.REFUSAL_PATTERNS)

    def test_refusal_and_error_phrases_match(self) -> None:
        for text in ("I can't help with that request.", "I cannot provide this.", "I'm unable to assist here.",
                     "Something went wrong. Try again later.", "죄송합니다. 이 요청은 도와드릴 수 없습니다.",
                     "문제가 발생했습니다. 다시 시도해 주세요."):
            with self.subTest(text=text):
                self.assertTrue(self._hit(text))

    def test_ordinary_report_text_does_not_match(self) -> None:
        for text in ("# 벡터 DB 비교\n\n정치경제학 관점의 비용 구조를 다룬다.", "This index doesn't do much at 1M vectors.",
                     "Vendors cannot provide p99 numbers for this setup, so the benchmark below was used."):
            with self.subTest(text=text):
                self.assertFalse(self._hit(text))

    def test_refusal_needs_missing_sources(self) -> None:
        job_dir = Path(self.dr.__file__).parent  # any directory without goal.md
        self.assertEqual(self.dr.compute_report_warnings("# T\nI can't help with that.", 4, {}, job_dir), [])
        self.assertEqual(self.dr.compute_report_warnings("# T\nI can't help with that.", 0, {}, job_dir),
                         ["NO_SOURCES", "REFUSAL_SUSPECTED"])


if __name__ == "__main__":
    unittest.main()

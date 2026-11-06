"""Tests for the adaptive --timeout and for stdout/stderr encoding safety (no agy call needed).

  python -m unittest discover -s scripts/tests     (from the skill folder)
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))
import agy_task  # noqa: E402


def ns(**kw):
    base = dict(timeout="auto", effort=None, deep_research=False)
    base.update(kw)
    return argparse.Namespace(**base)


class AdaptiveTimeout(unittest.TestCase):
    def test_teamwork_low_default_is_generous(self):
        val, src = agy_task.resolve_timeout(ns(effort="low"), teamwork=True, input_bytes=0)
        self.assertEqual((val, src), ("60m", "auto"))

    def test_single_agent_default(self):
        self.assertEqual(agy_task.resolve_timeout(ns(), teamwork=False, input_bytes=0), ("20m", "auto"))

    def test_effort_scales_teamwork(self):
        self.assertEqual(agy_task.resolve_timeout(ns(effort="medium"), teamwork=True, input_bytes=0)[0], "90m")
        self.assertEqual(agy_task.resolve_timeout(ns(effort="high"), teamwork=True, input_bytes=0)[0], "120m")

    def test_large_inputs_add_time_capped(self):
        # +1 minute per 50 KB staged, at most +30 minutes
        self.assertEqual(agy_task.resolve_timeout(ns(effort="low"), teamwork=True, input_bytes=500_000)[0], "70m")
        self.assertEqual(agy_task.resolve_timeout(ns(effort="low"), teamwork=True, input_bytes=50_000_000)[0], "90m")

    def test_deep_research_default(self):
        self.assertEqual(agy_task.resolve_timeout(ns(deep_research=True), teamwork=False, input_bytes=0)[0], "90m")

    def test_explicit_value_is_kept(self):
        self.assertEqual(agy_task.resolve_timeout(ns(timeout="15m"), teamwork=True, input_bytes=0), ("15m", "explicit"))
        self.assertEqual(agy_task.resolve_timeout(ns(timeout="0"), teamwork=True, input_bytes=0), ("0", "explicit"))

    def test_timeout_classified_as_partial(self):
        err = {"raw": "error: print timeout after 15m0s with the turn still in progress; the response is partial"}
        self.assertEqual(agy_task.classify_status(2, None, err), "timeout-partial")
        self.assertEqual(agy_task.classify_status(0, "SUCCESS", None), "ok")
        self.assertEqual(agy_task.classify_status(1, "FAILED", None), "agent-failed")
        self.assertEqual(agy_task.classify_status(1, None, None), "error")


class DryRun(unittest.TestCase):
    def run_cli(self, *args, encoding="cp949"):
        env = dict(os.environ, PYTHONIOENCODING=encoding)
        wd = tempfile.mkdtemp(prefix="agyt_")
        cp = subprocess.run([sys.executable, str(SCRIPTS / "agy_task.py"), *args[:1], "run", "-w", wd, *args[1:],
                             "--dry-run"], capture_output=True, env=env)
        return cp

    def test_non_cp949_text_does_not_crash_output(self):
        cp = self.run_cli("--model=gemini-4-argon-low", "-t", "/teamwork review F601–F605 — ✓ 한글")
        self.assertEqual(cp.returncode, 0, cp.stderr.decode("utf-8", "replace"))
        plan = json.loads(cp.stdout.decode("utf-8"))
        self.assertIn("F601–F605", plan["prompt"])

    def test_dry_run_shows_auto_teamwork_timeout(self):
        cp = self.run_cli("--model=gemini-4-argon-low", "-t", "/teamwork x", "--effort", "low")
        plan = json.loads(cp.stdout.decode("utf-8"))
        cmd = plan["command"]
        self.assertEqual(cmd[cmd.index("--print-timeout") + 1], "60m")
        self.assertEqual(plan["timeout"], {"value": "60m", "source": "auto"})


if __name__ == "__main__":
    unittest.main()

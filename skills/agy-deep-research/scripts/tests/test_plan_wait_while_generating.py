"""
test_plan_wait_while_generating.py - on 2026-11-13 Gemini took >300s to produce a Research
Plan while visibly generating (stop button + spinner). wait_for_new_plan gave up at its fixed
300s and the job failed with PLAN_TIMEOUT although nothing was wrong. While generation is in
progress the wait must extend up to max_wait_sec; when idle it still stops at timeout_sec.
"""
import sandbox  # noqa: F401  (must stay the first import: blocks real-app access)
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import run_deep_research as rdr


class Clock:
    def __init__(self):
        self.t = 0.0

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += max(s, 1.0)


def _run(plan_ready_at, generating, timeout_sec=10, max_wait_sec=100):
    clock = Clock()
    page = MagicMock()
    page.url = "https://gemini.google.com/app/abc"
    page.locator.return_value.count.return_value = 0
    page.locator.return_value.last.count.return_value = 0
    with patch.object(rdr.time, "time", clock.time), patch.object(rdr.time, "sleep", clock.sleep), \
         patch.object(rdr, "is_generation_in_progress", return_value=generating), \
         patch.object(rdr, "_plan_card_ready", side_effect=lambda *_: clock.t >= plan_ready_at), \
         patch.object(rdr, "assert_conversation"), \
         patch.object(rdr, "stop_auto_confirm_fuse_and_expand_plan", return_value="PLAN"), \
         patch.object(rdr, "log"):
        return rdr.wait_for_new_plan(page, timeout_sec=timeout_sec, max_wait_sec=max_wait_sec)


class TestPlanWait(unittest.TestCase):
    def test_extends_past_timeout_while_generating(self):
        out = _run(plan_ready_at=50, generating=True)
        self.assertEqual(out["status"], "plan_ready")
        self.assertEqual(out["plan"], "PLAN")

    def test_idle_still_times_out_at_timeout(self):
        with self.assertRaises(TimeoutError):
            _run(plan_ready_at=50, generating=False)

    def test_hard_cap_even_while_generating(self):
        with self.assertRaises(TimeoutError):
            _run(plan_ready_at=500, generating=True)

    def test_max_wait_default_from_env(self):
        with patch.dict(os.environ, {"GEMINI_DR_PLAN_MAX_WAIT": "77"}):
            self.assertEqual(rdr.plan_max_wait_default(), 77)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GEMINI_DR_PLAN_MAX_WAIT", None)
            self.assertEqual(rdr.plan_max_wait_default(), 900)


if __name__ == "__main__":
    unittest.main()

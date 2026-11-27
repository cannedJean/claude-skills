"""
test_plan_wait_while_generating.py - on 2026-11-13 Gemini took >300s to produce a Research
Plan while visibly generating. The wait gave up at a fixed 300s and the job failed with
PLAN_TIMEOUT although nothing was wrong.

UI Automation path (replaces the Playwright page tests): the plan-card wait has one cap,
steps.StepConfig.card_timeout_s, which the driver takes from GEMINI_DR_PLAN_MAX_WAIT (default 900 s)
through run_deep_research.default_step_config(); a card arriving any time before the cap is accepted,
and the cap is a hard limit (PLAN_TIMEOUT, with a running response stopped first).
"""
import sandbox  # noqa: F401  (must stay the first import: blocks real-app access)
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

import run_deep_research as rdr  # noqa: E402
import steps  # noqa: E402
from test_plan_steps import BOUND, AutoTree, Clock, cfg, load, locs, set_auto  # noqa: E402


def wait(card_after_snapshots, cap_s):
    spec = load("plan_flow_ko.json")
    spec["state"] = "bound"
    set_auto(spec, "bound", "card", card_after_snapshots)
    clock = Clock()
    tree = AutoTree(spec)
    res = steps.wait_plan_and_cancel(tree, locs(), cfg(clock, card_timeout_s=cap_s, poll_min_s=1.0), BOUND, 0)
    return res, clock


class TestPlanWait(unittest.TestCase):
    def test_extends_past_300s_while_generating(self):
        res, clock = wait(card_after_snapshots=400, cap_s=900)   # ~400 s of polling at 1 s
        self.assertEqual(res["cancel"], "cancel_button")
        self.assertGreater(clock.t, 300)

    def test_hard_cap_even_while_generating(self):
        with self.assertRaises(steps.StepError) as cm:
            wait(card_after_snapshots=5000, cap_s=100)
        self.assertEqual(cm.exception.code, "PLAN_TIMEOUT")

    def test_max_wait_default_from_env(self):
        with patch.dict(os.environ, {"GEMINI_DR_PLAN_MAX_WAIT": "77"}):
            self.assertEqual(rdr.plan_max_wait_default(), 77)
            self.assertEqual(rdr.default_step_config().card_timeout_s, 77)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GEMINI_DR_PLAN_MAX_WAIT", None)
            self.assertEqual(rdr.plan_max_wait_default(), 900)
            self.assertEqual(rdr.default_step_config().card_timeout_s, 900)
        with patch.dict(os.environ, {"GEMINI_DR_PLAN_MAX_WAIT": "x"}):
            self.assertEqual(rdr.plan_max_wait_default(), 900)


if __name__ == "__main__":
    unittest.main()

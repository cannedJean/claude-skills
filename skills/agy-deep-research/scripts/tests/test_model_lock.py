"""
test_model_lock.py - The Gemini model/thinking selection must be locked once, in the
new chat, before the research prompt is sent. Steps that operate on an existing
conversation (approve / collect via --resume-url, edit, probe) must never touch the model menu,
because the conversation's model is already bound and a reload can show a
different default (observed: 'Flash') that the driver would then "fix" mid-run.

UI Automation path (replaces the Playwright page tests): the plan step calls steps.select_mode
once, before the send; edit / probe never call it and never act on the mode button or its items;
approve / collect run nothing at all yet (UIA_DRIVER_NOT_IMPLEMENTED). The thinking level is chosen
only inside the `thinking_menu` submenu (locator names 사고 수준 / Thinking level), so a per-response
'show thinking' toggle can never be taken for it.
"""
import sandbox  # noqa: F401  (must stay the first import: blocks real-app access)
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

import locators  # noqa: E402
import run_deep_research as rdr  # noqa: E402
import steps  # noqa: E402
from test_driver_uia import BOUND, FEEDBACK, DriverHarness  # noqa: E402  (no test methods of its own)
from test_plan_steps import KO_PROMPT, AutoTree, acts, load, locs  # noqa: E402

MODE_NODES = ("b_mode", "mi_neon", "mi_hydrogen", "mi_carbon", "mi_auto", "mi_thinking", "ti_high", "ti_std")


class TestModelLock(DriverHarness):
    def mode_acts(self, tree):
        return [a for a in acts(tree) if a[1] in MODE_NODES]

    def test_plan_selects_the_model_exactly_once_before_the_send(self):
        tree = AutoTree(load("plan_flow_ko.json"))
        with patch.object(rdr.steps, "select_mode", side_effect=steps.select_mode) as sm:
            rc, out, err = self.run_main(["--step", "plan", "--raw-prompt", "--prompt", KO_PROMPT], tree)
        self.assertEqual(rc, 0, (out, err))
        self.assertEqual(sm.call_count, 1)
        a = acts(tree)
        self.assertTrue(self.mode_acts(tree))
        self.assertLess(a.index(self.mode_acts(tree)[-1]), a.index(("act", "b_send", "invoke")))

    def test_approve_on_existing_conversation_does_not_touch_model(self):
        for step in ("approve", "collect"):
            with self.subTest(step=step):
                tree = AutoTree(load("plan_flow_edit_two_cards.json"))
                with patch.object(rdr.steps, "select_mode") as sm:
                    rc, _, _ = self.run_main(["--step", step, "--resume-url", BOUND], tree)
                self.assertEqual(rc, 3)
                sm.assert_not_called()
                self.assertEqual(tree.actions, [])

    def test_edit_step_does_not_touch_model(self):
        tree = AutoTree(load("plan_flow_edit_two_cards.json"))
        with patch.object(rdr.steps, "select_mode") as sm:
            rc, out, err = self.run_main(["--step", "edit", "--resume-url", BOUND, "--edit-prompt", FEEDBACK], tree)
        self.assertEqual(rc, 0, (out, err))
        sm.assert_not_called()
        self.assertEqual(self.mode_acts(tree), [])

    def test_edit_plan_does_not_touch_model(self):
        tree = AutoTree(load("plan_flow_edit_two_cards.json"))
        with patch.object(steps, "select_mode") as sm, patch.object(steps, "_pick") as pick:
            steps.edit_plan(tree, locs(), self.cfg_fast(), BOUND, FEEDBACK)
        sm.assert_not_called()
        pick.assert_not_called()
        self.assertEqual(self.mode_acts(tree), [])

    def test_probe_does_not_touch_model(self):
        tree = AutoTree(load("plan_flow_edit_two_cards.json"))
        with patch.object(rdr.steps, "select_mode") as sm:
            rc, _, _ = self.run_main(["--step", "probe", "--resume-url", BOUND], tree)
        self.assertEqual(rc, 0)
        sm.assert_not_called()
        self.assertEqual(tree.actions, [])

    def test_show_thinking_toggle_is_not_a_thinking_level_control(self):
        names = locs()["locators"]["thinking_menu"]["names"]
        self.assertFalse(locators.match_name("생각하는 과정 표시", names))
        self.assertFalse(locators.match_name("Show thinking", names))
        self.assertTrue(locators.match_name("사고 수준", names))
        self.assertTrue(locators.match_name("Thinking level", names))

    def cfg_fast(self):
        from test_plan_steps import cfg
        return cfg()


if __name__ == "__main__":
    unittest.main()

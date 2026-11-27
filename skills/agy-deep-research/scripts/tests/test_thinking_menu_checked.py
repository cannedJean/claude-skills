"""
test_thinking_menu_checked.py - on 2026-11-16 the mode button read only the model name while the
open mode menu showed the thinking levels with one of them aria-checked="true". The driver logged
'no thinking level visible' and never verified or set the requested level. The level must be read
from the checked menu item and switched only if it differs.

UI Automation path (replaces the Playwright page tests): steps.select_mode reads the item's
AriaProperties `checked`, leaves a checked match alone, invokes it otherwise and verifies `checked`
afterwards (MODEL_NOT_SELECTED when it does not stick); a menu without the thinking submenu fails
closed (UI_LOCATOR_MISS) instead of being skipped, and nothing is sent.
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
from test_driver_uia import DriverHarness  # noqa: E402  (no test methods of its own)
from test_plan_steps import AutoTree, acts, load, nodes  # noqa: E402
from test_thinking_in_mode_menu import select  # noqa: E402


def checked_items(spec, state):
    st = spec["states"][state]
    return [d["name"] for d in nodes({"states": {state: st}})
            if d.get("role") == "menuitemradio" and str((d.get("aria") or {}).get("checked", "")).lower() == "true"]


class TestThinkingFromCheckedMenuItem(unittest.TestCase):
    def test_fixture_marks_the_level_checked(self):
        spec = load("plan_flow_en_synthetic.json")
        self.assertIn("Selected High", checked_items(spec, "th_hydrogen_high"))
        self.assertIn("Selected Standard", checked_items(spec, "th_hydrogen_std"))

    def test_reads_checked_level_and_leaves_matching_alone(self):
        tree, _ = select("fresh_hydrogen_high")
        self.assertIn(("act", "mi_thinking", "expand"), acts(tree))   # the level was read from the menu
        self.assertNotIn(("act", "ti_high", "invoke"), acts(tree))   # read only, no pick

    def test_switches_when_checked_level_differs(self):
        tree, _ = select("fresh_hydrogen_std")
        self.assertIn(("act", "ti_high", "invoke"), acts(tree))
        self.assertIn("Selected High", checked_items(load("plan_flow_en_synthetic.json"), "th_hydrogen_high"))
        self.assertEqual(tree.state, "fresh_hydrogen_high")


class TestModeWithoutThinkingLevels(DriverHarness):
    def test_missing_thinking_submenu_fails_closed_before_sending(self):
        spec = load("plan_flow_en_synthetic.json")
        for d in nodes(spec):
            d["children"] = [c for c in d.get("children") or () if c.get("id") != "mi_thinking"]
        tree = AutoTree(spec)
        with patch.dict(os.environ, {"GEMINI_DR_MODE": "flash", "GEMINI_DR_THINKING": "high"}):
            rc, out, err = self.run_main(["--step", "plan", "--raw-prompt", "--prompt", "Research batteries."], tree)
        rec = self.stderr_record(err)
        self.assertEqual((rc, rec["code"], rec["details"]["locator"]), (3, "UI_LOCATOR_MISS", "thinking_menu"))
        self.assertNotIn(("act", "b_send", "invoke"), tree.actions)
        self.assertEqual([a for a in tree.actions if a[0] == "set_value"], [])
        self.assertIsInstance(locators.LocatorMiss({"code": "x"}), Exception)
        self.assertEqual(rdr.exit_code_for("UI_LOCATOR_MISS"), 3)


if __name__ == "__main__":
    unittest.main()

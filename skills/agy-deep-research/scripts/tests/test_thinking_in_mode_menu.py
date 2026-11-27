"""
test_thinking_in_mode_menu.py - the 2026-11 Gemini UI folds the thinking level into the
model-mode menu: the levels are menuitemradio entries under a '사고 수준' / 'Thinking level'
submenu, and there is no separate thinking button. A requested level must be selected there,
never skipped silently (research once ran at 보통 although 'high' was requested).

UI Automation path (replaces the Playwright label-parsing tests): run_deep_research._select_mode
opens the mode menu, picks the model, opens the thinking submenu and invokes the matching item by
the names the menu shows at run time; the requested word is retried in the other language
('high' -> '높음'). Selection is verified by the item's checked state, not by the button label.
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
from test_plan_steps import AutoTree, acts, cfg, load, locs, rename  # noqa: E402


def select(state, model="flash", thinking="high", spec=None):
    spec = spec if spec is not None else load("plan_flow_en_synthetic.json")
    spec["state"] = state
    tree = AutoTree(spec)
    run = rdr.StepRun(args=None, tree=tree, locs=locs(), cfg=cfg(), job=None, session=None)
    env = {"GEMINI_DR_MODE": model, "GEMINI_DR_THINKING": thinking}
    with patch.dict(os.environ, env):
        name = rdr._select_mode(run)
    return tree, name


class TestThinkingCandidates(unittest.TestCase):
    def test_requested_word_first_then_the_other_language(self):
        self.assertEqual(rdr._thinking_candidates("high"), ["high", "높음"])
        self.assertEqual(rdr._thinking_candidates("높음"), ["높음", "high"])
        self.assertEqual(rdr._thinking_candidates("Medium"), ["Medium", "보통", "중간"])
        self.assertEqual(rdr._thinking_candidates("turbo"), ["turbo"])

    def test_no_level_requested(self):
        self.assertEqual(rdr._thinking_candidates(None), [None])
        self.assertEqual(rdr._thinking_candidates("  "), [None])


class TestSelectModeSetsThinking(unittest.TestCase):
    def test_switches_standard_to_high_via_mode_menu(self):
        tree, name = select("fresh_hydrogen_std")
        self.assertIn(("act", "b_mode", "expand"), acts(tree))   # the menu was opened
        self.assertIn(("act", "ti_high", "invoke"), acts(tree))
        self.assertEqual(tree.state, "fresh_hydrogen_high")
        self.assertIn("Hydrogen", name)

    def test_switches_with_korean_level_names(self):
        spec = load("plan_flow_en_synthetic.json")
        rename(spec, "High", "높음")
        rename(spec, "Selected High", "선택됨 높음")
        tree, _ = select("fresh_hydrogen_std", spec=spec)
        self.assertIn(("act", "ti_high", "invoke"), acts(tree))
        self.assertEqual(tree.state, "fresh_hydrogen_high")

    def test_leaves_matching_level_alone(self):
        tree, _ = select("fresh_hydrogen_high")
        self.assertNotIn(("act", "ti_high", "invoke"), acts(tree))
        self.assertNotIn(("act", "mi_hydrogen", "invoke"), acts(tree))
        self.assertEqual(tree.state, "fresh_hydrogen_high")

    def test_fails_when_switch_does_not_stick(self):
        spec = load("plan_flow_en_synthetic.json")
        for tr in spec["transitions"]:
            if tr.get("from") == "th_hydrogen_std" and tr["on"] == ["act", "ti_high", "invoke"]:
                tr["to"] = "fresh_hydrogen_std"   # the menu closes but the level stays Standard
        with self.assertRaises(steps.StepError) as cm:
            select("fresh_hydrogen_std", spec=spec)
        self.assertEqual(cm.exception.code, "MODEL_NOT_SELECTED")
        self.assertEqual(cm.exception.details["kind"], "thinking")


if __name__ == "__main__":
    unittest.main()

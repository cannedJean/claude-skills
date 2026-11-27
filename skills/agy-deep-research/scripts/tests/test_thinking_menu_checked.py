"""
test_thinking_menu_checked.py - on 2026-11-16 the Argon mode button read only 'Argon'
(aria-label '현재 Argon 모드 사용 중') while the open mode menu showed the thinking levels with
' 낮음 속도 및 효율성' aria-checked="true". The driver logged 'no thinking level visible' and
never verified or set the requested level. When the button carries no level, the driver must
read the checked level item from the menu and switch only if it differs.
"""
import sandbox  # noqa: F401  (must stay the first import: blocks real-app access)
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import run_deep_research as rdr


def _argon_page(evaluate_returns):
    """Mode button label never shows a level; page.evaluate returns the given sequence
    (menu reads return the checked item's text, picks return True/False)."""
    page = MagicMock()
    mode_btn = MagicMock()
    mode_btn.count.return_value = 1
    mode_btn.is_visible.return_value = True
    mode_btn.inner_text.return_value = "Argon"
    mode_btn.get_attribute.return_value = "모드 선택 도구 열기, 현재 Argon 모드 사용 중"
    none_loc = MagicMock()
    none_loc.count.return_value = 0

    def locator(sel):
        wrapper = MagicMock()
        wrapper.first = mode_btn if "mode-menu" in sel else none_loc
        return wrapper

    page.locator.side_effect = locator
    page.evaluate.side_effect = list(evaluate_returns)
    return page, mode_btn


def _run(page, thinking="low"):
    with patch.dict(os.environ, {"GEMINI_DR_MODE": "Argon", "GEMINI_DR_THINKING": thinking}), \
         patch.object(rdr.time, "sleep"), patch.object(rdr, "log") as log:
        rdr.ensure_pro_mode(page)
    return [str(c.args[0]) for c in log.call_args_list]


class TestThinkingFromCheckedMenuItem(unittest.TestCase):
    def test_reads_checked_level_and_leaves_matching_alone(self):
        page, _ = _argon_page([" 낮음 속도 및 효율성"])
        logs = _run(page, "low")
        self.assertTrue(any("Thinking level already set to: low (menu)" in m for m in logs), logs)
        self.assertEqual(page.evaluate.call_count, 1)  # read only, no pick

    def test_switches_when_checked_level_differs(self):
        page, _ = _argon_page([" 보통 균형 잡힌 깊이", True, " 낮음 속도 및 효율성"])
        logs = _run(page, "low")
        picks = [c.args for c in page.evaluate.call_args_list if len(c.args) > 1]
        self.assertTrue(any("낮음" in a[1] for a in picks), picks)
        self.assertTrue(any("Switched thinking level to: low" in m for m in logs), logs)

    def test_warns_when_menu_still_shows_other_level(self):
        page, _ = _argon_page([" 보통 균형 잡힌 깊이", True, " 보통 균형 잡힌 깊이"])
        logs = _run(page, "low")
        self.assertTrue(any("WARNING" in m and "thinking" in m.lower() for m in logs), logs)

    def test_mode_without_thinking_levels(self):
        page, _ = _argon_page([None])
        logs = _run(page, "low")
        self.assertTrue(any("no thinking level offered" in m for m in logs), logs)


def _argon_page_button_level(menu_after_pick):
    """2026-11-16 14:41: right after switching to Argon the button read 'Argon\\n높음'. After picking
    낮음 the button showed only 'Argon' (no level) while the menu had 낮음 checked, and the driver
    logged "WARNING: thinking level is 'None'" although the switch had worked."""
    page = MagicMock()
    mode_btn = MagicMock()
    state = {"picked": False}
    mode_btn.count.return_value = 1
    mode_btn.is_visible.return_value = True
    mode_btn.inner_text.side_effect = lambda: "Argon" if state["picked"] else "Argon\n높음"
    mode_btn.get_attribute.return_value = "모드 선택 도구 열기, 현재 Argon 모드 사용 중"
    none_loc = MagicMock()
    none_loc.count.return_value = 0

    def locator(sel):
        wrapper = MagicMock()
        wrapper.first = mode_btn if "mode-menu" in sel else none_loc
        return wrapper

    def evaluate(js, *args):
        if args:  # picking a level item
            state["picked"] = True
            return True
        return menu_after_pick if state["picked"] else " 높음 매우 철저함"

    page.locator.side_effect = locator
    page.evaluate.side_effect = evaluate
    return page, mode_btn


class TestVerifyAfterButtonStopsShowingLevel(unittest.TestCase):
    def test_blank_button_after_switch_is_verified_from_menu(self):
        page, _ = _argon_page_button_level(" 낮음 속도 및 효율성")
        logs = _run(page, "low")
        self.assertTrue(any("Switched thinking level to: low" in m for m in logs), logs)
        self.assertFalse(any("WARNING" in m for m in logs), logs)

    def test_blank_button_and_menu_still_other_level_warns(self):
        page, _ = _argon_page_button_level(" 높음 매우 철저함")
        logs = _run(page, "low")
        self.assertTrue(any("WARNING" in m and "'high'" in m for m in logs), logs)


if __name__ == "__main__":
    unittest.main()

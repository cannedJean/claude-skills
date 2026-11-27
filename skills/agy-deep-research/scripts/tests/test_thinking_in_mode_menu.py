"""
test_thinking_in_mode_menu.py - the 2026-11 Gemini UI folds the thinking level into the
model-mode menu: the button reads 'Pro\\n보통' and the levels are menuitemradio entries
(' 낮음 Fast and efficient', ' 보통 균형 잡힌 깊이', ' 높음 매우 철저함') under a '사고 수준'
header. There is no separate thinking button, so ensure_pro_mode used to skip the level
silently and research ran at 보통 although 'high' was requested.
"""
import sandbox  # noqa: F401  (must stay the first import: blocks real-app access)
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import run_deep_research as rdr


class TestThinkingLevelParsing(unittest.TestCase):
    def test_reads_level_from_mode_button_label(self):
        self.assertEqual(rdr.thinking_level_of("Pro\n보통"), "medium")
        self.assertEqual(rdr.thinking_level_of("Pro\n높음"), "high")
        self.assertEqual(rdr.thinking_level_of("모드 선택 도구 열기, 현재 Pro 낮음 모드 사용 중"), "low")
        self.assertEqual(rdr.thinking_level_of("Pro High"), "high")

    def test_no_level_in_label(self):
        self.assertIsNone(rdr.thinking_level_of("Pro"))
        self.assertIsNone(rdr.thinking_level_of(""))


def _page_with_mode_button(label_sequence, clicked_ok=True):
    """A page whose only visible control is the mode button; label changes after a switch."""
    page = MagicMock()
    mode_btn = MagicMock()
    mode_btn.count.return_value = 1
    mode_btn.is_visible.return_value = True
    labels = iter(label_sequence)
    current = {"v": next(labels)}

    def inner_text():
        return current["v"]

    def advance(*_a, **_k):
        try:
            current["v"] = next(labels)
        except StopIteration:
            pass
        return clicked_ok

    mode_btn.inner_text.side_effect = inner_text
    mode_btn.get_attribute.return_value = ""
    none_loc = MagicMock()
    none_loc.count.return_value = 0

    def locator(sel):
        wrapper = MagicMock()
        wrapper.first = mode_btn if "mode-menu" in sel else none_loc
        return wrapper

    page.locator.side_effect = locator
    page.evaluate.side_effect = advance
    return page, mode_btn


class TestEnsureProModeSetsThinking(unittest.TestCase):
    def test_switches_medium_to_high_via_mode_menu(self):
        page, mode_btn = _page_with_mode_button(["Pro\n보통", "Pro\n높음"])
        with patch.dict(os.environ, {"GEMINI_DR_MODE": "Pro", "GEMINI_DR_THINKING": "high"}), \
             patch.object(rdr.time, "sleep"), patch.object(rdr, "log") as log:
            rdr.ensure_pro_mode(page)
        mode_btn.evaluate.assert_called()  # the menu was opened
        js_args = [c.args for c in page.evaluate.call_args_list]
        self.assertTrue(any(len(a) > 1 and "높음" in a[1] for a in js_args), js_args)
        self.assertTrue(any("Switched thinking level" in str(c.args[0]) for c in log.call_args_list))

    def test_leaves_matching_level_alone(self):
        page, mode_btn = _page_with_mode_button(["Pro\n높음"])
        with patch.dict(os.environ, {"GEMINI_DR_MODE": "Pro", "GEMINI_DR_THINKING": "high"}), \
             patch.object(rdr.time, "sleep"), patch.object(rdr, "log"):
            rdr.ensure_pro_mode(page)
        mode_btn.evaluate.assert_not_called()

    def test_warns_when_switch_does_not_stick(self):
        page, _ = _page_with_mode_button(["Pro\n보통", "Pro\n보통"], clicked_ok=True)
        with patch.dict(os.environ, {"GEMINI_DR_MODE": "Pro", "GEMINI_DR_THINKING": "high"}), \
             patch.object(rdr.time, "sleep"), patch.object(rdr, "log") as log:
            rdr.ensure_pro_mode(page)
        self.assertTrue(any("WARNING" in str(c.args[0]) and "thinking" in str(c.args[0]).lower()
                            for c in log.call_args_list))


if __name__ == "__main__":
    unittest.main()

"""
test_model_lock.py - The Gemini model/thinking selection must be locked once, in the
new chat, before the research prompt is sent. Steps that operate on an existing
conversation (approve / collect via --resume-url, edit) must never click the model menu,
because the conversation's model is already bound and a page reload can show a
different default (observed: 'Flash') that the driver would then "fix" mid-run.
"""
import sandbox  # noqa: F401  (must stay the first import: blocks real-app access)
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import run_deep_research as rdr


class _Stop(Exception):
    pass


def _run_main(argv, page):
    """Run the driver's step flow on a fake page (the automation backend that supplies a real page is separate)."""
    args = rdr.parse_args(argv)
    with patch.object(rdr, "prevent_background_throttling"),          patch.object(rdr, "ensure_pro_mode") as epm:
        try:
            rdr.run_flow(page, args)
        except _Stop:
            pass
    return epm


class TestModelLock(unittest.TestCase):
    URL = "https://gemini.google.com/app/abc"

    def _page(self):
        page = MagicMock()
        page.url = self.URL
        page.locator.side_effect = _Stop()  # stop right after the mode-decision point
        return page

    def test_approve_on_existing_conversation_does_not_touch_model(self):
        epm = _run_main(["--step", "approve", "--resume-url", self.URL], self._page())
        epm.assert_not_called()

    def test_edit_step_does_not_touch_model(self):
        with patch.object(rdr, "apply_plan_edit", side_effect=_Stop()):
            epm = _run_main(["--step", "edit", "--resume-url", self.URL, "--edit-prompt", "Add a section."], self._page())
        epm.assert_not_called()

    def test_apply_plan_edit_does_not_touch_model(self):
        page = self._page()
        with patch.object(rdr, "ensure_pro_mode") as epm:
            with self.assertRaises(_Stop):
                rdr.apply_plan_edit(page, "Add a section.")
        epm.assert_not_called()

    def test_show_thinking_toggle_is_not_a_thinking_level_control(self):
        self.assertFalse(rdr.is_thinking_level_control("생각하는 과정 표시"))
        self.assertFalse(rdr.is_thinking_level_control("Show thinking"))
        self.assertTrue(rdr.is_thinking_level_control("사고 수준: 높음"))
        self.assertTrue(rdr.is_thinking_level_control("Thinking level: High"))


if __name__ == "__main__":
    unittest.main()

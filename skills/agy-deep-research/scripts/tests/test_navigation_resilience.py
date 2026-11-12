"""
test_navigation_resilience.py - wait_for_report_completion reloads the page every 60s;
a poll that lands mid-navigation raises 'Execution context was destroyed'. That is a
transient condition and must not kill the detached worker (observed DRIVER_CRASH twice).
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import run_deep_research as rdr

DONE = {"isCardCompleted": True, "hasWorkingIndicator": False, "statusText": "", "chipText": "",
        "isChipStillWriting": False, "hasImmersiveChip": True, "hasImmersivePanel": True,
        "reportTextLen": 900}
NAV = Exception("Page.evaluate: Execution context was destroyed, most likely because of a navigation")


class TestNavigationResilience(unittest.TestCase):
    def test_poll_survives_navigation_error(self):
        page = MagicMock()
        page.url = "https://gemini.google.com/app/x"
        page.evaluate.side_effect = [NAV, DONE, DONE, {"markdown": "# R\n" + "x" * 900, "sources": []}]
        with patch.object(rdr.time, "sleep"):
            out = rdr.wait_for_report_completion(page, timeout_sec=60)
        self.assertEqual(out["status"], "completed")

    def test_extraction_survives_navigation_error(self):
        page = MagicMock()
        page.url = "https://gemini.google.com/app/x"
        page.evaluate.side_effect = [DONE, DONE, NAV, {"markdown": "# R\n" + "x" * 900, "sources": []}]
        with patch.object(rdr.time, "sleep"):
            out = rdr.wait_for_report_completion(page, timeout_sec=60)
        self.assertTrue(out["markdown"].startswith("# R"))

    def test_non_navigation_errors_still_raise(self):
        page = MagicMock()
        page.evaluate.side_effect = [ValueError("boom")]
        with patch.object(rdr.time, "sleep"), self.assertRaises(ValueError):
            rdr.wait_for_report_completion(page, timeout_sec=60)


if __name__ == "__main__":
    unittest.main()

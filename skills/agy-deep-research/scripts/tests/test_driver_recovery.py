"""
test_driver_recovery.py - Empirical test harness for 60s freeze recovery and error guards.
Tests that page.reload() and prevent_background_throttling() are fully guarded
against unhandled exceptions across wait_for_new_plan and wait_for_report_completion.
"""
import unittest
from unittest.mock import MagicMock, patch
import time
import sys
import os

# Ensure scripts dir is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import run_deep_research


class TestDriverRecoveryGuards(unittest.TestCase):
    def test_prevent_background_throttling_normal(self):
        """Verify normal execution of prevent_background_throttling."""
        mock_page = MagicMock()
        mock_cdp = MagicMock()
        mock_page.context.new_cdp_session.return_value = mock_cdp

        # Should execute without raising
        run_deep_research.prevent_background_throttling(mock_page)

        mock_page.context.new_cdp_session.assert_called_once_with(mock_page)
        mock_cdp.send.assert_any_call("Page.enable")
        mock_cdp.send.assert_any_call("Page.setWebLifecycleState", {"state": "active"})
        mock_cdp.send.assert_any_call("Emulation.setFocusEmulationEnabled", {"enabled": True})

    def test_prevent_background_throttling_cdp_session_failure(self):
        """Verify that failure creating CDP session is caught and non-fatal."""
        mock_page = MagicMock()
        mock_page.context.new_cdp_session.side_effect = RuntimeError("CDP session creation failed")

        # Must not raise
        try:
            run_deep_research.prevent_background_throttling(mock_page)
        except Exception as e:
            self.fail(f"prevent_background_throttling raised unhandled exception: {e}")

    def test_prevent_background_throttling_cdp_send_failure(self):
        """Verify that failure in cdp.send is caught and non-fatal."""
        mock_page = MagicMock()
        mock_cdp = MagicMock()
        mock_cdp.send.side_effect = Exception("Protocol error: Target closed")
        mock_page.context.new_cdp_session.return_value = mock_cdp

        # Must not raise
        try:
            run_deep_research.prevent_background_throttling(mock_page)
        except Exception as e:
            self.fail(f"prevent_background_throttling raised unhandled exception on send failure: {e}")

    def test_wait_for_new_plan_reload_exception_guarded(self):
        """Verify wait_for_new_plan catches exceptions in page.reload without crashing."""
        mock_page = MagicMock()
        mock_page.url = "https://gemini.google.com/app/12345"

        # page.reload throws an error (e.g. TargetClosed or network error)
        mock_page.reload.side_effect = RuntimeError("Page reload connection reset")

        # Timeline:
        # call 0: start_time = 0.0
        # call 1: loop check 1: time.time() = 10.0 (elapsed=10.0) -> no reload
        # call 2: loop check 2: time.time() = 70.0 (elapsed=70.0 >= 60) -> triggers reload! reload throws -> caught!
        # call 3: loop check 3: time.time() = 80.0 (elapsed=80.0) -> plan ready -> returns!
        time_seq = [0.0, 10.0, 70.0, 70.0, 70.0, 80.0, 80.0, 80.0]
        step = [0]
        def fake_time():
            idx = min(step[0], len(time_seq) - 1)
            val = time_seq[idx]
            step[0] += 1
            return val

        def fake_locator(sel):
            loc = MagicMock()
            is_final = (step[0] >= 5)
            if "research-steps" in sel:
                loc.count.return_value = 1 if is_final else 0
                loc.last.count.return_value = 1 if is_final else 0
                loc.last.inner_text.return_value = "Mock Plan Steps"
                loc.last.locator.return_value.count.return_value = 0
            elif "confirm-button" in sel:
                loc.count.return_value = 1 if is_final else 0
                loc.last.count.return_value = 1 if is_final else 0
            elif "cancel-fuse-button" in sel:
                loc.count.return_value = 0
                loc.last.count.return_value = 0
            elif "model-response" in sel:
                loc.count.return_value = 1
                loc.last.count.return_value = 1
                loc.last.inner_text.return_value = "Generating plan..."
            else:
                loc.count.return_value = 0
                loc.last.count.return_value = 0
            return loc

        mock_page.locator = fake_locator

        with patch("time.time", side_effect=fake_time), \
             patch("time.sleep", return_value=None):
            result = run_deep_research.wait_for_new_plan(mock_page, prev_plan_count=0, timeout_sec=200)

        self.assertEqual(result["status"], "plan_ready")
        self.assertIn("Mock Plan Steps", result["plan"])
        mock_page.reload.assert_called()

    def test_wait_for_new_plan_wait_for_selector_timeout_guarded(self):
        """Verify wait_for_selector timeout after reload is caught inside try/except."""
        mock_page = MagicMock()
        mock_page.url = "https://gemini.google.com/app/abcde"
        mock_page.reload.return_value = None
        mock_page.wait_for_selector.side_effect = TimeoutError("Selector model-response timed out")

        time_seq = [0.0, 10.0, 70.0, 70.0, 70.0, 80.0, 80.0, 80.0]
        step = [0]
        def fake_time():
            idx = min(step[0], len(time_seq) - 1)
            val = time_seq[idx]
            step[0] += 1
            return val

        def fake_locator(sel):
            loc = MagicMock()
            is_final = (step[0] >= 5)
            if "research-steps" in sel:
                loc.count.return_value = 1 if is_final else 0
                loc.last.count.return_value = 1 if is_final else 0
                loc.last.inner_text.return_value = "Recovered Plan"
                loc.last.locator.return_value.count.return_value = 0
            elif "confirm-button" in sel:
                loc.count.return_value = 1 if is_final else 0
                loc.last.count.return_value = 1 if is_final else 0
            elif "cancel-fuse-button" in sel:
                loc.count.return_value = 0
                loc.last.count.return_value = 0
            elif "model-response" in sel:
                loc.count.return_value = 1
                loc.last.count.return_value = 1
                loc.last.inner_text.return_value = "Normal text"
            else:
                loc.count.return_value = 0
                loc.last.count.return_value = 0
            return loc

        mock_page.locator = fake_locator

        with patch("time.time", side_effect=fake_time), \
             patch("time.sleep", return_value=None):
            result = run_deep_research.wait_for_new_plan(mock_page, prev_plan_count=0, timeout_sec=200)

        self.assertEqual(result["status"], "plan_ready")
        self.assertIn("Recovered Plan", result["plan"])

    def test_wait_for_report_completion_reload_and_throttling_guarded(self):
        """Verify wait_for_report_completion catches reload exceptions and prevent_background_throttling exceptions."""
        mock_page = MagicMock()
        mock_page.url = "https://gemini.google.com/app/report123"

        # page.reload throws during 60s freeze recovery
        mock_page.reload.side_effect = Exception("Page crashed during reload")

        # start_time = 0.0, last_reload_time = 0.0
        # tick 1: t = 10.0 (elapsed=10s, reportTextLen=100, isChipStillWriting=True)
        # tick 2: t = 75.0 (t - last_reload_time = 75s > 60s) -> triggers reload! reload throws -> caught!
        # tick 3: t = 85.0 (reportTextLen=1500, isCardCompleted=True) -> tick 1 stable
        # tick 4: t = 90.0 (reportTextLen=1500, isCardCompleted=True) -> tick 2 stable -> complete!
        time_seq = [0.0, 0.0, 10.0, 10.0, 75.0, 75.0, 75.0, 85.0, 85.0, 90.0, 90.0, 95.0]
        step = [0]
        def fake_time():
            idx = min(step[0], len(time_seq) - 1)
            val = time_seq[idx]
            step[0] += 1
            return val

        def fake_evaluate(js, *args):
            if "convertNode" in str(js):
                return {"markdown": "# Deep Research Report\n\nFull findings...", "sources": []}
            curr_t = time_seq[min(step[0], len(time_seq) - 1)]
            if curr_t < 80.0:
                return {
                    "isCardCompleted": False,
                    "hasWorkingIndicator": True,
                    "statusText": "Investigating...",
                    "chipText": "Writing...",
                    "isChipStillWriting": True,
                    "hasImmersiveChip": False,
                    "hasImmersivePanel": False,
                    "reportTextLen": 100
                }
            else:
                return {
                    "isCardCompleted": True,
                    "hasWorkingIndicator": False,
                    "statusText": "Complete",
                    "chipText": "Complete",
                    "isChipStillWriting": False,
                    "hasImmersiveChip": True,
                    "hasImmersivePanel": True,
                    "reportTextLen": 1500
                }

        mock_page.evaluate = fake_evaluate

        with patch("time.time", side_effect=fake_time), \
             patch("time.sleep", return_value=None):
            result = run_deep_research.wait_for_report_completion(mock_page, timeout_sec=300)

        self.assertEqual(result["status"], "completed")
        self.assertIn("# Deep Research Report", result["markdown"])
        mock_page.reload.assert_called()


if __name__ == "__main__":
    unittest.main()

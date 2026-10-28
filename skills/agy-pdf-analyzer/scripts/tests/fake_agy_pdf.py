#!/usr/bin/env python3
"""fake_agy_pdf.py - agy stand-in for this skill's tests: agy-slide-analyzer's tests/fake_agy_task.py (scenarios
perfect | omit | hallucinate | digits | quota, set with FAKE_AGY_SCENARIO) plus

  omit_then_quota  the first session omits text (-> retry), the retry session hits RESOURCE_EXHAUSTED

Set AGY_TASK=<this file>. The shared fake is found next to this skill (or through FAKE_AGY_SLIDE_TESTS).
"""
import os
import sys
from pathlib import Path

SLIDE_TESTS = Path(os.environ.get("FAKE_AGY_SLIDE_TESTS")
                   or Path(__file__).resolve().parents[3] / "agy-slide-analyzer" / "scripts" / "tests")
sys.path.insert(0, str(SLIDE_TESTS))
import fake_agy_task as base  # noqa: E402


def main(argv) -> int:
    if os.environ.get("FAKE_AGY_SCENARIO") == "omit_then_quota" and "-f" in argv:
        prompt = Path(argv[argv.index("-f") + 1]).read_text(encoding="utf-8")
        os.environ["FAKE_AGY_SCENARIO"] = "quota" if "이전 시도에 대한 피드백" in prompt else "omit"
    return base.main(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv))

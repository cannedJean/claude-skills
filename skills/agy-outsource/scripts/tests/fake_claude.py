#!/usr/bin/env python3
"""fake_claude.py - stand-in for `claude -p` so the auditor, the referee and Claude contractors can be tested
without Claude. Set OUTSOURCE_CLAUDE=<this file>. The prompt arrives on stdin; the role is read from its first line.

  # Test audit ...     answers every listed test as traced (citing R1)
  # Failure triage ... answers code_bug for one failing test
  # Work order ...     writes the canned slugkit code of fake_agy_task.py into the cwd, like a contractor

FAKE_CLAUDE_SCENARIO: pass (default) | untraced (first test untraced) | skip (leaves the last test out)
| wrongmodel (reports a different model than asked for). The reported model is claude-<--model>-5-5.
"""
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fake_agy_task  # noqa: E402


def main(argv) -> int:
    args = argv[1:]
    model = args[args.index("--model") + 1] if "--model" in args else "sonnet"
    sc = os.environ.get("FAKE_CLAUDE_SCENARIO", "pass")
    model_id = model if model.startswith("claude-") else f"claude-{model}-5-5"
    if sc == "wrongmodel":
        model_id = "claude-opus-5-5"
    prompt = sys.stdin.buffer.read().decode("utf-8")
    log = os.environ.get("FAKE_CLAUDE_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps({"args": args, "cwd": os.getcwd(), "head": prompt[:60]}) + "\n")
    structured, result = None, "done"
    if prompt.startswith("# Test audit"):
        tests = re.findall(r"^- file `([^`]+)`, test `([^`]+)`$", prompt, re.M)
        if sc == "skip":
            tests = tests[:-1]
        structured = {"tests": [{"file": f, "test": t, "verdict": "traced", "items": ["R1"]} for f, t in tests],
                      "summary": "fake audit"}
        if sc == "untraced" and structured["tests"]:
            structured["tests"][0].update(verdict="untraced", items=[], problem="checks an unwritten rule",
                                          fix="add the rule")
    elif prompt.startswith("# Failure triage"):
        structured = {"failures": [{"test": "x::test", "verdict": "code_bug", "items": ["R1"]}], "summary": "fake"}
    elif prompt.startswith("# Work order"):
        unit = re.search(r"^# Work order (\S+) ", prompt, re.M).group(1)
        for rel, src in fake_agy_task.GOOD[unit].items():
            (Path.cwd() / rel).parent.mkdir(parents=True, exist_ok=True)
            (Path.cwd() / rel).write_text(src, encoding="utf-8")
        (Path.cwd() / "DELIVERY.json").write_text(json.dumps({
            "unit": unit, "files": sorted(fake_agy_task.GOOD[unit]), "assumptions": ["fake:claude"]}),
            encoding="utf-8")
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": result,
                      "structured_output": structured, "modelUsage": {model_id: {"outputTokens": 10}},
                      "total_cost_usd": 0.0, "permission_denials": []}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

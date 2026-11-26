#!/usr/bin/env python3
"""test_docs_consistency.py - keeps the documentation in step with deep_research.py.

Covers:
  - SKILL.md stays within its line budget and links every reference file (and every link resolves)
  - numbers the docs quote equal the constants in deep_research.py; values that were once wrong never come back
  - example commands in the docs parse with the real argparse parser and name jobs by bare id (isolated jobs root)
  - failure / warning codes the docs name exist in the scripts
"""
from __future__ import annotations

import contextlib
import io
import re
import shlex
import sys
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = TESTS_DIR.parent
SKILL_DIR = SCRIPTS_DIR.parent
sys.path.insert(0, str(SCRIPTS_DIR))
import deep_research as dr  # noqa: E402

SKILL_MD = SKILL_DIR / "SKILL.md"
AGY_SIDE_SKILL = SKILL_DIR / "references" / "SKILL.md"   # body of the agy-side skill, written for the agy agent
SKILL_MD_MAX_LINES = 150
ALL_DOCS = sorted([SKILL_MD, SKILL_DIR / "README.md", *(SKILL_DIR / "references").glob("*.md"),
                   *(SKILL_DIR / "prompts").glob("*.md")])
CALLER_DOCS = [p for p in ALL_DOCS if p != AGY_SIDE_SKILL]
SCRIPT_SOURCES = "".join((SCRIPTS_DIR / n).read_text(encoding="utf-8") for n in
                         ("deep_research.py", "relay_step.py", "run_deep_research.py", "patch_gemini_app.py"))
# values earlier revisions of the docs stated wrongly
STALE = ("420s", "600s", "--deadline 600", "11 tests", "5 deterministic", "Gemini 2.0", "ratusan", "35%",
         "Python 3.10", "2% weekly")


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def rel(path: Path) -> str:
    return path.relative_to(SKILL_DIR).as_posix()


def doc_commands(path: Path) -> list[str]:
    """deep_research.py invocations inside fenced code blocks, continuation lines joined, comments dropped."""
    cmds = []
    for block in re.findall(r"```[a-z]*\n(.*?)```", text(path), re.S):
        joined = re.sub(r"[\\`]\n\s*", " ", block)
        for line in joined.splitlines():
            m = re.match(r"\s*(?:\$DR|python\s+\"?(?:[^\s\"]*[/\\])?deep_research\.py\"?)\s+(.*)", line)
            if m:
                cmds.append(re.sub(r"\s+#\s.*$", "", m.group(1)).strip())
    return cmds


class DocsConsistencyTestCase(unittest.TestCase):
    def test_skill_md_line_budget(self) -> None:
        self.assertLessEqual(len(text(SKILL_MD).splitlines()), SKILL_MD_MAX_LINES)

    def test_skill_md_links_every_reference_and_links_resolve(self) -> None:
        body = text(SKILL_MD)
        for ref in (SKILL_DIR / "references").glob("*.md"):
            self.assertIn(f"references/{ref.name}", body, f"SKILL.md does not say when to read {ref.name}")
        for doc in CALLER_DOCS:
            for target in re.findall(r"\]\(([^)#]+\.md)\)", text(doc)):
                self.assertTrue((doc.parent / target).is_file(), f"{rel(doc)} links to missing {target}")

    def test_no_stale_values(self) -> None:
        for doc in ALL_DOCS:
            body = text(doc)
            for bad in STALE:
                self.assertNotIn(bad, body, f"{rel(doc)} still contains {bad!r}")

    def test_approval_reason_minimum_matches_constant(self) -> None:
        found = []
        for doc in CALLER_DOCS:
            for line in text(doc).splitlines():
                if "--reason" in line or "APPROVAL_GATE_REASON_TOO_SHORT" in line:
                    found += [int(n) for n in re.findall(r"(\d+)\+? characters", line)]
        self.assertTrue(found, "no doc states the --reason minimum")
        self.assertEqual(set(found), {dr.MIN_REASON_CHARS})

    def test_durations_in_caller_docs_are_code_constants(self) -> None:
        allowed = {dr.DEFAULT_TIMEOUT, dr.DEFAULT_PLAN_DEADLINE, dr.ARGON_TIMEOUT, dr.ARGON_PLAN_DEADLINE}
        for doc in CALLER_DOCS:
            for n in re.findall(r"(?<![\w.])(\d{3,4})\s?s\b", text(doc)):
                self.assertIn(int(n), allowed, f"{rel(doc)} quotes {n}s, which is no timeout constant")

    def test_report_minimum_not_misquoted(self) -> None:
        for doc in ALL_DOCS:
            for n in re.findall(r">=\s*(\d+)\s*char", text(doc)):
                self.assertIn(int(n), {dr.REPORT_MIN_CHARS, dr.MIN_REASON_CHARS, 2500},
                              f"{rel(doc)} quotes a {n}-character threshold")

    def test_listed_sensitive_keywords_are_real(self) -> None:
        for doc in ALL_DOCS:
            for line in text(doc).splitlines():
                if "keyword" not in line.lower():
                    continue
                for word in re.findall(r"`([가-힣]+)`", line):
                    self.assertTrue(any(kw in word for kw in dr.SENSITIVE_KEYWORDS),
                                    f"{rel(doc)} lists {word!r}, which SENSITIVE_KEYWORDS does not match")

    def test_python_version_is_stated_once_consistently(self) -> None:
        versions = set()
        for doc in ALL_DOCS:
            versions |= set(re.findall(r"Python (3\.\d+)(?:\+| or newer)", text(doc)))
        self.assertEqual(versions, {"3.9"})
        self.assertIn("Python 3.9+", dr.__doc__)

    def test_example_commands_parse_and_use_isolated_job_ids(self) -> None:
        parser = dr.build_parser()
        seen = 0
        for doc in CALLER_DOCS:
            for cmd in doc_commands(doc):
                seen += 1
                err = io.StringIO()
                try:
                    with contextlib.redirect_stderr(err):
                        ns = parser.parse_args(shlex.split(cmd))
                except SystemExit:
                    self.fail(f"{rel(doc)}: `{cmd}` does not parse: {err.getvalue().strip().splitlines()[-1:]}")
                job = getattr(ns, "job", None)
                if job:
                    self.assertFalse(job.startswith(".") or "/" in job or "\\" in job,
                                     f"{rel(doc)}: `{cmd}` names the job by path; use a bare id (GIT_REPO_ESCAPE inside a repo)")
        self.assertGreaterEqual(seen, 10, "expected the docs to carry example commands")

    def test_no_control_characters_in_docs_or_scripts(self) -> None:
        files = [*ALL_DOCS, *SCRIPTS_DIR.glob("*.py"), *TESTS_DIR.glob("*.py")]
        for f in files:
            bad = sorted({hex(ord(c)) for c in f.read_text(encoding="utf-8") if ord(c) < 32 and c not in "\n\t\r"})
            self.assertEqual(bad, [], f"{f.name} contains control characters (a mangled escape?)")

    def test_skill_md_shows_every_supervised_step(self) -> None:
        subcommands = [shlex.split(c)[0] for c in doc_commands(SKILL_MD)]
        for step in ("preflight", "init", "plan", "edit", "approve", "collect", "auto"):
            self.assertIn(step, subcommands, f"SKILL.md has no example for `{step}`")


if __name__ == "__main__":
    unittest.main()

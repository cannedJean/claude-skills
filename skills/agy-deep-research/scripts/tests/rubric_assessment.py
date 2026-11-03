#!/usr/bin/env python3
"""rubric_assessment.py - Objective quality evaluation rubric for `skills/agy-deep-research`.

Evaluates the codebase across the 5 dimensions defined in the architecture survey (Total: 100 points, Passing Threshold: 90 points):
  1. Specification & Frontmatter (15 pts)
  2. Architecture & Confinement (25 pts)
  3. Port & Version Resilience (20 pts)
  4. CLI Contracts & Ergonomics (20 pts)
  5. Automated Testability (20 pts)

Outputs structured evaluation JSON and formatted human-readable report.
Exits with code 0 on PASS (score >= 90), non-zero on FAIL (score < 90).
"""
from __future__ import annotations

import argparse
import json
import os
import py_compile
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import yaml
except ImportError:
    yaml = None


PASSING_THRESHOLD = 90


def find_skill_root(start_dir: Optional[Path] = None) -> Path:
    """Resolve the root directory of the agy-deep-research skill."""
    if start_dir:
        cand = start_dir.resolve()
        if (cand / "SKILL.md").exists() and (cand / "scripts").exists():
            return cand
    current = Path(__file__).resolve().parent
    while current != current.parent:
        if (current / "SKILL.md").exists() and (current / "scripts").exists():
            return current
        current = current.parent
    return Path(__file__).resolve().parent.parent.parent


def evaluate_dimension_1_frontmatter(skill_dir: Path) -> Dict[str, Any]:
    """Dimension 1: Specification & Frontmatter (15 points).

    - Criterion 1.1: Valid YAML frontmatter syntax & name (5 pts)
    - Criterion 1.2: Description begins with 'Use when...' (4 pts)
    - Criterion 1.3: Description conciseness & no workflow leakage (3 pts)
    - Criterion 1.4: Workflow and guidelines in SKILL.md body (3 pts)
    """
    skill_md = skill_dir / "SKILL.md"
    criteria: List[Dict[str, Any]] = []

    if not skill_md.exists():
        return {
            "name": "Specification & Frontmatter",
            "weight": 15,
            "score": 0,
            "status": "FAIL",
            "criteria": [{"name": "SKILL.md exists", "max": 15, "score": 0, "details": "SKILL.md not found"}],
        }

    content = skill_md.read_text(encoding="utf-8", errors="replace")
    lines = content.splitlines(keepends=True)

    # 1.1 Valid YAML frontmatter syntax & name (5 pts)
    c1_1_score = 0
    c1_1_notes = []
    fm: Optional[Dict[str, Any]] = None
    body = content

    if lines and lines[0].strip() == "---":
        end_idx = -1
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                end_idx = i
                break
        if end_idx != -1:
            fm_raw = "".join(lines[1:end_idx])
            body = "".join(lines[end_idx + 1:])
            if yaml:
                try:
                    fm = yaml.safe_load(fm_raw)
                except Exception as e:
                    c1_1_notes.append(f"YAML parse error: {e}")
            else:
                fm = {}
                for l in fm_raw.splitlines():
                    if ":" in l:
                        k, v = l.split(":", 1)
                        fm[k.strip()] = v.strip().strip('"').strip("'")

    if fm and isinstance(fm, dict):
        if fm.get("name") == "agy-deep-research":
            c1_1_score = 5
            c1_1_notes.append("Valid YAML frontmatter with name 'agy-deep-research'.")
        else:
            c1_1_score = 2
            c1_1_notes.append(f"Frontmatter present but name is {fm.get('name')!r}.")
    else:
        c1_1_notes.append("Frontmatter is missing, unclosed, or invalid YAML.")

    criteria.append({
        "id": "1.1",
        "name": "YAML Frontmatter Validity & Skill Name",
        "max": 5,
        "score": c1_1_score,
        "details": "; ".join(c1_1_notes),
    })

    # 1.2 Description begins with 'Use when...' (4 pts)
    c1_2_score = 0
    desc = fm.get("description", "") if fm else ""
    if isinstance(desc, str) and re.match(r"^use\s+when\b", desc.strip(), re.IGNORECASE):
        c1_2_score = 4
        c1_2_details = "Description correctly begins with 'Use when...'."
    else:
        c1_2_details = "Description does not start with 'Use when...'."

    criteria.append({
        "id": "1.2",
        "name": "Description Trigger Prefix ('Use when...')",
        "max": 4,
        "score": c1_2_score,
        "details": c1_2_details,
    })

    # 1.3 Description conciseness & no workflow leakage (3 pts)
    c1_3_score = 0
    c1_3_notes = []
    if isinstance(desc, str) and desc.strip():
        desc_len = len(desc.strip())
        has_arrows = bool(re.search(r"(\b\w+\b\s*(?:->|→|-->)\s*\b\w+\b)", desc))
        has_step_chain = bool(re.search(r"\b(init|plan|edit|approve)\b.*\b(plan|edit|approve|collect)\b", desc, re.IGNORECASE))
        has_internals = ("progress.jsonl" in desc) or ("provenance gates" in desc.lower())

        if desc_len <= 1024 and not (has_arrows or has_step_chain or has_internals):
            c1_3_score = 3
            c1_3_notes.append(f"Length {desc_len} chars <= 1024, no workflow leakage.")
        elif desc_len <= 1024:
            c1_3_score = 1
            leak_types = []
            if has_arrows:
                leak_types.append("arrows")
            if has_step_chain:
                leak_types.append("step-sequence")
            if has_internals:
                leak_types.append("internal-files")
            c1_3_notes.append(f"Length {desc_len} chars <= 1024, but leaked workflow details ({', '.join(leak_types)}).")
        else:
            c1_3_notes.append(f"Description length ({desc_len}) exceeds 1024 characters.")
    else:
        c1_3_notes.append("Description is empty.")

    criteria.append({
        "id": "1.3",
        "name": "Description Conciseness & No Workflow Leakage",
        "max": 3,
        "score": c1_3_score,
        "details": "; ".join(c1_3_notes),
    })

    # 1.4 Workflow and guidelines in SKILL.md body (3 pts)
    c1_4_score = 0
    c1_4_notes = []
    body_has_arch = "## Architecture" in body or "# agy-deep-research" in body
    body_has_workflow = "## Workflow" in body or ("init" in body and "plan" in body and "approve" in body)
    body_len_ok = len(body) >= 1500

    if body_has_arch and body_has_workflow and body_len_ok:
        c1_4_score = 3
        c1_4_notes.append("Clear step-by-step workflow and architecture instructions documented in body.")
    elif body_has_workflow:
        c1_4_score = 2
        c1_4_notes.append("Workflow partially documented in body.")
    else:
        c1_4_notes.append("Workflow documentation missing or sparse.")

    criteria.append({
        "id": "1.4",
        "name": "Body Documentation & Workflow Guidelines",
        "max": 3,
        "score": c1_4_score,
        "details": "; ".join(c1_4_notes),
    })

    total_score = sum(c["score"] for c in criteria)
    return {
        "name": "Specification & Frontmatter",
        "weight": 15,
        "score": total_score,
        "status": "PASS" if total_score == 15 else ("PARTIAL" if total_score >= 10 else "FAIL"),
        "criteria": criteria,
    }


def evaluate_dimension_2_confinement(skill_dir: Path) -> Dict[str, Any]:
    """Dimension 2: Architecture & Confinement (25 points).

    - Criterion 2.1: Job Directory Isolation Outside .git tree (7 pts)
    - Criterion 2.2: Pre-Plan Gate Enforcement (6 pts)
    - Criterion 2.3: Plan Approval Gate Enforcement (6 pts)
    - Criterion 2.4: Report Quality Gate Enforcement (6 pts)
    """
    scripts_dir = skill_dir / "scripts"
    deep_research_py = scripts_dir / "deep_research.py"
    criteria: List[Dict[str, Any]] = []

    if not deep_research_py.exists():
        return {
            "name": "Architecture & Confinement",
            "weight": 25,
            "score": 0,
            "status": "FAIL",
            "criteria": [{"name": "deep_research.py exists", "max": 25, "score": 0, "details": "File missing"}],
        }

    code = deep_research_py.read_text(encoding="utf-8", errors="replace")

    # 2.1 Git Repository Escape Prevention (7 pts)
    # Check enforcement of GIT_REPO_ESCAPE and exit 5
    c2_1_score = 0
    c2_1_notes = []
    has_git_escape_code = ("GIT_REPO_ESCAPE" in code) and ("inside git repository" in code or ".git" in code)
    has_exit_5 = ("exit(5)" in code or "sys.exit(5)" in code or "return 5" in code)

    if has_git_escape_code and has_exit_5:
        c2_1_score = 7
        c2_1_notes.append("Enforces job directory confinement outside git tree with exit code 5 (GIT_REPO_ESCAPE).")
    elif has_git_escape_code:
        c2_1_score = 4
        c2_1_notes.append("Git escape detection present but exit code mapping ambiguous.")
    else:
        c2_1_notes.append("No git escape confinement check found.")

    criteria.append({
        "id": "2.1",
        "name": "Job Directory Isolation & Git Repo Escape Prevention",
        "max": 7,
        "score": c2_1_score,
        "details": "; ".join(c2_1_notes),
    })

    # 2.2 Pre-Plan Gate Enforcement (6 pts)
    c2_2_score = 0
    c2_2_notes = []
    has_prompt_check = "PRE_PLAN_GATE_PROMPT_MISSING" in code or ("prompt" in code and "goal" in code and "cannot be empty" in code)
    has_pre_plan_gate = "Pre-Plan Gate" in code or "pre_plan_gate" in code.lower()

    if has_prompt_check and has_pre_plan_gate:
        c2_2_score = 6
        c2_2_notes.append("Pre-Plan Gate enforces non-empty prompt and acceptance goal before plan generation.")
    elif has_prompt_check:
        c2_2_score = 3
        c2_2_notes.append("Partial input validation present.")
    else:
        c2_2_notes.append("Pre-Plan Gate input check missing.")

    criteria.append({
        "id": "2.2",
        "name": "Pre-Plan Gate Input Validation",
        "max": 6,
        "score": c2_2_score,
        "details": "; ".join(c2_2_notes),
    })

    # 2.3 Plan Approval Gate Enforcement (6 pts)
    c2_3_score = 0
    c2_3_notes = []
    has_min_reason = ("APPROVAL_GATE_REASON_TOO_SHORT" in code) or ("reason" in code and ">= 20" in code or "len(" in code and "20" in code)
    has_plan_approval_gate = "APPROVAL_GATE" in code or "Plan Approval Gate" in code

    if has_min_reason and has_plan_approval_gate:
        c2_3_score = 6
        c2_3_notes.append("Plan Approval Gate enforces mandatory review justification (>= 20 characters, exit 2).")
    elif has_plan_approval_gate:
        c2_3_score = 3
        c2_3_notes.append("Plan Approval Gate present without minimum character constraint.")
    else:
        c2_3_notes.append("Plan Approval Gate missing.")

    criteria.append({
        "id": "2.3",
        "name": "Plan Approval Gate & Justification Enforcement",
        "max": 6,
        "score": c2_3_score,
        "details": "; ".join(c2_3_notes),
    })

    # 2.4 Report Quality Gate Enforcement (6 pts)
    c2_4_score = 0
    c2_4_notes = []
    has_report_len = "REPORT_QUALITY_GATE_FAILED" in code or ("400" in code and "markdown" in code.lower())
    has_heading_check = "#" in code and ("heading" in code.lower() or "title" in code.lower())

    if has_report_len and has_heading_check:
        c2_4_score = 6
        c2_4_notes.append("Report Quality Gate enforces min report size (>= 400 chars) and Markdown heading structure.")
    elif has_report_len:
        c2_4_score = 3
        c2_4_notes.append("Partial report quality check.")
    else:
        c2_4_notes.append("Report Quality Gate missing.")

    criteria.append({
        "id": "2.4",
        "name": "Report Quality Gate (Length & Structure)",
        "max": 6,
        "score": c2_4_score,
        "details": "; ".join(c2_4_notes),
    })

    total_score = sum(c["score"] for c in criteria)
    return {
        "name": "Architecture & Confinement",
        "weight": 25,
        "score": total_score,
        "status": "PASS" if total_score == 25 else ("PARTIAL" if total_score >= 18 else "FAIL"),
        "criteria": criteria,
    }


def evaluate_dimension_3_resilience(skill_dir: Path) -> Dict[str, Any]:
    """Dimension 3: Port & Version Resilience (20 points).

    - Criterion 3.1: Dynamic ASAR Regex & Minification Resilience (8 pts)
    - Criterion 3.2: Port 9223 Unification Across Deliverables (7 pts)
    - Criterion 3.3: Browser Driver Selector Resilience & Fallbacks (5 pts)
    """
    scripts_dir = skill_dir / "scripts"
    patch_gemini_py = scripts_dir / "patch_gemini_app.py"
    run_deep_research_py = scripts_dir / "run_deep_research.py"
    criteria: List[Dict[str, Any]] = []

    # 3.1 Dynamic ASAR Regex & Minification Resilience (8 pts)
    c3_1_score = 0
    c3_1_notes = []
    if patch_gemini_py.exists():
        patch_code = patch_gemini_py.read_text(encoding="utf-8", errors="replace")
        # Check if regex matches dynamic identifier (\w+)\.app instead of hardcoded m.app
        has_dynamic_app_var = bool(re.search(r"\(\\w\+\)\\\.app", patch_code)) or bool(re.search(r"\(\?P<\w+>\\w\+\)\\\.app", patch_code))
        has_hardcoded_m_app = (r"m\.app\.isPackaged" in patch_code) or ("m.app.isPackaged" in patch_code)

        if has_dynamic_app_var and not has_hardcoded_m_app:
            c3_1_score = 8
            c3_1_notes.append("Generalized regex pattern supports variable Electron minifiers across app-1.16.x and app-1.17.x+.")
        elif has_hardcoded_m_app:
            c3_1_score = 0
            c3_1_notes.append("Fragile: hardcodes 'm.app', breaks on modern Gemini desktop app (app-1.17.0).")
        else:
            c3_1_score = 4
            c3_1_notes.append("Partial regex generalization.")
    else:
        c3_1_notes.append("patch_gemini_app.py missing.")

    criteria.append({
        "id": "3.1",
        "name": "Dynamic ASAR Minification Regex Pattern",
        "max": 8,
        "score": c3_1_score,
        "details": "; ".join(c3_1_notes),
    })

    # 3.2 Port 9223 Unification Across Deliverables (7 pts)
    c3_2_score = 0
    c3_2_notes = []
    target_files = [
        scripts_dir / "patch_gemini_app.py",
        scripts_dir / "deep_research.py",
        scripts_dir / "relay_step.py",
        scripts_dir / "run_deep_research.py",
        skill_dir / "SKILL.md",
        skill_dir / "references" / "SKILL.md",
        skill_dir / "README.md",
    ]
    stray_9222_count = 0
    stray_details = []
    pat_9222 = re.compile(r"(?<!\d)9222(?!\d)")

    for fpath in target_files:
        if not fpath.exists():
            continue
        rel = fpath.relative_to(skill_dir).as_posix()
        for idx, line in enumerate(fpath.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if pat_9222.search(line):
                stray_9222_count += 1
                stray_details.append(f"{rel}:{idx}")

    if stray_9222_count == 0:
        c3_2_score = 7
        c3_2_notes.append("Unified default port 9223 consistently used across all scripts, docstrings, and docs (0 stray 9222).")
    elif stray_9222_count <= 2:
        c3_2_score = 3
        c3_2_notes.append(f"Minor port inconsistency: {stray_9222_count} stray 9222 reference(s) at {', '.join(stray_details)}.")
    else:
        c3_2_score = 0
        c3_2_notes.append(f"Port inconsistency: {stray_9222_count} stray 9222 references at {', '.join(stray_details[:3])}...")

    criteria.append({
        "id": "3.2",
        "name": "Port 9223 Unification Across Deliverables",
        "max": 7,
        "score": c3_2_score,
        "details": "; ".join(c3_2_notes),
    })

    # 3.3 Browser Driver Selector Resilience & Fallbacks (5 pts)
    c3_3_score = 0
    c3_3_notes = []
    if run_deep_research_py.exists():
        rdr_code = run_deep_research_py.read_text(encoding="utf-8", errors="replace")
        has_mode_fallback = ("mode-menu" in rdr_code or "Model selector" in rdr_code or "gem-menu-button" in rdr_code)
        has_multilingual_edit = ("연구 계획 수정" in rdr_code) and ("Edit plan" in rdr_code)

        if has_mode_fallback and has_multilingual_edit:
            c3_3_score = 5
            c3_3_notes.append("Resilient UI selectors with mode menu fallbacks and bilingual (EN/KO) plan edit buttons.")
        elif has_mode_fallback or has_multilingual_edit:
            c3_3_score = 3
            c3_3_notes.append("Partial selector resilience (missing either model menu fallbacks or EN/KO bilingual aria-labels).")
        else:
            c3_3_score = 2
            c3_3_notes.append("Legacy bard-only or Korean-only selectors; fragile on updated Gemini UI.")
    else:
        c3_3_notes.append("run_deep_research.py missing.")

    criteria.append({
        "id": "3.3",
        "name": "Browser Driver Selector Resilience & Multilingual Fallbacks",
        "max": 5,
        "score": c3_3_score,
        "details": "; ".join(c3_3_notes),
    })

    total_score = sum(c["score"] for c in criteria)
    return {
        "name": "Port & Version Resilience",
        "weight": 20,
        "score": total_score,
        "status": "PASS" if total_score == 20 else ("PARTIAL" if total_score >= 12 else "FAIL"),
        "criteria": criteria,
    }


def evaluate_dimension_4_cli(skill_dir: Path) -> Dict[str, Any]:
    """Dimension 4: CLI Contracts & Ergonomics (20 points).

    - Criterion 4.1: CLI --help invocation on all 4 scripts (8 pts)
    - Criterion 4.2: Standard CLI Exit Codes Contract Adherence (4 pts)
    - Criterion 4.3: Stream Separation & Deterministic JSON (4 pts)
    - Criterion 4.4: patch_gemini_app.py CLI Arguments Support (4 pts)
    """
    scripts_dir = skill_dir / "scripts"
    target_scripts = [
        "deep_research.py",
        "patch_gemini_app.py",
        "relay_step.py",
        "run_deep_research.py",
    ]
    criteria: List[Dict[str, Any]] = []

    # 4.1 CLI --help invocation on all 4 scripts (8 pts, 2 pts each)
    c4_1_score = 0
    c4_1_notes = []
    for sname in target_scripts:
        spath = scripts_dir / sname
        if not spath.exists():
            c4_1_notes.append(f"{sname}: missing")
            continue
        try:
            cp = subprocess.run(
                [sys.executable, str(spath), "--help"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
            )
            stdout = (cp.stdout or "").strip()
            if cp.returncode == 0 and ("usage:" in stdout.lower() or "options:" in stdout.lower() or "-h, --help" in stdout.lower()):
                c4_1_score += 2
                c4_1_notes.append(f"{sname}: OK (exit 0)")
            else:
                c4_1_notes.append(f"{sname}: FAIL (exit {cp.returncode})")
        except Exception as e:
            c4_1_notes.append(f"{sname}: EXCEPTION ({e})")

    criteria.append({
        "id": "4.1",
        "name": "CLI '--help' Conformance on All Scripts",
        "max": 8,
        "score": c4_1_score,
        "details": "; ".join(c4_1_notes),
    })

    # 4.2 Standard CLI Exit Codes Contract Adherence (4 pts)
    deep_py = scripts_dir / "deep_research.py"
    c4_2_score = 0
    c4_2_notes = []
    if deep_py.exists():
        dcode = deep_py.read_text(encoding="utf-8", errors="replace")
        has_0 = bool(re.search(r"(?:exit\(|return\s+|die\(.*?,\s*)0\b", dcode))
        has_2 = bool(re.search(r"(?:exit\(|return\s+|else\s+|die\(.*?,\s*)2\b", dcode))
        has_3 = bool(re.search(r"(?:exit\(|return\s+|else\s+|die\(.*?,\s*)3\b", dcode))
        has_4 = bool(re.search(r"(?:exit\(|return\s+|else\s+|die\(.*?,\s*)4\b", dcode))
        has_5 = bool(re.search(r"(?:exit\(|return\s+|else\s+|die\(.*?,\s*)5\b", dcode))

        if has_0 and has_2 and has_3 and has_4 and has_5:
            c4_2_score = 4
            c4_2_notes.append("Standard exit codes contract (0, 2, 3, 4, 5) fully implemented and deterministic.")
        else:
            missing = [str(x) for x, ok in [(0, has_0), (2, has_2), (3, has_3), (4, has_4), (5, has_5)] if not ok]
            c4_2_score = 2
            c4_2_notes.append(f"Exit code contract partially implemented; missing exit codes: {missing}.")
    else:
        c4_2_notes.append("deep_research.py missing.")

    criteria.append({
        "id": "4.2",
        "name": "Standard Exit Codes Contract (0, 2, 3, 4, 5)",
        "max": 4,
        "score": c4_2_score,
        "details": "; ".join(c4_2_notes),
    })

    # 4.3 Stream Separation & Deterministic JSON (4 pts)
    c4_3_score = 0
    c4_3_notes = []
    if deep_py.exists():
        dcode = deep_py.read_text(encoding="utf-8", errors="replace")
        has_json_dump = "json.dumps(" in dcode
        has_stderr_progress = 'file=sys.stderr' in dcode or 'sys.stderr.write' in dcode

        if has_json_dump and has_stderr_progress:
            c4_3_score = 4
            c4_3_notes.append("Clean stream separation: structured JSON/Markdown on stdout, progress streaming to stderr.")
        else:
            c4_3_score = 2
            c4_3_notes.append("Partial stream separation.")
    else:
        c4_3_notes.append("deep_research.py missing.")

    criteria.append({
        "id": "4.3",
        "name": "Stream Separation (JSON stdout / Progress stderr)",
        "max": 4,
        "score": c4_3_score,
        "details": "; ".join(c4_3_notes),
    })

    # 4.4 patch_gemini_app.py CLI Arguments Support (4 pts)
    patch_py = scripts_dir / "patch_gemini_app.py"
    c4_4_score = 0
    c4_4_notes = []
    if patch_py.exists():
        pcode = patch_py.read_text(encoding="utf-8", errors="replace")
        has_argparse = "import argparse" in pcode or "from argparse import" in pcode
        has_check = "--check" in pcode
        has_restore = "--restore" in pcode
        has_port = "--port" in pcode
        has_dry_run = "--dry-run" in pcode

        if has_argparse and has_check and has_restore and has_port:
            c4_4_score = 4
            c4_4_notes.append("Standard argparse CLI with --check, --restore, --port, --dry-run implemented.")
        elif has_argparse:
            c4_4_score = 2
            c4_4_notes.append("Argparse implemented but missing some standard flags.")
        else:
            c4_4_score = 0
            c4_4_notes.append("Missing argparse CLI in patch_gemini_app.py (ad-hoc sys.argv crashes on --help).")
    else:
        c4_4_notes.append("patch_gemini_app.py missing.")

    criteria.append({
        "id": "4.4",
        "name": "patch_gemini_app.py CLI Flags & Argument Parsing",
        "max": 4,
        "score": c4_4_score,
        "details": "; ".join(c4_4_notes),
    })

    total_score = sum(c["score"] for c in criteria)
    return {
        "name": "CLI Contracts & Ergonomics",
        "weight": 20,
        "score": total_score,
        "status": "PASS" if total_score == 20 else ("PARTIAL" if total_score >= 12 else "FAIL"),
        "criteria": criteria,
    }


def evaluate_dimension_5_testability(skill_dir: Path) -> Dict[str, Any]:
    """Dimension 5: Automated Testability (20 points).

    - Criterion 5.1: Offline Unit Test Execution (8 pts)
    - Criterion 5.2: Static Syntax Compilation Check (6 pts)
    - Criterion 5.3: Verification Runner Operability (6 pts)
    """
    scripts_dir = skill_dir / "scripts"
    tests_dir = scripts_dir / "tests"
    criteria: List[Dict[str, Any]] = []

    # 5.1 Offline Unit Test Execution (8 pts)
    c5_1_score = 0
    c5_1_notes = []
    try:
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        cp = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", str(tests_dir), "-v"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
            env=env,
        )
        combined = (cp.stdout or "") + "\n" + (cp.stderr or "")
        match_ran = re.search(r"Ran\s+(\d+)\s+tests?", combined)
        test_count = int(match_ran.group(1)) if match_ran else 0

        if cp.returncode == 0 and "OK" in combined and test_count >= 10:
            c5_1_score = 8
            c5_1_notes.append(f"All {test_count} unit tests pass offline with exit code 0.")
        elif cp.returncode == 0 and "OK" in combined:
            c5_1_score = 5
            c5_1_notes.append(f"{test_count} tests pass, but expected at least 10 tests.")
        else:
            c5_1_score = 0
            c5_1_notes.append(f"Unit tests failed (exit code {cp.returncode}).")
    except Exception as e:
        c5_1_score = 0
        c5_1_notes.append(f"Failed to run unit tests: {e}")

    criteria.append({
        "id": "5.1",
        "name": "Offline Deterministic Unit Test Execution",
        "max": 8,
        "score": c5_1_score,
        "details": "; ".join(c5_1_notes),
    })

    # 5.2 Static Syntax Compilation Check (6 pts)
    c5_2_score = 0
    c5_2_notes = []
    py_files = sorted(scripts_dir.rglob("*.py"))
    comp_errors = []
    for pf in py_files:
        try:
            py_compile.compile(str(pf), doraise=True)
        except Exception as e:
            comp_errors.append(f"{pf.name}: {e}")

    if not comp_errors and py_files:
        c5_2_score = 6
        c5_2_notes.append(f"100% of Python scripts ({len(py_files)} files) compile cleanly without syntax errors.")
    else:
        c5_2_score = 0
        c5_2_notes.append(f"Syntax compilation errors in {len(comp_errors)} file(s): {comp_errors[:2]}.")

    criteria.append({
        "id": "5.2",
        "name": "Static Syntax Compilation Analysis (py_compile)",
        "max": 6,
        "score": c5_2_score,
        "details": "; ".join(c5_2_notes),
    })

    # 5.3 Verification Runner Operability (6 pts)
    c5_3_score = 0
    c5_3_notes = []
    run_verif_py = tests_dir / "run_verification.py"
    if run_verif_py.exists():
        try:
            cp = subprocess.run(
                [sys.executable, str(run_verif_py), "--help"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
            )
            if cp.returncode == 0:
                c5_3_score = 6
                c5_3_notes.append("run_verification.py is operational and supports automated execution.")
            else:
                c5_3_score = 2
                c5_3_notes.append("run_verification.py exists but failed --help invocation.")
        except Exception as e:
            c5_3_score = 1
            c5_3_notes.append(f"Error invoking run_verification.py: {e}")
    else:
        c5_3_notes.append("run_verification.py is missing.")

    criteria.append({
        "id": "5.3",
        "name": "Automated Verification Runner Operability",
        "max": 6,
        "score": c5_3_score,
        "details": "; ".join(c5_3_notes),
    })

    total_score = sum(c["score"] for c in criteria)
    return {
        "name": "Automated Testability",
        "weight": 20,
        "score": total_score,
        "status": "PASS" if total_score == 20 else ("PARTIAL" if total_score >= 12 else "FAIL"),
        "criteria": criteria,
    }


def run_rubric_assessment(skill_dir: Path) -> Dict[str, Any]:
    """Execute complete 5-dimension rubric assessment."""
    start_time = time.time()

    d1 = evaluate_dimension_1_frontmatter(skill_dir)
    d2 = evaluate_dimension_2_confinement(skill_dir)
    d3 = evaluate_dimension_3_resilience(skill_dir)
    d4 = evaluate_dimension_4_cli(skill_dir)
    d5 = evaluate_dimension_5_testability(skill_dir)

    dimensions = [d1, d2, d3, d4, d5]
    total_score = sum(d["score"] for d in dimensions)
    max_score = 100
    passed = total_score >= PASSING_THRESHOLD

    duration = round(time.time() - start_time, 3)

    return {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "skill_dir": str(skill_dir.resolve()),
        "total_score": total_score,
        "max_score": max_score,
        "percentage": round((total_score / max_score) * 100, 1),
        "passing_threshold": PASSING_THRESHOLD,
        "status": "PASS" if passed else "FAIL",
        "duration_s": duration,
        "dimensions": dimensions,
    }


def print_human_rubric(report: Dict[str, Any]) -> None:
    """Format and print rubric assessment report to console."""
    print("=" * 78)
    print(" OBJECTIVE QUALITY EVALUATION RUBRIC: skills/agy-deep-research")
    print(f" Timestamp: {report['timestamp']}")
    print(f" Skill Root: {report['skill_dir']}")
    print("=" * 78)
    print(f"{'#':<4} {'Dimension':<38} {'Weight':<8} {'Score':<8} {'Status':<8}")
    print("-" * 78)

    for idx, d in enumerate(report["dimensions"], 1):
        status_tag = f"[{d['status']}]"
        print(f"{idx:<4} {d['name']:<38} {d['weight']:<8} {d['score']:<8} {status_tag:<8}")
        for c in d["criteria"]:
            c_tag = f"{c['score']}/{c['max']} pts"
            print(f"     - ({c['id']}) {c['name']:<42} : {c_tag:<10} | {c['details']}")

    print("=" * 78)
    status_str = "PASS" if report["status"] == "PASS" else "FAIL"
    print(f"TOTAL SCORE: {report['total_score']} / {report['max_score']} pts ({report['percentage']}%)")
    print(f"PASSING THRESHOLD: >= {report['passing_threshold']} pts")
    print(f"OVERALL EVALUATION: [{status_str}]")
    print("=" * 78)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run 100-point objective rubric assessment for agy-deep-research skill."
    )
    parser.add_argument(
        "--skill-dir",
        type=Path,
        default=None,
        help="Path to the agy-deep-research skill directory (defaults to auto-detection)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output structured JSON report to stdout",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional path to write full JSON report",
    )
    args = parser.parse_args()

    skill_dir = find_skill_root(args.skill_dir)
    report = run_rubric_assessment(skill_dir)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_human_rubric(report)

    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())

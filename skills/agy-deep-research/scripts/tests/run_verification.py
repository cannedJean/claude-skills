#!/usr/bin/env python3
"""run_verification.py - Automated verification check runner for `skills/agy-deep-research`.

Executes deterministic automated verification checks:
  1. Static syntax compilation (`py_compile`) on all Python scripts in `scripts/`.
  2. CLI `--help` invocation checks for all 4 scripts (`deep_research.py`, `patch_gemini_app.py`,
     `relay_step.py`, `run_deep_research.py`), verifying exit code 0.
  3. Frontmatter validation for `SKILL.md`: valid YAML, `name: agy-deep-research`,
     `description` begins with `"Use when..."`, description length <= 1024 characters,
     and no leaked workflow implementation details.
  4. Port unification check: verifies default port 9223 is consistently used across scripts,
     docstrings, and documentation (no stray 9222 references).
  5. Offline unit test execution: runs `unittest discover` and verifies exit code 0.

Outputs a structured JSON report and returns exit code 0 on pass, non-zero on failure.
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
    # Fallback to standard relative layout
    return Path(__file__).resolve().parent.parent.parent


def check_syntax_compilation(scripts_dir: Path) -> Dict[str, Any]:
    """Check static syntax compilation for all Python files under scripts/."""
    start_time = time.time()
    py_files = sorted(scripts_dir.rglob("*.py"))
    files_checked: List[str] = []
    errors: List[Dict[str, str]] = []

    for py_file in py_files:
        rel_path = py_file.relative_to(scripts_dir).as_posix()
        files_checked.append(rel_path)
        try:
            py_compile.compile(str(py_file), doraise=True)
        except py_compile.PyCompileError as e:
            errors.append({
                "file": rel_path,
                "error": str(e).strip()
            })
        except Exception as e:
            errors.append({
                "file": rel_path,
                "error": f"Unexpected compilation error: {e}"
            })

    duration = round(time.time() - start_time, 3)
    passed = len(errors) == 0
    return {
        "passed": passed,
        "duration_s": duration,
        "total_files": len(files_checked),
        "files_checked": files_checked,
        "errors": errors,
    }


def check_cli_help(scripts_dir: Path) -> Dict[str, Any]:
    """Verify that all core scripts output help text and exit with status code 0 on --help."""
    start_time = time.time()
    target_scripts = [
        "deep_research.py",
        "patch_gemini_app.py",
        "relay_step.py",
        "run_deep_research.py",
    ]
    results: Dict[str, Dict[str, Any]] = {}
    all_passed = True

    for script_name in target_scripts:
        script_path = scripts_dir / script_name
        if not script_path.exists():
            results[script_name] = {
                "passed": False,
                "exit_code": -1,
                "error": f"Script not found at {script_path}",
                "stdout_snippet": "",
                "stderr_snippet": "",
            }
            all_passed = False
            continue

        cmd = [sys.executable, str(script_path), "--help"]
        try:
            cp = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
            )
            stdout = (cp.stdout or "").strip()
            stderr = (cp.stderr or "").strip()
            has_usage = ("usage:" in stdout.lower() or "options:" in stdout.lower()
                         or "--help" in stdout.lower() or "-h, --help" in stdout.lower())
            script_passed = (cp.returncode == 0) and has_usage

            results[script_name] = {
                "passed": script_passed,
                "exit_code": cp.returncode,
                "error": stderr if cp.returncode != 0 else "",
                "stdout_snippet": stdout[:200] if stdout else "",
                "stderr_snippet": stderr[:200] if stderr else "",
            }
            if not script_passed:
                all_passed = False
        except subprocess.TimeoutExpired:
            results[script_name] = {
                "passed": False,
                "exit_code": -2,
                "error": "Execution timed out after 15 seconds",
                "stdout_snippet": "",
                "stderr_snippet": "",
            }
            all_passed = False
        except Exception as e:
            results[script_name] = {
                "passed": False,
                "exit_code": -3,
                "error": str(e),
                "stdout_snippet": "",
                "stderr_snippet": "",
            }
            all_passed = False

    duration = round(time.time() - start_time, 3)
    return {
        "passed": all_passed,
        "duration_s": duration,
        "scripts_checked": target_scripts,
        "results": results,
    }


def parse_frontmatter(content: str) -> Tuple[Optional[Dict[str, Any]], str, Optional[str]]:
    """Parse YAML frontmatter delimited by '---' from markdown content."""
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return None, content, "File does not start with '---' frontmatter delimiter."

    end_idx = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break

    if end_idx == -1:
        return None, content, "Unterminated frontmatter; missing closing '---'."

    fm_raw = "".join(lines[1:end_idx])
    body = "".join(lines[end_idx + 1:])

    if yaml is not None:
        try:
            parsed = yaml.safe_load(fm_raw)
            if not isinstance(parsed, dict):
                return None, body, "Frontmatter YAML did not parse to a dictionary/mapping."
            return parsed, body, None
        except Exception as e:
            return None, body, f"YAML parse error: {e}"
    else:
        # Fallback rudimentary parser if PyYAML is unavailable
        parsed = {}
        for line in fm_raw.splitlines():
            line_str = line.strip()
            if not line_str or line_str.startswith("#"):
                continue
            if ":" in line_str:
                k, v = line_str.split(":", 1)
                parsed[k.strip()] = v.strip().strip('"').strip("'")
        return parsed, body, None


def check_frontmatter(skill_dir: Path) -> Dict[str, Any]:
    """Validate SKILL.md frontmatter according to writing-skills specification."""
    start_time = time.time()
    skill_md = skill_dir / "SKILL.md"
    errors: List[str] = []

    if not skill_md.exists():
        return {
            "passed": False,
            "duration_s": 0.0,
            "errors": [f"SKILL.md not found at {skill_md}"],
        }

    try:
        content = skill_md.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return {
            "passed": False,
            "duration_s": 0.0,
            "errors": [f"Failed to read SKILL.md: {e}"],
        }

    fm, body, parse_err = parse_frontmatter(content)
    if parse_err:
        errors.append(parse_err)
        return {
            "passed": False,
            "duration_s": round(time.time() - start_time, 3),
            "errors": errors,
        }

    assert fm is not None
    name = fm.get("name")
    if name != "agy-deep-research":
        errors.append(f"Frontmatter 'name' must be 'agy-deep-research', got: {name!r}")

    description = fm.get("description", "")
    if not isinstance(description, str) or not description.strip():
        errors.append("Frontmatter 'description' must be a non-empty string.")
        description_str = ""
    else:
        description_str = description.strip()

    desc_len = len(description_str)
    if desc_len > 1024:
        errors.append(f"Frontmatter 'description' length ({desc_len}) exceeds maximum 1024 characters.")

    starts_with_use_when = bool(re.match(r"^use\s+when\b", description_str, re.IGNORECASE))
    if not starts_with_use_when:
        errors.append("Frontmatter 'description' must begin with 'Use when...'.")

    # Workflow leakage checks:
    # 1. Progression arrows like `->` or `→`
    # 2. Sequential lifecycle step chains (`init` -> `plan` -> `edit` -> `approve`)
    # 3. Internal file references like `relay/progress.jsonl`
    has_leakage = False
    leakage_reasons: List[str] = []
    if re.search(r"(\b\w+\b\s*(?:->|→|-->)\s*\b\w+\b)", description_str):
        has_leakage = True
        leakage_reasons.append("Contains workflow progression arrows (e.g. '->' or '→').")

    if re.search(r"\b(init|plan|edit|approve|collect)\b.*\b(plan|edit|approve|collect)\b", description_str, re.IGNORECASE):
        has_leakage = True
        leakage_reasons.append("Summarizes internal step sequence (e.g. init -> plan -> approve).")

    if "progress.jsonl" in description_str or "provenance gates" in description_str.lower():
        has_leakage = True
        leakage_reasons.append("Leakes internal file/gate details (e.g. progress.jsonl).")

    if has_leakage:
        errors.extend([f"Workflow leakage in description: {r}" for r in leakage_reasons])

    duration = round(time.time() - start_time, 3)
    passed = len(errors) == 0
    return {
        "passed": passed,
        "duration_s": duration,
        "name": name,
        "description_length": desc_len,
        "starts_with_use_when": starts_with_use_when,
        "no_workflow_leakage": not has_leakage,
        "errors": errors,
    }


def check_port_unification(skill_dir: Path) -> Dict[str, Any]:
    """Verify that default port 9223 is consistently used and no stray 9222 references exist."""
    start_time = time.time()
    target_files = [
        skill_dir / "scripts" / "patch_gemini_app.py",
        skill_dir / "scripts" / "deep_research.py",
        skill_dir / "scripts" / "relay_step.py",
        skill_dir / "scripts" / "run_deep_research.py",
        skill_dir / "SKILL.md",
        skill_dir / "references" / "SKILL.md",
        skill_dir / "README.md",
    ]

    stray_9222_found: List[Dict[str, Any]] = []
    port_9223_confirmed: List[str] = []
    port_9222_pattern = re.compile(r"(?<!\d)9222(?!\d)")
    port_9223_pattern = re.compile(r"(?<!\d)9223(?!\d)")

    for fpath in target_files:
        if not fpath.exists():
            continue
        rel_path = fpath.relative_to(skill_dir).as_posix()
        try:
            lines = fpath.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception as e:
            stray_9222_found.append({
                "file": rel_path,
                "line": 0,
                "content": f"Failed to read file: {e}"
            })
            continue

        file_has_9223 = False
        for line_idx, line in enumerate(lines, start=1):
            if port_9222_pattern.search(line):
                stray_9222_found.append({
                    "file": rel_path,
                    "line": line_idx,
                    "content": line.strip()
                })
            if port_9223_pattern.search(line):
                file_has_9223 = True

        if file_has_9223:
            port_9223_confirmed.append(rel_path)

    duration = round(time.time() - start_time, 3)
    passed = len(stray_9222_found) == 0
    return {
        "passed": passed,
        "duration_s": duration,
        "target_port": 9223,
        "port_9223_confirmed_files": port_9223_confirmed,
        "stray_9222_count": len(stray_9222_found),
        "stray_9222_found": stray_9222_found,
        "errors": [
            f"Stray 9222 reference at {item['file']}:{item['line']} -> {item['content']}"
            for item in stray_9222_found
        ],
    }


def check_unit_tests(tests_dir: Path) -> Dict[str, Any]:
    """Execute unit test discovery and verify all tests pass offline."""
    start_time = time.time()
    cmd = [sys.executable, "-m", "unittest", "discover", "-s", str(tests_dir), "-v"]

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    try:
        cp = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
            env=env,
        )
        combined_output = (cp.stdout or "") + "\n" + (cp.stderr or "")
        match_ran = re.search(r"Ran\s+(\d+)\s+tests?", combined_output)
        tests_run = int(match_ran.group(1)) if match_ran else 0

        passed = (cp.returncode == 0) and ("OK" in combined_output) and (tests_run > 0)
        duration = round(time.time() - start_time, 3)

        return {
            "passed": passed,
            "duration_s": duration,
            "exit_code": cp.returncode,
            "tests_run": tests_run,
            "output_snippet": combined_output[-500:].strip() if combined_output else "",
            "errors": [] if passed else [combined_output[-800:].strip()],
        }
    except subprocess.TimeoutExpired:
        return {
            "passed": False,
            "duration_s": round(time.time() - start_time, 3),
            "exit_code": -2,
            "tests_run": 0,
            "output_snippet": "",
            "errors": ["Unit test execution timed out after 90 seconds."],
        }
    except Exception as e:
        return {
            "passed": False,
            "duration_s": round(time.time() - start_time, 3),
            "exit_code": -3,
            "tests_run": 0,
            "output_snippet": "",
            "errors": [str(e)],
        }


def run_all_verification(skill_dir: Path) -> Dict[str, Any]:
    """Run all automated verification checks and return structured report."""
    scripts_dir = skill_dir / "scripts"
    tests_dir = scripts_dir / "tests"

    syntax_res = check_syntax_compilation(scripts_dir)
    cli_res = check_cli_help(scripts_dir)
    fm_res = check_frontmatter(skill_dir)
    port_res = check_port_unification(skill_dir)
    unit_res = check_unit_tests(tests_dir)

    all_passed = (
        syntax_res["passed"]
        and cli_res["passed"]
        and fm_res["passed"]
        and port_res["passed"]
        and unit_res["passed"]
    )

    checks = {
        "syntax_compilation": syntax_res,
        "cli_help": cli_res,
        "frontmatter": fm_res,
        "port_unification": port_res,
        "unit_tests": unit_res,
    }

    total_checks = len(checks)
    passed_count = sum(1 for c in checks.values() if c["passed"])
    failed_count = total_checks - passed_count

    return {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "skill_dir": str(skill_dir.resolve()),
        "all_passed": all_passed,
        "summary": {
            "total_checks": total_checks,
            "passed": passed_count,
            "failed": failed_count,
        },
        "checks": checks,
    }


def print_human_report(report: Dict[str, Any]) -> None:
    """Print clean formatted human-readable report."""
    print("=" * 72)
    print(" AUTOMATED VERIFICATION REPORT: skills/agy-deep-research")
    print(f" Timestamp: {report['timestamp']}")
    print(f" Skill Root: {report['skill_dir']}")
    print("=" * 72)

    checks = report["checks"]

    for name, c in checks.items():
        status_tag = "[PASS]" if c["passed"] else "[FAIL]"
        dur = f"{c.get('duration_s', 0.0):.2f}s"
        print(f"{status_tag} {name.replace('_', ' ').title():<30} (took {dur})")

        if not c["passed"]:
            errors = c.get("errors", [])
            if not errors and "results" in c:
                for sub_k, sub_v in c["results"].items():
                    if not sub_v.get("passed"):
                        errors.append(f"{sub_k}: {sub_v.get('error', 'failed')}")
            for err in errors[:5]:
                print(f"       ERROR: {err}")
            if len(errors) > 5:
                print(f"       ... and {len(errors) - 5} more errors")

    print("-" * 72)
    summary = report["summary"]
    status_str = "OVERALL STATUS: PASS" if report["all_passed"] else "OVERALL STATUS: FAIL"
    print(f"{status_str} ({summary['passed']}/{summary['total_checks']} checks passed)")
    print("=" * 72)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run automated verification suite for agy-deep-research skill."
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
        help="Output structured JSON to stdout instead of human-readable summary",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional path to write full JSON report",
    )
    args = parser.parse_args()

    skill_dir = find_skill_root(args.skill_dir)
    report = run_all_verification(skill_dir)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_human_report(report)

    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())

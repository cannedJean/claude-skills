#!/usr/bin/env python3
"""deep_research.py - Claude Code orchestrator for Gemini Deep Research via headless `agy`.

Subcommands
  preflight   verify agy, Playwright, Gemini desktop app CDP, and skill script sync status
  setup       synchronize bundled scripts and SKILL.md into ~/.gemini/config/skills/local-gemini-deep-research
  init        create an isolated job directory outside any git repo and lock prompt.md + goal.md
  plan        run the 'plan' relay step via agy-delegate --deep-research and capture the research plan
  edit        submit plan modification feedback ('edit' relay step) and capture the revised plan
  approve     enforce the Plan Approval Gate, start detached research execution, stream progress.jsonl, and collect report
  auto        unattended end-to-end execution (init + detached auto step + live progress stream + report gate)
  status      inspect job state, detached worker PID liveness, and latest progress
  collect     re-attach to a running/finished job, enforce Report Quality Gate, and write report + .meta.json

Exit codes: 0 ok, 2 research/gate failure, 3 runtime/driver/agy error, 4 preflight failure, 5 bad arguments/isolation error.
Python 3.9+, stdlib only.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import glob
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        with contextlib.suppress(Exception):
            _s.reconfigure(encoding="utf-8", errors="replace")

IS_WINDOWS = os.name == "nt"
SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
DEFAULT_AGY_TASK = SKILL_DIR.parent / "agy-delegate" / "scripts" / "agy_task.py"
DEFAULT_TARGET_SKILL_DIR = Path.home() / ".gemini" / "config" / "skills" / "local-gemini-deep-research"
BUNDLED_SCRIPTS = ("relay_step.py", "run_deep_research.py", "patch_gemini_app.py")
BUNDLED_REF_SKILL = SKILL_DIR / "references" / "SKILL.md"
RELAY_TASK_TEMPLATE = SKILL_DIR / "prompts" / "agy_relay_task.md"

RELAY_VERSION = 2
REPORT_MIN_CHARS = 400
MIN_REASON_CHARS = 20
DEFAULT_TIMEOUT = 1200
DEFAULT_PLAN_DEADLINE = 420
SENSITIVE_KEYWORDS = ("민주주의", "선거", "정치", "대통령", "정당", "투표")


# --------------------------------------------------------------------------- utilities
def now_iso() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def log(msg: str) -> None:
    sys.stderr.write(f"[deep-research {_dt.datetime.now():%H:%M:%S}] {msg}\n")
    sys.stderr.flush()


def die(msg: str, exit_code: int, **extra) -> None:
    log("ERROR: " + msg)
    payload = {"status": "error", "exit_code": exit_code, "message": msg}
    payload.update(extra)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    sys.exit(exit_code)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def write_json_atomic(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def read_json_safe(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def pid_alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if IS_WINDOWS:
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            h = k32.OpenProcess(0x1000, False, pid)
            if not h:
                return False
            code = ctypes.c_ulong()
            ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
            k32.CloseHandle(h)
            return bool(ok) and code.value == 259
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------- job isolation
def jobs_root() -> Path:
    """Return base directory for isolated deep-research jobs outside any git repository."""
    if os.environ.get("AGY_DR_JOBS"):
        return Path(os.environ["AGY_DR_JOBS"]).expanduser().resolve()
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base).resolve() / "agy-deep-research" / "jobs"
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base).resolve() / "agy-deep-research" / "jobs"


def check_job_dir(job_dir: Path) -> Path:
    """Refuse job directories inside any git repository or at home/root."""
    resolved = Path(job_dir).expanduser().resolve()
    if resolved == Path.home().resolve() or resolved.parent == resolved:
        die(f"refusing to use {resolved} as job directory (too broad)", 5, code="INVALID_JOB_DIR")
    for d in [resolved, *resolved.parents]:
        if (d / ".git").exists():
            die(
                f"refusing job directory {resolved} inside git repository {d}: "
                "agy treats the enclosing git repository as its workspace. "
                "Use a job directory outside any repository or set AGY_DR_JOBS.",
                5,
                code="GIT_REPO_ESCAPE",
            )
    return resolved


def resolve_job_dir(job_arg: str | None, must_exist: bool = True) -> Path:
    if not job_arg or not str(job_arg).strip():
        die("provide --job <job_id_or_path>", 5, code="MISSING_JOB")
    raw = str(job_arg).strip()
    cand_path = Path(raw).expanduser()
    if cand_path.is_absolute() or "/" in raw or "\\" in raw or raw.startswith("."):
        job_dir = cand_path.resolve()
    else:
        root_cand = jobs_root() / raw
        if root_cand.exists() or not cand_path.exists():
            job_dir = root_cand.resolve()
        else:
            job_dir = cand_path.resolve()
    job_dir = check_job_dir(job_dir)
    if must_exist and not job_dir.is_dir():
        die(f"job directory does not exist: {job_dir}", 5, code="JOB_NOT_FOUND")
    return job_dir


def allocate_job_dir(job_arg: str | None = None) -> tuple[str, Path]:
    if job_arg and str(job_arg).strip():
        raw = str(job_arg).strip()
        cand = Path(raw).expanduser()
        if cand.is_absolute() or "/" in raw or "\\" in raw or raw.startswith("."):
            job_dir = check_job_dir(cand.resolve())
            job_id = job_dir.name
        else:
            root = check_job_dir(jobs_root())
            job_id = raw
            job_dir = check_job_dir(root / job_id)
    else:
        root = check_job_dir(jobs_root())
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        job_id = f"{stamp}-{secrets.token_hex(4)}"
        job_dir = check_job_dir(root / job_id)
    job_dir.mkdir(parents=True, exist_ok=True)
    return job_id, job_dir


# --------------------------------------------------------------------------- gates & text inputs
def resolve_text_input(text_val: str | None, file_val: str | None, label: str) -> str:
    if file_val:
        p = Path(file_val).expanduser().resolve()
        if not p.is_file():
            die(f"{label} file not found: {p}", 5, code="INPUT_FILE_NOT_FOUND")
        content = read_text(p).strip()
    elif text_val is not None:
        content = str(text_val).strip()
    else:
        content = ""
    if not content:
        die(f"non-empty --{label} or --{label}-file is required", 5, code=f"EMPTY_{label.upper()}")
    return content


def check_korean_safety_keywords(prompt_text: str) -> list[str]:
    hits = [kw for kw in SENSITIVE_KEYWORDS if kw in prompt_text]
    if hits:
        log(
            f"WARNING: prompt contains sensitive Korean political/election keyword(s) ({', '.join(hits)}) "
            "which may trigger Gemini Web safety filters (SAFETY_BLOCKED). Consider rephrasing."
        )
    return hits


def enforce_pre_plan_gate(job_dir: Path) -> tuple[str, str, list[str]]:
    check_job_dir(job_dir)
    prompt_path = job_dir / "prompt.md"
    goal_path = job_dir / "goal.md"
    if not prompt_path.is_file() or not read_text(prompt_path).strip():
        die(f"Pre-Plan Gate failed: {prompt_path} is missing or empty. Run `init` with --prompt and --goal first.", 2,
            code="PRE_PLAN_GATE_PROMPT_MISSING")
    if not goal_path.is_file() or not read_text(goal_path).strip():
        die(f"Pre-Plan Gate failed: {goal_path} is missing or empty. Fixed review criteria in goal.md are required before generating a plan.", 2,
            code="PRE_PLAN_GATE_GOAL_MISSING")
    prompt_text = read_text(prompt_path).strip()
    goal_text = read_text(goal_path).strip()
    warnings = check_korean_safety_keywords(prompt_text)
    return prompt_text, goal_text, warnings


def enforce_approval_gate(job_dir: Path, job: dict, reason: str | None) -> str:
    check_job_dir(job_dir)
    enforce_pre_plan_gate(job_dir)
    status = job.get("status")
    if status not in ("plan_ready", "plan_revised"):
        die(
            f"Plan Approval Gate failed: job status is {status!r} (must be 'plan_ready' or 'plan_revised' before approve).",
            2,
            code="APPROVAL_GATE_INVALID_STATE",
        )
    if not (job.get("latest_plan") or "").strip():
        die("Plan Approval Gate failed: no reviewed research plan recorded on this job.", 2,
            code="APPROVAL_GATE_NO_PLAN")
    clean_reason = (reason or "").strip()
    if len(clean_reason) < MIN_REASON_CHARS:
        die(
            f"Plan Approval Gate failed: --reason must be at least {MIN_REASON_CHARS} characters explaining how the plan satisfies goal.md (got {len(clean_reason)} chars).",
            5,
            code="APPROVAL_GATE_REASON_TOO_SHORT",
        )
    return clean_reason


def enforce_report_quality_gate(markdown: str) -> dict:
    clean = (markdown or "").strip()
    chars = len(clean)
    if chars < REPORT_MIN_CHARS:
        die(
            f"Report Quality Gate failed: report has {chars} characters (minimum required is {REPORT_MIN_CHARS}).",
            2,
            code="REPORT_QUALITY_GATE_FAILED",
            report_chars=chars,
        )
    has_heading = bool(re.search(r"^#{1,6}\s+\S+", clean, re.M))
    if not has_heading:
        die(
            "Report Quality Gate failed: report lacks valid Markdown section headings (# ...).",
            2,
            code="REPORT_STRUCTURE_INVALID",
            report_chars=chars,
        )
    sources_match = re.search(r"^## 참고 출처 \(Sources\)\s*$(.*)", clean, re.M | re.S)
    sources_count = len(re.findall(r"^\d+\. \[", sources_match.group(1), re.M)) if sources_match else len(re.findall(r"\[[^\]]+\]\(https?://[^)]+\)", clean))
    return {"report_chars": chars, "has_heading": has_heading, "sources_count": sources_count}


# --------------------------------------------------------------------------- skill sync & preflight
def resolve_target_skill_dir(explicit: str | None = None) -> Path:
    cand = explicit or os.environ.get("AGY_DR_SKILL_DIR") or os.environ.get("AGY_CONFINE_DR_DIR")
    return Path(cand).expanduser().resolve() if cand else DEFAULT_TARGET_SKILL_DIR.resolve()


def resolve_active_dr_skill_dir(explicit: str | None = None) -> Path:
    """Return the skill directory to pass as --dr-skill-dir to agy_task.py.
    Uses target dir if relay_step.py exists there, otherwise falls back to this skill's own directory.
    """
    target = resolve_target_skill_dir(explicit)
    if (target / "scripts" / "relay_step.py").is_file():
        return target
    return SKILL_DIR


def check_sync_status(target_dir: Path) -> dict:
    files_status = {}
    all_present = True
    all_matching = True
    for name in BUNDLED_SCRIPTS:
        src = SCRIPT_DIR / name
        dst = target_dir / "scripts" / name
        src_sha = sha256_file(src) if src.is_file() else None
        dst_sha = sha256_file(dst) if dst.is_file() else None
        if dst_sha is None:
            all_present = False
            all_matching = False
        elif src_sha != dst_sha:
            all_matching = False
        files_status[f"scripts/{name}"] = {"bundled_sha256": src_sha, "target_sha256": dst_sha, "in_sync": src_sha == dst_sha}

    dst_skill = target_dir / "SKILL.md"
    src_skill_sha = sha256_file(BUNDLED_REF_SKILL) if BUNDLED_REF_SKILL.is_file() else None
    dst_skill_sha = sha256_file(dst_skill) if dst_skill.is_file() else None
    if dst_skill_sha is None:
        all_present = False
        all_matching = False
    elif src_skill_sha != dst_skill_sha:
        all_matching = False
    files_status["SKILL.md"] = {"bundled_sha256": src_skill_sha, "target_sha256": dst_skill_sha, "in_sync": src_skill_sha == dst_skill_sha}

    if not target_dir.exists() or not all_present:
        status = "missing_target"
    elif all_matching:
        status = "in_sync"
    else:
        status = "out_of_sync"
    return {"status": status, "target_dir": str(target_dir), "files": files_status}


def cmd_setup(a) -> int:
    target_dir = resolve_target_skill_dir(getattr(a, "target_dir", None))
    scripts_dst = target_dir / "scripts"
    scripts_dst.mkdir(parents=True, exist_ok=True)

    copied, updated, unchanged = [], [], []
    for name in BUNDLED_SCRIPTS:
        src = SCRIPT_DIR / name
        if not src.is_file():
            die(f"bundled script missing: {src}", 3, code="BUNDLED_SCRIPT_MISSING")
        dst = scripts_dst / name
        rel = f"scripts/{name}"
        if not dst.is_file():
            shutil.copy2(src, dst)
            copied.append(rel)
        elif sha256_file(src) != sha256_file(dst):
            shutil.copy2(src, dst)
            updated.append(rel)
        else:
            unchanged.append(rel)

    if BUNDLED_REF_SKILL.is_file():
        dst_skill = target_dir / "SKILL.md"
        if not dst_skill.is_file():
            shutil.copy2(BUNDLED_REF_SKILL, dst_skill)
            copied.append("SKILL.md")
        elif sha256_file(BUNDLED_REF_SKILL) != sha256_file(dst_skill):
            shutil.copy2(BUNDLED_REF_SKILL, dst_skill)
            updated.append("SKILL.md")
        else:
            unchanged.append("SKILL.md")

    sync_info = check_sync_status(target_dir)
    out = {
        "status": "ok",
        "target_dir": str(target_dir),
        "copied": copied,
        "updated": updated,
        "unchanged": unchanged,
        "sync_status": sync_info["status"],
        "files": sync_info["files"],
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_preflight(a) -> int:
    target_dir = resolve_target_skill_dir(getattr(a, "target_dir", None))
    sync_info = check_sync_status(target_dir)
    checks: dict = {"sync": sync_info}

    agy_task = Path(os.environ.get("AGY_TASK") or DEFAULT_AGY_TASK).resolve()
    checks["agy_task_script"] = str(agy_task) if agy_task.is_file() else "(missing)"
    agy_ok = False
    if agy_task.is_file():
        try:
            cp = subprocess.run([sys.executable, str(agy_task), "preflight"], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=90)
            pf_json = json.loads(cp.stdout) if cp.stdout.strip().startswith("{") else {"rc": cp.returncode}
            checks["agy_preflight"] = pf_json
            agy_ok = bool(pf_json.get("ok")) or cp.returncode == 0
        except Exception as e:
            checks["agy_preflight"] = {"ok": False, "error": str(e)}

    # Check Playwright
    if os.environ.get("AGY_DR_ASSUME_PLAYWRIGHT") == "1" or (checks.get("agy_preflight") or {}).get("checks", {}).get("agy") == "fake":
        pw_ok = True
        checks["playwright"] = {"available": True, "note": "simulated"}
    else:
        try:
            cp = subprocess.run([sys.executable, "-c", "import playwright.sync_api; print('ok')"],
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
            pw_ok = cp.returncode == 0 and "ok" in cp.stdout
            checks["playwright"] = {"available": pw_ok, "stderr": cp.stderr.strip()[:200] if not pw_ok else ""}
        except Exception as e:
            pw_ok = False
            checks["playwright"] = {"available": False, "error": str(e)}

    # Check Gemini Desktop App & CDP
    cdp_port = int(os.environ.get("GEMINI_CDP_PORT", 9223))
    gemini_base = os.path.expandvars(r"%LOCALAPPDATA%\Google\Gemini") if IS_WINDOWS else ""
    app_dirs = sorted(glob.glob(os.path.join(gemini_base, "app-*"))) if gemini_base else []
    cdp_alive = False
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{cdp_port}/json/version", timeout=1.5) as resp:
            cdp_alive = resp.status == 200
    except Exception:
        cdp_alive = False
    checks["gemini_app"] = {
        "installed": bool(app_dirs),
        "latest_app_dir": app_dirs[-1] if app_dirs else None,
        "cdp_port": cdp_port,
        "cdp_alive": cdp_alive,
    }

    bundled_ok = all((SCRIPT_DIR / name).is_file() for name in BUNDLED_SCRIPTS) and BUNDLED_REF_SKILL.is_file()
    checks["bundled_files"] = {"ok": bundled_ok}

    ok = bool(agy_ok and pw_ok and bundled_ok)
    res = {
        "ok": ok,
        "sync_status": sync_info["status"],
        "target_dir": str(target_dir),
        "checks": checks,
    }
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if ok else 4


# --------------------------------------------------------------------------- agy-delegate invocation
def load_job(job_dir: Path) -> dict:
    job_file = job_dir / "job.json"
    job = read_json_safe(job_file)
    if not job:
        die(f"job metadata missing or corrupted: {job_file}", 5, code="INVALID_JOB_STATE")
    return job


def save_job(job_dir: Path, job: dict) -> None:
    job["updated_at"] = now_iso()
    write_json_atomic(job_dir / "job.json", job)


def build_relay_task_prompt(step: str, seq: int, job_dir: Path, dr_skill_dir: Path) -> str:
    job_str = str(job_dir).replace("\\", "/")
    dr_str = str(dr_skill_dir).replace("\\", "/")
    if RELAY_TASK_TEMPLATE.is_file():
        tpl = read_text(RELAY_TASK_TEMPLATE)
        return tpl.format(step=step, seq=seq, job_dir=job_str, dr_skill_dir=dr_str)
    return (
        f"Run `python \"{dr_str}/scripts/relay_step.py\" --job \"{job_str}\"` to execute step '{step}' (seq {seq}), "
        "then read `relay/result.json` (or `relay/started.json`) and report the structured result."
    )


def invoke_agy_relay(job_dir: Path, step: str, seq: int, target_skill_dir: str | None = None) -> tuple[int, dict, dict | None]:
    agy_task = Path(os.environ.get("AGY_TASK") or DEFAULT_AGY_TASK).resolve()
    if not agy_task.is_file():
        die(f"agy_task.py not found at {agy_task}", 3, code="AGY_TASK_NOT_FOUND")
    dr_skill_dir = resolve_active_dr_skill_dir(target_skill_dir)
    task_prompt = build_relay_task_prompt(step, seq, job_dir, dr_skill_dir)

    cmd = [
        sys.executable,
        str(agy_task),
        "run",
        "--deep-research",
        "--dr-skill-dir",
        str(dr_skill_dir),
        "-w",
        str(job_dir),
        "-t",
        task_prompt,
    ]
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    cp = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    agy_summary = {}
    if cp.stdout and cp.stdout.strip():
        try:
            agy_summary = json.loads(cp.stdout.strip())
        except ValueError:
            pass
    relay_res = read_json_safe(job_dir / "relay" / "result.json")
    relay_started = read_json_safe(job_dir / "relay" / "started.json")
    dr_payload = relay_res or relay_started or agy_summary.get("deep_research")
    return cp.returncode, agy_summary, dr_payload


# --------------------------------------------------------------------------- streaming & finalization
def stream_progress_and_finalize(
    job_dir: Path,
    job: dict,
    out_path: Path,
    timeout_sec: int = DEFAULT_TIMEOUT,
    poll_interval: float = 0.25,
) -> int:
    relay_dir = job_dir / "relay"
    progress_file = relay_dir / "progress.jsonl"
    result_file = relay_dir / "result.json"
    started_file = relay_dir / "started.json"

    seen_lines = 0
    highlights: list[str] = list(job.get("progress_highlights") or [])
    t0 = time.time()
    dead_ticks = 0

    def drain_progress() -> None:
        nonlocal seen_lines
        if not progress_file.is_file():
            return
        try:
            lines = progress_file.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return
        while seen_lines < len(lines):
            raw = lines[seen_lines].strip()
            seen_lines += 1
            if not raw:
                continue
            try:
                ev = json.loads(raw)
                elapsed_s = ev.get("elapsed_s", 0)
                text = (ev.get("text") or "").strip()
                formatted = f"[progress {elapsed_s}s] {text}"
                sys.stderr.write(formatted + "\n")
                sys.stderr.flush()
                if formatted not in highlights:
                    highlights.append(formatted)
            except ValueError:
                continue

    while True:
        drain_progress()
        res = read_json_safe(result_file)
        if res and res.get("seq") == job.get("seq"):
            drain_progress()
            break

        if time.time() - t0 > timeout_sec:
            job["status"] = "timeout"
            job["progress_highlights"] = highlights
            save_job(job_dir, job)
            die(
                f"timed out after {timeout_sec}s waiting for detached research worker; "
                f"use `deep_research.py status --job {job['job_id']}` or `collect` later.",
                2,
                code="STREAM_TIMEOUT",
                job_id=job["job_id"],
            )

        started = read_json_safe(started_file) or {}
        pid = started.get("worker_pid") or started.get("pid") or job.get("worker_pid")
        if pid:
            if not pid_alive(pid):
                dead_ticks += 1
                if dead_ticks >= 3:
                    drain_progress()
                    res = read_json_safe(result_file)
                    if res and res.get("seq") == job.get("seq"):
                        break
                    job["status"] = "error"
                    job["error_code"] = "WORKER_DIED"
                    job["progress_highlights"] = highlights
                    save_job(job_dir, job)
                    die(
                        f"detached worker process (pid {pid}) exited without writing relay/result.json",
                        3,
                        code="WORKER_DIED",
                        job_id=job["job_id"],
                    )
            else:
                dead_ticks = 0

        time.sleep(poll_interval)

    return finalize_completed_report(job_dir, job, res, out_path, highlights)


def finalize_completed_report(
    job_dir: Path,
    job: dict,
    res: dict,
    out_path: Path,
    highlights: list[str] | None = None,
) -> int:
    if highlights is None:
        highlights = list(job.get("progress_highlights") or [])

    if res.get("status") != "completed":
        code = res.get("code") or "RESEARCH_FAILED"
        msg = res.get("message") or f"relay step ended with status {res.get('status')!r}"
        exit_code = int(res.get("exit_code") or 2)
        if code == "COLLECT_WOULD_START" and job.get("status") in ("plan_ready", "plan_revised"):
            pass
        else:
            job["status"] = "error"
        job["error_code"] = code
        job["error_message"] = msg
        job["progress_highlights"] = highlights
        save_job(job_dir, job)
        die(msg, exit_code, code=code, job_id=job["job_id"], relay_result=res)

    report_src = Path(res.get("report_file") or (job_dir / "report.md")).resolve()
    if not report_src.is_file():
        alt = job_dir / "report.md"
        if alt.is_file():
            report_src = alt
        else:
            die(f"completed result references missing report file: {report_src}", 2, code="REPORT_FILE_MISSING")

    markdown = read_text(report_src)
    gate_stats = enforce_report_quality_gate(markdown)

    out_path = Path(out_path).expanduser().resolve()
    if out_path != report_src:
        write_text_atomic(out_path, markdown.rstrip() + "\n")
    job_report = job_dir / "report.md"
    if job_report.resolve() != report_src.resolve():
        write_text_atomic(job_report, markdown.rstrip() + "\n")

    completed_at = now_iso()
    sources_count = res.get("sources_count") if res.get("sources_count") is not None else gate_stats["sources_count"]
    elapsed_s = res.get("elapsed_s") or 0.0
    url = res.get("url") or job.get("url") or ""
    if res.get("plan") and not job.get("latest_plan"):
        job["latest_plan"] = res["plan"]

    meta_path = Path(str(out_path) + ".meta.json")
    meta = {
        "job_id": job["job_id"],
        "created_at": job.get("created_at"),
        "completed_at": completed_at,
        "prompt": read_text(job_dir / "prompt.md").strip() if (job_dir / "prompt.md").is_file() else "",
        "goal": read_text(job_dir / "goal.md").strip() if (job_dir / "goal.md").is_file() else "",
        "approved_plan": job.get("latest_plan") or "",
        "plan_iterations": int(job.get("plan_iterations") or 0),
        "approval_reason": job.get("approval_reason") or "",
        "url": url,
        "report_file": str(out_path),
        "meta_file": str(meta_path),
        "report_chars": gate_stats["report_chars"],
        "sources_count": int(sources_count),
        "elapsed_s": float(elapsed_s),
        "progress_highlights": highlights,
        "confinement": job.get("last_confinement") or {"mode": "deep-research", "enabled": True},
    }
    write_json_atomic(meta_path, meta)

    job.update({
        "status": "completed",
        "completed_at": completed_at,
        "url": url,
        "report_file": str(out_path),
        "meta_file": str(meta_path),
        "report_chars": gate_stats["report_chars"],
        "sources_count": int(sources_count),
        "elapsed_s": float(elapsed_s),
        "progress_highlights": highlights,
    })
    save_job(job_dir, job)

    summary = {
        "status": "completed",
        "job_id": job["job_id"],
        "job_dir": str(job_dir),
        "url": url,
        "report_file": str(out_path),
        "meta_file": str(meta_path),
        "report_chars": gate_stats["report_chars"],
        "sources_count": int(sources_count),
        "plan_iterations": int(job.get("plan_iterations") or 0),
        "elapsed_s": float(elapsed_s),
        "progress_highlights": highlights,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


# --------------------------------------------------------------------------- lifecycle subcommands
def cmd_plan(a) -> int:
    if not a.job and (getattr(a, "prompt", None) or getattr(a, "prompt_file", None)):
        job_id, _, _ = _init_job_internal(a)
        a.job = job_id
    job_dir = resolve_job_dir(a.job, must_exist=True)
    enforce_pre_plan_gate(job_dir)
    job = load_job(job_dir)

    seq = int(job.get("seq") or 0) + 1
    job["seq"] = seq
    save_job(job_dir, job)

    relay_dir = job_dir / "relay"
    relay_dir.mkdir(parents=True, exist_ok=True)
    result_file = relay_dir / "result.json"
    if result_file.exists():
        result_file.unlink()

    dr_skill_dir = resolve_active_dr_skill_dir(getattr(a, "dr_skill_dir", None))
    req = {
        "relay_version": RELAY_VERSION,
        "step": "plan",
        "seq": seq,
        "driver": str(dr_skill_dir / "scripts" / "run_deep_research.py"),
        "prompt_file": str(job_dir / "prompt.md"),
        "deadline": int(getattr(a, "deadline", None) or DEFAULT_PLAN_DEADLINE),
    }
    if getattr(a, "show", False):
        req["show"] = True
    write_json_atomic(relay_dir / "request.json", req)

    rc, agy_summary, dr_res = invoke_agy_relay(job_dir, "plan", seq, getattr(a, "dr_skill_dir", None))
    if agy_summary.get("confinement"):
        job["last_confinement"] = agy_summary["confinement"]

    if not dr_res:
        job["status"] = "error"
        save_job(job_dir, job)
        die("relay/result.json was not produced during plan step", 3, code="NO_RELAY_RESULT", agy_rc=rc)

    if dr_res.get("status") != "plan_ready":
        code = dr_res.get("code") or "PLAN_FAILED"
        msg = dr_res.get("message") or f"plan step failed with status {dr_res.get('status')!r}"
        exit_code = int(dr_res.get("exit_code") or 2)
        job["status"] = "error"
        job["error_code"] = code
        job["error_message"] = msg
        save_job(job_dir, job)
        die(msg, exit_code, code=code, job_id=job["job_id"], relay_result=dr_res)

    plan_text = (dr_res.get("plan") or "").strip()
    iteration = int(job.get("plan_iterations") or 0) + 1
    plan_file = job_dir / f"plan_v{iteration}.md"
    write_text_atomic(plan_file, plan_text + "\n")

    job.update({
        "status": "plan_ready",
        "url": dr_res.get("url"),
        "latest_plan": plan_text,
        "plan_count": int(dr_res.get("plan_count") or 1),
        "plan_iterations": iteration,
        "latest_plan_file": str(plan_file),
    })
    save_job(job_dir, job)

    out = {
        "status": "plan_ready",
        "job_id": job["job_id"],
        "job_dir": str(job_dir),
        "seq": seq,
        "plan_iterations": iteration,
        "url": job["url"],
        "plan_count": job["plan_count"],
        "plan_file": str(plan_file),
        "plan": plan_text,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_edit(a) -> int:
    job_dir = resolve_job_dir(a.job, must_exist=True)
    enforce_pre_plan_gate(job_dir)
    job = load_job(job_dir)
    if job.get("status") not in ("plan_ready", "plan_revised"):
        die(
            f"cannot edit plan when job status is {job.get('status')!r} (must be 'plan_ready' or 'plan_revised')",
            2,
            code="EDIT_INVALID_STATE",
        )
    feedback_text = resolve_text_input(a.feedback, a.feedback_file, "feedback")
    seq = int(job.get("seq") or 0) + 1
    iteration = int(job.get("plan_iterations") or 1) + 1
    feedback_path = job_dir / f"feedback_{iteration}.md"
    write_text_atomic(feedback_path, feedback_text + "\n")

    job["seq"] = seq
    save_job(job_dir, job)

    relay_dir = job_dir / "relay"
    result_file = relay_dir / "result.json"
    if result_file.exists():
        result_file.unlink()

    dr_skill_dir = resolve_active_dr_skill_dir(getattr(a, "dr_skill_dir", None))
    req = {
        "relay_version": RELAY_VERSION,
        "step": "edit",
        "seq": seq,
        "driver": str(dr_skill_dir / "scripts" / "run_deep_research.py"),
        "resume_url": job.get("url") or "current",
        "feedback_file": str(feedback_path),
        "deadline": int(getattr(a, "deadline", None) or DEFAULT_PLAN_DEADLINE),
    }
    if getattr(a, "show", False):
        req["show"] = True
    write_json_atomic(relay_dir / "request.json", req)

    rc, agy_summary, dr_res = invoke_agy_relay(job_dir, "edit", seq, getattr(a, "dr_skill_dir", None))
    if agy_summary.get("confinement"):
        job["last_confinement"] = agy_summary["confinement"]

    if not dr_res:
        job["status"] = "error"
        save_job(job_dir, job)
        die("relay/result.json was not produced during edit step", 3, code="NO_RELAY_RESULT", agy_rc=rc)

    if dr_res.get("status") not in ("plan_revised", "plan_ready"):
        code = dr_res.get("code") or "EDIT_FAILED"
        msg = dr_res.get("message") or f"edit step failed with status {dr_res.get('status')!r}"
        exit_code = int(dr_res.get("exit_code") or 2)
        job["status"] = "error"
        job["error_code"] = code
        job["error_message"] = msg
        save_job(job_dir, job)
        die(msg, exit_code, code=code, job_id=job["job_id"], relay_result=dr_res)

    plan_text = (dr_res.get("plan") or "").strip()
    plan_file = job_dir / f"plan_v{iteration}.md"
    write_text_atomic(plan_file, plan_text + "\n")

    job.update({
        "status": "plan_revised",
        "url": dr_res.get("url") or job.get("url"),
        "latest_plan": plan_text,
        "plan_count": int(dr_res.get("plan_count") or job.get("plan_count") or 1),
        "plan_iterations": iteration,
        "latest_plan_file": str(plan_file),
    })
    save_job(job_dir, job)

    out = {
        "status": "plan_revised",
        "job_id": job["job_id"],
        "job_dir": str(job_dir),
        "seq": seq,
        "plan_iterations": iteration,
        "url": job["url"],
        "plan_count": job["plan_count"],
        "plan_file": str(plan_file),
        "plan": plan_text,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_approve(a) -> int:
    job_dir = resolve_job_dir(a.job, must_exist=True)
    job = load_job(job_dir)
    clean_reason = enforce_approval_gate(job_dir, job, a.reason)

    seq = int(job.get("seq") or 0) + 1
    out_path = Path(a.out).expanduser().resolve() if getattr(a, "out", None) else Path(job.get("out_path") or (job_dir / "report.md")).resolve()
    timeout_sec = int(getattr(a, "timeout", None) or DEFAULT_TIMEOUT)

    write_text_atomic(job_dir / "approval.md", clean_reason + "\n")
    job["seq"] = seq
    job["approval_reason"] = clean_reason
    job["out_path"] = str(out_path)
    save_job(job_dir, job)

    relay_dir = job_dir / "relay"
    result_file = relay_dir / "result.json"
    started_file = relay_dir / "started.json"
    for f in (result_file, started_file):
        if f.exists():
            f.unlink()

    dr_skill_dir = resolve_active_dr_skill_dir(getattr(a, "dr_skill_dir", None))
    req = {
        "relay_version": RELAY_VERSION,
        "step": "approve",
        "seq": seq,
        "mode": "detach",
        "driver": str(dr_skill_dir / "scripts" / "run_deep_research.py"),
        "resume_url": job.get("url") or "current",
        "expected_url": job.get("url"),
        "expected_plan_count": int(job.get("plan_count") or 1),
        "output": str(job_dir / "report.md"),
        "timeout": timeout_sec,
    }
    if getattr(a, "show", False):
        req["show"] = True
    write_json_atomic(relay_dir / "request.json", req)

    rc, agy_summary, dr_res = invoke_agy_relay(job_dir, "approve", seq, getattr(a, "dr_skill_dir", None))
    if agy_summary.get("confinement"):
        job["last_confinement"] = agy_summary["confinement"]

    started = read_json_safe(started_file)
    res_now = read_json_safe(result_file)

    if res_now and res_now.get("status") == "error":
        code = res_now.get("code") or "APPROVE_FAILED"
        msg = res_now.get("message") or "approve step failed"
        exit_code = int(res_now.get("exit_code") or 2)
        job["status"] = "error"
        job["error_code"] = code
        job["error_message"] = msg
        save_job(job_dir, job)
        die(msg, exit_code, code=code, job_id=job["job_id"], relay_result=res_now)

    pid = (started or {}).get("worker_pid") or (started or {}).get("pid")
    job["status"] = "running"
    job["worker_pid"] = pid
    save_job(job_dir, job)

    if getattr(a, "no_wait", False):
        out = {
            "status": "started",
            "job_id": job["job_id"],
            "job_dir": str(job_dir),
            "seq": seq,
            "pid": pid,
            "url": job.get("url"),
            "out_path": str(out_path),
            "message": "Detached Deep Research worker started; run `status` or `collect` to monitor/fetch.",
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    poll_interval = float(os.environ.get("AGY_DR_POLL_INTERVAL") or getattr(a, "poll_interval", 0.25) or 0.25)
    return stream_progress_and_finalize(job_dir, job, out_path, timeout_sec=timeout_sec, poll_interval=poll_interval)


def _init_job_internal(a) -> tuple[str, Path, dict]:
    prompt_text = resolve_text_input(a.prompt, a.prompt_file, "prompt")
    goal_text = resolve_text_input(a.goal, a.goal_file, "goal")
    warnings = check_korean_safety_keywords(prompt_text)

    job_id, job_dir = allocate_job_dir(getattr(a, "job", None))
    write_text_atomic(job_dir / "prompt.md", prompt_text + "\n")
    write_text_atomic(job_dir / "goal.md", goal_text + "\n")
    (job_dir / "relay").mkdir(parents=True, exist_ok=True)

    out_default = str(Path(a.out).expanduser().resolve()) if getattr(a, "out", None) else str(job_dir / "report.md")
    job = {
        "job_id": job_id,
        "job_dir": str(job_dir),
        "status": "initialized",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "seq": 0,
        "plan_iterations": 0,
        "raw_prompt": bool(getattr(a, "raw_prompt", True)),
        "warnings": warnings,
        "out_path": out_default,
        "url": None,
        "latest_plan": None,
        "plan_count": 0,
        "approval_reason": None,
        "worker_pid": None,
        "progress_highlights": [],
    }
    save_job(job_dir, job)
    return job_id, job_dir, job


def cmd_status(a) -> int:
    job_dir = resolve_job_dir(a.job, must_exist=True)
    job = load_job(job_dir)
    relay_dir = job_dir / "relay"
    started = read_json_safe(relay_dir / "started.json") or {}
    result = read_json_safe(relay_dir / "result.json")
    progress_file = relay_dir / "progress.jsonl"

    progress_entries = []
    if progress_file.is_file():
        for line in progress_file.read_text(encoding="utf-8", errors="replace").splitlines():
            with contextlib.suppress(ValueError):
                progress_entries.append(json.loads(line))

    pid = started.get("worker_pid") or started.get("pid") or job.get("worker_pid")
    alive = pid_alive(pid) if pid else False

    status = job.get("status")
    if result and result.get("seq") == job.get("seq"):
        if result.get("status") == "completed" and status == "running":
            status = "ready_to_collect"
        elif result.get("status") == "error":
            status = "error"

    out = {
        "status": status,
        "job_id": job["job_id"],
        "job_dir": str(job_dir),
        "seq": job.get("seq"),
        "plan_iterations": job.get("plan_iterations"),
        "url": (result or {}).get("url") or job.get("url"),
        "worker_pid": pid,
        "worker_alive": alive,
        "progress_count": len(progress_entries),
        "latest_progress": progress_entries[-1] if progress_entries else None,
        "report_file": job.get("report_file") or job.get("out_path"),
        "relay_result_status": (result or {}).get("status"),
        "relay_result_code": (result or {}).get("code"),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_collect(a) -> int:
    job_dir = resolve_job_dir(a.job, must_exist=True)
    job = load_job(job_dir)
    out_path = Path(a.out).expanduser().resolve() if getattr(a, "out", None) else Path(job.get("out_path") or (job_dir / "report.md")).resolve()
    timeout_sec = int(getattr(a, "timeout", None) or DEFAULT_TIMEOUT)
    poll_interval = float(os.environ.get("AGY_DR_POLL_INTERVAL") or getattr(a, "poll_interval", 0.25) or 0.25)

    relay_dir = job_dir / "relay"
    result_file = relay_dir / "result.json"
    started_file = relay_dir / "started.json"

    res_now = read_json_safe(result_file)
    started = read_json_safe(started_file) or {}
    pid = started.get("worker_pid") or started.get("pid") or job.get("worker_pid")

    # Case 1: Current sequence already produced a completed result.json on disk
    if res_now and res_now.get("status") == "completed":
        # Drain any progress highlights
        highlights = list(job.get("progress_highlights") or [])
        progress_file = relay_dir / "progress.jsonl"
        if progress_file.is_file():
            for ln in progress_file.read_text(encoding="utf-8", errors="replace").splitlines():
                with contextlib.suppress(ValueError):
                    ev = json.loads(ln)
                    fmt = f"[progress {ev.get('elapsed_s', 0)}s] {(ev.get('text') or '').strip()}"
                    if fmt not in highlights:
                        highlights.append(fmt)
        return finalize_completed_report(job_dir, job, res_now, out_path, highlights)

    # Case 2: Detached worker is still alive
    if pid and pid_alive(pid):
        if getattr(a, "no_wait", False):
            out = {
                "status": "running",
                "job_id": job["job_id"],
                "job_dir": str(job_dir),
                "pid": pid,
                "worker_alive": True,
                "message": "Worker is still running; omit --no-wait to stream progress until completion.",
            }
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0
        return stream_progress_and_finalize(job_dir, job, out_path, timeout_sec=timeout_sec, poll_interval=poll_interval)

    # Case 3: Worker is not running and no completed result is on disk -> run a 'collect' relay step via agy
    seq = int(job.get("seq") or 0) + 1
    job["seq"] = seq
    job["out_path"] = str(out_path)
    save_job(job_dir, job)

    for f in (result_file, started_file):
        if f.exists():
            f.unlink()

    dr_skill_dir = resolve_active_dr_skill_dir(getattr(a, "dr_skill_dir", None))
    req = {
        "relay_version": RELAY_VERSION,
        "step": "collect",
        "seq": seq,
        "mode": "detach" if getattr(a, "detach", False) else "wait",
        "driver": str(dr_skill_dir / "scripts" / "run_deep_research.py"),
        "resume_url": job.get("url") or "current",
        "output": str(job_dir / "report.md"),
        "timeout": timeout_sec,
    }
    if job.get("approval_reason"):
        req["allow_probe_error"] = False
    write_json_atomic(relay_dir / "request.json", req)

    rc, agy_summary, dr_res = invoke_agy_relay(job_dir, "collect", seq, getattr(a, "dr_skill_dir", None))
    if agy_summary.get("confinement"):
        job["last_confinement"] = agy_summary["confinement"]

    res_after = read_json_safe(result_file) or dr_res
    if not res_after:
        die("relay/result.json was not produced during collect step", 3, code="NO_RELAY_RESULT", agy_rc=rc)

    if res_after.get("status") == "started":
        if getattr(a, "no_wait", False):
            print(json.dumps(res_after, ensure_ascii=False, indent=2))
            return 0
        return stream_progress_and_finalize(job_dir, job, out_path, timeout_sec=timeout_sec, poll_interval=poll_interval)

    return finalize_completed_report(job_dir, job, res_after, out_path)


def run_cmd_init(a) -> int:
    job_id, job_dir, job = _init_job_internal(a)
    res = {
        "status": "initialized",
        "job_id": job_id,
        "job_dir": str(job_dir),
        "prompt_chars": len(read_text(job_dir / "prompt.md").strip()),
        "goal_chars": len(read_text(job_dir / "goal.md").strip()),
        "warnings": job["warnings"],
    }
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def run_cmd_auto(a) -> int:
    job_id, job_dir, job = _init_job_internal(a)
    reason = (getattr(a, "reason", None) or "Auto mode: unattended end-to-end execution requested by caller.").strip()
    if len(reason) < MIN_REASON_CHARS:
        die(f"--reason in auto mode must be at least {MIN_REASON_CHARS} characters", 5, code="APPROVAL_GATE_REASON_TOO_SHORT")

    seq = int(job.get("seq") or 0) + 1
    out_path = Path(a.out).expanduser().resolve() if getattr(a, "out", None) else Path(job.get("out_path") or (job_dir / "report.md")).resolve()
    timeout_sec = int(getattr(a, "timeout", None) or DEFAULT_TIMEOUT)

    job["seq"] = seq
    job["approval_reason"] = reason
    job["out_path"] = str(out_path)
    save_job(job_dir, job)

    relay_dir = job_dir / "relay"
    dr_skill_dir = resolve_active_dr_skill_dir(getattr(a, "dr_skill_dir", None))
    req = {
        "relay_version": RELAY_VERSION,
        "step": "auto",
        "seq": seq,
        "mode": "detach",
        "driver": str(dr_skill_dir / "scripts" / "run_deep_research.py"),
        "prompt_file": str(job_dir / "prompt.md"),
        "output": str(job_dir / "report.md"),
        "timeout": timeout_sec,
    }
    if getattr(a, "show", False):
        req["show"] = True
    write_json_atomic(relay_dir / "request.json", req)

    rc, agy_summary, dr_res = invoke_agy_relay(job_dir, "auto", seq, getattr(a, "dr_skill_dir", None))
    if agy_summary.get("confinement"):
        job["last_confinement"] = agy_summary["confinement"]

    result_file = relay_dir / "result.json"
    started_file = relay_dir / "started.json"
    res_now = read_json_safe(result_file)
    started = read_json_safe(started_file)

    if res_now and res_now.get("status") == "error":
        code = res_now.get("code") or "AUTO_FAILED"
        msg = res_now.get("message") or "auto step failed"
        exit_code = int(res_now.get("exit_code") or 2)
        job["status"] = "error"
        job["error_code"] = code
        save_job(job_dir, job)
        die(msg, exit_code, code=code, job_id=job_id, relay_result=res_now)

    pid = (started or {}).get("worker_pid") or (started or {}).get("pid")
    job["status"] = "running"
    job["worker_pid"] = pid
    save_job(job_dir, job)

    if getattr(a, "no_wait", False):
        out = {
            "status": "started",
            "job_id": job_id,
            "job_dir": str(job_dir),
            "seq": seq,
            "pid": pid,
            "out_path": str(out_path),
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    poll_interval = float(os.environ.get("AGY_DR_POLL_INTERVAL") or getattr(a, "poll_interval", 0.25) or 0.25)
    return stream_progress_and_finalize(job_dir, job, out_path, timeout_sec=timeout_sec, poll_interval=poll_interval)


# --------------------------------------------------------------------------- CLI parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="deep_research.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pf = sub.add_parser("preflight", help="verify agy, Playwright, Gemini app CDP, and script sync status")
    pf.add_argument("--target-dir", "--target", dest="target_dir", help="target local-gemini-deep-research skill dir")

    st = sub.add_parser("setup", help="sync bundled scripts and SKILL.md into local-gemini-deep-research skill dir")
    st.add_argument("--target-dir", "--target", dest="target_dir", help="target local-gemini-deep-research skill dir")

    ini = sub.add_parser("init", help="initialize an isolated job directory with prompt.md and goal.md")
    ini.add_argument("--job", "-j", help="optional job ID or path outside any git repository")
    ini.add_argument("--prompt", "-p", help="research prompt text")
    ini.add_argument("--prompt-file", help="UTF-8 file containing the research prompt")
    ini.add_argument("--goal", "-g", help="acceptance criteria / review rubric text (goal.md)")
    ini.add_argument("--goal-file", help="UTF-8 file containing acceptance criteria (goal.md)")
    ini.add_argument("--out", "-o", help="default output path for the final Markdown report")
    ini.add_argument("--raw-prompt", action="store_true", default=True, help="preserve exact prompt text")

    pl = sub.add_parser("plan", help="generate initial research plan via agy-delegate --deep-research")
    pl.add_argument("--job", "-j", required=True, help="job ID or directory path")
    pl.add_argument("--deadline", type=int, default=DEFAULT_PLAN_DEADLINE, help="plan timeout in seconds")
    pl.add_argument("--dr-skill-dir", help="override local-gemini-deep-research skill directory")
    pl.add_argument("--show", action="store_true", help="show Gemini desktop app window")

    ed = sub.add_parser("edit", help="modify existing research plan with feedback")
    ed.add_argument("--job", "-j", required=True, help="job ID or directory path")
    ed.add_argument("--feedback", "-f", help="plan modification instructions")
    ed.add_argument("--feedback-file", help="UTF-8 file with plan modification instructions")
    ed.add_argument("--deadline", type=int, default=DEFAULT_PLAN_DEADLINE, help="edit timeout in seconds")
    ed.add_argument("--dr-skill-dir", help="override local-gemini-deep-research skill directory")
    ed.add_argument("--show", action="store_true", help="show Gemini desktop app window")

    ap = sub.add_parser("approve", help="verify Plan Approval Gate, start detached research, stream progress, and collect report")
    ap.add_argument("--job", "-j", required=True, help="job ID or directory path")
    ap.add_argument("--reason", "-r", required=True, help=">=20 char explanation of how the plan satisfies goal.md")
    ap.add_argument("--out", "-o", help="output Markdown report path")
    ap.add_argument("--no-wait", action="store_true", help="return immediately after detached worker starts")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="research timeout in seconds (default 1200)")
    ap.add_argument("--poll-interval", type=float, default=0.25, help="progress poll interval in seconds")
    ap.add_argument("--dr-skill-dir", help="override local-gemini-deep-research skill directory")
    ap.add_argument("--show", action="store_true", help="show Gemini desktop app window")

    au = sub.add_parser("auto", help="unattended end-to-end execution (init + auto step + stream + report gate)")
    au.add_argument("--job", "-j", help="optional job ID or directory path")
    au.add_argument("--prompt", "-p", help="research prompt text")
    au.add_argument("--prompt-file", help="UTF-8 file containing the research prompt")
    au.add_argument("--goal", "-g", help="acceptance criteria / review rubric text (goal.md)")
    au.add_argument("--goal-file", help="UTF-8 file containing acceptance criteria (goal.md)")
    au.add_argument("--reason", "-r", default="Auto mode: unattended end-to-end execution requested by caller.",
                    help="reason recorded in metadata")
    au.add_argument("--out", "-o", help="output Markdown report path")
    au.add_argument("--no-wait", action="store_true", help="return immediately after detached worker starts")
    au.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="research timeout in seconds")
    au.add_argument("--poll-interval", type=float, default=0.25, help="progress poll interval in seconds")
    au.add_argument("--dr-skill-dir", help="override local-gemini-deep-research skill directory")
    au.add_argument("--show", action="store_true", help="show Gemini desktop app window")

    ss = sub.add_parser("status", help="check job status, detached worker PID liveness, and latest progress")
    ss.add_argument("--job", "-j", required=True, help="job ID or directory path")

    co = sub.add_parser("collect", help="re-attach to running or completed research and collect report + .meta.json")
    co.add_argument("--job", "-j", required=True, help="job ID or directory path")
    co.add_argument("--out", "-o", help="output Markdown report path")
    co.add_argument("--no-wait", action="store_true", help="do not block if worker is still running")
    co.add_argument("--detach", action="store_true", help="if starting a collect relay step, run in detach mode")
    co.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="collect timeout in seconds")
    co.add_argument("--poll-interval", type=float, default=0.25, help="progress poll interval in seconds")
    co.add_argument("--dr-skill-dir", help="override local-gemini-deep-research skill directory")

    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    if a.cmd == "preflight":
        return cmd_preflight(a)
    if a.cmd == "setup":
        return cmd_setup(a)
    if a.cmd == "init":
        return run_cmd_init(a)
    if a.cmd == "plan":
        return cmd_plan(a)
    if a.cmd == "edit":
        return cmd_edit(a)
    if a.cmd == "approve":
        return cmd_approve(a)
    if a.cmd == "auto":
        return run_cmd_auto(a)
    if a.cmd == "status":
        return cmd_status(a)
    if a.cmd == "collect":
        return cmd_collect(a)
    return 5


if __name__ == "__main__":
    sys.exit(main())

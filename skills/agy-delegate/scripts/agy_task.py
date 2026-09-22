#!/usr/bin/env python3
"""agy_task.py - delegate a self-contained coding task to the Antigravity CLI (`agy`)
in headless mode and collect what it produced.

  preflight                 locate agy, check version and that headless calls work
  run [options]             run one task in a work directory, report changed files
  resume CONVERSATION_ID    send a follow-up prompt to a previous task

How it works
  * agy runs with `-p` (headless), `--output-format stream-json` (live progress),
    `--mode accept-edits` (file edits auto-approved) and `--add-dir <workdir>` so the
    agent treats the work directory as its workspace. Without --add-dir an untrusted
    directory makes agy write into its own scratch folder instead.
  * Shell commands are NOT auto-approved by default (headless soft-denies them).
    Pre-approve specific ones in ~/.gemini/antigravity-cli/settings.json
    (permissions.allow, e.g. "command(regex:python -m pytest.*)"), or pass
    --allow-all, which maps to agy's --dangerously-skip-permissions.
  * The work directory is snapshotted before and after; the diff (created / modified /
    deleted files) is the deliverable, written to <workdir>/.agy_task/<stamp>/result.json
    and printed as JSON on stdout.

Exit codes: 0 done, 2 agent finished with a non-SUCCESS status, 3 agy/runtime error,
4 preflight failure, 5 bad arguments. Python 3.9+, stdlib only.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
SKIP_DIRS = {".git", ".agy_task", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache"}
SKIP_FILES = {".DS_Store", "Thumbs.db", "desktop.ini"}
INSTALL_HINT = (
    "Windows PowerShell: irm https://antigravity.google/cli/install.ps1 | iex"
    if IS_WINDOWS else
    "macOS/Linux: curl -fsSL https://antigravity.google/cli/install.sh | bash"
)
RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "What was done, 1-3 sentences"},
        "files": {"type": "array", "items": {"type": "string"}, "description": "Files created or modified, relative to the work directory"},
        "notes": {"type": "string", "description": "Caveats, untested parts, follow-ups"},
    },
    "required": ["summary", "files"],
}
PROMPT_FRAME = """You are completing a delegated coding task non-interactively.

Rules:
- Work ONLY inside this directory: {workdir}
- Write every output file there (relative paths). Do not write anywhere else.
- Do not ask questions; make reasonable assumptions and state them in `notes`.
{cmd_rule}
- When finished, report: a short summary, the list of files you created or modified
  (relative paths), and notes.

Task:
{task}
{inputs}"""


# --------------------------------------------------------------------------- utils
def log(msg: str) -> None:
    sys.stderr.write(f"[agy-task {_dt.datetime.now():%H:%M:%S}] {msg}\n")
    sys.stderr.flush()


def die(msg: str, code: int):
    log("ERROR: " + msg)
    print(json.dumps({"status": "error", "exit_code": code, "message": msg}, ensure_ascii=False))
    sys.exit(code)


def find_agy(explicit=None) -> str:
    if explicit:
        return explicit
    for name in ("agy", "agy.exe"):
        p = shutil.which(name)
        if p:
            return p
    candidates = []
    if IS_WINDOWS:
        candidates.append(Path(os.environ.get("LOCALAPPDATA", "")) / "agy" / "bin" / "agy.exe")
    candidates += [Path.home() / ".local" / "bin" / "agy", Path("/usr/local/bin/agy"), Path("/opt/homebrew/bin/agy")]
    for c in candidates:
        if c.exists():
            return str(c)
    raise FileNotFoundError(f"agy not found on PATH or in the default install location. Install with {INSTALL_HINT}")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def snapshot(root: Path) -> dict:
    snap = {}
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in SKIP_DIRS]
        for fn in fns:
            if fn in SKIP_FILES:
                continue
            p = Path(dp) / fn
            try:
                snap[str(p.relative_to(root)).replace("\\", "/")] = (p.stat().st_size, sha256(p))
            except OSError:
                continue
    return snap


def diff_snapshots(before: dict, after: dict) -> dict:
    created = sorted(k for k in after if k not in before)
    deleted = sorted(k for k in before if k not in after)
    modified = sorted(k for k in after if k in before and after[k] != before[k])
    return {"created": created, "modified": modified, "deleted": deleted,
            "sizes": {k: after[k][0] for k in created + modified}}


def parse_agy_error(stderr_text: str):
    for line in stderr_text.splitlines():
        line = line.strip()
        if line.startswith("AGY_ERROR:"):
            try:
                return json.loads(line[len("AGY_ERROR:"):].strip())
            except json.JSONDecodeError:
                return {"raw": line}
        if line.startswith("error:"):
            return {"raw": line}
    return None


# --------------------------------------------------------------------------- agy invocation
def agy_cmd(agy: str, prompt: str, workdir: Path, a, extra=None, stream: bool = True) -> list:
    """Build the agy argv. Same on every OS: argv list, no shell."""
    cmd = [agy, "-p", prompt, "--output-format", "stream-json" if stream else "json",
           "--mode", "accept-edits", "--add-dir", str(workdir)]
    for flag, val in (("--model", a.model), ("--effort", getattr(a, "effort", None)),
                      ("--print-timeout", getattr(a, "timeout", None)), ("--agent", getattr(a, "agent", None))):
        if val:
            cmd += [flag, val]
    if getattr(a, "allow_all", False):
        cmd.append("--dangerously-skip-permissions")
    if getattr(a, "sandbox", False):
        cmd.append("--sandbox")
    return cmd + list(extra or [])


def run_agy(agy: str, prompt: str, workdir: Path, a, extra=None, stream: bool = True):
    """Run agy headless in workdir; returns (rc, result_dict_or_None, stderr_text, events)."""
    cmd = agy_cmd(agy, prompt, workdir, a, extra, stream)
    env = dict(os.environ, PYTHONIOENCODING="utf-8", NO_COLOR="1")
    proc = subprocess.Popen(cmd, cwd=str(workdir), stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    events, result = [], None
    text_buf = ""
    assert proc.stdout is not None
    for raw in iter(proc.stdout.readline, b""):
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        if not stream:
            text_buf += line
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            sys.stderr.write("  | " + line + "\n")
            continue
        events.append(ev)
        kind = ev.get("event")
        if kind == "init":
            log(f"agy started (permission_mode={ev.get('init', {}).get('permission_mode')}, cwd={ev.get('init', {}).get('cwd')})")
        elif kind == "step_update":
            su = ev.get("step_update", {})
            if su.get("step_type") == "agent_response" and su.get("text_delta"):
                sys.stderr.write(su["text_delta"])
                sys.stderr.flush()
            elif su.get("step_type") not in ("user_input", "agent_response") and su.get("state") == "ACTIVE":
                info = su.get("tool_info") or {}
                log(f"step {su.get('step_index')}: {su.get('step_type')} {json.dumps(info, ensure_ascii=False)[:200]}")
        elif kind == "result":
            result = ev.get("result")
    stderr_text = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
    proc.wait()
    if not stream and text_buf:
        try:
            result = json.loads(text_buf)
        except json.JSONDecodeError:
            result = None
    if stream:
        sys.stderr.write("\n")
    return proc.returncode, result, stderr_text, events


# --------------------------------------------------------------------------- commands
def preflight(a) -> dict:
    r = {"ok": False, "checks": {}}
    try:
        agy = find_agy(a.agy_bin)
    except FileNotFoundError as e:
        r["error"] = str(e)
        return r
    r["checks"]["agy"] = agy
    cp = subprocess.run([agy, "--version"], capture_output=True, text=True, timeout=60)
    r["checks"]["version"] = (cp.stdout + cp.stderr).strip()
    settings = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"
    r["checks"]["settings"] = str(settings) if settings.exists() else "(missing)"
    if settings.exists():
        try:
            s = json.loads(settings.read_text(encoding="utf-8"))
            r["checks"]["allow_rules"] = s.get("permissions", {}).get("allow", [])
            r["checks"]["toolPermission"] = s.get("toolPermission")
        except json.JSONDecodeError:
            r["checks"]["allow_rules"] = "(settings.json unparseable)"
    tmp = Path(a.workdir).resolve() if a.workdir else Path.cwd()
    probe_opts = argparse.Namespace(model=a.model, timeout="3m")
    rc, res, err, _ = run_agy(agy, "Reply with exactly the word OK and nothing else.", tmp, probe_opts, stream=False)
    r["checks"]["headless_probe"] = {"rc": rc, "status": (res or {}).get("status"), "response": ((res or {}).get("response") or "").strip()[:40]}
    if rc != 0 or not res or res.get("status") != "SUCCESS":
        r["error"] = "headless probe failed; sign in first by running `agy` once interactively. " + (err.strip()[-500:] or "")
        return r
    r["ok"] = True
    return r


def cmd_run(a) -> int:
    try:
        agy = find_agy(a.agy_bin)
    except FileNotFoundError as e:
        die(str(e), 4)
    task = a.task
    if a.task_file:
        task = Path(a.task_file).read_text(encoding="utf-8")
    if not task or not task.strip():
        die("provide --task TEXT or --task-file FILE", 5)

    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    workdir = Path(a.workdir).resolve() if a.workdir else (Path.cwd() / "agy_tasks" / stamp)
    if workdir == Path.home() or workdir.parent == workdir:
        die(f"refusing to use {workdir} as work directory (too broad)", 5)
    meta_dir = workdir / ".agy_task" / stamp

    # stage input files
    staged = []
    for item in a.input or []:
        p = Path(item).resolve()
        if not p.exists():
            die(f"--input not found: {p}", 5)
        staged.append(p.name)
        if a.dry_run:
            continue
        workdir.mkdir(parents=True, exist_ok=True)
        target = workdir / p.name
        if p.is_dir():
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(p, target)
        else:
            shutil.copy2(p, target)
    inputs = ("\nInput files already placed in the directory: " + ", ".join(staged)) if staged else ""

    cmd_rule = ("- You MAY run shell commands (tests, builds) inside the directory."
                if a.allow_all else
                "- Shell commands are not approved in this session unless pre-allowed; prefer writing code "
                "and tests over running them. If you must verify, say what command to run in `notes`.")
    prompt = PROMPT_FRAME.format(workdir=workdir, task=task.strip(), inputs=inputs, cmd_rule=cmd_rule)
    schema_file = meta_dir / "schema.json"
    extra = [] if a.no_schema else ["--json-schema", str(schema_file)]
    if a.dry_run:
        plan = {"status": "dry-run", "workdir": str(workdir), "would_stage": staged,
                "command": agy_cmd(agy, "<prompt>", workdir, a, extra, stream=True), "prompt": prompt}
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    workdir.mkdir(parents=True, exist_ok=True)
    meta_dir.mkdir(parents=True, exist_ok=True)
    (meta_dir / "prompt.md").write_text(prompt, encoding="utf-8")
    schema_file.write_text(json.dumps(RESULT_SCHEMA), encoding="utf-8")

    before = snapshot(workdir)
    log(f"workdir={workdir} files_before={len(before)} model={a.model or 'default'} allow_all={a.allow_all}")
    t0 = time.time()
    rc, res, err, events = run_agy(agy, prompt, workdir, a, extra=extra, stream=True)
    elapsed = round(time.time() - t0, 1)
    after = snapshot(workdir)
    changes = diff_snapshots(before, after)
    # ignore our own metadata
    for k in ("created", "modified", "deleted"):
        changes[k] = [f for f in changes[k] if not f.startswith(".agy_task/")]

    (meta_dir / "events.jsonl").write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events), encoding="utf-8")
    if err.strip():
        (meta_dir / "stderr.txt").write_text(err, encoding="utf-8")

    status = (res or {}).get("status")
    summary = {
        "status": "ok" if rc == 0 and status == "SUCCESS" else ("agent-" + status.lower() if status else "error"),
        "agy_exit_code": rc,
        "agent_status": status,
        "conversation_id": (res or {}).get("conversation_id"),
        "workdir": str(workdir),
        "changes": changes,
        "structured": (res or {}).get("structured_output"),
        "response": ((res or {}).get("response") or "")[:4000],
        "usage": (res or {}).get("usage"),
        "num_turns": (res or {}).get("num_turns"),
        "elapsed_sec": elapsed,
        "meta_dir": str(meta_dir),
        "agy_error": parse_agy_error(err) or (res or {}).get("error"),
    }

    # optional local verification command (runs through the platform shell: /bin/sh or cmd.exe)
    if a.verify:
        log(f"verify: {a.verify}")
        vp = subprocess.run(a.verify, shell=True, cwd=str(workdir), capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=a.verify_timeout)
        summary["verify"] = {"command": a.verify, "rc": vp.returncode,
                             "output_tail": (vp.stdout + vp.stderr)[-3000:]}
        if vp.returncode != 0 and summary["status"] == "ok":
            summary["status"] = "verify-failed"

    # optional collection of changed files
    if a.collect:
        dest = Path(a.collect).resolve()
        dest.mkdir(parents=True, exist_ok=True)
        copied = []
        for f in changes["created"] + changes["modified"]:
            src = workdir / f
            if src.is_file():
                tgt = dest / f
                tgt.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, tgt)
                copied.append(str(tgt))
        summary["collected"] = copied

    (meta_dir / "result.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if summary["status"] == "ok":
        return 0
    if summary["status"] == "verify-failed" or summary["status"].startswith("agent-"):
        return 2
    return 3


def cmd_resume(a) -> int:
    try:
        agy = find_agy(a.agy_bin)
    except FileNotFoundError as e:
        die(str(e), 4)
    workdir = Path(a.workdir).resolve() if a.workdir else Path.cwd()
    before = snapshot(workdir)
    rc, res, err, _ = run_agy(agy, a.task, workdir, a, extra=["--conversation", a.conversation_id], stream=True)
    changes = diff_snapshots(before, snapshot(workdir))
    out = {"status": "ok" if rc == 0 and (res or {}).get("status") == "SUCCESS" else "error",
           "agent_status": (res or {}).get("status"), "conversation_id": (res or {}).get("conversation_id"),
           "changes": changes, "response": ((res or {}).get("response") or "")[:4000],
           "agy_error": parse_agy_error(err) or (res or {}).get("error")}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out["status"] == "ok" else 2


# --------------------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agy_task.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--agy-bin", default=os.environ.get("AGY_BIN"), help="path to the agy binary (auto-detected)")
    p.add_argument("--model", default=os.environ.get("AGY_TASK_MODEL"), help="model slug (see `agy models`)")
    sub = p.add_subparsers(dest="cmd", required=True)

    pf = sub.add_parser("preflight", help="check agy install + headless auth")
    pf.add_argument("--workdir", help="directory to probe from (default: cwd)")

    r = sub.add_parser("run", help="delegate one task")
    r.add_argument("--task", "-t", help="task description")
    r.add_argument("--task-file", "-f", help="file containing the task description (markdown ok)")
    r.add_argument("--workdir", "-w", help="work directory (default: ./agy_tasks/<stamp>, created)")
    r.add_argument("--input", "-i", action="append", help="file/dir to copy into the work directory first (repeatable)")
    r.add_argument("--effort", choices=["low", "medium", "high"])
    r.add_argument("--agent", help="agy agent name (see `agy agents`)")
    r.add_argument("--timeout", default=os.environ.get("AGY_TASK_TIMEOUT", "30m"), help="agy --print-timeout, e.g. 10m, 1h (0 = unlimited)")
    r.add_argument("--verify", help="shell command to run in the work directory afterwards, e.g. 'python -m pytest -q'")
    r.add_argument("--verify-timeout", type=int, default=600)
    r.add_argument("--collect", help="copy created/modified files into this directory")
    r.add_argument("--allow-all", action="store_true", help="let the agent run any shell command (agy --dangerously-skip-permissions). Off by default.")
    r.add_argument("--sandbox", action="store_true", help="agy --sandbox (terminal restrictions; macOS/Linux)")
    r.add_argument("--no-schema", action="store_true", help="do not request structured output")
    r.add_argument("--dry-run", action="store_true", help="print the agy command and the framed prompt; do not run")

    rs = sub.add_parser("resume", help="follow-up prompt on a previous conversation")
    rs.add_argument("conversation_id")
    rs.add_argument("--task", "-t", required=True)
    rs.add_argument("--workdir", "-w")
    rs.add_argument("--effort", choices=["low", "medium", "high"])
    rs.add_argument("--agent")
    rs.add_argument("--timeout", default="30m")
    rs.add_argument("--allow-all", action="store_true")
    rs.add_argument("--sandbox", action="store_true")
    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    if a.cmd == "preflight":
        res = preflight(a)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res["ok"] else 4
    if a.cmd == "run":
        return cmd_run(a)
    if a.cmd == "resume":
        return cmd_resume(a)
    return 5


if __name__ == "__main__":
    sys.exit(main())

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
  * Confinement (default for run/resume): for the duration of the run, <workdir>/.agents/hooks.json
    installs confine_hook.py as an agy PreToolUse hook. Every tool call whose path arguments
    resolve outside the work directory is hard-denied, and so is every shell command (a program
    agy starts could read any file), whatever settings.json allows. Decisions are logged to
    .agy_task/<stamp>/confine.jsonl and summarised under "confinement" in the result.
  * Only with --no-confine do shell commands fall back to agy's own rules: soft-denied in headless
    mode unless pre-approved in ~/.gemini/antigravity-cli/settings.json (permissions.allow, e.g.
    "command(regex:python -m pytest.*)"). --allow-all maps to agy's --dangerously-skip-permissions
    and needs --no-confine.
  * --inlined REL (run): a work-dir file whose full text the task already holds; the hook denies
    opening it with a view tool. --bare (run): send the task text without the rules/report frame.
  * The work directory is snapshotted before and after; the diff (created / modified /
    deleted files) is the deliverable, written to <workdir>/.agy_task/<stamp>/result.json
    and printed as JSON on stdout.

Exit codes: 0 done, 2 agent finished with a non-SUCCESS status, 3 agy/runtime error,
4 preflight failure, 5 bad arguments. Python 3.9+, stdlib only.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
CONFINE_HOOK = Path(__file__).resolve().parent / "confine_hook.py"
SKIP_DIRS = {".git", ".agy_task", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache"}
SKIP_FILES = {".DS_Store", "Thumbs.db", "desktop.ini"}
# the confinement hook's own files (see Confinement): left out of a confined run's snapshots, and only while their
# content is exactly what Confinement writes. Anything else in .agents/ (rules, workflows, a hooks.json agy wrote
# itself: possible only with --no-confine, and agy would run its command on every tool call of the next session in
# that dir) is a change.
HOOK_RELS = (".agents/hooks.json", ".agents/confine.cmd")
INSTALL_HINT = (
    "Windows PowerShell: irm https://antigravity.google/cli/install.ps1 | iex"
    if IS_WINDOWS else
    "macOS/Linux: curl -fsSL https://antigravity.google/cli/install.sh | bash"
)
DEFAULT_DR_SKILL_DIR = Path.home() / ".gemini" / "config" / "skills" / "local-gemini-deep-research"

ARGON_CANONICAL_DEFAULT = "gemini-4-argon-high"
ARGON_ALIASES = {"argon", "아르곤", "gemini-4-argon"}


def is_argon_model(model: str | None) -> bool:
    if not model:
        return False
    m = model.strip().lower()
    return m in ARGON_ALIASES or m.startswith("gemini-4-argon-")


def normalize_model(model: str | None, effort: str | None = None) -> str | None:
    if not model:
        return model
    m = model.strip().lower()
    if m in ARGON_ALIASES:
        eff = (effort or "").strip().lower()
        if eff in ("low", "medium", "high"):
            return f"gemini-4-argon-{eff}"
        return ARGON_CANONICAL_DEFAULT
    return model

RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "What was done, 1-3 sentences"},
        "files": {"type": "array", "items": {"type": "string"}, "description": "Files created or modified, relative to the work directory"},
        "notes": {"type": "string", "description": "Caveats, untested parts, follow-ups"},
    },
    "required": ["summary", "files"],
}

DEEP_RESEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "description": "Status of the relay step (e.g. plan_ready, plan_revised, started, completed, probed, error)",
        },
        "step": {
            "type": "string",
            "description": "Step executed (plan, edit, approve, collect, auto, probe)",
        },
        "code": {
            "type": "string",
            "description": "Result or error code from the relay script",
        },
        "url": {
            "type": "string",
            "description": "Gemini chat/session URL",
        },
        "plan_count": {
            "type": "integer",
            "description": "Number of research plan sections/steps",
        },
        "report_chars": {
            "type": "integer",
            "description": "Character count of the generated report",
        },
        "sources_count": {
            "type": "integer",
            "description": "Number of web sources cited in the report",
        },
        "summary": {
            "type": "string",
            "description": "Short summary of the research step outcome",
        },
        "notes": {
            "type": "string",
            "description": "Operational notes, warnings, or troubleshooting hints",
        },
    },
    "required": ["status", "step", "summary"],
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

DEEP_RESEARCH_FRAME = """You are acting strictly as the relay operator for Gemini Deep Research.

Rules:
- Work ONLY inside this directory: {workdir}
- You must run ONLY the relay operator script using run_command:
    python "{dr_dir}/scripts/relay_step.py" --job "{workdir}"
  (or python relay_step.py --job "{workdir}" if relay_step.py is inside the work directory).
  If the Gemini app CDP port is not ready, you may run:
    python "{dr_dir}/scripts/patch_gemini_app.py"
  Do NOT run any other shell commands. Shell chaining and redirection metacharacters are strictly blocked.
- Do NOT edit or overwrite caller control files:
  relay/request.json, prompt.md, goal.md, feedback_*.md, relay/result.json, relay/started.json.
- Do NOT craft custom prompts or alter research plans yourself.
- When the relay step finishes, read relay/result.json (or relay/started.json in detached mode).
- Report the structured outcome matching the required schema (status, step, code, url, plan_count, report_chars, sources_count, summary, notes).
  Do NOT retype or paraphrase the full plan or report text in the response.

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


def snapshot(root: Path, hook_files: bool = False) -> dict:
    """hook_files: leave out the confinement hook's own files (a confined run), as long as they hold exactly what
    Confinement writes."""
    snap = {}
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in SKIP_DIRS]
        for fn in fns:
            if fn in SKIP_FILES:
                continue
            p = Path(dp) / fn
            rel = str(p.relative_to(root)).replace("\\", "/")
            if hook_files and rel in HOOK_RELS and is_own_hook_file(p, rel):
                continue
            try:
                snap[rel] = (p.stat().st_size, sha256(p))
            except OSError:
                continue
    return snap


def diff_snapshots(before: dict, after: dict) -> dict:
    created = sorted(k for k in after if k not in before)
    deleted = sorted(k for k in before if k not in after)
    modified = sorted(k for k in after if k in before and after[k] != before[k])
    return {"created": created, "modified": modified, "deleted": deleted,
            "sizes": {k: after[k][0] for k in created + modified}}


HOOK_KEY = "agy-task-confine"
LAUNCHER = "confine.cmd"


def short_path(p: str) -> str:
    import ctypes
    buf = ctypes.create_unicode_buffer(32768)
    return buf.value if ctypes.windll.kernel32.GetShortPathNameW(p, buf, len(buf)) else p


def launcher_bytes():
    """The Windows launcher's content (None elsewhere: the command line needs none there)."""
    if not IS_WINDOWS:
        return None
    # cmd /c mangles quotes, & ^ and % on its command line; a batch file quotes normally and runs by a bare name
    import ctypes
    enc = f"cp{ctypes.windll.kernel32.GetOEMCP()}"  # cmd reads batch files in the OEM code page
    exe, hook = sys.executable, str(CONFINE_HOOK)
    try:
        data = f'@"{exe}" "{hook}"'.replace("%", "%%").encode(enc)
    except (UnicodeEncodeError, LookupError):
        data = f'@"{short_path(exe)}" "{short_path(hook)}"'.replace("%", "%%").encode("ascii", "replace")
    return data + b"\r\n"


def hook_command() -> str:
    """The hooks.json command line; agy runs it through cmd /c (Windows) or sh -c with cwd = .agents."""
    if not IS_WINDOWS:
        return f"{shlex.quote(sys.executable)} {shlex.quote(str(CONFINE_HOOK))}"
    return ".\\" + LAUNCHER


def hook_spec_bytes() -> bytes:
    """hooks.json as Confinement writes it (bytes, so no newline translation makes it differ)."""
    hook = {"type": "command", "command": hook_command(), "timeout": 30}
    return json.dumps({HOOK_KEY: {"PreToolUse": [{"matcher": "*", "hooks": [hook]}]}}, indent=2).encode("utf-8")


def is_own_hook_file(p: Path, rel: str) -> bool:
    try:
        data = p.read_bytes()
    except OSError:
        return False
    return data == (hook_spec_bytes() if rel.endswith("hooks.json") else launcher_bytes())


class Confinement:
    """Install confine_hook.py as <workdir>/.agents/hooks.json for one agy run; put back, byte for byte, what was
    there before. That includes our own entry and launcher left by a run that was killed: the agy session of that
    run may still be alive, and it must stay hooked."""

    def __init__(self, workdir: Path, log_file: Path, inlined=(), mode: str = "default", dr_dir: Path | str | None = None):
        self.workdir, self.log_file, self.inlined = workdir, log_file, list(inlined)
        self.mode = mode
        if dr_dir:
            self.dr_dir = Path(dr_dir).resolve()
        elif mode == "deep-research":
            env_dr = os.environ.get("AGY_CONFINE_DR_DIR")
            self.dr_dir = Path(env_dr).resolve() if env_dr else DEFAULT_DR_SKILL_DIR.resolve()
        else:
            self.dr_dir = None
        self.dir = workdir / ".agents"
        self.file = self.dir / "hooks.json"
        self.launcher = self.dir / LAUNCHER
        self.made_dir, self.backup, self.launcher_backup = False, None, None

    def env(self) -> dict:
        env = {"AGY_CONFINE_ROOT": str(self.workdir), "AGY_CONFINE_LOG": str(self.log_file)}
        if self.inlined:  # files whose text the prompt holds: the hook denies opening them
            env["AGY_CONFINE_INLINED"] = json.dumps(self.inlined)
        if self.mode == "deep-research":
            env["AGY_CONFINE_MODE"] = "deep-research"
            if self.dr_dir:
                env["AGY_CONFINE_DR_DIR"] = str(self.dr_dir)
        return env

    def __enter__(self):
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        self.made_dir = not self.dir.exists()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.backup = self.file.read_bytes() if self.file.exists() else None
        self.launcher_backup = self.launcher.read_bytes() if self.launcher.exists() else None
        launcher = launcher_bytes()
        if launcher is not None:
            self.launcher.write_bytes(launcher)
        self.file.write_bytes(hook_spec_bytes())
        return self

    def __exit__(self, *exc):
        if self.backup is None:
            self.file.unlink(missing_ok=True)
        else:
            self.file.write_bytes(self.backup)
        if self.launcher_backup is None:
            self.launcher.unlink(missing_ok=True)
        else:
            self.launcher.write_bytes(self.launcher_backup)
        if self.made_dir:
            with contextlib.suppress(OSError):
                self.dir.rmdir()  # only if empty
        return False

    def report(self, events=(), changed=False) -> dict:
        entries = []
        if self.log_file.exists():
            for line in self.log_file.read_text(encoding="utf-8", errors="replace").splitlines():
                with contextlib.suppress(json.JSONDecodeError):
                    entries.append(json.loads(line))
        # a view of a file whose text the prompt already holds (--inlined) is denied to save tokens, not because
        # it reaches outside: those are counted apart so denied_count keeps meaning "tried to get out"
        inlined = [e for e in entries if e.get("decision") != "ask" and e.get("rule") == "inlined"]
        denied = [e for e in entries if e.get("decision") != "ask" and e.get("rule") != "inlined"]
        # installed is not enforced: agy runs the hook once per tool call (measured), so every tool step must have
        # a log entry; and a work dir that changed while the hook logged nothing was written past it
        steps = {(e.get("step_update") or {}).get("step_index") for e in events
                 if (e.get("step_update") or {}).get("step_type") == "tool"}
        effective = len(entries) >= len(steps) and (bool(entries) or not changed)
        return {"enabled": effective, "installed": True, "mode": self.mode, "root": str(self.workdir), "log": str(self.log_file),
                "agent_tool_steps": len(steps), "tool_calls": len(entries), "denied_count": len(denied),
                "denied": denied[:30], "inlined_denied_count": len(inlined)}


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


# --------------------------------------------------------------------------- timeout / status
# Measured 2026-11-06 on Windows: three Argon-low /teamwork review runs each needed more than 15 minutes
# (906 s, ~930 s and 909 s, all cut off by a 15m limit). Defaults are therefore generous and scale with the run.
TIMEOUT_BASE_MIN = {"single": 20, "teamwork": 60, "deep_research": 90}
EFFORT_FACTOR = {None: 1.0, "low": 1.0, "medium": 1.5, "high": 2.0}
INPUT_MIN_PER_BYTES = 50_000      # +1 minute per 50 KB staged into the work directory
INPUT_EXTRA_CAP_MIN = 30


def resolve_timeout(a, teamwork: bool, input_bytes: int) -> tuple[str, str]:
    """Return (agy --print-timeout value, source). 'auto' (default) adapts to mode, effort and input size."""
    val = getattr(a, "timeout", None)
    if val and str(val).strip().lower() != "auto":
        return str(val).strip(), "explicit"
    mode = "deep_research" if getattr(a, "deep_research", False) else ("teamwork" if teamwork else "single")
    minutes = TIMEOUT_BASE_MIN[mode] * EFFORT_FACTOR.get(getattr(a, "effort", None), 1.0)
    minutes += min(INPUT_EXTRA_CAP_MIN, input_bytes // INPUT_MIN_PER_BYTES)
    return f"{int(minutes)}m", "auto"


def timeout_minutes(val: str) -> float | None:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smh]?)\s*", str(val))
    if not m:
        return None
    n, unit = float(m.group(1)), m.group(2) or "s"
    return n / 60 if unit == "s" else (n if unit == "m" else n * 60)


def classify_status(rc: int, agent_status: str | None, agy_error) -> str:
    raw = (agy_error or {}).get("raw", "") if isinstance(agy_error, dict) else str(agy_error or "")
    if "print timeout" in raw:
        return "timeout-partial"
    if rc == 0 and agent_status == "SUCCESS":
        return "ok"
    return "agent-" + agent_status.lower() if agent_status else "error"


def dir_bytes(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def use_utf8_stdio() -> None:
    """Windows consoles default to cp949/cp1252; JSON with e.g. U+2013 must not crash the run after agy finished."""
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding="utf-8", errors="replace")


# --------------------------------------------------------------------------- agy invocation
def agy_cmd(agy: str, prompt: str, workdir: Path, a, extra=None, stream: bool = True) -> list:
    """Build the agy argv. Same on every OS: argv list, no shell."""
    cmd = [agy, "-p", prompt, "--output-format", "stream-json" if stream else "json",
           "--mode", "accept-edits", "--add-dir", str(workdir)]
    for flag, val in (("--model", a.model), ("--effort", getattr(a, "effort", None)),
                      ("--print-timeout", getattr(a, "timeout", None)), ("--agent", getattr(a, "agent", None))):
        if val:
            cmd += [flag, val]
    if getattr(a, "allow_all", False) or getattr(a, "deep_research", False):
        cmd.append("--dangerously-skip-permissions")
    if getattr(a, "sandbox", False):
        cmd.append("--sandbox")
    return cmd + list(extra or [])


WINDOWS_CMDLINE_LIMIT = 32767


def cannot_start(cmd: list, e: OSError) -> str:
    line = len(subprocess.list2cmdline(cmd)) if IS_WINDOWS else sum(len(x) + 1 for x in cmd)
    if IS_WINDOWS and (getattr(e, "winerror", None) == 206 or line > WINDOWS_CMDLINE_LIMIT):
        return f"prompt too long for the Windows command line ({line} characters, limit {WINDOWS_CMDLINE_LIMIT})"
    return f"cannot start agy: {e}"


def run_agy(agy: str, prompt: str, workdir: Path, a, extra=None, stream: bool = True, confine=None):
    """Run agy headless in workdir (confined to it when confine is a Confinement);
    returns (rc, result_dict_or_None, stderr_text, events)."""
    with confine or contextlib.nullcontext():
        return _run_agy(agy, prompt, workdir, a, extra, stream, confine.env() if confine else {})


def _run_agy(agy: str, prompt: str, workdir: Path, a, extra, stream: bool, confine_env: dict):
    cmd = agy_cmd(agy, prompt, workdir, a, extra, stream)
    env = dict(os.environ, PYTHONIOENCODING="utf-8", NO_COLOR="1", **confine_env)
    try:
        proc = subprocess.Popen(cmd, cwd=str(workdir), stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    except OSError as e:  # e.g. WinError 206: CreateProcess takes at most 32,767 characters of command line
        why = cannot_start(cmd, e)
        log("ERROR: " + why)
        return 3, None, f"error: {why}\n", []
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


def check_confine_args(a) -> None:
    if getattr(a, "deep_research", False):
        if not a.confine:
            die("--deep-research requires confinement; cannot be used with --no-confine", 5)
        if getattr(a, "allow_all", False):
            die("--allow-all cannot be combined with --deep-research", 5)
        # Note: --deep-research is compatible with confinement and automatically passes
        # --dangerously-skip-permissions to agy because confine_hook restricts shell execution.
    elif a.confine and getattr(a, "allow_all", False):
        die("--allow-all lets agy run programs, and a program can read any file: the work-directory confinement "
            "cannot hold. Pass --no-confine together with --allow-all to accept that.", 5)
    for rel in getattr(a, "inlined", None) or ():
        # work-dir-relative, as the task names them: an absolute path would also put the caller's layout into argv
        if not rel or os.path.isabs(rel) or re.match(r"^([A-Za-z]:|[\\/~])", rel) or ".." in re.split(r"[\\/]", rel):
            die(f"--inlined {rel!r}: give a path relative to the work directory that stays inside it", 5)
    if getattr(a, "inlined", None) and not a.confine:
        log("note: --inlined has no effect with --no-confine (only the confinement hook enforces it)")


def cmd_run(a) -> int:
    a.model = normalize_model(a.model, getattr(a, "effort", None))
    try:
        agy = find_agy(a.agy_bin)
    except FileNotFoundError as e:
        die(str(e), 4)
    task = a.task
    if a.task_file:
        task = Path(a.task_file).read_text(encoding="utf-8")
    if not task or not task.strip():
        die("provide --task TEXT or --task-file FILE", 5)
    check_confine_args(a)

    is_dr = getattr(a, "deep_research", False)
    single_agent = getattr(a, "single_agent", False)
    if is_argon_model(a.model) and not single_agent and not is_dr:
        if not task.strip().startswith("/"):
            task = f"/teamwork {task.strip()}"

    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    workdir = Path(a.workdir).resolve() if a.workdir else (Path.cwd() / "agy_tasks" / stamp)
    if workdir == Path.home() or workdir.parent == workdir:
        die(f"refusing to use {workdir} as work directory (too broad)", 5)
    meta_dir = workdir / ".agy_task" / stamp

    # stage input files
    staged = []
    input_bytes = 0
    for item in a.input or []:
        p = Path(item).resolve()
        if not p.exists():
            die(f"--input not found: {p}", 5)
        staged.append(p.name)
        input_bytes += dir_bytes(p)
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

    is_dr = getattr(a, "deep_research", False)
    dr_dir = Path(getattr(a, "dr_skill_dir", None) or os.environ.get("AGY_CONFINE_DR_DIR") or DEFAULT_DR_SKILL_DIR).resolve()
    chosen_schema = DEEP_RESEARCH_SCHEMA if is_dr else RESULT_SCHEMA

    if is_dr and not a.bare:
        dr_dir_str = str(dr_dir).replace("\\", "/")
        workdir_str = str(workdir).replace("\\", "/")
        prompt = DEEP_RESEARCH_FRAME.format(workdir=workdir_str, dr_dir=dr_dir_str, task=task.strip(), inputs=inputs)
    elif a.bare or task.strip().startswith("/"):
        prompt = task.strip() + "\n" + inputs
    else:
        cmd_rule = ("- You MAY run shell commands (tests, builds) inside the directory."
                    if a.allow_all else
                    "- Every tool call that touches a path outside this directory is blocked, and shell commands are "
                    "disabled. Read and write only files inside it; if you would verify by running something, say "
                    "what to run in `notes`."
                    if a.confine else
                    "- Shell commands are not approved in this session unless pre-allowed; prefer writing code "
                    "and tests over running them. If you must verify, say what command to run in `notes`.")
        prompt = PROMPT_FRAME.format(workdir=workdir, task=task.strip(), inputs=inputs, cmd_rule=cmd_rule)

    teamwork = prompt.lstrip().startswith("/teamwork")
    a.timeout, timeout_src = resolve_timeout(a, teamwork, input_bytes)
    tmin = timeout_minutes(a.timeout)
    if teamwork and timeout_src == "explicit" and tmin and tmin < 30:
        log(f"WARNING: --timeout {a.timeout} is short for /teamwork (measured runs needed >15m); "
            f"omit --timeout to use the adaptive default")
    schema_file = meta_dir / "schema.json"
    extra = [] if a.no_schema else ["--json-schema", str(schema_file)]
    if a.dry_run:
        plan = {"status": "dry-run", "workdir": str(workdir), "would_stage": staged, "confined": a.confine,
                "inlined": a.inlined or [], "bare": a.bare,
                "command": agy_cmd(agy, "<prompt>", workdir, a, extra, stream=True), "prompt": prompt,
                "schema": chosen_schema, "timeout": {"value": a.timeout, "source": timeout_src}}
        if is_dr:
            plan["deep_research"] = True
            plan["dr_skill_dir"] = str(dr_dir)
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    workdir.mkdir(parents=True, exist_ok=True)
    meta_dir.mkdir(parents=True, exist_ok=True)
    (meta_dir / "prompt.md").write_text(prompt, encoding="utf-8")
    schema_file.write_text(json.dumps(chosen_schema, ensure_ascii=False, indent=2), encoding="utf-8")

    before = snapshot(workdir, hook_files=a.confine)
    confine_mode = "deep-research" if is_dr else "default"
    confine = Confinement(workdir, meta_dir / "confine.jsonl", a.inlined or (), mode=confine_mode, dr_dir=dr_dir) if a.confine else None
    log(f"workdir={workdir} files_before={len(before)} model={a.model or 'default'} allow_all={a.allow_all} "
        f"confined={a.confine} deep_research={is_dr}")
    t0 = time.time()
    rc, res, err, events = run_agy(agy, prompt, workdir, a, extra=extra, stream=True, confine=confine)
    elapsed = round(time.time() - t0, 1)
    after = snapshot(workdir, hook_files=a.confine)
    changes = diff_snapshots(before, after)
    # ignore our own metadata
    for k in ("created", "modified", "deleted"):
        changes[k] = [f for f in changes[k] if not f.startswith(".agy_task/")]

    (meta_dir / "events.jsonl").write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events), encoding="utf-8")
    if err.strip():
        (meta_dir / "stderr.txt").write_text(err, encoding="utf-8")

    status = (res or {}).get("status")
    agy_error = parse_agy_error(err) or (res or {}).get("error")
    summary = {
        "status": classify_status(rc, status, agy_error),
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
        "agy_error": agy_error,
        "timeout": {"value": a.timeout, "source": timeout_src},
        "confinement": confine.report(events, any(changes[k] for k in ("created", "modified", "deleted")))
        if confine else {"enabled": False},
    }
    if summary["status"] == "timeout-partial":
        log(f"agy hit the {a.timeout} print timeout; files written so far are listed in changes. "
            f"Re-run with a longer --timeout or continue with `resume {summary['conversation_id']}`")
    if is_dr:
        dr_info = None
        result_file = workdir / "relay" / "result.json"
        started_file = workdir / "relay" / "started.json"
        if result_file.is_file():
            try:
                dr_info = json.loads(result_file.read_text(encoding="utf-8"))
            except Exception as e:
                log(f"WARNING: failed to read {result_file}: {e}")
        elif started_file.is_file():
            try:
                dr_info = json.loads(started_file.read_text(encoding="utf-8"))
            except Exception as e:
                log(f"WARNING: failed to read {started_file}: {e}")
        summary["deep_research"] = dr_info
    if confine and not summary["confinement"]["enabled"]:
        log("WARNING: agy made tool calls but the confinement hook never ran; this run was NOT confined")
    if confine and summary["confinement"]["denied_count"]:
        log(f"confinement denied {summary['confinement']['denied_count']} tool call(s); see {confine.log_file}")
    if confine and summary["confinement"].get("inlined_denied_count"):
        log(f"agy tried to open {summary['confinement']['inlined_denied_count']} inlined file(s); pointed back "
            f"to the prompt")

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
    if summary["status"] in ("verify-failed", "timeout-partial") or summary["status"].startswith("agent-"):
        return 2
    return 3


def cmd_resume(a) -> int:
    a.model = normalize_model(a.model, getattr(a, "effort", None))
    try:
        agy = find_agy(a.agy_bin)
    except FileNotFoundError as e:
        die(str(e), 4)
    check_confine_args(a)
    workdir = Path(a.workdir).resolve() if a.workdir else Path.cwd()
    if a.confine and (workdir == Path.home() or workdir.parent == workdir):
        die(f"refusing to confine agy to {workdir} (too broad)", 5)
    before = snapshot(workdir, hook_files=a.confine)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    is_dr = getattr(a, "deep_research", False)
    a.timeout, _src = resolve_timeout(a, is_argon_model(a.model) and not getattr(a, "single_agent", False), 0)
    dr_dir = Path(getattr(a, "dr_skill_dir", None) or os.environ.get("AGY_CONFINE_DR_DIR") or DEFAULT_DR_SKILL_DIR).resolve()
    confine_mode = "deep-research" if is_dr else "default"
    confine = Confinement(workdir, workdir / ".agy_task" / f"resume-{stamp}" / "confine.jsonl", mode=confine_mode, dr_dir=dr_dir) if a.confine else None
    rc, res, err, events = run_agy(agy, a.task, workdir, a, extra=["--conversation", a.conversation_id], stream=True,
                              confine=confine)
    changes = diff_snapshots(before, snapshot(workdir, hook_files=a.confine))
    out = {"status": "ok" if rc == 0 and (res or {}).get("status") == "SUCCESS" else "error",
           "agent_status": (res or {}).get("status"), "conversation_id": (res or {}).get("conversation_id"),
           "changes": changes, "response": ((res or {}).get("response") or "")[:4000],
           "agy_error": parse_agy_error(err) or (res or {}).get("error"),
           "confinement": confine.report(events, any(changes[k] for k in ("created", "modified", "deleted")))
           if confine else {"enabled": False}}
    if is_dr:
        dr_info = None
        result_file = workdir / "relay" / "result.json"
        started_file = workdir / "relay" / "started.json"
        if result_file.is_file():
            with contextlib.suppress(Exception):
                dr_info = json.loads(result_file.read_text(encoding="utf-8"))
        elif started_file.is_file():
            with contextlib.suppress(Exception):
                dr_info = json.loads(started_file.read_text(encoding="utf-8"))
        out["deep_research"] = dr_info
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out["status"] == "ok" else 2


# --------------------------------------------------------------------------- batch (parallel independent tasks)
BATCH_KEYS = {"name", "task", "task_file", "inputs", "workdir", "model", "effort", "timeout", "verify", "collect",
              "single_agent", "bare", "no_schema", "inlined", "agent", "dry_run"}


def _batch_argv(a, t: dict, wd: Path) -> list:
    argv = [sys.executable, str(Path(__file__).resolve())]
    if a.agy_bin:
        argv += ["--agy-bin", a.agy_bin]
    model = t.get("model") or a.model
    if model:
        argv += ["--model", model]
    argv += ["run", "-w", str(wd)]
    argv += ["-f", str(Path(t["task_file"]).resolve())] if t.get("task_file") else ["-t", t["task"]]
    for item in t.get("inputs") or []:
        argv += ["-i", str(item)]
    for rel in t.get("inlined") or []:
        argv += ["--inlined", rel]
    effort = t.get("effort") or getattr(a, "effort", None)
    if effort:
        argv += ["--effort", effort]
    for key in ("timeout", "verify", "collect", "agent"):
        if t.get(key):
            argv += ["--" + key, str(t[key])]
    for key in ("single_agent", "bare", "no_schema", "dry_run"):
        if t.get(key):
            argv.append("--" + key.replace("_", "-"))
    return argv


def cmd_batch(a) -> int:
    """Run independent tasks as separate, concurrently running agy sessions (one work directory each)."""
    import concurrent.futures as cf
    try:
        data = json.loads(Path(a.manifest).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        die(f"cannot read manifest {a.manifest}: {e}", 5)
    tasks = data.get("tasks") if isinstance(data, dict) else data
    if not isinstance(tasks, list) or not tasks:
        die("manifest must be a list of tasks or {\"tasks\": [...]}", 5)
    names = [t.get("name") for t in tasks]
    if any(not n or not re.fullmatch(r"[A-Za-z0-9._-]+", str(n)) for n in names):
        die("every task needs a 'name' made of letters, digits, '.', '_' or '-'", 5)
    dup = sorted({n for n in names if names.count(n) > 1})
    if dup:
        die(f"duplicate task names: {', '.join(dup)}", 5)
    for t in tasks:
        unknown = set(t) - BATCH_KEYS
        if unknown:
            die(f"task {t['name']}: unknown keys {sorted(unknown)}", 5)
        if not t.get("task") and not t.get("task_file"):
            die(f"task {t['name']}: needs 'task' or 'task_file'", 5)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    batch_dir = Path(a.workdir).resolve() if a.workdir else (Path.cwd() / "agy_tasks" / f"batch-{stamp}")
    if batch_dir == Path.home() or batch_dir.parent == batch_dir:
        die(f"refusing to use {batch_dir} as batch directory (too broad)", 5)
    wds = [Path(t["workdir"]).resolve() if t.get("workdir") else batch_dir / t["name"] for t in tasks]
    for i, w in enumerate(wds):
        for j, o in enumerate(wds):
            if i != j and (w == o or o in w.parents):
                die(f"work directories must be distinct and not nested: {w} vs {o}", 5)
    batch_dir.mkdir(parents=True, exist_ok=True)
    parallel = max(1, min(a.parallel, len(tasks)))
    log(f"batch: {len(tasks)} task(s), parallel={parallel}, dir={batch_dir}")

    def one(i: int) -> dict:
        t, wd = tasks[i], wds[i]
        time.sleep(i % parallel * a.stagger)  # spread the first wave of logins/session starts
        started = _dt.datetime.now().isoformat(timespec="seconds")
        t0 = time.time()
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        cp = subprocess.run(_batch_argv(a, t, wd), capture_output=True, env=env)
        out_text = cp.stdout.decode("utf-8", errors="replace")
        (batch_dir / f"{t['name']}.log").write_bytes(cp.stderr)
        try:
            result = json.loads(out_text)
        except json.JSONDecodeError:
            result = {"status": "error", "message": out_text[-2000:]}
        status = result.get("status", "error")
        if status == "dry-run":
            status = "ok"
        log(f"batch: {t['name']} -> {status} ({round(time.time() - t0, 1)}s)")
        return {"name": t["name"], "status": status, "exit_code": cp.returncode, "workdir": str(wd),
                "started_at": started, "elapsed_sec": round(time.time() - t0, 1),
                "log": str(batch_dir / f"{t['name']}.log"), "result": result}

    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=parallel) as ex:
        results = list(ex.map(one, range(len(tasks))))
    ok = sum(r["status"] == "ok" for r in results)
    summary = {"status": "ok" if ok == len(results) else ("failed" if ok == 0 else "partial"),
               "parallel": parallel, "elapsed_sec": round(time.time() - t0, 1), "batch_dir": str(batch_dir),
               "tasks": results}
    (batch_dir / "batch_result.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["status"] == "ok" else (2 if summary["status"] == "partial" else 3)


# --------------------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agy_task.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--agy-bin", default=os.environ.get("AGY_BIN"), help="path to the agy binary (auto-detected)")
    p.add_argument("--model", default=os.environ.get("AGY_TASK_MODEL"), help="model slug (see `agy models`)")
    p.add_argument("--effort", choices=["low", "medium", "high"], default=os.environ.get("AGY_TASK_EFFORT"), help="reasoning effort level")
    sub = p.add_subparsers(dest="cmd", required=True)

    pf = sub.add_parser("preflight", help="check agy install + headless auth")
    pf.add_argument("--workdir", help="directory to probe from (default: cwd)")

    r = sub.add_parser("run", help="delegate one task")
    r.add_argument("--task", "-t", help="task description")
    r.add_argument("--task-file", "-f", help="file containing the task description (markdown ok)")
    r.add_argument("--workdir", "-w", help="work directory (default: ./agy_tasks/<stamp>, created)")
    r.add_argument("--input", "-i", action="append", help="file/dir to copy into the work directory first (repeatable)")
    r.add_argument("--effort", choices=["low", "medium", "high"], default=argparse.SUPPRESS)
    r.add_argument("--agent", help="agy agent name (see `agy agents`)")
    r.add_argument("--timeout", default=os.environ.get("AGY_TASK_TIMEOUT", "auto"),
                   help="agy --print-timeout: 'auto' (default: single 20m, /teamwork 60m, deep-research 90m; x1.5 medium, "
                        "x2 high effort; +1m per 50 KB of --input, max +30m) or an explicit value like 45m, 2h (0 = unlimited)")
    r.add_argument("--verify", help="shell command to run in the work directory afterwards, e.g. 'python -m pytest -q'")
    r.add_argument("--verify-timeout", type=int, default=600)
    r.add_argument("--collect", help="copy created/modified files into this directory")
    r.add_argument("--allow-all", action="store_true", help="let the agent run any shell command (agy --dangerously-skip-permissions). Off by default; needs --no-confine.")
    r.add_argument("--no-confine", dest="confine", action="store_false",
                   help="do not confine agy's tool calls to the work directory (confined by default)")
    r.add_argument("--deep-research", action="store_true",
                   help="run in deep-research mode (pass --dangerously-skip-permissions to agy while keeping confinement active)")
    r.add_argument("--dr-skill-dir", default=os.environ.get("AGY_CONFINE_DR_DIR"),
                   help="path to local-gemini-deep-research skill directory (default: ~/.gemini/config/skills/local-gemini-deep-research)")
    r.add_argument("--inlined", action="append", metavar="REL",
                   help="work-dir file whose full text the task already holds; the confinement hook denies opening "
                        "it (repeatable)")
    r.add_argument("--sandbox", action="store_true", help="agy --sandbox (terminal restrictions; macOS/Linux)")
    r.add_argument("--no-schema", action="store_true", help="do not request structured output")
    r.add_argument("--bare", action="store_true",
                   help="send the task as is, without the rules/report frame (for callers whose task text already "
                        "states the rules and the delivery format, e.g. agy-outsource work orders)")
    r.add_argument("--single-agent", action="store_true",
                   help="override default /teamwork execution when using Argon model")
    r.add_argument("--dry-run", action="store_true", help="print the agy command and the framed prompt; do not run")

    b = sub.add_parser("batch", help="run independent tasks as parallel agy sessions (one work dir each)")
    b.add_argument("--manifest", "-m", required=True,
                   help="JSON: [{name, task|task_file, inputs?, effort?, timeout?, verify?, ...}] or {\"tasks\": [...]}")
    b.add_argument("--workdir", "-w", help="batch directory; each task runs in <batch dir>/<name> (default: ./agy_tasks/batch-<stamp>)")
    b.add_argument("--parallel", "-p", type=int, default=int(os.environ.get("AGY_TASK_PARALLEL", "3")),
                   help="max concurrent agy sessions (default 3, env AGY_TASK_PARALLEL)")
    b.add_argument("--stagger", type=float, default=5.0, help="seconds between session starts in the first wave")
    b.add_argument("--effort", choices=["low", "medium", "high"], default=argparse.SUPPRESS)

    rs = sub.add_parser("resume", help="follow-up prompt on a previous conversation")
    rs.add_argument("conversation_id")
    rs.add_argument("--task", "-t", required=True)
    rs.add_argument("--workdir", "-w")
    rs.add_argument("--effort", choices=["low", "medium", "high"], default=argparse.SUPPRESS)
    rs.add_argument("--agent")
    rs.add_argument("--timeout", default=os.environ.get("AGY_TASK_TIMEOUT", "auto"))
    rs.add_argument("--allow-all", action="store_true")
    rs.add_argument("--no-confine", dest="confine", action="store_false")
    rs.add_argument("--deep-research", action="store_true",
                    help="run in deep-research mode (pass --dangerously-skip-permissions to agy while keeping confinement active)")
    rs.add_argument("--dr-skill-dir", default=os.environ.get("AGY_CONFINE_DR_DIR"),
                    help="path to local-gemini-deep-research skill directory (default: ~/.gemini/config/skills/local-gemini-deep-research)")
    rs.add_argument("--sandbox", action="store_true")
    rs.add_argument("--single-agent", action="store_true",
                    help="override default /teamwork execution when using Argon model")
    return p


def main(argv=None) -> int:
    use_utf8_stdio()
    a = build_parser().parse_args(argv)
    if a.cmd == "preflight":
        res = preflight(a)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res["ok"] else 4
    if a.cmd == "run":
        return cmd_run(a)
    if a.cmd == "resume":
        return cmd_resume(a)
    if a.cmd == "batch":
        return cmd_batch(a)
    return 5


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""relay_step.py - relay mode for the `local-gemini-deep-research` skill: run ONE driver step for a caller that
owns the research plan, and hand the result back as a file.

When the agent running the skill (agy) was itself started by another agent that wants to review the research
plan (for example the Claude Code skill `agy-deep-research`), the plan decisions are not agy's to make. The
caller writes `<job>/relay/request.json`, agy runs

    python relay_step.py --job <job dir>

and the relay runs exactly the requested `run_deep_research.py --step ...` command with the driver's stdout and
stderr captured to files, parses the `=== [PLAN_REVIEW_HOOK_START] ===` payload (plan / edit) or the report
(approve / collect) and writes `<job>/relay/result.json`. agy reads that file and reports; the caller reads it
too, so nothing the plan says passes through a language model's transcription.

request.json fields
  step            plan | edit | approve | collect | auto | probe
  driver          absolute path of run_deep_research.py (optional: next to this file, then the agy install)
  prompt_file     plan:            UTF-8 file with the exact research prompt
  feedback_file   edit:            UTF-8 file with the plan modification instructions
  resume_url      edit/approve/collect: the conversation URL returned by the plan step
  output          approve/collect: where the Markdown report goes
  timeout         approve/collect: seconds to wait for the report (driver --timeout, default 1200)
  deadline        plan/edit: seconds before the driver is killed (deep_research.py sets it)
  mode            approve/collect: "wait" (default: block until the report is in) or "detach" (start a worker
                  that keeps running after this process exits; result.json appears when the research is done)
  show            true to show the Gemini window if the driver (re)starts the app
  env             {NAME: value} added to the driver's environment (a detached worker does not inherit the caller's)
  (an old request's endpoint field `cdp_url` is ignored)

`collect` never starts a research: before running the driver it probes the page (read-only: the driver's
`--step probe`, which prints {"start_enabled": bool, "plan_cards": int, "url": str}) and refuses with COLLECT_WOULD_START when the plan
card still has an active start button. approve's PLAN_CHANGED gate compares `plan_cards` with the request's
`expected_plan_count`. `probe` only runs that read-only probe and writes it to result.json.
  allow_missing_confirm   approve: keep going when the plan card has no active start button (collect: always)

Conversation identity: the Gemini app has one page that every driver shares. approve/collect refuse with
CONVERSATION_MISMATCH when the probe could not settle the page on resume_url's conversation, and a report counts as
completed only when the same driver run printed its `[REPORT_PROVENANCE]` line (the conversation the report was
extracted from) and that conversation is resume_url's (auto: the new chat the driver pinned). No provenance ->
REPORT_PROVENANCE_MISSING; a report file left over from an earlier run is never accepted on its own.

result.json: status (plan_ready | plan_revised | completed | started | error), code, message, hint, rc, event, url,
conversation_id, report_title, plan, plan_count, edit_button_clicked, confirm_clicked, app_status, navigated,
report_file, report_chars, sources_count, elapsed_s, stdout_log, stderr_log, pid, and for plan / edit
foreground_invariant (the R6 tracer's summary: fg_tracer.py runs detached around the driver run and appends to
relay/foreground.jsonl) and foreground_invariant_violated (true: our instance became the default desktop's foreground
or showed a window there -> FOREGROUND_INVARIANT_VIOLATED, exit 2). For a completed report `url` /
`conversation_id` are the driver's provenance (where the report came from), never a later look at the page.

Privacy: the driver's stdout/stderr logs and progress.jsonl in relay/ are written line by line scrubbed (the relay parses
its in-memory copy), and every tail, hint and error message written to result.json (and printed) goes through
privacy.scrub (privacy.py next to this file); if that module cannot be imported a minimal local redaction of
e-mail addresses and `Google 계정:` / `Google Account:` lines is used instead, never the raw text.

Exit codes: 0 ok | 2 research-level failure (no usable plan / report) | 3 driver or environment error |
5 bad request. Python 3.9+, stdlib only.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import unicodedata
from urllib.parse import urlsplit

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
try:
    from privacy import scrub as _privacy_scrub
except Exception:  # noqa: BLE001 - an install without privacy.py still never writes raw tails
    _privacy_scrub = None

_FALLBACK_SENSITIVE = (re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)*"), re.compile(r"Google\s*(계정|Account)\s*:", re.I))


def _fallback_scrub(value):
    if isinstance(value, dict):
        return {_fallback_scrub(k): _fallback_scrub(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_fallback_scrub(v) for v in value]
    if isinstance(value, str):
        forms = (value, unicodedata.normalize("NFKC", value),
                 "".join(c for c in unicodedata.normalize("NFKC", value) if unicodedata.category(c)[0] not in "MC"))
        if any(rx.search(f) for rx in _FALLBACK_SENSITIVE for f in forms):
            return "<redacted:account>"
    return value


def scrub_text(value):
    """privacy.scrub, or the minimal local redaction when privacy.py is unavailable (never the raw text)."""
    if _privacy_scrub is not None:
        try:
            return _privacy_scrub(value)
        except Exception:  # noqa: BLE001
            pass
    return _fallback_scrub(value)


_SCRUBBED_KEYS = ("message", "hint", "plan_preview", "stderr_tail", "stdout_tail", "error", "detail")


def _scrub_line(line):
    """One captured driver line for a log file agy can read: scrubbed, line ending kept."""
    body = line.rstrip("\r\n")
    return scrub_text(body) + line[len(body):]


def scrub_result(res):
    """Scrub, in place, every tail / error message a result carries (top level and its probe)."""
    for k in _SCRUBBED_KEYS:
        if res.get(k) is not None:
            res[k] = scrub_text(res[k])
    probe = res.get("probe")
    if isinstance(probe, dict):
        for k in _SCRUBBED_KEYS:
            if probe.get(k) is not None:
                probe[k] = scrub_text(probe[k])
    return res


def write_result(path, res):
    write_json_atomic(path, scrub_result(res))

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

RELAY_VERSION = 2
DRIVER = "run_deep_research.py"
MARK_START = "=== [PLAN_REVIEW_HOOK_START] ==="
MARK_END = "=== [PLAN_REVIEW_HOOK_END] ==="
REPORT_START = "=== DEEP RESEARCH REPORT START ==="
REPORT_END = "=== DEEP RESEARCH REPORT END ==="
PLAN_BLOCK_START = "--- [Research Plan] ---"
PLAN_BLOCK_END = "-----------------------"
LINE_CONFIRM = "Clicking '연구 시작'"
LINE_EDIT = "Clicking '계획 수정'"
LINE_MONITOR = "Deep Research is running in the background"
LINE_APP = "Local Gemini app ready via CDP"
LINE_NAVIGATE = "Navigating to conversation:"
LINE_PROMPT_SENT = "Prompt submitted."
PROGRESS_RE = re.compile(r"\[(\d+)s\] Progress: (.*)$")
APP_STATUS_RE = re.compile(r"status=([a-z_]+)")
PROVENANCE_PREFIX = "[REPORT_PROVENANCE] "      # printed by the driver right before the report block
CONVERSATION_PREFIX = "[CONVERSATION] "          # printed by the driver as soon as the step's conversation is bound
DRIVER_ERROR_PREFIX = "[DRIVER_ERROR] "          # the driver's error record on stderr: {"code", "message", "details", "exit_code"}
STEP_DEADLINE_ENV = "AGY_DR_STEP_DEADLINE"     # the deadline this relay gives the driver run (s); the driver fits its waits in it
# every code the UI Automation driver may put in a [DRIVER_ERROR] record (run_deep_research.py; tests keep them in step)
DRIVER_ERROR_CODES = frozenset({
    "CONVERSATION_MISMATCH", "INPUT_NOT_EMPTY", "PROMPT_READBACK_MISMATCH", "SEND_NOT_AVAILABLE", "MODEL_NOT_AVAILABLE",
    "MODEL_AMBIGUOUS", "MODEL_NOT_SELECTED", "DEEP_RESEARCH_TOGGLE_NOT_FOUND", "UI_ACTION_FAILED", "UI_TIMEOUT",
    "NO_PLAN_CARD", "SAFETY_BLOCKED", "GEMINI_ERROR_RESPONSE", "PLAN_TIMEOUT", "PLAN_CARD_COUNT_MISMATCH",
    "PLAN_CARD_UNRECOGNISED", "UNREVIEWED_START", "COUNTDOWN_NOT_CANCELLED", "UI_LOCATOR_MISS",
    "GEMINI_IN_USE", "HOST_BUSY", "HOST_MODE_UNAVAILABLE", "HOST_START_TIMEOUT", "HOST_DESKTOP_LOST", "HOST_REFUSED",
    "APP_NOT_INSTALLED", "UIA_UNAVAILABLE", "LOCATOR_CONFIG_ERROR", "UIA_BACKEND_ERROR", "DRIVER_ERROR",
    "UIA_DRIVER_NOT_IMPLEMENTED", "DRIVER_STATE_MISMATCH", "DEADLINE_TOO_SHORT",
    "ALREADY_STARTED", "PLAN_CARD_STALE", "RESEARCH_NOT_STARTED", "RESEARCH_STALLED", "RECOVERY_UNAVAILABLE",
    "RESEARCH_TIMEOUT",
})
DRIVER_ERROR_HINT = ("the driver reported this code (see its details and the stderr log); the caller decides, and a step "
                     "that already sent a prompt or feedback is never re-run")
_CONVERSATION_PATH_RE = re.compile(r"(?:^|/)app/([A-Za-z0-9_-]+)(?:/|$)", re.IGNORECASE)   # same parser as the driver
_GEMINI_HOSTS = ("gemini.google.com",)
MISMATCH_HINT = ("nothing was accepted from it. The research keeps running in its own conversation: when no other job "
                 "is driving the Gemini app, the caller collects this job again (one job drives the app at a time)")
PLAN_MISMATCH_HINT = ("the shared Gemini page moved to another conversation during this step; nothing from it was used. "
                      "The caller repeats the step once no other job is driving the Gemini app (plan: the job has no reviewed "
                      "plan yet; edit: the reviewed plan is unchanged unless prompt_sent is true)")
PLAN_DEADLINE = 420
REPORT_TIMEOUT = 1200
PROMPT_ARGV_CAP = 28000
REPORT_MIN_CHARS = 400
RETRY_ONCE = ("DEEP_RESEARCH_TOGGLE_NOT_FOUND",)   # transient right after the driver opens a new chat (seen live)

# (substring of the driver's stderr/stdout, code, exit code, hint) - first match wins
FAILURES = [
    ("Conversation mismatch", "CONVERSATION_MISMATCH", 2, "the shared Gemini page showed another conversation or another research's report; " + MISMATCH_HINT),
    ("UIA_DRIVER_NOT_IMPLEMENTED", "UIA_DRIVER_NOT_IMPLEMENTED", 3,
     "the Windows UI Automation driver is not installed yet: nothing was sent to Gemini; report it, do not retry"),
    ("Could not find a gemini.google.com page", "GEMINI_PAGE_NOT_FOUND", 3,
     "the Gemini window is not on gemini.google.com (not signed in?); the user signs in (start it with --show)"),
    ("Could not activate 'Deep Research' mode", "DEEP_RESEARCH_TOGGLE_NOT_FOUND", 3, "the Tools menu did not offer Deep Research; UI changed or the account lacks it"),
    ("safety/keyword filter blocked", "SAFETY_BLOCKED", 2, "Gemini refused the prompt; the caller must rephrase in a new job"),
    ("Research Plan confirmation button did not appear", "PLAN_TIMEOUT", 2,
     "no plan card within the driver's wait; possibly a refusal worded differently or a prose answer; the caller decides (a plan step: retry in a new job; an edit: the plan is unknown now)"),
    ("Deep Research did not finish within", "REPORT_TIMEOUT", 2, "the research keeps running in the app; a collect step later fetches the report"),
    ("No active '연구 시작'", "NO_CONFIRM_BUTTON", 2, "the plan card has no active start button (countdown fired, already started, or wrong conversation)"),
    ("Stdin closed while waiting", "DRIVER_STDIN_WAIT", 3, "the driver entered its stdin review loop; relay mode never asks for it"),
    ("Timeout 30000ms exceeded", "UI_SELECTOR_TIMEOUT", 3, "a page element did not appear: most often not signed in; the user signs in (start it with --show)"),
    ("Timeout 20000ms exceeded", "UI_SELECTOR_TIMEOUT", 3, "the tools button did not appear; UI changed or not signed in"),
]


class Fail(Exception):
    def __init__(self, code, message, exit_code, hint="", **extra):
        super().__init__(message)
        self.code, self.message, self.exit_code, self.hint, self.extra = code, message, exit_code, hint, extra


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def conversation_id_from_url(url):
    """'https://gemini.google.com/app/<id>' (also /u/<n>/app/<id>, ?query, #fragment, trailing /) -> '<id>', else None.
    Only the Gemini app's own host counts."""
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or (parts.hostname or "") not in _GEMINI_HOSTS:
        return None
    m = _CONVERSATION_PATH_RE.search(parts.path)
    return m.group(1).lower() if m else None


def parse_conversation_line(line):
    """A driver '[CONVERSATION] {...}' line -> {"conversation_id", "url"} or None."""
    s = line.strip()
    if not s.startswith(CONVERSATION_PREFIX):
        return None
    try:
        data = json.loads(s[len(CONVERSATION_PREFIX):])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    cid = conversation_id_from_url(data.get("url")) or None
    if not cid or (data.get("conversation_id") and str(data["conversation_id"]).lower() != cid):
        return None
    return {"conversation_id": cid, "url": data["url"]}


def read_text(path):
    with open(path, "r", encoding="utf-8-sig") as f:
        return f.read()


def write_text(path, text):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def write_json_atomic(path, obj):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def pid_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
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


def kill_tree(proc):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            import signal
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass


FG_TRACER = "fg_tracer.py"
FG_JSONL = "foreground.jsonl"
FG_START_WAIT_S = 5.0     # wait this long for the tracer's start record before the driver runs (else late_start)
FG_STOP_WAIT_S = 5.0
FG_DETACHED_FLAGS = 0x00000008 | 0x00000200   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP: no console window, ever


def _fg_lines(path, run_id):
    out = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and rec.get("run") == run_id:
                    out.append(rec)
    except OSError:
        pass
    return out


def start_fg_tracer(job, relay_dir, run_id, max_s):
    """R6: start fg_tracer.py detached on this (default) desktop, watching this relay process; list argv, no shell,
    same interpreter, cwd = the job. Never started inside the test sandbox. Returns the handle stop_fg_tracer takes."""
    h = {"run_id": run_id, "path": os.path.join(relay_dir, FG_JSONL), "proc": None}
    script = os.path.join(_HERE, FG_TRACER)
    if os.environ.get("AGY_DR_TEST_SANDBOX") == "1":
        h["reason"] = "sandbox"
    elif os.name != "nt":
        h["reason"] = "platform"
    elif not os.path.isfile(script):
        h["reason"] = "tracer_missing"
    if h.get("reason"):
        return h
    argv = [sys.executable, script, "--job", job, "--watch-pid", str(os.getpid()), "--run-id", run_id,
            "--max-s", str(int(max_s)), "--stop-on-eof"]
    try:
        h["proc"] = subprocess.Popen(argv, cwd=job, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, close_fds=True, creationflags=FG_DETACHED_FLAGS)
    except OSError as e:
        h["reason"] = f"start_failed: {type(e).__name__}"
        return h
    t_end = time.time() + FG_START_WAIT_S
    while time.time() < t_end and h["proc"].poll() is None:
        if any(r.get("event") == "start" for r in _fg_lines(h["path"], run_id)):
            h["started"] = True
            break
        time.sleep(0.05)
    return h


def fg_summary_from_lines(lines):
    """The tracer's end record, or (it was killed) a summary rebuilt from its change records."""
    ends = [r for r in lines if r.get("event") == "end"]
    if ends:
        end = {k: v for k, v in ends[-1].items() if k not in ("event", "run")}
        end["complete"] = end.get("reason") != "refused"
        return end
    fg = [r for r in lines if r.get("event") == "foreground"]
    vis = [r for r in lines if r.get("event") == "visible"]
    ours_fg = sum(1 for r in fg if r.get("ours"))
    ours_vis = sum(1 for r in vis if r.get("ours"))
    return {"gemini_foreground_events": sum(1 for r in fg if r.get("gemini") or r.get("ours")),
            "foreground_changes": len(fg), "visible_windows": sum(1 for r in vis if r.get("gemini_windows")),
            "ours": {"foreground_events": ours_fg, "visible_window_samples": ours_vis},
            "violated": bool(ours_fg or ours_vis), "complete": False, "reason": "no_end_record"}


def stop_fg_tracer(h):
    """Stop the tracer (close its stdin; kill it after FG_STOP_WAIT_S) and read this run's records. Returns the
    result's `foreground_invariant` value: {"measured": False, "reason"} when nothing was traced."""
    proc = h.get("proc")
    if proc is None:
        return {"measured": False, "reason": h.get("reason") or "not_started", "violated": False}
    try:
        if proc.stdin:
            proc.stdin.close()
    except OSError:
        pass
    try:
        proc.wait(timeout=FG_STOP_WAIT_S)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
            proc.wait(timeout=FG_STOP_WAIT_S)
        except Exception:  # noqa: BLE001 - the summary below is rebuilt from the records written so far
            pass
    lines = _fg_lines(h["path"], h["run_id"])
    if not lines:
        return {"measured": False, "reason": "no_records", "violated": False}
    out = {"measured": True, "late_start": not h.get("started")}
    out.update(fg_summary_from_lines(lines))
    if out.get("reason") == "refused":
        out["measured"] = False
    out["violated"] = bool(out.get("violated"))
    return out


def resolve_driver(req):
    cands = [req.get("driver"), os.environ.get("GDR_DRIVER"),
             os.path.join(os.path.dirname(os.path.abspath(__file__)), DRIVER),
             os.path.join(os.path.expanduser("~"), ".gemini", "config", "skills", "local-gemini-deep-research", "scripts", DRIVER)]
    for c in cands:
        if c and os.path.isfile(c):
            return os.path.abspath(c)
    raise Fail("DRIVER_NOT_FOUND", f"{DRIVER} not found", 3, "install the local-gemini-deep-research skill or set request.driver")


def driver_features(driver):
    src = read_text(driver)
    return {"prompt_file": '"--prompt-file"' in src, "edit_prompt_file": '"--edit-prompt-file"' in src, "step": '"--step"' in src}


def text_arg(features, feat_key, flag_file, flag_text, path, what):
    text = read_text(path).strip()
    if not text:
        raise Fail("BAD_REQUEST", f"{what} file is empty: {path}", 5)
    if features.get(feat_key):
        return [flag_file, path]
    if len(text) > PROMPT_ARGV_CAP:
        raise Fail("PROMPT_TOO_LONG", f"{what} is {len(text)} chars and this driver has no {flag_file}", 5, "update the driver (file flags) or shorten the text")
    return [flag_text, text]


def probe_page(req, py, timeout=45, job=None):
    """Read-only look at the conversation through the driver's `--step probe` (it settles the page on req.resume_url).
    Returns the driver's {"start_enabled": bool, "plan_cards": int, "url": str} plus `active_confirm` (1/0) for the gates below, or
    {"error": ...} when the probe failed (non-zero exit or no JSON)."""
    try:
        driver = resolve_driver(req)
    except Fail as e:
        return {"error": f"{e.code}: {e.message}"}
    argv = [py, driver, "--step", "probe"]
    if req.get("resume_url"):
        argv += ["--resume-url", req["resume_url"]]
    if job and '"--job-dir"' in read_text(driver):   # the driver reads this job's job.json / relay/driver_state.json
        argv += ["--job-dir", job]
    extra_env = req.get("env") or {}
    if not isinstance(extra_env, dict):
        return {"error": f"request env must be an object of NAME: value, not {type(extra_env).__name__}"}
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    env.update({str(k): str(v) for k, v in extra_env.items()})
    try:
        r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, stdin=subprocess.DEVNULL, env=env, cwd=job or None)
    except Exception as e:  # noqa: BLE001 - the probe is best effort
        return {"error": scrub_text(str(e)[-300:])}
    lines = (r.stdout or "").strip().splitlines()
    last = lines[-1].strip() if lines else ""
    info = None
    if last.startswith("{"):
        try:
            info = json.loads(last)
        except ValueError:
            info = None
    if r.returncode != 0 or not isinstance(info, dict) or info.get("status") == "error":
        code = (info or {}).get("code") if isinstance(info, dict) else None
        detail = code or (r.stderr or "")[-300:] or "no output"
        return {"error": scrub_text(f"driver probe failed (rc {r.returncode}): {detail}")}
    # fail closed on a broken contract: a missing/non-boolean start_enabled must never read as "no start button"
    if not isinstance(info.get("start_enabled"), bool):
        return {"error": "driver probe broke its contract: no boolean start_enabled"}
    cards = info.get("plan_cards")
    if cards is not None and (isinstance(cards, bool) or not isinstance(cards, int)):
        return {"error": "driver probe broke its contract: plan_cards is not an integer"}
    info["active_confirm"] = 1 if info["start_enabled"] else 0
    return info


class Run:
    def __init__(self):
        self.rc = None
        self.out = []
        self.err = []
        self.elapsed = 0.0
        self.killed = None
        self.confirm_clicked = False
        self.edit_clicked = False
        self.monitoring = False
        self.navigated = False
        self.prompt_sent = False
        self.app_status = None
        self.progress = None
        self.guard_trip = None
        self.conversation = None   # {"conversation_id", "url"} from the driver's [CONVERSATION] line
        self.in_report = False
        self.started_at = None

    @property
    def stdout(self):
        return "".join(self.out)

    @property
    def stderr(self):
        return "".join(self.err)


def run_driver(argv, cwd, out_path, err_path, deadline, allow_missing_confirm, progress_path=None, extra_env=None,
               conversation_path=None, conversation_meta=None):
    env = dict(os.environ)
    env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "NO_COLOR": "1"})
    env.update({str(k): str(v) for k, v in (extra_env or {}).items()})
    kw = {"start_new_session": True} if os.name != "nt" else {}
    run = Run()
    t0 = time.time()
    run.started_at = t0
    try:
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace", bufsize=1, **kw)
    except OSError as e:
        if getattr(e, "winerror", None) == 206 or "Argument list too long" in str(e):
            raise Fail("CMDLINE_TOO_LONG", str(e), 3, "the driver lacks --prompt-file; update it or shorten the prompt")
        raise Fail("DRIVER_CRASH", f"could not start the driver: {e}", 3)
    lock = threading.Lock()

    def reader(stream, lines, path, is_out):
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            for line in stream:
                f.write(_scrub_line(line))   # the log is scrubbed; the in-memory copy below stays raw for parsing
                f.flush()
                with lock:
                    lines.append(line)
                if not is_out:
                    continue
                if REPORT_START in line:
                    run.in_report = True   # the report may quote anything: no control line counts after this
                if not run.in_report and run.conversation is None and line.lstrip().startswith(CONVERSATION_PREFIX):
                    conv = parse_conversation_line(line)
                    if conv:
                        run.conversation = conv
                        if conversation_path:   # persisted at once: survives a worker that dies later
                            try:
                                write_json_atomic(conversation_path, {**(conversation_meta or {}), **conv, "ts": now_iso()})
                            except OSError:
                                pass
                    continue
                if LINE_CONFIRM in line:
                    run.confirm_clicked = True
                elif LINE_EDIT in line:
                    run.edit_clicked = True
                elif LINE_NAVIGATE in line:
                    run.navigated = True
                elif LINE_PROMPT_SENT in line:
                    run.prompt_sent = True
                elif LINE_APP in line:
                    m = APP_STATUS_RE.search(line)
                    run.app_status = m.group(1) if m else None
                elif LINE_MONITOR in line:
                    run.monitoring = True
                    if not run.confirm_clicked and not allow_missing_confirm:
                        run.guard_trip = "CONFIRM_MISSING"
                else:
                    m = PROGRESS_RE.search(line)
                    if m:
                        run.progress = (int(m.group(1)), m.group(2).strip())
                        if progress_path:
                            try:
                                with open(progress_path, "a", encoding="utf-8", newline="\n") as pf:
                                    pf.write(json.dumps({"ts": now_iso(), "elapsed_s": run.progress[0], "text": scrub_text(run.progress[1])}, ensure_ascii=False) + "\n")
                            except OSError:
                                pass

    t_out = threading.Thread(target=reader, args=(proc.stdout, run.out, out_path, True), daemon=True)
    t_err = threading.Thread(target=reader, args=(proc.stderr, run.err, err_path, False), daemon=True)
    t_out.start()
    t_err.start()
    while proc.poll() is None:
        if run.guard_trip:
            run.killed = "guard"
            kill_tree(proc)
            break
        if time.time() - t0 > deadline:
            run.killed = "deadline"
            kill_tree(proc)
            break
        time.sleep(0.3)
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
    t_out.join(timeout=5)
    t_err.join(timeout=5)
    run.rc = proc.returncode
    run.elapsed = round(time.time() - t0, 1)
    return run


def parse_driver_error(stderr):
    """The driver's last '[DRIVER_ERROR] {...}' stderr record (code, message, details, exit_code) or None."""
    for line in reversed((stderr or "").splitlines()):
        line = line.strip()
        if not line.startswith(DRIVER_ERROR_PREFIX):
            continue
        try:
            rec = json.loads(line[len(DRIVER_ERROR_PREFIX):])
        except ValueError:
            return None
        return rec if isinstance(rec, dict) and isinstance(rec.get("code"), str) and rec["code"] else None
    return None


def classify(run, step):
    text = run.stderr   # exception texts only; stdout holds the plan payload and the report, which may quote any of the needles
    if run.killed == "deadline":
        return Fail("DEADLINE", f"{step}: the driver exceeded the relay deadline and was killed", 3,
                    "the conversation may still produce a plan or report on its own; the caller decides (a plan step: new job; a research: collect)")
    if run.killed == "guard" and run.guard_trip == "CONFIRM_MISSING":
        return Fail("CONFIRM_MISSING", "no active '연구 시작' button: the driver was about to wait for a report this job never started", 2,
                    "the countdown may have started the research, or the tab is on another conversation; the caller decides (collect or a new plan)")
    rec = parse_driver_error(text) if run.rc not in (0, None) else None
    if rec and (rec["code"] not in DRIVER_ERROR_CODES or rec.get("exit_code") not in (2, 3)
                or rec.get("exit_code") != run.rc):
        # a record this relay does not know, or one that disagrees with how the driver exited: not trusted
        return Fail("DRIVER_CRASH", scrub_text(f"inconsistent driver error record (rc {run.rc}, code {rec['code']!r}, "
                                               f"exit_code {rec.get('exit_code')!r})"), 3, "see the stderr log")
    if rec:   # the UI Automation driver's own error record: its code and exit code win over needles and rc
        code = rec["code"]
        exit_code = rec["exit_code"]
        hint = next((h for _n, c, _e, h in FAILURES if c == code), DRIVER_ERROR_HINT)
        if code == "CONVERSATION_MISMATCH" and step in ("plan", "edit"):
            hint = PLAN_MISMATCH_HINT
        extra = {"details": scrub_text(rec["details"])} if rec.get("details") else {}
        return Fail(code, scrub_text(str(rec.get("message") or code)), exit_code, hint, **extra)
    for needle, code, exit_code, hint in FAILURES:
        if needle in text:
            if code == "CONVERSATION_MISMATCH" and step in ("plan", "edit"):
                hint = PLAN_MISMATCH_HINT   # there is no research to collect after a plan or edit step
            tail = [ln.rstrip() for ln in run.stderr.splitlines() if ln.strip()][-3:]
            if code == "UI_SELECTOR_TIMEOUT" and run.prompt_sent:
                return Fail("PAGE_TIMEOUT_AFTER_SEND", scrub_text(tail[-1] if tail else needle), 2,
                            "a page wait timed out AFTER the prompt/feedback was typed: it is in the conversation; never re-run this request (the caller decides)",
                            stderr_tail=scrub_text(tail))
            return Fail(code, scrub_text(tail[-1] if tail else needle), exit_code, hint, stderr_tail=scrub_text(tail))
    if run.rc == 2:
        tail = [ln.rstrip() for ln in run.stderr.splitlines() if ln.strip()][-2:]
        return Fail("DRIVER_ARGS_REJECTED", scrub_text(" | ".join(tail)), 3, "this driver copy does not accept these flags")
    if run.rc not in (0, None):
        tail = [ln.rstrip() for ln in run.stderr.splitlines() if ln.strip()][-5:]
        return Fail("DRIVER_CRASH", scrub_text(tail[-1] if tail else f"exit {run.rc}"), 3, "see the stderr log",
                    stderr_tail=scrub_text(tail))
    return None


def extract_block(text, start, end):
    s = text.rfind(start)
    if s < 0:
        return None, False
    s += len(start)
    e = text.find(end, s)
    if e < 0:
        return text[s:], False
    return text[s:e], True


def parse_hook(run):
    body, complete = extract_block(run.stdout, MARK_START, MARK_END)
    if body is None:
        return None
    if not complete:
        raise Fail("MARKER_INCOMPLETE", "the plan payload started but never ended", 3, "see the stdout log")
    try:
        return json.loads(body.strip())
    except ValueError as e:
        raise Fail("MARKER_INCOMPLETE", f"plan payload is not valid JSON: {e}", 3, "see the stdout log")


def parse_plan_block(stdout):
    body, complete = extract_block(stdout, PLAN_BLOCK_START, PLAN_BLOCK_END)
    return body.strip() if body and complete else None


def parse_report(run):
    body, complete = extract_block(run.stdout, REPORT_START, REPORT_END)
    return body.strip() if body and complete else None


def before_report(stdout):
    """The driver's own log: everything before the first report marker (the report body may quote anything)."""
    cut = stdout.find(REPORT_START)
    return stdout if cut < 0 else stdout[:cut]


def parse_provenance(stdout):
    """The driver's [REPORT_PROVENANCE] line from THIS run: it must be the last line the driver printed before the
    report block (the driver prints it right before the marker), so nothing quoted elsewhere can stand in. -> dict or None"""
    if stdout.find(REPORT_START) < 0:
        return None
    lines = [ln.strip() for ln in before_report(stdout).splitlines() if ln.strip()]
    if not lines or not lines[-1].startswith(PROVENANCE_PREFIX):
        return None
    try:
        data = json.loads(lines[-1][len(PROVENANCE_PREFIX):])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def probe_conversation_mismatch(req, probe_info):
    """None, or why the probe left the page on another conversation than resume_url's ('current' is not judged)."""
    want = conversation_id_from_url(req.get("resume_url"))
    got = conversation_id_from_url(probe_info.get("url"))
    if want and got != want:
        return f"the page did not settle on this job's conversation {want} (it is on {got or probe_info.get('url') or 'unknown'})"
    return None


def set_aside(path, relay_dir, name, written_since=None):
    """Move a rejected report out of the job's way, kept as evidence; with written_since only a file written during
    this run (an older file may be a report accepted earlier). -> new path or None"""
    try:
        if path and os.path.isfile(path):
            if written_since is not None and os.path.getmtime(path) < written_since - 1:
                return None
            dest = os.path.join(relay_dir, name)
            os.replace(path, dest)
            return dest
    except OSError:
        pass
    return None


def count_sources(markdown):
    m = re.search(r"^## 참고 출처 \(Sources\)\s*$(.*)", markdown, re.M | re.S)
    return len(re.findall(r"^\d+\. \[", m.group(1), re.M)) if m else 0


def base_result(req, run=None):
    r = {"relay_version": RELAY_VERSION, "step": req["step"], "seq": req.get("seq"), "finished_at": now_iso()}
    if run is not None:
        r.update({"rc": run.rc, "elapsed_s": run.elapsed, "app_status": run.app_status, "navigated": run.navigated,
                  "prompt_sent": run.prompt_sent, "edit_button_clicked": run.edit_clicked, "confirm_clicked": run.confirm_clicked})
    return r


def do_step(job, req, relay_dir, result_path):
    step = req.get("step")
    if step not in ("plan", "edit", "approve", "collect", "auto", "probe"):
        raise Fail("BAD_REQUEST", f"unknown step '{step}'", 5)
    driver = resolve_driver(req)
    if step == "probe":
        info = probe_page(req, req.get("python") or sys.executable, job=job)
        res = base_result(req)
        res.update({"status": "probed" if not info.get("error") else "error", "probe": info})
        if info.get("error"):
            res.update({"code": "PROBE_FAILED", "message": info["error"], "exit_code": 3})
        write_result(result_path, res)
        return res, 0 if not info.get("error") else 3
    feats = driver_features(driver)
    if not feats["step"]:
        raise Fail("DRIVER_TOO_OLD", "this run_deep_research.py has no --step mode", 3, "update the local-gemini-deep-research skill")
    py = req.get("python") or sys.executable
    seq = int(req.get("seq") or 1)
    probe_info = None
    prev = None
    try:
        with open(result_path, "r", encoding="utf-8") as f:
            prev = json.load(f)
    except (OSError, ValueError):
        prev = None
    if prev and prev.get("seq") == seq and (prev.get("prompt_sent") or prev.get("edit_button_clicked") or prev.get("confirm_clicked")):
        res = base_result(req)
        res.update({"status": "error", "code": "STEP_ALREADY_ACTED", "exit_code": 2, "previous": {k: prev.get(k) for k in ("status", "code", "prompt_sent", "edit_button_clicked", "confirm_clicked", "url")},
                    "message": "this request already acted on the Gemini app (prompt/feedback typed or start clicked); not running the driver again",
                    "hint": "report this; the caller decides what happens next"})
        write_result(result_path, res)
        return res, 2
    prefix = f"{step}_{seq}"
    out_log = os.path.join(relay_dir, prefix + ".stdout.txt")
    err_log = os.path.join(relay_dir, prefix + ".stderr.txt")
    argv = [py, driver]
    if step == "plan":
        if not req.get("prompt_file"):
            raise Fail("BAD_REQUEST", "plan needs prompt_file", 5)
        argv += ["--step", "plan", "--raw-prompt"] + text_arg(feats, "prompt_file", "--prompt-file", "--prompt", req["prompt_file"], "prompt")
        deadline = int(req.get("deadline") or PLAN_DEADLINE)
        allow_missing = True
    elif step == "edit":
        if not req.get("feedback_file") or not req.get("resume_url"):
            raise Fail("BAD_REQUEST", "edit needs feedback_file and resume_url", 5)
        argv += ["--step", "edit", "--resume-url", req["resume_url"]] + text_arg(feats, "edit_prompt_file", "--edit-prompt-file", "--edit-prompt", req["feedback_file"], "feedback")
        deadline = int(req.get("deadline") or PLAN_DEADLINE)
        allow_missing = True
        probe_info = probe_page(req, py, job=job)   # settles the tab on the conversation first (the driver's own goto + 2.5 s is a race); errors are only reported
    elif step == "auto":
        if not req.get("prompt_file") or not req.get("output"):
            raise Fail("BAD_REQUEST", "auto needs prompt_file and output", 5)
        timeout = int(req.get("timeout") or REPORT_TIMEOUT)
        argv += ["--step", "auto", "--raw-prompt", "--output", req["output"], "--timeout", str(timeout)]
        argv += text_arg(feats, "prompt_file", "--prompt-file", "--prompt", req["prompt_file"], "prompt")
        deadline = timeout + PLAN_DEADLINE
        allow_missing = True
    else:
        if not req.get("resume_url") or not req.get("output"):
            raise Fail("BAD_REQUEST", f"{step} needs resume_url and output", 5)
        timeout = int(req.get("timeout") or REPORT_TIMEOUT)
        argv += ["--step", "approve", "--resume-url", req["resume_url"], "--output", req["output"], "--timeout", str(timeout)]
        deadline = timeout + 180
        allow_missing = True if step == "collect" else bool(req.get("allow_missing_confirm"))
        if step == "approve":
            # settle the tab on the conversation and make sure the card the caller approved is the one on screen
            probe_info = probe_page(req, py, job=job)
            if not probe_info.get("error"):
                why = probe_conversation_mismatch(req, probe_info)
                if why:   # the start button on screen belongs to another conversation: never click it
                    res = base_result(req)
                    res.update({"status": "error", "code": "CONVERSATION_MISMATCH", "exit_code": 2, "probe": probe_info,
                                "message": why, "hint": "another job or the user is driving the shared Gemini page, or the "
                                "conversation no longer exists; nothing was clicked. The caller approves again when the app is free"})
                    write_result(result_path, res)
                    return res, 2
                if not probe_info.get("active_confirm"):
                    res = base_result(req)
                    res.update({"status": "error", "code": "CONFIRM_MISSING", "exit_code": 2, "probe": probe_info,
                                "message": "no active '연구 시작' button on the conversation: nothing to approve (countdown fired, research already started, or wrong card)",
                                "hint": "the caller runs status, then collect if a research is running there, otherwise a new plan"})
                    write_result(result_path, res)
                    return res, 2
                exp_url, exp_cards = req.get("expected_url"), req.get("expected_plan_count")
                if (exp_url and probe_info.get("url", "").rstrip("/") != exp_url.rstrip("/")) or (exp_cards and probe_info.get("plan_cards") != exp_cards):
                    res = base_result(req)
                    res.update({"status": "error", "code": "PLAN_CHANGED", "exit_code": 2, "probe": probe_info,
                                "message": f"the conversation does not match what the caller reviewed (url {probe_info.get('url')}, plan cards {probe_info.get('plan_cards')}, expected {exp_cards})",
                                "hint": "someone changed the plan in the app or the tab moved; the caller must read the current plan again (edit) or start a new job"})
                    write_result(result_path, res)
                    return res, 2
        if step == "collect":
            # collect re-attaches; it must never be the click that starts a research the caller did not approve
            probe_info = probe_page(req, py, job=job)
            if probe_info.get("error") and not req.get("allow_probe_error"):
                res = base_result(req)
                res.update({"status": "error", "code": "COLLECT_PROBE_FAILED", "exit_code": 3, "probe": probe_info,
                            "message": f"the read-only page probe failed ({probe_info.get('error')}); without it collect cannot rule out a pending plan",
                            "hint": "fix the environment (the Gemini app and its driver must be available) and collect again"})
                write_result(result_path, res)
                return res, 3
            why = probe_conversation_mismatch(req, probe_info) if not probe_info.get("error") else None
            if why:
                res = base_result(req)
                res.update({"status": "error", "code": "CONVERSATION_MISMATCH", "exit_code": 2, "probe": probe_info,
                            "message": why, "hint": "another job or the user is driving the shared Gemini page, or the "
                            "conversation no longer exists; " + MISMATCH_HINT})
                write_result(result_path, res)
                return res, 2
            if probe_info.get("active_confirm"):
                res = base_result(req)
                res.update({"status": "error", "code": "COLLECT_WOULD_START", "exit_code": 2, "probe": probe_info,
                            "message": "the plan card still has an active '연구 시작' button: the research has not started",
                            "hint": "this is a pending plan, not a running research; the caller approves it (approve --reason) or edits it"})
                write_result(result_path, res)
                return res, 2
    if req.get("show"):
        argv.append("--show")
    write_json_atomic(os.path.join(relay_dir, prefix + ".cmd.json"), {"argv": argv, "cwd": job, "started": now_iso(), "deadline_s": deadline})
    progress = os.path.join(relay_dir, "progress.jsonl") if step in ("approve", "collect", "auto") else None
    conv_kw = {"conversation_path": os.path.join(relay_dir, "conversation.json"), "conversation_meta": {"seq": seq, "step": step}}
    drv_env = dict(req.get("env") or {}) if isinstance(req.get("env"), dict) else {}
    drv_env[STEP_DEADLINE_ENV] = str(deadline)
    # R6: the default desktop's foreground is traced around the plan / edit driver runs (spec section 14, 20-11)
    tracer = start_fg_tracer(job, relay_dir, f"{prefix}-{os.getpid()}-{int(time.time() * 1000)}",
                             2 * deadline + 60) if step in ("plan", "edit") else None
    try:
        run = run_driver(argv, job, out_log, err_log, deadline, allow_missing, progress_path=progress, extra_env=drv_env, **conv_kw)
        err = classify(run, step)
        retried = False
        if err and err.code in RETRY_ONCE and step in ("plan", "auto") and not run.prompt_sent:
            # Observed live: right after "Opening new chat..." the Tools menu sometimes does not open within the driver's
            # five attempts; a second run finds the tab already on the new-chat page and succeeds. One retry, same request.
            retried = True
            out_log = os.path.join(relay_dir, prefix + "b.stdout.txt")
            err_log = os.path.join(relay_dir, prefix + "b.stderr.txt")
            run = run_driver(argv, job, out_log, err_log, deadline, allow_missing, progress_path=progress, extra_env=drv_env, **conv_kw)
            err = classify(run, step)
    finally:
        fg = stop_fg_tracer(tracer) if tracer is not None else None
    res = base_result(req, run)
    res.update({"stdout_log": out_log, "stderr_log": err_log, "retried": retried})
    if fg is not None:
        res.update({"foreground_invariant": fg, "foreground_invariant_violated": bool(fg.get("violated"))})
    pinned = run.conversation or {}
    if pinned:   # the conversation this run bound, on every result (errors too), so the caller can collect later
        res.update({"conversation_id": pinned["conversation_id"], "conversation_url": pinned["url"]})
    if probe_info is not None:
        res["probe"] = {k: probe_info.get(k) for k in ("url", "active_confirm", "active_edit", "plan_cards", "report_len", "navigated", "error")}
    if step == "collect" and run.confirm_clicked:
        # the probe saw no active button, yet the driver clicked one (a card rendered in between): say so loudly
        res.update({"status": "error", "code": "COLLECT_STARTED_RESEARCH", "exit_code": 2,
                    "message": "collect clicked '연구 시작': a research started that the caller had not approved in this step",
                    "hint": "the research is now running in the app; the caller records it and may collect later"})
        write_result(result_path, res)
        return res, 2
    if fg is not None and fg.get("violated"):
        # our instance became the default desktop's foreground or showed a window there: the step fails (exit 2)
        if err:
            res["driver_code"] = err.code
        res.update({"status": "error", "code": "FOREGROUND_INVARIANT_VIOLATED", "exit_code": 2,
                    "message": "our Gemini instance became the foreground window or showed a window on the default desktop",
                    "hint": "nothing from this step is used; report it (R6), do not retry until the cause is known"})
        write_result(result_path, res)
        return res, 2
    if err:
        res.update({"status": "error", "code": err.code, "message": err.message, "hint": err.hint, "exit_code": err.exit_code})
        res.update(err.extra)
        write_result(result_path, res)
        return res, err.exit_code
    if step in ("plan", "edit"):
        payload = parse_hook(run)
        if not payload or not (payload.get("plan") or "").strip():
            res.update({"status": "error", "code": "NO_PLAN_PAYLOAD", "message": "the driver exited without a plan payload", "exit_code": 2,
                        "hint": "see the stdout log; the caller decides"})
            write_result(result_path, res)
            return res, 2
        res.update({"status": "plan_ready" if step == "plan" else "plan_revised", "event": payload.get("event"), "url": payload.get("url"),
                    "plan": payload.get("plan"), "plan_count": payload.get("plan_count"), "plan_chars": len(payload.get("plan") or "")})
        write_result(result_path, res)
        return res, 0
    # Conversation identity: the report counts only with this run's provenance line, naming the job's conversation
    prov = parse_provenance(run.stdout) or {}
    got = str(prov.get("conversation_id") or conversation_id_from_url(prov.get("url")) or "").lower() or None
    if not got:
        res.update({"status": "error", "code": "REPORT_PROVENANCE_MISSING", "exit_code": 2,
                    "rejected_report": set_aside(req["output"], relay_dir, f"rejected_{seq}.report.md", written_since=run.started_at),
                    "message": "the driver did not say which conversation the report came from; nothing was accepted",
                    "hint": "a driver older than this relay, or the run ended before a report was extracted: sync the skill "
                            "(deep_research.py setup) and collect again"})
        write_result(result_path, res)
        return res, 2
    want = conversation_id_from_url(req.get("resume_url")) or pinned.get("conversation_id")
    if want and got != want:
        res.update({"status": "error", "code": "CONVERSATION_MISMATCH", "exit_code": 2, "report_conversation_id": got,
                    "report_url": prov.get("url"), "report_title": prov.get("report_title"),
                    "rejected_report": set_aside(req["output"], relay_dir, f"rejected_{seq}.report.md"),
                    "message": f"the report was extracted from conversation {got}, not this job's {want}",
                    "hint": MISMATCH_HINT})
        write_result(result_path, res)
        return res, 2
    markdown = None
    if os.path.isfile(req["output"]):
        markdown = read_text(req["output"])
    if not markdown or not markdown.strip():
        markdown = parse_report(run)
        if markdown:
            write_text(req["output"], markdown + "\n")
    if not markdown or len(markdown.strip()) < REPORT_MIN_CHARS:
        res.update({"status": "error", "code": "REPORT_EMPTY", "message": f"the report has {len((markdown or '').strip())} chars", "exit_code": 2,
                    "hint": "collect again after a minute; the panel may not have rendered"})
        write_result(result_path, res)
        return res, 2
    res.update({"status": "completed", "report_file": os.path.abspath(req["output"]), "report_chars": len(markdown), "sources_count": count_sources(markdown),
                "url": prov.get("url") or f"https://gemini.google.com/app/{got}", "conversation_id": got,
                "report_title": prov.get("report_title") or ""})
    if step == "auto":
        res["plan"] = parse_plan_block(before_report(run.stdout))
        res["plan_chars"] = len(res["plan"] or "")
    write_result(result_path, res)
    return res, 0


def spawn_detached(job, req):
    """Start a worker relay that keeps running after the caller (and agy's shell) exits; it writes result.json when done.
    On Windows the worker is created through WMI (Win32_Process.Create), so it belongs to no job object or process
    tree that ends with the caller's turn."""
    relay_dir = os.path.join(job, "relay")
    os.makedirs(relay_dir, exist_ok=True)
    log_path = os.path.join(relay_dir, f"{req['step']}_worker.log")
    me = os.path.abspath(__file__)
    if os.name == "nt":
        ps_quote = lambda t: "'" + t.replace("'", "''") + "'"  # noqa: E731
        cmd_line = f'cmd /c ""{sys.executable}" "{me}" --job "{job}" --worker >> "{log_path}" 2>&1"'
        ps = ("$si = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ ShowWindow = [uint16]0 }; "
              f"$p = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{{ CommandLine = {ps_quote(cmd_line)}; "
              f"CurrentDirectory = {ps_quote(job)}; ProcessStartupInformation = $si }}; if ($p.ReturnValue -ne 0) {{ exit 1 }}; Write-Output $p.ProcessId")
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        pid = r.stdout.strip().splitlines()[-1] if r.returncode == 0 and r.stdout.strip() else None
        if pid and pid.isdigit():
            return int(pid), "wmi"
        with open(log_path, "ab") as lf:
            proc = subprocess.Popen([sys.executable, me, "--job", job, "--worker"], cwd=job, stdin=subprocess.DEVNULL, stdout=lf,
                                    stderr=subprocess.STDOUT, close_fds=True, creationflags=0x00000008 | 0x00000200)
        return proc.pid, "detached_process"
    with open(log_path, "ab") as lf:
        proc = subprocess.Popen([sys.executable, me, "--job", job, "--worker"], cwd=job, stdin=subprocess.DEVNULL, stdout=lf,
                                stderr=subprocess.STDOUT, close_fds=True, start_new_session=True)
    return proc.pid, "setsid"


def main(argv=None):
    p = argparse.ArgumentParser(description="relay mode: run one deep-research driver step for the caller that owns the plan")
    p.add_argument("--job", required=True, help="job directory holding relay/request.json")
    p.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    a = p.parse_args(argv)
    job = os.path.abspath(os.path.expanduser(a.job))
    relay_dir = os.path.join(job, "relay")
    result_path = os.path.join(relay_dir, "result.json")
    try:
        try:
            with open(os.path.join(relay_dir, "request.json"), "r", encoding="utf-8") as f:
                req = json.load(f)
        except (OSError, ValueError) as e:
            raise Fail("BAD_REQUEST", f"cannot read relay/request.json: {e}", 5)
        if int(req.get("relay_version") or 1) > RELAY_VERSION:
            raise Fail("BAD_REQUEST", f"request relay_version {req.get('relay_version')} is newer than this relay ({RELAY_VERSION})", 5, "update the skill")
        if req.get("step") in ("approve", "collect", "auto") and req.get("mode") == "detach" and not a.worker:
            started_path = os.path.join(relay_dir, "started.json")
            try:
                with open(started_path, "r", encoding="utf-8") as f:
                    prev = json.load(f)
            except (OSError, ValueError):
                prev = None
            if prev and prev.get("pid") and (prev.get("seq") == req.get("seq") or pid_alive(prev["pid"])):
                prev["note"] = ("a worker for this request was already started" if prev.get("seq") == req.get("seq")
                                else f"a worker (pid {prev['pid']}, seq {prev.get('seq')}) is still alive on this job") + "; not starting another"
                print(json.dumps(prev, ensure_ascii=False, indent=2))
                return 0
            try:
                os.remove(result_path)
            except OSError:
                pass
            pid, how = spawn_detached(job, req)
            res = base_result(req)
            res.update({"status": "started", "pid": pid, "launcher": how, "started_at": now_iso(),
                        "message": "worker started; relay/result.json appears when the research is done"})
            write_json_atomic(os.path.join(relay_dir, "started.json"), res)
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0
        if a.worker:
            print(f"[relay-worker] {now_iso()} pid {os.getpid()} step {req.get('step')}", flush=True)
            try:
                with open(os.path.join(relay_dir, "started.json"), "r", encoding="utf-8") as f:
                    st = json.load(f)
                st["worker_pid"] = os.getpid()
                write_json_atomic(os.path.join(relay_dir, "started.json"), st)
            except (OSError, ValueError):
                pass
        res, code = do_step(job, req, relay_dir, result_path)
    except Fail as e:
        _req = locals().get("req") or {}
        res = {"relay_version": RELAY_VERSION, "step": _req.get("step"), "seq": _req.get("seq"), "status": "error", "code": e.code,
               "message": e.message, "hint": e.hint, "exit_code": e.exit_code, "finished_at": now_iso()}
        res.update(e.extra)
        try:
            write_result(result_path, res)
        except OSError:
            pass
        code = e.exit_code
    if not a.worker:
        slim = {k: v for k, v in res.items() if k != "plan"}
        if "plan" in res:
            slim["plan_preview"] = (res["plan"] or "")[:300]
        print(json.dumps(scrub_result(slim), ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())

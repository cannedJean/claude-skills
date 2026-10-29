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
  deadline        plan/edit: seconds before the driver is killed (default 420)
  mode            approve/collect: "wait" (default: block until the report is in) or "detach" (start a worker
                  that keeps running after this process exits; result.json appears when the research is done)
  show            true to show the Gemini window if the driver (re)starts the app
  env             {NAME: value} added to the driver's environment (a detached worker does not inherit the caller's)
  cdp_url         CDP endpoint for the read-only page probe (default http://127.0.0.1:9222)

`collect` never starts a research: before running the driver it probes the page (read-only, through the driver's
Python and playwright) and refuses with COLLECT_WOULD_START when the plan card still has an active start button.
`probe` only runs that read-only probe and writes it to result.json. `auto` records the conversation URL after the run.
  allow_missing_confirm   approve: keep going when the plan card has no active start button (collect: always)

result.json: status (plan_ready | plan_revised | completed | started | error), code, message, hint, rc, event, url,
plan, plan_count, edit_button_clicked, confirm_clicked, app_status, navigated, report_file, report_chars,
sources_count, elapsed_s, stdout_log, stderr_log, pid.

Exit codes: 0 ok | 2 research-level failure (no usable plan / report) | 3 driver or environment error |
5 bad request. Python 3.9+, stdlib only (the driver itself needs playwright).
"""
import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time

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
PLAN_DEADLINE = 420
REPORT_TIMEOUT = 1200
PROMPT_ARGV_CAP = 28000
REPORT_MIN_CHARS = 400
RETRY_ONCE = ("DEEP_RESEARCH_TOGGLE_NOT_FOUND",)   # transient right after the driver opens a new chat (seen live)

# (substring of the driver's stderr/stdout, code, exit code, hint) - first match wins
FAILURES = [
    ("No module named 'playwright'", "PLAYWRIGHT_MISSING", 3, "install playwright into the Python that runs the driver (python -m pip install playwright)"),
    ("No Gemini app-* directory", "APP_NOT_INSTALLED", 3, "install the Google Gemini desktop app for Windows and sign in once"),
    ("Target pattern not found in main.js", "ASAR_PATTERN_MISMATCH", 3,
     "this Gemini app version is not supported by patch_gemini_app.py; Gemini.exe was killed and NOT relaunched: start it by hand, do not retry"),
    ("did not become ready in time", "CDP_NOT_READY", 3, "Gemini was (re)started but port 9222 stayed silent for 20 s; retry once, then check powershell/taskkill on PATH"),
    ("connect_over_cdp", "CDP_CONNECT_FAILED", 3, "CDP answered the liveness check but refused the connection; retry once"),
    ("Could not find a gemini.google.com page", "GEMINI_PAGE_NOT_FOUND", 3,
     "the app is not on gemini.google.com (not signed in?) or another browser owns port 9222; start the app with --show and sign in"),
    ("Could not activate 'Deep Research' mode", "DEEP_RESEARCH_TOGGLE_NOT_FOUND", 3, "the Tools menu did not offer Deep Research; UI changed or the account lacks it"),
    ("safety/keyword filter blocked", "SAFETY_BLOCKED", 2, "Gemini refused the prompt; the caller must rephrase in a new job"),
    ("Research Plan confirmation button did not appear", "PLAN_TIMEOUT", 2,
     "no plan card within the driver's wait; possibly a refusal worded differently or a prose answer; the caller decides (a plan step: retry in a new job; an edit: the plan is unknown now)"),
    ("Deep Research did not finish within", "REPORT_TIMEOUT", 2, "the research keeps running in the app; a collect step later fetches the report"),
    ("No active '연구 시작'", "NO_CONFIRM_BUTTON", 2, "the plan card has no active start button (countdown fired, already started, or wrong conversation)"),
    ("Stdin closed while waiting", "DRIVER_STDIN_WAIT", 3, "the driver entered its stdin review loop; relay mode never asks for it"),
    ("playwright._impl._errors.TimeoutError", "UI_SELECTOR_TIMEOUT", 3, "a page element did not appear: most often not signed in; start the app with --show and sign in"),
    ("Timeout 30000ms exceeded", "UI_SELECTOR_TIMEOUT", 3, "same as above"),
    ("Timeout 20000ms exceeded", "UI_SELECTOR_TIMEOUT", 3, "the tools button did not appear; UI changed or not signed in"),
]


class Fail(Exception):
    def __init__(self, code, message, exit_code, hint="", **extra):
        super().__init__(message)
        self.code, self.message, self.exit_code, self.hint, self.extra = code, message, exit_code, hint, extra


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


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


PROBE_SRC = r"""
import json, sys
from playwright.sync_api import sync_playwright
cdp, target = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "")
with sync_playwright() as p:
    b = p.chromium.connect_over_cdp(cdp)
    page = None
    for ctx in b.contexts:
        for pg in ctx.pages:
            if "gemini.google.com" in pg.url:
                page = pg
                break
        if page:
            break
    if page is None:
        print(json.dumps({"error": "no_gemini_page"}))
        sys.exit(0)
    before = page.url
    if target and target != "current" and page.url.rstrip("/") != target.rstrip("/"):
        page.goto(target, wait_until="domcontentloaded")
        try:
            page.wait_for_selector('[data-test-id="research-steps"], .research-steps, model-response', timeout=15000)
        except Exception:
            pass
        page.wait_for_timeout(2000)
    info = page.evaluate('''() => ({
        active_confirm: document.querySelectorAll('[data-test-id="confirm-button"]:not(.gem-button-disabled) button:not([disabled])').length,
        active_edit: document.querySelectorAll('[data-test-id="edit-button"]:not(.gem-button-disabled) button:not([disabled])').length,
        plan_cards: document.querySelectorAll('[data-test-id="research-steps"], .research-steps').length,
        responses: document.querySelectorAll('model-response').length,
        report_len: ((document.querySelector('deep-research-immersive-panel message-content, deep-research-immersive-panel [data-test-id="message-content"]') || {}).innerText || '').trim().length,
        chip: (((document.querySelector('immersive-entry-chip') || {}).innerText) || '').trim().slice(0, 80)})''')
    info.update({"url": page.url, "url_before": before, "navigated": before != page.url})
    print(json.dumps(info, ensure_ascii=False))
"""


def probe_page(req, py, timeout=45):
    """Read-only look at the Gemini tab (navigating to req.resume_url when the tab is elsewhere). {"error": ...} when unavailable."""
    cdp = (req.get("cdp_url") or os.environ.get("GEMINI_CDP_URL", "http://127.0.0.1:9223")).rstrip("/")
    try:
        r = subprocess.run([py, "-c", PROBE_SRC, cdp, req.get("resume_url") or ""], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, stdin=subprocess.DEVNULL)
        line = (r.stdout or "").strip().splitlines()
        return json.loads(line[-1]) if line and line[-1].startswith("{") else {"error": (r.stderr or "")[-300:] or "no output"}
    except Exception as e:  # noqa: BLE001 - the probe is best effort
        return {"error": str(e)[-300:]}


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

    @property
    def stdout(self):
        return "".join(self.out)

    @property
    def stderr(self):
        return "".join(self.err)


def run_driver(argv, cwd, out_path, err_path, deadline, allow_missing_confirm, progress_path=None, extra_env=None):
    env = dict(os.environ)
    env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "NO_COLOR": "1"})
    env.update({str(k): str(v) for k, v in (extra_env or {}).items()})
    kw = {"start_new_session": True} if os.name != "nt" else {}
    run = Run()
    t0 = time.time()
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
                f.write(line)
                f.flush()
                with lock:
                    lines.append(line)
                if not is_out:
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
                                    pf.write(json.dumps({"ts": now_iso(), "elapsed_s": run.progress[0], "text": run.progress[1]}, ensure_ascii=False) + "\n")
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


def classify(run, step):
    text = run.stderr   # exception texts only; stdout holds the plan payload and the report, which may quote any of the needles
    if run.killed == "deadline":
        return Fail("DEADLINE", f"{step}: the driver exceeded the relay deadline and was killed", 3,
                    "the conversation may still produce a plan or report on its own; the caller decides (a plan step: new job; a research: collect)")
    if run.killed == "guard" and run.guard_trip == "CONFIRM_MISSING":
        return Fail("CONFIRM_MISSING", "no active '연구 시작' button: the driver was about to wait for a report this job never started", 2,
                    "the countdown may have started the research, or the tab is on another conversation; the caller decides (collect or a new plan)")
    for needle, code, exit_code, hint in FAILURES:
        if needle in text:
            tail = [ln.rstrip() for ln in run.stderr.splitlines() if ln.strip()][-3:]
            if code == "UI_SELECTOR_TIMEOUT" and run.prompt_sent:
                return Fail("PAGE_TIMEOUT_AFTER_SEND", (tail[-1] if tail else needle), 2,
                            "a page wait timed out AFTER the prompt/feedback was typed: it is in the conversation; never re-run this request (the caller decides)",
                            stderr_tail=tail)
            return Fail(code, (tail[-1] if tail else needle), exit_code, hint, stderr_tail=tail)
    if run.rc == 2:
        tail = [ln.rstrip() for ln in run.stderr.splitlines() if ln.strip()][-2:]
        return Fail("DRIVER_ARGS_REJECTED", " | ".join(tail), 3, "this driver copy does not accept these flags")
    if run.rc not in (0, None):
        tail = [ln.rstrip() for ln in run.stderr.splitlines() if ln.strip()][-5:]
        return Fail("DRIVER_CRASH", (tail[-1] if tail else f"exit {run.rc}"), 3, "see the stderr log", stderr_tail=tail)
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
        info = probe_page(req, req.get("python") or sys.executable)
        res = base_result(req)
        res.update({"status": "probed" if not info.get("error") else "error", "probe": info})
        if info.get("error"):
            res.update({"code": "PROBE_FAILED", "message": info["error"], "exit_code": 3})
        write_json_atomic(result_path, res)
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
        write_json_atomic(result_path, res)
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
        probe_info = probe_page(req, py)   # settles the tab on the conversation first (the driver's own goto + 2.5 s is a race); errors are only reported
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
            probe_info = probe_page(req, py)
            if not probe_info.get("error"):
                if not probe_info.get("active_confirm"):
                    res = base_result(req)
                    res.update({"status": "error", "code": "CONFIRM_MISSING", "exit_code": 2, "probe": probe_info,
                                "message": "no active '연구 시작' button on the conversation: nothing to approve (countdown fired, research already started, or wrong card)",
                                "hint": "the caller runs status, then collect if a research is running there, otherwise a new plan"})
                    write_json_atomic(result_path, res)
                    return res, 2
                exp_url, exp_cards = req.get("expected_url"), req.get("expected_plan_count")
                if (exp_url and probe_info.get("url", "").rstrip("/") != exp_url.rstrip("/")) or (exp_cards and probe_info.get("plan_cards") != exp_cards):
                    res = base_result(req)
                    res.update({"status": "error", "code": "PLAN_CHANGED", "exit_code": 2, "probe": probe_info,
                                "message": f"the conversation does not match what the caller reviewed (url {probe_info.get('url')}, plan cards {probe_info.get('plan_cards')}, expected {exp_cards})",
                                "hint": "someone changed the plan in the app or the tab moved; the caller must read the current plan again (edit) or start a new job"})
                    write_json_atomic(result_path, res)
                    return res, 2
        if step == "collect":
            # collect re-attaches; it must never be the click that starts a research the caller did not approve
            probe_info = probe_page(req, py)
            if probe_info.get("error") and not req.get("allow_probe_error"):
                res = base_result(req)
                res.update({"status": "error", "code": "COLLECT_PROBE_FAILED", "exit_code": 3, "probe": probe_info,
                            "message": f"the read-only page probe failed ({probe_info.get('error')}); without it collect cannot rule out a pending plan",
                            "hint": "fix the environment (the app and CDP must be up; patch_gemini_app.py) and collect again"})
                write_json_atomic(result_path, res)
                return res, 3
            if probe_info.get("active_confirm"):
                res = base_result(req)
                res.update({"status": "error", "code": "COLLECT_WOULD_START", "exit_code": 2, "probe": probe_info,
                            "message": "the plan card still has an active '연구 시작' button: the research has not started",
                            "hint": "this is a pending plan, not a running research; the caller approves it (approve --reason) or edits it"})
                write_json_atomic(result_path, res)
                return res, 2
    if req.get("show"):
        argv.append("--show")
    write_json_atomic(os.path.join(relay_dir, prefix + ".cmd.json"), {"argv": argv, "cwd": job, "started": now_iso(), "deadline_s": deadline})
    progress = os.path.join(relay_dir, "progress.jsonl") if step in ("approve", "collect", "auto") else None
    run = run_driver(argv, job, out_log, err_log, deadline, allow_missing, progress_path=progress, extra_env=req.get("env"))
    err = classify(run, step)
    retried = False
    if err and err.code in RETRY_ONCE and step in ("plan", "auto") and not run.prompt_sent:
        # Observed live: right after "Opening new chat..." the Tools menu sometimes does not open within the driver's
        # five attempts; a second run finds the tab already on the new-chat page and succeeds. One retry, same request.
        retried = True
        out_log = os.path.join(relay_dir, prefix + "b.stdout.txt")
        err_log = os.path.join(relay_dir, prefix + "b.stderr.txt")
        run = run_driver(argv, job, out_log, err_log, deadline, allow_missing, progress_path=progress, extra_env=req.get("env"))
        err = classify(run, step)
    res = base_result(req, run)
    res.update({"stdout_log": out_log, "stderr_log": err_log, "retried": retried})
    if probe_info is not None:
        res["probe"] = {k: probe_info.get(k) for k in ("url", "active_confirm", "active_edit", "plan_cards", "report_len", "navigated", "error")}
    if step == "collect" and run.confirm_clicked:
        # the probe saw no active button, yet the driver clicked one (a card rendered in between): say so loudly
        res.update({"status": "error", "code": "COLLECT_STARTED_RESEARCH", "exit_code": 2,
                    "message": "collect clicked '연구 시작': a research started that the caller had not approved in this step",
                    "hint": "the research is now running in the app; the caller records it and may collect later"})
        write_json_atomic(result_path, res)
        return res, 2
    if err:
        res.update({"status": "error", "code": err.code, "message": err.message, "hint": err.hint, "exit_code": err.exit_code})
        res.update(err.extra)
        write_json_atomic(result_path, res)
        return res, err.exit_code
    if step in ("plan", "edit"):
        payload = parse_hook(run)
        if not payload or not (payload.get("plan") or "").strip():
            res.update({"status": "error", "code": "NO_PLAN_PAYLOAD", "message": "the driver exited without a plan payload", "exit_code": 2,
                        "hint": "see the stdout log; the caller decides"})
            write_json_atomic(result_path, res)
            return res, 2
        res.update({"status": "plan_ready" if step == "plan" else "plan_revised", "event": payload.get("event"), "url": payload.get("url"),
                    "plan": payload.get("plan"), "plan_count": payload.get("plan_count"), "plan_chars": len(payload.get("plan") or "")})
        write_json_atomic(result_path, res)
        return res, 0
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
        write_json_atomic(result_path, res)
        return res, 2
    res.update({"status": "completed", "report_file": os.path.abspath(req["output"]), "report_chars": len(markdown), "sources_count": count_sources(markdown)})
    if step == "auto":
        res["plan"] = parse_plan_block(run.stdout)
        res["plan_chars"] = len(res["plan"] or "")
        info = probe_page(dict(req, resume_url=""), py)
        if info.get("url"):
            res["url"] = info["url"]
    write_json_atomic(result_path, res)
    return res, 0


def spawn_detached(job, req):
    """Start a worker relay that keeps running after the caller (and agy's shell) exits; it writes result.json when done.
    On Windows the worker is created through WMI (Win32_Process.Create), the same way the driver launches Gemini.exe,
    so it belongs to no job object or process tree that ends with the caller's turn."""
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
            write_json_atomic(result_path, res)
        except OSError:
            pass
        code = e.exit_code
    if not a.worker:
        slim = {k: v for k, v in res.items() if k != "plan"}
        if "plan" in res:
            slim["plan_preview"] = (res["plan"] or "")[:300]
        print(json.dumps(slim, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())

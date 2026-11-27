"""run_deep_research.py - Gemini Deep Research driver (one step per run) for the relay (relay_step.py).

Runs one step on the Gemini desktop app through Windows UI Automation on the tool's hidden desktop
(gemini_host.Host for the instance, uia_backend.UiaTree for the tree, steps.py for the step logic):

  plan    new chat -> model / thinking level -> Deep Research -> prompt -> send; prints `[CONVERSATION] {...}` once
          the chat's URL binds, then cancels the plan card's countdown and prints the
          `=== [PLAN_REVIEW_HOOK_START] ===` block {"event", "status", "url", "plan", "plan_count"}.
  edit    on --resume-url: `연구 계획 수정` -> feedback -> send -> the revised card (countdown cancelled) -> the same block
          (event on_plan_revised). The previous step's cancel result comes from <job>/relay/driver_state.json.
  probe   read-only; prints one JSON line {"start_enabled": bool, "plan_cards": int, "url": str} (an active start
          button on any plan card, the plan-card count, the bound conversation's URL).
  approve, collect, auto: not written yet; print {"status": "error", "code": "UIA_DRIVER_NOT_IMPLEMENTED"} and exit 3
          without touching the app.

Every step holds the host lock (HOST_BUSY when another step holds it) and touches host.json after success. A failure
prints one JSON line {"status": "error", "code", "message"?, "details"?} on stdout and the same record, with
"exit_code", after `[DRIVER_ERROR] ` on stderr (the relay maps it); exit 2 = the job / caller decides, 3 = environment
or UI drift (exit_code_for). Everything printed passes privacy.scrub.
"""
import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

# Ensure UTF-8 output on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")
if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import gemini_host  # noqa: E402
import locators  # noqa: E402
import privacy  # noqa: E402
import relay_step  # noqa: E402  (shared constants only: deadline, error-record prefix)
import steps  # noqa: E402
import uia_backend  # noqa: E402  (imports no comtypes: load_com() does, after the desktop switch)

DRIVER_NOT_IMPLEMENTED = {"status": "error", "code": "UIA_DRIVER_NOT_IMPLEMENTED"}
PROVENANCE_PREFIX = "[REPORT_PROVENANCE] "
CONVERSATION_PREFIX = "[CONVERSATION] "
HOOK_START = "=== [PLAN_REVIEW_HOOK_START] ==="
HOOK_END = "=== [PLAN_REVIEW_HOOK_END] ==="
PLAN_BLOCK_START = "--- [Research Plan] ---"
PLAN_BLOCK_END = "-----------------------"
LINE_PROMPT_SENT = "Prompt submitted."               # relay: prompt_sent (the request acted on the app)
LINE_EDIT_CLICKED = "Clicking '계획 수정' (edit-button)..."  # relay: edit_button_clicked
STATE_FILE = os.path.join("relay", "driver_state.json")
DRIVER_ERROR_PREFIX = relay_step.DRIVER_ERROR_PREFIX   # stderr: the record relay_step.classify reads
DEADLINE_ENV = relay_step.STEP_DEADLINE_ENV       # the relay's deadline for this run (seconds)
RELAY_PLAN_DEADLINE = relay_step.PLAN_DEADLINE    # single source: the relay's default plan/edit deadline
SETUP_MARGIN_S = 60    # host ensure, menus and typing before the timed waits of a plan/edit step
MIN_CARD_WAIT_S = 60   # measured: a plan card appears about 21 s after the send
DEFAULT_MODEL = "Pro"

# exit codes (relay_step.FAILURES uses the same split): 2 = the job / caller decides, the app state is known;
# anything not listed is 3 (environment, host, UI drift or a driver bug).
EXIT_JOB = 2
EXIT_ENV = 3
JOB_LEVEL_CODES = frozenset({
    "CONVERSATION_MISMATCH", "SAFETY_BLOCKED", "GEMINI_ERROR_RESPONSE", "PLAN_TIMEOUT", "PLAN_CARD_COUNT_MISMATCH",
    "UNREVIEWED_START", "COUNTDOWN_NOT_CANCELLED", "INPUT_NOT_EMPTY", "PROMPT_READBACK_MISMATCH", "NO_PLAN_CARD",
    "MODEL_NOT_AVAILABLE", "MODEL_AMBIGUOUS", "MODEL_NOT_SELECTED", "HOST_MODE_UNAVAILABLE",
})

# Thinking-level words the menu may show for a requested level (the menu item names are read at run time; a ko
# account shows `높음` for "high"). The requested value itself is always tried first.
THINKING_WORDS = {
    "high": ("high", "높음"),
    "medium": ("medium", "보통", "중간"),
    "standard": ("standard", "표준"),
    "low": ("low", "낮음"),
}


def exit_code_for(code: str) -> int:
    return EXIT_JOB if code in JOB_LEVEL_CODES else EXIT_ENV


def _say(text: str) -> None:
    print(privacy.scrub(str(text)), flush=True)


def log(msg):
    _say(f"[{time.strftime('%H:%M:%S')}] {msg}")


# --------------------------------------------------------------------------- conversation identity
# The same url parser lives in relay_step.py and deep_research.py (tests/test_conversation_identity.py keeps
# the three in step).
_CONVERSATION_PATH_RE = re.compile(r"(?:^|/)app/([A-Za-z0-9_-]+)(?:/|$)", re.IGNORECASE)
_GEMINI_HOSTS = ("gemini.google.com",)


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


def format_report_provenance(result):
    """The stdout line the relay turns into result.json provenance; printed right before the report block."""
    return PROVENANCE_PREFIX + json.dumps({k: result.get(k) for k in ("conversation_id", "url", "report_title")}, ensure_ascii=False)


def format_conversation_line(conversation_id, url):
    """The stdout line the relay persists (relay/conversation.json) the moment the step's conversation is bound,
    so a later collect finds the research even when this run fails or dies."""
    return CONVERSATION_PREFIX + json.dumps({"conversation_id": conversation_id, "url": url}, ensure_ascii=False)


REQUIRED_XML_TAGS = (
    "research_objective",
    "context_and_scope",
    "investigation_axes",
    "source_governance",
    "output_format",
)


def has_full_xml_prompt_structure(text: str) -> bool:
    clean = (text or "").strip()
    return all(
        re.search(rf"<{tag}\b[^>]*>.*?</{tag}>", clean, re.S | re.I)
        for tag in REQUIRED_XML_TAGS
    )


def build_structured_xml_prompt(
    objective: str,
    context_scope: str | None = None,
    axes: list[str] | None = None,
    sources_policy: str | None = None,
    output_spec: str | None = None,
    workspace_sources: str | None = None,
) -> str:
    """Wrap a topic/objective into the 5-block XML Deep Research architecture."""
    obj_clean = (objective or "").strip()
    if not re.match(
        r"^(?:compare|analyze|synthesize|evaluate|investigate|conduct|design|benchmark|분석|비교|종합|평가|조사|검증)",
        obj_clean,
        re.I,
    ):
        obj_block = (
            "Conduct a comprehensive, multi-dimensional technical analysis and comparative synthesis on the following topic:\n"
            f"{obj_clean}"
        )
    else:
        obj_block = obj_clean

    ctx_lines = []
    if context_scope and str(context_scope).strip():
        ctx_lines.append(str(context_scope).strip())
    else:
        ctx_lines.append(
            "- Scope: Self-contained multi-source investigation covering 2022–2026 production architectures, peer-reviewed literature, and empirical benchmarks.\n"
            "- Exclusions: Skip introductory textbook definitions and superficial marketing overview pages. Do not ask clarifying questions; execute the investigation directly under these declared scope bounds."
        )
    if workspace_sources and str(workspace_sources).strip():
        ctx_lines.append(
            f"- Google Workspace Data Layer Integration: Cross-reference authenticated Google Workspace entities (Drive documents/folders, Gmail threads, Google Chat) matching: {str(workspace_sources).strip()}"
        )
    ctx_block = "\n".join(ctx_lines)

    if axes:
        axes_items = [str(x).strip() for x in axes if str(x).strip()]
    else:
        axes_items = [
            "1. Core Architecture & Algorithmic Mechanics: Analyze underlying data structures, system invariants, and implementation trade-offs.",
            "2. Empirical Performance & Resource Benchmarks: Compare throughput, tail latency (p50/p95/p99), compute/memory footprint, and scalability limits.",
            "3. Operational Resilience & Edge Cases: Evaluate failure modes, recovery protocols, security/compliance implications, and production bottlenecks.",
        ]
    axes_numbered = []
    for idx, item in enumerate(axes_items, 1):
        if re.match(r"^\d+[\.)]\s+", item):
            axes_numbered.append(item)
        else:
            axes_numbered.append(f"{idx}. {item}")
    axes_numbered.append(
        "Conditional Depth-First Trigger: When primary sources or benchmarks report conflicting quantitative metrics, trace the underlying hardware specifications, dataset distributions, and measurement methodologies in the primary text to explain the discrepancy."
    )
    axes_block = "\n".join(axes_numbered)

    if sources_policy and str(sources_policy).strip():
        custom_src = str(sources_policy).strip()
        if "검증 가능한 공개 근거 불충분" not in custom_src:
            src_block = (
                f"{custom_src}\n"
                "- Epistemic Calibration: Strictly separate confirmed empirical facts from architectural hypotheses. If verified public evidence is unavailable for any metric or claim, explicitly state '검증 가능한 공개 근거 불충분 (Insufficient verified evidence)' rather than extrapolating or fabricating numbers."
            )
        else:
            src_block = custom_src
    else:
        src_block = (
            "- Prioritize Tier-1 primary sources: peer-reviewed papers (with DOI/arXiv links), official engineering specifications, standard bodies, and reproducible benchmark repositories.\n"
            "- Do not rely on unverified secondary blogs or vendor marketing claims as sole evidence.\n"
            "- Provide numbered inline citations with publication/verification dates and direct URLs.\n"
            "- Epistemic Calibration: Strictly separate confirmed empirical facts from architectural hypotheses. If verified public evidence is unavailable for any metric or claim, explicitly state '검증 가능한 공개 근거 불충분 (Insufficient verified evidence)' rather than extrapolating or fabricating numbers."
        )

    if output_spec and str(output_spec).strip():
        out_block = (
            f"{str(output_spec).strip()}\n"
            "- Mandatory Gate Invariant: Output MUST be valid GitHub-Flavored Markdown (GFM) of at least 2,500 characters (strictly >= 400 characters) with explicit top-level `#` and section `##` Markdown headings. If structured JSON is requested, embed it inside a fenced ```json code block under a dedicated `##` Markdown section."
        )
    else:
        out_block = (
            "- Format: Comprehensive GitHub-Flavored Markdown (GFM) technical report (>= 2,500 characters; strictly >= 400 characters).\n"
            "- Mandatory Heading Hierarchy:\n"
            "  # [Report Title]\n"
            "  ## 1. Executive Summary & Architectural Verdict\n"
            "  ## 2. Deep-Dive Technical Analysis by Investigation Axis\n"
            "  ## 3. Quantitative Comparison Matrix (Markdown Table with fixed metric columns)\n"
            "  ## 4. Risk Analysis, Edge Cases & Production Recommendations\n"
            "  ## 참고 출처 (Sources)"
        )

    return (
        f"<research_objective>\n{obj_block}\n</research_objective>\n\n"
        f"<context_and_scope>\n{ctx_block}\n</context_and_scope>\n\n"
        f"<investigation_axes>\n{axes_block}\n</investigation_axes>\n\n"
        f"<source_governance>\n{src_block}\n</source_governance>\n\n"
        f"<output_format>\n{out_block}\n</output_format>"
    )


def validate_edit_instructions(edit_prompt: str, allow_questions: bool = False) -> str:
    clean = (edit_prompt or "").strip()
    if not clean:
        raise ValueError("--edit-prompt cannot be empty.")
    if not allow_questions:
        patterns = (
            re.compile(r"\?\s*$", re.M),
            re.compile(r"^\s*(?:why|how\s+come|what\s+is\s+the\s+reason|can\s+you\s+explain|could\s+you\s+explain)\b", re.I | re.M),
            re.compile(r"(?:왜\s+[^\n]*?(?:인가요|입니까|했나요|선택했나요|포함했나요)|이유가\s+무엇|설명해\s*주세|답변해\s*주세)", re.M),
        )
        for pat in patterns:
            if pat.search(clean):
                raise ValueError(
                    "Edit feedback contains conversational questions (e.g. '?'). Asking questions during plan editing "
                    "causes Gemini to reply with prose instead of generating a revised Research Plan card (PLAN_CHANGED / PLAN_TIMEOUT). "
                    "Use imperative delta instructions only (Add, Remove, Reorder, Pin)."
                )
    return clean


def plan_max_wait_default():
    """Cap (s) for the plan-card wait (steps.StepConfig.card_timeout_s). GEMINI_DR_PLAN_MAX_WAIT, default 900."""
    try:
        return max(1, int(os.environ.get("GEMINI_DR_PLAN_MAX_WAIT", "900")))
    except ValueError:
        return 900


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Run one Deep Research step on the local Windows Gemini desktop app "
                                                 "(UI Automation on a hidden desktop).")
    parser.add_argument("--prompt", type=str, help="Research prompt/topic to investigate.")
    parser.add_argument("--prompt-file", type=str, help="Path to a UTF-8 file containing the exact research prompt.")
    parser.add_argument(
        "--raw-prompt",
        action="store_true",
        help="Use the exact prompt string provided at invocation time without any modification.",
    )
    parser.add_argument(
        "--compose-xml",
        action="store_true",
        help="Automatically wrap a plain topic/prompt into the 5-block XML Deep Research architecture (<research_objective>, <context_and_scope>, <investigation_axes>, <source_governance>, <output_format>).",
    )
    parser.add_argument("--context-scope", type=str, help="Custom <context_and_scope> content when --compose-xml is used.")
    parser.add_argument("--axes", action="append", help="Investigation axis item(s) for <investigation_axes> (repeatable).")
    parser.add_argument("--sources-policy", type=str, help="Custom <source_governance> rules when --compose-xml is used.")
    parser.add_argument("--output-spec", type=str, help="Custom <output_format> rules when --compose-xml is used.")
    parser.add_argument("--workspace-sources", type=str, help="Google Workspace (Drive/Gmail/Chat) entities to include in <context_and_scope>.")
    parser.add_argument("--allow-questions", action="store_true", help="Bypass interrogative question check in --edit-prompt.")
    parser.add_argument("--output", type=str, help="Optional file path to save the resulting Markdown (plan step: the plan).")
    parser.add_argument("--review-plan", action="store_true",
                        help="Plan Review Hook loop of the auto step (the auto step is not available in this driver yet).")
    parser.add_argument("--plan-hook", type=str,
                        help="Plan hook command of the auto step (the auto step is not available in this driver yet).")
    parser.add_argument(
        "--step",
        choices=["auto", "plan", "edit", "approve", "collect", "probe"],
        default="auto",
        help=(
            "Step-by-step loop control for caller agents: "
            "'plan' (generate plan and return to caller), "
            "'edit' (modify existing plan with --edit-prompt and return revised plan), "
            "'approve' (confirm current plan and execute Deep Research), "
            "'collect' (fetch the report of a running research), "
            "'probe' (read-only look at the conversation: start button state and url), "
            "'auto' (default full flow)."
        ),
    )
    parser.add_argument("--edit-prompt", type=str, help="Modification instructions when using '--step edit'.")
    parser.add_argument("--edit-prompt-file", type=str,
                        help="Path to a UTF-8 file containing modification instructions when using '--step edit'.")
    parser.add_argument("--plan-only", action="store_true", help="Alias for '--step plan'.")
    parser.add_argument("--resume-url", type=str, help="The job's Gemini conversation URL (edit / probe / approve / collect).")
    parser.add_argument("--job-dir", type=str,
                        help="The job directory (default: the current directory, where the relay runs the driver).")
    parser.add_argument("--timeout", type=int, default=1200, help="Max wait time in seconds for research completion (default: 1200).")
    parser.add_argument("--show", action="store_true", help="Ignored: plan / edit / probe run on the hidden desktop only.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate the arguments and print the step that would run as JSON, without touching Gemini.")
    args = parser.parse_args(argv)

    if args.prompt_file:
        with open(args.prompt_file, "r", encoding="utf-8") as f:
            args.prompt = f.read().strip()

    if args.edit_prompt_file:
        with open(args.edit_prompt_file, "r", encoding="utf-8") as f:
            args.edit_prompt = f.read().strip()

    if args.plan_only:
        args.step = "plan"

    if args.step == "edit" and not args.edit_prompt:
        parser.error("--edit-prompt is required when --step edit is used.")
    if args.step == "edit" and args.edit_prompt:
        try:
            args.edit_prompt = validate_edit_instructions(args.edit_prompt, allow_questions=bool(args.allow_questions))
        except ValueError as e:
            parser.error(str(e))
    if args.step in ("edit", "probe") and not args.resume_url:
        parser.error(f"--resume-url (the job's conversation URL) is required when --step {args.step} is used.")
    if args.step == "plan" and not args.prompt:
        parser.error("--prompt (or --prompt-file) is required when --step plan is used.")
    if args.step == "auto" and not args.prompt and not args.resume_url:
        parser.error("Either --prompt or --resume-url must be provided.")
    if args.prompt and args.compose_xml and not args.raw_prompt and not has_full_xml_prompt_structure(args.prompt):
        args.prompt = build_structured_xml_prompt(
            objective=args.prompt,
            context_scope=args.context_scope,
            axes=args.axes,
            sources_policy=args.sources_policy,
            output_spec=args.output_spec,
            workspace_sources=args.workspace_sources,
        )
        log("Composed 5-block XML Deep Research prompt.")
    return args


# --------------------------------------------------------------------------- job files
STATE_KEYS = frozenset({"step", "url", "cancel", "cards", "host_mode", "browser_pid", "at"})


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def valid_state(rec) -> bool:
    """relay/driver_state.json schema: exactly STATE_KEYS with these types (agy cannot write the file, confine_hook
    protects it; anything else is treated as absent)."""
    return (isinstance(rec, dict) and set(rec) == STATE_KEYS
            and rec["step"] in ("plan", "edit")
            and isinstance(rec["url"], str) and bool(steps.CONVERSATION_RE.match(rec["url"]))
            and rec["cancel"] in ("cancel_button", "edit_fallback")
            and _is_int(rec["cards"]) and rec["cards"] >= 1
            and rec["host_mode"] in ("D1", "D2")
            and _is_int(rec["browser_pid"]) and rec["browser_pid"] > 0
            and isinstance(rec["at"], (int, float)) and not isinstance(rec["at"], bool))


class JobFiles:
    """The job directory: job.json (read only) and the driver's own relay/driver_state.json, which carries the
    previous plan/edit result (`cancel`, `cards`, `url`) and the job's pinned host mode to the next step. Nothing is
    written unless the directory holds a job.json (a manual run elsewhere leaves no files behind)."""

    def __init__(self, job_dir: Optional[str]):
        self.dir = os.path.abspath(job_dir or os.getcwd())

    def _read(self, rel: str) -> dict:
        try:
            with open(os.path.join(self.dir, rel), encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def is_job(self) -> bool:
        return os.path.isfile(os.path.join(self.dir, "job.json"))

    def job_id(self) -> str:
        jid = self._read("job.json").get("job_id")
        return jid if isinstance(jid, str) and jid.strip() else os.path.basename(self.dir)

    def state_present(self) -> bool:
        return os.path.exists(os.path.join(self.dir, STATE_FILE))

    def state(self) -> dict:
        """The driver state, or {} when it is absent or does not match the schema exactly (valid_state)."""
        rec = self._read(STATE_FILE)
        return rec if valid_state(rec) else {}

    def pinned_mode(self, host_record: Optional[dict]) -> Optional[str]:
        """The mode this job must run on, failing closed (HostError DRIVER_STATE_MISMATCH) when the files disagree.

        - job.json host_mode (written by deep_research.py) wins and must equal the driver state's mode;
        - the driver state's mode must equal host.json's mode when host.json describes the instance the state
          recorded (same browser_pid); a D1 pin that host.json cannot confirm is never trusted;
        - a state file that is present but malformed pins D2 (never a downgrade to D1)."""
        job_mode = self._read("job.json").get("host_mode")
        job_mode = job_mode if job_mode in ("D1", "D2") else None
        st = self.state()
        if st:
            mode = st["host_mode"]
            if job_mode is not None and job_mode != mode:
                raise gemini_host.HostError("DRIVER_STATE_MISMATCH", f"job.json pins {job_mode}, driver state says {mode}")
            same = isinstance(host_record, dict) and host_record.get("browser_pid") == st["browser_pid"]
            if same and host_record.get("mode") != mode:
                raise gemini_host.HostError("DRIVER_STATE_MISMATCH",
                                            f"driver state says {mode}, the instance it recorded runs {host_record.get('mode')}")
            if not same and mode == "D1" and job_mode is None:
                raise gemini_host.HostError("DRIVER_STATE_MISMATCH",
                                            "the driver state pins D1 but no running instance confirms it; start a new job")
            return mode
        if self.state_present():
            return job_mode or "D2"
        return job_mode

    def prev_cancel(self) -> Optional[str]:
        return self.state().get("cancel")

    def save_state(self, **fields) -> None:
        if not self.is_job():
            return
        rec = dict(self.state())
        rec.update(privacy.scrub(fields))
        rec["at"] = time.time()
        if not valid_state(rec):
            raise ValueError("driver state does not match its schema")
        path = os.path.join(self.dir, STATE_FILE)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = "%s.%d.tmp" % (path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(rec, fh, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, path)


# --------------------------------------------------------------------------- driver parts
@dataclass
class DriverParts:
    """What a step needs. `host`: gemini_host.Host (lock / ensure / touch); `open_tree(session)` -> UiTree for that
    instance (the real one switches this thread onto the hidden desktop first); `locs` / `step_config` default to
    the bundled locators (+ locators.local.json) and default_step_config()."""
    host: Any
    open_tree: Callable[[Any], Any]
    locs: Optional[dict] = None
    step_config: Optional[steps.StepConfig] = None


def step_deadline_s() -> int:
    """The relay's deadline for this driver run (relay_step passes it in DEADLINE_ENV; default its PLAN_DEADLINE)."""
    try:
        value = int(os.environ.get(DEADLINE_ENV, ""))
    except ValueError:
        return RELAY_PLAN_DEADLINE
    return value if value > 0 else RELAY_PLAN_DEADLINE


def step_budget_s(cfg: steps.StepConfig, step: str) -> float:
    """Worst-case wall time of a plan/edit step that ends in a stop: setup, the wait before the card wait (URL
    bind for plan, Gemini's canned turn for edit), the card wait, cancel verify, the edit fallback's canned turn,
    the second verify, and a stop_response wait."""
    before = cfg.bind_timeout_s if step == "plan" else cfg.turn_timeout_s
    return (SETUP_MARGIN_S + before + cfg.card_timeout_s + cfg.cancel_verify_s + cfg.turn_timeout_s
            + cfg.cancel_verify_s + cfg.stop_timeout_s)


def default_step_config(step: str = "plan", deadline_s: Optional[int] = None) -> steps.StepConfig:
    """Step timeouts that fit inside the relay's deadline (which kills the driver): the plan-card wait gets what is
    left after every other wait of the step, capped by GEMINI_DR_PLAN_MAX_WAIT. DEADLINE_TOO_SHORT when less than
    MIN_CARD_WAIT_S would remain."""
    cfg = steps.StepConfig(cancel_verify_s=5, ui_timeout_s=15, bind_timeout_s=60, turn_timeout_s=60,
                           stop_timeout_s=60)
    if step not in ("plan", "edit"):
        return cfg
    deadline = step_deadline_s() if deadline_s is None else deadline_s
    cfg.card_timeout_s = 0
    left = deadline - step_budget_s(cfg, step)
    if left < MIN_CARD_WAIT_S:
        raise gemini_host.HostError("DEADLINE_TOO_SHORT", f"a {deadline}s relay deadline leaves {left:.0f}s for the plan card")
    cfg.card_timeout_s = min(left, plan_max_wait_default())
    return cfg


def load_bundled_locators() -> dict:
    from pathlib import Path
    return locators.load_locators(Path(SCRIPT_DIR) / "locators.json", Path(SCRIPT_DIR) / "locators.local.json")


def default_stub(environ=None) -> str:
    """The app's launcher stub (only ever started by gemini_host, never written to)."""
    env = os.environ if environ is None else environ
    base = env.get("LOCALAPPDATA", "")
    if not base or not base.strip():
        raise gemini_host.HostError("APP_NOT_INSTALLED", "LOCALAPPDATA is not set")
    return os.path.join(base, "Google", "Gemini", "Gemini.exe")


_GENERIC_ALL = 0x10000000


def _switch_thread_desktop(name: str) -> None:
    """OpenDesktopW(name) + SetThreadDesktop for the calling thread. Must run before comtypes is imported
    (comtypes creates its hidden STA window on the thread's desktop at import)."""
    gemini_host.refuse_in_sandbox()
    if "comtypes" in sys.modules:
        raise uia_backend.UiaUnavailable("order", "comtypes was imported before the desktop switch")
    import ctypes
    import ctypes.wintypes as wt
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    u32.OpenDesktopW.restype = wt.HANDLE
    u32.OpenDesktopW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.BOOL, wt.DWORD]
    u32.SetThreadDesktop.restype = wt.BOOL
    u32.SetThreadDesktop.argtypes = [wt.HANDLE]
    handle = u32.OpenDesktopW(name, 0, False, _GENERIC_ALL)
    if not handle:
        raise gemini_host.HostError("HOST_DESKTOP_LOST", "the hidden desktop cannot be opened")
    if not u32.SetThreadDesktop(handle):
        raise OSError(ctypes.get_last_error(), "SetThreadDesktop failed")
    # the handle stays open while this thread is on the desktop (the process exits after the step)


def default_driver_factory(args) -> DriverParts:
    """The real host (Win32HostOS) and UIA tree. Refused inside the test sandbox before anything is touched."""
    gemini_host.refuse_in_sandbox()
    if sys.platform != "win32":
        raise uia_backend.UiaUnavailable("platform", "UI Automation is Windows only")
    os_ = gemini_host.Win32HostOS()
    cfg = gemini_host.HostConfig(stub=default_stub(), profile_dir=gemini_host.default_profile_dir())
    host = gemini_host.Host(os_, cfg)

    def open_tree(session):
        _switch_thread_desktop(session.desktop)
        com = uia_backend.load_com()
        inst = [i for i in host.instances() if i.browser_pid == session.browser_pid]
        if len(inst) != 1:
            raise gemini_host.HostError("HOST_DESKTOP_LOST", "our Gemini instance is gone")
        pids = frozenset(inst[0].pids)
        return uia_backend.UiaTree(com, lambda pid: pid in pids)

    return DriverParts(host=host, open_tree=open_tree)


class _WatchedTree:
    """UiTree proxy that prints the relay's action lines BEFORE the act that may change the conversation (fail
    closed: an act that raised may still have happened): the first act after a non-empty set_value is the send
    (`Prompt submitted.`); in an edit step an act before anything is typed is the `연구 계획 수정` click."""

    def __init__(self, tree, step: str):
        self._tree, self._step = tree, step
        self.typed = self.prompt_sent = self.edit_clicked = False

    def documents(self):
        return self._tree.documents()

    def desktop_root(self):
        return self._tree.desktop_root()

    def snapshot(self, doc_id):
        return self._tree.snapshot(doc_id)

    def act(self, node_id, pattern):
        if self.typed and not self.prompt_sent:
            self.prompt_sent = True
            _say(LINE_PROMPT_SENT)
        elif self._step == "edit" and not self.typed and not self.prompt_sent and not self.edit_clicked:
            self.edit_clicked = True
            _say(LINE_EDIT_CLICKED)
        return self._tree.act(node_id, pattern)

    def set_value(self, node_id, text):
        self.typed = bool(text)
        return self._tree.set_value(node_id, text)

    def get_value(self, node_id):
        return self._tree.get_value(node_id)


@dataclass
class StepRun:
    args: Any
    tree: Any
    locs: dict
    cfg: steps.StepConfig
    job: JobFiles
    session: Any


def _hook_block(payload: dict) -> None:
    _say(HOOK_START)
    print(json.dumps(privacy.scrub(payload), ensure_ascii=False, indent=2), flush=True)
    _say(HOOK_END)


def _plan_payload(event: str, url: str, res: dict) -> dict:
    return {"event": event, "status": "plan_ready", "url": url, "plan": res["plan_text"], "plan_count": res["cards"]}


def _thinking_candidates(wanted: Optional[str]) -> list:
    if wanted is None or not str(wanted).strip():
        return [None]
    w = str(wanted).strip()
    out = [w]
    for words in THINKING_WORDS.values():
        if w.casefold() in (x.casefold() for x in words):
            out += [x for x in words if x.casefold() != w.casefold()]
    return out


def _select_mode(run: StepRun) -> str:
    """The job's model (GEMINI_DR_MODE) and thinking level (GEMINI_DR_THINKING), chosen once in the new chat by the
    names the menu shows now. A thinking word the menu does not show is retried in its other language."""
    model = (os.environ.get("GEMINI_DR_MODE") or "").strip() or DEFAULT_MODEL
    cands = _thinking_candidates(os.environ.get("GEMINI_DR_THINKING"))
    for i, thinking in enumerate(cands):
        try:
            return steps.select_mode(run.tree, run.locs, run.cfg, model, thinking)
        except steps.StepError as exc:
            if exc.code == "MODEL_NOT_AVAILABLE" and exc.details.get("kind") == "thinking" and i + 1 < len(cands):
                continue
            raise
    raise AssertionError("unreachable")


def step_plan(run: StepRun) -> None:
    tree, locs, cfg = run.tree, run.locs, run.cfg
    doc = steps.ensure_fresh_chat(tree, locs, cfg)
    mode = _select_mode(run)
    log(f"Mode: {mode}")
    steps.enable_deep_research(tree, locs, cfg)
    steps.enter_prompt(tree, locs, cfg, doc, run.args.prompt)
    url = steps.send_and_bind(tree, locs, cfg, doc)
    _say(format_conversation_line(conversation_id_from_url(url), url))   # only now: the URL is bound
    res = steps.wait_plan_and_cancel(tree, locs, cfg, url, 0)
    run.job.save_state(step="plan", url=url, cancel=res["cancel"], cards=res["cards"], host_mode=run.session.mode,
                       browser_pid=run.session.browser_pid)
    _say("\n" + PLAN_BLOCK_START + "\n" + res["plan_text"] + "\n" + PLAN_BLOCK_END + "\n")
    _hook_block(_plan_payload("on_plan_generated", url, res))
    if run.args.output:
        out = os.path.abspath(run.args.output)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            f.write(privacy.scrub(f"# Research Plan\n\nURL: {url}\n\n{res['plan_text']}\n"))


def step_edit(run: StepRun) -> None:
    url = run.args.resume_url
    steps.bind_conversation(run.tree, url)   # nothing is acted on before the job's conversation is bound
    _say(format_conversation_line(conversation_id_from_url(url), url))
    prev = run.job.prev_cancel()
    res = steps.edit_plan(run.tree, run.locs, run.cfg, url, run.args.edit_prompt, prev_cancel=prev)
    run.job.save_state(step="edit", url=url, cancel=res["cancel"], cards=res["cards"], host_mode=run.session.mode,
                       browser_pid=run.session.browser_pid)
    _hook_block(_plan_payload("on_plan_revised", url, res))


def step_probe(run: StepRun) -> None:
    info = steps.probe_conversation(run.tree, run.locs, run.cfg, run.args.resume_url)
    _say(json.dumps({"start_enabled": bool(info["start_enabled"]), "plan_cards": int(info["plan_cards"]),
                     "url": info["url"]}, ensure_ascii=False))


# step name -> handler; None = not written yet (UIA_DRIVER_NOT_IMPLEMENTED, exit 3, nothing touched)
STEP_HANDLERS = {
    "plan": step_plan,
    "edit": step_edit,
    "probe": step_probe,
    "approve": None,
    "collect": None,
    "auto": None,
}


def _error(code: str, message: str = "", details: Any = None, stdout_record: Optional[dict] = None) -> int:
    rc = exit_code_for(code)
    rec = {"status": "error", "code": code}
    if message:
        rec["message"] = message
    if details:
        rec["details"] = details
    rec = privacy.scrub(rec)
    print(json.dumps(stdout_record if stdout_record is not None else rec, ensure_ascii=False), flush=True)
    print(DRIVER_ERROR_PREFIX + json.dumps(dict(rec, exit_code=rc), ensure_ascii=False), file=sys.stderr, flush=True)
    return rc


def _describe(exc: BaseException):
    """-> (code, message, details) of a failed step; messages and details are scrubbed by _error."""
    if isinstance(exc, steps.StepError):
        return exc.code, "", exc.details
    if isinstance(exc, locators.LocatorMiss):
        return exc.payload.get("code") or "UI_LOCATOR_MISS", "", exc.payload
    if isinstance(exc, gemini_host.HostError):
        return exc.code, exc.message, None
    if isinstance(exc, gemini_host.HostRefused):
        return "HOST_REFUSED", f"refused: {exc.reason}", None
    if isinstance(exc, uia_backend.UiaUnavailable):
        return "UIA_UNAVAILABLE", exc.hint, {"reason": exc.reason, "detail": exc.detail}
    if isinstance(exc, locators.LocatorConfigError):
        return "LOCATOR_CONFIG_ERROR", str(exc)[:300], None
    if isinstance(exc, steps.BackendError):
        return "UIA_BACKEND_ERROR", type(exc).__name__, getattr(exc, "stop_attempt", None)
    return "DRIVER_ERROR", type(exc).__name__, None


def main(argv=None, driver_factory=None):
    args = parse_args(argv)
    if args.dry_run:
        print(json.dumps({"status": "dry_run", "step": args.step, "resume_url": args.resume_url,
                          "prompt_chars": len(args.prompt or ""), "edit_prompt_chars": len(args.edit_prompt or "")},
                         ensure_ascii=False), flush=True)
        return 0
    handler = STEP_HANDLERS.get(args.step)
    if handler is None:
        log(f"ERROR: the {args.step} step is not written for the UI Automation driver yet; nothing was run")
        return _error("UIA_DRIVER_NOT_IMPLEMENTED", f"step {args.step}", stdout_record=DRIVER_NOT_IMPLEMENTED)
    if args.show:
        log("Notice: --show is ignored; this step runs on the hidden desktop only")
    try:
        parts = (driver_factory or default_driver_factory)(args)
        job = JobFiles(args.job_dir)
        with parts.host.lock():
            session = parts.host.ensure(job.pinned_mode(parts.host.load_state()))
            log(f"Gemini host ready: mode={session.mode} reason={session.reason or '-'}")
            tree = _WatchedTree(parts.open_tree(session), args.step)
            run = StepRun(args, tree, parts.locs if parts.locs is not None else load_bundled_locators(),
                          parts.step_config if parts.step_config is not None else default_step_config(args.step),
                          job, session)
            handler(run)
            try:   # bookkeeping only: the step already acted and printed its result
                parts.host.touch(job.job_id())
            except Exception as exc:  # noqa: BLE001
                log(f"Warning: host.json was not updated after the step ({type(exc).__name__})")
        return 0
    except Exception as exc:  # noqa: BLE001 - every failure becomes one scrubbed error record
        code, message, details = _describe(exc)
        return _error(code, message, details)


if __name__ == "__main__":
    sys.exit(main())

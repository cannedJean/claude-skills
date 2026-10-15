"""roles.py - keep the model that writes a unit's code away from the models that judge it.

A model that grades work tends to prefer work it (or its model family) produced, and a lead that wrote the hidden
tests and then writes the code is coding against tests it has read. So every role goes to a different model, and
the script records which model actually filled it:

  lead        the Claude session running this skill. Writes the plan and every test, reviews accepted code.
              Never writes unit code: it knows the hidden tests.
  contractor  writes unit code and never sees a hidden test. agy (Gemini) by default; `claude:<model>` runs
              `claude -p --restricted` in the same kind of job dir when agy keeps failing. Never the lead's model.
  auditor     judges the tests before any code exists (audit: every assertion must follow from a written rule) and
              judges failures (triage: code bug, test bug, spec gap or ambiguity) without seeing the code. A
              `claude -p` process on a model that is neither the lead's nor the unit's contractor's. Never agy: agy
              keeps memories across sessions, so an agy auditor that read hidden tests could pass them to a contractor.
  acceptance  the gates in gates.py and outsource.py: deterministic, no model.

The lead's model is declared in plan.json ("roles": {"lead": "opus"}). The auditor's and a Claude contractor's
models are read back from `claude -p --output-format json` (modelUsage), not taken on trust.
Python 3.9+, stdlib only.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

FAMILIES = ("opus", "sonnet", "haiku", "fable", "gemini")
DEFAULT_AUDITOR = "sonnet"
CONTRACTOR_TOOLS = "Read,Write,Edit,Glob,Grep"  # no shell, no web: --restricted also confines them to the cwd


class RoleError(Exception):
    pass


class ClaudeRunError(RoleError):
    """The claude process itself failed (auth, quota, timeout): a runtime error, not a bad role choice."""


def family(model) -> str:
    """'claude-sonnet-5-5' / 'sonnet' -> 'sonnet'; 'agy' or any Gemini slug -> 'gemini'."""
    m = str(model or "").strip().lower()
    if m == "agy":
        return "gemini"
    return next((f for f in FAMILIES if f in m), m)


def plan_roles(plan: dict) -> dict:
    roles = dict(plan.get("roles") or {})
    if not roles.get("lead"):
        raise RoleError('plan.json must declare the lead\'s model: "roles": {"lead": "<your model, e.g. opus>"} — '
                        "the auditor and any Claude contractor are checked against it")
    roles.setdefault("auditor", DEFAULT_AUDITOR)
    return roles


def require_distinct(role: str, model, **others) -> None:
    """role's model family must differ from every other named role's (self-preference / shared knowledge)."""
    for name, other in others.items():
        if other and family(model) == family(other):
            raise RoleError(f"the {role} ({model}) and the {name} ({other}) are the same model family "
                            f"({family(model)}): pick a different --{'auditor' if role == 'auditor' else 'contractor'}")


def find_claude() -> list:
    exe = os.environ.get("OUTSOURCE_CLAUDE")  # a stand-in for tests (a .py file runs with this Python)
    if exe:
        return [sys.executable, exe] if exe.endswith(".py") else [exe]
    for name in ("claude", "claude.exe"):
        p = shutil.which(name)
        if p:
            return [p]
    for c in (Path.home() / ".local" / "bin" / "claude.exe", Path.home() / ".local" / "bin" / "claude"):
        if c.is_file():
            return [str(c)]
    raise RoleError("the claude CLI was not found (needed for the auditor and Claude contractors)")


def run_claude(prompt: str, *, model: str, cwd: Path, tools: str = "", schema=None, permission_mode: str = "dontAsk",
               max_turns: int = 4, timeout: int = 900) -> dict:
    """One headless, stateless Claude process: --restricted (no code-running tools, file tools confined to cwd, user
    and project settings ignored), no session persistence, no auto memory. The prompt goes in on stdin."""
    cmd = find_claude() + ["-p", "--restricted", "--model", model, "--output-format", "json",
                           "--no-session-persistence", "--permission-mode", permission_mode,
                           "--max-turns", str(max_turns), "--tools", tools]
    if schema:
        cmd += ["--json-schema", json.dumps(schema)]
    env = dict(os.environ, CLAUDE_CODE_DISABLE_AUTO_MEMORY="1", PYTHONIOENCODING="utf-8")
    try:
        cp = subprocess.run(cmd, input=prompt.encode("utf-8"), cwd=str(cwd), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"rc": None, "error": f"claude timed out after {timeout}s", "models": [], "model": None}
    out = cp.stdout.decode("utf-8", "replace")
    res = {}
    try:
        res = json.loads(out[out.index("{"):]) if "{" in out else {}
    except json.JSONDecodeError:
        pass
    usage = res.get("modelUsage") or {}
    models = sorted(usage)
    want = [m for m in models if family(m) == family(model)]
    primary = want[0] if want else (max(models, key=lambda m: (usage[m] or {}).get("outputTokens", 0)) if models else None)
    return {"rc": cp.returncode, "is_error": res.get("is_error"), "subtype": res.get("subtype"),
            "result": res.get("result"), "structured": res.get("structured_output"), "models": models,
            "model": primary, "cost_usd": res.get("total_cost_usd"), "denials": res.get("permission_denials") or [],
            "stderr_tail": cp.stderr.decode("utf-8", "replace")[-600:]}


def check_ran_as(res: dict, role: str, requested: str) -> None:
    """The process must really have run on the requested model family (an alias can resolve elsewhere)."""
    if res.get("rc") != 0 or res.get("is_error"):
        raise ClaudeRunError(f"{role} run failed: {res.get('error') or res.get('subtype') or ''} "
                        f"{(res.get('result') or '')[:200]} {res.get('stderr_tail', '')[-200:]}".strip())
    if family(res.get("model")) != family(requested):
        raise RoleError(f"{role} was asked for {requested} but ran on {res.get('models')}")

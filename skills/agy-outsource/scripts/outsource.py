#!/usr/bin/env python3
"""outsource.py - outsource a coding task to the Antigravity CLI (`agy`) one unit-testable unit at a time.

The lead agent (Claude) writes a plan: units with deliverable files, an exact public interface, behaviour
rules, dependencies, visible tests and sealed hidden tests. Each unit becomes a work order for one agy session;
the delivery is accepted only by deterministic gates (format, scope, interface, imports, visible tests, hidden
tests). Failed gates go back to agy as feedback; units that exhaust their attempts come back to the lead.

  preflight                  agy (via agy-delegate) + test runner
  check PLAN [--red]         validate the plan; --red: the tests must FAIL against interface stubs
  render PLAN UNIT           print the work order agy would get (attempt 1)
  run PLAN [options]         run pending units in dependency order, then the integration tests
  status PLAN                print report.md
  integrate PLAN --dest DIR  copy accepted deliverables (optionally the tests) into a project tree

Exit codes: 0 all selected units accepted (and integration passed), 2 some unit not accepted or integration
failed, 3 agy quota / runtime error, 4 preflight failure, 5 bad plan or arguments. Python 3.9+, stdlib only.
"""
from __future__ import annotations

import argparse
import ast
import concurrent.futures as cf
import datetime as _dt
import hashlib
import importlib.util
import json
import os
import re
import secrets
import shlex
import shutil
import string
import subprocess
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gates  # noqa: E402
import roles  # noqa: E402

SKILL_DIR = Path(__file__).resolve().parent.parent
PROMPT = SKILL_DIR / "prompts" / "work_order.md"
MINI_TEST = Path(__file__).resolve().parent / "mini_test.py"
QUOTA_RE = re.compile(r"RESOURCE_EXHAUSTED|quota reached|\(code 429\)", re.I)
UNIT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_lock = threading.Lock()


class QuotaExhausted(Exception):
    """agy reported RESOURCE_EXHAUSTED; every further call would fail the same way."""


class PlanError(Exception):
    pass


def log(msg: str) -> None:
    with _lock:
        sys.stderr.write(f"[outsource {_dt.datetime.now():%H:%M:%S}] {msg}\n")
        sys.stderr.flush()


def find_agy_task() -> Path:
    cands = [os.environ.get("AGY_TASK"),
             SKILL_DIR.parent / "agy-delegate" / "scripts" / "agy_task.py",
             Path.home() / ".claude" / "skills" / "agy-delegate" / "scripts" / "agy_task.py"]
    for c in cands:
        if c and Path(c).is_file():
            return Path(c)
    raise FileNotFoundError("agy-delegate skill not found; install it next to this skill or set AGY_TASK")


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except json.JSONDecodeError:
        return default


def copy_file(src, dst: Path) -> None:
    """src is a path, or the bytes of a sealed hidden test."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(src, bytes):
        dst.write_bytes(src)
    else:
        shutil.copy2(src, dst)


# --------------------------------------------------------------------------- sealed hidden tests
# `seal` moves the hidden tests into <plan dir>/hidden.sealed, encrypted with Windows DPAPI (user scope), and
# deletes the plaintext. They then exist in clear only inside verify/red/integration dirs while no agy session
# runs, and are deleted from there right after the test gate.
SEALED = "hidden.sealed"


def _dpapi(data: bytes, protect: bool) -> bytes:
    if os.name != "nt":
        raise PlanError("sealing hidden tests needs Windows DPAPI")
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
    buf = ctypes.create_string_buffer(data, len(data))
    inp, outp = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), Blob()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(inp), None, None, None, None, 0x1, ctypes.byref(outp)):  # 0x1: UI_FORBIDDEN
        raise PlanError(f"DPAPI {'protect' if protect else 'unprotect'} failed ({ctypes.GetLastError()})")
    try:
        return ctypes.string_at(outp.pbData, outp.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(outp.pbData)


def sealed_store(plan) -> dict:
    return read_json(plan["_dir"] / SEALED, {}) or {}


def hidden_src(plan, rel: str):
    """The plaintext file if it is still there, else the decrypted bytes from hidden.sealed (None if neither)."""
    p = plan["_dir"] / rel
    if p.is_file():
        return p
    blob = sealed_store(plan).get(rel)
    return _dpapi(bytes.fromhex(blob), False) if blob else None


def drop_hidden(plan, root: Path) -> None:
    """Delete every hidden test copied under root (they never stay where agy could look)."""
    for u in plan["units"]:
        for f in u["hidden_tests"]:
            (root / f).unlink(missing_ok=True)


def sweep_hidden(plan, root: Path) -> None:
    """Delete every plaintext copy of a hidden test anywhere under root (a run dir or a unit dir), e.g. what a
    crashed or interrupted run left in a verify, red or integration tree. The plan's own hidden/ files are kept."""
    names = {Path(f).name for u in plan["units"] for f in u["hidden_tests"]}
    originals = {(plan["_dir"] / f).resolve() for u in plan["units"] for f in u["hidden_tests"]}
    if names and Path(root).is_dir():
        for p in Path(root).rglob("*"):
            if p.name in names and p.is_file() and p.resolve() not in originals:
                p.unlink(missing_ok=True)


def digest_path(p: Path):
    return gates.sha256_file(p) if p.is_file() else None


def digest(src):
    """sha256 of a source given as a path or as bytes (a decrypted hidden test)."""
    return hashlib.sha256(src).hexdigest() if isinstance(src, bytes) else digest_path(Path(src))


# --------------------------------------------------------------------------- contractor job dirs
def jobs_root() -> Path:
    """Where contractor work dirs are made. agy takes the git repository that encloses its work dir as its
    workspace, so a work dir under <project>/.outsource/... exposes the whole repository (hidden tests, plan, run
    records) to agy's own permission checks; here the work dir is outside every repository instead."""
    if os.environ.get("AGY_OUTSOURCE_JOBS"):
        return Path(os.environ["AGY_OUTSOURCE_JOBS"]).resolve()
    base = os.environ.get("LOCALAPPDATA") if os.name == "nt" else os.environ.get("XDG_CACHE_HOME")
    return Path(base or Path.home() / ".cache").resolve() / "agy-outsource" / "jobs"


def is_within(path: Path, root: Path) -> bool:
    path, root = Path(path).resolve(), Path(root).resolve()
    return path == root or root in path.parents


def check_jobs_root(plan, out: Path) -> Path:
    root = jobs_root()
    for d in [root, *root.parents]:
        if (d / ".git").exists():
            raise PlanError(f"contractor work dirs would be inside the git repository {d}: agy treats the whole "
                            "repository as its workspace. Set AGY_OUTSOURCE_JOBS to a folder outside any repository.")
    for what, d in (("plan folder", plan["_dir"]), ("project root", plan["_root"]), ("run dir", out)):
        if is_within(root, d) or is_within(d, root):
            raise PlanError(f"contractor work dirs ({root}) and the {what} ({d}) must not contain each other; "
                            "set AGY_OUTSOURCE_JOBS to a separate folder")
    return root


def new_job_dir() -> Path:
    """A fresh, randomly named job dir: its path says nothing about the plan or the unit."""
    root = jobs_root()
    root.mkdir(parents=True, exist_ok=True)
    job = root / secrets.token_hex(6)
    job.mkdir()
    return job


# --------------------------------------------------------------------------- plan
def safe_rel(p: str) -> bool:
    pp = Path(p)
    return bool(p) and not pp.is_absolute() and ".." not in pp.parts and not re.match(r"^[A-Za-z]:", p) \
        and p != gates.MANIFEST


def load_plan(path) -> dict:
    path = Path(path).resolve()
    try:
        plan = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as e:
        raise PlanError(f"cannot read plan {path}: {e}")
    if not isinstance(plan, dict) or not isinstance(plan.get("units"), list):
        raise PlanError('plan must be a JSON object with a "units" list')
    plan["_path"], plan["_dir"] = path, path.parent
    plan["_root"] = (path.parent / plan.get("project_root", ".")).resolve()
    for u in plan["units"]:
        if not isinstance(u, dict):
            raise PlanError("every unit must be an object")
        for key in ("deliverables", "interface", "spec", "examples", "constraints", "depends_on",
                    "context_files", "tests", "hidden_tests"):
            u.setdefault(key, [])
        for key in ("deliverables", "context_files", "tests", "hidden_tests"):
            u[key] = [str(x).replace("\\", "/") for x in u[key]]
        for it in u["interface"]:
            if isinstance(it, dict) and "file" in it:
                it["file"] = str(it["file"]).replace("\\", "/")
    return plan


def units_by_id(plan) -> dict:
    return {u["id"]: u for u in plan["units"]}


def topo_order(plan) -> list:
    """Unit ids in dependency order; raises PlanError on a cycle."""
    by = units_by_id(plan)
    order, state = [], {}

    def visit(uid, stack):
        if state.get(uid) == 2:
            return
        if state.get(uid) == 1:
            raise PlanError("dependency cycle: " + " -> ".join(stack + [uid]))
        state[uid] = 1
        for d in by[uid]["depends_on"]:
            if d in by:
                visit(d, stack + [uid])
        state[uid] = 2
        order.append(uid)

    for u in plan["units"]:
        visit(u["id"], [])
    return order


def all_deps(plan, uid) -> list:
    """Transitive dependencies of a unit, in dependency order."""
    by, seen = units_by_id(plan), []

    def walk(x):
        for d in by[x]["depends_on"]:
            if d not in seen:
                walk(d)
                seen.append(d)
    walk(uid)
    return seen


def validate_plan(plan) -> tuple:
    errors, warnings = [], []
    if not plan.get("project"):
        errors.append('"project" (a short name) is required')
    if not plan["units"]:
        errors.append("plan has no units")
    lang = plan.get("language", "python")
    ids, owner = set(), {}
    for u in plan["units"]:
        uid = u.get("id")
        where = f"unit {uid!r}"
        if not isinstance(uid, str) or not UNIT_ID_RE.match(uid):
            errors.append(f"{where}: id must match {UNIT_ID_RE.pattern}")
            continue
        if uid in ids:
            errors.append(f"{where}: duplicate id")
        ids.add(uid)
        for k in ("title", "goal"):
            if not u.get(k):
                errors.append(f"{where}: {k!r} is required")
        if not u["deliverables"]:
            errors.append(f"{where}: no deliverables")
        if len(u["deliverables"]) > 4:
            warnings.append(f"{where}: {len(u['deliverables'])} deliverables; units of 1-3 files work best — split it")
        for f in u["deliverables"]:
            if not safe_rel(f):
                errors.append(f"{where}: unsafe deliverable path {f!r}")
            elif f in owner:
                errors.append(f"{where}: deliverable {f} is already owned by unit {owner[f]!r}")
            owner.setdefault(f, uid)
        for it in u["interface"]:
            if not isinstance(it, dict) or not it.get("file") or not it.get("symbol"):
                errors.append(f"{where}: interface items need 'file' and 'symbol'")
                continue
            if it["file"] not in u["deliverables"]:
                errors.append(f"{where}: interface file {it['file']} is not a deliverable of this unit")
            if it.get("signature") and it["file"].endswith(".py"):
                try:
                    gates.parse_signature(it["signature"])
                except SyntaxError as e:
                    errors.append(f"{where}: signature of {it['symbol']} does not parse: {e.msg}")
            if not it.get("contract"):
                warnings.append(f"{where}: {it['symbol']} has no 'contract' sentence")
        if lang == "python" and not u["interface"]:
            warnings.append(f"{where}: no interface declared — the interface gate checks nothing")
        if not u["tests"] and not u["hidden_tests"]:
            errors.append(f"{where}: no tests — every unit must be unit-testable (visible and/or hidden tests)")
        if not u["hidden_tests"]:
            warnings.append(f"{where}: no hidden tests — a delivery can be fitted to the visible tests")
        if not u["spec"]:
            warnings.append(f"{where}: no behaviour rules ('spec')")
        for key in ("tests", "hidden_tests"):
            for f in u[key]:
                if not safe_rel(f):
                    errors.append(f"{where}: unsafe {key} path {f!r}")
                elif key == "hidden_tests" and f in sealed_store(plan):
                    pass
                elif not (plan["_dir"] / f).is_file():
                    errors.append(f"{where}: {key} file not found: {plan['_dir'] / f}")
        vis = {Path(f).name for f in u["tests"]}
        for f in u["hidden_tests"]:
            if Path(f).name in vis:
                errors.append(f"{where}: hidden test {f} has the same file name as a visible test")
        for f in u["context_files"]:
            if not safe_rel(f):
                errors.append(f"{where}: unsafe context path {f!r}")
            elif not (plan["_root"] / f).is_file():
                errors.append(f"{where}: context file not found: {plan['_root'] / f}")
        if u.get("hidden_feedback", plan.get("hidden_feedback", "tail")) not in ("tail", "summary", "none"):
            errors.append(f"{where}: hidden_feedback must be tail, summary or none")
        if lang != "python" and not (u.get("test_cmd") or plan.get("test_cmd") not in (None, "auto")):
            errors.append(f"{where}: language {lang!r} needs an explicit test_cmd")
    for u in plan["units"]:
        for d in u.get("depends_on", []):
            if d == u.get("id"):
                errors.append(f"unit {u.get('id')!r}: depends on itself")
            elif d not in ids:
                errors.append(f"unit {u.get('id')!r}: unknown dependency {d!r}")
        for key in ("tests", "hidden_tests", "context_files"):
            for f in u[key]:
                if f in owner:
                    errors.append(f"unit {u.get('id')!r}: {key} path {f} collides with a deliverable of {owner[f]!r}")
    if not errors:
        try:
            topo_order(plan)
        except PlanError as e:
            errors.append(str(e))
    return errors, warnings


def spec_hash(plan, unit, state) -> str:
    """Changes when the unit, the plan-wide rules, its test/context files or its dependencies' accepted code change."""
    h = hashlib.sha256()
    keep = {k: v for k, v in unit.items() if not k.startswith("_")}
    glob_ = {k: plan.get(k) for k in ("project", "context", "language", "runtime", "python", "imports",
                                      "forbid_patterns", "test_cmd", "conventions", "hidden_feedback")}
    h.update(json.dumps([keep, glob_], sort_keys=True, ensure_ascii=False).encode("utf-8"))
    for f in unit["tests"]:
        h.update(gates.sha256_file(plan["_dir"] / f).encode())
    for f in unit["hidden_tests"]:  # hash the plaintext, so sealing does not make accepted units stale
        src = hidden_src(plan, f)
        h.update((hashlib.sha256(src).hexdigest() if isinstance(src, bytes) else gates.sha256_file(src)).encode())
    for f in unit["context_files"]:
        h.update(gates.sha256_file(plan["_root"] / f).encode())
    for d in all_deps(plan, unit["id"]):
        h.update(json.dumps(state["units"].get(d, {}).get("files", {}), sort_keys=True).encode())
    h.update(gates.sha256_file(PROMPT).encode())
    return h.hexdigest()[:16]


# --------------------------------------------------------------------------- work order
def module_of(path: str) -> str:
    mod = path[:-3] if path.endswith(".py") else path
    mod = mod.replace("/", ".")
    return mod[: -len(".__init__")] if mod.endswith(".__init__") else mod


def bullets(items, empty="(none)") -> str:
    return "\n".join(f"- {x}" for x in items) if items else empty


def fmt_interface(unit) -> str:
    lines = []
    for it in unit["interface"]:
        lines.append(f"- in `{it['file']}`: `{it.get('signature') or it['symbol']}`"
                     + (f" — symbol `{it['symbol']}`" if "." in it["symbol"] else ""))
        if it.get("contract"):
            lines.append(f"  - {it['contract']}")
    return "\n".join(lines) or "(no fixed interface; follow the behaviour rules)"


def fmt_examples(unit) -> str:
    out = []
    for ex in unit["examples"]:
        if isinstance(ex, dict):
            if "raises" in ex:
                out.append(f"- `{ex.get('call')}` raises `{ex['raises']}`")
            else:
                out.append(f"- `{ex.get('call')}` → `{ex.get('returns')}`")
        else:
            out.append(f"- {ex}")
    return "\n".join(out) or "(see the tests)"


def fmt_available(plan, unit) -> str:
    by, lines = units_by_id(plan), []
    for d in all_deps(plan, unit["id"]):
        du = by[d]
        lines.append(f"- unit {d} ({du['title']}): " + ", ".join(f"`{f}`" for f in du["deliverables"]))
        for it in du["interface"]:
            imp = ""
            if it["file"].endswith(".py") and "." not in it["symbol"]:
                imp = f" — `from {module_of(it['file'])} import {it['symbol']}`"
            lines.append(f"  - `{it.get('signature') or it['symbol']}`{imp}"
                         + (f": {it['contract']}" if it.get("contract") else ""))
    for f in unit["context_files"]:
        lines.append(f"- context file `{f}`")
    return "\n".join(lines) or "(nothing; this unit has no dependencies)"


def test_cmd_display(plan, unit) -> str:
    cmd = unit.get("test_cmd") or plan.get("test_cmd") or "auto"
    files = " ".join(unit["tests"])
    if cmd == "auto":
        return f"python -m pytest -q {files}" if have_pytest() else f"python -m unittest {files}"
    return cmd.replace("{python}", "python").replace("{tests}", files)


def render_work_order(plan, unit, workdir, feedback="", allow_all=False, confined=True) -> str:
    policy = unit.get("imports", plan.get("imports"))
    imports_rule = ""
    if policy:
        allow = policy.get("allow", [])
        imports_rule = ("Imports: " + ("the standard library, " if policy.get("stdlib", True) else "")
                        + "the project modules listed in this work order"
                        + (", and " + ", ".join(allow) if allow else "") + " only.")
    forbid = list(plan.get("forbid_patterns", [])) + list(unit.get("forbid_patterns", []))
    constraints = list(plan.get("conventions", [])) + list(unit["constraints"])
    constraints += [f"No line may match the regex `{p}`." for p in forbid]
    if plan.get("language", "python") == "python":
        constraints.append(gates.tamper_rule(gates.tamper_allow(plan, unit)))
    runtime =plan.get("runtime") or (f"Python {plan.get('python', '3.9+')}" if plan.get("language", "python") == "python"
                                      else plan.get("language"))
    run_rule = (f"You may run the visible tests with `{test_cmd_display(plan, unit)}`; leave no generated files behind."
                if allow_all else
                "Only files inside your work directory are reachable: any tool call on a path outside it is denied, "
                "and shell commands are disabled. Check your code against the tests by reading them."
                if confined else
                "Shell commands may be denied in this session: check your code against the tests by reading them.")
    tpl = string.Template(PROMPT.read_text(encoding="utf-8"))
    return tpl.safe_substitute(
        unit_id=unit["id"], title=unit["title"], project=plan["project"],
        context=plan.get("context") or "(none)", goal=unit["goal"],
        deliverables=bullets([f"`{f}`" for f in unit["deliverables"]]),
        interface=fmt_interface(unit), spec=bullets(unit["spec"]), examples=fmt_examples(unit),
        constraints=bullets(constraints), available=fmt_available(plan, unit),
        visible_tests=", ".join(f"`{f}`" for f in unit["tests"]) or "(none; only hidden tests)",
        run_rule=run_rule, runtime=runtime, imports_rule=imports_rule,
        files_json=", ".join(json.dumps(f) for f in unit["deliverables"]),
        workdir=str(workdir), feedback=feedback).rstrip() + "\n"


def hidden_summary(tail: str, level: str) -> str:
    """level: tail (output tail), summary (failing test names and assertion lines only), none."""
    if level == "none":
        return "(details withheld)"
    if level == "summary":
        keep = [ln for ln in tail.splitlines()
                if re.match(r"\s*(FAIL|ERROR|FAILED)\b|\s*\w*(Error|Exception)\b[:\s]|\s*E\s{2,}", ln)]
        return "\n".join(keep[-40:]) or "(no assertion lines)"
    return tail[-1500:]


def render_feedback(attempt: int, max_attempts: int, results: list, hidden_level: str = "summary") -> str:
    out = [f"\n## Previous attempt was REJECTED (attempt {attempt} of {max_attempts})",
           "Fix every problem below. The deliverables from the previous attempt are still in the directory; "
           "keep what works, change what is listed. Then rewrite DELIVERY.json."]
    for g in results:
        if g["ok"] or g["gate"].startswith("tests:"):
            continue
        out += [f"- [{g['gate']}] {p}" for p in g["problems"]]
    for g in results:
        if g["gate"] == "tests:visible" and not g["ok"]:
            out += ["", "### Visible tests failed — output tail", "```", g.get("output_tail", "")[-3000:], "```"]
        if g["gate"] == "tests:hidden" and not g["ok"]:
            out += ["", "### Hidden acceptance tests failed — their source is sealed; this is all you get",
                    "```", hidden_summary(g.get("output_tail", ""), hidden_level), "```",
                    "Re-read the behaviour rules: hidden tests only check rules that are written in this work order."]
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- independent auditor (roles.py)
AUDIT = "audit.json"
AUDIT_PROMPT = SKILL_DIR / "prompts" / "audit.md"
TRIAGE_PROMPT = SKILL_DIR / "prompts" / "triage.md"
_VERDICT_ITEM = {"type": "object", "required": ["verdict", "items"],
                 "properties": {"verdict": {"type": "string"}, "items": {"type": "array", "items": {"type": "string"}},
                                "problem": {"type": "string"}, "fix": {"type": "string"}}}
AUDIT_SCHEMA = {"type": "object", "required": ["tests", "summary"], "properties": {
    "tests": {"type": "array", "items": dict(_VERDICT_ITEM, required=["file", "test", "verdict", "items"], properties=dict(
        _VERDICT_ITEM["properties"], file={"type": "string"}, test={"type": "string"},
        verdict={"type": "string", "enum": ["traced", "untraced", "contradicts", "ambiguous"]}))},
    "summary": {"type": "string"}}}
TRIAGE_SCHEMA = {"type": "object", "required": ["failures", "summary"], "properties": {
    "failures": {"type": "array", "items": dict(_VERDICT_ITEM, required=["test", "verdict", "items"], properties=dict(
        _VERDICT_ITEM["properties"], test={"type": "string"},
        verdict={"type": "string", "enum": ["code_bug", "test_bug", "spec_gap", "unclear"]}))},
    "summary": {"type": "string"}}}


def spec_items(plan, unit, prefix: str = "", with_constraints: bool = True) -> list:
    """[(id, text)]: the written specification of a unit as citable items (C interface, R rules, E examples,
    K constraints). This is everything a hidden test may rely on."""
    items = [(f"{prefix}C{i}", f"in `{it['file']}`: `{it.get('signature') or it['symbol']}`"
              + (f" — {it['contract']}" if it.get("contract") else "")) for i, it in enumerate(unit["interface"], 1)]
    items += [(f"{prefix}R{i}", r) for i, r in enumerate(unit["spec"], 1)]
    items += [(f"{prefix}E{i}", line[2:]) for i, line in enumerate(fmt_examples(unit).splitlines(), 1)
              if unit["examples"]]
    if with_constraints:
        items += [(f"{prefix}K{i}", c) for i, c in
                  enumerate(list(plan.get("conventions", [])) + list(unit["constraints"]), 1)]
    return items


def all_spec_items(plan, unit) -> list:
    by = units_by_id(plan)
    items = spec_items(plan, unit)
    for d in all_deps(plan, unit["id"]):
        items += spec_items(plan, by[d], prefix=f"{d}/", with_constraints=False)
    return items


def test_sources(plan, unit) -> list:
    """[(rel, kind, text)] for the unit's visible and hidden tests (hidden ones decrypted in memory only)."""
    out = [(f, "visible", (plan["_dir"] / f).read_text(encoding="utf-8")) for f in unit["tests"]]
    for f in unit["hidden_tests"]:
        src = hidden_src(plan, f)
        out.append((f, "hidden", src.decode("utf-8") if isinstance(src, bytes) else Path(src).read_text(encoding="utf-8")))
    return out


def test_names(rel: str, text: str) -> list:
    """Test functions in a test file: `function` or `Class.method` (non-Python files count as one test)."""
    if not rel.endswith(".py"):
        return ["(file)"]
    names = []
    for n in ast.parse(text).body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test"):
            names.append(n.name)
        elif isinstance(n, ast.ClassDef):
            names += [f"{n.name}.{m.name}" for m in n.body
                      if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m.name.startswith("test")]
    return names


def fmt_items(items) -> str:
    return "\n".join(f"- **{i}** {t}" for i, t in items) or "(no written items)"


def fmt_sources(sources) -> str:
    fence = lambda rel: "python" if rel.endswith(".py") else ""  # noqa: E731
    return "\n\n".join(f"### `{rel}` ({kind})\n```{fence(rel)}\n{text.rstrip()}\n```" for rel, kind, text in sources)


def audit_hash(plan, unit) -> str:
    """Changes when anything the auditor judged changes: the unit's spec, its dependencies' spec, its tests."""
    keep = {k: v for k, v in unit.items() if not k.startswith("_")}
    glob_ = {k: plan.get(k) for k in ("project", "context", "conventions")}
    h = hashlib.sha256(json.dumps([keep, glob_, all_spec_items(plan, unit)], sort_keys=True,
                                  ensure_ascii=False).encode("utf-8"))
    for rel, kind, text in test_sources(plan, unit):
        h.update(f"{kind}:{rel}:{hashlib.sha256(text.encode('utf-8')).hexdigest()}".encode())
    h.update(gates.sha256_file(AUDIT_PROMPT).encode())
    return h.hexdigest()[:16]


def render_audit(plan, unit) -> tuple:
    sources = test_sources(plan, unit)
    expected = [(rel, t) for rel, _, text in sources for t in test_names(rel, text)]
    prompt = string.Template(AUDIT_PROMPT.read_text(encoding="utf-8")).safe_substitute(
        unit_id=unit["id"], title=unit["title"], project=plan["project"], context=plan.get("context") or "",
        goal=unit["goal"], items=fmt_items(all_spec_items(plan, unit)),
        index="\n".join(f"- file `{rel}`, test `{t}`" for rel, t in expected), sources=fmt_sources(sources))
    return prompt, expected


def judge_audit(plan, unit, structured, expected) -> list:
    """Deterministic reading of the auditor's answer: every test judged, every one traced, every cited item real."""
    valid = {i for i, _ in all_spec_items(plan, unit)}
    got = {}
    for e in (structured or {}).get("tests", []):
        rel, t = str(e.get("file", "")).replace("\\", "/").strip("`"), str(e.get("test", "")).strip("`")
        t = t.split("::")[-1]
        full = [x for r, x in expected if r == rel and (x == t or x.split(".")[-1] == t)]
        got[(rel, full[0] if len(full) == 1 else t)] = e
    problems = []
    for rel, t in expected:
        e = got.get((rel, t))
        where = f"{rel}::{t}"
        if e is None:
            problems.append(f"{where}: not judged by the auditor")
        elif e.get("verdict") != "traced":
            problems.append(f"{where}: {e.get('verdict')}: {e.get('problem', '')}"
                            + (f" — fix: {e['fix']}" if e.get("fix") else ""))
        elif not e.get("items"):
            problems.append(f"{where}: traced without citing a specification item")
        elif set(e["items"]) - valid:
            problems.append(f"{where}: cites items that do not exist: {', '.join(sorted(set(e['items']) - valid))}")
    return problems


def audit_problems(plan, roles_: dict, uids) -> list:
    """Why `run` must not start yet: a unit's tests were not passed by an independent auditor in their current form."""
    store = read_json(plan["_dir"] / AUDIT, {}) or {}
    out = []
    for uid in uids:
        e = (store.get("units") or {}).get(uid)
        u = units_by_id(plan)[uid]
        if not e:
            out.append(f"unit {uid!r}: not audited — run `outsource.py audit {plan['_path']}`")
        elif e.get("hash") != audit_hash(plan, u):
            out.append(f"unit {uid!r}: its spec or tests changed since the audit — run `audit` again")
        elif e.get("verdict") != "pass":
            out.append(f"unit {uid!r}: the audit failed — fix the spec or the tests, then run `audit` again: "
                       + "; ".join(e.get("problems", []))[:400])
        elif roles.family(e.get("model")) in (roles.family(roles_["lead"]), "gemini"):
            out.append(f"unit {uid!r}: audited by {e.get('model')}, which is the lead's model family or agy")
    return out


def cmd_audit(a) -> int:
    plan = load_plan(a.plan)
    errors, _ = validate_plan(plan)
    if errors:
        print(json.dumps({"status": "bad-plan", "errors": errors}, ensure_ascii=False, indent=2))
        return 5
    roles_ = roles.plan_roles(plan)
    auditor = a.auditor or roles_["auditor"]
    if roles.family(auditor) == "gemini":
        raise roles.RoleError("agy must never see hidden tests: the auditor has to be a Claude model")
    roles.require_distinct("auditor", auditor, lead=roles_["lead"])
    by = units_by_id(plan)
    uids = [u.strip() for u in a.units.split(",")] if a.units else topo_order(plan)
    store = read_json(plan["_dir"] / AUDIT, {}) or {}
    store.setdefault("units", {})
    summary = {}
    for uid in uids:
        unit = by[uid]
        h = audit_hash(plan, unit)
        old = store["units"].get(uid) or {}
        if not a.force and old.get("hash") == h and old.get("verdict") == "pass":
            summary[uid] = {"verdict": "pass", "model": old.get("model"), "cached": True}
            continue
        prompt, expected = render_audit(plan, unit)
        job = new_job_dir()  # an empty cwd: the auditor reads nothing but the prompt
        try:
            log(f"{uid}: audit -> claude {auditor}")
            res = roles.run_claude(prompt, model=auditor, cwd=job, tools="", schema=AUDIT_SCHEMA,
                                   max_turns=3, timeout=a.timeout * 60)
        finally:
            shutil.rmtree(job, ignore_errors=True)
        roles.check_ran_as(res, "auditor", auditor)
        problems = judge_audit(plan, unit, res["structured"], expected)
        store["units"][uid] = {"hash": h, "verdict": "fail" if problems else "pass", "problems": problems,
                               "auditor": auditor, "model": res["model"], "lead": roles_["lead"],
                               "tests": (res["structured"] or {}).get("tests", []),
                               "summary": (res["structured"] or {}).get("summary", ""), "cost_usd": res.get("cost_usd"),
                               "at": _dt.datetime.now().isoformat(timespec="seconds")}
        write_json(plan["_dir"] / AUDIT, store)
        summary[uid] = {"verdict": store["units"][uid]["verdict"], "model": res["model"], "problems": problems}
        log(f"{uid}: audit {store['units'][uid]['verdict']} ({res['model']})")
    ok = all(s["verdict"] == "pass" for s in summary.values())
    print(json.dumps({"status": "ok" if ok else "fail", "audit": str(plan["_dir"] / AUDIT), "units": summary},
                     ensure_ascii=False, indent=2))
    return 0 if ok else 2


def cmd_triage(a) -> int:
    plan = load_plan(a.plan)
    roles_ = roles.plan_roles(plan)
    by = units_by_id(plan)
    if a.unit not in by:
        raise PlanError(f"unknown unit {a.unit!r}")
    unit, out = by[a.unit], out_dir(plan, a)
    st = (read_json(out / "state.json", {}) or {}).get("units", {}).get(a.unit) or {}
    rec = read_json(out / "units" / a.unit / f"attempt_{st.get('attempts', 0)}.json", None)
    failing = [g for g in (rec or {}).get("gates", []) if g["gate"].startswith("tests:") and not g["ok"]]
    if not failing:
        print(json.dumps({"status": "nothing-to-triage", "message": "the last attempt has no failing test gate",
                          "failed_gates": (rec or {}).get("failed_gates")}, ensure_ascii=False, indent=2))
        return 0
    contractor = st.get("contractor_model") or ("agy" if st.get("contractor", "agy") == "agy"
                                                else st["contractor"].split(":", 1)[1])
    auditor = a.auditor or roles_["auditor"]
    if roles.family(auditor) == "gemini":
        raise roles.RoleError("agy must never see hidden tests: the referee has to be a Claude model")
    roles.require_distinct("auditor", auditor, lead=roles_["lead"], contractor=contractor)
    prompt = string.Template(TRIAGE_PROMPT.read_text(encoding="utf-8")).safe_substitute(
        unit_id=unit["id"], title=unit["title"], project=plan["project"], context=plan.get("context") or "",
        goal=unit["goal"], items=fmt_items(all_spec_items(plan, unit)),
        assumptions=bullets(rec.get("assumptions") or []), sources=fmt_sources(test_sources(plan, unit)),
        failures="\n\n".join(f"### {g['gate']}\n```\n{g.get('output_tail', '')[-3000:]}\n```" for g in failing))
    job = new_job_dir()
    try:
        log(f"{a.unit}: triage -> claude {auditor}")
        res = roles.run_claude(prompt, model=auditor, cwd=job, tools="", schema=TRIAGE_SCHEMA, max_turns=3,
                               timeout=a.timeout * 60)
    finally:
        shutil.rmtree(job, ignore_errors=True)
    roles.check_ran_as(res, "referee", auditor)
    verdict = {"attempt": rec.get("attempt"), "model": res["model"], "contractor": contractor,
               "failures": (res["structured"] or {}).get("failures", []),
               "summary": (res["structured"] or {}).get("summary", ""),
               "at": _dt.datetime.now().isoformat(timespec="seconds")}
    write_json(out / "units" / a.unit / "triage.json", verdict)
    print(json.dumps(dict(verdict, status="ok"), ensure_ascii=False, indent=2))
    return 0


def parse_contractor(spec: str) -> tuple:
    """'agy' -> ('agy', None); 'claude:haiku' -> ('claude', 'haiku')."""
    if not spec or spec == "agy":
        return "agy", None
    kind, _, model = spec.partition(":")
    if kind != "claude" or not model:
        raise PlanError(f"--contractor must be agy or claude:<model>, not {spec!r}")
    return kind, model


def call_claude_contractor(contractor: str, work: Path, prompt_file: Path, a, roles_: dict) -> dict:
    """The fallback contractor: `claude -p --restricted` in the job dir, file tools only, on a model that is not
    the lead's. Returns the same shape as call_agy."""
    _, model = parse_contractor(contractor)
    res = roles.run_claude(prompt_file.read_text(encoding="utf-8"), model=model, cwd=work, tools=roles.CONTRACTOR_TOOLS,
                           permission_mode="acceptEdits", max_turns=80, timeout=a.timeout * 60 + 120)
    (prompt_file.with_suffix("").with_suffix(".claude.json")).write_text(
        json.dumps({k: v for k, v in res.items() if k != "structured"}, ensure_ascii=False, indent=2), encoding="utf-8")
    err = None
    if res.get("rc") != 0 or res.get("is_error"):
        err = f"{res.get('error') or res.get('subtype')}: {(res.get('result') or '')[:300]} {res.get('stderr_tail', '')[-200:]}"
    elif roles.family(res.get("model")) != roles.family(model):
        err = f"asked for {model} but ran on {res.get('models')}"
    elif roles.family(res.get("model")) == roles.family(roles_.get("lead")):
        err = f"ran on the lead's model family ({res.get('model')})"
    quota = None
    if err and re.search(r"usage limit|rate limit|quota", err, re.I):
        quota = err[:200]
    denied = [{"tool": d.get("tool_name"), "reason": json.dumps(d.get("tool_input"), ensure_ascii=False)[:200]}
              for d in res.get("denials", [])]
    return {"rc": 3 if err else 0, "error": err, "quota": quota, "status": res.get("subtype"), "model": res.get("model"),
            "confined": True, "denied_count": len(denied), "denied": denied[:10], "cost_usd": res.get("cost_usd")}


# --------------------------------------------------------------------------- tests
def have_pytest() -> bool:
    return importlib.util.find_spec("pytest") is not None


def test_command(plan, unit, files: list):
    """(argv or shell string, shell flag)."""
    cmd = unit.get("test_cmd") or plan.get("test_cmd") or "auto"
    if cmd == "auto":
        if have_pytest():
            return [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *files], False
        return [sys.executable, str(MINI_TEST), *files], False
    q = (lambda s: subprocess.list2cmdline([s])) if os.name == "nt" else shlex.quote
    return cmd.replace("{python}", q(sys.executable)).replace("{tests}", " ".join(q(f) for f in files)), True


def run_cmd(cmd, shell: bool, cwd: Path, timeout: int) -> tuple:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8",
               PYTHONPATH=os.pathsep.join([str(cwd)] + ([os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else [])))
    try:
        cp = subprocess.run(cmd, shell=shell, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=timeout, env=env)
        return cp.returncode, (cp.stdout + cp.stderr)
    except subprocess.TimeoutExpired as e:
        return 124, f"timed out after {timeout}s\n" + str(e.output or "")[-2000:]


def test_gate(name: str, plan, unit, files: list, cwd: Path) -> dict:
    if not files:
        return dict(gates.gate(name), skipped=True)
    cmd, shell = test_command(plan, unit, files)
    rc, out = run_cmd(cmd, shell, cwd, int(unit.get("test_timeout", plan.get("test_timeout", 120))))
    problems = [f"test command exited {rc}"] if rc != 0 else []
    if rc == 0 and not shell:
        problems += result_line_problems(out)
    g = gates.gate(name, problems)
    g["output_tail"] = out[-4000:]
    g["rc"] = rc
    return g


def result_line_problems(out: str) -> list:
    """Exit code 0 is not proof: the code under test shares the test process and could end it early with 0.
    The runner's own result line must be there and report passed tests and nothing failed or skipped."""
    m = list(re.finditer(r"^mini_test: (\d+) passed, (\d+) failed(?:, (\d+) skipped)?\s*$", out, re.M))
    if m:
        passed, failed, skipped = (int(x or 0) for x in m[-1].groups())
    else:
        tail = [ln for ln in out.splitlines() if re.search(r"\b\d+ (passed|failed|errors?|skipped)\b", ln)]
        if not tail:
            return ["the test run ended without the runner's result line (did the code under test exit the process?)"]
        counts = {k: sum(int(n) for n, kk in re.findall(r"\b(\d+) (passed|failed|errors?|skipped)\b", tail[-1])
                         if kk.rstrip("s") == k.rstrip("s")) for k in ("passed", "failed", "error", "skipped")}
        passed, failed, skipped = counts["passed"], counts["failed"] + counts["error"], counts["skipped"]
    if failed or not passed:
        return [f"the runner reports {passed} passed, {failed} failed"]
    if skipped:
        return [f"{skipped} test(s) skipped: acceptance tests must all run"]
    return []


def build_tree(dst: Path, plan, sources: list) -> None:
    """sources: [(src_path, rel_path)] copied into a fresh dst."""
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    for src, rel in sources:
        copy_file(src if isinstance(src, bytes) else Path(src), dst / rel)


# --------------------------------------------------------------------------- agy
def call_agy(agy_task: Path, work: Path, prompt_file: Path, unit, a) -> dict:
    cmd = [sys.executable, str(agy_task)]
    if a.model:
        cmd += ["--model", a.model]
    cmd += ["run", "-w", str(work), "-f", str(prompt_file), "--timeout", f"{a.timeout}m", "--no-schema"]
    effort = a.effort or unit.get("effort")
    if effort:
        cmd += ["--effort", effort]
    if a.allow_all:
        cmd.append("--allow-all")
    if getattr(a, "no_confine", False):
        cmd.append("--no-confine")
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    log_file = prompt_file.with_suffix("").with_suffix(".agy.log")  # live agy progress: tail -f this file
    try:
        with open(log_file, "wb") as lf:
            cp = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=lf, env=env, timeout=a.timeout * 60 + 120)
    except subprocess.TimeoutExpired:
        return {"rc": None, "error": "timeout"}
    out, err = cp.stdout.decode("utf-8", "replace"), log_file.read_text(encoding="utf-8", errors="replace")
    res = {}
    try:
        res = json.loads(out[out.index("{"):]) if "{" in out else {}
    except json.JSONDecodeError:
        pass
    # agy's quota error (stderr / AGY_ERROR); the caller only believes it when nothing was delivered
    quota = None
    texts = [err, json.dumps(res.get("agy_error") or "")]
    texts += [f.read_text(encoding="utf-8", errors="replace") for f in work.glob(".agy_task/*/stderr.txt")]
    for line in "\n".join(texts).splitlines():
        if QUOTA_RE.search(line):
            m = re.search(r"Resets in [^.\"]+", line)
            quota = m.group(0) if m else line.strip()[:200]
            break
    conf = res.get("confinement") or {}
    return {"rc": cp.returncode, "quota": quota, "status": res.get("status"), "agent_status": res.get("agent_status"),
            "conversation_id": res.get("conversation_id"), "elapsed_sec": res.get("elapsed_sec"),
            "agy_error": res.get("agy_error"), "stderr_tail": err[-600:] if cp.returncode not in (0, 2) else "",
            # agy_task.py confines agy's tool calls to the work dir; only its own report says it really did
            "confined": bool(conf.get("enabled")), "denied_count": conf.get("denied_count", 0),
            "denied": [{k: d.get(k) for k in ("tool", "reason")} for d in conf.get("denied", [])][:10]}


def path_forms(p) -> set:
    s = str(Path(p).resolve())
    return {s, s.replace("\\", "/"), s.replace("\\", "\\\\")}


def scrub(text: str, dirs: list) -> str:
    """Replace absolute run/spec paths in test output so feedback never points agy at the sealed tests."""
    for d in sorted({f for d in dirs for f in path_forms(d)}, key=len, reverse=True):
        text = re.sub(re.escape(d) + r"[\\/]?", "", text, flags=re.I)
    return text


def hidden_peek(plan, unit, work: Path, extra_dirs=()) -> list:
    """Hidden test names or paths that show up in agy's recorded tool calls (it should never see them)."""
    hits = []
    needles = set()
    for f in unit["hidden_tests"]:
        p = (plan["_dir"] / f).resolve()
        needles |= {p.name} | path_forms(p.parent)
    for d in extra_dirs:
        needles |= path_forms(d)
    for ev in work.glob(".agy_task/*/events.jsonl"):
        text = ev.read_text(encoding="utf-8", errors="replace")
        hits += [n for n in needles if n and n in text]
    return sorted(set(hits))


# --------------------------------------------------------------------------- run one unit
class Runner:
    def __init__(self, plan, out: Path, a, agy_task, roles_=None):
        self.plan, self.out, self.a, self.agy_task = plan, out, a, agy_task
        self.roles = roles_ or {}
        self.contractor = getattr(a, "contractor", None) or "agy"
        # what acceptance depends on, fingerprinted once: any change during the run voids the run
        self.evaluator = {str(p): digest_path(p) for p in
                          (Path(__file__).resolve(), Path(gates.__file__).resolve(), MINI_TEST, PROMPT, plan["_path"],
                           plan["_dir"] / SEALED, *[plan["_dir"] / f for u in plan["units"] for f in u["hidden_tests"]])}
        self.by = units_by_id(plan)
        self.state_path = out / "state.json"
        self.state = read_json(self.state_path, None) or {"plan": str(plan["_path"]), "units": {}}
        self.state.setdefault("units", {})

    def save(self):
        with _lock:
            write_json(self.state_path, self.state)

    def ustate(self, uid) -> dict:
        return self.state["units"].setdefault(uid, {"status": "pending", "attempts": 0})

    def frozen_sources(self, unit) -> list:
        """[(src, rel)] read-only inputs: dependencies' accepted code, context files, visible tests."""
        src = []
        for d in all_deps(self.plan, unit["id"]):
            for f in self.by[d]["deliverables"]:
                src.append((self.out / "accepted" / f, f))
        src += [(self.plan["_root"] / f, f) for f in unit["context_files"]]
        src += [(self.plan["_dir"] / f, f) for f in unit["tests"]]
        return src

    def process(self, uid) -> str:
        unit, st = self.by[uid], self.ustate(uid)
        udir = self.out / "units" / uid
        udir.mkdir(parents=True, exist_ok=True)
        # the contractor works in a job dir that has no path relation to the plan, the run dir or any git
        # repository, so neither its own permissions nor a guessed path lead it to the tests or the gates
        job = new_job_dir()
        work = job / "work"
        work.mkdir(parents=True)
        try:
            return self._attempts(uid, unit, st, udir, work)
        finally:
            sweep_hidden(self.plan, udir)
            if not os.environ.get("AGY_OUTSOURCE_KEEP_JOBS"):
                shutil.rmtree(job, ignore_errors=True)

    def _attempts(self, uid, unit, st, udir: Path, work: Path) -> str:
        frozen_src = self.frozen_sources(unit)
        baseline = {rel: digest(src) for src, rel in frozen_src}
        max_attempts = int(self.a.max_attempts or unit.get("max_attempts") or self.plan.get("max_attempts") or 3)
        st.update(status="running", attempts=0, spec_hash=spec_hash(self.plan, unit, self.state), history=[],
                  contractor=self.contractor)
        self.save()
        feedback = ""
        for attempt in range(1, max_attempts + 1):
            for src, rel in frozen_src:  # restore read-only inputs (undoes any tampering of a previous attempt)
                copy_file(src, work / rel)
            (work / gates.MANIFEST).unlink(missing_ok=True)
            frozen = {rel: gates.sha256_file(work / rel) for _, rel in frozen_src}
            before = gates.snapshot(work)
            prompt = render_work_order(self.plan, unit, work, feedback, self.a.allow_all,
                                       not getattr(self.a, "no_confine", False))
            pfile = udir / f"attempt_{attempt}.prompt.md"
            pfile.write_text(prompt, encoding="utf-8")
            log(f"{uid}: attempt {attempt}/{max_attempts} -> {self.contractor}")
            if self.contractor == "agy":
                agy = call_agy(self.agy_task, work, pfile, unit, self.a)
            else:
                agy = call_claude_contractor(self.contractor, work, pfile, self.a, self.roles)
            if (work / ".agy_task").is_dir():  # keep agy's own record of the session; the job dir is deleted
                shutil.copytree(work / ".agy_task", udir / f"attempt_{attempt}.agy_task", dirs_exist_ok=True)
            if agy.get("quota") and not (work / gates.MANIFEST).exists():
                st.update(attempts=attempt - 1)
                raise QuotaExhausted(agy["quota"])
            changes = gates.diff(before, gates.snapshot(work))
            results = gates.static_gates(work, unit, changes, frozen, self.plan)
            if agy.get("rc") not in (0, 2):
                results.insert(0, gates.gate("agy", [f"{self.contractor} run failed (rc={agy.get('rc')}): "
                                                     f"{agy.get('error') or agy.get('agy_error') or agy.get('stderr_tail')}"]))
            integrity = self.integrity_gate(frozen_src, baseline)
            results.insert(0, integrity)
            # tests run on a clean copy: deliverables + read-only inputs (+ the sealed hidden tests, which exist in
            # clear only between build_tree and sweep_hidden, while this unit's contractor session is over)
            verify = udir / "verify"
            srcs = [(work / f, f) for f in unit["deliverables"] if (work / f).is_file()] + frozen_src
            try:
                build_tree(verify, self.plan, srcs + [(hidden_src(self.plan, f), f) for f in unit["hidden_tests"]])
                results.append(test_gate("tests:visible", self.plan, unit, unit["tests"], verify))
                results.append(test_gate("tests:hidden", self.plan, unit, unit["hidden_tests"], verify))
            finally:
                sweep_hidden(self.plan, udir)
            for g in results:
                if "output_tail" in g:
                    g["output_tail"] = scrub(g["output_tail"], [verify, work, self.plan["_dir"]])
            peek = hidden_peek(self.plan, unit, work, [verify])
            ok = all(g["ok"] for g in results)
            failed = [g["gate"] for g in results if not g["ok"]]
            rec = {"attempt": attempt, "ok": ok, "failed_gates": failed, "changes": changes, "agy": agy,
                   "contractor": self.contractor, "gates": results, "hidden_peek": peek,
                   "assumptions": (gates.load_manifest(work)[0] or {}).get("assumptions", [])}
            write_json(udir / f"attempt_{attempt}.json", rec)
            st["attempts"] = attempt
            st["history"].append({"attempt": attempt, "failed_gates": failed})
            if agy.get("model"):
                st["contractor_model"] = agy["model"]
            log(f"{uid}: attempt {attempt} " + ("ACCEPTED" if ok else "rejected: " + ", ".join(failed)))
            if not integrity["ok"]:  # the evaluator or a read-only input changed under us: retrying proves nothing
                st.update(status="needs_attention", last_failed=failed,
                          note="integrity: " + "; ".join(integrity["problems"])[:300])
                self.save()
                return "needs_attention"
            if ok:
                files = {}
                for f in unit["deliverables"]:
                    copy_file(work / f, self.out / "accepted" / f)
                    files[f] = gates.sha256_file(work / f)
                manifest, _ = gates.load_manifest(work)
                if agy.get("confined"):  # a hidden-test mention was a denied attempt: the pass stands
                    flags = (["hidden_peek_denied"] if peek else []) + \
                            (["outside_access_denied"] if agy.get("denied_count") else [])
                else:
                    flags = ["unconfined"] + (["hidden_peek"] if peek else [])
                st.update(status="accepted", files=files, flags=flags,
                          assumptions=(manifest or {}).get("assumptions", []),
                          warnings=[w for g in results for w in g.get("warnings", [])],
                          accepted_at=_dt.datetime.now().isoformat(timespec="seconds"))
                self.save()
                return "accepted"
            feedback = render_feedback(attempt, max_attempts, results,
                                       unit.get("hidden_feedback", self.plan.get("hidden_feedback", "summary")))
            self.save()
        st.update(status="needs_attention", last_failed=st["history"][-1]["failed_gates"])
        self.save()
        return "needs_attention"

    def integrity_gate(self, frozen_src, baseline: dict) -> dict:
        """Nothing the acceptance depends on may change while a unit runs: the gate scripts, the plan, the
        hidden-test store and this unit's read-only inputs (its dependencies' accepted code, context, visible tests)."""
        problems = [f"evaluator file changed: {p}" for p, h in self.evaluator.items() if digest_path(Path(p)) != h]
        problems += [f"read-only input changed at its source: {rel}" for src, rel in frozen_src
                     if digest(src) != baseline[rel]]
        return gates.gate("integrity", problems)

    # ----------------------------------------------------------------------- scheduling
    def pending(self, selected: set) -> list:
        todo = []
        for uid in topo_order(self.plan):
            if uid not in selected:
                continue
            st = self.ustate(uid)
            if self.a.force and uid in self.forced:
                todo.append(uid)
            elif st.get("status") != "accepted" or st.get("spec_hash") != spec_hash(self.plan, self.by[uid], self.state):
                todo.append(uid)
        return todo

    def run(self, selected: set) -> dict:
        self.forced = set(selected) if self.a.force else set()
        done, quota, todo = set(), None, []
        while not quota:
            # recomputed every wave: a re-accepted dependency changes its dependents' spec hash
            todo = [u for u in self.pending(selected) if u not in done]
            for uid in todo:
                if self.ustate(uid).get("status") == "accepted":
                    self.ustate(uid)["status"] = "stale"
            ready = [u for u in todo if all(self.ustate(d).get("status") == "accepted" for d in self.by[u]["depends_on"])]
            if not ready:
                break
            with cf.ThreadPoolExecutor(max_workers=max(1, self.a.jobs)) as ex:
                futs = {ex.submit(self.process, u): u for u in ready}
                for fut in cf.as_completed(futs):
                    uid = futs[fut]
                    try:
                        fut.result()
                    except QuotaExhausted as e:
                        quota = str(e)
                        self.ustate(uid).update(status="pending", note=f"agy quota exhausted: {quota}")
                        log(f"agy quota exhausted ({quota}); stopping after the running units")
            done |= set(ready)
        todo = [u for u in todo if u not in done]
        for uid in todo:  # never became ready
            bad = [d for d in all_deps(self.plan, uid) if self.ustate(d).get("status") != "accepted"]
            if not quota:
                self.ustate(uid).update(status="blocked", note="waiting for " + ", ".join(bad))
        self.save()
        return {"quota": quota}

    # ----------------------------------------------------------------------- integration
    def integrate_check(self) -> dict:
        """All units accepted: assemble accepted code + all tests and run them together, then integration_test_cmd."""
        integ = self.out / "integration"
        srcs = [(self.out / "accepted" / f, f) for u in self.plan["units"] for f in u["deliverables"]]
        tests = []
        for u in self.plan["units"]:
            srcs += [(self.plan["_root"] / f, f) for f in u["context_files"]]
            for f in u["tests"] + u["hidden_tests"]:
                srcs.append((hidden_src(self.plan, f) if f in u["hidden_tests"] else self.plan["_dir"] / f, f))
                tests.append(f)
        build_tree(integ, self.plan, srcs)
        res = {"tests": test_gate("integration:tests", self.plan, {}, sorted(set(tests)), integ)}
        drop_hidden(self.plan, integ)
        if self.plan.get("integration_test_cmd"):
            cmd, _ = test_command(self.plan, {"test_cmd": self.plan["integration_test_cmd"]}, [])
            rc, out = run_cmd(cmd, True, integ, int(self.plan.get("test_timeout", 120)) * 2)
            res["command"] = dict(gates.gate("integration:command", [] if rc == 0 else [f"exited {rc}"]),
                                  rc=rc, output_tail=out[-3000:])
        res["ok"] = all(g["ok"] for g in res.values() if isinstance(g, dict))
        res["at"] = _dt.datetime.now().isoformat(timespec="seconds")
        self.state["integration"] = res
        self.save()
        return res


# --------------------------------------------------------------------------- report
def write_report(plan, out: Path) -> str:
    state = read_json(out / "state.json", {"units": {}})
    lines = [f"# agy-outsource report — {plan['project']}", "",
             f"plan: `{plan['_path']}`  ", f"run dir: `{out}`", "",
             "| unit | title | status | attempts | failed gates (last) | flags |", "|---|---|---|---|---|---|"]
    counts = {}
    for uid in topo_order(plan):
        u = units_by_id(plan)[uid]
        st = state["units"].get(uid, {"status": "pending", "attempts": 0})
        stale = st.get("status") == "accepted" and st.get("spec_hash") != spec_hash(plan, u, state)
        status = "stale" if stale else st.get("status", "pending")
        counts[status] = counts.get(status, 0) + 1
        last = (st.get("history") or [{}])[-1].get("failed_gates", []) if status != "accepted" else []
        lines.append(f"| {uid} | {u['title']} | **{status}** | {st.get('attempts', 0)} | {', '.join(last) or '-'} | "
                     f"{', '.join(st.get('flags', [])) or '-'} |")
    lines += ["", "status: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())), ""]
    integ = state.get("integration")
    if integ:
        lines.append(f"integration ({integ.get('at')}): **{'passed' if integ.get('ok') else 'FAILED'}**")
        for k in ("tests", "command"):
            if k in integ and not integ[k]["ok"]:
                lines += ["```", integ[k].get("output_tail", "")[-1500:], "```"]
        lines.append("")
    attn = [uid for uid in topo_order(plan) if state["units"].get(uid, {}).get("status") in ("needs_attention", "blocked")]
    if attn:
        lines.append("## Needs the lead")
        for uid in attn:
            st = state["units"][uid]
            lines.append(f"- **{uid}** ({st['status']}): {st.get('note') or 'last attempt failed ' + ', '.join(st.get('last_failed', []))}"
                         f" — details in `units/{uid}/attempt_{st.get('attempts', 0)}.json`")
        lines.append("")
    notes = [(uid, st) for uid, st in state["units"].items() if st.get("status") == "accepted"
             and (st.get("assumptions") or st.get("warnings") or st.get("flags"))]
    if notes:
        lines.append("## Accepted with notes (review these)")
        for uid, st in notes:
            for x in st.get("assumptions", []):
                lines.append(f"- {uid} assumption: {x}")
            for x in st.get("warnings", []):
                lines.append(f"- {uid} warning: {x}")
            if "hidden_peek" in st.get("flags", []):
                lines.append(f"- {uid}: agy's tool calls mention the hidden tests — treat its hidden-test pass as unproven")
            if "unconfined" in st.get("flags", []):
                lines.append(f"- {uid}: agy ran without work-dir confinement (--no-confine or an older agy_task.py)")
            if {"hidden_peek_denied", "outside_access_denied"} & set(st.get("flags", [])):
                lines.append(f"- {uid}: agy tried to reach files outside its work dir; every such call was denied "
                             f"(see `units/{uid}/attempt_{st.get('attempts', 0)}.json`, agy.denied)")
    lines += ["", "## Who did what (lead writes tests, contractor writes code, auditor judges tests — never the same model)",
              f"lead: {(plan.get('roles') or {}).get('lead', '(not declared)')}  ", "",
              "| unit | contractor | test audit | triage of the last failure |", "|---|---|---|---|"]
    audits = (read_json(plan["_dir"] / AUDIT, {}) or {}).get("units", {})
    for uid in topo_order(plan):
        st, au = state["units"].get(uid, {}), audits.get(uid, {})
        tri = read_json(out / "units" / uid / "triage.json", None)
        tri_text = "-" if not tri else f"{tri.get('model')}: " + ", ".join(
            f"{f.get('verdict')}" for f in tri.get("failures", [])) + f" (attempt {tri.get('attempt')})"
        lines.append(f"| {uid} | {st.get('contractor_model') or st.get('contractor', '-')} | "
                     f"{au.get('verdict', 'none')} ({au.get('model', '-')}) | {tri_text} |")
    text = "\n".join(lines) + "\n"
    (out / "report.md").write_text(text, encoding="utf-8")
    return text


# --------------------------------------------------------------------------- commands
def out_dir(plan, a) -> Path:
    return Path(a.out).resolve() if getattr(a, "out", None) else plan["_dir"] / ".agy_outsource"


def red_check(plan, out: Path) -> list:
    """Run every unit's tests against interface stubs; tests that pass there do not constrain anything."""
    red = out / "red"
    build_tree(red, plan, [])
    for rel, src in gates.stub_sources(plan["units"]).items():
        (red / rel).parent.mkdir(parents=True, exist_ok=True)
        (red / rel).write_text(src, encoding="utf-8")
    for u in plan["units"]:
        for f in u["context_files"]:
            copy_file(plan["_root"] / f, red / f)
        for f in u["tests"]:
            copy_file(plan["_dir"] / f, red / f)
        for f in u["hidden_tests"]:
            copy_file(hidden_src(plan, f), red / f)
    try:
        return _red_report(plan, red)
    finally:
        drop_hidden(plan, red)


def _red_report(plan, red: Path) -> list:
    report = []
    for u in plan["units"]:
        files = u["tests"] + u["hidden_tests"]
        g = test_gate("red", plan, u, files, red)
        out_tail = g.get("output_tail", "")
        item = {"unit": u["id"], "fails_on_stubs": not g["ok"]}
        m = re.search(r"(cannot import name [^\n]+|ModuleNotFoundError[^\n]+|ImportError[^\n]+|AttributeError: module [^\n]+)", out_tail)
        if m:
            item["import_problem"] = m.group(1).strip()
        elif not g["ok"] and "NotImplementedError" not in out_tail:
            item["other_failure"] = out_tail[-600:]
        report.append(item)
    return report


def cmd_check(a) -> int:
    plan = load_plan(a.plan)
    errors, warnings = validate_plan(plan)
    res = {"ok": not errors, "errors": errors, "warnings": warnings,
           "order": topo_order(plan) if not errors else None}
    if a.red and not errors:
        red = red_check(plan, out_dir(plan, a))
        res["red"] = red
        for r in red:
            if not r["fails_on_stubs"]:
                res["errors"].append(f"unit {r['unit']!r}: its tests PASS against stubs — they do not test the code")
            if r.get("import_problem"):
                res["errors"].append(f"unit {r['unit']!r}: tests import something the interface does not declare: "
                                     f"{r['import_problem']}")
            if r.get("other_failure"):
                res["warnings"].append(f"unit {r['unit']!r}: tests fail on stubs but not with NotImplementedError — "
                                       f"check the tests themselves run: {r['other_failure'][-200:]}")
        res["ok"] = not res["errors"]
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res["ok"] else 5


def cmd_render(a) -> int:
    plan = load_plan(a.plan)
    by = units_by_id(plan)
    if a.unit not in by:
        raise PlanError(f"unknown unit {a.unit!r}; units: {', '.join(by)}")
    sys.stdout.write(render_work_order(plan, by[a.unit], jobs_root() / "<job>" / "work",
                                       allow_all=a.allow_all, confined=not a.no_confine))
    return 0


def cmd_run(a) -> int:
    plan = load_plan(a.plan)
    errors, warnings = validate_plan(plan)
    if errors:
        print(json.dumps({"status": "bad-plan", "errors": errors}, ensure_ascii=False, indent=2))
        return 5
    for w in warnings:
        log("plan warning: " + w)
    roles_ = roles.plan_roles(plan)
    kind, model = parse_contractor(a.contractor)
    if kind == "claude":  # the lead wrote the hidden tests: it must not be the one writing the code
        roles.require_distinct("contractor", model, lead=roles_["lead"])
        roles.find_claude()
    if a.allow_all and not a.no_confine:
        print(json.dumps({"status": "bad-args", "errors": [
            "--allow-all lets agy run programs, and a program can read any file (the sealed hidden tests too): the "
            "work-dir confinement cannot hold. Pass --no-confine with --allow-all to accept that."]}, indent=2))
        return 5
    if os.name == "nt":  # agy must never find the hidden tests in clear while it runs
        sealed = seal_plan(plan)["sealed"]
        if sealed:
            log("sealed hidden tests: " + ", ".join(sealed))
    else:
        log("warning: hidden tests stay in plaintext (sealing needs Windows DPAPI)")
    try:
        agy_task = find_agy_task() if kind == "agy" else None
    except FileNotFoundError as e:
        print(json.dumps({"status": "error", "message": str(e)}))
        return 4
    out = out_dir(plan, a)
    check_jobs_root(plan, out)
    out.mkdir(parents=True, exist_ok=True)
    sweep_hidden(plan, out)  # plaintext copies a crashed or interrupted run left behind
    by = units_by_id(plan)
    selected = set(by)
    if a.units:
        selected = {x.strip() for x in a.units.split(",") if x.strip()}
        unknown = selected - set(by)
        if unknown:
            raise PlanError("unknown units: " + ", ".join(sorted(unknown)))
    runner = Runner(plan, out, a, agy_task, roles_)
    for uid in list(selected):  # pull in dependencies that are not accepted yet
        for d in all_deps(plan, uid):
            if runner.ustate(d).get("status") != "accepted" and d not in selected:
                log(f"including dependency {d} of {uid}")
                selected.add(d)
    unaudited = audit_problems(plan, roles_, topo_order(plan))  # integration runs every unit's tests
    if unaudited:
        print(json.dumps({"status": "not-audited", "errors": unaudited}, ensure_ascii=False, indent=2))
        return 5
    res = runner.run(selected)
    all_ok = all(runner.ustate(u).get("status") == "accepted" for u in by)
    integ = None
    if all_ok and not res["quota"] and not a.no_integration:
        log("all units accepted -> integration tests")
        integ = runner.integrate_check()
    write_report(plan, out)
    summary = {
        "status": "quota" if res["quota"] else ("ok" if all(runner.ustate(u).get("status") == "accepted" for u in selected)
                                                and (integ is None or integ["ok"]) else "incomplete"),
        "quota": res["quota"], "run_dir": str(out), "report": str(out / "report.md"),
        "units": {u: {k: runner.ustate(u).get(k) for k in ("status", "attempts", "note") if runner.ustate(u).get(k) is not None}
                  for u in topo_order(plan)},
        "integration": None if integ is None else {"ok": integ["ok"]},
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return {"ok": 0, "incomplete": 2, "quota": 3}[summary["status"]]


def cmd_status(a) -> int:
    plan = load_plan(a.plan)
    sys.stdout.write(write_report(plan, out_dir(plan, a)))
    return 0


def cmd_integrate(a) -> int:
    plan = load_plan(a.plan)
    out = out_dir(plan, a)
    state = read_json(out / "state.json", {"units": {}})
    dest = Path(a.dest).resolve()
    if dest == Path.home() or dest.parent == dest:
        raise PlanError(f"refusing to integrate into {dest}")
    missing = [u["id"] for u in plan["units"] if state["units"].get(u["id"], {}).get("status") != "accepted"]
    if missing and not a.partial:
        print(json.dumps({"status": "not-ready", "not_accepted": missing}, ensure_ascii=False, indent=2))
        return 2
    if not a.partial and not (state.get("integration") or {}).get("ok"):
        print(json.dumps({"status": "not-ready", "message": "integration tests have not passed; run `run` first"}))
        return 2
    copies = []
    for u in plan["units"]:
        if u["id"] in missing:
            continue
        copies += [(out / "accepted" / f, f) for f in u["deliverables"]]
        if a.with_tests:
            copies += [(plan["_dir"] / f, f) for f in u["tests"]]
            copies += [(hidden_src(plan, f), f) for f in u["hidden_tests"]]
    conflicts = [rel for src, rel in copies
                 if (dest / rel).exists() and gates.sha256_file(dest / rel) != digest(src)]
    if conflicts and not a.overwrite:
        print(json.dumps({"status": "conflict", "message": "files differ in the destination; pass --overwrite",
                          "conflicts": conflicts}, ensure_ascii=False, indent=2))
        return 2
    if not a.dry_run:
        for src, rel in copies:
            copy_file(src, dest / rel)
    print(json.dumps({"status": "dry-run" if a.dry_run else "ok", "dest": str(dest),
                      "files": [rel for _, rel in copies], "overwritten": conflicts, "skipped_units": missing},
                     ensure_ascii=False, indent=2))
    return 0


def cmd_seal(a) -> int:
    res = seal_plan(load_plan(a.plan), a.cmd)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def seal_plan(plan, mode: str = "seal") -> dict:
    """Encrypt the plan's hidden tests into hidden.sealed and delete the plaintext (mode unseal reverses it)."""
    store = sealed_store(plan)
    files = sorted({f for u in plan["units"] for f in u["hidden_tests"]})
    done = []
    for f in files:
        p = plan["_dir"] / f
        if mode == "seal" and p.is_file():
            store[f] = _dpapi(p.read_bytes(), True).hex()
            write_json(plan["_dir"] / SEALED, store)
            if _dpapi(bytes.fromhex(store[f]), False) != p.read_bytes():
                raise PlanError(f"seal round-trip failed for {f}")
            p.unlink()
            done.append(f)
        elif mode == "unseal" and f in store:
            copy_file(_dpapi(bytes.fromhex(store.pop(f)), False), p)
            done.append(f)
    if mode == "unseal":
        if store:
            write_json(plan["_dir"] / SEALED, store)
        else:
            (plan["_dir"] / SEALED).unlink(missing_ok=True)
    for d in {(plan["_dir"] / f).parent for f in files}:  # drop directories the seal emptied
        if d != plan["_dir"] and d.is_dir() and not any(d.iterdir()):
            d.rmdir()
    return {"status": "ok", mode + "ed": done, "sealed_now": sorted(sealed_store(plan))}


def cmd_preflight(a) -> int:
    res = {"python": sys.version.split()[0], "pytest": have_pytest(),
           "test_runner": "pytest" if have_pytest() else f"mini_test.py (stdlib; install pytest for fixtures)"}
    try:
        agy_task = find_agy_task()
    except FileNotFoundError as e:
        res.update(ok=False, error=str(e))
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 4
    res["agy_task"] = str(agy_task)
    cp = subprocess.run([sys.executable, str(agy_task), "preflight"], capture_output=True,
                        env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    try:
        res["agy"] = json.loads(cp.stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        res["agy"] = {"ok": False, "raw": cp.stdout.decode("utf-8", "replace")[-800:]}
    res["ok"] = cp.returncode == 0
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res["ok"] else 4


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="outsource.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("preflight", help="check agy (via agy-delegate) and the test runner")

    c = sub.add_parser("check", help="validate a plan")
    c.add_argument("plan")
    c.add_argument("--red", action="store_true", help="also require every unit's tests to fail against interface stubs")
    c.add_argument("--out")

    r = sub.add_parser("render", help="print the work order of one unit")
    r.add_argument("plan")
    r.add_argument("unit")
    r.add_argument("--out")
    r.add_argument("--allow-all", action="store_true")
    r.add_argument("--no-confine", action="store_true")

    rn = sub.add_parser("run", help="run pending units, then the integration tests")
    rn.add_argument("plan")
    rn.add_argument("--out", help="run directory (default: <plan dir>/.agy_outsource)")
    rn.add_argument("--units", help="comma-separated unit ids (their unaccepted dependencies are added)")
    rn.add_argument("-j", "--jobs", type=int, default=2, help="units run in parallel when independent (default 2)")
    rn.add_argument("--force", action="store_true", help="re-run the selected units even when accepted")
    rn.add_argument("--max-attempts", type=int, help="attempts per unit (default: unit/plan max_attempts or 3)")
    rn.add_argument("--model", default=os.environ.get("AGY_TASK_MODEL"))
    rn.add_argument("--effort", choices=["low", "medium", "high"])
    rn.add_argument("--timeout", type=int, default=20, help="minutes per agy session (default 20)")
    rn.add_argument("--allow-all", action="store_true",
                    help="let agy run shell commands (e.g. the visible tests) in the unit's throwaway work dir; "
                         "needs --no-confine")
    rn.add_argument("--no-confine", action="store_true",
                    help="do not confine agy's tool calls to the unit's work dir (confined by default)")
    rn.add_argument("--no-integration", action="store_true")
    rn.add_argument("--contractor", default="agy",
                    help="who writes the code: agy (default) or claude:<model>, a Claude model other than the lead's "
                         "(runs `claude -p --restricted` in the job dir) for units agy keeps failing")

    au = sub.add_parser("audit", help="an independent Claude model checks every test against the written spec "
                                      "(required before run)")
    au.add_argument("plan")
    au.add_argument("--units", help="comma-separated unit ids (default: all)")
    au.add_argument("--auditor", help="Claude model for the auditor (default: plan roles.auditor or sonnet); "
                                      "must differ from the lead's")
    au.add_argument("--force", action="store_true", help="audit again even if nothing changed")
    au.add_argument("--timeout", type=int, default=15, help="minutes per unit (default 15)")

    tr = sub.add_parser("triage", help="an independent Claude model judges the failing tests of a unit (without "
                                       "seeing the code): code bug, test bug, spec gap")
    tr.add_argument("plan")
    tr.add_argument("unit")
    tr.add_argument("--out")
    tr.add_argument("--auditor", help="Claude model (default: plan roles.auditor); must differ from the lead's and "
                                      "the unit's contractor's")
    tr.add_argument("--timeout", type=int, default=15)

    s = sub.add_parser("status", help="write and print report.md")
    s.add_argument("plan")
    s.add_argument("--out")

    i = sub.add_parser("integrate", help="copy accepted deliverables into a project tree")
    i.add_argument("plan")
    i.add_argument("--dest", required=True)
    i.add_argument("--out")
    i.add_argument("--with-tests", action="store_true", help="also copy the visible and hidden tests")
    i.add_argument("--overwrite", action="store_true", help="replace destination files that differ")
    i.add_argument("--partial", action="store_true", help="copy the accepted units even if others are not")
    i.add_argument("--dry-run", action="store_true")

    for name, text in (("seal", "encrypt the hidden tests into hidden.sealed (DPAPI) and delete the plaintext"),
                       ("unseal", "restore the hidden tests from hidden.sealed (to edit them; seal again after)")):
        sub.add_parser(name, help=text).add_argument("plan")
    return p


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):  # JSON with non-ASCII text must not die on a legacy console code page
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    a = build_parser().parse_args(argv)
    try:
        return {"preflight": cmd_preflight, "check": cmd_check, "render": cmd_render, "run": cmd_run,
                "audit": cmd_audit, "triage": cmd_triage,
                "status": cmd_status, "integrate": cmd_integrate, "seal": cmd_seal, "unseal": cmd_seal}[a.cmd](a)
    except PlanError as e:
        print(json.dumps({"status": "bad-plan", "errors": [str(e)]}, ensure_ascii=False, indent=2))
        return 5
    except roles.ClaudeRunError as e:
        print(json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False, indent=2))
        return 3
    except roles.RoleError as e:
        print(json.dumps({"status": "bad-roles", "errors": [str(e)]}, ensure_ascii=False, indent=2))
        return 5


if __name__ == "__main__":
    sys.exit(main())

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
import concurrent.futures as cf
import datetime as _dt
import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import string
import subprocess
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gates  # noqa: E402

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


def copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


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
    for f in unit["tests"] + unit["hidden_tests"]:
        h.update(gates.sha256_file(plan["_dir"] / f).encode())
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


def render_work_order(plan, unit, workdir, feedback="", allow_all=False) -> str:
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
    runtime = plan.get("runtime") or (f"Python {plan.get('python', '3.9+')}" if plan.get("language", "python") == "python"
                                      else plan.get("language"))
    run_rule = (f"You may run the visible tests with `{test_cmd_display(plan, unit)}`; leave no generated files behind."
                if allow_all else
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


def render_feedback(attempt: int, max_attempts: int, results: list, hidden_level: str = "tail") -> str:
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
    g = gates.gate(name, [] if rc == 0 else [f"test command exited {rc}"])
    g["output_tail"] = out[-4000:]
    g["rc"] = rc
    return g


def build_tree(dst: Path, plan, sources: list) -> None:
    """sources: [(src_path, rel_path)] copied into a fresh dst."""
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    for src, rel in sources:
        copy_file(Path(src), dst / rel)


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
    return {"rc": cp.returncode, "quota": quota, "status": res.get("status"), "agent_status": res.get("agent_status"),
            "conversation_id": res.get("conversation_id"), "elapsed_sec": res.get("elapsed_sec"),
            "agy_error": res.get("agy_error"), "stderr_tail": err[-600:] if cp.returncode not in (0, 2) else ""}


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
    def __init__(self, plan, out: Path, a, agy_task):
        self.plan, self.out, self.a, self.agy_task = plan, out, a, agy_task
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
        work = udir / "work"
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(parents=True)
        frozen_src = self.frozen_sources(unit)
        max_attempts = int(self.a.max_attempts or unit.get("max_attempts") or self.plan.get("max_attempts") or 3)
        st.update(status="running", attempts=0, spec_hash=spec_hash(self.plan, unit, self.state), history=[])
        self.save()
        feedback = ""
        for attempt in range(1, max_attempts + 1):
            for src, rel in frozen_src:  # restore read-only inputs (undoes any tampering of a previous attempt)
                copy_file(src, work / rel)
            (work / gates.MANIFEST).unlink(missing_ok=True)
            frozen = {rel: gates.sha256_file(work / rel) for _, rel in frozen_src}
            before = gates.snapshot(work)
            prompt = render_work_order(self.plan, unit, work, feedback, self.a.allow_all)
            pfile = udir / f"attempt_{attempt}.prompt.md"
            pfile.write_text(prompt, encoding="utf-8")
            log(f"{uid}: attempt {attempt}/{max_attempts} -> agy")
            agy = call_agy(self.agy_task, work, pfile, unit, self.a)
            if agy.get("quota") and not (work / gates.MANIFEST).exists():
                st.update(attempts=attempt - 1)
                raise QuotaExhausted(agy["quota"])
            changes = gates.diff(before, gates.snapshot(work))
            results = gates.static_gates(work, unit, changes, frozen, self.plan)
            if agy.get("rc") not in (0, 2):
                results.insert(0, gates.gate("agy", [f"agy run failed (rc={agy.get('rc')}): "
                                                     f"{agy.get('error') or agy.get('agy_error') or agy.get('stderr_tail')}"]))
            # tests run on a clean copy: deliverables + read-only inputs (+ the sealed hidden tests)
            verify = udir / "verify"
            srcs = [(work / f, f) for f in unit["deliverables"] if (work / f).is_file()] + frozen_src
            build_tree(verify, self.plan, srcs + [(self.plan["_dir"] / f, f) for f in unit["hidden_tests"]])
            results.append(test_gate("tests:visible", self.plan, unit, unit["tests"], verify))
            results.append(test_gate("tests:hidden", self.plan, unit, unit["hidden_tests"], verify))
            for f in unit["hidden_tests"]:  # the sealed tests never stay where agy could look
                (verify / f).unlink(missing_ok=True)
            for g in results:
                if "output_tail" in g:
                    g["output_tail"] = scrub(g["output_tail"], [verify, work, self.plan["_dir"]])
            peek = hidden_peek(self.plan, unit, work, [verify])
            ok = all(g["ok"] for g in results)
            failed = [g["gate"] for g in results if not g["ok"]]
            rec = {"attempt": attempt, "ok": ok, "failed_gates": failed, "changes": changes, "agy": agy,
                   "gates": results, "hidden_peek": peek}
            write_json(udir / f"attempt_{attempt}.json", rec)
            st["attempts"] = attempt
            st["history"].append({"attempt": attempt, "failed_gates": failed})
            log(f"{uid}: attempt {attempt} " + ("ACCEPTED" if ok else "rejected: " + ", ".join(failed)))
            if ok:
                files = {}
                for f in unit["deliverables"]:
                    copy_file(work / f, self.out / "accepted" / f)
                    files[f] = gates.sha256_file(work / f)
                manifest, _ = gates.load_manifest(work)
                st.update(status="accepted", files=files, flags=["hidden_peek"] if peek else [],
                          assumptions=(manifest or {}).get("assumptions", []),
                          warnings=[w for g in results for w in g.get("warnings", [])],
                          accepted_at=_dt.datetime.now().isoformat(timespec="seconds"))
                self.save()
                return "accepted"
            feedback = render_feedback(attempt, max_attempts, results,
                                       unit.get("hidden_feedback", self.plan.get("hidden_feedback", "tail")))
            self.save()
        st.update(status="needs_attention", last_failed=st["history"][-1]["failed_gates"])
        self.save()
        return "needs_attention"

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
                srcs.append((self.plan["_dir"] / f, f))
                tests.append(f)
        build_tree(integ, self.plan, srcs)
        res = {"tests": test_gate("integration:tests", self.plan, {}, sorted(set(tests)), integ)}
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
        for f in u["tests"] + u["hidden_tests"]:
            copy_file(plan["_dir"] / f, red / f)
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
    sys.stdout.write(render_work_order(plan, by[a.unit], out_dir(plan, a) / "units" / a.unit / "work",
                                       allow_all=a.allow_all))
    return 0


def cmd_run(a) -> int:
    plan = load_plan(a.plan)
    errors, warnings = validate_plan(plan)
    if errors:
        print(json.dumps({"status": "bad-plan", "errors": errors}, ensure_ascii=False, indent=2))
        return 5
    for w in warnings:
        log("plan warning: " + w)
    try:
        agy_task = find_agy_task()
    except FileNotFoundError as e:
        print(json.dumps({"status": "error", "message": str(e)}))
        return 4
    out = out_dir(plan, a)
    out.mkdir(parents=True, exist_ok=True)
    by = units_by_id(plan)
    selected = set(by)
    if a.units:
        selected = {x.strip() for x in a.units.split(",") if x.strip()}
        unknown = selected - set(by)
        if unknown:
            raise PlanError("unknown units: " + ", ".join(sorted(unknown)))
    runner = Runner(plan, out, a, agy_task)
    for uid in list(selected):  # pull in dependencies that are not accepted yet
        for d in all_deps(plan, uid):
            if runner.ustate(d).get("status") != "accepted" and d not in selected:
                log(f"including dependency {d} of {uid}")
                selected.add(d)
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
            copies += [(plan["_dir"] / f, f) for f in u["tests"] + u["hidden_tests"]]
    conflicts = [rel for src, rel in copies
                 if (dest / rel).exists() and gates.sha256_file(dest / rel) != gates.sha256_file(src)]
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
                    help="let agy run shell commands (e.g. the visible tests) in the unit's throwaway work dir")
    rn.add_argument("--no-integration", action="store_true")

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
    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    try:
        return {"preflight": cmd_preflight, "check": cmd_check, "render": cmd_render, "run": cmd_run,
                "status": cmd_status, "integrate": cmd_integrate}[a.cmd](a)
    except PlanError as e:
        print(json.dumps({"status": "bad-plan", "errors": [str(e)]}, ensure_ascii=False, indent=2))
        return 5


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""outsource.py - outsource a coding task to the Antigravity CLI (`agy`) one unit-testable unit at a time.

The lead agent (Claude) writes a plan: units with deliverable files, an exact public interface, behaviour
rules, dependencies, visible tests and sealed hidden tests. An auditor on another Claude model must trace every
test to a written rule before any code is written. Each unit then becomes a work order for one agy session in a
job dir outside every repository, confined to it by agy-delegate's hook; the delivery is accepted only by
deterministic gates: integrity, agy, manifest, scope, files, syntax, interface, imports, forbid, tamper,
tests:visible, tests:hidden (a test gate needs the runner's result line, exit code 0 is not enough). Failed gates
go back to agy as feedback; units that exhaust their attempts come back to the lead, and a referee on a third model
can judge their failing tests (triage).

  preflight                  agy (via agy-delegate, at least as new as this script) + test runner
  check PLAN [--red]         validate the plan; --red: the tests must FAIL against interface stubs
  audit PLAN [--units ...]   the auditor checks every test against the written spec (required before run)
  render PLAN UNIT           print the work order agy would get (attempt 1, without inlined file texts)
  run PLAN [options]         seal the hidden tests (Windows), run pending units in dependency order, then the
                             integration tests
  triage PLAN UNIT           the referee judges the last failing attempt: code_bug / test_bug / spec_gap / unclear
  status PLAN                print report.md
  integrate PLAN --dest DIR  copy accepted deliverables (optionally the tests) into a project tree
  seal PLAN / unseal PLAN    encrypt the hidden tests into hidden.sealed (DPAPI) / restore them to edit

Exit codes: 0 all selected units accepted (and integration passed) / audit passed; 2 some unit not accepted,
integration failed, integrate conflict or not ready, audit failed; 3 agy quota or runtime error, also a failed
`claude` run; 4 preflight failure, also `run` with an outdated agy-delegate; 5 bad plan or arguments, a missing or outdated audit (not-audited), roles of
one model family (bad-roles). Python 3.9+, stdlib only.
"""
from __future__ import annotations

import argparse
import ast
import concurrent.futures as cf
import contextlib
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
from pathlib import Path, PureWindowsPath

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gates  # noqa: E402
import roles  # noqa: E402

SKILL_DIR = Path(__file__).resolve().parent.parent
PROMPT = SKILL_DIR / "prompts" / "work_order.md"
MINI_TEST = Path(__file__).resolve().parent / "mini_test.py"
QUOTA_RE = re.compile(r"RESOURCE_EXHAUSTED|quota reached|\(code 429\)", re.I)
# the claude CLI's own limit messages ("Claude AI usage limit reached|<epoch>", "API Error: 429 {...
# rate_limit_error ...}"), matched in its error fields only (call_claude_contractor)
CLAUDE_QUOTA_RE = re.compile(r"Claude AI usage limit reached|rate_limit_error|API Error:\s*429\b", re.I)
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
# deletes the plaintext. They then exist in clear only inside verify/red/integration dirs while no contractor session
# of the run is live (SessionGate: with -j > 1 a unit's hidden-test gate waits until every running session is over,
# and no session starts until the plaintext is gone), and are deleted from there right after the test gate.
SEALED = "hidden.sealed"


class SessionGate:
    """Contractor sessions and plaintext hidden tests never overlap. A session holds the gate shared; the phase that
    decrypts hidden tests, runs them and sweeps them holds it exclusively. A waiting plaintext phase goes first, so a
    stream of new sessions cannot starve it. Nobody waits for the exclusive side while holding the shared one."""

    def __init__(self):
        self._c = threading.Condition()
        self._sessions = self._waiting = 0
        self._plaintext = False

    @contextlib.contextmanager
    def session(self):
        with self._c:
            while self._plaintext or self._waiting:
                self._c.wait()
            self._sessions += 1
        try:
            yield
        finally:
            with self._c:
                self._sessions -= 1
                self._c.notify_all()

    @contextlib.contextmanager
    def plaintext(self):
        with self._c:
            self._waiting += 1
            while self._plaintext or self._sessions:
                self._c.wait()
            self._waiting -= 1
            self._plaintext = True
        try:
            yield
        finally:
            with self._c:
                self._plaintext = False
                self._c.notify_all()


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
    """The hidden test's bytes: the plaintext file if it is still there, else decrypted from hidden.sealed (None if
    neither). Read at once: a seal running between a check and a later read must not break the reader."""
    try:
        return (plan["_dir"] / rel).read_bytes()
    except FileNotFoundError:
        pass
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


def full_snapshot(root: Path) -> dict:
    """{rel: sha256} of EVERY file under root, caches and agy-delegate's records included (gates.snapshot leaves
    those out); a linked directory is recorded by its target, not walked."""
    snap, root = {}, Path(root)
    for dp, dns, fns in os.walk(root):
        for name in dns + fns:
            p = Path(dp) / name
            rel = p.relative_to(root).as_posix()
            try:
                if p.is_symlink():
                    snap[rel] = "link:" + os.readlink(p)
                elif name in fns:
                    snap[rel] = gates.sha256_file(p)
            except OSError:
                snap[rel] = "unreadable"
    return snap


def diff_names(a: dict, b: dict) -> list:
    return sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))


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
    """A plain relative path that stays inside whatever tree it is joined to. Rooted forms are refused as well: on
    Windows Path('/pkg/a.py').is_absolute() is False, yet base / '/pkg/a.py' drops the base and lands on the drive
    root. Components made only of dots or spaces (`...`, `. .`) are traversal look-alikes, never a real name."""
    if not p or p == gates.MANIFEST or re.match(r"^([A-Za-z]:|[\\/~])", p):
        return False
    pp, pw = Path(p), PureWindowsPath(p)
    if pp.is_absolute() or pw.drive or pw.root:
        return False
    return not any(part == ".." or not part.strip(" .") for part in re.split(r"[\\/]", p) if part not in ("", "."))


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
    try:  # what `audit` and `run` would refuse later, reported before the tests are written against the plan
        if not isinstance(plan.get("roles") or {}, dict):
            raise roles.RoleError('"roles" must be an object such as {"lead": "opus", "auditor": "sonnet"}')
        roles_ = roles.plan_roles(plan)
        if roles.family(roles_["auditor"]) == "gemini":
            raise roles.RoleError("roles.auditor must be a Claude model: agy must never see hidden tests")
        roles.require_distinct("auditor", roles_["auditor"], lead=roles_["lead"])
    except roles.RoleError as e:
        errors.append(f"roles: {e}")
    lang = plan.get("language", "python")
    if plan.get("effort") not in (None,) + EFFORT_LADDER:  # checked once: every unit may override it
        errors.append(f'plan: effort must be one of {", ".join(EFFORT_LADDER)}')
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
        hidden_all = {f: x.get("id") for x in plan["units"] if isinstance(x, dict) for f in x["hidden_tests"]}
        hidden_names = {Path(h).name for h in hidden_all}
        for f in u["tests"] + u["context_files"]:  # anything listed here is copied into this unit's work dir
            if f in hidden_all or Path(f).name in hidden_names:
                errors.append(f"{where}: {f} is a hidden test (of {hidden_all.get(f, 'some unit')}) and would be "
                              f"copied into this unit's work dir")
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
        if u.get("effort") not in (None,) + EFFORT_LADDER:
            errors.append(f"{where}: effort must be one of {', '.join(EFFORT_LADDER)}")
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


# Bump whenever the gates get stricter (or acceptance otherwise changes in a way the plan does not show), so units
# accepted under the older rules are re-validated by the next run.
# 3: nonce'd mini_test result line + test count, process-exit/getattr tamper checks
# 4: frame/traceback/__dict__/attrgetter tamper checks, exit names on any base, pytest nonce, collected-test count
ACCEPTANCE_VERSION = "4"
# unit keys that change how the contractor is driven, not what an accepted delivery must satisfy
EXECUTION_KEYS = ("effort", "max_attempts")


def spec_hash(plan, unit, state) -> str:
    """Changes when the unit, the plan-wide rules, its test/context files, its dependencies' accepted code or
    context files, or the acceptance rules change."""
    h = hashlib.sha256(f"acceptance {ACCEPTANCE_VERSION}\n".encode())
    keep = {k: v for k, v in unit.items() if not k.startswith("_") and k not in EXECUTION_KEYS}
    glob_ = {k: plan.get(k) for k in ("project", "context", "language", "runtime", "python", "imports",
                                      "forbid_patterns", "test_cmd", "conventions", "hidden_feedback", "tamper_allow")}
    h.update(json.dumps([keep, glob_], sort_keys=True, ensure_ascii=False).encode("utf-8"))
    for f in unit["tests"]:
        h.update(gates.sha256_file(plan["_dir"] / f).encode())
    for f in unit["hidden_tests"]:  # hash the plaintext, so sealing does not make accepted units stale
        src = hidden_src(plan, f)
        h.update((hashlib.sha256(src).hexdigest() if src is not None else "missing").encode())
    for f in unit["context_files"]:
        h.update(gates.sha256_file(plan["_root"] / f).encode())
    by = units_by_id(plan)
    for d in all_deps(plan, unit["id"]):
        h.update(json.dumps(state["units"].get(d, {}).get("files", {}), sort_keys=True).encode())
        for f in by[d]["context_files"]:  # also in this unit's work and verify dirs (unit_inputs: dep_context)
            if f not in unit["context_files"]:
                h.update(gates.sha256_file(plan["_root"] / f).encode())
    # not the work-order template: its wording changes how the contractor is told, not what an accepted unit must
    # satisfy (the tamper rule comes from gates.py, the checks from the gates), and hashing it would re-send every
    # accepted unit to agy (~140k tokens each) after any prompt edit. Runner.evaluator still fingerprints it for
    # the length of a run.
    return h.hexdigest()[:16]


def is_current(plan, state, uid) -> bool:
    """Accepted under the CURRENT spec: the one notion of 'accepted' that scheduling, run's verdict, the report and
    `integrate` share. A unit accepted before its spec, tests, dependencies' code or the acceptance rules changed is
    stale, never a dependency another unit can be built against."""
    st = state["units"].get(uid) or {}
    return st.get("status") == "accepted" and st.get("spec_hash") == spec_hash(plan, units_by_id(plan)[uid], state)


def integration_fingerprint(plan, state) -> dict:
    """What an integration result proves, stored with it and compared by `integrate`."""
    return {"spec_hashes": {u["id"]: spec_hash(plan, u, state) for u in plan["units"]},
            "files": {u["id"]: (state["units"].get(u["id"]) or {}).get("files") for u in plan["units"]},
            "integration_cmd": plan.get("integration_test_cmd")}


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


def project_modules(plan, unit) -> set:
    """Top-level importable names the unit's verify tree holds: its deliverables and its read-only inputs. The
    imports gate allows these, not whatever lies in the job dir (a leftover yaml.py from an earlier attempt is not
    in the verify tree, so `import yaml` there would load an installed third-party package)."""
    names = set()
    for rel in list(unit["deliverables"]) + list(unit_inputs(plan, unit)):
        top = rel.split("/")[0]
        names.add(top[:-3] if "/" not in rel and top.endswith(".py") else top)
    return names


INLINED_KINDS = ("dep", "context", "test")


def unit_inputs(plan, unit) -> dict:
    """rel -> kind of every read-only file in the unit's work dir; the single source of truth for what is copied in
    (Runner.frozen_sources), listed (fmt_listing) and inlined (fmt_inline). dep / indirect: a direct / transitive
    dependency's deliverable; dep_context: a dependency's context file; context; test (visible). Hidden tests are
    never inputs. Only INLINED_KINDS are put in full into the work order: an indirect file's interface is already
    listed under "Already available", and its text would cost tokens on every model call of the session."""
    by, out = units_by_id(plan), {}
    for d in all_deps(plan, unit["id"]):
        out.update({f: "dep_context" for f in by[d]["context_files"]})
        out.update({f: "dep" if d in unit["depends_on"] else "indirect" for f in by[d]["deliverables"]})
    out.update({f: "context" for f in unit["context_files"]})
    out.update({f: "test" for f in unit["tests"]})
    return out


def fmt_listing(plan, unit, present=None) -> str:
    """The work dir as the contractor finds it, so it does not spend model calls probing for files.
    present: the work dir's files (gates.snapshot keys); None = as before the first attempt."""
    inputs = unit_inputs(plan, unit)
    frozen = set(inputs)
    present = set(frozen) if present is None else set(present) - {gates.MANIFEST}
    lines = [f"- `{f}` (read-only" + ("; an indirect dependency: use the interface listed above)"
                                      if inputs[f] == "indirect" else ")") for f in sorted(present & frozen)]
    lines += [f"- `{f}` (yours, from the previous attempt: fix it in place)" for f in unit["deliverables"] if f in present]
    lines += [f"- `{f}` (left over from a previous attempt, not part of the delivery: ignore it)"
              for f in sorted(present - frozen - set(unit["deliverables"]))]
    lines += [f"- `{f}` does not exist yet: you create it" for f in unit["deliverables"] if f not in present]
    return "\n".join(lines)


INLINE_CAP = 24_000  # characters of file text put into one work order
# agy-delegate passes the whole work order on agy's command line, and Windows' CreateProcess takes at most 32,767
# characters, after list2cmdline escaped every '"' (and the backslashes before one). The prompt may use PROMPT_BUDGET
# of them; the rest of agy's argv (binary, flags, work dir) gets what is left. Inlined text only fills the room the
# rest of the order (spec, listing, feedback) leaves under it.
PROMPT_BUDGET = 30_000
FEEDBACK_CUT = "\n... (the rest of the feedback was cut to fit agy's command line)\n"


def cmdline_cost(text: str) -> int:
    """Upper bound of the characters text takes on a Windows command line."""
    return len(text) + text.count('"') + text.count("\\") + 2


def _inlinable(workdir: Path, rel: str):
    """workdir/rel if it is a plain file inside workdir (the contractor can write the work dir: a symlink or a hard
    link there could point at a file it must not see, e.g. a plaintext hidden test, and pull it into the prompt)."""
    p = Path(workdir) / rel
    try:
        if p.is_symlink() or not p.is_file() or os.stat(p).st_nlink > 1:
            return None
        root = Path(workdir).resolve()
        return p if root in p.resolve().parents else None
    except OSError:
        return None


INLINE_HEADER = "\n## Full text of the existing files — do not open them, read them here\n"


def fenced(text: str, lang: str = "") -> str:
    """text in a Markdown code block whose fence is longer than any backtick run inside it, so a ``` line of the
    file's own (a fenced example in a docstring or a Markdown file) cannot end the block early."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{lang}\n{text}\n{fence}"


def fmt_inline(plan, unit, workdir: Path, present=None, inlined=None, own=True, room=None) -> str:
    """The text of the unit's direct inputs (direct dependencies' deliverables, context files, visible tests) and,
    if own, of the previous attempt's deliverables, so the contractor does not open them: each opened file is one
    more model call over the whole context. Never a hidden test (never in the work dir), an indirect dependency, a
    dependency's context file or a leftover. Files that do not fit INLINE_CAP, or the command-line room left by the
    rest of the order (room, in cmdline_cost units), stay in the listing only (a later, smaller file may still fit).
    Read from the work dir right after the read-only inputs were restored, so the text is byte for byte what the
    integrity gate checks. inlined: a list that receives the READ-ONLY paths put in full (the confinement hook denies
    opening those); the contractor's own files are inlined but never listed there."""
    inputs = unit_inputs(plan, unit)
    wanted = {f for f, k in inputs.items() if k in INLINED_KINDS} | (set(unit["deliverables"]) if own else set())
    rels = sorted(set(present) & wanted) if present is not None else []
    out, used = [], 0
    room = (PROMPT_BUDGET if room is None else room) - cmdline_cost(INLINE_HEADER)
    for rel in rels:
        p = _inlinable(workdir, rel)
        if p is None:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        lang = "python" if rel.endswith(".py") else ""
        block = f"### `{rel}`\n{fenced(text.rstrip(), lang)}\n\n"
        if used + len(text) > INLINE_CAP or cmdline_cost(block) > room:
            continue
        used += len(text)
        room -= cmdline_cost(block)
        if inlined is not None and rel in inputs:
            inlined.append(rel)
        out.append(block)
    if not out:
        return ""
    return INLINE_HEADER + "".join(out).rstrip("\n") + "\n"


def test_cmd_display(plan, unit) -> str:
    cmd = unit.get("test_cmd") or plan.get("test_cmd") or "auto"
    files = " ".join(unit["tests"])
    if cmd == "auto":
        return f"python -m pytest -q {files}" if have_pytest() else f"python -m unittest {files}"
    return cmd.replace("{python}", "python").replace("{tests}", files)


def render_work_order(plan, unit, workdir, feedback="", allow_all=False, confined=True, present=None,
                      inlined=None, inline_own=True) -> str:
    """present: the work dir's files right now (None: as before the first attempt, without inlined text, which is
    what `render` shows). inlined: receives the read-only paths whose full text is in the order. inline_own: also
    inline the contractor's own files from the previous attempt (not for a Claude contractor, whose Edit tool must
    Read a file before changing it)."""
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
    runtime = plan.get("runtime") or (f"Python {plan.get('python', '3.9+')}" if plan.get("language", "python") == "python"
                                      else plan.get("language"))
    run_rule = (f"You may run the visible tests with `{test_cmd_display(plan, unit)}`; leave no generated files behind."
                if allow_all else
                "Only files inside your work directory are reachable: any tool call on a path outside it is denied, "
                "and shell commands are disabled. Check your code against the tests by reading them."
                if confined else
                "Shell commands may be denied in this session: check your code against the tests by reading them.")
    tpl = string.Template(PROMPT.read_text(encoding="utf-8"))
    listing = fmt_listing(plan, unit, present)

    def fill(inline: str, feedback: str = feedback) -> str:
        return tpl.safe_substitute(
            unit_id=unit["id"], title=unit["title"], project=plan["project"],
            context=plan.get("context") or "(none)", goal=unit["goal"],
            deliverables=bullets([f"`{f}`" for f in unit["deliverables"]]),
            interface=fmt_interface(unit), spec=bullets(unit["spec"]), examples=fmt_examples(unit),
            constraints=bullets(constraints), available=fmt_available(plan, unit),
            visible_tests=", ".join(f"`{f}`" for f in unit["tests"]) or "(none; only hidden tests)",
            run_rule=run_rule, runtime=runtime, imports_rule=imports_rule, listing=listing + inline,
            files_json=", ".join(json.dumps(f) for f in unit["deliverables"]),
            workdir=str(workdir), feedback=feedback).rstrip() + "\n"

    # the order must fit agy's command line (see PROMPT_BUDGET): feedback is cut from its end if it does not fit
    # (its parts are capped, but a test tail full of quotes doubles on the command line), and inlined text only
    # gets the room the rest leaves
    if feedback:
        allowed = PROMPT_BUDGET - cmdline_cost(fill("", ""))
        while feedback and cmdline_cost(feedback) > allowed:
            excess = cmdline_cost(feedback) - allowed + cmdline_cost(FEEDBACK_CUT)
            feedback = feedback[: max(0, len(feedback) - excess)].rstrip() + FEEDBACK_CUT if excess < len(feedback) else ""
    room = PROMPT_BUDGET - cmdline_cost(fill("", feedback))
    return fill(fmt_inline(plan, unit, Path(workdir), present, inlined, own=inline_own, room=room), feedback)


HIDDEN_TOKEN = "<hidden test>"
SUMMARY_LINES, LINE_CAP, PROBLEMS_PER_GATE = 30, 300, 20  # feedback caps: the order must fit a command line


def neutral_hidden(text: str, hidden=()) -> str:
    """Replace every hidden test's path, file name and module name (pytest's 'FAILED hidden/test_x_hidden.py::...',
    unittest's '(test_x_hidden.Class.test)') with a neutral token: the test function names stay, the file never
    reaches agy, so a search for it in agy's next session neither finds anything nor sets hidden_peek."""
    needles = set()
    for rel in hidden:
        rel = str(rel).replace("\\", "/")
        name = rel.rsplit("/", 1)[-1]
        needles |= {rel, rel.replace("/", "\\"), name, module_of(rel), name.rsplit(".", 1)[0]}
    for n in sorted((n for n in needles if n), key=len, reverse=True):
        text = re.sub(re.escape(n), HIDDEN_TOKEN, text, flags=re.I)
    return text


def hidden_summary(tail: str, level: str, hidden=()) -> str:
    """level: tail (output tail), summary (failing test names and assertion lines only), none. hidden: the plan's
    hidden test paths, replaced by a neutral token at every level."""
    if level == "none":
        return "(details withheld)"
    if level == "summary":
        keep = [ln[:LINE_CAP] for ln in neutral_hidden(tail, hidden).splitlines()
                if re.match(r"\s*(FAIL|ERROR|FAILED)\b|\s*\w*(Error|Exception)\b[:\s]|\s*E\s{2,}", ln)]
        return "\n".join(keep[-SUMMARY_LINES:]) or "(no assertion lines)"
    return neutral_hidden(tail[-1500:], hidden)


QUOTE_MIN = 12  # a hidden-test line this long (stripped) is specific enough to count as quoting the test
QUOTE_DROPPED = "(a line quoting a hidden test was removed)"


def hidden_quote_needles(hidden_texts, known_texts=()) -> set:
    """Lines of the hidden tests' sources that feedback must never carry: every stripped line of at least QUOTE_MIN
    characters that is not also in a file the contractor already has (a visible test, say `import unittest`)."""
    known = {ln.strip() for t in known_texts for ln in t.splitlines()}
    return {s for t in hidden_texts for s in (ln.strip() for ln in t.splitlines())
            if len(s) >= QUOTE_MIN and s not in known}


def drop_quoted(text: str, needles) -> str:
    """text without the lines that contain a hidden-test line (the code under test runs as the user and prints into
    the output that goes back to agy: whatever it read must not get through)."""
    if not needles or not text:
        return text
    out, dropped = [], False
    for ln in text.splitlines():
        if any(n in ln for n in needles):
            if not dropped:
                out.append(QUOTE_DROPPED)
            dropped = True
            continue
        dropped = False
        out.append(ln)
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def render_feedback(attempt: int, max_attempts: int, results: list, hidden_level: str = "summary",
                    hidden=(), quoted=()) -> str:
    """quoted: hidden_quote_needles of every hidden test; any feedback line containing one is dropped."""
    out = [f"\n## Previous attempt was REJECTED (attempt {attempt} of {max_attempts})",
           "Fix every problem below. The deliverables from the previous attempt are still in the directory; "
           "keep what works, change what is listed. Then rewrite DELIVERY.json."]
    for g in results:
        if g["ok"] or g["gate"].startswith("tests:"):
            continue
        probs = g["problems"]
        out += [f"- [{g['gate']}] {p[:LINE_CAP]}" for p in probs[:PROBLEMS_PER_GATE]]
        if len(probs) > PROBLEMS_PER_GATE:
            out.append(f"- [{g['gate']}] ... and {len(probs) - PROBLEMS_PER_GATE} more of the same kind")
    for g in results:
        if g["gate"] == "tests:visible" and not g["ok"]:
            out += ["", "### Visible tests failed — output tail", "```", g.get("output_tail", "")[-3000:], "```"]
        if g["gate"] == "tests:hidden" and not g["ok"]:
            out += ["", "### Hidden acceptance tests failed — their source is sealed; this is all you get",
                    "```", hidden_summary(g.get("output_tail", ""), hidden_level, hidden), "```",
                    "Re-read the behaviour rules: hidden tests only check rules that are written in this work order."]
    return drop_quoted("\n".join(out) + "\n", quoted)


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
        if src is None:
            raise PlanError(f"hidden test {f} of unit {unit['id']!r} is neither in the plan folder nor in {SEALED}")
        # newlines as read_text gives them (the visible tests above), so audit hashes do not change with the source
        out.append((f, "hidden", src.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")))
    return out


_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)


def _scope(body) -> dict:
    """name -> the def/class that the name is bound to after the body ran (a name defined twice: the last one)."""
    out = {}
    for n in body:
        if isinstance(n, _DEFS + (ast.ClassDef,)):
            out.pop(n.name, None)
            out[n.name] = n
    return out


def test_names(rel: str, text: str) -> list:
    """Every test function a test file defines, whoever collects it: `function` or `Class.method` (each name once:
    a redefinition replaces the earlier one); non-Python files count as one test. What the auditor judges."""
    if not rel.endswith(".py"):
        return ["(file)"]
    names = []
    for name, n in _scope(ast.parse(text).body).items():
        if isinstance(n, _DEFS) and name.startswith("test"):
            names.append(name)
        elif isinstance(n, ast.ClassDef):
            names += [f"{name}.{m}" for m, d in _scope(n.body).items() if isinstance(d, _DEFS) and m.startswith("test")]
    return names


def test_defs(text: str) -> int:
    """How many `test*` defs a test file holds at module and class level, every redefinition counted."""
    tree = ast.parse(text)
    return sum(1 for n in tree.body if isinstance(n, _DEFS) and n.name.startswith("test")) + sum(
        1 for c in tree.body if isinstance(c, ast.ClassDef)
        for m in c.body if isinstance(m, _DEFS) and m.name.startswith("test"))


def collected_tests(rel: str, text: str, runner: str) -> list:
    """The tests a runner collects from a file (one id per test; parametrized pytest tests count once):
      mini_test  unittest.TestCase classes, and top-level `test_*` functions
      unittest   unittest.TestCase classes only (also the lower bound used for a custom test_cmd)
      pytest     TestCase classes, top-level `test*` functions and `Test*` classes without __init__
    A class's test methods include those inherited from classes of the same file. A TestCase is recognised by a base
    spelled `TestCase` / `x.TestCase` / `...TestCase`, directly or through a class of the same file."""
    if not rel.endswith(".py"):
        return ["(file)"]
    top = _scope(ast.parse(text).body)
    classes = {k: v for k, v in top.items() if isinstance(v, ast.ClassDef)}

    def bases(c):
        return [ast.unparse(b) for b in c.bases]

    def is_case(c, seen=()):
        return any(re.search(r"(^|\.)\w*TestCase$", b) or (b in classes and b not in seen and
                                                         is_case(classes[b], seen + (c.name,))) for b in bases(c))

    def methods(c, seen=()):
        out = {}
        for b in reversed(bases(c)):
            if b in classes and b not in seen:
                out.update(methods(classes[b], seen + (c.name,)))
        out.update((m, 1) for m, d in _scope(c.body).items() if isinstance(d, _DEFS) and m.startswith("test"))
        return out

    names = []
    for name, n in top.items():
        if isinstance(n, _DEFS):
            if (runner == "mini_test" and name.startswith("test_")) or (runner == "pytest" and name.startswith("test")):
                names.append(name)
        elif is_case(n) or (runner == "pytest" and name.startswith("Test") and "__init__" not in _scope(n.body)):
            names += [f"{name}.{m}" for m in methods(n)]
    return names


def collection_problems(plan) -> list:
    """Test files whose test functions the runner would not all collect: a name defined twice in one scope (the
    last definition wins), a top-level function the runner skips (mini_test runs `test_*` functions only), a class
    that is no unittest.TestCase (pytest: and not a `Test*` class). Each would make every attempt of the unit fail the
    test count, so it is a plan error. Skipped for a custom test_cmd (its runner is not known)."""
    errors = []
    for u in plan["units"]:
        runner = runner_kind(plan, u)
        if runner == "unittest":
            continue
        for rel, kind, text in test_sources(plan, u):
            if not rel.endswith(".py"):
                continue
            try:
                defs, got = test_defs(text), len(collected_tests(rel, text, runner))
            except (SyntaxError, ValueError) as e:
                errors.append(f"unit {u['id']!r}: {kind} test {rel} does not parse: {e}")
                continue
            if got < defs:
                errors.append(f"unit {u['id']!r}: {kind} test {rel} defines {defs} test functions but {runner} "
                              f"collects {got}: a test name defined twice (the last one wins), a top-level function "
                              f"the runner skips ({'`test*`' if runner == 'pytest' else '`test_*`'} ones run), or a "
                              "class that is not a unittest.TestCase" + (" or a `Test*` class" if runner == "pytest"
                                                                       else "") + "; rename or fix the tests")
    return errors


def fmt_items(items) -> str:
    return "\n".join(f"- **{i}** {t}" for i, t in items) or "(no written items)"


def fmt_sources(sources) -> str:
    lang = lambda rel: "python" if rel.endswith(".py") else ""  # noqa: E731
    return "\n\n".join(f"### `{rel}` ({kind})\n{fenced(text.rstrip(), lang(rel))}" for rel, kind, text in sources)


def audit_hash(plan, unit) -> str:
    """Changes when anything the auditor judged changes: the unit's spec, its dependencies' spec, its tests.
    Execution knobs (effort, max_attempts, test_timeout) are not judged by the auditor: pinning a unit's effort
    must not void its audit."""
    keep = {k: v for k, v in unit.items() if not k.startswith("_") and k not in EXECUTION_KEYS + ("test_timeout",)}
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
    uids = [u.strip() for u in a.units.split(",") if u.strip()] if a.units else topo_order(plan)
    unknown = sorted(set(uids) - set(by))
    if unknown:
        raise PlanError("unknown units: " + ", ".join(unknown) + "; units: " + ", ".join(by))
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
    # who wrote the failing attempt, from that attempt's own record (unit state is overwritten by later runs)
    kind = rec.get("contractor") or st.get("contractor") or "agy"
    contractor = "agy" if kind == "agy" else ((rec.get("agy") or {}).get("model") or kind.split(":", 1)[1])
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


def call_claude_contractor(contractor: str, work: Path, prompt_file: Path, a, roles_: dict, procs=None) -> dict:
    """The fallback contractor: `claude -p --restricted` in the job dir, file tools only, on a model that is not
    the lead's. Returns the same shape as call_agy; past --timeout its whole process tree is killed and the
    attempt is a timeout like agy's (no further attempt in that job dir)."""
    _, model = parse_contractor(contractor)

    def runner(cmd, input_bytes, cwd, env, timeout):
        return run_tree(cmd, subprocess.PIPE, env, timeout, cwd=cwd, input=input_bytes, procs=procs)
    res = roles.run_claude(prompt_file.read_text(encoding="utf-8"), model=model, cwd=work, tools=roles.CONTRACTOR_TOOLS,
                           permission_mode="acceptEdits", max_turns=80, timeout=a.timeout * 60 + 120, runner=runner)
    (prompt_file.with_suffix("").with_suffix(".claude.json")).write_text(
        json.dumps({k: v for k, v in res.items() if k != "structured"}, ensure_ascii=False, indent=2), encoding="utf-8")
    if res.get("timed_out"):
        return {"rc": None, "error": "timeout", "confined": True, "denied_count": 0, "denied": []}
    err = None
    if res.get("rc") != 0 or res.get("is_error"):
        err = f"{res.get('error') or res.get('subtype')}: {(res.get('result') or '')[:300]} {res.get('stderr_tail', '')[-200:]}"
    elif roles.family(res.get("model")) != roles.family(model):
        err = f"asked for {model} but ran on {res.get('models')}"
    elif roles.family(res.get("model")) == roles.family(roles_.get("lead")):
        err = f"ran on the lead's model family ({res.get('model')})"
    quota = None
    if err:
        # only the CLI's own error fields, never the model's reply text (a unit about rate limiting may say "rate
        # limit" in it); the reply counts only when it IS the CLI's limit message (it is put there on is_error)
        cli = " ".join(str(res.get(k) or "") for k in ("error", "subtype", "stderr_tail"))
        reply = (res.get("result") or "").strip() if res.get("is_error") else ""
        m = CLAUDE_QUOTA_RE.search(cli) or CLAUDE_QUOTA_RE.match(reply)
        if m:
            quota = (reply if CLAUDE_QUOTA_RE.match(reply) else cli.strip())[:200]
    denied = [{"tool": d.get("tool_name"), "reason": json.dumps(d.get("tool_input"), ensure_ascii=False)[:200]}
              for d in res.get("denials", [])]
    return {"rc": 3 if err else 0, "error": err, "quota": quota, "status": res.get("subtype"), "model": res.get("model"),
            "confined": True, "denied_count": len(denied), "denied": denied[:10], "cost_usd": res.get("cost_usd")}


# --------------------------------------------------------------------------- tests
def have_pytest() -> bool:
    return importlib.util.find_spec("pytest") is not None


NONCE_FLAG = "--nonce-stdin"
# pytest plugin (-p) that prints the run's last line with the nonce, like mini_test.py's NONCE_FLAG
PYTEST_PLUGIN = "agy_pytest_nonce"
PYTEST_PLUGIN_DIR = Path(__file__).resolve().parent / "pytest_nonce"


def test_command(plan, unit, files: list):
    """(argv or shell string, shell flag). Both auto runners take a per-run nonce on stdin: mini_test.py with
    NONCE_FLAG, pytest through PYTEST_PLUGIN."""
    cmd = unit.get("test_cmd") or plan.get("test_cmd") or "auto"
    if cmd == "auto":
        if have_pytest():
            return [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", PYTEST_PLUGIN, *files], False
        return [sys.executable, str(MINI_TEST), NONCE_FLAG, *files], False
    q = (lambda s: subprocess.list2cmdline([s])) if os.name == "nt" else shlex.quote
    return cmd.replace("{python}", q(sys.executable)).replace("{tests}", " ".join(q(f) for f in files)), True


def runner_kind(plan, unit) -> str:
    """Which runner collects the tests (collected_tests): the auto runner in use, else 'unittest' as a lower bound
    for a custom test_cmd (every Python runner collects unittest.TestCase classes)."""
    if (unit.get("test_cmd") or plan.get("test_cmd") or "auto") != "auto":
        return "unittest"
    return "pytest" if have_pytest() else "mini_test"


def run_cmd_split(cmd, shell: bool, cwd: Path, timeout: int, stdin: str = "") -> tuple:
    """(rc, stdout, stderr); rc 124 on a timeout."""
    path = [str(cwd)] + ([os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else [])
    extra = {}
    if not shell and PYTEST_PLUGIN in cmd:
        path.insert(0, str(PYTEST_PLUGIN_DIR))
        extra = {"AGY_PYTEST_NONCE_STDIN": "1"} if stdin else {}
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8", PYTHONPATH=os.pathsep.join(path),
               **extra)
    try:
        cp = subprocess.run(cmd, shell=shell, cwd=str(cwd), input=stdin, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=timeout, env=env)
        return cp.returncode, cp.stdout, cp.stderr
    except subprocess.TimeoutExpired as e:
        return 124, f"timed out after {timeout}s\n" + str(e.output or "")[-2000:], ""


def run_cmd(cmd, shell: bool, cwd: Path, timeout: int) -> tuple:
    rc, out, err = run_cmd_split(cmd, shell, cwd, timeout)
    return rc, out + err


def expected_tests(cwd: Path, files: list, runner: str = "mini_test"):
    """How many tests the runner collects from the given files (collected_tests: duplicate names once, only what
    this runner collects); None when a file cannot be parsed (its collection error fails the run anyway) or is not
    Python."""
    n = 0
    for f in files:
        if not f.endswith(".py"):
            return None
        try:
            n += len(collected_tests(f, (Path(cwd) / f).read_text(encoding="utf-8"), runner))
        except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
            return None
    return n


COLLECTION = "test collection:"  # a test gate problem no code change can fix: the lead's test files are at fault


def test_gate(name: str, plan, unit, files: list, cwd: Path) -> dict:
    if not files:
        return dict(gates.gate(name), skipped=True)
    cmd, shell = test_command(plan, unit, files)
    nonce = secrets.token_hex(8) if not shell and (NONCE_FLAG in cmd or PYTEST_PLUGIN in cmd) else None
    rc, out, err = run_cmd_split(cmd, shell, cwd, int(unit.get("test_timeout", plan.get("test_timeout", 120))),
                                 stdin=(nonce + "\n") if nonce else "")
    problems = [f"test command exited {rc}"] if rc != 0 else []
    # a custom test_cmd of a Python plan must print a result line too (pytest, unittest or mini_test); only the
    # test_cmd of a non-Python plan is judged by its exit code alone
    if rc == 0 and (not shell or plan.get("language", "python") == "python"):
        problems += result_line_problems(out, err, nonce=nonce,
                                         expected=expected_tests(cwd, files, runner_kind(plan, unit)))
    g = gates.gate(name, problems)
    g["output_tail"] = (out + err)[-4000:]
    g["rc"] = rc
    g["counts"] = runner_counts(out, err)
    if any(p.startswith(COLLECTION) for p in problems):
        g["collection"] = True
    return g


RESULT_LINE = re.compile(r"^(mini_test|pytest-nonce): (\d+) passed, (\d+) failed(?:, (\d+) skipped)?"
                         r"(?: \[([0-9a-f]+)\])?$")
PYTEST_COUNT = re.compile(r"\b(\d+) (passed|failed|errors?|skipped)\b")
UNITTEST_END = re.compile(r"^Ran (\d+) tests? in [\d.]+s\s*\n\s*\n?^(OK|FAILED)\b(.*)\Z", re.M)


def runner_counts(stdout: str, stderr: str = ""):
    """(passed, failed, skipped, nonce) from the runner's result line, which must be the LAST line of its stream
    (mini_test, pytest: stdout; unittest: stderr); None without one. nonce: what the line carries (None for pytest's
    or unittest's own summary)."""
    lines = lambda t: [ln.strip() for ln in t.splitlines() if ln.strip()]  # noqa: E731
    last_out = (lines(stdout) or [""])[-1]
    m = RESULT_LINE.match(last_out)
    if m:
        return int(m.group(2)), int(m.group(3)), int(m.group(4) or 0), m.group(5)
    if PYTEST_COUNT.search(last_out) and not re.match(r"(mini_test|pytest-nonce):", last_out):  # pytest's summary
        counts = {k: sum(int(n) for n, kk in PYTEST_COUNT.findall(last_out)
                         if kk.rstrip("s") == k.rstrip("s")) for k in ("passed", "failed", "error", "skipped")}
        return counts["passed"], counts["failed"] + counts["error"], counts["skipped"], None
    ran = next((r for r in (UNITTEST_END.search(t.rstrip()) for t in (stderr, stdout)) if r), None)
    if ran:  # python -m unittest: "Ran 3 tests in 0.01s" then "OK (skipped=1)" / "FAILED (failures=1, errors=2)"
        total, verdict, detail = int(ran.group(1)), ran.group(2), ran.group(3)
        n = lambda k: sum(int(x) for x in re.findall(rf"\b{k}=(\d+)", detail))  # noqa: E731
        failed = n("failures") + n("errors") + n("unexpected successes") + (1 if verdict == "FAILED" and not
                                                                             re.search(r"\d", detail) else 0)
        skipped = n("skipped") + n("expected failures")
        return max(total - failed - skipped, 0), failed, skipped, None
    return None


def result_line_problems(stdout: str, stderr: str = "", nonce=None, expected=None) -> list:
    """Exit code 0 is not proof: the code under test shares the test process and could end it early with 0, or
    print a passing line of its own first. The runner's own result line must be the LAST line of its stream and
    report passed tests and nothing failed or skipped. nonce: the auto runner's line (mini_test, or pytest through
    its nonce plugin) must carry it (the code under test never sees it). expected: how many tests the runner
    collects from the files; fewer passed means some never ran, a fault of the test files (COLLECTION)."""
    counts = runner_counts(stdout, stderr)
    if nonce is not None and not (counts and counts[3] == nonce):  # nothing but the runner's own line counts
        return ["the test run ended without the runner's own result line as its last line (did the code under "
                "test exit the process, or print a result line of its own?)"]
    if counts is None:
        return ["the test run ended without the runner's result line as its last line (did the code under test "
                "exit the process, or print after it?)"]
    passed, failed, skipped, _ = counts
    if failed or not passed:
        return [f"the runner reports {passed} passed, {failed} failed"]
    if skipped:
        return [f"{skipped} test(s) skipped: acceptance tests must all run"]
    if expected and passed < expected:
        return [f"{COLLECTION} the runner reports {passed} passed, but the test files hold {expected} tests it "
                "collects: some never ran, though every test that ran passed. Check the test files (`check` "
                "compares what they define with what the runner collects)"]
    return []


def build_tree(dst: Path, plan, sources: list) -> None:
    """sources: [(src_path, rel_path)] copied into a fresh dst."""
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    for src, rel in sources:
        copy_file(src if isinstance(src, bytes) else Path(src), dst / rel)


# --------------------------------------------------------------------------- agy
# attempt N runs at EFFORT_LADDER[N-1] (high from then on) unless --effort / unit / plan `effort` pins every attempt.
# Measured on slugkit with a fast variant of this skill WITHOUT the confinement hook, the sealing, the session gate,
# the tamper gate and the result-line check: a 3-unit run took 552 s at agy's default effort and 45 s with the
# ladder, where low effort passed every gate of that variant on attempt 1. Not re-measured with those layers.
# agy only: a Claude contractor has no effort setting.
EFFORT_LADDER = ("low", "medium", "high")


def effort_for(plan, unit, a, attempt: int) -> str:
    pinned = getattr(a, "effort", None) or unit.get("effort") or plan.get("effort")
    return pinned or EFFORT_LADDER[min(attempt, len(EFFORT_LADDER)) - 1]


# agy_task.py flags this script relies on (agy-delegate must be at least this new)
AGY_TASK_FLAGS = ("--bare", "--inlined")


def kill_tree(proc) -> None:
    """Kill proc and every process it started. agy is agy_task.py's child: killing agy_task.py alone (what
    subprocess.run does on a timeout) leaves agy running in the job dir."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=60)
        else:
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    with contextlib.suppress(OSError):
        proc.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=60)


class Interrupted(Exception):
    """The run was interrupted (Ctrl+C): no further session starts."""


class ProcRegistry:
    """The contractor process trees a run has live. Sessions run in worker threads, where no KeyboardInterrupt ever
    arrives, and in their own process group, which a console Ctrl+C does not reach: on an interrupt the main thread
    closes the registry, which kills every live tree and every tree that would start after it."""

    def __init__(self):
        self._lock = threading.Lock()
        self._procs = set()
        self.closed = False

    def add(self, proc) -> None:
        with self._lock:
            if not self.closed:
                self._procs.add(proc)
                return
        kill_tree(proc)
        raise Interrupted("the run was interrupted")

    def discard(self, proc) -> None:
        with self._lock:
            self._procs.discard(proc)

    def close(self) -> None:
        with self._lock:
            self.closed = True
            procs = list(self._procs)
        for p in procs:
            kill_tree(p)


def run_tree(cmd: list, stderr, env: dict, timeout: float, cwd=None, input=None, procs=None) -> tuple:
    """(rc, stdout bytes, stderr bytes or None) of cmd started in its own process group; on a timeout the whole
    tree is killed and TimeoutExpired re-raised. procs: a ProcRegistry that kills the tree on an interrupt."""
    kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=stderr, env=env, cwd=None if cwd is None else str(cwd),
                            stdin=subprocess.PIPE if input is not None else None, **kw)
    if procs is not None:
        procs.add(proc)
    try:
        out, err = proc.communicate(input=input, timeout=timeout)
    except BaseException:
        kill_tree(proc)
        for s in (proc.stdout, proc.stderr, proc.stdin):
            if s:
                with contextlib.suppress(OSError):
                    s.close()
        raise
    finally:
        if procs is not None:
            procs.discard(proc)
    return proc.returncode, out, err


def agy_task_missing_flags(agy_task: Path) -> list:
    """The flags call_agy passes that this agy_task.py does not know (an older agy-delegate rejects them: argparse
    exit 2, nothing delivered). Costs no agy call."""
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    hp = subprocess.run([sys.executable, str(agy_task), "run", "--help"], capture_output=True, env=env)
    helptext = hp.stdout.decode("utf-8", "replace")
    return [f for f in AGY_TASK_FLAGS if f not in helptext]


def outdated_agy_task_message(agy_task, missing) -> str:
    return (f"{agy_task} lacks {', '.join(missing)}: update the agy-delegate skill next to this one (or point "
            "AGY_TASK at a current agy_task.py)")


def own_hook_digests(agy_task) -> dict:
    """{rel: sha256} of the confinement hook's files exactly as this agy_task.py writes them (its hook_spec_bytes()
    and launcher_bytes(), the bytes its own is_own_hook_file() compares), so both layers agree on what counts as the
    hook's own file. {} when agy_task.py does not say (a stand-in): then nothing in .agents/ is left out."""
    try:
        spec = importlib.util.spec_from_file_location("_agy_task_hooks", str(agy_task))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception:  # noqa: BLE001 - an unreadable agy_task.py leaves nothing out, the safe side
        return {}
    out = {}
    for rel, fn in zip(gates.IGNORE_RELS, ("hook_spec_bytes", "launcher_bytes")):
        try:
            data = getattr(mod, fn)() if callable(getattr(mod, fn, None)) else None
        except Exception:  # noqa: BLE001
            data = None
        if isinstance(data, bytes):
            out[rel] = hashlib.sha256(data).hexdigest()
    return out


AGY_TASK_NO_RESULT = "agy_task.py printed no result"


def call_agy(agy_task: Path, work: Path, prompt_file: Path, a, effort: str, inlined=(), procs=None) -> dict:
    cmd = [sys.executable, str(agy_task)]
    if a.model:
        cmd += ["--model", a.model]
    # --bare: the work order already states the rules (work dir, confinement) and the delivery format; the
    # launcher's frame would repeat them and ask for a summary report nobody reads (output tokens on every attempt)
    cmd += ["run", "-w", str(work), "-f", str(prompt_file), "--timeout", f"{a.timeout}m", "--no-schema", "--bare",
            "--effort", effort]
    if a.allow_all:
        cmd.append("--allow-all")
    if getattr(a, "no_confine", False):
        cmd.append("--no-confine")
    else:  # read-only files the work order holds in full: the confinement hook denies opening them
        for rel in inlined:
            cmd += ["--inlined", rel]
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    log_file = prompt_file.with_suffix("").with_suffix(".agy.log")  # live agy progress: tail -f this file
    try:
        with open(log_file, "wb") as lf:
            rc, raw, _ = run_tree(cmd, lf, env, a.timeout * 60 + 120, procs=procs)
    except subprocess.TimeoutExpired:
        # agy ignored its own --print-timeout; agy_task.py and agy (its child) were killed as one tree
        return {"rc": None, "error": "timeout"}
    out, err = raw.decode("utf-8", "replace"), log_file.read_text(encoding="utf-8", errors="replace")
    res = {}
    try:
        res = json.loads(out[out.index("{"):]) if "{" in out else {}
    except json.JSONDecodeError:
        pass
    if not isinstance(res, dict):
        res = {}
    # this attempt's record dir: only one inside the job dir's .agy_task counts
    meta = Path(res["meta_dir"]) if isinstance(res.get("meta_dir"), str) else None
    if meta is not None and not (is_within(meta, work / ".agy_task") and meta.is_dir()):
        meta = None
    # agy's quota error; the caller only believes it when nothing was delivered. Only what agy itself said in THIS
    # attempt (its stderr.txt and the AGY_ERROR agy_task.py parsed): the job dir keeps every earlier attempt's
    # stderr, and agy_task.py's own log echoes the agent's narration and tool arguments, which may mention
    # RESOURCE_EXHAUSTED or a 429 for reasons of the unit's own (a gRPC or HTTP-status module)
    texts = [json.dumps(res.get("agy_error") or ""), json.dumps(res.get("error") or "")]
    if meta is not None and (meta / "stderr.txt").is_file():
        texts.append((meta / "stderr.txt").read_text(encoding="utf-8", errors="replace"))
    if not res:  # no result at all: only agy's own error lines in agy_task.py's log, never the narration
        texts += [ln for ln in err.splitlines() if re.match(r"\s*(AGY_ERROR:|error:)", ln, re.I)]
    quota = None
    for line in "\n".join(texts).splitlines():
        if QUOTA_RE.search(line):
            m = re.search(r"Resets in [^.\"]+", line)
            quota = m.group(0) if m else line.strip()[:200]
            break
    conf = res.get("confinement") or {}
    out = {"rc": rc, "quota": quota, "status": res.get("status"), "agent_status": res.get("agent_status"),
           "conversation_id": res.get("conversation_id"), "elapsed_sec": res.get("elapsed_sec"),
           "agy_error": res.get("agy_error"), "stderr_tail": err[-600:] if rc not in (0, 2) or not res else "",
           # agy_task.py confines agy's tool calls to the work dir; only its own report says it really did (the hook
           # ran for every tool call). denied_count counts calls that tried to get out; views of a file the work
           # order already holds in full (--inlined) are denied too, but counted apart
           "meta_dir": str(meta) if meta is not None else None, "ran": bool(res),
           "confined": bool(conf.get("enabled")), "denied_count": conf.get("denied_count", 0),
           "inlined_denied_count": conf.get("inlined_denied_count", 0),
           "denied": [{k: d.get(k) for k in ("tool", "rule", "reason") if d.get(k) is not None}
                      for d in conf.get("denied", [])][:10]}
    if not res:  # agy_task.py always prints its result
        # rc 2 without a result is argparse rejecting a flag: agy never started. Any other rc is agy_task.py
        # dying after agy may have run (e.g. an uncaught exception): nobody reported whether the hook held
        out["never_started"] = rc == 2
        out["error"] = (f"{AGY_TASK_NO_RESULT} (rc={rc}): "
                        + ("an agy-delegate older than this agy-outsource (needs " + ", ".join(AGY_TASK_FLAGS) + ")? "
                           if rc == 2 else "it crashed after agy may have run. ")
                        + "stderr: " + err[-300:].strip())
    return out


def path_forms(p) -> set:
    s = str(Path(p).resolve())
    return {s, s.replace("\\", "/"), s.replace("\\", "\\\\")}


def scrub(text: str, dirs: list) -> str:
    """Replace absolute run/spec paths in test output so feedback never points agy at the sealed tests."""
    for d in sorted({f for d in dirs for f in path_forms(d)}, key=len, reverse=True):
        text = re.sub(re.escape(d) + r"[\\/]?", "", text, flags=re.I)
    return text


def hidden_peek(plan, unit, work: Path, extra_dirs=(), states=None, files=None) -> list:
    """Hidden test names or paths that show up in agy's recorded tool calls (it should never see them).

    Only tool steps count (the prompt or the feedback echoed in the event stream is not a peek), judged by each
    step's final state. states=None: calls that ran, i.e. every tool step whose last state is not ERROR (DONE, and
    also a step left ACTIVE by a killed or timed-out session: it may have read the file). states=("ERROR",): calls
    that were refused (the confinement hook's denials end in ERROR and read nothing). files: the events.jsonl files
    to read (one attempt's); None covers every attempt (the job dir keeps each attempt's .agy_task record)."""
    hits = []
    needles = set()
    for f in unit["hidden_tests"]:
        p = (plan["_dir"] / f).resolve()
        needles |= {p.name} | path_forms(p.parent)
    for d in extra_dirs:
        needles |= path_forms(d)
    for ev in (work.glob(".agy_task/*/events.jsonl") if files is None else [Path(f) for f in files if Path(f).is_file()]):
        final = {}  # step -> (last state, tool_info of every update of that step)
        for line in ev.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                su = json.loads(line).get("step_update") or {}
            except (json.JSONDecodeError, AttributeError):
                continue
            if su.get("step_type") != "tool":
                continue
            state, infos = final.get(su.get("step_index"), (None, []))
            final[su.get("step_index")] = (su.get("state") or state,
                                           infos + [json.dumps(su.get("tool_info"), ensure_ascii=False)])
        text = "\n".join(i for state, infos in final.values()
                         if (state != "ERROR" if states is None else state in states) for i in infos)
        hits += [n for n in needles if n and n in text]
    return sorted(set(hits))


# --------------------------------------------------------------------------- run one unit
class Runner:
    def __init__(self, plan, out: Path, a, agy_task, roles_=None):
        self.plan, self.out, self.a, self.agy_task = plan, out, a, agy_task
        self.roles = roles_ or {}
        self.contractor = getattr(a, "contractor", None) or "agy"
        # what acceptance depends on, fingerprinted once: any change during the run voids the run. agy-delegate's
        # scripts too: they install the confinement hook (and its launcher) and report whether it held
        delegate = []
        if agy_task:
            d = Path(agy_task).resolve().parent
            delegate = [Path(agy_task).resolve(), d / "confine_hook.py"]
        self.evaluator = {str(p): digest_path(p) for p in
                          (Path(__file__).resolve(), Path(gates.__file__).resolve(), Path(roles.__file__).resolve(),
                           MINI_TEST, PROMPT, *delegate, plan["_path"], plan["_dir"] / SEALED,
                           *[plan["_dir"] / f for u in plan["units"] for f in u["hidden_tests"]])}
        self.own_hooks = own_hook_digests(agy_task) if agy_task else {}
        self.by = units_by_id(plan)
        self.gate = SessionGate()
        self.procs = ProcRegistry()  # live contractor process trees, killed on an interrupt
        self.stop = threading.Event()
        self._quoted = None
        self.state_path = out / "state.json"
        self.state = read_json(self.state_path, None) or {"plan": str(plan["_path"]), "units": {}}
        self.state.setdefault("units", {})

    def save(self):
        with _lock:
            write_json(self.state_path, self.state)

    def ustate(self, uid) -> dict:
        return self.state["units"].setdefault(uid, {"status": "pending", "attempts": 0})

    def frozen_sources(self, unit) -> list:
        """[(src, rel)] read-only inputs (unit_inputs): dependencies' accepted code and context files (transitively),
        the unit's context files, visible tests. src is always the original, never a job dir: the integrity gate
        fingerprints these sources."""
        base = {"dep": self.out / "accepted", "indirect": self.out / "accepted", "dep_context": self.plan["_root"],
                "context": self.plan["_root"], "test": self.plan["_dir"]}
        return [(base[kind] / rel, rel) for rel, kind in unit_inputs(self.plan, unit).items()]

    def quoted(self) -> set:
        """hidden_quote_needles of every hidden test of the plan (any of them could have been read by code under
        test, which runs as the user), computed in memory once per run."""
        with _lock:
            if self._quoted is None:
                hidden = [(hidden_src(self.plan, f) or b"").decode("utf-8", "replace")
                          for u in self.plan["units"] for f in u["hidden_tests"]]
                known = [(self.plan["_dir"] / f).read_text(encoding="utf-8", errors="replace")
                         for u in self.plan["units"] for f in u["tests"] if (self.plan["_dir"] / f).is_file()]
                self._quoted = hidden_quote_needles(hidden, known)
            return self._quoted

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
        missing = [rel for src, rel in frozen_src if not Path(src).is_file()]
        if missing:  # e.g. a dependency's accepted code under a deliverable path its plan entry no longer has
            for k in ("contractor_model", "last_failed", "flags"):
                st.pop(k, None)
            st.update(status="needs_attention", attempts=0, history=[],
                      note="inputs missing: " + ", ".join(missing)[:250] + " (a dependency not accepted under its "
                           "current deliverables, or a context file / visible test that is gone); nothing was sent")
            self.save()
            return "needs_attention"
        baseline = {rel: digest(src) for src, rel in frozen_src}
        max_attempts = int(self.a.max_attempts or unit.get("max_attempts") or self.plan.get("max_attempts") or 3)
        # history restarts with every run of the unit, so a re-run of a needs_attention unit climbs the effort
        # ladder from the bottom again (unless the effort is pinned). Nothing an earlier run left may speak for this
        # one: its note (quota, timeout, integrity, blocked), failed gates, flags and the referee's verdict on its
        # failure; contractor_model is only set by a contractor that reports its model (Claude)
        for k in ("contractor_model", "note", "last_failed", "flags"):
            st.pop(k, None)
        (udir / "triage.json").unlink(missing_ok=True)
        st.update(status="running", attempts=0, spec_hash=spec_hash(self.plan, unit, self.state), history=[],
                  contractor=self.contractor)
        self.save()
        # flags from EVERY attempt: code fitted in an unconfined attempt (or one that saw a hidden test) survives
        # into the accepted one, since the job dir and the contractor's files persist across a unit's attempts
        unit_flags = set()
        is_agy = self.contractor == "agy"
        confined = is_agy and not getattr(self.a, "no_confine", False)
        # the hook's own files are left out of the snapshots only when agy-delegate installs them (a confined agy
        # run), and only while they hold exactly its bytes; otherwise a .agents/hooks.json or launcher in the work dir
        # is agy's own work and must fail the scope gate (agy would run its command on every tool call of the next
        # session)
        hook_rels = self.own_hooks if confined else {}
        local = project_modules(self.plan, unit)  # what the verify tree holds, not what lies around in the job dir
        all_hidden = [f for u in self.plan["units"] for f in u["hidden_tests"]]
        job = work.parent
        job_snap = None  # the whole job dir as the previous attempt's gates and tests left it
        feedback, results = "", []
        for attempt in range(1, max_attempts + 1):
            if self.stop.is_set():
                return self._interrupted(st, attempt - 1, unit_flags)
            if job_snap is not None:
                moved = diff_names(job_snap, full_snapshot(job))
                if moved:  # nothing of ours touches the job dir between attempts: other code wrote into it
                    st.update(status="needs_attention", last_failed=["integrity"], flags=sorted(unit_flags),
                              note="integrity: the job dir changed between attempts: " + ", ".join(moved)[:250])
                    self.save()
                    return "needs_attention"
            for src, rel in frozen_src:  # restore read-only inputs (undoes any tampering of a previous attempt)
                copy_file(src, work / rel)
            (work / gates.MANIFEST).unlink(missing_ok=True)
            frozen = {rel: gates.sha256_file(work / rel) for _, rel in frozen_src}
            before = gates.snapshot(work, hook_rels)
            inlined = []
            prompt = render_work_order(self.plan, unit, work, feedback, self.a.allow_all,
                                       not getattr(self.a, "no_confine", False), present=before, inlined=inlined,
                                       inline_own=is_agy)
            pfile = udir / f"attempt_{attempt}.prompt.md"
            pfile.write_text(prompt, encoding="utf-8")
            effort = effort_for(self.plan, unit, self.a, attempt) if is_agy else None
            log(f"{uid}: attempt {attempt}/{max_attempts} -> {self.contractor}"
                + (f" (effort {effort})" if effort else ""))
            records = work / ".agy_task"
            stamps_before = {p.name for p in records.iterdir()} if records.is_dir() else set()
            try:
                with self.gate.session():  # no hidden test is in plaintext anywhere while a session runs
                    if self.stop.is_set():
                        raise Interrupted()
                    if is_agy:
                        agy = call_agy(self.agy_task, work, pfile, self.a, effort, inlined, procs=self.procs)
                    else:  # no hook for a Claude contractor: `inlined` would not be enforced there, so it is not passed
                        agy = call_claude_contractor(self.contractor, work, pfile, self.a, self.roles, procs=self.procs)
            except Interrupted:
                return self._interrupted(st, attempt - 1, unit_flags)
            if self.stop.is_set():  # the session was killed by the interrupt: its delivery proves nothing
                return self._interrupted(st, attempt - 1, unit_flags)
            job_snap0 = full_snapshot(job)  # from here on only our gates and the code under test run
            timed_out = agy.get("rc") is None and agy.get("error") == "timeout"
            # agy-delegate's record of THIS session only (the job dir keeps every earlier attempt's as well)
            rec_dirs = ([Path(agy["meta_dir"])] if agy.get("meta_dir") else
                        sorted(p for p in records.glob("*") if p.is_dir() and p.name not in stamps_before))
            for d in rec_dirs:
                shutil.copytree(d, udir / f"attempt_{attempt}.agy_task" / d.name, dirs_exist_ok=True)
            if agy.get("quota") and not (work / gates.MANIFEST).exists():
                st.update(attempts=attempt - 1, flags=sorted(unit_flags))
                raise QuotaExhausted(agy["quota"])
            changes = gates.diff(before, gates.snapshot(work, hook_rels))
            results = gates.static_gates(work, unit, changes, frozen, self.plan, local=local)
            integrity = self.integrity_gate(frozen_src, baseline)
            results.insert(0, integrity)
            run_problem = None
            if agy.get("rc") not in (0, 2) or agy.get("error"):
                # one line: agy_task.py's log tail is multi-line and ends up in a report.md bullet
                run_problem = " ".join(
                    (f"{self.contractor} run failed (rc={agy.get('rc')}): "
                     f"{agy.get('error') or agy.get('agy_error') or agy.get('stderr_tail') or ''}"
                     f"{'' if agy.get('error') else ' (status ' + str(agy.get('status')) + ')'}").split())[:400]
            # tests run on clean copies: deliverables + read-only inputs. The visible tests run in a tree that holds
            # no hidden test (their output goes back to agy, and the code under test can print whatever it reads);
            # the hidden tests then run in a fresh tree (nothing the visible run planted there, e.g. a conftest.py
            # or sitecustomize.py, runs next to them), in clear only between build_tree and sweep_hidden while no
            # contractor session of this run is live. Not after a timeout: the session was killed, the unit stops.
            verify = udir / "verify"
            if not timed_out:
                srcs = [(work / f, f) for f in unit["deliverables"] if (work / f).is_file()] + frozen_src
                build_tree(verify, self.plan, srcs)
                results.append(test_gate("tests:visible", self.plan, unit, unit["tests"], verify))
                with self.gate.plaintext():
                    try:
                        build_tree(verify, self.plan,
                                   srcs + [(hidden_src(self.plan, f), f) for f in unit["hidden_tests"]])
                        results.append(test_gate("tests:hidden", self.plan, unit, unit["hidden_tests"], verify))
                    finally:
                        sweep_hidden(self.plan, udir)
                # the code under test ran unconfined as the user: nothing acceptance depends on may have changed
                # while it ran, nor anything in the job dir (the next attempt lists and inlines it)
                late = [p for p in self.integrity_gate(frozen_src, baseline)["problems"]
                        if p not in integrity["problems"]]
                moved = diff_names(job_snap0, full_snapshot(job))
                if moved:
                    late.append("job dir changed while the gates and tests ran: " + ", ".join(moved)[:250])
                if late:
                    integrity = gates.gate("integrity", integrity["problems"] + [f"during the tests: {p}" for p in late])
                    results[0] = integrity
            job_snap = full_snapshot(job)
            for g in results:
                if "output_tail" in g:
                    g["output_tail"] = scrub(g["output_tail"], [verify, work, self.plan["_dir"]])
            if run_problem:
                # agy's exit status alone is only a warning when the delivery passes every other gate (integrity,
                # static, tamper, result-line tests). Always a hard fail for a Claude contractor (its rc 3 also means
                # it ran on the wrong model or on the lead's family), for a timeout, and when agy_task.py printed no
                # result
                soft = (is_agy and agy.get("rc") is not None and not agy.get("error") and not timed_out
                        and all(g["ok"] for g in results))
                results.insert(1, gates.gate("agy", [] if soft else [run_problem], [run_problem] if soft else []))
            # local scan of THIS attempt's recorded tool calls (no tokens, independent of the hook's own report)
            ev_files = [d / "events.jsonl" for d in rec_dirs]
            peek = hidden_peek(self.plan, unit, work, [verify], files=ev_files)
            peek_denied = hidden_peek(self.plan, unit, work, [verify], states=("ERROR",), files=ev_files)
            attempt_flags = set()
            if is_agy and agy.get("rc") is None:  # killed before agy_task.py could report whether the hook held
                attempt_flags.add("confinement_unknown")
            elif is_agy and not agy.get("ran"):
                # no result from agy_task.py: an argparse rejection (rc 2) means agy never started; any other exit
                # (a crash after agy ran, maybe writing files the next attempt carries forward) leaves the hook
                # unreported
                if not agy.get("never_started"):
                    attempt_flags.add("confinement_unknown")
            elif not agy.get("confined"):
                attempt_flags.add("unconfined")
            if peek:  # a call that ran mentions a hidden test, confined or not: the hidden pass is unproven
                attempt_flags.add("hidden_peek")
            if peek_denied:
                attempt_flags.add("hidden_peek_denied")
            if agy.get("denied_count"):  # calls that tried to get out (views of inlined files are counted apart)
                attempt_flags.add("outside_access_denied")
            unit_flags |= attempt_flags
            ok = all(g["ok"] for g in results)
            failed = [g["gate"] for g in results if not g["ok"]]
            rec = {"attempt": attempt, "effort": effort, "ok": ok, "failed_gates": failed, "changes": changes,
                   "agy": agy, "contractor": self.contractor, "gates": results, "hidden_peek": peek,
                   "hidden_peek_denied": peek_denied, "flags": sorted(attempt_flags),
                   # read-only files passed as --inlined, i.e. what the hook denied opening: none when no hook ran
                   # (a Claude contractor, --no-confine); the work order may still hold their text
                   "inlined": inlined if confined else [],
                   "assumptions": (gates.load_manifest(work)[0] or {}).get("assumptions", [])}
            write_json(udir / f"attempt_{attempt}.json", rec)
            st["attempts"] = attempt
            st["history"].append({"attempt": attempt, "effort": effort, "failed_gates": failed})
            if agy.get("model"):
                st["contractor_model"] = agy["model"]
            log(f"{uid}: attempt {attempt} " + ("ACCEPTED" if ok else "rejected: " + ", ".join(failed)))
            if not integrity["ok"]:  # the evaluator or a read-only input changed under us: retrying proves nothing
                st.update(status="needs_attention", last_failed=failed, flags=sorted(unit_flags),
                          note="integrity: " + "; ".join(integrity["problems"])[:300])
                self.save()
                return "needs_attention"
            if timed_out:  # never another attempt in a job dir a killed session wrote into
                st.update(status="needs_attention", last_failed=failed, flags=sorted(unit_flags),
                          note=f"timeout: the {self.contractor} session ran past --timeout ({self.a.timeout} min) "
                               "and was killed; re-run the unit (a larger --timeout, or split it)")
                self.save()
                return "needs_attention"
            coll = [p for g in results if g.get("collection") and not g["ok"] for p in g["problems"]
                    if p.startswith(COLLECTION)]
            if coll:  # no change to the code can fix the test files: another attempt would only burn a session
                st.update(status="needs_attention", last_failed=failed, flags=sorted(unit_flags),
                          note=f"tests: {coll[0]}"[:300])
                self.save()
                return "needs_attention"
            if ok:
                files = {}
                for f in unit["deliverables"]:
                    copy_file(work / f, self.out / "accepted" / f)
                    files[f] = gates.sha256_file(work / f)
                manifest, _ = gates.load_manifest(work)
                # an integration result proves the code it ran; this unit's accepted code is new
                self.state.pop("integration", None)
                st.update(status="accepted", files=files, flags=sorted(unit_flags),
                          assumptions=(manifest or {}).get("assumptions", []),
                          warnings=[w for g in results for w in g.get("warnings", [])],
                          accepted_at=_dt.datetime.now().isoformat(timespec="seconds"))
                self.save()
                return "accepted"
            feedback = render_feedback(attempt, max_attempts, results,
                                       unit.get("hidden_feedback", self.plan.get("hidden_feedback", "summary")),
                                       hidden=all_hidden, quoted=self.quoted())
            self.save()
        st.update(status="needs_attention", last_failed=st["history"][-1]["failed_gates"], flags=sorted(unit_flags))
        # agy_task.py gave no result on the last attempt: say why here, the failed-gate list alone points to triage
        agy_gate = next((g for g in results if g["gate"] == "agy" and not g["ok"]), None)
        if agy_gate and any(AGY_TASK_NO_RESULT in p for p in agy_gate["problems"]):
            st["note"] = "agy: " + agy_gate["problems"][0][:300]
        self.save()
        return "needs_attention"

    def _interrupted(self, st, attempts: int, unit_flags) -> str:
        st.update(status="pending", attempts=attempts, flags=sorted(unit_flags),
                  note="interrupted: the run was stopped (Ctrl+C); re-run the unit")
        self.save()
        return "interrupted"

    def integrity_gate(self, frozen_src, baseline: dict) -> dict:
        """Nothing the acceptance depends on may change while a unit runs: the gate scripts, the plan, the
        hidden-test store and this unit's read-only inputs (its dependencies' accepted code, context, visible tests)."""
        problems = [f"evaluator file changed: {p}" for p, h in self.evaluator.items() if digest_path(Path(p)) != h]
        problems += [f"read-only input changed at its source: {rel}" for src, rel in frozen_src
                     if digest(src) != baseline[rel]]
        return gates.gate("integrity", problems)

    # ----------------------------------------------------------------------- scheduling
    def current(self, uid) -> bool:
        return is_current(self.plan, self.state, uid)

    def pending(self, selected: set) -> list:
        todo = []
        for uid in topo_order(self.plan):
            if uid not in selected:
                continue
            if (self.a.force and uid in self.forced) or not self.current(uid):
                todo.append(uid)
        return todo

    def run(self, selected: set, forced=None) -> dict:
        """forced: the units --force re-runs even when current (default: selected); dependencies pulled in by
        cmd_run are re-run only when they are not current."""
        self.forced = set(selected if forced is None else forced) if self.a.force else set()
        done, quota, todo = set(), None, []
        while not quota:
            # recomputed every wave: a re-accepted dependency changes its dependents' spec hash
            todo = [u for u in self.pending(selected) if u not in done]
            for uid in todo:
                if self.ustate(uid).get("status") == "accepted":
                    self.ustate(uid)["status"] = "stale"
            # a dependency counts only when accepted under its current spec: a stale one would hand this unit
            # outdated code (or, if its deliverables were renamed, files that do not exist)
            ready = [u for u in todo if all(self.current(d) for d in self.by[u]["depends_on"])]
            if not ready:
                break
            try:
                with cf.ThreadPoolExecutor(max_workers=max(1, self.a.jobs)) as ex:
                    futs = {ex.submit(self.process, u): u for u in ready}
                    pending = set(futs)
                    try:
                        while pending:  # timed waits: a Ctrl+C reaches this (main) thread within a second
                            finished, pending = cf.wait(pending, timeout=0.5, return_when=cf.FIRST_COMPLETED)
                            for fut in finished:
                                uid = futs[fut]
                                try:
                                    fut.result()
                                except QuotaExhausted as e:
                                    quota = str(e)
                                    self.ustate(uid).update(status="pending", note=f"agy quota exhausted: {quota}")
                                    log(f"agy quota exhausted ({quota}); stopping after the running units")
                    except BaseException:
                        # KeyboardInterrupt arrives here only: the sessions run in workers, in their own process
                        # groups. Kill every live tree, start no further session or attempt, then let the workers
                        # finish (the executor waits for them on the way out)
                        self.stop.set()
                        self.procs.close()
                        log("stopping: killing the running sessions; no further attempt starts")
                        raise
            except BaseException:
                self.save()
                raise
            done |= set(ready)
        todo = [u for u in todo if u not in done]
        for uid in todo:  # never became ready
            bad = [d for d in all_deps(self.plan, uid) if not self.current(d)]
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
        with self.gate.plaintext():
            try:
                build_tree(integ, self.plan, srcs)
                res = {"tests": test_gate("integration:tests", self.plan, {}, sorted(set(tests)), integ)}
            finally:
                drop_hidden(self.plan, integ)
        if self.plan.get("integration_test_cmd"):
            # judged by its exit code only (documented): it is the lead's own check, not a test runner
            cmd, _ = test_command(self.plan, {"test_cmd": self.plan["integration_test_cmd"]}, [])
            rc, out = run_cmd(cmd, True, integ, int(self.plan.get("test_timeout", 120)) * 2)
            res["command"] = dict(gates.gate("integration:command", [] if rc == 0 else [f"exited {rc}"]),
                                  rc=rc, output_tail=out[-3000:])
        res["ok"] = all(g["ok"] for g in res.values() if isinstance(g, dict))
        res["at"] = _dt.datetime.now().isoformat(timespec="seconds")
        # what this result proves: `integrate` trusts it only while every unit's spec hash, every unit's accepted
        # files (a re-accepted unit without dependents changes no hash) and the integration command are the same
        res.update(integration_fingerprint(self.plan, self.state))
        self.state["integration"] = res
        self.save()
        return res


# --------------------------------------------------------------------------- report
def write_report(plan, out: Path) -> str:
    state = read_json(out / "state.json", {"units": {}})
    lines = [f"# agy-outsource report — {plan['project']}", "",
             f"plan: `{plan['_path']}`  ", f"run dir: `{out}`", "",
             "| unit | title | status | attempts (effort) | failed gates (last) | flags |", "|---|---|---|---|---|---|"]
    counts = {}
    for uid in topo_order(plan):
        u = units_by_id(plan)[uid]
        st = state["units"].get(uid, {"status": "pending", "attempts": 0})
        stale = st.get("status") == "accepted" and not is_current(plan, state, uid)
        status = "stale" if stale else st.get("status", "pending")
        counts[status] = counts.get(status, 0) + 1
        last = (st.get("history") or [{}])[-1].get("failed_gates", []) if status != "accepted" else []
        efforts = ", ".join(h["effort"] for h in st.get("history", []) if h.get("effort"))
        lines.append(f"| {uid} | {u['title']} | **{status}** | {st.get('attempts', 0)}"
                     + (f" ({efforts})" if efforts else "") + f" | {', '.join(last) or '-'} | "
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
            flags = set(st.get("flags", []))  # collected over every attempt of the unit
            if "hidden_peek" in flags:
                lines.append(f"- {uid}: a tool call that ran mentions the hidden tests — treat its hidden-test pass "
                             "as unproven")
            if "unconfined" in flags:
                lines.append(f"- {uid}: an attempt ran without work-dir confinement (--no-confine, a hook that never "
                             "ran, or an older agy_task.py) — treat its hidden-test pass as unproven")
            if "confinement_unknown" in flags:
                lines.append(f"- {uid}: an attempt was killed (timeout), or agy_task.py crashed, before it reported "
                             "its confinement — review it closely")
            if {"hidden_peek_denied", "outside_access_denied"} & flags:
                lines.append(f"- {uid}: agy tried to reach files outside its work dir; every such call was denied "
                             f"(see `units/{uid}/attempt_N.json`, agy.denied and flags)")
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
        # every collected test fails or passes on the stubs: the runner's total is what it collects
        expected = expected_tests(red, files, runner_kind(plan, u))
        if not m and g.get("counts") and expected:
            collected = sum(g["counts"][:3])
            if collected < expected:
                item["collection_problem"] = (f"the runner collected {collected} tests, but the test files hold "
                                              f"{expected} it should collect")
        report.append(item)
    return report


def cmd_check(a) -> int:
    plan = load_plan(a.plan)
    errors, warnings = validate_plan(plan)
    if not errors:  # what every test gate would trip over, before any contractor attempt is spent on it
        errors += collection_problems(plan)
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
            if r.get("collection_problem"):
                res["errors"].append(f"unit {r['unit']!r}: test file defines more tests than the runner collects: "
                                     f"{r['collection_problem']}")
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
    if not errors:
        errors += collection_problems(plan)
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
    try:
        agy_task = find_agy_task() if kind == "agy" else None
    except FileNotFoundError as e:
        print(json.dumps({"status": "error", "message": str(e)}))
        return 4
    if agy_task is not None:  # an outdated agy-delegate would fail every attempt of every unit on the agy gate
        missing = agy_task_missing_flags(agy_task)
        if missing:
            print(json.dumps({"status": "error", "missing_flags": missing,
                              "message": outdated_agy_task_message(agy_task, missing)}, ensure_ascii=False, indent=2))
            return 4
    out = out_dir(plan, a)
    check_jobs_root(plan, out)
    by = units_by_id(plan)
    selected = set(by)
    if a.units:
        selected = {x.strip() for x in a.units.split(",") if x.strip()}
        unknown = selected - set(by)
        if unknown:
            raise PlanError("unknown units: " + ", ".join(sorted(unknown)))
    unaudited = audit_problems(plan, roles_, topo_order(plan))  # integration runs every unit's tests
    if unaudited:
        print(json.dumps({"status": "not-audited", "errors": unaudited}, ensure_ascii=False, indent=2))
        return 5
    # sealed only once every refusal above is behind us (a refused run leaves the spec folder as it was), and before
    # the Runner fingerprints the hidden-test store
    if os.name == "nt":  # agy must never find the hidden tests in clear while it runs
        sealed = seal_plan(plan)["sealed"]
        if sealed:
            log("sealed hidden tests: " + ", ".join(sealed))
    else:
        log("warning: hidden tests stay in plaintext (sealing needs Windows DPAPI)")
    out.mkdir(parents=True, exist_ok=True)
    sweep_hidden(plan, out)  # plaintext copies a crashed or interrupted run left behind
    runner = Runner(plan, out, a, agy_task, roles_)
    asked = set(selected)
    # pull in every dependency that is not accepted under its current spec (stale or never accepted), and every
    # dependency of a selected unit that sits on top of one pulled in: it goes stale when that one is redone
    deps = {d for uid in asked for d in all_deps(plan, uid)}
    for d in topo_order(plan):
        if d in deps and d not in selected and (not runner.current(d)
                                                or any(x in selected for x in all_deps(plan, d))):
            log(f"including dependency {d}" + (" (stale)" if runner.ustate(d).get("status") == "accepted"
                                                and not runner.current(d) else ""))
            selected.add(d)
    res = runner.run(selected, forced=asked)
    all_ok = all(runner.current(u) for u in by)
    integ = None
    if all_ok and not res["quota"] and not a.no_integration:
        log("all units accepted -> integration tests")
        integ = runner.integrate_check()
    write_report(plan, out)
    summary = {
        "status": "quota" if res["quota"] else ("ok" if all(runner.current(u) for u in selected)
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
    # accepted means accepted under the CURRENT spec: a unit whose spec, tests, dependencies or acceptance rules
    # changed since (report.md shows it as stale) is re-validated by the next `run`, never copied as it is
    stale = [u["id"] for u in plan["units"] if state["units"].get(u["id"], {}).get("status") == "accepted"
             and not is_current(plan, state, u["id"])]
    missing = [u["id"] for u in plan["units"]
               if state["units"].get(u["id"], {}).get("status") != "accepted" or u["id"] in stale]
    if missing and not a.partial:
        print(json.dumps({"status": "not-ready", "not_accepted": [u for u in missing if u not in stale],
                          "stale": stale, "message": "run `run` first (stale units are re-validated by it)"},
                         ensure_ascii=False, indent=2))
        return 2
    integ = state.get("integration") or {}
    fp = integration_fingerprint(plan, state)
    if not a.partial and not (integ.get("ok") and all(integ.get(k) == v for k, v in fp.items())):
        print(json.dumps({"status": "not-ready", "message": "the integration tests have not passed for the current "
                          "spec and accepted code of every unit (and the current integration_test_cmd); run `run` "
                          "first"}, ensure_ascii=False, indent=2))
        return 2
    copies, context = [], []
    for u in plan["units"]:
        if u["id"] in missing:
            continue
        copies += [(out / "accepted" / f, f) for f in u["deliverables"]]
        if a.with_tests:
            copies += [(plan["_dir"] / f, f) for f in u["tests"]]
            copies += [(hidden_src(plan, f), f) for f in u["hidden_tests"]]
        # context files (e.g. a new package's __init__.py) the destination lacks: without them the copied
        # deliverables would not import the way they were tested. Existing destination files are never touched.
        context += [f for f in u["context_files"] if not (dest / f).exists() and f not in context]
    conflicts = [rel for src, rel in copies
                 if (dest / rel).exists() and gates.sha256_file(dest / rel) != digest(src)]
    if conflicts and not a.overwrite:
        print(json.dumps({"status": "conflict", "message": "files differ in the destination; pass --overwrite",
                          "conflicts": conflicts}, ensure_ascii=False, indent=2))
        return 2
    if not a.dry_run:
        for src, rel in copies:
            copy_file(src, dest / rel)
        for rel in context:
            copy_file(plan["_root"] / rel, dest / rel)
    print(json.dumps({"status": "dry-run" if a.dry_run else "ok", "dest": str(dest),
                      "files": [rel for _, rel in copies], "context_files_added": context, "overwritten": conflicts,
                      "skipped_units": [u for u in missing if u not in stale], "skipped_stale_units": stale},
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
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    # an older agy-delegate rejects the flags call_agy passes (argparse exit 2, nothing delivered): catch it here
    missing = agy_task_missing_flags(agy_task)
    res["agy_task_flags"] = {"ok": not missing, "missing": missing}
    if missing:
        res["agy_task_flags"]["error"] = outdated_agy_task_message(agy_task, missing)
    cp = subprocess.run([sys.executable, str(agy_task), "preflight"], capture_output=True, env=env)
    try:
        res["agy"] = json.loads(cp.stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        res["agy"] = {"ok": False, "raw": cp.stdout.decode("utf-8", "replace")[-800:]}
    res["ok"] = cp.returncode == 0 and not missing
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
    rn.add_argument("--effort", choices=EFFORT_LADDER,
                    help="pin every agy attempt to this effort (default: attempt 1 low, 2 medium, 3+ high; a unit's "
                         "or the plan's `effort` also pins). Not used by a Claude contractor")
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

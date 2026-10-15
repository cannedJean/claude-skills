"""gates.py - deterministic acceptance gates for one agy delivery (no agy, no LLM).

A delivery is the state of a unit's work directory after agy finished. Each gate returns
{"gate": name, "ok": bool, "problems": [...], "warnings": [...]}; a unit is accepted only when every gate is ok.

  manifest    DELIVERY.json exists, parses, names the unit and lists exactly the deliverables
  scope       only deliverables (and DELIVERY.json) were written; read-only files are untouched
  files       every deliverable exists and is not empty
  syntax      Python deliverables parse
  interface   declared symbols exist with the declared kind and parameter list (Python: AST; else: name present)
  imports     Python imports stay inside the allowed set (stdlib / project modules / plan allow-list)
  forbid      no deliverable matches a forbidden regex
  tamper      Python deliverables cannot see or steer the tests they run next to (no test-framework or frame
              introspection imports, no early exit or process-ending exception, no eval/exec, no getattr & co. on a
              module, with a computed name or as a value, no class-hierarchy, __dict__, frame or traceback
              attributes, no process-exit attribute names on an unresolved base, no string naming a test file)
  tests       (run by outsource.py) visible tests, then sealed hidden tests

Also: Python stub generation for the "red" check (tests must fail before the code exists).
Python 3.9+, stdlib only.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import sys
from pathlib import Path

MANIFEST = "DELIVERY.json"
IGNORE_DIRS = {"__pycache__", ".pytest_cache", ".agy_task", ".mypy_cache", ".ruff_cache", ".git",
               "node_modules", ".venv", "venv"}
IGNORE_FILES = {".DS_Store", "Thumbs.db", "desktop.ini"}
# the confinement hook's own files (agy-delegate writes them for the session and puts back what was there after).
# snapshot() leaves them out only when asked to, i.e. for a confined agy run, and only while each holds exactly the
# bytes agy-delegate writes (the same rule as agy_task.is_own_hook_file). Anything else there, e.g. a hooks.json or
# launcher agy rewrote and a killed session never got restored, is agy's own work (agy would run its command on
# every tool call of the next session there) and fails the scope gate.
IGNORE_RELS = (".agents/hooks.json", ".agents/confine.cmd")
IGNORE_SUFFIXES = (".pyc", ".pyo")


def gate(name: str, problems=None, warnings=None) -> dict:
    problems = list(problems or [])
    return {"gate": name, "ok": not problems, "problems": problems, "warnings": list(warnings or [])}


# --------------------------------------------------------------------------- snapshots
def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def snapshot(root: Path, own_hooks=None) -> dict:
    """{relative/posix/path: sha256} for every file under root, skipping caches and agy metadata. own_hooks:
    {rel: sha256} of the confinement hook's files exactly as agy-delegate writes them (a confined agy run; see
    outsource.own_hook_digests); a file at one of those rels is left out only while its digest matches."""
    snap = {}
    root = Path(root)
    own_hooks = own_hooks or {}
    if not root.exists():
        return snap
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in IGNORE_DIRS]
        for fn in fns:
            if fn in IGNORE_FILES or fn.endswith(IGNORE_SUFFIXES):
                continue
            p = Path(dp) / fn
            rel = p.relative_to(root).as_posix()
            try:
                digest = sha256_file(p)
            except OSError:
                continue
            if own_hooks.get(rel) == digest:
                continue
            snap[rel] = digest
    return snap


def diff(before: dict, after: dict) -> dict:
    return {"created": sorted(k for k in after if k not in before),
            "modified": sorted(k for k in after if k in before and after[k] != before[k]),
            "deleted": sorted(k for k in before if k not in after)}


# --------------------------------------------------------------------------- manifest / scope / files
def load_manifest(work: Path):
    """Parse DELIVERY.json (tolerates a BOM and a ```json fence). Returns (dict | None, error | None)."""
    p = Path(work) / MANIFEST
    if not p.exists():
        return None, f"{MANIFEST} was not written"
    text = p.read_text(encoding="utf-8-sig", errors="replace").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return None, f"{MANIFEST} is not valid JSON: {e}"
    if not isinstance(data, dict):
        return None, f"{MANIFEST} must be a JSON object"
    return data, None


def check_manifest(work: Path, unit: dict) -> dict:
    data, err = load_manifest(work)
    if err:
        return gate("manifest", [err])
    problems, warnings = [], []
    if data.get("unit") != unit["id"]:
        problems.append(f'"unit" is {data.get("unit")!r}, expected {unit["id"]!r}')
    files = data.get("files")
    if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
        problems.append('"files" must be a list of relative paths')
    else:
        got = {re.sub(r"^(\./)+", "", f.replace("\\", "/")) for f in files}
        want = set(unit["deliverables"])
        if want - got:
            problems.append('"files" is missing: ' + ", ".join(sorted(want - got)))
        if got - want:
            problems.append('"files" lists paths that are not deliverables: ' + ", ".join(sorted(got - want)))
    # the work order asks for unit, files and assumptions only (symbols / self_check cost output tokens and no gate
    # read them); extra keys from an older work order are ignored
    if "assumptions" not in data:
        warnings.append('"assumptions" missing')
    elif not isinstance(data["assumptions"], list):
        problems.append('"assumptions" must be a list')
    return gate("manifest", problems, warnings)


def check_scope(changes: dict, unit: dict, frozen: dict) -> dict:
    """changes: diff() of the work dir around the agy run. frozen: {path: sha256} of read-only files."""
    allowed = set(unit["deliverables"]) | {MANIFEST}
    problems = []
    for f in changes["created"]:
        if f not in allowed:
            problems.append(f"extra file written (not a deliverable): {f}")
    for f in changes["modified"] + changes["deleted"]:
        if f in frozen:
            problems.append(f"read-only file {'modified' if f in changes['modified'] else 'deleted'}: {f}")
        elif f not in allowed:
            problems.append(f"file outside the deliverables {'modified' if f in changes['modified'] else 'deleted'}: {f}")
    return gate("scope", problems)


def check_files(work: Path, unit: dict) -> dict:
    problems = []
    for f in unit["deliverables"]:
        p = Path(work) / f
        if not p.is_file():
            problems.append(f"deliverable not written: {f}")
        elif p.stat().st_size == 0 and not f.endswith("__init__.py"):
            problems.append(f"deliverable is empty: {f}")
    return gate("files", problems)


def check_syntax(work: Path, unit: dict) -> dict:
    problems = []
    for f in unit["deliverables"]:
        p = Path(work) / f
        if p.suffix == ".py" and p.is_file():
            try:
                ast.parse(p.read_text(encoding="utf-8"), filename=f)
            except SyntaxError as e:
                problems.append(f"{f}:{e.lineno}: syntax error: {e.msg}")
            except UnicodeDecodeError:
                problems.append(f"{f}: not UTF-8")
    return gate("syntax", problems)


# --------------------------------------------------------------------------- interface
def parse_signature(sig: str):
    """'def f(a, *, b=1) -> int' / 'async def ...' / 'class C(Base)' / 'NAME' -> ast node or None (constant)."""
    s = sig.strip().rstrip(":").strip()
    if s.startswith(("def ", "async def ")):
        return ast.parse(s + ": ...").body[0]
    if s.startswith("class "):
        return ast.parse(s + ": ...").body[0]
    return None


def param_list(fn) -> list:
    """[(name, kind, has_default)] with kind in posonly / pos / var / kwonly / varkw."""
    a = fn.args
    pos = list(a.posonlyargs) + list(a.args)
    first_default = len(pos) - len(a.defaults)
    out = [(arg.arg, "posonly" if i < len(a.posonlyargs) else "pos", i >= first_default) for i, arg in enumerate(pos)]
    if a.vararg:
        out.append(("*" + a.vararg.arg, "var", False))
    out += [(arg.arg, "kwonly", d is not None) for arg, d in zip(a.kwonlyargs, a.kw_defaults)]
    if a.kwarg:
        out.append(("**" + a.kwarg.arg, "varkw", False))
    return out


def fmt_params(params: list) -> str:
    parts, star_done = [], False
    for name, kind, dflt in params:
        if kind == "kwonly" and not star_done:
            parts.append("*")
            star_done = True
        if kind == "var":
            star_done = True
        parts.append(name + ("=…" if dflt else ""))
    return "(" + ", ".join(parts) + ")"


def find_symbol(tree, dotted: str):
    """Top-level def/class/assignment by name; 'Class.method' looks inside the class body."""
    scope = tree.body
    node = None
    for part in dotted.split("."):
        node = None
        for n in scope:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == part:
                node = n
            elif isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == part for t in n.targets):
                node = n
            elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id == part:
                node = n
        if node is None:
            return None
        scope = getattr(node, "body", [])
    return node


def check_interface(work: Path, unit: dict) -> dict:
    problems, warnings = [], []
    trees = {}
    for item in unit.get("interface", []):
        f, sym, sig = item["file"], item["symbol"], item.get("signature", "")
        p = Path(work) / f
        if not p.is_file():
            problems.append(f"{f}: missing (needed for {sym})")
            continue
        if p.suffix != ".py":
            if not re.search(r"\b" + re.escape(sym.split(".")[-1]) + r"\b", p.read_text(encoding="utf-8", errors="replace")):
                problems.append(f"{f}: symbol {sym} not found")
            continue
        if f not in trees:
            try:
                trees[f] = ast.parse(p.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                trees[f] = None
        tree = trees[f]
        if tree is None:
            continue  # reported by the syntax gate
        node = find_symbol(tree, sym)
        if node is None:
            problems.append(f"{f}: {sym} is not defined at the declared place")
            continue
        want = parse_signature(sig) if sig else None
        if isinstance(want, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                problems.append(f"{f}: {sym} must be a function, found {type(node).__name__}")
                continue
            if isinstance(want, ast.AsyncFunctionDef) != isinstance(node, ast.AsyncFunctionDef):
                problems.append(f"{f}: {sym} must {'' if isinstance(want, ast.AsyncFunctionDef) else 'not '}be async")
            wp, gp = param_list(want), param_list(node)
            if wp != gp:
                problems.append(f"{f}: {sym}{fmt_params(gp)} does not match the declared {sym}{fmt_params(wp)}")
            if want.returns is not None and node.returns is None:
                warnings.append(f"{f}: {sym} has no return annotation (declared {ast.unparse(want.returns)})")
        elif isinstance(want, ast.ClassDef):
            if not isinstance(node, ast.ClassDef):
                problems.append(f"{f}: {sym} must be a class, found {type(node).__name__}")
            else:
                wb = [ast.unparse(b) for b in want.bases]
                gb = [ast.unparse(b) for b in node.bases]
                missing = [b for b in wb if b not in gb]
                if missing:
                    problems.append(f"{f}: class {sym} must derive from {', '.join(missing)}")
        elif want is None and sig and not isinstance(node, (ast.Assign, ast.AnnAssign)):
            problems.append(f"{f}: {sym} must be a module-level constant")
    return gate("interface", problems, warnings)


# --------------------------------------------------------------------------- imports / forbid
STDLIB = set(getattr(sys, "stdlib_module_names", ())) | {"__future__"}


def local_modules(root: Path) -> set:
    """Top-level importable names in the assembled tree (packages and modules)."""
    names = set()
    for p in Path(root).iterdir() if Path(root).exists() else []:
        if p.name in IGNORE_DIRS or p.name.startswith("."):
            continue
        if p.is_dir():
            names.add(p.name)
        elif p.suffix == ".py":
            names.add(p.stem)
    return names


def check_imports(work: Path, unit: dict, policy, local=None) -> dict:
    """policy: None (no check) or {"stdlib": bool, "allow": [names]}. local: the project's top-level module names
    (default: what lies in work; outsource.py passes what the verify tree will hold)."""
    if not policy:
        return gate("imports")
    allowed = set(policy.get("allow", [])) | (local_modules(work) if local is None else set(local))
    if policy.get("stdlib", True):
        if not STDLIB:
            return gate("imports", warnings=["stdlib list needs Python 3.10+; import check skipped"])
        allowed |= STDLIB
    problems = []
    for f in unit["deliverables"]:
        p = Path(work) / f
        if p.suffix != ".py" or not p.is_file():
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for n in ast.walk(tree):
            mods = []
            if isinstance(n, ast.Import):
                mods = [a.name for a in n.names]
            elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
                mods = [n.module]
            for m in mods:
                if m.split(".")[0] not in allowed:
                    problems.append(f"{f}:{n.lineno}: import of {m} is not allowed")
    return gate("imports", problems)


def check_forbid(work: Path, unit: dict, patterns: list) -> dict:
    problems = []
    regs = [(p, re.compile(p)) for p in patterns or []]
    for f in unit["deliverables"]:
        p = Path(work) / f
        if not regs or not p.is_file():
            continue
        for ln, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            for src, rx in regs:
                if rx.search(line):
                    problems.append(f"{f}:{ln}: matches forbidden pattern {src!r}: {line.strip()[:120]}")
    return gate("forbid", problems)


# --------------------------------------------------------------------------- tamper
# The code under test runs in the same process as the tests, so it could read the calling test (frames, inspect,
# traceback), patch the runner (unittest, pytest, builtins), end the run early with a passing exit code, or hide
# any of that behind eval/exec. None of that implements a behaviour rule; a unit that really needs one of these
# names lists it in the plan's or the unit's "tamper_allow".
TAMPER_MODULES = {"unittest", "pytest", "_pytest", "doctest", "inspect", "traceback", "linecache", "gc", "ctypes",
                  "builtins", "atexit", "faulthandler", "importlib", "subprocess", "sitecustomize", "usercustomize",
                  "__main__"}  # __main__ is the test runner itself (mini_test.py, or pytest's entry module)
TAMPER_ATTRS = {"sys._getframe", "sys._current_frames", "sys.settrace", "sys.setprofile", "sys.modules", "sys.argv",
                "sys.exit", "sys.excepthook", "os._exit", "os.abort", "os.kill", "os.system", "os.popen",
                "operator.attrgetter", "operator.methodcaller"}
TAMPER_ATTR_PREFIXES = ("os.exec", "os.spawn", "os.posix_spawn")
# the attribute names of TAMPER_ATTRS, flagged on any base the gate cannot resolve to an imported module (x = os;
# x._exit(0), sys.modules["os"]._exit(0), (lambda: os)()._exit(0)). The exec/spawn families are spelled out: a bare
# "exec" prefix would also hit cursor.execute()
TAMPER_TAILS = ({a.split(".", 1)[1] for a in TAMPER_ATTRS}
                | {"execl", "execle", "execlp", "execlpe", "execv", "execve", "execvp", "execvpe", "spawnl", "spawnle",
                   "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe", "posix_spawn", "posix_spawnp"})
TAMPER_CALLS = {"eval", "exec", "compile", "__import__", "exit", "quit", "globals", "breakpoint"}
# exceptions that end the test process (or a runner that catches only Exception) before it reports
TAMPER_NAMES = {"SystemExit", "KeyboardInterrupt", "GeneratorExit", "BaseException"}
# reaching a name the gate checks by a detour: getattr(os, "_ex" + "it"), Exception.__base__, f.__globals__,
# os.__dict__["_exit"]; also passed as a value (functools.partial(getattr, os), map(getattr, ...))
ATTR_CALLS = {"getattr", "setattr", "delattr", "vars"}
TAMPER_DUNDERS = {"__base__", "__bases__", "__mro__", "__subclasses__", "__globals__", "__code__", "__closure__",
                  "__builtins__", "__loader__", "__spec__", "__getattribute__", "__dict__"}
# frames and tracebacks: from an exception it caught itself, code under test could walk up into the runner's frames
# and read its locals and globals (a caught exception's __traceback__.tb_frame.f_back.f_locals)
FRAME_ATTRS = {"__traceback__", "tb_frame", "tb_next", "f_back", "f_locals", "f_globals", "f_builtins", "f_code",
               "gi_frame", "cr_frame", "ag_frame"}
# the same names inside a string literal: str.format reaches attributes too ("{0.__traceback__.tb_frame}".format(e))
FRAME_TEXT = re.compile(r"(?<![A-Za-z0-9_])(" + "|".join(sorted(FRAME_ATTRS | TAMPER_DUNDERS)) + r")(?![A-Za-z0-9_])")
# matched against every string literal (Python) or line (other files): names of test files, test folders and
# the runner's environment, including the pieces a path is usually built from (Path(...) / "hidden")
TAMPER_TEXT = re.compile(r"PYTEST_CURRENT_TEST|DELIVERY\.json|conftest|_hidden|^test_|^(hidden|tests)$"
                         r"|(^|[\\/])(hidden|tests?)[\\/]|[\\/](hidden|tests?)$", re.I | re.M)


def tamper_rule(allow=()) -> str:
    """The tamper gate as one sentence for the work order (the contractor is told every rule it is checked on)."""
    allow = set(allow)
    mods = sorted(TAMPER_MODULES - allow)
    attrs = sorted(a for a in TAMPER_ATTRS if a not in allow) + [p + "*" for p in TAMPER_ATTR_PREFIXES if p not in allow]
    calls = sorted(TAMPER_CALLS - allow)
    names = sorted(TAMPER_NAMES - allow)
    dunders = sorted((TAMPER_DUNDERS | FRAME_ATTRS) - allow)
    tails = sorted(t for t in TAMPER_TAILS - allow if not t.startswith(("exec", "spawn", "posix_spawn")))
    attr_calls = sorted(ATTR_CALLS - allow)
    return ("The code must not look at or interfere with the tests that run it: do not import "
            + ", ".join(mods) + "; do not use " + ", ".join(attrs) + "; do not call " + ", ".join(f"{c}()" for c in calls)
            + (("; do not name " + ", ".join(names)) if names else "")
            + (("; do not use the attributes " + ", ".join(dunders) + ", not even inside a string") if dunders else "")
            + (("; do not use an attribute named " + ", ".join(tails) + " or exec*/spawn* on anything but a plainly "
                "imported module (where the rules above apply)") if tails else "")
            + (("; do not call " + "/".join(f"{c}()" for c in attr_calls)
                + " on an imported module or with an attribute name that is not a string literal, nor pass them "
                "around as values") if attr_calls else "")
            + "; and no string may name a test file or test folder.")


def check_tamper(work: Path, unit: dict, allow=()) -> dict:
    allow = set(allow)
    problems = []
    for f in unit["deliverables"]:
        p = Path(work) / f
        if not p.is_file():
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        if p.suffix != ".py":
            if "text" not in allow:
                problems += [f"{f}:{ln}: names a test file or folder ({m.group(0)!r})"
                             for ln, line in enumerate(text.splitlines(), 1) for m in [TAMPER_TEXT.search(line)] if m]
            continue
        try:
            tree = ast.parse(text)
        except (SyntaxError, UnicodeDecodeError):
            continue  # reported by the syntax gate
        if "text" not in allow:
            docs = {id(b[0].value) for b in [tree.body] + [n.body for n in ast.walk(tree) if isinstance(
                n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))] if b and isinstance(b[0], ast.Expr)}
            for n in ast.walk(tree):
                if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs:
                    m = TAMPER_TEXT.search(n.value)
                    if m:
                        problems.append(f"{f}:{n.lineno}: a string names a test file or folder ({n.value[:60]!r})")
        docs = {id(b[0].value) for b in [tree.body] + [n.body for n in ast.walk(tree) if isinstance(
            n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))] if b and isinstance(b[0], ast.Expr)}
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs:
                hits = sorted({m.group(1) for m in FRAME_TEXT.finditer(n.value)} - allow)
                if hits:
                    problems.append(f"{f}:{n.lineno}: a string names the attribute {', '.join(hits)} "
                                    f"(str.format can reach it)")
        called = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
        alias = {}  # local name -> module it stands for
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    alias[(a.asname or a.name).split(".")[0]] = a.name.split(".")[0] if not a.asname else a.name
            elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
                for a in n.names:
                    alias[a.asname or a.name] = f"{n.module}.{a.name}"
        for n in ast.walk(tree):
            problems += [f"{f}:{n.lineno}: {p}" for p in _tamper_detour(n, alias, allow, called)]
            used = []
            if isinstance(n, ast.Import):
                used = [a.name for a in n.names]
            elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
                used = [n.module] + [f"{n.module}.{a.name}" for a in n.names]
            elif isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id in alias:
                used = [f"{alias[n.value.id]}.{n.attr}"]
            elif isinstance(n, ast.Attribute) and n.attr in TAMPER_TAILS and n.attr not in allow:
                # the base is not a plainly imported module: it may still be one (x = os; sys.modules["os"])
                problems.append(f"{f}:{n.lineno}: uses .{n.attr} on an expression that may be a module")
            elif isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id not in alias:
                used = [n.func.id] if n.func.id in TAMPER_CALLS else []
            elif isinstance(n, ast.Name) and n.id == "__builtins__":
                used = ["__builtins__"]
            for u in used:
                hit = _tamper_hit(u, allow)
                if hit:
                    problems.append(f"{f}:{n.lineno}: uses {hit}")
    line = lambda s: (s.split(":")[0], int(s.split(":")[1]))  # noqa: E731
    return gate("tamper", sorted(set(problems), key=line))


def _tamper_detour(n, alias: dict, allow: set, called=frozenset()) -> list:
    """Process-ending exceptions, class-hierarchy dunders, frame and traceback attributes, and getattr-style calls
    that would reach a checked name without spelling it out (getattr(os, "_ex" + "it")(0) is os._exit(0),
    os.__dict__["_exit"] too). called: id() of every Call's func node (getattr passed as a value is a detour)."""
    if isinstance(n, ast.Name) and n.id in TAMPER_NAMES and n.id not in allow:
        return [f"uses {n.id}"]
    if isinstance(n, ast.Attribute) and n.attr in (TAMPER_DUNDERS | FRAME_ATTRS) and n.attr not in allow:
        return [f"uses {n.attr}"]
    if (isinstance(n, ast.Name) and n.id in ATTR_CALLS and n.id not in alias and n.id not in allow
            and id(n) not in called and isinstance(n.ctx, ast.Load)):
        return [f"passes {n.id} around as a value"]
    if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ATTR_CALLS
            and n.func.id not in alias and n.func.id not in allow):
        target = n.args[0] if n.args else None
        if isinstance(target, ast.Name) and target.id in alias:
            return [f"calls {n.func.id}() on the imported {alias[target.id]}"]
        if n.func.id == "vars":  # vars(x) is x.__dict__; vars() alone is the caller's own locals
            return ["calls vars() on an object (that is its __dict__)"] if n.args or n.keywords else []
        if len(n.args) < 2 or not (isinstance(n.args[1], ast.Constant) and isinstance(n.args[1].value, str)):
            return [f"calls {n.func.id}() with an attribute name that is not a string literal"]
        name = n.args[1].value
        if name in (TAMPER_DUNDERS | FRAME_ATTRS | TAMPER_TAILS) and name not in allow:
            return [f"calls {n.func.id}() for {name}"]
    return []


def _tamper_hit(name: str, allow: set):
    top = name.split(".")[0]
    if top in TAMPER_MODULES and top not in allow and name not in allow:
        return top
    if name in TAMPER_ATTRS and name not in allow:
        return name
    if name.startswith(TAMPER_ATTR_PREFIXES) and not any(name.startswith(p) and p in allow for p in TAMPER_ATTR_PREFIXES):
        return name
    if name in TAMPER_CALLS | {"__builtins__"} and name not in allow:
        return name + ("()" if name in TAMPER_CALLS else "")
    return None


def static_gates(work: Path, unit: dict, changes: dict, frozen: dict, plan: dict, local=None) -> list:
    forbid = list(plan.get("forbid_patterns", [])) + list(unit.get("forbid_patterns", []))
    policy = unit.get("imports", plan.get("imports"))
    res = [check_manifest(work, unit), check_scope(changes, unit, frozen), check_files(work, unit),
           check_syntax(work, unit), check_interface(work, unit), check_imports(work, unit, policy, local),
           check_forbid(work, unit, forbid)]
    if plan.get("language", "python") == "python":
        res.append(check_tamper(work, unit, tamper_allow(plan, unit)))
    return res


def tamper_allow(plan: dict, unit: dict) -> list:
    return list(plan.get("tamper_allow", [])) + list(unit.get("tamper_allow", []))


# --------------------------------------------------------------------------- stubs (red check)
def stub_sources(units: list) -> dict:
    """{path: python source} with every declared Python symbol raising NotImplementedError."""
    by_file = {}
    for u in units:
        for f in u["deliverables"]:
            if f.endswith(".py"):
                by_file.setdefault(f, [])
        for item in u.get("interface", []):
            if item["file"].endswith(".py"):
                by_file.setdefault(item["file"], []).append(item)
    out = {}
    for f, items in by_file.items():
        lines = ["from __future__ import annotations", ""]
        classes = {}
        for it in items:
            parts = it["symbol"].split(".")
            if len(parts) == 2:
                classes.setdefault(parts[0], []).append(it)
        done_classes = set()
        for it in items:
            parts = it["symbol"].split(".")
            sig = (it.get("signature") or "").strip().rstrip(":")
            node = parse_signature(sig) if sig else None
            if len(parts) == 2:
                if parts[0] not in done_classes:
                    lines += [f"class {parts[0]}:", "    pass", ""]
                    done_classes.add(parts[0])
                continue
            if isinstance(node, ast.ClassDef):
                lines.append(f"class {node.name}:")
                for m in classes.get(node.name, []):
                    lines += ["    " + m["signature"].strip().rstrip(":") + ":", "        raise NotImplementedError"]
                lines += ["    pass", ""]
                done_classes.add(node.name)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                lines += [sig + ":", "    raise NotImplementedError", ""]
            else:
                lines += [f"{parts[0]} = None", ""]
        # methods of classes that were not declared themselves
        text = "\n".join(lines)
        for cname, methods in classes.items():
            body = "".join("    " + m["signature"].strip().rstrip(":") + ":\n        raise NotImplementedError\n" for m in methods)
            text = text.replace(f"class {cname}:\n    pass\n", f"class {cname}:\n{body}", 1)
        out[f] = text + "\n"
    return out

"""sandbox.py - keeps every test run away from the real Gemini desktop app.

Import this FIRST in every test module (effective on import, idempotent):
  * LOCALAPPDATA / APPDATA point at fresh temp dirs (real values kept in AGY_DR_REAL_*),
    AGY_DR_TEST_SANDBOX=1; child processes inherit all of it. The temp dirs are removed at exit
    (best effort) by the process that created them.
  * subprocess.Popen.__init__ (run/call/check_output), os.system, os.startfile, os.spawn*
    and os.exec* refuse (SandboxViolation) anything that names the real
    %LOCALAPPDATA%\\Google\\Gemini (also as cwd), Gemini.exe, the patcher, a browser, a
    remote-debugging port, or a Start-Process/start command pointing at Google\\Gemini. The check
    runs BEFORE the original call, so no process ever starts.
  * Write-type file operations (open in w/a/x/+ mode, os.replace/rename/remove/unlink/makedirs/mkdir/...,
    shutil.copy*/move/rmtree, pathlib write/touch/unlink/mkdir/rename/replace) refuse any target
    under the real Gemini dir or the real %APPDATA%\\gemini. Reads stay allowed.
  * Child processes: a temp `sitecustomize.py` that imports this module is put first on PYTHONPATH
    (together with this directory), so every Python child and grandchild is guarded too. Non-Python
    children (and `python -I` / `-S`, which ignore PYTHONPATH / site) only get the environment
    redirect, not the guard.
"""
from __future__ import annotations

import atexit
import builtins
import functools
import io
import os
import pathlib
import re
import shutil
import subprocess
import tempfile

_DONE_FLAG = "AGY_DR_TEST_SANDBOX"
_SHIM_VAR = "AGY_DR_SHIM_DIR"
_MARK = "agy-dr-sandbox-"
_orig_rmtree = shutil.rmtree


class SandboxViolation(RuntimeError):
    """A test tried to reach the real Gemini app, the patcher, a browser or a debug port."""


def _norm(text: str) -> str:
    return text.replace("/", "\\").lower()


def real_gemini_dir() -> str:
    real = os.environ.get("AGY_DR_REAL_LOCALAPPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(real, "Google", "Gemini")


def _real_appdata_gemini() -> str:
    real = os.environ.get("AGY_DR_REAL_APPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Roaming")
    return os.path.join(real, "gemini")


def _variants(path: str) -> set:
    """Lower-cased backslash spellings of a path: raw, absolute and (if it exists) real."""
    out = {_norm(path)}
    try:
        out.add(_norm(os.path.abspath(path)))
        if os.path.exists(path):
            out.add(_norm(os.path.realpath(path)))
    except (OSError, ValueError):
        pass
    return out


def _needles(extra: bool = False) -> set:
    dirs = [real_gemini_dir(), os.path.join(os.path.expanduser("~"), "AppData", "Local", "Google", "Gemini")]
    if extra:
        dirs += [_real_appdata_gemini(), os.path.join(os.path.expanduser("~"), "AppData", "Roaming", "gemini")]
    out = set()
    for d in dirs:
        out |= _variants(d)
    return {n.rstrip("\\") for n in out if n}


def _under(path: str, needles: set) -> bool:
    return any(v == n or v.startswith(n + "\\") for v in _variants(path) for n in needles)


_BAD_NAMES = ("gemini.exe", "patch_gemini_app.py", "chrome.exe", "msedge.exe")
_BAD_STEMS = {"chrome", "msedge"}
_TOKEN_SPLIT = re.compile(r"[\s\"'=;,]+")
_START_APP = re.compile(r"\bstart(-process)?\b.*google\\gemini")


def _reason(item: str, is_command: bool = False):
    low = _norm(item)
    if "--remote-debugging-port" in low:
        return "remote debugging port"
    needles = _needles()
    for v in _variants(item):
        if any(n in v for n in needles):
            return "real Gemini app directory"
    if _START_APP.search(low):
        return "start command for the Gemini app"
    for name in _BAD_NAMES:
        if name in low:
            return name
    if is_command:  # bare `chrome` / `msedge` as the program, not as an argument value
        toks = [t for t in _TOKEN_SPLIT.split(low) if t]
        if toks and toks[0].rsplit("\\", 1)[-1] in _BAD_STEMS:
            return "browser"
    return None


def _as_text(item):
    if isinstance(item, bytes):
        item = os.fsdecode(item)
    if isinstance(item, os.PathLike):
        item = os.fspath(item)
        if isinstance(item, bytes):
            item = os.fsdecode(item)
    return item if isinstance(item, str) else None


def _check(parts, cwd=None) -> None:
    for idx, raw in enumerate(parts):
        item = _as_text(raw)
        if item is None:
            continue
        why = _reason(item, idx == 0)
        if why:
            raise SandboxViolation(f"test sandbox refused: {why}: {item!r}")
    c = _as_text(cwd)
    if c and _under(c, _needles()):
        raise SandboxViolation(f"test sandbox refused: cwd under the real Gemini app: {c!r}")


def _check_write(paths) -> None:
    needles = None
    for raw in paths:
        item = _as_text(raw)
        if not item:
            continue
        if needles is None:
            needles = _needles(extra=True)
        if _under(item, needles):
            raise SandboxViolation(f"test sandbox refused write to the real Gemini app data: {item!r}")


def _flatten(args):
    for a in args:
        if isinstance(a, (list, tuple)):
            yield from a
        elif not isinstance(a, (dict, int)):
            yield a


def _mark(fn):
    fn._agy_sandbox = True
    return fn


def _wrap_write(owner, name, positions, kwnames=("path", "src", "dst", "target", "file")) -> None:
    orig = getattr(owner, name, None)
    if orig is None or getattr(orig, "_agy_sandbox", False):
        return

    @functools.wraps(orig)
    def guarded(*a, **kw):
        paths = [a[i] for i in positions if i < len(a)] + [v for k, v in kw.items() if k in kwnames]
        _check_write(paths)
        return orig(*a, **kw)

    setattr(owner, name, _mark(guarded))


_WRITE_FLAGS = (getattr(os, "O_WRONLY", 1) | getattr(os, "O_RDWR", 2) | getattr(os, "O_CREAT", 0)
                | getattr(os, "O_APPEND", 0) | getattr(os, "O_TRUNC", 0))


def _install_write_guards() -> None:
    orig_open = builtins.open
    if not getattr(orig_open, "_agy_sandbox", False):
        @functools.wraps(orig_open)
        def guarded_open(file, mode="r", *a, **kw):
            if isinstance(mode, str) and any(c in mode for c in "wax+"):
                _check_write([file])
            return orig_open(file, mode, *a, **kw)

        builtins.open = io.open = _mark(guarded_open)

    orig_os_open = os.open
    if not getattr(orig_os_open, "_agy_sandbox", False):
        @functools.wraps(orig_os_open)
        def guarded_os_open(path, flags, *a, **kw):
            if flags & _WRITE_FLAGS:
                _check_write([path])
            return orig_os_open(path, flags, *a, **kw)

        os.open = _mark(guarded_os_open)

    for name, pos in (("replace", (0, 1)), ("rename", (0, 1)), ("remove", (0,)), ("unlink", (0,)),
                      ("makedirs", (0,)), ("mkdir", (0,)), ("rmdir", (0,)), ("removedirs", (0,)),
                      ("symlink", (1,)), ("link", (1,)), ("truncate", (0,))):
        _wrap_write(os, name, pos)
    for name, pos in (("copy", (1,)), ("copy2", (1,)), ("copyfile", (1,)), ("copytree", (1,)),
                      ("move", (0, 1)), ("rmtree", (0,))):
        _wrap_write(shutil, name, pos)
    for name, pos in (("write_text", (0,)), ("write_bytes", (0,)), ("touch", (0,)), ("unlink", (0,)),
                      ("mkdir", (0,)), ("rmdir", (0,)), ("rename", (0, 1)), ("replace", (0, 1))):
        _wrap_write(pathlib.Path, name, pos)


def _install_exec_guards() -> None:
    if getattr(subprocess.Popen.__init__, "_agy_sandbox", False):
        return
    orig_init = subprocess.Popen.__init__

    def guarded_init(self, args, *a, **kw):
        parts = [args] if isinstance(args, (str, bytes, os.PathLike)) else list(args)
        executable = kw.get("executable", a[1] if len(a) > 1 else None)
        if executable is not None:
            parts.append(executable)
        _check(parts, kw.get("cwd", a[8] if len(a) > 8 else None))
        return orig_init(self, args, *a, **kw)

    subprocess.Popen.__init__ = _mark(guarded_init)

    orig_system = os.system

    def guarded_system(command):
        _check([command])
        return orig_system(command)

    os.system = _mark(guarded_system)

    if hasattr(os, "startfile"):
        orig_startfile = os.startfile

        def guarded_startfile(path, *a, **kw):
            _check([path, *[x for x in a if isinstance(x, str)]])
            return orig_startfile(path, *a, **kw)

        os.startfile = _mark(guarded_startfile)

    for name in ("spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe",
                 "execl", "execle", "execlp", "execlpe", "execv", "execve", "execvp", "execvpe"):
        orig = getattr(os, name, None)
        if orig is None:
            continue

        def make(orig):
            @functools.wraps(orig)
            def guarded(*a, **kw):
                _check(list(_flatten(a)))
                return orig(*a, **kw)
            return _mark(guarded)

        setattr(os, name, make(orig))


def _make_shim() -> None:
    """Temp sitecustomize.py so every Python child/grandchild imports this guard."""
    shim = tempfile.mkdtemp(prefix="agy-dr-shim-")
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(shim, "sitecustomize.py"), "w", encoding="utf-8") as fh:
        fh.write("try:\n    import sandbox  # noqa: F401\nexcept ImportError:\n    pass\n")
    os.environ[_SHIM_VAR] = shim
    old = os.environ.get("PYTHONPATH")
    os.environ["PYTHONPATH"] = os.pathsep.join([shim, here] + ([old] if old else []))
    atexit.register(_orig_rmtree, shim, True)


def install() -> None:
    """Idempotent: env redirect, temp dirs, child shim, and all runtime guards."""
    for var in ("LOCALAPPDATA", "APPDATA"):
        real = "AGY_DR_REAL_" + var
        if real not in os.environ:
            os.environ[real] = os.environ.get(var, "")
    if os.environ.get(_DONE_FLAG) != "1" or _MARK not in os.environ.get("LOCALAPPDATA", ""):
        for var in ("LOCALAPPDATA", "APPDATA"):
            d = tempfile.mkdtemp(prefix=_MARK)
            os.environ[var] = d
            atexit.register(_orig_rmtree, d, True)
    os.environ[_DONE_FLAG] = "1"
    if _SHIM_VAR not in os.environ:
        _make_shim()
    _install_exec_guards()
    _install_write_guards()


install()

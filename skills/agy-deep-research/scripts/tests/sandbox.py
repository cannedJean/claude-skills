"""sandbox.py - keeps every test run away from the real Gemini desktop app.

Import this FIRST in every test module (effective on import, idempotent):
  * LOCALAPPDATA / APPDATA point at fresh temp dirs (real values kept in AGY_DR_REAL_*),
    AGY_DR_TEST_SANDBOX=1; child processes inherit all of it.
  * subprocess.Popen.__init__, os.system and os.startfile refuse (SandboxViolation) anything that
    names the real %LOCALAPPDATA%\\Google\\Gemini, Gemini.exe, the patcher, a browser, or a
    remote-debugging port. The check runs BEFORE the original call, so no process ever starts.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile

_DONE_FLAG = "AGY_DR_TEST_SANDBOX"


class SandboxViolation(RuntimeError):
    """A test tried to reach the real Gemini app, the patcher, a browser or a debug port."""


def _norm(text: str) -> str:
    return text.replace("/", "\\").lower()


def real_gemini_dir() -> str:
    real = os.environ.get("AGY_DR_REAL_LOCALAPPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(real, "Google", "Gemini")


_BAD_NAMES = ("gemini.exe", "patch_gemini_app.py", "chrome.exe", "msedge.exe")
_BAD_STEMS = {"chrome", "msedge"}
_TOKEN_SPLIT = re.compile(r"[\s\"'=;,]+")


def _reason(item: str, is_command: bool = False):
    low = _norm(item)
    if "--remote-debugging-port" in low:
        return "remote debugging port"
    if _norm(real_gemini_dir()) in low:
        return "real Gemini app directory"
    for name in _BAD_NAMES:
        if name in low:
            return name
    if is_command:  # bare `chrome` / `msedge` as the program, not as an argument value
        toks = [t for t in _TOKEN_SPLIT.split(low) if t]
        if toks and toks[0].rsplit("\\", 1)[-1] in _BAD_STEMS:
            return "browser"
    return None


def _check(parts) -> None:
    for idx, item in enumerate(parts):
        if isinstance(item, bytes):
            item = os.fsdecode(item)
        if not isinstance(item, (str, os.PathLike)):
            continue
        why = _reason(os.fspath(item) if isinstance(item, os.PathLike) else item, idx == 0)
        if why:
            raise SandboxViolation(f"test sandbox refused: {why}: {os.fspath(item)!r}")


def _install() -> None:
    for var in ("LOCALAPPDATA", "APPDATA"):
        real = "AGY_DR_REAL_" + var
        if real not in os.environ:
            os.environ[real] = os.environ.get(var, "")
    if os.environ.get(_DONE_FLAG) != "1" or "agy-dr-sandbox-" not in os.environ.get("LOCALAPPDATA", ""):
        os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="agy-dr-sandbox-")
        os.environ["APPDATA"] = tempfile.mkdtemp(prefix="agy-dr-sandbox-")
    os.environ[_DONE_FLAG] = "1"

    if getattr(subprocess.Popen.__init__, "_agy_sandbox", False):
        return

    orig_init = subprocess.Popen.__init__

    def guarded_init(self, args, *a, **kw):
        parts = [args] if isinstance(args, (str, bytes, os.PathLike)) else list(args)
        if kw.get("executable") is not None:
            parts.append(kw["executable"])
        _check(parts)
        return orig_init(self, args, *a, **kw)

    guarded_init._agy_sandbox = True
    subprocess.Popen.__init__ = guarded_init

    orig_system = os.system

    def guarded_system(command):
        _check([command])
        return orig_system(command)

    guarded_system._agy_sandbox = True
    os.system = guarded_system

    if hasattr(os, "startfile"):
        orig_startfile = os.startfile

        def guarded_startfile(path, *a, **kw):
            _check([path, *[x for x in a if isinstance(x, str)]])
            return orig_startfile(path, *a, **kw)

        guarded_startfile._agy_sandbox = True
        os.startfile = guarded_startfile


_install()

# One module object whatever the import spelling (`import sandbox` / `from tests import sandbox`).
for _alias in ("sandbox", "tests.sandbox"):
    sys.modules.setdefault(_alias, sys.modules[__name__])

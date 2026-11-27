#!/usr/bin/env python3
"""gemini_host.py - pure host decisions for the Gemini desktop UI Automation driver.

No Win32 calls and no process launching here: only the decisions (launch command guard, instance identity,
launch classification, mode choice). Windows path rules are applied through ntpath so behaviour is the same on any OS.
"""
from __future__ import annotations

import ntpath
import re
import subprocess
from dataclasses import dataclass
from typing import Literal, Mapping, Optional, Sequence

DESKTOP_NAME = "agy-deep-research"
ALLOWED_FLAG_SWITCHES = (
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-features=CalculateNativeWinOcclusion",
)
START_TIMEOUT_S = 60.0


def _validate_profile_dir(profile_dir: str) -> None:
    if not profile_dir:
        raise ValueError("profile_dir is empty")
    if '"' in profile_dir or any(ord(ch) < 32 or ord(ch) == 127 for ch in profile_dir):
        raise ValueError("profile_dir contains a quote or control character")
    drive, rest = ntpath.splitdrive(profile_dir)
    if not drive or not rest.startswith(("\\", "/")):
        raise ValueError("profile_dir must be absolute (drive or UNC)")
    canon = _canon(profile_dir)
    if "\\google\\gemini\\" in canon + "\\":
        raise ValueError("profile_dir must not be under a Google\\Gemini directory")


def build_launch_command(stub: str, profile_dir: Optional[str], flags: Sequence[str]) -> str:
    """Command line for the launcher stub.

    D1 (profile_dir None) is the bare stub: no --user-data-dir and no flags at all.
    D2 adds --user-data-dir plus the allowed throttle switches.
    """
    for f in flags:
        if f not in ALLOWED_FLAG_SWITCHES:
            raise ValueError(f"switch not allowed: {f}")
    args = [stub]
    if profile_dir is None:
        if flags:
            raise ValueError("D1 launches the bare stub: no switches allowed")
    else:
        _validate_profile_dir(profile_dir)
        args.append(f"--user-data-dir={profile_dir}")
        args.extend(flags)
    return subprocess.list2cmdline(args)


@dataclass
class ProcInfo:
    pid: int
    ppid: int
    image: str
    cmdline: str
    created: float


def _split_args(cmdline: str) -> list:
    """CommandLineToArgvW-style splitting (backslash/quote rules)."""
    args: list = []
    cur: list = []
    started = False
    in_q = False
    i, n = 0, len(cmdline)
    while i < n:
        ch = cmdline[i]
        if ch == "\\":
            j = i
            while j < n and cmdline[j] == "\\":
                j += 1
            k = j - i
            if j < n and cmdline[j] == '"':
                cur.append("\\" * (k // 2))
                if k % 2:
                    cur.append('"')
                    j += 1
                started = True
                i = j
                continue
            cur.append("\\" * k)
            started = True
            i = j
            continue
        if ch == '"':
            in_q = not in_q
            started = True
        elif ch in (" ", "\t") and not in_q:
            if started:
                args.append("".join(cur))
                cur, started = [], False
        else:
            cur.append(ch)
            started = True
        i += 1
    if started:
        args.append("".join(cur))
    return args


def parse_user_data_dir(cmdline: str) -> Optional[str]:
    """The --user-data-dir=<value> (last one wins, like Chromium; switch name case-insensitive), or None."""
    found = None
    for a in _split_args(cmdline or "")[1:]:
        if a.lower().startswith("--user-data-dir="):
            found = a[len("--user-data-dir="):]
    return found or None


def _canon(path: str) -> str:
    return ntpath.normcase(ntpath.normpath(path))


_GEMINI_IMAGE = re.compile(r"(?:^|\\)google\\gemini\\app-[^\\]+\\gemini\.exe$")


def is_gemini_image(image: str) -> bool:
    return bool(_GEMINI_IMAGE.search(_canon(image or "")))


def is_our_instance(p: ProcInfo, profile_dir: str, started_after: Optional[float] = None) -> bool:
    if not is_gemini_image(p.image):
        return False
    udd = parse_user_data_dir(p.cmdline)
    if udd is None or _canon(udd) != _canon(profile_dir):
        return False
    if started_after is not None and p.created < started_after:
        return False
    return True


@dataclass
class LaunchCheck:
    """Observations of a launch. `exited` means our app (browser) process exited - never the launcher stub,
    which normally exits after handing off."""
    window_on_our_desktop: bool
    cmdline_has_profile: bool
    local_state_updated: bool
    exited: bool
    elapsed_s: float


def classify_launch(c: LaunchCheck) -> Literal["ok", "blocked", "pending", "timeout"]:
    """`exited` (our app process, not the stub) takes precedence over everything else."""
    if c.exited:
        return "blocked"
    if c.window_on_our_desktop and not c.cmdline_has_profile:
        return "blocked"
    if c.window_on_our_desktop and c.cmdline_has_profile and c.local_state_updated:
        return "ok"
    if c.elapsed_s < START_TIMEOUT_S:
        return "pending"
    return "timeout"


@dataclass
class Decision:
    mode: Optional[Literal["D2", "D1"]]
    code: Optional[str]
    reason: Optional[str]


def decide_mode(job_mode: Optional[str], d2: Literal["ok", "blocked", "timeout"],
                user_gemini_running: bool) -> Decision:
    if job_mode not in (None, "D2", "D1"):
        raise ValueError(f"unknown job mode: {job_mode}")
    if d2 not in ("ok", "blocked", "timeout"):
        raise ValueError(f"unknown d2 state: {d2}")
    if job_mode == "D2":
        if d2 == "ok":
            return Decision("D2", None, None)
        if d2 == "timeout":
            return Decision(None, "HOST_START_TIMEOUT", "Gemini did not start in time")
        return Decision(None, "HOST_MODE_UNAVAILABLE", f"job is pinned to D2 but the profile launch is {d2}")
    if job_mode == "D1":
        if user_gemini_running:
            return Decision(None, "GEMINI_IN_USE", "job is pinned to D1 but a Gemini process of the user is running")
        return Decision("D1", None, None)
    if d2 == "ok":
        return Decision("D2", None, None)
    if d2 == "blocked":
        if not user_gemini_running:
            return Decision("D1", None, "profile_switch_ignored")
        return Decision(None, "GEMINI_IN_USE", "the profile switch is ignored and a Gemini process of the user is running")
    return Decision(None, "HOST_START_TIMEOUT", "Gemini did not start in time")


def clean_env(env: Mapping[str, str]) -> dict:
    return {k: v for k, v in env.items() if not k.upper().startswith(("NODE_", "ELECTRON_"))}

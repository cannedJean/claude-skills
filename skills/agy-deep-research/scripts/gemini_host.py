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


def build_launch_command(stub: str, profile_dir: Optional[str], flags: Sequence[str]) -> str:
    """Command line for the launcher stub. profile_dir None (D1) adds no --user-data-dir."""
    for f in flags:
        if f not in ALLOWED_FLAG_SWITCHES:
            raise ValueError(f"switch not allowed: {f}")
    args = [stub]
    if profile_dir is not None:
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


_UDD_QUOTED_VALUE = re.compile(r'(?:^|\s)--user-data-dir=(?:"([^"]*)"|([^\s"]*))')
_UDD_QUOTED_ARG = re.compile(r'(?:^|\s)"--user-data-dir=([^"]*)"')


def parse_user_data_dir(cmdline: str) -> Optional[str]:
    """The --user-data-dir value (last one wins, like Chromium), or None."""
    found: list[tuple[int, str]] = []
    for m in _UDD_QUOTED_VALUE.finditer(cmdline or ""):
        found.append((m.start(), m.group(1) if m.group(1) is not None else m.group(2)))
    for m in _UDD_QUOTED_ARG.finditer(cmdline or ""):
        found.append((m.start(), m.group(1)))
    if not found:
        return None
    return max(found)[1]


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
    window_on_our_desktop: bool
    cmdline_has_profile: bool
    local_state_updated: bool
    exited: bool
    elapsed_s: float


def classify_launch(c: LaunchCheck) -> Literal["ok", "blocked", "pending", "timeout"]:
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
    if job_mode == "D2":
        if d2 == "ok":
            return Decision("D2", None, None)
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
    return {k: v for k, v in env.items() if not k.startswith(("NODE_", "ELECTRON_"))}

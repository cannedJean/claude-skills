#!/usr/bin/env python3
r"""gemini_host.py - host for the Gemini desktop UI Automation driver.

Pure half (no Win32 calls): the decisions - launch command guard, instance identity, launch classification, mode
choice. Windows path rules are applied through ntpath so behaviour is the same on any OS.

Validation is deliberately stricter than the minimal spec (fail closed):
- the launcher stub must be non-empty, with no quote or control character;
- profile_dir must be absolute with a drive or UNC root, with no quote, control character or ':' after the drive
  (no alternate data streams), and must not lie under the app's data directory (a `Google` path component directly
  followed by a `Gemini` one) anywhere in the path (path components are compared the way Windows resolves them:
  case-insensitively, `..` collapsed, trailing dots and spaces ignored, so `Gemini.` / `Gemini ` count as `Gemini`);
- is_gemini_image needs an absolute image path whose last four components are `Google`, `Gemini`, `app-<ver>` and
  `Gemini.exe`;
- an empty profile_dir is never "our" instance.
(No app-directory path literal at module level: see is_gemini_image.)

Win32 half: `HostOS` (the injectable OS surface), `Win32HostOS` (ctypes, loaded lazily; refuses every call with
HostRefused("sandbox") when AGY_DR_TEST_SANDBOX=1) and `Host` (ensure / instances / close / lock), which talks only
to a HostOS so all of its logic is tested with a fake. Measured facts it relies on: the hidden desktop lives in
WinSta0 and survives its creator while the app runs, and vanishes when the app exits; the launcher stub exits right
after handing off, so only the app's browser process counts; a slow start can exceed 15 s; a cross-desktop
WM_CLOSE fails (error 1400), so closing is the app menu from a thread on that desktop (injected `quit_via_menu`),
then a same-desktop WM_CLOSE, then TerminateProcess of our verified process tree only.
Host never acts on the user's Gemini: "user running" is re-read right before a D1 launch, a Gemini.exe it cannot
inspect counts as the user's, and anything we launched is closed again before a refusal. State lives in
%LOCALAPPDATA%\agy-deep-research\host.json (written atomically; never under the app's directory).
"""
from __future__ import annotations

import contextlib
import json
import ntpath
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, Iterator, List, Literal, Mapping, Optional, Protocol, Sequence, Tuple, Union

DESKTOP_NAME = "agy-deep-research"
ALLOWED_FLAG_SWITCHES = (
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-features=CalculateNativeWinOcclusion",
)
START_TIMEOUT_S = 60.0


def _bad_chars(text: str) -> bool:
    return '"' in text or any(ord(ch) < 32 or ord(ch) == 127 for ch in text)


def _validate_stub(stub: str) -> None:
    if not isinstance(stub, str) or not stub.strip():
        raise ValueError("launcher stub is empty")
    if _bad_chars(stub):
        raise ValueError("launcher stub contains a quote or control character")


def _validate_profile_dir(profile_dir: str) -> None:
    if not profile_dir:
        raise ValueError("profile_dir is empty")
    if _bad_chars(profile_dir):
        raise ValueError("profile_dir contains a quote or control character")
    drive, rest = ntpath.splitdrive(profile_dir)
    if not drive or not rest.startswith(("\\", "/")):
        raise ValueError("profile_dir must be absolute (drive or UNC)")
    if ":" in rest:
        raise ValueError("profile_dir must not contain ':' after the drive (alternate data stream)")
    # Windows drops trailing dots and spaces of a path component: "Gemini." and "Gemini " name "Gemini"
    parts = [c.rstrip(". ") for c in _canon(profile_dir).split("\\")]
    if any(a == "google" and b == "gemini" for a, b in zip(parts, parts[1:])):
        raise ValueError("profile_dir must not be under a Google\\Gemini directory")


def build_launch_command(stub: str, profile_dir: Optional[str], flags: Sequence[str]) -> str:
    """Command line for the launcher stub.

    D1 (profile_dir None) is the bare stub: no --user-data-dir and no flags at all.
    D2 adds --user-data-dir plus the allowed throttle switches.
    Raises ValueError for an unlisted switch, flags with D1, an empty stub or one with a quote/control character,
    and a profile_dir that fails the strict checks in the module docstring.
    """
    _validate_stub(stub)
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
    # False: a Gemini.exe process whose image, creation time or command line could not be read. Host treats it
    # as the user's (fails closed): never ours, never terminated, and it makes D1 refuse.
    inspected: bool = True


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
    """The --user-data-dir=<value> (last one wins, like Chromium; switch name case-insensitive), or None.

    `cmdline` is a whole process command line: token 0 is the program and is never read as a switch."""
    found = None
    for a in _split_args(cmdline or "")[1:]:
        if a.lower().startswith("--user-data-dir="):
            found = a[len("--user-data-dir="):]
    return found or None


def _canon(path: str) -> str:
    return ntpath.normcase(ntpath.normpath(path))


def is_gemini_image(image: str) -> bool:
    """Absolute (drive or UNC) image path ending exactly in \\Google\\Gemini\\app-<ver>\\Gemini.exe."""
    # The pattern stays inside this function: a module-level app-directory literal would put every write call of
    # this module (the host lock file) in the scope of run_verification's no-app-dir-writes check.
    image = image or ""
    drive, rest = ntpath.splitdrive(image)
    if not drive or not rest.startswith(("\\", "/")) or _bad_chars(image):
        return False
    return bool(re.search(r"\\google\\gemini\\app-[^\\]+\\gemini\.exe\Z", _canon(image)))


def is_our_instance(p: ProcInfo, profile_dir: str, started_after: Optional[float] = None) -> bool:
    if not profile_dir:  # "" would canonicalise to "." and match --user-data-dir=.
        return False
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


def decide_mode(job_mode: Optional[str], d2: Optional[Literal["ok", "blocked", "timeout"]],
                user_gemini_running: bool) -> Decision:
    """d2 may be None only for a job pinned to D1 (the D2 launch state is irrelevant there and is ignored)."""
    if job_mode not in (None, "D2", "D1"):
        raise ValueError(f"unknown job mode: {job_mode}")
    if d2 not in ("ok", "blocked", "timeout") and not (job_mode == "D1" and d2 is None):
        raise ValueError(f"unknown d2 state: {d2}")
    if job_mode == "D2":
        if d2 == "ok":
            return Decision("D2", None, None)
        if d2 == "timeout":
            return Decision(None, "HOST_START_TIMEOUT", "Gemini did not start in time")
        return Decision(None, "HOST_MODE_UNAVAILABLE", f"job is pinned to D2 but the dedicated data-dir launch is {d2}")
    if job_mode == "D1":
        if user_gemini_running:
            return Decision(None, "GEMINI_IN_USE", "job is pinned to D1 but a Gemini process of the user is running")
        return Decision("D1", None, None)
    if d2 == "ok":
        return Decision("D2", None, None)
    if d2 == "blocked":
        if not user_gemini_running:
            return Decision("D1", None, "data_dir_switch_ignored")
        return Decision(None, "GEMINI_IN_USE", "the data-dir switch is ignored and a Gemini process of the user is running")
    return Decision(None, "HOST_START_TIMEOUT", "Gemini did not start in time")


def clean_env(env: Mapping[str, str]) -> dict:
    return {k: v for k, v in env.items() if not k.upper().startswith(("NODE_", "ELECTRON_"))}


# ---------------------------------------------------------------------------------------------------------------------
# Win32 half: HostOS surface, Host orchestration, Win32HostOS (ctypes)
# ---------------------------------------------------------------------------------------------------------------------

SANDBOX_VAR = "AGY_DR_TEST_SANDBOX"
WINDOW_STATION = "WinSta0"
MAIN_WINDOW_CLASS = "Chrome_WidgetWin_1"
LAUNCH_SLACK_S = 2.0  # a browser process created up to this long before the launch call still counts as ours
SIGNAL_FILE = "Local State"


class HostRefused(RuntimeError):
    """A real desktop, process, window or COM call was refused (`reason` "sandbox": AGY_DR_TEST_SANDBOX=1)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class HostError(RuntimeError):
    """Host refusal with an error code (GEMINI_IN_USE, HOST_BUSY, HOST_MODE_UNAVAILABLE, HOST_START_TIMEOUT)."""

    def __init__(self, code: str, message: str = ""):
        super().__init__("%s: %s" % (code, message) if message else code)
        self.code, self.message = code, message


def refuse_in_sandbox() -> None:
    if os.environ.get(SANDBOX_VAR) == "1":
        raise HostRefused("sandbox")


@dataclass
class WindowInfo:
    """A top-level window. No title text is read, only whether it has one."""
    hwnd: int
    pid: int
    class_name: str
    visible: bool
    has_title: bool
    owner: int = 0

    @property
    def is_main(self) -> bool:
        return self.class_name == MAIN_WINDOW_CLASS and self.visible and self.has_title and not self.owner


class HostOS(Protocol):
    def open_desktop(self, name: str) -> bool: ...  # does the desktop exist (OpenDesktopW by name)

    def create_desktop(self, name: str) -> None: ...  # CreateDesktopW in WinSta0, handle kept until released

    def release_desktops(self) -> None: ...  # close the desktop handles create_desktop kept

    def enum_desktops(self) -> List[str]: ...

    def desktop_windows(self, name: str) -> List[WindowInfo]: ...  # [] when the desktop cannot be opened

    def processes(self) -> List[ProcInfo]: ...  # Gemini.exe processes; uninspectable ones as inspected=False

    def launch(self, cmdline: str, desktop: str, env: Mapping[str, str],
               cwd: Optional[str] = None) -> int: ...  # pid of the launcher stub

    def terminate(self, pid: int, created: Optional[float] = None) -> bool: ...

    def post_close(self, desktop: str, hwnds: Sequence[int]) -> int: ...  # WM_CLOSE from a thread on `desktop`

    def local_state_mtime(self, profile: str) -> Optional[float]: ...


def _tool_dir(environ: Optional[Mapping[str, str]]) -> str:
    env = os.environ if environ is None else environ
    base = env.get("LOCALAPPDATA", "")
    if not base or not base.strip():
        raise ValueError("LOCALAPPDATA is not set: cannot place the agy-deep-research host files")
    return os.path.join(base, "agy-deep-research")


def default_profile_dir(environ: Optional[Mapping[str, str]] = None) -> str:
    return os.path.join(_tool_dir(environ), "gemini-profile")


def default_lock_path(environ: Optional[Mapping[str, str]] = None) -> str:
    return os.path.join(_tool_dir(environ), "host.lock")


def default_state_path(environ: Optional[Mapping[str, str]] = None) -> str:
    return os.path.join(_tool_dir(environ), "host.json")


def app_version(image: str) -> Optional[str]:
    """`1.2.3` from ...\\app-1.2.3\\Gemini.exe (None when the image is not an app image)."""
    if not is_gemini_image(image):
        return None
    folder = ntpath.basename(ntpath.dirname(image))
    return folder[len("app-"):] if folder.lower().startswith("app-") else None


@dataclass
class HostConfig:
    stub: str
    profile_dir: str
    desktop: str = DESKTOP_NAME
    flags: Sequence[str] = ALLOWED_FLAG_SWITCHES
    lock_path: Optional[str] = None  # default: %LOCALAPPDATA%\agy-deep-research\host.lock
    state_path: Optional[str] = None  # default: %LOCALAPPDATA%\agy-deep-research\host.json
    environ: Optional[Mapping[str, str]] = None  # launch environment before clean_env (default os.environ)
    clock: Callable[[], float] = time.time  # wall clock: compared with process creation times and file mtimes
    sleep: Callable[[float], None] = time.sleep
    poll_s: float = 0.5
    quit_wait_s: float = 10.0
    terminate_wait_s: float = 5.0

    def __post_init__(self):
        if self.lock_path is None:
            self.lock_path = default_lock_path()
        if self.state_path is None:
            self.state_path = default_state_path()


@dataclass
class HostSession:
    mode: Literal["D2", "D1"]
    desktop: str
    browser_pid: int
    reason: Optional[str] = None  # "reused" | "data_dir_switch_ignored" | None


@dataclass
class InstanceInfo:
    desktop: Optional[str]  # None: the dedicated profile runs with no window on any desktop we can read
    pids: List[int]
    browser_pid: Optional[int]
    profile: Optional[str]  # --user-data-dir of the browser; None for the default profile (D1)
    mode: Literal["D2", "D1"]
    orphan: bool
    last_job: Optional[str] = None  # from host.json, when it describes this browser process
    last_step_at: Optional[float] = None
    started_at: Optional[float] = None


@dataclass
class CloseResult:
    remaining: int
    method: str  # none | menu | wm_close | terminate
    desktop_gone: bool
    terminated: List[int] = field(default_factory=list)
    menu_error: Optional[str] = None  # exception type name when quit_via_menu raised


def _profile_differs(parent: ProcInfo, child: ProcInfo) -> bool:
    a, b = parse_user_data_dir(parent.cmdline), parse_user_data_dir(child.cmdline)
    if a is None or b is None:
        return (a is None) != (b is None)
    return _canon(a) != _canon(b)


def _write_json_atomic(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _same_process(rec: Optional[dict], pid: Optional[int], created: Optional[float]) -> bool:
    if not rec or pid is None or created is None:
        return False
    try:
        return int(rec.get("browser_pid")) == pid and abs(float(rec.get("browser_created")) - created) < 1e-3
    except (TypeError, ValueError):
        return False


QuitViaMenu = Callable[[str, List[int], List[int]], None]


class Host:
    """ensure / instances / close / touch / lock over an injected HostOS. Never acts on a process it has not
    verified as ours: the dedicated profile (D2), or a default-profile process tree owning a window on our hidden
    desktop (D1). A Gemini.exe process that cannot be inspected counts as the user's (fail closed)."""

    def __init__(self, os_: HostOS, cfg: HostConfig):
        self.os, self.cfg = os_, cfg
        self._launch_t0: Optional[float] = None

    # --- process trees -------------------------------------------------------------------------------------------
    def _scan(self) -> Tuple[Dict[int, ProcInfo], List[ProcInfo]]:
        """(inspected Gemini app processes by pid, uninspectable Gemini.exe processes)."""
        procs = self.os.processes()
        byid = {p.pid: p for p in procs if p.inspected and is_gemini_image(p.image)}
        blind = [p for p in procs if not p.inspected]
        return byid, blind

    def _procs(self) -> Dict[int, ProcInfo]:
        return self._scan()[0]

    @staticmethod
    def _root(pid: int, byid: Dict[int, ProcInfo]) -> Optional[ProcInfo]:
        """Walk up to the browser process: parents that are Gemini app processes of the same profile, created no
        later than the child (a reused parent pid is newer and stops the walk). The stub is not an app image."""
        p = byid.get(pid)
        seen = set()
        while p is not None and p.pid not in seen:
            seen.add(p.pid)
            parent = byid.get(p.ppid)
            if parent is None or parent.created > p.created or _profile_differs(parent, p):
                return p
            p = parent
        return p

    @staticmethod
    def _tree(root: ProcInfo, byid: Dict[int, ProcInfo]) -> List[ProcInfo]:
        out, stack, seen = [], [root], set()
        while stack:
            p = stack.pop()
            if p.pid in seen:
                continue
            seen.add(p.pid)
            out.append(p)
            stack.extend(c for c in byid.values() if c.ppid == p.pid and c.created >= p.created and c.pid not in seen)
        return out

    def _ours_d2(self, p: ProcInfo, started_after: Optional[float] = None) -> bool:
        return p.inspected and is_our_instance(p, self.cfg.profile_dir, started_after)

    def _desktop_roots(self, desktop: str, byid: Dict[int, ProcInfo], main_only: bool) -> List[ProcInfo]:
        roots: Dict[int, ProcInfo] = {}
        for w in self.os.desktop_windows(desktop):
            if w.pid in byid and (w.is_main or not main_only):
                r = self._root(w.pid, byid)
                if r is not None:
                    roots[r.pid] = r
        return list(roots.values())

    def _d1_trees(self, byid: Dict[int, ProcInfo]) -> Dict[int, ProcInfo]:
        """Default-profile trees owning a window on our hidden desktop (the user's windows live elsewhere)."""
        out: Dict[int, ProcInfo] = {}
        for root in self._desktop_roots(self.cfg.desktop, byid, main_only=False):
            if parse_user_data_dir(root.cmdline) is None:
                for p in self._tree(root, byid):
                    out[p.pid] = p
        return out

    def _verified_tree(self, desktop: Optional[str], mode: str) -> List[ProcInfo]:
        byid = self._procs()
        if mode == "D2":
            return [p for p in byid.values() if self._ours_d2(p)]
        if desktop != self.cfg.desktop:
            return []  # a D1 instance is identified only by our hidden desktop
        return list(self._d1_trees(byid).values())

    def _user_running(self, byid: Dict[int, ProcInfo], blind: List[ProcInfo]) -> bool:
        """Any Gemini process that is not ours. An uninspectable Gemini.exe always counts (fail closed)."""
        if blind:
            return True
        mine = {pid for pid, p in byid.items() if self._ours_d2(p)} | set(self._d1_trees(byid))
        return any(pid not in mine for pid in byid)

    def _instance_on_desktop(self, byid: Dict[int, ProcInfo]) -> Optional[Tuple[str, ProcInfo]]:
        for root in self._desktop_roots(self.cfg.desktop, byid, main_only=True):
            if self._ours_d2(root):
                return "D2", root
            if parse_user_data_dir(root.cmdline) is None:
                return "D1", root
        return None

    # --- host.json -------------------------------------------------------------------------------------------------
    def load_state(self) -> Optional[dict]:
        try:
            with open(self.cfg.state_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _save_state(self, session: HostSession, root: ProcInfo) -> None:
        old = self.load_state()
        same = _same_process(old, root.pid, root.created)
        started = self._launch_t0 if self._launch_t0 is not None else root.created
        rec = {
            "mode": session.mode,
            "browser_pid": root.pid,
            "browser_created": root.created,
            "desktop": session.desktop,
            "started_at": old.get("started_at", started) if same else started,
            "last_job": old.get("last_job") if same else None,
            "last_step_at": old.get("last_step_at") if same else None,
            "app_version": app_version(root.image),
        }
        _write_json_atomic(self.cfg.state_path, rec)

    def touch(self, job_id: str) -> bool:
        """Record that `job_id` just ran a step on the current instance (False when there is no host.json)."""
        rec = self.load_state()
        if rec is None:
            return False
        rec["last_job"] = job_id
        rec["last_step_at"] = self.cfg.clock()
        _write_json_atomic(self.cfg.state_path, rec)
        return True

    def _clear_state(self, browser_pid: Optional[int]) -> None:
        rec = self.load_state()
        if rec is not None and browser_pid is not None and rec.get("browser_pid") == browser_pid:
            try:
                os.remove(self.cfg.state_path)
            except OSError:
                pass

    # --- ensure ----------------------------------------------------------------------------------------------------
    def _env(self) -> dict:
        return clean_env(os.environ if self.cfg.environ is None else self.cfg.environ)

    def _station_desktop(self) -> str:
        return "%s\\%s" % (WINDOW_STATION, self.cfg.desktop)

    def _ensure_desktop(self) -> None:
        if not self.os.open_desktop(self.cfg.desktop):
            self.os.create_desktop(self.cfg.desktop)

    def _launch(self, cmd: str) -> float:
        self._ensure_desktop()
        t0 = self.cfg.clock()
        if self._launch_t0 is None:
            self._launch_t0 = t0
        self.os.launch(cmd, self._station_desktop(), self._env(), self.cfg.profile_dir)
        return t0

    def _fail(self, code: str, reason: Optional[str]) -> HostError:
        """Before refusing after a launch of ours: terminate what that launch started (verified, ours only)."""
        if self._launch_t0 is not None:
            since = self._launch_t0 - LAUNCH_SLACK_S
            byid = self._procs()
            victims = {pid: p for pid, p in byid.items() if self._ours_d2(p, since)}
            victims.update({pid: p for pid, p in self._d1_trees(byid).items() if p.created >= since})
            for p in victims.values():
                self.os.terminate(p.pid, p.created)
            self.os.release_desktops()
        return HostError(code, reason or "")

    def ensure(self, job_mode: Optional[str]) -> HostSession:
        """Reuse the instance on our hidden desktop, else start D2 (dedicated profile) and fall back to D1 per
        decide_mode; writes host.json. Raises HostError (GEMINI_IN_USE, HOST_BUSY, HOST_MODE_UNAVAILABLE,
        HOST_START_TIMEOUT)."""
        if job_mode not in (None, "D2", "D1"):
            raise ValueError(f"unknown job mode: {job_mode}")
        self._launch_t0 = None
        byid, blind = self._scan()
        existing = self._instance_on_desktop(byid) if self.os.open_desktop(self.cfg.desktop) else None
        if existing is not None:
            mode, root = existing
            if job_mode is not None and job_mode != mode:
                raise HostError("HOST_MODE_UNAVAILABLE",
                                f"job is pinned to {job_mode} but the running instance is {mode}")
            return self._session(mode, root, "reused")
        if any(self._ours_d2(p) for p in byid.values()):
            raise HostError("HOST_BUSY", "the dedicated data-dir instance runs outside the hidden desktop "
                                         "(signin or a visible debug window); close it first")
        if job_mode == "D1":
            dec = decide_mode("D1", None, self._user_running(byid, blind))
            if dec.code:
                raise HostError(dec.code, dec.reason or "")
            return self._launch_d1(None)
        user_before = self._user_running(byid, blind)
        state, root = self._launch_d2()
        if state == "ok":
            return self._session("D2", root, None)
        # the user may have started Gemini while we waited: decide (and launch D1) on a fresh inventory
        user_now = self._user_running(*self._scan())
        dec = decide_mode(job_mode, state, user_before or user_now)
        if dec.code:
            raise self._fail(dec.code, dec.reason)
        if root is not None:  # the stub dropped the switch: a default-profile instance is already on our desktop
            return self._session("D1", root, dec.reason)
        return self._launch_d1(dec.reason)

    def _session(self, mode: str, root: ProcInfo, reason: Optional[str]) -> HostSession:
        s = HostSession(mode, self.cfg.desktop, root.pid, reason)
        self._save_state(s, root)
        return s

    def _launch_d2(self) -> Tuple[str, Optional[ProcInfo]]:
        """("ok", our browser) | ("blocked", default-profile browser on our desktop or None) | ("timeout", None)."""
        cfg = self.cfg
        cmd = build_launch_command(cfg.stub, cfg.profile_dir, list(cfg.flags))
        before = self.os.local_state_mtime(cfg.profile_dir)
        t0 = self._launch(cmd)
        seen: set = set()
        while True:
            byid = self._procs()
            ours = {pid for pid, p in byid.items() if self._ours_d2(p, t0 - LAUNCH_SLACK_S)}
            seen |= ours
            roots = self._desktop_roots(cfg.desktop, byid, main_only=True)
            mine = [r for r in roots if self._ours_d2(r)]
            mtime = self.os.local_state_mtime(cfg.profile_dir)
            check = LaunchCheck(
                window_on_our_desktop=bool(roots),
                cmdline_has_profile=bool(mine),
                local_state_updated=mtime is not None and (before is None or mtime > before),
                exited=bool(seen) and not ours,  # our app process (never the stub) was seen and is gone
                elapsed_s=cfg.clock() - t0)
            state = classify_launch(check)
            if state == "ok":
                return "ok", mine[0]
            if state == "blocked":
                default = [r for r in roots if parse_user_data_dir(r.cmdline) is None]
                return "blocked", (default[0] if default and not check.exited else None)
            if state == "timeout":
                return "timeout", None
            cfg.sleep(cfg.poll_s)

    def _launch_d1(self, reason: Optional[str]) -> HostSession:
        cfg = self.cfg
        t0 = self._launch(build_launch_command(cfg.stub, None, []))
        while True:
            found = self._instance_on_desktop(self._procs())
            if found is not None and found[0] == "D1":
                return self._session("D1", found[1], reason)
            if cfg.clock() - t0 >= START_TIMEOUT_S:
                raise self._fail("HOST_START_TIMEOUT", "Gemini did not start in time")
            cfg.sleep(cfg.poll_s)

    # --- inventory -------------------------------------------------------------------------------------------------
    def instances(self, active_jobs: Iterable[str] = ()) -> List[InstanceInfo]:
        """Our instances only: default-profile trees on our hidden desktop (D1) and the dedicated profile wherever
        it runs (D2); the user's instances are never listed. host.json is merged when it describes the same
        browser process; `orphan` = alive instance whose last_job is not one of `active_jobs`."""
        active = set(active_jobs)
        byid = self._procs()
        rec = self.load_state()
        out: List[InstanceInfo] = []
        covered: set = set()

        def add(desk, root, tree, mode):
            covered.update(p.pid for p in tree)
            same = _same_process(rec, root.pid, root.created)
            last_job = rec.get("last_job") if same else None
            out.append(InstanceInfo(desk, sorted(p.pid for p in tree), root.pid, parse_user_data_dir(root.cmdline),
                                    mode, not (last_job and last_job in active), last_job,
                                    rec.get("last_step_at") if same else None,
                                    rec.get("started_at") if same else None))

        for desk in self.os.enum_desktops():
            for root in self._desktop_roots(desk, byid, main_only=False):
                if root.pid in covered:
                    continue
                d2 = self._ours_d2(root)
                if not d2 and (desk != self.cfg.desktop or parse_user_data_dir(root.cmdline) is not None):
                    continue
                add(desk, root, self._tree(root, byid), "D2" if d2 else "D1")
        rest = {pid: p for pid, p in byid.items() if pid not in covered and self._ours_d2(p)}
        for pid in sorted(rest):
            if pid in covered:
                continue
            root = self._root(pid, rest) or rest[pid]
            add(None, root, self._tree(root, rest), "D2")
        return out

    # --- close -----------------------------------------------------------------------------------------------------
    def _wait_gone(self, desktop: Optional[str], mode: str, seconds: float) -> List[ProcInfo]:
        deadline = self.cfg.clock() + seconds
        while True:
            left = self._verified_tree(desktop, mode)
            if not left or self.cfg.clock() >= deadline:
                return left
            self.cfg.sleep(self.cfg.poll_s)

    def _desktop_gone(self) -> bool:
        return self.cfg.desktop not in self.os.enum_desktops()

    def _main_hwnds(self, desktop: str, left: List[ProcInfo]) -> List[int]:
        pids = {p.pid for p in left}
        return [w.hwnd for w in self.os.desktop_windows(desktop) if w.pid in pids and w.is_main]

    def close(self, target: Union[HostSession, InstanceInfo], quit_via_menu: QuitViaMenu) -> CloseResult:
        """On our hidden desktop only: quit_via_menu(desktop, pids, hwnds) - the caller runs the app menu
        (파일 -> 종료) via UIA from a thread on that desktop, limited to those verified pids/windows -> wait.
        Then WM_CLOSE to our verified main windows from a thread on their desktop -> wait -> TerminateProcess of
        our tree, re-verified at every step (same pid and creation time, still ours). Releases held desktop
        handles; removes host.json when it described the closed instance."""
        try:
            res = self._close(target, quit_via_menu)
        finally:
            self.os.release_desktops()
        if res.remaining == 0:
            self._clear_state(target.browser_pid)
        return res

    def _close(self, target: Union[HostSession, InstanceInfo], quit_via_menu: QuitViaMenu) -> CloseResult:
        desktop, mode = target.desktop, target.mode
        left = self._verified_tree(desktop, mode)
        if not left:
            return CloseResult(0, "none", self._desktop_gone())
        menu_error = None
        if desktop == self.cfg.desktop:
            try:
                quit_via_menu(desktop, sorted(p.pid for p in left), self._main_hwnds(desktop, left))
            except Exception as exc:  # fall through to WM_CLOSE
                menu_error = type(exc).__name__
            left = self._wait_gone(desktop, mode, self.cfg.quit_wait_s)
            if not left:
                return CloseResult(0, "menu", self._desktop_gone(), menu_error=menu_error)
        if desktop:
            hwnds = self._main_hwnds(desktop, left)
            if hwnds:
                self.os.post_close(desktop, hwnds)
                left = self._wait_gone(desktop, mode, self.cfg.quit_wait_s)
                if not left:
                    return CloseResult(0, "wm_close", self._desktop_gone(), menu_error=menu_error)
        terminated = []
        for p in left:
            if self.os.terminate(p.pid, p.created) is not False:
                terminated.append(p.pid)
        left = self._wait_gone(desktop, mode, self.cfg.terminate_wait_s)
        return CloseResult(len(left), "terminate", self._desktop_gone(), terminated, menu_error)

    # --- lock ------------------------------------------------------------------------------------------------------
    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        """One host operation at a time across processes: a non-blocking byte lock on the host lock file
        (msvcrt.locking). Raises HostError("HOST_BUSY") while another holder has it."""
        path = self.cfg.lock_path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fh = open(path, "a+b")
        try:
            try:
                _lock_byte(fh)
            except OSError:
                raise HostError("HOST_BUSY", "another agy-deep-research step holds the Gemini host") from None
            try:
                yield
            finally:
                _unlock_byte(fh)
        finally:
            fh.close()


def _lock_byte(fh) -> None:
    fh.seek(0)
    try:
        import msvcrt
    except ImportError:  # non-Windows (tooling only): advisory flock
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)


def _unlock_byte(fh) -> None:
    fh.seek(0)
    try:
        import msvcrt
    except ImportError:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        return
    try:
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


# --- Win32HostOS -----------------------------------------------------------------------------------------------------

_GENERIC_ALL = 0x10000000
_DESKTOP_READOBJECTS = 0x0001
_DESKTOP_ENUMERATE = 0x0040
_PROCESS_TERMINATE = 0x0001
_PROCESS_QUERY_LIMITED = 0x1000
_CREATE_NO_WINDOW = 0x08000000
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_TH32CS_SNAPPROCESS = 0x00000002
_PROCESS_COMMAND_LINE_INFORMATION = 60
_WM_CLOSE = 0x0010
_GW_OWNER = 4
_EPOCH_AS_FILETIME = 116444736000000000
_APP_EXE = "gemini.exe"


def _env_block(env: Mapping[str, str]) -> str:
    items = sorted(env.items(), key=lambda kv: kv[0].upper())
    return "".join("%s=%s\0" % (k, v) for k, v in items) + "\0"


class _Win32:
    """ctypes bindings, built on first use (never at import)."""

    def __init__(self):
        import ctypes
        import ctypes.wintypes as wt
        self.ctypes, self.wt = ctypes, wt
        self.u32 = u = ctypes.WinDLL("user32", use_last_error=True)
        self.k32 = k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.ntdll = ctypes.WinDLL("ntdll")
        H = wt.HANDLE
        u.OpenDesktopW.restype = H
        u.OpenDesktopW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.BOOL, wt.DWORD]
        u.CreateDesktopW.restype = H
        u.CreateDesktopW.argtypes = [wt.LPCWSTR, wt.LPCWSTR, ctypes.c_void_p, wt.DWORD, wt.DWORD, ctypes.c_void_p]
        u.CloseDesktop.argtypes = [H]
        u.SetThreadDesktop.argtypes = [H]
        u.GetProcessWindowStation.restype = H
        self.ENUMSTR = ctypes.WINFUNCTYPE(wt.BOOL, wt.LPWSTR, wt.LPARAM)
        self.ENUMWND = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
        u.EnumDesktopsW.argtypes = [H, self.ENUMSTR, wt.LPARAM]
        u.EnumDesktopWindows.argtypes = [H, self.ENUMWND, wt.LPARAM]
        u.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
        u.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
        u.IsWindowVisible.argtypes = [wt.HWND]
        u.GetWindowTextLengthW.argtypes = [wt.HWND]
        u.GetWindow.restype = wt.HWND
        u.GetWindow.argtypes = [wt.HWND, wt.UINT]
        u.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]

        class STARTUPINFOW(ctypes.Structure):
            _fields_ = [("cb", wt.DWORD), ("lpReserved", wt.LPWSTR), ("lpDesktop", wt.LPWSTR),
                        ("lpTitle", wt.LPWSTR), ("dwX", wt.DWORD), ("dwY", wt.DWORD), ("dwXSize", wt.DWORD),
                        ("dwYSize", wt.DWORD), ("dwXCountChars", wt.DWORD), ("dwYCountChars", wt.DWORD),
                        ("dwFillAttribute", wt.DWORD), ("dwFlags", wt.DWORD), ("wShowWindow", wt.WORD),
                        ("cbReserved2", wt.WORD), ("lpReserved2", ctypes.c_void_p), ("hStdInput", H),
                        ("hStdOutput", H), ("hStdError", H)]

        class PROCESS_INFORMATION(ctypes.Structure):
            _fields_ = [("hProcess", H), ("hThread", H), ("dwProcessId", wt.DWORD), ("dwThreadId", wt.DWORD)]

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                        ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wt.DWORD),
                        ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
                        ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
                        ("szExeFile", ctypes.c_wchar * 260)]

        class UNICODE_STRING(ctypes.Structure):
            _fields_ = [("Length", wt.USHORT), ("MaximumLength", wt.USHORT), ("Buffer", ctypes.c_void_p)]

        self.STARTUPINFOW, self.PROCESS_INFORMATION = STARTUPINFOW, PROCESS_INFORMATION
        self.PROCESSENTRY32W, self.UNICODE_STRING = PROCESSENTRY32W, UNICODE_STRING
        k.CreateProcessW.argtypes = [wt.LPCWSTR, wt.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wt.BOOL, wt.DWORD,
                                     ctypes.c_void_p, wt.LPCWSTR, ctypes.POINTER(STARTUPINFOW),
                                     ctypes.POINTER(PROCESS_INFORMATION)]
        k.CloseHandle.argtypes = [H]
        k.OpenProcess.restype = H
        k.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        k.QueryFullProcessImageNameW.argtypes = [H, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
        k.GetProcessTimes.argtypes = [H] + [ctypes.POINTER(wt.FILETIME)] * 4
        k.TerminateProcess.argtypes = [H, wt.UINT]
        k.CreateToolhelp32Snapshot.restype = H
        k.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
        k.Process32FirstW.argtypes = [H, ctypes.POINTER(PROCESSENTRY32W)]
        k.Process32NextW.argtypes = [H, ctypes.POINTER(PROCESSENTRY32W)]
        self.ntdll.NtQueryInformationProcess.restype = ctypes.c_ulong
        self.ntdll.NtQueryInformationProcess.argtypes = [H, ctypes.c_int, ctypes.c_void_p, wt.ULONG,
                                                         ctypes.POINTER(wt.ULONG)]

    def err(self, what: str) -> OSError:
        code = self.ctypes.get_last_error()
        return OSError(code, "%s failed (error %d)" % (what, code))

    def image(self, h) -> str:
        buf = self.ctypes.create_unicode_buffer(1024)
        n = self.wt.DWORD(1024)
        return buf.value if self.k32.QueryFullProcessImageNameW(h, 0, buf, self.ctypes.byref(n)) else ""

    def created(self, h) -> Optional[float]:
        ft = [self.wt.FILETIME() for _ in range(4)]
        if not self.k32.GetProcessTimes(h, *[self.ctypes.byref(f) for f in ft]):
            return None
        v = (ft[0].dwHighDateTime << 32) | ft[0].dwLowDateTime
        return (v - _EPOCH_AS_FILETIME) / 1e7

    def cmdline(self, h) -> Optional[str]:
        """ProcessCommandLineInformation (Windows 8.1+): same-user processes, no WMI/PowerShell. None on failure."""
        ctypes, wt = self.ctypes, self.wt
        size = wt.ULONG(0)
        self.ntdll.NtQueryInformationProcess(h, _PROCESS_COMMAND_LINE_INFORMATION, None, 0, ctypes.byref(size))
        if not size.value:
            return None
        buf = ctypes.create_string_buffer(size.value)
        if self.ntdll.NtQueryInformationProcess(h, _PROCESS_COMMAND_LINE_INFORMATION, buf, size,
                                                ctypes.byref(size)) != 0:
            return None
        us = self.UNICODE_STRING.from_buffer(buf)
        return ctypes.wstring_at(us.Buffer, us.Length // 2) if us.Buffer and us.Length else ""


class Win32HostOS:
    """The real HostOS (ctypes). Every method first refuses with HostRefused("sandbox") under
    AGY_DR_TEST_SANDBOX=1, before any Win32 library is loaded."""

    def __init__(self):
        self._w: Optional[_Win32] = None
        self._held: Dict[str, int] = {}  # desktops we created: handle kept so the desktop lives until the app runs

    def _api(self) -> _Win32:
        refuse_in_sandbox()
        if self._w is None:
            self._w = _Win32()
        return self._w

    def open_desktop(self, name: str) -> bool:
        w = self._api()
        h = w.u32.OpenDesktopW(name, 0, False, _DESKTOP_READOBJECTS)
        if not h:
            return False
        w.u32.CloseDesktop(h)
        return True

    def create_desktop(self, name: str) -> None:
        w = self._api()
        h = w.u32.CreateDesktopW(name, None, None, 0, _GENERIC_ALL, None)
        if not h:
            raise w.err("CreateDesktopW")
        self._held[name] = h

    def release_desktops(self) -> None:
        w = self._api()
        held, self._held = self._held, {}
        for h in held.values():
            w.u32.CloseDesktop(h)

    def enum_desktops(self) -> List[str]:
        w = self._api()
        names: List[str] = []
        errors: List[BaseException] = []

        def cb(n, _):
            try:
                names.append(n)
                return True
            except BaseException as exc:  # an exception cannot cross the ctypes callback: record it, stop
                errors.append(exc)
                return False

        w.u32.EnumDesktopsW(w.u32.GetProcessWindowStation(), w.ENUMSTR(cb), 0)
        if errors:
            raise errors[0]
        return names

    def desktop_windows(self, name: str) -> List[WindowInfo]:
        w = self._api()
        ctypes, wt, u = w.ctypes, w.wt, w.u32
        h = u.OpenDesktopW(name, 0, False, _DESKTOP_ENUMERATE | _DESKTOP_READOBJECTS)
        if not h:
            return []
        out: List[WindowInfo] = []
        errors: List[BaseException] = []

        def cb(hwnd, _):
            try:
                pid = wt.DWORD()
                u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                cls = ctypes.create_unicode_buffer(256)
                u.GetClassNameW(hwnd, cls, 256)
                out.append(WindowInfo(int(hwnd or 0), int(pid.value), cls.value, bool(u.IsWindowVisible(hwnd)),
                                      u.GetWindowTextLengthW(hwnd) > 0, int(u.GetWindow(hwnd, _GW_OWNER) or 0)))
                return True
            except BaseException as exc:  # an exception cannot cross the ctypes callback: record it, stop
                errors.append(exc)
                return False

        try:
            u.EnumDesktopWindows(h, w.ENUMWND(cb), 0)
        finally:
            u.CloseDesktop(h)
        if errors:
            raise errors[0]
        return out

    def processes(self) -> List[ProcInfo]:
        """Gemini.exe processes (Toolhelp snapshot, then image, creation time and command line per process). One
        that cannot be opened or read is returned as ProcInfo(inspected=False): Host counts it as the user's."""
        w = self._api()
        k, ctypes = w.k32, w.ctypes
        snap = k.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if not snap or snap == ctypes.c_void_p(-1).value:
            raise w.err("CreateToolhelp32Snapshot")
        entries = []
        try:
            e = w.PROCESSENTRY32W()
            e.dwSize = ctypes.sizeof(e)
            ok = k.Process32FirstW(snap, ctypes.byref(e))
            while ok:
                if e.szExeFile.lower() == _APP_EXE:
                    entries.append((int(e.th32ProcessID), int(e.th32ParentProcessID)))
                ok = k.Process32NextW(snap, ctypes.byref(e))
        finally:
            k.CloseHandle(snap)
        out: List[ProcInfo] = []
        for pid, ppid in entries:
            sentinel = ProcInfo(pid, ppid, "", "", 0.0, inspected=False)
            h = k.OpenProcess(_PROCESS_QUERY_LIMITED, False, pid)
            if not h:
                out.append(sentinel)
                continue
            try:
                created, image, cmd = w.created(h), w.image(h), w.cmdline(h)
                if created is None or not image or cmd is None:
                    out.append(sentinel)
                else:
                    out.append(ProcInfo(pid, ppid, image, cmd, created))
            finally:
                k.CloseHandle(h)
        return out

    def launch(self, cmdline: str, desktop: str, env: Mapping[str, str], cwd: Optional[str] = None) -> int:
        """CreateProcessW on `desktop` (WinSta0\\<name>) with CREATE_NO_WINDOW and `cwd` (the profile directory,
        created when missing) as the current directory; returns the stub's pid."""
        w = self._api()
        if not desktop.startswith(WINDOW_STATION + "\\"):
            raise ValueError("desktop must be %s\\<name>" % WINDOW_STATION)
        if cwd:
            _validate_profile_dir(cwd)  # never a directory under the app's own data directory
            os.makedirs(cwd, exist_ok=True)
        ctypes = w.ctypes
        si = w.STARTUPINFOW()
        si.cb = ctypes.sizeof(si)
        si.lpDesktop = desktop
        pi = w.PROCESS_INFORMATION()
        buf = ctypes.create_unicode_buffer(cmdline)
        block = ctypes.create_unicode_buffer(_env_block(env))
        ok = w.k32.CreateProcessW(None, buf, None, None, False, _CREATE_NO_WINDOW | _CREATE_UNICODE_ENVIRONMENT,
                                  block, cwd or None, ctypes.byref(si), ctypes.byref(pi))
        if not ok:
            raise w.err("CreateProcessW")
        w.k32.CloseHandle(pi.hThread)
        w.k32.CloseHandle(pi.hProcess)
        return int(pi.dwProcessId)

    def terminate(self, pid: int, created: Optional[float] = None) -> bool:
        """TerminateProcess only after re-checking on the opened handle that it is a Gemini app process with the
        expected creation time (when given): a reused pid is never killed."""
        w = self._api()
        h = w.k32.OpenProcess(_PROCESS_TERMINATE | _PROCESS_QUERY_LIMITED, False, pid)
        if not h:
            return False
        try:
            if not is_gemini_image(w.image(h)):
                return False
            got = w.created(h)
            if created is not None and (got is None or abs(got - created) > 1e-3):
                return False
            return bool(w.k32.TerminateProcess(h, 1))
        finally:
            w.k32.CloseHandle(h)

    def post_close(self, desktop: str, hwnds: Sequence[int]) -> int:
        """WM_CLOSE from a fresh thread switched onto `desktop` (a cross-desktop post fails with error 1400)."""
        w = self._api()
        result: dict = {"posted": 0, "error": None}

        def run():
            h = w.u32.OpenDesktopW(desktop, 0, False, _GENERIC_ALL)
            if not h:
                result["error"] = w.err("OpenDesktopW")
                return
            try:
                if not w.u32.SetThreadDesktop(h):
                    result["error"] = w.err("SetThreadDesktop")
                    return
                for hwnd in hwnds:
                    if w.u32.PostMessageW(hwnd, _WM_CLOSE, 0, 0):
                        result["posted"] += 1
            finally:
                w.u32.CloseDesktop(h)

        t = threading.Thread(target=run, name="agy-dr-wm-close", daemon=True)
        t.start()
        t.join(10)
        if result["error"] is not None:
            raise result["error"]
        return result["posted"]

    def local_state_mtime(self, profile: str) -> Optional[float]:
        refuse_in_sandbox()
        try:
            return os.stat(os.path.join(profile, SIGNAL_FILE)).st_mtime
        except OSError:
            return None

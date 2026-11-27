#!/usr/bin/env python3
r"""fg_tracer.py - R6 invariant tracer: the driver never changes the default desktop's foreground.

`FgTracer(os_api, period_s=0.02, our_pids=...)` samples the foreground window of the desktop it runs on (the
default, input desktop) every `period_s`, and every `window_every` samples the visible top-level windows there.
It records, per change, only the foreground process image basename and whether it is a Gemini app process and
whether it belongs to OUR instance. "Ours" = a pid of our verified instance tree: `our_pids()` is a callable that
returns the pids (in detached mode `host_pids_source`: `gemini_host.Host.instances()`, any Gemini process tree
matching our dedicated `--user-data-dir`, and the verified tree of the session in `host.json` / the job's
`relay/driver_state.json` so even a D1 instance that lands on the default desktop is caught); it is called again
every `refresh_s` because our instance may start after the tracer, and every pid it ever returned stays ours for
the tracer's lifetime, so a foreground event seen before the pid was known is re-classified at stop(). The user's
own Gemini windows are visible and come to the foreground on the default desktop all the time (live 2026-11-27:
58 foreground changes, ours 0): they are counted (`gemini_foreground_events`, `visible_windows`) but are never a
violation. Violation = one of our pids owned the foreground window, or owned a visible top-level window on this
desktop (`ours.foreground_events` / `ours.visible_window_samples` > 0).

No window title is ever read. The OS surface is `FgOS` (injectable); `Win32FgOS` is the real one (ctypes, loaded
lazily) and refuses every call with gemini_host.HostRefused("sandbox") under AGY_DR_TEST_SANDBOX=1.

Detached mode (started by deep_research.py on the user's default desktop around each agy relay step — never by
relay_step.py, which runs on agy's hidden `exebox-*` desktop):

    python -I fg_tracer.py --job <job dir> [--watch-pid <pid>] [--stop-file <path>...] [--run-id ID]
                           [--max-s SEC] [--stop-on-eof] [--seed-pid PID...]

appends one scrubbed JSON object per line to `<job>/relay/foreground.jsonl` (protected from agy by the agy-delegate
confine hook; `--job` and every `--stop-file` must resolve inside the given job directory): a `start` record, one
`foreground` record per foreground change, one `visible` record per change of the visible-Gemini-window state, and
an `end` record with the summary. Every record carries `run` and holds only event names, times, counts, process
image basenames and booleans. For synchronous steps (`plan`, `edit`, `collect` wait) `deep_research.py` passes
`--watch-pid` and `--stop-on-eof`; for detached-worker steps (`approve`, `auto`, `collect --detach`, including
`--no-wait`) it passes `--stop-file <job>/relay/result.json` (plus a per-run stop file) so the tracer stays alive
on the default desktop until the worker finishes and writes `result.json`, or `--max-s` seconds pass.
Exit codes: 0 ok | 3 environment error (refused, no Win32) | 5 bad arguments. Python 3.9+, stdlib only.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Protocol, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import gemini_host  # noqa: E402

try:
    from privacy import scrub as _scrub
except Exception:  # noqa: BLE001 - records hold only basenames / booleans / numbers anyway
    _scrub = None

APP_IMAGE = "gemini.exe"
NONE_IMAGE = "<none>"        # no foreground window
UNKNOWN_IMAGE = "<unknown>"  # a process whose image cannot be read (never its pid or title)
JSONL_NAME = "foreground.jsonl"
DEFAULT_MAX_S = 3600.0
WATCH_EVERY_S = 0.5
MAX_EVENTS = 100000
DEFAULT_DESKTOP = "Default"
DRIVER_STATE_REL = os.path.join("relay", "driver_state.json")


def now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class FgWindow:
    """A top-level window: owner pid and visibility only (no title, no class)."""
    hwnd: int
    pid: int
    visible: bool


class FgOS(Protocol):
    def foreground(self) -> Tuple[int, int]: ...  # (hwnd, pid) of the foreground window; (0, 0) when none

    def image(self, pid: int) -> str: ...  # the process image basename, "" when it cannot be read

    def top_windows(self) -> List[FgWindow]: ...  # top-level windows of the calling thread's desktop

    def pid_alive(self, pid: int) -> bool: ...


def _basename(image: str) -> str:
    return os.path.basename((image or "").replace("\\", "/")) if image else ""


class FgTracer:
    """Foreground / visible-window trace of the calling desktop. `sample()` is one tick (tests drive it directly);
    `start()` runs it on a daemon thread every `period_s`; `stop()` ends the thread, takes a final sample and
    returns the summary. `on_record(dict)` receives each change record (detached mode writes them)."""

    def __init__(self, os_api: FgOS, period_s: float = 0.02, our_pids: Optional[Callable[[], Iterable[int]]] = None,
                 refresh_s: float = 1.0, window_every: int = 10, clock: Callable[[], float] = time.monotonic,
                 on_record: Optional[Callable[[dict], None]] = None):
        self.os, self.period_s = os_api, float(period_s)
        self._our_pids_fn = our_pids
        self.refresh_s, self.window_every = float(refresh_s), max(1, int(window_every))
        self.clock, self.on_record = clock, on_record
        self.ours: set = set()
        self.t0: Optional[float] = None
        self.started_at: Optional[str] = None
        self.ended_at: Optional[str] = None
        self.samples = self.window_samples = self.pid_refresh_errors = self.sample_errors = 0
        self.pid_refreshes = 0
        self._last_refresh: Optional[float] = None
        self._last_fg: Optional[Tuple[int, int]] = None
        self._last_visible: Optional[Tuple[int, bool]] = None
        self.changes: List[Tuple[float, int, str]] = []       # (t, pid, basename) per foreground change
        self.visible: List[Tuple[float, Tuple[int, ...]]] = []  # (t, pids of visible Gemini / ours windows)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    # --- pids ---------------------------------------------------------------------------------------------------
    def add_pids(self, pids: Iterable[int]) -> None:
        with self._lock:
            self.ours.update(int(p) for p in pids)

    def refresh(self, force: bool = False) -> None:
        if self._our_pids_fn is None:
            return
        now = self.clock()
        if not force and self._last_refresh is not None and now - self._last_refresh < self.refresh_s:
            return
        self._last_refresh = now
        try:
            pids = list(self._our_pids_fn() or ())
        except Exception:  # noqa: BLE001 - an inventory failure keeps the pids known so far
            self.pid_refresh_errors += 1
            return
        self.pid_refreshes += 1
        self.add_pids(pids)

    def _is_ours(self, pid: int) -> bool:
        with self._lock:
            return bool(pid) and pid in self.ours

    # --- sampling -----------------------------------------------------------------------------------------------
    def _t(self) -> float:
        if self.t0 is None:
            self.t0 = self.clock()
            self.started_at = now_iso()
        return round(self.clock() - self.t0, 3)

    def _emit(self, rec: dict) -> None:
        if self.on_record is not None:
            self.on_record(rec)

    def sample(self, windows: bool = False) -> None:
        t = self._t()
        self.refresh()
        try:
            self._sample_foreground(t)
            if windows or self.samples % self.window_every == 0:
                self._sample_windows(t)
        except gemini_host.HostRefused:
            raise
        except Exception:  # noqa: BLE001 - one failed read is counted, never fatal
            self.sample_errors += 1
        self.samples += 1

    def _sample_foreground(self, t: float) -> None:
        hwnd, pid = self.os.foreground()
        key = (int(hwnd or 0), int(pid or 0))
        if key == self._last_fg:
            return
        self._last_fg = key
        name = NONE_IMAGE if not key[0] else (_basename(self.os.image(key[1])) if key[1] else "") or UNKNOWN_IMAGE
        if len(self.changes) < MAX_EVENTS:
            self.changes.append((t, key[1], name))
        self._emit({"event": "foreground", "t": t, "image": name, "gemini": name.lower() == APP_IMAGE,
                    "ours": self._is_ours(key[1])})

    def _sample_windows(self, t: float) -> None:
        self.window_samples += 1
        hits = []
        images = {}
        for w in self.os.top_windows():
            if not w.visible:
                continue
            if self._is_ours(w.pid):
                hits.append(w.pid)
                continue
            if w.pid not in images:
                images[w.pid] = _basename(self.os.image(w.pid)).lower() if w.pid else ""
            if images[w.pid] == APP_IMAGE:
                hits.append(w.pid)
        if hits and len(self.visible) < MAX_EVENTS:
            self.visible.append((t, tuple(sorted(set(hits)))))
        state = (len(set(hits)), any(self._is_ours(p) for p in hits))
        if state != self._last_visible:
            self._last_visible = state
            self._emit({"event": "visible", "t": t, "gemini_windows": state[0], "ours": state[1]})

    # --- thread -------------------------------------------------------------------------------------------------
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.sample()
            except gemini_host.HostRefused:
                return
            self._stop.wait(self.period_s)

    def start(self) -> "FgTracer":
        self._t()
        self.refresh(force=True)
        self._thread = threading.Thread(target=self._run, name="agy-dr-fg-tracer", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> dict:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2.0)
        self.refresh(force=True)
        try:
            self.sample(windows=True)   # a final sample: even a very short trace has looked once
        except gemini_host.HostRefused:
            pass
        return self.result()

    # --- summary ------------------------------------------------------------------------------------------------
    def result(self) -> dict:
        """Classified against every pid of ours ever known (an early event is re-classified at the end)."""
        self.ended_at = now_iso()
        with self._lock:
            ours = set(self.ours)
        gem_fg = [c for c in self.changes if c[2].lower() == APP_IMAGE or (c[1] and c[1] in ours)]
        ours_fg = [c for c in gem_fg if c[1] and c[1] in ours]
        ours_vis = [v for v in self.visible if any(p in ours for p in v[1])]
        res = {
            "gemini_foreground_events": len(gem_fg),
            "foreground_changes": len(self.changes),
            "visible_windows": len(self.visible),
            "ours": {"foreground_events": len(ours_fg), "visible_window_samples": len(ours_vis),
                     "pids_known": bool(ours), "pid_refreshes": self.pid_refreshes,
                     "pid_refresh_errors": self.pid_refresh_errors},
            "violated": bool(ours_fg or ours_vis),
            "samples": self.samples,
            "window_samples": self.window_samples,
            "sample_errors": self.sample_errors,
            "period_s": self.period_s,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
        }
        return _scrub(res) if _scrub is not None else res


# --- real OS ---------------------------------------------------------------------------------------------------------
_PROCESS_QUERY_LIMITED = 0x1000
_SYNCHRONIZE = 0x00100000
_WAIT_TIMEOUT = 0x102
_ERROR_ACCESS_DENIED = 5


class Win32FgOS:
    """The real FgOS (ctypes, user32 / kernel32 loaded on first use). Every method first refuses with
    HostRefused("sandbox") under AGY_DR_TEST_SANDBOX=1. Reads only: foreground window, window owner pid and
    visibility, process image name, process liveness. Never a title, never a message, never a focus change."""

    def __init__(self):
        self._w = None

    def _api(self):
        gemini_host.refuse_in_sandbox()
        if self._w is None:
            import ctypes
            import ctypes.wintypes as wt
            u = ctypes.WinDLL("user32", use_last_error=True)
            k = ctypes.WinDLL("kernel32", use_last_error=True)
            u.GetForegroundWindow.restype = wt.HWND
            u.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
            u.IsWindowVisible.argtypes = [wt.HWND]
            enum_proc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
            u.EnumWindows.argtypes = [enum_proc, wt.LPARAM]
            k.OpenProcess.restype = wt.HANDLE
            k.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
            k.CloseHandle.argtypes = [wt.HANDLE]
            k.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
            k.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
            k.WaitForSingleObject.restype = wt.DWORD
            self._w = (ctypes, wt, u, k, enum_proc)
        return self._w

    def _pid_of(self, hwnd) -> int:
        ctypes, wt, u, _, _ = self._api()
        pid = wt.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)

    def foreground(self) -> Tuple[int, int]:
        _, _, u, _, _ = self._api()
        hwnd = u.GetForegroundWindow()
        if not hwnd:
            return 0, 0
        return int(hwnd), self._pid_of(hwnd)

    def image(self, pid: int) -> str:
        ctypes, wt, _, k, _ = self._api()
        h = k.OpenProcess(_PROCESS_QUERY_LIMITED, False, pid)
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            n = wt.DWORD(1024)
            return _basename(buf.value) if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)) else ""
        finally:
            k.CloseHandle(h)

    def top_windows(self) -> List[FgWindow]:
        ctypes, wt, u, _, enum_proc = self._api()
        out: List[FgWindow] = []
        errors: List[BaseException] = []

        def cb(hwnd, _):
            try:
                pid = wt.DWORD()
                u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                out.append(FgWindow(int(hwnd or 0), int(pid.value), bool(u.IsWindowVisible(hwnd))))
                return True
            except BaseException as exc:  # an exception cannot cross the ctypes callback: record it, stop
                errors.append(exc)
                return False

        u.EnumWindows(enum_proc(cb), 0)
        if errors:
            raise errors[0]
        return out

    def pid_alive(self, pid: int) -> bool:
        ctypes, _, _, k, _ = self._api()
        h = k.OpenProcess(_SYNCHRONIZE | _PROCESS_QUERY_LIMITED, False, pid)
        if not h:
            return ctypes.get_last_error() == _ERROR_ACCESS_DENIED   # exists, just not ours to open
        try:
            return k.WaitForSingleObject(h, 0) == _WAIT_TIMEOUT
        finally:
            k.CloseHandle(h)


def _inside_dir(base: str, target: str) -> bool:
    """True only when `target` (after resolving symlinks and '..') lies strictly inside `base`."""
    try:
        base_r = os.path.normcase(os.path.realpath(base))
        target_r = os.path.normcase(os.path.realpath(target))
        if target_r == base_r:
            return False
        return os.path.commonpath([base_r, target_r]) == base_r
    except (OSError, ValueError):
        return False


def _read_driver_state(job: Optional[str]) -> Optional[dict]:
    if not job:
        return None
    path = os.path.join(os.path.realpath(job), DRIVER_STATE_REL)
    if not _inside_dir(job, path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _tree_pids(pid: int, byid: dict) -> List[int]:
    root = gemini_host.Host._root(pid, byid)
    if root is None:
        return []
    return [p.pid for p in gemini_host.Host._tree(root, byid)]


def _session_record_pids(rec: Optional[dict], byid: dict, profile_dir: str, *, require_created: bool) -> List[int]:
    """Verified process tree for a browser_pid recorded in host.json or relay/driver_state.json."""
    if not isinstance(rec, dict):
        return []
    raw_pid = rec.get("browser_pid")
    if isinstance(raw_pid, bool) or not isinstance(raw_pid, int) or raw_pid <= 0:
        return []
    proc = byid.get(raw_pid)
    if proc is None:
        return []
    mode = rec.get("mode") or rec.get("host_mode")
    if require_created:
        if not gemini_host._same_process(rec, proc.pid, proc.created):
            return []
    else:
        at = rec.get("at")
        if isinstance(at, (int, float)) and not isinstance(at, bool):
            if proc.created > float(at) + gemini_host.LAUNCH_SLACK_S:
                return []
    if mode == "D2" and not gemini_host.is_our_instance(proc, profile_dir):
        return []
    if mode == "D1" and gemini_host.parse_user_data_dir(proc.cmdline) is not None:
        return []
    return _tree_pids(raw_pid, byid)


def host_pids_source(host, job: Optional[str] = None,
                     seed_pids: Optional[Callable[[], Iterable[int]]] = None) -> Callable[[], List[int]]:
    """Our verified instance pids from a gemini_host.Host (never the user's instances):
    - every instance Host.instances() lists;
    - any inspected Gemini process (and its verified tree) matching our dedicated data dir (`--user-data-dir`),
      including any Gemini window on the Default desktop whose tree matches the dedicated data dir;
    - the verified process tree of the session recorded in host.json (catches a D1 instance that ended up on the
      Default desktop) and in <job>/relay/driver_state.json, plus any live `seed_pids`."""
    def pids() -> List[int]:
        found: set = set()
        for inst in host.instances():
            found.update(inst.pids)
        procs = []
        os_api = getattr(host, "os", None)
        if os_api is not None and hasattr(os_api, "processes"):
            try:
                procs = list(os_api.processes())
            except gemini_host.HostRefused:
                raise
            except Exception:  # noqa: BLE001 - keep what instances() already found
                procs = []
        byid = {p.pid: p for p in procs if p.inspected and gemini_host.is_gemini_image(p.image)}
        profile_dir = getattr(getattr(host, "cfg", None), "profile_dir", "") or ""
        if profile_dir and byid:
            for p in byid.values():
                if gemini_host.is_our_instance(p, profile_dir):
                    found.update(_tree_pids(p.pid, byid))
            if os_api is not None and hasattr(os_api, "desktop_windows"):
                try:
                    default_wins = list(os_api.desktop_windows(DEFAULT_DESKTOP))
                except gemini_host.HostRefused:
                    raise
                except Exception:  # noqa: BLE001
                    default_wins = []
                for w in default_wins:
                    if w.pid in byid:
                        root = gemini_host.Host._root(w.pid, byid)
                        if root is not None:
                            tree = gemini_host.Host._tree(root, byid)
                            if any(gemini_host.is_our_instance(tp, profile_dir) for tp in tree):
                                found.update(tp.pid for tp in tree)
        host_rec = None
        if hasattr(host, "load_state"):
            try:
                host_rec = host.load_state()
            except Exception:  # noqa: BLE001
                host_rec = None
        found.update(_session_record_pids(host_rec, byid, profile_dir, require_created=True))
        found.update(_session_record_pids(_read_driver_state(job), byid, profile_dir, require_created=False))
        if seed_pids is not None:
            for raw in (seed_pids() or ()):
                sp = int(raw)
                if sp <= 0:
                    continue
                if sp in byid:
                    found.update(_tree_pids(sp, byid))
                else:
                    found.add(sp)
        return sorted(found)
    return pids


def default_parts(job: Optional[str] = None,
                  seed_pids: Optional[Callable[[], Iterable[int]]] = None) -> Tuple[FgOS, Callable[[], List[int]]]:
    """The real FgOS and the real Host pid source (both refused inside the test sandbox)."""
    gemini_host.refuse_in_sandbox()
    host = gemini_host.Host(gemini_host.Win32HostOS(),
                            gemini_host.HostConfig(stub="", profile_dir=gemini_host.default_profile_dir()))
    return Win32FgOS(), host_pids_source(host, job=job, seed_pids=seed_pids)


# --- detached mode ---------------------------------------------------------------------------------------------------
class JsonlWriter:
    """Appends one scrubbed JSON object per line (flushed per record) to <job>/relay/foreground.jsonl."""

    def __init__(self, path: str, run_id: str, job: Optional[str] = None):
        if job is not None:
            parent = os.path.dirname(path)
            if not _inside_dir(job, parent) or not _inside_dir(job, path):
                raise ValueError("writer path resolves outside the job directory")
        self.path, self.run_id = path, run_id
        self.job = os.path.realpath(job) if job is not None else None
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if self.job is not None and not _inside_dir(self.job, self.path):
            raise ValueError("writer path resolves outside the job directory")

    def __call__(self, rec: dict) -> None:
        if self.job is not None and not _inside_dir(self.job, self.path):
            raise ValueError("writer path resolves outside the job directory")
        rec = dict(rec, run=self.run_id)
        if _scrub is not None:
            rec = _scrub(rec)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
            fh.flush()


def jsonl_path(job: str) -> str:
    base = os.path.realpath(job)
    relay = os.path.join(base, "relay")
    path = os.path.join(relay, JSONL_NAME)
    if not _inside_dir(base, relay) or not _inside_dir(base, path):
        raise ValueError("foreground.jsonl resolves outside the job directory")
    return os.path.realpath(path)


def run_detached(job: str, watch_pid: Optional[int], os_api: FgOS,
                 our_pids: Optional[Callable[[], Iterable[int]]],
                 run_id: str = "", max_s: float = DEFAULT_MAX_S, period_s: float = 0.02,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 stop_event: Optional[threading.Event] = None,
                 stop_files: Iterable[str] = ()) -> dict:
    """Trace until the watched pid ends, a stop file appears, `stop_event` is set, or `max_s` passes; returns the
    end record."""
    job_real = os.path.realpath(job)
    stop_list = []
    for sf in stop_files:
        if not _inside_dir(job_real, sf):
            raise ValueError("stop file resolves outside the job directory")
        stop_list.append(sf)
    writer = JsonlWriter(jsonl_path(job_real), run_id, job=job_real)
    tracer = FgTracer(os_api, period_s=period_s, our_pids=our_pids, clock=clock, on_record=writer)
    t0 = clock()
    writer({"event": "start", "at": now_iso(), "period_s": period_s, "max_s": max_s})
    tracer.refresh(force=True)
    reason = None
    last_watch = None
    error = None
    while reason is None:
        try:
            tracer.sample()
        except gemini_host.HostRefused as exc:
            reason, error = "refused", exc.reason
            break
        now = clock()
        if stop_event is not None and stop_event.is_set():
            reason = "stopped"
        elif stop_list and any(os.path.exists(sf) for sf in stop_list):
            reason = "stop_file"
        elif watch_pid and (last_watch is None or now - last_watch >= WATCH_EVERY_S):
            last_watch = now
            try:
                alive = os_api.pid_alive(watch_pid)
            except gemini_host.HostRefused as exc:
                reason, error = "refused", exc.reason
                break
            except Exception:  # noqa: BLE001 - cannot tell: keep tracing until max_s
                alive = True
            if not alive:
                reason = "watched_pid_ended"
        if reason is None and now - t0 >= max_s:
            reason = "max_duration"
        if reason is None:
            sleep(period_s)
    tracer.refresh(force=True)
    if reason != "refused":
        tracer.sample(windows=True)
    summary = tracer.result()
    end = dict(summary, event="end", reason=reason)
    if error:
        end["error"] = error
    writer(end)
    return end


def _stdin_eof_event() -> threading.Event:
    ev = threading.Event()

    def read():
        try:
            while sys.stdin.buffer.read(4096):
                pass
        except Exception:  # noqa: BLE001 - a broken pipe also means stop
            pass
        ev.set()

    threading.Thread(target=read, name="agy-dr-fg-stdin", daemon=True).start()
    return ev


def main(argv=None, parts_factory: Optional[Callable[[], Tuple[FgOS, Callable[[], Iterable[int]]]]] = None) -> int:
    p = argparse.ArgumentParser(description="R6 foreground tracer (detached mode): writes <job>/relay/foreground.jsonl")
    p.add_argument("--job", required=True, help="job directory")
    p.add_argument("--watch-pid", type=int, default=None, help="exit when this process ends")
    p.add_argument("--stop-file", action="append", default=[],
                   help="exit when this file (resolved inside --job) appears")
    p.add_argument("--seed-pid", action="append", type=int, default=[],
                   help="additional verified pid of our instance")
    p.add_argument("--run-id", default="", help="tag written on every record")
    p.add_argument("--max-s", type=float, default=DEFAULT_MAX_S, help="exit after this many seconds")
    p.add_argument("--stop-on-eof", action="store_true", help="exit when stdin reaches EOF")
    try:
        a = p.parse_args(argv)
    except SystemExit:
        return 5
    job = os.path.realpath(a.job)
    if not os.path.isdir(job) or a.max_s <= 0:
        return 5
    if a.watch_pid is not None and a.watch_pid <= 0:
        return 5
    if a.watch_pid is None and not a.stop_file and not a.stop_on_eof:
        return 5
    if any(sp <= 0 for sp in a.seed_pid):
        return 5
    try:
        out_path = jsonl_path(job)
    except ValueError:
        return 5
    for sf in a.stop_file:
        if not _inside_dir(job, sf):
            return 5
    seed_fn = (lambda: list(a.seed_pid)) if a.seed_pid else None
    try:
        if parts_factory is not None:
            os_api, base_pids = parts_factory()
            if seed_fn is not None:
                our_pids = (lambda: sorted(set(base_pids() if base_pids is not None else ()) | set(seed_fn())))
            else:
                our_pids = base_pids
        else:
            os_api, our_pids = default_parts(job=job, seed_pids=seed_fn)
    except gemini_host.HostRefused as exc:
        JsonlWriter(out_path, a.run_id, job=job)({"event": "end", "reason": "refused", "error": exc.reason})
        return 3
    stop = _stdin_eof_event() if a.stop_on_eof else None
    try:
        end = run_detached(job, a.watch_pid, os_api, our_pids, run_id=a.run_id, max_s=a.max_s,
                           stop_event=stop, stop_files=a.stop_file)
    except ValueError:
        return 5
    return 3 if end.get("reason") == "refused" else 0


if __name__ == "__main__":
    sys.exit(main())

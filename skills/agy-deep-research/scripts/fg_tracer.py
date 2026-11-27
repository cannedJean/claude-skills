#!/usr/bin/env python3
r"""fg_tracer.py - R6 invariant tracer: the driver never changes the default desktop's foreground.

`FgTracer(os_api, period_s=0.02, our_pids=...)` samples the foreground window of the desktop it runs on (the
default, input desktop) every `period_s`, and every `window_every` samples the visible top-level windows there.
It records, per change, only the foreground process image basename and whether it is a Gemini app process and
whether it belongs to OUR instance. "Ours" = a pid of our verified instance tree: `our_pids()` is a callable that
returns the pids (in detached mode gemini_host.Host.instances(), which never lists the user's instances); it is
called again every `refresh_s` because our instance may start after the tracer, and every pid it ever returned
stays ours for the tracer's lifetime, so a foreground event seen before the pid was known is re-classified at
stop(). The user's own Gemini windows are visible and come to the foreground on the default desktop all the time
(live 2026-11-27: 58 foreground changes, ours 0): they are counted (`gemini_foreground_events`,
`visible_windows`) but are never a violation. Violation = one of our pids owned the foreground window, or owned a
visible top-level window on this desktop (`ours.foreground_events` / `ours.visible_window_samples` > 0).

No window title is ever read. The OS surface is `FgOS` (injectable); `Win32FgOS` is the real one (ctypes, loaded
lazily) and refuses every call with gemini_host.HostRefused("sandbox") under AGY_DR_TEST_SANDBOX=1.

Detached mode (started by relay_step.py around a plan / edit driver run, on the default desktop):

    python fg_tracer.py --job <job dir> --watch-pid <pid> [--run-id ID] [--max-s SEC] [--stop-on-eof]

appends one scrubbed JSON object per line to <job>/relay/foreground.jsonl (protected from agy by the agy-delegate
confine hook): a `start` record, one `foreground` record per foreground change, one `visible` record per change of
the visible-Gemini-window state, and an `end` record with the summary. Every record carries `run` and holds only
event names, times, counts, process image basenames and booleans. It exits when the watched pid ends, when stdin
reaches EOF (`--stop-on-eof`: the relay closes the pipe to stop it), or after `--max-s` seconds.
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


def host_pids_source(host) -> Callable[[], List[int]]:
    """Our verified instance pids from a gemini_host.Host (never the user's instances)."""
    def pids() -> List[int]:
        return [pid for inst in host.instances() for pid in inst.pids]
    return pids


def default_parts() -> Tuple[FgOS, Callable[[], List[int]]]:
    """The real FgOS and the real Host pid source (both refused inside the test sandbox)."""
    gemini_host.refuse_in_sandbox()
    host = gemini_host.Host(gemini_host.Win32HostOS(),
                            gemini_host.HostConfig(stub="", profile_dir=gemini_host.default_profile_dir()))
    return Win32FgOS(), host_pids_source(host)


# --- detached mode ---------------------------------------------------------------------------------------------------
class JsonlWriter:
    """Appends one scrubbed JSON object per line (flushed per record) to <job>/relay/foreground.jsonl."""

    def __init__(self, path: str, run_id: str):
        self.path, self.run_id = path, run_id
        os.makedirs(os.path.dirname(path), exist_ok=True)

    def __call__(self, rec: dict) -> None:
        rec = dict(rec, run=self.run_id)
        if _scrub is not None:
            rec = _scrub(rec)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
            fh.flush()


def jsonl_path(job: str) -> str:
    return os.path.join(job, "relay", JSONL_NAME)


def run_detached(job: str, watch_pid: int, os_api: FgOS, our_pids: Optional[Callable[[], Iterable[int]]],
                 run_id: str = "", max_s: float = DEFAULT_MAX_S, period_s: float = 0.02,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 stop_event: Optional[threading.Event] = None) -> dict:
    """Trace until the watched pid ends, `stop_event` is set or `max_s` passes; returns the end record."""
    writer = JsonlWriter(jsonl_path(job), run_id)
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
        elif last_watch is None or now - last_watch >= WATCH_EVERY_S:
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
    p.add_argument("--watch-pid", type=int, required=True, help="exit when this process ends")
    p.add_argument("--run-id", default="", help="tag written on every record")
    p.add_argument("--max-s", type=float, default=DEFAULT_MAX_S, help="exit after this many seconds")
    p.add_argument("--stop-on-eof", action="store_true", help="exit when stdin reaches EOF")
    try:
        a = p.parse_args(argv)
    except SystemExit:
        return 5
    job = os.path.abspath(a.job)
    if not os.path.isdir(job) or a.watch_pid <= 0 or a.max_s <= 0:
        return 5
    try:
        os_api, our_pids = (parts_factory or default_parts)()
    except gemini_host.HostRefused as exc:
        JsonlWriter(jsonl_path(job), a.run_id)({"event": "end", "reason": "refused", "error": exc.reason})
        return 3
    stop = _stdin_eof_event() if a.stop_on_eof else None
    end = run_detached(job, a.watch_pid, os_api, our_pids, run_id=a.run_id, max_s=a.max_s, stop_event=stop)
    return 3 if end.get("reason") == "refused" else 0


if __name__ == "__main__":
    sys.exit(main())

import sandbox  # noqa: F401  (must stay the first import: blocks real-app access)
# test_gemini_host_win32.py - Host orchestration (ensure / instances / close / lock) against a fake HostOS.
# No real desktop, window, process or PowerShell is touched: Host only talks to FakeOS, and Win32HostOS is only
# called to prove it refuses everything inside the test sandbox.
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gemini_host as gh  # noqa: E402

STUB = r"C:\Users\Me\AppData\Local\Google\Gemini\Gemini.exe"
APP = r"C:\Users\Me\AppData\Local\Google\Gemini\app-1.2.3\Gemini.exe"
PROFILE = r"C:\Users\Me\AppData\Local\agy-deep-research\gemini-profile"
OURS = gh.DESKTOP_NAME
UDD = "--user-data-dir=" + PROFILE
T0 = 1_000_000.0


class FakeOS:
    """In-memory HostOS. `sleep` advances the clock and fires scheduled events (time, fn)."""

    def __init__(self):
        self.now = T0
        self.desktops = {"Default"}
        self.windows = {}  # desktop -> [WindowInfo]
        self.procs = {}  # pid -> ProcInfo
        self.local_state = {}
        self.calls = []
        self.events = []
        self.on_launch = None
        self.on_post_close = None
        self.next_pid = 5000

    # --- scenario helpers ---------------------------------------------------------------------------------------
    def add_proc(self, pid, ppid, cmdline, image=APP, created=None):
        self.procs[pid] = gh.ProcInfo(pid, ppid, image, cmdline, self.now if created is None else created)
        return pid

    def add_window(self, desktop, pid, main=True, hwnd=None):
        self.desktops.add(desktop)
        hwnd = hwnd or (pid * 10 + len(self.windows.get(desktop, [])))
        self.windows.setdefault(desktop, []).append(
            gh.WindowInfo(hwnd, pid, "Chrome_WidgetWin_1", True, main))

    def kill(self, pid):
        self.procs.pop(pid, None)
        for d in list(self.windows):
            self.windows[d] = [w for w in self.windows[d] if w.pid != pid]
        # the desktop object vanishes with the last window of the instance on it
        if OURS in self.desktops and not self.windows.get(OURS) and OURS not in getattr(self, "_held", set()):
            self.desktops.discard(OURS)

    def at(self, delay, fn):
        self.events.append((self.now + delay, fn))

    def clock(self):
        return self.now

    def sleep(self, s):
        self.now += s
        due = [e for e in self.events if e[0] <= self.now]
        self.events = [e for e in self.events if e[0] > self.now]
        for _, fn in sorted(due, key=lambda e: e[0]):
            fn()

    # --- HostOS ---------------------------------------------------------------------------------------------------
    def open_desktop(self, name):
        self.calls.append(("open_desktop", name))
        return name in self.desktops

    def create_desktop(self, name):
        self.calls.append(("create_desktop", name))
        self.desktops.add(name)
        self._held = {name}

    def enum_desktops(self):
        self.calls.append(("enum_desktops",))
        return sorted(self.desktops)

    def desktop_windows(self, name):
        self.calls.append(("desktop_windows", name))
        return list(self.windows.get(name, []))

    def processes(self):
        self.calls.append(("processes",))
        return list(self.procs.values())

    def launch(self, cmdline, desktop, env):
        self.calls.append(("launch", cmdline, desktop, dict(env)))
        stub = self.next_pid
        self.next_pid += 100
        self.add_proc(stub, 1, cmdline, image=STUB)
        self.at(0.5, lambda: self.procs.pop(stub, None))  # the stub hands off and exits (exit code 2)
        if self.on_launch:
            self.on_launch(cmdline, desktop)
        return stub

    def terminate(self, pid, created=None):
        self.calls.append(("terminate", pid))
        self.kill(pid)

    def post_close(self, desktop, hwnds):
        self.calls.append(("post_close", desktop, tuple(hwnds)))
        if self.on_post_close:
            self.on_post_close()
        return len(hwnds)

    def local_state_mtime(self, profile):
        self.calls.append(("local_state_mtime", profile))
        return self.local_state.get(profile)

    # --- queries --------------------------------------------------------------------------------------------------
    def launches(self):
        return [c for c in self.calls if c[0] == "launch"]

    def terminated(self):
        return [c[1] for c in self.calls if c[0] == "terminate"]


def start_d2_instance(fake, browser=7000, window_at=2.0, state_at=3.0, desktop=OURS):
    """Schedule our dedicated-profile browser (+ renderer) and its window on `desktop`."""
    def spawn():
        fake.add_proc(browser, 4242, '"%s" %s' % (APP, UDD))
        fake.add_proc(browser + 1, browser, '"%s" %s renderer' % (APP, UDD))
    fake.at(1.0, spawn)
    fake.at(window_at, lambda: fake.add_window(desktop, browser))
    fake.at(state_at, lambda: fake.local_state.__setitem__(PROFILE, fake.now))


def make_host(fake, lock_path=None, active_job=lambda: False, env=None):
    cfg = gh.HostConfig(stub=STUB, profile_dir=PROFILE, lock_path=lock_path or os.path.join(
        tempfile.mkdtemp(prefix="agy-dr-lock-"), "host.lock"), clock=fake.clock, sleep=fake.sleep,
        active_job=active_job, environ=env if env is not None else {"PATH": "x", "NODE_OPTIONS": "y",
                                                                    "ELECTRON_RUN_AS_NODE": "1"})
    return gh.Host(fake, cfg)


def add_user_instance(fake, browser=100):
    fake.add_proc(browser, 50, '"%s"' % APP, created=T0 - 3600)
    fake.add_proc(browser + 1, browser, '"%s" renderer' % APP, created=T0 - 3600)
    fake.add_window("Default", browser)


class EnsureTests(unittest.TestCase):
    def test_d2_ok_path(self):
        fake = FakeOS()
        fake.on_launch = lambda cmd, desk: start_d2_instance(fake)
        s = make_host(fake).ensure(None)
        self.assertEqual((s.mode, s.desktop, s.browser_pid, s.reason), ("D2", OURS, 7000, None))
        self.assertIn(("create_desktop", OURS), fake.calls)
        (launch,) = fake.launches()
        self.assertEqual(launch[1], gh.build_launch_command(STUB, PROFILE, gh.ALLOWED_FLAG_SWITCHES))
        self.assertEqual(launch[2], "WinSta0\\" + OURS)
        self.assertEqual(launch[3], {"PATH": "x"})  # clean_env: no NODE_* / ELECTRON_*

    def test_d2_blocked_with_user_app_is_gemini_in_use_and_never_launches_d1(self):
        fake = FakeOS()
        add_user_instance(fake)

        def guard_exit(cmd, desk):  # our app process starts, then the guard ends it
            fake.at(1.0, lambda: fake.add_proc(7000, 4242, '"%s" %s' % (APP, UDD)))
            fake.at(2.0, lambda: fake.kill(7000))
        fake.on_launch = guard_exit
        with self.assertRaises(gh.HostError) as cm:
            make_host(fake).ensure(None)
        self.assertEqual(cm.exception.code, "GEMINI_IN_USE")
        self.assertEqual(len(fake.launches()), 1)  # only the D2 attempt
        self.assertEqual(fake.terminated(), [])  # the user's app is never touched

    def test_d2_blocked_without_user_app_falls_back_to_d1(self):
        fake = FakeOS()

        def guard_exit(cmd, desk):
            if UDD in cmd:
                fake.at(1.0, lambda: fake.add_proc(7000, 4242, '"%s" %s' % (APP, UDD)))
                fake.at(2.0, lambda: fake.kill(7000))
            else:
                fake.at(1.0, lambda: fake.add_proc(8000, 4343, '"%s"' % APP))
                fake.at(2.0, lambda: fake.add_window(OURS, 8000))
        fake.on_launch = guard_exit
        s = make_host(fake).ensure(None)
        self.assertEqual((s.mode, s.browser_pid, s.reason), ("D1", 8000, "profile_switch_ignored"))
        cmds = [c[1] for c in fake.launches()]
        self.assertEqual(cmds, [gh.build_launch_command(STUB, PROFILE, gh.ALLOWED_FLAG_SWITCHES),
                                gh.build_launch_command(STUB, None, [])])

    def test_switch_ignored_instance_on_our_desktop_is_reused_as_d1(self):
        fake = FakeOS()

        def ignored(cmd, desk):  # the stub drops the switch: default profile, window on our desktop
            fake.at(1.0, lambda: fake.add_proc(8000, 4343, '"%s"' % APP))
            fake.at(2.0, lambda: fake.add_window(OURS, 8000))
        fake.on_launch = ignored
        s = make_host(fake).ensure(None)
        self.assertEqual((s.mode, s.browser_pid, s.reason), ("D1", 8000, "profile_switch_ignored"))
        self.assertEqual(len(fake.launches()), 1)

    def test_d2_slow_start_is_pending_then_ok(self):
        fake = FakeOS()
        fake.on_launch = lambda cmd, desk: start_d2_instance(fake, window_at=20.0, state_at=22.0)
        s = make_host(fake).ensure(None)
        self.assertEqual(s.mode, "D2")
        self.assertGreater(fake.now - T0, 15.0)
        self.assertEqual(len(fake.launches()), 1)

    def test_d2_timeout(self):
        fake = FakeOS()
        with self.assertRaises(gh.HostError) as cm:
            make_host(fake).ensure(None)
        self.assertEqual(cm.exception.code, "HOST_START_TIMEOUT")
        self.assertEqual(len(fake.launches()), 1)

    def test_pinned_d1_without_user_app_launches_bare_stub_only(self):
        fake = FakeOS()

        def d1(cmd, desk):
            fake.at(1.0, lambda: fake.add_proc(8000, 4343, '"%s"' % APP))
            fake.at(2.0, lambda: fake.add_window(OURS, 8000))
        fake.on_launch = d1
        s = make_host(fake).ensure("D1")
        self.assertEqual((s.mode, s.browser_pid), ("D1", 8000))
        self.assertEqual([c[1] for c in fake.launches()], [gh.build_launch_command(STUB, None, [])])

    def test_pinned_d1_with_user_app_refuses_without_launch(self):
        fake = FakeOS()
        add_user_instance(fake)
        with self.assertRaises(gh.HostError) as cm:
            make_host(fake).ensure("D1")
        self.assertEqual(cm.exception.code, "GEMINI_IN_USE")
        self.assertEqual(fake.launches(), [])
        self.assertNotIn(("create_desktop", OURS), fake.calls)

    def test_pinned_d2_blocked_is_mode_unavailable_without_d1(self):
        fake = FakeOS()

        def guard_exit(cmd, desk):
            fake.at(1.0, lambda: fake.add_proc(7000, 4242, '"%s" %s' % (APP, UDD)))
            fake.at(2.0, lambda: fake.kill(7000))
        fake.on_launch = guard_exit
        with self.assertRaises(gh.HostError) as cm:
            make_host(fake).ensure("D2")
        self.assertEqual(cm.exception.code, "HOST_MODE_UNAVAILABLE")
        self.assertEqual(len(fake.launches()), 1)

    def test_running_instance_on_our_desktop_is_reused(self):
        fake = FakeOS()
        fake.add_proc(7000, 1, '"%s" %s' % (APP, UDD))
        fake.add_window(OURS, 7000)
        s = make_host(fake).ensure(None)
        self.assertEqual((s.mode, s.browser_pid, s.reason), ("D2", 7000, "reused"))
        self.assertEqual(fake.launches(), [])

    def test_reused_instance_mode_must_match_pinned_mode(self):
        fake = FakeOS()
        fake.add_proc(8000, 1, '"%s"' % APP)
        fake.add_window(OURS, 8000)
        with self.assertRaises(gh.HostError) as cm:
            make_host(fake).ensure("D2")
        self.assertEqual(cm.exception.code, "HOST_MODE_UNAVAILABLE")
        self.assertEqual(fake.launches(), [])

    def test_dedicated_profile_running_elsewhere_is_not_relaunched(self):
        fake = FakeOS()
        fake.add_proc(7000, 1, '"%s" %s' % (APP, UDD))
        fake.add_window("Default", 7000)  # e.g. a visible signin window
        with self.assertRaises(gh.HostError) as cm:
            make_host(fake).ensure(None)
        self.assertEqual(cm.exception.code, "HOST_BUSY")
        self.assertEqual(fake.launches(), [])


class InstanceTests(unittest.TestCase):
    def _fake(self):
        fake = FakeOS()
        add_user_instance(fake)
        fake.add_proc(7000, 1, '"%s" %s' % (APP, UDD))
        fake.add_proc(7001, 7000, '"%s" %s renderer' % (APP, UDD))
        fake.add_window(OURS, 7000)
        return fake

    def test_orphan_when_no_active_job(self):
        (inst,) = make_host(self._fake()).instances()
        self.assertEqual((inst.desktop, inst.browser_pid, inst.mode, inst.orphan), (OURS, 7000, "D2", True))
        self.assertEqual(sorted(inst.pids), [7000, 7001])
        self.assertEqual(gh._canon(inst.profile), gh._canon(PROFILE))

    def test_not_orphan_with_active_job(self):
        (inst,) = make_host(self._fake(), active_job=lambda: True).instances()
        self.assertFalse(inst.orphan)

    def test_windowless_dedicated_profile_instance_is_listed(self):
        fake = FakeOS()
        add_user_instance(fake)
        fake.add_proc(7000, 1, '"%s" %s' % (APP, UDD))
        (inst,) = make_host(fake).instances()
        self.assertEqual((inst.desktop, inst.pids, inst.orphan), (None, [7000], True))

    def test_d1_instance_on_our_desktop(self):
        fake = FakeOS()
        add_user_instance(fake)
        fake.add_proc(8000, 1, '"%s"' % APP)
        fake.add_window(OURS, 8000)
        (inst,) = make_host(fake).instances()
        self.assertEqual((inst.desktop, inst.mode, inst.profile, inst.pids), (OURS, "D1", None, [8000]))


class CloseTests(unittest.TestCase):
    def _fake(self, d1=False):
        fake = FakeOS()
        add_user_instance(fake)  # pids 100, 101 on Default
        udd = "" if d1 else " " + UDD
        fake.add_proc(7000, 1, '"%s"%s' % (APP, udd))
        fake.add_proc(7001, 7000, '"%s"%s renderer' % (APP, udd))
        fake.add_proc(7002, 7000, '"%s"%s gpu' % (APP, udd))
        fake.add_proc(7003, 7001, r'"C:\Windows\notepad.exe"', image=r"C:\Windows\notepad.exe")  # not Gemini
        fake.add_window(OURS, 7000)
        return fake

    def test_close_terminates_only_our_verified_tree(self):
        for d1 in (False, True):
            with self.subTest(d1=d1):
                fake = self._fake(d1)
                host = make_host(fake)
                (inst,) = host.instances()
                menu = []
                res = host.close(inst, quit_via_menu=lambda desktop: menu.append(desktop))
                self.assertEqual(menu, [OURS])
                self.assertEqual(sorted(fake.terminated()), [7000, 7001, 7002])
                self.assertEqual(res.remaining, 0)
                self.assertEqual(res.method, "terminate")
                self.assertIn(100, fake.procs)
                self.assertIn(101, fake.procs)
                self.assertIn(7003, fake.procs)
                self.assertTrue(any(c[0] == "post_close" and c[1] == OURS for c in fake.calls))

    def test_menu_quit_avoids_terminate(self):
        fake = self._fake()
        host = make_host(fake)
        (inst,) = host.instances()

        def quit_menu(desktop):
            fake.at(2.0, lambda: [fake.kill(p) for p in (7001, 7002, 7000)])
        res = host.close(inst, quit_via_menu=quit_menu)
        self.assertEqual((res.remaining, res.method), (0, "menu"))
        self.assertEqual(fake.terminated(), [])
        self.assertFalse(any(c[0] == "post_close" for c in fake.calls))
        self.assertTrue(res.desktop_gone)

    def test_menu_failure_falls_through_to_wm_close(self):
        fake = self._fake()
        host = make_host(fake)
        (inst,) = host.instances()
        fake.on_post_close = lambda: fake.at(1.0, lambda: [fake.kill(p) for p in (7001, 7002, 7000)])

        def broken(desktop):
            raise RuntimeError("menu not found")
        res = host.close(inst, quit_via_menu=broken)
        self.assertEqual((res.remaining, res.method), (0, "wm_close"))
        self.assertEqual(fake.terminated(), [])

    def test_close_session_from_ensure(self):
        fake = FakeOS()
        fake.on_launch = lambda cmd, desk: start_d2_instance(fake)
        host = make_host(fake)
        s = host.ensure(None)
        add_user_instance(fake)
        res = host.close(s, quit_via_menu=lambda d: None)
        self.assertEqual(sorted(fake.terminated()), [7000, 7001])
        self.assertEqual(res.remaining, 0)

    def test_reused_pid_is_not_terminated(self):
        fake = self._fake()
        host = make_host(fake)
        (inst,) = host.instances()
        # our browser exits and its pid is reused by the user's new Gemini instance on the Default desktop
        fake.kill(7000)
        fake.add_proc(7000, 50, '"%s"' % APP, created=fake.now + 1)
        fake.add_window("Default", 7000)
        host.close(inst, quit_via_menu=lambda d: None)
        self.assertNotIn(7000, fake.terminated())
        self.assertIn(7000, fake.procs)

    def test_nothing_to_close(self):
        fake = FakeOS()
        add_user_instance(fake)
        host = make_host(fake)
        res = host.close(gh.InstanceInfo(OURS, [], None, None, "D2", True), quit_via_menu=lambda d: None)
        self.assertEqual((res.remaining, res.method), (0, "none"))
        self.assertEqual(fake.terminated(), [])


class LockTests(unittest.TestCase):
    def test_lock_contention_is_host_busy(self):
        path = os.path.join(tempfile.mkdtemp(prefix="agy-dr-lock-"), "sub", "host.lock")
        a, b = make_host(FakeOS(), lock_path=path), make_host(FakeOS(), lock_path=path)
        with a.lock():
            with self.assertRaises(gh.HostError) as cm:
                with b.lock():
                    pass
            self.assertEqual(cm.exception.code, "HOST_BUSY")
        with b.lock():  # released
            pass

    def test_default_lock_path_is_under_localappdata(self):
        cfg = gh.HostConfig(stub=STUB, profile_dir=PROFILE)
        self.assertEqual(cfg.lock_path, os.path.join(os.environ["LOCALAPPDATA"], "agy-deep-research", "host.lock"))


class Win32SandboxTests(unittest.TestCase):
    def test_every_call_refused_in_sandbox(self):
        self.assertEqual(os.environ.get("AGY_DR_TEST_SANDBOX"), "1")
        w = gh.Win32HostOS()
        calls = [lambda: w.open_desktop(OURS), lambda: w.create_desktop(OURS), w.enum_desktops,
                 lambda: w.desktop_windows(OURS), w.processes, lambda: w.launch("x", "WinSta0\\" + OURS, {}),
                 lambda: w.terminate(1), lambda: w.post_close(OURS, [1]), lambda: w.local_state_mtime(PROFILE)]
        for call in calls:
            with self.assertRaises(gh.HostRefused) as cm:
                call()
            self.assertEqual(cm.exception.reason, "sandbox")

    def test_host_refused_is_one_class(self):
        import uia_backend
        self.assertIs(uia_backend.HostRefused, gh.HostRefused)


if __name__ == "__main__":
    unittest.main()

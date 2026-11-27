import sandbox  # noqa: F401  (must stay the first import: blocks real-app access)
# test_fg_tracer.py - the R6 foreground tracer (fg_tracer.py) and its deep_research.py / relay_step.py wiring,
# on a fake FgOS only. No real window, process or tracer process is touched: Popen is mocked/injected, and the
# real Win32FgOS refuses in the sandbox.
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

import deep_research as dr  # noqa: E402
import fg_tracer as ft  # noqa: E402
import gemini_host as gh  # noqa: E402
import relay_step  # noqa: E402
from test_gemini_host_win32 import (APP, OURS as OURS_DESKTOP, PROFILE, UDD, FakeOS,  # noqa: E402
                                    add_user_instance, make_host)

USER_GEMINI = 100      # the user's own Gemini on the default desktop
OURS = 7000            # our verified instance (hidden desktop)
EXPLORER = 50
SECRET_TITLE = "비밀 대화 제목 test@example.invalid"
VALID_REPORT_MD = "# Report Title\n\n" + ("Detailed research finding with empirical benchmarks. " * 15) + "\n"


class FakeFg:
    """In-memory FgOS. `foreground` / `windows` are what the default desktop shows right now."""

    def __init__(self):
        self.fg = (500, EXPLORER)
        self.images = {EXPLORER: "explorer.exe", USER_GEMINI: "Gemini.exe", OURS: "Gemini.exe", 60: "chrome.exe"}
        self.windows = []
        self.alive = set()
        self.calls = 0

    def foreground(self):
        self.calls += 1
        return self.fg

    def image(self, pid):
        return self.images.get(pid, "")

    def top_windows(self):
        return list(self.windows)

    def pid_alive(self, pid):
        return pid in self.alive


def window(pid, visible=True, hwnd=None):
    w = ft.FgWindow(hwnd or pid * 10, pid, visible)
    w.title = SECRET_TITLE   # a fake carries a title; the tracer must never read or write it
    return w


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.hooks = []   # (at, fn)

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.now += s
        due = [h for h in self.hooks if h[0] <= self.now]
        self.hooks = [h for h in self.hooks if h[0] > self.now]
        for _, fn in due:
            fn()


def tracer(fake, pids=(OURS,), clock=None, **kw):
    clock = clock or Clock()
    return ft.FgTracer(fake, our_pids=lambda: list(pids), clock=clock, **kw), clock


class FgTracerTests(unittest.TestCase):
    def test_result_shape(self):
        t, _ = tracer(FakeFg())
        t.sample()
        res = t.result()
        for key in ("gemini_foreground_events", "ours", "visible_windows", "samples", "started_at", "ended_at",
                    "violated", "foreground_changes"):
            self.assertIn(key, res)
        self.assertEqual(set(res["ours"]) >= {"foreground_events", "visible_window_samples"}, True)
        self.assertEqual(res["period_s"], 0.02)

    def test_user_gemini_foreground_and_window_never_count_as_ours(self):
        fake = FakeFg()
        fake.windows = [window(USER_GEMINI), window(EXPLORER)]
        t, clock = tracer(fake)
        t.sample()
        fake.fg = (1000, USER_GEMINI)
        for _ in range(25):
            clock.sleep(0.02)
            t.sample()
        fake.fg = (500, EXPLORER)
        t.sample()
        res = t.result()
        self.assertEqual(res["gemini_foreground_events"], 1)
        self.assertGreater(res["visible_windows"], 0)
        self.assertEqual((res["ours"]["foreground_events"], res["ours"]["visible_window_samples"]), (0, 0))
        self.assertFalse(res["violated"])

    def test_our_instance_in_the_foreground_is_a_violation(self):
        fake = FakeFg()
        t, _ = tracer(fake)
        t.sample()
        fake.fg = (7777, OURS)
        t.sample()
        res = t.result()
        self.assertEqual(res["ours"]["foreground_events"], 1)
        self.assertTrue(res["violated"])

    def test_our_window_visible_but_not_foreground_is_a_violation(self):
        fake = FakeFg()
        fake.windows = [window(OURS)]
        t, _ = tracer(fake)
        t.sample()   # sample 0 looks at the windows
        res = t.result()
        self.assertEqual(res["ours"]["foreground_events"], 0)
        self.assertEqual(res["ours"]["visible_window_samples"], 1)
        self.assertTrue(res["violated"])

    def test_our_invisible_window_is_no_violation(self):
        fake = FakeFg()
        fake.windows = [window(OURS, visible=False)]
        t, _ = tracer(fake)
        t.sample()
        self.assertFalse(t.result()["violated"])

    def test_ours_is_decided_by_pid_not_by_image(self):
        fake = FakeFg()
        fake.images[OURS] = ""   # image unreadable: still ours by pid
        t, _ = tracer(fake)
        t.sample()
        fake.fg = (7777, OURS)
        t.sample()
        self.assertTrue(t.result()["violated"])

    def test_pids_learned_later_reclassify_earlier_events(self):
        fake = FakeFg()
        known = []
        clock = Clock()
        t = ft.FgTracer(fake, our_pids=lambda: list(known), clock=clock, refresh_s=1.0)
        t.sample()
        fake.fg = (7777, OURS)   # our instance shows up before the inventory lists it
        clock.sleep(0.02)
        t.sample()
        fake.fg = (500, EXPLORER)
        clock.sleep(0.02)
        t.sample()
        self.assertFalse(t.result()["violated"])
        known.append(OURS)
        clock.sleep(1.5)
        t.sample()   # refresh due
        res = t.result()
        self.assertTrue(res["violated"])
        self.assertEqual(res["ours"]["foreground_events"], 1)

    def test_pid_source_failure_is_counted_and_keeps_known_pids(self):
        fake = FakeFg()
        clock = Clock()
        calls = []

        def pids():
            calls.append(1)
            if len(calls) > 1:
                raise OSError("inventory failed")
            return [OURS]

        t = ft.FgTracer(fake, our_pids=pids, clock=clock, refresh_s=0.5)
        t.sample()
        clock.sleep(1.0)
        t.sample()
        fake.fg = (7777, OURS)
        t.sample()
        res = t.result()
        self.assertEqual(res["ours"]["pid_refresh_errors"], 1)
        self.assertTrue(res["violated"])

    def test_window_check_every_n_samples(self):
        fake = FakeFg()
        t, _ = tracer(fake, window_every=10)
        for _ in range(25):
            t.sample()
        self.assertEqual((t.samples, t.window_samples), (25, 3))

    def test_records_hold_basenames_and_booleans_only(self):
        fake = FakeFg()
        fake.images[60] = "C:\\Users\\someone\\AppData\\chrome.exe"
        recs = []
        t, _ = tracer(fake, on_record=recs.append)
        fake.windows = [window(USER_GEMINI)]
        t.sample()
        fake.fg = (600, 60)
        t.sample()
        fg = [r for r in recs if r["event"] == "foreground"]
        self.assertEqual([r["image"] for r in fg], ["explorer.exe", "chrome.exe"])
        for r in recs:
            for v in r.values():
                self.assertIsInstance(v, (str, bool, int, float))
            self.assertNotIn("someone", json.dumps(r))

    def test_thread_start_stop(self):
        fake = FakeFg()
        fake.fg = (7777, OURS)
        t = ft.FgTracer(fake, period_s=0.005, our_pids=lambda: [OURS])
        t.start()
        res = t.stop()
        self.assertGreaterEqual(res["samples"], 1)
        self.assertTrue(res["violated"])
        self.assertFalse(t._thread.is_alive())

    def test_stop_takes_a_final_sample_with_windows(self):
        fake = FakeFg()
        t = ft.FgTracer(fake, period_s=10, our_pids=lambda: [OURS])
        t.start()
        fake.windows = [window(OURS)]
        res = t.stop()
        self.assertTrue(res["violated"])


class HostPidClassificationTests(unittest.TestCase):
    """host_pids_source catches our D1 and D2 instances even when they land on the Default desktop."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.job = self._tmp.name
        os.makedirs(os.path.join(self.job, "relay"), exist_ok=True)
        self.os = FakeOS()
        self.host = make_host(self.os)

    def test_d1_instance_on_default_desktop_from_host_json_is_a_violation(self):
        add_user_instance(self.os)  # user's Gemini pids 100, 101 on Default
        # Our D1 instance (no --user-data-dir) ended up on Default instead of our hidden desktop
        self.os.add_proc(8000, 4242, '"%s"' % APP)
        self.os.add_proc(8001, 8000, '"%s" renderer' % APP)
        self.os.add_window("Default", 8000)
        # Host.instances() alone misses a D1 instance on Default
        self.assertEqual(self.host.instances(), [])
        # host.json recorded our D1 session
        self.host._save_state(gh.HostSession("D1", OURS_DESKTOP, 8000), self.os.procs[8000])

        fg = FakeFg()
        fg.images.update({8000: "Gemini.exe", 8001: "Gemini.exe"})
        clock = Clock()
        t = ft.FgTracer(fg, our_pids=ft.host_pids_source(self.host, job=self.job), clock=clock)
        # First, user's Gemini on Default is NOT a violation
        fg.fg = (1000, USER_GEMINI)
        fg.windows = [window(USER_GEMINI)]
        t.sample(windows=True)
        self.assertFalse(t.result()["violated"])
        # Now our D1 renderer on Default becomes foreground -> violation
        fg.fg = (80010, 8001)
        fg.windows = [window(USER_GEMINI), window(8000)]
        t.sample(windows=True)
        res = t.result()
        self.assertTrue(res["violated"])
        self.assertEqual(res["ours"]["foreground_events"], 1)
        self.assertEqual(res["ours"]["visible_window_samples"], 1)

    def test_d1_instance_on_default_desktop_from_driver_state_is_a_violation(self):
        add_user_instance(self.os)
        self.os.add_proc(8100, 4242, '"%s"' % APP)
        self.os.add_proc(8101, 8100, '"%s" renderer' % APP)
        self.os.add_window("Default", 8100)
        self.assertEqual(self.host.instances(), [])
        state = {"step": "plan", "url": "https://gemini.google.com/app/abc123", "cancel": "cancel_button",
                 "cards": 1, "host_mode": "D1", "browser_pid": 8100, "at": self.os.now}
        with open(os.path.join(self.job, "relay", "driver_state.json"), "w", encoding="utf-8") as fh:
            json.dump(state, fh)

        fg = FakeFg()
        fg.images.update({8100: "Gemini.exe", 8101: "Gemini.exe"})
        t = ft.FgTracer(fg, our_pids=ft.host_pids_source(self.host, job=self.job), clock=Clock())
        fg.fg = (81000, 8100)
        t.sample()
        res = t.result()
        self.assertTrue(res["violated"])
        self.assertEqual(res["ours"]["foreground_events"], 1)

    def test_d2_instance_on_default_desktop_without_state_file_is_a_violation(self):
        add_user_instance(self.os)
        self.os.add_proc(7000, 4242, '"%s" %s' % (APP, UDD))
        self.os.add_proc(7001, 7000, '"%s" %s renderer' % (APP, UDD))
        self.os.add_window("Default", 7000)

        fg = FakeFg()
        fg.images.update({7000: "Gemini.exe", 7001: "Gemini.exe"})
        t = ft.FgTracer(fg, our_pids=ft.host_pids_source(self.host, job=self.job), clock=Clock())
        fg.windows = [window(USER_GEMINI), window(7000)]
        t.sample(windows=True)
        res = t.result()
        self.assertTrue(res["violated"])
        self.assertEqual(res["ours"]["visible_window_samples"], 1)

    def test_late_start_of_instance_after_tracer_started(self):
        add_user_instance(self.os)
        fg = FakeFg()
        fg.images.update({7200: "Gemini.exe", 7201: "Gemini.exe"})
        clock = Clock()
        t = ft.FgTracer(fg, our_pids=ft.host_pids_source(self.host, job=self.job), clock=clock, refresh_s=1.0)
        # Tracer starts before our instance exists
        t.sample()
        self.assertFalse(t.result()["ours"]["pids_known"])
        # Our instance launches AFTER tracer started, briefly takes foreground on Default, then returns to Explorer
        clock.sleep(0.1)
        self.os.add_proc(7200, 4242, '"%s" %s' % (APP, UDD))
        self.os.add_proc(7201, 7200, '"%s" %s renderer' % (APP, UDD))
        self.os.add_window("Default", 7200)
        fg.fg = (72000, 7200)
        t.sample()
        clock.sleep(0.02)
        fg.fg = (500, EXPLORER)
        t.sample()
        # Next refresh tick discovers the late-starting instance and re-classifies the early foreground event
        clock.sleep(1.2)
        t.sample()
        res = t.result()
        self.assertTrue(res["ours"]["pids_known"])
        self.assertTrue(res["violated"])
        self.assertEqual(res["ours"]["foreground_events"], 1)


class Win32FgOSSandboxTests(unittest.TestCase):
    def test_every_call_is_refused_in_the_sandbox(self):
        self.assertEqual(os.environ.get("AGY_DR_TEST_SANDBOX"), "1")
        w = ft.Win32FgOS()
        for call in (w.foreground, lambda: w.image(1), w.top_windows, lambda: w.pid_alive(1)):
            with self.assertRaises(gh.HostRefused) as cm:
                call()
            self.assertEqual(cm.exception.reason, "sandbox")
        self.assertIsNone(w._w)   # no Win32 library was loaded

    def test_default_parts_refused(self):
        with self.assertRaises(gh.HostRefused):
            ft.default_parts()

    def test_tracer_thread_stops_on_refusal(self):
        t = ft.FgTracer(ft.Win32FgOS(), period_s=0.001)
        t.start()
        t._thread.join(2)
        self.assertFalse(t._thread.is_alive())
        self.assertEqual(t.stop()["samples"], 0)


def read_jsonl(job):
    with open(ft.jsonl_path(job), encoding="utf-8") as fh:
        return [json.loads(line) for line in fh]


class DetachedTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.job = self._tmp.name

    def test_exits_when_the_watched_pid_ends(self):
        fake = FakeFg()
        fake.alive = {4242}
        clock = Clock()
        clock.hooks.append((clock.now + 2.0, lambda: fake.alive.discard(4242)))
        end = ft.run_detached(self.job, 4242, fake, lambda: [OURS], run_id="r1", max_s=60, clock=clock,
                              sleep=clock.sleep)
        self.assertEqual(end["reason"], "watched_pid_ended")
        self.assertLess(clock.now - 1000.0, 3.5)
        recs = read_jsonl(self.job)
        self.assertEqual(recs[0]["event"], "start")
        self.assertEqual(recs[-1]["event"], "end")
        self.assertTrue(all(r["run"] == "r1" for r in recs))
        self.assertFalse(recs[-1]["violated"])

    def test_exits_after_max_duration(self):
        fake = FakeFg()
        fake.alive = {4242}
        clock = Clock()
        end = ft.run_detached(self.job, 4242, fake, None, run_id="r2", max_s=1.0, clock=clock, sleep=clock.sleep)
        self.assertEqual(end["reason"], "max_duration")

    def test_exits_on_stop_event(self):
        fake = FakeFg()
        fake.alive = {4242}
        clock = Clock()
        ev = threading.Event()
        clock.hooks.append((clock.now + 0.5, ev.set))
        end = ft.run_detached(self.job, 4242, fake, None, run_id="r3", max_s=60, clock=clock, sleep=clock.sleep,
                              stop_event=ev)
        self.assertEqual(end["reason"], "stopped")

    def test_exits_on_stop_file(self):
        fake = FakeFg()
        clock = Clock()
        stop_file = os.path.join(self.job, "relay", "result.json")
        os.makedirs(os.path.dirname(stop_file), exist_ok=True)
        clock.hooks.append((clock.now + 0.4, lambda: Path(stop_file).write_text("{}", encoding="utf-8")))
        end = ft.run_detached(self.job, None, fake, lambda: [OURS], run_id="rsf", max_s=60, clock=clock,
                              sleep=clock.sleep, stop_files=[stop_file])
        self.assertEqual(end["reason"], "stop_file")
        self.assertLess(clock.now - 1000.0, 1.0)

    def test_violation_and_no_titles_in_jsonl(self):
        fake = FakeFg()
        fake.alive = {4242}
        fake.images[61] = "test@example.invalid.exe"
        fake.windows = [window(USER_GEMINI), window(OURS)]
        clock = Clock()
        clock.hooks.append((clock.now + 0.1, lambda: setattr(fake, "fg", (610, 61))))
        clock.hooks.append((clock.now + 0.2, lambda: setattr(fake, "fg", (7777, OURS))))
        clock.hooks.append((clock.now + 1.0, lambda: fake.alive.clear()))
        end = ft.run_detached(self.job, 4242, fake, lambda: [OURS], run_id="r4", max_s=60, clock=clock,
                              sleep=clock.sleep)
        self.assertTrue(end["violated"])
        self.assertEqual(end["ours"]["foreground_events"], 1)
        with open(ft.jsonl_path(self.job), encoding="utf-8") as fh:
            text = fh.read()
        self.assertNotIn("비밀", text)
        self.assertNotIn("test@example.invalid", text)
        self.assertNotIn("title", text)
        allowed = {"event", "run", "t", "image", "gemini", "ours", "gemini_windows", "at", "period_s", "max_s"}
        for r in read_jsonl(self.job):
            if r["event"] != "end":
                self.assertLessEqual(set(r), allowed, r)

    def test_appends_to_an_existing_file(self):
        fake = FakeFg()
        clock = Clock()
        ft.run_detached(self.job, 1, fake, None, run_id="a", max_s=1, clock=clock, sleep=clock.sleep)
        ft.run_detached(self.job, 1, fake, None, run_id="b", max_s=1, clock=clock, sleep=clock.sleep)
        runs = {r["run"] for r in read_jsonl(self.job)}
        self.assertEqual(runs, {"a", "b"})

    def test_main_refused_in_the_sandbox(self):
        rc = ft.main(["--job", self.job, "--watch-pid", "4242", "--run-id", "m"])
        self.assertEqual(rc, 3)
        recs = read_jsonl(self.job)
        self.assertEqual((recs[-1]["event"], recs[-1]["reason"], recs[-1]["error"]), ("end", "refused", "sandbox"))

    def test_main_with_injected_parts(self):
        fake = FakeFg()
        rc = ft.main(["--job", self.job, "--watch-pid", "4242", "--run-id", "m2", "--max-s", "5"],
                     parts_factory=lambda: (fake, lambda: [OURS]))
        self.assertEqual(rc, 0)
        self.assertEqual(read_jsonl(self.job)[-1]["reason"], "watched_pid_ended")

    def test_main_bad_arguments(self):
        self.assertEqual(ft.main(["--job", os.path.join(self.job, "missing"), "--watch-pid", "1"]), 5)
        self.assertEqual(ft.main(["--job", self.job, "--watch-pid", "0"]), 5)
        with mock.patch("sys.stderr"):
            self.assertEqual(ft.main(["--job", self.job]), 5)

    def test_jsonl_writer_and_main_refuse_paths_outside_job_dir(self):
        outside = tempfile.mkdtemp(prefix="agy-dr-outside-")
        self.addCleanup(lambda: os.path.isdir(outside) and __import__("shutil").rmtree(outside, ignore_errors=True))
        with self.assertRaises(ValueError):
            ft.JsonlWriter(os.path.join(outside, "foreground.jsonl"), "r", job=self.job)
        with self.assertRaises(ValueError):
            ft.JsonlWriter(os.path.join(self.job, "relay", "..", "..", "escape.jsonl"), "r", job=self.job)
        # A stop file outside --job is refused (exit 5)
        rc = ft.main(["--job", self.job, "--stop-file", os.path.join(outside, "stop"), "--run-id", "esc"],
                     parts_factory=lambda: (FakeFg(), lambda: []))
        self.assertEqual(rc, 5)
        # If relay resolves outside --job (e.g. junction/symlink), jsonl_path and main refuse
        orig_realpath = os.path.realpath

        def fake_realpath(p):
            s = orig_realpath(p)
            if os.path.basename(s) == "relay" or (os.path.basename(os.path.dirname(s)) == "relay"
                                                  and os.path.basename(s) == ft.JSONL_NAME):
                return os.path.join(outside, os.path.basename(s))
            return s

        with mock.patch.object(ft.os.path, "realpath", side_effect=fake_realpath):
            with self.assertRaises(ValueError):
                ft.jsonl_path(self.job)
            self.assertEqual(ft.main(["--job", self.job, "--watch-pid", "1", "--run-id", "esc"],
                                     parts_factory=lambda: (FakeFg(), lambda: [])), 5)


class FakeProc:
    def __init__(self, hang=False, pid=999, exited=False):
        self.stdin = mock.Mock()
        self.hang, self.killed = hang, False
        self.pid = pid
        self._exited = exited

    def poll(self):
        return 0 if self._exited else None

    def wait(self, timeout=None):
        if self.hang and not self.killed:
            raise subprocess.TimeoutExpired("fg_tracer", timeout)
        return 0

    def kill(self):
        self.killed = True


class OrchestratorTracerTests(unittest.TestCase):
    """R6 tracer wiring: deep_research.py starts/stops fg_tracer.py on the default desktop; relay_step.py never does."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.jobs = self.tmp / "jobs"
        self.jobs.mkdir()
        self.job_dir = self.jobs / "j1"
        self.relay = self.job_dir / "relay"
        self.relay.mkdir(parents=True)
        self.job = str(self.job_dir)
        self.result = str(self.relay / "result.json")
        (self.job_dir / "prompt.md").write_text("Analyze distributed consensus protocols.\n", encoding="utf-8")
        (self.job_dir / "goal.md").write_text("Compare Raft and Paxos throughput and latency.\n", encoding="utf-8")
        self.write_job(status="initialized", seq=0)
        env = mock.patch.dict(os.environ, {"AGY_DR_JOBS": str(self.jobs), "AGY_DR_POLL_INTERVAL": "0.01"})
        env.start()
        self.addCleanup(env.stop)

    def write_job(self, **fields):
        base = {
            "job_id": "j1",
            "job_dir": str(self.job_dir),
            "status": "initialized",
            "created_at": "2026-11-27T10:00:00",
            "updated_at": "2026-11-27T10:00:00",
            "seq": 0,
            "plan_iterations": 0,
            "raw_prompt": True,
            "warnings": [],
            "architecture_warnings": [],
            "xml_structured": False,
            "present_xml_tags": [],
            "missing_xml_tags": [],
            "out_path": str(self.job_dir / "report.md"),
            "url": None,
            "latest_plan": None,
            "plan_count": 0,
            "approval_reason": None,
            "worker_pid": None,
            "progress_highlights": [],
            "agy_model": "flash 3.8",
            "agy_effort": "high",
            "gemini_model": "Pro",
            "gemini_thinking": "high",
            "host": "uia",
        }
        base.update(fields)
        (self.job_dir / "job.json").write_text(json.dumps(base, indent=2), encoding="utf-8")

    def write_lines(self, recs):
        with open(self.relay / dr.FG_JSONL, "a", encoding="utf-8") as fh:
            for r in recs:
                fh.write(json.dumps(r) + "\n")

    def run_dr(self, argv, **kw):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = dr.main(argv, **kw)
            except SystemExit as exc:
                rc = exc.code if isinstance(exc.code, int) else 1
        text = out.getvalue()
        return rc, (json.loads(text) if text.strip() else None), text, err.getvalue()

    def make_fg_popen(self, records_fn=None, *, write_start=True, hang=False, exited=False):
        calls = []

        def fake_popen(argv, **kw):
            calls.append((list(argv), dict(kw)))
            run_id = argv[argv.index("--run-id") + 1]
            if write_start:
                self.write_lines([{"event": "start", "run": run_id}])
            if records_fn is not None:
                extra = records_fn(run_id, argv, kw)
                if extra:
                    self.write_lines(extra)
            return FakeProc(hang=hang, exited=exited)

        return fake_popen, calls

    # --- pin where the tracer starts ------------------------------------------------------------------------------
    def test_relay_step_never_starts_fg_tracer(self):
        self.assertFalse(hasattr(relay_step, "start_fg_tracer"))
        self.assertFalse(hasattr(relay_step, "stop_fg_tracer"))
        run = relay_step.Run()
        run.rc = 0
        run.out = ["[CONVERSATION] https://gemini.google.com/app/abc123\n", relay_step.MARK_START + "\n",
                   json.dumps({"event": "plan", "url": "https://gemini.google.com/app/abc123", "plan": "1. x",
                               "plan_count": 1}) + "\n", relay_step.MARK_END + "\n"]
        req = {"relay_version": 2, "step": "plan", "seq": 1,
               "driver": os.path.join(HERE, "..", "run_deep_research.py"),
               "prompt_file": str(self.job_dir / "prompt.md")}
        with mock.patch.dict(os.environ, {"AGY_DR_TEST_SANDBOX": "0"}), \
                mock.patch.object(relay_step.os, "name", "nt"), \
                mock.patch.object(relay_step, "run_driver", return_value=run), \
                mock.patch.object(relay_step.subprocess, "Popen") as popen:
            res, code = relay_step.do_step(self.job, req, str(self.relay), self.result)
        popen.assert_not_called()
        self.assertEqual((code, res["status"]), (0, "plan_ready"))
        self.assertNotIn("foreground_invariant", res)

    def test_sandbox_never_starts_a_tracer_process(self):
        with mock.patch.object(dr.subprocess, "Popen") as popen:
            h = dr.start_fg_tracer(self.job, str(self.relay), "x", 100)
        popen.assert_not_called()
        self.assertEqual(h["reason"], "sandbox")
        self.assertEqual(dr.stop_fg_tracer(h), {"measured": False, "reason": "sandbox", "violated": False})

    def test_start_argv_is_isolated_detached_list_with_the_same_interpreter(self):
        self.write_lines([{"event": "start", "run": "r"}])
        with mock.patch.dict(os.environ, {"AGY_DR_TEST_SANDBOX": "0"}), \
                mock.patch.object(dr.os, "name", "nt"), \
                mock.patch.object(dr.subprocess, "Popen", return_value=FakeProc()) as popen:
            h = dr.start_fg_tracer(self.job, str(self.relay), "r", 900)
        self.assertEqual(os.environ.get("AGY_DR_TEST_SANDBOX"), "1")
        argv = popen.call_args.args[0]
        kw = popen.call_args.kwargs
        self.assertIsInstance(argv, list)
        self.assertEqual(argv[:2], [sys.executable, "-I"])
        self.assertEqual(os.path.basename(argv[2]), "fg_tracer.py")
        self.assertEqual(argv[argv.index("--job") + 1], str(self.job_dir.resolve()))
        self.assertEqual(argv[argv.index("--watch-pid") + 1], str(os.getpid()))
        self.assertEqual(argv[argv.index("--run-id") + 1], "r")
        self.assertIn("--stop-on-eof", argv)
        self.assertNotIn("shell", kw)
        self.assertEqual(kw["cwd"], str(self.job_dir.resolve()))
        self.assertEqual(kw["stdin"], subprocess.PIPE)
        self.assertTrue(kw["creationflags"] & 0x00000008)   # DETACHED_PROCESS: no console window
        self.assertTrue(h["started"])

    def test_detached_worker_tracer_uses_stop_files_and_survives_caller_exit(self):
        self.write_lines([{"event": "start", "run": "rd"}])
        popen, calls = self.make_fg_popen(write_start=False)
        h = dr.start_fg_tracer(self.job, str(self.relay), "rd", 1800, detached_worker=True,
                               platform="win32", sandbox=False, popen=popen)
        ((argv, kw),) = calls
        self.assertEqual(argv[:2], [sys.executable, "-I"])
        self.assertEqual(os.path.basename(argv[2]), "fg_tracer.py")
        self.assertNotIn("--watch-pid", argv)
        self.assertNotIn("--stop-on-eof", argv)
        self.assertIn("--stop-file", argv)
        self.assertIn(str((self.relay / "result.json").resolve()), argv)
        self.assertEqual(kw["stdin"], subprocess.DEVNULL)
        self.assertTrue(kw["creationflags"] & 0x00000008)
        self.assertTrue(h["started"])

    def test_stop_reads_the_end_record_of_this_run_only(self):
        self.write_lines([{"event": "end", "run": "old", "violated": True},
                          {"event": "start", "run": "r"},
                          {"event": "foreground", "run": "r", "image": "Gemini.exe", "gemini": True, "ours": False},
                          {"event": "end", "run": "r", "reason": "stopped", "violated": False,
                           "gemini_foreground_events": 1, "ours": {"foreground_events": 0}}])
        proc = FakeProc()
        fg = dr.stop_fg_tracer({"proc": proc, "run_id": "r", "started": True,
                                "path": str(self.relay / dr.FG_JSONL)})
        proc.stdin.close.assert_called_once()
        self.assertEqual((fg["measured"], fg["violated"], fg["complete"], fg["gemini_foreground_events"]),
                         (True, False, True, 1))

    def test_killed_tracer_summary_is_rebuilt_from_its_records(self):
        self.write_lines([{"event": "start", "run": "r"},
                          {"event": "foreground", "run": "r", "image": "Gemini.exe", "gemini": True, "ours": True}])
        proc = FakeProc(hang=True)
        fg = dr.stop_fg_tracer({"proc": proc, "run_id": "r", "started": True,
                                "path": str(self.relay / dr.FG_JSONL)})
        self.assertTrue(proc.killed)
        self.assertEqual((fg["measured"], fg["violated"], fg["complete"]), (True, True, False))

    def test_late_start_flag_when_start_record_arrives_later(self):
        popen, _ = self.make_fg_popen(write_start=False)
        h = dr.start_fg_tracer(self.job, str(self.relay), "late", 60, platform="win32", sandbox=False,
                               popen=popen, start_wait_s=0.0)
        self.assertFalse(h["started"])
        self.write_lines([{"event": "start", "run": "late"},
                          {"event": "end", "run": "late", "reason": "stopped", "violated": True,
                           "samples": 2, "ours": {"foreground_events": 1, "visible_window_samples": 0}}])
        fg = dr.stop_fg_tracer(h)
        self.assertEqual((fg["measured"], fg["late_start"], fg["violated"]), (True, True, True))

    # --- deep_research.py steps -----------------------------------------------------------------------------------
    def _ok_records(self, run_id, _argv, _kw):
        return [{"event": "end", "run": run_id, "reason": "stopped", "violated": False, "samples": 10,
                 "gemini_foreground_events": 3, "ours": {"foreground_events": 0, "visible_window_samples": 0}}]

    def _violated_records(self, run_id, _argv, _kw):
        return [{"event": "end", "run": run_id, "reason": "stopped", "violated": True, "samples": 10,
                 "gemini_foreground_events": 1, "ours": {"foreground_events": 1, "visible_window_samples": 0}}]

    def _fake_plan_relay(self, job_dir, step, seq, *_a, **_kw):
        res = {"relay_version": 2, "step": "plan", "seq": seq, "status": "plan_ready",
               "url": "https://gemini.google.com/app/abc123", "plan": "1. Investigate Raft\n2. Compare Paxos",
               "plan_count": 1}
        dr.write_json_atomic(job_dir / "relay" / "result.json", res)
        return 0, {}, res

    def _fake_edit_relay(self, job_dir, step, seq, *_a, **_kw):
        res = {"relay_version": 2, "step": "edit", "seq": seq, "status": "plan_revised",
               "url": "https://gemini.google.com/app/abc123", "plan": "1. Investigate Raft\n2. Add Zab",
               "plan_count": 2}
        dr.write_json_atomic(job_dir / "relay" / "result.json", res)
        return 0, {}, res

    def _fake_report_relay(self, job_dir, step, seq, *_a, **_kw):
        staged = dr.staged_report_path(job_dir, seq)
        staged.write_text(VALID_REPORT_MD, encoding="utf-8")
        res = {"relay_version": 2, "step": step, "seq": seq, "status": "completed",
               "url": "https://gemini.google.com/app/abc123", "conversation_id": "abc123",
               "conversation_url": "https://gemini.google.com/app/abc123",
               "report_file": str(staged), "report_chars": len(VALID_REPORT_MD),
               "sources_count": 1, "elapsed_s": 5.0}
        if step == "approve":
            st = {"relay_version": 2, "step": step, "seq": seq, "status": "started", "pid": os.getpid()}
            dr.write_json_atomic(job_dir / "relay" / "started.json", st)
        dr.write_json_atomic(job_dir / "relay" / "result.json", res)
        return 0, {}, res

    def test_plan_with_our_instance_in_the_foreground_fails_with_exit_2(self):
        popen, calls = self.make_fg_popen(self._violated_records)
        with mock.patch.object(dr, "invoke_agy_relay", side_effect=self._fake_plan_relay):
            rc, res, _, _ = self.run_dr(["plan", "--job", "j1"], platform="win32", sandbox=False, fg_popen=popen)
        self.assertEqual(rc, 2)
        self.assertEqual((res["status"], res["code"], res["exit_code"]), ("error", "FOREGROUND_INVARIANT_VIOLATED", 2))
        self.assertTrue(res["foreground_invariant_violated"])
        self.assertEqual(res["foreground_invariant"]["ours"]["foreground_events"], 1)
        self.assertFalse((self.job_dir / "plan_v1.md").exists())
        self.assertTrue(json.loads((self.relay / "result.json").read_text(encoding="utf-8"))["foreground_invariant_violated"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0][:2], [sys.executable, "-I"])

    def test_plan_without_violation_keeps_the_plan_and_records_the_trace(self):
        popen, calls = self.make_fg_popen(self._ok_records)
        with mock.patch.object(dr, "invoke_agy_relay", side_effect=self._fake_plan_relay):
            rc, res, _, _ = self.run_dr(["plan", "--job", "j1"], platform="win32", sandbox=False, fg_popen=popen)
        self.assertEqual((rc, res["status"]), (0, "plan_ready"), res)
        self.assertFalse(res["foreground_invariant_violated"])
        self.assertEqual(res["foreground_invariant"]["gemini_foreground_events"], 3)
        self.assertEqual(len(calls), 1)

    def test_violation_wins_over_a_driver_error(self):
        def failing_plan(job_dir, step, seq, *_a, **_kw):
            res = {"relay_version": 2, "step": "plan", "seq": seq, "status": "error", "code": "PLAN_TIMEOUT",
                   "exit_code": 2, "message": "plan timed out"}
            dr.write_json_atomic(job_dir / "relay" / "result.json", res)
            return 0, {}, res

        popen, _ = self.make_fg_popen(self._violated_records)
        with mock.patch.object(dr, "invoke_agy_relay", side_effect=failing_plan):
            rc, res, _, _ = self.run_dr(["plan", "--job", "j1"], platform="win32", sandbox=False, fg_popen=popen)
        self.assertEqual((rc, res["code"], res["driver_code"]), (2, "FOREGROUND_INVARIANT_VIOLATED", "PLAN_TIMEOUT"))

    def test_edit_approve_and_collect_start_tracer_and_enforce_violation(self):
        # 1. edit
        self.write_job(status="plan_ready", seq=1, url="https://gemini.google.com/app/abc123",
                       latest_plan="1. x", plan_count=1, plan_iterations=1)
        popen, calls = self.make_fg_popen(self._violated_records)
        with mock.patch.object(dr, "invoke_agy_relay", side_effect=self._fake_edit_relay):
            rc, res, _, _ = self.run_dr(["edit", "--job", "j1", "--feedback", "Add Zab comparison."],
                                        platform="win32", sandbox=False, fg_popen=popen)
        self.assertEqual((rc, res["code"]), (2, "FOREGROUND_INVARIANT_VIOLATED"))
        self.assertEqual(len(calls), 1)

        # 2. approve (wait)
        self.write_job(status="plan_ready", seq=1, url="https://gemini.google.com/app/abc123",
                       latest_plan="1. x", plan_count=1, plan_iterations=1)
        popen, calls = self.make_fg_popen(self._violated_records)
        with mock.patch.object(dr, "invoke_agy_relay", side_effect=self._fake_report_relay):
            rc, res, _, _ = self.run_dr(
                ["approve", "--job", "j1", "--reason", "Plan covers Raft and Paxos throughput and latency."],
                platform="win32", sandbox=False, fg_popen=popen)
        self.assertEqual((rc, res["code"]), (2, "FOREGROUND_INVARIANT_VIOLATED"))
        self.assertEqual(len(calls), 1)
        self.assertIn("--stop-file", calls[0][0])
        self.assertFalse((self.job_dir / "report.md").exists())

        # 3. collect (wait)
        self.write_job(status="running", seq=2, url="https://gemini.google.com/app/abc123",
                       latest_plan="1. x", plan_count=1, plan_iterations=1)
        (self.relay / "result.json").unlink(missing_ok=True)
        (self.relay / "started.json").unlink(missing_ok=True)
        popen, calls = self.make_fg_popen(self._ok_records)
        with mock.patch.object(dr, "invoke_agy_relay", side_effect=self._fake_report_relay):
            rc, res, _, _ = self.run_dr(["collect", "--job", "j1"], platform="win32", sandbox=False, fg_popen=popen)
        self.assertEqual((rc, res["status"]), (0, "completed"), res)
        self.assertFalse(res["foreground_invariant_violated"])
        self.assertEqual(res["foreground_invariant"]["measured"], True)
        self.assertEqual(len(calls), 1)

    def test_approve_no_wait_then_collect_merges_detached_tracer(self):
        self.write_job(status="plan_ready", seq=1, url="https://gemini.google.com/app/abc123",
                       latest_plan="1. x", plan_count=1, plan_iterations=1)

        def fake_approve_start(job_dir, step, seq, *_a, **_kw):
            st = {"relay_version": 2, "step": step, "seq": seq, "status": "started", "pid": 999999}
            dr.write_json_atomic(job_dir / "relay" / "started.json", st)
            return 0, {}, st

        popen, calls = self.make_fg_popen(write_start=True)
        with mock.patch.object(dr, "invoke_agy_relay", side_effect=fake_approve_start):
            rc, start_res, _, _ = self.run_dr(
                ["approve", "--job", "j1", "--reason", "Plan covers Raft and Paxos throughput and latency.",
                 "--no-wait"],
                platform="win32", sandbox=False, fg_popen=popen)
        self.assertEqual((rc, start_res["status"]), (0, "started"), start_res)
        run_id = start_res["fg_run_id"]
        self.assertEqual(len(calls), 1)

        # Simulate detached worker finishing and detached tracer writing its end record
        staged = dr.staged_report_path(self.job_dir, 2)
        staged.write_text(VALID_REPORT_MD, encoding="utf-8")
        dr.write_json_atomic(self.relay / "result.json", {
            "relay_version": 2, "step": "approve", "seq": 2, "status": "completed",
            "url": "https://gemini.google.com/app/abc123", "conversation_id": "abc123",
            "conversation_url": "https://gemini.google.com/app/abc123",
            "report_file": str(staged), "report_chars": len(VALID_REPORT_MD), "sources_count": 1, "elapsed_s": 4.0,
        })
        self.write_lines([{"event": "end", "run": run_id, "reason": "stop_file", "violated": True, "samples": 5,
                           "ours": {"foreground_events": 1, "visible_window_samples": 0}}])
        with mock.patch.object(dr, "pid_alive", return_value=False):
            rc2, res2, _, _ = self.run_dr(["collect", "--job", "j1"], platform="win32", sandbox=False)
        self.assertEqual((rc2, res2["code"]), (2, "FOREGROUND_INVARIANT_VIOLATED"))
        self.assertFalse((self.job_dir / "report.md").exists())

    def test_tracer_stopped_when_agy_relay_raises(self):
        with mock.patch.object(dr, "invoke_agy_relay", side_effect=RuntimeError("boom")), \
                mock.patch.object(dr, "start_fg_tracer", return_value={"proc": "h"}), \
                mock.patch.object(dr, "stop_fg_tracer", return_value={"measured": True, "violated": False}) as stop:
            with self.assertRaises(RuntimeError):
                dr.main(["plan", "--job", "j1"], platform="win32", sandbox=False)
        stop.assert_called_once()

    # --- Ruling item 4: FOREGROUND_UNMEASURED fail closed vs sandbox / visible mode -------------------------------
    def test_foreground_unmeasured_fails_closed_outside_sandbox_when_start_fails(self):
        def broken_popen(*_a, **_kw):
            raise OSError("cannot spawn tracer")

        for step_name, setup, argv in (
            ("plan", lambda: self.write_job(status="initialized", seq=0), ["plan", "--job", "j1"]),
            ("edit", lambda: self.write_job(status="plan_ready", seq=1, url="https://gemini.google.com/app/abc123",
                                            latest_plan="1. x", plan_count=1, plan_iterations=1),
             ["edit", "--job", "j1", "--feedback", "Add Zab comparison."]),
            ("approve", lambda: self.write_job(status="plan_ready", seq=1, url="https://gemini.google.com/app/abc123",
                                               latest_plan="1. x", plan_count=1, plan_iterations=1),
             ["approve", "--job", "j1", "--reason", "Plan covers Raft and Paxos throughput and latency."]),
            ("collect", lambda: self.write_job(status="running", seq=1, url="https://gemini.google.com/app/abc123",
                                               latest_plan="1. x", plan_count=1, plan_iterations=1),
             ["collect", "--job", "j1"]),
        ):
            with self.subTest(step=step_name):
                setup()
                (self.relay / "result.json").unlink(missing_ok=True)
                (self.relay / "started.json").unlink(missing_ok=True)
                with mock.patch.object(dr, "invoke_agy_relay") as relay_mock:
                    rc, res, _, _ = self.run_dr(argv, platform="win32", sandbox=False, fg_popen=broken_popen)
                self.assertEqual((rc, res["code"]), (2, "FOREGROUND_UNMEASURED"), res)
                self.assertFalse(res["foreground_invariant"]["measured"])
                relay_mock.assert_not_called()

    def test_foreground_unmeasured_fails_closed_outside_sandbox_when_no_records_written(self):
        popen, _ = self.make_fg_popen(records_fn=None, write_start=False, exited=True)
        with mock.patch.object(dr, "invoke_agy_relay", side_effect=self._fake_plan_relay):
            rc, res, _, _ = self.run_dr(["plan", "--job", "j1"], platform="win32", sandbox=False, fg_popen=popen)
        self.assertEqual((rc, res["code"]), (2, "FOREGROUND_UNMEASURED"), res)
        self.assertEqual(res["foreground_invariant"]["reason"], "no_records")
        self.assertFalse(res["foreground_invariant"]["measured"])
        self.assertFalse((self.job_dir / "plan_v1.md").exists())

    def test_measured_false_allowed_in_sandbox_and_visible_mode(self):
        # 1. Inside sandbox (sandbox=True): allowed without spawning tracer
        with mock.patch.object(dr, "invoke_agy_relay", side_effect=self._fake_plan_relay), \
                mock.patch.object(dr.subprocess, "Popen") as popen:
            rc, res, _, _ = self.run_dr(["plan", "--job", "j1"], platform="win32", sandbox=True)
        popen.assert_not_called()
        self.assertEqual((rc, res["status"]), (0, "plan_ready"), res)
        self.assertEqual(res["foreground_invariant"], {"measured": False, "reason": "sandbox", "violated": False})
        self.assertFalse(res["foreground_invariant_violated"])

        # 2. Outside sandbox on Windows with explicit visible/debug mode (--show): allowed
        self.write_job(status="initialized", seq=0)
        with mock.patch.object(dr, "invoke_agy_relay", side_effect=self._fake_plan_relay), \
                mock.patch.object(dr.subprocess, "Popen") as popen:
            rc, res, _, _ = self.run_dr(["plan", "--job", "j1", "--show"], platform="win32", sandbox=False)
        popen.assert_not_called()
        self.assertEqual((rc, res["status"]), (0, "plan_ready"), res)
        self.assertEqual(res["foreground_invariant"], {"measured": False, "reason": "visible_mode", "violated": False})
        self.assertTrue(res["visible_mode"])


if __name__ == "__main__":
    unittest.main()

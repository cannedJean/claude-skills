import sandbox  # noqa: F401  (must stay the first import: blocks real-app access)
# test_fg_tracer.py - the R6 foreground tracer (fg_tracer.py) and its relay / doctor wiring, on a fake FgOS only.
# No real window, process or tracer process is touched: the relay's Popen is mocked, the real Win32FgOS refuses in
# the sandbox.
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

import fg_tracer as ft  # noqa: E402
import gemini_host as gh  # noqa: E402
import relay_step  # noqa: E402

USER_GEMINI = 100      # the user's own Gemini on the default desktop
OURS = 7000            # our verified instance (hidden desktop)
EXPLORER = 50
SECRET_TITLE = "비밀 대화 제목 test@example.invalid"


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


class FakeProc:
    def __init__(self, hang=False):
        self.stdin = mock.Mock()
        self.hang, self.killed = hang, False
        self.pid = 999

    def poll(self):
        return None

    def wait(self, timeout=None):
        if self.hang and not self.killed:
            raise subprocess.TimeoutExpired("fg_tracer", timeout)
        return 0

    def kill(self):
        self.killed = True


class RelayTracerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.job = self._tmp.name
        self.relay = os.path.join(self.job, "relay")
        os.makedirs(self.relay)
        self.result = os.path.join(self.relay, "result.json")

    def write_lines(self, recs):
        with open(os.path.join(self.relay, relay_step.FG_JSONL), "a", encoding="utf-8") as fh:
            for r in recs:
                fh.write(json.dumps(r) + "\n")

    def test_sandbox_never_starts_a_tracer_process(self):
        with mock.patch.object(relay_step.subprocess, "Popen") as popen:
            h = relay_step.start_fg_tracer(self.job, self.relay, "x", 100)
        popen.assert_not_called()
        self.assertEqual(h["reason"], "sandbox")
        self.assertEqual(relay_step.stop_fg_tracer(h), {"measured": False, "reason": "sandbox", "violated": False})

    def test_start_argv_is_a_detached_list_with_the_same_interpreter(self):
        self.write_lines([{"event": "start", "run": "r"}])
        with mock.patch.dict(os.environ, {"AGY_DR_TEST_SANDBOX": "0"}), \
                mock.patch.object(relay_step.os, "name", "nt"), \
                mock.patch.object(relay_step.subprocess, "Popen", return_value=FakeProc()) as popen:
            h = relay_step.start_fg_tracer(self.job, self.relay, "r", 900)
        self.assertEqual(os.environ.get("AGY_DR_TEST_SANDBOX"), "1")
        argv = popen.call_args.args[0]
        kw = popen.call_args.kwargs
        self.assertIsInstance(argv, list)
        self.assertEqual(argv[0], sys.executable)
        self.assertEqual(os.path.basename(argv[1]), "fg_tracer.py")
        self.assertEqual(argv[argv.index("--job") + 1], self.job)
        self.assertEqual(argv[argv.index("--watch-pid") + 1], str(os.getpid()))
        self.assertEqual(argv[argv.index("--run-id") + 1], "r")
        self.assertIn("--stop-on-eof", argv)
        self.assertNotIn("shell", kw)
        self.assertEqual(kw["cwd"], self.job)
        self.assertEqual(kw["stdin"], subprocess.PIPE)
        self.assertTrue(kw["creationflags"] & 0x00000008)   # DETACHED_PROCESS: no console window
        self.assertTrue(h["started"])

    def test_stop_reads_the_end_record_of_this_run_only(self):
        self.write_lines([{"event": "end", "run": "old", "violated": True},
                          {"event": "start", "run": "r"},
                          {"event": "foreground", "run": "r", "image": "Gemini.exe", "gemini": True, "ours": False},
                          {"event": "end", "run": "r", "reason": "stopped", "violated": False,
                           "gemini_foreground_events": 1, "ours": {"foreground_events": 0}}])
        proc = FakeProc()
        fg = relay_step.stop_fg_tracer({"proc": proc, "run_id": "r", "started": True,
                                        "path": os.path.join(self.relay, relay_step.FG_JSONL)})
        proc.stdin.close.assert_called_once()
        self.assertEqual((fg["measured"], fg["violated"], fg["complete"], fg["gemini_foreground_events"]),
                         (True, False, True, 1))

    def test_killed_tracer_summary_is_rebuilt_from_its_records(self):
        self.write_lines([{"event": "start", "run": "r"},
                          {"event": "foreground", "run": "r", "image": "Gemini.exe", "gemini": True, "ours": True}])
        proc = FakeProc(hang=True)
        fg = relay_step.stop_fg_tracer({"proc": proc, "run_id": "r", "started": True,
                                        "path": os.path.join(self.relay, relay_step.FG_JSONL)})
        self.assertTrue(proc.killed)
        self.assertEqual((fg["measured"], fg["violated"], fg["complete"]), (True, True, False))

    # --- do_step ---------------------------------------------------------------------------------------------------
    def plan_req(self):
        prompt = os.path.join(self.job, "prompt.md")
        with open(prompt, "w", encoding="utf-8") as f:
            f.write("topic")
        return {"relay_version": 2, "step": "plan", "seq": 1, "driver": os.path.join(HERE, "..", "run_deep_research.py"),
                "prompt_file": prompt}

    def plan_run(self):
        run = relay_step.Run()
        run.rc = 0
        run.out = ["[CONVERSATION] https://gemini.google.com/app/abc123\n", relay_step.MARK_START + "\n",
                   json.dumps({"event": "plan", "url": "https://gemini.google.com/app/abc123", "plan": "1. x",
                               "plan_count": 1}) + "\n", relay_step.MARK_END + "\n"]
        return run

    def do(self, req, fg, run=None):
        with mock.patch.object(relay_step, "run_driver", return_value=run or self.plan_run()), \
                mock.patch.object(relay_step, "start_fg_tracer", return_value={"proc": "h"}) as start, \
                mock.patch.object(relay_step, "stop_fg_tracer", return_value=fg) as stop:
            res, code = relay_step.do_step(self.job, req, self.relay, self.result)
        return res, code, start, stop

    def test_plan_with_our_instance_in_the_foreground_fails_with_exit_2(self):
        fg = {"measured": True, "violated": True, "ours": {"foreground_events": 1, "visible_window_samples": 0}}
        res, code, start, stop = self.do(self.plan_req(), fg)
        self.assertEqual(code, 2)
        self.assertEqual((res["status"], res["code"], res["exit_code"]), ("error", "FOREGROUND_INVARIANT_VIOLATED", 2))
        self.assertTrue(res["foreground_invariant_violated"])
        self.assertEqual(res["foreground_invariant"]["ours"]["foreground_events"], 1)
        self.assertNotIn("plan", res)
        with open(self.result, encoding="utf-8") as fh:
            self.assertTrue(json.load(fh)["foreground_invariant_violated"])
        start.assert_called_once()
        self.assertEqual(start.call_args.args[:2], (self.job, self.relay))
        stop.assert_called_once_with({"proc": "h"})

    def test_plan_without_violation_keeps_the_plan_and_records_the_trace(self):
        fg = {"measured": True, "violated": False, "gemini_foreground_events": 3}
        res, code, _, _ = self.do(self.plan_req(), fg)
        self.assertEqual((code, res["status"]), (0, "plan_ready"), res)
        self.assertFalse(res["foreground_invariant_violated"])
        self.assertEqual(res["foreground_invariant"]["gemini_foreground_events"], 3)

    def test_violation_wins_over_a_driver_error(self):
        run = relay_step.Run()
        run.rc, run.err = 2, [relay_step.DRIVER_ERROR_PREFIX + json.dumps({"code": "PLAN_TIMEOUT", "exit_code": 2}) + "\n"]
        res, code, _, _ = self.do(self.plan_req(), {"measured": True, "violated": True}, run)
        self.assertEqual((code, res["code"], res["driver_code"]), (2, "FOREGROUND_INVARIANT_VIOLATED", "PLAN_TIMEOUT"))

    def test_edit_is_traced_too(self):
        fb = os.path.join(self.job, "fb.md")
        with open(fb, "w", encoding="utf-8") as f:
            f.write("more")
        req = {"relay_version": 2, "step": "edit", "seq": 2, "driver": os.path.join(HERE, "..", "run_deep_research.py"),
               "feedback_file": fb, "resume_url": "https://gemini.google.com/app/abc123"}
        with mock.patch.object(relay_step, "probe_page", return_value={"error": None}):
            res, code, start, _ = self.do(req, {"measured": True, "violated": True})
        start.assert_called_once()
        self.assertEqual((code, res["code"]), (2, "FOREGROUND_INVARIANT_VIOLATED"))

    def test_tracer_stopped_when_the_driver_run_raises(self):
        with mock.patch.object(relay_step, "run_driver", side_effect=RuntimeError("boom")), \
                mock.patch.object(relay_step, "start_fg_tracer", return_value={"proc": "h"}), \
                mock.patch.object(relay_step, "stop_fg_tracer", return_value={}) as stop:
            with self.assertRaises(RuntimeError):
                relay_step.do_step(self.job, self.plan_req(), self.relay, self.result)
        stop.assert_called_once()

    def test_sandboxed_plan_records_not_measured(self):
        with mock.patch.object(relay_step, "run_driver", return_value=self.plan_run()), \
                mock.patch.object(relay_step.subprocess, "Popen") as popen:
            res, code = relay_step.do_step(self.job, self.plan_req(), self.relay, self.result)
        popen.assert_not_called()
        self.assertEqual(code, 0, res)
        self.assertEqual(res["foreground_invariant"], {"measured": False, "reason": "sandbox", "violated": False})
        self.assertFalse(res["foreground_invariant_violated"])


if __name__ == "__main__":
    unittest.main()

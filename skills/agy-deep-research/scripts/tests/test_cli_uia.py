import sandbox  # noqa: F401  (must stay the first import: blocks real-app access)
# test_cli_uia.py - deep_research.py preflight / setup --with-venv / doctor / signin / app on fakes only.
# A real gemini_host.Host runs over the in-memory FakeOS of test_gemini_host_win32 (lock and host.json in temp
# files) and every UiTree is a FakeTree built from the committed fixtures. No real desktop, window, process, COM
# object, venv or pip is touched: setup's runner only records argv, and the real factory refuses in the sandbox.
import contextlib
import copy
import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

import deep_research as dr  # noqa: E402
import gemini_host as gh  # noqa: E402
from test_gemini_host_win32 import (APP, OURS, PROFILE, STUB, UDD, FakeOS, add_user_instance,  # noqa: E402
                                    make_host, start_d2_instance)
from test_plan_steps import locs as bundled_locs  # noqa: E402
from uitree import FakeTree  # noqa: E402

SECRETS = ("비밀 대화 제목", "test@example.invalid", "Google 계정", "테스트(")
USER_PIDS = {100, 101}
OUR_PIDS = {7000, 7001}


def fixture(name):
    with open(os.path.join(HERE, "fixtures", name), encoding="utf-8") as fh:
        return json.load(fh)


def walk(d):
    yield d
    for c in d.get("children") or ():
        yield from walk(c)


def all_nodes(spec):
    for st in spec["states"].values():
        for doc in st["documents"]:
            yield from walk(doc)
        if st.get("desktop"):
            yield from walk(st["desktop"])


def page(signed_in=False):
    """locators_page_ko (sidebar with an account link and secret chat titles) with menu patterns; signed_in drops
    the `로그인` link."""
    spec = fixture("locators_page_ko.json")
    for n in all_nodes(spec):
        if n["id"] == "pm_file":
            n["patterns"] = ["expand"]
        if n["id"] == "pm_quit":
            n["patterns"] = ["invoke"]
    if signed_in:
        for n in all_nodes(spec):
            n["children"] = [c for c in n.get("children") or () if c["id"] != "signin"]
    return spec


class QuitTree(FakeTree):
    """Invoking `종료` ends our instance (the user's processes are never touched by the tree)."""

    def __init__(self, spec, fake):
        super().__init__(spec)
        self.fake = fake

    def act(self, node_id, pattern):
        super().act(node_id, pattern)
        if node_id == "pm_quit":
            for pid in list(self.fake.procs):
                if pid in OUR_PIDS:
                    self.fake.kill(pid)


def add_our_instance(fake, desktop=OURS):
    fake.add_proc(7000, 4242, '"%s" %s' % (APP, UDD))
    fake.add_proc(7001, 7000, '"%s" %s renderer' % (APP, UDD))
    fake.add_window(desktop, 7000)


class CliHarness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.jobs = self.tmp / "jobs"
        self.jobs.mkdir()
        self.venv = self.tmp / "venv"
        env = mock.patch.dict(os.environ, {"AGY_DR_JOBS": str(self.jobs), "AGY_DR_VENV": str(self.venv),
                                           "AGY_TASK": str(self.tmp / "no_agy_task.py"),
                                           "AGY_DR_SKILL_DIR": str(self.tmp / "target")})
        env.start()
        self.addCleanup(env.stop)
        self.fake = FakeOS()
        self.host = make_host(self.fake)
        self.tree_calls = []
        self.tree = None
        self.factory_calls = 0

    def parts(self, tree=None, quit_via_menu=None, locs=None):
        self.tree = tree

        def tree_on(desktop, pids):
            self.tree_calls.append((desktop, sorted(pids)))
            if isinstance(self.tree, Exception):
                raise self.tree
            return self.tree

        return dr.CliParts(host=self.host, open_tree=lambda s: self.tree, tree_on=tree_on,
                           locs=locs or bundled_locs(), clock=self.fake.clock, sleep=self.fake.sleep,
                           tool_dir=self.tmp / "tool", quit_via_menu=quit_via_menu)

    def run_cli(self, argv, parts=None, platform="win32", runner=None):
        def factory(a):
            self.factory_calls += 1
            return parts

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = dr.main(argv, platform=platform, parts_factory=factory if parts is not None else None, runner=runner)
        text = out.getvalue()
        return rc, (json.loads(text) if text.strip() else None), text, err.getvalue()

    def assert_no_secrets(self, *texts):
        for t in texts:
            for s in SECRETS:
                self.assertNotIn(s, t)
            self.assertNotIn(PROFILE, t)

    def write_job(self, job_id, status):
        d = self.jobs / job_id
        d.mkdir()
        (d / "job.json").write_text(json.dumps({"job_id": job_id, "host": "uia", "status": status}), encoding="utf-8")

    def make_venv(self):
        py = dr.venv_python(self.venv)
        py.parent.mkdir(parents=True)
        py.write_bytes(b"")
        sp = self.venv / "Lib" / "site-packages"
        (sp / "comtypes").mkdir(parents=True)
        (sp / "comtypes" / "__init__.py").write_text("", encoding="utf-8")
        (sp / "comtypes-1.4.11.dist-info").mkdir()


class PreflightTests(CliHarness):
    def test_non_windows_is_unsupported_platform_and_skips_the_host(self):
        self.make_venv()
        rc, res, _, _ = self.run_cli(["preflight"], parts=self.parts(), platform="linux")
        self.assertEqual(rc, 4)
        self.assertEqual(res["code"], "UNSUPPORTED_PLATFORM")
        self.assertIn("UNSUPPORTED_PLATFORM", res["codes"])
        self.assertEqual(res["checks"]["platform"], {"ok": False, "platform": "linux", "code": "UNSUPPORTED_PLATFORM"})
        self.assertNotIn("instances", res["checks"])
        self.assertEqual(self.factory_calls, 0)

    def test_missing_venv_is_uia_unavailable_with_the_setup_command(self):
        rc, res, _, _ = self.run_cli(["preflight"], parts=self.parts())
        self.assertEqual(rc, 4)
        self.assertIn("UIA_UNAVAILABLE", res["codes"])
        uia = res["checks"]["uia"]
        self.assertFalse(uia["ok"])
        self.assertEqual(uia["code"], "UIA_UNAVAILABLE")
        self.assertEqual(uia["setup_command"], dr.setup_command())
        self.assertTrue(uia["setup_command"].endswith("deep_research.py\" setup --with-venv"))
        self.assertTrue(any("setup --with-venv" in w for w in res["warnings"]))

    def test_venv_with_pinned_comtypes_passes_the_uia_check(self):
        self.make_venv()
        rc, res, _, _ = self.run_cli(["preflight"], parts=self.parts())
        self.assertTrue(res["checks"]["uia"]["ok"])
        self.assertNotIn("UIA_UNAVAILABLE", res["codes"])
        # a different comtypes version is not the pinned one
        (self.venv / "Lib" / "site-packages" / "comtypes-1.4.11.dist-info").rename(
            self.venv / "Lib" / "site-packages" / "comtypes-1.4.10.dist-info")
        self.assertFalse(dr.uia_check()["ok"])

    def test_orphan_instance_is_a_warning_only(self):
        self.make_venv()
        add_our_instance(self.fake)
        self.host.ensure(None)
        self.host.touch("old-job")
        self.write_job("old-job", "completed")
        rc, res, text, _ = self.run_cli(["preflight"], parts=self.parts())
        inst = res["checks"]["instances"]
        self.assertEqual((inst["ok"], inst["count"], inst["orphans"]), (True, 1, 1))
        self.assertEqual(inst["instances"][0]["data_dir"], "dedicated")
        self.assertTrue(any("orphan" in w and "app close" in w for w in res["warnings"]))
        self.assertEqual(res["codes"], [])   # not a failure code
        self.assertNotIn(PROFILE, text)

    def test_instance_of_an_active_job_is_not_an_orphan(self):
        self.make_venv()
        add_our_instance(self.fake)
        self.host.ensure(None)
        self.host.touch("job-a")
        self.write_job("job-a", "plan_ready")
        _, res, _, _ = self.run_cli(["preflight"], parts=self.parts())
        self.assertEqual(res["checks"]["instances"]["orphans"], 0)
        self.assertFalse(any("orphan" in w for w in res["warnings"]))

    def test_inventory_failure_never_blocks(self):
        self.make_venv()
        self.host.os = mock.Mock(processes=mock.Mock(side_effect=gh.HostRefused("sandbox")))
        _, res, _, _ = self.run_cli(["preflight"], parts=self.parts())
        self.assertEqual(res["checks"]["instances"], {"ok": None, "error": "HOST_REFUSED"})
        self.assertEqual(res["codes"], [])

    def test_real_factory_is_refused_in_the_sandbox(self):
        self.make_venv()
        rc, res, _, _ = self.run_cli(["preflight"])
        self.assertEqual(res["checks"]["instances"], {"ok": None, "error": "HOST_REFUSED"})

    def test_app_version_change_is_diagnostic_only(self):
        self.make_venv()
        add_our_instance(self.fake)
        self.host.ensure(None)   # host.json records app_version 1.2.3 (APP is app-1.2.3)
        with mock.patch.object(dr, "installed_app_version", return_value="1.2.4"):
            _, res, _, _ = self.run_cli(["preflight"], parts=self.parts())
        self.assertEqual(res["checks"]["app_version"],
                         {"installed": "1.2.4", "last_success": "1.2.3", "changed": True, "doctor_recommended": True})
        self.assertTrue(any("doctor" in w for w in res["warnings"]))
        self.assertEqual(res["codes"], [])
        with mock.patch.object(dr, "installed_app_version", return_value="1.2.3"):
            _, res, _, _ = self.run_cli(["preflight"], parts=self.parts())
        self.assertFalse(res["checks"]["app_version"]["changed"])

    def test_installed_app_version_reads_the_newest_app_folder(self):
        root = self.tmp / "Local" / "Google" / "Gemini"
        for ver in ("1.2.3", "1.10.0", "2.0.0-empty"):
            (root / f"app-{ver}").mkdir(parents=True)
        for ver in ("1.2.3", "1.10.0"):
            (root / f"app-{ver}" / "Gemini.exe").write_bytes(b"")
        self.assertEqual(dr.installed_app_version(root), "1.10.0")
        self.assertIsNone(dr.installed_app_version(self.tmp / "nowhere"))


class SetupVenvTests(CliHarness):
    def setup_argv(self, *extra):
        return ["setup", "--target-dir", str(self.tmp / "target"), *extra]

    def test_with_venv_creates_the_venv_and_installs_with_hashes(self):
        ran = []
        rc, res, _, _ = self.run_cli(self.setup_argv("--with-venv"), runner=lambda argv: ran.append(argv) or 0)
        self.assertEqual(rc, 0, res)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(len(ran), 2)
        self.assertEqual(ran[0], [sys.executable, "-m", "venv", str(self.venv)])
        pip = ran[1]
        self.assertEqual(pip[:5], [str(dr.venv_python(self.venv)), "-I", "-m", "pip", "install"])
        self.assertIn("--require-hashes", pip)
        self.assertIn("--only-binary=:all:", pip)
        self.assertEqual(pip[pip.index("-r") + 1], str(dr.UIA_REQUIREMENTS))
        req = dr.UIA_REQUIREMENTS.read_text(encoding="utf-8")
        pins = [ln for ln in req.splitlines() if ln.strip() and not ln.startswith("#")]
        self.assertEqual(len(pins), 1)
        self.assertRegex(pins[0], r"^comtypes==1\.4\.11 --hash=sha256:[0-9a-f]{64}$")
        self.assertEqual(res["venv"]["pin"], "comtypes==1.4.11")
        self.assertEqual(res["venv"]["commands"], ran)

    def test_existing_venv_is_not_recreated(self):
        self.make_venv()
        ran = []
        rc, _, _, _ = self.run_cli(self.setup_argv("--with-venv"), runner=lambda argv: ran.append(argv) or 0)
        self.assertEqual(rc, 0)
        self.assertEqual(len(ran), 1)
        self.assertIn("--require-hashes", ran[0])

    def test_without_the_flag_nothing_is_run(self):
        ran = []
        rc, res, _, _ = self.run_cli(self.setup_argv(), runner=lambda argv: ran.append(argv) or 0)
        self.assertEqual((rc, ran), (0, []))
        self.assertNotIn("venv", res)

    def test_pip_failure_is_uia_setup_failed(self):
        rc, res, _, _ = self.run_cli(self.setup_argv("--with-venv"), runner=lambda argv: 1)
        self.assertEqual(rc, 3)
        self.assertEqual((res["status"], res["code"]), ("error", "UIA_SETUP_FAILED"))

    def test_non_windows_never_runs_anything(self):
        ran = []
        rc, res, _, _ = self.run_cli(self.setup_argv("--with-venv"), platform="darwin",
                                     runner=lambda argv: ran.append(argv) or 0)
        self.assertEqual((rc, res["code"], ran), (4, "UNSUPPORTED_PLATFORM", []))


class DoctorTests(CliHarness):
    def test_doctor_json_shape_and_no_sidebar_or_account_strings(self):
        add_our_instance(self.fake)
        add_user_instance(self.fake)
        rc, res, text, err = self.run_cli(["doctor", "--snapshot"], parts=self.parts(FakeTree(page())))
        self.assertEqual(rc, 0, res)
        for key in ("status", "command", "foreground", "host_mode", "host_reason", "app_version", "instances",
                    "binding", "privacy_region", "signed_in", "locators", "summary", "snapshot", "locators_override"):
            self.assertIn(key, res)
        self.assertIsNone(res["foreground"])   # the R6 tracer fills it (Task B5)
        self.assertEqual((res["host_mode"], res["host_reason"], res["app_version"]), ("D2", "reused", "1.2.3"))
        self.assertEqual([i["browser_pid"] for i in res["instances"]], [7000])   # never the user's instance
        self.assertEqual(res["binding"], {"conversation_documents": 1, "bindable": True, "kind": "fresh"})
        self.assertEqual(res["privacy_region"], {"ok": True, "reason": "ok"})
        self.assertFalse(res["signed_in"])   # the fixture shows the `로그인` link
        self.assertEqual(set(res["locators"]), set(bundled_locs()["locators"]))
        self.assertEqual(res["locators"]["prompt_input"]["status"], "resolved")
        self.assertEqual(res["locators"]["prompt_input"]["found"], 1)
        for rec in res["locators"].values():
            self.assertIn(rec["status"], ("resolved", "miss", "config_error", "error"))
        self.assert_no_secrets(text, err)
        snap_path = Path(res["snapshot"])
        self.assertEqual(snap_path.parent, self.tmp / "tool" / "doctor")
        snap = snap_path.read_text(encoding="utf-8")
        self.assert_no_secrets(snap)
        data = json.loads(snap)
        self.assertNotIn("chat1", [n["id"] for n in data["allowed"]])   # sidebar nodes are outside the region
        self.assertTrue(data["outside"])
        texts = [n for n in data["allowed"] if n["ct"] in ("Text", "Hyperlink", "ListItem")]
        self.assertTrue(texts)
        self.assertTrue(all(n["name"] in ("", "<hidden>") for n in texts))
        # read-only: nothing acted on, typed or launched
        self.assertEqual(self.tree.actions, [])
        self.assertEqual(self.fake.launches(), [])

    def test_miss_candidates_never_come_from_the_sidebar(self):
        add_our_instance(self.fake)
        lc = bundled_locs()
        lc["locators"]["new_chat"]["names"] = ["비밀 대화"]   # close to the sidebar chat titles
        lc["locators"]["prompt_input"]["names"] = ["Google 계정"]
        rc, res, text, err = self.run_cli(["doctor"], parts=self.parts(FakeTree(page()), locs=lc))
        self.assertEqual(rc, 0, res)
        for key in ("new_chat", "prompt_input"):
            self.assertEqual(res["locators"][key]["status"], "miss")
        names = [c["name"] for c in res["locators"]["new_chat"]["candidates"]]
        self.assertFalse(any("비밀" in n for n in names), names)
        self.assert_no_secrets(text, err)
        self.assertIsNone(res["snapshot"])

    def test_signed_in_page(self):
        add_our_instance(self.fake)
        _, res, text, _ = self.run_cli(["doctor"], parts=self.parts(FakeTree(page(signed_in=True))))
        self.assertTrue(res["signed_in"])
        self.assert_no_secrets(text)

    def test_no_start_without_an_instance_launches_nothing(self):
        rc, res, _, _ = self.run_cli(["doctor", "--no-start"], parts=self.parts(FakeTree(page())))
        self.assertEqual(rc, 0)
        self.assertEqual((res["instances"], res["binding"], res["locators"], res["host_mode"]), ([], None, None, None))
        self.assertEqual(self.fake.launches(), [])

    def test_gemini_in_use_is_reported_scrubbed(self):
        add_user_instance(self.fake)
        self.host.ensure = mock.Mock(side_effect=gh.HostError("GEMINI_IN_USE", "the user's Gemini runs"))
        rc, res, _, _ = self.run_cli(["doctor"], parts=self.parts(FakeTree(page())))
        self.assertEqual((rc, res["status"], res["code"]), (3, "error", "GEMINI_IN_USE"))

    def test_real_factory_refused_in_the_sandbox(self):
        rc, res, _, _ = self.run_cli(["doctor"])
        self.assertEqual((rc, res["code"]), (3, "HOST_REFUSED"))

    def test_non_windows(self):
        rc, res, _, _ = self.run_cli(["doctor"], parts=self.parts(), platform="linux")
        self.assertEqual((rc, res["code"]), (4, "UNSUPPORTED_PLATFORM"))
        self.assertEqual(self.factory_calls, 0)


class SigninTests(CliHarness):
    def launch_visible(self):
        self.fake.on_launch = lambda cmd, desk: start_d2_instance(self.fake, desktop="Default")

    def test_signin_launches_visibly_detects_and_quits_via_menu(self):
        add_user_instance(self.fake)
        self.launch_visible()
        tree = QuitTree(page(signed_in=True), self.fake)
        rc, res, text, err = self.run_cli(["signin"], parts=self.parts(tree))
        self.assertEqual(rc, 0, res)
        self.assertEqual((res["status"], res["signed_in"], res["visible_mode"], res["desktop"]),
                         ("ok", True, True, "Default"))
        (launch,) = self.fake.launches()
        self.assertEqual(launch[1], gh.build_launch_command(STUB, PROFILE, gh.ALLOWED_FLAG_SWITCHES))
        self.assertEqual(launch[2], "WinSta0\\Default")
        self.assertNotIn("NODE_OPTIONS", launch[3])
        self.assertEqual((res["close"]["method"], res["close"]["remaining"]), ("menu", 0))
        self.assertEqual(self.fake.terminated(), [])
        self.assertFalse([c for c in self.fake.calls if c[0] == "post_close"])
        for desktop, pids in self.tree_calls:   # only our pids, on the default desktop
            self.assertEqual(desktop, "Default")
            self.assertTrue(pids and set(pids) <= OUR_PIDS, pids)
        self.assertTrue(USER_PIDS <= set(self.fake.procs))   # the user's Gemini is untouched
        self.assertIn("signed_in=true", err)
        self.assert_no_secrets(text, err)
        self.assertEqual(self.tree.actions[-1], ("act", "pm_quit", "invoke"))

    def test_not_signed_in_times_out_logs_only_false_and_still_closes(self):
        add_user_instance(self.fake)
        self.launch_visible()
        tree = QuitTree(page(), self.fake)
        rc, res, text, err = self.run_cli(["signin", "--timeout", "20"], parts=self.parts(tree))
        self.assertEqual(rc, 2)
        self.assertEqual((res["status"], res["code"], res["signed_in"]), ("error", "NOT_SIGNED_IN", False))
        self.assertEqual(res["close"]["remaining"], 0)
        signin_lines = [ln for ln in err.splitlines() if "signin:" in ln]
        self.assertTrue(any(ln.endswith("signed_in=false") for ln in signin_lines))
        self.assertFalse(any("signed_in=true" in ln for ln in signin_lines))
        self.assert_no_secrets(text, err)
        self.assertTrue(USER_PIDS <= set(self.fake.procs))

    def test_failed_menu_falls_back_to_our_windows_and_tree_only(self):
        add_user_instance(self.fake)
        self.launch_visible()

        def broken_quit(desktop, pids, hwnds):
            raise OSError("menu gone")
        tree = FakeTree(page(signed_in=True))
        rc, res, _, _ = self.run_cli(["signin"], parts=self.parts(tree, quit_via_menu=broken_quit))
        self.assertEqual(rc, 0, res)
        self.assertEqual((res["close"]["method"], res["close"]["menu_error"], res["close"]["remaining"]),
                         ("host_close", "OSError", 0))
        self.assertTrue(set(self.fake.terminated()) <= OUR_PIDS, self.fake.terminated())
        for c in self.fake.calls:
            if c[0] == "post_close":
                self.assertEqual(c[1], "Default")
                self.assertTrue(all(h // 10 in OUR_PIDS for h in c[2]), c)
        self.assertTrue(USER_PIDS <= set(self.fake.procs))

    def test_busy_when_our_instance_runs_on_the_hidden_desktop(self):
        add_our_instance(self.fake)
        rc, res, _, _ = self.run_cli(["signin"], parts=self.parts(FakeTree(page())))
        self.assertEqual((rc, res["code"]), (3, "HOST_BUSY"))
        self.assertEqual(self.fake.launches(), [])
        self.assertEqual(self.fake.terminated(), [])

    def test_window_never_appears_is_start_timeout_and_nothing_of_the_user_is_closed(self):
        add_user_instance(self.fake)   # the stub hands off to the user's Gemini: no process of ours appears
        rc, res, _, _ = self.run_cli(["signin"], parts=self.parts(FakeTree(page())))
        self.assertEqual((rc, res["code"]), (3, "HOST_START_TIMEOUT"))
        self.assertEqual(self.fake.terminated(), [])
        self.assertFalse([c for c in self.fake.calls if c[0] == "post_close"])
        self.assertEqual(self.tree_calls, [])
        self.assertTrue(USER_PIDS <= set(self.fake.procs))

    def test_lock_held_is_host_busy(self):
        with self.host.lock():
            rc, res, _, _ = self.run_cli(["signin"], parts=self.parts(FakeTree(page())))
        self.assertEqual(res["code"], "HOST_BUSY")
        self.assertEqual(self.fake.launches(), [])


class AppTests(CliHarness):
    def test_status_lists_only_ours_without_paths(self):
        add_our_instance(self.fake)
        add_user_instance(self.fake)
        rc, res, text, _ = self.run_cli(["app", "status"], parts=self.parts())
        self.assertEqual(rc, 0)
        self.assertEqual((res["status"], res["command"], res["count"]), ("ok", "app status", 1))
        (inst,) = res["instances"]
        self.assertEqual(set(inst), {"desktop", "mode", "browser_pid", "pids", "data_dir", "orphan", "last_job",
                                     "last_step_at", "started_at"})
        self.assertEqual((inst["desktop"], inst["mode"], inst["data_dir"], sorted(inst["pids"])),
                         (OURS, "D2", "dedicated", sorted(OUR_PIDS)))
        self.assertNotIn(PROFILE, text)
        self.assertEqual(self.fake.terminated(), [])

    def test_close_only_ours_via_menu(self):
        add_our_instance(self.fake)
        add_user_instance(self.fake)
        rc, res, _, _ = self.run_cli(["app", "close"], parts=self.parts(QuitTree(page(), self.fake)))
        self.assertEqual(rc, 0, res)
        (closed,) = res["closed"]
        self.assertEqual((closed["browser_pid"], closed["method"], closed["remaining"]), (7000, "menu", 0))
        self.assertEqual(res["remaining"], 0)
        self.assertEqual(self.tree_calls, [(OURS, sorted(OUR_PIDS))])
        self.assertEqual(self.fake.terminated(), [])
        self.assertTrue(USER_PIDS <= set(self.fake.procs))

    def test_close_when_the_menu_fails_terminates_only_ours(self):
        add_our_instance(self.fake)
        add_user_instance(self.fake)
        parts = self.parts(OSError("no menu"))
        rc, res, _, _ = self.run_cli(["app", "close"], parts=parts)
        self.assertEqual(rc, 0, res)
        (closed,) = res["closed"]
        self.assertEqual((closed["method"], closed["menu_error"], closed["remaining"]), ("terminate", "OSError", 0))
        self.assertEqual(set(self.fake.terminated()), OUR_PIDS)
        for c in self.fake.calls:
            if c[0] == "post_close":
                self.assertEqual(c[1], OURS)
        self.assertTrue(USER_PIDS <= set(self.fake.procs))

    def test_close_with_nothing_of_ours(self):
        add_user_instance(self.fake)
        rc, res, _, _ = self.run_cli(["app", "close"], parts=self.parts(QuitTree(page(), self.fake)))
        self.assertEqual((rc, res["closed"], res["remaining"]), (0, [], 0))
        self.assertEqual(self.fake.terminated(), [])
        self.assertEqual(self.tree_calls, [])

    def test_app_non_windows(self):
        rc, res, _, _ = self.run_cli(["app", "status"], parts=self.parts(), platform="linux")
        self.assertEqual((rc, res["code"]), (4, "UNSUPPORTED_PLATFORM"))

    def test_menu_quit_walks_menu_file_quit(self):
        tree = FakeTree(page())
        dr.menu_quit(tree, bundled_locs(), self.fake.clock, self.fake.sleep)
        self.assertEqual(tree.actions, [("act", "tb_menu", "invoke"), ("act", "pm_file", "expand"),
                                        ("act", "pm_quit", "invoke")])


if __name__ == "__main__":
    unittest.main()

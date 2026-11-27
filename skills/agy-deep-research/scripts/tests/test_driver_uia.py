import sandbox  # noqa: F401  (must stay the first import: blocks real-app access)
# test_driver_uia.py - run_deep_research.py drives plan / edit / probe on the UI Automation backend.
# Everything runs on fakes: FakeTree plan fixtures (AutoTree from test_plan_steps), a real gemini_host.Host over
# the in-memory FakeOS of test_gemini_host_win32 (lock and host.json in temp files). No real desktop, window,
# process or COM object is touched; the real driver factory refuses inside the sandbox.
import contextlib
import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

import gemini_host as gh  # noqa: E402
import relay_step  # noqa: E402
import run_deep_research as rdr  # noqa: E402
import steps  # noqa: E402
from test_gemini_host_win32 import APP, OURS, UDD, FakeOS, add_user_instance, make_host  # noqa: E402
from test_plan_steps import (BOUND, EMAIL, KO_PLAN, KO_PLAN2, KO_PROMPT, SECRET_TITLE, AutoTree, Clock,  # noqa: E402
                             acts, cfg, load, locs, nodes, rename, set_auto)

FRESH = "https://gemini.google.com/app"
BOUND_ID = BOUND.rsplit("/", 1)[1]
OTHER = "https://gemini.google.com/app/aaaabbbbccccdddd"
FEEDBACK = "2번 단계를 빼 주세요."
JOB_ID = "20261127-job1"


def blocks(out):
    """(conversation line index, hook start index, hook payload) of a driver stdout."""
    lines = out.splitlines()
    conv = [i for i, ln in enumerate(lines) if ln.startswith(rdr.CONVERSATION_PREFIX)]
    hook = [i for i, ln in enumerate(lines) if ln.strip() == relay_step.MARK_START]
    run = relay_step.Run()
    run.out = [out]
    payload = relay_step.parse_hook(run)
    return conv, hook, payload


class DriverHarness(unittest.TestCase):
    """main() with a fake factory: a real Host over FakeOS (our D2 instance already on the hidden desktop)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.job_dir = os.path.join(self._tmp.name, JOB_ID)
        os.makedirs(os.path.join(self.job_dir, "relay"))
        with open(os.path.join(self.job_dir, "job.json"), "w", encoding="utf-8") as f:
            json.dump({"job_id": JOB_ID, "host": "uia"}, f)
        self.fake = FakeOS()
        self.fake.add_proc(7000, 4242, '"%s" %s' % (APP, UDD))
        self.fake.add_window(OURS, 7000)
        self.lock_path = os.path.join(self._tmp.name, "host.lock")
        self.state_path = os.path.join(self._tmp.name, "host.json")
        self.host = make_host(self.fake, lock_path=self.lock_path, state_path=self.state_path)
        self.factory_calls = []
        self.opened = []
        env = {"GEMINI_DR_MODE": "pro"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("GEMINI_DR_THINKING", None)

    def factory(self, tree, on_open=None):
        def make(args):
            self.factory_calls.append(args.step)

            def open_tree(session):
                self.opened.append(session)
                if on_open is not None:
                    on_open(session)
                return tree
            return rdr.DriverParts(host=self.host, open_tree=open_tree, locs=locs(), step_config=cfg())
        return make

    def run_main(self, argv, tree=None, on_open=None, factory=None):
        out, err = io.StringIO(), io.StringIO()
        argv = list(argv) + ["--job-dir", self.job_dir]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = rdr.main(argv, driver_factory=factory or self.factory(tree, on_open))
        return rc, out.getvalue(), err.getvalue()

    def plan(self, spec=None, **kw):
        tree = AutoTree(spec if spec is not None else load("plan_flow_ko.json"))
        rc, out, err = self.run_main(["--step", "plan", "--raw-prompt", "--prompt", KO_PROMPT], tree, **kw)
        return rc, out, err, tree

    def edit(self, tree, **kw):
        return self.run_main(["--step", "edit", "--resume-url", BOUND, "--edit-prompt", FEEDBACK], tree, **kw)

    def probe(self, tree, url=BOUND):
        return self.run_main(["--step", "probe", "--resume-url", url], tree)

    def last_json(self, out):
        return json.loads(out.strip().splitlines()[-1])

    def driver_state(self):
        with open(os.path.join(self.job_dir, "relay", "driver_state.json"), encoding="utf-8") as f:
            return json.load(f)

    def write_state(self, **state):
        state.setdefault("browser_pid", 7000)
        state.setdefault("at", 1.0)
        with open(os.path.join(self.job_dir, "relay", "driver_state.json"), "w", encoding="utf-8") as f:
            json.dump(state, f)

    def host_record(self):
        with open(self.state_path, encoding="utf-8") as f:
            return json.load(f)

    def stderr_record(self, err):
        recs = [ln for ln in err.splitlines() if ln.startswith(rdr.DRIVER_ERROR_PREFIX)]
        self.assertEqual(len(recs), 1, err)
        return json.loads(recs[0][len(rdr.DRIVER_ERROR_PREFIX):])


class PlanStepTests(DriverHarness):
    def test_plan_prints_conversation_then_the_hook_block(self):
        rc, out, err, tree = self.plan()
        self.assertEqual(rc, 0, (out, err))
        conv, hook, payload = blocks(out)
        self.assertEqual(len(conv), 1, out)
        self.assertEqual(len(hook), 1, out)
        self.assertLess(conv[0], hook[0])
        line = out.splitlines()[conv[0]]
        self.assertEqual(relay_step.parse_conversation_line(line), {"conversation_id": BOUND_ID, "url": BOUND})
        self.assertEqual(payload, {"event": "on_plan_generated", "status": "plan_ready", "url": BOUND,
                                   "plan": KO_PLAN, "plan_count": 1})
        lines = out.splitlines()
        sent = [i for i, ln in enumerate(lines) if relay_step.LINE_PROMPT_SENT in ln]
        self.assertTrue(sent and sent[0] < conv[0], out)
        self.assertEqual(relay_step.parse_plan_block(out), KO_PLAN)
        self.assertEqual(tree.state, "cancelled")
        self.assertNotIn(("act", "plan1_start", "invoke"), tree.actions)
        self.assertEqual(err, "")

    def test_plan_selects_the_model_once_before_sending(self):
        rc, out, _, tree = self.plan()
        self.assertEqual(rc, 0, out)
        a = acts(tree)
        self.assertIn(("act", "mi_neon", "invoke"), a)
        mode_acts = [i for i, x in enumerate(a) if x[1] in ("b_mode", "mi_neon", "mi_thinking")]
        self.assertLess(max(mode_acts), a.index(("act", "b_send", "invoke")))

    def test_conversation_is_never_printed_before_the_url_binds(self):
        buf = {"out": None}
        seen = []
        spec = load("plan_flow_ko.json")
        bound_states = {"bound", "card", "cancelled", "researching", "stopped", "card_fired"}

        class Watch(AutoTree):
            def snapshot(self, doc_id):
                node = super().snapshot(doc_id)
                text = buf["out"].getvalue() if buf["out"] is not None else ""
                seen.append((self.state, rdr.CONVERSATION_PREFIX in text))
                return node

        tree = Watch(spec)
        out, err = io.StringIO(), io.StringIO()
        buf["out"] = out
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = rdr.main(["--step", "plan", "--raw-prompt", "--prompt", KO_PROMPT, "--job-dir", self.job_dir],
                          driver_factory=self.factory(tree))
        self.assertEqual(rc, 0, out.getvalue())
        early = [s for s, printed in seen if printed and s not in bound_states]
        self.assertEqual(early, [], seen)
        self.assertTrue(any(printed for _, printed in seen))

    def test_no_conversation_line_when_the_url_never_binds(self):
        spec = load("plan_flow_ko.json")
        spec["auto"] = [a for a in spec["auto"] if a["from"] != "sent"]
        tree = AutoTree(spec)
        with mock.patch.object(rdr, "default_step_config", return_value=cfg(bind_timeout_s=3)):
            rc, out, err = self.run_main(["--step", "plan", "--raw-prompt", "--prompt", KO_PROMPT],
                                         factory=self._factory_with_cfg(tree, cfg(bind_timeout_s=3)))
        self.assertNotIn(rdr.CONVERSATION_PREFIX, out)
        self.assertIn(relay_step.LINE_PROMPT_SENT, out)  # the send was invoked: the relay must never re-run it
        rec = self.stderr_record(err)
        self.assertEqual(rec["code"], "UI_TIMEOUT")
        self.assertEqual(rc, rec["exit_code"])
        self.assertNotIn(relay_step.MARK_START, out)

    def _factory_with_cfg(self, tree, step_config):
        def make(args):
            return rdr.DriverParts(host=self.host, open_tree=lambda s: tree, locs=locs(), step_config=step_config)
        return make

    def test_plan_persists_the_cancel_result_and_touches_the_host(self):
        rc, out, _, _ = self.plan()
        self.assertEqual(rc, 0, out)
        st = self.driver_state()
        self.assertEqual((st["step"], st["cancel"], st["cards"], st["url"], st["host_mode"]),
                         ("plan", "cancel_button", 1, BOUND, "D2"))
        rec = self.host_record()
        self.assertEqual(rec["last_job"], JOB_ID)
        self.assertIsNotNone(rec["last_step_at"])

    def test_plan_en_synthetic_with_thinking_level(self):
        with mock.patch.dict(os.environ, {"GEMINI_DR_MODE": "flash", "GEMINI_DR_THINKING": "high"}):
            tree = AutoTree(load("plan_flow_en_synthetic.json"))
            rc, out, err = self.run_main(["--step", "plan", "--raw-prompt", "--prompt", "Research batteries."], tree)
        self.assertEqual(rc, 0, (out, err))
        _, _, payload = blocks(out)
        self.assertTrue(payload["plan"].startswith("Here is my research plan."))
        self.assertIn(("act", "ti_high", "invoke"), tree.actions)

    def test_thinking_level_matches_the_korean_label(self):
        # replaces the Playwright thinking-menu tests: 'high' is found as the ko item '높음' in the menu itself
        spec = load("plan_flow_en_synthetic.json")
        rename(spec, "High", "높음")
        rename(spec, "Selected High", "선택됨 높음")
        with mock.patch.dict(os.environ, {"GEMINI_DR_MODE": "flash", "GEMINI_DR_THINKING": "high"}):
            tree = AutoTree(spec)
            rc, out, err = self.run_main(["--step", "plan", "--raw-prompt", "--prompt", "Research batteries."], tree)
        self.assertEqual(rc, 0, (out, err))
        self.assertIn(("act", "ti_high", "invoke"), tree.actions)
        self.assertEqual(tree.state, "cancelled")

    def test_unknown_thinking_level_is_model_not_available(self):
        with mock.patch.dict(os.environ, {"GEMINI_DR_MODE": "flash", "GEMINI_DR_THINKING": "turbo"}):
            tree = AutoTree(load("plan_flow_en_synthetic.json"))
            rc, out, err = self.run_main(["--step", "plan", "--raw-prompt", "--prompt", "Research batteries."], tree)
        rec = self.stderr_record(err)
        self.assertEqual((rec["code"], rec["details"]["kind"]), ("MODEL_NOT_AVAILABLE", "thinking"))
        self.assertEqual(rc, 2)
        self.assertNotIn(("act", "b_send", "invoke"), tree.actions)

    def test_url_change_during_the_plan_wait_is_a_mismatch(self):
        spec = load("plan_flow_ko.json")
        for d in spec["states"]["card"]["documents"]:
            if d.get("value") == BOUND:
                d["value"] = OTHER
        rc, out, err, tree = self.plan(spec)
        rec = self.stderr_record(err)
        self.assertEqual((rc, rec["code"]), (2, "CONVERSATION_MISMATCH"))
        conv, _, payload = blocks(out)
        self.assertEqual(len(conv), 1)
        self.assertIn(BOUND_ID, out.splitlines()[conv[0]])
        self.assertIsNone(payload)
        self.assertNotIn(("act", "plan1_cancel", "invoke"), tree.actions)  # another conversation's card is never touched
        err_run = relay_step.Run()
        err_run.rc, err_run.err = rc, [err]
        fail = relay_step.classify(err_run, "plan")
        self.assertEqual((fail.code, fail.exit_code, fail.hint), ("CONVERSATION_MISMATCH", 2, relay_step.PLAN_MISMATCH_HINT))

    def test_plan_without_a_prompt_is_rejected_by_the_parser(self):
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(io.StringIO()):
            rdr.parse_args(["--step", "plan"])
        self.assertEqual(cm.exception.code, 2)


class EditStepTests(DriverHarness):
    def test_edit_passes_the_previous_cancel_result(self):
        spec = load("plan_flow_no_cancel.json")
        spec["transitions"].append({"from": "card", "on": ["act", "plan1_edit", "invoke"], "to": "canned_gen"})
        tree = AutoTree(spec)
        self.assertEqual(steps.wait_plan_and_cancel(tree, locs(), cfg(), BOUND, 0)["cancel"], "edit_fallback")
        tree.actions.clear()
        self.write_state(step="plan", cancel="edit_fallback", cards=1, url=BOUND, host_mode="D2")
        real = steps.edit_plan
        with mock.patch.object(rdr.steps, "edit_plan", side_effect=real) as ep:
            rc, out, err = self.edit(tree)
        self.assertEqual(rc, 0, (out, err))
        self.assertEqual(ep.call_args.kwargs.get("prev_cancel"), "edit_fallback")
        # plan_edit was already invoked by the plan step's fallback: not again
        self.assertEqual(acts(tree), [("act", "b_send", "invoke"), ("act", "plan2_cancel", "invoke")])
        conv, hook, payload = blocks(out)
        self.assertLess(conv[0], hook[0])
        self.assertEqual(payload, {"event": "on_plan_revised", "status": "plan_ready", "url": BOUND,
                                   "plan": KO_PLAN2, "plan_count": 2})
        self.assertIn(relay_step.LINE_PROMPT_SENT, out)
        st = self.driver_state()
        self.assertEqual((st["step"], st["cancel"], st["cards"]), ("edit", "cancel_button", 2))

    def test_edit_after_a_plain_cancel_invokes_plan_edit(self):
        tree = AutoTree(load("plan_flow_edit_two_cards.json"))
        self.write_state(step="plan", cancel="cancel_button", cards=1, url=BOUND, host_mode="D2")
        with mock.patch.object(rdr.steps, "edit_plan", side_effect=steps.edit_plan) as ep:
            rc, out, err = self.edit(tree)
        self.assertEqual(rc, 0, (out, err))
        self.assertEqual(ep.call_args.kwargs.get("prev_cancel"), "cancel_button")
        self.assertEqual(acts(tree), [("act", "plan1_edit", "invoke"), ("act", "b_send", "invoke"),
                                      ("act", "plan2_cancel", "invoke")])
        self.assertIn(relay_step.LINE_EDIT, out)
        lines = out.splitlines()
        self.assertLess(min(i for i, ln in enumerate(lines) if relay_step.LINE_EDIT in ln),
                        min(i for i, ln in enumerate(lines) if relay_step.LINE_PROMPT_SENT in ln))

    def test_plan_then_edit_hands_over_the_cancel_result(self):
        rc, out, _, _ = self.plan()
        self.assertEqual(rc, 0, out)
        fake_res = {"cancel": "cancel_button", "cards": 2, "plan_text": "revised"}
        tree = AutoTree(load("plan_flow_edit_two_cards.json"))
        with mock.patch.object(rdr.steps, "edit_plan", return_value=fake_res) as ep:
            rc, out, err = self.edit(tree)
        self.assertEqual(rc, 0, (out, err))
        self.assertEqual(ep.call_args.kwargs.get("prev_cancel"), "cancel_button")

    def test_edit_never_touches_the_model(self):
        # model lock: the mode is chosen once in the new chat; an existing conversation is never re-moded
        tree = AutoTree(load("plan_flow_edit_two_cards.json"))
        with mock.patch.object(rdr.steps, "select_mode") as sm, \
                mock.patch.object(rdr.steps, "ensure_fresh_chat") as fresh:
            rc, out, err = self.edit(tree)
        self.assertEqual(rc, 0, (out, err))
        sm.assert_not_called()
        fresh.assert_not_called()
        self.assertFalse([a for a in acts(tree) if a[1] in ("b_mode", "mi_neon", "mi_thinking")])

    def test_edit_on_another_conversation_acts_on_nothing(self):
        tree = AutoTree(load("plan_flow_edit_two_cards.json"))
        rc, out, err = self.run_main(["--step", "edit", "--resume-url", OTHER, "--edit-prompt", FEEDBACK], tree)
        rec = self.stderr_record(err)
        self.assertEqual((rc, rec["code"]), (2, "CONVERSATION_MISMATCH"))
        self.assertEqual(tree.actions, [])
        self.assertNotIn(rdr.CONVERSATION_PREFIX, out)
        self.assertNotIn(relay_step.LINE_EDIT, out)

    def test_edit_requires_a_resume_url(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            rdr.parse_args(["--step", "edit", "--edit-prompt", "Add a section."])


class ProbeStepTests(DriverHarness):
    def test_probe_contract(self):
        tree = AutoTree(load("plan_flow_edit_two_cards.json"))
        rc, out, err = self.probe(tree)
        self.assertEqual(rc, 0, (out, err))
        info = self.last_json(out)
        self.assertEqual(info, {"start_enabled": True, "plan_cards": 1, "url": BOUND})
        self.assertIs(type(info["start_enabled"]), bool)
        self.assertIs(type(info["plan_cards"]), int)
        self.assertEqual(tree.actions, [])  # read-only
        done = subprocess.CompletedProcess([], rc, stdout=out, stderr=err)
        with mock.patch.object(relay_step.subprocess, "run", return_value=done):
            got = relay_step.probe_page({"resume_url": BOUND, "driver": rdr.__file__}, sys.executable)
        self.assertNotIn("error", got)
        self.assertEqual((got["active_confirm"], got["plan_cards"]), (1, 1))

    def test_probe_counts_every_card_and_reads_disabled_start(self):
        spec = load("plan_flow_ko.json")
        spec["state"] = "card_fired"   # the countdown fired: the card's start is disabled
        tree = AutoTree(spec)
        rc, out, err = self.probe(tree)
        self.assertEqual(rc, 0, (out, err))
        info = self.last_json(out)
        self.assertEqual((info["start_enabled"], info["plan_cards"]), (False, 1))
        spec = load("plan_flow_edit_two_cards.json")
        spec["state"] = "edit_cancelled"
        rc, out, _ = self.probe(AutoTree(spec))
        self.assertEqual(self.last_json(out)["plan_cards"], 2)

    def test_probe_on_another_conversation_fails_closed(self):
        tree = AutoTree(load("plan_flow_edit_two_cards.json"))
        rc, out, err = self.probe(tree, url=OTHER)
        self.assertEqual(rc, 2)
        last = self.last_json(out)
        self.assertEqual((last["status"], last["code"]), ("error", "CONVERSATION_MISMATCH"))
        done = subprocess.CompletedProcess([], rc, stdout=out, stderr=err)
        with mock.patch.object(relay_step.subprocess, "run", return_value=done):
            got = relay_step.probe_page({"resume_url": OTHER, "driver": rdr.__file__}, sys.executable)
        self.assertIn("error", got)

    def test_probe_never_touches_the_model(self):
        tree = AutoTree(load("plan_flow_edit_two_cards.json"))
        with mock.patch.object(rdr.steps, "select_mode") as sm:
            self.probe(tree)
        sm.assert_not_called()


class ErrorMappingTests(DriverHarness):
    def run_classified(self, rc, err, step="plan"):
        run = relay_step.Run()
        run.rc, run.err = rc, [err]
        return relay_step.classify(run, step)

    def test_step_error_is_one_json_line_with_its_code_and_exit_code(self):
        spec = load("plan_flow_ko.json")
        set_auto(spec, "bound", "safety", 2)
        rc, out, err, tree = self.plan(spec)
        self.assertEqual(rc, 2)
        last = self.last_json(out)
        self.assertEqual((last["status"], last["code"]), ("error", "SAFETY_BLOCKED"))
        rec = self.stderr_record(err)
        self.assertEqual((rec["code"], rec["exit_code"]), ("SAFETY_BLOCKED", 2))
        fail = self.run_classified(rc, err)
        self.assertEqual((fail.code, fail.exit_code), ("SAFETY_BLOCKED", 2))
        self.assertFalse(os.path.exists(os.path.join(self.job_dir, "relay", "driver_state.json")))
        self.assertNotIn("last_job", {k: v for k, v in self.host_record().items() if v})

    def test_exit_codes_follow_the_relay_table(self):
        for code, needle_code in (("CONVERSATION_MISMATCH", "CONVERSATION_MISMATCH"), ("SAFETY_BLOCKED", "SAFETY_BLOCKED"),
                                  ("PLAN_TIMEOUT", "PLAN_TIMEOUT"), ("UIA_DRIVER_NOT_IMPLEMENTED", None),
                                  ("DEEP_RESEARCH_TOGGLE_NOT_FOUND", None)):
            with self.subTest(code=code):
                rows = [f for f in relay_step.FAILURES if f[1] == code]
                self.assertTrue(rows)
                self.assertEqual(rdr.exit_code_for(code), rows[0][2])
        for code in ("UNREVIEWED_START", "COUNTDOWN_NOT_CANCELLED", "PLAN_CARD_COUNT_MISMATCH", "GEMINI_ERROR_RESPONSE",
                     "PROMPT_READBACK_MISMATCH", "INPUT_NOT_EMPTY", "MODEL_NOT_AVAILABLE", "NO_PLAN_CARD"):
            self.assertEqual(rdr.exit_code_for(code), 2, code)
        for code in ("UI_LOCATOR_MISS", "UIA_UNAVAILABLE", "HOST_BUSY", "HOST_REFUSED", "DRIVER_ERROR", "UI_TIMEOUT"):
            self.assertEqual(rdr.exit_code_for(code), 3, code)

    def test_driver_error_record_beats_the_argparse_exit_code(self):
        err = rdr.DRIVER_ERROR_PREFIX + json.dumps({"status": "error", "code": "UNREVIEWED_START", "exit_code": 2,
                                                    "details": {"stopped": True}}) + "\n"
        fail = self.run_classified(2, err)
        self.assertEqual((fail.code, fail.exit_code), ("UNREVIEWED_START", 2))
        self.assertEqual(fail.extra.get("details"), {"stopped": True})

    def test_locator_miss_is_exit_3_with_its_payload(self):
        spec = load("plan_flow_ko.json")
        rename(spec, "연구 시작", "조사 시작")
        rc, out, err, _ = self.plan(spec)
        rec = self.stderr_record(err)
        self.assertEqual((rc, rec["code"]), (3, "UI_LOCATOR_MISS"))
        self.assertEqual(rec["details"]["locator"], "plan_start")
        self.assertIn("override_snippet", rec["details"])

    def test_host_error_gemini_in_use(self):
        fake = FakeOS()
        add_user_instance(fake)
        self.host = make_host(fake, lock_path=self.lock_path, state_path=self.state_path)
        with open(os.path.join(self.job_dir, "job.json"), "w", encoding="utf-8") as f:
            json.dump({"job_id": JOB_ID, "host": "uia", "host_mode": "D1"}, f)
        tree = AutoTree(load("plan_flow_ko.json"))
        rc, out, err, _ = self.plan()
        rec = self.stderr_record(err)
        self.assertEqual(rec["code"], "GEMINI_IN_USE")
        self.assertEqual(rc, rdr.exit_code_for("GEMINI_IN_USE"))
        self.assertEqual(fake.launches(), [])
        self.assertEqual(self.opened, [])
        self.assertEqual(tree.actions, [])

    def test_uia_unavailable(self):
        def fail(_session):
            raise rdr.uia_backend.UiaUnavailable("comtypes", detail="ImportError")
        rc, out, err, tree = self.plan(on_open=fail)
        rec = self.stderr_record(err)
        self.assertEqual((rc, rec["code"]), (3, "UIA_UNAVAILABLE"))
        self.assertEqual(tree.actions, [])

    def test_backend_and_programming_errors(self):
        class Gone(AutoTree):
            def snapshot(self, doc_id):
                raise OSError("COM failure for test@example.invalid")
        rc, out, err = self.run_main(["--step", "probe", "--resume-url", BOUND], Gone(load("plan_flow_edit_two_cards.json")))
        rec = self.stderr_record(err)
        self.assertEqual((rc, rec["code"]), (3, "UIA_BACKEND_ERROR"))

        class Bug(AutoTree):
            def documents(self):
                raise ZeroDivisionError("bug near test@example.invalid")
        rc, out, err = self.run_main(["--step", "probe", "--resume-url", BOUND], Bug(load("plan_flow_edit_two_cards.json")))
        rec = self.stderr_record(err)
        self.assertEqual((rc, rec["code"]), (3, "DRIVER_ERROR"))
        self.assertNotIn(EMAIL, out + err)
        self.assertNotIn("Traceback", err)


class LockTests(DriverHarness):
    def test_lock_is_held_for_the_whole_step(self):
        contention = []

        class Probe(AutoTree):
            def snapshot(inner, doc_id):  # noqa: N805
                other = make_host(FakeOS(), lock_path=self.lock_path)
                try:
                    with other.lock():
                        contention.append("free")
                except gh.HostError as exc:
                    contention.append(exc.code)
                return AutoTree.snapshot(inner, doc_id)

        rc, out, err = self.run_main(["--step", "plan", "--raw-prompt", "--prompt", KO_PROMPT],
                                     Probe(load("plan_flow_ko.json")))
        self.assertEqual(rc, 0, (out, err))
        self.assertTrue(contention)
        self.assertEqual(set(contention), {"HOST_BUSY"})
        with make_host(FakeOS(), lock_path=self.lock_path).lock():  # released afterwards
            pass

    def test_held_lock_is_host_busy_before_anything_runs(self):
        tree = AutoTree(load("plan_flow_ko.json"))
        other = make_host(FakeOS(), lock_path=self.lock_path)
        with other.lock():
            rc, out, err = self.run_main(["--step", "plan", "--raw-prompt", "--prompt", KO_PROMPT], tree)
        rec = self.stderr_record(err)
        self.assertEqual((rc, rec["code"]), (3, "HOST_BUSY"))
        self.assertEqual(self.fake.calls, [])  # ensure never ran
        self.assertEqual(self.opened, [])
        self.assertEqual(tree.actions, [])


class PrivacyTests(DriverHarness):
    def leaky_spec(self):
        spec = load("plan_flow_ko.json")
        priv = load("privacy_page.json")
        pdoc = priv["states"]["s"]["documents"][0]
        side = [d for d in nodes({"states": {"s": {"documents": [pdoc]}}}) if d["id"] == "sidebar"][0]
        extra = [c for c in side["children"] if c["id"] != "nav1"]
        for d in nodes(spec):
            if d["id"] == "sidebar":
                d["children"] = d["children"] + copy.deepcopy(extra)
            if d["id"] == "c1_body":  # Gemini echoes an account-like string inside the plan card
                d["children"].insert(1, {"id": "c1_leak", "ct": "Text", "name": "문의: " + EMAIL})
        return spec

    def assert_clean(self, text):
        self.assertNotIn(EMAIL, text)
        self.assertNotIn("example.invalid", text)
        self.assertNotIn(SECRET_TITLE, text)

    def test_nothing_unscrubbed_reaches_stdout_or_stderr(self):
        rc, out, err, _ = self.plan(self.leaky_spec())
        self.assertEqual(rc, 0, (out, err))
        self.assert_clean(out + err)
        _, _, payload = blocks(out)
        self.assertIn("<redacted:", payload["plan"])
        self.assert_clean(json.dumps(self.driver_state(), ensure_ascii=False))

    def test_error_details_are_scrubbed(self):
        spec = self.leaky_spec()
        rename(spec, "코드 및 복잡한 작업 4 카본", "코드 작업 " + EMAIL)
        with mock.patch.dict(os.environ, {"GEMINI_DR_MODE": "Ultra"}):
            rc, out, err, _ = self.plan(spec)
        rec = self.stderr_record(err)
        self.assertEqual(rec["code"], "MODEL_NOT_AVAILABLE")
        self.assert_clean(out + err)
        spec = self.leaky_spec()
        rename(spec, "연구 시작", "조사 시작")
        rc, out, err, _ = self.plan(spec)
        self.assertEqual(self.stderr_record(err)["code"], "UI_LOCATOR_MISS")
        self.assert_clean(out + err)


class NotImplementedStepTests(DriverHarness):
    def test_approve_collect_auto_still_exit_3(self):
        for argv in (["--step", "approve", "--resume-url", BOUND], ["--step", "collect", "--resume-url", BOUND],
                     ["--step", "auto", "--prompt", "topic"], ["--resume-url", BOUND]):
            with self.subTest(argv=argv):
                rc, out, err = self.run_main(argv, AutoTree(load("plan_flow_ko.json")))
                self.assertEqual(rc, 3)
                self.assertEqual(self.last_json(out), rdr.DRIVER_NOT_IMPLEMENTED)
                self.assertEqual(self.stderr_record(err)["code"], "UIA_DRIVER_NOT_IMPLEMENTED")
                # no report can be accepted: no provenance, no report block
                self.assertNotIn(rdr.PROVENANCE_PREFIX, out)
                self.assertNotIn(relay_step.REPORT_START, out)
        self.assertEqual(self.factory_calls, [])
        self.assertEqual(self.fake.calls, [])

    def test_dispatch_table_names_every_step(self):
        self.assertEqual(set(rdr.STEP_HANDLERS), {"plan", "edit", "probe", "approve", "collect", "auto"})
        for name in ("approve", "collect", "auto"):
            self.assertIsNone(rdr.STEP_HANDLERS[name])

    def test_old_page_code_is_gone(self):
        for name in ("run_flow", "wait_for_new_plan", "apply_plan_edit", "wait_for_report_completion",
                     "prevent_background_throttling", "ensure_pro_mode", "HTML_TO_MARKDOWN_JS"):
            self.assertFalse(hasattr(rdr, name), name)


class RealFactoryTests(unittest.TestCase):
    def test_real_factory_refuses_in_the_sandbox(self):
        out, err = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = rdr.main(["--step", "probe", "--resume-url", BOUND, "--job-dir", tmp])
        self.assertEqual(rc, 3)
        self.assertEqual(json.loads(out.getvalue().strip().splitlines()[-1])["code"], "HOST_REFUSED")

    def test_default_step_config_uses_the_plan_max_wait(self):
        with mock.patch.dict(os.environ, {"GEMINI_DR_PLAN_MAX_WAIT": "77"}):
            self.assertEqual(rdr.plan_max_wait_default(), 77)
            self.assertEqual(rdr.default_step_config().card_timeout_s, 77)
        with mock.patch.dict(os.environ, {}):
            os.environ.pop("GEMINI_DR_PLAN_MAX_WAIT", None)
            self.assertEqual(rdr.plan_max_wait_default(), 900)

    def test_default_stub_is_the_launcher_under_localappdata(self):
        self.assertEqual(rdr.default_stub({"LOCALAPPDATA": r"C:\L"}), r"C:\L\Google\Gemini\Gemini.exe")


class DriverStateTests(DriverHarness):
    """relay/driver_state.json: strict schema; its host mode must agree with job.json and host.json (fail closed)."""

    def edit_tree(self):
        return AutoTree(load("plan_flow_edit_two_cards.json"))

    def record_instance(self):
        rc, out, err = self.probe(self.edit_tree())   # ensure() writes host.json for our D2 instance (pid 7000)
        self.assertEqual(rc, 0, (out, err))
        self.assertEqual((self.host_record()["mode"], self.host_record()["browser_pid"]), ("D2", 7000))

    def assert_refused(self, rc, err, tree):
        rec = self.stderr_record(err)
        self.assertEqual((rc, rec["code"]), (3, "DRIVER_STATE_MISMATCH"))
        self.assertEqual(tree.actions, [])
        self.assertEqual(self.fake.launches(), [])

    def test_saved_state_matches_the_schema(self):
        rc, out, _, _ = self.plan()
        self.assertEqual(rc, 0, out)
        st = self.driver_state()
        self.assertTrue(rdr.valid_state(st), st)
        self.assertEqual(st["browser_pid"], 7000)

    def test_state_mode_differing_from_the_recorded_instance_is_refused(self):
        self.record_instance()
        self.write_state(step="plan", cancel="cancel_button", cards=1, url=BOUND, host_mode="D1")
        tree = self.edit_tree()
        rc, out, err = self.edit(tree)
        self.assert_refused(rc, err, tree)

    def test_unconfirmed_d1_pin_is_refused(self):
        self.write_state(step="plan", cancel="cancel_button", cards=1, url=BOUND, host_mode="D1", browser_pid=4321)
        tree = self.edit_tree()
        rc, out, err = self.edit(tree)
        self.assert_refused(rc, err, tree)

    def test_job_json_and_state_disagreeing_is_refused(self):
        with open(os.path.join(self.job_dir, "job.json"), "w", encoding="utf-8") as f:
            json.dump({"job_id": JOB_ID, "host_mode": "D2"}, f)
        self.write_state(step="plan", cancel="cancel_button", cards=1, url=BOUND, host_mode="D1")
        tree = self.edit_tree()
        rc, out, err = self.edit(tree)
        self.assert_refused(rc, err, tree)

    def test_malformed_state_drops_cancel_and_never_downgrades(self):
        bad = [dict(step="plan", cancel="edit_fallback", cards=1, url=BOUND, host_mode="D1", extra=1),
               dict(step="plan", cancel="edit_fallback", cards=True, url=BOUND, host_mode="D1"),
               dict(step="plan", cancel="edit_fallback", cards=1, url="https://evil.example/app/1234abcd", host_mode="D1"),
               dict(step="plan", cancel="skip_edit", cards=1, url=BOUND, host_mode="D1")]
        for state in bad:
            with self.subTest(state=state):
                self.write_state(**state)
                self.assertFalse(rdr.valid_state(rdr.JobFiles(self.job_dir)._read(rdr.STATE_FILE)))
                tree = self.edit_tree()
                with mock.patch.object(self.host, "ensure", side_effect=self.host.ensure) as ens, \
                        mock.patch.object(rdr.steps, "edit_plan", side_effect=steps.edit_plan) as ep:
                    rc, out, err = self.edit(tree)
                self.assertEqual(rc, 0, (out, err))
                self.assertEqual(ens.call_args.args, ("D2",))             # pinned to D2, not the file's D1
                self.assertIsNone(ep.call_args.kwargs.get("prev_cancel"))  # cancel treated as absent
                self.assertIn(("act", "plan1_edit", "invoke"), tree.actions)
        with open(os.path.join(self.job_dir, "relay", "driver_state.json"), "w", encoding="utf-8") as f:
            f.write("not json")
        self.assertEqual(rdr.JobFiles(self.job_dir).pinned_mode(None), "D2")

    def test_no_state_leaves_the_mode_to_the_host(self):
        self.assertIsNone(rdr.JobFiles(self.job_dir).pinned_mode(None))


class BackendErrorStopTests(DriverHarness):
    """A backend failure during the plan wait cancels the countdown / stops the response before it propagates."""

    def flaky(self, fail_in_state, times=1):
        state = {"left": times}

        class Flaky(AutoTree):
            def snapshot(inner, doc_id):  # noqa: N805
                node = AutoTree.snapshot(inner, doc_id)
                if inner.state == fail_in_state and state["left"] > 0:   # the snapshot that would show this state
                    state["left"] -= 1
                    raise OSError("COM failure")
                return node
        return Flaky

    def test_backend_error_after_send_cancels_the_countdown_and_raises(self):
        spec = load("plan_flow_ko.json")
        set_auto(spec, "bound", "card", 1)
        tree = self.flaky("card")(spec)
        rc, out, err = self.run_main(["--step", "plan", "--raw-prompt", "--prompt", KO_PROMPT], tree)
        rec = self.stderr_record(err)
        self.assertEqual((rc, rec["code"]), (3, "UIA_BACKEND_ERROR"))
        self.assertIn(("act", "plan1_cancel", "invoke"), tree.actions)
        self.assertTrue(rec["details"]["cancel_invoked"])
        self.assertEqual(tree.state, "cancelled")
        self.assertNotIn(relay_step.MARK_START, out)

    def test_backend_error_with_research_running_stops_it(self):
        spec = load("plan_flow_ko.json")
        spec["state"] = "researching"
        tree = self.flaky("researching")(spec)
        with self.assertRaises(OSError) as cm:
            steps.wait_plan_and_cancel(tree, locs(), cfg(), BOUND, 0)
        self.assertIn(("act", "b_stop", "invoke"), tree.actions)
        self.assertEqual((cm.exception.stop_attempt["stop_invoked"], cm.exception.stop_attempt["stopped"]), (True, True))

    def test_backend_that_keeps_failing_still_raises_the_original(self):
        spec = load("plan_flow_ko.json")
        spec["state"] = "card"
        tree = self.flaky("card", times=99)(spec)
        with self.assertRaises(OSError) as cm:
            steps.wait_plan_and_cancel(tree, locs(), cfg(), BOUND, 0)
        self.assertEqual(cm.exception.stop_attempt["error"], "OSError")
        self.assertEqual(tree.actions, [])

    def test_edit_backend_error_goes_through_the_stop_path(self):
        spec = load("plan_flow_edit_two_cards.json")
        tree = self.flaky("edit_card")(spec)
        with self.assertRaises(OSError) as cm:
            steps.edit_plan(tree, locs(), cfg(), BOUND, FEEDBACK)
        self.assertTrue(cm.exception.stop_attempt["cancel_invoked"])
        self.assertIn(("act", "plan2_cancel", "invoke"), tree.actions)


class DeadlineTests(unittest.TestCase):
    def deadlines(self):
        import deep_research as dr   # read-only: the orchestrator's plan/edit deadlines
        return sorted({relay_step.PLAN_DEADLINE, dr.DEFAULT_PLAN_DEADLINE, dr.ARGON_PLAN_DEADLINE})

    def test_every_wait_of_plan_and_edit_fits_inside_the_relay_deadline(self):
        self.assertIs(rdr.RELAY_PLAN_DEADLINE, relay_step.PLAN_DEADLINE)
        for deadline in self.deadlines():
            for step in ("plan", "edit"):
                with self.subTest(deadline=deadline, step=step), mock.patch.dict(os.environ, {}):
                    os.environ.pop("GEMINI_DR_PLAN_MAX_WAIT", None)
                    c = rdr.default_step_config(step, deadline)
                    self.assertGreaterEqual(c.card_timeout_s, rdr.MIN_CARD_WAIT_S)
                    self.assertLessEqual(rdr.step_budget_s(c, step), deadline)
                    before = c.bind_timeout_s if step == "plan" else c.turn_timeout_s
                    # setup, the wait before the card, the card wait, both cancel verifications, the fallback
                    # turn and a stop_response wait
                    self.assertLessEqual(rdr.SETUP_MARGIN_S + before + c.card_timeout_s + 2 * c.cancel_verify_s
                                         + c.turn_timeout_s + c.stop_timeout_s, deadline)

    def test_deadline_comes_from_the_relay_env(self):
        with mock.patch.dict(os.environ, {relay_step.STEP_DEADLINE_ENV: "900"}):
            os.environ.pop("GEMINI_DR_PLAN_MAX_WAIT", None)
            self.assertEqual(rdr.step_deadline_s(), 900)
            self.assertGreater(rdr.default_step_config("plan").card_timeout_s,
                               rdr.default_step_config("plan", relay_step.PLAN_DEADLINE).card_timeout_s)
        with mock.patch.dict(os.environ, {relay_step.STEP_DEADLINE_ENV: "junk"}):
            self.assertEqual(rdr.step_deadline_s(), relay_step.PLAN_DEADLINE)

    def test_too_short_deadline_is_refused(self):
        with self.assertRaises(gh.HostError) as cm:
            rdr.default_step_config("edit", 200)
        self.assertEqual(cm.exception.code, "DEADLINE_TOO_SHORT")

    def test_relay_hands_its_deadline_to_the_driver(self):
        with tempfile.TemporaryDirectory() as job:
            relay = os.path.join(job, "relay")
            os.makedirs(relay)
            prompt = os.path.join(job, "prompt.md")
            with open(prompt, "w", encoding="utf-8") as f:
                f.write("topic")
            req = {"relay_version": 2, "step": "plan", "seq": 1, "driver": rdr.__file__, "prompt_file": prompt,
                   "deadline": 500, "env": {"GEMINI_DR_MODE": "Pro"}}
            run = relay_step.Run()
            run.rc = 0
            with mock.patch.object(relay_step, "run_driver", return_value=run) as rd:
                relay_step.do_step(job, req, relay, os.path.join(relay, "result.json"))
            env = rd.call_args.kwargs["extra_env"]
            self.assertEqual((env[relay_step.STEP_DEADLINE_ENV], env["GEMINI_DR_MODE"]), ("500", "Pro"))
            self.assertEqual(rd.call_args.args[1], job)   # cwd = the job: the driver's JobFiles default


class RelayContractTests(unittest.TestCase):
    def test_probe_runs_in_the_job(self):
        with tempfile.TemporaryDirectory() as job:
            done = subprocess.CompletedProcess([], 0, stdout='{"start_enabled": false, "plan_cards": 1, "url": "u"}\n',
                                               stderr="")
            with mock.patch.object(relay_step.subprocess, "run", return_value=done) as run:
                relay_step.probe_page({"resume_url": BOUND, "driver": rdr.__file__}, sys.executable, job=job)
            argv = run.call_args.args[0]
            self.assertEqual(argv[argv.index("--job-dir") + 1], job)
            self.assertEqual(run.call_args.kwargs["cwd"], job)

    def test_do_step_probe_passes_the_job(self):
        with tempfile.TemporaryDirectory() as job:
            relay = os.path.join(job, "relay")
            os.makedirs(relay)
            with mock.patch.object(relay_step, "probe_page", return_value={"start_enabled": False}) as pp:
                relay_step.do_step(job, {"step": "probe", "seq": 1, "driver": rdr.__file__, "resume_url": BOUND},
                                   relay, os.path.join(relay, "result.json"))
            self.assertEqual(pp.call_args.kwargs.get("job"), job)

    def run_with(self, rc, rec):
        run = relay_step.Run()
        run.rc, run.err = rc, [rdr.DRIVER_ERROR_PREFIX + json.dumps(rec) + "\n"]
        return relay_step.classify(run, "plan")

    def test_unknown_code_or_exit_code_mismatch_is_driver_crash(self):
        for rc, rec in ((3, {"code": "MADE_UP", "exit_code": 3}), (3, {"code": "SAFETY_BLOCKED", "exit_code": 2}),
                        (2, {"code": "UI_TIMEOUT", "exit_code": 7}), (1, {"code": "UI_TIMEOUT", "exit_code": 3})):
            with self.subTest(rc=rc, rec=rec):
                self.assertEqual(self.run_with(rc, rec).code, "DRIVER_CRASH")
        self.assertEqual(self.run_with(2, {"code": "SAFETY_BLOCKED", "exit_code": 2}).code, "SAFETY_BLOCKED")

    def test_every_driver_code_is_known_to_the_relay(self):
        import re as _re
        src = "".join(open(os.path.join(HERE, "..", n), encoding="utf-8").read()
                      for n in ("steps.py", "gemini_host.py", "run_deep_research.py"))
        codes = set(_re.findall(r'(?:StepError|HostError)\(\s*"([A-Z_]+)"', src))
        codes |= set(_re.findall(r'Decision\(None, "([A-Z_]+)"', src))
        codes |= set(_re.findall(r'return "([A-Z_]{6,})", ', src))
        codes |= {"UI_LOCATOR_MISS", "UIA_DRIVER_NOT_IMPLEMENTED"} | set(rdr.JOB_LEVEL_CODES)
        self.assertGreater(len(codes), 25)
        self.assertEqual(codes - relay_step.DRIVER_ERROR_CODES, set())


class TouchFailureTests(DriverHarness):
    def test_touch_failure_after_success_keeps_the_step_successful(self):
        with mock.patch.object(self.host, "touch", side_effect=OSError("disk full")):
            rc, out, err, _ = self.plan()
        self.assertEqual(rc, 0, (out, err))
        self.assertIn("Warning: host.json was not updated", out)
        self.assertNotIn(rdr.DRIVER_ERROR_PREFIX, err)
        _, _, payload = blocks(out)
        self.assertEqual(payload["plan"], KO_PLAN)


if __name__ == "__main__":
    unittest.main()

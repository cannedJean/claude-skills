import sandbox  # noqa: F401  (must stay the first import: blocks real-app access)
import contextlib
import copy
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from uitree import FakeTree, Node, PatternNotSupported  # noqa: E402
import locators  # noqa: E402
import privacy  # noqa: E402
import steps  # noqa: E402

BUNDLED = Path(HERE).parent / "locators.json"
FRESH = "https://gemini.google.com/app"
BOUND = "https://gemini.google.com/app/3f9a0c1b2d4e5f60"
KO_PROMPT = "국내 배터리 재활용 시장 동향을 조사해 줘.\n표로 정리해 줘."
EN_PROMPT = "Research the battery recycling market.\nUse a table."
KO_PLAN = "\n".join([
    "조사 계획을 세웠습니다. 수정이 필요하면 알려 주세요.",
    "### 배터리 재활용 시장 조사 계획",
    "(1) 국내 배터리 재활용 업체와 처리 용량을 조사합니다.",
    "(2) 2020년 이후 정책과 규제 변화를 정리합니다.",
    "(3) 해외 주요 시장과 비교합니다.",
    "연구 웹사이트", "결과 분석", "보고서 생성",
])
KO_PLAN2 = "\n".join([
    "조사 계획을 세웠습니다. 수정이 필요하면 알려 주세요.",
    "### 배터리 재활용 시장 조사 계획 (수정)",
    "(1) 국내 배터리 재활용 업체와 처리 용량을 조사합니다.",
    "(2) 해외 주요 시장과 비교합니다.",
    "연구 웹사이트", "결과 분석", "보고서 생성",
])
EMAIL = "test@example.invalid"
SECRET_TITLE = "비밀 대화 제목"


class AutoTree(FakeTree):
    """FakeTree plus time-driven changes: spec `auto` = [{from, to, after_snapshots}].

    Gemini changes the page on its own (URL bind, plan card, countdown expiry). When no
    action-triggered transition is pending, entering a snapshot in `from` arms the same
    `after_snapshots` delay towards `to`; an action fired meanwhile replaces it (last wins).
    """

    def __init__(self, spec):
        super().__init__(spec)
        self._auto = {a["from"]: (a["to"], a["after_snapshots"]) for a in spec.get("auto", ())}
        for frm, (to, _n) in self._auto.items():
            if frm not in self._states or to not in self._states:
                raise ValueError("auto: unknown state %r -> %r" % (frm, to))
        self.seen = []

    def snapshot(self, doc_id):
        if self._pending is None and self.state in self._auto:
            to, delay = self._auto[self.state]
            self._pending = [to, delay]
        node = super().snapshot(doc_id)
        self.seen.append(self.state)
        return node


class Clock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def load(name):
    with open(os.path.join(HERE, "fixtures", name), encoding="utf-8") as fh:
        return json.load(fh)


def cfg(clock=None, **kw):
    clock = clock or Clock()
    return steps.StepConfig(sleep=clock.sleep, clock=clock, **kw)


def locs(override=None):
    return locators.load_locators(BUNDLED, override)


def nodes(spec):
    """Every node dict in every state (documents and desktop)."""
    out = []

    def rec(d):
        out.append(d)
        for c in d.get("children") or ():
            rec(c)

    for st in spec["states"].values():
        for d in st["documents"]:
            rec(d)
        if st.get("desktop"):
            rec(st["desktop"])
    return out


def rename(spec, old, new):
    for d in nodes(spec):
        if d.get("name") == old:
            d["name"] = new


def set_auto(spec, frm, to, after=1):
    spec["auto"] = [a for a in spec["auto"] if a["from"] != frm] + [
        {"from": frm, "to": to, "after_snapshots": after}]


def acts(tree):
    return [a for a in tree.actions if a[0] == "act"]


def run_ko_plan(tree, lc, c, wanted="pro", thinking=None, prompt=KO_PROMPT):
    """Full plan step on a fresh tree: returns (mode_name, url, result)."""
    doc = steps.ensure_fresh_chat(tree, lc, c)
    name = steps.select_mode(tree, lc, c, wanted, thinking)
    steps.enable_deep_research(tree, lc, c)
    steps.enter_prompt(tree, lc, c, doc, prompt)
    url = steps.send_and_bind(tree, lc, c, doc)
    res = steps.wait_plan_and_cancel(tree, lc, c, url, 0)
    return name, url, res


class PlanFlowTests(unittest.TestCase):
    def test_plan_flow_ko_cancels_and_extracts(self):
        tree = AutoTree(load("plan_flow_ko.json"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            name, url, res = run_ko_plan(tree, locs(), cfg())
        self.assertEqual(out.getvalue(), "")
        self.assertIn("Neon", name)
        self.assertEqual(url, BOUND)
        self.assertEqual(res, {"cancel": "cancel_button", "plan_text": KO_PLAN, "cards": 1})
        self.assertEqual(tree.state, "cancelled")
        a = acts(tree)
        self.assertIn(("act", "newchat", "invoke"), a)
        self.assertIn(("act", "mi_neon", "invoke"), a)
        self.assertIn(("act", "pm_dr", "toggle"), a)
        # the countdown is cancelled right after send, nothing in between, start never touched
        i = a.index(("act", "b_send", "invoke"))
        self.assertEqual(a[i + 1:], [("act", "plan1_cancel", "invoke")])
        self.assertNotIn("researching", tree.seen)
        # cancel happened on the first snapshot that showed the card
        self.assertEqual(tree.seen.count("card"), 1)
        sv = [x for x in tree.actions if x[0] == "set_value"]
        self.assertEqual(sv, [("set_value", "prompt", KO_PROMPT)])

    def test_plan_flow_en_synthetic(self):
        tree = AutoTree(load("plan_flow_en_synthetic.json"))
        lc, c = locs(), cfg()
        doc = steps.ensure_fresh_chat(tree, lc, c)
        name = steps.select_mode(tree, lc, c, "flash", "high")
        self.assertIn("Hydrogen", name)
        self.assertEqual(tree.state, "fresh_hydrogen_high")
        steps.enable_deep_research(tree, lc, c)
        steps.enter_prompt(tree, lc, c, doc, EN_PROMPT)
        url = steps.send_and_bind(tree, lc, c, doc)
        res = steps.wait_plan_and_cancel(tree, lc, c, url, 0)
        self.assertEqual(res["cancel"], "cancel_button")
        self.assertEqual(res["cards"], 1)
        self.assertTrue(res["plan_text"].startswith("Here is my research plan."))
        self.assertIn("### Battery recycling market research plan", res["plan_text"])
        self.assertNotIn("Start research", res["plan_text"])
        self.assertIn(("act", "ti_high", "invoke"), tree.actions)
        self.assertEqual(tree.state, "cancelled")

    def test_empty_check_requires_input_anchor(self):
        # input label drifted: send is absent, but that must not be read as "input empty"
        spec = load("plan_flow_ko.json")
        rename(spec, "Gemini 프롬프트 입력", "Gemini 프롬프트 입력란")
        spec["state"] = "dr_on"
        tree = AutoTree(spec)
        doc = steps.bind_conversation(tree, FRESH)
        with self.assertRaises(locators.LocatorMiss) as cm:
            steps.enter_prompt(tree, locs(), cfg(), doc, KO_PROMPT)
        self.assertEqual(cm.exception.payload["locator"], "prompt_input")
        self.assertEqual(tree.actions, [])
        # input present and send present -> not empty
        spec = load("plan_flow_ko.json")
        spec["state"] = "typed"
        tree = AutoTree(spec)
        doc = steps.bind_conversation(tree, FRESH)
        with self.assertRaises(steps.StepError) as cm:
            steps.enter_prompt(tree, locs(), cfg(), doc, KO_PROMPT)
        self.assertEqual(cm.exception.code, "INPUT_NOT_EMPTY")
        self.assertEqual(tree.actions, [])

    def test_readback_mismatch_clears_and_fails(self):
        class Garbling(AutoTree):
            def get_value(self, node_id):
                return "x" + super().get_value(node_id)

        spec = load("plan_flow_ko.json")
        spec["state"] = "dr_on"
        tree = Garbling(spec)
        doc = steps.bind_conversation(tree, FRESH)
        with self.assertRaises(steps.StepError) as cm:
            steps.enter_prompt(tree, locs(), cfg(), doc, KO_PROMPT)
        e = cm.exception
        self.assertEqual(e.code, "PROMPT_READBACK_MISMATCH")
        self.assertEqual(e.details["expected_sha256"], hashlib.sha256(KO_PROMPT.encode("utf-8")).hexdigest())
        self.assertNotEqual(e.details["actual_sha256"], e.details["expected_sha256"])
        self.assertNotIn(KO_PROMPT, json.dumps(e.details, ensure_ascii=False))
        self.assertEqual(tree.actions[-1], ("set_value", "prompt", ""))
        self.assertNotIn(("act", "b_send", "invoke"), tree.actions)

    def test_no_cancel_button_uses_edit_fallback(self):
        spec = load("plan_flow_no_cancel.json")
        spec["transitions"].append({"from": "card", "on": ["act", "plan1_edit", "invoke"], "to": "canned_gen"})
        tree = AutoTree(spec)
        res = steps.wait_plan_and_cancel(tree, locs(), cfg(), BOUND, 0)
        self.assertEqual(res, {"cancel": "edit_fallback", "plan_text": KO_PLAN, "cards": 1})
        self.assertEqual(acts(tree), [("act", "plan1_edit", "invoke")])
        # the canned turn showed stop_response while generating; it was waited out, not stopped
        self.assertIn("canned_gen", tree.seen)
        self.assertEqual(tree.state, "canned")

    def test_edit_after_edit_fallback_reuses_canned_turn(self):
        spec = load("plan_flow_no_cancel.json")
        spec["transitions"].append({"from": "card", "on": ["act", "plan1_edit", "invoke"], "to": "canned_gen"})
        tree = AutoTree(spec)
        lc = locs()
        self.assertEqual(steps.wait_plan_and_cancel(tree, lc, cfg(), BOUND, 0)["cancel"], "edit_fallback")
        res = steps.edit_plan(tree, lc, cfg(), BOUND, "2번 단계를 빼 주세요.",
                              prev_cancel="edit_fallback")
        self.assertEqual(res, {"cancel": "cancel_button", "plan_text": KO_PLAN2, "cards": 2})
        self.assertEqual(acts(tree), [("act", "plan1_edit", "invoke"), ("act", "b_send", "invoke"),
                                      ("act", "plan2_cancel", "invoke")])

    def test_card_missed_research_running_is_stopped_on_timeout(self):
        spec = load("plan_flow_ko.json")
        spec["state"] = "bound"
        set_auto(spec, "bound", "researching", 3)  # card + countdown came and went between two polls
        tree = AutoTree(spec)
        with self.assertRaises(steps.StepError) as cm:
            steps.wait_plan_and_cancel(tree, locs(), cfg(card_timeout_s=3), BOUND, 0)
        e = cm.exception
        self.assertEqual(e.code, "PLAN_TIMEOUT")
        self.assertTrue(e.details["stop_invoked"])
        self.assertTrue(e.details["stopped"])
        self.assertEqual(acts(tree), [("act", "b_stop", "invoke")])
        self.assertEqual(tree.state, "stopped")

    def test_card_first_seen_disabled_is_unreviewed(self):
        spec = load("plan_flow_ko.json")
        spec["state"] = "bound"
        set_auto(spec, "bound", "card_fired", 3)
        tree = AutoTree(spec)
        with self.assertRaises(steps.StepError) as cm:
            steps.wait_plan_and_cancel(tree, locs(), cfg(), BOUND, 0)
        e = cm.exception
        self.assertEqual(e.code, "UNREVIEWED_START")
        self.assertTrue(e.details["stopped"])
        self.assertEqual(acts(tree), [("act", "plan1_cancel", "invoke"), ("act", "b_stop", "invoke")])
        self.assertEqual(tree.state, "stopped")

    def test_locator_miss_with_research_running_stops_first(self):
        spec = load("plan_flow_ko.json")
        rename(spec, "연구 시작", "조사 시작")
        spec["state"] = "bound"
        set_auto(spec, "bound", "card_fired", 3)
        tree = AutoTree(spec)
        with self.assertRaises(locators.LocatorMiss) as cm:
            steps.wait_plan_and_cancel(tree, locs(), cfg(), BOUND, 0)
        self.assertEqual(cm.exception.payload["locator"], "plan_start")
        self.assertTrue(cm.exception.payload["stop_invoked"])
        self.assertEqual(acts(tree), [("act", "plan1_cancel", "invoke"), ("act", "b_stop", "invoke")])

    def test_research_starting_after_cancel_is_stopped(self):
        spec = load("plan_flow_ko.json")
        spec["state"] = "bound"
        spec["auto"].append({"from": "cancelled", "to": "researching", "after_snapshots": 5})
        tree = AutoTree(spec)
        with self.assertRaises(steps.StepError) as cm:
            steps.wait_plan_and_cancel(tree, locs(), cfg(), BOUND, 0)
        e = cm.exception
        self.assertEqual(e.code, "UNREVIEWED_START")
        self.assertEqual(e.details["cancel"], "cancel_button")
        self.assertTrue(e.details["stopped"])
        self.assertEqual(acts(tree), [("act", "plan1_cancel", "invoke"), ("act", "b_stop", "invoke")])

    def test_ineffective_cancel_uses_edit_fallback(self):
        spec = load("plan_flow_ko.json")
        spec["state"] = "bound"
        spec["transitions"] = [t for t in spec["transitions"] if t["on"] != ["act", "plan1_cancel", "invoke"]]
        set_auto(spec, "card", "researching", 10000)
        tree = AutoTree(spec)
        res = steps.wait_plan_and_cancel(tree, locs(), cfg(), BOUND, 0)
        self.assertEqual(res, {"cancel": "edit_fallback", "plan_text": KO_PLAN, "cards": 1})
        self.assertEqual(acts(tree), [("act", "plan1_cancel", "invoke"), ("act", "plan1_edit", "invoke")])
        self.assertEqual(tree.state, "edit_canned")

    def test_ineffective_cancel_and_edit_is_countdown_not_cancelled(self):
        spec = load("plan_flow_ko.json")
        spec["state"] = "bound"
        spec["transitions"] = [t for t in spec["transitions"] if t.get("from") != "card"]
        set_auto(spec, "card", "researching", 10000)
        tree = AutoTree(spec)
        with self.assertRaises(steps.StepError) as cm:
            steps.wait_plan_and_cancel(tree, locs(), cfg(turn_timeout_s=3), BOUND, 0)
        self.assertEqual(cm.exception.code, "COUNTDOWN_NOT_CANCELLED")
        self.assertEqual(cm.exception.details["cancel"], "edit_fallback")
        self.assertEqual(acts(tree), [("act", "plan1_cancel", "invoke"), ("act", "plan1_edit", "invoke")])

    def test_cancel_pattern_error_uses_edit_fallback(self):
        class Refusing(AutoTree):
            def act(self, node_id, pattern):
                if node_id == "plan1_cancel":
                    raise PatternNotSupported("refused")
                return super().act(node_id, pattern)

        spec = load("plan_flow_ko.json")
        spec["state"] = "bound"
        tree = Refusing(spec)
        res = steps.wait_plan_and_cancel(tree, locs(), cfg(), BOUND, 0)
        self.assertEqual(res["cancel"], "edit_fallback")
        self.assertEqual(acts(tree), [("act", "plan1_edit", "invoke")])

    def test_count_mismatch_cancels_every_new_card(self):
        spec = load("plan_flow_edit_two_cards.json")
        spec["state"] = "edit_card"
        for name in ("edit_card", "edit_cancelled", "wrong_card"):
            for d in nodes({"states": {"s": spec["states"][name]}}):
                if d["id"] == "plan1_start":
                    d["enabled"] = True  # two live cards, no research
        tree = AutoTree(spec)
        with self.assertRaises(steps.StepError) as cm:
            steps.wait_plan_and_cancel(tree, locs(), cfg(), BOUND, 0)
        self.assertEqual(cm.exception.code, "PLAN_CARD_COUNT_MISMATCH")
        self.assertEqual(sorted(acts(tree)), [("act", "plan1_cancel", "invoke"), ("act", "plan2_cancel", "invoke")])

    def test_count_mismatch_with_research_is_unreviewed(self):
        spec = load("plan_flow_edit_two_cards.json")
        spec["state"] = "edit_card"  # card 1 has a disabled start: research may have started
        tree = AutoTree(spec)
        with self.assertRaises(steps.StepError) as cm:
            steps.wait_plan_and_cancel(tree, locs(), cfg(), BOUND, 0)
        self.assertEqual(cm.exception.code, "UNREVIEWED_START")
        self.assertEqual(sorted(acts(tree)), [("act", "plan1_cancel", "invoke"), ("act", "plan2_cancel", "invoke")])

    def test_countdown_not_cancelled_stops_running_response(self):
        # after the cancel a new non-card turn generates while the card's start stays enabled
        spec = load("plan_flow_ko.json")
        spec["state"] = "bound"
        spec["auto"] = [a for a in spec["auto"] if a["from"] != "edit_canned_gen"]
        spec["auto"].append({"from": "cancelled", "to": "edit_canned_gen", "after_snapshots": 2})
        spec["transitions"].append({"from": "edit_canned_gen", "on": ["act", "b_stop", "invoke"], "to": "edit_canned"})
        tree = AutoTree(spec)
        with self.assertRaises(steps.StepError) as cm:
            steps.wait_plan_and_cancel(tree, locs(), cfg(turn_timeout_s=3), BOUND, 0)
        e = cm.exception
        self.assertEqual(e.code, "COUNTDOWN_NOT_CANCELLED")
        self.assertTrue(e.details["stop_invoked"])
        self.assertTrue(e.details["stopped"])
        self.assertEqual(acts(tree), [("act", "plan1_cancel", "invoke"), ("act", "plan1_edit", "invoke"),
                                      ("act", "b_stop", "invoke")])

    def test_backend_errors_fall_through_programming_errors_propagate(self):
        class Broken(AutoTree):
            exc = {}

            def act(self, node_id, pattern):
                if node_id in self.exc:
                    raise self.exc[node_id]
                return super().act(node_id, pattern)

        spec = load("plan_flow_ko.json")
        spec["state"] = "bound"
        set_auto(spec, "card", "researching", 10000)  # countdown longer than the watch window
        tree = Broken(copy.deepcopy(spec))
        tree.exc = {"plan1_cancel": OSError("com"), "plan1_edit": PatternNotSupported("no")}
        with self.assertRaises(steps.StepError) as cm:
            steps.wait_plan_and_cancel(tree, locs(), cfg(), BOUND, 0)
        self.assertEqual(cm.exception.code, "COUNTDOWN_NOT_CANCELLED")
        self.assertEqual([x["error"] for x in cm.exception.details["act_errors"]], ["OSError", "PatternNotSupported"])
        tree = Broken(copy.deepcopy(spec))
        tree.exc = {"plan1_cancel": LookupError("gone")}
        self.assertEqual(steps.wait_plan_and_cancel(tree, locs(), cfg(), BOUND, 0)["cancel"], "edit_fallback")
        tree = Broken(copy.deepcopy(spec))
        tree.exc = {"plan1_cancel": TypeError("bug")}
        with self.assertRaises(TypeError):
            steps.wait_plan_and_cancel(tree, locs(), cfg(), BOUND, 0)
        self.assertEqual(steps.BackendError, (PatternNotSupported, LookupError, OSError))
        spec2 = load("plan_flow_ko.json")
        spec2["state"] = "fresh_neon_std"
        tree = Broken(spec2)
        tree.exc = {"b_tools": OSError("com")}
        with self.assertRaises(steps.StepError) as cm:
            steps.enable_deep_research(tree, locs(), cfg())
        self.assertEqual(cm.exception.code, "UI_ACTION_FAILED")
        self.assertEqual(cm.exception.details["error"], "OSError")

    def test_card_losing_start_without_cancel_is_unreviewed(self):
        spec = load("plan_flow_ko.json")
        st = copy.deepcopy(spec["states"]["card_fired"])
        for d in nodes({"states": {"s": st}}):
            if d["id"] == "c1_bar":
                d["children"] = [c for c in d["children"] if c["id"] not in ("plan1_start", "plan1_cancel")]
        spec["states"]["card_lost"] = st
        spec["transitions"].append({"from": "card_lost", "on": ["act", "b_stop", "invoke"], "to": "stopped"})
        spec["state"] = "bound"
        set_auto(spec, "bound", "card_lost", 3)
        tree = AutoTree(spec)
        with self.assertRaises(steps.StepError) as cm:
            steps.wait_plan_and_cancel(tree, locs(), cfg(), BOUND, 0)
        self.assertEqual(cm.exception.code, "UNREVIEWED_START")
        self.assertEqual(acts(tree), [("act", "b_stop", "invoke")])
        self.assertLess(len(tree.seen), 20)  # detected in the poll loop, not at PLAN_TIMEOUT

    def test_card_count_drop_with_stop_is_unreviewed(self):
        spec = load("plan_flow_no_cancel.json")
        spec["state"] = "researching"  # the earlier card is gone, a response is running
        tree = AutoTree(spec)
        with self.assertRaises(steps.StepError) as cm:
            steps.wait_plan_and_cancel(tree, locs(), cfg(), BOUND, 1)
        self.assertEqual(cm.exception.code, "UNREVIEWED_START")
        self.assertEqual(acts(tree), [("act", "b_stop", "invoke")])

    def test_edit_invokes_plan_edit_unless_previous_was_edit_fallback(self):
        spec = load("plan_flow_no_cancel.json")
        spec["state"] = "canned"  # a non-card turn follows the card, but no edit_fallback result is known
        tree = AutoTree(spec)
        with self.assertRaises(steps.StepError) as cm:
            steps.edit_plan(tree, locs(), cfg(turn_timeout_s=2), BOUND, "2번 단계를 빼 주세요.")
        self.assertEqual(cm.exception.code, "UI_TIMEOUT")
        self.assertEqual(acts(tree), [("act", "plan1_edit", "invoke")])

    def test_stop_wait_reports_unstopped(self):
        self.assertEqual(steps.StepConfig().stop_timeout_s, 300)
        spec = load("plan_flow_no_cancel.json")
        spec["transitions"] = [t for t in spec["transitions"] if t["on"] != ["act", "b_stop", "invoke"]]
        clock = Clock()
        tree = AutoTree(spec)
        with self.assertRaises(steps.StepError) as cm:
            steps.wait_plan_and_cancel(tree, locs(), cfg(clock, stop_timeout_s=7), BOUND, 0)
        self.assertEqual(cm.exception.code, "UNREVIEWED_START")
        self.assertTrue(cm.exception.details["stop_invoked"])
        self.assertFalse(cm.exception.details["stopped"])
        self.assertGreaterEqual(clock.t, 7)

    def test_edit_canned_turn_timeout_stops_generation(self):
        spec = load("plan_flow_edit_two_cards.json")
        spec["auto"] = [a for a in spec["auto"] if a["from"] != "canned_gen"]
        spec["transitions"].append({"from": "canned_gen", "on": ["act", "b_stop", "invoke"], "to": "canned"})
        tree = AutoTree(spec)
        with self.assertRaises(steps.StepError) as cm:
            steps.edit_plan(tree, locs(), cfg(turn_timeout_s=3), BOUND, "2번 단계를 빼 주세요.")
        e = cm.exception
        self.assertEqual(e.code, "UI_TIMEOUT")
        self.assertTrue(e.details["stop_invoked"])
        self.assertTrue(e.details["stopped"])
        self.assertEqual(acts(tree), [("act", "plan1_edit", "invoke"), ("act", "b_stop", "invoke")])

    def test_unreviewed_start_is_stopped_and_reported(self):
        tree = AutoTree(load("plan_flow_no_cancel.json"))
        with self.assertRaises(steps.StepError) as cm:
            steps.wait_plan_and_cancel(tree, locs(), cfg(), BOUND, 0)
        e = cm.exception
        self.assertEqual(e.code, "UNREVIEWED_START")
        self.assertEqual(e.details["cancel"], "edit_fallback")
        self.assertTrue(e.details["stopped"])
        self.assertEqual(acts(tree), [("act", "plan1_edit", "invoke"), ("act", "b_stop", "invoke")])
        self.assertEqual(tree.state, "stopped")

    def test_cancel_not_verified_reports_countdown_not_cancelled(self):
        # cancel invoked, but the start button turns disabled without any research progress
        spec = load("plan_flow_ko.json")
        for d in nodes({"states": {"c": spec["states"]["cancelled"]}}):
            if d["id"] == "plan1_start":
                d["enabled"] = False
        spec["state"] = "bound"
        tree = AutoTree(spec)
        with self.assertRaises(steps.StepError) as cm:
            steps.wait_plan_and_cancel(tree, locs(), cfg(turn_timeout_s=3), BOUND, 0)
        self.assertEqual(cm.exception.code, "COUNTDOWN_NOT_CANCELLED")
        # the failed verification after a successful cancel still tries the second stop
        self.assertEqual(acts(tree), [("act", "plan1_cancel", "invoke"), ("act", "plan1_edit", "invoke")])

    def test_edit_targets_newest_card_only(self):
        tree = AutoTree(load("plan_flow_edit_two_cards.json"))
        res = steps.edit_plan(tree, locs(), cfg(), BOUND, "2번 단계를 빼 주세요.")
        self.assertEqual(res, {"cancel": "cancel_button", "plan_text": KO_PLAN2, "cards": 2})
        self.assertEqual(acts(tree), [("act", "plan1_edit", "invoke"), ("act", "b_send", "invoke"),
                                      ("act", "plan2_cancel", "invoke")])
        self.assertIn(("set_value", "prompt", "2번 단계를 빼 주세요."), tree.actions)
        self.assertEqual(tree.state, "edit_cancelled")
        # the canned turn finished before the feedback was typed
        i = tree.actions.index(("set_value", "prompt", "2번 단계를 빼 주세요."))
        self.assertIn("canned", tree.seen)
        self.assertEqual(tree.seen[-1], "edit_cancelled")
        self.assertGreater(i, 0)

    def test_model_unique_match_and_ambiguous(self):
        spec = load("plan_flow_ko.json")
        spec["state"] = "fresh_hydrogen_std"
        tree = AutoTree(spec)
        lc = locs()
        with self.assertRaises(steps.StepError) as cm:
            steps.select_mode(tree, lc, cfg(), "4", None)
        self.assertEqual(cm.exception.code, "MODEL_AMBIGUOUS")
        self.assertEqual(len(cm.exception.details["matches"]), 3)
        self.assertEqual(tree.state, "fresh_hydrogen_std")  # menu closed again
        name = steps.select_mode(tree, lc, cfg(), "PRO", None)
        self.assertEqual(name, "모드 선택 도구 열기, 현재 Neon 모드 사용 중")
        self.assertEqual(tree.state, "fresh_neon_std")
        names = ["자동", "일상생활 지원 4 Hydrogen", "선택됨 심층 분석 4 Neon", "코드 및 복잡한 작업 4 카본"]
        aliases = lc["model_aliases"]
        self.assertEqual(steps.match_names(names, "argon", aliases), ["코드 및 복잡한 작업 4 카본"])
        self.assertEqual(steps.match_names(names, "carbon", aliases), ["코드 및 복잡한 작업 4 카본"])
        self.assertEqual(steps.match_names(names, "neon", aliases), ["선택됨 심층 분석 4 Neon"])
        self.assertEqual(steps.match_names(names, "flash", aliases), ["일상생활 지원 4 Hydrogen"])
        self.assertEqual(steps.match_names(names, "  hydrogen ", aliases), ["일상생활 지원 4 Hydrogen"])
        self.assertEqual(len(steps.match_names(names, "4", aliases)), 3)

    def test_model_not_available_lists_menu(self):
        spec = load("plan_flow_ko.json")
        spec["state"] = "fresh_hydrogen_std"
        tree = AutoTree(spec)
        with self.assertRaises(steps.StepError) as cm:
            steps.select_mode(tree, locs(), cfg(), "Ultra", None)
        e = cm.exception
        self.assertEqual(e.code, "MODEL_NOT_AVAILABLE")
        self.assertEqual(e.details["available"], ["자동", "선택됨 일상생활 지원 4 Hydrogen", "심층 분석 4 Neon",
                                                  "코드 및 복잡한 작업 4 카본"])
        self.assertEqual(tree.state, "fresh_hydrogen_std")
        self.assertNotIn(("act", "mi_neon", "invoke"), tree.actions)

    def test_gemini_error_response_code(self):
        spec = load("plan_flow_ko.json")
        spec["state"] = "bound"
        set_auto(spec, "bound", "error", 2)
        tree = AutoTree(spec)
        with self.assertRaises(steps.StepError) as cm:
            steps.wait_plan_and_cancel(tree, locs(), cfg(), BOUND, 0)
        self.assertEqual(cm.exception.code, "GEMINI_ERROR_RESPONSE")
        self.assertEqual(tree.actions, [])  # never retried

    def test_safety_phrase_blocks(self):
        spec = load("plan_flow_ko.json")
        spec["state"] = "bound"
        set_auto(spec, "bound", "safety", 2)
        tree = AutoTree(spec)
        with self.assertRaises(steps.StepError) as cm:
            steps.wait_plan_and_cancel(tree, locs(), cfg(), BOUND, 0)
        self.assertEqual(cm.exception.code, "SAFETY_BLOCKED")
        self.assertEqual(tree.actions, [])

    def test_plan_timeout(self):
        spec = load("plan_flow_ko.json")
        spec["state"] = "bound"
        spec["auto"] = []
        tree = AutoTree(spec)
        with self.assertRaises(steps.StepError) as cm:
            steps.wait_plan_and_cancel(tree, locs(), cfg(card_timeout_s=3), BOUND, 0)
        self.assertEqual(cm.exception.code, "PLAN_TIMEOUT")

    def test_label_drift_raises_locator_miss_and_override_recovers(self):
        spec = load("plan_flow_ko.json")
        rename(spec, "연구 시작", "조사 시작")
        spec["state"] = "bound"
        tree = AutoTree(copy.deepcopy(spec))
        with self.assertRaises(locators.LocatorMiss) as cm:
            steps.wait_plan_and_cancel(tree, locs(), cfg(card_timeout_s=30), BOUND, 0)
        p = cm.exception.payload
        self.assertEqual(p["locator"], "plan_start")
        self.assertIn("조사 시작", [c["name"] for c in p["candidates"]])
        # fail closed: the countdown of the unrecognised card was still cancelled
        self.assertEqual(acts(tree), [("act", "plan1_cancel", "invoke")])
        with tempfile.TemporaryDirectory() as tmp:
            ov = Path(tmp) / "locators.local.json"
            ov.write_text(json.dumps(p["override_snippet"], ensure_ascii=False), encoding="utf-8")
            tree = AutoTree(copy.deepcopy(spec))
            res = steps.wait_plan_and_cancel(tree, locs(ov), cfg(), BOUND, 0)
        self.assertEqual(res, {"cancel": "cancel_button", "plan_text": KO_PLAN, "cards": 1})

    def test_no_account_string_in_any_output(self):
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
        outputs = []
        tree = AutoTree(copy.deepcopy(spec))
        name, url, res = run_ko_plan(tree, locs(), cfg())
        outputs += [name, url, res]
        self.assertIn("<redacted:", res["plan_text"])
        # error paths: model list, label drift hint, a raw StepError carrying an address
        spec2 = copy.deepcopy(spec)
        spec2["state"] = "fresh_hydrogen_std"
        with self.assertRaises(steps.StepError) as cm:
            steps.select_mode(AutoTree(spec2), locs(), cfg(), "Ultra", None)
        outputs.append(cm.exception.details)
        spec3 = copy.deepcopy(spec)
        rename(spec3, "연구 시작", "조사 시작")
        spec3["state"] = "bound"
        with self.assertRaises(locators.LocatorMiss) as cm2:
            steps.wait_plan_and_cancel(AutoTree(spec3), locs(), cfg(card_timeout_s=30), BOUND, 0)
        outputs.append(cm2.exception.payload)
        outputs.append(steps.StepError("CONVERSATION_MISMATCH", {"expected": "x " + EMAIL}).details)
        with self.assertRaises(steps.StepError) as cm3:
            steps.bind_conversation(AutoTree(copy.deepcopy(spec)), BOUND + "?u=" + EMAIL)
        outputs.append(cm3.exception.details)
        blob = json.dumps(outputs, ensure_ascii=False)
        self.assertNotIn(EMAIL, blob)
        self.assertNotIn(SECRET_TITLE, blob)
        self.assertNotIn("example.invalid", blob)


class UnitTests(unittest.TestCase):
    def test_every_error_code_is_documented(self):
        src = Path(steps.__file__).read_text(encoding="utf-8")
        codes = set(re.findall(r'StepError\(\s*"([A-Z_]+)"', src))
        self.assertGreaterEqual(len(codes), 15)
        doc = steps.__doc__
        for code in sorted(codes):
            with self.subTest(code=code):
                self.assertRegex(doc, r"(?m)^\s*%s\s+\S" % code)
        for fn in (steps.bind_conversation, steps.ensure_fresh_chat):
            self.assertIn("never serialize", fn.__doc__)

    def test_bind_conversation_exact_match(self):
        spec = load("plan_flow_ko.json")
        spec["state"] = "cancelled"
        tree = AutoTree(spec)
        self.assertEqual(steps.bind_conversation(tree, BOUND).id, "doc")
        for bad in (FRESH, BOUND + "0", BOUND[:-1], BOUND + "/"):
            with self.subTest(url=bad):
                with self.assertRaises(steps.StepError) as cm:
                    steps.bind_conversation(tree, bad)
                self.assertEqual(cm.exception.code, "CONVERSATION_MISMATCH")

    def test_wait_rejects_url_change(self):
        spec = load("plan_flow_ko.json")
        spec["state"] = "bound"
        for d in spec["states"]["card"]["documents"]:
            if d["id"] == "doc":
                d["value"] = "https://gemini.google.com/app/ffffffffffffffff"
        tree = AutoTree(spec)
        with self.assertRaises(steps.StepError) as cm:
            steps.wait_plan_and_cancel(tree, locs(), cfg(), BOUND, 0)
        self.assertEqual(cm.exception.code, "CONVERSATION_MISMATCH")
        self.assertEqual(tree.actions, [])

    def test_send_requires_fresh_chat(self):
        spec = load("plan_flow_ko.json")
        spec["state"] = "cancelled"
        tree = AutoTree(spec)
        doc = steps.bind_conversation(tree, BOUND)
        with self.assertRaises(steps.StepError) as cm:
            steps.send_and_bind(tree, locs(), cfg(), doc)
        self.assertEqual(cm.exception.code, "CONVERSATION_MISMATCH")
        self.assertEqual(tree.actions, [])

    def test_deep_research_skipped_when_on_and_fails_closed(self):
        spec = load("plan_flow_ko.json")
        spec["state"] = "dr_on"
        tree = AutoTree(spec)
        steps.enable_deep_research(tree, locs(), cfg())
        self.assertEqual(tree.actions, [])
        spec = load("plan_flow_ko.json")
        spec["state"] = "fresh_neon_std"
        spec["transitions"] = [t for t in spec["transitions"] if t["on"] != ["act", "pm_dr", "toggle"]]
        tree = AutoTree(spec)
        with self.assertRaises(steps.StepError) as cm:
            steps.enable_deep_research(tree, locs(), cfg())
        self.assertEqual(cm.exception.code, "DEEP_RESEARCH_TOGGLE_NOT_FOUND")

    def test_ensure_fresh_chat_noop_and_sidebar_restore(self):
        spec = load("plan_flow_ko.json")
        spec["state"] = "fresh_hydrogen_std"
        tree = AutoTree(spec)
        self.assertEqual(steps.ensure_fresh_chat(tree, locs(), cfg()).value, FRESH)
        self.assertEqual(tree.actions, [])
        # sidebar closed: the new-chat link appears only after opening it; it is closed again after
        spec = load("plan_flow_ko.json")
        closed = copy.deepcopy(spec["states"]["start"])
        fresh_closed = copy.deepcopy(spec["states"]["fresh_hydrogen_std"])
        for st in (closed, fresh_closed):
            for d in nodes({"states": {"s": st}}):
                if d["id"] == "nav1":
                    d["children"] = [{"id": "sb_open", "ct": "Button", "name": "사이드바 열기", "patterns": ["invoke"]}]
        spec["states"]["start_closed"] = closed
        spec["states"]["fresh_closed"] = fresh_closed
        spec["state"] = "start_closed"
        spec["transitions"] += [
            {"from": "start_closed", "on": ["act", "sb_open", "invoke"], "to": "start"},
            {"from": "fresh_hydrogen_std", "on": ["act", "sb_close", "invoke"], "to": "fresh_closed"},
        ]
        tree = AutoTree(spec)
        doc = steps.ensure_fresh_chat(tree, locs(), cfg())
        self.assertEqual(doc.value, FRESH)
        self.assertEqual(acts(tree), [("act", "sb_open", "invoke"), ("act", "newchat", "invoke"),
                                      ("act", "sb_close", "invoke")])
        self.assertEqual(tree.state, "fresh_closed")

    def test_poll_interval_tracks_snapshot_duration(self):
        clock = Clock()

        class Slow(AutoTree):
            def snapshot(self, doc_id):
                clock.t += 0.4
                return super().snapshot(doc_id)

        spec = load("plan_flow_ko.json")
        spec["state"] = "bound"
        tree = Slow(spec)
        steps.wait_plan_and_cancel(tree, locs(), cfg(clock, poll_min_s=0.1, cancel_verify_s=2), BOUND, 0)
        self.assertTrue(clock.sleeps)
        for s in clock.sleeps:
            self.assertAlmostEqual(s, 0.8)
        clock2 = Clock()
        spec = load("plan_flow_ko.json")
        spec["state"] = "bound"
        steps.wait_plan_and_cancel(AutoTree(spec), locs(), cfg(clock2, poll_min_s=0.25), BOUND, 0)
        self.assertTrue(clock2.sleeps)
        self.assertTrue(all(s == 0.25 for s in clock2.sleeps))

    def test_extract_plan_text_rules(self):
        card = Node.from_dict({"id": "t", "ct": "Group", "children": [
            {"id": "h", "ct": "Text", "name": "제목", "heading": 2},
            {"id": "h2", "ct": "Text", "name": " 제목 "},
            {"id": "a", "ct": "Text", "name": "단계 1"},
            {"id": "b", "ct": "Button", "name": "연구 시작", "children": [{"id": "bt", "ct": "Text", "name": "연구 시작"}]},
            {"id": "hid", "ct": "Text", "name": "허용 밖"},
            {"id": "e", "ct": "Text", "name": ""},
            {"id": "g", "ct": "Group", "name": "그룹", "heading": 4, "children": [
                {"id": "c", "ct": "Text", "name": "단계 1"}]},
            {"id": "m", "ct": "Text", "name": "메일 " + EMAIL},
        ]})
        allowed = {"t", "h", "h2", "a", "b", "bt", "e", "g", "c", "m"}
        self.assertEqual(steps.extract_plan_text(card, allowed),
                         "## 제목\n단계 1\n#### 그룹\n단계 1\n<redacted:account len=%d>" % len("메일 " + EMAIL))
        self.assertEqual(steps.extract_plan_text(card, set()), "")


if __name__ == "__main__":
    unittest.main()

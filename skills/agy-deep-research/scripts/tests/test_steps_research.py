import sandbox  # noqa: F401  (must stay the first import: blocks real-app access)
import copy
import json
import os
import re
import sys
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from uitree import FakeTree  # noqa: E402
import locators  # noqa: E402
import privacy  # noqa: E402
import relay_step  # noqa: E402
import report_md  # noqa: E402
import steps  # noqa: E402

BUNDLED = Path(HERE).parent / "locators.json"
BOUND = "https://gemini.google.com/app/3f9a0c1b2d4e5f60"
OTHER = "https://gemini.google.com/app/9e8d7c6b5a493827"
EMAIL = "test@example.invalid"
START = ("act", "plan1_start", "invoke")
# research steps of the fixture timeline, in order (spec 23.1 live shape)
STEPS = ["r00", "r01", "r02", "r03", "r04", "r05", "r06", "r07", "r08", "r09", "r10", "r11"]


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        assert s > 0
        self.t += s


class TimelineTree(FakeTree):
    """FakeTree plus clock-driven changes: spec `timeline` = [{from, to, after_s}].

    Once `from` has been the current state for `after_s` seconds of the injected clock, the next
    documents()/snapshot() shows `to` (Gemini changing the page on its own during the research)."""

    def __init__(self, spec, clock):
        self.clock = clock
        self.entered = clock()
        super().__init__(spec)
        self._tl = {t["from"]: (t["to"], t["after_s"]) for t in spec.get("timeline", ())}
        for frm, (to, _s) in self._tl.items():
            if frm not in self._states or to not in self._states:
                raise ValueError("timeline: unknown state %r -> %r" % (frm, to))
        self.snaps = 0

    def _enter(self, state):
        super()._enter(state)
        self.entered = self.clock()

    def jump(self, state):
        self._pending = None
        self._enter(state)

    def _advance(self):
        while self.state in self._tl and self.clock() - self.entered >= self._tl[self.state][1]:
            to, after = self._tl[self.state]
            at = self.entered + after
            self._enter(to)
            self.entered = at

    def documents(self):
        self._advance()
        if self._values:
            return super().documents()
        cache = self.__dict__.setdefault("_dcache", {})
        if self.state not in cache:
            cache[self.state] = super().documents()
        return list(cache[self.state])

    def snapshot(self, doc_id):
        self._advance()
        self.snaps += 1
        if self._pending is not None or self._values:
            return super().snapshot(doc_id)
        # hours of 5 s polls: reuse one copy per (state, document); the steps never mutate nodes
        cache = self.__dict__.setdefault("_cache", {})
        key = (self.state, doc_id)
        if key not in cache:
            cache[key] = super().snapshot(doc_id)
        return cache[key]


class FlakyTree(TimelineTree):
    """Raises `errors` (a list of exception instances or None) from snapshot(), one per call, then works."""

    def __init__(self, spec, clock, errors, after_snaps=0):
        super().__init__(spec, clock)
        self.errors = list(errors)
        self.after = after_snaps

    def snapshot(self, doc_id):
        if self.snaps >= self.after and self.errors:
            exc = self.errors.pop(0)
            self.snaps += 1
            if exc is not None:
                raise exc
        return super().snapshot(doc_id)


class Recover:
    """Injected §11-12 recovery fake: records calls (clock time, url), then runs `effect`."""

    def __init__(self, clock, effect=None, result=True):
        self.clock, self.effect, self.result = clock, effect, result
        self.calls = []

    def __call__(self, url):
        self.calls.append((self.clock(), url))
        if self.effect is not None:
            self.effect()
        return self.result


def load(name):
    with open(os.path.join(HERE, "fixtures", name), encoding="utf-8") as fh:
        return json.load(fh)


def locs():
    return locators.load_locators(BUNDLED, None)


def cfg(clock, **kw):
    return steps.StepConfig(sleep=clock.sleep, clock=clock, **kw)


def walk(d):
    yield d
    for c in d.get("children") or ():
        yield from walk(c)


def doc_of(spec, state, doc_id="doc"):
    return next(d for d in spec["states"][state]["documents"] if d["id"] == doc_id)


def node(spec, state, nid):
    for root in spec["states"][state]["documents"]:
        for d in walk(root):
            if d["id"] == nid:
                return d
    raise KeyError(nid)


def remove(spec, nid, states=None):
    for name, st in spec["states"].items():
        if states is not None and name not in states:
            continue
        for root in st["documents"]:
            for d in walk(root):
                if "children" in d:
                    d["children"] = [c for c in d["children"] if c["id"] != nid]


def set_timeline(spec, frm, to=None, after_s=None):
    spec["timeline"] = [t for t in spec["timeline"] if t["from"] != frm]
    if to is not None:
        spec["timeline"].append({"from": frm, "to": to, "after_s": after_s})


def acts(tree):
    return [a for a in tree.actions if a[0] == "act"]


def approve_and_wait(spec, clock=None, recover=None, lines=None, **kw):
    clock = clock or Clock()
    tree = TimelineTree(spec, clock)
    c = cfg(clock, **kw)
    lc = locs()
    res = steps.approve(tree, lc, c, BOUND)
    out = lines if lines is not None else []
    wait = steps.wait_research(tree, lc, c, BOUND, out.append, recover)
    return tree, res, wait, out, clock


# -- locators -----------------------------------------------------------------
class ResearchLocatorTests(unittest.TestCase):
    NEW = ("research_status", "research_thought_title", "report_done_phrase", "report_share_export", "report_toc",
           "report_sources_used")

    def test_new_locators_have_ko_and_en_names(self):
        lc = locs()["locators"]
        for key in self.NEW:
            self.assertIn(key, lc)
        flat = " ".join(n for k in self.NEW for n in lc[k]["names"])
        for label in ("공유 및 내보내기", "Share & export", "목차 메뉴", "Table of contents", "보고서에서 사용된 소스",
                      "Sources used in the report"):
            self.assertIn(label, flat)
        self.assertIn("report_panel", locs()["scopes"])

    def test_status_and_done_phrases_match_live_texts_ko_and_en(self):
        lc = locs()["locators"]
        for text in ("조사 시작 중...", "웹사이트 37개 조사 중...", "웹사이트 1,024개 조사 중...", "Starting research...",
                     "Researching 37 websites..."):
            self.assertTrue(locators.match_name(text, lc["research_status"]["names"]), text)
        for text in ("완료되는 대로 알려 드릴게요.", "무엇을 조사하고 싶으신가요?", "SQL 표준 연혁 조사 계획 수립"):
            self.assertFalse(locators.match_name(text, lc["research_status"]["names"]), text)
        for text in ("I've completed your research.", "I’ve completed your research.", "연구를 완료했습니다.",
                     "조사를 완료했습니다."):
            self.assertTrue(locators.match_name(text, lc["report_done_phrase"]["names"]), text)
        self.assertFalse(locators.match_name("그 주제에 대한 연구 계획이에요.", lc["report_done_phrase"]["names"]))

    def test_report_md_labels_come_from_locators_json(self):
        lc = locs()["locators"]
        names = {n for k in ("report_share_export", "report_toc", "report_sources_used") for n in lc[k]["names"]}
        self.assertEqual(set(report_md.REPORT_CONTROL_LABELS), names)
        self.assertTrue({"공유 및 내보내기", "Share & export", "Share and export", "목차 메뉴", "Table of contents",
                         "보고서에서 사용된 소스", "Sources used in the report"} <= names)

    def _ctx(self, spec, state):
        spec["state"] = state
        tree = FakeTree(spec)
        doc = tree.snapshot("doc")
        return locators.LocatorContext(tree, doc, locs(), privacy.allowed_ids(doc))

    def test_report_controls_resolve_only_inside_the_report_panel(self):
        for fixture in ("research_flow_ko.json", "research_flow_en_synthetic.json"):
            with self.subTest(fixture=fixture):
                ctx = self._ctx(load(fixture), "complete")
                for key in ("report_share_export", "report_toc", "report_sources_used"):
                    self.assertEqual(len(locators.resolve(key, ctx)), 1, key)
                ctx = self._ctx(load(fixture), "r05")
                for key in ("report_share_export", "report_toc", "report_sources_used"):
                    self.assertEqual(locators.resolve(key, ctx), [], key)

    def test_report_controls_in_the_chat_column_do_not_count(self):
        spec = load("research_flow_ko.json")
        remove(spec, "report_panel", ["complete"])
        turn = node(spec, "complete", "turn_r2")   # a chip-like copy of the controls inside the chat turn
        turn["children"] += [{"id": "fake_toc", "ct": "Button", "name": "목차 메뉴", "patterns": ["invoke"]},
                             {"id": "fake_h1", "ct": "Text", "role": "heading", "heading": 1, "name": "가짜 보고서"}]
        ctx = self._ctx(spec, "complete")
        self.assertEqual(locators.resolve("report_toc", ctx), [])


# -- approve ------------------------------------------------------------------
class ApproveTests(unittest.TestCase):
    def test_approve_invokes_the_newest_start_once_and_sees_progress(self):
        for fixture in ("research_flow_ko.json", "research_flow_en_synthetic.json"):
            with self.subTest(fixture=fixture):
                clock = Clock()
                tree = TimelineTree(load(fixture), clock)
                res = steps.approve(tree, locs(), cfg(clock), BOUND)
                self.assertIsInstance(res, steps.ApproveResult)
                self.assertTrue(res.started)
                self.assertEqual(res.signal, "stop_response")
                self.assertEqual(res.cards, 1)
                self.assertEqual(acts(tree), [START])

    def test_before_invoke_runs_before_the_act(self):
        clock = Clock()
        tree = TimelineTree(load("research_flow_ko.json"), clock)
        seen = []
        steps.approve(tree, locs(), cfg(clock), BOUND, before_invoke=lambda: seen.append(list(acts(tree))))
        self.assertEqual(seen, [[]])
        self.assertEqual(acts(tree), [START])

    def test_approve_twice_is_refused_and_never_invokes_again(self):
        clock = Clock()
        tree = TimelineTree(load("research_flow_ko.json"), clock)
        steps.approve(tree, locs(), cfg(clock), BOUND)
        for _ in range(3):
            clock.t += 100
            with self.assertRaises(steps.StepError) as cm:
                steps.approve(tree, locs(), cfg(clock), BOUND)
            self.assertEqual(cm.exception.code, "ALREADY_STARTED")
        self.assertEqual(acts(tree), [START])

    def test_research_running_with_start_still_enabled_is_already_started(self):
        for state in ("r00", "r05", "r11", "complete"):
            with self.subTest(state=state):
                spec = load("research_flow_ko.json")
                spec["state"] = state
                self.assertTrue(node(spec, state, "plan1_start").get("enabled", True))
                clock = Clock()
                tree = TimelineTree(spec, clock)
                with self.assertRaises(steps.StepError) as cm:
                    steps.approve(tree, locs(), cfg(clock), BOUND)
                self.assertEqual(cm.exception.code, "ALREADY_STARTED")
                self.assertEqual(acts(tree), [])

    def test_frozen_research_without_stop_button_is_already_started(self):
        spec = load("research_flow_ko.json")
        spec["state"] = "r05"
        remove(spec, "b_stop")
        clock = Clock()
        tree = TimelineTree(spec, clock)
        with self.assertRaises(steps.StepError) as cm:
            steps.approve(tree, locs(), cfg(clock), BOUND)
        self.assertEqual(cm.exception.code, "ALREADY_STARTED")
        self.assertEqual(cm.exception.details["signal"], "research_status")
        self.assertEqual(acts(tree), [])

    def _two_cards(self, newest_start=True, newest_enabled=False):
        spec = load("research_flow_ko.json")
        conv = node(spec, "card", "conv")
        c2 = copy.deepcopy(node(spec, "card", "turn_c1"))
        for d in walk(c2):
            d["id"] = d["id"].replace("c1", "c2").replace("plan1", "plan2")
        c2["automation_id"] = "model-response-message-content-c2"
        bar = next(d for d in walk(c2) if d["id"] == "c2_bar")
        if not newest_start:
            bar["children"] = [b for b in bar["children"] if b["id"] != "plan2_start"]
        else:
            next(b for b in bar["children"] if b["id"] == "plan2_start")["enabled"] = newest_enabled
        conv["children"].append(c2)
        return spec

    def test_stale_card_is_refused(self):
        for kw in ({"newest_enabled": False}, {"newest_start": False}):
            with self.subTest(**kw):
                clock = Clock()
                tree = TimelineTree(self._two_cards(**kw), clock)
                with self.assertRaises(steps.StepError) as cm:
                    steps.approve(tree, locs(), cfg(clock), BOUND)
                self.assertEqual(cm.exception.code, "PLAN_CARD_STALE")
                self.assertEqual(acts(tree), [])

    def test_newest_of_two_enabled_cards_is_the_one_invoked(self):
        spec = self._two_cards(newest_enabled=True)
        spec["transitions"].append({"from": "card", "on": ["act", "plan2_start", "invoke"], "to": "r00",
                                    "after_snapshots": 1})
        clock = Clock()
        tree = TimelineTree(spec, clock)
        res = steps.approve(tree, locs(), cfg(clock), BOUND, expect_cards=2)
        self.assertEqual(res.cards, 2)
        self.assertEqual(acts(tree), [("act", "plan2_start", "invoke")])

    def test_card_count_other_than_the_reviewed_one_is_refused(self):
        clock = Clock()
        tree = TimelineTree(load("research_flow_ko.json"), clock)
        with self.assertRaises(steps.StepError) as cm:
            steps.approve(tree, locs(), cfg(clock), BOUND, expect_cards=2)
        self.assertEqual(cm.exception.code, "PLAN_CARD_STALE")
        self.assertEqual((cm.exception.details["expected"], cm.exception.details["found"]), (2, 1))
        self.assertEqual(acts(tree), [])

    def test_no_progress_signal_is_research_not_started_after_one_invoke(self):
        spec = load("research_flow_ko.json")
        spec["transitions"] = []
        clock = Clock()
        tree = TimelineTree(spec, clock)
        with self.assertRaises(steps.StepError) as cm:
            steps.approve(tree, locs(), cfg(clock, approve_verify_s=45), BOUND)
        self.assertEqual(cm.exception.code, "RESEARCH_NOT_STARTED")
        self.assertGreaterEqual(clock.t, 45)
        self.assertEqual(acts(tree), [START])

    def test_wrong_conversation_is_never_acted_on(self):
        clock = Clock()
        tree = TimelineTree(load("research_flow_ko.json"), clock)
        with self.assertRaises(steps.StepError) as cm:
            steps.approve(tree, locs(), cfg(clock), OTHER)
        self.assertEqual(cm.exception.code, "CONVERSATION_MISMATCH")
        self.assertEqual(acts(tree), [])

    def test_url_change_after_the_invoke_is_a_mismatch(self):
        spec = load("research_flow_ko.json")
        doc_of(spec, "r00")["value"] = OTHER
        clock = Clock()
        tree = TimelineTree(spec, clock)
        with self.assertRaises(steps.StepError) as cm:
            steps.approve(tree, locs(), cfg(clock), BOUND)
        self.assertEqual(cm.exception.code, "CONVERSATION_MISMATCH")
        self.assertEqual(acts(tree), [START])

    def test_no_card_is_no_plan_card(self):
        spec = load("research_flow_ko.json")
        remove(spec, "turn_c1")
        clock = Clock()
        tree = TimelineTree(spec, clock)
        with self.assertRaises(steps.StepError) as cm:
            steps.approve(tree, locs(), cfg(clock), BOUND)
        self.assertEqual(cm.exception.code, "NO_PLAN_CARD")

    def test_failed_invoke_is_reported_and_not_retried(self):
        class FailingAct(TimelineTree):
            attempts = 0

            def act(self, node_id, pattern):
                FailingAct.attempts += 1
                raise OSError("COM error")

        clock = Clock()
        tree = FailingAct(load("research_flow_ko.json"), clock)
        with self.assertRaises(steps.StepError) as cm:
            steps.approve(tree, locs(), cfg(clock), BOUND)
        self.assertEqual(cm.exception.code, "UI_ACTION_FAILED")
        self.assertEqual(FailingAct.attempts, 1)


# -- the long wait ------------------------------------------------------------
class WaitTests(unittest.TestCase):
    def test_live_timeline_completes_without_any_recovery(self):
        for fixture in ("research_flow_ko.json", "research_flow_en_synthetic.json"):
            with self.subTest(fixture=fixture):
                clock = Clock()
                rec = Recover(clock)
                tree, res, wait, lines, clock = approve_and_wait(load(fixture), clock, rec)
                self.assertIsInstance(wait, steps.WaitResult)
                self.assertTrue(wait.complete)
                self.assertEqual(wait.recoveries, [])
                self.assertEqual(rec.calls, [])
                self.assertEqual(acts(tree), [START])            # the wait itself never acts
                self.assertGreaterEqual(wait.longest_no_change_s, 205)   # live: 3.5 min (13:23:33-13:27:05)
                self.assertLess(wait.longest_no_change_s, 222)
                self.assertGreaterEqual(clock.t, 1120 + 20)       # done at ~1121 s + held 20 s
                self.assertLess(clock.t, 1120 + 20 + 15)

    def test_progress_lines_are_in_relay_format_and_scrubbed(self):
        spec = load("research_flow_ko.json")
        node(spec, "r03", "r1_status")["name"] = "웹사이트 37개 조사 중... " + EMAIL
        _tree, _res, wait, lines, _clock = approve_and_wait(spec)
        self.assertTrue(wait.complete)
        self.assertGreaterEqual(len(lines), 6)
        secs = []
        for line in lines:
            m = relay_step.PROGRESS_RE.search(line)
            self.assertIsNotNone(m, line)
            self.assertEqual(line, "[%s] Progress: %s" % (m.group(1) + "s", m.group(2)))
            self.assertNotIn("\n", line)
            secs.append(int(m.group(1)))
        self.assertEqual(secs, sorted(secs))
        blob = "\n".join(lines)
        for want in ("조사 시작 중...", "웹사이트 37개 조사 중...", "SQL 표준 연혁 조사 계획 수립",
                     "공식 출처 인용 및 최종 보고서 작성 준비"):
            self.assertIn(want, blob)
        for never in (EMAIL, "example.invalid", "테스트 대화 제목", "다른 테스트 대화", "Gemini와의 대화",
                      "말씀하신 내용", "Gemini의 응답", "SQL 표준 주요 버전 역사"):
            self.assertNotIn(never, blob)
        self.assertNotIn(EMAIL, wait.last_status)

    def test_completion_needs_stop_gone_done_phrase_and_a_report_control(self):
        def no_stop_gone(spec):
            for st in ("complete", "complete_chip"):
                node(spec, st, "composer")["children"].append(
                    {"id": "b_stop", "ct": "Button", "name": "대답 생성 중지", "patterns": ["invoke"]})

        def no_done(spec):
            remove(spec, "r2_done")

        def no_controls(spec):
            for b in ("rep_toc", "rep_share", "rep_sources"):
                remove(spec, b)

        for name, mutate in (("stop", no_stop_gone), ("done", no_done), ("controls", no_controls)):
            with self.subTest(missing=name):
                spec = load("research_flow_ko.json")
                mutate(spec)
                with self.assertRaises(steps.StepError) as cm:
                    approve_and_wait(spec, max_wait_s=2400)
                self.assertEqual(cm.exception.code, "RESEARCH_TIMEOUT")

    def test_any_one_report_control_is_enough(self):
        for keep in ("rep_toc", "rep_share", "rep_sources"):
            with self.subTest(keep=keep):
                spec = load("research_flow_ko.json")
                for b in ("rep_toc", "rep_share", "rep_sources"):
                    if b != keep:
                        remove(spec, b)
                self.assertTrue(approve_and_wait(spec)[2].complete)

    def test_ko_done_phrase_is_accepted(self):
        spec = load("research_flow_ko.json")
        for st in ("complete", "complete_chip"):
            node(spec, st, "r2_done")["name"] = "연구를 완료했습니다."
        self.assertTrue(approve_and_wait(spec)[2].complete)

    def test_completion_must_hold_for_20_s(self):
        spec = load("research_flow_ko.json")
        spec["states"]["r11b"] = copy.deepcopy(spec["states"]["r11"])
        spec["states"]["complete2"] = copy.deepcopy(spec["states"]["complete"])
        set_timeline(spec, "complete", "r11b", 10)       # flickers back once
        set_timeline(spec, "r11b", "complete2", 30)
        clock = Clock()
        tree = TimelineTree(spec, clock)
        lc = locs()
        steps.approve(tree, lc, cfg(clock), BOUND)
        wait = steps.wait_research(tree, lc, cfg(clock), BOUND, None, None)
        self.assertTrue(wait.complete)
        self.assertEqual(tree.state, "complete2")
        # complete at ~1121, back to r11 at ~1131, complete2 at ~1161: done only after ~1181
        self.assertGreaterEqual(clock.t, 1180)

    def test_already_complete_conversation_returns_after_the_hold(self):
        spec = load("research_flow_ko.json")
        spec["state"] = "complete_chip"
        clock = Clock()
        tree = TimelineTree(spec, clock)
        wait = steps.wait_research(tree, locs(), cfg(clock), BOUND, None, None)
        self.assertTrue(wait.complete)
        self.assertGreaterEqual(clock.t, 20)
        self.assertLess(clock.t, 30)
        self.assertEqual(acts(tree), [])

    def test_stall_below_stall_s_never_recovers(self):
        spec = load("research_flow_ko.json")
        set_timeline(spec, "r06", "r07", 3500)
        clock = Clock()
        rec = Recover(clock)
        _tree, _res, wait, _lines, _clock = approve_and_wait(spec, clock, rec)
        self.assertTrue(wait.complete)
        self.assertEqual(rec.calls, [])
        self.assertGreaterEqual(wait.longest_no_change_s, 3490)

    def test_progress_changing_for_longer_than_stall_s_never_recovers(self):
        spec = load("research_flow_ko.json")
        base = spec["states"]["r10"]
        chain = []
        for i in range(30):                              # 30 x 590 s: 4.9 h of slow but steady progress
            st = copy.deepcopy(base)
            for d in walk(st["documents"][0]):
                if d["id"] == "r1_status":
                    d["name"] = "웹사이트 %d개 조사 중..." % (40 + i)
            spec["states"]["slow%d" % i] = st
            chain.append("slow%d" % i)
        set_timeline(spec, "r10", chain[0], 36)
        for a, b in zip(chain, chain[1:] + ["r11"]):
            set_timeline(spec, a, b, 590)
        clock = Clock()
        rec = Recover(clock)
        _tree, _res, wait, lines, _clock = approve_and_wait(spec, clock, rec, max_wait_s=30000, stall_s=600)
        self.assertTrue(wait.complete)
        self.assertEqual(rec.calls, [])
        self.assertEqual(wait.recoveries, [])
        self.assertLess(wait.longest_no_change_s, 600)
        self.assertIn("웹사이트 69개 조사 중...", "\n".join(lines))

    def _stuck(self, at="r06"):
        spec = load("research_flow_ko.json")
        set_timeline(spec, at)                           # the hidden window stops updating here
        return spec

    def test_stall_with_return_link_visible_recovers_exactly_once_then_completes(self):
        spec = self._stuck()
        clock = Clock()
        holder = {}
        rec = Recover(clock, effect=lambda: holder["tree"].jump("r10"))   # the refresh shows the jump
        tree = TimelineTree(spec, clock)
        holder["tree"] = tree
        lc = locs()
        steps.approve(tree, lc, cfg(clock), BOUND)
        frozen_at = 353.0                                # r06 entered at ~353 s after the start Invoke
        lines = []
        wait = steps.wait_research(tree, lc, cfg(clock), BOUND, lines.append, rec)
        self.assertTrue(wait.complete)
        self.assertEqual(len(rec.calls), 1)
        called_at, url = rec.calls[0]
        self.assertEqual(url, BOUND)
        self.assertGreaterEqual(called_at - frozen_at, 3600 - 10)
        self.assertEqual(len(wait.recoveries), 1)
        self.assertTrue(wait.recoveries[0]["ok"])
        self.assertEqual(wait.recoveries[0]["why"], "no_change")
        self.assertGreaterEqual(wait.longest_no_change_s, 3590)
        self.assertEqual(acts(tree), [START])

    def test_stall_without_a_visible_return_link_is_recovery_unavailable_without_refresh(self):
        def gone(spec):
            remove(spec, "conv_link")

        def offscreen(spec):
            node(spec, "r06", "conv_link")["offscreen"] = True

        def other_url(spec):
            node(spec, "r06", "conv_link")["value"] = BOUND + "0"

        def outside_sidebar(spec):
            remove(spec, "conv_link", ["r06"])
            node(spec, "r06", "turn_r1")["children"].append(
                {"id": "x_link", "ct": "Hyperlink", "name": "링크", "value": BOUND, "patterns": ["invoke"]})

        for mutate in (gone, offscreen, other_url, outside_sidebar):
            with self.subTest(case=mutate.__name__):
                spec = self._stuck()
                mutate(spec)
                clock = Clock()
                rec = Recover(clock)
                with self.assertRaises(steps.StepError) as cm:
                    approve_and_wait(spec, clock, rec)
                self.assertEqual(cm.exception.code, "RECOVERY_UNAVAILABLE")
                self.assertEqual(rec.calls, [])
                self.assertEqual(cm.exception.details["recoveries"], [])
                self.assertGreaterEqual(cm.exception.details["longest_no_change_s"], 3600)

    def test_stall_without_a_recover_callable_is_recovery_unavailable(self):
        with self.assertRaises(steps.StepError) as cm:
            approve_and_wait(self._stuck(), recover=None)
        self.assertEqual(cm.exception.code, "RECOVERY_UNAVAILABLE")

    def test_second_stall_after_the_recovery_is_research_stalled(self):
        clock = Clock()
        rec = Recover(clock)                             # "refresh" that changes nothing
        with self.assertRaises(steps.StepError) as cm:
            approve_and_wait(self._stuck(), clock, rec)
        self.assertEqual(cm.exception.code, "RESEARCH_STALLED")
        self.assertEqual(len(rec.calls), 1)
        self.assertGreaterEqual(clock.t - rec.calls[0][0], 3600)
        d = cm.exception.details
        self.assertEqual(len(d["recoveries"]), 1)
        self.assertTrue(d["recoveries"][0]["ok"])
        self.assertGreaterEqual(d["longest_no_change_s"], 3600)
        self.assertIn("last_status", d)

    def test_failed_recovery_is_research_stalled_and_not_repeated(self):
        for result, exc in ((False, None), (True, OSError("COM"))):
            with self.subTest(result=result, exc=exc):
                clock = Clock()

                def boom():
                    raise exc

                rec = Recover(clock, effect=boom if exc else None, result=result)
                with self.assertRaises(steps.StepError) as cm:
                    approve_and_wait(self._stuck(), clock, rec)
                self.assertEqual(cm.exception.code, "RESEARCH_STALLED")
                self.assertEqual(len(rec.calls), 1)
                self.assertFalse(cm.exception.details["recoveries"][0]["ok"])

    def test_recover_step_error_propagates(self):
        clock = Clock()

        def mismatch():
            raise steps.StepError("CONVERSATION_MISMATCH", {"reason": "rebind"})

        with self.assertRaises(steps.StepError) as cm:
            approve_and_wait(self._stuck(), clock, Recover(clock, effect=mismatch))
        self.assertEqual(cm.exception.code, "CONVERSATION_MISMATCH")

    def test_hard_cap_is_separate_from_the_stall_timer(self):
        spec = load("research_flow_ko.json")
        set_timeline(spec, "r03", "r04", 100)
        set_timeline(spec, "r04", "r03", 100)            # endless progress
        clock = Clock()
        rec = Recover(clock)
        with self.assertRaises(steps.StepError) as cm:
            approve_and_wait(spec, clock, rec, max_wait_s=1000)
        self.assertEqual(cm.exception.code, "RESEARCH_TIMEOUT")
        self.assertEqual(rec.calls, [])
        self.assertGreaterEqual(clock.t, 1000)
        self.assertLess(clock.t, 1100)
        self.assertLess(cm.exception.details["longest_no_change_s"], 200)

    def test_transient_backend_errors_are_retried(self):
        for errors in ([OSError("COM")] * 3, [KeyError("gone"), LookupError("x"), OSError("y")],
                       [OSError("a"), None, OSError("b"), None, OSError("c"), OSError("d")]):
            with self.subTest(errors=errors):
                clock = Clock()
                tree = FlakyTree(load("research_flow_ko.json"), clock, errors, after_snaps=20)
                lc = locs()
                steps.approve(tree, lc, cfg(clock), BOUND)
                wait = steps.wait_research(tree, lc, cfg(clock), BOUND, None, None)
                self.assertTrue(wait.complete)

    def test_persistent_backend_error_raises_after_the_bound(self):
        clock = Clock()
        tree = FlakyTree(load("research_flow_ko.json"), clock, [OSError("COM")] * 4, after_snaps=20)
        lc = locs()
        steps.approve(tree, lc, cfg(clock), BOUND)
        with self.assertRaises(OSError):
            steps.wait_research(tree, lc, cfg(clock), BOUND, None, None)

    def test_other_errors_are_not_retried(self):
        clock = Clock()
        tree = FlakyTree(load("research_flow_ko.json"), clock, [ValueError("bug")], after_snaps=20)
        lc = locs()
        steps.approve(tree, lc, cfg(clock), BOUND)
        with self.assertRaises(ValueError):
            steps.wait_research(tree, lc, cfg(clock), BOUND, None, None)

    def test_url_change_during_the_wait_is_a_mismatch(self):
        spec = load("research_flow_ko.json")
        doc_of(spec, "r05")["value"] = OTHER
        with self.assertRaises(steps.StepError) as cm:
            approve_and_wait(spec)
        self.assertEqual(cm.exception.code, "CONVERSATION_MISMATCH")

    def test_progress_of_another_conversation_never_counts(self):
        spec = self._stuck()
        other = copy.deepcopy(doc_of(spec, "complete_chip"))
        for d in walk(other):
            d["id"] = "x_" + d["id"]
        other["value"] = OTHER
        for name, st in spec["states"].items():
            if name in STEPS:
                st["documents"].append(copy.deepcopy(other))
        clock = Clock()
        rec = Recover(clock)
        lines = []
        with self.assertRaises(steps.StepError) as cm:
            approve_and_wait(spec, clock, rec, lines=lines)
        self.assertEqual(cm.exception.code, "RESEARCH_STALLED")
        self.assertNotIn("I've completed", "\n".join(lines))

    def test_two_documents_with_the_job_url_are_a_mismatch(self):
        spec = load("research_flow_ko.json")
        dup = copy.deepcopy(doc_of(spec, "r03"))
        for d in walk(dup):
            d["id"] = "y_" + d["id"]
        spec["states"]["r03"]["documents"].append(dup)
        with self.assertRaises(steps.StepError) as cm:
            approve_and_wait(spec)
        self.assertEqual(cm.exception.code, "CONVERSATION_MISMATCH")

    def test_gemini_error_turn_ends_the_wait(self):
        spec = load("research_flow_ko.json")
        st = spec["states"]["r05"]
        for d in walk(st["documents"][0]):
            if d["id"] == "r1_status":
                d["name"] = "요청하신 작업을 처리하는 중에 오류가 발생했습니다. 나중에 다시 시도해 주세요."
        remove(spec, "b_stop", ["r05"])
        set_timeline(spec, "r05")
        with self.assertRaises(steps.StepError) as cm:
            approve_and_wait(spec)
        self.assertEqual(cm.exception.code, "GEMINI_ERROR_RESPONSE")

    def test_every_new_error_code_is_documented(self):
        for code in ("ALREADY_STARTED", "PLAN_CARD_STALE", "RESEARCH_NOT_STARTED", "RESEARCH_STALLED",
                     "RECOVERY_UNAVAILABLE", "RESEARCH_TIMEOUT"):
            self.assertRegex(steps.__doc__, r"(?m)^\s*%s\s+\S" % code)

    def test_defaults_match_the_spec(self):
        c = steps.StepConfig()
        self.assertEqual(c.stall_s, 3600)
        self.assertEqual(c.complete_hold_s, 20)
        self.assertEqual(c.research_poll_s, 5)
        self.assertEqual(c.backend_retries, 3)
        self.assertGreater(c.max_wait_s, c.stall_s)


if __name__ == "__main__":
    unittest.main()

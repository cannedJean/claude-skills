"""
test_conversation_identity.py - a report is accepted only from the job's own Gemini conversation.

Regression for two jobs driving the one shared Gemini page at the same time: each driver polled whatever
conversation the page showed, and job B saved job A's report (byte-identical) as its own. These tests pin
the identity checks in the driver (run_deep_research.py) and the relay (relay_step.py); the orchestrator
side (deep_research.py) is covered end to end in test_deep_research.py.
"""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import deep_research as dr
import relay_step as rs
import run_deep_research as rdr

A_ID, B_ID = "2867cf7e0c33d819", "88a4c8b70eab140c"
A_URL = f"https://gemini.google.com/app/{A_ID}"
B_URL = f"https://gemini.google.com/app/{B_ID}"
TITLE_A, TITLE_B = "AI Agent Sandboxing Strategy Report", "Mapping Global Corporate Power Structures"
REPORT_MD = "# Report\n\n" + "finding " * 120


def state(title=TITLE_A, done=False, panel=None, chip_titles=None, report_len=None):
    """A wait-loop poll result as the page JS returns it (title-aware fields included)."""
    return {
        "isCardCompleted": done,
        "hasWorkingIndicator": not done,
        "statusText": "Writing your report..." if not done else "",
        "chipText": f"{title} {'11월 12일 오후 3:32' if done else 'Writing your report...'}",
        "isChipStillWriting": not done,
        "hasImmersiveChip": True,
        "hasImmersivePanel": True,
        "reportTextLen": (report_len if report_len is not None else (900 if done else 50)),
        "chipTitles": [title] if chip_titles is None else chip_titles,
        "panelTitle": title if panel is None else panel,
    }


class FakePage:
    """Stand-in for a Playwright page. Poll evaluates replay `states` (the last one repeats); the report
    extraction (HTML_TO_MARKDOWN_JS) returns `extraction`. A step may carry the url the page is on afterwards."""

    def __init__(self, url, states, extraction=None, queries=None):
        self.url = url
        self.states = list(states)
        self.extraction = extraction if extraction is not None else ({"markdown": REPORT_MD, "sources": []}, None)
        self.extracted = 0
        self.goto_calls = []
        self.goto_lands_on = None
        self.queries = list(queries or [])
        self.context = MagicMock()

    def evaluate(self, script, arg=None):
        if "user-query" in str(script) and "convertNode" not in str(script):
            return list(self.queries)
        if "convertNode" in str(script):
            self.extracted += 1
            value, url_after = self.extraction
        else:
            value, url_after = self.states[0] if len(self.states) == 1 else self.states.pop(0)
        if url_after:
            self.url = url_after
        if isinstance(value, Exception):
            raise value
        return value

    def locator(self, *args, **kwargs):
        loc = MagicMock()
        loc.count.return_value = 0
        loc.first = loc
        loc.last = loc
        return loc

    def goto(self, url, **kwargs):
        self.goto_calls.append(url)
        self.url = self.goto_lands_on or url

    def reload(self, **kwargs):
        pass

    def wait_for_load_state(self, *args, **kwargs):
        pass


class TestConversationIdParsing(unittest.TestCase):
    CASES = [
        (A_URL, A_ID),
        (A_URL + "/", A_ID),
        (A_URL + "?hl=ko", A_ID),
        (A_URL + "#section", A_ID),
        (f"https://gemini.google.com/u/1/app/{A_ID}", A_ID),
        (A_URL.upper().replace("HTTPS://GEMINI.GOOGLE.COM", "https://gemini.google.com"), A_ID),
        ("https://gemini.google.com/app/fake_conv_001", "fake_conv_001"),
        ("https://GEMINI.google.com/app/" + A_ID, A_ID),
        ("https://example.com/app/" + A_ID, None),          # only the Gemini app's own conversations
        ("gemini.google.com/app/" + A_ID, None),             # not a url
        ("https://gemini.google.com/app", None),
        ("https://gemini.google.com/app/", None),
        ("current", None),
        ("", None),
        (None, None),
    ]

    def test_all_three_components_agree(self):
        for fn in (rdr.conversation_id_from_url, rs.conversation_id_from_url, dr.conversation_id_from_url):
            for url, want in self.CASES:
                with self.subTest(component=fn.__module__, url=url):
                    self.assertEqual(fn(url), want)

    def test_non_string_url_is_none(self):
        self.assertIsNone(rdr.conversation_id_from_url(MagicMock()))


class TestDriverWaitLoopIdentity(unittest.TestCase):
    def run_wait(self, page, expected=A_ID, timeout=5):
        logs = []
        with patch.object(rdr.time, "sleep"), patch.object(rdr, "log", side_effect=logs.append):
            try:
                return rdr.wait_for_report_completion(page, timeout_sec=timeout, expected_conversation_id=expected), logs
            except (rdr.ConversationMismatch, TimeoutError) as e:
                return e, logs

    def test_incident_page_moves_to_other_conversation_before_completion(self):
        # job B waits on its conversation; the shared page is pulled to job A's conversation, whose research finishes
        page = FakePage(B_URL, [(state(TITLE_B), None), (state(TITLE_A, done=True), A_URL)])
        out, logs = self.run_wait(page, expected=B_ID)
        self.assertIsInstance(out, rdr.ConversationMismatch)
        self.assertEqual(page.extracted, 0, "nothing may be extracted from another conversation")
        self.assertFalse(any(TITLE_A in line for line in logs), f"foreign progress was logged: {logs}")

    def test_report_panel_of_another_research_is_rejected(self):
        # url stays on the job's conversation, but the global report panel shows a different research
        page = FakePage(A_URL, [(state(TITLE_A), None), (state(TITLE_A, done=True, panel=TITLE_B), None)])
        out, _ = self.run_wait(page)
        self.assertIsInstance(out, rdr.ConversationMismatch)
        self.assertEqual(page.extracted, 0)

    def test_research_chip_replaced_by_another_conversations_chip(self):
        page = FakePage(A_URL, [(state(TITLE_A), None),
                                (state(TITLE_B, done=True, chip_titles=[TITLE_B], panel=TITLE_B), None)])
        out, _ = self.run_wait(page)
        self.assertIsInstance(out, rdr.ConversationMismatch)
        self.assertEqual(page.extracted, 0)

    def test_panel_of_another_research_is_not_accepted_before_this_conversations_chip_appears(self):
        # review round 2: the app-shell panel still shows another research while this conversation has no chip yet
        page = FakePage(A_URL, [(state(TITLE_B, done=True, chip_titles=[], panel=TITLE_B, report_len=5000), None)],
                        extraction=({"markdown": "# B report", "sources": [], "chipTitles": [], "panelTitle": TITLE_B}, None))
        out, _ = self.run_wait(page, timeout=2)
        self.assertNotIsInstance(out, dict, f"a report was accepted: {out}")
        self.assertEqual(page.extracted, 0)

    def test_chip_check_alone_rejects_another_research(self):
        page = FakePage(A_URL, [(state(TITLE_A), None), (state(TITLE_B, done=True, chip_titles=[TITLE_B], panel=""), None)])
        out, _ = self.run_wait(page)
        self.assertIsInstance(out, rdr.ConversationMismatch)

    def test_page_moves_during_extraction(self):
        page = FakePage(A_URL, [(state(TITLE_A, done=True), None)],
                        extraction=({"markdown": REPORT_MD, "sources": []}, B_URL))
        out, _ = self.run_wait(page)
        self.assertIsInstance(out, rdr.ConversationMismatch)

    def test_page_already_elsewhere_fails_before_reading(self):
        page = FakePage(B_URL, [(state(TITLE_B, done=True), None)])
        out, logs = self.run_wait(page, expected=A_ID)
        self.assertIsInstance(out, rdr.ConversationMismatch)
        self.assertFalse(any("Progress:" in line for line in logs), logs)

    def test_mismatch_message_names_both_conversations(self):
        page = FakePage(B_URL, [(state(TITLE_B), None)])
        out, _ = self.run_wait(page, expected=A_ID)
        self.assertIn("Conversation mismatch", str(out))
        self.assertIn(A_ID, str(out))
        self.assertIn(B_ID, str(out))

    def test_own_report_carries_provenance(self):
        page = FakePage(A_URL, [(state(TITLE_A), None), (state(TITLE_A, done=True), None)])
        out, _ = self.run_wait(page)
        self.assertNotIsInstance(out, Exception, out)
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["conversation_id"], A_ID)
        self.assertEqual(out["url"], A_URL)
        self.assertEqual(out["report_title"], TITLE_A)

    def test_without_expected_id_the_starting_conversation_is_pinned(self):
        page = FakePage(A_URL, [(state(TITLE_A), None), (state(TITLE_B, done=True), B_URL)])
        out, _ = self.run_wait(page, expected=None)
        self.assertIsInstance(out, rdr.ConversationMismatch)

    def test_states_without_title_fields_still_complete(self):
        legacy = {k: v for k, v in state(TITLE_A, done=True).items() if k not in ("chipTitles", "panelTitle")}
        page = FakePage(A_URL, [(legacy, None)])
        out, _ = self.run_wait(page)
        self.assertNotIsInstance(out, Exception, out)
        self.assertEqual(out["conversation_id"], A_ID)

    def test_provenance_line_round_trips(self):
        line = rdr.format_report_provenance({"conversation_id": A_ID, "url": A_URL, "report_title": TITLE_A, "markdown": "x"})
        self.assertTrue(line.startswith(rdr.PROVENANCE_PREFIX))
        payload = json.loads(line[len(rdr.PROVENANCE_PREFIX):])
        self.assertEqual(payload, {"conversation_id": A_ID, "url": A_URL, "report_title": TITLE_A})


class TestDriverBindConversation(unittest.TestCase):
    def bind(self, page, resume_url):
        with patch.object(rdr.time, "sleep"), patch.object(rdr, "log"):
            return rdr.bind_conversation(page, resume_url)

    def test_already_on_the_conversation(self):
        page = FakePage(A_URL + "?hl=ko", [])
        self.assertEqual(self.bind(page, A_URL), A_ID)
        self.assertEqual(page.goto_calls, [])

    def test_navigates_to_the_conversation(self):
        page = FakePage(B_URL, [])
        self.assertEqual(self.bind(page, A_URL), A_ID)
        self.assertEqual(page.goto_calls, [A_URL])

    def test_navigation_that_lands_elsewhere_fails(self):
        page = FakePage(B_URL, [])
        page.goto_lands_on = "https://gemini.google.com/app"  # e.g. a deleted conversation redirects to a new chat
        with self.assertRaises(rdr.ConversationMismatch):
            self.bind(page, A_URL)

    def test_current_pins_the_open_conversation(self):
        self.assertEqual(self.bind(FakePage(B_URL, []), "current"), B_ID)

    def test_current_without_a_conversation_fails(self):
        with self.assertRaises(rdr.ConversationMismatch):
            self.bind(FakePage("https://gemini.google.com/app", []), "current")

    def test_resume_url_without_id_fails(self):
        with self.assertRaises(rdr.ConversationMismatch):
            self.bind(FakePage(A_URL, []), "https://gemini.google.com/app")

    def test_conversation_line_round_trips(self):
        line = rdr.format_conversation_line(A_ID, A_URL)
        self.assertTrue(line.startswith(rdr.CONVERSATION_PREFIX))
        self.assertEqual(json.loads(line[len(rdr.CONVERSATION_PREFIX):]), {"conversation_id": A_ID, "url": A_URL})


MY_PROMPT = "<research_objective>\nAssess the production readiness of CPython's free-threaded build.\n</research_objective>"
OTHER_PROMPT = "<research_objective>\nCompare and recommend sandboxing strategies for agents.\n</research_objective>"


def rendered_query(prompt):
    """How Gemini renders a user query's innerText: a localized label, a collapsed preview, then the full text."""
    flat = " ".join(prompt.split())
    return f"말씀하신 내용 {flat[:100]}… {flat}"


class TestDriverNewChatIdentity(unittest.TestCase):
    """A new chat (plan / auto) owns only the conversation its own prompt created."""

    def test_page_already_on_a_conversation_is_not_a_new_chat(self):
        rdr.assert_fresh_new_chat(FakePage("https://gemini.google.com/app", []))
        with self.assertRaises(rdr.ConversationMismatch):
            rdr.assert_fresh_new_chat(FakePage(A_URL, []))

    def test_new_chat_holding_this_runs_prompt_is_pinned(self):
        page = FakePage(A_URL, [], queries=[rendered_query(MY_PROMPT)])
        with patch.object(rdr.time, "sleep"):
            self.assertEqual(rdr.verify_new_chat_conversation(page, MY_PROMPT), A_ID)

    def test_conversation_of_another_prompt_is_refused(self):
        page = FakePage(A_URL, [], queries=[rendered_query(OTHER_PROMPT)])
        with patch.object(rdr.time, "sleep"), self.assertRaises(rdr.ConversationMismatch):
            rdr.verify_new_chat_conversation(page, MY_PROMPT)

    def test_composed_prompts_sharing_boilerplate_are_told_apart(self):
        # review round 2: every --compose-xml prompt starts with the same template sentence
        mine = "<research_objective>\nConduct a comprehensive, multi-dimensional technical analysis on: Postgres vacuum\n</research_objective>"
        other = "<research_objective>\nConduct a comprehensive, multi-dimensional technical analysis on: Rust async runtimes\n</research_objective>"
        page = FakePage(A_URL, [], queries=[rendered_query(other)])
        with patch.object(rdr.time, "sleep"), self.assertRaises(rdr.ConversationMismatch):
            rdr.verify_new_chat_conversation(page, mine)

    def test_conversation_with_more_than_one_query_is_refused(self):
        page = FakePage(A_URL, [], queries=[rendered_query(MY_PROMPT), "말씀하신 내용 Start research"])
        with patch.object(rdr.time, "sleep"), self.assertRaises(rdr.ConversationMismatch):
            rdr.verify_new_chat_conversation(page, MY_PROMPT)

    def test_page_without_a_conversation_is_refused(self):
        page = FakePage("https://gemini.google.com/app", [], queries=[rendered_query(MY_PROMPT)])
        with patch.object(rdr.time, "sleep"), self.assertRaises(rdr.ConversationMismatch):
            rdr.verify_new_chat_conversation(page, MY_PROMPT)

    def test_plan_wait_refuses_a_conversation_the_page_was_moved_to_before_touching_it(self):
        # review finding: the page moves to job A's conversation (pending plan, active start button) before this
        # new chat gets its id; the plan wait must not adopt A's plan, cancel A's countdown or expand A's plan
        page = FakePage("https://gemini.google.com/app", [], queries=[rendered_query(OTHER_PROMPT)])

        def locator(sel, **kwargs):
            loc = MagicMock()
            on_conversation = rdr.conversation_id_from_url(page.url) is not None
            loc.count.return_value = 1 if on_conversation else 0
            loc.last = loc
            loc.first = loc
            loc.inner_text.return_value = "A's plan"
            return loc

        page.locator = locator

        def generation_in_progress(_page):
            page.url = A_URL   # another job's probe moved the shared page
            return False

        with patch.object(rdr.time, "sleep"), \
             patch.object(rdr, "is_generation_in_progress", side_effect=generation_in_progress), \
             patch.object(rdr, "stop_auto_confirm_fuse_and_expand_plan") as fuse, \
             self.assertRaises(rdr.ConversationMismatch):
            rdr.wait_for_new_plan(page, prev_plan_count=0, timeout_sec=30, new_chat_prompt=MY_PROMPT)
        fuse.assert_not_called()

    def test_edit_checks_the_conversation_before_clicking_or_typing(self):
        page = FakePage(B_URL, [])
        with patch.object(rdr.time, "sleep"), patch.object(rdr, "log"), \
             patch.object(rdr, "log_current_model_mode"), \
             patch.object(rdr, "submit_prompt") as submit, \
             patch.object(rdr, "wait_for_new_plan") as wait_plan, \
             self.assertRaises(rdr.ConversationMismatch):
            rdr.apply_plan_edit(page, "Add a GPU memory comparison.", conversation_id=A_ID)
        submit.assert_not_called()
        wait_plan.assert_not_called()

    def test_edit_on_its_own_conversation_proceeds(self):
        page = FakePage(A_URL, [])
        with patch.object(rdr.time, "sleep"), patch.object(rdr, "log"), \
             patch.object(rdr, "log_current_model_mode"), \
             patch.object(rdr, "submit_prompt") as submit, \
             patch.object(rdr, "wait_for_new_plan", return_value={"status": "plan_ready", "url": A_URL, "plan": "p"}):
            rdr.apply_plan_edit(page, "Add a GPU memory comparison.", conversation_id=A_ID)
        submit.assert_called_once()


FAKE_DRIVER_SRC = '#!/usr/bin/env python3\n# relay feature probe: "--step" "--prompt-file" "--edit-prompt-file"\n'


def driver_stdout(provenance_id=A_ID, title=TITLE_A, report=REPORT_MD, with_provenance=True, provenance_in_body=False):
    lines = ["[10:00:00] Local Gemini app ready via CDP (http://127.0.0.1:9223, status=already_running)",
             "[10:00:01] Deep Research is running in the background. Monitoring progress...",
             "[10:05:00] Deep Research report complete! (900 chars)"]
    if with_provenance:
        lines.append(rdr.format_report_provenance(
            {"conversation_id": provenance_id, "url": f"https://gemini.google.com/app/{provenance_id}", "report_title": title}))
    body = report
    if provenance_in_body:
        body += "\n" + rdr.format_report_provenance({"conversation_id": A_ID, "url": A_URL, "report_title": TITLE_A})
    lines += ["", rs.REPORT_START, "", body, "", rs.REPORT_END, ""]
    return "\n".join(lines) + "\n"


def make_run(stdout, rc=0, stderr="", confirm_clicked=False):
    run = rs.Run()
    run.rc, run.out, run.err = rc, [stdout], [stderr]
    run.confirm_clicked = confirm_clicked
    run.monitoring = True
    return run


class TestRelayIdentity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.job = self.tmp.name
        self.relay_dir = os.path.join(self.job, "relay")
        os.makedirs(self.relay_dir)
        self.result_path = os.path.join(self.relay_dir, "result.json")
        self.driver = os.path.join(self.job, "run_deep_research.py")
        with open(self.driver, "w", encoding="utf-8") as f:
            f.write(FAKE_DRIVER_SRC)
        self.output = os.path.join(self.job, "report.md")

    def req(self, step, resume_url=A_URL, **extra):
        r = {"relay_version": 2, "step": step, "seq": 3, "driver": self.driver, "output": self.output, "timeout": 60}
        if step == "auto":
            prompt = os.path.join(self.job, "prompt.md")
            with open(prompt, "w", encoding="utf-8") as f:
                f.write("<research_objective>x</research_objective>\n")
            r["prompt_file"] = prompt
        else:
            r["resume_url"] = resume_url
        r.update(extra)
        return r

    def do(self, req, probe, run):
        with patch.object(rs, "probe_page", return_value=probe) as p_probe, \
             patch.object(rs, "run_driver", return_value=run) as p_run:
            res, code = rs.do_step(self.job, req, self.relay_dir, self.result_path)
        return res, code, p_probe, p_run

    def probe(self, url=A_URL, active_confirm=0):
        return {"url": url, "url_before": url, "navigated": False, "active_confirm": active_confirm, "active_edit": 1,
                "plan_cards": 1, "responses": 2, "report_len": 0, "error": None}

    def test_collect_refuses_when_probe_is_on_another_conversation(self):
        res, code, _, p_run = self.do(self.req("collect"), self.probe(B_URL), make_run(driver_stdout()))
        self.assertEqual((res["code"], code), ("CONVERSATION_MISMATCH", 2), res)
        p_run.assert_not_called()

    def test_approve_refuses_before_clicking_on_another_conversation(self):
        res, code, _, p_run = self.do(self.req("approve", expected_url=A_URL, expected_plan_count=1),
                                      self.probe(B_URL, active_confirm=1), make_run(driver_stdout()))
        self.assertEqual((res["code"], code), ("CONVERSATION_MISMATCH", 2), res)
        p_run.assert_not_called()

    def test_report_from_another_conversation_is_not_completed(self):
        res, code, _, _ = self.do(self.req("collect"), self.probe(), make_run(driver_stdout(provenance_id=B_ID, title=TITLE_B)))
        self.assertEqual(res["status"], "error")
        self.assertEqual((res["code"], code), ("CONVERSATION_MISMATCH", 2), res)

    def test_report_without_provenance_is_not_completed(self):
        res, code, _, _ = self.do(self.req("collect"), self.probe(), make_run(driver_stdout(with_provenance=False)))
        self.assertEqual(res["status"], "error")
        self.assertEqual((res["code"], code), ("REPORT_PROVENANCE_MISSING", 2), res)

    def test_provenance_quoted_inside_the_report_does_not_count(self):
        res, _, _, _ = self.do(self.req("collect"), self.probe(),
                               make_run(driver_stdout(with_provenance=False, provenance_in_body=True)))
        self.assertEqual(res.get("code"), "REPORT_PROVENANCE_MISSING", res)

    def test_stale_report_file_without_provenance_is_not_completed(self):
        with open(self.output, "w", encoding="utf-8") as f:
            f.write(REPORT_MD)  # left over from an earlier run
        res, _, _, _ = self.do(self.req("collect"), self.probe(), make_run("[10:00:00] driver exited early\n"))
        self.assertNotEqual(res["status"], "completed", res)

    def test_own_report_completes_with_provenance(self):
        res, code, _, _ = self.do(self.req("collect"), self.probe(), make_run(driver_stdout()))
        self.assertEqual((res["status"], code), ("completed", 0), res)
        self.assertEqual(res["conversation_id"], A_ID)
        self.assertEqual(res["url"], A_URL)
        self.assertEqual(res["report_title"], TITLE_A)

    def test_auto_records_the_driver_pinned_conversation_not_a_later_probe(self):
        res, code, _, _ = self.do(self.req("auto"), self.probe(B_URL), make_run(driver_stdout()))
        self.assertEqual((res["status"], code), ("completed", 0), res)
        self.assertEqual(res["url"], A_URL)
        self.assertEqual(res["conversation_id"], A_ID)

    def test_legacy_current_resume_url_still_requires_provenance(self):
        res, _, _, _ = self.do(self.req("collect", resume_url="current"), self.probe(B_URL), make_run(driver_stdout(B_ID, TITLE_B)))
        self.assertEqual(res["status"], "completed", res)
        self.assertEqual(res["conversation_id"], B_ID)

    def test_driver_mismatch_exception_maps_to_code(self):
        run = make_run("", rc=1, stderr="Traceback (most recent call last):\n  ...\n"
                       f"run_deep_research.ConversationMismatch: Conversation mismatch (poll): expected {A_ID}, page is on {B_ID}\n")
        err = rs.classify(run, "collect")
        self.assertEqual((err.code, err.exit_code), ("CONVERSATION_MISMATCH", 2))

    def test_plan_and_edit_mismatch_hints_do_not_send_the_caller_to_collect(self):
        run = make_run("", rc=1, stderr="__main__.ConversationMismatch: Conversation mismatch (plan wait): expected a, page is on b\n")
        for step in ("plan", "edit"):
            err = rs.classify(run, step)
            self.assertEqual(err.code, "CONVERSATION_MISMATCH")
            self.assertNotIn("collect", err.hint)
        self.assertIn("collect", rs.classify(run, "collect").hint)

    def test_provenance_must_immediately_precede_the_report(self):
        out = driver_stdout().replace(rs.REPORT_START, "[10:05:01] a later log line\n" + rs.REPORT_START, 1)
        res, _, _, _ = self.do(self.req("collect"), self.probe(), make_run(out))
        self.assertEqual(res.get("code"), "REPORT_PROVENANCE_MISSING", res)

    def test_error_results_carry_the_pinned_conversation(self):
        run = make_run("", rc=1, stderr="Traceback ...\nTimeoutError: Deep Research did not finish within 1200 seconds.\n")
        run.conversation = {"conversation_id": A_ID, "url": A_URL}
        res, code, _, _ = self.do(self.req("auto"), self.probe(), run)
        self.assertEqual((res["code"], code), ("REPORT_TIMEOUT", 2))
        self.assertEqual((res["conversation_id"], res["conversation_url"]), (A_ID, A_URL))

    def test_auto_report_must_come_from_the_conversation_it_pinned(self):
        run = make_run(driver_stdout(provenance_id=B_ID, title=TITLE_B))
        run.conversation = {"conversation_id": A_ID, "url": A_URL}
        res, code, _, _ = self.do(self.req("auto"), self.probe(), run)
        self.assertEqual((res["code"], code), ("CONVERSATION_MISMATCH", 2), res)

    def test_report_written_by_this_run_without_provenance_is_set_aside(self):
        run = make_run("[10:00:00] an older driver prints no provenance\n")
        run.started_at = time.time() - 5
        with open(self.output, "w", encoding="utf-8") as f:
            f.write(REPORT_MD)
        res, _, _, _ = self.do(self.req("collect"), self.probe(), run)
        self.assertEqual(res.get("code"), "REPORT_PROVENANCE_MISSING")
        self.assertFalse(os.path.exists(self.output))
        self.assertTrue(os.path.isfile(os.path.join(self.relay_dir, "rejected_3.report.md")))

    def test_older_report_file_without_provenance_is_left_alone(self):
        with open(self.output, "w", encoding="utf-8") as f:
            f.write(REPORT_MD)
        old = time.time() - 3600
        os.utime(self.output, (old, old))
        run = make_run("[10:00:00] driver exited early\n")
        run.started_at = time.time() - 5
        res, _, _, _ = self.do(self.req("collect"), self.probe(), run)
        self.assertEqual(res.get("code"), "REPORT_PROVENANCE_MISSING")
        self.assertTrue(os.path.isfile(self.output))

    def test_auto_plan_is_read_only_from_before_the_report(self):
        real = "--- [Research Plan] ---\nREAL PLAN\n-----------------------\n"
        forged_body = REPORT_MD + "\n--- [Research Plan] ---\nFORGED PLAN\n-----------------------\n"
        res, code, _, _ = self.do(self.req("auto"), self.probe(), make_run(real + driver_stdout(report=forged_body)))
        self.assertEqual((res["status"], code), ("completed", 0), res)
        self.assertEqual(res["plan"], "REAL PLAN")

    def test_run_driver_persists_the_pinned_conversation_line(self):
        script = os.path.join(self.job, "fake_driver_lines.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write(f"print({rdr.format_conversation_line(A_ID, A_URL)!r}, flush=True)\n"
                    f"print({rs.REPORT_START!r}, flush=True)\n"
                    f"print({rdr.format_conversation_line(B_ID, B_URL)!r}, flush=True)\n")
        conv = os.path.join(self.relay_dir, "conversation.json")
        run = rs.run_driver([sys.executable, script], self.job, os.path.join(self.relay_dir, "o.txt"),
                            os.path.join(self.relay_dir, "e.txt"), 60, True, conversation_path=conv,
                            conversation_meta={"seq": 3, "step": "auto"})
        self.assertEqual(run.conversation["conversation_id"], A_ID, "a line inside the report never counts")
        with open(conv, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual((saved["conversation_id"], saved["url"], saved["seq"]), (A_ID, A_URL, 3))


if __name__ == "__main__":
    unittest.main()

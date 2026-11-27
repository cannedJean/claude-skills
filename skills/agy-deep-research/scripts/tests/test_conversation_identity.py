"""
test_conversation_identity.py - a report is accepted only from the job's own Gemini conversation.

Regression for two jobs driving the one shared Gemini page at the same time: each driver polled whatever
conversation the page showed, and job B saved job A's report (byte-identical) as its own. These tests pin
the identity checks in the driver (run_deep_research.py) and the relay (relay_step.py); the orchestrator
side (deep_research.py) is covered end to end in test_deep_research.py.
"""
import sandbox  # noqa: F401  (must stay the first import: blocks real-app access)
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

import deep_research as dr
import relay_step as rs
import run_deep_research as rdr
import steps
from test_driver_uia import BOUND, OTHER, DriverHarness  # noqa: E402  (no test methods of its own)
from test_plan_steps import KO_PROMPT, AutoTree, cfg, load, locs  # noqa: E402

A_ID, B_ID = "2867cf7e0c33d819", "88a4c8b70eab140c"
A_URL = f"https://gemini.google.com/app/{A_ID}"
B_URL = f"https://gemini.google.com/app/{B_ID}"
TITLE_A, TITLE_B = "AI Agent Sandboxing Strategy Report", "Mapping Global Corporate Power Structures"
REPORT_MD = "# Report\n\n" + "finding " * 120


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


class TestUiaDriverIdentity(DriverHarness):
    """The UI Automation driver (run_deep_research.py) reads and acts only on the document whose URL is exactly the
    job's conversation. These replace the Playwright page tests (goto / user-query / report-panel identity):
    - binding: exact URL match, never a navigation; 'current', a URL variant or a URL without an id binds nothing;
    - a new chat is the document the prompt was typed into, bound when its URL becomes /app/<id>; another driver
      cannot move the instance meanwhile (host lock, test_driver_uia.LockTests);
    - report identity: approve / collect are not written for UIA yet and print no provenance or report, so the
      relay accepts no report (the relay-side identity gates below are unchanged)."""

    def run_probe(self, url, tree):
        return self.run_main(["--step", "probe", "--resume-url", url], tree)

    def two_cards(self):
        return AutoTree(load("plan_flow_edit_two_cards.json"))

    def test_only_the_exact_conversation_url_binds(self):
        for url in (BOUND + "?hl=ko", BOUND + "/", BOUND.upper().replace("HTTPS://GEMINI.GOOGLE.COM",
                                                                         "https://gemini.google.com"),
                    OTHER, "https://gemini.google.com/app", "current"):
            with self.subTest(url=url):
                tree = self.two_cards()
                rc, out, err = self.run_probe(url, tree)
                self.assertEqual(rc, 2, (out, err))
                self.assertEqual(json.loads(out.strip().splitlines()[-1])["code"], "CONVERSATION_MISMATCH")
                self.assertEqual(tree.actions, [])
        rc, out, _ = self.run_probe(BOUND, self.two_cards())
        self.assertEqual((rc, json.loads(out.strip().splitlines()[-1])["url"]), (0, BOUND))

    def test_edit_on_another_conversation_clicks_and_types_nothing(self):
        tree = self.two_cards()
        rc, out, err = self.run_main(["--step", "edit", "--resume-url", OTHER, "--edit-prompt", "Add a GPU section."], tree)
        self.assertEqual(rc, 2, (out, err))
        self.assertEqual(tree.actions, [])
        self.assertNotIn(rs.LINE_PROMPT_SENT, out)
        self.assertNotIn(rs.LINE_EDIT, out)

    def test_new_chat_binds_the_document_it_typed_into(self):
        tree = AutoTree(load("plan_flow_ko.json"))
        rc, out, err = self.run_main(["--step", "plan", "--raw-prompt", "--prompt", KO_PROMPT], tree)
        self.assertEqual(rc, 0, (out, err))
        conv = [rs.parse_conversation_line(ln) for ln in out.splitlines() if ln.startswith(rdr.CONVERSATION_PREFIX)]
        self.assertEqual(conv, [{"conversation_id": BOUND.rsplit("/", 1)[1], "url": BOUND}])
        typed = [a for a in tree.actions if a[0] == "set_value"]
        self.assertEqual(typed, [("set_value", "prompt", KO_PROMPT)])

    def test_send_on_a_document_that_is_not_a_new_chat_is_refused(self):
        spec = load("plan_flow_ko.json")
        spec["state"] = "typed"
        tree = AutoTree(spec)
        doc = steps.bind_conversation(tree, "https://gemini.google.com/app")
        for d in tree._states["typed"]["documents"]:
            if d.id == doc.id:
                d.value = OTHER   # the typed-into document moved to another conversation before the send
        with self.assertRaises(steps.StepError) as cm:
            steps.send_and_bind(tree, locs(), cfg(), doc)
        self.assertEqual(cm.exception.code, "CONVERSATION_MISMATCH")
        self.assertNotIn(("act", "b_send", "invoke"), tree.actions)

    def test_steps_without_a_report_print_no_provenance(self):
        for step in ("approve", "collect"):
            with self.subTest(step=step):
                rc, out, _ = self.run_main(["--step", step, "--resume-url", BOUND], self.two_cards())
                self.assertEqual(rc, 3)
                self.assertNotIn(rdr.PROVENANCE_PREFIX, out)
                self.assertNotIn(rs.REPORT_START, out)
                self.assertIsNone(rs.parse_provenance(out))


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

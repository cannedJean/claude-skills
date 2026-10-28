"""Tests for the pipeline layer (stdlib unittest): slide_analyze (report scope, audits, resolve, shards, deck guard,
references first, engines, robustness), crops (judging Claude's reads, fingerprints), slide_validate (guards and
prompt-rule warnings) and the analyst prompt (few-shot markers, enums).

    python -m unittest discover -s scripts/tests -v        (from the skill folder)

No agy and no OCR engine needed: records are written by hand, the scorer / references are stubbed where a test is
about plumbing, and the CLI cases run tests/fake_agy_task.py with OCR_ENGINE=tesseract (no engine on a machine
without Tesseract; Tesseract when present, which only makes them slower).
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
SKILL = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(HERE))

import crops as cropmod  # noqa: E402
import ocr_ref  # noqa: E402
import ocr_score  # noqa: E402
import slide_analyze as sa  # noqa: E402
import slide_validate as sv  # noqa: E402
import synth  # noqa: E402
from fake_agy_task import CANNED, TITLE_ONLY  # noqa: E402

FAKE = HERE / "fake_agy_task.py"


def ns(**kw):
    return argparse.Namespace(**{"reviewer": "none", "audit_rate": 0.0, "grounding_check": "flagged", "shards": 2, **kw})


def an(base, page, file, **kw):
    d = copy.deepcopy(base)
    d.update(page=page, file=file, **kw)
    return d


def write_rec(out: Path, img: Path, page: int, status="accepted", decision="accept", queue=(), judgments=(),
              validation=(), analysis=None, **extra) -> dict:
    rec = {"page": page, "file": img.name, "status": status, "decision": decision, "attempts": [{"attempt": 1}],
           "analysis": analysis or an(TITLE_ONLY, page, img.name), "validation": list(validation),
           "score": {"decision": decision, "metrics": {}, "reasons": [], "triggers": [], "needs": []},
           "queue": list(queue), "judgments": list(judgments), **extra}
    (out / "results").mkdir(parents=True, exist_ok=True)
    (out / "results" / f"{img.stem}.json").write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    return rec


def read_rec(out: Path, img: Path) -> dict:
    return json.loads((out / "results" / f"{img.stem}.json").read_text(encoding="utf-8"))


def struct_entry(out: Path, img: Path, page: int, declared=0) -> dict:
    return cropmod.thumbnail(img, out / "crops", img.stem, page, "thumb_struct", "q", {"tables_declared": declared})


@contextlib.contextmanager
def quiet():
    with contextlib.redirect_stdout(io.StringIO()) as o, contextlib.redirect_stderr(io.StringIO()) as e:
        yield o, e


class TmpCase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.out = self.tmp / "out"
        (self.out / "results").mkdir(parents=True)

    def tearDown(self):
        self._td.cleanup()

    def deck(self, n=4, size=(320, 180), name="slides"):
        return synth.make_template_deck(self.tmp / name, n, size)


# --------------------------------------------------------------------------- validator
class ValidatorTests(unittest.TestCase):
    def codes(self, d, level=None):
        return {i["code"] for i in sv.validate_analysis(d, d.get("page", 1), d.get("file", "x.png")) if level in (None, i["level"])}

    def test_malformed_values_become_issues(self):
        d = an(CANNED, 1, "x.png", tables=[["a", "b"], ["1", "2"]])
        d["verbatim_text"].append({"region": "body", "text": None})
        self.assertIn("bad_table", self.codes(d, "error"))
        d = an(CANNED, 1, "x.png")
        d["reasoning"] = {"observation": None, "structure": None, "interpretation": None, "self_check": None}
        d["diagrams"] = ["원", {"kind": "illustration", "relations": None}]
        d["code_blocks"] = ["SELECT 1", {"code": None}]
        self.assertIn("reasoning_gap", self.codes(d))

    def test_interpretation_on_title_only(self):
        d = an(TITLE_ONLY, 1, "x.png", exam_points=["v-model 수식어 .lazy 활용"])
        self.assertIn("interpretation_on_title_only", self.codes(d, "warn"))
        d = an(TITLE_ONLY, 1, "x.png", key_concepts=[{"term": "관계형 모델", "explanation": "x"}] * 2)
        self.assertIn("interpretation_on_title_only", self.codes(d))
        self.assertNotIn("interpretation_on_title_only", self.codes(an(TITLE_ONLY, 1, "x.png")))

    def test_visual_undeclared(self):
        d = an(CANNED, 1, "x.png")
        d["reasoning"]["observation"] = "코드 박스 오른쪽에 개발자 도구 콘솔 캡처가 있다."
        self.assertIn("visual_undeclared", self.codes(d))
        d["diagrams"].append({"kind": "screenshot", "description": "콘솔", "elements": ["Console"], "relations": []})
        self.assertNotIn("visual_undeclared", self.codes(d))
        d = an(CANNED, 1, "x.png")
        d["reasoning"]["structure"] = "코드 한 줄에 빨간 박스 강조가 있다."
        self.assertIn("visual_undeclared", self.codes(d))

    def test_code_duplicated(self):
        d = an(CANNED, 1, "x.png")
        d["verbatim_text"].append({"region": "body", "text": "SELECT id, name FROM students"})
        self.assertIn("code_duplicated", self.codes(d))
        self.assertNotIn("code_duplicated", self.codes(an(CANNED, 1, "x.png")))

    def test_kind_and_divider_warnings(self):
        d = an(CANNED, 1, "x.png")
        d["diagrams"][0]["kind"] = "illustration"
        self.assertIn("illustration_with_relations", self.codes(d))
        d = an(TITLE_ONLY, 1, "x.png")
        d["verbatim_text"].append({"region": "body", "text": "DOM 요소에 이벤트 리스너를 연결하고 수신한다"})
        self.assertIn("divider_with_body", self.codes(d))
        self.assertNotIn("divider_with_body", self.codes(an(TITLE_ONLY, 1, "x.png")))

    def test_canned_analysis_raises_no_prompt_rule_warning(self):
        new = {"interpretation_on_title_only", "visual_undeclared", "code_duplicated", "illustration_with_relations",
               "divider_with_body"}
        for base in (CANNED, TITLE_ONLY):
            self.assertFalse(self.codes(an(base, 1, "x.png")) & new)

    def test_series_type_mixed_is_info(self):
        a1 = an(CANNED, 1, "a.png", title="반복문 (1/2)", slide_type="concept")
        a2 = an(CANNED, 2, "b.png", title="반복문 (2/2)", slide_type="code")
        a2["verbatim_text"][1]["text"] = "다른 본문 문장이 여기에 충분히 길게 들어간다고 가정한다."
        out = sv.cross_check({1: a1, 2: a2})
        self.assertEqual([i["level"] for i in out[1]], ["info"])
        self.assertEqual(out[2][0]["code"], "series_type_mixed")
        self.assertEqual(sv.cross_check({1: a1, 2: an(a2, 2, "b.png", slide_type="concept")}), {})


# --------------------------------------------------------------------------- analyst prompt
class PromptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.analyst = (SKILL / "prompts" / "analyst.md").read_text(encoding="utf-8")
        cls.reviewer = (SKILL / "prompts" / "reviewer.md").read_text(encoding="utf-8")

    def test_every_fewshot_marker_is_in_a_prompt(self):
        for m in sv.FEWSHOT_LEAKS:
            self.assertTrue(m in self.analyst or m in self.reviewer, m)

    def test_every_example_token_is_a_marker(self):
        for text in (self.analyst, self.reviewer):
            for mt in re.finditer(r"EXAMPLE \S+|example\w+", text):
                self.assertTrue(any(text.startswith(m, mt.start()) for m in sv.FEWSHOT_LEAKS), mt.group(0))

    def test_prompt_formats_and_its_examples_pass_the_validator(self):
        text = self.analyst.format(feedback="", page=1, file="a.png", total=3, subject="x", deck_context="y")
        blocks = re.findall(r"```json\n(.*?)\n```", text, re.S)[1:]
        self.assertEqual(len(blocks), 3)
        for b in blocks:
            d = json.loads(b)
            issues = sv.validate_analysis(d, d["page"], d["file"])
            self.assertEqual({i["code"] for i in issues}, {"fewshot_leak"}, (d["file"], issues))
        self.assertIn("{{ exampleCount }}", text)

    def test_prompt_enums_match_the_validator(self):
        kinds = re.search(r'"kind": "([a-z_ |]+)"', self.analyst).group(1)
        self.assertEqual({k.strip() for k in kinds.split("|")}, sv.DIAGRAM_KINDS)
        types = re.search(r'"slide_type": "([a-z_ |]+) 중 하나"', self.analyst).group(1)
        self.assertEqual({k.strip() for k in types.split("|")}, sv.SLIDE_TYPES)


# --------------------------------------------------------------------------- judging Claude's reads
def ref_with(lines, weight="mid"):
    out = []
    for i, text in enumerate(lines):
        words = [{"text": w, "conf": None, "box": [10, 10 + 40 * i, 30, 20], "weight": weight} for w in text.split()]
        out.append({"id": f"L{i + 1:03d}", "engine": "winocr", "text": text, "box": [10, 10 + 40 * i, 600, 20], "words": words})
    return {"lines": out}


class JudgeTests(unittest.TestCase):
    def entry(self, kind="thumb_read", hidden=None):
        return {"id": "e1", "page": 2, "stem": "s", "kind": kind, "path": "x", "px": 1000, "box": None,
                "question": "", "hidden": hidden or {}, "fp": "abc"}

    def test_full_read_numbers_must_match(self):
        a = an(CANNED, 2, "s.png")
        a["verbatim_text"][2]["text"] = "기본 키는 테이블당 하나만 존재한다. (2023년 기준 1,042개 행)"
        read = ["3장. 기본 키와 외래 키", "기본 키는 테이블당 하나만 존재한다. (2024년 기준 1,024개 행)"]
        j = cropmod.judge([self.entry()], {"e1": read}, a)[0]
        self.assertEqual(j["verdict"], "issue")
        self.assertEqual(j["wrong_digits"], ["1024", "2024"])
        self.assertEqual(j["fp"], "abc")
        self.assertEqual(cropmod.judge([self.entry()], {"e1": read}, an(CANNED, 2, "s.png"))[0]["verdict"], "ok")

    def test_full_read_identifiers_must_match(self):
        a = an(CANNED, 2, "s.png", code_blocks=[{"language": "sql", "code": "SELECT id, name FROM studnets WHERE grade = 'A';"}])
        j = cropmod.judge([self.entry("thumb_audit")], {"e1": "SELECT id, name FROM students WHERE grade = 'A';"}, a)[0]
        self.assertEqual((j["verdict"], j["wrong_identifiers"]), ("issue", ["students"]))

    def test_mid_weight_lines_enable_the_hallucination_check(self):
        lines = ["3장. 기본 키와 외래 키", "기본 키는 NULL을 허용하지 않는다.", "기본 키는 테이블당 하나만 존재한다."]
        ref = ref_with(lines)
        hidden = cropmod._hidden_for_read(ref, {"unsupported_units": [{"text": "무결성을"}, {"text": "보장하며"}]})
        self.assertEqual(hidden["ref_lines"], lines)
        self.assertEqual(cropmod._hidden_for_read(ref_with(lines, "low"), {})["ref_lines"], [])
        strong = ref_with(lines)
        strong["lines"][0]["words"][0]["weight"] = strong["lines"][0]["words"][1]["weight"] = "variant"
        self.assertEqual(cropmod._hidden_for_read(strong, {})["ref_lines"], lines[:1])
        a = an(CANNED, 2, "s.png")
        a["verbatim_text"].append({"region": "body", "text": "외래 키는 참조 무결성을 보장하며 복합 키로 구성될 수 있다."})
        j = cropmod.judge([self.entry("thumb_read", hidden)], {"e1": lines}, a)[0]
        self.assertEqual(j["verdict"], "issue")
        self.assertEqual(j["hallucination_suspects"], ["무결성을", "보장하며"])

    def test_boilerplate_and_curly_quotes_are_not_suspects(self):
        hidden = {"ref_lines": ["3장. 기본 키와 외래 키"], "unsupported": ["acmelogo", "‘alice’", "무결성을"]}
        a = an(CANNED, 2, "s.png")
        j = cropmod.judge([self.entry("thumb_read", hidden)], {"e1": ["3장. 기본 키와 외래 키", "name = 'alice'"]}, a,
                          boilerplate={"acmelogo"})[0]
        self.assertEqual(j["hallucination_suspects"], ["무결성을"])
        h = cropmod._hidden_for_read({}, {"unsupported_units": [{"text": "acmelogo"}, {"text": "“ab”"}, {"text": "새문장"}]},
                                     boilerplate={"acmelogo"})
        self.assertEqual(h["unsupported"], ["새문장"])

    def test_line_reading_checks_operators(self):
        a = an(CANNED, 2, "s.png", code_blocks=[{"language": "javascript", "code": "if (age > 30) {\n  total = 1\n}"}])
        j = cropmod.judge([self.entry("line")], {"e1": "if (age < 30) {"}, a)[0]
        self.assertEqual(j["verdict"], "agy_wrong")
        self.assertTrue(j["token_issues"])
        self.assertEqual(cropmod.judge([self.entry("line")], {"e1": "if (age > 30) {"}, a)[0]["verdict"], "agy_ok")


class FingerprintTests(TmpCase):
    def test_entries_fingerprint_what_claude_sees(self):
        img = self.deck(1)[0]
        e1, e2 = struct_entry(self.out, img, 1, 0), struct_entry(self.out, img, 1, 1)
        self.assertNotEqual(e1["fp"], e2["fp"])
        r1 = cropmod.thumbnail(img, self.out / "crops", img.stem, 1, "thumb_read", "q")
        r2 = cropmod.thumbnail(img, self.out / "crops", img.stem, 1, "thumb_read", "q")
        self.assertEqual(r1["fp"], r2["fp"])
        sc = {"missing_lines": [{"id": "L001", "text": "x", "box": [10, 10, 100, 20], "missing_words": ["x"]}], "needs": ["crops"]}
        self.assertTrue(cropmod.prepare(img, {}, sc, {}, self.out / "crops", img.stem, 1)[0]["fp"])


# --------------------------------------------------------------------------- report scope and audits
class ReportTests(TmpCase):
    def test_pages_selection_never_truncates_deck_files(self):
        slides = self.deck(4)
        for p, img in enumerate(slides, 1):
            if p == 3:
                write_rec(self.out, img, p, "escalated", "escalate", queue=[struct_entry(self.out, img, p)] * 2)
            else:
                write_rec(self.out, img, p)
        with quiet():
            counts = sa.build_report(slides, [2], self.out, ns())
        self.assertEqual(counts, {"accepted": 1})
        self.assertEqual(len(json.loads((self.out / "all_slides.json").read_text(encoding="utf-8"))), 4)
        queue = json.loads((self.out / "review_queue.json").read_text(encoding="utf-8"))
        self.assertEqual([e["id"] for e in queue], ["slide_003__thumb_struct"])  # deduplicated, from outside the selection
        report = (self.out / "report.md").read_text(encoding="utf-8")
        self.assertIn("Status: accepted=3, escalated=1", report)
        self.assertIn("This selection (--pages): accepted=1", report)
        self.assertIn("total tokens per item measured", report)

    def test_unreadable_result_is_reported_missing(self):
        slides = self.deck(2)
        write_rec(self.out, slides[0], 1)
        (self.out / "results" / "slide_002.json").write_text('{"page": 2, "sta', encoding="utf-8")
        with quiet() as (_, err):
            counts = sa.build_report(slides, [1, 2], self.out, ns())
        self.assertEqual(counts, {"accepted": 1, "missing": 1})
        self.assertIn("unreadable result slide_002.json", err.getvalue())

    def test_audits_sample_ocr_accepted_slides_only(self):
        slides = self.deck(4)
        write_rec(self.out, slides[0], 1)
        write_rec(self.out, slides[1], 2)
        write_rec(self.out, slides[2], 3, "accepted", "escalate",
                  queue=[{"id": "slide_003__thumb_read", "kind": "thumb_read", "page": 3, "stem": "slide_003"}],
                  judgments=[{"id": "slide_003__thumb_read", "kind": "thumb_read", "verdict": "ok"}])
        write_rec(self.out, slides[3], 4, "needs_attention", "retry")
        with quiet():
            sa.build_report(slides, [1, 2, 3, 4], self.out, ns(audit_rate=1.0))
        for img in slides[:2]:
            r = read_rec(self.out, img)
            self.assertTrue(r["audit"])
            self.assertEqual([e["id"] for e in r["queue"]], [f"{img.stem}__thumb_audit"])
        for img in slides[2:]:
            self.assertFalse(read_rec(self.out, img).get("audit"))
        report = (self.out / "report.md").read_text(encoding="utf-8")
        self.assertIn("Audit (OCR-accepted): no audit judged yet (2 accepted, 2 audit item(s) pending)", report)
        self.assertIn("Claude-accepted: 1 slide(s)", report)

    def test_audit_minimum_grows_with_the_accepted_count(self):
        slides = self.deck(50, size=(160, 90))
        for p, img in enumerate(slides, 1):
            write_rec(self.out, img, p)
        recs = sa.load_results(slides, self.out)
        with quiet():
            self.assertEqual(sa.pick_audits(slides, dict(list(recs.items())[:20]), self.out, 1e-9), 2)
            self.assertEqual(sa.pick_audits(slides, recs, self.out, 1e-9), 8)  # 2 already marked, 10 in all
            self.assertEqual(sa.pick_audits(slides, recs, self.out, 1e-9), 0)  # stable: nothing more on a re-run
        self.assertEqual(sum(1 for r in sa.load_results(slides, self.out).values() if r.get("audit")), 10)

    def test_upper_bound_only_when_informative(self):
        def recs(n_audited, n_bad=0):
            out = {}
            for p in range(1, 61):
                js = [{"id": "x", "kind": "thumb_audit", "verdict": "issue" if p <= n_bad else "ok"}] if p <= n_audited else []
                out[p] = {"decision": "accept", "status": "accepted", "analysis": {"x": 1}, "judgments": js, "queue": []}
            return out
        self.assertIn("too few audits", sa.audit_lines(recs(5))[0])
        self.assertIn("(upper ≈ 25%)", sa.audit_lines(recs(12))[0])
        self.assertIn("audited 12 of 60 accepted, 0 with issues", sa.audit_lines(recs(12))[0])
        self.assertIn("1 with issues", sa.audit_lines(recs(12, 1))[0])

    def shadow_deck(self, n=12, new_only=8):
        """n accepted slides; the first `new_only` are accepted only by the new rules (score_legacy escalates)."""
        slides = self.deck(n, size=(160, 90))
        for p, img in enumerate(slides, 1):
            write_rec(self.out, img, p, score_legacy={"decision": "escalate" if p <= new_only else "accept", "reasons": []})
        return slides

    def test_shadow_audits_sample_new_only_accepts(self):
        slides = self.shadow_deck()
        with quiet():
            self.assertEqual(sa.pick_audits(slides, sa.load_results(slides, self.out), self.out, 0.0, shadow_rate=0.0), 0)
            self.assertEqual(sa.pick_audits(slides, sa.load_results(slides, self.out), self.out, 0.0, shadow_rate=1.0), 8)
        for p, r in sa.load_results(slides, self.out).items():
            self.assertEqual(r.get("audit_stratum"), "new_only" if p <= 8 else None, p)
            if p <= 8:
                self.assertEqual([e["id"] for e in r["queue"]], [f"{slides[p - 1].stem}__thumb_audit"])
        # a stem-hash share, stable across re-runs
        self._td.cleanup()
        (self.out / "results").mkdir(parents=True)
        slides = self.shadow_deck()
        with quiet():
            sa.pick_audits(slides, sa.load_results(slides, self.out), self.out, 0.0, shadow_rate=0.3)
            self.assertEqual(sa.pick_audits(slides, sa.load_results(slides, self.out), self.out, 0.0, shadow_rate=0.3), 0)
        got = {p for p, r in sa.load_results(slides, self.out).items() if r.get("audit")}
        self.assertEqual(got, {p for p in range(1, 9) if sa.audit_hash(slides[p - 1].stem) < 0.3})

    def test_a_slide_is_audited_once_and_the_ocr_minimum_is_its_own(self):
        slides = self.shadow_deck()
        with quiet():
            self.assertEqual(sa.pick_audits(slides, sa.load_results(slides, self.out), self.out, 1.0, shadow_rate=1.0), 12)
        recs = sa.load_results(slides, self.out)
        self.assertEqual({r["audit_stratum"] for r in recs.values()}, {"ocr"})
        self.assertTrue(all(len(r["queue"]) == 1 for r in recs.values()))
        self._td.cleanup()
        (self.out / "results").mkdir(parents=True)
        slides = self.shadow_deck()
        with quiet():  # shadow audits first: the OCR stratum's minimum (2) is still drawn on top of them
            self.assertEqual(sa.pick_audits(slides, sa.load_results(slides, self.out), self.out, 0.0, shadow_rate=1.0), 8)
            self.assertEqual(sa.pick_audits(slides, sa.load_results(slides, self.out), self.out, 1e-9, shadow_rate=1.0), 2)
        strata = Counter(r.get("audit_stratum") for r in sa.load_results(slides, self.out).values())
        self.assertEqual(strata, Counter({"new_only": 8, "ocr": 2, None: 2}))

    def test_report_shows_the_new_only_stratum(self):
        def recs(legacy=True):
            out = {}
            for p in range(1, 61):
                js = [{"id": "x", "kind": "thumb_audit", "verdict": "issue" if p == 1 else "ok"}] if p <= 12 or 41 <= p <= 45 else []
                out[p] = {"decision": "accept", "status": "accepted", "analysis": {"x": 1}, "judgments": js, "queue": [],
                          "audit": bool(js), "audit_stratum": ("new_only" if p <= 12 else "ocr") if js else None}
                if legacy:
                    out[p]["score_legacy"] = {"decision": "escalate" if p <= 40 else "accept", "reasons": []}
            return out
        lines = sa.audit_lines(recs())
        self.assertIn("Audit (OCR-accepted): audited 5 of 60 accepted, 0 with issues", lines[0])  # its own sample only
        self.assertIn("Audit (new-rule-only accepts): audited 12 of 40 accepted, 1 with issues → estimated residual "
                      "error 8%", lines[2])
        self.assertEqual(lines[3], "Shadow mode (score_legacy): the scorer accepts 60 of 60 scored slide(s), its "
                                   "first-release rules 20; 40 accepted by the new rules only, 0 by the old rules only")
        self.assertEqual(len(sa.audit_lines(recs(legacy=False))), 2)  # records from before shadow mode
        slides = self.shadow_deck()
        with quiet():
            sa.build_report(slides, list(range(1, 13)), self.out, ns(shadow_audit_rate=0.0))
        report = (self.out / "report.md").read_text(encoding="utf-8")
        self.assertIn("Audit (new-rule-only accepts): no audit judged yet (8 accepted)", report)
        self.assertFalse(any(r.get("audit") for r in sa.load_results(slides, self.out).values()))

    def test_title_only_interpretation_goes_to_the_grounding_queue(self):
        slides = self.deck(2)
        write_rec(self.out, slides[0], 1, validation=[{"level": "warn", "code": "interpretation_on_title_only", "msg": ""}])
        write_rec(self.out, slides[1], 2)
        with quiet():
            sa.build_report(slides, [1, 2], self.out, ns())
        items = json.loads((self.out / "grounding_queue.json").read_text(encoding="utf-8"))
        self.assertEqual([i["page"] for i in items], [1])
        write_rec(self.out, slides[0], 1, grounding={"unsupported": [], "note": ""},
                  validation=[{"level": "warn", "code": "interpretation_on_title_only", "msg": ""}])
        with quiet():
            sa.build_report(slides, [1, 2], self.out, ns())
        self.assertFalse((self.out / "grounding_queue.json").exists())


# --------------------------------------------------------------------------- resolve: shards, conflicts, stale answers
class ResolveTests(TmpCase):
    def escalated(self, slides, pages):
        for p in pages:
            write_rec(self.out, slides[p - 1], p, "escalated", "escalate", queue=[struct_entry(self.out, slides[p - 1], p)])

    def resolve(self, pages):
        with quiet() as (o, e):
            rc = sa.cmd_resolve(self.slides, pages, self.out, ns())
        return rc, o.getvalue(), e.getvalue()

    def test_shards_are_disjoint_and_merged_back(self):
        self.slides = self.deck(4)
        self.escalated(self.slides, [1, 2, 3])
        write_rec(self.out, self.slides[3], 4)
        with quiet():
            sa.cmd_queue(self.slides, [1, 2, 3, 4], self.out, ns(shards=2))
        ids = []
        for k in (1, 2):
            md = (self.out / f"review_queue_{k}.md").read_text(encoding="utf-8")
            self.assertIn(f"crop_reads_{k}.json", md)
            got = re.findall(r"^## (\S+)", md, re.M)
            ids.append(got)
            (self.out / f"crop_reads_{k}.json").write_text(json.dumps({i: {"verdict": "ok"} for i in got}), encoding="utf-8")
        self.assertFalse(set(ids[0]) & set(ids[1]))
        self.assertEqual(sorted(ids[0] + ids[1]), [f"slide_00{p}__thumb_struct" for p in (1, 2, 3)])
        rc, _, _ = self.resolve([1, 2, 3, 4])
        self.assertEqual(rc, 0)
        self.assertEqual({read_rec(self.out, img)["status"] for img in self.slides}, {"accepted"})
        self.assertFalse(list(self.out.glob("review_queue*.md")))

    def test_conflicting_answers_stop_resolve(self):
        self.slides = self.deck(2)
        self.escalated(self.slides, [1])
        (self.out / "crop_reads.json").write_text(json.dumps({"slide_001__thumb_struct": {"verdict": "ok"}}), encoding="utf-8")
        (self.out / "crop_reads_2.json").write_text(json.dumps({"slide_001__thumb_struct": {"verdict": "issue"}}), encoding="utf-8")
        rc, _, err = self.resolve([1])
        self.assertEqual(rc, 5)
        self.assertIn("different answers", err)
        self.assertEqual(read_rec(self.out, self.slides[0])["status"], "escalated")
        (self.out / "crop_reads_2.json").unlink()
        self.assertEqual(self.resolve([1])[0], 0)
        # once the item is judged, an old shard file that disagrees about it no longer blocks resolve
        (self.out / "crop_reads_2.json").write_text(json.dumps({"slide_001__thumb_struct": {"verdict": "issue"}}), encoding="utf-8")
        self.assertEqual(self.resolve([1])[0], 0)
        self.assertEqual(read_rec(self.out, self.slides[0])["status"], "accepted")

    def test_answer_for_an_older_entry_is_ignored(self):
        self.slides = self.deck(2)
        img = self.slides[0]
        self.escalated(self.slides, [1])
        reads = self.out / "crop_reads.json"
        reads.write_text(json.dumps({"slide_001__thumb_struct": {"verdict": "ok"}}), encoding="utf-8")
        self.assertEqual(self.resolve([1])[0], 0)
        self.assertEqual(read_rec(self.out, img)["status"], "accepted")
        # a re-run changes what the question shows under the same id: the old answer must not settle it
        write_rec(self.out, img, 1, "escalated", "escalate", queue=[struct_entry(self.out, img, 1, declared=1)])
        rc, out, err = self.resolve([1])
        self.assertEqual((rc, json.loads(out)["stale_answers"]), (2, 1))
        self.assertIn("older version", err)
        self.assertEqual(read_rec(self.out, img)["status"], "escalated")
        reads.write_text(json.dumps({"slide_001__thumb_struct": {"verdict": "issue", "note": "표 없음"}}), encoding="utf-8")
        self.resolve([1])
        self.assertEqual(read_rec(self.out, img)["status"], "needs_attention")
        # the explicit form names the entry it answers
        e = struct_entry(self.out, img, 1, declared=2)
        write_rec(self.out, img, 1, "escalated", "escalate", queue=[e])
        reads.write_text(json.dumps({"slide_001__thumb_struct": {"answer": {"verdict": "ok"}, "fp": "0000"}}), encoding="utf-8")
        self.resolve([1])
        self.assertEqual(read_rec(self.out, img)["status"], "escalated")
        reads.write_text(json.dumps({"slide_001__thumb_struct": {"answer": {"verdict": "ok"}, "fp": e["fp"]}}), encoding="utf-8")
        self.resolve([1])
        self.assertEqual(read_rec(self.out, img)["status"], "accepted")

    def test_rescore_discards_answers_unless_the_entry_is_identical(self):
        self.slides = self.deck(2)
        img = self.slides[0]
        old = struct_entry(self.out, img, 1)
        legacy = {k: v for k, v in old.items() if k != "fp"}  # judged before fingerprints existed
        write_rec(self.out, img, 1, "accepted", "escalate", queue=[legacy],
                  judgments=[{"id": old["id"], "kind": "thumb_struct", "verdict": "ok"}])
        (self.out / "crop_reads.json").write_text(json.dumps({old["id"]: {"verdict": "ok"}}), encoding="utf-8")
        esc = {"decision": "escalate", "reasons": ["graphics_undeclared"], "metrics": {}, "triggers": ["graphics_undeclared"],
               "needs": ["thumbnail"]}
        with mock.patch.object(sa, "score", return_value=esc), quiet():
            sa.rescore(self.slides, [1], self.out, ns(), sa.load_thresholds(), lambda i, p: None)
        self.assertEqual(read_rec(self.out, img)["status"], "escalated")
        self.assertEqual(self.resolve([1])[0], 2)  # the old answer is not applied to the re-created question
        self.assertEqual(read_rec(self.out, img)["status"], "escalated")
        (self.out / "crop_reads.json").write_text(json.dumps({old["id"]: {"verdict": "ok", "note": "다시 봄"}}), encoding="utf-8")
        self.resolve([1])
        self.assertEqual(read_rec(self.out, img)["status"], "accepted")
        # with fingerprints on both sides, an identical question keeps its answer through a rescore
        with mock.patch.object(sa, "score", return_value=esc), quiet():
            sa.rescore(self.slides, [1], self.out, ns(), sa.load_thresholds(), lambda i, p: None)
        self.resolve([1])
        self.assertEqual(read_rec(self.out, img)["status"], "accepted")

    def test_resolve_passes_deck_boilerplate_to_judge(self):
        self.slides = self.deck(3)
        for p, img in enumerate(self.slides, 1):
            write_rec(self.out, img, p, "escalated" if p == 1 else "accepted", "escalate" if p == 1 else "accept",
                      queue=[struct_entry(self.out, img, p)] if p == 1 else [])
        (self.out / "crop_reads.json").write_text(json.dumps({"slide_001__thumb_struct": {"verdict": "ok"}}), encoding="utf-8")
        with mock.patch.object(cropmod, "judge", wraps=cropmod.judge) as j:
            self.resolve([1, 2, 3])
        self.assertIn("관계형", j.call_args.kwargs["boilerplate"])  # in all three analyses


# --------------------------------------------------------------------------- robustness, deck guard, references
class RobustnessTests(TmpCase):
    def test_write_atomic_and_tolerant_reads(self):
        f = self.tmp / "a.json"
        cropmod.write_atomic(f, '{"x": 1}')
        self.assertEqual(sa.read_json(f), {"x": 1})
        self.assertFalse(list(self.tmp.glob("*.tmp")))
        f.write_text('{"x": ', encoding="utf-8")
        self.assertEqual(sa.read_json(f, "d"), "d")
        f.write_bytes(b'{"x": "\xff\xfe"}')
        self.assertIsNone(sa.read_json(f))

    def test_pending_reason(self):
        img = self.deck(1)[0]
        with quiet():
            self.assertEqual(sa.pending_reason(self.out, img, 1), "new")
            write_rec(self.out, img, 1, sha256=ocr_ref.sha256_file(img))
            self.assertIsNone(sa.pending_reason(self.out, img, 1))
            self.assertEqual(sa.pending_reason(self.out, img, 1, force=True), "force")
            write_rec(self.out, img, 1, "failed", None)
            self.assertEqual(sa.pending_reason(self.out, img, 1), "failed")
            write_rec(self.out, img, 1, sha256="0" * 64)
            self.assertEqual(sa.pending_reason(self.out, img, 1), "stale")
            (self.out / "results" / f"{img.stem}.json").write_text("{", encoding="utf-8")
            self.assertEqual(sa.pending_reason(self.out, img, 1), "unreadable")

    def test_out_of_another_deck_is_refused(self):
        a = synth.make_deck(self.tmp / "a")
        b = self.deck(3, name="b")
        self.assertEqual(sa.check_deck(self.out, a), 0)
        write_rec(self.out, a[0], 1)
        with quiet() as (_, err):
            self.assertEqual(sa.check_deck(self.out, b), 5)
        self.assertIn("another deck", err.getvalue())
        grown = a + [synth.slide_with_footer(self.tmp / "a" / f"slide_00{k}.png", k) for k in (4, 5, 6, 7)]
        self.assertEqual(sa.check_deck(self.out, grown, write=False), 0)  # the deck grew: still the same deck
        self.assertEqual(sa.check_deck(self.out, a[:2] + b[:1]), 0)  # most images known: the same deck, partly changed
        self.assertEqual(sa.check_deck(self.out, b, force=True), 0)
        man = json.loads((self.out / sa.DECK_MANIFEST).read_text(encoding="utf-8"))
        self.assertEqual(set(man["files"].values()), {ocr_ref.sha256_file(p) for p in b})
        self.assertEqual(sa.DECK_MANIFEST, "slides_source.json")
        self.assertFalse((self.out / "source.json").exists())

    def test_rescore_and_force_may_rebuild_judged_references(self):
        img = self.deck(1)[0]
        with mock.patch.object(ocr_ref, "load_or_build", return_value={"x": 1}) as lob:
            sa.get_reference(img, self.out, ["e"])
            sa.get_reference(img, self.out, ["e"], rebuild=True)
            self.assertIsNone(sa.get_reference(img, self.out, []))
        self.assertEqual([c.kwargs["rebuild"] for c in lob.call_args_list], [False, True])

    def test_quota_on_retry_keeps_the_scored_attempt(self):
        slides = synth.make_deck(self.tmp / "slides")
        calls = []

        def fake_call(agy_task, wd, prompt, out_name, stage, a):
            calls.append(prompt)
            if len(calls) == 2:
                raise sa.QuotaExhausted("Resets in 2h")
            return an(CANNED, 2, slides[1].name, title="EXAMPLE SELECT 기본"), None

        a = ns(force=False, subject="s", context="c")
        with mock.patch.object(sa, "call_agy", fake_call), quiet():
            with self.assertRaises(sa.QuotaExhausted):
                sa.analyse("agy", slides[1], 2, self.out, self.tmp / "work", dict(page=2, file=slides[1].name, total=3,
                           subject="s", deck_context="c"), a, [], sa.load_thresholds(), refs={2: None})
        r = read_rec(self.out, slides[1])
        self.assertEqual((r["status"], r["decision"], len(r["attempts"])), ("needs_attention", "retry", 2))
        self.assertIn("quota", r["attempts"][1]["error"])
        self.assertTrue(r["analysis"])


class EngineAndRunTests(TmpCase):
    def test_single_engine_gets_variants(self):
        class E:
            def __init__(self, name):
                self.name = name
        with mock.patch.object(ocr_ref, "available_engines", return_value=[E("winocr")]), \
                mock.patch.object(ocr_ref, "with_variants", side_effect=lambda es, *a: es + [E("winocr_x2")]) as wv, quiet():
            self.assertEqual([e.name for e in sa.pick_engines(ns(engine="auto"))], ["winocr", "winocr_x2"])
        self.assertEqual(wv.call_count, 1)
        with mock.patch.object(ocr_ref, "available_engines", return_value=[E("winocr"), E("tesseract")]), \
                mock.patch.object(ocr_ref, "with_variants") as wv, quiet():
            sa.pick_engines(ns(engine="auto"))
        wv.assert_not_called()

    def run_main(self, slides, *extra, score=None, refs_log=None):
        """sa.main in-process with stubbed references and scorer; returns (rc, built-before-first-agy-call, score kwargs)."""
        built, at_first_call, seen = [], [], []

        def fake_ref(img, out, engines, rebuild=False):
            ref = {"version": 3, "file": img.name, "sha256": ocr_ref.sha256_file(img), "size": [320, 180],
                   "engines": [{"engine": "fake", "ok": True}], "lines": [], "quality": {"n_words": 0}}
            sa.ref_path(out, img).parent.mkdir(parents=True, exist_ok=True)
            sa.ref_path(out, img).write_text(json.dumps(ref), encoding="utf-8")
            built.append(img.name)
            return ref

        real_call = sa.call_agy

        def call(*a, **k):
            if not at_first_call:
                at_first_call.append(list(built))
            return real_call(*a, **k)

        def fake_score(analysis, ref, th=None, validator_errors=(), tkeys=None, boilerplate=None):
            if (th or {}).get("line_check", True):  # not the shadow view (score_legacy, every switch off)
                seen.append(tkeys)
            return {"decision": "accept", "reasons": ["reference agrees"], "metrics": {}, "triggers": [], "needs": []}

        env = {"AGY_TASK": str(FAKE), "FAKE_AGY_SCENARIO": "perfect"}
        sa.STOP.clear()
        with mock.patch.dict(os.environ, env), mock.patch.object(sa, "pick_engines", return_value=["engine"]), \
                mock.patch.object(sa, "get_reference", side_effect=fake_ref), mock.patch.object(sa, "call_agy", call), \
                mock.patch.object(sa, "score", side_effect=fake_score), \
                mock.patch.object(ocr_score, "template_keys", return_value={("footer", 49, 10)}) as tk, quiet():
            rc = sa.main(["run", "--slides", str(slides[0].parent), "--out", str(self.out), "--audit-rate", "0", "-j", "2", *extra])
        return rc, (at_first_call or [[]])[0], seen, tk, built

    def test_run_builds_every_reference_first_and_scores_with_template_keys(self):
        slides = self.deck(6)
        rc, first, seen, tk, built = self.run_main(slides, "--pages", "2")
        self.assertEqual(rc, 0)
        self.assertEqual(sorted(first), [p.name for p in slides])  # the whole deck, before the first agy call
        self.assertEqual(len(tk.call_args.args[0]), 6)
        self.assertEqual(seen, [{("footer", 49, 10)}] * 2)  # the analysis, then settle (same keys)
        # a second run: slide 2 is done and every reference exists, so nothing is rebuilt or re-analysed
        rc, first, seen, tk, built = self.run_main(slides, "--pages", "1-3")
        self.assertEqual(sorted(built), ["slide_001.png", "slide_003.png"])
        self.assertEqual(len(seen), 5)  # slides 1 and 3 analysed, then settle scores every analysed slide (1-3)

    def test_rescore_rebuilds_references_and_warns_on_old_calibration(self):
        slides = self.deck(2)
        write_rec(self.out, slides[0], 1, judgments=[{"id": "slide_001__L001", "kind": "line", "verdict": "agy_ok"}])
        (self.out / "calibration.json").write_text(json.dumps({"thresholds": {"recall_accept": 0.9}}), encoding="utf-8")
        with mock.patch.dict(os.environ, {"AGY_TASK": str(FAKE)}), mock.patch.object(sa, "pick_engines", return_value=["e"]), \
                mock.patch.object(sa, "get_reference", return_value=None) as gr, quiet() as (_, err):
            rc = sa.main(["rescore", "--slides", str(slides[0].parent), "--out", str(self.out), "--audit-rate", "0"])
        self.assertEqual(gr.call_args.kwargs, {"rebuild": True})
        self.assertIn("older scorer", err.getvalue())
        self.assertEqual(read_rec(self.out, slides[0])["judgments"], [])
        self.assertIn(rc, (0, 2))

    def test_run_leaves_the_reference_of_a_judged_slide_alone(self):
        slides = self.deck(3)
        write_rec(self.out, slides[0], 1, judgments=[{"id": "slide_001__L003", "kind": "line", "verdict": "agy_ok"}],
                  sha256=ocr_ref.sha256_file(slides[0]))
        sa.ref_path(self.out, slides[0]).parent.mkdir(parents=True)
        sa.ref_path(self.out, slides[0]).write_text('{"version": 2}', encoding="utf-8")
        rc, first, seen, tk, built = self.run_main(slides)
        self.assertEqual(sorted(built), ["slide_002.png", "slide_003.png"])
        self.assertEqual(sa.read_json(sa.ref_path(self.out, slides[0])), {"version": 2})


# --------------------------------------------------------------------------- CLI end to end (fake agy)
class CliTests(TmpCase):
    def setUp(self):
        super().setUp()
        self.slides = synth.make_deck(self.tmp / "slides")
        self.calls = self.tmp / "calls.log"

    def run_cli(self, *args, scenario="perfect", env=None, out=None):
        e = {k: v for k, v in os.environ.items() if k not in ("PYTHONUTF8", "PYTHONIOENCODING")}
        e.update(AGY_TASK=str(FAKE), FAKE_AGY_SCENARIO=scenario, FAKE_CALL_LOG=str(self.calls), OCR_ENGINE="tesseract",
                 PYTHONUTF8="1")
        e.update(env or {})
        cmd = [sys.executable, str(SCRIPTS / "slide_analyze.py"), *args, "--slides", str(self.slides[0].parent),
               "--out", str(out or self.out)]
        return subprocess.run(cmd, capture_output=True, env=e)

    def n_calls(self):
        return len(self.calls.read_text(encoding="utf-8").splitlines()) if self.calls.exists() else 0

    def test_cp949_pipe_gets_utf8_json_and_the_real_exit_code(self):
        for scenario, rc in (("perfect", 0), ("quota", 3)):
            out = self.tmp / f"2학기 – {scenario}" / "slide_analysis"  # '–' has no cp949 code
            cp = self.run_cli("run", "--pages", "1", scenario=scenario, out=out, env={"PYTHONIOENCODING": "cp949", "PYTHONUTF8": "0"})
            self.assertEqual(cp.returncode, rc, cp.stderr.decode("utf-8", "replace"))
            self.assertEqual(json.loads(cp.stdout.decode("utf-8"))["out"], str(out))

    def test_quota_on_retry_saves_the_paid_for_attempt(self):
        cp = self.run_cli("run", "--pages", "2", scenario="quota_on_retry")
        self.assertEqual(cp.returncode, 3, cp.stderr.decode("utf-8", "replace"))
        r = json.loads((self.out / "results" / "slide_002.json").read_text(encoding="utf-8"))
        self.assertEqual((r["status"], self.n_calls()), ("needs_attention", 2))
        self.run_cli("run", "--pages", "2")
        self.assertEqual(self.n_calls(), 2)  # kept: not re-sent to agy

    def test_truncated_result_is_redone_and_reports_still_work(self):
        self.run_cli("run")
        self.assertEqual(self.n_calls(), 3)
        f = self.out / "results" / "slide_001.json"
        f.write_text(f.read_text(encoding="utf-8")[:40], encoding="utf-8")
        cp = self.run_cli("run")
        self.assertIn("unreadable result slide_001.json", cp.stderr.decode("utf-8"))
        self.assertEqual(self.n_calls(), 4)
        self.assertTrue(sa.read_json(f))

    def test_pages_report_keeps_the_deck_files(self):
        self.run_cli("run")
        cp = self.run_cli("report", "--pages", "1")
        self.assertEqual(cp.returncode, 0, cp.stderr.decode("utf-8", "replace"))  # page 1 alone is accepted
        self.assertEqual(len(json.loads((self.out / "all_slides.json").read_text(encoding="utf-8"))), 3)

    def test_changed_image_is_redone_and_another_deck_refused(self):
        self.run_cli("run", "--pages", "1")
        synth.slide_with_footer(self.slides[0], 9)
        self.run_cli("run", "--pages", "1")
        self.assertEqual(self.n_calls(), 2)
        other = synth.make_template_deck(self.tmp / "other", 3, (320, 180))
        e = {k: v for k, v in os.environ.items() if k not in ("PYTHONUTF8", "PYTHONIOENCODING")}
        e.update(AGY_TASK=str(FAKE), OCR_ENGINE="tesseract", PYTHONUTF8="1")
        cmd = [sys.executable, str(SCRIPTS / "slide_analyze.py"), "run", "--slides", str(other[0].parent), "--out", str(self.out)]
        self.assertEqual(subprocess.run(cmd, capture_output=True, env=e).returncode, 5)
        self.assertIn(subprocess.run(cmd + ["--force", "--pages", "1"], capture_output=True, env=e).returncode, (0, 2))


if __name__ == "__main__":
    unittest.main()

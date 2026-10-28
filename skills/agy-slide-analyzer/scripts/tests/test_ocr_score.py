"""Unit tests for the evaluation layer (stdlib unittest).

    python -m unittest discover -s scripts/tests -v        (from the skill folder)

Two layers:
  * pure tests on a hand-built reference (always run)
  * tests on a synthetic slide through a real OCR engine (skipped when no engine is available) and the
    whole pipeline through tests/fake_agy_task.py (no agy needed)
"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(HERE))

import crops as cropmod  # noqa: E402
import ocr_ref  # noqa: E402
from fake_agy_task import CANNED  # noqa: E402
from ocr_score import DEFAULT_THRESHOLDS, feedback_text, gold_metrics, score, suggest_thresholds  # noqa: E402
from textnorm import digit_seqs, fuzzy_in, norm, units  # noqa: E402

ANALYSIS = dict(CANNED, page=2, file="slide_002.png")


def fake_reference(lines, conf=92.0):
    """A reference like ocr_ref would build, from [(text, box)] lines, every word weight high."""
    out, y = [], 10
    for i, (text, box) in enumerate(lines):
        words = []
        x = box[0]
        for w in text.split():
            words.append({"text": w, "conf": conf, "box": [x, box[1], 30, box[3]], "weight": "high", "agreed": False})
            x += 40
        out.append({"id": f"L{i + 1:03d}", "engine": "fake", "text": text, "conf": conf, "box": box, "words": words})
    n = sum(len(l["words"]) for l in out)
    return {"version": 1, "file": "x.png", "sha256": "0", "size": [1280, 720], "engines": [{"engine": "fake", "ok": True}],
            "primary": "fake", "lines": out, "text": {"fake": norm(" ".join(t for t, _ in lines))},
            "layout": {"available": True, "graphics_ratio": 0.01, "table_candidate": None, "ruled_table": False},
            "quality": {"n_words": n, "n_high": n, "n_mid": 0, "mean_conf": conf, "usable": n >= 5}}


REF = fake_reference([
    ("3 장 . 기본 키 와 외래 키", [60, 40, 500, 50]),
    ("기본 키 는 NULL 을 허용 하지 않는다 .", [60, 140, 500, 30]),
    ("기본 키 는 테 이 블 당 하나만 존 재 한다 . ( 2024 년 기준 1.024 개 행 )", [60, 190, 800, 30]),
    ("SELECT id, name FROM students WHERE grade = 'A';", [60, 280, 600, 26]),
    ("구분 파일 시스템 DBMS", [60, 360, 600, 25]),
    ("중복 높음 최소화", [60, 405, 600, 25]),
    ("무결성 응용 책임 제약 조건", [60, 450, 600, 25]),
])


class TextNormTests(unittest.TestCase):
    def test_norm_and_units(self):
        self.assertEqual(norm("SELECT id, name"), "selectidname")
        self.assertIn("null을", units("기본 키는 NULL을 허용하지"))

    def test_digit_separators(self):
        self.assertEqual(digit_seqs("1,024개 1.024 2024년"), ["1024", "1024", "2024"])

    def test_fuzzy_handles_syllable_splits(self):
        hay = norm("기본 키는 테이블당 하나만")
        from textnorm import bigrams
        self.assertTrue(fuzzy_in(norm("테 이 블 당"), hay, bigrams(hay), 0.75))


class ScoreTests(unittest.TestCase):
    def test_perfect_is_accepted(self):
        sc = score(ANALYSIS, REF)
        self.assertEqual(sc["decision"], "accept", sc["reasons"])
        self.assertGreaterEqual(sc["metrics"]["recall"], 0.95)
        self.assertEqual(sc["metrics"]["digit_recall"], 1.0)

    def test_omission_retries_and_names_the_line(self):
        a = copy.deepcopy(ANALYSIS)
        del a["verbatim_text"][2]
        a["code_blocks"] = []
        sc = score(a, REF)
        self.assertEqual(sc["decision"], "retry")
        ids = [m["id"] for m in sc["missing_lines"]]
        self.assertIn("L003", ids)
        self.assertIn("L004", ids)
        fb = feedback_text(sc)
        self.assertIn("SELECT", fb)
        self.assertIn("분석 결과에 없습니다", fb)

    def test_hallucination_escalates_with_thumbnail(self):
        a = copy.deepcopy(ANALYSIS)
        a["verbatim_text"].append({"region": "body", "text": "외래 키는 참조 무결성을 보장하며 복합 키로 구성될 수 있다."})
        sc = score(a, REF)
        self.assertEqual(sc["decision"], "escalate")
        self.assertIn("thumbnail", sc["needs"])
        self.assertTrue(any("무결성을" == u["text"] for u in sc["unsupported_units"]))

    def test_changed_digit_escalates_with_crop(self):
        a = copy.deepcopy(ANALYSIS)
        a["verbatim_text"][2]["text"] = "기본 키는 테이블당 하나만 존재한다. (2023년 기준 1,042개 행)"
        sc = score(a, REF)
        self.assertEqual(sc["decision"], "escalate")
        self.assertIn("crops", sc["needs"])
        self.assertEqual(sc["missing_lines"][0]["id"], "L003")
        self.assertIn("2023", sc["extra_digits"])

    def test_validator_error_forces_retry(self):
        sc = score(ANALYSIS, REF, validator_errors=[{"level": "error", "code": "fewshot_leak", "msg": "x"}])
        self.assertEqual(sc["decision"], "retry")

    def test_table_candidate_trigger(self):
        ref = copy.deepcopy(REF)
        ref["layout"]["table_candidate"] = {"rows": 3, "cols": 3}
        a = copy.deepcopy(ANALYSIS)
        a["tables"] = []
        a["verbatim_text"].append({"region": "body", "text": "구분 파일 시스템 DBMS 중복 높음 최소화 무결성 응용 책임 제약 조건"})
        sc = score(a, ref)
        self.assertIn("table_candidate_undeclared", sc["triggers"])
        self.assertEqual(sc["decision"], "escalate")

    def test_mid_confidence_misread_does_not_fail_digits(self):
        ref = copy.deepcopy(REF)
        ref["lines"][4]["words"][3].update(text="08146", weight="mid", conf=65.0)  # DBMS misread
        sc = score(ANALYSIS, ref)
        self.assertEqual(sc["decision"], "accept", sc["reasons"])

    def test_no_reference(self):
        self.assertEqual(score(ANALYSIS, None)["decision"], "unverified")
        self.assertEqual(score({"title": "x", "verbatim_text": []}, None)["decision"], "accept")

    def test_thin_reference_with_rich_analysis(self):
        ref = fake_reference([("2부", [10, 10, 50, 50])])
        sc = score(ANALYSIS, ref)
        self.assertEqual(sc["decision"], "escalate")
        self.assertIn("thumbnail", sc["needs"])  # plus a crop of the OCR line the analysis lacks
        old = score(ANALYSIS, ref, dict(DEFAULT_THRESHOLDS, close_thin_shortcut=False))
        self.assertEqual((old["decision"], old["needs"]), ("escalate", ["thumbnail"]))

    def test_gold_metrics_and_threshold_suggestion(self):
        a = copy.deepcopy(ANALYSIS)
        del a["verbatim_text"][2]
        t = gold_metrics(ANALYSIS, a)
        self.assertLess(t["true_recall"], 0.9)
        self.assertEqual(t["true_precision"], 1.0)
        rows = [{"proxy": score(ANALYSIS, REF), "truth": gold_metrics(ANALYSIS, ANALYSIS)},
                {"proxy": score(a, REF), "truth": t}]
        s = suggest_thresholds(rows)
        self.assertNotIn("error", s)
        self.assertEqual(s["false_accepts"], 0)


class JudgeTests(unittest.TestCase):
    def entry(self, kind="line", hidden=None):
        return {"id": "e1", "page": 2, "stem": "s", "kind": kind, "path": "x", "px": 1000, "box": None,
                "question": "", "hidden": hidden or {}}

    def test_line_agrees(self):
        j = cropmod.judge([self.entry()], {"e1": "기본 키는 테이블당 하나만 존재한다. (2024년 기준 1,024개 행)"}, ANALYSIS)
        self.assertEqual(j[0]["verdict"], "agy_ok")

    def test_line_digit_mismatch_is_wrong(self):
        a = copy.deepcopy(ANALYSIS)
        a["verbatim_text"][2]["text"] = "기본 키는 테이블당 하나만 존재한다. (2023년 기준 1,042개 행)"
        j = cropmod.judge([self.entry()], {"e1": "기본 키는 테이블당 하나만 존재한다. (2024년 기준 1,024개 행)"}, a)
        self.assertEqual(j[0]["verdict"], "agy_wrong")
        self.assertEqual(j[0]["wrong_digits"], ["1024", "2024"])

    def test_unreadable(self):
        j = cropmod.judge([self.entry()], {"e1": "[?]"}, ANALYSIS)
        self.assertEqual(j[0]["verdict"], "unreadable")
        self.assertEqual(cropmod.page_verdict(j), "accepted")

    def test_thumbnail_read_finds_hallucination(self):
        a = copy.deepcopy(ANALYSIS)
        a["verbatim_text"].append({"region": "body", "text": "외래 키는 참조 무결성을 보장하며 복합 키로 구성될 수 있다."})
        sc = score(a, REF)
        hidden = cropmod._hidden_for_read(REF, sc)
        reads = {"e1": [l["text"] for l in REF["lines"]]}
        j = cropmod.judge([self.entry("thumb_read", hidden)], reads, a)
        self.assertEqual(j[0]["verdict"], "issue")
        self.assertTrue(j[0]["hallucination_suspects"])

    def test_thumbnail_read_ok(self):
        sc = score(ANALYSIS, REF)
        j = cropmod.judge([self.entry("thumb_audit", cropmod._hidden_for_read(REF, sc))],
                          {"e1": "3장. 기본 키와 외래 키\n기본 키는 NULL을 허용하지 않는다."}, ANALYSIS)
        self.assertEqual(j[0]["verdict"], "ok")


@unittest.skipUnless(ocr_ref.Image is not None, "Pillow missing")
class EngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import synth
        cls.tmp = Path(tempfile.mkdtemp())
        cls.img = synth.slide_main(cls.tmp / "slide_002.png")
        cls.engines = ocr_ref.available_engines()

    def test_each_engine_reads_the_slide(self):
        if not self.engines:
            self.skipTest("no OCR engine available")
        # a single engine may read the table badly (Windows OCR does on this slide); then a correct analysis
        # may escalate to a thumbnail, but it must never be sent back for a retry, and an omission must
        # never be accepted
        for e in self.engines:
            with self.subTest(engine=e.name):
                ref = ocr_ref.build_reference(self.img, [e])
                self.assertTrue(ref["quality"]["usable"], ref["engines"])
                sc = score(ANALYSIS, ref)
                self.assertIn(sc["decision"], ("accept", "escalate"), (e.name, sc["reasons"], sc["missing_lines"]))
                a = copy.deepcopy(ANALYSIS)
                del a["verbatim_text"][2]
                a["code_blocks"] = []
                sc2 = score(a, ref)
                self.assertIn(sc2["decision"], ("retry", "escalate"))
                self.assertLess(sc2["metrics"]["recall"], sc["metrics"]["recall"])

    def test_two_engines_accept_the_perfect_analysis(self):
        if len(self.engines) < 2:
            self.skipTest("needs both OCR engines")
        ref = ocr_ref.build_reference(self.img, self.engines)
        sc = score(ANALYSIS, ref)
        self.assertEqual(sc["decision"], "accept", (sc["reasons"], sc["missing_lines"]))

    def test_ensemble_and_layout(self):
        if not self.engines:
            self.skipTest("no OCR engine available")
        ref = ocr_ref.build_reference(self.img, self.engines)
        self.assertTrue(ref["layout"]["available"])
        self.assertTrue(ref["layout"]["title_band"])
        self.assertIn("table_candidate", ref["layout"])
        if len(self.engines) > 1:
            # one engine may garble the small table text (Windows OCR does here), so only the ensemble must see the grid
            self.assertIsNotNone(ref["layout"]["table_candidate"])
            self.assertTrue(any(w["agreed"] for l in ref["lines"] for w in l["words"]))

    def test_crops_are_written(self):
        if not self.engines:
            self.skipTest("no OCR engine available")
        ref = ocr_ref.build_reference(self.img, self.engines[:1])
        a = copy.deepcopy(ANALYSIS)
        a["verbatim_text"][2]["text"] = "기본 키는 테이블당 하나만 존재한다. (2023년 기준 1,042개 행)"
        sc = score(a, ref)
        entries = cropmod.prepare(self.img, ref, sc, a, self.tmp / "crops", "slide_002", 2)
        self.assertTrue(entries and all(Path(e["path"]).exists() for e in entries))
        self.assertGreater(cropmod.token_estimate(entries), 0)


@unittest.skipUnless(ocr_ref.Image is not None and ocr_ref.available_engines(), "needs Pillow and an OCR engine")
class PipelineTests(unittest.TestCase):
    """slide_analyze.py end to end with the fake agy: omit -> retry -> accepted; digits -> escalate -> resolve."""

    def run_cli(self, *args, scenario="perfect"):
        env = dict(os.environ, AGY_TASK=str(HERE / "fake_agy_task.py"), FAKE_AGY_SCENARIO=scenario, PYTHONUTF8="1")
        return subprocess.run([sys.executable, str(SCRIPTS / "slide_analyze.py"), *args], capture_output=True, env=env, text=True,
                              encoding="utf-8")

    def setUp(self):
        import synth
        self.tmp = Path(tempfile.mkdtemp())
        self.slides = self.tmp / "slides"
        synth.make_deck(self.slides)
        self.out = self.tmp / "out"

    def rec(self):
        return json.loads((self.out / "results" / "slide_002.json").read_text(encoding="utf-8"))

    def test_omit_then_retry(self):
        cp = self.run_cli("run", "--slides", str(self.slides), "--pages", "2", "--out", str(self.out), "--audit-rate", "0", scenario="omit")
        r = self.rec()
        self.assertEqual(len(r["attempts"]), 2)
        self.assertEqual(r["attempts"][0]["decision"], "retry")
        if len(ocr_ref.available_engines()) > 1:
            self.assertEqual(cp.returncode, 0, cp.stderr)
            self.assertEqual(r["status"], "accepted")
        else:
            # one engine misreads the table, so the perfect retry may still need a look; it must never retry again
            self.assertIn(r["attempts"][1]["decision"], ("accept", "escalate"), cp.stderr)
            self.assertIn(cp.returncode, (0, 2), cp.stderr)
            self.assertIn(r["status"], ("accepted", "escalated"))

    def test_digits_escalate_and_resolve(self):
        cp = self.run_cli("run", "--slides", str(self.slides), "--pages", "2", "--out", str(self.out), "--audit-rate", "0", scenario="digits")
        self.assertEqual(cp.returncode, 2, cp.stderr)
        r = self.rec()
        self.assertEqual(r["status"], "escalated")
        self.assertTrue((self.out / "review_queue.md").exists())
        reads = {e["id"]: "기본 키는 테이블당 하나만 존재한다. (2024년 기준 1,024개 행)" for e in r["queue"]}
        (self.out / "crop_reads.json").write_text(json.dumps(reads, ensure_ascii=False), encoding="utf-8")
        cp = self.run_cli("resolve", "--slides", str(self.slides), "--pages", "2", "--out", str(self.out), "--audit-rate", "0")
        self.assertEqual(self.rec()["status"], "needs_attention", cp.stderr)
        self.assertFalse((self.out / "review_queue.md").exists())

    def test_quota_leaves_slide_pending(self):
        cp = self.run_cli("run", "--slides", str(self.slides), "--pages", "2", "--out", str(self.out), scenario="quota")
        self.assertEqual(cp.returncode, 3)
        self.assertFalse((self.out / "results" / "slide_002.json").exists())


if __name__ == "__main__":
    unittest.main()

"""The contract between this folder's shared evaluation code and the sibling skills that import it (stdlib unittest).

agy-pdf-analyzer (pdf_analyze.py) and lecture-knowledge-pack (kp_*.py) import textnorm, ocr_score, ocr_ref, crops and
slide_analyze from here instead of copying them, so a renamed function, a dropped argument or a changed reason string
breaks them without a single failing test in this skill. This file pins exactly what they use:

  * signatures: every call a sibling makes must still bind (inspect.signature(...).bind)
  * textnorm golden values: kp keys its concept registry and code comparisons on norm() (which strips operators)
  * vocabularies: decisions, statuses, judgment verdicts, word weights, reason strings kp intersects with
  * formats: judgment / queue ids '<stem>__<line_id>', corpus_parts paths, results/ and ref/ fields
  * end to end: a fake-agy run whose results/ref files go through kp_ingest + kp_regions, and a 3-page image PDF
    through pdf_analyze (each skipped when that sibling is not next to this skill)
  * the REF_VERSION 3 / scorer v2 additions, all additive: textnorm fold / ocr_key / skel; layout v3 keys
    (graphics_ratio_nocode, code_boxes, table_grid2, ruled_table2, text_masked); variant OCR (weight 'variant',
    never 'high'; available_engines() still lists real engines only); score(tkeys=), template_keys, the token
    checks; the threshold switches; deck files covering every result; queue fingerprints; slides_source.json
  * deck header/footer boilerplate (score(boilerplate=), hf_boilerplate, the hf_boilerplate switch, settle) and
    shadow mode (score_legacy on every scored record, --shadow-audit-rate, audit_stratum), all additive

    python -m unittest discover -s scripts/tests -v        (from the skill folder)
"""
from __future__ import annotations

import argparse
import contextlib
import inspect
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
SKILLS = SCRIPTS.parent.parent
KP_SCRIPTS = SKILLS / "lecture-knowledge-pack" / "scripts"
PDF_SKILL = SKILLS / "agy-pdf-analyzer"
os.environ["OCR_EVAL_DIR"] = str(SCRIPTS)  # siblings imported below must bind to this folder, not an installed copy
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(HERE))

import crops as cropmod  # noqa: E402
import ocr_ref  # noqa: E402
import ocr_score  # noqa: E402
import slide_analyze as sa  # noqa: E402
import slide_validate  # noqa: E402
import textnorm  # noqa: E402
from fake_agy_task import CANNED  # noqa: E402

HAVE_KP = (KP_SCRIPTS / "kp_ingest.py").is_file()
HAVE_PDF = (PDF_SKILL / "scripts" / "pdf_analyze.py").is_file()
HAVE_IMG = ocr_ref.Image is not None

DECISIONS = {"accept", "retry", "escalate", "unverified"}
STATUSES = {"accepted", "escalated", "needs_attention", "unverified", "failed", "ok", "unreviewed"}  # kp_ingest maps these
VERDICTS = {"agy_ok", "agy_wrong", "unreadable", "ok", "issue"}
WEIGHTS = {"high", "mid", "low", "variant"}  # 'variant' (one recognizer at two scales) is never 'high'
TEXT_CHECKS = {"claude_verified", "thin_accepted", "ocr_verified", "review_pending", "needs_attention", "unverified",
               "image_only", "legacy_unverified"}
THIN_REASON, NOREF_REASON = "little text on both sides", "no_reference_little_text"  # kp_ingest.THIN_REASONS
PART_RE = re.compile(r"^(title|verbatim_text\[\d+\]|tables\[\d+\]\.(caption|headers\[\d+\]|rows\[\d+\]\[\d+\])|"
                     r"code_blocks\[\d+\]\.code|diagrams\[\d+\]\.elements\[\d+\])$")
ANALYSIS = dict(CANNED, page=2, file="slide_002.png")


def binds(fn, *args, **kw):
    """Raises TypeError when the call a sibling makes no longer fits the signature."""
    return inspect.signature(fn).bind(*args, **kw)


def fake_reference(lines, weight="high"):
    """A reference shaped like ocr_ref's, from [(text, [l, t, w, h])], one word box per whitespace token."""
    out = []
    for i, (text, box) in enumerate(lines):
        words, x = [], box[0]
        for w in text.split():
            words.append({"text": w, "conf": 92.0, "box": [x, box[1], 30, box[3]], "weight": weight, "agreed": True})
            x += 40
        out.append({"id": f"L{i + 1:03d}", "engine": "fake", "text": text, "conf": 92.0, "box": box, "words": words})
    n = sum(len(l["words"]) for l in out)
    return {"version": ocr_ref.REF_VERSION, "file": "x.png", "sha256": "0", "size": [1280, 720],
            "engines": [{"engine": "fake", "ok": True}], "primary": "fake", "lines": out,
            "text": {"fake": textnorm.norm(" ".join(t for t, _ in lines))},
            "layout": {"available": True, "graphics_ratio": 0.01, "table_candidate": None, "ruled_table": False},
            "quality": {"n_words": n, "n_high": n, "n_mid": 0, "mean_conf": 92.0, "usable": n >= 5}}


REF = fake_reference([
    ("3 장 . 기본 키 와 외래 키", [60, 40, 500, 50]),
    ("기본 키 는 NULL 을 허용 하지 않는다 .", [60, 140, 500, 30]),
    ("기본 키 는 테 이 블 당 하나만 존 재 한다 . ( 2024 년 기준 1.024 개 행 )", [60, 190, 800, 30]),
    ("SELECT id, name FROM students WHERE grade = 'A';", [60, 280, 600, 26]),
    ("구분 파일 시스템 DBMS", [60, 360, 600, 25]),
    ("중복 높음 최소화", [60, 405, 600, 25]),
    ("무결성 응용 책임 제약 조건", [60, 450, 600, 25]),
])
DIGITS_TEXT = "기본 키는 테이블당 하나만 존재한다. (2023년 기준 1,042개 행)"


def digits_analysis():
    a = json.loads(json.dumps(ANALYSIS))
    a["verbatim_text"][2]["text"] = DIGITS_TEXT
    return a


def run_env(scenario="perfect"):
    return dict(os.environ, AGY_TASK=str(HERE / "fake_agy_task.py"), FAKE_AGY_SCENARIO=scenario, PYTHONUTF8="1",
                OCR_EVAL_DIR=str(SCRIPTS), PYTHONDONTWRITEBYTECODE="1")


def run_cli(script, *args, scenario="perfect"):
    return subprocess.run([sys.executable, str(script), *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=run_env(scenario))


def answer_queue(queue: list) -> dict:
    """Plausible blind answers for every queue entry (what Claude would write into crop_reads.json)."""
    reads = {}
    for e in queue:
        if e["kind"] == "line":
            reads[e["id"]] = (e.get("hidden") or {}).get("ocr_text") or "[?]"
        elif e["kind"] == "thumb_struct":
            reads[e["id"]] = {"verdict": "ok", "note": ""}
        else:
            reads[e["id"]] = list((e.get("hidden") or {}).get("ref_lines") or []) or ["3장. 기본 키와 외래 키"]
    return reads


# --------------------------------------------------------------------------- signatures
class SignatureTests(unittest.TestCase):
    """The exact calls agy-pdf-analyzer and lecture-knowledge-pack make (argument counts and keywords)."""

    def test_slide_analyze(self):
        x = object()
        binds(sa.build_report, [x], [1], x, x)
        binds(sa.build_report, [x], [1], x)
        binds(sa.cmd_resolve, [x], [1], x, x)
        binds(sa.cmd_calibrate, [x], [1], x, x, {})
        binds(sa.cmd_bench, [x], [1], x, x)
        binds(sa.rescore, [x], [1], x, x, {}, lambda img, pg: None, lambda an, pg, f: [])
        binds(sa.pick_engines, x)
        binds(sa.read_json, x)
        binds(sa.read_json, x, [])

    def test_read_json_is_tolerant(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "bad.json").write_text('{"page": 1, "sta', encoding="utf-8")
        (tmp / "ok.json").write_text('{"page": 1}', encoding="utf-8")
        self.assertEqual(sa.read_json(tmp / "missing.json", []), [])
        self.assertIsNone(sa.read_json(tmp / "bad.json"))
        self.assertEqual(sa.read_json(tmp / "ok.json"), {"page": 1})

    def test_crops(self):
        x = object()
        binds(cropmod.prepare, x, {}, {}, {}, x, "stem", 1)
        binds(cropmod.token_estimate, [])
        binds(cropmod.judge, [], {}, {})
        binds(cropmod.page_verdict, [])
        self.assertEqual(cropmod.token_estimate([{"px": 7500}, {"px": 750}]), 11)
        self.assertIsInstance(cropmod.token_estimate([]), int)

    def test_ocr_ref(self):
        x = object()
        self.assertTrue(hasattr(ocr_ref, "Image"))  # pdf_analyze: `ocr_ref.Image is not None` / Image.open
        binds(ocr_ref.layout_stats, x, [])
        binds(ocr_ref.load_or_build, x, x, [])
        binds(ocr_ref.make_engines)
        binds(ocr_ref.available_engines)
        binds(ocr_ref.pdf_page_image, x, 1)
        binds(ocr_ref.pdf_page_text, x, 1)
        binds(ocr_ref.text_reference, "text", "lecture.pdf")
        for e in ocr_ref.make_engines():
            self.assertTrue(callable(e.available) and callable(e.info) and isinstance(e.name, str))

    def test_available_engines_lists_real_engines_only(self):
        # knowledge_pack preflight warns 'one OCR engine only' from this list: scale variants must not appear in it
        names = {e.name for e in ocr_ref.available_engines()}
        self.assertLessEqual(names, set(ocr_ref.ENGINE_ORDER))

    def test_ocr_score(self):
        x = object()
        binds(ocr_score.score, {}, None, {}, validator_errors=[])
        binds(ocr_score.score, {}, None)
        binds(ocr_score.feedback_text, {}, [])
        binds(ocr_score.load_thresholds, None, x, x)
        binds(ocr_score.corpus_parts, {})
        binds(ocr_score.corpus_text, {})
        th = ocr_score.load_thresholds(None, Path(tempfile.mkdtemp()) / "none.json")
        for k in ("recall_accept", "unsupported_accept", "min_ref_weight", "rich_corpus_chars", "graphics_trigger", "weights"):
            self.assertIn(k, th)
        self.assertNotIn("_source", th)

    def test_slide_validate(self):
        binds(slide_validate.validate_analysis, {}, 1, "f.png")
        binds(slide_validate.cross_check, {})
        self.assertTrue(slide_validate.FEWSHOT_LEAKS)


# --------------------------------------------------------------------------- textnorm golden values
class TextnormGoldenTests(unittest.TestCase):
    """Byte-identical behaviour: kp_compare / kp_regions rely on norm() stripping operators, and kp keys its concept
    registry on norm(). OCR look-alike folding belongs in separate helpers, never in these."""

    PUNCT_SHA = "b13543ca402f1d120907cef9509ddd8e146c1c3c8f5c9d9af9f96b4f70f53bbf"  # of PUNCT_RE.pattern (escapes as written)
    STRIPPED = [0xB7, 0x2022, 0x2026, 0x203B, 0x2192, 0x2190, 0x2191, 0x2193, 0x300C, 0x300D, 0x300E, 0x300F,
                0x3010, 0x3011, 0x3008, 0x3009, 0x300A, 0x300B, 0xFF08, 0xFF09, 0xFF0C, 0xFF0E]
    # dashes, curly quotes, O-slash, 十; U+318D survives too (NFKC turns it into U+119E before the strip)
    KEPT = [0x2013, 0x2014, 0x2212, 0x201C, 0x201D, 0x2018, 0x2019, 0xD8, 0x5341]

    def test_punct_pattern(self):
        import hashlib
        self.assertEqual(hashlib.sha256(textnorm.PUNCT_RE.pattern.encode("utf-8")).hexdigest(), self.PUNCT_SHA)
        for c in self.STRIPPED:
            self.assertEqual(textnorm.norm(f"a{chr(c)}b"), "ab", hex(c))
        for c in self.KEPT:  # look-alike folding lives in textnorm.fold / ocr_key, never in norm
            self.assertEqual(textnorm.norm(f"a{chr(c)}b"), f"a{chr(c)}b".lower(), hex(c))
        self.assertEqual(textnorm.norm(f"a{chr(0x318D)}b"), f"a{chr(0x119E)}b")

    def test_norm(self):
        golden = {
            "age > 30": "age30", "age < 30": "age30",
            "if (a === b && c !== 0) { x++ }": "ifabc0x",
            "기본 키는 NULL을 허용하지 않는다.": "기본키는null을허용하지않는다",
            "v—show – 〈div〉 “quoted” ‘x’ Ø 十 −5": "v—show–div“quoted”‘x’ø十−5",
            "ＡＢＣ１２３ ㎏": "abc123kg",
            "EnabIing Pe0Ple He110": "enabiingpe0plehe110",
            "a|b ※ → ← · • ㆍ …": "abᆞ",
        }
        for s, want in golden.items():
            self.assertEqual(textnorm.norm(s), want, s)
        self.assertEqual(textnorm.nfkc("ＡＢＣ１２３ ㎏"), "ABC123 kg")

    def test_units_digits_latin(self):
        self.assertEqual(textnorm.units("1,024개 1.024 2024년 3.14 10:30"), ["024개", "024", "2024년", "14", "10", "30"])
        self.assertEqual(textnorm.units("v—show – 〈div〉 “quoted”"), ["v—show", "div", "“quoted”"])
        self.assertEqual(textnorm.digit_seqs("1,024개 1.024 2024년 3.14 10:30"), ["1024", "1024", "2024", "314", "1030"])
        self.assertEqual(textnorm.digit_seqs("EnabIing Pe0Ple He110 tit1e my_var2"), ["0", "110", "1", "2"])
        self.assertEqual(textnorm.latin_tokens("EnabIing Pe0Ple He110 tit1e my_var2 ab"),
                         ["enabiing", "pe0ple", "he110", "tit1e", "my_var2"])

    def test_bigrams_overlap_fuzzy(self):
        self.assertEqual(textnorm.bigrams("age30"), {"ag", "ge", "e3", "30"})
        self.assertEqual(textnorm.bigrams("a"), {"a"})
        self.assertEqual(textnorm.bigrams(""), set())
        hay = textnorm.norm("기본 키는 테이블당 하나만 존재한다")
        hb = textnorm.bigrams(hay)
        self.assertEqual(textnorm.overlap(textnorm.norm("존재함"), hb), 0.5)
        self.assertTrue(textnorm.fuzzy_in(textnorm.norm("테 이 블 당"), hay, hb, 0.75))
        self.assertFalse(textnorm.fuzzy_in(textnorm.norm("존재함"), hay, hb, 0.75))
        self.assertFalse(textnorm.fuzzy_in("", hay, hb, 0.1))


# --------------------------------------------------------------------------- vocabularies and formats
class VocabularyTests(unittest.TestCase):
    def test_no_reference_reasons(self):
        sc = ocr_score.score({"title": "x", "verbatim_text": []}, None)
        self.assertEqual((sc["decision"], sc["reasons"]), ("accept", [NOREF_REASON]))
        self.assertEqual(ocr_score.score(ANALYSIS, None)["decision"], "unverified")

    def test_thin_reference_accept_keeps_the_kp_reason(self):
        ref = fake_reference([("2부 관계형 모델", [300, 300, 600, 64])])
        an = dict(ANALYSIS, slide_type="section_divider", title="2부. 관계형 모델", tables=[], code_blocks=[], diagrams=[],
                  verbatim_text=[{"region": "title", "text": "2부. 관계형 모델"}])
        sc = ocr_score.score(an, ref)
        self.assertEqual(sc["decision"], "accept", sc["reasons"])
        self.assertIn(THIN_REASON, sc["reasons"])

    def test_score_shape(self):
        for an, ref in ((ANALYSIS, REF), (digits_analysis(), REF), (ANALYSIS, None)):
            sc = ocr_score.score(an, ref)
            self.assertIn(sc["decision"], DECISIONS)
            for k in ("metrics", "reasons", "triggers", "needs", "missing_lines", "unsupported_units"):
                self.assertIn(k, sc)
            for k in ("recall", "unsupported_ratio", "digit_recall", "corpus_chars"):
                self.assertIn(k, sc["metrics"])
            self.assertLessEqual(set(sc["needs"]), {"crops", "thumbnail"})

    def test_corpus_parts_paths(self):
        an = dict(ANALYSIS, tables=[{"caption": "c", "headers": ["h"], "rows": [["r"]]}])
        parts = ocr_score.corpus_parts(an)
        self.assertTrue(parts)
        for part in parts:
            self.assertEqual(len(part), 3)
            path, text, kind = part
            self.assertRegex(path, PART_RE)
            self.assertIsInstance(text, str)
            self.assertIn(kind, {"text", "table", "code", "diagram"})
        self.assertEqual(ocr_score.corpus_text(an), " ".join(p[1] for p in parts))

    def test_decide_statuses(self):
        tmp = Path(tempfile.mkdtemp())
        for d in ("accept", "unverified", "retry"):
            rec = sa.decide({"analysis": ANALYSIS}, {"decision": d, "needs": []}, None, tmp / "x.png", tmp, 1)
            self.assertIn(rec["status"], STATUSES)
        self.assertLessEqual(sa.PENDING, STATUSES)
        self.assertLessEqual(sa.FINAL_OK, STATUSES)

    def test_judge_verdicts(self):
        e = lambda kind, hidden=None: {"id": f"s__{kind}", "page": 2, "stem": "s", "kind": kind, "path": "x", "px": 1,
                                       "box": None, "question": "", "hidden": hidden or {}}
        line_ok = cropmod.judge([e("line")], {"s__line": "기본 키는 NULL을 허용하지 않는다."}, ANALYSIS)
        line_bad = cropmod.judge([e("line")], {"s__line": "기본 키는 테이블당 하나만 존재한다. (2024년 기준 1,024개 행)"},
                                 digits_analysis())
        unread = cropmod.judge([e("line")], {"s__line": "[?]"}, ANALYSIS)
        struct = cropmod.judge([e("thumb_struct")], {"s__thumb_struct": {"verdict": "issue", "note": "표 미선언"}}, ANALYSIS)
        read = cropmod.judge([e("thumb_read")], {"s__thumb_read": ["3장. 기본 키와 외래 키"]}, ANALYSIS)
        got = [j[0]["verdict"] for j in (line_ok, line_bad, unread, struct, read)]
        self.assertEqual(got[:4], ["agy_ok", "agy_wrong", "unreadable", "issue"])
        self.assertIn(got[4], VERDICTS)
        for j in line_ok + line_bad + unread + struct + read:
            self.assertLessEqual({"id", "kind", "verdict"}, set(j))
        self.assertEqual(line_bad[0]["claude_text"][:5], "기본 키는")
        self.assertEqual(struct[0]["note"], "표 미선언")
        self.assertEqual(cropmod.page_verdict(line_ok), "accepted")
        self.assertEqual(cropmod.page_verdict(line_bad), "needs_attention")
        self.assertEqual(cropmod.page_verdict([]), "pending")

    @unittest.skipUnless(HAVE_IMG, "Pillow missing")
    def test_queue_and_judgment_ids(self):
        import synth
        tmp = Path(tempfile.mkdtemp())
        img = synth.slide_main(tmp / "slide_002.png")
        a = digits_analysis()
        sc = ocr_score.score(a, REF)
        self.assertEqual(sc["decision"], "escalate")
        entries = cropmod.prepare(img, REF, sc, a, tmp / "crops", "slide_002", 2)
        self.assertTrue(any(e["kind"] == "line" for e in entries), entries)
        line_ids = {ln["id"] for ln in REF["lines"]}
        for e in entries:
            self.assertTrue(e["id"].startswith("slide_002__"), e["id"])
            self.assertTrue(Path(e["path"]).is_file())
            if e["kind"] == "line":
                self.assertEqual(e["id"].rsplit("__", 1)[1], e["line_id"])  # kp_regions.judged_wrong_lines
                self.assertIn(e["line_id"], line_ids)
        js = cropmod.judge(entries, answer_queue(entries), a)
        self.assertEqual([j["id"] for j in js], [e["id"] for e in entries])


# --------------------------------------------------------------------------- end to end through the siblings
@unittest.skipUnless(HAVE_IMG, "Pillow missing")
class SlideRunContractTests(unittest.TestCase):
    """A fake-agy run of slide_analyze.py: the files kp reads (results/, ref/, review_queue.md) carry the fields it uses."""

    @classmethod
    def setUpClass(cls):
        import synth
        cls.tmp = Path(tempfile.mkdtemp())
        cls.slides = cls.tmp / "slides"
        synth.make_deck(cls.slides)
        cls.out = cls.tmp / "out"
        cls.engines = [e.name for e in ocr_ref.available_engines()]
        cls.cp_run = run_cli(SCRIPTS / "slide_analyze.py", "run", "--slides", str(cls.slides), "--out", str(cls.out),
                             "--audit-rate", "0", scenario="digits")
        cls.queue = sa.read_json(cls.out / "review_queue.json", []) or []
        (cls.out / "crop_reads.json").write_text(json.dumps(answer_queue(cls.queue), ensure_ascii=False), encoding="utf-8")
        cls.cp_resolve = run_cli(SCRIPTS / "slide_analyze.py", "resolve", "--slides", str(cls.slides), "--out", str(cls.out),
                                 "--audit-rate", "0")
        cls.cp_report = run_cli(SCRIPTS / "slide_analyze.py", "report", "--slides", str(cls.slides), "--out", str(cls.out),
                                "--pages", "2", "--audit-rate", "0", "--shadow-audit-rate", "0")

    def results(self):
        return {f.stem: json.loads(f.read_text(encoding="utf-8")) for f in sorted((self.out / "results").glob("*.json"))}

    def test_run_exit_and_outputs(self):
        self.assertIn(self.cp_run.returncode, (0, 2), self.cp_run.stderr)
        self.assertIn(self.cp_resolve.returncode, (0, 2), self.cp_resolve.stderr)
        self.assertNotIn("Traceback", self.cp_run.stderr + self.cp_resolve.stderr)
        self.assertEqual(len(self.results()), 3)
        self.assertEqual(len(json.loads((self.out / "all_slides.json").read_text(encoding="utf-8"))), 3)
        self.assertTrue((self.out / "report.md").is_file())
        self.assertFalse((self.out / "source.json").exists())  # kp reads source.json as the agy-pdf-analyzer marker

    def test_results_fields(self):
        for stem, r in self.results().items():
            self.assertEqual(r["file"], f"{stem}.png")
            self.assertIsInstance(r["page"], int)
            self.assertIn(r["status"], STATUSES)
            self.assertIsInstance(r["analysis"], dict)
            sc = r["score"]
            self.assertIn(sc["decision"], DECISIONS)
            for k in ("metrics", "reasons", "triggers"):
                self.assertIn(k, sc)
            self.assertTrue(all("code" in v and "level" in v for v in r["validation"]))
            self.assertIn(r["score_legacy"]["decision"], DECISIONS)  # shadow mode, never the status
            self.assertIsInstance(r["score_legacy"]["reasons"], list)
            self.assertIn(r.get("audit_stratum"), {None, "ocr", "new_only"})
            for j in r.get("judgments") or []:
                self.assertTrue(j["id"].startswith(f"{stem}__"), j["id"])
                self.assertIn(j["verdict"], VERDICTS)
                self.assertIn(j["kind"], {"line", "thumb_read", "thumb_struct", "thumb_audit"})

    def test_ref_fields(self):
        if not self.engines:
            self.skipTest("no OCR engine available")
        for f in sorted((self.out / "ref").glob("*.json")):
            ref = json.loads(f.read_text(encoding="utf-8"))
            self.assertEqual(len(ref["sha256"]), 64)
            self.assertEqual(ref["version"], ocr_ref.REF_VERSION)
            self.assertEqual(len(ref["size"]), 2)
            self.assertTrue(all("engine" in e and "ok" in e for e in ref["engines"]))
            self.assertIsInstance(ref["layout"].get("graphics_ratio"), float)
            for ln in ref["lines"]:
                self.assertRegex(ln["id"], r"^L\d{3}$")
                self.assertEqual(len(ln["box"]), 4)
                self.assertIsInstance(ln["text"], str)
                for w in ln["words"]:
                    self.assertIn(w["weight"], WEIGHTS)
                    self.assertEqual(len(w["box"]), 4)

    def test_queue_entries(self):
        self.assertEqual((self.out / "review_queue.md").exists(), bool(sa.read_json(self.out / "review_queue.json", [])))
        for e in self.queue:
            self.assertTrue(e["id"].startswith(f"{e['stem']}__"), e["id"])
            self.assertRegex(e.get("fp") or "", r"^[0-9a-f]{8,}$")  # what Claude was shown; resolve drops stale answers
            if e["kind"] == "line":
                self.assertEqual(e["id"].rsplit("__", 1)[1], e["line_id"])

    def test_plain_answers_are_judged(self):
        judged = {j["id"] for r in self.results().values() for j in r.get("judgments") or []}
        self.assertLessEqual({e["id"] for e in self.queue}, judged)

    def test_deck_files_cover_every_result_and_counts_the_selection(self):
        self.assertIn(self.cp_report.returncode, (0, 2), self.cp_report.stderr)
        self.assertEqual(sum(json.loads(self.cp_report.stdout)["counts"].values()), 1)
        self.assertEqual(len(json.loads((self.out / "all_slides.json").read_text(encoding="utf-8"))), 3)
        manifest = json.loads((self.out / "slides_source.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(manifest["files"]), ["slide_001.png", "slide_002.png", "slide_003.png"])

    def test_v3_reference_fields(self):
        if not self.engines:
            self.skipTest("no OCR engine available")
        for f in sorted((self.out / "ref").glob("*.json")):
            ref = json.loads(f.read_text(encoding="utf-8"))
            self.assertIn(ref["agreement"], {"cross_engine", "variants", "single"})
            self.assertIn("n_variant", ref["quality"])
            lay = ref["layout"]
            for k in ("graphics_ratio", "table_candidate", "ruled_table", "title_band", "graphics_ratio_nocode",
                      "code_boxes", "table_grid2", "ruled_table2"):
                self.assertIn(k, lay)
            self.assertEqual((lay["layout_version"], lay["text_masked"]), (3, True))
            for ln in ref["lines"]:
                for w in ln["words"]:
                    self.assertIn(w.get("agreed_by"), {"engine", "variant", None})
                    if w.get("agreed_by") == "variant":
                        self.assertNotEqual(w["weight"], "high")  # kp: digits_checked means two real engines

    @unittest.skipUnless(HAVE_KP, "lecture-knowledge-pack not next to this skill")
    def test_kp_ingest_and_regions(self):
        sys.path.insert(0, str(KP_SCRIPTS))
        import kp_ingest
        import kp_regions
        lec = {"id": "db01", "title": "contract", "slides": self.slides, "analysis": self.out}
        ing = kp_ingest.ingest_lecture(lec)
        self.assertEqual(len(ing["records"]), 3)
        res = self.results()
        for rec in ing["records"]:
            self.assertIn(rec["text_check"], TEXT_CHECKS)
            self.assertEqual(rec["text_check"], kp_ingest.classify_text_check(res[rec["stem"]], None))
            self.assertNotEqual(rec["provenance"], "stale")
            if self.engines:
                self.assertEqual(rec["provenance"], "ok")
            ref = sa.read_json(Path(rec["ref_path"])) if rec["ref_path"] else None
            for reg in kp_regions.select_regions(rec, ref):
                self.assertLessEqual({"rid", "slide", "kind", "part", "priority", "reason"}, set(reg))
            wrong = kp_regions.judged_wrong_lines(rec)
            ids = {ln["id"] for ln in (ref or {}).get("lines") or []}
            self.assertLessEqual(set(wrong), ids)


@unittest.skipUnless(HAVE_PDF and HAVE_IMG, "agy-pdf-analyzer not next to this skill, or Pillow missing")
class PdfRunContractTests(unittest.TestCase):
    """agy-pdf-analyzer drives the shared report / resolve / rescore code through pseudo slide paths (ref/page_XXXX.png)."""

    @classmethod
    def setUpClass(cls):
        try:
            import pypdf  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("pypdf missing")
        import synth
        cls.tmp = Path(tempfile.mkdtemp())
        deck = synth.make_deck(cls.tmp / "src")
        cls.pdf = cls.tmp / "deck.pdf"
        ims = [ocr_ref.Image.open(p).convert("RGB") for p in deck]
        ims[0].save(cls.pdf, save_all=True, append_images=ims[1:])
        cls.out = cls.tmp / "deck_analysis"
        cls.script = PDF_SKILL / "scripts" / "pdf_analyze.py"
        cls.cps, cls.n_pages, cls.decisions = [], [], []
        for args in (["run", "--pdf", str(cls.pdf), "--out", str(cls.out), "--audit-rate", "0"],
                     ["report", "--pdf", str(cls.pdf), "--out", str(cls.out), "--pages", "2", "--audit-rate", "0",
                      "--shadow-audit-rate", "0"],
                     ["rescore", "--pdf", str(cls.pdf), "--out", str(cls.out), "--audit-rate", "0"]):
            cls.cps.append(run_cli(cls.script, *args, scenario="digits"))
            ap = cls.out / "all_pages.json"
            cls.n_pages.append(len(json.loads(ap.read_text(encoding="utf-8"))) if ap.exists() else None)
            cls.decisions.append({f.stem: (sa.read_json(f) or {}).get("decision") for f in (cls.out / "results").glob("*.json")})
        queue = sa.read_json(cls.out / "review_queue.json", []) or []
        (cls.out / "crop_reads.json").write_text(json.dumps(answer_queue(queue), ensure_ascii=False), encoding="utf-8")
        cls.cps.append(run_cli(cls.script, "resolve", "--pdf", str(cls.pdf), "--out", str(cls.out), "--audit-rate", "0"))

    def test_commands_complete(self):
        for cp in self.cps:
            self.assertIn(cp.returncode, (0, 2), cp.stderr[-2000:])
            self.assertNotIn("Traceback", cp.stderr)
        self.assertFalse((self.out / "all_slides.json").exists())
        self.assertTrue((self.out / "all_pages.json").is_file())

    def test_report_of_a_selection_keeps_every_page(self):
        self.assertEqual(self.n_pages, [3, 3, 3])

    def test_run_settles_on_the_rescore_decisions(self):
        self.assertEqual(self.decisions[0], self.decisions[2])
        self.assertEqual(len(self.decisions[0]), 3)

    def test_pdf_outputs(self):
        src = json.loads((self.out / "source.json").read_text(encoding="utf-8"))
        self.assertEqual((src["file"], src["pages"], len(src["sha256"])), ("deck.pdf", 3, 64))
        recs = sorted((self.out / "results").glob("page_*.json"))
        self.assertEqual([f.stem for f in recs], ["page_0001", "page_0002", "page_0003"])
        for f in recs:
            r = json.loads(f.read_text(encoding="utf-8"))
            self.assertIn(r["status"], STATUSES)
            self.assertEqual(r["file"], "deck.pdf")
            self.assertIn(r["score_legacy"]["decision"], DECISIONS)
            for j in r.get("judgments") or []:
                self.assertTrue(j["id"].startswith(f"{f.stem}__"), j["id"])

    @unittest.skipUnless(HAVE_KP, "lecture-knowledge-pack not next to this skill")
    def test_kp_reads_pdf_output(self):
        sys.path.insert(0, str(KP_SCRIPTS))
        import kp_ingest
        ing = kp_ingest.ingest_lecture({"id": "pdf01", "title": "contract", "slides": self.out / "ref", "analysis": self.out})
        self.assertEqual(len(ing["records"]), 3)
        for rec in ing["records"]:
            self.assertIn(rec["text_check"], TEXT_CHECKS)
            self.assertNotEqual(rec["provenance"], "stale")


# --------------------------------------------------------------------------- REF_VERSION 3 / scorer v2 contracts
class FoldTests(unittest.TestCase):
    """K1: OCR look-alike folding lives in fold / ocr_key / skel; norm() keeps its old behaviour (pinned above)."""

    def test_fold(self):
        self.assertEqual(textnorm.fold("〈div〉 v—show a−b 十 Ø “q” ‘x’"), "<div> v-show a-b + 0 \"q\" 'x'")
        for lookalike, true in (("EnabIing", "Enabling"), ("Pe0Ple", "People"), ("v—show", "v-show"), ("tit1e", "title")):
            self.assertEqual(textnorm.ocr_key(lookalike), textnorm.ocr_key(true), lookalike)
            self.assertEqual(textnorm.skel(lookalike), textnorm.skel(true), lookalike)
        self.assertNotEqual(textnorm.norm("EnabIing"), textnorm.norm("Enabling"))


class LayoutV3Tests(unittest.TestCase):
    """K2: layout v3 keys sit next to the old ones; graphics_ratio keeps its raw meaning (kp_regions / kp_ingest)."""

    @unittest.skipUnless(HAVE_IMG and ocr_ref.np is not None, "Pillow / numpy missing")
    def test_code_panel_is_painted_out_of_nocode_only(self):
        from PIL import ImageDraw
        im = ocr_ref.Image.new("RGB", (1280, 720), "white")
        ImageDraw.Draw(im).rectangle((100, 200, 900, 500), fill=(20, 20, 20))
        panels = ocr_ref.detect_code_panels(im)
        self.assertEqual(len(panels), 1)
        self.assertEqual(len(panels[0]), 4)
        lay = ocr_ref.layout_stats(im, [])
        self.assertGreaterEqual(lay["graphics_ratio"], 0.06)
        self.assertLess(lay["graphics_ratio_nocode"], 0.015)
        self.assertEqual(lay["code_boxes"], panels)
        self.assertIs(lay["text_masked"], False)  # no OCR word mask: what pdf_analyze passes for text-layer pages
        self.assertEqual(lay["layout_version"], 3)
        self.assertEqual(ocr_ref.REF_VERSION, 3)

    def test_variants_are_named_and_never_listed_as_available(self):
        names = [e.name for e in ocr_ref.with_variants([ocr_ref.WinOcrEngine()], [1, 2])]
        self.assertEqual(names, ["winocr", "winocr_x2"])
        self.assertEqual([e.name for e in ocr_ref.with_variants([], [1, 2])], [])
        binds(ocr_ref.load_or_build, object(), object(), [], rebuild=True)

    def test_pick_engines_adds_variants_only_for_one_real_engine(self):
        real = ocr_ref.available_engines()
        saved = os.environ.get("OCR_SCALES")
        quiet = contextlib.redirect_stderr(io.StringIO())
        try:
            with quiet:
                os.environ.pop("OCR_SCALES", None)
                got = [e.name for e in sa.pick_engines(argparse.Namespace(engine="auto"))]
                os.environ["OCR_SCALES"] = "1"
                plain = [e.name for e in sa.pick_engines(argparse.Namespace(engine="auto"))]
        finally:
            os.environ.pop("OCR_SCALES", None) if saved is None else os.environ.__setitem__("OCR_SCALES", saved)
        self.assertEqual(plain, [e.name for e in real])
        if len(real) == 1 and real[0].name == "winocr":
            self.assertEqual(got, ["winocr", "winocr_x2"])
        elif len(real) != 1:
            self.assertEqual(got, [e.name for e in real])

    def test_a_judged_stale_reference_is_kept_unless_rebuilt(self):
        if not HAVE_IMG:
            self.skipTest("Pillow missing")
        import synth
        tmp = Path(tempfile.mkdtemp())
        img = synth.slide_title_only(tmp / "slides" / "slide_001.png")
        rp, res = tmp / "out" / "ref" / "slide_001.json", tmp / "out" / "results" / "slide_001.json"
        rp.parent.mkdir(parents=True)
        res.parent.mkdir(parents=True)
        old = fake_reference([("2부 관계형 모델", [300, 300, 600, 64])])
        old.update(version=2, sha256=ocr_ref.sha256_file(img), engines=[{"engine": "winocr", "ok": True}])
        rp.write_text(json.dumps(old), encoding="utf-8")
        res.write_text(json.dumps({"page": 1, "judgments": [{"id": "slide_001__L001", "kind": "line", "verdict": "agy_ok"}]}),
                       encoding="utf-8")
        # judgments name the old line ids: a run must not renumber them (only rescore, which drops judgments, may)
        self.assertEqual(ocr_ref.load_or_build(img, rp, [ocr_ref.WinOcrEngine()])["version"], 2)
        self.assertEqual(sa.get_reference(img, tmp / "out", [ocr_ref.WinOcrEngine()])["version"], 2)


class ScorerV2Tests(unittest.TestCase):
    """K3: new outputs and switches are additive; old calibration files keep loading."""

    def test_thresholds_and_switches(self):
        th = ocr_score.DEFAULT_THRESHOLDS
        self.assertEqual(th["graphics_trigger_nocode"], 0.015)
        for k in ("template_exclusion", "close_thin_shortcut", "line_check", "latin_mid", "copy_check", "hf_boilerplate"):
            self.assertIs(th[k], True, k)
        self.assertLessEqual(set(ocr_score.SWITCHES_OFF), set(th))
        self.assertIs(ocr_score.legacy_thresholds(th)["hf_boilerplate"], False)
        self.assertEqual(th["weights"]["variant"], 1.0)
        self.assertEqual(set(ocr_score.STRONG), {"high", "variant"})
        tmp = Path(tempfile.mkdtemp()) / "calibration.json"
        tmp.write_text(json.dumps({"thresholds": {"recall_accept": 0.88, "weights": {"high": 1.0, "mid": 0.4, "low": 0.0}}}),
                       encoding="utf-8")
        old = ocr_score.load_thresholds(tmp)
        self.assertEqual((old["recall_accept"], old["weights"]["mid"], old["weights"]["variant"]), (0.88, 0.4, 1.0))

    def test_new_signatures(self):
        binds(ocr_score.score, {}, None, {}, validator_errors=[], tkeys=set())
        binds(ocr_score.template_keys, [], min_slides=5)
        binds(ocr_score.line_token_issues, {}, {})
        binds(ocr_score.text_token_issues, "", {})
        binds(ocr_score.copy_consistency, {})
        binds(cropmod.prepare, object(), {}, {}, {}, object(), "s", 1, audit=False, boilerplate=set())
        binds(cropmod.judge, [], {}, {}, boilerplate=set())
        x = object()
        binds(ocr_score.score, {}, None, {}, validator_errors=[], tkeys=set(), boilerplate=set())
        binds(ocr_score.hf_boilerplate, [], min_slides=3)
        binds(ocr_score.legacy_thresholds, {})
        binds(sa.score_pair, {}, None, {}, [], None, None)
        binds(sa.deck_hf_boilerplate, [x], x, {}, {})
        binds(sa.settle, [x], x, {}, lambda rec, sc, ref, img, pg: None)  # pdf_analyze run

    def test_additive_score_outputs(self):
        sc = ocr_score.score(ANALYSIS, REF)
        self.assertIn("line_issues", sc)
        self.assertIn("content_chars", sc["metrics"])
        self.assertIn("template_lines", sc["metrics"])
        self.assertEqual(sc["metrics"]["boilerplate_chars"], 0)
        self.assertEqual(ocr_score.score(ANALYSIS, REF, tkeys=None)["decision"], sc["decision"])
        self.assertEqual(ocr_score.score(ANALYSIS, REF, boilerplate={"12"})["metrics"]["boilerplate_chars"], 2)
        self.assertEqual(ocr_score.score(ANALYSIS, None)["metrics"]["boilerplate_chars"], 0)

    def test_operator_checks(self):
        an = dict(ANALYSIS, verbatim_text=[{"region": "body", "text": "if (age > 30) return"}], code_blocks=[], tables=[])
        self.assertTrue(ocr_score.text_token_issues("if (age < 30) return", an))
        self.assertEqual(ocr_score.text_token_issues("if (age > 30) return", an), [])
        two = dict(an, code_blocks=[{"language": "js", "code": "const n = items.length % 3"}],
                   verbatim_text=[{"region": "body", "text": "const n = items.length % 2"}])
        self.assertTrue(ocr_score.copy_consistency(two))

    def test_template_keys(self):
        footer = [("EXAMPLE CODE ACADEMY FOR TEENS", [900, 680, 320, 24])]
        refs = [fake_reference(footer + [(f"본문 {i}번째 줄입니다", [60, 140, 500, 30])]) for i in range(6)]
        keys = ocr_score.template_keys(refs, min_slides=5)
        self.assertEqual(len(keys), 1)
        self.assertEqual(ocr_score.template_keys(refs[:4], min_slides=5), set())
        text_refs = [ocr_ref.text_reference("EXAMPLE CODE ACADEMY FOR TEENS\n본문", "x.pdf")] * 6
        self.assertEqual(ocr_score.template_keys(text_refs), set())  # pdf text-layer references have no boxes
        sc = ocr_score.score(dict(ANALYSIS), refs[0], tkeys=keys)
        self.assertEqual(sc["metrics"]["template_lines"], 1)

    def test_thin_reference_with_unsupported_text_escalates(self):
        ref = fake_reference([("2부 관계형 모델", [300, 300, 600, 64])])
        an = dict(ANALYSIS, slide_type="section_divider", title="2부. 관계형 모델", tables=[], code_blocks=[], diagrams=[],
                  verbatim_text=[{"region": "title", "text": "2부. 관계형 모델"},
                                 {"region": "body", "text": "릴레이션 스키마"}])
        sc = ocr_score.score(an, ref)
        self.assertEqual(sc["decision"], "escalate", sc["reasons"])

    @unittest.skipUnless(HAVE_IMG and ocr_ref.np is not None, "Pillow / numpy missing")
    def test_unmasked_text_layer_page_never_fires_the_graphics_trigger(self):
        # agy-pdf-analyzer: a text-layer page's layout has no OCR word mask, so its ink is text and graphics alike
        from PIL import ImageDraw
        im = ocr_ref.Image.new("RGB", (1280, 720), "white")
        d = ImageDraw.Draw(im)
        for i in range(0, 600, 40):
            d.line((100 + i, 150, 700 - i, 650), fill=(220, 30, 30), width=14)
        text = "3장. 기본 키와 외래 키\n기본 키는 NULL을 허용하지 않는다.\n기본 키는 테이블당 하나만 존재한다."
        ref = ocr_ref.text_reference(text, "x.pdf")
        ref.update(size=list(im.size), layout=ocr_ref.layout_stats(im, []))
        an = dict(ANALYSIS, tables=[], code_blocks=[], diagrams=[], verbatim_text=[
            {"region": "body", "text": t} for t in text.splitlines()])
        self.assertGreaterEqual(ref["layout"]["graphics_ratio_nocode"], 0.015)
        self.assertNotIn("graphics_undeclared", ocr_score.score(an, ref)["triggers"])
        ref["layout"]["text_masked"] = True
        self.assertIn("graphics_undeclared", ocr_score.score(an, ref)["triggers"])


class CropsV2Tests(unittest.TestCase):
    """K4: a full-slide read is checked for numbers / identifiers the analysis lacks (issue, additive fields)."""

    def test_thumb_read_with_foreign_digits_is_an_issue(self):
        e = {"id": "s__thumb_read", "page": 2, "stem": "s", "kind": "thumb_read", "path": "x", "px": 1, "box": None,
             "question": "", "hidden": cropmod._hidden_for_read(REF, ocr_score.score(digits_analysis(), REF))}
        j = cropmod.judge([e], {"s__thumb_read": ["기본 키는 테이블당 하나만 존재한다. (2024년 기준 1,024개 행)"]},
                          digits_analysis())[0]
        self.assertEqual(j["verdict"], "issue")
        self.assertIn("2024", j.get("wrong_digits") or [])


class SiblingEnumTests(unittest.TestCase):
    """K5: the diagram-kind enum is shared by both analyzer prompts, both validators and kp's card labels."""

    def test_prompt_enum_matches_the_validator(self):
        text = (SCRIPTS.parent / "prompts" / "analyst.md").read_text(encoding="utf-8")
        enum = set(re.search(r'"kind": "([^"]+)"', text).group(1).split(" | "))
        self.assertEqual(enum, slide_validate.DIAGRAM_KINDS)
        self.assertLessEqual({"syntax_breakdown", "screenshot", "annotation"}, enum)

    @unittest.skipUnless(HAVE_KP, "lecture-knowledge-pack not next to this skill")
    def test_kp_has_a_label_for_every_kind(self):
        sys.path.insert(0, str(KP_SCRIPTS))
        import kp_render
        self.assertLessEqual(slide_validate.DIAGRAM_KINDS, set(kp_render.KIND_KO))

    @unittest.skipUnless(HAVE_PDF, "agy-pdf-analyzer not next to this skill")
    def test_pdf_validator_in_step(self):
        sys.path.insert(0, str(PDF_SKILL / "scripts"))
        import pdf_validate
        self.assertEqual(pdf_validate.DIAGRAM_KINDS, slide_validate.DIAGRAM_KINDS)
        self.assertEqual(set(pdf_validate.FEWSHOT_LEAKS), set(slide_validate.FEWSHOT_LEAKS) - {"example_slide_"})


@unittest.skipUnless(HAVE_PDF, "agy-pdf-analyzer not next to this skill")
class PdfImportTests(unittest.TestCase):
    def test_pdf_analyze_binds_to_this_folder(self):
        sys.path.insert(0, str(PDF_SKILL / "scripts"))
        import pdf_analyze
        self.assertIsNone(pdf_analyze.SHARED_ERR, pdf_analyze.SHARED_ERR)
        self.assertEqual(Path(pdf_analyze.SHARED).resolve(), SCRIPTS.resolve())
        import pdf_validate
        binds(pdf_validate.validate_analysis, {}, 1)
        binds(pdf_validate.cross_check, {})


if __name__ == "__main__":
    unittest.main()

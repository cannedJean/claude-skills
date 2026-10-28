"""agy-pdf-analyzer tests (stdlib unittest).

    python -m unittest discover -s scripts/tests -v        (from the skill folder)

Three layers:
  * pdf_validate: crash guards and the content warnings (always run)
  * prompts: few-shot leak markers, str.format placeholders, enums in step with agy-slide-analyzer (always run;
    the cross-skill check is skipped when that skill is not next to this one)
  * pdf_analyze with a fake agy (tests/fake_agy_pdf.py): cp949 console, a truncated results file, a quota hit
    during a retry, another PDF in the same output folder, an outdated shared copy, text-layer references, the
    header/footer boilerplate of this PDF only and settling every page. These need agy-slide-analyzer next to
    this skill (shared code + its synthetic slides), Pillow and pypdf; skipped otherwise.
"""
from __future__ import annotations

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
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
SKILL = SCRIPTS.parent
SLIDE_SKILL = SKILL.parent / "agy-slide-analyzer"
SHARED = SLIDE_SKILL / "scripts"
sys.path.insert(0, str(SCRIPTS))

import pdf_validate as pv  # noqa: E402

HAVE_SHARED = (SHARED / "ocr_score.py").is_file() and (SHARED / "tests" / "fake_agy_task.py").is_file()
try:
    import pypdf  # noqa: F401
    from PIL import Image
    HAVE_LIBS = True
except ImportError:
    HAVE_LIBS = False

REASONING = {"observation": "제목과 본문 2문장", "structure": "표·코드·도식 없음", "interpretation": "개념 도입",
             "self_check": "누락 없음"}


def analysis(**kw):
    d = {"page": 3, "reasoning": dict(REASONING), "slide_type": "concept", "title": "3장. 기본 키",
         "printed_page_number": "3",
         "verbatim_text": [{"region": "title", "text": "3장. 기본 키"},
                           {"region": "body", "text": "기본 키는 NULL을 허용하지 않는다."},
                           {"region": "header/footer", "text": "데이터베이스 개론"}],
         "tables": [], "code_blocks": [], "diagrams": [],
         "key_concepts": [{"term": "기본 키", "explanation": "NULL 불허"}],
         "summary": "기본 키가 NULL을 허용하지 않는다는 성질을 설명한다.", "lecture_role": "개념 도입",
         "exam_points": ["기본 키는 NULL 불허"], "uncertain": [], "confidence": 0.9}
    d.update(kw)
    return d


def codes(d, page=3):
    return [i["code"] for i in pv.validate_analysis(d, page)]


# --------------------------------------------------------------------------- pdf_validate
class ValidateTests(unittest.TestCase):
    def test_clean_page(self):
        self.assertEqual(pv.validate_analysis(analysis(), 3), [])

    def test_malformed_values_give_issues_not_exceptions(self):
        bad = [analysis(tables=[["a", "b"], ["1", "2"]]),
               analysis(verbatim_text=[{"region": "body", "text": None}, {"region": "title", "text": 3}]),
               analysis(key_concepts=[{"term": None}, {"term": ["x"]}, "기본 키"]),
               analysis(tables=[{"headers": ["a"], "rows": 5}]),
               analysis(diagrams=["도식", {"kind": None, "relations": None}], code_blocks=[{"code": None}, "x"]),
               analysis(reasoning={"observation": None})]
        for d in bad:
            issues = pv.validate_analysis(d, 3)
            self.assertIsInstance(issues, list)
            self.assertTrue(all({"level", "code", "msg"} <= set(i) for i in issues))
        self.assertIn("bad_table", codes(bad[0]))
        self.assertIn("bad_type", codes(bad[3]))

    def test_interpretation_on_title_only(self):
        title_only = dict(slide_type="section_divider", title="2부. 관계형 모델", tables=[], code_blocks=[], diagrams=[],
                          verbatim_text=[{"region": "title", "text": "2부. 관계형 모델"},
                                         {"region": "header/footer", "text": "데이터베이스 개론"}])
        self.assertIn("interpretation_on_title_only", codes(analysis(**title_only, exam_points=["v-model 수식어 .lazy"])))
        two = [{"term": "관계형 모델", "explanation": "단원 제목"}, {"term": "릴레이션", "explanation": "예측"}]
        self.assertIn("interpretation_on_title_only", codes(analysis(**title_only, exam_points=[], key_concepts=two)))
        self.assertNotIn("interpretation_on_title_only", codes(analysis(**title_only, exam_points=[], key_concepts=two[:1])))
        self.assertNotIn("interpretation_on_title_only", codes(analysis()))

    def test_visual_undeclared(self):
        shot = dict(REASONING, observation="코드 상자 오른쪽에 개발자 도구 캡처가 있다.")
        mark = dict(REASONING, structure="빨간색 박스로 :key 줄을 강조했다.")
        for r in (shot, mark):
            self.assertIn("visual_undeclared", codes(analysis(reasoning=r)))
        dg = [{"kind": "screenshot", "description": "개발자 도구", "elements": ["Elements", "Console"], "relations": []}]
        self.assertNotIn("visual_undeclared", codes(analysis(reasoning=shot, diagrams=dg)))
        self.assertNotIn("visual_undeclared", codes(analysis()))

    def test_code_duplicated(self):
        code = [{"language": "javascript", "code": "const count = ref(0)\ncount.value++"}]
        vt = analysis()["verbatim_text"] + [{"region": "body", "text": "const count = ref(0)"}]
        self.assertIn("code_duplicated", codes(analysis(code_blocks=code, verbatim_text=vt)))
        self.assertNotIn("code_duplicated", codes(analysis(code_blocks=code)))
        short = analysis()["verbatim_text"] + [{"region": "body", "text": "count"}]  # under 8 chars: a mention
        self.assertNotIn("code_duplicated", codes(analysis(code_blocks=code, verbatim_text=short)))

    def test_illustration_with_relations(self):
        dg = {"kind": "illustration", "description": "문법 분해", "elements": ["@submit", "Argument"],
              "relations": ["보라색 밑줄 submit -> Argument : Follows the colon"]}
        self.assertIn("illustration_with_relations", codes(analysis(diagrams=[dg])))
        self.assertNotIn("illustration_with_relations", codes(analysis(diagrams=[dict(dg, kind="syntax_breakdown")])))
        self.assertNotIn("illustration_with_relations", codes(analysis(diagrams=[dict(dg, relations=[])])))

    def test_divider_with_body(self):
        d = analysis(slide_type="section_divider")
        self.assertIn("divider_with_body", codes(d))
        d["verbatim_text"] = d["verbatim_text"][:1] + d["verbatim_text"][2:]
        self.assertNotIn("divider_with_body", codes(d))

    def test_fewshot_leak_is_an_error(self):
        for m in pv.FEWSHOT_LEAKS:
            self.assertIn("fewshot_leak", codes(analysis(summary=f"{m} 를 그대로 옮긴 요약입니다. 길게 적습니다.")), m)

    def test_cross_check(self):
        a = analysis(verbatim_text=[{"region": "body", "text": "같은 본문이 두 페이지에 그대로 들어 있으면 청크 경계에서 "
                                                             "페이지가 밀렸을 수 있다"}])
        out = pv.cross_check({1: a, 2: copy.deepcopy(a)})
        self.assertEqual([i["code"] for i in out[2]], ["duplicate_content"])
        series = pv.cross_check({4: analysis(title="v-for (1/2)", slide_type="example"),
                                 5: analysis(title="v-for (2/2)", slide_type="code", verbatim_text=[])})
        self.assertEqual({i["code"] for p in (4, 5) for i in series[p]}, {"series_type_mixed"})
        self.assertTrue(all(i["level"] == "info" for p in (4, 5) for i in series[p]))


# --------------------------------------------------------------------------- prompts
class PromptTests(unittest.TestCase):
    analyst = (SKILL / "prompts" / "analyst.md").read_text(encoding="utf-8")
    reviewer = (SKILL / "prompts" / "reviewer.md").read_text(encoding="utf-8")
    CTX = dict(subject="데이터베이스", deck_context="ctx", total=3, pdf_name="x.pdf", chunk_file="lecture.pdf",
               chunk_desc="원본 PDF 전체", chunk_pages=3, n_targets=1, page_table="| 1번째 | 1 | `page_0001.json` |")

    def test_templates_format_and_examples_follow_the_rules(self):
        self.reviewer.format(**self.CTX)
        text = self.analyst.format(feedback="", **self.CTX)
        shots = re.findall(r"```json\s*(.*?)\s*```", text.split("# 예시 (Few-shot)", 1)[1], re.S)
        self.assertEqual(len(shots), 3)
        for raw in shots:
            ex = json.loads(raw)  # literal braces doubled correctly
            issues = [i["code"] for i in pv.validate_analysis(ex, ex["page"]) if i["code"] != "fewshot_leak"]
            self.assertEqual(issues, [], ex["title"])

    def test_fewshot_markers_occur_in_the_prompts(self):
        for m in pv.FEWSHOT_LEAKS:
            self.assertTrue(m in self.analyst or m in self.reviewer, m)

    def test_every_example_token_is_covered_by_a_marker(self):
        for text in (self.analyst, self.reviewer):
            for m in re.finditer(r"EXAMPLE |\bexample[_A-Z]", text):
                self.assertTrue(any(text.startswith(k, m.start()) for k in pv.FEWSHOT_LEAKS),
                                text[m.start():m.start() + 30])

    def test_diagram_kinds(self):
        enum = re.search(r'"kind": "([^"]+)"', self.analyst).group(1).split(" | ")
        self.assertEqual(set(enum), pv.DIAGRAM_KINDS)
        self.assertLessEqual({"syntax_breakdown", "screenshot", "annotation"}, pv.DIAGRAM_KINDS)

    def test_slide_types_match_the_validator(self):
        enum = re.search(r'"slide_type": "([^"]+) 중 하나"', self.analyst).group(1).split(" | ")
        self.assertEqual(set(enum), pv.SLIDE_TYPES)

    @unittest.skipUnless((SLIDE_SKILL / "prompts" / "analyst.md").is_file(), "agy-slide-analyzer not next to this skill")
    def test_in_step_with_the_slide_analyzer(self):
        other = (SLIDE_SKILL / "prompts" / "analyst.md").read_text(encoding="utf-8")
        for pat in (r'"kind": "([^"]+)"', r'"slide_type": "([^"]+)"', r'"region": "([^"]+)"'):
            self.assertEqual(re.search(pat, self.analyst).group(1), re.search(pat, other).group(1), pat)
        sys.path.insert(0, str(SHARED))
        import slide_validate as sv
        self.assertEqual(set(pv.FEWSHOT_LEAKS), set(sv.FEWSHOT_LEAKS) - {"example_slide_"})
        self.assertEqual((pv.SLIDE_TYPES, pv.REGIONS), (sv.SLIDE_TYPES, sv.REGIONS))
        self.assertEqual(pv.DIAGRAM_KINDS, sv.DIAGRAM_KINDS)
        for name in ("SHOT_RE", "MARK_RE", "SERIES_RE"):
            self.assertEqual(getattr(pv, name).pattern, getattr(sv, name).pattern, name)


# --------------------------------------------------------------------------- pdf_analyze with a fake agy
def make_pdf(path: Path, which=(0, 1, 2)) -> Path:
    sys.path.insert(0, str(SHARED / "tests"))
    import synth
    tmp = Path(tempfile.mkdtemp())
    deck = synth.make_deck(tmp)
    ims = [Image.open(deck[i]).convert("RGB") for i in which]
    ims[0].save(path, save_all=True, append_images=ims[1:])
    return path


def run_pdf(*args, scenario="perfect", utf8=True, extra_env=None):
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONUTF8", "PYTHONIOENCODING")}
    env.update(AGY_TASK=str(HERE / "fake_agy_pdf.py"), FAKE_AGY_SCENARIO=scenario, OCR_EVAL_DIR=str(SHARED),
               PYTHONDONTWRITEBYTECODE="1", **(extra_env or {}))
    if utf8:
        env["PYTHONUTF8"] = "1"
    cp = subprocess.run([sys.executable, str(SCRIPTS / "pdf_analyze.py"), *args], capture_output=True, env=env)
    return cp.returncode, cp.stdout.decode("utf-8", "replace"), cp.stderr.decode("utf-8", "replace")


@unittest.skipUnless(HAVE_SHARED and HAVE_LIBS, "needs agy-slide-analyzer next to this skill, Pillow and pypdf")
class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.pdf = make_pdf(self.tmp / "deck.pdf")

    def rec(self, out, page):
        return json.loads((out / "results" / f"page_{page:04d}.json").read_text(encoding="utf-8"))

    def test_cp949_console_still_gets_json_and_the_exit_code(self):
        out = self.tmp / "2학기 – DB" / "deck_analysis"
        rc, stdout, err = run_pdf("run", "--pdf", str(self.pdf), "--out", str(out), "--audit-rate", "0",
                                  utf8=False, extra_env={"PYTHONIOENCODING": "cp949"})
        self.assertIn(rc, (0, 2), err[-1500:])
        data = json.loads(stdout)
        self.assertEqual(data["out"], str(out))
        self.assertEqual(sum(data["counts"].values()), 3)

    def test_truncated_result_is_redone_alone(self):
        out = self.tmp / "out"
        rc, _, err = run_pdf("run", "--pdf", str(self.pdf), "--out", str(out), "--audit-rate", "0")
        self.assertIn(rc, (0, 2), err[-1500:])
        f = out / "results" / "page_0002.json"
        f.write_text(f.read_text(encoding="utf-8")[:120], encoding="utf-8")
        rc, _, err = run_pdf("run", "--pdf", str(self.pdf), "--out", str(out), "--audit-rate", "0")
        self.assertIn(rc, (0, 2), err[-1500:])
        self.assertIn("unreadable result page_0002.json", err)
        self.assertIn("1 to process", err)
        self.assertEqual(self.rec(out, 2)["page"], 2)
        for args in (["report"], ["resolve"]):
            rc, _, err = run_pdf(*args, "--pdf", str(self.pdf), "--out", str(out))
            self.assertNotIn("Traceback", err)
        self.assertFalse(list((out / "results").glob("*.tmp")))

    def test_quota_during_retry_keeps_the_first_analysis(self):
        sys.path.insert(0, str(SHARED))
        import ocr_ref
        if not ocr_ref.available_engines():
            self.skipTest("no OCR engine: no reference, so no retry")
        out = self.tmp / "out"
        rc, stdout, err = run_pdf("run", "--pdf", str(self.pdf), "--out", str(out), "--audit-rate", "0",
                                  scenario="omit_then_quota")
        self.assertEqual(rc, 3, err[-1500:])
        self.assertTrue(json.loads(stdout)["quota_exhausted"])
        r = self.rec(out, 2)
        self.assertEqual((r["status"], len(r["attempts"]), r["attempts"][0]["decision"]), ("needs_attention", 1, "retry"))
        self.assertTrue(r["analysis"]["verbatim_text"])
        rc, _, err = run_pdf("run", "--pdf", str(self.pdf), "--out", str(out), "--audit-rate", "0")
        self.assertIn("0 to process", err)  # nothing is re-spent on the kept page

    def test_pilot_run_seals_every_page_reference_first(self):
        sys.path.insert(0, str(SHARED))
        import ocr_ref
        if not ocr_ref.available_engines():
            self.skipTest("no OCR engine: scanned pages get no reference")
        out = self.tmp / "out"
        rc, _, err = run_pdf("run", "--pdf", str(self.pdf), "--out", str(out), "--pages", "2", "--audit-rate", "0")
        self.assertIn(rc, (0, 2), err[-1500:])
        self.assertIn("building 3 sealed reference(s) before any analysis", err)
        self.assertEqual(sorted(f.name for f in (out / "ref").glob("page_*.json")),
                         ["page_0001.json", "page_0002.json", "page_0003.json"])
        self.assertEqual([f.name for f in (out / "results").glob("*.json")], ["page_0002.json"])

    def test_queue_shards_then_resolve(self):
        out = self.tmp / "out"
        rc, _, err = run_pdf("run", "--pdf", str(self.pdf), "--out", str(out), "--audit-rate", "0", scenario="digits")
        self.assertIn(rc, (0, 2), err[-1500:])
        queue = json.loads((out / "review_queue.json").read_text(encoding="utf-8"))
        rc, stdout, err = run_pdf("queue", "--pdf", str(self.pdf), "--out", str(out), "--shards", "2", "--audit-rate", "0")
        self.assertIn(rc, (0, 2), err[-1500:])
        shards = json.loads(stdout)["shards"]
        self.assertTrue(shards and all(s["items"] and s["answers"].startswith("crop_reads_") for s in shards), shards)
        self.assertTrue((out / "review_queue_1.md").is_file())
        self.assertTrue((out / "all_pages.json").is_file())
        for k in (0, 1):  # answers may be split across files however the reviewers like; resolve merges them
            reads = {e["id"]: (e.get("hidden") or {}).get("ocr_text") or ["3장. 기본 키와 외래 키"]
                     for e in queue[k::2] if e["kind"] != "thumb_struct"}
            reads.update({e["id"]: {"verdict": "ok", "note": ""} for e in queue[k::2] if e["kind"] == "thumb_struct"})
            (out / f"crop_reads_{k + 1}.json").write_text(json.dumps(reads, ensure_ascii=False), encoding="utf-8")
        rc, _, err = run_pdf("resolve", "--pdf", str(self.pdf), "--out", str(out), "--audit-rate", "0")
        self.assertIn(rc, (0, 2), err[-1500:])
        recs = [json.loads(f.read_text(encoding="utf-8")) for f in sorted((out / "results").glob("page_*.json"))]
        self.assertEqual(sum(len(r.get("judgments") or []) for r in recs), len(queue))
        self.assertFalse([r["page"] for r in recs if r["status"] == "escalated"])

    def test_another_pdf_is_refused_and_force_takes_the_folder_over(self):
        out = self.tmp / "out"
        rc, _, err = run_pdf("run", "--pdf", str(self.pdf), "--out", str(out), "--audit-rate", "0")
        self.assertIn(rc, (0, 2), err[-1500:])
        other = make_pdf(self.tmp / "other.pdf", which=(2, 1, 0))
        for cmd in ("run", "reference", "rescore"):
            rc, _, err = run_pdf(cmd, "--pdf", str(other), "--out", str(out))
            self.assertEqual(rc, 5, (cmd, err[-800:]))
        self.assertEqual(json.loads((out / "source.json").read_text(encoding="utf-8"))["file"], "deck.pdf")
        before = (out / "ref" / "page_0001.png").read_bytes()
        rc, _, err = run_pdf("run", "--pdf", str(other), "--out", str(out), "--force", "--audit-rate", "0")
        self.assertIn(rc, (0, 2), err[-1500:])
        self.assertNotEqual((out / "ref" / "page_0001.png").read_bytes(), before)
        self.assertEqual(json.loads((out / "source.json").read_text(encoding="utf-8"))["file"], "other.pdf")

    def test_an_older_shared_copy_is_refused(self):
        old = self.tmp / "old_shared"
        old.mkdir()
        for f in SHARED.glob("*.py"):
            text = f.read_text(encoding="utf-8")
            if f.name == "ocr_ref.py":
                text = re.sub(r"(?m)^REF_VERSION = \d+", "REF_VERSION = 2", text)
            (old / f.name).write_text(text, encoding="utf-8")
        env = dict(os.environ, OCR_EVAL_DIR=str(old), AGY_TASK=str(HERE / "fake_agy_pdf.py"), PYTHONUTF8="1",
                   PYTHONDONTWRITEBYTECODE="1")
        cp = subprocess.run([sys.executable, str(SCRIPTS / "pdf_analyze.py"), "report", "--pdf", str(self.pdf),
                             "--out", str(self.tmp / "out")], capture_output=True, env=env)
        self.assertEqual(cp.returncode, 4)
        self.assertIn("older than this skill", cp.stderr.decode("utf-8", "replace"))


@unittest.skipUnless(HAVE_SHARED and HAVE_LIBS, "needs agy-slide-analyzer next to this skill, Pillow and pypdf")
class TextLayerReferenceTests(unittest.TestCase):
    """page_reference for a born-digital page, with the PDF readers stubbed (no PDF writer with a text layer needed)."""

    TEXT = "3장. 기본 키\n기본 키는 NULL을 허용하지 않는다.\n데이터베이스 개론"

    def setUp(self):
        os.environ.setdefault("OCR_EVAL_DIR", str(SHARED))
        import pdf_analyze
        self.pa = pdf_analyze
        self.assertIsNone(pdf_analyze.SHARED_ERR)
        self.saved = (pdf_analyze.ocr_ref.pdf_page_text, pdf_analyze.ocr_ref.pdf_page_image)
        img = Image.new("RGB", (1280, 720), "white")
        from PIL import ImageDraw
        ImageDraw.Draw(img).rectangle((100, 100, 1100, 600), fill="black")  # ink everywhere, no word boxes
        pdf_analyze.ocr_ref.pdf_page_text = lambda pdf, page: self.TEXT
        pdf_analyze.ocr_ref.pdf_page_image = lambda pdf, page: img.copy()
        self.out = Path(tempfile.mkdtemp())

    def tearDown(self):
        self.pa.ocr_ref.pdf_page_text, self.pa.ocr_ref.pdf_page_image = self.saved

    def test_text_reference_is_marked_unmasked_and_reused(self):
        ref = self.pa.page_reference(Path("x.pdf"), 1, self.out, [], "sha-1")
        self.assertEqual(ref["primary"], "pdf_text")
        self.assertIs(ref["layout"]["text_masked"], False)
        self.assertEqual(ref["size"], [1280, 720])
        self.assertEqual(ref["source_sha"], "sha-1")
        rp = self.out / "ref" / "page_0001.json"
        stamp = rp.stat().st_mtime_ns
        self.assertEqual(self.pa.page_reference(Path("x.pdf"), 1, self.out, [], "sha-1"), ref)
        self.assertEqual(rp.stat().st_mtime_ns, stamp)

    def test_an_older_text_reference_is_rebuilt(self):
        self.pa.page_reference(Path("x.pdf"), 1, self.out, [], "sha-1")
        rp = self.out / "ref" / "page_0001.json"
        old = json.loads(rp.read_text(encoding="utf-8"))
        old["version"] -= 1
        del old["layout"]["text_masked"]
        rp.write_text(json.dumps(old), encoding="utf-8")
        ref = self.pa.page_reference(Path("x.pdf"), 1, self.out, [], "sha-1")
        self.assertEqual(ref["version"], self.pa.ocr_ref.REF_VERSION)
        self.assertIs(ref["layout"]["text_masked"], False)
        self.assertEqual(json.loads(rp.read_text(encoding="utf-8"))["version"], self.pa.ocr_ref.REF_VERSION)

    def test_operator_mismatch_against_the_text_layer_retries(self):
        sql = "SELECT name FROM users WHERE age > 30 AND id = 1024;"
        self.pa.ocr_ref.pdf_page_text = lambda pdf, page: "\n".join([self.TEXT, sql])
        ref = self.pa.page_reference(Path("x.pdf"), 1, self.out, [], "sha-1")
        a = analysis(code_blocks=[{"language": "sql", "code": "SELECT name FROM users WHERE age < 30 AND id = 1024;"}])
        sc = self.pa.score(a, ref)
        # the text layer is exact: the analyst re-reads; no thumbnail read can overrule it
        self.assertEqual((sc["decision"], sc["reasons"]), ("retry", ["operator_or_digit_mismatch"]))
        a["code_blocks"][0]["code"] = "SELECT name FROM users WHERE age > 30 AND id = 1024;"
        self.assertNotIn("operator_or_digit_mismatch", self.pa.score(a, ref)["reasons"])

    def test_ruled_table_on_a_text_layer_page_still_triggers(self):
        from PIL import ImageDraw
        img = Image.new("RGB", (1280, 720), "white")
        d = ImageDraw.Draw(img)
        for i in range(4):  # a ruled 3x3 grid; the cells' text is in the text layer only
            d.line((140, 200 + 100 * i, 1140, 200 + 100 * i), fill="black", width=3)
            d.line((140 + 333 * i, 200, 140 + 333 * i, 500), fill="black", width=3)
        self.pa.ocr_ref.pdf_page_image = lambda pdf, page: img.copy()
        ref = self.pa.page_reference(Path("x.pdf"), 1, self.out, [], "sha-1")
        self.assertTrue(ref["layout"]["ruled_table"])
        self.assertIs(ref["layout"]["text_masked"], False)
        sc = self.pa.score(analysis(), ref)  # the rows went into verbatim_text, tables: []
        self.assertIn("table_candidate_undeclared", sc["triggers"])

    def test_truncated_page_image_is_extracted_again(self):
        self.pa.ocr_ref.pdf_page_text = lambda pdf, page: ""  # a scanned page; no OCR engine is passed
        self.assertIsNone(self.pa.page_reference(Path("x.pdf"), 1, self.out, [], "sha-1"))
        img = self.out / "ref" / "page_0001.png"
        good = img.read_bytes()
        img.write_bytes(good[:len(good) // 3])  # a kill during the save
        self.assertFalse(self.pa.image_ok(img))
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.pa.page_reference(Path("x.pdf"), 1, self.out, [], "sha-1")
        self.assertIn("page_0001.png is damaged", err.getvalue())
        self.assertTrue(self.pa.image_ok(img))
        self.assertEqual(img.read_bytes(), good)
        self.assertFalse([f.name for f in (self.out / "ref").iterdir() if "tmp" in f.name])


@unittest.skipUnless(HAVE_SHARED and HAVE_LIBS, "needs agy-slide-analyzer next to this skill, Pillow and pypdf")
class DeckBoilerplateTests(unittest.TestCase):
    """The PDF's header/footer boilerplate: pages of this PDF only, and `run` settles every page of it."""

    TEXT = "3장. 기본 키\n기본 키는 NULL을 허용하지 않는다."  # the footer '데이터베이스 개론' is never in the text layer
    setUp, tearDown = TextLayerReferenceTests.setUp, TextLayerReferenceTests.tearDown

    def write(self, page, **rec):
        (self.out / "results").mkdir(parents=True, exist_ok=True)
        rec = dict({"page": page, "file": "x.pdf", "analysis": analysis(page=page), "validation": [], "queue": [],
                    "judgments": [], "attempts": [{"attempt": 1}]}, **rec)
        self.pa.save(self.out, rec)

    def test_pages_past_the_pdf_never_count(self):
        th = self.pa.load_thresholds()
        for pg in (1, 2, 51, 52, 53):  # 51-53: left over from a longer PDF in the same --out
            self.write(pg)
        self.assertEqual(self.pa.chunk_boilerplate(self.out, th, {}, 2), set())
        self.assertEqual(self.pa.chunk_boilerplate(self.out, th, {3: {"analysis": analysis(page=3)}}, 3),
                         {"데이터베이스", "개론"})

    def test_settle_decides_every_page_again(self):
        th = self.pa.load_thresholds()
        pseudo = [self.pa.page_image_path(self.out, pg) for pg in range(1, 4)]
        for pg in range(1, 4):  # as a run stopped by the quota leaves them: scored against too few pages
            ref = self.pa.page_reference(Path("x.pdf"), pg, self.out, [], "sha-1")
            sc, legacy = self.pa.sa.score_pair(analysis(page=pg), ref, th)
            self.assertEqual(sc["decision"], "escalate")
            self.write(pg, score=sc, decision=sc["decision"], score_legacy=legacy, status="needs_attention")
        with contextlib.redirect_stderr(io.StringIO()):
            changed = self.pa.sa.settle(pseudo, self.out, th,
                                        lambda rec, sc, ref, img, pg: self.pa.decide_page(rec, sc, ref, img, self.out))
        self.assertEqual(changed, [(pg, "escalate", "accept") for pg in range(1, 4)])
        for pg in range(1, 4):
            r = json.loads((self.out / "results" / f"page_{pg:04d}.json").read_text(encoding="utf-8"))
            self.assertEqual((r["status"], r["score_legacy"]["decision"]), ("accepted", "escalate"))


if __name__ == "__main__":
    unittest.main()

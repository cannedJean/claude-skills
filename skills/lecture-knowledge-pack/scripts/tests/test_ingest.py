"""kp_ingest + kp_regions on fixture courses (no agy, no OCR engine needed)."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import kp_common as kc  # noqa: E402
import kp_fixtures  # noqa: E402
import kp_ingest  # noqa: E402
import kp_regions  # noqa: E402


def ingest_all(course_json):
    c = kc.load_course(course_json)
    return {l["id"]: kp_ingest.ingest_lecture(l) for l in c["lectures"]}


def regions_of(rec):
    return kp_regions.select_regions(rec, kc.read_json(rec["ref_path"]) if rec["ref_path"] else None)


class TrustTableTests(unittest.TestCase):
    def test_text_check_rows(self):
        scen = ["accepted", "thin", "claude_verified", "escalated", "needs_attention", "unverified", "failed", "missing"]
        spec = {"db01": {"slides": [("formula", s) for s in scen]}}
        with tempfile.TemporaryDirectory() as t:
            recs = ingest_all(kp_fixtures.make_course(t, spec))["db01"]["records"]
        got = [r["text_check"] for r in recs]
        self.assertEqual(got, ["ocr_verified", "thin_accepted", "claude_verified", "review_pending", "needs_attention",
                               "unverified", "image_only", "image_only"])
        trust = [kp_ingest.slide_trust(r, []) for r in recs]
        self.assertEqual(trust[3:], ["low", "low", "low", "image_only", "image_only"])
        self.assertEqual(recs[5]["provenance"], "unverifiable")

    def test_digits_checked_by_engine_count(self):
        spec = {"db01": {"slides": [("sql", "accepted")]}}
        with tempfile.TemporaryDirectory() as t:
            two = ingest_all(kp_fixtures.make_course(Path(t) / "a", spec, engine="two"))["db01"]["records"][0]
            one = ingest_all(kp_fixtures.make_course(Path(t) / "b", spec, engine="winocr"))["db01"]["records"][0]
            self.assertTrue(two["digits_checked"])
            self.assertFalse(one["digits_checked"])
            # winocr only and a faithful analysis: no line region, but trust stays below high until the pack checked
            self.assertEqual([g for g in regions_of(one) if g["kind"] == "line"], [])
            agreed = [dict(g, state="agreed") for g in regions_of(one)]
            self.assertEqual(kp_ingest.slide_trust(one, agreed), "medium")
            self.assertEqual(kp_ingest.slide_trust(one, agreed, {"digits_checked_by_pack": True}), "high")
            # a digit the analysis lost becomes a line region (the analyzer never checked it: no 'high' words)
            an = dict(one["analysis"])
            an["verbatim_text"] = [dict(v, text=v["text"].replace("30", "3")) for v in an["verbatim_text"]]
            an["code_blocks"] = [dict(c, code=c["code"].replace("30", "3")) for c in an["code_blocks"]]
            lines = [g for g in regions_of(dict(one, analysis=an)) if g["kind"] == "line"]
            self.assertEqual(len(lines), 1)
            self.assertIn("30", lines[0]["reason"])

    def test_stale_image(self):
        spec = {"db01": {"slides": [("sql", "accepted"), ("table", "accepted")]}}
        with tempfile.TemporaryDirectory() as t:
            cj = kp_fixtures.make_course(t, spec)
            img = Path(t) / "db01" / "slides" / "slide_002.png"
            from PIL import Image
            with Image.open(img) as im:
                im.rotate(1).save(img)
            recs = ingest_all(cj)["db01"]["records"]
        self.assertEqual(recs[1]["provenance"], "stale")
        self.assertIn("stale_analysis", recs[1]["flags"])
        self.assertEqual(kp_ingest.slide_trust(recs[1], []), "low")


class StructureTests(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.ing = ingest_all(kp_fixtures.make_course(self.t.name))

    def tearDown(self):
        self.t.cleanup()

    def test_boilerplate_and_build_sequence(self):
        recs = self.ing["db01"]["records"]
        self.assertEqual(recs[0]["boilerplate"], [2])  # the SSAFY footer
        self.assertEqual(recs[4]["build_of"], recs[5]["id"])
        self.assertIsNone(recs[5]["build_of"])

    def test_regions(self):
        recs = self.ing["db01"]["records"]
        kinds = {r["id"]: sorted(g["kind"] for g in regions_of(r)) for r in recs}
        self.assertEqual(kinds["db01-s001"], ["code"])
        self.assertEqual(kinds["db01-s002"], ["table"])
        self.assertEqual(kinds["db01-s003"], ["diagram"])
        f = self.ing["db02"]["records"]
        self.assertEqual(sorted(g["kind"] for g in regions_of(f[0])), ["formula"])
        self.assertEqual(sorted(g["kind"] for g in regions_of(f[2])), ["visual"])  # thin + graphics, no diagram

    def test_trust_needs_regions(self):
        sql = self.ing["db01"]["records"][0]
        regs = regions_of(sql)
        self.assertEqual(kp_ingest.slide_trust(sql, [dict(g, state="unchecked") for g in regs]), "medium")
        self.assertEqual(kp_ingest.slide_trust(sql, [dict(g, state="corrected") for g in regs]), "high")
        self.assertEqual(kp_ingest.slide_trust(sql, [dict(g, state="disputed") for g in regs]), "low")
        thin = self.ing["db02"]["records"][2]
        self.assertEqual(kp_ingest.slide_trust(thin, [dict(g, state="unchecked") for g in regions_of(thin)]), "low")
        self.assertEqual(kp_ingest.slide_trust(thin, [dict(g, state="agreed") for g in regions_of(thin)]), "medium")

    def test_injection_flag(self):
        inj = self.ing["db02"]["records"][1]
        self.assertIn("injection_like", inj["flags"])
        self.assertIn("ko_ignore", inj["injection_patterns"])

    def test_detectors(self):
        self.assertTrue(kp_regions.is_code_like("SELECT a\nFROM t\nWHERE a > 1"))
        self.assertFalse(kp_regions.is_code_like("기본 키는 하나다.\n외래 키는 참조한다."))
        self.assertTrue(kp_regions.is_formula("y = wx + b"))
        self.assertTrue(kp_regions.is_formula("σ² = E[(X-μ)²]"))
        self.assertFalse(kp_regions.is_formula("기본 키 = 후보 키 중 하나를 선택한 것"))
        self.assertIn("pk", kp_regions.exact_tokens("PK는 NULL 불가, 3NF"))
        self.assertIn("3nf", kp_regions.exact_tokens("PK는 NULL 불가, 3NF"))


def _rec(n, title, verbatim=(), code=(), tables=(), diagrams=()):
    an = {"title": title, "slide_type": "concept", "code_blocks": [{"language": "sql", "code": c} for c in code],
          "verbatim_text": [{"region": "title", "text": title}] + [{"region": "body", "text": t} for t in verbatim],
          "tables": list(tables), "diagrams": list(diagrams)}
    return {"id": f"db09-s{n:03d}", "analysis": an, "boilerplate": [], "build_of": None}


def _builds(*recs):
    recs = list(recs)
    kp_ingest.detect_build_sequences(recs)
    kp_ingest.detect_boilerplate(recs)
    return [r["build_of"] for r in recs]


class BuildSequenceTests(unittest.TestCase):
    def test_same_title_different_code_tables_diagrams_is_no_build(self):
        # code lives only in code_blocks (the analyzer's own few-shot style), the second slide adds a line
        a = _rec(1, "JOIN 예제", code=["SELECT *\nFROM emp NATURAL JOIN dept;"])
        b = _rec(2, "JOIN 예제", ["왼쪽 테이블의 모든 행을 남긴다."], code=["SELECT *\nFROM emp LEFT OUTER JOIN dept ON emp.d = dept.d;"])
        self.assertEqual(_builds(a, b), [None, None])
        t1 = _rec(1, "정규화 예시", tables=[{"headers": ["학번", "과목"], "rows": [["1", "DB"]]}])
        t2 = _rec(2, "정규화 예시", ["※ 분해 후"], tables=[{"headers": ["학번", "이름"], "rows": [["1", "kim"]]}])
        self.assertEqual(_builds(t1, t2), [None, None])
        d1 = _rec(1, "ERD", diagrams=[{"elements": ["학생", "과목"], "relations": ["학생 -> 과목"]}])
        d2 = _rec(2, "ERD", ["설명"], diagrams=[{"elements": ["학생", "과목"], "relations": ["과목 -> 학생"]}])
        self.assertEqual(_builds(d1, d2), [None, None])
        # an earlier slide with nothing but its title is not a reveal step
        self.assertEqual(_builds(_rec(1, "JOIN 예제"), _rec(2, "JOIN 예제", ["본문"])), [None, None])

    def test_reveals_of_code_tables_and_diagrams_are_builds(self):
        c1 = _rec(1, "SELECT", code=["SELECT name\nFROM students"])
        c2 = _rec(2, "SELECT", code=["SELECT name\nFROM students\nWHERE age >= 30;"])
        self.assertEqual(_builds(c1, c2), [c2["id"], None])
        t1 = _rec(1, "비교", tables=[{"headers": ["구분", "DBMS"], "rows": [["중복", ""]]}])
        t2 = _rec(2, "비교", tables=[{"headers": ["구분", "DBMS"], "rows": [["중복", "최소화"], ["무결성", "제약"]]}])
        self.assertEqual(_builds(t1, t2), [t2["id"], None])
        d1 = _rec(1, "ERD", diagrams=[{"elements": ["학생", "수강"], "relations": ["학생 -> 수강"]}])
        d2 = _rec(2, "ERD", diagrams=[{"elements": ["학생", "수강", "과목"], "relations": ["학생 -> 수강", "수강 -> 과목"]}])
        self.assertEqual(_builds(d1, d2), [d2["id"], None])

    def test_reveal_bullets_are_not_boilerplate(self):
        steps = ["1NF: 원자값만 가진다.", "2NF: 부분 함수 종속을 제거한다.", "3NF: 이행 함수 종속을 제거한다.", "BCNF: 결정자가 후보키다."]
        for others in (0, 2, 4):
            recs = [_rec(i + 1, "정규형 단계", steps[:i + 1]) for i in range(4)]
            recs += [_rec(5 + j, f"주제 {j}", [f"내용 {j}"]) for j in range(others)]
            self.assertEqual(_builds(*recs)[:4], [recs[3]["id"]] * 3 + [None])
            self.assertEqual([r["boilerplate"] for r in recs[:4]], [[], [], [], []], others)
        # through ingest_lecture (order of the two detectors): a 3-step reveal in a 4-slide lecture
        spec = {"db01": {"slides": [("build1", "accepted"), ("build2", "accepted"), ("build3", "accepted"), ("sql", "accepted")]}}
        with tempfile.TemporaryDirectory() as t:
            recs = ingest_all(kp_fixtures.make_course(t, spec))["db01"]["records"]
        self.assertEqual([r["build_of"] for r in recs], ["db01-s003", "db01-s003", None, None])
        self.assertEqual([r["boilerplate"] for r in recs[:3]], [[2], [3], [4]])  # the labelled footer only


class AnalyzerChangeTests(unittest.TestCase):
    def test_results_and_ref_shas(self):
        spec = {"db01": {"slides": [("sql", "accepted"), ("table", "accepted"), ("erd", "missing")]}}
        with tempfile.TemporaryDirectory() as t:
            recs = ingest_all(kp_fixtures.make_course(t, spec))["db01"]["records"]
            adir = Path(t) / "db01" / "slide_analysis"
            self.assertEqual(recs[0]["results_sha"], kc.sha256_file(adir / "results" / "slide_001.json"))
            self.assertEqual(recs[0]["ref_sha"], kc.sha256_file(adir / "ref" / "slide_001.json"))
            self.assertIsNone(recs[2]["results_sha"])
            self.assertEqual([kp_ingest.analyzer_changed(r) for r in recs], [False, False, False])
            # an analyzer resolve corrects a cell (same image)
            kp_fixtures.set_analysis(t, "db01", "slide_002", kp_fixtures.mutate(recs[1]["analysis"], "swap_cells"))
            # a rescore rewrites the reference; a missing result appears
            ref = adir / "ref" / "slide_001.json"
            ref.write_text(ref.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            (adir / "results" / "slide_003.json").write_text((adir / "results" / "slide_001.json").read_text(encoding="utf-8"),
                                                              encoding="utf-8")
            self.assertEqual([kp_ingest.analyzer_changed(r) for r in recs], [True, True, True])
            legacy = {k: v for k, v in recs[0].items() if k not in ("results_sha", "ref_sha")}
            self.assertTrue(kp_ingest.analyzer_changed(legacy))  # ingested before the shas were recorded


class PdfAnalyzerLayoutTests(unittest.TestCase):
    def test_pdf_output_is_not_stale(self):
        spec = {"db01": {"slides": [("sql", "accepted"), ("table", "accepted"), ("erd", "accepted")]}}
        with tempfile.TemporaryDirectory() as t:
            cj = kp_fixtures.make_pdf_course(t, spec)
            ing = ingest_all(cj)["db01"]
            recs = ing["records"]
            self.assertEqual([r["file"] for r in recs], ["page_0001.png", "page_0002.png", "page_0003.png"])
            self.assertEqual([r["provenance"] for r in recs], ["ok", "unverifiable", "unverifiable"])
            self.assertEqual([r["flags"] for r in recs], [[], [], []])
            self.assertEqual(ing["warnings"], [])
            self.assertEqual({r["text_check"] for r in recs}, {"ocr_verified"})
            self.assertTrue(all(r["digits_checked"] for r in recs))  # a text layer is a trusted reference
            agreed = [dict(g, state="agreed") for g in regions_of(recs[1])]
            self.assertEqual(kp_ingest.slide_trust(recs[1], agreed), "high")
            # a result that names another PDF
            res = Path(t) / "db01_analysis" / "results" / "page_0001.json"
            res.write_text(res.read_text(encoding="utf-8").replace('"file": "lecture.pdf"', '"file": "other.pdf"'),
                           encoding="utf-8")
            self.assertEqual([r["provenance"] for r in ingest_all(cj)["db01"]["records"]], ["stale", "unverifiable", "unverifiable"])
            # the PDF was replaced (source.json) but the text-layer references are from the old one
            src = Path(t) / "db01_analysis" / "source.json"
            src.write_text(src.read_text(encoding="utf-8").replace("pdf-sha-1", "pdf-sha-2"), encoding="utf-8")
            self.assertEqual([r["provenance"] for r in ingest_all(cj)["db01"]["records"]], ["stale", "stale", "stale"])


class OrderAndRenumberTests(unittest.TestCase):
    def test_natural_order_join_by_stem(self):
        spec = {"db01": {"slides": [("sql", "accepted"), ("table", "accepted"), ("erd", "accepted")]}}
        names = {"db01": ["slide_1.png", "slide_2.png", "slide_10.png"]}
        with tempfile.TemporaryDirectory() as t:
            ing = ingest_all(kp_fixtures.make_course(t, spec, names=names))["db01"]
        recs = ing["records"]
        self.assertEqual([r["file"] for r in recs], ["slide_1.png", "slide_2.png", "slide_10.png"])
        self.assertEqual(recs[2]["analysis"]["file"], "slide_10.png")
        self.assertEqual(recs[2]["analyzer_page"], 2)  # plain sort put slide_10 second
        self.assertTrue(any(w["code"] == "order_differs" for w in ing["warnings"]))

    def test_renumber_diff(self):
        self.assertIsNone(kp_ingest.renumber_diff([], ["a"]))
        self.assertIsNone(kp_ingest.renumber_diff(["a", "b", "c"], ["a", "b", "c"]))
        self.assertIsNone(kp_ingest.renumber_diff(["a", "b", "c"], ["a", "B", "c"]))  # edit in place keeps ids
        d = kp_ingest.renumber_diff(["a", "b", "c"], ["a", "x", "b", "c"])
        self.assertEqual(d["moved"], [[2, 3], [3, 4]])
        self.assertEqual(d["added"], [2])
        d = kp_ingest.renumber_diff(["a", "b", "c"], ["a", "c"])
        self.assertEqual(d["removed"], [2])


if __name__ == "__main__":
    unittest.main()

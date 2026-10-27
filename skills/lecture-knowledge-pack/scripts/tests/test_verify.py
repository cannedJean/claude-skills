"""The struct stage end to end on fixtures: localisation, blind batches, the 2-of-3 vote, the canonical overlay,
and the seeded-error acceptance test (errors the upstream scorer accepts must end `corrected`)."""
from __future__ import annotations

import copy
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import fake_reader  # noqa: E402
import kp_common as kc  # noqa: E402
import kp_fixtures  # noqa: E402
import kp_ingest  # noqa: E402
import kp_regions  # noqa: E402
import kp_render  # noqa: E402
import kp_gate  # noqa: E402
import kp_synth  # noqa: E402
import kp_verify  # noqa: E402

SPEC = {"db01": {"title": "1강", "slides": [("sql", "accepted"), ("table", "accepted"), ("erd", "accepted"),
                                           ("formula", "accepted"), ("graphic", "thin")]}}
KINDS = ["sql", "table", "erd", "formula", "graphic"]
SQL = "SELECT name\nFROM students\nWHERE age >= 30;"


class Deck:
    """A fixture course from any spec; analyses: {slide id: analysis} replacing TRUTH for that slide."""

    def __init__(self, root, spec, engine="two", analyses=None):
        self.root = Path(root)
        self.cj = kp_fixtures.make_course(root, spec, engine=engine)
        self.truth = {}
        for lec, ls in spec.items():
            for i, (kind, _) in enumerate(ls["slides"], start=1):
                self.truth[f"{lec}-s{i:03d}"] = dict(copy.deepcopy(kp_fixtures.TRUTH[kind]), page=i, file=f"slide_{i:03d}.png")
        for sid, an in (analyses or {}).items():
            self.set(sid, an, reingest=False)
        self.reingest()

    def set(self, sid, an, reingest=True):
        n = int(sid[-3:])
        kp_fixtures.set_analysis(self.root, sid.rsplit("-s", 1)[0], f"slide_{n:03d}",
                                 dict(copy.deepcopy(an), page=n, file=f"slide_{n:03d}.png"))
        if reingest:
            self.reingest()

    def reingest(self):
        self.course = kc.load_course(self.cj)
        self.refs, self.recs = {}, {}
        for lec in self.course["lectures"]:
            ing = kp_ingest.ingest_lecture(lec)
            kc.write_json(self.course["work"] / "ingest" / f"{lec['id']}.json", ing)
            for r in ing["records"]:
                self.recs[r["id"]], self.refs[r["id"]] = r, kc.read_json(r["ref_path"]) if r.get("ref_path") else None

    def advance(self, lectures=None, **opts):
        lecs = [l for l in self.course["lectures"] if lectures is None or l["id"] in lectures]
        return kp_verify.advance(self.course, lecs, opts)

    def state(self):
        return kp_verify.load_state(self.course["work"])

    def answer_pending(self, mode="truthful", only=None):
        st = self.state()
        n = 0
        for bid, b in st["batches"].items():
            if b["status"] == "pending":
                fake_reader.answer_batch(self.course["work"] / "struct" / "batches" / f"{bid}.json", self.truth,
                                         self.refs, st, mode=mode, only=only)
                n += 1
        return n

    def answer(self, fn):
        """fn(region) -> answer or None, for every pending batch."""
        st = self.state()
        for bid, b in st["batches"].items():
            if b["status"] != "pending":
                continue
            out = {}
            for item in b["items"]:
                ans = {slot: a for slot, rid in item["regions"].items() for a in [fn(st["regions"][rid])] if a is not None}
                if ans:
                    out[item["id"]] = ans["_"] if item["type"] == "crop" else ans
            self.reads(bid).parent.mkdir(parents=True, exist_ok=True)
            self.reads(bid).write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")

    def run(self, mode="truthful", rounds=3):
        res = self.advance()
        for _ in range(rounds):
            if not res["pending_batches"]:
                break
            self.answer_pending(mode)
            res = self.advance()
        return res

    def reads(self, bid):
        return self.course["work"] / "struct" / "reads" / f"{bid}.json"

    def batch_md(self, bid):
        return (self.course["work"] / "struct" / "batches" / f"{bid}.md").read_text(encoding="utf-8")

    def region(self, rid):
        return self.state()["regions"][rid]

    def states(self):
        return {rid: r["state"] for rid, r in self.state()["regions"].items()}

    def overlay(self):
        return kc.read_json(self.course["work"] / "struct" / "overlay.json")

    def verbatim(self, sid):
        return [v["text"] for v in self.overlay()[sid]["analysis"]["verbatim_text"]]

    def trust(self, sid):
        """slide_trust from the struct overlay, as kp_render.prepare computes it."""
        ov, rec = self.overlay()[sid], self.recs[sid]
        regs = kp_regions.select_regions(rec, self.refs[sid])
        for g in regs:
            g["state"] = (ov["regions"].get(g["local"]) or {}).get("state", "unchecked")
        return kp_ingest.slide_trust(rec, regs, {"digits_checked_by_pack": ov["digits_checked_by_pack"],
                                                 "uncertain_remaining": ov["uncertain_remaining"]})


class Course(Deck):
    def __init__(self, root, mutations=None, engine="two"):
        base = {f"db01-s{i:03d}": dict(copy.deepcopy(kp_fixtures.TRUTH[k]), page=i, file=f"slide_{i:03d}.png")
                for i, k in enumerate(KINDS, start=1)}
        super().__init__(root, SPEC, engine, {sid: kp_fixtures.mutate(base[sid], how) for sid, how in (mutations or {}).items()})


def inside(ref, box, pad=14):
    """Texts of the OCR lines a crop of box shows completely."""
    l, t, r, b = box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad
    return [ln["text"] for ln in ref["lines"] if ln["box"][0] >= l and ln["box"][1] >= t
            and ln["box"][0] + ln["box"][2] <= r and ln["box"][1] + ln["box"][3] <= b]


# --------------------------------------------------------------------------- custom slides
def _draw(texts, size=None, boxes=(), graphic=None):
    """texts: [(x, y, text, font, size)]; size overrides kp_synth's 1280x720 for this slide only."""
    def draw(path):
        old = (kp_synth.W, kp_synth.H)
        kp_synth.W, kp_synth.H = size or old
        try:
            s = kp_synth.Slide(path)
            for b in boxes:
                s.d.rectangle(b, outline="black", width=4)
            for x, y, t, f, sz in texts:
                s.text((x, y), t, f, sz)
            s.graphic_box = graphic
            return s.save()
        finally:
            kp_synth.W, kp_synth.H = old
    return draw


B, F, M = kp_synth.BOLD, kp_synth.FONTS, kp_synth.MONO


def _custom(key, texts, analysis, **kw):
    kp_fixtures.DRAW[key] = _draw(texts, **kw)
    kp_fixtures.TRUTH[key] = analysis


def _vt(*texts):
    return [{"region": "title" if i == 0 else "body", "text": t} for i, t in enumerate(texts)]


_custom("t_twoblk", [(60, 40, "틀린 예와 맞는 예", B, 44)] + [(80, 160 + i * 44, l, M, 30) for i, l in enumerate(SQL.split("\n"))]
        + [(80, 420 + i * 44, l, M, 30) for i, l in enumerate(SQL.replace(">=", ">").split("\n"))],
        kp_fixtures._a("code", "틀린 예와 맞는 예", _vt("틀린 예와 맞는 예"), code_blocks=[
            {"language": "sql", "code": SQL}, {"language": "sql", "code": SQL.replace(">=", ">")}]))
_custom("t_twof", [(60, 40, "두 모델", B, 48), (80, 200, "y = wx + b", M, 44), (80, 400, "y = wx - b", M, 44)],
        kp_fixtures._a("concept", "두 모델", _vt("두 모델", "y = wx + b", "y = wx - b")))
_custom("t_insprose", [(60, 40, "행 추가", B, 48), (60, 120, "INSERT 문으로 테이블 t에 행을 추가한다.", F, 28),
                       (80, 200, "INSERT INTO t VALUES (1);", M, 30), (80, 288, "INSERT INTO t VALUES (2);", M, 30)],
        kp_fixtures._a("code", "행 추가", _vt("행 추가", "INSERT 문으로 테이블 t에 행을 추가한다."),
                       code_blocks=[{"language": "sql", "code": "INSERT INTO t VALUES (1);"}]))
_custom("t_create", [(60, 40, "테이블 만들기", B, 48), (80, 200, "CREATE TABLE t (id INT);", M, 30),
                     (80, 288, "INSERT INTO t VALUES (1);", M, 30)],
        kp_fixtures._a("code", "테이블 만들기", _vt("테이블 만들기"), code_blocks=[{"language": "sql", "code": "CREATE TABLE t (id INT);"}]))
_custom("t_caption", [(60, 40, "SQL 조회", B, 48), (80, 186, "다음 쿼리를 실행한다:", F, 30)]
        + [(80, 230 + i * 44, l, M, 30) for i, l in enumerate(SQL.split("\n"))],
        kp_fixtures._a("code", "SQL 조회", _vt("SQL 조회", "다음 쿼리를 실행한다:"), code_blocks=[{"language": "sql", "code": SQL}]))
_custom("t_vals", [(60, 40, "측정값", B, 48), (60, 160, "항목 1: 값 101", F, 30), (60, 220, "항목 2: 값 102", F, 30)],
        kp_fixtures._a("concept", "측정값", _vt("측정값", "항목 1: 값 201\n항목 2: 값 202")))
_custom("t_eight", [(60, 30, "측정값", B, 40)] + [(60, 90 + k * 60, f"항목 {k}: 값 {100 + k}", F, 30) for k in range(1, 9)],
        kp_fixtures._a("concept", "측정값", _vt("측정값", *[f"항목 {k}: 값 {200 + k}" for k in range(1, 9)])))
_custom("t_twodg", [(60, 40, "시스템 구성과 ERD", B, 44), (110, 195, "클라이언트", F, 26), (480, 195, "서버", F, 26)]
        + [(x + 60, 460, n, F, 30) for x, n in ((150, "학생"), (560, "수강"), (970, "과목"))],
        kp_fixtures._a("diagram", "시스템 구성과 ERD", _vt("시스템 구성과 ERD"), diagrams=[
            {"kind": "architecture", "description": "상자 두 개", "elements": ["클라이언트", "서버"], "relations": []},
            {"kind": "erd", "description": "ERD", "elements": ["학생", "수강", "과목"], "relations": ["학생 -> 수강 : 1:N", "수강 -> 과목"]}]),
        boxes=[(80, 160, 280, 260), (420, 160, 620, 260), (150, 420, 350, 540), (560, 420, 760, 540), (970, 420, 1170, 540)])
_custom("t_bigsql", [(120, 80, "4장. SQL 조회", B, 96)] + [(2000, 1200 + i * 88, l, M, 60) for i, l in enumerate(SQL.split("\n"))],
        kp_fixtures._a("code", "4장. SQL 조회", _vt("4장. SQL 조회"), code_blocks=[{"language": "sql", "code": SQL}]),
        size=(3136, 1764))
FIN = [["연도", "매출", "이익"], ["2019", "120", "35"], ["2020", "150", "40"]]
_custom("t_fin", [(60, 40, "연도별 실적", B, 44)] + [(80 + k * 200, 180 + r * 60, v, F, 28) for r, row in enumerate(FIN) for k, v in enumerate(row)],
        kp_fixtures._a("table", "연도별 실적", _vt("연도별 실적"), tables=[{"caption": None, "headers": FIN[0], "rows": FIN[1:]}]))
_custom("t_cs", [(380, 330, "클라이언트", F, 30), (810, 330, "서버", F, 30)],
        kp_fixtures._a("other", None, [{"region": "diagram_label", "text": "클라이언트"}], summary="상자 두 개."),
        boxes=[(300, 200, 600, 500), (700, 220, 1000, 480)], graphic=[300, 200, 700, 300])


def one(key, scenario="accepted"):
    return {"db01": {"title": "1강", "slides": [(key, scenario)]}}


class LocaliseTests(unittest.TestCase):
    def test_crop_boxes_contain_the_region(self):
        with tempfile.TemporaryDirectory() as t:
            c = Course(t)
            ing = kc.read_json(c.course["work"] / "ingest" / "db01.json")
            recs = {r["id"]: r for r in ing["records"]}
            sql = recs["db01-s001"]
            reg = next(g for g in kp_regions.select_regions(sql, c.refs[sql["id"]]) if g["kind"] == "code")
            loc = kp_regions.locate(reg, sql, c.refs[sql["id"]])
            self.assertEqual(loc["mode"], "crop")
            code_lines = [l["box"] for l in c.refs[sql["id"]]["lines"] if l["text"].startswith(("SELECT", "FROM", "WHERE"))]
            for l, t_, w, h in code_lines:
                self.assertLessEqual(loc["box"][0], l)
                self.assertGreaterEqual(loc["box"][2], l + w)
                self.assertLessEqual(loc["box"][1], t_)
                self.assertGreaterEqual(loc["box"][3], t_ + h)
            tbl = recs["db01-s002"]
            reg = next(g for g in kp_regions.select_regions(tbl, c.refs[tbl["id"]]) if g["kind"] == "table")
            self.assertEqual(kp_regions.locate(reg, tbl, c.refs[tbl["id"]])["mode"], "crop")
            erd = recs["db01-s003"]
            reg = next(g for g in kp_regions.select_regions(erd, c.refs[erd["id"]]) if g["kind"] == "diagram")
            self.assertEqual(kp_regions.locate(reg, erd, c.refs[erd["id"]])["mode"], "view")

    def test_skipped_line_still_inside_code_crop(self):
        """An analyst that dropped the WHERE line: the crop grows over adjacent OCR lines, so the reader sees it."""
        with tempfile.TemporaryDirectory() as t:
            c = Course(t)
            ing = kc.read_json(c.course["work"] / "ingest" / "db01.json")
            sql = copy.deepcopy(ing["records"][0])
            sql["analysis"]["code_blocks"][0]["code"] = "SELECT name\nFROM students"
            reg = next(g for g in kp_regions.select_regions(sql, c.refs[sql["id"]]) if g["kind"] == "code")
            loc = kp_regions.locate(reg, sql, c.refs[sql["id"]])
            where = next(l["box"] for l in c.refs[sql["id"]]["lines"] if l["text"].startswith("WHERE"))
            self.assertGreaterEqual(loc["box"][3], where[1] + where[3])


class FlowTests(unittest.TestCase):
    def test_truthful_reader_agrees_everything(self):
        with tempfile.TemporaryDirectory() as t:
            c = Course(t)
            res = c.advance()
            self.assertTrue(res["pending_batches"])
            md = Path(res["pending_batches"][0]).read_text(encoding="utf-8")
            for secret in ("WHERE age >= 30", "파일 시스템", "학생 -> 수강", "y = wx + b"):
                self.assertNotIn(secret, md, "a batch must not leak the analysis or OCR text")
            c.answer_pending()
            res = c.advance()
            self.assertEqual(res["pending_batches"], [])
            self.assertEqual(set(c.states().values()), {"agreed"})
            again = c.advance()  # idempotent: nothing new is queued
            self.assertEqual(again["new_batches"], [])

    def test_seeded_errors_end_corrected(self):
        """The four blind spots of the character-coverage scorer."""
        muts = {"db01-s001": "operator", "db01-s002": "swap_cells", "db01-s003": "reverse_edge", "db01-s004": "formula"}
        with tempfile.TemporaryDirectory() as t:
            c = Course(t, muts)
            c.advance()
            c.answer_pending()
            r2 = c.advance()
            reg = c.state()["regions"]["db01-s001.code1"]
            self.assertEqual((reg["state"], reg["round"]), ("queued", 2))
            self.assertTrue(r2["pending_batches"], "mismatches must go to a second, separate batch")
            first = {i["id"] for b in c.state()["batches"].values() if b["round"] == 1 for i in b["items"]}
            self.assertNotIn(reg["item"], first)
            c.answer_pending()
            c.advance()
            st = c.states()
            for rid in ("db01-s001.code1", "db01-s002.table1", "db01-s003.diagram1", "db01-s004.formula2"):
                self.assertEqual(st[rid], "corrected", (rid, st))
            ov = c.overlay()
            self.assertEqual(ov["db01-s001"]["analysis"]["code_blocks"][0]["code"], c.truth["db01-s001"]["code_blocks"][0]["code"])
            self.assertEqual(ov["db01-s002"]["analysis"]["tables"][0]["rows"], c.truth["db01-s002"]["tables"][0]["rows"])
            self.assertIn("학생 -> 수강 : 1:N", ov["db01-s003"]["analysis"]["diagrams"][0]["relations"])
            self.assertEqual(ov["db01-s004"]["analysis"]["verbatim_text"][1]["text"], "y = wx + b")

    def test_noisy_first_read_upheld_by_second(self):
        with tempfile.TemporaryDirectory() as t:
            c = Course(t)
            c.advance()
            c.answer_pending(mode="noisy")
            c.advance()
            c.answer_pending(mode="truthful")
            c.advance()
            st = c.states()
            self.assertEqual(st["db01-s001.code1"], "agy_upheld")
            self.assertEqual(st["db01-s003.diagram1"], "agy_upheld")

    def test_disputed_when_no_majority(self):
        muts = {"db01-s001": "operator"}
        with tempfile.TemporaryDirectory() as t:
            c = Course(t, muts)
            c.advance()
            c.answer_pending(mode="truthful")   # read 1 = truth ('>=') vs agy ('<')
            c.advance()
            c.answer_pending(mode="noisy")      # read 2 = '>'  -> three different versions
            c.advance()
            self.assertEqual(c.states()["db01-s001.code1"], "disputed")
            ing = kc.read_json(c.course["work"] / "ingest" / "db01.json")
            rec = ing["records"][0]
            regs = kp_render.prepare(kp_render.build_context(c.course, c.course["lectures"], {}))["views"][0]["regions"]
            self.assertEqual(kp_ingest.slide_trust(rec, regs), "low")

    def test_not_found_requeues_as_view_then_final(self):
        with tempfile.TemporaryDirectory() as t:
            c = Course(t)
            c.advance()
            c.answer_pending(mode="not_found")
            c.advance()
            st = c.state()["regions"]["db01-s001.code1"]
            self.assertEqual((st["state"], st["mode"]), ("queued", "view"))
            c.answer_pending(mode="not_found")
            c.advance()
            self.assertEqual(c.states()["db01-s001.code1"], "not_found")

    def test_unanswered_items_are_requeued(self):
        with tempfile.TemporaryDirectory() as t:
            c = Course(t)
            c.advance()
            c.answer_pending(only={"code1"})
            res = c.advance()
            self.assertEqual(c.states()["db01-s001.code1"], "agreed")
            self.assertTrue(res["new_batches"], "unanswered regions go into a new batch")

    def test_budget_and_skip(self):
        with tempfile.TemporaryDirectory() as t:
            c = Course(t)
            c.advance(budget_tokens=1)
            self.assertEqual(set(c.states().values()), {"skipped_budget"})
            c.advance(force=True)
            self.assertIn("queued", set(c.states().values()))
        with tempfile.TemporaryDirectory() as t:
            c = Course(t)
            res = c.advance(skip=True)
            self.assertEqual(res["pending_batches"], [])
            self.assertEqual(set(c.states().values()), {"skipped"})

    def test_changed_analysis_requeues_only_that_region(self):
        with tempfile.TemporaryDirectory() as t:
            c = Course(t)
            c.advance()
            c.answer_pending()
            c.advance()
            kp_fixtures.set_analysis(t, "db01", "slide_001", kp_fixtures.mutate(c.truth["db01-s001"], "operator"))
            lec = c.course["lectures"][0]
            kc.write_json(c.course["work"] / "ingest" / "db01.json", kp_ingest.ingest_lecture(lec))
            c.advance()
            st = c.states()
            self.assertEqual(st["db01-s001.code1"], "queued")
            self.assertEqual(st["db01-s002.table1"], "agreed")


class SingleEngineTests(unittest.TestCase):
    def test_line_regions_and_digits_by_pack(self):
        with tempfile.TemporaryDirectory() as t:
            c = Course(t, engine="winocr")
            c.advance()
            ov = c.overlay()
            # winocr only and nothing missing: the pack's line selection found no gap -> digits checked by the pack
            self.assertTrue(ov["db01-s004"]["digits_checked_by_pack"])
            c.answer_pending()
            c.advance()
            ctx = kp_render.build_context(c.course, c.course["lectures"], {})
            trust = {v["id"]: v["trust"] for v in kp_render.prepare(ctx)["views"]}
            self.assertEqual(trust["db01-s001"], "high")


class BuildAfterStructTests(unittest.TestCase):
    def test_corrected_values_reach_the_card_and_gate_passes(self):
        with tempfile.TemporaryDirectory() as t:
            c = Course(t, {"db01-s001": "operator"})
            c.advance()
            c.answer_pending()
            c.advance()
            c.answer_pending()
            c.advance()
            ctx = kp_render.build_context(c.course, c.course["lectures"], {"allow_pending": False})
            prep = kp_render.prepare(ctx)
            out = Path(t) / "pkg"
            kp_render.render(ctx, prep, out)
            gate = kp_gate.run_gate(out, ctx, prep)
            # every struct region is final; only the (not yet run) integration blocks publishing
            self.assertEqual({x["id"] for x in gate["checks"] if not x["ok"]}, {"G11"})
            card = (out / "lectures/db01/cards/db01-s001.md").read_text(encoding="utf-8")
            self.assertIn("WHERE age >= 30;", card)
            self.assertIn("검증: corrected", card)
            self.assertTrue((out / "evidence/db01/db01-s001.code1.png").is_file())
            stored = json.loads((out / "lectures/db01/slides/db01-s001.json").read_text(encoding="utf-8"))
            self.assertIn("WHERE age < 30;", stored["analysis"]["code_blocks"][0]["code"])  # original kept as is


class JointLocateTests(unittest.TestCase):
    """Crops come from one operator-aware assignment of the analysis' lines to OCR lines per slide."""

    def test_near_identical_blocks_get_their_own_crops(self):
        with tempfile.TemporaryDirectory() as t:
            d = Deck(t, {"db01": {"title": "1강", "slides": [("t_twoblk", "accepted"), ("t_twof", "accepted")]}})
            d.advance()
            seen = {rid: inside(d.refs[r["slide"]], r["box"]) for rid, r in d.state()["regions"].items()}
            self.assertEqual(seen["db01-s001.code1"], SQL.split("\n"))
            self.assertEqual(seen["db01-s001.code2"], SQL.replace(">=", ">").split("\n"))
            self.assertEqual((seen["db01-s002.formula2"], seen["db01-s002.formula3"]), (["y = wx + b"], ["y = wx - b"]))
            d.run("visible")
            self.assertEqual(set(d.states().values()), {"agreed"})
            self.assertEqual(d.verbatim("db01-s002"), ["두 모델", "y = wx + b", "y = wx - b"])

    def test_identical_blocks_are_assigned_in_reading_order(self):
        """The analyst copied block 1 into block 2: block 2's crop still shows block 2, so the copy is corrected."""
        an = copy.deepcopy(kp_fixtures.TRUTH["t_twoblk"])
        an["code_blocks"][1]["code"] = SQL
        with tempfile.TemporaryDirectory() as t:
            d = Deck(t, one("t_twoblk"), analyses={"db01-s001": an})
            d.run("visible")
            self.assertEqual(d.states(), {"db01-s001.code1": "agreed", "db01-s001.code2": "corrected"})
            self.assertEqual([b["code"] for b in d.overlay()["db01-s001"]["analysis"]["code_blocks"]], [SQL, SQL.replace(">=", ">")])

    def test_prose_word_does_not_anchor_the_code_crop(self):
        with tempfile.TemporaryDirectory() as t:
            d = Deck(t, one("t_insprose"))
            d.run("visible")
            r = d.region("db01-s001.code1")
            self.assertEqual(inside(d.refs["db01-s001"], r["box"]), ["INSERT INTO t VALUES (1);", "INSERT INTO t VALUES (2);"])
            self.assertEqual(d.overlay()["db01-s001"]["analysis"]["code_blocks"][0]["code"],
                             "INSERT INTO t VALUES (1);\nINSERT INTO t VALUES (2);")

    def test_code_crop_reaches_a_statement_after_a_blank_line(self):
        """Two engines: the upstream scorer accepts the omission; one engine: the line region agrees once the code
        correction holds the line."""
        for engine in ("two", "winocr"):
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as t:
                d = Deck(t, one("t_create"), engine=engine)
                d.run("visible")
                self.assertEqual(d.overlay()["db01-s001"]["analysis"]["code_blocks"][0]["code"],
                                 "CREATE TABLE t (id INT);\nINSERT INTO t VALUES (1);")
                want = {"db01-s001.code1": "corrected"}
                if engine == "winocr":
                    want["db01-s001.line1"] = "agreed"
                self.assertEqual(d.states(), want)
                self.assertEqual(d.trust("db01-s001"), "high")

    def test_uncertain_crop_is_only_its_own_line(self):
        an = copy.deepcopy(kp_fixtures.TRUTH["build2"])
        an["verbatim_text"][1]["text"] = "1NF: 원자값만 [?]다."
        with tempfile.TemporaryDirectory() as t:
            d = Deck(t, one("build2"), analyses={"db01-s001": an})
            d.advance()
            self.assertEqual(inside(d.refs["db01-s001"], d.region("db01-s001.q2")["box"]), ["1NF: 원자값만 가진다."])
            d.run("visible")
            self.assertEqual(d.states(), {"db01-s001.q2": "filled"})
            self.assertEqual(d.verbatim("db01-s001"), ["정규화 단계", "1NF: 원자값만 가진다.", "2NF: 부분 함수 종속을 제거한다.",
                                                       "SSAFY 데이터베이스"])

    def test_caption_above_code_stays_out_of_the_crop(self):
        """Transcribed, the caption is claimed by its own item; not transcribed, it is prose, not code."""
        silent = dict(copy.deepcopy(kp_fixtures.TRUTH["t_caption"]), verbatim_text=_vt("SQL 조회"))
        for analyses in ({}, {"db01-s001": silent}):
            with self.subTest(caption_transcribed=not analyses), tempfile.TemporaryDirectory() as t:
                d = Deck(t, one("t_caption"), analyses=analyses)
                d.run("visible")
                self.assertEqual(inside(d.refs["db01-s001"], d.region("db01-s001.code1")["box"]), SQL.split("\n"))
                self.assertEqual(d.states(), {"db01-s001.code1": "agreed"})

    def test_neighbour_lines_in_a_read_are_not_copied_into_the_item(self):
        an = copy.deepcopy(kp_fixtures.TRUTH["build2"])
        an["verbatim_text"][1]["text"] = "1NF: 원자값만 [?]다."
        kp_verify.apply_canonical(an, {"kind": "uncertain", "part": ["verbatim_text", 1]},
                                  {"text": "1NF: 원자값만 가진다.\n2NF: 부분 함수 종속을 제거한다."})
        self.assertEqual(an["verbatim_text"][1]["text"], "1NF: 원자값만 가진다.")
        an = copy.deepcopy(kp_fixtures.TRUTH["t_caption"])
        kp_verify.apply_canonical(an, {"kind": "code", "part": ["code_blocks", 0]},
                                  {"language": "sql", "code": "다음 쿼리를 실행한다:\n" + SQL.replace(">=", ">")})
        self.assertEqual(an["code_blocks"][0]["code"], SQL.replace(">=", ">"))

    def test_split_assignment_falls_back_to_view(self):
        """The analyst's two lines bind equally well in two places far apart: no crop can be trusted."""
        rec = {"id": "x-s001", "analysis": kp_fixtures._a("code", None, [], code_blocks=[
            {"language": "sql", "code": "SELECT a\nFROM u"}])}
        lines = [("SELECT a", 100), ("FROM t", 140), ("SELECT b", 500), ("FROM u", 540)]
        ref = {"size": [1280, 720], "lines": [{"id": f"L{i}", "text": s, "box": [80, y, 200, 28], "words": []}
                                              for i, (s, y) in enumerate(lines)]}
        reg = kp_regions.select_regions(rec)[0]
        self.assertEqual(kp_regions.locate(reg, rec, ref)["mode"], "view")
        ref["lines"][3]["text"] = "FROM t"  # now the first block holds both lines
        loc = kp_regions.locate(reg, rec, ref)
        self.assertEqual((loc["mode"], loc["box"][1], loc["box"][3]), ("crop", 100, 168))


class StateMachineTests(unittest.TestCase):
    def test_kinds_only_filters_new_work(self):
        with tempfile.TemporaryDirectory() as t:
            c = Course(t, {"db01-s002": "swap_cells"})
            c.advance(kinds="code")
            self.assertEqual((c.states()["db01-s001.code1"], c.states()["db01-s002.table1"]), ("queued", "new"))
            c.run()
            self.assertEqual(c.states()["db01-s002.table1"], "corrected")
            before = c.overlay()["db01-s002"]
            self.assertEqual(c.advance(kinds="code")["new_batches"], [])
            self.assertEqual(c.states()["db01-s002.table1"], "corrected")
            self.assertEqual(c.overlay()["db01-s002"], before)
            self.assertEqual(c.advance()["new_batches"], [], "nothing is paid for twice")

    def test_line_region_restarts_when_its_target_moves(self):
        bad = kp_fixtures.mutate(kp_fixtures.TRUTH["build2"], "nf")
        with tempfile.TemporaryDirectory() as t:
            d = Deck(t, one("build2"), engine="winocr", analyses={"db01-s001": bad})
            d.run()
            r = d.region("db01-s001.line1")
            self.assertEqual((r["state"], r["target"]), ("corrected", "verbatim_text[2]"))
            vt = bad["verbatim_text"]
            d.set("db01-s001", dict(bad, verbatim_text=[vt[0], vt[1], vt[3], vt[2]]))  # re-run: footer before 3NF
            self.assertTrue(d.advance()["new_batches"])
            self.assertEqual(d.region("db01-s001.line1")["target"], "verbatim_text[3]")
            self.assertFalse(d.overlay()["db01-s001"]["digits_checked_by_pack"])
            d.run()
            self.assertEqual(d.verbatim("db01-s001"), ["정규화 단계", "1NF: 원자값만 가진다.", "SSAFY 데이터베이스",
                                                       "2NF: 부분 함수 종속을 제거한다."])

    def test_visual_region_restarts_when_the_analysis_changes(self):
        both = {"lines": ["클라이언트", "서버"], "nodes": [], "edges": []}
        with tempfile.TemporaryDirectory() as t:
            d = Deck(t, one("t_cs", "thin"))
            d.advance()
            d.answer(lambda r: both)
            d.advance()
            self.assertEqual(d.states(), {"db01-s001.visual1": "missed_content"})
            an = copy.deepcopy(kp_fixtures.TRUTH["t_cs"])
            an["verbatim_text"].append({"region": "diagram_label", "text": "서버"})
            d.set("db01-s001", an)
            self.assertTrue(d.advance()["new_batches"])
            d.answer(lambda r: both)
            d.advance()
            self.assertEqual(d.states(), {"db01-s001.visual1": "agreed"})
            self.assertIsNone(d.overlay()["db01-s001"]["extra_read"])

    def test_view_questions_count_every_object_and_use_view_pixels(self):
        with tempfile.TemporaryDirectory() as t:
            d = Deck(t, {"db01": {"title": "1강", "slides": [("t_twodg", "accepted"), ("t_bigsql", "accepted")]}})
            res = d.advance()
            md = "\n".join(Path(p).read_text(encoding="utf-8") for p in res["pending_batches"])
            self.assertIn("`diagram2` (diagram): 슬라이드의 도식 2개 중 2번째", md)
            d.answer(lambda r: {"not_found": True} if r["kind"] == "code" else None)
            res = d.advance()
            r = d.region("db01-s002.code1")
            self.assertEqual((r["state"], r["mode"]), ("queued", "view"))
            md = "\n".join(Path(p).read_text(encoding="utf-8") for p in res["pending_batches"])
            q = next(l for l in md.splitlines() if "`code1`" in l)
            w, h, l, rr, tp, b = map(int, re.search(r"이 이미지 (\d+)x(\d+) px 기준 대략 x (\d+)~(\d+), y (\d+)~(\d+)", q).groups())
            self.assertEqual((w, h), (1568, 882))
            self.assertTrue(0 <= l < rr <= w and 0 <= tp < b <= h, q)
            self.assertAlmostEqual(l, r["box"][0] / 2, delta=1)

    def test_two_lines_of_one_item_are_both_corrected(self):
        with tempfile.TemporaryDirectory() as t:
            d = Deck(t, one("t_vals"), engine="winocr")
            d.run()
            self.assertEqual(d.states(), {"db01-s001.line1": "corrected", "db01-s001.line2": "corrected"})
            self.assertEqual(d.verbatim("db01-s001")[1], "항목 1: 값 101\n항목 2: 값 102")

    def test_two_header_rows_keep_every_row(self):
        with tempfile.TemporaryDirectory() as t:
            c = Course(t, {"db01-s002": "swap_cells"})
            tb = c.truth["db01-s002"]["tables"][0]
            c.advance()
            for _ in range(2):
                c.answer(lambda r: {"rows": [tb["headers"]] + tb["rows"], "header_rows": 2} if r["kind"] == "table" else None)
                c.advance()
            self.assertEqual(c.states()["db01-s002.table1"], "corrected")
            got = c.overlay()["db01-s002"]["analysis"]["tables"][0]
            self.assertEqual((got["headers"], got["rows"]), (tb["headers"], tb["rows"]))
        an = {"tables": [{"headers": [], "rows": []}]}
        kp_verify.apply_canonical(an, {"kind": "table", "part": ["tables", 0]},
                                  {"rows": [["a", "b"], ["c", "d"], ["e", "f"]], "header_rows": 2})
        self.assertEqual(an["tables"][0], {"headers": ["a", "b"], "rows": [["c", "d"], ["e", "f"]]})

    def test_line_cap_leaves_digits_unchecked(self):
        with tempfile.TemporaryDirectory() as t:
            d = Deck(t, one("t_eight"), engine="winocr")
            self.assertEqual(kp_regions.unchecked_lines(d.recs["db01-s001"], d.refs["db01-s001"]), 2)
            d.run()
            self.assertEqual(set(d.states().values()), {"corrected"})
            self.assertFalse(d.overlay()["db01-s001"]["digits_checked_by_pack"])
            self.assertEqual(d.trust("db01-s001"), "medium")

    def test_odd_answer_types_take_the_malformed_path(self):
        def odd(r):
            if r["kind"] == "diagram":  # the prompt asks for 1 / N labels: a bare number is fine
                return {"nodes": ["학생", "수강", "과목"], "edges": [{"from": "학생", "to": "수강", "label": 1},
                                                                  {"from": "수강", "to": "과목", "label": None}]}
            return {"table": {"rows": [["x"]], "header_rows": "첫 행"}, "code": {"language": ["sql"], "code": SQL},
                    "formula": {"plain": "y = wx + b", "latex": 3}}.get(r["kind"])
        with tempfile.TemporaryDirectory() as t:
            c = Course(t)
            c.advance()
            c.answer(odd)
            c.advance()
            regs = c.state()["regions"]
            for rid in ("db01-s001.code1", "db01-s002.table1", "db01-s004.formula2"):
                self.assertEqual((regs[rid]["malformed"], regs[rid]["state"]), (1, "queued"), rid)
            self.assertEqual(len(regs["db01-s003.diagram1"]["reads"]), 1)
            c.answer(odd)
            c.advance()
            self.assertEqual(c.states()["db01-s002.table1"], "disputed")

    def test_utf16_reads_file_is_taken_in(self):
        with tempfile.TemporaryDirectory() as t:
            c = Course(t)
            c.advance()
            c.answer_pending()
            for p in c.reads("b001").parent.glob("*.json"):
                p.write_text(p.read_text(encoding="utf-8"), encoding="utf-16")
            res = c.advance()
            self.assertEqual((res["unreadable_reads"], set(c.states().values())), ([], {"agreed"}))

    def test_unreadable_reads_are_reported(self):
        with tempfile.TemporaryDirectory() as t:
            c = Course(t)
            c.advance()
            c.answer_pending()
            good = {p: p.read_text(encoding="utf-8") for p in c.reads("b001").parent.glob("*.json")}
            for p, text in good.items():
                p.write_text("{" + text + "}", encoding="utf-8")  # doubled braces, as in a str.format template
            res = c.advance()
            self.assertEqual(sorted(res["unreadable_reads"]), sorted(str(p) for p in good))
            self.assertEqual(res["intake"]["batches"], 0)
            self.assertEqual(len(res["pending_batches"]), len(good))
            for p, text in good.items():
                p.write_text(text, encoding="utf-8")
            res = c.advance()
            self.assertEqual((res["unreadable_reads"], set(c.states().values())), ([], {"agreed"}))

    def test_skip_closes_written_batches_and_stays_in_its_lectures(self):
        spec = {"db01": {"title": "1강", "slides": [("sql", "accepted")]}, "db02": {"title": "2강", "slides": [("formula", "accepted")]}}
        with tempfile.TemporaryDirectory() as t:
            d = Deck(t, spec)
            d.advance()
            res = d.advance(lectures=["db02"], skip=True)
            self.assertEqual(d.states(), {"db01-s001.code1": "queued", "db02-s001.formula2": "skipped"})
            self.assertTrue(res["pending_batches"], "db01's read is still wanted")
            self.assertEqual(d.advance(skip=True)["pending_batches"], [])
            self.assertEqual({b["status"] for b in d.state()["batches"].values()}, {"cancelled"})
            self.assertEqual(list((d.course["work"] / "struct" / "batches").glob("*.md")), [])
            self.assertEqual(d.advance()["pending_batches"], [])

    def test_ocr_line_spanning_a_table_row_is_not_missed_content(self):
        with tempfile.TemporaryDirectory() as t:
            d = Deck(t, one("t_fin"), engine="winocr")
            rp = d.root / "db01" / "slide_analysis" / "ref" / "slide_001.json"
            ref = json.loads(rp.read_text(encoding="utf-8"))
            merged = [ref["lines"][0]]  # the OCR engine reads each table row as one line; '2019' -> '2O19'
            for r in range(3):
                cells = ref["lines"][1 + 3 * r: 4 + 3 * r]
                text = "    ".join(x["text"] for x in cells).replace("2019", "2O19")
                l, tp = min(x["box"][0] for x in cells), min(x["box"][1] for x in cells)
                box = [l, tp, max(x["box"][0] + x["box"][2] for x in cells) - l, max(x["box"][1] + x["box"][3] for x in cells) - tp]
                merged.append({"id": f"L{len(merged) + 1:03d}", "engine": "winocr", "text": text, "conf": None, "box": box,
                               "words": kp_fixtures._words({"text": text, "box": box}, None, "mid")})
            ref["lines"] = merged
            rp.write_text(json.dumps(ref, ensure_ascii=False), encoding="utf-8")
            d.reingest()
            d.advance()
            self.assertEqual(d.region("db01-s001.table1")["mode"], "crop")
            d.answer(lambda r: {"rows": FIN, "header_rows": 1} if r["kind"] == "table" else "2019    120    35")
            d.advance()
            self.assertEqual(d.states(), {"db01-s001.table1": "agreed", "db01-s001.line1": "agreed"})
            self.assertEqual(d.trust("db01-s001"), "high")


if __name__ == "__main__":
    unittest.main()

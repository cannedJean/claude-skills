"""Regressions from the real-deck test (SSAFY Vue, 123 slides): crops cut code blocks short, two readers of the same
cut crop agreed, and the vote 'corrected' exact analyses by deleting their last lines / line ends."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import kp_regions  # noqa: E402
import kp_verify  # noqa: E402
from test_verify import Course  # noqa: E402

CODE = "const evenNumbers = computed(() => {\n  return numbers.value.filter((n) => n % 2 === 0)\n})"
CUT_TAIL = "const evenNumbers = computed(() => {\n  return numbers.value.filter((n) => n % 2 === 0)"
CUT_RIGHT = "const evenNumbers = computed(() => {\n  return numbers.value.filter((n) => n % 2\n})"


def reg(kind="code"):
    return {"rid": "x.code1", "kind": kind, "part": ["code_blocks", 0], "note": None}


class DeletionGuardTests(unittest.TestCase):
    def agy(self):
        return {"language": "javascript", "code": CODE}

    def test_both_reads_missing_the_last_line_do_not_delete_it(self):
        r = {"language": "javascript", "code": CUT_TAIL}
        state, canonical, note = kp_verify.decide_round2(reg(), "code", self.agy(), r, dict(r))
        self.assertEqual((state, canonical), ("agy_upheld", None))
        self.assertIn("deletion", note)

    def test_both_reads_missing_a_line_end_do_not_cut_it(self):
        r = {"language": "javascript", "code": CUT_RIGHT}
        self.assertEqual(kp_verify.decide_round2(reg(), "code", self.agy(), r, dict(r))[0], "agy_upheld")

    def test_a_real_change_is_still_corrected(self):
        r = {"language": "javascript", "code": CODE.replace("=== 0", "=== 1")}
        state, canonical, _ = kp_verify.decide_round2(reg(), "code", self.agy(), r, dict(r))
        self.assertEqual(state, "corrected")
        self.assertIn("=== 1", canonical["code"])

    def test_a_missed_line_is_still_added(self):
        agy = {"language": "javascript", "code": CUT_TAIL}
        r = {"language": "javascript", "code": CODE}
        state, canonical, _ = kp_verify.decide_round2(reg(), "code", agy, r, dict(r))
        self.assertEqual((state, canonical["code"]), ("corrected", CODE))

    def test_change_plus_deletion_is_disputed(self):
        r = {"language": "javascript", "code": CUT_TAIL.replace("=== 0", "=== 1")}
        self.assertEqual(kp_verify.decide_round2(reg(), "code", self.agy(), r, dict(r))[0], "disputed")

    def test_line_read_that_is_part_of_the_item_is_upheld(self):
        agy = {"text": "Find apps... Find components... <Root> Filter state..."}
        r = {"text": "Find apps... <Root>"}
        self.assertEqual(kp_verify.decide_round2(reg("line"), "line", agy, r, dict(r))[0], "agy_upheld")

    def test_table_with_fewer_rows_is_not_a_correction(self):
        agy = {"rows": [["a", "b"], ["1", "2"], ["3", "4"]], "header_rows": 1}
        r = {"rows": [["a", "b"], ["1", "2"]], "header_rows": 1}
        self.assertEqual(kp_verify.decide_round2(reg("table"), "table", agy, r, dict(r))[0], "agy_upheld")


class LineDecisionTests(unittest.TestCase):
    """Line regions on the Vue deck: reads matched to the wrong unit (code skipped, URL fragments) were disputed, and
    DevTools labels both readers saw were disputed instead of attached as missed_content."""
    AN = {"title": "Built-in Directives",
          "verbatim_text": [{"region": "body", "text": "• select에서 v-model 표현식의 초기 값이 어떤 option과도 일치하지 않는 경우"},
                            {"region": "body", "text": "https://vuejs.org/api/built-in-directives.html"},
                            {"region": "body", "text": "릴레이션 R은 3NF를 만족한다."}],
          "code_blocks": [{"language": "html", "code": "<select v-model=\"selected\">\n  <option>Bella</option>\n</select>"},
                          {"language": "js", "code": "const myArr = ref([\n  { id: id++, name: 'Alice' },\n])"}]}

    def extra(self, strong=False):
        return {"joined": kp_verify._joined(self.AN), "strong": strong, "target": "x"}

    def r1(self, text, agy="", strong=False):
        return kp_verify.decide_round1(reg("line"), "line", {"text": agy}, {"text": text}, self.extra(strong))[0]

    def r2(self, t1, t2, agy="", strong=False):
        r = reg("line")
        out = kp_verify.decide_round2(r, "line", {"text": agy}, {"text": t1}, {"text": t2}, self.extra(strong))
        return out[0], r.get("extra_read"), out[1]

    def test_code_line_read_is_in_the_analysis(self):
        self.assertEqual(self.r1("<option>Bella</option>", agy=self.AN["verbatim_text"][0]["text"]), "agreed")

    def test_curly_quotes_do_not_hide_a_code_line(self):
        self.assertEqual(self.r1("‘Alice’ },"), "agreed")

    def test_fragment_and_full_line_are_upheld_when_the_full_line_is_there(self):
        self.assertEqual(self.r2("uilt-in-c", "https://vuejs.org/api/built-in-directives.html")[0], "agy_upheld")

    def test_agreed_label_missing_from_the_analysis_is_missed_content(self):
        self.assertEqual(self.r1("Find apps..."), "needs_second_read")
        state, extra, _ = self.r2("Find apps...", "Find apps...", agy=self.AN["verbatim_text"][0]["text"])
        self.assertEqual((state, extra), ("missed_content", ["Find apps..."]))

    def test_fragment_and_full_label_attach_the_longer_read(self):
        self.assertEqual(self.r2("Find c", "Find components...")[:2], ("missed_content", ["Find components..."]))

    def test_different_reads_stay_disputed(self):
        self.assertEqual(self.r2("Find apps...", "Filter state...")[0], "disputed")

    def test_strong_target_is_still_corrected(self):
        agy = self.AN["verbatim_text"][2]["text"]
        state, _, canonical = self.r2("릴레이션 R은 2NF를 만족한다.", "릴레이션 R은 2NF를 만족한다.", agy=agy, strong=True)
        self.assertEqual((state, canonical["text"]), ("corrected", "릴레이션 R은 2NF를 만족한다."))

    def test_only_ui_marks_are_minor(self):
        self.assertEqual(self.r2("▼ <option>Bella</option> ▶", "▼ <option>Bella</option> ▶")[0], "agy_upheld")
        self.assertEqual(self.r2("▼ ▶ ⋯", "▼ ▶ ⋯")[0], "minor")

    def test_ocr_noise_line_is_not_a_problem(self):
        self.assertEqual(self.r1(""), "minor")
        r = dict(reg("line"), mode="view", slide="x", not_found=0,
                 reads=[{"answer": ""}, {"answer": {"not_found": True}}])
        kp_verify._after_read(r, {"x": {"analysis": self.AN}}, {"x": None})
        self.assertEqual(r["state"], "minor")

    def test_missed_content_is_medium_trust(self):
        import kp_ingest
        rec = {"text_check": "ocr_verified", "provenance": "ok", "digits_checked": True, "uncertain_count": 0,
               "analysis": {"diagrams": [{}]}, "triggers": []}
        self.assertEqual(kp_ingest.slide_trust(rec, [{"kind": "line", "state": "missed_content"}]), "medium")
        self.assertEqual(kp_ingest.slide_trust(rec, [{"kind": "line", "state": "disputed"}]), "low")


class DetectorTests(unittest.TestCase):
    def test_vue_code_filed_as_verbatim_is_code_not_a_formula(self):
        for t in ["const isShow = ref(false)", "<!-- EventHandling.vue -->\n\nconst count = ref(0)",
                  "const styleObj2 = ref({\n  color: 'blue',\n  border: '1px solid black'\n})",
                  "const warning = function (message, event) {\n  console.log(message)\n}"]:
            self.assertTrue(kp_regions.is_code_like(t), t)
            self.assertFalse(kp_regions.is_formula(t), t)
        for t in ['<div :style="styleObj">Text</div>', "() => count.value++"]:
            self.assertFalse(kp_regions.is_formula(t), t)
        for t in ["y = wx + b", "σ² = E[(X-μ)²]"]:
            self.assertTrue(kp_regions.is_formula(t), t)
            self.assertFalse(kp_regions.is_code_like(t), t)


class BlockBoxTests(unittest.TestCase):
    def test_dark_code_box_is_cropped_whole(self):
        from PIL import Image, ImageDraw
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "s.png"
            im = Image.new("RGB", (1000, 600), "white")
            d = ImageDraw.Draw(im)
            d.rectangle((100, 150, 900, 450), fill=(0, 0, 0))
            for i, s in enumerate(["const a = ref([", "  1, 2, 3", "])"]):
                d.text((120, 170 + i * 60), s, fill=(200, 200, 255))
            im.save(p)
            # the OCR saw only the first two lines, and not the full width
            box = kp_regions.block_box(p, [118, 168, 400, 250], 30)
            self.assertLessEqual(box[1], 150)
            self.assertGreaterEqual(box[3], 450)
            self.assertLessEqual(box[0], 100)
            self.assertGreaterEqual(box[2], 900)

    def test_white_block_extends_over_the_rest_of_the_line(self):
        from PIL import Image, ImageDraw
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "s.png"
            im = Image.new("RGB", (1000, 400), "white")
            d = ImageDraw.Draw(im)
            d.rectangle((50, 100, 950, 160), outline="black", width=2)
            s = '<div style="color: blue; font-size: 50px; border: 1px solid black;">'
            d.text((60, 120), s, fill="black")
            end = d.textbbox((60, 120), s)[2]
            im.save(p)
            box = kp_regions.block_box(p, [58, 118, 60 + (end - 60) // 2, 135], 20)  # the OCR saw half the line
            self.assertGreaterEqual(box[2], end)  # the clipped end of the line is inside
            self.assertLessEqual(box[1], 100)     # up to the border line
            self.assertGreaterEqual(box[3], 160)


class StatusTests(unittest.TestCase):
    def test_settled_line_regions_are_not_reported_open(self):
        """status hashed line regions without their OCR reference and kept saying 'struct' on single-engine decks."""
        import copy
        import kp_common as kc
        import kp_fixtures
        import kp_ingest
        import knowledge_pack
        with tempfile.TemporaryDirectory() as t:
            c = Course(t, engine="winocr")
            an = copy.deepcopy(c.truth["db01-s001"])
            an["verbatim_text"] = [dict(v, text=v["text"].replace("30", "3")) for v in an["verbatim_text"]]
            an["code_blocks"] = [dict(b, code=b["code"].replace("30", "3")) for b in an["code_blocks"]]
            kp_fixtures.set_analysis(t, "db01", "slide_001", an)
            lec = c.course["lectures"][0]
            kc.write_json(c.course["work"] / "ingest" / "db01.json", kp_ingest.ingest_lecture(lec))
            for _ in range(4):
                if not c.advance()["pending_batches"]:
                    break
                c.answer_pending()
            st = c.state()
            self.assertTrue(any(r["kind"] == "line" for r in st["regions"].values()))
            ing = [kc.read_json(c.course["work"] / "ingest" / "db01.json")]
            self.assertEqual(knowledge_pack.struct_open(ing, st), [])


class SecondReadOnViewTests(unittest.TestCase):
    def test_round_two_items_are_whole_slide_views(self):
        with tempfile.TemporaryDirectory() as t:
            c = Course(t, {"db01-s001": "operator"})
            c.advance()
            c.answer_pending()
            c.advance()
            st = c.state()
            r2 = [b for b in st["batches"].values() if b["round"] == 2]
            self.assertTrue(r2)
            self.assertEqual({i["type"] for b in r2 for i in b["items"]}, {"view"})
            reg = st["regions"]["db01-s001.code1"]
            self.assertEqual((reg["mode"], reg.get("mode_r1")), ("view", "crop"))
            self.assertTrue(reg["crop"])  # the round-1 crop stays as evidence
            c.answer_pending()
            c.advance()
            self.assertEqual(c.states()["db01-s001.code1"], "corrected")


if __name__ == "__main__":
    unittest.main()

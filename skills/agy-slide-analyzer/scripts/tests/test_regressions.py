"""Regression tests for the second review of the scorer / reference / resolve changes (stdlib unittest).

    python -m unittest discover -s scripts/tests -v        (from the skill folder)

One class per area; each test names the false accept or the stale-answer path it closes:
  * scorer: deck-template lines only in the header/footer bands (a progressive code walkthrough is content),
    header/footer-tagged text still checked, but the deck's own header/footer boilerplate (a logo the OCR never
    reads) is not, number+unit digits, thin references keep the digit / identifier checks
    and question an invented diagram, one-to-one line anchoring, order checks on code copied into verbatim_text,
    single-number lines, text-layer references retry instead of escalating, v2 table keys on unmasked layouts
  * layout: a dark diagram is not a code panel, right-aligned / centred table columns
  * judge: full-slide reads check operators on every copy, line crops are held against the disputed twin, the
    crops of a line-order dispute are judged together
  * pipeline: `run` settles on the records `rescore` makes (without agy; the whole deck, also after a quota stop or
    a partial --force run, re-queued when only the reasons changed; results of stems outside the deck never feed
    the boilerplate), answers written for a replaced
    record are stale (run and rescore, also when interrupted), a stale
    answer neither blocks resolve nor wins a conflict, the explicit {answer, fp} form clears it, `reference` keeps
    a ref whose slide has queued line crops and reports built / kept / unchanged
No agy and no OCR engine needed: references are built by hand (test_scorer_v2.ref_of) or by a fake engine.
"""
from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import crops as cropmod  # noqa: E402
import ocr_ref  # noqa: E402
import ocr_score  # noqa: E402
import slide_analyze as sa  # noqa: E402
from ocr_score import score, template_keys  # noqa: E402
from test_ocr_ref import NEEDS_FONT, NEEDS_NP, FakeEngine, panel_slide, put  # noqa: E402
from fake_agy_task import TITLE_ONLY  # noqa: E402
from test_pipeline import TmpCase, ns, quiet, read_rec, struct_entry, write_rec  # noqa: E402
from test_scorer_v2 import ANALYSIS, CODE_LINES, FOOTER, REF, ref_of, th, with_code  # noqa: E402
from textnorm import skel, units  # noqa: E402

try:
    from PIL import Image, ImageDraw
    import synth
except ImportError:  # pragma: no cover
    Image = None

WALK = ["const scores = [72, 85, 90, 64];", "let passed = 0;", "for (let i = 0; i < scores.length; i++) {",
        "if (scores[i] >= 70) passed += 1;", "}", "console.log(passed === 3);"]


def walkthrough(k: int, weight="variant") -> dict:
    """Step k of a progressive build: its own title and note, the same code listing at the same place."""
    lines = [(f"{k}단계 반복문 따라가기", [60, 40, 500, 44], weight)]
    lines += [(t, [90, 165 + i * 52, 22 * len(t), 30], weight) for i, t in enumerate(WALK)]
    lines += [(f"설명 {k}번째 줄을 따라간다", [60, 560, 600, 32], weight), FOOTER]
    return ref_of(lines, engines=("winocr", "winocr_x2"), agreement="variants")


LOGO = "모두 함께 성장 Growing Together"  # a fictional course logo


def logo_slide(k: int, logo=LOGO) -> tuple:
    """(analysis, reference) of slide k of a deck whose footer logo is an image: the analyst transcribes it as a
    header/footer item, the OCR never reads it."""
    title, body = f"{k}장 컴포넌트 기초", f"컴포넌트는 화면을 {k}개의 조각으로 나눈다"
    an = dict(copy.deepcopy(TITLE_ONLY), slide_type="concept", title=title,
              verbatim_text=[{"region": "title", "text": title}, {"region": "body", "text": body}]
              + ([{"region": "header/footer", "text": logo}] if logo else []),
              key_concepts=[{"term": "컴포넌트", "explanation": "화면 조각"}],
              summary="컴포넌트가 화면을 조각으로 나눈다는 것을 설명한다.", lecture_role="컴포넌트 도입")
    return an, ref_of([(title, [60, 40, 500, 44]), (body, [60, 140, 700, 30])])


def walk_analysis(k: int, code="\n".join(WALK)) -> dict:
    return {"title": f"{k}단계 반복문 따라가기", "slide_type": "example",
            "verbatim_text": [{"region": "title", "text": f"{k}단계 반복문 따라가기"},
                              {"region": "body", "text": f"설명 {k}번째 줄을 따라간다"},
                              {"region": "header/footer", "text": FOOTER[0]}],
            "code_blocks": [{"language": "javascript", "code": code}] if code else []}


# --------------------------------------------------------------------------- scorer
class TemplateBandTests(unittest.TestCase):
    """A line repeated at the same place on 5+ slides is template only in the header/footer bands."""

    def test_progressive_code_walkthrough_is_content(self):
        refs = {f"step{k}": walkthrough(k) for k in range(1, 7)}
        tk = template_keys(refs)
        self.assertEqual(len(tk), 1)  # the footer only, none of the six code lines
        r6 = copy.deepcopy(refs["step6"])
        self.assertEqual(score(walk_analysis(6), copy.deepcopy(r6), tkeys=tk)["decision"], "accept")
        dropped = score(walk_analysis(6, code=""), copy.deepcopy(r6), tkeys=tk)
        self.assertNotEqual(dropped["decision"], "accept")
        self.assertLess(dropped["metrics"]["recall"], 0.6)
        wrong = "\n".join(WALK).replace(">= 70", "< 70").replace("passed === 3", "passed !== 4")
        sc = score(walk_analysis(6, code=wrong), copy.deepcopy(r6), tkeys=tk)
        self.assertEqual(sc["decision"], "escalate")
        self.assertIn("operator_or_digit_mismatch", sc["reasons"])

    def test_key_keeps_operators_and_skips_code_panels(self):
        def band(text, panels=()):
            ref = ref_of([(text, [60, 20, 400, 30])])
            ref["layout"]["code_boxes"] = [list(p) for p in panels]
            return ref
        mixed = [band("if (x > 1) return") for _ in range(3)] + [band("if (x < l) return") for _ in range(3)]
        self.assertEqual(skel("if (x > 1) return"), skel("if (x < l) return"))  # why the key is not skel
        self.assertEqual(template_keys(mixed), set())
        self.assertEqual(len(template_keys([band("EXAMPLE CODE ACADEMY") for _ in range(5)])), 1)
        self.assertEqual(template_keys([band("EXAMPLE CODE ACADEMY", [(40, 10, 600, 60)]) for _ in range(5)]), set())


class HeaderFooterTests(unittest.TestCase):
    """The analyst's region tag keeps an item out of content_chars, not out of the hallucination check."""

    def test_invented_text_tagged_header_footer_escalates(self):
        self.assertEqual(score(ANALYSIS, copy.deepcopy(REF))["decision"], "accept")
        for region in ("header/footer", "page_number", "body"):
            with self.subTest(region=region):
                a = copy.deepcopy(ANALYSIS)
                a["verbatim_text"].append({"region": region, "text": "v-model.lazy 수식어는 change 이벤트 후에 동기화한다"})
                sc = score(a, copy.deepcopy(REF))
                self.assertEqual(sc["decision"], "escalate")
                self.assertIn("unsupported_field: verbatim_text[4]", sc["reasons"])


class DeckBoilerplateTests(unittest.TestCase):
    """Units the deck's header/footer items repeat on 3+ slides (a logo the OCR never reads) leave the unsupported
    ratio; every other unit of a header/footer item is still checked, so the invented-footer path stays closed."""

    def analyses(self, n=4):
        return [logo_slide(k)[0] for k in range(1, n + 1)]

    def test_boilerplate_comes_from_header_footer_items_only(self):
        ans = self.analyses()
        bp = ocr_score.hf_boilerplate(ans)
        self.assertEqual(bp, set(units(LOGO)))  # '컴포넌트' is in every slide's body: never boilerplate
        self.assertEqual(ocr_score.hf_boilerplate({f"s{i}": a for i, a in enumerate(ans)}), bp)
        self.assertEqual(ocr_score.hf_boilerplate(ans[:2]), set())
        moved = copy.deepcopy(ans)
        for a in moved[2:]:  # the logo words in a body item of two slides: only two header/footer items remain
            a["verbatim_text"][2]["region"] = "body"
        self.assertEqual(ocr_score.hf_boilerplate(moved), set())
        pn = copy.deepcopy(ans[:2]) + [dict(ans[2], verbatim_text=[{"region": "page_number", "text": LOGO}])]
        self.assertEqual(ocr_score.hf_boilerplate(pn), bp)  # page_number items count like header/footer

    def test_logo_the_ocr_never_reads(self):
        bp = ocr_score.hf_boilerplate(self.analyses())
        for k in range(1, 5):
            an, ref = logo_slide(k)
            on = score(an, copy.deepcopy(ref), boilerplate=bp)
            self.assertEqual(on["decision"], "accept", on["reasons"])
            self.assertEqual(on["metrics"]["boilerplate_chars"], sum(map(len, units(LOGO))))
            self.assertFalse(on["unsupported_fields"] or on["unsupported_units"])
            off = score(an, copy.deepcopy(ref), th(hf_boilerplate=False), boilerplate=bp)
            self.assertEqual(off["decision"], "escalate")
            self.assertIn("unsupported_field: verbatim_text[2]", off["reasons"])
            self.assertEqual(off["metrics"]["boilerplate_chars"], 0)
            self.assertEqual(score(an, copy.deepcopy(ref))["decision"], "escalate")  # no deck boilerplate given
        body, ref = logo_slide(1)
        body["verbatim_text"][2]["region"] = "body"  # the same words outside a header/footer item stay checked
        self.assertEqual(score(body, copy.deepcopy(ref), boilerplate=bp)["decision"], "escalate")

    def test_invented_footer_text_still_escalates(self):
        bp = ocr_score.hf_boilerplate(self.analyses())
        an, ref = logo_slide(1)
        an["verbatim_text"].append({"region": "header/footer", "text": LOGO + " v-model.lazy 수식어는 change 이벤트 후에 동기화한다"})
        sc = score(an, copy.deepcopy(ref), boilerplate=bp)
        self.assertEqual(sc["decision"], "escalate")
        self.assertIn("unsupported_field: verbatim_text[3]", sc["reasons"])
        self.assertIn("change", {u["text"] for u in sc["unsupported_units"]})
        self.assertNotIn("growing", {u["text"] for u in sc["unsupported_units"]})

    def test_a_repeated_invented_footer_is_boilerplate_by_design(self):
        """Known limit of the rule (review 3): to a text-only scorer the same sentence tagged header/footer on 3+
        slides and never read by the OCR is a logo. Pinned so a change is deliberate; on two slides, or in any item
        that is not tagged header/footer / page_number, it is still checked."""
        inv = "시험 범위 컴포넌트 props는 자식에서 부모로 전달된다"
        pairs = [logo_slide(k, logo=inv) for k in range(1, 4)]
        bp = ocr_score.hf_boilerplate([a for a, _ in pairs])
        self.assertEqual(bp, set(units(inv)))
        for an, ref in pairs:
            self.assertEqual(score(an, copy.deepcopy(ref), boilerplate=bp)["decision"], "accept")
            lg = score(an, copy.deepcopy(ref), ocr_score.legacy_thresholds(), boilerplate=bp)
            self.assertEqual(lg["decision"], "escalate")  # so shadow mode counts it as a new-only accept
        two = ocr_score.hf_boilerplate([a for a, _ in pairs[:2]])
        self.assertEqual(score(pairs[0][0], copy.deepcopy(pairs[0][1]), boilerplate=two)["decision"], "escalate")
        an, ref = logo_slide(4, logo=None)
        an["verbatim_text"].append({"region": "body", "text": inv})
        self.assertEqual(score(an, copy.deepcopy(ref), boilerplate=bp)["decision"], "escalate")

    def test_boilerplate_still_counts_for_recall(self):
        an, ref = logo_slide(1)
        ref = ref_of([(an["title"], [60, 40, 500, 44]), (an["verbatim_text"][1]["text"], [60, 140, 700, 30]),
                      (LOGO, [40, 680, 400, 20])])  # this slide's logo was read by the OCR after all
        sc = score(an, copy.deepcopy(ref), boilerplate=ocr_score.hf_boilerplate(self.analyses()))
        self.assertEqual((sc["metrics"]["recall"], sc["missing_lines"]), (1.0, []))
        bare = copy.deepcopy(an)
        del bare["verbatim_text"][2]
        self.assertLess(score(bare, copy.deepcopy(ref))["metrics"]["recall"], 1.0)

    def test_legacy_view_switches_every_check_off(self):
        lg = ocr_score.legacy_thresholds(th(recall_accept=0.8))
        self.assertEqual(lg["recall_accept"], 0.8)
        for k, v in ocr_score.SWITCHES_OFF.items():
            self.assertIn(k, ocr_score.DEFAULT_THRESHOLDS)
            self.assertEqual(lg[k], v)
        switches = {k for k, v in ocr_score.DEFAULT_THRESHOLDS.items() if isinstance(v, bool)}
        self.assertEqual(switches | {"graphics_trigger_nocode"}, set(ocr_score.SWITCHES_OFF))  # a new check needs a legacy value
        an, ref = logo_slide(1)
        bp = ocr_score.hf_boilerplate(self.analyses())
        self.assertEqual(score(an, copy.deepcopy(ref), lg, boilerplate=bp)["decision"], "escalate")


class DigitTests(unittest.TestCase):
    LINES = [("트랜지션 설정", [60, 40, 300, 44]), ("사이드바 너비는 1200px로 고정한다", [60, 140, 600, 30]),
             ("열고 닫는 애니메이션은 500ms 동안 진행된다", [60, 200, 700, 30]),
             ("모바일에서는 너비를 줄여서 보여준다", [60, 260, 600, 30])]

    def analysis(self, *texts):
        return {"title": "트랜지션 설정", "verbatim_text": [{"region": "title", "text": "트랜지션 설정"}]
                + [{"region": "body", "text": t} for t in texts]}

    def test_number_with_unit_is_checked(self):
        ref = ref_of(self.LINES, weight="variant", engines=("winocr", "winocr_x2"), agreement="variants")
        ok = self.analysis(*[t for t, _ in self.LINES[1:]])
        self.assertEqual(score(ok, copy.deepcopy(ref))["decision"], "accept")
        bad = self.analysis("사이드바 너비는 120px로 고정한다", "열고 닫는 애니메이션은 50ms 동안 진행된다", self.LINES[3][0])
        sc = score(bad, copy.deepcopy(ref))
        self.assertEqual(sc["decision"], "escalate")
        self.assertTrue(any(r.startswith("digit_recall") for r in sc["reasons"]), sc["reasons"])
        self.assertEqual(ocr_score._digit_runs("He110 1200px v12 500ms", True), ["1200", "500"])  # after a letter: glyph

    def test_line_whose_only_token_is_the_number(self):
        lines = [("제한 사항", [60, 40, 300, 44]), ("업로드 파일은 최대 1000개", [60, 140, 600, 30]),
                 ("한 번에 여러 개를 고를 수 있다", [60, 200, 600, 30]), ("목록은 이름 순으로 정렬된다", [60, 260, 600, 30]),
                 ("삭제한 파일은 복구할 수 없다", [60, 320, 600, 30])]
        ref = ref_of(lines, weight="mid", engines=("winocr",))
        a = {"title": "제한 사항", "verbatim_text": [{"region": "body", "text": t} for t, _ in lines[1:]]}
        self.assertEqual(score(a, copy.deepcopy(ref))["decision"], "accept")
        a["verbatim_text"][0]["text"] = "업로드 파일은 최대 10000개"
        sc = score(a, copy.deepcopy(ref))
        self.assertIn("operator_or_digit_mismatch", sc["reasons"])
        self.assertEqual(sc["line_issues"][0]["issues"], [{"kind": "digit", "read": ["1000"], "analysis": ["10000"]}])
        # a lone OCR fragment is not a substitution of the whole literal it stands next to
        self.assertEqual(ocr_score._token_diff("const 5", "const scores = [72, 85, 90, 64];"), [])


class ThinTests(unittest.TestCase):
    def test_invented_diagram_on_a_thin_reference_escalates(self):
        ref = ref_of([("ERD 예시", [60, 40, 300, 44])], weight="variant", engines=("winocr", "winocr_x2"))
        invented = {"title": "ERD 예시", "verbatim_text": [{"region": "title", "text": "ERD 예시"}],
                    "diagrams": [{"kind": "erd", "elements": ["학생(학번 PK, 이름, 학과, 학년)", "과목(과목코드 PK, 과목명, 학점 3)",
                                                             "수강(학번 FK, 과목코드 FK, 성적)"], "relations": []}]}
        sc = score(invented, copy.deepcopy(ref))
        self.assertIn("agy_rich_ref_empty", sc["triggers"])
        self.assertEqual(sc["decision"], "escalate")
        self.assertIn("reference too thin to confirm a text-rich analysis", sc["reasons"])
        self.assertIn("thumbnail", sc["needs"])
        labelled = ref_of([("ERD 예시", [60, 40, 300, 44]), ("학생", [200, 300, 80, 30], "mid"), ("수강", [600, 300, 80, 30], "mid"),
                           ("과목", [1000, 300, 80, 30], "mid")], weight="variant", engines=("winocr", "winocr_x2"))
        seen = dict(invented, diagrams=[{"kind": "erd", "elements": ["학생", "수강", "과목"], "relations": ["학생 -> 수강"]}])
        self.assertEqual(score(seen, labelled)["decision"], "accept")

    def test_thin_reference_keeps_the_digit_check(self):
        deck = {f"s{i}": ref_of([FOOTER]) for i in range(5)}
        ref = ref_of([("6장 제한", [60, 40, 300, 44], "variant"), ("최대 1000개", [60, 140, 300, 30], "variant"), FOOTER],
                     engines=("winocr", "winocr_x2"), agreement="variants")
        tk = template_keys(dict(deck, this=ref))
        a = {"title": "6장 제한", "verbatim_text": [{"region": "title", "text": "6장 제한"},
                                                 {"region": "body", "text": "최대 10000개"},
                                                 {"region": "header/footer", "text": FOOTER[0]}]}
        sc = score(a, copy.deepcopy(ref), tkeys=tk)
        self.assertIn("ref_thin", sc["triggers"])
        self.assertEqual(sc["decision"], "escalate")
        self.assertIn("digit_recall 0.00 < 0.9", sc["reasons"])
        self.assertEqual(sc["needs"], ["crops", "thumbnail"])
        a["verbatim_text"][1]["text"] = "최대 1000개"
        self.assertEqual(score(a, copy.deepcopy(ref), tkeys=tk)["reasons"],
                         ["little text on both sides", "thin reference, content supported"])


class AnchorTests(unittest.TestCase):
    def test_order_check_when_the_code_is_also_in_verbatim_text(self):
        swapped = "const even = i % 2 === 0\nconst isAdult = age > 30\nreturn isAdult && even"
        for both in (False, True):
            with self.subTest(code_also_in_verbatim=both):
                a = with_code(swapped)
                if both:  # agy copies code into both fields; verbatim_text comes first
                    a["verbatim_text"].insert(2, {"region": "body", "text": swapped})
                ref = copy.deepcopy(REF)
                ref["layout"]["code_boxes"] = [[60, 190, 700, 130]]
                sc = score(a, ref)
                self.assertEqual(sc["decision"], "escalate")
                self.assertIn("line_order_mismatch", sc["reasons"])
                self.assertEqual({m["order_field"] for m in sc["missing_lines"] if m.get("reason") == "line_order"},
                                 {"code_blocks[0].code"})

    def test_a_line_shown_twice_is_compared_with_both_copies(self):
        twice = "if (value > 100) return 100;"
        ref = ref_of([("범위 제한 함수", [60, 40, 400, 44]), ("function clamp(value) {", [80, 140, 500, 28]),
                      (twice, [100, 180, 600, 28]), ("function percent(value) {", [80, 260, 500, 28]),
                      (twice, [100, 300, 600, 28])], weight="variant", engines=("winocr", "winocr_x2"))
        code = ["function clamp(value) {", twice, "}", "function percent(value) {", twice, "}"]
        a = {"title": "범위 제한 함수", "code_blocks": [{"code": "\n".join(code)}]}
        self.assertEqual(score(a, copy.deepcopy(ref))["decision"], "accept")
        for k in (1, 4):
            with self.subTest(changed_copy=k):
                c = list(code)
                c[k] = "if (value >= 1000) return 1000;"
                sc = score({"title": "범위 제한 함수", "code_blocks": [{"code": "\n".join(c)}]}, copy.deepcopy(ref))
                self.assertEqual(sc["decision"], "escalate")
                self.assertIn("operator_or_digit_mismatch", sc["reasons"])
                disputed = [m for m in sc["missing_lines"] if m.get("reason") == "operator_or_digit"]
                self.assertEqual([m.get("analysis_line") for m in disputed], ["if (value >= 1000) return 1000;"])


class ExactAndUnmaskedTests(unittest.TestCase):
    def test_text_layer_mismatch_retries(self):
        text = "\n".join(["비교 연산자와 조건식", "나이가 30보다 크면 성인으로 판단한다", *(t for t, _ in CODE_LINES), FOOTER[0], "7"])
        ref = ocr_ref.text_reference(text, "x.pdf")
        sc = score(with_code("const isAdult = age < 30\nconst even = i % 2 === 0\nreturn isAdult && even"), ref)
        self.assertEqual((sc["decision"], sc["reasons"]), ("retry", ["operator_or_digit_mismatch"]))
        self.assertIn("연산자/숫자", ocr_score.feedback_text(sc))

    def test_unmasked_layout_falls_back_to_the_v2_table_keys(self):
        ref = copy.deepcopy(REF)
        ref["layout"].update(ruled_table=True, ruled_table2=False, table_grid2=None, text_masked=False)
        prose = dict(ANALYSIS, code_blocks=[])
        self.assertIn("table_candidate_undeclared", score(prose, ref)["triggers"])
        ref["layout"]["text_masked"] = True  # with word boxes the v3 key is the better one
        self.assertNotIn("table_candidate_undeclared", score(prose, ref)["triggers"])


# --------------------------------------------------------------------------- layout
def dark_figure():
    """A dark-theme architecture figure: outlined boxes with labels, arrows and a dashed lane, no code. Its words:
    the labels and the huge 'word' Windows OCR reads off the row of boxes."""
    img = Image.new("RGB", (1280, 720), "white")
    d = ImageDraw.Draw(img)
    words = put(d, 60, 40, "서비스 구성도", synth.font(synth.BOLD_CANDIDATES, 44), [])
    d.rectangle((60, 200, 1220, 560), fill=(28, 28, 32))
    lab = synth.font(synth.FONT_CANDIDATES, 28)
    for x, name, col in ((120, "Client", (90, 160, 255)), (520, "Server", (80, 200, 120)), (920, "DB", (240, 160, 60))):
        d.rounded_rectangle((x, 290, x + 240, 430), radius=18, outline=col, width=6)
        put(d, x + 70, 342, name, lab, words, fill=(235, 235, 235))
    for x0, x1 in ((360, 520), (760, 920)):
        d.line((x0 + 5, 360, x1 - 25, 360), fill=(235, 235, 235), width=6)
        d.polygon([(x1 - 5, 360), (x1 - 30, 345), (x1 - 30, 375)], fill=(235, 235, 235))
    for x in range(150, 1150, 60):
        d.line((x, 520, x + 30, 520), fill=(150, 150, 150), width=4)
    words.append({"text": "口-口-口", "conf": None, "box": [120, 288, 1044, 144], "weight": "mid"})
    return img, words


@NEEDS_FONT
class DarkPanelTests(unittest.TestCase):
    def test_dark_figure_is_not_a_code_panel(self):
        img, words = dark_figure()
        self.assertTrue(any(w >= 1000 for _, _, w, _ in ocr_ref.detect_code_panels(img)))  # dark and uniform enough
        lay = ocr_ref.layout_stats(img, words, [words])
        self.assertFalse(any(w >= 1000 for _, _, w, _ in lay["code_boxes"]), lay["code_boxes"])
        self.assertGreaterEqual(lay["graphics_ratio_nocode"], 0.015)
        sc = score({"title": "서비스 구성도", "verbatim_text": [{"region": "title", "text": "서비스 구성도"}]},
                   ref_of([("서비스 구성도", [60, 40, 300, 44])], layout=lay))
        self.assertIn("graphics_undeclared", sc["triggers"])

    def test_code_panel_with_words_and_marks_on_it(self):
        img, words = panel_slide()
        lay = ocr_ref.layout_stats(img, words, [words])
        self.assertEqual(len(lay["code_boxes"]), 1)  # OCR text covers its light ink
        self.assertLess(lay["graphics_ratio_nocode"], 0.015)
        d = ImageDraw.Draw(img)
        d.rectangle((80, 260, 420, 330), outline=(255, 40, 40), width=6)  # highlight box, arrow and ring on the panel
        d.line((700, 480, 440, 300), fill=(255, 40, 40), width=8)
        d.ellipse((540, 400, 740, 500), outline=(255, 200, 0), width=6)
        marked = ocr_ref.layout_stats(img, words, [words])
        self.assertGreaterEqual(marked["graphics_ratio_nocode"], 0.015)
        self.assertIn("graphics_undeclared", score(ANALYSIS, ref_of([("x y", [60, 40, 300, 44])] * 6, layout=marked))["triggers"])

    def test_no_words_keeps_the_unvalidated_detector(self):
        img, words = dark_figure()
        self.assertEqual(ocr_ref.detect_code_panels(img), ocr_ref.detect_code_panels(img, words=None))
        self.assertEqual(ocr_ref.detect_code_panels(img, words=[]), [])  # OCR ran and read nothing there


class TableAlignmentTests(unittest.TestCase):
    @staticmethod
    def table(align, widths=(260, 240, 240), x0=160, y0=200, rh=70, h=28):
        rows = [["항목", "수량", "단가"], ["사과", "1,250", "900"], ["바나나", "30", "4,500"], ["포도", "4,800", "12"],
                ["딸기", "75", "15,000"]]
        xs = [x0]
        for w in widths:
            xs.append(xs[-1] + w)
        words = []
        for r, row in enumerate(rows):
            for c, cell in enumerate(row):
                tw = 15 * len(cell)
                a = align[c]
                x = xs[c] + 14 if a == "left" else (xs[c] + (widths[c] - tw) / 2 if a == "center" else xs[c + 1] - 14 - tw)
                words.append({"text": cell, "box": [int(x), y0 + r * rh + 14, tw, h], "col": c})
        return words

    def test_right_aligned_and_centred_numbers_form_columns(self):
        for align in (("left", "right", "right"), ("center", "center", "center"), ("left", "center", "right")):
            with self.subTest(align=align):
                g = ocr_ref.table_grid2(self.table(align), 720)
                self.assertEqual(g, {"rows": 5, "cols": 3})

    @NEEDS_FONT
    def test_drawn_tables_with_rules_or_none(self):
        for style in ("booktabs", "none"):
            for align in (("left", "right", "right"), ("center", "center", "center")):
                with self.subTest(style=style, align=align):
                    img = Image.new("RGB", (1280, 720), "white")
                    d = ImageDraw.Draw(img)
                    words = put(d, 60, 40, "성적표 예시", synth.font(synth.BOLD_CANDIDATES, 44), [])
                    f = synth.font(synth.FONT_CANDIDATES, 28)
                    for w in self.table(align):
                        put(d, w["box"][0], w["box"][1], w["text"], f, words)
                    if style == "booktabs":
                        for y in (200, 270, 550):
                            d.line([(160, y), (900, y)], fill="black", width=2)
                    lay = ocr_ref.layout_stats(img, words, [words])
                    self.assertTrue(lay["table_grid2"], lay["table_grid2"])

    @NEEDS_FONT
    def test_numeric_tables_through_windows_ocr(self):
        eng = ocr_ref.WinOcrEngine()
        if not eng.available():
            self.skipTest("needs Windows OCR")
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        for style, align in (("booktabs", ("left", "right", "right")), ("none", ("center", "center", "center"))):
            with self.subTest(style=style, align=align):
                img = Image.new("RGB", (1280, 720), "white")
                d = ImageDraw.Draw(img)
                d.text((60, 40), "성적표 예시", font=synth.font(synth.BOLD_CANDIDATES, 44), fill="black")
                f = synth.font(synth.FONT_CANDIDATES, 28)
                for w in self.table(align):  # cell positions from the box model, text drawn at its real width
                    slack = 15 * len(w["text"]) - d.textlength(w["text"], font=f)
                    x = w["box"][0] + slack * {"left": 0, "center": 0.5, "right": 1}[align[w["col"]]]
                    d.text((x, w["box"][1]), w["text"], font=f, fill="black")
                if style == "booktabs":
                    for y in (200, 270, 550):
                        d.line([(160, y), (900, y)], fill="black", width=2)
                p = tmp / f"{style}.png"
                img.save(p)
                lay = ocr_ref.build_reference(p, ocr_ref.with_variants([eng], "1,2"))["layout"]
                self.assertTrue(lay["table_grid2"] or lay["ruled_table2"], lay)


# --------------------------------------------------------------------------- judge
def line_entry(eid, y, hidden):
    return {"id": eid, "page": 1, "stem": "s", "kind": "line", "path": "x", "px": 1, "box": [80, y, 700, y + 40],
            "question": "", "fp": "f" + eid[-1], "hidden": hidden}


class JudgeTests(unittest.TestCase):
    def test_full_read_checks_operators_on_every_copy(self):
        a = {"title": "나이 조건 분기", "verbatim_text": [{"region": "title", "text": "나이 조건 분기"},
                                                       {"region": "body", "text": "if (age > 30) { price = adult; }"}],
             "code_blocks": [{"code": "if (age < 30) { price = adult; }"}]}
        e = {"id": "s__thumb_read", "page": 1, "stem": "s", "kind": "thumb_read", "path": "x", "px": 1, "box": None,
             "question": "", "fp": "f", "hidden": {"ref_lines": ["나이 조건 분기"], "unsupported": []}}
        j = cropmod.judge([e], {e["id"]: ["나이 조건 분기", "if (age > 30) { price = adult; }"]}, a)[0]
        self.assertEqual(j["verdict"], "issue")
        self.assertEqual(j["token_issues"][0]["issues"], [{"kind": "op", "read": [">"], "analysis": ["<"]}])
        self.assertEqual(cropmod.page_verdict([j]), "needs_attention")
        a["code_blocks"][0]["code"] = "if (age > 30) { price = adult; }"
        self.assertEqual(cropmod.judge([e], {e["id"]: ["나이 조건 분기", "if (age > 30) { price = adult; }"]}, a)[0]["verdict"], "ok")

    def test_line_crop_is_held_against_the_disputed_twin(self):
        twice = "if (value > 100) return 100;"
        a = {"title": "범위 제한", "code_blocks": [{"code": f"function clamp(value) {{\n{twice}\n}}\nfunction percent(value) {{\n"
                                                         "if (value >= 1000) return 1000;\n}"}]}
        plain = line_entry("s__L5", 300, {"ocr_text": twice})
        self.assertEqual(cropmod.judge([plain], {plain["id"]: twice}, a)[0]["verdict"], "agy_ok")  # matches the right twin
        held = line_entry("s__L5", 300, {"ocr_text": twice, "reason": "operator_or_digit",
                                         "analysis_line": "if (value >= 1000) return 1000;", "field": "code_blocks[0].code"})
        j = cropmod.judge([held], {held["id"]: twice}, a)[0]
        self.assertEqual(j["verdict"], "agy_wrong")
        self.assertTrue(j["token_issues"])

    def test_order_dispute_crops_are_judged_together(self):
        code = ["let message = makeGreeting(name);", "showReplyOnScreen(reply);", "let reply = sendMessage(message);"]
        a = {"title": "함수 호출 순서", "code_blocks": [{"code": "\n".join(code)}]}
        hid = {"reason": "line_order", "order_field": "code_blocks[0].code"}
        e3 = line_entry("s__L3", 250, dict(hid, ocr_text="let reply = sendMessage(message);"))
        e4 = line_entry("s__L4", 320, dict(hid, ocr_text="showRep1yOnScreen(rep1y)"))
        reads = {e3["id"]: "let reply = sendMessage(message);", e4["id"]: "showReplyOnScreen(reply);"}
        self.assertEqual(cropmod.judge([e3, e4], {e3["id"]: reads[e3["id"]]}, a), [])  # half a pair waits
        js = cropmod.judge([e3, e4], reads, a)
        self.assertEqual([j["verdict"] for j in js], ["agy_wrong", "agy_wrong"])
        self.assertEqual(js[0]["order_issue"], {"crops": ["s__L3", "s__L4"], "analysis_lines": [2, 1]})
        self.assertEqual(cropmod.page_verdict(js), "needs_attention")
        a["code_blocks"][0]["code"] = "\n".join([code[0], code[2], code[1]])
        self.assertEqual([j["verdict"] for j in cropmod.judge([e3, e4], reads, a)], ["agy_ok", "agy_ok"])

    @unittest.skipIf(Image is None, "Pillow missing")
    def test_line_crops_carry_the_dispute(self):
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        img = tmp / "s.png"
        Image.new("RGB", (640, 360), "white").save(img)
        sc = {"needs": ["crops"], "missing_lines": [
            {"id": "L003", "text": "a", "box": [10, 10, 200, 20], "missing_words": [], "reason": "line_order",
             "order_field": "code_blocks[0].code"},
            {"id": "L004", "text": "b", "box": [10, 50, 200, 20], "missing_words": [], "reason": "operator_or_digit",
             "analysis_line": "x < 1", "field": "code_blocks[0].code"}]}
        es = cropmod.prepare(img, {}, sc, {}, tmp / "crops", "s", 1)
        self.assertEqual(es[0]["hidden"], {"ocr_text": "a", "missing_words": [], "reason": "line_order",
                                           "order_field": "code_blocks[0].code"})
        self.assertEqual(es[1]["hidden"]["analysis_line"], "x < 1")


# --------------------------------------------------------------------------- pipeline
class OrderResolveTests(TmpCase):
    @unittest.skipIf(Image is None, "Pillow missing")
    def test_swapped_lines_with_correct_readings_end_needs_attention(self):
        img = self.deck(1, (1280, 720))[0]
        code = ["let message = makeGreeting(name);", "let reply = sendMessage(message);", "showReplyOnScreen(reply);"]
        ref = ref_of([("함수 호출 순서", [60, 40, 400, 44])] + [(t, [90, 180 + 70 * i, 700, 32]) for i, t in enumerate(code)],
                     layout={"code_boxes": [[60, 150, 1040, 270]]})
        a = {"title": "함수 호출 순서", "verbatim_text": [{"region": "title", "text": "함수 호출 순서"}],
             "code_blocks": [{"language": "javascript", "code": "\n".join([code[0], code[2], code[1]])}]}
        sc = score(a, ref)
        self.assertIn("line_order_mismatch", sc["reasons"])
        queue = cropmod.prepare(img, ref, sc, a, self.out / "crops", img.stem, 1)
        write_rec(self.out, img, 1, "escalated", "escalate", queue=queue, analysis=a, score=sc)
        lines = [e for e in queue if e["kind"] == "line"]
        self.assertEqual(len(lines), 2)
        reads = {e["id"]: e["hidden"]["ocr_text"] for e in lines}  # the reviewer types exactly what each crop shows
        reads.update({e["id"]: ["함수 호출 순서", *code] for e in queue if e["kind"] != "line"})
        (self.out / "crop_reads.json").write_text(json.dumps(reads, ensure_ascii=False), encoding="utf-8")
        with quiet():
            sa.cmd_resolve([img], [1], self.out, ns())
        r = read_rec(self.out, img)
        self.assertEqual(r["status"], "needs_attention")
        self.assertEqual({j["verdict"] for j in r["judgments"] if j["kind"] == "line"}, {"agy_wrong"})


class StaleAnswerTests(TmpCase):
    def resolve(self, slides, pages):
        with quiet() as (o, e):
            rc = sa.cmd_resolve(slides, pages, self.out, ns())
        return rc, o.getvalue(), e.getvalue()

    def test_answers_to_a_replaced_record_are_stale(self):
        slides = self.deck(2)
        img = slides[0]
        old = struct_entry(self.out, img, 1, declared=0)
        write_rec(self.out, img, 1, "escalated", "escalate", queue=[old])
        reads = self.out / "crop_reads.json"
        reads.write_text(json.dumps({old["id"]: {"verdict": "ok"}}), encoding="utf-8")  # answered, never resolved
        rec = read_rec(self.out, img)
        self.assertEqual(sa.seed_ledger(self.out, {img.stem: rec}), 1)  # what `run` does before replacing it
        self.assertEqual(sa.read_json(self.out / sa.LEDGER)[old["id"]]["fp"], old["fp"])
        write_rec(self.out, img, 1, "escalated", "escalate", queue=[struct_entry(self.out, img, 1, declared=1)])
        rc, out, err = self.resolve(slides, [1])
        self.assertEqual(json.loads(out)["stale_answers"], 1)
        self.assertEqual(read_rec(self.out, img)["status"], "escalated")
        self.assertIn("review_queue.md", err)
        # an identical new entry keeps the answer
        write_rec(self.out, img, 1, "escalated", "escalate", queue=[old])
        self.resolve(slides, [1])
        self.assertEqual(read_rec(self.out, img)["status"], "accepted")

    def test_seeds_cover_answers_without_an_entry(self):
        seeds = sa.stale_seeds({"slide_001": None, "slide_01": {"queue": [{"id": "slide_01__L002", "fp": "ab"}]}},
                               {"slide_001__L002": "x", "slide_01__L002": "y", "slide_0011__L001": "z"})
        self.assertEqual({k: v["fp"] for k, v in seeds.items()}, {"slide_001__L002": "before-rerun", "slide_01__L002": "ab"})

    def test_run_seeds_the_ledger_before_replacing_a_record(self):
        slides = self.deck(6)
        img = slides[1]
        old = struct_entry(self.out, img, 2)
        write_rec(self.out, img, 2, "escalated", "escalate", queue=[old], sha256="0" * 64)  # the image changed
        (self.out / "crop_reads.json").write_text(json.dumps({old["id"]: {"verdict": "ok"}}), encoding="utf-8")
        from test_pipeline import EngineAndRunTests
        runner = EngineAndRunTests("test_single_engine_gets_variants")
        runner.out, runner.tmp = self.out, self.tmp
        runner.run_main(slides, "--pages", "2")
        self.assertEqual(sa.read_json(self.out / sa.LEDGER)[old["id"]]["fp"], old["fp"])

    def test_explicit_form_clears_a_stale_answer_and_the_queue_shows_fp(self):
        slides = self.deck(2)
        img = slides[0]
        e1 = struct_entry(self.out, img, 1, declared=0)
        write_rec(self.out, img, 1, "escalated", "escalate", queue=[e1])
        reads = self.out / "crop_reads.json"
        reads.write_text(json.dumps({e1["id"]: {"verdict": "ok"}}), encoding="utf-8")
        sa.seed_ledger(self.out, {img.stem: read_rec(self.out, img)})
        e2 = struct_entry(self.out, img, 1, declared=1)
        write_rec(self.out, img, 1, "escalated", "escalate", queue=[e2])
        self.resolve(slides, [1])
        self.assertIn(f"- fp: `{e2['fp']}`", (self.out / "review_queue.md").read_text(encoding="utf-8"))
        self.assertEqual(read_rec(self.out, img)["status"], "escalated")  # the same plain answer stays stale
        reads.write_text(json.dumps({e1["id"]: {"answer": {"verdict": "ok"}, "fp": e2["fp"]}}), encoding="utf-8")
        self.resolve(slides, [1])
        self.assertEqual(read_rec(self.out, img)["status"], "accepted")

    def test_a_stale_answer_never_blocks_or_beats_a_fresh_one(self):
        slides = self.deck(2)
        img = slides[0]
        e1 = struct_entry(self.out, img, 1, declared=0)
        write_rec(self.out, img, 1, "escalated", "escalate", queue=[e1])
        (self.out / "crop_reads.json").write_text(json.dumps({e1["id"]: {"verdict": "ok"}}), encoding="utf-8")
        sa.seed_ledger(self.out, {img.stem: read_rec(self.out, img)})
        write_rec(self.out, img, 1, "escalated", "escalate", queue=[struct_entry(self.out, img, 1, declared=1)])
        (self.out / "crop_reads_1.json").write_text(json.dumps({e1["id"]: {"verdict": "issue", "note": "표 없음"}}),
                                                    encoding="utf-8")
        rc, out, err = self.resolve(slides, [1])
        self.assertNotEqual(rc, 5, err)
        self.assertEqual(read_rec(self.out, img)["status"], "needs_attention")  # the fresh shard answer was used
        # two fresh answers that differ still stop resolve
        write_rec(self.out, img, 1, "escalated", "escalate", queue=[struct_entry(self.out, img, 1, declared=2)])
        (self.out / sa.LEDGER).unlink()
        self.assertEqual(self.resolve(slides, [1])[0], 5)

    def test_interrupted_rescore_leaves_no_current_looking_answers(self):
        slides = self.deck(3)
        for p, img in enumerate(slides, 1):
            write_rec(self.out, img, p, "escalated", "escalate", queue=[struct_entry(self.out, img, p)])
        (self.out / "crop_reads.json").write_text(json.dumps({f"{img.stem}__thumb_struct": {"verdict": "ok"}
                                                              for img in slides}), encoding="utf-8")
        esc = {"decision": "accept", "reasons": [], "metrics": {}, "triggers": [], "needs": []}
        calls = []

        def dying_save(out, img, rec):
            calls.append(img.stem)
            raise KeyboardInterrupt  # killed at the first save
        with mock.patch.object(sa, "score", return_value=esc), mock.patch.object(sa, "save", dying_save), quiet():
            with self.assertRaises(KeyboardInterrupt):
                sa.rescore(slides, [1, 2, 3], self.out, ns(), sa.load_thresholds(), lambda i, p: None)
        self.assertEqual(calls, ["slide_001"])
        self.assertEqual(sorted(sa.read_json(self.out / sa.LEDGER)), [f"{img.stem}__thumb_struct" for img in slides])


class SettleTests(TmpCase):
    """`run` scores each slide against the deck boilerplate of the analyses on disk at that moment (the first slides
    see too few); settle() then re-scores the slides it analysed against the whole deck, without agy, so `run` ends
    with the decisions `rescore` makes on the same analyses and references."""

    def main(self, slides, cmd="run", *args, thresholds=None, mk=logo_slide, quota_after=None):
        """mk(k) -> (analysis agy returns, reference) of slide k; quota_after: agy runs out after that many calls."""
        calls = []

        def fake_call(agy_task, wd, prompt, out_name, stage, a):
            if quota_after is not None and len(calls) >= quota_after:
                raise sa.QuotaExhausted("RESOURCE_EXHAUSTED (simulated)")
            calls.append(stage[0][1])
            k = int(Path(stage[0][1]).stem.split("_")[1])
            return dict(mk(k)[0], page=k, file=stage[0][1]), None

        def fake_ref(img, out, engines, rebuild=False):
            ref = dict(mk(int(img.stem.split("_")[1]))[1], file=img.name, sha256=ocr_ref.sha256_file(img))
            sa.ref_path(out, img).parent.mkdir(parents=True, exist_ok=True)
            sa.ref_path(out, img).write_text(json.dumps(ref, ensure_ascii=False), encoding="utf-8")
            return ref
        extra = []
        if thresholds:
            (self.tmp / "th.json").write_text(json.dumps(thresholds), encoding="utf-8")
            extra = ["--thresholds", str(self.tmp / "th.json")]
        sa.STOP.clear()
        with mock.patch.dict("os.environ", {"AGY_TASK": str(HERE / "fake_agy_task.py")}), \
                mock.patch.object(sa, "pick_engines", return_value=["engine"]), \
                mock.patch.object(sa, "get_reference", side_effect=fake_ref), \
                mock.patch.object(sa, "call_agy", fake_call), quiet() as (_o, err):
            rc = sa.main([cmd, "--slides", str(slides[0].parent), "--out", str(self.out), "--audit-rate", "0",
                          "--shadow-audit-rate", "0", *args, *extra])
        return rc, calls, err.getvalue()

    def decisions(self, slides):
        return {img.stem: read_rec(self.out, img)["decision"] for img in slides}

    def records(self, slides):
        """What run and rescore must agree on: decision, status, queue, the stored score and score_legacy."""
        out = {}
        for img in slides:
            r = read_rec(self.out, img)
            out[img.stem] = (r["decision"], r["status"], tuple(sorted((e["kind"], e["id"], e["fp"]) for e in r["queue"])),
                             r["score"], r["score_legacy"])
        return out

    def assert_run_is_rescore(self, slides, mk=logo_slide):
        after_run = self.records(slides)
        rc, calls, _err = self.main(slides, "rescore", mk=mk)
        self.assertEqual(calls, [])
        self.assertEqual(self.records(slides), after_run)
        return after_run

    def test_run_ends_with_the_rescore_decisions(self):
        slides = self.deck(4)
        rc, calls, err = self.main(slides, "run", "-j", "1")  # one at a time: slides 1-2 see fewer than 3 logos
        self.assertEqual((rc, len(calls)), (0, 4))
        for img in slides[:2]:
            r = read_rec(self.out, img)
            self.assertEqual(r["attempts"][-1]["decision"], "escalate")  # the first decision
            self.assertEqual((r["decision"], r["status"], r["queue"]), ("accept", "accepted", []))
            self.assertIn(f"[{int(img.stem[-3:]):03d}] settle: escalate -> accept", err)
            self.assertEqual(r["score_legacy"]["decision"], "escalate")
        self.assertEqual(read_rec(self.out, slides[2])["attempts"][-1]["decision"], "accept")
        after_run = self.decisions(slides)
        rc, calls, _err = self.main(slides, "rescore")
        self.assertEqual((rc, calls), (0, []))
        self.assertEqual(self.decisions(slides), after_run)
        self.assertEqual(set(after_run.values()), {"accept"})
        report = (self.out / "report.md").read_text(encoding="utf-8")
        self.assertIn("4 accepted by the new rules only", report)

    def test_resumed_run_settles_the_slides_of_the_first_run(self):
        slides = self.deck(5)
        rc, calls, _err = self.main(slides, "run", "-j", "1", quota_after=2)  # quota stop: slides 1-2 saw 2 logos
        self.assertEqual((rc, len(calls)), (3, 2))
        self.assertEqual(self.decisions(slides[:2]), {"slide_001": "escalate", "slide_002": "escalate"})
        rc, calls, err = self.main(slides, "run", "-j", "1")  # the documented "re-run the same command"
        self.assertEqual((rc, len(calls)), (0, 3))
        self.assertIn("[001] settle: escalate -> accept", err)
        after = self.assert_run_is_rescore(slides)
        self.assertEqual({v[:3] for v in after.values()}, {("accept", "accepted", ())})

    def test_partial_force_run_settles_the_slides_outside_it(self):
        slides = self.deck(4)
        self.assertEqual(self.main(slides, "run", "-j", "1")[0], 0)
        # the re-analyses of slides 2-4 no longer transcribe the footer: slide 1's logo is backed by nothing now
        mk = lambda k: logo_slide(k, logo=LOGO if k == 1 else None)  # noqa: E731
        rc, calls, err = self.main(slides, "run", "-j", "1", "--force", "--pages", "2-4", mk=mk)
        self.assertEqual(len(calls), 3)
        self.assertIn("[001] settle: accept -> escalate", err)
        r1 = read_rec(self.out, slides[0])
        self.assertEqual((r1["decision"], r1["status"]), ("escalate", "escalated"))
        self.assert_run_is_rescore(slides, mk)

    def test_settle_requeues_an_escalation_whose_reasons_changed(self):
        extra = "슬롯은 바깥 내용을 끼워 넣는다"

        def mk(k):  # every slide also omits a body line: it escalates for recall with or without the logo
            an, _ref = logo_slide(k)
            return an, ref_of([(an["title"], [60, 40, 500, 44]), (an["verbatim_text"][1]["text"], [60, 140, 700, 30]),
                               (extra, [60, 200, 700, 30])])
        slides = self.deck(4, (1280, 720))  # the line boxes must lie inside the image to be cropped
        rc, calls, err = self.main(slides, "run", "-j", "1", mk=mk)
        self.assertEqual((rc, len(calls)), (2, 4))
        r1 = read_rec(self.out, slides[0])
        self.assertIn("unsupported_field: verbatim_text[2]", r1["attempts"][-1]["reasons"])  # the first decision
        self.assertEqual([r.split()[0] for r in r1["score"]["reasons"]], ["recall"])
        self.assertEqual([e["kind"] for e in r1["queue"]], ["line"])  # no thumbnail for the logo any more
        self.assertIn("[001] settle: escalate -> escalate", err)
        self.assert_run_is_rescore(slides, mk)

    def test_switch_off_keeps_the_logo_unsupported(self):
        slides = self.deck(4)
        rc, calls, err = self.main(slides, "run", "-j", "1", thresholds={"hf_boilerplate": False})
        self.assertEqual((rc, len(calls)), (2, 4))
        self.assertNotIn("settle:", err)
        self.assertEqual(set(self.decisions(slides).values()), {"escalate"})

    def test_settle_never_calls_agy_and_keeps_a_retry(self):
        slides = self.deck(4)
        for p, img in enumerate(slides, 1):
            an, ref = logo_slide(p)
            sa.ref_path(self.out, img).parent.mkdir(parents=True, exist_ok=True)
            sa.ref_path(self.out, img).write_text(json.dumps(ref, ensure_ascii=False), encoding="utf-8")
            judged = [{"id": f"{img.stem}__thumb_read", "kind": "thumb_read", "verdict": "ok"}] if p == 3 else []
            write_rec(self.out, img, p, "escalated", "escalate", analysis=an, judgments=judged,
                      queue=[struct_entry(self.out, img, p)])
        # slide 4: stored as accepted, but against its reference the analysis now misses most lines -> retry
        an, ref = logo_slide(4, logo=None)
        ref = ref_of([(an["title"], [60, 40, 500, 44])] + [(t, [60, 140 + 50 * i, 700, 30]) for i, t in enumerate(
            ("속성은 부모에서 자식으로 내려간다", "이벤트는 자식에서 부모로 올라간다", "슬롯은 바깥 내용을 끼워 넣는다"))])
        sa.ref_path(self.out, slides[3]).write_text(json.dumps(ref, ensure_ascii=False), encoding="utf-8")
        write_rec(self.out, slides[3], 4, analysis=an)
        old = read_rec(self.out, slides[0])["queue"][0]
        (self.out / "crop_reads.json").write_text(json.dumps({old["id"]: {"verdict": "ok"}}), encoding="utf-8")
        with mock.patch.object(sa, "call_agy", side_effect=AssertionError("settle called agy")), quiet() as (_o, err):
            changed = sa.settle(slides, self.out, sa.load_thresholds())
        self.assertEqual(changed, [(1, "escalate", "accept"), (2, "escalate", "accept")])  # 3 holds judgments
        r1 = read_rec(self.out, slides[0])
        self.assertEqual((r1["status"], r1["queue"], r1["score_legacy"]["decision"]), ("accepted", [], "escalate"))
        self.assertEqual(sa.read_json(self.out / sa.LEDGER)[old["id"]]["fp"], old["fp"])  # its answer is stale now
        self.assertEqual(read_rec(self.out, slides[2])["decision"], "escalate")
        r4 = read_rec(self.out, slides[3])
        self.assertEqual((r4["decision"], r4["status"]), ("accept", "accepted"))
        self.assertIn("[004] settle: re-scored as a retry, which would need agy; the stored decision (accept) stays",
                      err.getvalue())
        self.assertEqual(sa.settle(slides, self.out, sa.load_thresholds()), [])  # settled records stay as they are

    def test_results_outside_the_deck_never_feed_the_boilerplate(self):
        slides = self.deck(2)
        for p, img in enumerate(slides, 1):
            an, ref = logo_slide(p)
            sa.ref_path(self.out, img).parent.mkdir(parents=True, exist_ok=True)
            sa.ref_path(self.out, img).write_text(json.dumps(ref, ensure_ascii=False), encoding="utf-8")
            write_rec(self.out, img, p, analysis=dict(an, page=p, file=img.name))
        for k in range(50, 53):  # left over from a longer deck in the same --out (run --force keeps them)
            (self.out / "results" / f"slide_{k:03d}.json").write_text(
                json.dumps({"page": k, "analysis": logo_slide(k)[0]}, ensure_ascii=False), encoding="utf-8")
        th = sa.load_thresholds()
        self.assertEqual(sa.deck_hf_boilerplate(slides, self.out, th), set())
        extra = {f"slide_{k:03d}": logo_slide(k)[0] for k in (50, 51)}  # nor do analyses passed in for other stems
        self.assertEqual(sa.deck_hf_boilerplate(slides, self.out, th, extra), set())
        self.assertEqual(sa.deck_hf_boilerplate(slides + [self.tmp / "slides" / "slide_050.png"], self.out, th),
                         set(units(LOGO)))
        with quiet():
            sa.rescore(slides, [1, 2], self.out, ns(), th, lambda img, p: sa.read_json(sa.ref_path(self.out, img)))
        self.assertEqual(self.decisions(slides), {"slide_001": "escalate", "slide_002": "escalate"})

    def test_settle_ledger_keeps_only_entries_of_the_same_answer(self):
        slides = self.deck(1)
        img = slides[0]
        entry = struct_entry(self.out, img, 1)
        write_rec(self.out, img, 1, "escalated", "escalate", queue=[entry])
        rid = entry["id"]
        (self.out / "crop_reads.json").write_text(json.dumps({rid: {"verdict": "ok"}}), encoding="utf-8")
        sha = sa._ans_sha({"verdict": "ok"})
        led = {rid: {"fp": "older", "sha": sha}}  # this answer was already judged against an older entry
        (self.out / sa.LEDGER).write_text(json.dumps(led), encoding="utf-8")
        self.assertEqual(sa.seed_ledger(self.out, {img.stem: read_rec(self.out, img)}, keep=True), 0)
        led[rid]["sha"] = "another answer"  # the answer on disk is newer: it was written for the entry shown now
        (self.out / sa.LEDGER).write_text(json.dumps(led), encoding="utf-8")
        self.assertEqual(sa.seed_ledger(self.out, {img.stem: read_rec(self.out, img)}, keep=True), 1)
        self.assertEqual(sa.read_json(self.out / sa.LEDGER)[rid], {"fp": entry["fp"], "sha": sha})


@NEEDS_NP
class ReferenceKeepTests(TmpCase):
    TEXTS = ["SELECT id, name FROM students WHERE grade"]

    def setUp(self):
        super().setUp()
        self.slides = self.deck(2)
        self.eng = FakeEngine("fake", self.TEXTS)
        for img in self.slides:  # version 2 references of the same images
            ref = ocr_ref.build_reference(img, [self.eng])
            ref["version"] = 2
            sa.ref_path(self.out, img).parent.mkdir(parents=True, exist_ok=True)
            sa.ref_path(self.out, img).write_text(json.dumps(ref), encoding="utf-8")

    def test_pending_line_crop_keeps_the_reference(self):
        img = self.slides[0]
        write_rec(self.out, img, 1, "escalated", "escalate",
                  queue=[{"id": f"{img.stem}__L001", "kind": "line", "page": 1, "stem": img.stem}])
        self.assertEqual(ocr_ref.ref_state(img, sa.ref_path(self.out, img), [self.eng])[0], "kept")
        self.assertEqual(ocr_ref.ref_state(img, sa.ref_path(self.out, img), [self.eng], rebuild=True)[0], "build")
        write_rec(self.out, img, 1, "escalated", "escalate", queue=[struct_entry(self.out, img, 1)])
        self.assertEqual(ocr_ref.ref_state(img, sa.ref_path(self.out, img), [self.eng])[0], "build")  # no line ids

    def test_reference_reports_built_kept_and_unchanged(self):
        img = self.slides[0]
        write_rec(self.out, img, 1, "escalated", "escalate",
                  queue=[{"id": f"{img.stem}__L001", "kind": "line", "page": 1, "stem": img.stem}])

        def main():
            with mock.patch.object(sa, "pick_engines", return_value=[self.eng]), \
                    mock.patch.dict("os.environ", {"AGY_TASK": str(HERE / "fake_agy_task.py")}), quiet() as (o, _e):
                self.assertEqual(sa.main(["reference", "--slides", str(img.parent), "--out", str(self.out)]), 0)
            return json.loads(o.getvalue())
        self.assertEqual({k: v for k, v in main().items() if k != "out"},
                         {"built": 1, "kept": 1, "unchanged": 0, "failed": 0, "of": 2})
        self.assertEqual(sa.read_json(sa.ref_path(self.out, img))["version"], 2)
        self.assertEqual({k: v for k, v in main().items() if k != "out"},
                         {"built": 0, "kept": 1, "unchanged": 1, "failed": 0, "of": 2})


if __name__ == "__main__":
    unittest.main()

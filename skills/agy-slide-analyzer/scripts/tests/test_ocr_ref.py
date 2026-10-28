"""Unit tests for ocr_ref (stdlib unittest): dark code panels and the layout version 3 keys, table_grid2 /
ruled_table2 on synthetic tables, variant agreement in merge_results, with_variants / make_engines, the Windows
OCR size cap, REF_VERSION 3 and the load_or_build judgment guard.

    python -m unittest discover -s scripts/tests -v        (from the skill folder)

Most tests draw their own images and know every word box, so they need no OCR engine; the few that read through
Windows OCR are skipped when it is unavailable.
"""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import ocr_ref  # noqa: E402
from textnorm import norm  # noqa: E402

try:
    from PIL import Image, ImageDraw
    import synth
    HAVE_FONT = any(Path(c).exists() for c in synth.FONT_CANDIDATES)
except ImportError:  # pragma: no cover
    Image = None
    HAVE_FONT = False
NEEDS_NP = unittest.skipUnless(ocr_ref.np is not None and Image is not None, "needs numpy and Pillow")
NEEDS_FONT = unittest.skipUnless(ocr_ref.np is not None and HAVE_FONT, "needs numpy and a Korean TTF font")
OLD_LAYOUT_KEYS = ("available", "dark_background", "ink_ratio", "text_ink_ratio", "graphics_ratio", "h_rules", "v_rules",
                   "graphic_region", "table_candidate", "ruled_table", "title_band", "dhash")


def winocr():
    e = ocr_ref.WinOcrEngine()
    return e if e.available() else None


def put(d, x, y, text, fnt, words, fill="black"):
    """Draw text word by word from (x, y) and record each word's box as an OCR word would carry it."""
    space = fnt.getlength(" ")
    for t in text.split():
        l, tp, r, b = d.textbbox((x, y), t, font=fnt)
        d.text((x, y), t, font=fnt, fill=fill)
        words.append({"text": t, "conf": None, "box": [int(l), int(tp), int(r - l), int(b - tp)], "weight": "mid"})
        x += fnt.getlength(t) + space
    return words


# --------------------------------------------------------------------------- code panels / layout v3
def panel_slide(red_x=False, photo=False):
    """1280x720: a title, a sentence, a dark code panel with light code text, optionally a dark-red X drawn against
    the panel's right edge or a noisy photo block."""
    img = Image.new("RGB", (1280, 720), "white")
    d = ImageDraw.Draw(img)
    words = []
    put(d, 60, 30, "3장. 반응형 데이터", synth.font(synth.BOLD_CANDIDATES, 40), words)
    put(d, 60, 120, "아래 코드는 클릭 횟수를 화면에 보여준다.", synth.font(synth.FONT_CANDIDATES, 28), words)
    d.rectangle((60, 200, 760, 520), fill=(30, 30, 30))
    mono = synth.font(synth.MONO_CANDIDATES, 24)
    for i, line in enumerate(["const count = ref(0)", "function add() {", "  count.value++", "}",
                              "watch(count, (n) => log(n))", "export default { setup }"]):
        put(d, 90, 225 + i * 48, line, mono, words, fill=(220, 220, 220))
    if red_x:  # luma ~67: dark under a looser threshold, ink either way
        d.line((760, 210, 1060, 510), fill=(200, 10, 10), width=24)
        d.line((1060, 210, 760, 510), fill=(200, 10, 10), width=24)
    if photo:
        import numpy as np
        noise = np.random.default_rng(0).integers(0, 60, size=(320, 400), dtype=np.uint8)
        img.paste(Image.fromarray(noise).convert("RGB"), (820, 200))
    return img, words


@NEEDS_FONT
class CodePanelTests(unittest.TestCase):
    def test_panel_is_detected(self):
        img, _ = panel_slide()
        panels = ocr_ref.detect_code_panels(img)
        self.assertEqual(len(panels), 1, panels)
        l, t, w, h = panels[0]
        self.assertTrue(l <= 66 and t <= 206 and l + w >= 754 and t + h >= 514, panels)

    def test_code_panel_is_not_graphics(self):
        img, words = panel_slide()
        lay = ocr_ref.layout_stats(img, words)
        self.assertGreaterEqual(lay["graphics_ratio"], 0.06)  # the raw ratio keeps its version 2 meaning
        self.assertLess(lay["graphics_ratio_nocode"], 0.015)
        self.assertEqual(lay["code_boxes"], ocr_ref.detect_code_panels(img))
        self.assertTrue(lay["text_masked"])
        self.assertEqual(lay["layout_version"], 3)

    def test_red_mark_against_panel_stays_graphics(self):
        img, words = panel_slide(red_x=True)
        lay = ocr_ref.layout_stats(img, words)
        self.assertEqual(len(lay["code_boxes"]), 1)
        l, _, w, _ = lay["code_boxes"][0]
        self.assertLessEqual(l + w, 766, "the red X must not be swallowed into the panel")
        self.assertGreaterEqual(lay["graphics_ratio_nocode"], 0.015)

    def test_photo_stays_graphics(self):
        img, words = panel_slide(photo=True)
        lay = ocr_ref.layout_stats(img, words)
        self.assertFalse(any(l > 800 for l, _, _, _ in lay["code_boxes"]), lay["code_boxes"])
        self.assertGreaterEqual(lay["graphics_ratio_nocode"], 0.06)

    def test_no_panel_on_dark_slide_or_narrow_box(self):
        dark = Image.new("RGB", (1280, 720), (20, 20, 20))
        ImageDraw.Draw(dark).rectangle((100, 200, 900, 500), fill=(0, 0, 0))
        self.assertEqual(ocr_ref.detect_code_panels(dark), [])
        narrow = Image.new("RGB", (1280, 720), "white")
        ImageDraw.Draw(narrow).rectangle((100, 200, 220, 500), fill=(0, 0, 0))  # < 12% of the page wide
        self.assertEqual(ocr_ref.detect_code_panels(narrow), [])

    def test_old_keys_kept_and_nocode_equals_raw_without_panels(self):
        img = Image.new("RGB", (1280, 720), "white")
        d = ImageDraw.Draw(img)
        words = put(d, 60, 150, "기본 키는 NULL을 허용하지 않는다.", synth.font(synth.FONT_CANDIDATES, 30), [])
        d.ellipse((800, 300, 1000, 500), outline="black", width=6)
        lay = ocr_ref.layout_stats(img, words)
        for k in OLD_LAYOUT_KEYS:
            self.assertIn(k, lay)
        self.assertEqual(lay["code_boxes"], [])
        self.assertEqual(lay["graphics_ratio_nocode"], lay["graphics_ratio"])

    def test_text_layer_page_is_not_masked(self):
        img, words = panel_slide()
        self.assertFalse(ocr_ref.layout_stats(img, [])["text_masked"])
        self.assertTrue(ocr_ref.layout_stats(img, [], text_masked=True)["text_masked"])
        self.assertTrue(ocr_ref.layout_stats(img, words)["text_masked"])


# --------------------------------------------------------------------------- tables
T1 = [["디렉티브", "역할", "비고"],
      ["v-if", "조건이 거짓이면 렌더링하지 않음", "전환 비용 높음"],
      ["v-show", "display 속성만 전환", "초기 비용 높음"],
      ["v-for", "배열 반복 렌더링", "key 필수"],
      ["v-bind", "속성 바인딩", "약어 :"],
      ["v-on", "이벤트 리스너 연결", "약어 @"]]
T2 = [["구분", "Options API", "Composition API"],
      ["상태 선언", "data 함수", "ref / reactive"],
      ["계산 속성", "computed 옵션", "computed 함수"],
      ["감시", "watch 옵션", "watch 함수"],
      ["생명주기", "mounted 등", "onMounted 등"]]
TABLES = {  # name: (rows, column widths, style, cell padding)
    "T1_ruled": (T1, [220, 520, 320], "ruled", 16),
    "T1_booktabs": (T1, [220, 520, 320], "booktabs", 16),
    "T1_hlines": (T1, [220, 520, 320], "hlines", 16),
    "T1_zebra": (T1, [220, 520, 320], "zebra", 16),
    "T1_borderless": (T1, [220, 520, 320], "none", 16),
    "T2_ruled": (T2, [220, 330, 330], "ruled", 16),
    "T2_borderless": (T2, [220, 330, 330], "none", 16),
    "T2_booktabs": (T2, [220, 330, 330], "booktabs", 16),
    "T2_tight": (T2, [140, 200, 200], "none", 4),
    "T1_tight_ruled": (T1, [130, 380, 200], "ruled", 6),
}


def table_slide(rows, widths, style, pad, rh=56, x0=200, y0=230):
    img = Image.new("RGB", (1500, 844), "white")
    d = ImageDraw.Draw(img)
    words = put(d, 80, 140, "디렉티브 비교", synth.font(synth.BOLD_CANDIDATES, 34), [])
    body, bold = synth.font(synth.FONT_CANDIDATES, 22), synth.font(synth.BOLD_CANDIDATES, 22)
    xs = [x0]
    for w in widths:
        xs.append(xs[-1] + w)
    for r, row in enumerate(rows):
        y = y0 + r * rh
        if style == "zebra" and r % 2 == 0:
            d.rectangle([x0, y, xs[-1], y + rh], fill=(225, 230, 240))
        if style == "ruled" and r == 0:
            d.rectangle([x0, y, xs[-1], y + rh], fill=(210, 220, 235))
        for c, cell in enumerate(row):
            put(d, xs[c] + pad, y + 15, cell, bold if r == 0 else body, words)
    yb = y0 + len(rows) * rh
    if style == "ruled":
        for r in range(len(rows) + 1):
            d.line([(x0, y0 + r * rh), (xs[-1], y0 + r * rh)], fill=(60, 60, 60), width=2)
        for x in xs:
            d.line([(x, y0), (x, yb)], fill=(60, 60, 60), width=2)
    if style == "booktabs":
        for y in (y0, y0 + rh, yb):
            d.line([(x0, y), (xs[-1], y)], fill="black", width=2)
    if style == "hlines":
        for r in range(len(rows) + 1):
            d.line([(x0, y0 + r * rh), (xs[-1], y0 + r * rh)], fill=(150, 150, 150), width=1)
    return img, words


def framed_prose():
    """A memo frame (border plus a folded corner) around bulleted prose: a ruled box, but not a table."""
    img = Image.new("RGB", (1500, 844), "white")
    d = ImageDraw.Draw(img)
    d.rectangle([200, 200, 1300, 600], outline="black", width=2)
    d.line([(1250, 200), (1300, 250)], fill="black", width=2)
    words, fnt = [], synth.font(synth.FONT_CANDIDATES, 26)
    for i, t in enumerate(["• 반드시 v-for와 key를 함께 사용한다", "• 내부 컴포넌트의 상태를 일관 되게 하여",
                           "• 데이터의 예측 가능한 행동을 유지하기 위함", "• 조건에 관계 없이 항상 렌더링"]):
        put(d, 260, 240 + i * 80, t, fnt, words)
    return img, words


def grid_words(words):
    return [w for w in words if len(norm(w["text"])) >= 2]


@NEEDS_FONT
class TableTests(unittest.TestCase):
    def test_synthetic_tables_are_found(self):
        for name, (rows, widths, style, pad) in TABLES.items():
            with self.subTest(table=name):
                img, words = table_slide(rows, widths, style, pad)
                lay = ocr_ref.layout_stats(img, grid_words(words), [grid_words(words)])
                self.assertTrue(lay["table_grid2"] or lay["ruled_table2"], (lay["table_grid2"], lay["ruled_table2"]))

    def test_ruled_table_has_text_in_cells(self):
        img, words = table_slide(*TABLES["T1_ruled"])
        self.assertTrue(ocr_ref.layout_stats(img, grid_words(words))["ruled_table2"])

    def test_framed_prose_is_not_a_table(self):
        img, words = framed_prose()
        lay = ocr_ref.layout_stats(img, grid_words(words), [grid_words(words)])
        self.assertIsNone(lay["table_grid2"])
        self.assertFalse(lay["ruled_table2"])

    def test_grid2_ignores_header_band_and_prose(self):
        hdr = [{"text": t, "box": [100 + 300 * c, 20 + 30 * r, 120, 24]} for r in range(3) for c, t in enumerate(("ab", "cd", "ef"))]
        self.assertIsNone(ocr_ref.table_grid2(hdr, 844))  # every row sits in the top 13% template band
        prose = []
        for r in range(5):
            x = 100
            for t in "this sentence keeps going with ordinary spaces between words".split():
                prose.append({"text": t, "box": [x, 300 + 50 * r, 20 * len(t), 24]})
                x += 20 * len(t) + 8
        self.assertIsNone(ocr_ref.table_grid2(prose, 844))


@unittest.skipUnless(ocr_ref.np is not None and HAVE_FONT and Image is not None and winocr(), "needs Windows OCR")
class TableOcrTests(unittest.TestCase):
    """The same detection on words read by Windows OCR rather than drawn boxes."""

    def test_tables_through_ocr(self):
        tmp = Path(tempfile.mkdtemp())
        cases = {"T1_ruled": True, "T2_borderless": True, "framed": False}
        for name, want in cases.items():
            with self.subTest(case=name):
                img = framed_prose()[0] if name == "framed" else table_slide(*TABLES[name])[0]
                p = tmp / f"{name}.png"
                img.save(p)
                lay = ocr_ref.build_reference(p, [winocr()])["layout"]
                self.assertEqual(bool(lay["table_grid2"] or lay["ruled_table2"]), want, lay)


# --------------------------------------------------------------------------- merge / agreement
def result(engine, texts, conf=None, recognizer=None, y0=100):
    lines = []
    for i, t in enumerate(texts):
        words = [{"text": w, "conf": conf, "box": [100 + 120 * j, y0 + 50 * i, 100, 30]} for j, w in enumerate(t.split())]
        lines.append({"text": t, "conf": conf, "box": ocr_ref._union([w["box"] for w in words]), "words": words})
    r = {"engine": engine, "lang": "ko", "lines": lines}
    if recognizer:
        r["recognizer"] = recognizer
    return r


def by_text(lines):
    return {w["text"]: w for l in lines for w in l["words"]}


class MergeTests(unittest.TestCase):
    def test_variant_agreement_is_exact_ocr_key(self):
        base = result("winocr", ["EnabIing temperature Netwo「k x", "다같이 배워요"])
        up = result("winocr_x2", ["Enabling temperatura Network", "다같이 배워요"])
        lines, texts = ocr_ref.merge_results([base, up])
        w = by_text(lines)
        self.assertEqual((w["EnabIing"]["weight"], w["EnabIing"]["agreed_by"]), ("variant", "variant"))
        self.assertEqual(w["Netwo「k"]["weight"], "variant")  # folded look-alike: 「 -> r
        # a fuzzy match is enough for two different engines, never for two readings of one recognizer
        self.assertEqual((w["temperature"]["weight"], w["temperature"]["agreed_by"]), ("mid", None))
        self.assertFalse(w["temperature"]["agreed"])
        self.assertEqual(w["x"]["weight"], "low")
        # the base text holds too few of the upscaled line's words fuzzily, so that line is kept as well
        # (the version 2 rule for a secondary reading); its words agree back with the base exactly
        self.assertEqual([l["engine"] for l in lines], ["winocr", "winocr", "winocr_x2"])
        self.assertEqual((w["Enabling"]["weight"], w["temperatura"]["weight"]), ("variant", "mid"))
        self.assertEqual(set(texts), {"winocr", "winocr_x2"})

    def test_variant_adds_missed_line_and_is_never_high(self):
        base = result("winocr", ["기본 키는 NULL을 허용하지 않는다"])
        up = result("winocr_x2", ["기본 키는 NULL을 허용하지 않는다", "EXAMPLE CODE AI ACADEMY FOR TEENS"], y0=100)
        lines, _ = ocr_ref.merge_results([base, up])
        self.assertEqual([l["engine"] for l in lines], ["winocr", "winocr_x2"])
        self.assertTrue({w["weight"] for l in lines for w in l["words"]} <= {"variant", "mid", "low"})
        self.assertEqual(lines[1]["id"], "L002")

    def test_cross_engine_keeps_fuzzy_agreement(self):
        lines, _ = ocr_ref.merge_results([result("winocr", ["temperatura"]), result("tesseract", ["temperature"], conf=90.0)])
        w = lines[0]["words"][0]
        self.assertEqual((w["weight"], w["agreed_by"], w["agreed"]), ("high", "engine", True))

    def test_engine_agreement_wins_over_variant(self):
        lines, _ = ocr_ref.merge_results([result("winocr", ["Hello"]), result("tesseract", ["Hello"], conf=90.0),
                                          result("winocr_x2", ["Hello"])])
        self.assertEqual(lines[0]["words"][0]["agreed_by"], "engine")
        self.assertEqual(lines[0]["words"][0]["weight"], "high")

    def test_recognizer_of_an_output(self):
        self.assertEqual(ocr_ref._recognizer({"engine": "winocr_x2"}), "winocr")
        self.assertEqual(ocr_ref._recognizer({"engine": "winocr_x1.5"}), "winocr")
        self.assertEqual(ocr_ref._recognizer({"engine": "tesseract"}), "tesseract")
        self.assertEqual(ocr_ref._recognizer({"engine": "fake2", "recognizer": "fake"}), "fake")


# --------------------------------------------------------------------------- engines
class FakeEngine(ocr_ref.Engine):
    def __init__(self, name, texts, recognizer=None):
        self.name, self.texts, self.recognizer, self.calls = name, texts, recognizer, 0

    def available(self):
        return True

    def run(self, image):
        self.calls += 1
        return result(self.name, self.texts, recognizer=self.recognizer)


class VariantEngineTests(unittest.TestCase):
    def test_with_variants(self):
        base = ocr_ref.WinOcrEngine(lang="en-US")
        names = lambda es: [e.name for e in es]  # noqa: E731
        self.assertEqual(names(ocr_ref.with_variants([base], "1,2")), ["winocr", "winocr_x2"])
        self.assertEqual(names(ocr_ref.with_variants([base], [1, 2, 3])), ["winocr", "winocr_x2", "winocr_x3"])
        twice = ocr_ref.with_variants(ocr_ref.with_variants([base], "1,2"), "1,2")
        self.assertEqual(names(twice), ["winocr", "winocr_x2"])
        self.assertEqual(twice[1].lang, "en-US")
        self.assertEqual(twice[1].scale, 2)
        self.assertEqual(names(ocr_ref.with_variants([base], "1")), ["winocr"])
        tess = ocr_ref.TesseractEngine(binary="tesseract")
        self.assertEqual(names(ocr_ref.with_variants([tess], "1,2")), ["tesseract"])  # has its own confidences
        self.assertEqual(names(ocr_ref.with_variants([base, tess], "1,2")), ["winocr", "tesseract", "winocr_x2"])

    def test_scales_from_environment(self):
        base = ocr_ref.WinOcrEngine()
        with mock.patch.dict(os.environ, {"OCR_SCALES": "1,2,3"}):
            self.assertEqual([e.name for e in ocr_ref.with_variants([base])], ["winocr", "winocr_x2", "winocr_x3"])
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual([e.name for e in ocr_ref.with_variants([base])], ["winocr", "winocr_x2"])
        self.assertEqual(ocr_ref.parse_scales("1, 2,x3,abc,nan,inf,0,-1,1.5,2"), [1, 2, 3, 1.5])

    def test_make_engines_accepts_variant_names(self):
        e = ocr_ref.make_engines(["winocr_x2"])[0]
        self.assertEqual((e.name, e.scale), ("winocr_x2", 2))
        self.assertEqual(e.info()["variant_of"], "winocr")
        self.assertEqual(ocr_ref.WinOcrEngine().name, "winocr")
        self.assertNotIn("scale", ocr_ref.WinOcrEngine().info())
        for bad in ("tesseract_x2", "bogus", "winocr_x0"):
            with self.assertRaises(ValueError):
                ocr_ref.make_engines([bad])

    def test_available_engines_never_lists_variants(self):
        self.assertFalse(any("_x" in e.name for e in ocr_ref.available_engines()))
        self.assertEqual([e.name for e in ocr_ref.make_engines()], list(ocr_ref.ENGINE_ORDER))

    def test_max_dimension_comes_from_the_class(self):
        e = ocr_ref.WinOcrEngine()
        e._mods = {"OcrEngine": type("OcrEngine", (), {"max_image_dimension": 10000})}
        self.assertEqual(e._max_dim(), 10000)
        e._mods = {"OcrEngine": type("OcrEngine", (), {})}
        self.assertEqual(e._max_dim(), ocr_ref.WINOCR_MAX_DIM)


@NEEDS_NP
class BuildReferenceTests(unittest.TestCase):
    TEXTS = ["기본 키는 NULL을 허용하지 않는다", "SELECT id FROM students"]

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.img = self.tmp / "s.png"
        Image.new("RGB", (1280, 720), "white").save(self.img)

    def test_variant_reference(self):
        ref = ocr_ref.build_reference(self.img, [FakeEngine("fake", self.TEXTS), FakeEngine("fake_x2", self.TEXTS)])
        self.assertEqual((ref["version"], ref["agreement"]), (3, "variants"))
        q = ref["quality"]
        self.assertEqual((q["n_high"], q["n_mid"], q["n_variant"]), (0, 0, 9))
        self.assertTrue(q["usable"])
        words = [w for l in ref["lines"] for w in l["words"]]
        self.assertTrue(all(w["agreed_by"] == "variant" and w["weight"] == "variant" for w in words))
        self.assertEqual([w["box"] for w in words], [w["box"] for l in result("x", self.TEXTS)["lines"] for w in l["words"]])
        self.assertEqual(ref["layout"]["layout_version"], 3)
        self.assertTrue(ref["layout"]["text_masked"])

    def test_agreement_kinds_and_usable_weight(self):
        single = ocr_ref.build_reference(self.img, [FakeEngine("fake", self.TEXTS)])
        self.assertEqual(single["agreement"], "single")
        self.assertEqual(single["quality"]["n_variant"], 0)
        self.assertFalse(single["quality"]["usable"])  # 9 mid words weigh 4.5; as variant words they weigh 9
        cross = ocr_ref.build_reference(self.img, [FakeEngine("a", self.TEXTS), FakeEngine("b", self.TEXTS)])
        self.assertEqual(cross["agreement"], "cross_engine")
        self.assertEqual(cross["quality"]["n_high"], 9)
        named = ocr_ref.build_reference(self.img, [FakeEngine("a", self.TEXTS), FakeEngine("a2", self.TEXTS, recognizer="a")])
        self.assertEqual(named["agreement"], "variants")

    def test_text_reference(self):
        ref = ocr_ref.text_reference("기본 키는 NULL을\n허용하지 않는다", "x.pdf")
        self.assertEqual((ref["version"], ref["agreement"], ref["quality"]["n_variant"]), (ocr_ref.REF_VERSION, "single", 0))
        self.assertEqual(ocr_ref.REF_VERSION, 3)


@NEEDS_NP
class LoadOrBuildTests(unittest.TestCase):
    TEXTS = ["기본 키는 NULL을 허용하지 않는다"]

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.img = self.tmp / "slides" / "s.png"
        self.img.parent.mkdir()
        Image.new("RGB", (640, 360), "white").save(self.img)
        self.rp = self.tmp / "out" / "ref" / "s.json"
        self.res = self.tmp / "out" / "results" / "s.json"
        self.eng = FakeEngine("fake", self.TEXTS)

    def old_ref(self, **kw):
        ref = ocr_ref.build_reference(self.img, [FakeEngine("fake", self.TEXTS)])
        ref.update(version=2, **kw)
        self.rp.parent.mkdir(parents=True, exist_ok=True)
        self.rp.write_text(json.dumps(ref), encoding="utf-8")
        return ref

    def judged(self, judgments):
        self.res.parent.mkdir(parents=True, exist_ok=True)
        self.res.write_text(json.dumps({"page": 1, "judgments": judgments}), encoding="utf-8")

    def test_reuse_when_current(self):
        a = ocr_ref.load_or_build(self.img, self.rp, [self.eng])
        b = ocr_ref.load_or_build(self.img, self.rp, [self.eng])
        self.assertEqual(self.eng.calls, 1)
        self.assertEqual(a, b)

    def test_stale_ref_is_rebuilt(self):
        self.old_ref()
        self.judged([])  # a result without judgments does not protect the ref
        self.assertEqual(ocr_ref.load_or_build(self.img, self.rp, [self.eng])["version"], 3)
        self.assertEqual(self.eng.calls, 1)

    def test_judged_stale_ref_is_kept_unless_rebuild(self):
        self.old_ref()
        self.judged([{"id": "s__L001", "kind": "line", "verdict": "agy_ok"}])
        kept = ocr_ref.load_or_build(self.img, self.rp, [self.eng, FakeEngine("fake_x2", self.TEXTS)])
        self.assertEqual((kept["version"], self.eng.calls), (2, 0))
        self.assertEqual(json.loads(self.rp.read_text(encoding="utf-8"))["version"], 2)
        new = ocr_ref.load_or_build(self.img, self.rp, [self.eng], rebuild=True)
        self.assertEqual((new["version"], self.eng.calls), (3, 1))

    def test_judged_ref_of_another_image_is_rebuilt(self):
        self.old_ref(sha256="0" * 64)
        self.judged([{"id": "s__L001", "kind": "line", "verdict": "agy_ok"}])
        self.assertEqual(ocr_ref.load_or_build(self.img, self.rp, [self.eng])["version"], 3)

    def test_unreadable_ref_is_rebuilt(self):
        self.rp.parent.mkdir(parents=True)
        self.rp.write_bytes(b"\xff\xfe{not json")
        self.judged([{"id": "s__L001"}])
        self.assertEqual(ocr_ref.load_or_build(self.img, self.rp, [self.eng])["version"], 3)
        self.rp.write_text("[1, 2]", encoding="utf-8")
        self.assertEqual(ocr_ref.load_or_build(self.img, self.rp, [self.eng])["version"], 3)


# --------------------------------------------------------------------------- Windows OCR
@unittest.skipUnless(Image is not None and ocr_ref.np is not None and HAVE_FONT and winocr(), "needs Windows OCR")
class WinOcrVariantTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.img = synth.slide_main(cls.tmp / "slide_002.png")
        cls.base = ocr_ref.WinOcrEngine().run(cls.img)

    def common_boxes(self, other):
        a = {norm(w["text"]): w["box"] for l in self.base["lines"] for w in l["words"] if len(norm(w["text"])) >= 3}
        b = {norm(w["text"]): w["box"] for l in other["lines"] for w in l["words"] if len(norm(w["text"])) >= 3}
        return [(a[k], b[k]) for k in a.keys() & b.keys()]

    def test_upscaled_pass_reports_original_coordinates(self):
        up = ocr_ref.WinOcrEngine(scale=2).run(self.img)
        self.assertEqual((up["engine"], up["recognizer"]), ("winocr_x2", "winocr"))
        pairs = self.common_boxes(up)
        self.assertGreaterEqual(len(pairs), 3)
        for p, q in pairs:
            self.assertTrue(all(abs(x - y) <= 8 for x, y in zip(p, q)), (p, q))

    def test_size_cap_keeps_original_coordinates(self):
        e = ocr_ref.WinOcrEngine()
        self.assertGreaterEqual(e._max_dim(), ocr_ref.WINOCR_MAX_DIM)
        with mock.patch.object(ocr_ref.WinOcrEngine, "_max_dim", lambda self: 1000):
            small = e.run(self.img)  # 1280 px wide: read at 1000 px, boxes scaled back
        pairs = self.common_boxes(small)
        self.assertGreaterEqual(len(pairs), 3)
        for p, q in pairs:
            self.assertTrue(all(abs(x - y) <= 8 for x, y in zip(p, q)), (p, q))

    def test_variant_reference_on_one_engine(self):
        ref = ocr_ref.build_reference(self.img, ocr_ref.with_variants([ocr_ref.WinOcrEngine()], "1,2"))
        self.assertEqual(ref["agreement"], "variants")
        self.assertEqual([e["engine"] for e in ref["engines"]], ["winocr", "winocr_x2"])
        self.assertGreater(ref["quality"]["n_variant"], 0)
        self.assertEqual(ref["quality"]["n_high"], 0)
        self.assertTrue({w["weight"] for l in ref["lines"] for w in l["words"]} <= {"variant", "mid", "low"})
        self.assertTrue(ref["quality"]["usable"])


if __name__ == "__main__":
    unittest.main()

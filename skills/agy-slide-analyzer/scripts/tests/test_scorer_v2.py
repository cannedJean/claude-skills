"""Tests for the second-generation scorer checks (stdlib unittest; no OCR engine needed unless stated).

    python -m unittest discover -s scripts/tests -v        (from the skill folder)

Every check here sits behind a DEFAULT_THRESHOLDS switch; each test also shows what the switch turns off:
  * OCR look-alike folding (textnorm.fold / ocr_key / skel), with norm() and friends pinned unchanged
  * deck-template lines (template_keys / tkeys), header/footer-free content_chars
  * the closed thin-reference shortcut (kp's 'little text on both sides' string kept on accept)
  * layout v3 keys (graphics_ratio_nocode, text_masked, table_grid2 / ruled_table2) with fallback to the old keys
  * operator / number / code-line-order checks, copy consistency, latin_mid, glued digits, 'variant' weights
"""
from __future__ import annotations

import copy
import inspect
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))

import ocr_ref  # noqa: E402
import ocr_score  # noqa: E402
from ocr_score import (DEFAULT_THRESHOLDS, STRONG, copy_consistency, line_token_issues, load_thresholds, score,  # noqa: E402
                       template_keys, text_token_issues)
from textnorm import (PUNCT_RE, bigrams, digit_seqs, fold, fuzzy_in, latin_tokens, norm, ocr_key, overlap,  # noqa: E402
                      skel, units)

SIZE = (1280, 720)


def ref_of(lines, weight="high", engines=("winocr", "tesseract"), size=SIZE, layout=None, **extra):
    """A reference shaped like ocr_ref's: lines = [(text, box) | (text, box, weight)], one word per space-split token,
    one-character words 'low' as ocr_ref makes them."""
    out = []
    for i, ln in enumerate(lines):
        text, box = ln[0], ln[1]
        wt = ln[2] if len(ln) > 2 else weight
        words, x = [], box[0] if box else 0
        for w in text.split():
            words.append({"text": w, "conf": None, "box": [x, box[1], 12 * len(w), box[3]] if box else None,
                          "weight": wt if len(norm(w)) >= 2 else "low", "agreed": wt in STRONG})
            x += 12 * len(w) + 10
        out.append({"id": f"L{i + 1:03d}", "engine": engines[0], "text": text, "conf": None, "box": box, "words": words})
    n = sum(len(l["words"]) for l in out)
    ref = {"version": 3, "file": "x.png", "sha256": "0", "size": list(size) if size else None,
           "engines": [{"engine": e, "ok": True} for e in engines], "primary": engines[0], "lines": out,
           "text": {engines[0]: norm(" ".join(l[0] for l in lines))},
           "layout": dict({"available": True, "graphics_ratio": 0.01, "table_candidate": None, "ruled_table": False},
                          **(layout or {})),
           "quality": {"n_words": n, "n_high": n, "n_mid": 0, "mean_conf": None, "usable": True}}
    ref.update(extra)
    return ref


def th(**kw):
    t = json.loads(json.dumps(DEFAULT_THRESHOLDS))
    t.update(kw)
    return t


CODE_LINES = [("const isAdult = age > 30", [80, 200, 500, 28]),
              ("const even = i % 2 === 0", [80, 240, 500, 28]),
              ("return isAdult && even", [80, 280, 450, 28])]
FOOTER = ("EXAMPLE CODE ACADEMY", [40, 680, 300, 20])
REF = ref_of([("비교 연산자와 조건식", [60, 40, 400, 40]),
              ("나이가 30보다 크면 성인으로 판단한다", [60, 120, 600, 30]),
              *CODE_LINES, FOOTER, ("7", [1200, 680, 20, 20])])
ANALYSIS = {"slide_type": "code", "title": "비교 연산자와 조건식",
            "verbatim_text": [{"region": "title", "text": "비교 연산자와 조건식"},
                              {"region": "body", "text": "나이가 30보다 크면 성인으로 판단한다"},
                              {"region": "header/footer", "text": "EXAMPLE CODE ACADEMY"},
                              {"region": "page_number", "text": "7"}],
            "code_blocks": [{"language": "js", "code": "const isAdult = age > 30\nconst even = i % 2 === 0\nreturn isAdult && even"}]}


def with_code(code, a=ANALYSIS):
    a = copy.deepcopy(a)
    a["code_blocks"][0]["code"] = code
    return a


class TextNormTests(unittest.TestCase):
    """2b: folding lives in new helpers; norm() and the other shared helpers keep their behaviour exactly."""

    def test_norm_and_friends_unchanged(self):
        golden = {"age > 30": "age30", "age < 30": "age30", "x === 0": "x0", "SELECT id, name": "selectidname",
                  "v—show": "v—show", "EnabIing": "enabiing", "〈div〉": "div", "“quote”": "“quote”", "id十十": "id十十"}
        for s, n in golden.items():
            self.assertEqual(norm(s), n, s)
        self.assertEqual(units("i % 2 === 0"), [])
        self.assertEqual(units("기본 키는 NULL을"), ["기본", "키는", "null을"])
        self.assertEqual(digit_seqs("1,024개 He110 2024년"), ["1024", "110", "2024"])
        self.assertEqual(latin_tokens("v-show He110 id"), ["show", "he110"])
        self.assertEqual(bigrams("abc"), {"ab", "bc"})
        self.assertEqual(overlap("abc", {"ab"}), 0.5)
        self.assertTrue(fuzzy_in("abcd", "xxabcdxx", bigrams("xxabcdxx"), 0.75))
        self.assertEqual(PUNCT_RE.pattern, (r"[\s\u00b7\u2022\u318d\u2026\-_()\[\]{}'\"`.,:;!?/\|<>=+*~^%$#@&\u203b\u2192\u2190\u2191\u2193"
                         r"\u300c\u300d\u300e\u300f\u3010\u3011\u3008\u3009\u300a\u300b\uff08\uff09\uff0c\uff0e]+"))
        self.assertNotIn("\u2014", PUNCT_RE.pattern)  # dashes are folded by fold(), never stripped by norm()

    def test_fold(self):
        self.assertEqual(fold("〈!-- v—if --〉"), "<!-- v-if -->")
        self.assertEqual(fold("id十十 5Øpx “a” ‘b’"), "id++ 50px \"a\" 'b'")
        self.assertEqual(fold("Netwo「k 「참고」"), "Network 「참고」")  # 「 becomes r next to Latin letters only
        for c in map(chr, (*range(0x2010, 0x2016), 0x2212)):  # every dash and the minus sign
            self.assertEqual(fold(f"a{c}b"), "a-b")

    def test_ocr_key_and_skel(self):
        for ocr, true in (("EnabIing", "Enabling"), ("Pe0Ple", "People"), ("v—show", "v-show"), ("He110", "Hello"),
                          ("A1ice", "Alice"), ("Netwo「k", "Network")):
            self.assertEqual(ocr_key(ocr), ocr_key(true), ocr)
            self.assertEqual(skel(ocr), skel(true), ocr)
        self.assertEqual(skel("5tyle"), skel("style"))
        self.assertNotEqual(ocr_key("5tyle"), ocr_key("style"))  # 5 -> s only in the loosest key
        self.assertEqual(ocr_key("age > 30"), "age3o")  # operators are still stripped: the line check uses tokens


class ApiTests(unittest.TestCase):
    def test_signatures_and_defaults(self):
        self.assertEqual(list(inspect.signature(score).parameters),
                         ["analysis", "reference", "thresholds", "validator_errors", "tkeys", "boilerplate"])
        self.assertEqual(STRONG, {"high", "variant"})
        d = DEFAULT_THRESHOLDS
        self.assertEqual(d["graphics_trigger_nocode"], 0.015)
        self.assertEqual(d["weights"]["variant"], 1.0)
        for k in ("template_exclusion", "close_thin_shortcut", "line_check", "latin_mid", "copy_check", "fold_match",
                  "glued_digit_skip", "table_v2", "hf_boilerplate"):
            self.assertIs(d[k], True, k)

    def test_outputs_are_additive_and_json(self):
        sc = score(ANALYSIS, copy.deepcopy(REF))
        self.assertEqual(sc["decision"], "accept", sc["reasons"])
        for k in ("metrics", "decision", "reasons", "missing_lines", "unsupported_units", "extra_digits", "triggers",
                  "needs", "missing_latin", "unsupported_fields", "line_issues", "copy_issues"):
            self.assertIn(k, sc)
        for k in ("recall", "unsupported_ratio", "digit_recall", "latin_recall", "length_ratio", "corpus_chars",
                  "content_chars", "template_lines"):
            self.assertIn(k, sc["metrics"])
        json.dumps(sc, ensure_ascii=False)

    def test_old_calibration_file_keeps_variant_weight(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "calibration.json"
            p.write_text(json.dumps({"thresholds": {"weights": {"high": 1.0, "mid": 0.4, "low": 0.0}, "recall_accept": 0.85}}),
                         encoding="utf-8")
            t = load_thresholds(p)
        self.assertEqual(t["weights"], {"high": 1.0, "variant": 1.0, "mid": 0.4, "low": 0.0})
        self.assertIs(t["line_check"], True)
        # a hand-made thresholds dict without the new keys behaves like the defaults
        old = {k: v for k, v in DEFAULT_THRESHOLDS.items() if k in ocr_score.DEFAULT_THRESHOLDS and not isinstance(v, bool)
               and k != "graphics_trigger_nocode"}
        old["weights"] = {"high": 1.0, "mid": 0.5, "low": 0.0}
        self.assertEqual(score(ANALYSIS, copy.deepcopy(REF), old)["decision"], "accept")


class FoldMatchTests(unittest.TestCase):
    REF = ref_of([("다같이 배워요 EnabIing C0ders", [60, 40, 500, 30]),
                  ("v—show 는 display 속성만 전환한다", [60, 100, 600, 30]),
                  ("Netwo「k 탭에서 요청을 확인한다", [60, 160, 600, 30])], weight="mid", engines=("winocr",))
    A = {"title": "v-show", "verbatim_text": [{"region": "body", "text": "다같이 배워요 Enabling Coders"},
                                              {"region": "body", "text": "v-show는 display 속성만 전환한다"},
                                              {"region": "body", "text": "Network 탭에서 요청을 확인한다"}]}

    def test_lookalikes_count_as_found_and_supported(self):
        on = score(self.A, copy.deepcopy(self.REF))
        off = score(self.A, copy.deepcopy(self.REF), th(fold_match=False))
        self.assertEqual(on["metrics"]["recall"], 1.0)
        self.assertLess(off["metrics"]["recall"], 0.9)
        self.assertNotIn("network", [u["text"] for u in on["unsupported_units"]])
        self.assertIn("network", [u["text"] for u in off["unsupported_units"]])
        self.assertEqual(on["decision"], "accept", on["reasons"])


class TemplateTests(unittest.TestCase):
    def deck(self, n, footer_box=FOOTER[1]):
        return {f"s{i}": ref_of([(f"슬라이드 {i} 의 본문 내용 문장", [60, 120, 600, 30]), (FOOTER[0], footer_box)])
                for i in range(n)}

    def test_template_keys(self):
        keys = template_keys(self.deck(5))
        self.assertEqual(len(keys), 1)
        self.assertEqual(next(iter(keys))[0], skel(FOOTER[0]))
        self.assertEqual(template_keys(list(self.deck(5).values())), keys)  # list or dict
        self.assertEqual(template_keys(self.deck(4)), set())
        self.assertEqual(len(template_keys(self.deck(4), min_slides=4)), 1)
        moved = self.deck(3)
        moved.update({f"m{i}": r for i, r in enumerate(self.deck(3, [40, 40, 300, 20]).values())})
        self.assertEqual(template_keys(moved), set())  # same text, different place: not a template line
        pdf = ocr_ref.text_reference("EXAMPLE CODE ACADEMY", "x.pdf")
        self.assertEqual(template_keys([pdf] * 6), set())  # no boxes, no keys

    def test_template_lines_do_not_count(self):
        a = copy.deepcopy(ANALYSIS)
        a["verbatim_text"] = [t for t in a["verbatim_text"] if t["region"] != "header/footer"]  # agy left the logo out
        tk = template_keys(dict(self.deck(6), this=REF))
        sc = score(a, copy.deepcopy(REF), tkeys=tk)
        self.assertEqual(sc["metrics"]["template_lines"], 1)
        self.assertNotIn("L006", [m["id"] for m in sc["missing_lines"]])
        self.assertEqual(sc["decision"], "accept", sc["reasons"])
        sc_json_keys = score(a, copy.deepcopy(REF), tkeys=[list(k) for k in tk])  # keys round-tripped through JSON
        self.assertEqual(sc_json_keys["metrics"]["template_lines"], 1)
        off = score(a, copy.deepcopy(REF), th(template_exclusion=False), tkeys=tk)
        self.assertEqual(off["metrics"]["template_lines"], 0)
        self.assertIn("L006", [m["id"] for m in off["missing_lines"]])

    def test_content_chars_and_boilerplate(self):
        sc = score(ANALYSIS, copy.deepcopy(REF))
        expect = len(norm("비교 연산자와 조건식 나이가 30보다 크면 성인으로 판단한다 " + ANALYSIS["code_blocks"][0]["code"]))
        self.assertEqual(sc["metrics"]["content_chars"], expect)  # title once, no footer, no page number
        self.assertGreater(sc["metrics"]["corpus_chars"], expect)
        # a header/footer tag keeps the item out of content_chars, not out of the hallucination check: the OCR saw
        # this footer (supported), but text it never saw is unsupported whatever region the analyst gave it
        self.assertEqual(sc["metrics"]["unsupported_ratio"], 0.0)
        a = copy.deepcopy(ANALYSIS)
        a["verbatim_text"].append({"region": "header/footer", "text": "다같이 배워요 Enabling Coders Confidential"})
        sa = score(a, copy.deepcopy(REF))
        self.assertGreater(sa["metrics"]["unsupported_ratio"], 0.15)
        self.assertEqual(sa["metrics"]["content_chars"], sc["metrics"]["content_chars"])


class ThinTests(unittest.TestCase):
    """2d: a thin reference accepts only what it supports; kp's THIN_REASONS string stays on that accept."""
    KP_THIN = {"little text on both sides", "no_reference_little_text"}
    THIN = ref_of([("v-if 디렉티브", [300, 300, 400, 60])], weight="mid", engines=("winocr",))

    def test_thin_supported_accepts_with_kp_reason(self):
        a = {"title": "v-if 디렉티브", "verbatim_text": [{"region": "title", "text": "v-if 디렉티브"},
                                                     {"region": "header/footer", "text": "EXAMPLE CODE ACADEMY"}]}
        ref = ref_of([("v-if 디렉티브", [300, 300, 400, 60], "mid"), FOOTER], weight="mid", engines=("winocr",))
        deck = {f"s{i}": ref_of([FOOTER]) for i in range(5)}
        sc = score(a, ref, tkeys=template_keys(dict(deck, this=ref)))  # the footer: a template line the OCR saw
        self.assertEqual(sc["metrics"]["template_lines"], 1)
        self.assertIn("ref_thin", sc["triggers"])
        self.assertEqual(sc["decision"], "accept")
        self.assertEqual(sc["reasons"], ["little text on both sides", "thin reference, content supported"])
        self.assertTrue(set(sc["reasons"]) & self.KP_THIN)

    def test_thin_rich_but_supported_accepts(self):
        ref = ref_of([("v-if 디렉티브는 조건부로 렌더링한다", [100, 300, 800, 50])], weight="mid", engines=("winocr",))
        a = {"title": "v-if 디렉티브는 조건부로 렌더링한다",
             "verbatim_text": [{"region": "body", "text": "v-if 디렉티브는 조건부로 렌더링한다 v-if 디렉티브는 조건부로 렌더링한다"}]}
        sc = score(a, ref)
        self.assertIn("agy_rich_ref_empty", sc["triggers"])
        self.assertEqual(sc["decision"], "accept", sc["reasons"])
        old = score(a, ref, th(close_thin_shortcut=False))
        self.assertEqual((old["decision"], old["needs"]), ("escalate", ["thumbnail"]))

    def test_thin_unsupported_escalates(self):
        a = {"title": "참고", "verbatim_text": [{"region": "title", "text": "참고"}]}
        sc = score(a, copy.deepcopy(self.THIN))
        self.assertEqual(sc["decision"], "escalate")
        self.assertIn("thumbnail", sc["needs"])
        self.assertFalse(set(sc["reasons"]) & self.KP_THIN)
        self.assertEqual(sc["reasons"][0], "thin reference, content not confirmed")
        old = score(a, copy.deepcopy(self.THIN), th(close_thin_shortcut=False))
        self.assertEqual((old["decision"], old["reasons"]), ("accept", ["little text on both sides"]))

    def test_only_template_text_in_reference(self):
        deck = {f"s{i}": ref_of([FOOTER]) for i in range(5)}
        ref = ref_of([FOOTER])
        a = {"title": "참고", "verbatim_text": [{"region": "title", "text": "참고"}]}
        sc = score(a, ref, tkeys=template_keys(deck))
        self.assertIsNone(sc["metrics"]["recall"])
        self.assertEqual((sc["decision"], sc["needs"]), ("escalate", ["thumbnail"]))

    def test_thin_with_struct_or_line_issue_escalates(self):
        ref = copy.deepcopy(self.THIN)
        ref["layout"].update(graphics_ratio_nocode=0.05, text_masked=True)
        a = {"title": "v-if 디렉티브", "verbatim_text": [{"region": "title", "text": "v-if 디렉티브"}]}
        sc = score(a, ref)
        self.assertEqual(sc["decision"], "escalate")
        self.assertIn("graphics_undeclared", sc["reasons"])
        ref = ref_of([("if (a > 10)", [300, 300, 300, 40])], weight="mid", engines=("winocr",))
        sc = score({"title": "if (a < 10)"}, ref)
        self.assertEqual(sc["decision"], "escalate")
        self.assertIn("operator_or_digit_mismatch", sc["reasons"])
        self.assertEqual(sc["needs"], ["crops", "thumbnail"])


class LayoutTests(unittest.TestCase):
    """2a / 2e scorer side: layout v3 keys win when present, the old keys are the fallback."""

    def trig(self, layout, a=None, **kw):
        ref = copy.deepcopy(REF)
        ref["layout"].update(layout)
        return score(a or ANALYSIS, ref, th(**kw))["triggers"]

    def test_graphics_nocode(self):
        self.assertNotIn("graphics_undeclared", self.trig({"graphics_ratio": 0.08, "graphics_ratio_nocode": 0.004}))
        self.assertIn("graphics_undeclared", self.trig({"graphics_ratio": 0.08, "graphics_ratio_nocode": 0.02}))
        self.assertIn("graphics_undeclared", self.trig({"graphics_ratio": 0.08}))  # an old reference: old rule
        self.assertNotIn("graphics_undeclared", self.trig({"graphics_ratio": 0.02}))
        self.assertIn("graphics_undeclared", self.trig({"graphics_ratio": 0.08, "graphics_ratio_nocode": 0.004},
                                                       graphics_trigger_nocode=None))
        declared = dict(ANALYSIS, diagrams=[{"kind": "flow", "elements": ["a"], "relations": []}])
        self.assertNotIn("graphics_undeclared", self.trig({"graphics_ratio_nocode": 0.05}, declared))

    def test_unmasked_layout_has_no_graphics_say(self):
        lay = {"graphics_ratio": 0.2, "graphics_ratio_nocode": 0.2, "text_masked": False}
        self.assertNotIn("graphics_undeclared", self.trig(lay))
        self.assertIn("graphics_undeclared", self.trig(dict(lay, text_masked=True)))

    def test_table_v2_keys(self):
        old_grid = {"table_candidate": {"rows": 4, "cols": 3}}
        prose = dict(ANALYSIS, code_blocks=[])
        self.assertIn("table_candidate_undeclared", self.trig(old_grid, prose))
        self.assertNotIn("table_candidate_undeclared", self.trig(dict(old_grid, table_grid2=None, ruled_table2=False), prose))
        self.assertIn("table_candidate_undeclared", self.trig({"table_grid2": {"rows": 3, "cols": 3}, "ruled_table2": False}, prose))
        self.assertIn("table_candidate_undeclared", self.trig({"ruled_table": False, "ruled_table2": True}, prose))
        self.assertNotIn("table_candidate_undeclared", self.trig({"ruled_table": True, "ruled_table2": False}, prose))
        self.assertIn("table_candidate_undeclared", self.trig(dict(old_grid, table_grid2=None), prose, table_v2=False))
        # a multi-line code block still explains a grid away
        self.assertNotIn("table_candidate_undeclared", self.trig({"table_grid2": {"rows": 3, "cols": 3}}))


class LineCheckTests(unittest.TestCase):
    """Item 3: operators, standalone numbers and code-line order on anchored lines."""

    def test_operator_swap(self):
        a = with_code("const isAdult = age < 30\nconst even = i % 2 === 0\nreturn isAdult && even")
        sc = score(a, copy.deepcopy(REF))
        self.assertEqual(sc["decision"], "escalate")
        self.assertIn("operator_or_digit_mismatch", sc["reasons"])
        self.assertIn("crops", sc["needs"])
        li = sc["line_issues"][0]
        self.assertEqual((li["id"], li["field"], li["analysis_line"]), ("L003", "code_blocks[0].code", "const isAdult = age < 30"))
        self.assertEqual(li["issues"], [{"kind": "op", "read": [">"], "analysis": ["<"]}])
        self.assertIn(("L003", "operator_or_digit"), [(m["id"], m.get("reason")) for m in sc["missing_lines"]])
        self.assertEqual(score(a, copy.deepcopy(REF), th(line_check=False))["decision"], "accept")  # norm() is blind to it

    def test_single_digit_and_compound_operators(self):
        sc = score(with_code("const isAdult = age > 30\nconst even = i % 3 === 0\nreturn isAdult && even"), copy.deepcopy(REF))
        self.assertEqual(sc["line_issues"][0]["issues"], [{"kind": "digit", "read": ["2"], "analysis": ["3"]}])
        sc = score(with_code("const isAdult = age > 30\nconst even = i % 2 !== 0\nreturn isAdult || even"), copy.deepcopy(REF))
        self.assertEqual(sorted(i["issues"][0]["read"][0] for i in sc["line_issues"]), ["&&", "==="])

    def test_lone_minus_plus_bang_never_decide(self):
        ref = ref_of([("total = a - b", [80, 200, 400, 28]), ("- 항목 하나를 더 적는다", [60, 260, 400, 28]),
                      ("값을 계산해서 돌려준다 ok", [60, 320, 400, 28])])
        a = {"title": "", "verbatim_text": [{"region": "body", "text": "• 항목 하나를 더 적는다"},
                                            {"region": "body", "text": "값을 계산해서 돌려준다 ok"}],
             "code_blocks": [{"code": "total = a + b"}]}
        self.assertEqual(line_token_issues(a, ref), [])

    def test_line_swap_inside_one_panel(self):
        swapped = with_code("const even = i % 2 === 0\nconst isAdult = age > 30\nreturn isAdult && even")
        for layout in ({"code_boxes": [[60, 190, 700, 130]]}, {}):
            with self.subTest(panels=bool(layout)):
                ref = copy.deepcopy(REF)
                ref["layout"].update(layout)
                sc = score(swapped, ref)
                self.assertIn("line_order_mismatch", sc["reasons"])
                order = [i for li in sc["line_issues"] for i in li["issues"] if i["kind"] == "order"]
                self.assertEqual(order, [{"kind": "order", "read": ["L003", "L004"], "analysis": [1, 0]}])
                self.assertEqual({m["id"] for m in sc["missing_lines"] if m.get("reason") == "line_order"}, {"L003", "L004"})
        # lines in two different panels are not compared
        ref = copy.deepcopy(REF)
        ref["layout"]["code_boxes"] = [[60, 190, 700, 40], [60, 232, 700, 40]]
        self.assertNotIn("line_order_mismatch", score(swapped, ref)["reasons"])

    def test_feedback_names_the_lines(self):
        a = with_code("const isAdult = age < 30\nconst even = i % 2 === 0\nreturn isAdult && even")
        a["verbatim_text"].append({"region": "body", "text": "return isAdult || even"})
        fb = ocr_score.feedback_text(score(a, copy.deepcopy(REF)))
        self.assertIn("「const isAdult = age > 30」", fb)
        self.assertIn("「const isAdult = age < 30」", fb)
        self.assertIn("`verbatim_text[4]`", fb)

    def test_text_reference_has_no_crops(self):
        ref = ocr_ref.text_reference("\n".join(t for t, _ in [("비교 연산자와 조건식", 0), ("나이가 30보다 크면 성인으로 판단한다", 0)])
                                     + "\n" + "\n".join(t for t, _ in CODE_LINES) + "\n" + FOOTER[0] + "\n7", "x.pdf")
        a = with_code("const isAdult = age < 30\nconst even = i % 2 === 0\nreturn isAdult && even")
        sc = score(a, ref)
        # a text layer is exact: the analyst re-reads (retry), no crop or thumbnail can overrule it
        self.assertEqual((sc["decision"], sc["reasons"], sc["needs"]), ("retry", ["operator_or_digit_mismatch"], []))
        self.assertIn("「const isAdult = age > 30」", ocr_score.feedback_text(sc))
        good = with_code("\n".join(t for t, _ in CODE_LINES))
        self.assertEqual(score(good, ref)["decision"], "accept")

    def test_text_token_issues(self):
        a = with_code("const isAdult = age < 30\nconst even = i % 2 === 0")
        iss = text_token_issues("const isAdult = age > 30", a)
        self.assertEqual(iss[0]["issues"], [{"kind": "op", "read": [">"], "analysis": ["<"]}])
        self.assertIsNone(iss[0]["id"])
        self.assertEqual(text_token_issues(["const isAdult = age < 30", "const even = i % 2 === 0"], a), [])
        self.assertEqual(text_token_issues("[?]", a), [])
        self.assertEqual(text_token_issues("완전히 다른 내용의 문장입니다", a), [])
        # the right copy is anchored first; the wrong copy in another field is still compared
        b = copy.deepcopy(ANALYSIS)
        b["verbatim_text"].insert(1, {"region": "body", "text": "const isAdult = age > 30"})
        b["code_blocks"][0]["code"] = "const isAdult = age < 30"
        iss = text_token_issues("const isAdult = age > 30", b)
        self.assertEqual([(i["field"], i["issues"][0]["analysis"]) for i in iss], [("code_blocks[0].code", ["<"])])


class CopyConsistencyTests(unittest.TestCase):
    def test_clean_copies_pass(self):
        a = copy.deepcopy(ANALYSIS)
        a["verbatim_text"].append({"region": "body", "text": a["code_blocks"][0]["code"]})
        self.assertEqual(copy_consistency(a), [])
        twins = {"title": "목차", "verbatim_text": [{"region": "body", "text": "1.1 반응형 데이터 선언"},
                                                  {"region": "body", "text": "2.1 반응형 데이터 선언"}]}
        self.assertEqual(copy_consistency(twins), [])  # two verbatim items may be near-twins

    def test_one_copy_mutation_is_caught(self):
        a = copy.deepcopy(ANALYSIS)
        a["verbatim_text"].append({"region": "body", "text": "const isAdult = age < 30"})
        ci = copy_consistency(a)
        self.assertEqual(len(ci), 1)
        self.assertEqual(ci[0]["fields"], ["verbatim_text[4]", "code_blocks[0].code"])
        self.assertEqual(ci[0]["tokens"], [["<", "30"], [">", "30"]])
        sc = score(a, copy.deepcopy(REF), th(line_check=False))
        self.assertEqual(sc["decision"], "escalate")
        self.assertIn("copy_mismatch", sc["reasons"])
        self.assertIn(("L003", "copy_mismatch"), [(m["id"], m.get("reason")) for m in sc["missing_lines"]])
        self.assertEqual(score(a, copy.deepcopy(REF), th(line_check=False, copy_check=False))["decision"], "accept")
        t = copy.deepcopy(ANALYSIS)
        t["title"] = "비교 연산자와 조건식 3"
        t["verbatim_text"][0]["text"] = "비교 연산자와 조건식 4"
        self.assertEqual(len(copy_consistency(t)), 1)  # title vs verbatim title

    def test_no_reference(self):
        a = {"title": "count == 10 이면 종료", "verbatim_text": [{"region": "title", "text": "count == 12 이면 종료"}]}
        self.assertEqual(score(a, None)["reasons"], ["no_reference", "copy_mismatch"])
        self.assertEqual(score(a, None, th(copy_check=False))["reasons"], ["no_reference_little_text"])


class SingleEngineTests(unittest.TestCase):
    """latin_mid, glued digits and the 'variant' weight (items 3 and 4, scorer side)."""
    LINES = [("computed 속성은 캐시된다", [60, 100, 500, 30]), ("watch 함수는 변화를 감시한다", [60, 150, 500, 30]),
             ("반응형 데이터는 ref 로 선언한다", [60, 200, 500, 30]), ("템플릿에서 메서드를 호출할 수 있다", [60, 250, 500, 30])]
    A = {"title": "", "verbatim_text": [{"region": "body", "text": "computed 속성은 캐시된다"},
                                        {"region": "body", "text": "observe 함수는 변화를 감시한다"},
                                        {"region": "body", "text": "반응형 데이터는 ref로 선언한다"},
                                        {"region": "body", "text": "템플릿에서 메서드를 호출할 수 있다"}]}

    def test_latin_mid(self):
        ref = ref_of(self.LINES, weight="mid", engines=("winocr",))
        sc = score(self.A, copy.deepcopy(ref))
        self.assertEqual(sc["decision"], "escalate")
        self.assertEqual(sc["missing_latin"], ["watch"])
        self.assertTrue(any(r.startswith("latin_recall") for r in sc["reasons"]))
        self.assertIn(("L002", "digit_or_identifier"), [(m["id"], m.get("reason")) for m in sc["missing_lines"]])
        self.assertEqual(score(self.A, copy.deepcopy(ref), th(latin_mid=False))["decision"], "accept")
        variants = ref_of(self.LINES, weight="mid", engines=("winocr", "winocr_x2"))
        self.assertEqual(score(self.A, copy.deepcopy(variants))["decision"], "escalate")  # variants of one recognizer
        cross = ref_of(self.LINES, weight="mid", engines=("winocr", "tesseract"))
        self.assertEqual(score(self.A, copy.deepcopy(cross))["decision"], "accept")  # mid = the engines disagreed
        self.assertEqual(score(self.A, dict(copy.deepcopy(ref), agreement="cross_engine"))["decision"], "accept")

    def test_mid_digits_are_not_checked(self):
        # Windows OCR reads 'log(' as '109(': a set-based digit check on mid words would fail a correct analysis
        ref = ref_of(self.LINES + [("console.109(`hi`) 로 출력한다", [60, 300, 500, 30])], weight="mid", engines=("winocr",))
        a = copy.deepcopy(self.A)
        a["verbatim_text"][1]["text"] = "watch 함수는 변화를 감시한다"
        a["verbatim_text"].append({"region": "body", "text": "console.log(`hi`)로 출력한다"})
        sc = score(a, ref)
        self.assertIsNone(sc["metrics"]["digit_recall"])
        self.assertEqual(sc["decision"], "accept", sc["reasons"])

    def test_glued_digits_and_variant_weight(self):
        lines = [(t, b, "variant") for t, b in self.LINES] + [("console.log('He110') 를 출력한다", [60, 300, 500, 30], "variant")]
        ref = ref_of(lines, engines=("winocr", "winocr_x2"), agreement="variants")
        a = copy.deepcopy(self.A)
        a["verbatim_text"][1]["text"] = "watch 함수는 변화를 감시한다"
        a["verbatim_text"].append({"region": "body", "text": "console.log('Hello')를 출력한다"})
        sc = score(a, copy.deepcopy(ref))
        self.assertEqual(sc["decision"], "accept", sc["reasons"])
        self.assertIsNone(sc["metrics"]["digit_recall"])
        self.assertEqual(sc["metrics"]["ref_weight"], 17.0)  # variant words weigh 1.0
        off = score(a, copy.deepcopy(ref), th(glued_digit_skip=False))
        self.assertEqual(off["metrics"]["digit_recall"], 0.0)
        self.assertEqual(off["decision"], "escalate")

    def test_variant_words_get_digit_checks(self):
        lines = [(t, b, "variant") for t, b in self.LINES] + [("2024년 기준 1,024개 행", [60, 300, 500, 30], "variant")]
        ref = ref_of(lines, engines=("winocr", "winocr_x2"), agreement="variants")
        a = copy.deepcopy(self.A)
        a["verbatim_text"][1]["text"] = "watch 함수는 변화를 감시한다"
        a["verbatim_text"].append({"region": "body", "text": "2023년 기준 1,042개 행"})
        sc = score(a, ref)
        self.assertEqual(sc["decision"], "escalate")
        self.assertEqual(sc["metrics"]["digit_recall"], 0.0)
        self.assertIn("2023", sc["extra_digits"])
        self.assertIn("L005", [m["id"] for m in sc["missing_lines"]])
        mid = ref_of([(t, b, "mid") for t, b, _ in lines], engines=("winocr",))
        self.assertIsNone(score(a, mid)["metrics"]["digit_recall"])  # unconfirmed words: the line check's job


@unittest.skipUnless(ocr_ref.Image is not None and ocr_ref.available_engines(), "needs Pillow and an OCR engine")
class EngineLineCheckTests(unittest.TestCase):
    """The operator check on a real OCR read of a rendered code line."""

    def test_operator_swap_through_ocr(self):
        import synth
        from PIL import Image, ImageDraw
        tmp = Path(tempfile.mkdtemp())
        img = Image.new("RGB", (1280, 720), "white")
        d = ImageDraw.Draw(img)
        d.text((60, 40), "비교 연산자와 조건식", font=synth.font(synth.BOLD_CANDIDATES, 48), fill="black")
        mono = synth.font(synth.MONO_CANDIDATES, 40)
        for i, line in enumerate(("if (age > 30) {", "  total = count % 7;", "}")):
            d.text((80, 200 + i * 70), line, font=mono, fill="black")
        img.save(tmp / "code.png")
        ref = ocr_ref.build_reference(tmp / "code.png", ocr_ref.available_engines()[:1])
        if not any(">" in l["text"] and "30" in l["text"] for l in ref["lines"]):
            self.skipTest(f"the engine did not read the operator: {[l['text'] for l in ref['lines']]}")
        good = {"title": "비교 연산자와 조건식", "code_blocks": [{"code": "if (age > 30) {\n  total = count % 7;\n}"}]}
        bad = {"title": "비교 연산자와 조건식", "code_blocks": [{"code": "if (age < 30) {\n  total = count % 7;\n}"}]}
        self.assertEqual(line_token_issues(good, ref), [])
        self.assertIn("operator_or_digit_mismatch", score(bad, ref)["reasons"])


if __name__ == "__main__":
    unittest.main()

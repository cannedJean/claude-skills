"""Tests for kp_compare: comparators per region kind, majority vote, value rebuild and line patching."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import kp_common  # noqa: E402,F401
import kp_compare as kc  # noqa: E402


def code(c, lang="sql"):
    return {"language": lang, "code": c}


def table(rows, hr=1):
    return {"rows": rows, "header_rows": hr}


def diagram(rels, nodes=None):
    return kc.agy_value("diagram", {"elements": nodes or [], "relations": rels})


def formula(p, latex=None):
    return {"plain": p, "latex": latex}


def line(t):
    return {"text": t}


def edge(a, b, label=None, directed=True):
    return {"from": a, "to": b, "label": label, "directed": directed}


def joined(res):
    return " | ".join(res["diffs"])


# every input used below; the property tests run over all of them
SQL = "SELECT * FROM t WHERE age > 30"
PY4 = "if x > 0:\n    y = 1\n    z = 2\nprint(y)"
PY2 = "if x > 0:\n  y = 1\n  z = 2\nprint(y)"
PY_OUT = "if x > 0:\n    y = 1\nz = 2\nprint(y)"
SAMPLES = [
    ("code", code(SQL)), ("code", code("SELECT * FROM t WHERE age < 30")), ("code", code("select * from t")),
    ("code", code("SELECT *\n   FROM t\n  WHERE id = 1")), ("code", code(PY4, "python")),
    ("code", code(PY2, "python")), ("code", code(PY_OUT, "python")), ("code", code("x = ‘A’", None)),
    ("code", code("a − b", None)), ("code", code("WHERE city = 'Seoul'")), ("code", code("", None)),
    ("table", table([["이름", "나이"], ["Kim", "30"], ["Lee", "25"]])),
    ("table", table([["이름", "나이"], ["Kim", "[?]"]])),
    ("table", table([["A", "B", ""], ["1", "2", ""], ["", "", ""]])),
    ("table", table([["a", "b"], ["c"]], 0)),
    ("table", table([], 0)),
    ("diagram", diagram(["학생 -> 수강 : 1:N", "과목 -> 수강 : 1:N"], ["학생", "수강", "과목"])),
    ("diagram", diagram(["A -- B", "B <-> C"])),
    ("diagram", diagram(["A -> B", "A and B are related"])),
    ("formula", formula("y = wx + b")), ("formula", formula("x²")), ("formula", formula("α·β")),
    ("formula", formula("(a)/(b)", r"\frac{a}{b}")),
    ("line", line("릴레이션 R은 3NF를 만족한다.")), ("line", line("[?]를 만족한다")), ("line", line("학번 (PK)")),
    ("line", line("")),
    # regression inputs (adversarial review)
    ("code", code("for i in range(10):  # 10번 반복\n    s += i\nprint(s)", None)),
    ("code", code("x = foo(a,\n    b)", "python3")),
    ("table", table([["σ", "O(n²)"], ["삽입ㆍ삭제", "학번 (PK)"]])),
    ("diagram", diagram(["학생 -> 수강 (1:N)", "A -(수강)-> B", "입력 -> 처리 -> 출력"], ["학생", "수강"])),
    ("formula", formula("ŷ = wᵀx + b")), ("formula", formula("hₜ = tanh(W hₜ₋₁)")),
    ("formula", formula(r"\frac{1}{n}\sum_{i=1}^{n} x_i")),
    ("line", line("y = σ(Wx + b)")), ("line", line("▶ 학습률은 10⁻³으로 설정")), ("line", line("삽입ㆍ삭제ㆍ갱신 이상")),
]


def has_wild(value) -> bool:
    return kc.WILD in repr(value)


# --------------------------------------------------------------------------- canon and tokens
class CanonTokens(unittest.TestCase):
    def test_canon(self):
        self.assertEqual(kc.canon(None), "")
        self.assertEqual(kc.canon("\n\n  a  \r\n b\t \r\n\n"), "  a\n b")
        self.assertEqual(kc.canon("‘x’ “y” ″z″ a–b—c−d"), "'x' \"y\" \"z\" a-b-c-d")
        self.assertEqual(kc.canon("ＡＢ１"), "AB1")

    def test_token_classes(self):
        # ',' separates arguments in code: (3,4) == (3, 4); lines keep 1,024 as one number
        self.assertEqual(kc.code_tokens("3NF x2 id 1,024 3.14"), ["3NF", "x2", "id", "1", ",", "024", "3.14"])
        self.assertEqual(kc.line_tokens("1,024개 3,4"), ["1024", "개", "3", "4"])
        self.assertEqual(kc.code_tokens("a<=b<>c->d::e"), ["a", "<=", "b", "<>", "c", "->", "d", "::", "e"])
        self.assertEqual(kc.code_tokens("x <<= 1 >>= 2"), ["x", "<<=", "1", ">>=", "2"])
        self.assertEqual(kc.code_tokens("릴레이션R은"), ["릴레이션", "R", "은"])
        self.assertEqual(kc.code_tokens("16px"), ["16px"])

    def test_string_literals(self):
        self.assertEqual(kc.code_tokens(r"s = 'it\'s' + " + '"a\\"b"'), ["s", "=", r"'it\'s'", "+", '"a\\"b"'])
        self.assertEqual(kc.code_tokens("print('a b')"), ["print", "(", "'a b'", ")"])
        self.assertEqual(kc.code_tokens("'a\nb'"), ["'", "a", "b", "'"])  # literals never span lines
        self.assertEqual(kc.code_tokens("don't"), ["don", "'", "t"])

    def test_indentation(self):
        self.assertEqual(kc.code_tokens("if a:\n    b\n", "python"), ["<IND:0>", "if", "a", ":", "<IND:1>", "b"])
        self.assertEqual(kc.code_tokens("if a:\n\tb", None), ["<IND:0>", "if", "a", ":", "<IND:1>", "b"])
        self.assertNotIn("<IND:0>", kc.code_tokens("SELECT a\n  FROM t", "sql"))
        self.assertNotIn("<IND:0>", kc.code_tokens("SELECT a\n  FROM t", None))
        self.assertEqual(kc.code_tokens("k:\n  v: 1", "YAML")[0], "<IND:0>")

    def test_spans_point_into_canon(self):
        s = "  SELECT ‘a’,\r\n  b >= 2  \n"
        text = kc.canon(s)
        for tok, a, b in kc.code_tokens_spans(s):
            self.assertEqual(text[a:b], tok)

    def test_line_tokens(self):
        self.assertEqual(kc.line_tokens("릴레이션 R은 3NF를 만족한다."), ["릴레이션", "r", "은", "3nf", "를", "만족한다"])
        self.assertEqual(kc.line_tokens("「정규화」 (Normal Form) … 'x'"), ["정규화", "normal", "form", "x"])
        self.assertEqual(kc.line_tokens("a < b + c"), ["a", "<", "b", "+", "c"])
        self.assertEqual(kc.line_tokens("[?]를 만족"), [kc.WILD, "를", "만족"])


# --------------------------------------------------------------------------- code
class Code(unittest.TestCase):
    def test_operator_flip(self):
        res = kc.compare("code", code(SQL), code("SELECT * FROM t WHERE age < 30"))
        self.assertEqual(res["verdict"], "mismatch")
        self.assertIn("'>' -> '<'", joined(res))
        self.assertIn("line 1", joined(res))

    def test_ge_vs_gt(self):
        self.assertEqual(kc.compare("code", code("a >= 3"), code("a > 3"))["verdict"], "mismatch")

    def test_keyword_case_is_minor(self):
        res = kc.compare("code", code("select * from t"), code("SELECT * FROM t"))
        self.assertEqual(res["verdict"], "minor")
        self.assertTrue(res["diffs"])

    def test_whitespace_layout(self):
        res = kc.compare("code", code("SELECT *\n   FROM t\n\n  WHERE id = 1  "), code("SELECT * FROM t WHERE id = 1"))
        self.assertEqual(res["verdict"], "agree")

    def test_python_reindent(self):
        self.assertEqual(kc.compare("code", code(PY4, "python"), code(PY2, "python"))["verdict"], "agree")
        self.assertEqual(kc.compare("code", code(PY4, None), code(PY2, None))["verdict"], "agree")

    def test_python_line_moved_out_of_block(self):
        res = kc.compare("code", code(PY4, "python"), code(PY_OUT, "python"))
        self.assertEqual(res["verdict"], "mismatch")
        self.assertIn("line 3", joined(res))

    def test_language_missing_on_one_side(self):
        self.assertEqual(kc.compare("code", code("x = 1", "python"), code("x = 1", None))["verdict"], "agree")
        self.assertEqual(kc.compare("code", code("foo(a,\n    b)", "python"), code("foo(a,\n    b)", "sql"))["verdict"],
                         "agree")
        self.assertEqual(kc.compare("code", code(PY4, None), code(PY_OUT, "python"))["verdict"], "mismatch")

    def test_quotes_and_dashes(self):
        self.assertEqual(kc.compare("code", code("x = ‘A’"), code("x = 'A'"))["verdict"], "agree")
        self.assertEqual(kc.compare("code", code("a - b"), code("a − b"))["verdict"], "agree")

    def test_string_literal_case_is_mismatch(self):
        res = kc.compare("code", code("WHERE city = 'Seoul'"), code("WHERE city = 'seoul'"))
        self.assertEqual(res["verdict"], "mismatch")

    def test_missing_and_extra(self):
        res = kc.compare("code", code("WHERE a = 1 AND b = 2"), code("WHERE a = 1 b = 2"))
        self.assertIn("extra 'AND'", joined(res))
        res = kc.compare("code", code("WHERE a = 1"), code("WHERE a = 1 OR b = 2"))
        self.assertIn("missing 'OR b = 2'", joined(res))

    def test_diff_line_numbers_and_cap(self):
        res = kc.compare("code", code("SELECT a\nFROM t\nWHERE x > 1"), code("SELECT a\nFROM t\nWHERE x < 1"))
        self.assertEqual(res["diffs"], ["line 3: '>' -> '<'"])
        a = " ".join(f"a{i} + 1" for i in range(30))
        b = " ".join(f"a{i} - 1" for i in range(30))
        res = kc.compare("code", code(a), code(b))
        self.assertEqual(len(res["diffs"]), 10)
        self.assertTrue(res["diffs"][-1].startswith("..."))

    def test_units(self):
        u = kc.units("code", code("SELECT 1"))
        self.assertEqual(u, {"code": (("SELECT", "1"), "SELECT 1")})


# --------------------------------------------------------------------------- table
class Table(unittest.TestCase):
    T = [["이름", "나이"], ["Kim", "30"], ["Lee", "25"]]

    def test_swapped_cells(self):
        res = kc.compare("table", table(self.T), table([["이름", "나이"], ["30", "Kim"], ["Lee", "25"]]))
        self.assertEqual(res["verdict"], "mismatch")
        self.assertIn("(1,0): 'Kim' vs '30'", res["diffs"])
        self.assertIn("(1,1): '30' vs 'Kim'", res["diffs"])
        self.assertEqual(res["mismatch_cells"], [[1, 0], [1, 1]])

    def test_shape_and_header_rows(self):
        res = kc.compare("table", table(self.T), table([r + ["x"] for r in self.T]))
        self.assertEqual((res["verdict"], res["diffs"]), ("mismatch", ["shape 3x2 vs 3x3"]))
        res = kc.compare("table", table(self.T, 1), table(self.T, 0))
        self.assertEqual((res["verdict"], res["diffs"]), ("mismatch", ["header_rows 1 vs 0"]))

    def test_uncertain_cell(self):
        res = kc.compare("table", table(self.T), table([["이름", "나이"], ["Kim", "[?]"], ["Lee", "25"]]))
        self.assertEqual(res["verdict"], "uncertain")
        self.assertEqual(res["uncertain_cells"], [[1, 1]])
        res = kc.compare("table", table(self.T), table([["이름", "나이"], ["Kim", "[?]"], ["Lee", "52"]]))
        self.assertEqual(res["verdict"], "mismatch")

    def test_trailing_empty_rows_and_columns(self):
        padded = [r + ["", " "] for r in self.T] + [["", "", "", ""], [None, "", "", ""]]
        self.assertEqual(kc.compare("table", table(self.T), table(padded))["verdict"], "agree")

    def test_ragged_rows(self):
        self.assertEqual(kc.compare("table", table([["a", "b"], ["c"]]), table([["a", "b"], ["c", ""]]))["verdict"],
                         "agree")

    def test_case_and_space(self):
        res = kc.compare("table", table([["Name", "AGE"], ["kim  lee", 30]]), table([["name", "age"], ["Kim Lee", "30"]]))
        self.assertEqual(res["verdict"], "agree")

    def test_cell_key(self):
        self.assertEqual(kc.cell_key("  Hello\n  World "), "hello world")
        self.assertEqual(kc.cell_key(None), "")
        self.assertEqual(kc.cell_key(3), "3")

    def test_agy_value_and_units(self):
        v = kc.agy_value("table", {"caption": "c", "headers": ["A", "B"], "rows": [["1", "2"]]})
        self.assertEqual(v, {"rows": [["A", "B"], ["1", "2"]], "header_rows": 1})
        self.assertEqual(kc.agy_value("table", {"headers": [], "rows": [["1"]]}), {"rows": [["1"]], "header_rows": 0})
        u = kc.units("table", v)
        self.assertEqual(u["shape"], ((2, 2, 1), None))
        self.assertEqual(u["r1c0"], ("1", "1"))
        self.assertEqual(list(u), ["shape", "r0c0", "r0c1", "r1c0", "r1c1"])


# --------------------------------------------------------------------------- diagram
class Diagram(unittest.TestCase):
    def test_parse_relation_arrows(self):
        for arrow in ("<->", "↔", "<=>", "<-->"):
            self.assertEqual(kc.parse_relation(f"A {arrow} B"), edge("A", "B", directed=False), arrow)
        for arrow in ("<--", "<-", "←"):
            self.assertEqual(kc.parse_relation(f"A {arrow} B"), edge("B", "A"), arrow)
        for arrow in ("-->", "->", "=>", "→", "⟶"):
            self.assertEqual(kc.parse_relation(f"A {arrow} B"), edge("A", "B"), arrow)
        for arrow in ("--", "—", "–"):
            self.assertEqual(kc.parse_relation(f"A {arrow} B"), edge("A", "B", directed=False), arrow)

    def test_parse_relation_labels(self):
        self.assertEqual(kc.parse_relation("학생 -> 수강 : 1:N"), edge("학생", "수강", "1:N"))
        self.assertEqual(kc.parse_relation("학생 -> 수강 : 1:N 신청"), edge("학생", "수강", "1:N 신청"))
        self.assertEqual(kc.parse_relation("A -> B:1:N"), edge("A", "B", "1:N"))
        self.assertEqual(kc.parse_relation("A -> N:M"), edge("A", "N:M"))
        self.assertEqual(kc.parse_relation("A->B"), edge("A", "B"))

    def test_parse_relation_failures(self):
        for s in ("A and B", "-> B", "A ->", "", None, 3):
            self.assertIsNone(kc.parse_relation(s), s)

    def test_cardinality(self):
        self.assertEqual(kc.cardinality("1:N"), ("1:N",))
        self.assertEqual(kc.cardinality("1..*"), ("1..*",))
        self.assertEqual(kc.cardinality("0..n 신청"), ("0..N",))
        self.assertEqual(kc.cardinality("1 : m"), ("1:N",))  # spaces around ':' and M == N (both mean many)
        self.assertEqual(kc.cardinality("N:M"), ("N:N",))
        self.assertEqual(kc.cardinality(1), ("1",))
        self.assertEqual(kc.cardinality("has many"), ())
        self.assertEqual(kc.cardinality(None), ())

    def test_match_nodes(self):
        m = kc.match_nodes(["학생", "과목", "수강"], ["학생(Student)", "과목", "강의실"])
        self.assertEqual(m, {"학생(Student)": "학생", "과목": "과목", "강의실": "강의실"})
        self.assertEqual(kc.match_nodes(["Student"], ["student", "STUDENT"]), {"student": "Student", "STUDENT": "STUDENT"})
        self.assertEqual(kc.match_nodes(["학생"], ["학"]), {"학": "학"})

    def test_edge_key(self):
        self.assertEqual(kc.edge_key(edge("A", "B")), "A->B")
        self.assertEqual(kc.edge_key(edge("B", "A", directed=False)), "A--B")
        self.assertEqual(kc.edge_key(edge("x", "B"), {"x": "A"}), "A->B")

    def test_reversed_edge(self):
        res = kc.compare("diagram", diagram(["A -> B"]), {"nodes": ["A", "B"], "edges": [edge("B", "A")]})
        self.assertEqual(res["verdict"], "mismatch")
        self.assertEqual(res["diffs"], ["reversed: A->B vs B->A"])

    def test_node_names_matched(self):
        agy = diagram(["학생 -> 수강 : 1:N"], ["학생", "수강"])
        read = {"nodes": ["학생(Student)", "수강"], "edges": [edge("학생(Student)", "수강", "1:N")]}
        res = kc.compare("diagram", agy, read)
        self.assertEqual(res["verdict"], "agree")
        self.assertEqual(res["node_map"]["학생(Student)"], "학생")

    def test_cardinality_flip(self):
        res = kc.compare("diagram", diagram(["학생 -> 수강 : 1:N"]), {"nodes": [], "edges": [edge("학생", "수강", "N:1")]})
        self.assertEqual(res["verdict"], "mismatch")
        self.assertEqual(res["diffs"], ["label 학생->수강: 1:N vs N:1"])

    def test_label_text_is_mismatch(self):
        # 신청 vs 수강 (or FK vs PK) is a different relationship, not a minor difference
        res = kc.compare("diagram", diagram(["A -> B : 1:N 신청"]), {"nodes": [], "edges": [edge("A", "B", "1:N 수강")]})
        self.assertEqual((res["verdict"], res["diffs"]), ("mismatch", ["label A->B: '1:N 신청' vs '1:N 수강'"]))
        res = kc.compare("diagram", diagram(["A -> B : 1:N 신청"]), {"nodes": [], "edges": [edge("A", "B", "1 : N  신청")]})
        self.assertEqual(res["verdict"], "agree")

    def test_undirected_order(self):
        res = kc.compare("diagram", diagram(["A -- B"]), {"nodes": [], "edges": [edge("B", "A", directed=False)]})
        self.assertEqual(res["verdict"], "agree")
        res = kc.compare("diagram", diagram(["A -- B"]), {"nodes": [], "edges": [edge("A", "B")]})
        self.assertEqual((res["verdict"], res["diffs"]), ("mismatch", ["direction: A--B vs A->B"]))

    def test_missing_and_extra_edges(self):
        res = kc.compare("diagram", diagram(["A -> B", "B -> C"]),
                         {"nodes": [], "edges": [edge("A", "B"), edge("C", "D")]})
        self.assertEqual(res["verdict"], "mismatch")
        self.assertEqual(res["diffs"], ["extra edge B->C", "missing edge C->D"])

    def test_unparsed_is_uncertain(self):
        agy = diagram(["A -> B", "A and B are related"])
        self.assertEqual(agy["unparsed"], ["A and B are related"])
        res = kc.compare("diagram", agy, {"nodes": [], "edges": [edge("A", "B")]})
        self.assertEqual(res["verdict"], "uncertain")
        self.assertEqual(res["unparsed"], ["A and B are related"])
        res = kc.compare("diagram", agy, {"nodes": [], "edges": [edge("B", "A")]})
        self.assertEqual(res["verdict"], "mismatch")

    def test_units_use_node_map(self):
        read = {"nodes": [], "edges": [edge("학생(Student)", "수강", "1:N")]}
        u = kc.units("diagram", read, {"학생(Student)": "학생"})
        self.assertEqual(list(u), ["학생->수강"])
        self.assertEqual(u["학생->수강"][0], (("1:N",), ""))  # (cardinality, label text without it)
        self.assertEqual(u["학생->수강"][1]["from"], "학생")

    def test_agy_value(self):
        v = kc.agy_value("diagram", {"elements": ["A", "B"], "relations": ["A <- B : 1:N", "??"]})
        self.assertEqual(v, {"nodes": ["A", "B"], "edges": [edge("B", "A", "1:N")], "unparsed": ["??"]})


# --------------------------------------------------------------------------- formula
class Formula(unittest.TestCase):
    def test_sign_flip(self):
        res = kc.compare("formula", formula("y = wx + b"), formula("y = wx - b"))
        self.assertEqual(res["verdict"], "mismatch")
        self.assertIn("'+' -> '-'", res["diffs"])

    def test_superscripts_and_subscripts(self):
        self.assertEqual(kc.math_tokens("x²"), kc.math_tokens("x^2"))
        self.assertEqual(kc.math_tokens("x¹⁰"), ["x", "^", "10"])
        self.assertEqual(kc.math_tokens("x²y"), ["x", "^", "2", "y"])
        self.assertEqual(kc.math_tokens("x₁ + x_1"), ["x", "_", "1", "+", "x", "_", "1"])
        self.assertEqual(kc.compare("formula", formula("x² + 1"), formula("x^2+1"))["verdict"], "agree")

    def test_greek_and_operators(self):
        self.assertEqual(kc.math_tokens("α·β"), kc.math_tokens("alpha*beta"))
        self.assertEqual(kc.math_tokens("Σ x ≤ 3 × y ≠ z ÷ 2"),
                         ["sum", "x", "<=", "3", "*", "y", "!=", "z", "/", "2"])  # Σ is the sum operator, σ is sigma
        self.assertEqual(kc.math_tokens("a − b"), ["a", "-", "b"])

    def test_latex(self):
        self.assertEqual(kc.latex_tokens(r"\frac{a}{b}"), ["(", "a", ")", "/", "(", "b", ")"])
        self.assertTrue(kc.latex_consistent(formula("(a)/(b)", r"\frac{a}{b}")))
        self.assertTrue(kc.latex_consistent(formula("α·β", r"\alpha \cdot \beta")))
        self.assertTrue(kc.latex_consistent(formula("x² + x₁", r"x^{2} + x_1")))
        self.assertTrue(kc.latex_consistent(formula("sqrt(x^2 + y^2)", r"\sqrt{x^2 + y^2}")))
        self.assertTrue(kc.latex_consistent(formula("e^(-x)", r"e^{-x}")))
        self.assertTrue(kc.latex_consistent(formula("x⁻¹", r"x^{-1}")))
        self.assertTrue(kc.latex_consistent(formula("a ≤ b ≠ c", r"a \le b \neq c")))
        self.assertTrue(kc.latex_consistent(formula("(a)", r"\left( a \right)")))
        self.assertTrue(kc.latex_consistent(formula("Δx", r"\Delta x")))
        self.assertTrue(kc.latex_consistent(formula("sum x _ i", r"\sum x_i")))
        self.assertFalse(kc.latex_consistent(formula("(a)/(b)", r"\frac{b}{a}")))
        self.assertIsNone(kc.latex_consistent(formula("x")))

    def test_latex_consistency_reported(self):
        res = kc.compare("formula", formula("x²"), formula("x^2", "x^{3}"))
        self.assertEqual(res["verdict"], "agree")
        self.assertIs(res["latex_consistent"], False)

    def test_agy_value(self):
        self.assertEqual(kc.agy_value("formula", "E = mc²"), {"plain": "E = mc²", "latex": None})


# --------------------------------------------------------------------------- line
class Line(unittest.TestCase):
    AGY = "릴레이션 R은 3NF를 만족한다."

    def test_read_inside_longer_item(self):
        res = kc.compare("line", line(self.AGY), line("3NF를 만족한다"))
        self.assertEqual(res["verdict"], "agree")
        self.assertEqual(res["window"], [3, 6])

    def test_nf_mismatch(self):
        res = kc.compare("line", line(self.AGY), line("2NF를 만족한다"))
        self.assertEqual(res["verdict"], "mismatch")
        self.assertEqual(res["diffs"], ["'3nf' -> '2nf'"])

    def test_pk_present_vs_absent(self):
        res = kc.compare("line", line("학번"), line("학번 (PK)"))
        self.assertEqual(res["verdict"], "mismatch")
        self.assertIn("missing 'pk'", res["diffs"])
        res = kc.compare("line", line("학번 PK 이름"), line("학번 이름"))
        self.assertEqual(res["verdict"], "mismatch")
        self.assertIn("extra 'pk'", res["diffs"])
        self.assertEqual(kc.compare("line", line("학번 (PK)"), line("학번"))["verdict"], "agree")

    def test_wildcard(self):
        res = kc.compare("line", line(self.AGY), line("[?]를 만족한다"))
        self.assertEqual(res["verdict"], "uncertain")
        self.assertEqual(res["wildcards"], 1)
        res = kc.compare("line", line(self.AGY), line("[?]를 위반한다"))
        self.assertEqual(res["verdict"], "mismatch")

    def test_operators_matter(self):
        self.assertEqual(kc.compare("line", line("나이 > 30 인 학생"), line("나이 < 30"))["verdict"], "mismatch")
        self.assertEqual(kc.compare("line", line("“정규화”란?"), line('"정규화"란'))["verdict"], "agree")

    def test_empty_read(self):
        self.assertEqual(kc.compare("line", line(self.AGY), line("..."))["verdict"], "uncertain")

    def test_long_item(self):
        agy = " ".join(f"단어{i}" for i in range(300)) + " " + self.AGY
        res = kc.compare("line", line(agy), line("2NF를 만족한다"))
        self.assertEqual((res["verdict"], res["diffs"]), ("mismatch", ["'3nf' -> '2nf'"]))
        self.assertEqual(kc.compare("line", line(agy), line("R은 3NF를"))["verdict"], "agree")


# --------------------------------------------------------------------------- vote
def lu(t):
    return kc.units("line", line(t))


class Vote(unittest.TestCase):
    def test_corrected(self):
        v = kc.vote(lu("A"), lu("B"), lu("B"))
        self.assertEqual((v["status"], v["changed"], v["canonical"]), ("corrected", ["line"], {"line": "B"}))

    def test_upheld(self):
        v = kc.vote(lu("A"), lu("B"), lu("A"))
        self.assertEqual((v["status"], v["changed"], v["canonical"]), ("agy_upheld", [], {"line": "A"}))

    def test_disputed(self):
        v = kc.vote(lu("A"), lu("B"), lu("C"))
        self.assertEqual((v["status"], v["disputed"], v["canonical"]), ("disputed", ["line"], {"line": "A"}))

    def test_raw_preference(self):
        v = kc.vote(lu("A."), lu("a"), lu("B"))  # same cmp key, agy's raw wins
        self.assertEqual(v["canonical"], {"line": "A."})
        v = kc.vote(lu("X"), lu("b!"), lu("B"))
        self.assertEqual(v["canonical"], {"line": "b!"})

    def test_table_one_cell(self):
        agy = kc.units("table", table([["A", "B"], ["1", "2"]]))
        rd = kc.units("table", table([["A", "B"], ["1", "3"]]))
        v = kc.vote(agy, rd, dict(rd))
        self.assertEqual((v["status"], v["changed"]), ("corrected", ["r1c1"]))
        self.assertEqual(kc.to_value("table", v["canonical"], table([["A", "B"], ["1", "2"]])),
                         table([["A", "B"], ["1", "3"]]))

    def test_table_shape_majority(self):
        agy = kc.units("table", table([["A", "B"], ["1", "2"]]))
        r1 = kc.units("table", table([["A", "B", "C"], ["1", "2", "3"]]))
        r2 = kc.units("table", table([["A", "B", "C"], ["1", "2", "4"]]))
        v = kc.vote(agy, r1, r2)
        self.assertEqual(v["status"], "disputed")
        self.assertEqual(v["disputed"], ["r1c2"])  # two participants that disagree
        self.assertIn("shape", v["changed"])
        self.assertEqual(v["canonical"]["shape"], (2, 3, 1))

    def test_table_shape_disputed(self):
        a = kc.units("table", table([["A"]]))
        b = kc.units("table", table([["A", "B"]]))
        c = kc.units("table", table([["A"], ["B"]]))
        v = kc.vote(a, b, c)
        self.assertEqual(v["status"], "disputed")
        self.assertEqual(v["disputed"], ["shape", "r0c0", "r0c1", "r1c0"])
        self.assertEqual(v["canonical"], {"shape": (1, 1, 1), "r0c0": "A"})

    def test_edge_reversal_corrected(self):
        agy = diagram(["A -> B : 1:N"], ["A", "B"])
        rd = {"nodes": ["A", "B"], "edges": [edge("B", "A", "1:N")]}
        nm = kc.diagram_node_map(agy, rd)
        v = kc.vote(kc.units("diagram", agy), kc.units("diagram", rd, nm), kc.units("diagram", rd, nm))
        self.assertEqual((v["status"], v["changed"]), ("corrected", ["A->B", "B->A"]))
        self.assertEqual(kc.to_value("diagram", v["canonical"], agy),
                         {"nodes": ["A", "B"], "edges": [edge("B", "A", "1:N")], "unparsed": []})

    def test_absent_majority_removes_unit(self):
        agy = kc.units("diagram", diagram(["A -> B", "B -> C"]))
        rd = kc.units("diagram", diagram(["A -> B"]))
        v = kc.vote(agy, rd, rd)
        self.assertEqual((v["status"], v["changed"], list(v["canonical"])), ("corrected", ["B->C"], ["A->B"]))

    def test_one_read_only(self):
        v = kc.vote(lu("A"), lu("A"))
        self.assertEqual((v["status"], v["pending"]), ("agy_upheld", []))
        v = kc.vote(lu("A"), lu("B"), None)
        self.assertEqual((v["status"], v["pending"], v["canonical"]), ("needs_second_read", ["line"], {"line": "A"}))

    def test_one_read_table(self):
        agy = kc.units("table", table([["A", "B"], ["1", "2"]]))
        v = kc.vote(agy, kc.units("table", table([["A", "B"], ["1", "3"]])))
        self.assertEqual((v["status"], v["pending"]), ("needs_second_read", ["r1c1"]))
        v = kc.vote(agy, kc.units("table", table([["A", "B", "C"]])))
        self.assertEqual(v["pending"][0], "shape")
        self.assertEqual(len(v["pending"]), 1 + 4 + 1)  # shape, agy's 4 cells, the read's extra r0c2

    def test_json_roundtrip_keys(self):
        agy = kc.units("table", table([["A"]]))
        listed = {k: (list(c) if isinstance(c, tuple) else c, r) for k, (c, r) in agy.items()}
        self.assertEqual(kc.vote(agy, listed, listed)["status"], "agy_upheld")


# --------------------------------------------------------------------------- patch_line
class PatchLine(unittest.TestCase):
    AGY = "릴레이션 R은 3NF를 만족한다."

    def test_replace(self):
        self.assertEqual(kc.patch_line(self.AGY, "2NF를 만족한다"), "릴레이션 R은 2NF를 만족한다.")

    def test_insert_is_none(self):
        self.assertIsNone(kc.patch_line(self.AGY, "3NF를 반드시 만족한다"))
        self.assertIsNone(kc.patch_line(self.AGY, "릴레이션 3NF를 만족한다"))

    def test_agree_unchanged(self):
        agy = "릴레이션  R은\u3000３NF를 만족한다.  "
        self.assertEqual(kc.patch_line(agy, "3nf를 만족한다"), agy)

    def test_spacing_outside_window_kept(self):
        agy = "정의:  “R”은   3NF를 만족한다 !"
        self.assertEqual(kc.patch_line(agy, "R은 2NF를 만족한다"), "정의:  “R”은   2NF를 만족한다 !")

    def test_wildcard_never_replaces(self):
        self.assertEqual(kc.patch_line("a > b 이고 c", "[?] < b"), "a < b 이고 c")

    def test_non_canonical_agy_text(self):
        agy = "정의:\r\n  ＲＤＢ는 3NF를 만족한다  "
        self.assertEqual(kc.patch_line(agy, "2NF를"), "정의:\r\n  ＲＤＢ는 2NF를 만족한다  ")
        self.assertEqual(kc.patch_line("‘3NF’ 조건", "'2nf' 조건"), "‘2nf’ 조건")

    def test_deletion_is_none(self):
        self.assertIsNone(kc.patch_line("학번 PK 이름", "학번 이름"))
        self.assertIsNone(kc.patch_line("x ½ y", "1/3"))  # two read tokens would land in one agy character

    def test_empty(self):
        self.assertEqual(kc.patch_line("", ""), "")
        self.assertIsNone(kc.patch_line(self.AGY, "..."))


# --------------------------------------------------------------------------- properties
class Properties(unittest.TestCase):
    def test_self_compare(self):
        for kind, x in SAMPLES:
            want = "uncertain" if has_wild(x) or (kind == "diagram" and x.get("unparsed")) else "agree"
            self.assertEqual(kc.compare(kind, x, x)["verdict"], want, (kind, x))

    def test_unanimous_vote(self):
        for kind, x in SAMPLES:
            u = kc.units(kind, x)
            v = kc.vote(u, u, u)
            self.assertEqual((v["status"], v["changed"], v["disputed"]), ("agy_upheld", [], []), (kind, x))
            self.assertEqual(kc.vote(u, u)["status"], "agy_upheld")

    def test_rebuild(self):
        for kind, x in SAMPLES:
            back = kc.to_value(kind, kc.vote(*[kc.units(kind, x)] * 3)["canonical"], x)
            self.assertEqual(kc.units(kind, back), kc.units(kind, x), (kind, x))
            if kind == "table":
                rows, _, hr = kc._grid(x)
                x = {"rows": rows, "header_rows": hr}  # trimmed/padded form
            if kind != "diagram":
                self.assertEqual(back, x, (kind, x))

    def test_to_value_table_without_shape(self):
        self.assertEqual(kc.to_value("table", {"r0c0": "a", "r1c1": "b"}, table([["x"]], 1)),
                         table([["a", ""], ["", "b"]], 1))

    def test_to_value_diagram_order(self):
        tmpl = diagram(["A -> B", "C -> D", "A -> B : again"])
        canonical = {"Z->Y": edge("Z", "Y"), "C->D": edge("C", "D"), "A->B#2": edge("A", "B", "again"),
                     "E->F": edge("E", "F")}
        self.assertEqual([kc.edge_key(e) for e in kc.to_value("diagram", canonical, tmpl)["edges"]],
                         ["C->D", "A->B", "E->F", "Z->Y"])

    def test_unknown_kind(self):
        for fn in (lambda: kc.compare("img", {}, {}), lambda: kc.units("img", {}), lambda: kc.agy_value("img", {})):
            self.assertRaises(ValueError, fn)


# --------------------------------------------------------------------------- regressions (adversarial review)
def verdict(kind, a, b):
    return kc.compare(kind, a, b)["verdict"]


class RegressPatchLine(unittest.TestCase):
    AGY = "릴레이션 R은 3NF를 만족한다."

    def test_spacing_merge_never_duplicates_a_word(self):
        for agy, rd in (("기본 키는 NULL 값을 가질 수 없다.", "기본키는 NULL 값을 가질 수 없다"),
                        ("데이터 베이스 관리 시스템(DBMS)", "데이터베이스 관리 시스템(DBMS)")):
            self.assertEqual(kc.patch_line(agy, rd), agy)  # spacing only: agy upheld as written
        # spacing difference plus a real change: the change lands, agy's spacing stays
        self.assertEqual(kc.patch_line("기본 키는 NULL 값을 가질 수 있다.", "기본키는 NULL 값을 가질 수 없다"),
                         "기본 키는 NULL 값을 가질 수 없다.")

    def test_wrapped_or_unrelated_read_writes_nothing(self):
        self.assertEqual(kc.patch_line(self.AGY, "릴레이션 R은 3NF를 만족"), self.AGY)  # line wraps 만족 / 한다
        self.assertEqual(kc.patch_line(self.AGY, "한다."), self.AGY)  # the second visual line
        self.assertIsNone(kc.patch_line("학번은 학생 릴레이션의 기본키이다", "PK"))  # shares nothing with agy
        self.assertIsNone(kc.patch_line("데이터 학생 테이블", "교수 테이블"))  # edge word replaced: maybe an insertion

    def test_parallel_entries_are_ambiguous(self):
        self.assertIsNone(kc.patch_line("2NF: 부분 함수 종속 제거, 3NF: 이행 함수 종속 제거", "3NF: 부분 함수 종속 제거"))
        self.assertIsNone(kc.patch_line("A는 B를 만족한다. C는 D를 만족한다.", "A는 E를 만족한다"))

    def test_edge_insertion_is_not_a_replace(self):
        self.assertIsNone(kc.patch_line(self.AGY, "반드시 3NF를 만족한다"))
        self.assertIsNone(kc.patch_line("R은 3NF를 만족한다. 따라서 이상이 없다.", "3NF를 만족한다 반드시"))
        self.assertEqual(kc.compare("line", line(self.AGY), line("반드시 3NF를 만족한다"))["diffs"], ["missing '반드시'"])

    def test_decomposed_hangul(self):
        import unicodedata
        nfd = unicodedata.normalize("NFD", self.AGY)
        self.assertEqual(kc.patch_line(nfd, "3NF를 만족한다"), nfd)
        self.assertEqual(unicodedata.normalize("NFC", kc.patch_line(nfd, "2NF를 만족한다")), "릴레이션 R은 2NF를 만족한다.")

    def test_more_safe_patches(self):
        self.assertEqual(kc.patch_line(self.AGY, "릴레이션 R은 3NF를 만족했다"), "릴레이션 R은 3NF를 만족했다.")
        self.assertEqual(kc.patch_line("총 1,024개의 행", "총 1,025개의 행"), "총 1,025개의 행")
        self.assertEqual(kc.patch_line("학습률은 10⁻³으로 설정", "학습률은 10⁻⁴으로 설정"), "학습률은 10⁻⁴으로 설정")
        self.assertEqual(kc.patch_line("정규화 → 이상 현상 제거", "정규화 -> 이상 현상 제거"), "정규화 → 이상 현상 제거")
        self.assertIsNone(kc.patch_line("y = σ(Wx + b)", "y = σ(Wx) + b"))  # grouping cannot be patched token by token
        self.assertIsNone(kc.patch_line("비정규화를 수행한다", "정규화를 수행한다"))


class RegressLine(unittest.TestCase):
    def test_wrapped_word_is_minor(self):
        res = kc.compare("line", line("릴레이션 R은 3NF를 만족한다."), line("릴레이션 R은 3NF를 만족"))
        self.assertEqual((res["verdict"], res["partial_edges"]), ("minor", ["end"]))
        self.assertEqual(verdict("line", line("릴레이션 R은 3NF를 만족한다."), line("한다.")), "minor")
        # but not after a one-syllable prefix (비/불/무/미 + word is another word), nor inside a Latin token
        self.assertEqual(verdict("line", line("비정규화를 수행한다"), line("정규화를 수행한다")), "mismatch")
        self.assertEqual(verdict("line", line("불가능하다"), line("가능하다")), "mismatch")
        self.assertEqual(verdict("line", line("12NF 조건"), line("2NF 조건")), "mismatch")

    def test_scripts_survive(self):
        self.assertEqual(verdict("line", line("학습률은 10⁻³으로 설정"), line("학습률은 10-3으로 설정")), "mismatch")
        self.assertEqual(verdict("line", line("학습률은 10⁻³으로 설정"), line("학습률은 10^-3으로 설정")), "agree")
        self.assertEqual(verdict("line", line("데이터는 10³개"), line("데이터는 103개")), "mismatch")

    def test_greek_case(self):
        self.assertEqual(verdict("line", line("Φ(x)는 누적분포함수"), line("φ(x)는 누적분포함수")), "mismatch")
        self.assertEqual(verdict("line", line("∑ x"), line("Σ x")), "agree")

    def test_brackets(self):
        self.assertEqual(kc.compare("line", line("y = σ(Wx + b)"), line("y = σ(Wx) + b"))["diffs"],
                         ["brackets: 'y=σ(wx+b)' vs 'y=σ(wx)+b'"])
        self.assertEqual(verdict("line", line("(a + b) * c"), line("a + (b * c)")), "mismatch")
        self.assertEqual(verdict("line", line("출력은 y = σ(Wx + b)"), line("y = σ(Wx + b)")), "agree")
        self.assertEqual(verdict("line", line("학번(PK), 이름"), line("학번 PK 이름")), "agree")  # prose: style only

    def test_korean_spacing(self):
        for a, b in (("기본 키는 NULL 값을 가질 수 없다.", "기본키는 NULL 값을 가질 수 없다"),
                     ("데이터베이스 관리 시스템", "데이터베이스관리시스템"), ("‘정규화’란 무엇인가", "정규화란 무엇인가")):
            self.assertEqual(verdict("line", line(a), line(b)), "agree", (a, b))

    def test_wildcard_inside_or_over_a_word(self):
        for a, b in (("릴레이션 R은 3NF를 만족한다.", "릴레이션 R은 [?] 만족한다"), ("릴레이션 R은 3NF를 만족한다.", "R은 [?] 만족한다"),
                     ("정규화는 이상 현상을 제거한다", "정[?]화는 이상 현상을 제거한다"), ("3NF를 만족한다", "3[?]F를 만족한다"),
                     ("학번은 PK가 된다", "학번은 [?] 된다")):
            self.assertEqual(verdict("line", line(a), line(b)), "uncertain", (a, b))
        self.assertEqual(verdict("line", line("릴레이션 R은 3NF를 만족한다."), line("릴레이션 R은 [?] 위반한다")), "mismatch")

    def test_bullets_arrows_and_dots(self):
        for a, b in (("기본키는 유일하다", "▶ 기본키는 유일하다"), ("기본키는 유일하다", "- 기본키는 유일하다"),
                     ("정규화 → 이상 현상 제거", "정규화 -> 이상 현상 제거"), ("나이 ≥ 20인 학생", "나이 >= 20인 학생"),
                     ("삽입ㆍ삭제ㆍ갱신 이상이 발생한다", "삽입·삭제·갱신 이상이 발생한다"), ("1, 2, 3단계", "1,2,3단계")):
            self.assertEqual(verdict("line", line(a), line(b)), "agree", (a, b))
        self.assertEqual(verdict("line", line("a ▶ b"), line("a b")), "mismatch")  # a bullet only at the line start
        self.assertEqual(kc.line_tokens("삽입ㆍ삭제"), ["삽입", "삭제"])


class RegressTable(unittest.TestCase):
    def test_scripts_and_greek(self):
        for a, b in (("σ", "Σ"), ("O(n²)", "O(n2)"), ("x²", "x₂")):
            res = kc.compare("table", table([[a]]), table([[b]]))
            self.assertEqual(res["verdict"], "mismatch", (a, b))
        self.assertEqual(kc.compare("table", table([["x²"]]), table([["x₂"]]))["diffs"], ["(0,0): 'x²' vs 'x₂'"])
        self.assertEqual(verdict("table", table([["x²"]]), table([["x^2"]])), "agree")

    def test_spacing_and_symbols(self):
        for a, b in (("학번(PK)", "학번 (PK)"), ("기본 키", "기본키"), ("PK, FK", "PK,FK"), ("삽입ㆍ삭제", "삽입·삭제"),
                     ("≥ 20", ">=20")):
            self.assertEqual(verdict("table", table([[a]]), table([[b]])), "agree", (a, b))
        for a, b in (("Kim Lee", "KimLee"), ("1 2", "12")):
            self.assertEqual(verdict("table", table([[a]]), table([[b]])), "mismatch", (a, b))


class RegressCode(unittest.TestCase):
    MOVED = ("if x > 0:\n    y = 1\nz = 2", "if x > 0:\n    y = 1\n    z = 2")

    def test_python_labels_and_comments(self):
        for lang in ("python3", "Python 3", "ipython", "py3"):
            self.assertEqual(verdict("code", code(self.MOVED[0], lang), code(self.MOVED[1], lang)), "mismatch", lang)
        a = "for i in range(10):  # 10번 반복\n    s += i\nprint(s)"
        b = "for i in range(10):  # 10번 반복\n    s += i\n    print(s)"
        self.assertEqual(verdict("code", code(a, None), code(b, None)), "mismatch")

    def test_case_only_minor_for_case_insensitive_languages(self):
        for a, b in (("X = np.array(data)\nprint(X.shape)", "x = np.array(data)\nprint(x.shape)"),
                     ("model.train(verbose=True)", "model.train(verbose=true)"),
                     ("if x is None:\n    return True", "if x is none:\n    return true")):
            self.assertEqual(verdict("code", code(a, "python"), code(b, "python")), "mismatch", a)
            self.assertEqual(verdict("code", code(a, None), code(b, None)), "mismatch", a)
        self.assertEqual(verdict("code", code("select * from t", "mysql"), code("SELECT * FROM t", "mysql")), "minor")
        self.assertEqual(verdict("code", code("select * from t", None), code("SELECT * FROM t", None)), "minor")

    def test_comma_spacing(self):
        for a, b, lang in (("x = np.zeros((3,4))", "x = np.zeros((3, 4))", "python"),
                           ("price DECIMAL(10,2)", "price DECIMAL(10, 2)", "sql"),
                           ("INSERT INTO t VALUES (1,2,3)", "INSERT INTO t VALUES (1, 2, 3)", "sql")):
            self.assertEqual(verdict("code", code(a, lang), code(b, lang)), "agree", a)

    def test_units_do_not_depend_on_the_language_label(self):
        src = "import numpy as np\nx = np.array([1, 2])\nprint(x.shape)"
        a = kc.units("code", code(src, "python"))
        self.assertEqual(kc.vote(a, kc.units("code", code(src.replace("[1, 2]", "[1, 3]"), "python")),
                                 kc.units("code", code(src, None)))["status"], "agy_upheld")
        self.assertEqual(kc.vote(a, kc.units("code", code(src, "python3")))["status"], "agy_upheld")
        cont = "x = foo(a,\n        b)"
        self.assertEqual(kc.units("code", code(cont, "python")), kc.units("code", code(cont, None)))
        blk = "if x:\n    y = 1\nz = 2"
        self.assertEqual(kc.vote(kc.units("code", code(blk, "python")), kc.units("code", code(blk, "python3")))["status"],
                         "agy_upheld")


class RegressFormula(unittest.TestCase):
    def test_greek_case_matters(self):
        for a, b in (("y = σ(Wx + b)", "y = Σ(Wx + b)"), ("Φ(x)", "φ(x)"), ("QΛQ⁻¹", "QλQ⁻¹"), ("Δw", "δw"), ("Π_i", "π_i")):
            self.assertEqual(verdict("formula", formula(a), formula(b)), "mismatch", (a, b))
        self.assertEqual(verdict("formula", formula("L = Σ (y - ŷ)²"), formula("L = ∑ (y - ŷ)²")), "agree")
        self.assertTrue(kc.latex_consistent(formula("Σ x_i", r"\sum x_i")))
        self.assertTrue(kc.latex_consistent(formula("σ(x) + Δ", r"\sigma(x) + \Delta")))
        v = kc.vote(*(kc.units("formula", formula(p)) for p in ("Σx", "σx", "σx")))
        self.assertEqual((v["status"], v["canonical"]), ("corrected", {"formula": "σx"}))

    def test_letter_scripts(self):
        self.assertEqual(verdict("formula", formula("ŷ = wᵀx + b"), formula("ŷ = w^T x + b")), "agree")
        self.assertEqual(verdict("formula", formula("ŷ = wᵀx + b"), formula("ŷ = wTx + b")), "mismatch")
        self.assertEqual(verdict("formula", formula("(XᵀX)⁻¹Xᵀy"), formula("(XTX)⁻¹XTy")), "mismatch")
        self.assertEqual(verdict("formula", formula("hₜ = tanh(W hₜ₋₁ + U xₜ)"),
                                 formula("h_t = tanh(W h_(t-1) + U x_t)")), "agree")
        self.assertEqual(verdict("formula", formula("xᵢ"), formula("ξ")), "mismatch")
        self.assertEqual(kc.math_tokens("xⁿ"), ["x", "^", "n"])

    def test_signed_scripts(self):
        for a, b in (("x⁻¹", "x^-1"), ("W⁻¹", "W^(-1)"), ("10⁻³", "10^-3"), ("x^{2}", "x²"), ("e^(-x)", "e^-x")):
            self.assertEqual(verdict("formula", formula(a), formula(b)), "agree", (a, b))
        self.assertEqual(verdict("formula", formula("xⁿ⁺¹"), formula("x^n+1")), "mismatch")
        self.assertEqual(verdict("formula", formula("xⁿ⁺¹"), formula("x^(n+1)")), "agree")

    def test_latex_written_as_plain(self):
        self.assertEqual(verdict("formula", formula(r"\frac{1}{n}\sum_{i=1}^{n} x_i"), formula("(1)/(n) ∑_(i=1)^n x_i")),
                         "agree")

    def test_primes_are_not_strings(self):
        self.assertEqual(verdict("formula", formula("f'(x) = g'(x)"), formula("f'(x)=g'(x)")), "agree")
        self.assertEqual(verdict("formula", formula("f′(x) + g′(x)"), formula("f′(x)+g′(x)")), "agree")
        self.assertEqual(verdict("formula", formula("f″(x)"), formula("f''(x)")), "agree")
        self.assertEqual(kc.compare("formula", formula("f'(x) + g'(x)"), formula("f'(x) - g'(x)"))["diffs"], ["'+' -> '-'"])


class RegressDiagram(unittest.TestCase):
    def test_near_miss_entity_names(self):
        agy = diagram(["주문 -> 주문상태 : 1:N"], ["주문", "주문상태"])
        rd = {"nodes": ["주문", "주문상세"], "edges": [edge("주문", "주문상세", "1:N")]}
        res = kc.compare("diagram", agy, rd)
        self.assertEqual(res["verdict"], "mismatch")
        self.assertEqual(res["diffs"][0], "node '주문상태' vs '주문상세'")
        nm = kc.diagram_node_map(agy, rd)
        v = kc.vote(kc.units("diagram", agy), kc.units("diagram", rd, nm), kc.units("diagram", rd, nm))
        self.assertEqual(v["status"], "corrected")
        self.assertEqual([e["to"] for e in kc.to_value("diagram", v["canonical"], agy)["edges"]], ["주문상세"])
        for x, y in (("고객주소", "고객주문"), ("Encoder", "Decoder"), ("부서", "부서장")):
            self.assertEqual(verdict("diagram", {"nodes": [x], "edges": [edge(x, "B")]},
                                     {"nodes": [y], "edges": [edge(y, "B")]}), "mismatch", (x, y))

    def test_match_nodes_by_alias(self):
        self.assertEqual(kc.match_nodes(["사원", "사원번호"], ["사원번호(PK)", "사원(Employee)"]),
                         {"사원번호(PK)": "사원번호", "사원(Employee)": "사원"})
        self.assertEqual(kc.match_nodes(["학생"], ["Student(학생)"]), {"Student(학생)": "학생"})
        self.assertEqual(kc.match_nodes(["학번(PK)"], ["주문번호(PK)"]), {"주문번호(PK)": "주문번호(PK)"})
        res = kc.compare("diagram", {"nodes": ["사원", "사원번호", "부서"], "edges": [edge("사원", "부서")]},
                         {"nodes": ["사원번호(PK)", "사원(Employee)", "부서"], "edges": [edge("사원(Employee)", "부서")]})
        self.assertEqual(res["verdict"], "agree")

    def test_label_text_is_voted(self):
        agy = diagram(["학생 -> 수강 : FK"], ["학생", "수강"])
        rd = {"nodes": ["학생", "수강"], "edges": [edge("학생", "수강", "PK")]}
        self.assertEqual(verdict("diagram", agy, rd), "mismatch")
        v = kc.vote(kc.units("diagram", agy), kc.units("diagram", rd), kc.units("diagram", rd))
        self.assertEqual((v["status"], v["canonical"]["학생->수강"]["label"]), ("corrected", "PK"))

    def test_cardinality_spelling(self):
        agy = diagram(["학생 -> 수강 : 1 : N"], ["학생", "수강"])
        self.assertEqual(agy["edges"][0]["label"], "1 : N")
        self.assertEqual(verdict("diagram", agy, {"nodes": [], "edges": [edge("학생", "수강", "1:N")]}), "agree")
        self.assertEqual(verdict("diagram", diagram(["A -> B : 1:N"]), {"nodes": [], "edges": [edge("A", "B", "1:M")]}),
                         "agree")
        # 1:N (ratio) and 1..* (multiplicity) are different notations: still a mismatch
        self.assertEqual(verdict("diagram", diagram(["A -> B : 1:N"]), {"nodes": [], "edges": [edge("A", "B", "1..*")]}),
                         "mismatch")

    def test_relation_styles(self):
        self.assertEqual(kc.parse_relation("학생 -> 수강 (1:N)"), edge("학생", "수강", "1:N"))
        self.assertEqual(kc.parse_relation("학생(1) → 수강(N)"), edge("학생", "수강", "1:N"))
        self.assertEqual(kc.parse_relation("수강(N) <- 학생(1)"), edge("학생", "수강", "1:N"))
        self.assertEqual(kc.parse_relation("학생 -(수강)-> 과목"), edge("학생", "과목", "수강"))
        self.assertEqual(kc.parse_relation("A -- 수강 --> B"), edge("A", "B", "수강"))
        self.assertEqual(kc.parse_relation("A -->|수강| B"), edge("A", "B", "수강"))
        self.assertEqual(kc.parse_relation("Student -> Section:1:N"), edge("Student", "Section", "1:N"))
        self.assertEqual(kc.parse_relation("A -> Table2:1:N"), edge("A", "Table2", "1:N"))
        self.assertEqual(kc.parse_relation("학생(Student) -> 수강"), edge("학생(Student)", "수강"))
        self.assertIsNone(kc.parse_relation("입력 -> 처리 -> 출력"))
        res = kc.compare("diagram", diagram(["입력 -> 처리 -> 출력"]), {"nodes": [], "edges": [edge("입력", "처리")]})
        self.assertEqual(res["unparsed"], ["입력 -> 처리 -> 출력"])

    def test_numeric_label_does_not_crash(self):
        res = kc.compare("diagram", {"nodes": ["A", "B"], "edges": [edge("A", "B", "1")]},
                         {"nodes": ["A", "B"], "edges": [edge("A", "B", 1)]})
        self.assertEqual(res["verdict"], "agree")
        self.assertEqual(kc.units("diagram", {"edges": [edge("A", "B", 2)]})["A->B"][0], ((), "2"))


class RegressVote(unittest.TestCase):
    def test_wildcard_reads_abstain(self):
        a, r = table([["학번", "점수"], ["1", "85"]]), table([["학번", "점수"], ["1", "[?]"]])
        v = kc.vote(kc.units("table", a), kc.units("table", r), kc.units("table", r))
        self.assertEqual((v["status"], v["canonical"]["r1c1"]), ("agy_upheld", "85"))
        for kind, x, w in (("formula", formula("y = wx + b"), formula("y = wx + [?]")),
                           ("code", code("age > 30"), code("age [?] 30")),
                           ("diagram", {"edges": [edge("A", "B", "1:N")]}, {"edges": [edge("A", "B", "[?]")]})):
            v = kc.vote(kc.units(kind, x), kc.units(kind, w), kc.units(kind, w))
            self.assertEqual(v["status"], "agy_upheld", kind)
            self.assertNotIn(kc.WILD, repr(v["canonical"]), kind)
        # a wildcard read does not uphold what its readable part contradicts
        v = kc.vote(kc.units("code", code("SELECT a FROM t")), kc.units("code", code("age [?] 30")),
                    kc.units("code", code("age [?] 30")))
        self.assertEqual(v["status"], "disputed")
        # one '[?]' read and one different read: no majority
        v = kc.vote(kc.units("table", a), kc.units("table", r), kc.units("table", table([["학번", "점수"], ["1", "90"]])))
        self.assertEqual((v["status"], v["disputed"]), ("disputed", ["r1c1"]))
        # agy could not read the cell, two reads agree: filled
        v = kc.vote(kc.units("table", r), kc.units("table", a), kc.units("table", a))
        self.assertEqual((v["status"], v["changed"], v["canonical"]["r1c1"]), ("corrected", ["r1c1"], "85"))

    def test_line_vote_uses_line_semantics(self):
        agy = "릴레이션 R은 3NF를 만족한다."
        v = kc.vote(lu(agy), lu("3NF를 만족한다"), lu("3NF를 만족한다"))
        self.assertEqual((v["status"], v["canonical"]), ("agy_upheld", {"line": agy}))
        self.assertEqual(kc.vote(lu(agy), lu("3NF를 만족한다"))["status"], "agy_upheld")
        v = kc.vote(lu(agy), lu("2NF를 만족한다"), lu("2NF를 만족 한다"))
        self.assertEqual((v["status"], v["canonical"]), ("corrected", {"line": "릴레이션 R은 2NF를 만족한다."}))
        v = kc.vote(lu(agy), lu("[?]를 만족한다"), lu("[?]를 만족한다"))
        self.assertEqual(v["status"], "agy_upheld")

    def test_shape_gate_does_not_keep_shifted_agy_cells(self):
        agy = table([["학번", "이름", "학과"], ["1", "Kim", "CS"]])
        r1 = table([["학번", "이름", "학년", "학과"], ["1", "Kim", "3", "CS"]])
        r2 = table([["학번", "이름", "학년", "학과"], ["1", "Kim", "4", "CS"]])
        v = kc.vote(kc.units("table", agy), kc.units("table", r1), kc.units("table", r2))
        self.assertEqual((v["status"], v["disputed"]), ("disputed", ["r1c2"]))
        self.assertNotIn("r1c2", v["canonical"])
        self.assertEqual(kc.to_value("table", v["canonical"], agy)["rows"], [["학번", "이름", "학년", "학과"], ["1", "Kim", "", "CS"]])


if __name__ == "__main__":
    unittest.main()

import sandbox  # noqa: F401  (must stay the first import: blocks real-app access)
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from uitree import FakeTree, Node  # noqa: E402
import privacy  # noqa: E402
import report_md  # noqa: E402

FIXTURE = os.path.join(HERE, "fixtures", "report_sql_ko.json")
HEADER = ["버전 (통칭)", "공개 연도", "공식 표준 번호", "대표적인 변화"]


def _doc():
    return FakeTree.from_file(FIXTURE).documents()[0]


def _fixture_md():
    doc = _doc()
    allowed = privacy.allowed_ids(doc)
    root = report_md.find_report_root(doc, allowed)
    return root, allowed, report_md.to_markdown(root, allowed)


def _tables(md):
    """Each GFM table as a list of rows (list of cell strings), header first, separator dropped."""
    out, cur = [], None
    for line in md.split("\n"):
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip()[1:-1].split(" | ")]
            if cur is None:
                cur = []
                out.append(cur)
            if not all(re.fullmatch(r"-{3,}", c) for c in cells):
                cur.append(cells)
        else:
            cur = None
    return out


def _n(i, ct, name="", role="", children=(), **kw):
    return Node(id=i, ct=ct, role=role, name=name, children=list(children), **kw)


def _page(*report_children, extra_panel=()):
    """Minimal synthetic page: main > [sidebar region(nav), chat column(Edit), report panel]."""
    panel = _n("panel", "Group", children=[
        _n("ptitle", "Text", "제목", role="heading", heading=2),
        _n("share", "Button", "공유 및 내보내기", role="button"),
        _n("body", "Group", children=list(report_children)),
    ] + list(extra_panel))
    chat = _n("chat", "Group", children=[
        _n("chat_h1", "Text", "Gemini와의 대화", role="heading", heading=1),
        _n("turn", "Text", "말씀하신 내용 질문", role="heading", heading=5),
        _n("composer", "Edit", "Gemini 프롬프트 입력", role="textbox"),
    ])
    main = _n("main", "Group", role="main", children=[
        _n("sidebar", "Group", role="region", children=[
            _n("nav", "Group", role="navigation", children=[_n("acct", "Hyperlink", "사이드바 대화 비밀 제목")])]),
        _n("cols", "Group", children=[chat, panel]),
    ])
    return _n("doc", "Document", role="RootWebArea", value="https://gemini.google.com/app/<test>",
              children=[main])


def _md_of(doc):
    allowed = privacy.allowed_ids(doc)
    root = report_md.find_report_root(doc, allowed)
    return report_md.to_markdown(root, allowed)


class FixtureConversionTests(unittest.TestCase):
    def setUp(self):
        self.root, self.allowed, self.md = _fixture_md()
        self.stats = report_md.report_stats(self.md)

    def test_root_is_report_body(self):
        self.assertIsNotNone(self.root)
        self.assertEqual(self.root.id, "n139")
        self.assertTrue(self.md.startswith("# SQL 표준 주요 버전 역사 (SQL-86 ~ SQL:2011)\n"), self.md[:80])

    def test_stats(self):
        # 4 headings in the report body (H1 + 3 H2). The "7" measured in spec §23.3 came from the
        # prototype run over the whole main region, which also counted chat-turn headings.
        self.assertEqual(self.stats["headings"], 4)
        self.assertEqual(self.stats["tables"], 1)
        self.assertGreaterEqual(self.stats["links"], 30)
        self.assertEqual(self.stats["links"], 33)
        self.assertGreater(self.stats["chars"], 2000)
        self.assertEqual(self.stats["chars"], len(self.md))

    def test_headings_levels(self):
        heads = [l for l in self.md.split("\n") if l.startswith("#")]
        self.assertEqual(heads, ["# SQL 표준 주요 버전 역사 (SQL-86 ~ SQL:2011)", "## 버전별 공개 연도 및 대표적인 변화",
                                 "## 버전 변천에서 주목할 세 가지 맥락", "## 주요 공개 출처"])

    def test_table_shape(self):
        tables = _tables(self.md)
        self.assertEqual(len(tables), 1)
        rows = tables[0]
        self.assertEqual(rows[0], HEADER)
        self.assertEqual(len(rows), 9)
        for row in rows:
            self.assertEqual(len(row), 4, row)
            for cell in row:
                self.assertTrue(cell, row)
        self.assertEqual(rows[1][0], "SQL-86 / SQL-87")
        self.assertEqual(rows[8][0], "SQL:2011")
        self.assertIn("| 버전 (통칭) | 공개 연도 | 공식 표준 번호 | 대표적인 변화 |", self.md)

    def test_inline_runs_one_paragraph(self):
        paras = self.md.split("\n\n")
        hit = [p for p in paras if "ANSI X3.135-1986" in p and not p.startswith("|")]
        self.assertEqual(len(hit), 1)
        p = hit[0]
        self.assertNotIn("\n", p)
        self.assertTrue(p.startswith("SQL 표준은 1986년 미국표준협회(ANSI)가 ANSI X3.135-1986으로 최초 제정하고"), p[:80])
        self.assertIn("ISO 9075:1987로 채택하면서 공식 출범했다. 이후 2011년 SQL:2011까지", p)

    def test_paragraphs_split_at_citation_chips(self):
        paras = self.md.split("\n\n")
        first = [p for p in paras if p.startswith("첫째, ")]
        second = [p for p in paras if p.startswith("둘째, ")]
        third = [p for p in paras if p.startswith("셋째, ")]
        self.assertEqual((len(first), len(second), len(third)), (1, 1, 1))
        self.assertNotIn("둘째", first[0])
        self.assertIn("FETCH FIRST n ROWS ONLY는 SQL:2008에서 도입되었고", second[0])

    def test_list_items(self):
        items = [l for l in self.md.split("\n") if l.startswith("- ") and not l.startswith("- [")
                 and not l.startswith("- researchgate.net")]
        self.assertEqual(len(items), 3)
        self.assertTrue(items[0].startswith("- 정부 표준 및 표준 기구 공식 기록: 미국 NIST FIPS PUB 127-1 (SQL-86/89"))

    def test_source_links_are_list_items(self):
        links = [l for l in self.md.split("\n") if l.startswith("- [")]
        self.assertEqual(len(links), 33)
        self.assertIn("- [en.wikipedia.org SQL - Wikipedia](https://en.wikipedia.org/wiki/SQL)", links)
        self.assertNotIn("새 창에서 열기", self.md)

    def test_no_chat_turns(self):
        for bad in ("연구 계획", "말씀하신 내용", "SQL 표준의 주요 버전 역사(1986년", "각 버전의 공개 연도와 대표적인 변화를",
                    "I've completed", "Gemini와의 대화", "Gemini의 응답", "연구 시작", "무엇을 조사하고 싶으신가요",
                    "Gemini는 AI이며", "support.google.com", "몇 분 후 완료"):
            self.assertNotIn(bad, self.md)

    def test_controls_skipped(self):
        for bad in ("자세히 알아보기", "공유 및 내보내기", "목차 메뉴", "보고서에서 사용된 소스", "대답이 마음에",
                    "생각하는 과정 표시", "미리보기", "패널 닫기", "만들기"):
            self.assertNotIn(bad, self.md)

    def test_non_http_hyperlink_is_not_a_link(self):
        # n329's href was redacted by scrub: it stays as plain text, never as [text](...)
        self.assertNotIn("<redacted", self.md)
        self.assertEqual(len(re.findall(r"\]\((?!https?://)", self.md)), 0)
        self.assertIn("\n- researchgate.net Fred Zemke PhD Software Engineer at Oracle Corporation\n", self.md)

    def test_structural_check_clean(self):
        self.assertEqual(report_md.structural_check(self.md, self.stats), [])

    def test_disallowed_nodes_never_contribute(self):
        not_h1 = set(self.allowed) - {"n142", "n144"}
        md = report_md.to_markdown(self.root, not_h1)
        self.assertNotIn("SQL 표준 주요 버전 역사 (SQL-86", md)
        self.assertNotIn("ANSI X3.135-1986으로", md)
        self.assertIn("## 버전별 공개 연도 및 대표적인 변화", md)
        no_table = set(self.allowed) - {"n155"}
        self.assertNotIn("버전 (통칭)", report_md.to_markdown(self.root, no_table))
        self.assertEqual(report_md.to_markdown(self.root, set()), "")
        self.assertEqual(report_md.to_markdown(self.root, None), "")


class FixturePrivacyTests(unittest.TestCase):
    def test_committed_fixture_has_no_conversation_url_or_account(self):
        with open(FIXTURE, encoding="utf-8") as fh:
            raw = fh.read()
        self.assertEqual(re.findall(r"gemini\.google\.com/app/[0-9a-f]{8,}", raw), [])
        self.assertEqual(re.findall(r"/app/[0-9a-f]{12,}", raw), [])
        self.assertEqual(re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", raw), [])
        for bad in ("Google 계정", "Google Account", "@"):
            self.assertNotIn(bad, raw)
        self.assertIn("https://gemini.google.com/app/<test>", raw)

    def test_fixture_is_fake_tree_with_allowed_region(self):
        doc = _doc()
        self.assertEqual(privacy.region_check(doc), (True, "ok"))
        self.assertNotIn("newchat", privacy.allowed_ids(doc))


class PanelScopeTests(unittest.TestCase):
    def test_synthetic_page_scoped_to_panel_body(self):
        doc = _page(_n("h1", "Text", "보고서", role="heading", heading=1),
                    _n("t1", "Text", "본문 "), _n("t2", "Text", "코드"), _n("t3", "Text", " 끝."))
        md = _md_of(doc)
        self.assertEqual(md, "# 보고서\n\n본문 코드 끝.")

    def test_sidebar_text_never_contributes(self):
        doc = _page(_n("h1", "Text", "보고서", role="heading", heading=1))
        allowed = privacy.allowed_ids(doc)
        sidebar = report_md.to_markdown(doc, allowed)
        self.assertNotIn("비밀 제목", sidebar)

    def test_no_report_controls_means_no_root(self):
        doc = _page(_n("h1", "Text", "보고서", role="heading", heading=1))
        for n, _ in doc.walk():
            if n.id == "share":
                n.name = "다른 버튼"
        self.assertIsNone(report_md.find_report_root(doc, privacy.allowed_ids(doc)))

    def test_no_h1_means_no_root(self):
        doc = _page(_n("h2", "Text", "소제목", role="heading", heading=2))
        self.assertIsNone(report_md.find_report_root(doc, privacy.allowed_ids(doc)))

    def test_panel_containing_composer_rejected(self):
        # controls living in the chat column (no separate panel) -> fail closed
        doc = _page(_n("h1", "Text", "보고서", role="heading", heading=1))
        for n, anc in doc.walk():
            if n.id == "chat":
                n.children.append(_n("toc", "Button", "Table of contents", role="button"))
            if n.id == "panel":
                n.children = [c for c in n.children if c.id != "share"]
        self.assertIsNone(report_md.find_report_root(doc, privacy.allowed_ids(doc)))

    def test_english_labels(self):
        doc = _page(_n("h1", "Text", "Report", role="heading", heading=1))
        for n, _ in doc.walk():
            if n.id == "share":
                n.name = "Share & export"
        root = report_md.find_report_root(doc, privacy.allowed_ids(doc))
        self.assertIsNotNone(root)
        self.assertEqual(root.id, "body")

    def test_unallowed_doc_fails_closed(self):
        doc = _page(_n("h1", "Text", "보고서", role="heading", heading=1))
        self.assertIsNone(report_md.find_report_root(doc, set()))


class RenderRuleTests(unittest.TestCase):
    def _render(self, *children):
        root = _n("r", "Group", children=list(children))
        return report_md.to_markdown(root, {n.id for n, _ in root.walk()})

    def test_links_only_http(self):
        md = self._render(_n("t", "Text", "참고 "),
                          _n("a", "Hyperlink", "사이트 새 창에서 열기", value="https://example.org/a b"),
                          _n("b", "Hyperlink", "로컬", value="javascript:void(0)"),
                          _n("c", "Hyperlink", "앱", value="file:///c:/x"))
        self.assertEqual(md, "참고 [사이트](https://example.org/a%20b)로컬앱")

    def test_link_href_from_aria(self):
        md = self._render(_n("a", "Hyperlink", "x", aria={"href": "http://example.org/"}))
        self.assertEqual(md, "- [x](http://example.org/)")

    def test_whitespace_and_newlines_collapse(self):
        md = self._render(_n("t1", "Text", "가\n나"), _n("t2", "Text", " \xa0 "))
        self.assertEqual(md, "가 나")

    def test_table_pipes_escaped(self):
        md = self._render(_n("g", "Table", role="grid", children=[
            _n("r0", "DataItem", role="row", children=[_n("h0", "DataItem", "a|b", role="columnheader")]),
            _n("r1", "DataItem", role="row", children=[_n("c0", "DataItem", "1\n2", role="gridcell")])]))
        self.assertEqual(md, "| a\\|b |\n| --- |\n| 1 2 |")


class StructuralCheckTests(unittest.TestCase):
    def _check(self, *children):
        root = _n("r", "Group", children=list(children))
        md = report_md.to_markdown(root, {n.id for n, _ in root.walk()})
        return report_md.structural_check(md, report_md.report_stats(md))

    def _link(self):
        return _n("a", "Hyperlink", "src", value="https://example.org/")

    def test_flags_empty_table_cell(self):
        probs = self._check(_n("h", "Text", "제목", role="heading", heading=1), self._link(),
                            _n("g", "Table", role="grid", children=[
                                _n("r0", "DataItem", role="row", children=[
                                    _n("h0", "DataItem", "A", role="columnheader"),
                                    _n("h1", "DataItem", "B", role="columnheader")]),
                                _n("r1", "DataItem", role="row", children=[
                                    _n("c0", "DataItem", "1", role="gridcell"),
                                    _n("c1", "DataItem", "", role="gridcell")])]))
        self.assertIn("empty_table_cell", probs)
        self.assertNotIn("no_heading", probs)

    def test_flags_ragged_table(self):
        probs = self._check(_n("h", "Text", "제목", role="heading", heading=1), self._link(),
                            _n("g", "Table", role="grid", children=[
                                _n("r0", "DataItem", role="row", children=[
                                    _n("h0", "DataItem", "A", role="columnheader"),
                                    _n("h1", "DataItem", "B", role="columnheader")]),
                                _n("r1", "DataItem", role="row", children=[
                                    _n("c0", "DataItem", "1", role="gridcell")])]))
        self.assertIn("empty_table_cell", probs)

    def test_flags_missing_heading(self):
        probs = self._check(_n("t", "Text", "본문만 있다."), self._link())
        self.assertIn("no_heading", probs)
        self.assertNotIn("empty_table_cell", probs)

    def test_flags_no_links_and_empty(self):
        self.assertIn("no_links", self._check(_n("h", "Text", "제목", role="heading", heading=1)))
        self.assertIn("empty_report", report_md.structural_check("", report_md.report_stats("")))

    def test_clean_small_report(self):
        self.assertEqual(self._check(_n("h", "Text", "제목", role="heading", heading=1), _n("t", "Text", "본문"),
                                     self._link()), [])


if __name__ == "__main__":
    unittest.main()

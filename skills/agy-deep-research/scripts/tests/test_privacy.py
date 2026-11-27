import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from uitree import Node  # noqa: E402
import privacy  # noqa: E402


def _page():
    with open(os.path.join(HERE, "fixtures", "privacy_page.json"), encoding="utf-8") as fh:
        return Node.from_dict(json.load(fh))


class ScrubTests(unittest.TestCase):
    def test_scrub_short_account_string(self):
        s = "Google 계정: 테스트(test@example.invalid)"
        out = privacy.scrub(s)
        self.assertEqual(out, "<redacted:account len=%d>" % len(s))
        self.assertNotIn("example", out)

    def test_scrub_long_text_keeps_content_removes_email(self):
        text = "본문 " * 80 + "연락: a.b+c@host.example.invalid 끝\nGoogle 계정: 테스트(x)\n다음 줄"
        self.assertGreater(len(text), 200)
        out = privacy.scrub(text)
        self.assertIn("본문", out)
        self.assertIn("다음 줄", out)
        self.assertNotIn("@", out)
        self.assertIn("<redacted:email>", out)
        self.assertIn("<redacted:account>", out)
        self.assertNotIn("테스트", out)

    def test_scrub_recurses_dicts_lists(self):
        data = {"a": ["ok", "x@y.invalid", ("프로필 사진", 3)], "b": {"c": "Account", "n": 5}}
        out = privacy.scrub(data)
        self.assertEqual(out["a"][0], "ok")
        self.assertTrue(out["a"][1].startswith("<redacted:account"))
        self.assertTrue(out["a"][2][0].startswith("<redacted:account"))
        self.assertEqual(out["a"][2][1], 3)
        self.assertTrue(out["b"]["c"].startswith("<redacted:account"))
        self.assertEqual(out["b"]["n"], 5)


class ScrubHardeningTests(unittest.TestCase):
    def test_scrub_dict_keys(self):
        out = privacy.scrub({"Google 계정: 테스트(t@e.invalid)": 1, "ok": 2})
        self.assertEqual(sorted(out.values()), [1, 2])
        self.assertNotIn("테스트", json.dumps(out, ensure_ascii=False))
        self.assertIn("ok", out)

    def test_scrub_bytes(self):
        out = privacy.scrub("Google 계정: 테스트(t@e.invalid)".encode("utf-8"))
        self.assertIsInstance(out, str)
        self.assertTrue(out.startswith("<redacted:account"))
        out2 = privacy.scrub(bytearray(b"\xff\xfe plain"))
        self.assertIsInstance(out2, str)

    def test_scrub_sets(self):
        out = privacy.scrub({"a@b.invalid", "fine"})
        self.assertIsInstance(out, set)
        self.assertNotIn("a@b.invalid", out)
        self.assertIn("fine", out)
        fout = privacy.scrub(frozenset({"a@b.invalid"}))
        self.assertIsInstance(fout, frozenset)
        self.assertNotIn("a@b.invalid", fout)

    def test_scrub_long_text_variants(self):
        pad = "본문 " * 80
        out = privacy.scrub(pad + " user@localhost x GOOGLE ACCOUNT: Bob\n끝")
        self.assertNotIn("user@localhost", out)
        self.assertNotIn("Bob", out)
        self.assertIn("끝", out)
        out = privacy.scrub(pad + " google 계정 : 밥\n끝")
        self.assertNotIn("밥", out)

    def test_scrub_length_boundary(self):
        s200 = "u@" + "x" * 198
        s201 = "u@" + "x" * 199
        self.assertEqual(len(s200), 200)
        self.assertEqual(privacy.scrub(s200), "<redacted:account len=200>")
        out = privacy.scrub(s201)
        self.assertNotEqual(out, "<redacted:account len=201>")
        self.assertNotIn("@", out)


class AllowListTests(unittest.TestCase):
    def test_fail_closed_without_nav_region(self):
        doc = _page()
        for n, _ in doc.walk():
            if n.role == "navigation":
                n.role = "group"
        self.assertEqual(privacy.region_check(doc), (False, "no_nav_region"))
        self.assertEqual(privacy.allowed_ids(doc), set())

    def test_uppercase_roles_still_excluded(self):
        doc = _page()
        for n, _ in doc.walk():
            n.role = n.role.upper()
        ok, reason = privacy.region_check(doc)
        self.assertTrue(ok, reason)
        allowed = privacy.allowed_ids(doc)
        for bad in ("sidebar", "nav1", "acct", "chat1"):
            self.assertNotIn(bad, allowed)
        self.assertIn("composer", allowed)

    def test_fail_closed_on_duplicate_ids(self):
        doc = _page()
        doc.children[0].children[0].children.append(Node(id="composer", ct="Edit"))
        self.assertEqual(privacy.region_check(doc), (False, "duplicate_ids"))
        self.assertEqual(privacy.allowed_ids(doc), set())

    def test_account_under_main_caught_by_scrub(self):
        doc = _page()
        main = [n for n, _ in doc.walk() if n.role == "main"][0]
        main.children.append(Node(id="stray", ct="Hyperlink",
                                  name="Google 계정: 테스트(test@example.invalid)"))
        # structural layer does not protect this node ...
        self.assertIn("stray", privacy.allowed_ids(doc))
        # ... the scrub backstop does
        self.assertTrue(privacy.scrub(main.children[-1].name).startswith("<redacted:account"))

    def test_nested_region_in_region_excluded(self):
        doc = _page()
        main = [n for n, _ in doc.walk() if n.role == "main"][0]
        main.children.append(Node(id="outer", ct="Group", role="region", children=[
            Node(id="inner", ct="Group", role="region", children=[
                Node(id="n9", ct="Group", role="navigation", children=[
                    Node(id="deep", ct="Text", name="x")])]),
            Node(id="sib", ct="Text", name="y")]))
        allowed = privacy.allowed_ids(doc)
        for bad in ("outer", "inner", "n9", "deep", "sib"):
            self.assertNotIn(bad, allowed)
        self.assertIn("composer", allowed)

    def test_main_inside_excluded_fails_closed(self):
        doc = Node(id="d", ct="Document", children=[
            Node(id="b", ct="Group", role="banner", children=[
                Node(id="m", ct="Group", role="main", children=[
                    Node(id="r", ct="Group", role="region", children=[
                        Node(id="n", ct="Group", role="navigation")])])])])
        self.assertEqual(privacy.region_check(doc), (False, "main_inside_excluded"))
        self.assertEqual(privacy.allowed_ids(doc), set())

    def test_allowed_excludes_sidebar_region_and_account(self):
        doc = _page()
        ok, reason = privacy.region_check(doc)
        self.assertTrue(ok, reason)
        allowed = privacy.allowed_ids(doc)
        for bad in ("sidebar", "nav1", "nav2", "acct", "chat1", "chat2", "newchat"):
            self.assertNotIn(bad, allowed)
        for good in ("conv", "turn1", "turn1t", "composer"):
            self.assertIn(good, allowed)

    def test_fail_closed_without_main(self):
        doc = _page()
        for n, _ in doc.walk():
            if n.role == "main":
                n.role = "group"
        self.assertEqual(privacy.region_check(doc), (False, "main_count=0"))
        self.assertEqual(privacy.allowed_ids(doc), set())

    def test_fail_closed_with_two_mains(self):
        doc = _page()
        doc.children.append(Node(id="main2", ct="Group", role="main"))
        self.assertEqual(privacy.region_check(doc), (False, "main_count=2"))
        self.assertEqual(privacy.allowed_ids(doc), set())

    def test_fail_closed_when_nav_region_outside_main(self):
        doc = _page()
        doc.children.append(Node(id="r2", ct="Group", role="region", children=[
            Node(id="n3", ct="Group", role="navigation")]))
        ok, _reason = privacy.region_check(doc)
        self.assertFalse(ok)
        self.assertEqual(privacy.allowed_ids(doc), set())

    def test_outside_summary_counts_only(self):
        doc = _page()
        allowed = privacy.allowed_ids(doc)
        summary = privacy.outside_summary(doc, allowed)
        self.assertEqual(summary.get("Hyperlink/-"), 4)
        self.assertEqual(summary.get("Group/navigation"), 2)
        self.assertEqual(summary.get("Group/region"), 1)
        self.assertTrue(all(isinstance(v, int) for v in summary.values()))
        self.assertNotIn("계정", json.dumps(summary, ensure_ascii=False))

    def test_generic_labels(self):
        self.assertIn("새 채팅", privacy.GENERIC_LABELS)
        self.assertIsInstance(privacy.GENERIC_LABELS, frozenset)


if __name__ == "__main__":
    unittest.main()

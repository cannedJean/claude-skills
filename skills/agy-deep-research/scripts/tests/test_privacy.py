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


class AllowListTests(unittest.TestCase):
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

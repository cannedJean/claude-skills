import sandbox  # noqa: F401  (must stay the first import: blocks real-app access)
import json
import os
import sys
import unicodedata
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from uitree import FakeTree, Node  # noqa: E402
import privacy  # noqa: E402


def _page():
    return FakeTree.from_file(os.path.join(HERE, "fixtures", "privacy_page.json")).documents()[0]


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
        self.assertEqual(privacy.region_check(doc), (False, "nav_region_outside_main"))
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


class PrivacyReviewFixTests(unittest.TestCase):
    # B2
    def test_scrub_unicode_normalisation(self):
        nfd = unicodedata.normalize("NFD", "Google 계정: 홍길동")
        self.assertNotEqual(nfd, "Google 계정: 홍길동")
        for label, s in (("nfd hangul", nfd),
                         ("fullwidth at", "test＠example.invalid"),
                         ("fullwidth Account", "Ａｃｃｏｕｎｔ")):
            with self.subTest(label):
                self.assertTrue(privacy.scrub(s).startswith("<redacted:account"), label)
        pad = "본문 " * 80
        out = privacy.scrub(pad + " user＠host.example.invalid 끝")
        self.assertNotIn("host.example", out)
        self.assertIn("<redacted:email>", out)
        self.assertIn("끝", out)

    def test_scrub_plain_short_text_unchanged(self):
        self.assertEqual(privacy.scrub("답변 본문 ｱ"), "답변 본문 ｱ")

    # B3
    def test_role_whitespace_does_not_bypass_excluded_landmark(self):
        doc = _page()
        main = [n for n, _ in doc.walk() if n.role == "main"][0]
        main.children.append(Node(id="ws_banner", ct="Group", role=" banner ", children=[
            Node(id="ws_secret", ct="Text", name="secret")]))
        ok, reason = privacy.region_check(doc)
        self.assertTrue(ok, reason)
        allowed = privacy.allowed_ids(doc)
        self.assertNotIn("ws_banner", allowed)
        self.assertNotIn("ws_secret", allowed)
        self.assertIn("composer", allowed)

    def test_role_whitespace_main_and_navigation_still_recognised(self):
        doc = _page()
        for n, _ in doc.walk():
            if n.role in ("main", "navigation", "region"):
                n.role = "  %s\t" % n.role.upper()
        ok, reason = privacy.region_check(doc)
        self.assertTrue(ok, reason)
        allowed = privacy.allowed_ids(doc)
        self.assertNotIn("chat1", allowed)
        self.assertIn("composer", allowed)

    # B17 / M2
    def test_scrub_dict_key_collisions_keep_every_value(self):
        out = privacy.scrub({"a@b.com": 1, "c@d.com": 2, "e@f.com": 3})
        self.assertEqual(len(out), 3)
        self.assertEqual(sorted(out.values()), [1, 2, 3])
        self.assertNotIn("@", json.dumps(out))

    # T1
    def test_allowed_excludes_every_chrome_landmark_under_main_and_outside_main(self):
        doc = _page()
        main = [n for n, _ in doc.walk() if n.role == "main"][0]
        for role in ("banner", "contentinfo", "complementary", "search", "navigation"):
            main.children.append(Node(id="x_" + role, ct="Group", role=role, children=[
                Node(id="x_%s_c" % role, ct="Group", children=[
                    Node(id="x_%s_t" % role, ct="Text", name="t")])]))
        doc.children.append(Node(id="outside", ct="Group", children=[
            Node(id="outside_t", ct="Text", name="o")]))
        ok, reason = privacy.region_check(doc)
        self.assertTrue(ok, reason)
        allowed = privacy.allowed_ids(doc)
        for role in ("banner", "contentinfo", "complementary", "search", "navigation"):
            for suffix in ("", "_c", "_t"):
                self.assertNotIn("x_%s%s" % (role, suffix), allowed, role)
        for bad in ("doc", "g0", "main", "outside", "outside_t"):
            self.assertNotIn(bad, allowed)
        self.assertEqual(allowed, {"conv", "turn1", "turn1t", "composer"})

    # T12
    def test_non_nav_region_under_main_is_allowed_and_not_counted(self):
        doc = _page()
        main = [n for n, _ in doc.walk() if n.role == "main"][0]
        main.children.append(Node(id="plain_region", ct="Group", role="region", children=[
            Node(id="plain_t", ct="Text", name="x")]))
        self.assertEqual(privacy.region_check(doc), (True, "ok"))
        allowed = privacy.allowed_ids(doc)
        self.assertIn("plain_region", allowed)
        self.assertIn("plain_t", allowed)
        self.assertNotIn("sidebar", allowed)

    def test_region_check_reasons_are_exact(self):
        doc = _page()
        doc.children.append(Node(id="r2", ct="Group", role="region", children=[
            Node(id="n3", ct="Group", role="navigation")]))
        self.assertEqual(privacy.region_check(doc), (False, "nav_region_outside_main"))
        self.assertEqual(privacy.region_check(_page()), (True, "ok"))

    # T3
    def test_long_string_email_redaction_is_exact(self):
        pad = "본문 " * 80
        out = privacy.scrub(pad + "연락: a.b+c@host.example.invalid 끝")
        self.assertEqual(out, pad + "연락: <redacted:email> 끝")
        for leak in ("a.b", "host", "example", "invalid"):
            self.assertNotIn(leak, out)
        s201 = "u@" + "x" * 199
        self.assertEqual(privacy.scrub(s201), "<redacted:email>")

    # T4
    def test_outside_summary_exact(self):
        doc = _page()
        summary = privacy.outside_summary(doc, privacy.allowed_ids(doc))
        self.assertEqual(summary, {
            "Document/RootWebArea": 1, "Group/-": 1, "Group/main": 1, "Group/region": 1,
            "Group/navigation": 2, "Hyperlink/-": 4})
        self.assertNotIn("Text/-", summary)
        self.assertNotIn("Edit/-", summary)

    # T10
    def test_sensitive_tokens_each_redacted_case_insensitively(self):
        for tok in ("@", "계정", "Account", "프로필", "Profile",
                    "account", "ACCOUNT", "profile", "PROFILE"):
            with self.subTest(tok):
                s = "x %s y" % tok
                self.assertEqual(privacy.scrub(s), "<redacted:account len=%d>" % len(s))
        self.assertEqual(privacy.scrub("plain text"), "plain text")

    def test_scrub_container_types_and_content(self):
        t = privacy.scrub(("a@b.invalid", "fine", 3))
        self.assertIsInstance(t, tuple)
        self.assertEqual(t, ("<redacted:account len=11>", "fine", 3))
        self.assertIsInstance(privacy.scrub(["x"]), list)
        s = privacy.scrub({"a@b.invalid", "fine"})
        self.assertEqual(s, {"<redacted:account len=11>", "fine"})
        fs = privacy.scrub(frozenset({"a@b.invalid", "fine"}))
        self.assertEqual(fs, frozenset({"<redacted:account len=11>", "fine"}))
        self.assertEqual(privacy.scrub(b"fine bytes"), "fine bytes")
        self.assertEqual(privacy.scrub(b"x@y"), "<redacted:account len=3>")
        self.assertEqual(privacy.scrub(bytearray(b"Profile")), "<redacted:account len=7>")
        self.assertIsNone(privacy.scrub(None))
        self.assertEqual(privacy.scrub(7), 7)

    # T11
    def test_generic_labels_exact(self):
        self.assertEqual(privacy.GENERIC_LABELS, frozenset({
            "새 채팅", "New chat", "사이드바 열기", "사이드바 닫기", "Open sidebar",
            "Close sidebar", "더보기", "Show more", "메인 메뉴", "Main menu"}))


class ScrubKeepsOriginalTests(unittest.TestCase):
    def test_long_text_unchanged_except_redacted_span(self):
        body = "① 본문 １２３ ｱ " * 30
        text = body + "연락 a@b.invalid 끝 " + body
        out = privacy.scrub(text)
        self.assertEqual(out, body + "연락 <redacted:email> 끝 " + body)
        self.assertEqual(privacy.scrub(body), body)

    def test_fullwidth_email_in_long_text_is_redacted(self):
        body = "① 본문 １２３ " * 30
        out = privacy.scrub(body + "ｕ＠ｅｘ．ｃｏｍ 끝")
        self.assertNotIn("ｅｘ", out)
        self.assertNotIn("＠", out)
        self.assertIn("<redacted:email>", out)
        self.assertTrue(out.startswith(body))
        self.assertTrue(out.endswith(" 끝"))

    def test_unmappable_nfd_account_fails_closed(self):
        nfd = unicodedata.normalize("NFD", "Google 계정: 홍길동")
        out = privacy.scrub("본문 " * 80 + nfd)
        self.assertNotIn("홍", out)
        self.assertNotIn(unicodedata.normalize("NFD", "홍"), out)
        self.assertEqual(out, "<redacted:account>")

    def test_long_nfd_label_still_hits_sensitive_re(self):
        # B15: 220 code points in NFD, 76 in NFC: judged by the short-string rule on its NFC length
        nfd = unicodedata.normalize("NFD", "프로필 설정: " + "한" * 68)
        self.assertGreater(len(nfd), 200)
        self.assertEqual(privacy.scrub(nfd), "<redacted:account len=%d>" % len(nfd))
        nfc = unicodedata.normalize("NFC", nfd)
        self.assertEqual(privacy.scrub(nfc), "<redacted:account len=%d>" % len(nfc))


class ScrubCombiningMarkTests(unittest.TestCase):
    PAD = "본문 " * 80

    def test_combining_mark_in_local_part_leaks_nothing(self):
        for label, local in (("first letter", "álice"), ("middle letter", "aĺice"),
                             ("last letter", "aliće"), ("two marks", "ạ́lice")):
            with self.subTest(label):
                out = privacy.scrub(self.PAD + "연락 " + local + "@example.com 끝")
                self.assertNotIn("@", out)
                for frag in ("a", "l", "i", "c", "e", "́", "example"):
                    self.assertNotIn(frag, out.replace("<redacted:email>", "")
                                     .replace("<redacted:account>", ""), label)
                self.assertTrue(out.startswith(self.PAD) or out == "<redacted:account>")

    ZW = ("\u200b", "\u200c", "\u200d", "\u2060", "\ufeff", "\u00ad")

    def _no_part(self, out, parts, label, zw_inside=True):
        rest = out.replace("<redacted:email>", "").replace("<redacted:account>", "")
        self.assertNotIn("@", rest, label)
        for frag in parts:
            self.assertNotIn(frag, rest, label)
        if zw_inside:  # a zero-width char inside the address goes with it (one before it may stay outside)
            rest = rest.replace(self.PAD + "연락 ", "")
            for zw in self.ZW:
                self.assertNotIn(zw, rest.replace(" 끝", ""), label)

    def test_zero_width_in_local_part_leaks_nothing(self):
        for zw in self.ZW:
            for local in ("al" + zw + "ice", zw + "alice", "alice" + zw, "a" + zw + zw + "lice"):
                label = "%r in %r" % (zw, local)
                with self.subTest(label):
                    out = privacy.scrub(self.PAD + "연락 " + local + "@example.com 끝")
                    self._no_part(out, ("alice", "al", "ice", "example"), label, zw_inside=not local.startswith(zw))
                    self.assertTrue(out.startswith(self.PAD) or out == "<redacted:account>", label)

    def test_zero_width_in_domain_leaks_nothing(self):
        for zw in self.ZW:
            for domain in ("exa" + zw + "mple.com", "example" + zw + ".com", "example." + zw + "com", "example.co" + zw + "m"):
                label = "%r in %r" % (zw, domain)
                with self.subTest(label):
                    out = privacy.scrub(self.PAD + "연락 alice@" + domain + " 끝")
                    self._no_part(out, ("alice", "exa", "mple", "example", ".com", "com"), label)
                    self.assertTrue(out.endswith(" 끝") or out == "<redacted:account>", label)

    def test_zero_width_around_at_sign_and_in_account_label(self):
        out = privacy.scrub(self.PAD + "연락 alice\u200b@\u200bexample.com 끝")
        self._no_part(out, ("alice", "example"), "around @")
        out = privacy.scrub(self.PAD + "Google \u200b계\u200b정: 홍길동\n끝")
        self.assertNotIn("홍길동", out)
        self.assertEqual(privacy.scrub("계\u200b정 설정"), "<redacted:account len=6>")

    def test_zero_width_elsewhere_is_unchanged(self):
        for zw in self.ZW:
            with self.subTest(zw=repr(zw)):
                text = self.PAD + "본문" + zw + "끝 café" + zw + " ① １２３ a" + zw + "b"
                self.assertEqual(privacy.scrub(text), text)

    def test_combining_mark_elsewhere_is_unchanged(self):
        text = self.PAD + "café ① １２３ 끝"
        self.assertEqual(privacy.scrub(text), text)
        text2 = self.PAD + "álice 은 é 만 있다"
        self.assertEqual(privacy.scrub(text2), text2)


if __name__ == "__main__":
    unittest.main()

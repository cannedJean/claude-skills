import unittest

from slugkit.normalize import collapse_whitespace, strip_accents


class TestNormalizeHidden(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(strip_accents(""), "")
        self.assertEqual(collapse_whitespace(""), "")

    def test_more_accents(self):
        self.assertEqual(strip_accents("Ångström Øre naïve"), "Angstrom Øre naive")

    def test_compatibility_decomposition(self):
        self.assertEqual(strip_accents("ﬁne"), "fine")

    def test_hangul_unchanged(self):
        self.assertEqual(strip_accents("한글 제목"), "한글 제목")

    def test_unicode_whitespace(self):
        self.assertEqual(collapse_whitespace("　a  b\r\n"), "a b")
        self.assertEqual(collapse_whitespace(" \t\n "), "")

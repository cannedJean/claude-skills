import unittest

from slugkit.slug import slugify


class TestSlugifyHidden(unittest.TestCase):
    def test_nothing_left(self):
        self.assertEqual(slugify(""), "")
        self.assertEqual(slugify("!!! ???"), "")

    def test_no_leading_or_trailing_sep(self):
        self.assertEqual(slugify("  --Already--Slugged--  "), "already-slugged")

    def test_symbols_collapse(self):
        self.assertEqual(slugify("C++ & C#"), "c-c")

    def test_max_len_not_needed(self):
        self.assertEqual(slugify("abc", max_len=10), "abc")
        self.assertEqual(slugify("abcdef", max_len=3), "abc")

    def test_bad_max_len(self):
        with self.assertRaises(ValueError):
            slugify("x", max_len=0)

    def test_multichar_sep(self):
        self.assertEqual(slugify("Ångström 2024", sep="--"), "angstrom--2024")
        self.assertEqual(slugify("ab cd", sep="--", max_len=4), "ab")

    def test_non_latin_letters_are_separators(self):
        self.assertEqual(slugify("한글 title 2"), "title-2")

    def test_keyword_only(self):
        with self.assertRaises(TypeError):
            slugify("a b", "_")

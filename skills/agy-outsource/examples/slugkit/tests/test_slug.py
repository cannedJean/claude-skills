import unittest

from slugkit.slug import slugify


class TestSlugify(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(slugify("Hello, World!"), "hello-world")

    def test_accents(self):
        self.assertEqual(slugify("Café  crème brûlée"), "cafe-creme-brulee")

    def test_custom_sep(self):
        self.assertEqual(slugify("a b", sep="_"), "a_b")

    def test_max_len_drops_trailing_sep(self):
        self.assertEqual(slugify("hello world", max_len=6), "hello")

    def test_empty_sep_rejected(self):
        with self.assertRaises(ValueError):
            slugify("x", sep="")

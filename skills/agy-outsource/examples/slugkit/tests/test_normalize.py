import unittest

from slugkit.normalize import collapse_whitespace, strip_accents


class TestNormalize(unittest.TestCase):
    def test_strip_accents_basic(self):
        self.assertEqual(strip_accents("Café crème"), "Cafe creme")

    def test_strip_accents_keeps_plain_ascii(self):
        self.assertEqual(strip_accents("abc-123 XYZ"), "abc-123 XYZ")

    def test_collapse_whitespace(self):
        self.assertEqual(collapse_whitespace("  a \t b\n\nc  "), "a b c")

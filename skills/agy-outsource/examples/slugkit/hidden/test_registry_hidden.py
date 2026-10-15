import unittest

from slugkit.registry import SlugRegistry


class TestRegistryHidden(unittest.TestCase):
    def test_empty_becomes_item(self):
        r = SlugRegistry()
        self.assertEqual(r.add(""), "item")
        self.assertEqual(r.add(""), "item-2")
        self.assertEqual(r.add("item"), "item-3")

    def test_custom_sep(self):
        r = SlugRegistry(sep="_")
        self.assertEqual(r.add("a_b"), "a_b")
        self.assertEqual(r.add("a_b"), "a_b_2")

    def test_slug_is_taken_as_given(self):
        r = SlugRegistry()
        self.assertEqual(r.add("Hello World"), "Hello World")
        self.assertEqual(r.add("hello world"), "hello world")

    def test_suffix_skips_taken(self):
        r = SlugRegistry()
        self.assertEqual(r.add("a-2"), "a-2")
        self.assertEqual(r.add("a"), "a")
        self.assertEqual(r.add("a"), "a-3")

    def test_membership_and_independence(self):
        r1, r2 = SlugRegistry(), SlugRegistry()
        r1.add("x")
        self.assertNotIn("zzz", r1)
        self.assertNotIn("x", r2)
        self.assertEqual(len(r2), 0)
        self.assertEqual(r2.add("x"), "x")

import unittest

from slugkit.registry import SlugRegistry


class TestRegistry(unittest.TestCase):
    def test_suffixes(self):
        r = SlugRegistry()
        self.assertEqual(r.add("Hello World"), "hello-world")
        self.assertEqual(r.add("hello world"), "hello-world-2")
        self.assertEqual(r.add("Hello-World"), "hello-world-3")
        self.assertEqual(len(r), 3)
        self.assertIn("hello-world-2", r)

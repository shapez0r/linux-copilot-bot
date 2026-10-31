import unittest

from support import make_config  # noqa: F401
from bot.telegram_api import MESSAGE_LIMIT, chunk_text


class ChunkTests(unittest.TestCase):
    def test_short_text_is_one_message(self):
        self.assertEqual(chunk_text("hi"), ["hi"])

    def test_long_text_splits_on_lines_within_the_limit(self):
        text = "\n".join("line %d" % i for i in range(2000))
        parts = chunk_text(text)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p) <= MESSAGE_LIMIT for p in parts))
        self.assertEqual("\n".join(parts).split("\n"), text.split("\n"))

    def test_unbroken_text_is_cut_at_the_limit(self):
        parts = chunk_text("x" * (MESSAGE_LIMIT * 2 + 5))
        self.assertEqual([len(p) for p in parts], [MESSAGE_LIMIT, MESSAGE_LIMIT, 5])


if __name__ == "__main__":
    unittest.main()

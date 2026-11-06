"""Diff-style change summaries of a commit."""

import unittest

from support import make_config  # noqa: F401  (adds the repository root to sys.path)

from bot.diff_view import ADDED, DELETED, MAX_FILES, format_changes, parse_numstat


class DiffViewTests(unittest.TestCase):
    def test_numstat_rows_render_green_additions_and_red_deletions(self):
        rows = parse_numstat("10\t0\tnew.py\n3\t7\tbot/service.py\n-\t-\tlogo.png\n0\t1\told.txt\n")
        self.assertEqual(rows[2], (None, None, "logo.png"))
        text = format_changes(rows)
        lines = text.splitlines()
        self.assertEqual(lines[0], "Changes: 4 files \u00b7 \U0001f7e2 +13 \U0001f534 \u22128")
        self.assertEqual(lines[1], "\U0001f4c4 new.py  +10 \u22120  " + ADDED * 5)
        self.assertEqual(lines[2], "\U0001f4c4 bot/service.py  +3 \u22127  " + ADDED * 2 + DELETED * 3)
        self.assertEqual(lines[3], "\U0001f4c4 logo.png  binary")
        self.assertEqual(lines[4], "\U0001f4c4 old.txt  +0 \u22121  " + DELETED)

    def test_small_mixed_change_shows_both_colors(self):
        text = format_changes([(1, 1, "a"), (100, 0, "b")])
        self.assertIn("+1 \u22121  " + ADDED + DELETED, text)

    def test_long_lists_are_truncated_and_empty_input_renders_nothing(self):
        rows = [(1, 0, f"f{i}") for i in range(MAX_FILES + 3)]
        self.assertTrue(format_changes(rows).endswith("\u2026 and 3 more file(s)"))
        self.assertEqual(format_changes([]), "")
        self.assertEqual(parse_numstat("garbage"), [])


if __name__ == "__main__":
    unittest.main()

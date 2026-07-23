from __future__ import annotations

import unittest

from scrape_hungary import _safe_error
from terminal_safety import terminal_line


class TerminalSafetyTests(unittest.TestCase):
    def test_terminal_line_removes_c0_c1_sequences_and_flattens_whitespace(self):
        rendered = terminal_line(
            "Tour\x1b]52;c;clipboard\x07\nnext\trow\x9b31m",
            limit=200,
        )

        self.assertEqual(rendered, "Tour]52;c;clipboard next row31m")
        self.assertFalse(any(ord(character) < 32 for character in rendered))
        self.assertNotIn("\x9b", rendered)

    def test_safe_error_cannot_emit_provider_terminal_controls(self):
        rendered = _safe_error(
            RuntimeError("remote\x1b[31m failure\x1b[0m\x9b2J\nsecond line")
        )

        self.assertEqual(
            rendered,
            "RuntimeError: remote[31m failure[0m2J second line",
        )
        self.assertNotIn("\x1b", rendered)
        self.assertNotIn("\x9b", rendered)


if __name__ == "__main__":
    unittest.main()

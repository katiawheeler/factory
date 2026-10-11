"""Hidden test for the word-count-whitespace case. Run from the repo root."""
import contextlib
import io
import unittest

from textstat import cli, core


class HiddenWordCountTest(unittest.TestCase):
    def test_runs_of_spaces_and_tabs(self):
        self.assertEqual(core.count_words("a  b  c"), 3)
        self.assertEqual(core.count_words("a\tb"), 2)

    def test_leading_trailing_and_newlines(self):
        self.assertEqual(core.count_words("  one\ntwo  \n"), 2)

    def test_empty(self):
        self.assertEqual(core.count_words(""), 0)
        self.assertEqual(core.count_words("   "), 0)

    def test_cli(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["words", "a  b  c"])
        self.assertEqual(out.getvalue().strip(), "3")


if __name__ == "__main__":
    unittest.main()

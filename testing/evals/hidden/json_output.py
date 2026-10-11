"""Hidden test for the json-output case. Run from the repo root."""
import contextlib
import io
import json
import os
import tempfile
import unittest

from textstat import cli, core

TEXT = "one two\nthree\n"


def run(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.main(list(argv))
    return code, out.getvalue()


class HiddenJsonTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".txt")
        with os.fdopen(fd, "w") as f:
            f.write(TEXT)
        self.addCleanup(os.unlink, self.path)

    def test_json_flag(self):
        code, out = run("stats", self.path, "--json")
        self.assertIn(code, (0, None))
        expected = core.stats(TEXT)
        self.assertEqual(json.loads(out), {"lines": expected["lines"], "words": expected["words"],
                                           "chars": expected["chars"]})

    def test_plain_output_unchanged(self):
        expected = core.stats(TEXT)
        _, out = run("stats", self.path)
        self.assertEqual(out.strip(), f"lines {expected['lines']} words {expected['words']} chars {expected['chars']}")


if __name__ == "__main__":
    unittest.main()

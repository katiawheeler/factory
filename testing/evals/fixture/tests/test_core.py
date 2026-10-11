import unittest

from textstat import core


class CountWordsTest(unittest.TestCase):
    def test_single_spaces(self):
        self.assertEqual(core.count_words("one two three"), 3)

    def test_one_word(self):
        self.assertEqual(core.count_words("hello"), 1)


class StatsTest(unittest.TestCase):
    def test_counts_lines_and_chars(self):
        result = core.stats("a b\n")
        self.assertEqual((result["lines"], result["chars"]), (1, 4))


if __name__ == "__main__":
    unittest.main()

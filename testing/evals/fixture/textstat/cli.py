import argparse

from . import core


def main(argv=None):
    parser = argparse.ArgumentParser(prog="textstat")
    commands = parser.add_subparsers(dest="command", required=True)
    words = commands.add_parser("words", help="count the words in TEXT")
    words.add_argument("text")
    stats = commands.add_parser("stats", help="count lines, words, and characters in FILE")
    stats.add_argument("file")
    args = parser.parse_args(argv)

    if args.command == "words":
        print(core.count_words(args.text))
    else:
        with open(args.file) as f:
            result = core.stats(f.read())
        print(f"lines {result['lines']} words {result['words']} chars {result['chars']}")
    return 0

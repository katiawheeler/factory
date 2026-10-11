def count_words(text):
    """Number of words in text."""
    return len(text.split(" "))


def stats(text):
    """Lines, words, and characters in text."""
    return {"lines": text.count("\n"), "words": count_words(text), "chars": len(text)}

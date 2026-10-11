# textstat

Small text statistics from the command line.

```sh
python3 -m textstat words "some text"     # number of words
python3 -m textstat stats notes.txt       # lines, words, and characters in a file
```

## Development

No dependencies beyond Python 3.9+. Run the tests with:

```sh
python3 -m unittest discover -s tests
```

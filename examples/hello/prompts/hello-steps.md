# A tiny example (steps H1, H2)

A Python package `hello` (standard library only) with `greet(name)` in `hello/greet.py` and its test in `tests/test_greet.py`. The tests run with `python3 -m unittest discover -s tests -v`.

## What is decided

- Tests use `unittest` (no pytest in the sandbox), one test class per module, in `tests/`.
- Every function has a one-line docstring.

## How to work

- Read only the files your step lists under "Read first", nothing else: everything you need is in them and in this prompt.
- Write the first file within your first ten tool calls. Write a long file in several edits, not in one call.
- Run the tests after each file you write.
- Never stop processes (`kill`, `pkill`): your shell shares the agent's process group, and a broad kill ends the agent itself.

## H1: slugify()

Read first: `hello/greet.py` (the docstring style), `tests/test_greet.py` (the test style).

Write `hello/text.py` with `slugify(text: str) -> str`:

1. Lower-case; letters with accents become their base letter (`ü` → `u`, `é` → `e`; use `unicodedata.normalize("NFKD", ...)` and drop the combining marks), `ß` becomes `ss`.
2. Every run of characters that are not `a-z` or `0-9` becomes one `-`; no `-` at the start or end.
3. Examples: `"Hello, World!"` → `"hello-world"`, `"  Grüße  aus Köln "` → `"grusse-aus-koln"`, `"***"` → `""`.

Tests in `tests/test_text.py`: the three examples, plus digits (`"Route 66"` → `"route-66"`).

## H2: a command line

Read first: `hello/greet.py` and `hello/text.py` (the two functions you call), `tests/test_text.py` (the test style).

`python3 -m hello NAME` prints the greeting and the slug of NAME: `python3 -m hello 'Ada Lovelace'` prints `Hello, Ada Lovelace! (ada-lovelace)`. Without a name it prints `usage: python3 -m hello NAME` to stderr and exits with code 2.

Write it in `hello/__main__.py` (use `greet` and `slugify`; a `main(argv) -> int` function that the module calls, so the tests can call it), tests in `tests/test_main.py` (the output for a name via `contextlib.redirect_stdout`, and the exit code without one).

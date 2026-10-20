#!/usr/bin/env bash
# Create the example's repository: a tiny Python package with one passing
# test, as the base the plan's steps build on.
#   examples/hello/make-repo.sh [DIR]      (default: ~/qwen-hello)
set -euo pipefail
dir=${1:-$HOME/qwen-hello}
[ -e "$dir" ] && { echo "$dir exists"; exit 1; }
mkdir -p "$dir/hello" "$dir/tests"
cd "$dir"
git init -q -b main
printf '__pycache__/\n' > .gitignore
: > hello/__init__.py
cat > hello/greet.py <<'PY'
"""Greetings."""


def greet(name: str) -> str:
    """A greeting for name."""
    return f"Hello, {name}!"
PY
: > tests/__init__.py
cat > tests/test_greet.py <<'PY'
import unittest

from hello.greet import greet


class GreetTest(unittest.TestCase):
    def test_greet(self):
        self.assertEqual(greet("Ada"), "Hello, Ada!")


if __name__ == "__main__":
    unittest.main()
PY
git add -A
git -c user.name=example -c user.email=example@example.invalid commit -q -m "The base: greet() with a test"
echo "created $dir (branch main)"

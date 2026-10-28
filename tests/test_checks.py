"""Table-driven tests for checks.masked_pipes / failure_line / find_brief_paths."""
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "delegate" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import checks  # noqa: E402


class MaskedPipesTests(unittest.TestCase):
    def test_warns(self):
        cases = [
            ("a | tail -5", ["tail -5"]),
            ("a 2>&1 | tail -5 && b", ["tail -5"]),
            ("a |& tail", ["tail"]),
            ("( a | tail )", ["tail"]),
            ("a | /usr/bin/tail -n3", ["/usr/bin/tail -n3"]),
            ("a | LC_ALL=C sort", ["LC_ALL=C sort"]),
            ("a | tail -1; b | head -2", ["tail -1", "head -2"]),
            # Quoted stage words: shlex tokenizes before basename.
            ('false | X="two words" tail -1', ['X="two words" tail -1']),
            ('false | "/usr/bin/tail" -1', ['"/usr/bin/tail" -1']),
            # Continued pipelines: newline after |, and backslash-newline join.
            ("false |\n  tail -1", ["tail -1"]),
            ("false |&\n  tail -1", ["tail -1"]),
            ("false \\\n | tail -1", ["tail -1"]),
            # >| is clobber redirection, not a pipe.
            ("false | tail -1 >| /dev/null", ["tail -1 >| /dev/null"]),
            # Escaped quote inside double quotes does not end the string early.
            (r'''printf '%s\n' "\""; false | tail -1''', ["tail -1"]),
        ]
        for cmd, expected in cases:
            with self.subTest(cmd=cmd):
                self.assertEqual(checks.masked_pipes(cmd), expected)

    def test_quiet(self):
        cases = [
            "a || b",
            "set -o pipefail; a | tail -5",
            "bash -o pipefail -c 'a | tail'",
            "a | grep -q x",
            "a 2>&1 | tee /dev/stderr | grep -q '^0 failure(s)$'",
            "echo 'a | tail'",
            'echo "a | tail"',
            r"echo a \| tail",
            'x=$(a | tail -1); test "$x" = ok',
            "x=`a | tail -1`",
            "a # | tail",
            # Pipe only inside a double-quoted escaped fragment stays quiet.
            r'''printf '%s\n' "\" | tail -1"''',
        ]
        for cmd in cases:
            with self.subTest(cmd=cmd):
                self.assertEqual(checks.masked_pipes(cmd), [])

    def test_stage_name_uses_quote_aware_words(self):
        self.assertEqual(checks.stage_name('X="two words" tail -1'), "tail")
        self.assertEqual(checks.stage_name('"/usr/bin/tail" -1'), "tail")
        self.assertEqual(checks.stage_name("LC_ALL=C sort"), "sort")

    def test_double_quote_escape_invariant(self):
        """Inside \"…\", backslash escapes the next char; inside '…' it does not."""
        # A pipe after a properly closed \"…\" string is still a pipe.
        self.assertEqual(
            checks.masked_pipes(r'''echo "\""; false | head -1'''), ["head -1"])
        # A pipe character that is only inside the escaped double-quoted string is not.
        self.assertEqual(checks.masked_pipes(r'''echo "foo \" | tail -1"'''), [])
        # Single quotes: backslash is literal, so \" does not protect a later pipe outside.
        self.assertEqual(checks.masked_pipes(r"""echo '\'; false | tail -1"""), ["tail -1"])

    def test_continued_pipeline_invariant(self):
        """Newline after | / |& continues; backslash-newline outside ' joins lines."""
        for cmd in ("false |\n\ttail -1", "false |  \n  tail -1", "false |&\n tail -1"):
            with self.subTest(cmd=cmd):
                self.assertEqual(checks.masked_pipes(cmd), ["tail -1"])
        # Backslash-newline before the pipe joins onto one logical line.
        self.assertEqual(checks.masked_pipes("false \\\n| tail -1"), ["tail -1"])
        # A bare newline (not after a pipe) still separates lists.
        self.assertEqual(checks.masked_pipes("false\ntail -1"), [])

    def test_clobber_redirect_is_not_a_pipe(self):
        """`>|` is Bash clobber redirection; it must not split a stage."""
        self.assertEqual(
            checks.masked_pipes("false | tee out >| /dev/null"),
            ["tee out >| /dev/null"])
        self.assertEqual(checks.stage_name("tee out >| /dev/null"), "tee")


class FailureLineTests(unittest.TestCase):
    def test_hits(self):
        cases = [
            "FAILED (failures=1)",
            "FAIL: test_x (m.T)",
            "ERROR: test_y",
            "1 failed, 4 passed in 0.3s",
            "3 failure(s)",
            "Traceback (most recent call last):",
            "not ok 3 - x",
            "AssertionError: boom",
        ]
        for line in cases:
            with self.subTest(line=line):
                self.assertEqual(checks.failure_line(line), line)

    def test_misses(self):
        cases = [
            "0 failure(s)",
            "0 errors",
            "Ran 5 tests in 0.1s\n\nOK",
            "5 passed in 0.2s",
            "$ check.py | grep -q '^0 failure(s)$'",
            # Every physical echoed command line is `$ `-prefixed.
            "$ printf 'ok\\n'\n$ # 3 failure(s)\nok\n",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertIsNone(checks.failure_line(text))

    def test_long_line_cut_to_160(self):
        line = "FAILED " + ("x" * 200)
        hit = checks.failure_line(line)
        self.assertEqual(len(hit), 160)
        self.assertTrue(hit.startswith("FAILED "))


class FindBriefPathsTests(unittest.TestCase):
    def test_kinds_and_dedupe(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            repo.mkdir()
            (repo / "tracked.txt").write_text("x\n")
            text = (
                f"see `{repo}/tracked.txt` and /tmp/claude-1000/sess/shot.png. "
                f"also ~/elsewhere/x https://example.org/a/b and/or "
                f"again {repo}/tracked.txt and root {repo}"
            )
            got = checks.find_brief_paths(text, repo)
            self.assertEqual(got[0], (f"{repo}/tracked.txt", "checkout", "tracked.txt"))
            self.assertEqual(got[1], ("/tmp/claude-1000/sess/shot.png", "scratchpad", ""))
            self.assertEqual(got[2], (str(repo), "checkout", "."))
            self.assertEqual(len(got), 3)
            # outside repo / URL / and/or produce nothing extra
            self.assertFalse(any("elsewhere" in p or "example.org" in p or p == "and/or"
                                 for p, _, _ in got))

    def test_empty(self):
        self.assertEqual(checks.find_brief_paths("", "/tmp/repo"), [])
        self.assertEqual(checks.find_brief_paths(None or "", "/tmp/repo"), [])


if __name__ == "__main__":
    unittest.main()

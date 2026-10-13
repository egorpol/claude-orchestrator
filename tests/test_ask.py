"""Tests for ask.py with fake cursor-agent and codex on PATH."""
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "delegate" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import ask  # noqa: E402


CURSOR_SH = r'''#!/bin/sh
# fake cursor-agent: ignore args; emit a result JSON object
printf '%s\n' '{"result":"answer from cursor: path:1 says hi","usage":{"inputTokens":11,"outputTokens":7,"cacheReadTokens":100,"cacheWriteTokens":0},"duration_ms":1200}'
'''

CODEX_SH = r'''#!/bin/sh
# fake codex: read stdin (required), write -o file, emit turn.completed on stdout
out=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    -) shift ;;
    *) shift ;;
  esac
done
# consume stdin so a real-style block would not hang
cat >/dev/null
if [ -n "$out" ]; then
  printf '%s\n' 'codex says see foo.py:3' > "$out"
fi
printf '%s\n' '{"type":"turn.completed","usage":{"input_tokens":17140,"cached_input_tokens":6912,"output_tokens":42,"reasoning_output_tokens":10}}'
'''


class TestAsk(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bindir = Path(self.tmp.name) / "bin"
        self.bindir.mkdir()
        self.workspace = Path(self.tmp.name) / "ws"
        self.workspace.mkdir()
        (self.workspace / "foo.py").write_text("x = 1\n", encoding="utf-8")
        self._write_exe("cursor-agent", CURSOR_SH)
        self._write_exe("codex", CODEX_SH)
        self.old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{self.bindir}:{self.old_path}"

    def tearDown(self):
        os.environ["PATH"] = self.old_path
        self.tmp.cleanup()

    def _write_exe(self, name, body):
        p = self.bindir / name
        p.write_text(body, encoding="utf-8")
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def test_build_prompt(self):
        p = ask.build_prompt("What is x?", 50, ["foo.py"])
        self.assertIn("at most 50 words", p)
        self.assertIn("path:line", p)
        self.assertIn("not found", p)
        self.assertIn("read-only", p)
        self.assertIn("- foo.py", p)
        self.assertIn("What is x?", p)

    def test_cursor_ask(self):
        ns = argparse_ns(
            question="What is x?", agent="cursor", model=None, effort=None,
            dir=str(self.workspace), max_words=200, timeout=30, files=["foo.py"],
        )
        from io import StringIO
        buf = StringIO()
        with mock.patch("sys.stdout", buf):
            ask.cmd_ask(ns)
        out = buf.getvalue()
        self.assertIn("answer from cursor", out)
        self.assertIn("(cursor auto ·", out)
        self.assertIn("in=11", out)
        self.assertIn("out=7", out)
        self.assertIn("cached=100", out)

    def test_codex_ask(self):
        ns = argparse_ns(
            question="What is x?", agent="codex", model=None, effort=None,
            dir=str(self.workspace), max_words=100, timeout=30, files=None,
        )
        from io import StringIO
        buf = StringIO()
        with mock.patch("sys.stdout", buf):
            ask.cmd_ask(ns)
        out = buf.getvalue()
        self.assertIn("foo.py:3", out)
        self.assertIn("(codex gpt-6.1-sol ·", out)
        # input = 17140 - 6912 = 10228
        self.assertIn("in=10228", out)
        self.assertIn("out=42", out)
        self.assertIn("cached=6912", out)

    def test_cursor_failure_prints_stderr_tail(self):
        self._write_exe("cursor-agent", "#!/bin/sh\necho line1 >&2; echo line2 >&2; exit 3\n")
        ns = argparse_ns(
            question="x", agent="cursor", model="auto", effort=None,
            dir=str(self.workspace), max_words=200, timeout=30, files=None,
        )
        from io import StringIO
        err = StringIO()
        with mock.patch("sys.stderr", err):
            with self.assertRaises(SystemExit) as cm:
                ask.cmd_ask(ns)
        self.assertEqual(cm.exception.code, 3)
        self.assertIn("line2", err.getvalue())

    def _both_ns(self, **kw):
        base = dict(question="Pick A or B?", agent="both", model=None, effort=None,
                    dir=str(self.workspace), max_words=100, timeout=30, files=None)
        base.update(kw)
        return argparse_ns(**base)

    def test_both_prints_one_block_per_agent(self):
        from io import StringIO
        buf = StringIO()
        with mock.patch("sys.stdout", buf):
            ask.cmd_ask(self._both_ns())
        out = buf.getvalue()
        self.assertLess(out.index("## codex"), out.index("## cursor"))
        self.assertIn("foo.py:3", out)
        self.assertIn("answer from cursor", out)
        self.assertIn("(codex gpt-6.1-sol ·", out)
        self.assertIn("(cursor auto ·", out)

    def test_both_keeps_the_other_answer_on_failure(self):
        self._write_exe("cursor-agent", "#!/bin/sh\necho quota exhausted >&2; exit 3\n")
        from io import StringIO
        buf = StringIO()
        with mock.patch("sys.stdout", buf):
            with self.assertRaises(SystemExit) as cm:
                ask.cmd_ask(self._both_ns())
        self.assertEqual(cm.exception.code, 3)
        out = buf.getvalue()
        self.assertIn("foo.py:3", out)
        self.assertIn("(failed, exit 3) quota exhausted", out)

    def test_both_rejects_model(self):
        with self.assertRaises(SystemExit) as cm:
            ask.cmd_ask(self._both_ns(model="auto"))
        self.assertIn("single --agent", str(cm.exception))

    def test_never_passes_force(self):
        seen = []

        def fake_run(cmd, **kwargs):
            seen.append(cmd)
            class R:
                returncode = 0
                stdout = json.dumps({"result": "ok", "usage": {}})
                stderr = ""
            return R()

        with mock.patch("ask.subprocess.run", side_effect=fake_run):
            ask.run_cursor("q", "auto", self.workspace, 10)
        self.assertTrue(seen)
        self.assertNotIn("--force", seen[0])
        self.assertIn("--mode", seen[0])
        self.assertIn("ask", seen[0])


def argparse_ns(**kwargs):
    return type("NS", (), kwargs)()


if __name__ == "__main__":
    unittest.main()

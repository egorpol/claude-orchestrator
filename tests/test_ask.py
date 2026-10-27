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
import frontier  # noqa: E402


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
        self.session_log = Path(self.tmp.name) / "sessions.jsonl"
        env = mock.patch.dict(os.environ, {"CLAUDE_SESSION_LOG": str(self.session_log)})
        env.start()
        self.addCleanup(env.stop)
        # Both now checks quota. Never consult the machine's real openusage.
        fetch = mock.patch("ask.quota.fetch", return_value={})
        pools = mock.patch("ask.quota.pools", return_value={
            "codex-5h": {"left_pct": 50}, "codex-7d": {"left_pct": 50}})
        self.quota_fetch, self.quota_pools = fetch.start(), pools.start()
        self.addCleanup(fetch.stop)
        self.addCleanup(pools.stop)
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)  # set when the tests run inside Claude Code
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

    def test_ask_logs_a_delegate_row_under_the_calling_session(self):
        ns = argparse_ns(
            question="What is x?", agent="cursor", model=None, effort=None,
            dir=str(self.workspace), max_words=100, timeout=30, files=None,
        )
        from io import StringIO
        with mock.patch("sys.stdout", StringIO()):
            ask.cmd_ask(ns)
            self.assertFalse(self.session_log.exists())  # no Claude session: nothing to attribute
            os.environ["CLAUDE_CODE_SESSION_ID"] = "parent"
            ask.cmd_ask(ns)
        rows = [json.loads(line) for line in self.session_log.read_text().splitlines()]
        self.assertEqual([{k: r[k] for k in ("event", "session", "command", "agent")} for r in rows],
                         [{"event": "delegate", "session": "parent", "command": "ask", "agent": "cursor"}])

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

    def _ask_both_with(self, answers):
        """answers: agent -> answer str, or AskFailed to fail that agent."""
        from io import StringIO

        def fake_run(agent, prompt, a, directory):
            val = answers[agent]
            if isinstance(val, ask.AskFailed):
                raise val
            model = "gpt-6.1-sol" if agent == "codex" else "auto"
            return val, {"input": 1, "output": 1, "cached_input": 0}, 0.5, model

        buf = StringIO()
        with mock.patch("ask._run_one", side_effect=fake_run), mock.patch("sys.stdout", buf):
            ask.cmd_ask(self._both_ns())
        return buf.getvalue()

    def test_both_picks_from_markdown_wrapped_pick_after_preamble(self):
        out = self._ask_both_with({
            "codex": "Here is my reasoning.\n\n**Pick:** B, keep the 10 KB cap\nWhy: smaller",
            "cursor": "I prefer A.\n- Pick: C\nWhy: clearer",
        })
        self.assertIn("## picks", out)
        self.assertIn("Pick (codex): B, keep the 10 KB cap", out)
        self.assertIn("Pick (cursor): C", out)
        from io import StringIO
        buf = StringIO()
        with mock.patch("sys.stdout", buf):
            ask.cmd_ask(argparse_ns(
                question="x", agent="cursor", model=None, effort=None,
                dir=str(self.workspace), max_words=100, timeout=30, files=None))
        self.assertNotIn("## picks", buf.getvalue())

    def test_both_picks_heading_form_and_missing_and_failed(self):
        out = self._ask_both_with({
            "codex": "### Pick: A\nWhy: first",
            "cursor": "no pick here",
        })
        self.assertIn("Pick (codex): A", out)
        self.assertIn("Pick (cursor): (no Pick line found)", out)

        out = self._ask_both_with({"codex": "no structured pick", "cursor": "also none"})
        self.assertNotIn("## picks", out)

        from io import StringIO
        buf = StringIO()
        with mock.patch("ask._run_one", side_effect=lambda ag, *a, **k: (
                (_ for _ in ()).throw(ask.AskFailed("boom", 3)) if ag == "cursor"
                else ("Pick: B\n", {"input": 1, "output": 1, "cached_input": 0}, 0.5, "gpt-6.1-sol"))), \
             mock.patch("sys.stdout", buf):
            with self.assertRaises(SystemExit) as cm:
                ask.cmd_ask(self._both_ns())
        self.assertEqual(cm.exception.code, 3)
        out = buf.getvalue()
        self.assertIn("## picks", out)
        self.assertIn("Pick (codex): B", out)
        self.assertIn("Pick (cursor): (failed)", out)

    def test_extract_pick_helpers(self):
        self.assertEqual(ask.extract_pick("preamble\n**Pick:** B, keep\n"), "B, keep")
        self.assertEqual(ask.extract_pick("- Pick: C"), "C")
        self.assertEqual(ask.extract_pick("### Pick: A"), "A")
        self.assertIsNone(ask.extract_pick("nope\nPick me\n"))
        # Cursor's result joins its preamble message and the answer without a newline.
        self.assertEqual(ask.extract_pick("I'll read the doc first.**Pick: A**\n\n**Why:** x"), "A")
        self.assertEqual(ask.extract_pick("what's already decided in-repo.Pick: C  \nWhy: y"), "C")
        self.assertIsNone(ask.extract_pick("the best pick: none of them"))

    def _fallback_report(self, catalog, default_codex):
        def rating(name, intelligence, cost, creator="OpenAI"):
            return {"name": name, "intelligence": intelligence, "cost_per_task": cost,
                    "price_blended": 1, "creator": creator}
        models = {
            "gpt-6-1-sol-medium": rating("GPT-6.1 Sol (Medium)", 47.8, 0.2135),
            "gpt-6-1-sol-high": rating("GPT-6.1 Sol (High)", 50.2, 0.319),
            "deepseek-v4-1-flash-1017": rating("DeepSeek V4.1 Flash 1017 (Max)", 45.1, 0.262, "DeepSeek"),
            "better": rating("Better (High)", 49, 0.55),
            "claude": rating("Claude (High)", 90, 0.1, "Anthropic"),
            "grok": rating("Grok (High)", 91, 0.1, "xAI"),
        }
        return frontier.build_report(catalog, {"source": "fake", "fetched_at": 1, "models": models},
                                     ["auto", "deepseek-v41-flash-1017-max", "better-high", "claude-high", "grok-high"],
                                     {}, default_codex)

    def _run_fallback_both(self, **kw):
        from io import StringIO
        out = StringIO()
        result = ("Pick: A\nWhy: good", {"input": 1, "output": 2}, 0.1, "fake")
        with mock.patch("frontier.load_report", side_effect=self._fallback_report) as load, \
             mock.patch("ask.run_cursor", return_value=result) as cursor, \
             mock.patch("ask.run_codex", return_value=result) as codex, \
             mock.patch("sys.stdout", out):
            ask.cmd_ask(self._both_ns(**kw))
        return out.getvalue(), cursor, codex, load

    def test_both_normal_only_reads_quota(self):
        out, cursor, codex, load = self._run_fallback_both()
        self.quota_fetch.assert_called_once_with("auto", timeout=15)
        load.assert_not_called()
        codex.assert_called_once()
        self.assertEqual(cursor.call_args.args[1], "auto")
        self.assertNotIn("fallback", out)

    def test_both_empty_switches_and_labels_both_blocks_and_picks(self):
        self.quota_pools.return_value = {"codex-5h": {"left_pct": 3}, "codex-7d": {"left_pct": 80}}
        out, cursor, codex, load = self._run_fallback_both()
        codex.assert_not_called()
        self.assertEqual(sorted(c.args[1] for c in cursor.call_args_list), ["auto", "deepseek-v41-flash-1017-max"])
        line = out.splitlines()[0]
        for bit in ("codex-5h 3% left", "10% threshold", "deepseek-v41-flash-1017-max", "AA 45.1",
                    "$0.262/task", "cap $0.427/task", "2x gpt-6.1-sol@medium", "$0.213/task", "bills cursor-premium?"):
            self.assertIn(bit, line)
        self.assertIn("## fallback (cursor deepseek-v41-flash-1017-max)", out)
        self.assertIn("Pick (fallback): A", out)
        self.assertIn("## cursor", out)
        self.assertIn("Pick (cursor): A", out)
        self.assertLess(out.index("ask:"), out.index("## fallback"))

    def test_7d_alone_empty_and_effort_high_changes_the_cap(self):
        self.quota_pools.return_value = {"codex-5h": {"left_pct": 80}, "codex-7d": {"left_pct": 9}}
        out, cursor, codex, load = self._run_fallback_both(effort="high")
        codex.assert_not_called()
        self.assertIn("codex-7d 9% left", out)
        self.assertIn("cap $0.638/task = 2x gpt-6.1-sol@high $0.319/task", out)
        self.assertIn("## fallback (cursor better-high)", out)
        self.assertEqual(load.call_args.kwargs["default_codex"], ("gpt-6.1-sol", "high"))

    def test_empty_with_no_candidate_or_task_cost_keeps_codex(self):
        self.quota_pools.return_value = {"codex-5h": {"left_pct": 0}, "codex-7d": {"left_pct": 50}}
        original = self._fallback_report
        for task in (0.001, None):
            def no_fit(catalog, default_codex):
                report = original(catalog, default_codex)
                for row in report["runnable"]:
                    if row["agent"] == "codex":
                        row["cost_per_task"] = task
                report["fallback"], report["fallback_reason"] = frontier.pick_fallback(report["runnable"], *default_codex)
                return report
            with mock.patch.object(self, "_fallback_report", side_effect=no_fit):
                out, cursor, codex, load = self._run_fallback_both()
            codex.assert_called_once()
            self.assertEqual(cursor.call_count, 1)
            self.assertIn("keeping Codex", out)
            self.assertIn("no eligible" if task else "no AA $/task", out)
            self.assertNotIn("## fallback", out)

    def test_unknown_quota_failure_missing_or_error_keeps_codex(self):
        for pools in ({}, {"codex-5h": {"error": "missing"}, "codex-7d": {"left_pct": 50}},
                      {"codex-5h": {"error": "failed", "left_pct": 0}, "codex-7d": {"left_pct": 50}},
                      {"codex-5h": {"left_pct": 50}}):
            self.quota_pools.return_value = pools
            out, cursor, codex, load = self._run_fallback_both()
            self.assertIn("quota unknown", out)
            codex.assert_called_once()
            load.assert_not_called()
        self.quota_fetch.side_effect = FileNotFoundError("openusage missing")
        out, cursor, codex, load = self._run_fallback_both()
        self.assertIn("quota unknown", out)
        codex.assert_called_once()
        load.assert_not_called()

    def test_quota_threshold_is_strict_and_comes_from_catalog(self):
        self.quota_pools.return_value = {"codex-5h": {"left_pct": 10}, "codex-7d": {"left_pct": 10}}
        out, cursor, codex, load = self._run_fallback_both()
        load.assert_not_called()
        codex.assert_called_once()
        Path(self.tmp.name, "catalog.json").write_text(json.dumps({"min_quota_pct": 15, "delegates": []}))
        with mock.patch.object(ask, "SCRIPTS", Path(self.tmp.name)):
            out, cursor, codex, load = self._run_fallback_both()
        codex.assert_not_called()
        self.assertIn("15% threshold", out)

    def test_quota_fetch_timeout_keeps_codex_and_prints_one_unknown_line(self):
        self.quota_fetch.side_effect = ask.subprocess.TimeoutExpired(["openusage", "export"], 15)
        out, cursor, codex, load = self._run_fallback_both()
        self.quota_fetch.assert_called_once_with("auto", timeout=15)
        self.quota_pools.assert_not_called()
        load.assert_not_called()
        codex.assert_called_once()
        self.assertEqual(cursor.call_count, 1)
        notices = [line for line in out.splitlines() if line.startswith("ask:")]
        self.assertEqual(len(notices), 1)
        self.assertIn("quota unknown", notices[0])
        self.assertIn("keeping Codex", notices[0])
        self.assertIn("## codex", out)
        self.assertNotIn("## fallback", out)

    def test_no_fallback_skips_quota_even_when_empty(self):
        self.quota_fetch.side_effect = AssertionError("quota read")
        out, cursor, codex, load = self._run_fallback_both(no_fallback=True)
        codex.assert_called_once()
        load.assert_not_called()
        self.quota_fetch.assert_not_called()
        self.assertNotIn("ask:", out)

    def test_fallback_discovery_failure_keeps_codex(self):
        self.quota_pools.return_value = {"codex-5h": {"left_pct": 0}}
        with mock.patch.object(self, "_fallback_report", side_effect=RuntimeError("AA unavailable")):
            out, cursor, codex, load = self._run_fallback_both()
        self.assertIn("fallback ratings unavailable (AA unavailable)", out)
        codex.assert_called_once()

    def test_single_agents_never_read_quota(self):
        from io import StringIO
        self.quota_fetch.side_effect = AssertionError("quota read")
        for agent in ("cursor", "codex"):
            with mock.patch("sys.stdout", StringIO()):
                ask.cmd_ask(self._both_ns(agent=agent))
        self.quota_fetch.assert_not_called()

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

"""Tests for ask.py with fake cursor-agent and codex on PATH."""
import json
import os
import signal
import stat
import sys
import tempfile
import time
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
        env = mock.patch.dict(os.environ, {"CLAUDE_SESSION_LOG": str(self.session_log),
                                           "DELEGATE_CATALOG_LOCAL": ""})
        env.start()
        self.addCleanup(env.stop)
        # Both now checks quota. Never consult the machine's real openusage.
        fetch = mock.patch("ask.quota.fetch", return_value={})
        pools = mock.patch("ask.quota.pools", return_value={
            "codex-5h": {"left_pct": 50, "age_min": 0}, "codex-7d": {"left_pct": 50, "age_min": 0}})
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
        self.assertEqual(p, "\n".join([
            "Answer in at most 50 words.",
            "Cite path:line for claims.",
            "Cite tracked files or the question itself, not working notes such as HANDOFF.md that may change while you answer.",
            'Say "not found" rather than guess.',
            "This is read-only; do not modify, create or delete any files.",
            "Focus on these files:",
            "- foo.py",
            "",
            "What is x?",
        ]))

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

    def test_both_stale_quota_refreshes_before_selecting_fallback(self):
        self.quota_pools.side_effect = [
            {"codex-5h": {"left_pct": 80, "age_min": 16},
             "codex-7d": {"left_pct": 80, "age_min": 16}},
            {"codex-5h": {"left_pct": 1, "age_min": 0},
             "codex-7d": {"left_pct": 80, "age_min": 0}},
        ]
        out, cursor, codex, load = self._run_fallback_both()
        self.assertEqual(self.quota_fetch.call_args_list,
                         [mock.call("auto", timeout=15), mock.call("direct", timeout=30)])
        codex.assert_not_called()
        load.assert_called_once()
        self.assertIn("codex-5h 1% left", out)
        self.assertEqual(cursor.call_count, 2)

    def test_both_empty_switches_and_labels_both_blocks_and_picks(self):
        self.quota_pools.return_value = {"codex-5h": {"left_pct": 3, "age_min": 0}, "codex-7d": {"left_pct": 80, "age_min": 0}}
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
        self.quota_pools.return_value = {"codex-5h": {"left_pct": 80, "age_min": 0}, "codex-7d": {"left_pct": 9, "age_min": 0}}
        out, cursor, codex, load = self._run_fallback_both(effort="high")
        codex.assert_not_called()
        self.assertIn("codex-7d 9% left", out)
        self.assertIn("cap $0.638/task = 2x gpt-6.1-sol@high $0.319/task", out)
        self.assertIn("## fallback (cursor better-high)", out)
        self.assertEqual(load.call_args.kwargs["default_codex"], ("gpt-6.1-sol", "high"))

    def test_empty_with_no_candidate_or_task_cost_keeps_codex(self):
        self.quota_pools.return_value = {"codex-5h": {"left_pct": 0, "age_min": 0}, "codex-7d": {"left_pct": 50, "age_min": 0}}
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
        for pools in ({}, {"codex-5h": {"error": "missing", "age_min": 0}, "codex-7d": {"left_pct": 50, "age_min": 0}},
                      {"codex-5h": {"error": "failed", "left_pct": 0, "age_min": 0}, "codex-7d": {"left_pct": 50, "age_min": 0}},
                      {"codex-5h": {"left_pct": 50, "age_min": 0}}):
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
        self.quota_pools.return_value = {"codex-5h": {"left_pct": 10, "age_min": 0}, "codex-7d": {"left_pct": 10, "age_min": 0}}
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
        self.quota_pools.return_value = {"codex-5h": {"left_pct": 0, "age_min": 0}}
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


class TestConfinedCursor(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="confined-ask-test-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.bindir = self.root / "bin"
        self.bindir.mkdir()
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        env = mock.patch.dict(os.environ, {"PATH": f"{self.bindir}:{os.environ.get('PATH', '')}"})
        env.start()
        self.addCleanup(env.stop)
        self.result = {"type": "result", "is_error": False, "result": " final answer ",
                       "usage": {"inputTokens": 11, "outputTokens": 7, "cacheReadTokens": 100}}

    _write_exe = TestAsk._write_exe

    def fake_cursor(self, events=(), before="", after="", raw=None):
        body = f"#!{sys.executable}\nimport json, os, signal, sys, time\nfrom pathlib import Path\n"
        body += before + "\n"
        if raw is None:
            body += f"for event in {events!r}:\n    print(json.dumps(event), flush=True)\n"
        else:
            body += f"sys.stdout.buffer.write({raw!r})\nsys.stdout.buffer.flush()\n"
        self._write_exe("cursor-agent", body + after + "\n")

    def run_cursor(self, timeout=10):
        return ask.run_cursor("question", "auto", self.workspace, timeout, confine=True)

    def tool_event(self, kind, args, subtype="started"):
        return {"type": "tool_call", "subtype": subtype, "tool_call": {
            kind: {"args": args}, "toolCallId": "call-1", "startedAtMs": "1",
            "completedAtMs": "2", "hookAdditionalContexts": []}}

    def glob_completed(self, args, files, path=""):
        event = self.tool_event("globToolCall", args, "completed")
        event["tool_call"]["globToolCall"]["result"] = {"success": {
            "pattern": "", "path": path, "files": files, "totalFiles": len(files),
            "clientTruncated": False, "ripgrepTruncated": False}}
        return event

    def assert_violation(self, event, kind, path=None):
        self.fake_cursor([event, self.result])
        with self.assertRaises(ask.OutsideRead) as cm:
            self.run_cursor()
        self.assertEqual(cm.exception.code, 4)
        self.assertIsInstance(cm.exception, ask.AskFailed)
        self.assertIn(kind, str(cm.exception))
        if path is not None:
            self.assertIn(path, str(cm.exception))
        return str(cm.exception)

    def test_no_tools_uses_stream_command_clean_env_and_result(self):
        record = self.root / "command.json"
        before = (f"Path({str(record)!r}).write_text(json.dumps({{'argv': sys.argv[1:], "
                  "'cwd': os.getcwd(), 'ide': os.environ.get('CURSOR_GUARD_TEST'), "
                  "'pid': os.getpid(), 'pgid': os.getpgrp(), 'sid': os.getsid(0)}))")
        events = [{"type": t} for t in ("system", "user", "thinking", "assistant")]
        events += [{"type": "result", "result": "earlier answer"}, self.result,
                   {"type": "assistant", "result": "not the result"}]
        self.fake_cursor(events, before=before)
        with mock.patch.dict(os.environ, {"CURSOR_GUARD_TEST": "scrub me"}):
            answer, usage, elapsed, model = self.run_cursor()
        self.assertEqual((answer, usage, model),
                         ("final answer", {"input": 11, "output": 7, "cached_input": 100}, "auto"))
        self.assertGreaterEqual(elapsed, 0)
        command = json.loads(record.read_text())
        self.assertEqual(command["argv"], ["-p", "--model", "auto", "--mode", "ask", "--trust",
                                           "--workspace", str(self.workspace),
                                           "--output-format", "stream-json", "question"])
        self.assertNotIn("--force", command["argv"])
        self.assertEqual(command["cwd"], str(self.workspace))
        self.assertIsNone(command["ide"])
        self.assertEqual(command["pgid"], command["pid"])
        self.assertEqual(command["sid"], command["pid"])

    def test_inside_tools_pass_on_started_and_completed(self):
        (self.workspace / "sub").mkdir()
        (self.workspace / "sub/a.txt").write_text("inside")
        (self.workspace / "link").symlink_to(self.workspace / "sub", target_is_directory=True)
        tools = [("readToolCall", {"path": str(self.workspace / "sub/a.txt")}),
                 ("readToolCall", {"path": "sub/a.txt"}),
                 ("readToolCall", {"path": "link/a.txt"}),
                 ("grepToolCall", {"path": str(self.workspace), "pattern": "x"}),
                 ("grepToolCall", {"path": ".", "pattern": "x"}),
                 ("globToolCall", {"targetDirectory": str(self.workspace), "globPattern": "**/*"}),
                 ("globToolCall", {"targetDirectory": "sub", "globPattern": "a.txt"}),
                 ("getMcpToolsToolCall", {}),
                 ("getMcpToolsToolCall", {"pattern": "semantic|search|codebase"}),
                 ("getMcpToolsToolCall", {"server": "cursor"})]
        events = []
        for kind, args in tools:
            events.append(self.tool_event(kind, args))
            events.append(self.glob_completed(args, []) if kind == "globToolCall"
                          else self.tool_event(kind, args, "completed"))
        self.fake_cursor(events + [self.result])
        self.assertEqual(self.run_cursor()[0], "final answer")

    def test_read_escape_paths_fail_on_both_subtypes(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "a.txt").write_text("outside")
        (self.workspace / "escape").symlink_to(outside, target_is_directory=True)
        paths = [str(outside / "a.txt"), str(self.root / "ws-evil/a"),
                 str(self.workspace) + "/../outside/a.txt", "../outside/a.txt",
                 str(self.workspace / "escape/a.txt"), "~/secret", "~other/secret"]
        for path in paths:
            for subtype in ("started", "completed"):
                with self.subTest(path=path, subtype=subtype):
                    self.assert_violation(self.tool_event("readToolCall", {"path": path}, subtype),
                                          "readToolCall", path)

    def test_missing_and_invalid_read_paths_fail(self):
        for path in (None, "", 7, [], {}, "a\0b"):
            with self.subTest(path=path):
                self.assert_violation(self.tool_event("readToolCall", {"path": path}), "readToolCall")
        self.assert_violation(self.tool_event("readToolCall", {}), "readToolCall")

    def test_grep_outside_missing_and_empty_paths_fail(self):
        for args in ({"path": str(self.root), "pattern": "x"}, {"pattern": "x"},
                     {"path": ""}, {"path": None}):
            for subtype in ("started", "completed"):
                with self.subTest(args=args, subtype=subtype):
                    self.assert_violation(self.tool_event("grepToolCall", args, subtype), "grepToolCall")

    def test_glob_targets_and_patterns_fail_closed(self):
        bad_args = [{"targetDirectory": str(self.root), "globPattern": "**/*"},
                    {"targetDirectory": "."}]
        bad_args += [{"targetDirectory": path, "globPattern": "*"} for path in ("", None, 7, [])]
        bad_args += [{"targetDirectory": ".", "globPattern": pattern} for pattern in
                     ("/outside/*", "~/secret/*", "../*", "sub/../*", "sub/..", None, [], "a\0b")]
        for args in bad_args:
            for subtype in ("started", "completed"):
                with self.subTest(args=args, subtype=subtype):
                    self.assert_violation(self.tool_event("globToolCall", args, subtype), "globToolCall")

    def test_glob_without_target_directory_passes_observed_result(self):
        (self.workspace / "sub").mkdir()
        (self.workspace / "sub/inside.txt").write_text("inside")
        args = {"globPattern": "**/inside.txt"}
        self.fake_cursor([self.tool_event("globToolCall", args),
                          self.glob_completed(args, ["./sub/inside.txt"]), self.result])
        self.assertEqual(self.run_cursor()[0], "final answer")

    def test_glob_without_target_directory_passes_no_matches(self):
        args = {"globPattern": "**/inside.txt"}
        self.fake_cursor([self.tool_event("globToolCall", args),
                          self.glob_completed(args, []), self.result])
        self.assertEqual(self.run_cursor()[0], "final answer")

    def test_glob_explicit_subdirectory_passes_relative_files(self):
        sub = self.workspace / "sub"
        sub.mkdir()
        (sub / "inside.txt").write_text("inside")
        for target in (str(sub), "sub"):
            with self.subTest(target=target):
                args = {"targetDirectory": target, "globPattern": "**/inside.txt"}
                self.fake_cursor([self.tool_event("globToolCall", args),
                                  self.glob_completed(args, ["./inside.txt"], str(sub)), self.result])
                self.assertEqual(self.run_cursor()[0], "final answer")

    def test_glob_without_target_directory_rejects_unsafe_patterns(self):
        for pattern in ("/outside/*", "~/secret/*", "../*", "sub/../inside.txt"):
            for subtype in ("started", "completed"):
                with self.subTest(pattern=pattern, subtype=subtype):
                    self.assert_violation(self.tool_event("globToolCall", {"globPattern": pattern}, subtype),
                                          "globToolCall", pattern)

    def test_glob_completed_entries_cannot_escape_searched_directory(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "a").write_text("outside")
        (self.workspace / "escape").symlink_to(outside, target_is_directory=True)
        (self.workspace / "sub").mkdir()
        (self.workspace / "other.txt").write_text("inside workspace, outside search")
        args = {"globPattern": "**/*"}
        cases = [(args, "../outside/a"), (args, str(outside / "a")),
                 (args, "./escape/a"), (args, "~/secret"),
                 ({**args, "targetDirectory": str(self.workspace / "sub")}, "../other.txt")]
        for args, entry in cases:
            with self.subTest(args=args, entry=entry):
                self.assert_violation(self.glob_completed(args, [entry]), "globToolCall", entry)

    def test_glob_completed_result_shapes_fail_closed(self):
        args = {"globPattern": "**/*"}
        self.assert_violation(self.tool_event("globToolCall", args, "completed"), "globToolCall")
        results = [None, [], {}, {"error": {"message": "SECRET RESULT"}}, {"success": None},
                   {"success": {}}, {"success": {"files": "./inside.txt"}},
                   {"success": {"files": None}}, {"success": {"files": {}}},
                   {"success": {"files": [None]}}, {"success": {"files": [7]}},
                   {"success": {"files": [{}]}}, {"success": {"files": []}, "error": {}}]
        for result in results:
            with self.subTest(result=result):
                event = self.tool_event("globToolCall", args, "completed")
                event["tool_call"]["globToolCall"]["result"] = result
                message = self.assert_violation(event, "globToolCall")
                self.assertNotIn("SECRET RESULT", message)

    def test_glob_completed_success_path_must_be_inside_workspace(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.workspace / "escape").symlink_to(outside, target_is_directory=True)
        for path in (str(outside), "../outside", "~/secret", "escape"):
            with self.subTest(path=path):
                self.assert_violation(self.glob_completed({"globPattern": "*"}, [], path),
                                      "globToolCall", path)

    def test_glob_violation_message_excludes_other_entries_and_content(self):
        entry = "../outside/a"
        event = self.glob_completed({"globPattern": "*"}, ["./inside.txt", entry, "SECRET OTHER ENTRY"])
        event["tool_call"]["globToolCall"]["result"]["success"]["content"] = "SECRET CONTENT"
        message = self.assert_violation(event, "globToolCall", entry)
        self.assertNotIn("./inside.txt", message)
        self.assertNotIn("SECRET", message)

    def test_all_other_tool_kinds_fail(self):
        for kind in ("shellToolCall", "taskToolCall", "fooToolCall", "mcpToolCall",
                     "webSearchToolCall", "webFetchToolCall", "editToolCall", "writeToolCall"):
            for subtype in ("started", "completed"):
                with self.subTest(kind=kind, subtype=subtype):
                    self.assert_violation(self.tool_event(kind, {}, subtype), kind)

    def test_zero_or_multiple_tool_kinds_fail(self):
        for call in ({}, {"toolCallId": "x", "hookAdditionalContexts": []},
                     {"readToolCall": {"args": {"path": "."}},
                      "grepToolCall": {"args": {"path": "."}}}):
            with self.subTest(call=call):
                message = self.assert_violation(
                    {"type": "tool_call", "subtype": "started", "tool_call": call}, "ToolCall")
                for kind in call:
                    if kind.endswith("ToolCall"):
                        self.assertIn(kind, message)

    def test_malformed_tool_envelopes_fail(self):
        for call in (None, [], "readToolCall"):
            with self.subTest(call=call):
                self.assert_violation({"type": "tool_call", "tool_call": call}, "tool_call")
        for tool in (None, [], {}, {"args": None}, {"args": []}):
            with self.subTest(tool=tool):
                self.assert_violation({"type": "tool_call", "subtype": "started",
                                       "tool_call": {"readToolCall": tool}}, "readToolCall")

    def test_unknown_tool_subtypes_fail(self):
        for subtype in (None, "delta", "cancelled"):
            with self.subTest(subtype=subtype):
                self.assert_violation(self.tool_event("readToolCall", {"path": "."}, subtype),
                                      "readToolCall")

    def test_stream_format_changes_fail_without_echoing_lines(self):
        for line in (b"not JSON SECRET", b"[]", b"null", b"42", b'"SECRET"', b'{}',
                     b'{"type":"new-event","result":"SECRET"}', b'{"type":[]}', b"\xff"):
            with self.subTest(line=line):
                self.fake_cursor(raw=line + b"\n")
                with self.assertRaises(ask.OutsideRead) as cm:
                    self.run_cursor()
                self.assertEqual(cm.exception.code, 4)
                self.assertNotIn("SECRET", str(cm.exception))

    def test_duplicate_json_keys_fail(self):
        self.fake_cursor(raw=b'{"type":"tool_call","type":"assistant"}\n')
        with self.assertRaises(ask.OutsideRead):
            self.run_cursor()

    def test_non_json_numeric_constants_fail(self):
        for value in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(value=value):
                self.fake_cursor(raw=('{"type":"assistant","value":%s}\n' % value).encode())
                with self.assertRaises(ask.OutsideRead):
                    self.run_cursor()

    def test_blank_lines_and_unterminated_result_pass(self):
        self.fake_cursor(raw=b"\n \t\n" + json.dumps(self.result).encode())
        self.assertEqual(self.run_cursor()[0], "final answer")

    def test_split_stream_chunks_pass(self):
        line = json.dumps(self.tool_event("readToolCall", {"path": "sub/a.txt"}))
        before = f"sys.stdout.write({line[:30]!r}); sys.stdout.flush(); time.sleep(0.05)"
        self.fake_cursor(raw=(line[30:] + "\n" + json.dumps(self.result) + "\n").encode(), before=before)
        self.assertEqual(self.run_cursor()[0], "final answer")

    def test_completed_violation_never_exposes_file_contents(self):
        path = str(self.root / "outside.txt")
        event = self.tool_event("readToolCall", {"path": path}, "completed")
        event["tool_call"]["readToolCall"]["result"] = {"success": {"content": "SECRET FILE CONTENT"}}
        message = self.assert_violation(event, "readToolCall", path)
        self.assertNotIn("SECRET FILE CONTENT", message)

    def test_tools_after_result_are_still_checked(self):
        self.fake_cursor([self.result, self.tool_event("taskToolCall", {})])
        with self.assertRaisesRegex(ask.OutsideRead, "taskToolCall"):
            self.run_cursor()

    def assert_stopped(self, error, events=(), before="", after="", timeout=10, code=4):
        pidfile = self.root / "pid"
        before = f"Path({str(pidfile)!r}).write_text(str(os.getpid()))\n" + before
        self.fake_cursor(events, before=before, after=after + "\ntime.sleep(30)\n"
                         + f"print({json.dumps(self.result)!r}, flush=True)")
        started = time.monotonic()
        with self.assertRaises(error) as cm:
            self.run_cursor(timeout=timeout)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(cm.exception.code, code)
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pidfile.read_text()), 0)
        return cm.exception

    def test_violation_terminates_sleeping_process_immediately(self):
        self.assert_stopped(ask.OutsideRead, [self.tool_event("readToolCall", {"path": str(self.root)})])

    def test_violation_kills_process_ignoring_sigterm(self):
        self.assert_stopped(ask.OutsideRead, [self.tool_event("shellToolCall", {})],
                            before="signal.signal(signal.SIGTERM, signal.SIG_IGN)")

    def child_running(self, pid):
        try:
            os.kill(pid, 0)
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        except (ProcessLookupError, FileNotFoundError):
            return False
        return state != "Z"

    def sigterm_resistant_child(self):
        pidfile = self.root / "child-pid"

        def cleanup():
            if pidfile.exists():
                pid = int(pidfile.read_text())
                if self.child_running(pid):
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

        self.addCleanup(cleanup)
        before = ("child = os.fork()\n"
                  "if child == 0:\n"
                  "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                  "    os.close(1)\n"
                  f"    Path({str(pidfile)!r}).write_text(str(os.getpid()))\n"
                  "    time.sleep(60)\n"
                  "    os._exit(0)\n"
                  f"while not Path({str(pidfile)!r}).exists():\n"
                  "    time.sleep(0.01)\n")
        return pidfile, before

    def assert_child_stopped(self, pidfile):
        pid = int(pidfile.read_text())
        deadline = time.monotonic() + 5
        while self.child_running(pid):
            if time.monotonic() >= deadline:
                self.fail(f"Cursor child {pid} survived process group cleanup")
            time.sleep(0.05)

    def test_violation_kills_sigterm_resistant_child(self):
        pidfile, before = self.sigterm_resistant_child()
        self.assert_stopped(ask.OutsideRead, [self.tool_event("shellToolCall", {})], before=before)
        self.assert_child_stopped(pidfile)

    def test_timeout_kills_sigterm_resistant_child(self):
        pidfile, before = self.sigterm_resistant_child()
        error = self.assert_stopped(ask.AskFailed, before=before, timeout=1, code=124)
        self.assertNotIsInstance(error, ask.OutsideRead)
        self.assert_child_stopped(pidfile)

    def test_clean_exit_kills_child_after_leader_is_reaped(self):
        pidfile, before = self.sigterm_resistant_child()
        self.fake_cursor([self.result], before=before)
        self.assertEqual(self.run_cursor()[0], "final answer")
        self.assert_child_stopped(pidfile)

    def test_nonzero_exit_kills_child(self):
        pidfile, before = self.sigterm_resistant_child()
        self.fake_cursor(before=before, after="sys.exit(3)")
        with self.assertRaises(ask.AskFailed) as cm:
            self.run_cursor()
        self.assertEqual(cm.exception.code, 3)
        self.assertNotIsInstance(cm.exception, ask.OutsideRead)
        self.assert_child_stopped(pidfile)

    def test_keyboard_interrupt_while_streaming_kills_child(self):
        pidfile, before = self.sigterm_resistant_child()
        self.fake_cursor([{"type": "thinking"}], before=before, after="time.sleep(30)")
        started = time.monotonic()
        with mock.patch("ask._check_cursor_event", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_cursor()
        self.assertLess(time.monotonic() - started, 10)
        self.assert_child_stopped(pidfile)

    def test_timeout_terminates_process_and_keeps_stderr_tail(self):
        error = self.assert_stopped(ask.AskFailed, timeout=0.3, code=124,
                                    before="print('timeout stderr marker', file=sys.stderr, flush=True)")
        self.assertNotIsInstance(error, ask.OutsideRead)
        self.assertIn("timeout stderr marker", str(error))

    def test_timeout_after_stdout_closes_still_stops_process(self):
        self.assert_stopped(ask.AskFailed, [self.result], after="os.close(1)", timeout=0.3, code=124)

    def test_timeout_with_partial_line_kills_sigterm_resistant_process(self):
        self.assert_stopped(ask.AskFailed, timeout=0.3, code=124,
                            before="signal.signal(signal.SIGTERM, signal.SIG_IGN)",
                            after="sys.stdout.write('{'); sys.stdout.flush()")

    def test_large_stderr_cannot_deadlock_stdout(self):
        self.fake_cursor([self.result], before="sys.stderr.write('x' * 200000); sys.stderr.flush()")
        self.assertEqual(self.run_cursor(timeout=3)[0], "final answer")

    def test_nonzero_exit_preserves_stderr_tail(self):
        self.fake_cursor([self.result], before="print('\\n'.join('line%d' % i for i in range(20)), "
                         "file=sys.stderr)", after="sys.exit(3)")
        with self.assertRaises(ask.AskFailed) as cm:
            self.run_cursor()
        self.assertNotIsInstance(cm.exception, ask.OutsideRead)
        self.assertEqual(cm.exception.code, 3)
        self.assertEqual(str(cm.exception).splitlines(), [f"line{i}" for i in range(10, 20)])

    def test_missing_result_is_ask_failure(self):
        self.fake_cursor([{"type": "assistant", "result": "not final"}])
        with self.assertRaises(ask.AskFailed) as cm:
            self.run_cursor()
        self.assertNotIsInstance(cm.exception, ask.OutsideRead)
        self.assertIn("missing result", str(cm.exception))

    def test_error_result_is_ask_failure(self):
        self.fake_cursor([{**self.result, "is_error": True}])
        with self.assertRaises(ask.AskFailed) as cm:
            self.run_cursor()
        self.assertNotIsInstance(cm.exception, ask.OutsideRead)

    def test_unconfined_still_uses_original_subprocess_run(self):
        result = mock.Mock(returncode=0, stdout=json.dumps(self.result), stderr="")
        with mock.patch("ask.subprocess.run", return_value=result) as run, \
                mock.patch("ask.subprocess.Popen") as popen, \
                mock.patch("ask.clean_env", return_value={"PATH": "scrubbed"}):
            answer, usage, _, model = ask.run_cursor("question", "auto", self.workspace, 7)
        run.assert_called_once_with(
            ["cursor-agent", "-p", "--model", "auto", "--mode", "ask", "--trust",
             "--workspace", str(self.workspace), "--output-format", "json", "question"],
            stdin=ask.subprocess.DEVNULL, capture_output=True, text=True, timeout=7,
            env={"PATH": "scrubbed"}, cwd=str(self.workspace))
        popen.assert_not_called()
        self.assertEqual((answer, model), ("final answer", "auto"))
        self.assertEqual(usage, {"input": 11, "output": 7, "cached_input": 100})


def argparse_ns(**kwargs):
    return type("NS", (), kwargs)()


if __name__ == "__main__":
    unittest.main()

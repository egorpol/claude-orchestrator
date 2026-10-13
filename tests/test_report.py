"""Field-report collection, using fake Claude transcripts and no live quota."""
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "delegate" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cost  # noqa: E402
import report  # noqa: E402

SESSION = "11111111-2222-4333-8444-555555555555"
OTHER_SESSION = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def assistant(mid, timestamp, inp, read, write, output, model="claude-z-sonnet"):
    return {
        "type": "assistant", "sessionId": SESSION, "timestamp": timestamp,
        "message": {"id": mid, "model": model, "usage": {
            "input_tokens": inp, "cache_read_input_tokens": read,
            "cache_creation_input_tokens": write, "output_tokens": output,
        }},
    }


def typed_prompt(timestamp, text="do the work", **overrides):
    return {"type": "user", "timestamp": timestamp, "message": {"content": text}, **overrides}


def queued_prompt(timestamp, kind="human", mode="prompt"):
    return {"type": "attachment", "timestamp": timestamp, "isSidechain": False,
            "attachment": {"type": "queued_command", "prompt": "queued work", "commandMode": mode,
                           "origin": {"kind": kind}, "timestamp": "2026-10-10T20:58:00Z"}}


def guard_event(timestamp, guard, **overrides):
    if guard == "nudge":
        row = {"type": "attachment", "attachment": {"type": "hook_system_message",
               "content": "context guard: ~240k tokens; a new work item is cheaper after a handoff",
               "hookEvent": "UserPromptSubmit"}}
    else:
        row = {"type": "system", "subtype": "informational", "level": "warning",
               "preventContinuation": True,
               "content": "UserPromptSubmit operation blocked by hook:\ncontext guard: idle 464 min"}
    return {**row, "timestamp": timestamp, "isSidechain": False, **overrides}


class TestReport(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = self.root / "toy"
        self.repo.mkdir()
        self.remote = "git@github.com:owner/Toy.git"
        old_cwd = Path.cwd()
        os.chdir(str(self.repo))
        self.addCleanup(os.chdir, str(old_cwd))
        self.cfg = self.root / "claude"
        self.project = self.cfg / "projects" / cost.munge_cwd(str(self.repo))
        self.project.mkdir(parents=True)
        self.transcript = self.project / f"{SESSION}.jsonl"
        self.calls = [
            assistant("m1", "2026-10-10T21:00:00Z", 10, 1200000, 1000, 200),
            assistant("m2", "2026-10-10T21:59:00Z", 5, 2000, 50, 30, "claude-a-opus"),
            assistant("m3", "2026-10-10T23:00:00Z", 7, 4000, 3000, 40),
        ]
        lines = [typed_prompt("2026-10-10T20:59:00Z"),
                 self.calls[0], self.calls[0],
                 assistant("synthetic", "2026-10-10T21:30:00Z", 999, 999, 999, 999, "<synthetic>"),
                 typed_prompt("2026-10-10T21:58:01Z", "continue"),
                 guard_event("2026-10-10T21:58:01.030Z", "nudge"), self.calls[1],
                 guard_event("2026-10-10T22:59:00Z", "idle_hold"),
                 typed_prompt("2026-10-10T22:59:01Z", "go ahead"), self.calls[2]]
        self.transcript.write_text("bad json\n" + "".join(json.dumps(obj) + "\n" for obj in lines), encoding="utf-8")
        self.feedback = self.root / "fb"
        self.log = self.root / "delegate-runs.jsonl"
        env = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.cfg), "TZ": "Europe/Berlin"})
        env.start()
        self.addCleanup(time.tzset)
        self.addCleanup(env.stop)
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        os.environ.pop("CLAUDE_ORCHESTRATOR_FEEDBACK", None)
        time.tzset()
        for patcher in (
            mock.patch("report._git", side_effect=self.fake_git),
            mock.patch("cost.DEFAULT_LOG", self.log),
            mock.patch("quota.fetch", side_effect=AssertionError("live quota must not be called")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def fake_git(self, directory, *args):
        path = Path(directory).resolve()
        if args == ("rev-parse", "--show-toplevel"):
            return str(self.repo) if path == self.repo or self.repo in path.parents else None
        if args == ("remote", "get-url", "origin"):
            return self.remote
        self.fail(f"unexpected git command: {args}")

    def invoke(self, *args, defaults=True):
        argv = ["--session", SESSION, "--feedback-dir", str(self.feedback), "--no-quota"] if defaults else []
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                report.main(argv + list(args))
                code = 0
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def usage_row(self):
        return json.loads((self.feedback / "token_usage.jsonl").read_text(encoding="utf-8"))

    def skeleton(self):
        return self.feedback / "2026-10-11-toy-session.md"

    def snapshot(self):
        return {p.name: p.read_bytes() for p in self.feedback.iterdir()}

    def test_first_run_matches_cost_and_fills_skeleton(self):
        code, out, err = self.invoke("--item", "Build report")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(len(out.splitlines()), 4)
        self.assertIn("row appended", out)
        self.assertIn("runs copied: 0", out)
        self.assertIn("fill in the <!-- --> sections", out)
        buf = io.StringIO()
        with redirect_stdout(buf):
            cost.main(["--session", SESSION, "--json"])
        summary = json.loads(buf.getvalue())
        row = self.usage_row()
        self.assertEqual(row, {
            "date": "2026-10-11", "repo": "owner/Toy", "item": "Build report",
            "kind": "orchestrator_session", "session": SESSION, "model": "claude-z-sonnet",
            "source": "delegate report", "api_calls": summary["calls"],
            "fresh_input": summary["input"], "output": summary["output"],
            "cache_read": summary["cache_read"], "cache_write": summary["cache_creation"],
            "context_per_call": {"first": summary["context_first"], "last": summary["context_last"],
                                 "max": summary["context_max"]},
            "cost_units": round(summary["cost_units"]),
            "idle_rebuilds": summary["idle_rebuilds"], "quota_end": None,
            "usd_list_price": None, "subagent_calls": 0, "prices_as_of": "2026-10-06",
            "prompts": [
                {"timestamp": "2026-10-10T20:59:00Z", "context": 1201010, "guard": None},
                {"timestamp": "2026-10-10T21:58:01Z", "context": 2055, "guard": "nudge"},
                {"timestamp": "2026-10-10T22:59:01Z", "context": 7007, "guard": "idle_hold"},
            ],
        })
        self.assertEqual(row["api_calls"], 3)
        self.assertEqual(row["cache_read"], 1206000)
        self.assertEqual(row["idle_rebuilds"], [{"gap_min": 61, "cache_creation": 3000}])
        text = self.skeleton().read_text(encoding="utf-8")
        self.assertIn("# Field report: Build report (2026-10-11)", text)
        self.assertIn(f"session `{SESSION[:8]}`", text)
        self.assertIn("3 calls, 1.2M cache read, 4.0k cache write, 270 output", text)
        self.assertIn("context 1201k → 7.0k", text)
        self.assertIn(f"{cost._fmt_int(row['cost_units'])} cost units", text)
        self.assertIn("Idle rebuilds: 1 (61 min; 3.0k written", text)
        context_line = "Context at typed prompts: 1201k, 2.1k (guard), 7.0k (idle hold)."
        self.assertIn(context_line, text)
        paragraphs = text.split("\n\n")
        self.assertTrue(paragraphs[paragraphs.index(context_line) - 1].startswith("Session up to this log: "))
        self.assertIn("Quota at the end: not measured.", text)
        self.assertIn("Delegate runs: none.", text)
        total = summary["cost_units"]
        for key, label in (("cache_read", "cache read"), ("cache_write", "cache write"), ("output", "output")):
            self.assertIn(f"{label} {100 * summary['cost_parts'][key] / total:.1f}%", text)
        self.assertIn(f"{100 * summary['idle_rebuild_units'] / total:.1f}% of cost units", text)
        for heading in ("What worked", "Friction", "Suggestions for claude-orchestrator", "Not in this repo's scope"):
            self.assertIn(f"## {heading}", text)

    def test_no_typed_prompts(self):
        self.write_log(self.transcript, [
            typed_prompt("2026-10-10T20:59:00Z", isMeta=True),
            typed_prompt("2026-10-10T20:59:01Z", isSidechain=True),
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "done"}]}},
            *self.calls,
        ])
        code, _, err = self.invoke()
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.usage_row()["prompts"], [])
        self.assertIn("Context at typed prompts: none.\n",
                      self.skeleton().read_text(encoding="utf-8"))

    def test_prompt_at_end_uses_last_main_call(self):
        timestamp = "2026-10-10T23:01:00Z"
        self.write_log(self.transcript, [*self.calls, typed_prompt(timestamp)])
        code, out, err = self.invoke("--dry-run", "--json")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["row"]["prompts"], [
            {"timestamp": timestamp, "context": 7007, "guard": None},
        ])

    def test_sidechain_guard_events_are_ignored(self):
        self.write_log(self.transcript, [
            guard_event("2026-10-10T20:58:01Z", "idle_hold", isSidechain=True),
            typed_prompt("2026-10-10T20:59:00Z"),
            guard_event("2026-10-10T20:59:00.030Z", "nudge", isSidechain=True), *self.calls,
        ])
        self.assertEqual(report.prompt_contexts(self.transcript), [
            {"timestamp": "2026-10-10T20:59:00Z", "context": 1201010, "guard": None},
        ])

    def test_idle_hold_between_calls_tags_next_typed_prompt(self):
        self.write_log(self.transcript, [
            typed_prompt("2026-10-10T20:59:00Z"), self.calls[0],
            guard_event("2026-10-10T21:30:00Z", "idle_hold"), self.calls[1],
            typed_prompt("2026-10-10T22:59:00Z"), self.calls[2],
        ])
        self.assertEqual(report.prompt_contexts(self.transcript), [
            {"timestamp": "2026-10-10T20:59:00Z", "context": 1201010, "guard": None},
            {"timestamp": "2026-10-10T22:59:00Z", "context": 7007, "guard": "idle_hold"},
        ])

    def test_nudge_after_prompt_tags_it_instead_of_the_next_prompt(self):
        self.write_log(self.transcript, [
            typed_prompt("2026-10-10T20:59:00Z"),
            guard_event("2026-10-10T20:59:00.030Z", "nudge"),
            {**self.calls[0], "timestamp": "2026-10-10T20:59:01Z"},
            typed_prompt("2026-10-10T21:00:00Z"), self.calls[1],
        ])
        self.assertEqual(report.prompt_contexts(self.transcript), [
            {"timestamp": "2026-10-10T20:59:00Z", "context": 1201010, "guard": "nudge"},
            {"timestamp": "2026-10-10T21:00:00Z", "context": 2055, "guard": None},
        ])

    def test_nudge_after_skill_expansion_tags_the_command_prompt(self):
        # /field-report: the command row, then its isMeta skill body, then the nudge (3240a01e).
        self.write_log(self.transcript, [
            typed_prompt("2026-10-10T20:59:00Z"),
            typed_prompt("2026-10-10T20:59:00.001Z", isMeta=True),
            guard_event("2026-10-10T20:59:00.030Z", "nudge"), *self.calls,
        ])
        self.assertEqual(report.prompt_contexts(self.transcript), [
            {"timestamp": "2026-10-10T20:59:00Z", "context": 1201010, "guard": "nudge"},
        ])

    def test_nudge_before_any_typed_prompt_is_dropped(self):
        self.write_log(self.transcript, [
            guard_event("2026-10-10T20:58:00Z", "nudge"),
            typed_prompt("2026-10-10T20:59:00Z"), *self.calls,
        ])
        self.assertEqual(report.prompt_contexts(self.transcript), [
            {"timestamp": "2026-10-10T20:59:00Z", "context": 1201010, "guard": None},
        ])

    def test_nudge_after_final_prompt_is_retained(self):
        self.write_log(self.transcript, [
            *self.calls, typed_prompt("2026-10-10T23:01:00Z"),
            guard_event("2026-10-10T23:01:00.030Z", "nudge"),
        ])
        self.assertEqual(report.prompt_contexts(self.transcript), [
            {"timestamp": "2026-10-10T23:01:00Z", "context": 7007, "guard": "nudge"},
        ])

    def test_nudge_after_task_notification_or_peer_queue_is_dropped(self):
        for kind, mode in (("task-notification", "task-notification"), ("peer", "prompt")):
            with self.subTest(kind=kind):
                self.write_log(self.transcript, [
                    typed_prompt("2026-10-10T20:59:00Z"), self.calls[0],
                    queued_prompt("2026-10-10T21:58:00Z", kind, mode),
                    {"type": "attachment", "attachment": {"type": "skill_listing"}},
                    {"type": "system", "subtype": "informational", "content": "hook completed"},
                    guard_event("2026-10-10T21:58:00.030Z", "nudge"), *self.calls[1:],
                ])
                self.assertEqual(report.prompt_contexts(self.transcript), [
                    {"timestamp": "2026-10-10T20:59:00Z", "context": 1201010, "guard": None},
                ])

    def test_nudge_after_human_queued_prompt_tags_it_at_delivery_time(self):
        self.write_log(self.transcript, [
            typed_prompt("2026-10-10T20:59:00Z"), self.calls[0],
            queued_prompt("2026-10-10T21:58:00Z"),
            {"type": "attachment", "attachment": {"type": "skill_listing"}},
            {"type": "system", "subtype": "informational", "content": "hook completed"},
            guard_event("2026-10-10T21:58:00.030Z", "nudge", parentUuid="unrelated attachment"),
            *self.calls[1:],
        ])
        code, _, err = self.invoke()
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.usage_row()["prompts"], [
            {"timestamp": "2026-10-10T20:59:00Z", "context": 1201010, "guard": None},
            {"timestamp": "2026-10-10T21:58:00Z", "context": 2055, "guard": "nudge"},
        ])
        self.assertIn("Context at typed prompts: 1201k, 2.1k (guard).",
                      self.skeleton().read_text(encoding="utf-8"))

    def test_nudge_after_tool_result_or_peer_user_is_dropped(self):
        timestamp = "2026-10-10T20:59:00Z"
        for non_typed in (
            {"type": "user", "timestamp": timestamp,
             "message": {"content": [{"type": "tool_result", "content": "done"}]}},
            typed_prompt(timestamp, "peer message", origin={"kind": "peer"}),
        ):
            with self.subTest(non_typed=non_typed):
                self.write_log(self.transcript, [
                    typed_prompt(timestamp, origin={"kind": "human"}), non_typed,
                    guard_event("2026-10-10T20:59:00.030Z", "nudge"), *self.calls,
                ])
                self.assertEqual(report.prompt_contexts(self.transcript), [
                    {"timestamp": timestamp, "context": 1201010, "guard": None},
                ])

    def test_idle_hold_wins_and_unmatched_idle_holds_are_dropped(self):
        for first, second in (("idle_hold", "nudge"), ("nudge", "idle_hold")):
            with self.subTest(first=first):
                self.write_log(self.transcript, [
                    typed_prompt("2026-10-10T20:59:00Z"),
                    guard_event("2026-10-10T20:59:00Z", first),
                    guard_event("2026-10-10T20:59:00Z", second),
                    *self.calls,
                    guard_event("2026-10-10T23:01:00Z", "idle_hold"),
                ])
                self.assertEqual(report.prompt_contexts(self.transcript), [
                    {"timestamp": "2026-10-10T20:59:00Z", "context": 1201010, "guard": "idle_hold"},
                ])

    def test_context_guard_text_in_other_shapes_is_ignored(self):
        wrong_hook = guard_event("2026-10-10T20:59:00.030Z", "nudge")
        wrong_hook["attachment"]["hookEvent"] = "PostToolUse"
        self.write_log(self.transcript, [
            guard_event("2026-10-10T20:58:01Z", "idle_hold", preventContinuation=False),
            guard_event("2026-10-10T20:58:02Z", "idle_hold", subtype="other"),
            {"type": "system", "subtype": "informational", "preventContinuation": True,
             "content": "context guard: quoted text", "timestamp": "2026-10-10T20:58:03Z"},
            typed_prompt("2026-10-10T20:59:00Z", "Explain context guard:"), wrong_hook, *self.calls,
        ])
        self.assertEqual(report.prompt_contexts(self.transcript)[0]["guard"], None)

    def test_context_uses_chronological_main_calls_and_equal_timestamp(self):
        before_ts = "2026-10-10T21:57:00Z"
        prompt_ts = "2026-10-10T23:59:00+02:00"  # exactly the second main call
        self.write_log(self.transcript, [typed_prompt(before_ts), typed_prompt(prompt_ts),
                                         self.calls[2], self.calls[0], self.calls[1]])
        agent_dir = self.transcript.with_suffix("") / "subagents"
        agent_dir.mkdir(parents=True)
        self.write_log(agent_dir / "agent-a.jsonl", [
            guard_event(prompt_ts, "idle_hold"), typed_prompt(prompt_ts),
            assistant("agent", "2026-10-10T21:58:00Z", 100, 200, 300, 400),
        ])
        self.assertEqual(report.prompt_contexts(self.transcript), [
            {"timestamp": before_ts, "context": 2055, "guard": None},
            {"timestamp": prompt_ts, "context": 2055, "guard": None},
        ])

    def test_prompt_extraction_uses_raters_origin_rule(self):
        self.write_log(self.transcript, [
            typed_prompt("2026-10-10T20:58:00Z"),
            typed_prompt("2026-10-10T20:58:01Z", origin={"kind": "automation"}),
            typed_prompt("2026-10-10T20:59:00Z", origin={"kind": "human"}), *self.calls,
        ])
        self.assertEqual(report.prompt_contexts(self.transcript), [
            {"timestamp": "2026-10-10T20:59:00Z", "context": 1201010, "guard": None},
        ])

    def test_untimed_prompt_keeps_legacy_report_working(self):
        self.write_log(self.transcript, [typed_prompt(None), *self.calls])
        code, out, err = self.invoke("--dry-run", "--json")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["row"]["prompts"], [
            {"timestamp": None, "context": 1201010, "guard": None},
        ])

    def test_upsert_replaces_row_logged_with_short_session_prefix(self):
        sid = "55a11d16-0000-4000-8000-000000000000"
        old = (b'{"kind": "orchestrator_session", "session": "55a11d16", "api_calls": 85}\n'
               b'{"kind": "orchestrator_session", "session": "55a11d1", "api_calls": 1}\n')
        data, action = report.upsert_usage(old, {"kind": "orchestrator_session", "session": sid})
        self.assertEqual(action, "row replaced")
        lines = data.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["session"], sid)
        self.assertEqual(lines[1], old.splitlines()[1])  # under 8 chars: not a match

    def test_force_replaces_in_place_and_conflict_writes_nothing(self):
        self.assertEqual(self.invoke()[0], 0)
        path = self.feedback / "token_usage.jsonl"
        before = b' {"kind":"delegate","session":"' + SESSION.encode() + b'", "note":"keep spaces"}\r\n'
        after = b'not json\n{"kind":"orchestrator_session","session":"other"}\n'
        original = path.read_bytes()
        path.write_bytes(before + original + after)
        snapshot = self.snapshot()
        with mock.patch("quota.fetch") as fetch:
            code, _, err = self.invoke("--item", "regenerated")
        self.assertEqual(code, 1)
        self.assertIn(str(self.skeleton()), err)
        fetch.assert_not_called()
        self.assertEqual(self.snapshot(), snapshot)
        with self.transcript.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(assistant("m4", "2026-10-10T23:10:00Z", 3, 8, 9, 2)) + "\n")
        code, out, err = self.invoke("--force", "--item", "regenerated")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("row replaced", out)
        data = path.read_bytes()
        self.assertTrue(data.startswith(before))
        self.assertTrue(data.endswith(after))
        self.assertEqual(len(data.splitlines()), len(snapshot[path.name].splitlines()))
        row = json.loads(data.splitlines()[1])
        self.assertEqual(row["item"], "regenerated")
        self.assertEqual(row["api_calls"], 4)

    def test_runs_filter_repo_and_time_and_deduplicate(self):
        subdir = self.repo / "src"
        subdir.mkdir()
        alias = self.root / "repo-alias"
        alias.symlink_to(self.repo, target_is_directory=True)

        def run(name, **overrides):
            row = {"run": name, "ts": "2026-10-10T21:00:00Z", "repo": "different-label",
                   "repo_path": str(subdir), "agent": "codex", "model": "gpt-test",
                   "rounds": [{"n": 1}, {"n": 2}], "outcome": "applied"}
            row.update(overrides)
            return row

        rows = [run("boundary"), run("by-name", repo="toy", repo_path="/missing"),
                run("by-alias", repo_path=str(alias)),
                run("too-old", ts="2026-10-10T20:59:59Z"),
                run("other-repo", repo_path=str(self.root / "other")),
                run("no-time", ts=None), run("bad-time", ts="bad"), run("boundary")]
        self.log.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        self.feedback.mkdir()
        retained = b' {"run":"preexisting", "ts":"old", "session":"another"}\r\n'
        target = self.feedback / "runs.jsonl"
        target.write_bytes(retained)
        code, out, err = self.invoke("--repo", "custom/report-label")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("runs copied: 3", out)
        copied = [json.loads(line) for line in target.read_bytes().splitlines()[1:]]
        self.assertEqual([r["run"] for r in copied], ["boundary", "by-name", "by-alias"])
        for row in copied:
            self.assertEqual(row["session"], SESSION)
            source = next(r for r in rows if r["run"] == row["run"])
            self.assertEqual(row, {**source, "repo": "custom/report-label", "session": SESSION})
        old = target.read_bytes()
        self.assertTrue(old.startswith(retained))
        code, out, err = self.invoke("--force", "--repo", "custom/report-label")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("runs copied: 0", out)
        self.assertEqual(target.read_bytes(), old)
        text = (self.feedback / "2026-10-11-report-label-session.md").read_text(encoding="utf-8")
        self.assertIn("- `boundary`: codex gpt-test, 2 round(s), applied", text)
        self.assertNotIn("too-old", text)

    def test_feedback_precedence_and_copy_install_error(self):
        install = self.root / "install"
        script = install / "skills/delegate/scripts/report.py"
        script.parent.mkdir(parents=True)
        (install / "skills/delegate/SKILL.md").write_text("skill", encoding="utf-8")
        (install / ".git").write_text("gitdir: worktree\n", encoding="utf-8")
        env_dir = self.root / "env-fb"
        args = ["--session", SESSION, "--no-quota", "--json"]
        with mock.patch("report.__file__", str(script)), \
                mock.patch.dict(os.environ, {"CLAUDE_ORCHESTRATOR_FEEDBACK": str(env_dir)}):
            code, out, _ = self.invoke(*args, "--feedback-dir", str(self.feedback), defaults=False)
            self.assertEqual(code, 0)
            self.assertEqual(Path(json.loads(out)["skeleton"]).parent, self.feedback)
            self.assertFalse(env_dir.exists())
            code, out, _ = self.invoke(*args, defaults=False)
            self.assertEqual(code, 0)
            self.assertEqual(Path(json.loads(out)["skeleton"]).parent, env_dir)
            os.environ.pop("CLAUDE_ORCHESTRATOR_FEEDBACK")
            code, out, _ = self.invoke(*args, defaults=False)
            self.assertEqual(code, 0)
            self.assertEqual(Path(json.loads(out)["skeleton"]).parent, install / "feedback")
            (install / ".git").unlink()
            code, _, err = self.invoke(*args, defaults=False)
            self.assertEqual(code, 2)
            self.assertEqual(len(err.splitlines()), 1)
            self.assertIn("--feedback-dir", err)
            self.assertIn("CLAUDE_ORCHESTRATOR_FEEDBACK", err)

    def test_dry_run_writes_nothing_and_prints_row(self):
        code, out, err = self.invoke("--dry-run")
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual(json.loads(lines[0])["api_calls"], 3)
        self.assertEqual(lines[1], "runs copied: 0")
        self.assertEqual(lines[2], str(self.skeleton()))
        self.assertFalse(self.feedback.exists())
        self.assertEqual(self.invoke()[0], 0)
        before = self.snapshot()
        code, out, err = self.invoke("--dry-run", "--force", "--json", "--item", "preview")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.snapshot(), before)
        result = json.loads(out)
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["row_action"], "row replaced")
        self.assertEqual(result["row"]["item"], "preview")

    def test_session_env_beats_newer_transcript_and_flag_beats_env(self):
        other = self.project / f"{OTHER_SESSION}.jsonl"
        obj = assistant("other", "2026-10-10T23:30:00Z", 1, 2, 3, 4)
        obj["sessionId"] = OTHER_SESSION
        other.write_text(json.dumps(obj) + "\n", encoding="utf-8")
        os.utime(str(self.transcript), (10, 10))
        os.utime(str(other), (20, 20))
        args = ["--feedback-dir", str(self.feedback), "--no-quota", "--dry-run", "--json"]
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": SESSION}):
            code, out, err = self.invoke(*args, defaults=False)
            self.assertEqual((code, err), (0, ""))
            self.assertEqual(json.loads(out)["row"]["session"], SESSION)
            code, out, _ = self.invoke(*args, "--session", OTHER_SESSION, defaults=False)
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["row"]["session"], OTHER_SESSION)
        code, out, _ = self.invoke(*args, defaults=False)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["row"]["session"], OTHER_SESSION)

    def test_quota_failure_records_error_and_still_writes(self):
        with mock.patch("quota.fetch", side_effect=RuntimeError("openusage\nmissing")) as fetch:
            code, _, err = self.invoke("--session", SESSION, "--feedback-dir", str(self.feedback), defaults=False)
        self.assertEqual((code, err), (0, ""))
        fetch.assert_called_once_with("auto")
        self.assertEqual(self.usage_row()["quota_end"], {"error": "openusage missing"})
        self.assertTrue(self.skeleton().is_file())
        self.assertIn("Quota at the end: not measured.", self.skeleton().read_text(encoding="utf-8"))

    def test_quota_success_uses_shared_pool_function(self):
        envelope = {"snapshots": []}
        with mock.patch("quota.fetch", return_value=envelope), \
                mock.patch("quota.pools", return_value={"claude-5h": {"left_pct": 86.5},
                                                       "claude-7d": {"left_pct": 98},
                                                       "codex-5h": {"error": "missing"}}) as pools:
            code, _, err = self.invoke("--session", SESSION, "--feedback-dir", str(self.feedback), defaults=False)
        self.assertEqual((code, err), (0, ""))
        pools.assert_called_once_with(envelope)
        self.assertEqual(self.usage_row()["quota_end"], {"claude-5h": 86.5, "claude-7d": 98})
        self.assertIn("Quota at the end: claude-5h 86.5%, claude-7d 98%.", self.skeleton().read_text(encoding="utf-8"))

    def test_repo_name_remote_and_fallbacks(self):
        for remote in ("https://github.com/owner/name.git", "git@github.com:owner/name.git",
                       "ssh://git@github.com/owner/name.git", "ssh://git@github.com:22/owner/name.git"):
            with self.subTest(remote=remote):
                self.assertEqual(report.github_repo(remote), "owner/name")
        self.remote = "https://gitlab.com/owner/name.git"
        self.assertEqual(report.repo_identity(), ("toy", self.repo))
        self.assertEqual(report.repo_identity("explicit/name"), ("explicit/name", self.repo))
        with mock.patch("report._git", return_value=None):
            self.assertEqual(report.repo_identity(), ("toy", self.repo))

    def test_paths_cannot_write_outside_feedback(self):
        code, _, err = self.invoke("--topic", "../../escape")
        self.assertEqual(code, 2)
        self.assertIn("--topic", err)
        self.assertFalse(self.feedback.exists())
        self.feedback.mkdir()
        outside = self.root / "outside"
        outside.write_bytes(b"leave alone\n")
        (self.feedback / "runs.jsonl").symlink_to(outside)
        code, _, err = self.invoke()
        self.assertEqual(code, 2)
        self.assertIn("escapes feedback directory", err)
        self.assertEqual(outside.read_bytes(), b"leave alone\n")
        self.assertEqual(sorted(p.name for p in self.feedback.iterdir()), ["runs.jsonl"])

    def test_catalog_weights_are_shared_and_cost_is_rounded(self):
        weights = {"input": 1.3, "cache_read": 0.133, "cache_write": 2.75, "output": 4.51}
        with mock.patch("cost.load_catalog_cost", return_value=(weights, None)):
            self.assertEqual(self.invoke()[0], 0)
        expected = cost.summarise_session(cost.parse_assistant_usage(self.transcript), weights, None)
        self.assertEqual(self.usage_row()["cost_units"], round(expected["cost_units"]))
        self.assertIsInstance(self.usage_row()["cost_units"], int)

    def test_list_price_includes_subagents_and_preserves_existing_row_values(self):
        self.calls[0]["message"]["model"] = "claude-opus-5-5"
        self.transcript.write_text("".join(json.dumps(call) + "\n" for call in self.calls),
                                   encoding="utf-8")
        code, out, err = self.invoke("--dry-run", "--json")
        self.assertEqual((code, err), (0, ""))
        before = json.loads(out)["row"]
        self.assertEqual(before["usd_list_price"], 0.249)
        agent_dir = self.transcript.with_suffix("") / "subagents"
        agent_dir.mkdir(parents=True)
        # A later subagent call must not change the main session date/model/context.
        agent = assistant("agent", "2026-10-12T12:00:00Z", 10, 1000, 100, 2000, "claude-haiku-5-5")
        (agent_dir / "agent-a.jsonl").write_text(json.dumps(agent) + "\n" +
                                                json.dumps(self.calls[0]) + "\n", encoding="utf-8")
        self.assertEqual(self.invoke()[0], 0)
        row = self.usage_row()
        for key, value in before.items():
            if key not in ("usd_list_price", "subagent_calls"):
                self.assertEqual(row[key], value, key)
        self.assertEqual(row["subagent_calls"], 1)
        self.assertEqual(row["usd_list_price"], 0.2501)  # round only the final 0.2500635 total
        self.assertEqual(row["prices_as_of"], "2026-10-06")
        self.assertIsInstance(row["usd_list_price"], float)

    def write_log(self, path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    def read_log(self, path):
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_auto_summary_joins_session_log_and_quota_readings(self):
        log = self.root / "cache/sessions.jsonl"
        agent_dir = self.transcript.with_suffix("") / "subagents"
        agent_dir.mkdir(parents=True)
        agent = assistant("agent", "2026-10-11T12:00:00Z", 10, 1000, 100, 2000, "claude-haiku-5-5")
        (agent_dir / "agent-a.jsonl").write_text(json.dumps(agent) + "\n" + json.dumps(self.calls[0]) + "\n",
                                                encoding="utf-8")
        self.write_log(log, [
            {"event": "start", "session": SESSION, "ts": "2026-10-10T22:59:00+02:00", "source": "startup",
             "mode": "claude-only", "repo": "owner/Toy", "repo_dir": "toy", "transcript": str(self.transcript)},
            {"event": "delegate", "session": OTHER_SESSION, "command": "launch"},
            {"event": "blocked", "session": SESSION, "mode": "claude-only", "tool": "Bash"},
            {"event": "delegate", "session": SESSION, "command": "status"},
            {"event": "delegate", "session": SESSION, "command": "ask"},
            {"event": "blocked", "session": SESSION, "mode": "claude-only", "tool": "Skill"},
            {"event": "end", "session": SESSION, "reason": "clear", "calls": 3, "reroll": False},
            # A resume can log its start while the end summary is still taking its quota reading.
            {"event": "start", "session": SESSION, "source": "resume", "mode": "claude-only",
             "repo": "owner/Toy", "repo_dir": "toy", "transcript": str(self.transcript)},
        ])
        os.utime(self.transcript, (1_800_000_000, 1_800_000_000))
        os.utime(agent_dir / "agent-a.jsonl", (1_800_000_100.5, 1_800_000_100.5))
        window = "2026-10-13T09:50:00.287914+00:00"
        self.write_log(log.parent / "quota.jsonl", [
            {"ts": "2026-10-10T20:59:00+00:00", "reason": "session-start", "session": SESSION, "pools": {
                "claude-5h": {"used_pct": 10.0, "resets_at": window, "stale": False},
                "codex-5h": {"used_pct": 3.0, "resets_at": window, "stale": True},
                "cursor-auto": {"used_pct": 40.0, "resets_at": "2026-10-18T01:53:54+00:00", "stale": False}}},
            {"ts": "2026-10-10T21:00:00+00:00", "reason": "session-start", "session": OTHER_SESSION,
             "pools": {"claude-5h": {"used_pct": 99.0, "resets_at": window, "stale": False}}},
        ])
        end = {"ts": "2026-10-10T23:01:00+00:00", "reason": "session-end", "session": SESSION, "pools": {
            # Claude's reset time moves by fractions of a second between polls; same window.
            "claude-5h": {"used_pct": 14.5, "resets_at": "2026-10-13T09:49:59.838722+00:00", "stale": False},
            "codex-5h": {"used_pct": 4.0, "resets_at": window, "stale": False},
            "cursor-auto": {"used_pct": 1.0, "resets_at": "2026-11-18T01:53:54+00:00", "stale": False}}}
        with mock.patch("quota.record", return_value=(end, None)) as record:
            code, out, err = self.invoke("--auto", "--trigger", "end", "--session", SESSION,
                                         "--log", str(log), defaults=False)
        self.assertEqual((code, err), (0, ""))
        record.assert_called_once_with("session-end", session=SESSION, log=log.parent / "quota.jsonl")
        row = self.read_log(log)[-1]
        self.assertEqual(json.loads(out), row)
        self.assertFalse(self.feedback.exists())  # no skeleton, no feedback directory needed
        self.assertEqual({k: row[k] for k in ("event", "session", "trigger", "mode", "end_reason", "reroll",
                                              "repo", "repo_dir", "transcript", "model", "calls",
                                              "subagent_calls", "blocked", "delegate")},
                         {"event": "summary", "session": SESSION, "trigger": "end", "mode": "claude-only",
                          "end_reason": "clear", "reroll": False, "repo": "owner/Toy", "repo_dir": "toy",
                          "transcript": str(self.transcript), "model": "claude-z-sonnet", "calls": 3,
                          "subagent_calls": 1, "blocked": 2, "delegate": {"status": 1, "ask": 1}})
        self.assertEqual(row["activity"], "2027-01-15T08:01:40.500000+00:00")  # the newer subagent file
        self.assertEqual((row["first_call"], row["last_call"]),
                         ("2026-10-10T21:00:00+00:00", "2026-10-10T23:00:00+00:00"))
        self.assertEqual(row["tokens"], {"input": 32, "cache_read": 1207000, "cache_write_5m": 0,
                                         "cache_write_1h": 0, "cache_write_unsplit": 4150, "output": 2270})
        weights, prices = cost.load_catalog_cost()
        main = cost.summarise_session(cost.parse_session_usage(self.transcript), weights, prices)
        self.assertEqual(row["cost_units_main"], round(main["cost_units"]))
        self.assertEqual(row["cost_units"], round(main["cost_units"] + 10 * weights["input"]
                                                  + 1000 * weights["cache_read"] + 100 * weights["cache_write"]
                                                  + 2000 * weights["output"]))
        self.assertEqual([(g["source"], g["model"], g["calls"]) for g in row["by_model"]],
                         [("main", "claude-a-opus", 1), ("main", "claude-z-sonnet", 2),
                          ("subagents", "claude-haiku-5-5", 1)])
        self.assertEqual(row["quota"], {"start": "2026-10-10T20:59:00+00:00", "end": "2026-10-10T23:01:00+00:00",
                                        "pools": {"claude-5h": {"start": 10.0, "end": 14.5, "delta": 4.5},
                                                  "codex-5h": {"start": None, "end": 4.0, "delta": None},
                                                  "cursor-auto": {"start": 40.0, "end": 1.0, "delta": None}}})

    def test_auto_backfill_summarises_only_sessions_that_look_over(self):
        log = self.root / "cache/sessions.jsonl"
        now = time.time()
        stamp = lambda minutes: time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now - 60 * minutes))
        active = self.root / "active.jsonl"
        active.write_text("", encoding="utf-8")
        os.utime(self.transcript, (now - 7200, now - 7200))

        def start(session, minutes, **fields):
            return {"event": "start", "session": session, "ts": stamp(minutes), "mode": "vanilla", **fields}

        def transcript(name, minutes):
            path = self.root / f"{name}.jsonl"
            path.write_text("", encoding="utf-8")
            os.utime(path, (now - 60 * minutes, now - 60 * minutes))
            return str(path)
        quiet, idle = transcript("quiet", 200), transcript("idle", 200)
        busy = transcript("busy", 300)
        (self.root / "busy/subagents").mkdir(parents=True)
        (self.root / "busy/subagents/agent-x.jsonl").write_text("", encoding="utf-8")  # still running
        self.write_log(log, [
            start("done", 180), {"event": "summary", "session": "done", "ts": stamp(170)},
            start("over", 180),
            start("fresh", 10),
            start("ending", 180), {"event": "end", "session": "ending", "ts": stamp(2)},
            start("ended", 180), {"event": "end", "session": "ended", "ts": stamp(30), "reason": "other"},
            # Went on writing after its summary (a resumed session or a back-filled live tab).
            start("resumed", 300, transcript=transcript("resumed", 90)),
            {"event": "summary", "session": "resumed", "ts": stamp(240), "activity": stamp(250)},
            start("resumed", 120, source="resume"),
            # Resumed without writing anything: the final summary must stand.
            start("idle", 300, transcript=idle), {"event": "end", "session": "idle", "reason": "clear"},
            {"event": "summary", "session": "idle", "ts": stamp(199),
             "activity": report.transcript_activity(Path(idle)).isoformat()},
            start("idle", 120, source="resume"),
            # A transcript that appeared after a zero-call summary.
            start("late", 300), {"event": "summary", "session": "late", "ts": stamp(250), "activity": None},
            start("late", 120, transcript=quiet),
            start("subagent", 400, transcript=busy),
            start("excluded", 180),
            start("active", 180, transcript=str(active)),
            start(SESSION, 180, transcript=str(self.transcript)),
            {"event": "delegate", "session": "no-start", "ts": stamp(600), "command": "ask"},
        ])
        with mock.patch("quota.record", side_effect=AssertionError("back-fill takes no reading")):
            code, out, err = self.invoke("--auto", "--backfill", "--exclude", "excluded", "--log", str(log),
                                         "--dry-run", defaults=False)
            self.assertEqual((code, err), (0, ""))
            self.assertEqual(len(self.read_log(log)), 23)  # dry run: printed, not written
            self.assertEqual([json.loads(line)["session"] for line in out.splitlines()],
                             ["over", "ended", "resumed", "late", SESSION])
            code, out, err = self.invoke("--auto", "--backfill", "--exclude", "excluded", "--log", str(log),
                                         defaults=False)
            self.assertEqual((code, err), (0, ""))
            added = self.read_log(log)[23:]
            self.assertEqual([(r["session"], r["trigger"], r["calls"], r["quota"]) for r in added],
                             [("over", "backfill", 0, None), ("ended", "backfill", 0, None),
                              ("resumed", "backfill", 0, None), ("late", "backfill", 0, None),
                              (SESSION, "backfill", 3, None)])
            self.assertEqual(added[3]["activity"], report.transcript_activity(Path(quiet)).isoformat())
            self.assertEqual(added[1]["end_reason"], "other")
            self.assertIsNone(added[0]["transcript"])
            self.assertEqual(self.invoke("--auto", "--backfill", "--exclude", "excluded", "--log", str(log),
                                         defaults=False), (0, "backfill: nothing due\n", ""))
            self.assertEqual(self.invoke("--auto", "--backfill", "--log", str(log), defaults=False),
                             (0, "excluded: 0 calls, backfilled\n", ""))

    def test_auto_flags_and_dry_run(self):
        log = self.root / "cache/sessions.jsonl"
        code, _, err = self.invoke("--backfill", defaults=False)
        self.assertEqual(code, 2)
        self.assertIn("need --auto", err)
        code, _, err = self.invoke("--auto", "--log", str(log), defaults=False)
        self.assertEqual(code, 2)
        self.assertIn("--auto needs --session", err)
        with mock.patch("quota.record", side_effect=AssertionError("dry run takes no reading")):
            code, out, err = self.invoke("--auto", "--session", str(self.transcript), "--log", str(log),
                                         "--dry-run", defaults=False)
        self.assertEqual((code, err), (0, ""))
        row = json.loads(out)
        self.assertEqual((row["session"], row["trigger"], row["mode"], row["calls"], row["quota"]),
                         (SESSION, "manual", None, 3, None))
        self.assertFalse(log.exists())

    def test_cli_registration_and_default_log_env(self):
        env = os.environ.copy()
        env["CLAUDE_DELEGATE_ROOT"] = str(self.root / "run-root")
        env["CLAUDE_CODE_SESSION_ID"] = SESSION
        run_root = Path(env["CLAUDE_DELEGATE_ROOT"])
        run_root.mkdir()
        (run_root / "runs.jsonl").write_text(json.dumps({
            "run": "env-root-run", "ts": "2026-10-10T21:00:00Z", "repo": "toy",
            "repo_path": str(self.repo),
            "agent": "cursor", "model": "auto", "rounds": [], "outcome": "applied",
        }) + "\n", encoding="utf-8")
        result = subprocess.run([str(SCRIPTS / "delegate"), "report", "--dry-run", "--json", "--no-quota",
                                 "--feedback-dir", str(self.feedback)],
                                capture_output=True, text=True, env=env, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["row"]["session"], SESSION)
        self.assertEqual(payload["runs_copied"], 1)
        self.assertFalse(self.feedback.exists())


if __name__ == "__main__":
    unittest.main()

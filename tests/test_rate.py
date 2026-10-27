"""Blinded item rating; only temporary logs, transcripts, repos and fake raters."""
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/delegate/scripts"
sys.path.insert(0, str(SCRIPTS))

import ask  # noqa: E402
import cost  # noqa: E402
import rate  # noqa: E402
import report  # noqa: E402

SESSION = "11111111-2222-4333-8444-555555555555"
OTHER = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
NOW = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)


def user(text="do the work", ts="2026-10-13T09:00:00Z", **fields):
    return {"type": "user", "message": {"content": text}, "timestamp": ts, **fields}


def queued_prompt(text="queued work", ts="2026-10-13T09:30:00Z", kind="human", mode="prompt", **fields):
    return {"type": "attachment", "timestamp": ts, "isSidechain": False, "attachment": {
        "type": "queued_command", "prompt": text, "commandMode": mode,
        "origin": {"kind": kind}, "timestamp": "2026-10-13T08:59:00Z"}, **fields}


def item(start=0, **fields):
    return {"start": start, "type": "code", "size": "M", "status": "finished",
            "continuation": False, "note": "Committed and accepted", **fields}


def assistant(mid, ts, model="test-model", **fields):
    return {"type": "assistant", "timestamp": ts, "message": {
        "id": mid, "model": model, "usage": {"input_tokens": 10, "cache_read_input_tokens": 100,
        "cache_creation_input_tokens": 20, "output_tokens": 5, **fields}}}


class RateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="rate-test-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.log = self.root / "sessions.jsonl"
        self.transcript = self.root / (SESSION + ".jsonl")
        self.write_rows(self.transcript, [user(), user("commit", "2026-10-13T10:00:00Z")])
        self.start = {"event": "start", "session": SESSION, "ts": "2026-10-13T08:00:00Z",
                      "mode": "vanilla", "u": 0.8, "cwd": str(self.root / "missing"),
                      "transcript": str(self.transcript)}
        self.summary = {"event": "summary", "session": SESSION, "last_call": "2026-10-13T10:00:00Z",
                        "transcript": str(self.transcript), "mode": "vanilla"}
        self.write_rows(self.log, [self.start, self.summary])
        patcher = mock.patch.dict(os.environ, {"CLAUDE_SESSION_LOG": str(self.log),
                                              "CLAUDE_CONFIG_DIR": str(self.root / "claude"),
                                              "CLAUDE_CODE_SESSION_ID": "rater-parent"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_rows(self, path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    def invoke(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                rate.main(list(args))
                code = 0
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def record(self, rows=None):
        return rate.select_sessions(rows or [self.start, self.summary], sessions=[SESSION])[0]

    def select(self, rows=None, **kwargs):
        return [r["session"] for r in rate.select_sessions(
            rows or [self.start, self.summary], now=NOW, **kwargs)]

    def test_old_transcripts_skip_meta_sidechain_and_tool_answers(self):
        rows = [user("typed"), user("expansion", isMeta=True), user("side", isSidechain=True),
                user([{"type": "tool_result", "content": "yes"}]),
                user([{"type": "text", "text": "mixed"}, {"type": "tool_result", "content": "yes"}]),
                {"type": "assistant", "message": {"content": "SECRET"}},
                user(None), user([{"type": "image", "source": "SECRET IMAGE"}])]
        self.assertEqual(rate.typed_prompts(rows), [{"text": "typed", "timestamp": rows[0]["timestamp"]}])

    def test_origin_rule_is_global_even_for_a_later_non_user_row(self):
        rows = [user("old unmarked"), user("human", origin={"kind": "human"}),
                user("generated", origin={"kind": "agent"}), user("null", origin=None),
                user("malformed", origin="human"), {"type": "progress", "origin": {}}]
        self.assertEqual([p["text"] for p in rate.typed_prompts(rows)], ["human"])
        self.assertEqual(rate.typed_prompts([user(), {"type": "assistant", "origin": {}}]), [])

    def test_human_queued_prompts_use_delivery_time_in_file_order(self):
        rows = [user("first"), queued_prompt(kind="task-notification", mode="task-notification"),
                queued_prompt(kind="peer"), queued_prompt("typed mid-turn"),
                queued_prompt("sidechain", isSidechain=True), queued_prompt("meta", isMeta=True),
                user("last", "2026-10-13T10:00:00Z")]
        self.assertEqual(rate.typed_prompts(rows), [
            {"text": "first", "timestamp": "2026-10-13T09:00:00Z"},
            {"text": "typed mid-turn", "timestamp": "2026-10-13T09:30:00Z"},
            {"text": "last", "timestamp": "2026-10-13T10:00:00Z"},
        ])

    def test_queued_prompts_require_human_origin_and_prompt_mode(self):
        for origin in (None, "human", {}, {"kind": "peer"}, {"kind": "task-notification"}):
            with self.subTest(origin=origin):
                row = queued_prompt()
                row["attachment"]["origin"] = origin
                self.assertEqual(rate.typed_prompts([row]), [])
        self.assertEqual(rate.typed_prompts([queued_prompt(mode="task-notification")]), [])

    def test_queued_prompts_share_user_text_cleanup_and_truncation(self):
        text = "<ide_selection>secret\nselection</ide_selection>\n" \
               "<system-reminder>secret\nreminder</system-reminder>" + "x" * 5000
        prompt = rate.typed_prompts([queued_prompt(text)])[0]
        self.assertEqual(prompt, {"text": "x" * (2000 - len(rate.TRUNCATED)) + rate.TRUNCATED,
                                  "timestamp": "2026-10-13T09:30:00Z"})
        self.assertEqual(rate.typed_prompts([queued_prompt("<command-name>/clear</command-name>")]), [])
        self.assertEqual(rate.typed_prompts([queued_prompt("<command-name>/next</command-name>")])[0]["text"],
                         "[continue from HANDOFF.md; its start-time copy is below]")

    def test_queued_delivery_time_keeps_rating_and_item_boundaries_nondecreasing(self):
        self.write_rows(self.transcript, [
            user("first"), assistant("first-call", "2026-10-13T09:01:00Z"),
            queued_prompt(), assistant("queued-call", "2026-10-13T09:31:00Z"),
            user("last", "2026-10-13T10:00:00Z"), assistant("last-call", "2026-10-13T10:01:00Z"),
        ])
        rating = rate.rating_row(self.record(), "packet", [item(0), item(1), item(2)], "cursor", "auto")
        self.assertEqual(rating["rubric"], rate.RUBRIC)
        self.assertEqual(rating["prompt_ts"], ["2026-10-13T09:00:00Z", "2026-10-13T09:30:00Z",
                                              "2026-10-13T10:00:00Z"])
        totals = rate.item_costs(rating, self.transcript, cost.DEFAULT_WEIGHTS, None)
        self.assertEqual([total["calls"] for total in totals], [1, 1, 1])

    def test_clear_local_commands_and_command_expansions_are_not_prompts(self):
        rows = [user("<command-message>clear</command-message>\n<command-name>/clear</command-name>"),
                user("<command-name>/clear</command-name>"),
                user(" <local-command-stdout>ok</local-command-stdout>"),
                user("<local-command-caveat>caution</local-command-caveat>"),
                user("expanded next", isMeta=True)]
        self.assertEqual(rate.typed_prompts(rows), [])

    def test_next_other_commands_and_args_rendered(self):
        rows = [user("<command-message>next</command-message>\n<command-name>/next</command-name>"),
                user("<command-name>/field-report</command-name>"),
                user("<command-name>/foo</command-name>\n<command-args>check x</command-args>")]
        self.assertEqual([p["text"] for p in rate.typed_prompts(rows)], [
            "[continue from HANDOFF.md; its start-time copy is below]",
            "[command /field-report]", "[command /foo check x]"])
        # A /clear row containing actual user prose is not silently discarded.
        self.assertEqual(rate.typed_prompts([user("<command-name>/clear</command-name>\nkeep this")])[0]["text"],
                         "[command /clear]\nkeep this")

    def test_images_selection_reminders_and_pasted_content(self):
        text = ("<ide_selection>secret\nselection</ide_selection>before"
                "<system-reminder>secret\nreminder</system-reminder>"
                '<pasted_content id="1">keep\nthis</pasted_content>')
        prompts = rate.typed_prompts([user([{"type": "text", "text": text},
                                           {"type": "image", "source": "never include bytes"},
                                           {"type": "text", "text": "after"}])])
        self.assertEqual(prompts[0]["text"], 'before<pasted_content id="1">keep\nthis</pasted_content>\n[image]\nafter')

    def test_prompt_truncation_preserves_timestamp_and_limit(self):
        prompt = rate.typed_prompts([user("x" * 5000)])[0]
        self.assertEqual(len(prompt["text"]), 2000)
        self.assertTrue(prompt["text"].endswith("… [truncated]"))
        self.assertEqual(prompt["timestamp"], "2026-10-13T09:00:00Z")

    def test_packet_sections_timings_redaction_and_determinism(self):
        words = ("DELEGATE delegation Codex CURSOR-agent cursor ORCHESTRATOR orchestrating "
                 "claude-only Vanilla control session session-mode session mode gpt-6.1-sol "
                 "SOL second opinion second opinions Cursor Auto --agent both --agent=codex both models "
                 "other vendor vendors")
        handoff = self.root / "start.md"
        handoff.write_text(words + "\ncontinue this item", encoding="utf-8")
        self.start["handoff"] = str(handoff)
        self.write_rows(self.transcript, [user(words), user("commit", "2026-10-13T11:00:00+02:00"),
                                         {"type": "assistant", "message": {"content": "SECRET ASSISTANT"}},
                                         {"type": "progress", "data": "SECRET TOOL"}])
        record = self.record()
        with mock.patch("rate.session_commits", return_value="abc123 delegate Codex work"):
            packet = rate.build_packet(record)
            self.assertEqual(packet, rate.build_packet(record))
        self.assertIn("## Prompts", packet)
        self.assertIn("### P0 (0.0 min)", packet)
        self.assertIn("### P1 (0.0 min)", packet)
        self.assertIn("## HANDOFF.md at session start", packet)
        self.assertIn("\n> continue this item\n", packet)
        self.assertIn("## Commits in the session window", packet)
        self.assertIn("abc123 [redacted] [redacted] work", packet)
        self.assertNotIn("SECRET", packet)
        for pattern in rate.REDACT:
            self.assertNotRegex(packet.lower(), pattern)
        self.assertGreater(packet.count("[redacted]"), 30)
        self.assertNotIn(str(self.root), packet)

    def test_handoff_truncated_and_missing_copy_omits_section(self):
        handoff = self.root / "handoff.md"
        handoff.write_text("h" * 9000, encoding="utf-8")
        self.start["handoff"] = str(handoff)
        with mock.patch("rate.session_commits", return_value="(none)"):
            packet = rate.build_packet(self.record())
            body = packet.split("## HANDOFF.md at session start\n", 1)[1].split("\n\n## HANDOFF.md at session end", 1)[0]
            self.assertEqual(len(body), len("> ") + 8000)  # one line, quoted
            self.assertTrue(body.endswith(rate.TRUNCATED))
            handoff.unlink()
            self.assertNotIn("## HANDOFF.md at session start", rate.build_packet(self.record()))

    def test_next_marker_says_when_no_copy_was_saved(self):
        self.write_rows(self.transcript, [user("<command-name>/next</command-name>\nfocus x")])
        handoff = self.root / "handoff.md"
        handoff.write_text("continue this item", encoding="utf-8")
        self.start["handoff"] = str(handoff)
        with mock.patch("rate.session_commits", return_value="(none)"):
            self.assertIn(rate.NEXT_PROMPT + "\nfocus x", rate.build_packet(self.record()))
            for missing in (None, str(self.root / "gone.md")):
                self.start["handoff"] = missing
                packet = rate.build_packet(self.record())
                self.assertIn(rate.NEXT_PROMPT_NO_COPY + "\nfocus x", packet)
                self.assertNotIn(rate.NEXT_PROMPT, packet)

    def test_end_state_prefers_session_end_copy_then_next_start_within_gap(self):
        start_copy, next_copy, end_copy = (self.root / f"{name}.md" for name in ("start", "next", "end"))
        start_copy.write_text("next: build the viewer", encoding="utf-8")
        next_copy.write_text("viewer done 2026-10-13; next: Codex review", encoding="utf-8")
        self.start.update(repo_dir="proj", handoff=str(start_copy))
        # Last call 10:00Z; this start at 10:20Z is within the 30-minute gap.
        later = {"event": "start", "session": OTHER, "ts": "2026-10-13T12:20:00+02:00", "source": "clear",
                 "repo_dir": "proj", "handoff": str(next_copy)}
        record = self.record([self.start, self.summary, later])
        self.assertEqual((record["end_handoff"], record["end_source"]), (str(next_copy), "next-start"))
        with mock.patch("rate.session_commits", return_value="(none)"):
            packet = rate.build_packet(record)
        self.assertIn("## Prompts (session date 2026-10-13)", packet)
        self.assertIn("## HANDOFF.md at session start\n> next: build the viewer\n\n"
                      "## HANDOFF.md at session end\n> viewer done 2026-10-13; next: [redacted] review\n\n"
                      "## Commits", packet)
        for change in ({"ts": "2026-10-13T10:31:00Z"}, {"ts": "2026-10-13T09:59:00Z"}, {"repo_dir": "other"},
                       {"source": "resume"}, {"session": SESSION}, {"handoff": str(self.root / "gone.md")},
                       {"handoff": None}):
            record = self.record([self.start, self.summary, {**later, **change}])
            self.assertEqual((record["end_handoff"], record["end_source"]), (None, None), change)
        earlier = {**later, "session": "early", "ts": "2026-10-13T10:05:00Z", "handoff": str(start_copy)}
        # An earlier start in the gap that can't vouch for the end state blocks the later one:
        # no copy, a resume, or a repeated start row whose copy dates from its first start.
        repeated = {**earlier, "ts": "2026-10-13T09:00:00Z"}
        for blockers in ([{**earlier, "handoff": str(self.root / "gone.md")}], [{**earlier, "handoff": None}],
                         [{**earlier, "source": "resume"}], [repeated, earlier]):
            record = self.record([self.start, self.summary, *blockers, later])
            self.assertEqual((record["end_handoff"], record["end_source"]), (None, None), blockers)
        record = self.record([self.start, self.summary, later, earlier])
        self.assertEqual(record["end_handoff"], str(start_copy))
        with mock.patch("rate.session_commits", return_value="(none)"):
            self.assertIn("## HANDOFF.md at session end\n(unchanged from the start copy)",
                          rate.build_packet(record))
        # Without a repo_dir the next start must share the cwd.
        del self.start["repo_dir"]
        self.assertIsNone(self.record([self.start, self.summary, later])["end_handoff"])
        self.assertEqual(self.record([self.start, self.summary, {**later, "cwd": self.start["cwd"]}])
                         ["end_handoff"], str(next_copy))
        # The session's own SessionEnd copy wins over any next start.
        end_copy.write_text("x" * 9000, encoding="utf-8")
        ended = {"event": "end", "session": SESSION, "handoff": str(end_copy)}
        record = self.record([self.start, self.summary, later, ended])
        self.assertEqual((record["end_handoff"], record["end_source"]), (str(end_copy), "end"))
        with mock.patch("rate.session_commits", return_value="(none)"):
            body = rate.build_packet(record).split("## HANDOFF.md at session end\n", 1)[1].split("\n\n## Commits")[0]
        self.assertEqual(len(body), len("> ") + rate.HANDOFF_LIMIT)

    def test_packet_cap_trims_longest_prompts_first_and_is_deterministic(self):
        prompts = [user("x" * 1990) for _ in range(40)] + [user("KEEP SHORT")]
        self.write_rows(self.transcript, prompts)
        record = self.record()
        original = [dict(p) for p in record["prompts"]]
        with mock.patch("rate.session_commits", return_value="(none)"):
            packet = rate.build_packet(record)
            self.assertEqual(packet, rate.build_packet(record))
        self.assertLessEqual(len(packet), 60000)
        self.assertIn("longest prompts trimmed first", packet)
        self.assertIn("KEEP SHORT", packet)
        self.assertIn("### P40", packet)
        self.assertEqual(record["prompts"], original)

    def test_packet_cap_includes_redaction_expansion_and_large_commit_subjects(self):
        self.write_rows(self.transcript, [user("sol " * 600) for _ in range(40)])
        with mock.patch("rate.session_commits", return_value="x" * 61000):
            packet = rate.build_packet(self.record())
        self.assertEqual(len(packet), 60000)
        self.assertIn("longest prompts trimmed first", packet)
        self.assertTrue(packet.endswith(rate.TRUNCATED))
        self.assertNotRegex(packet, r"\bsol\b")

    def git(self, repo, *args, input=None, env=None):
        result = subprocess.run(["git", "-C", str(repo), *args], input=input,
                                env=env, capture_output=True, text=True, check=True)
        return result.stdout.strip()

    def make_commits(self, repo, dates):
        """Use detached test HEAD and plumbing; never commit in the worktree."""
        repo.mkdir()
        self.git(repo, "init", "--quiet")
        parent = None
        for i, (stamp, subject) in enumerate(dates):
            blob = self.git(repo, "hash-object", "-w", "--stdin", input=str(i) + "\n")
            tree = self.git(repo, "mktree", input=f"100644 blob {blob}\tfile.txt\n")
            env = {**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.test",
                   "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.test",
                   "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
            args = ["commit-tree", tree, "-m", subject]
            if parent:
                args += ["-p", parent]
            parent = self.git(repo, *args, env=env)
        self.git(repo, "update-ref", "--no-deref", "HEAD", parent)

    def test_git_commits_only_inside_window_with_offset_and_ten_minute_grace(self):
        repo = self.root / "repo"
        self.make_commits(repo, [
            ("2026-10-11T13:07:00Z", "BEFORE"),
            ("2026-10-11T13:09:00Z", "INSIDE codex"),
            ("2026-10-13T08:40:00Z", "GRACE"),
            ("2026-10-13T08:42:00Z", "AFTER"),
        ])
        self.start["cwd"] = str(repo)
        self.summary["last_call"] = "2026-10-13T08:31:00Z"
        self.write_rows(self.transcript, [user(ts="2026-10-11T13:08:00Z")])
        packet = rate.build_packet(self.record())
        self.assertIn("INSIDE [redacted]", packet)
        self.assertIn("GRACE", packet)
        self.assertIn("file changed", packet)
        self.assertNotIn("BEFORE", packet)
        self.assertNotIn("AFTER", packet)

    def test_commits_at_most_fifty(self):
        repo = self.root / "many-commits"
        self.make_commits(repo, [(f"2026-10-13T09:{i:02d}:00Z", f"commit-{i}") for i in range(55)])
        text = rate.session_commits(repo, NOW - timedelta(hours=4), NOW)
        self.assertEqual(sum("commit-" in line for line in text.splitlines()), 50)

    def test_git_failures_unavailable(self):
        first = NOW - timedelta(hours=1)
        self.assertEqual(rate.session_commits(self.root / "gone", first, NOW), "(unavailable)")
        for error in (OSError("gone"), subprocess.TimeoutExpired("git", 30)):
            with mock.patch("rate.subprocess.run", side_effect=error):
                self.assertEqual(rate.session_commits(self.root, first, NOW), "(unavailable)")

    def test_no_start_row_uses_first_transcript_cwd_and_summary_window(self):
        self.write_rows(self.transcript, [user(cwd=str(self.root)), user("next", cwd="wrong")])
        with mock.patch("rate.session_commits", return_value="(none)") as commits:
            packet = rate.build_packet(self.record([self.summary]))
        commits.assert_called_once_with(str(self.root), datetime(2026, 10, 13, 9, tzinfo=timezone.utc),
                                        datetime(2026, 10, 13, 10, tzinfo=timezone.utc))
        self.assertNotIn("## HANDOFF.md at session start", packet)

    def test_explicit_without_summary_uses_last_call_timestamp(self):
        self.write_rows(self.transcript, [user(cwd=str(self.root)), assistant("m", "2026-10-13T11:00:00Z")])
        with mock.patch("rate.session_commits", return_value="(none)") as commits:
            rate.build_packet(self.record([self.start]))
        self.assertEqual(commits.call_args.args[2], NOW - timedelta(hours=1))

    def test_selection_drawn_only_from_earliest_assignment(self):
        rows = []
        for i, fields in enumerate(({}, {"u": None}, {"forced": True}, {"excluded": True}, {"unassigned": True})):
            session = f"session{i}"
            rows += [{**self.start, "session": session, **fields},
                     {**self.start, "session": session, "source": "resume", "u": None, "mode": "orchestrator"},
                     {**self.summary, "session": session}]
        rows += [{**self.summary, "session": "no-start"}, {**self.start, "session": "no-summary"}]
        self.assertEqual(self.select(rows), ["session0"])
        self.assertEqual(rate.select_sessions(rows, now=NOW)[0]["start"]["mode"], "vanilla")

    def test_selection_min_age_and_last_summary_win(self):
        self.assertEqual(self.select(min_age=120), [SESSION])
        self.assertEqual(self.select(min_age=121), [])
        newer = {**self.summary, "last_call": "2026-10-13T11:50:00Z"}
        self.assertEqual(self.select([self.start, self.summary, newer]), [])
        self.assertEqual(self.select([self.start, self.summary, newer], min_age=10), [SESSION])
        self.assertEqual(self.select([self.start, {**self.summary, "last_call": None}]), [])

    def test_selection_rubric_growth_force_and_last_rating_win(self):
        rating = {"event": "rating", "session": SESSION, "rubric": rate.RUBRIC, "prompts": 2}
        rows = [self.start, self.summary, rating]
        self.assertEqual(self.select(rows), [])
        self.assertEqual(self.select(rows, force=True), [SESSION])
        self.assertEqual(self.select(rows + [{**rating, "rubric": "v0"}]), [SESSION])
        self.assertEqual(self.select(rows + [{**rating, "prompts": 1}]), [SESSION])
        self.write_rows(self.transcript, [user(), user("followup"), user("commit")])
        self.assertEqual(self.select(rows), [SESSION])

    def test_rated_session_with_missing_transcript_is_skipped_unless_asked(self):
        rating = {"event": "rating", "session": SESSION, "rubric": rate.RUBRIC, "prompts": 2}
        rows = [self.start, self.summary, rating]
        self.transcript.unlink()
        self.assertEqual(self.select(rows), [])
        self.assertEqual(self.select(rows, force=True), [SESSION])
        self.assertEqual(self.select(rows + [{**rating, "rubric": "v0"}]), [SESSION])

    def test_old_transcripts_skip_interrupt_markers(self):
        rows = [user("typed"), user("[Request interrupted by user]"),
                user([{"type": "text", "text": "[Request interrupted by user for tool use]"}])]
        self.assertEqual([p["text"] for p in rate.typed_prompts(rows)], ["typed"])

    def test_selection_since_earliest_start_local_dates_and_offsets(self):
        rows = [self.start, {**self.start, "ts": "2026-10-13T11:00:00Z", "u": None}, self.summary]
        self.assertEqual(self.select(rows, since="2026-10-13T08:00:00Z"), [SESSION])
        self.assertEqual(self.select(rows, since="2026-10-13T10:00:01+02:00"), [])
        self.assertEqual(self.select(rows, since="2026-10-14"), [])
        # Match cost's local-midnight interpretation, independent of host timezone.
        local_midnight = cost._parse_ts("2026-10-13").astimezone()
        start = {**self.start, "ts": local_midnight.isoformat()}
        self.assertEqual(self.select([start, self.summary], since="2026-10-13"), [SESSION])
        with self.assertRaisesRegex(ValueError, "--since"):
            self.select(since="not a date")

    def test_explicit_prefix_ignores_draw_age_rubric_and_since_and_deduplicates(self):
        rows = [{**self.start, "u": None, "forced": True}, self.summary,
                {"event": "rating", "session": SESSION, "rubric": rate.RUBRIC, "prompts": 999}]
        self.assertEqual(self.select(rows, sessions=[SESSION[:8], SESSION], min_age=9999,
                                     since="2099-01-01"), [SESSION])
        self.assertEqual(self.select([self.summary], sessions=[SESSION[:8]]), [SESSION])
        with self.assertRaisesRegex(ValueError, "unknown"):
            self.select(sessions=["unknown"])
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.select(rows + [{**self.summary, "session": SESSION[:-1] + "6"}], sessions=[SESSION[:8]])

    def test_transcript_summary_preferred_else_start_and_read_errors_reported(self):
        other = self.root / "other.jsonl"
        self.write_rows(other, [user("preferred")])
        self.assertEqual(self.record([self.start, {**self.summary, "transcript": str(other)}])["prompts"][0]["text"],
                         "preferred")
        self.assertEqual(len(self.record([self.start, {**self.summary, "transcript": None}])["prompts"]), 2)
        record = self.record([{**self.summary, "transcript": None}])
        self.assertIn("no transcript", record["error"])
        self.transcript.unlink()
        self.assertIn("error", self.record())

    def test_malformed_json_lines_are_ignored(self):
        with self.transcript.open("a") as stream:
            stream.write('bad json\n[]\nnull\n{"unfinished":\n')
        self.assertEqual(len(self.record()["prompts"]), 2)
        with self.log.open("a") as stream:
            stream.write("bad json\n[]\nnull\n")
        self.assertEqual(len(rate.session_records(rate.json_rows(self.log))), 1)

    def test_reply_first_top_level_object_and_fence(self):
        payload = json.dumps({"items": [item(note='A brace } and quote " inside') ]})
        self.assertEqual(rate.parse_reply("Here is the result:\n```json\n" + payload + "\n```", 2),
                         [item(note='A brace } and quote " inside')])
        self.assertEqual(rate.parse_reply(payload + json.dumps({"items": [item(size="XL")]}), 2)[0]["size"], "M")
        # Do not accept an item object nested inside a top-level array.
        with self.assertRaisesRegex(ValueError, "top-level"):
            rate.parse_reply("[" + payload + "]", 2)

    def test_reply_validation_of_shape_types_enums_and_boundaries(self):
        bad = [{}, {"items": {}}, {"items": []}, {"items": [None]}, {"items": [{"start": 0}]},
               {"items": [item(start=True)]}, {"items": [item(start=0.0)]},
               {"items": [item(start=-1)]}, {"items": [item(start=1)]},
               {"items": [item(), item(start=0)]}, {"items": [item(), item(start=2)]},
               {"items": [item(), item(start=1), item(start=0)]},
               {"items": [item(type="coding")]}, {"items": [item(type=[])]},
               {"items": [item(size="s")]}, {"items": [item(status="done")]},
               {"items": [item(continuation=1)]}, {"items": [item(note=None)]},
               {"items": [item(note="word " * 16)]}]
        for reply in bad:
            with self.subTest(reply=reply), self.assertRaises(ValueError):
                rate.parse_reply(json.dumps(reply), 2)
        with self.assertRaises(ValueError):
            rate.parse_reply("not JSON", 2)
        self.assertEqual(rate.parse_reply('{"items": []}', 0), [])
        self.assertEqual(rate.parse_reply(json.dumps({"items": [item(), item(1)]}), 2), [item(), item(1)])

    def test_field_report_prompts_start_their_own_typed_items(self):
        rows = [user("work"), user("<command-name>/field-report</command-name>\n<command-args>x</command-args>"),
                user("commit")]
        self.assertEqual(rate.field_report_prompts(rate.typed_prompts(rows)), [1])
        report = item(1, type="field-report")
        self.assertEqual(rate.parse_reply(json.dumps({"items": [item(), report]}), 3, [1]), [item(), report])
        for items, starts, message in (
                ([item(), item(1)], [1], "item 1 type must be field-report"),
                ([item(), item(2, type="field-report")], [1], "item 1 type must be field-report"),
                ([item(), report], [], "item 1 type must be field-report"),
                ([item(type="field-report")], [1], "item 0 type must be field-report"),
                ([item()], [1], "P1 must each start an item of type field-report")):
            with self.assertRaisesRegex(ValueError, message):
                rate.parse_reply(json.dumps({"items": items}), 3, starts)

    def test_one_invalid_reply_retried_in_fresh_empty_directory_and_cleaned_up(self):
        directories, prompts = [], []

        def fake(prompt, model, directory, timeout, confine=False):
            self.assertEqual((model, timeout), ("auto", 600))
            self.assertTrue(confine)
            self.assertEqual(list(directory.iterdir()), [])
            self.assertNotEqual(directory, self.root)
            self.assertIn(rate.RUBRIC_TEXT, prompt)
            directories.append(directory)
            prompts.append(prompt)
            (directory / "rater-created-file").write_text("test")
            reply = '{"items": []}' if len(prompts) == 1 else json.dumps({"items": [item()]})
            return reply, None, 1, model

        with mock.patch("ask.run_cursor", side_effect=fake), mock.patch("runs.log_delegation") as logging:
            self.assertEqual(rate.rate_packet("## Prompts\nP0\nwork", 1), [item()])
        logging.assert_not_called()
        self.assertIn("Validation error", prompts[1])
        self.assertNotEqual(directories[0], directories[1])
        self.assertTrue(all(not d.exists() for d in directories))

    def test_two_invalid_replies_write_nothing_continue_and_exit_nonzero(self):
        other = [{**self.start, "session": OTHER}, {**self.summary, "session": OTHER}]
        self.write_rows(self.log, [self.start, self.summary, *other])
        with mock.patch("ask.run_cursor", side_effect=[("bad", None, 0, "auto"),
                                                       ('{"items": []}', None, 0, "auto"),
                                                       (json.dumps({"items": [item()]}), None, 0, "auto")]) as run:
            code, out, err = self.invoke("--session", SESSION, "--session", OTHER)
        self.assertEqual(code, 1)
        self.assertEqual(run.call_count, 3)
        self.assertIn("first start must be 0", err)
        self.assertIn(OTHER[:8], out)
        ratings = [r for r in rate.json_rows(self.log) if r["event"] == "rating"]
        self.assertEqual([r["session"] for r in ratings], [OTHER])

    def test_rater_failure_and_missing_transcript_are_reported_without_writes(self):
        before = self.log.read_bytes()
        with mock.patch("ask.run_cursor", side_effect=ask.AskFailed("quota exhausted", 3)) as run:
            code, _, err = self.invoke("--session", SESSION)
        self.assertEqual(code, 1)
        self.assertIn("quota exhausted", err)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(self.log.read_bytes(), before)
        self.transcript.unlink()
        with mock.patch("ask.run_cursor") as run:
            code, _, err = self.invoke("--session", SESSION)
        self.assertEqual(code, 1)
        self.assertIn("No such file", err)
        run.assert_not_called()

    def test_codex_refused(self):
        before = self.log.read_bytes()
        with mock.patch("ask.run_codex") as codex, mock.patch("ask.run_cursor") as cursor:
            code, _, _ = self.invoke("--session", SESSION, "--agent", "codex")
            self.assertIn("Cursor", str(code))
            with self.assertRaises(ValueError):
                rate.rate_packet("packet", 1, "codex")
        codex.assert_not_called()
        cursor.assert_not_called()
        self.assertEqual(self.log.read_bytes(), before)

    def test_cursor_model_override(self):
        with mock.patch("ask.run_cursor", return_value=(json.dumps({"items": [item()]}), None, 0, "x")) as run:
            rate.rate_packet("packet", 1, model="custom-cursor")
        self.assertEqual(run.call_args.args[1], "custom-cursor")
        self.assertEqual(run.call_args.kwargs, {"confine": True})

    def test_cursor_rating_always_confined(self):
        with mock.patch("ask.run_cursor", return_value=(json.dumps({"items": [item()]}), None, 0, "auto")) as run:
            self.assertEqual(rate.rate_packet("packet", 1, timeout=12), [item()])
        self.assertEqual(run.call_args.args[1], "auto")
        self.assertEqual(run.call_args.args[3], 12)
        self.assertEqual(run.call_args.kwargs, {"confine": True})

    def test_outside_read_reports_session_without_retry_or_rating(self):
        before = self.log.read_bytes()
        error = ask.OutsideRead("cursor-agent: readToolCall outside workspace: /outside/a")
        with mock.patch("ask.run_cursor", side_effect=error) as run:
            code, out, err = self.invoke("--session", SESSION)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn(SESSION[:8], err)
        self.assertIn(str(error), err)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.kwargs, {"confine": True})
        self.assertEqual(self.log.read_bytes(), before)

    def test_rating_row_shape_append_only_hash_and_no_delegation_row(self):
        before = self.log.read_bytes()
        with mock.patch("ask.run_cursor", return_value=(json.dumps({"items": [item()]}), None, 0, "auto")), \
                mock.patch("runs.log_delegation") as logging:
            self.assertEqual(self.invoke("--session", SESSION)[0], 0)
        logging.assert_not_called()
        self.assertTrue(self.log.read_bytes().startswith(before))
        rows = rate.json_rows(self.log)
        self.assertEqual([r["event"] for r in rows], ["start", "summary", "rating"])
        row = rows[-1]
        self.assertEqual(set(row), {"event", "session", "ts", "rubric", "rater", "prompts",
                                    "prompt_ts", "packet_sha256", "end_state", "items"})
        self.assertEqual((row["session"], row["rubric"], row["rater"], row["prompts"], row["items"]),
                         (SESSION, rate.RUBRIC, "cursor/auto", 2, [item()]))
        self.assertEqual(row["prompt_ts"], ["2026-10-13T09:00:00Z", "2026-10-13T10:00:00Z"])
        self.assertIsNotNone(datetime.fromisoformat(row["ts"]).utcoffset())
        packet = rate.build_packet(self.record())
        self.assertEqual(row["packet_sha256"], hashlib.sha256(packet.encode("utf-8")).hexdigest())

    def test_cli_passes_field_report_prompts_and_logs_end_state(self):
        self.write_rows(self.transcript, [user(), user("<command-name>/field-report</command-name>")])
        end_copy = self.root / "end.md"
        end_copy.write_text("done", encoding="utf-8")
        self.write_rows(self.log, [self.start, self.summary,
                                   {"event": "end", "session": SESSION, "handoff": str(end_copy)}])
        reply = [item(), item(1, type="field-report")]
        with mock.patch("rate.rate_packet", return_value=reply) as rater:
            self.assertEqual(self.invoke("--session", SESSION)[0], 0)
        self.assertEqual(rater.call_args.args[5], [1])
        row = rate.json_rows(self.log)[-1]
        self.assertEqual((row["end_state"], row["items"]), ("end", reply))

    def test_append_rating_is_one_unbuffered_write(self):
        stream = mock.MagicMock()
        stream.__enter__.return_value = stream
        with mock.patch.object(Path, "open", return_value=stream) as opened:
            rate.append_rating(self.log, {"event": "rating", "session": SESSION})
        opened.assert_called_once_with("ab", buffering=0)
        stream.write.assert_called_once()
        self.assertTrue(stream.write.call_args.args[0].endswith(b"\n"))

    def test_zero_typed_prompts_records_empty_items_without_call(self):
        self.write_rows(self.transcript, [user("expansion", isMeta=True), assistant("m", "2026-10-13T10:00:00Z")])
        with mock.patch("ask.run_cursor") as run:
            self.assertEqual(self.invoke("--session", SESSION)[0], 0)
        run.assert_not_called()
        row = rate.json_rows(self.log)[-1]
        self.assertEqual((row["prompts"], row["items"], row["prompt_ts"]), (0, [], []))
        self.assertEqual(row["rater"], "cursor/auto")

    def test_dry_run_print_packet_does_not_call_or_write(self):
        before = self.log.read_bytes()
        with mock.patch("ask.run_cursor") as run:
            code, out, err = self.invoke("--session", SESSION[:8], "--dry-run", "--print-packet")
        self.assertEqual((code, err), (0, ""))
        self.assertIn(SESSION[:8] + " mode=vanilla prompts=2 packet=", out)
        self.assertIn("## Prompts", out)
        self.assertNotIn("## HANDOFF.md at session start", out)
        self.assertEqual(before, self.log.read_bytes())
        run.assert_not_called()

    def test_cli_registration_explicit_log_and_invalid_flags(self):
        env = os.environ.copy()
        env["CLAUDE_SESSION_LOG"] = str(self.root / "not-the-log")
        result = subprocess.run([str(SCRIPTS / "delegate"), "rate", "--session-log", str(self.log),
                                 "--session", SESSION[:8], "--dry-run"],
                                capture_output=True, text=True, env=env)
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertIn("prompts=2", result.stdout)
        for args in (("--print-packet",), ("--min-age", "-1"), ("--min-age", "nan"), ("--timeout", "0")):
            with self.subTest(args=args):
                self.assertIsInstance(self.invoke(*args)[0], str)
        self.assertEqual(self.invoke("--session", "unknown")[0], 2)

    def test_default_empty_selection_has_no_side_effects(self):
        self.log.unlink()
        with mock.patch("ask.run_cursor") as run:
            self.assertEqual(self.invoke("--dry-run"), (0, "", ""))
        self.assertFalse(self.log.exists())
        run.assert_not_called()


class ItemCostTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="item-cost-test-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.transcript = self.root / "transcript.jsonl"
        self.weights = {"input": 2, "cache_read": 0.2, "cache_write": 3, "output": 7}
        self.prices = {"models": {"test-model": {k: 1 for k in cost.TOKEN_CLASSES if k != "cache_write_unsplit"}}}
        self.rating = {"items": [item(), item(2), item(3)], "prompt_ts": [
            "2026-10-13T09:00:00Z", "2026-10-13T09:30:00Z",
            "2026-10-13T10:00:00Z", "2026-10-13T11:00:00Z"]}

    def write_calls(self, path, calls):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(c) + "\n" for c in calls), encoding="utf-8")

    def test_boundaries_before_first_exact_next_start_after_last_and_subagents(self):
        calls = [assistant("before", "2026-10-13T08:00:00Z"),
                 assistant("first", "2026-10-13T09:00:00Z"),
                 assistant("within", "2026-10-13T09:59:59Z"),
                 assistant("second", "2026-10-13T12:00:00+02:00"),
                 assistant("last", "2026-10-13T11:00:00Z"),
                 assistant("after", "2026-10-13T13:00:00Z")]
        self.write_calls(self.transcript, calls)
        self.write_calls(self.transcript.with_suffix("") / "subagents/agent-a.jsonl", [
            calls[0], assistant("sub", "2026-10-13T10:30:00Z")])
        before = json.dumps(self.rating)
        totals = rate.item_costs(self.rating, self.transcript, self.weights, self.prices)
        self.assertEqual([t["calls"] for t in totals], [3, 2, 2])
        unit = 10 * 2 + 100 * 0.2 + 20 * 3 + 5 * 7
        usd = 135 / 1e6
        for total, n in zip(totals, (3, 2, 2)):
            self.assertEqual(total["cost_units"], n * unit)
            self.assertAlmostEqual(total["usd"], n * usd)
            self.assertAlmostEqual(total["share"], n / 7)
            self.assertEqual(total["unpriced_calls"], 0)
        self.assertEqual(json.dumps(self.rating), before)
        summary = report.summary_row("session", [], self.transcript, "manual", self.weights, self.prices)
        self.assertEqual(round(sum(t["cost_units"] for t in totals)), summary["cost_units"])
        self.assertAlmostEqual(sum(t["usd"] for t in totals), summary["usd_list_price"])

    def test_unpriced_calls_and_empty_item(self):
        self.write_calls(self.transcript, [assistant("known", "2026-10-13T09:00:00Z"),
                                          assistant("unknown", "2026-10-13T09:30:00Z", model="unknown"),
                                          assistant("last", "2026-10-13T11:00:00Z", speed="fast")])
        totals = rate.item_costs(self.rating, self.transcript, self.weights, self.prices)
        self.assertEqual([t["unpriced_calls"] for t in totals], [1, 0, 1])
        self.assertEqual([t["share"] for t in totals], [1, 0, None])
        self.assertEqual([t["usd"] for t in totals], [135 / 1e6, 0, None])
        self.assertEqual(sum(t["calls"] for t in totals), 3)

    def test_unpriced_session_has_no_usd_share(self):
        self.write_calls(self.transcript, [assistant("unpriced", "2026-10-13T10:00:00Z", model="unknown")])
        totals = rate.item_costs(self.rating, self.transcript, self.weights, None)
        self.assertEqual([t["share"] for t in totals], [None, None, None])
        self.assertIsNone(totals[1]["usd"])
        self.assertEqual(totals[1]["unpriced_calls"], 1)

    def test_no_items_and_untimed_calls_preserve_totals(self):
        self.assertEqual(rate.item_costs({"items": [], "prompt_ts": []}, "unused", {}, None), [])
        self.write_calls(self.transcript, [assistant("untimed", None)])
        totals = rate.item_costs(self.rating, self.transcript, self.weights, self.prices)
        self.assertEqual([t["calls"] for t in totals], [1, 0, 0])

    def test_invalid_prompt_boundary_timestamps_raise(self):
        self.write_calls(self.transcript, [])
        self.rating["prompt_ts"][2] = None
        with self.assertRaisesRegex(ValueError, "timestamps"):
            rate.item_costs(self.rating, self.transcript, self.weights, self.prices)



class ItemReportTests(unittest.TestCase):
    """`delegate cost --by-mode` with ratings: rate.attach_items on cost.by_mode's result."""
    setUp = ItemCostTests.setUp
    write_calls = ItemCostTests.write_calls

    def session_rows(self, session, mode, transcript, usd, rating=None):
        rows = [{"event": "start", "session": session, "ts": "2026-10-13T08:00:00Z", "mode": mode, "u": 0.1},
                {"event": "summary", "session": session, "transcript": str(transcript), "calls": 3,
                 "usd_list_price": usd}]
        return rows + ([{"event": "rating", "session": session, "rubric": rate.RUBRIC, **rating}] if rating else [])

    def test_headline_divides_all_rated_usd_by_finished_items(self):
        self.write_calls(self.transcript, [assistant("a", "2026-10-13T09:10:00Z"),
                                           assistant("b", "2026-10-13T10:10:00Z"),
                                           assistant("c", "2026-10-13T11:10:00Z")])
        rating = {**self.rating, "items": [item(), item(2, status="partial", size="S"),
                                           item(3, type="docs", continuation=True)]}
        other = self.root / "other.jsonl"
        self.write_calls(other, [assistant("d", "2026-10-13T09:10:00Z")])
        rows = (self.session_rows("rated", "vanilla", self.transcript, 6.0, rating)
                + self.session_rows("unrated", "vanilla", other, 100.0)
                + self.session_rows("old", "claude-only", other, 1.0, {**rating, "rubric": "v0"})
                + [{"event": "start", "session": "pending", "ts": "2026-10-13T08:00:00Z",
                    "mode": "vanilla", "u": 0.2},
                   {"event": "start", "session": "ghost", "ts": "2026-10-13T08:00:00Z", "mode": "vanilla", "u": 0.3},
                   {"event": "summary", "session": "ghost", "transcript": None, "calls": 0, "usd_list_price": None}])
        result = rate.attach_items(cost.by_mode(rows, []), rows, self.weights, self.prices)
        items = result["arms"]["vanilla"]["items"]
        self.assertEqual((items["rated_sessions"], items["unrated_sessions"], items["items"]), (1, 1, 3))
        self.assertEqual((items["finished"], items["partial"], items["continuations"]), (2, 1, 1))
        self.assertEqual(items["usd_per_finished"], 3.0)  # the session's 6.0 USD, unfinished work included
        self.assertAlmostEqual(items["finished_share"], 2 / 3)
        self.assertEqual(list(items["by_size"]), ["S", "M"])
        self.assertEqual(items["by_size"]["S"]["usd_per_finished"], None)
        self.assertAlmostEqual(items["by_size"]["M"]["usd_per_finished"], 135 / 1e6)
        self.assertEqual(result["arms"]["claude-only"]["items"]["unrated_sessions"], 1)
        by_id = {s["session"]: s for s in result["sessions"]}
        self.assertEqual([i["calls"] for i in by_id["rated"]["items"]], [1, 1, 1])
        self.assertIsNone(by_id["pending"]["items"])
        text = cost.render_by_mode(result, sessions=True)
        self.assertIn(f"items (rubric {rate.RUBRIC}", text)
        self.assertIn("1/1 0.00", text)  # docs: one finished item at 135e-6 USD
        lines = text.splitlines()
        section = next(i for i, l in enumerate(lines) if l.startswith("items (rubric"))
        line = next(l for l in lines[section:] if l.startswith("vanilla"))
        self.assertEqual(line.split()[1:], ["1", "1", "3", "2", "1", "0", "1", "67", "3.00", "3.00", "0", "0"])
        self.assertIn("2/3", text)

    def test_field_reports_left_out_with_range_and_end_state_coverage(self):
        self.write_calls(self.transcript, [assistant("a", "2026-10-13T09:10:00Z")])
        rating = {**self.rating, "end_state": "next-start",
                  "items": [item(), item(2, status="unclear"), item(3, type="field-report", status="unclear")]}
        rows = (self.session_rows("rated", "orchestrator", self.transcript, 6.0, rating)
                + self.session_rows("bare", "orchestrator", self.transcript, 2.0,
                                    {**self.rating, "items": [item(status="partial")]}))
        result = rate.attach_items(cost.by_mode(rows, []), rows, self.weights, self.prices)
        items = result["arms"]["orchestrator"]["items"]
        self.assertEqual((items["items"], items["finished"], items["unclear"], items["field_reports"]), (3, 1, 1, 1))
        self.assertEqual((items["rated_sessions"], items["end_state_sessions"]), (2, 1))
        self.assertEqual(items["usd_per_finished"], 8.0)  # field-report USD stays in the numerator
        self.assertEqual(items["usd_per_finished_or_unclear"], 4.0)
        self.assertNotIn("field-report", items["by_type"])
        self.assertNotIn("field-report", result["item_order"]["by_type"])
        text = cost.render_by_mode(result, sessions=True)
        lines = text.splitlines()
        section = next(i for i, l in enumerate(lines) if l.startswith("items (rubric"))
        line = next(l for l in lines[section:] if l.startswith("orchestrator"))
        self.assertEqual(line.split()[-4:], ["8.00", "4.00", "1", "1"])
        self.assertIn("1/2", text)  # FIN/ITEMS of "rated" leaves the field report out

    def test_missing_transcript_counts_as_error_not_rated(self):
        rows = self.session_rows("gone", "orchestrator", self.root / "missing.jsonl", 2.0, self.rating)
        result = rate.attach_items(cost.by_mode(rows, []), rows, self.weights, self.prices)
        items = result["arms"]["orchestrator"]["items"]
        self.assertEqual((items["rated_sessions"], items["errors"], items["usd_per_finished"]), (0, 1, None))


if __name__ == "__main__":
    unittest.main()

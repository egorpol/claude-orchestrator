"""Automatic field-report dry runs: fake logs, transcripts and feedback only."""
import io
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/delegate/scripts"
sys.path.insert(0, str(SCRIPTS))

import autoreport as afr  # noqa: E402
import cost  # noqa: E402
import rate  # noqa: E402
import report  # noqa: E402

BASE = datetime(2026, 10, 20, tzinfo=timezone.utc)
PRIVATE = "private-repo-must-not-appear"


def stamp(minutes=0):
    return (BASE + timedelta(minutes=minutes)).isoformat()


def user(text="do the work", minutes=0, **extra):
    return {"type": "user", "timestamp": stamp(minutes), "message": {"content": text}, **extra}


def assistant(blocks=None, minutes=1, mid="m1", write=10, **extra):
    return {"type": "assistant", "timestamp": stamp(minutes),
            "message": {"id": mid, "model": "claude-test", "content": blocks or [],
                        "usage": {"input_tokens": 10, "cache_creation_input_tokens": write,
                                  "cache_read_input_tokens": 100, "output_tokens": 5}}, **extra}


def tool(name="Bash", args=None, tid="t1"):
    return {"type": "tool_use", "id": tid, "name": name, "input": args or {"command": "true"}}


def result(text="done", error=False, tid="t1", minutes=2, **extra):
    return user([{"type": "tool_result", "tool_use_id": tid, "content": text, "is_error": error}],
                minutes, **extra)


def command(name="field-report", minutes=10):
    return user(f"<command-name>/{name}</command-name><command-args>args</command-args>", minutes)


def picker(tid="picker"):
    return tool("AskUserQuestion", {"questions": [{"header": "Next", "question": "What next?",
                "options": [{"label": "/field-report"}, {"label": "Continue"}]}]}, tid)


class Fixtures(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.feedback = self.root / "feedback"
        self.feedback.mkdir()
        self.log = self.root / "sessions.jsonl"
        self.out = self.root / "out"
        self.rows = []
        patcher = mock.patch("rate.session_commits", return_value="(none)")
        self.commit_patcher = patcher
        self.commits = patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("ask.run_cursor", "ask.run_codex", "quota.fetch"):
            patcher = mock.patch(name, side_effect=AssertionError("no agent or live quota calls"))
            patcher.start()
            self.addCleanup(patcher.stop)

    def save(self, path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("bad json\n" + "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    def session(self, n=1, units=100, mode="orchestrator", rows=None, first=None, **summary):
        sid = f"{n:08x}-aaaa-bbbb-cccc-dddddddddddd"
        # Claude Code names the transcript folder after the project path.
        path = self.root / "projects" / f"-home-user-{PRIVATE}" / f"{sid}.jsonl"
        self.save(path, rows if rows is not None else [user(), assistant()])
        start = {"event": "start", "session": sid, "mode": mode, "ts": stamp(),
                 "repo": PRIVATE, "repo_dir": str(self.root / PRIVATE)}
        end = {"event": "summary", "session": sid, "mode": mode,
               "first_call": first or stamp(1), "last_call": stamp(100), "model": "claude-test",
               "calls": 2, "subagent_calls": 3, "context_max": 10000, "cost_units": units,
               "usd_list_price": 1.5, "delegate": {"ask": 2, "launch": 1},
               "repo": PRIVATE, "repo_dir": start["repo_dir"], "transcript": str(path), **summary}
        self.rows.extend([start, end])
        return sid

    def record(self, sid):
        return rate.session_records(self.rows)[sid]

    def digest(self, sid):
        return afr.build_digest(self.record(sid), [r for r in self.rows if r.get("event") == "start"])

    def select(self, **kw):
        return afr.select_sessions(self.rows, self.feedback, seed=kw.pop("seed", 1), **kw)

    def invoke(self, *args):
        self.save(self.log, self.rows)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                report.main(["--log", str(self.log), "--feedback-dir", str(self.feedback), *args])
                code = 0
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()


class SelectionTests(Fixtures):
    def test_eligibility_last_summary_first_start_and_mode_fallbacks(self):
        a = self.session(1, mode=None)
        self.rows[0]["mode"] = "vanilla"
        self.rows.append({**self.rows[0], "mode": "claude-only"})
        self.rows.append({**self.rows[1], "cost_units": 222})
        b = self.session(2, mode=None)
        self.session(3, first=stamp(-1))
        self.session(4, first=stamp(60))
        missing = self.session(5)
        Path(self.record(missing)["summary"]["transcript"]).unlink()
        no_summary = self.session(6)
        self.rows = [r for r in self.rows if not (r["session"] == no_summary and r["event"] == "summary")]
        self.session(7, first="invalid")
        chosen = self.select(since=stamp(), until=stamp(60))
        self.assertEqual(chosen["counts"]["eligible"], 2)
        picks = {p["session"]: p for p in chosen["picks"]}
        self.assertEqual(picks[a]["cost_units"], 222)
        self.assertEqual(picks[a]["mode"], "vanilla")
        self.assertEqual(picks[b]["mode"], "unlabelled")

    def test_reported_header_top_level_first_3000_and_q3_includes_reported(self):
        ids = [self.session(i, units=i * 100) for i in range(1, 5)]
        (self.feedback / "hand.md").write_text(f"Orchestrator: me (session `{ids[3][:8]}`)\n"
                                                f"launched in session `{ids[0][:8]}`", encoding="utf-8")
        (self.feedback / "auto").mkdir()
        (self.feedback / "auto" / "x.md").write_text(f"(session `{ids[1][:8]}`)", encoding="utf-8")
        (self.feedback / "late.md").write_text("x" * 3000 + f"(session `{ids[2][:8]}`)", encoding="utf-8")
        got = self.select()
        self.assertEqual(got["q3_cost_units"], 400)
        self.assertEqual(got["counts"], {"eligible": 4, "reported": 1, "unreported": 3,
                                       "flagged": 0, "denied": 0, "idle": 0, "top_quartile": 0})
        self.assertEqual(got["flagged_by_mode"], {})
        self.assertEqual({p["session"] for p in got["picks"]}, set(ids[:3]))

    def test_denial_prefixes_errors_only_and_main_only(self):
        phrases = ["PreToolUse:Bash hook error: denied", "Permission for this action was denied",
                   "Permission to use Bash has been denied", "Permission to use Edit was denied",
                   "The user doesn't want to proceed", "The tool use was rejected"]
        rows = [result("  " + p + "\n", True) for p in phrases]
        rows += [result([{"type": "text", "text": "Permission to use"},
                         {"type": "text", "text": "Bash was denied"}], True)]
        rows += [result("PreToolUse:Bash hook error", False),
                 result("CHANGELOG: PreToolUse:Bash hook error", True),
                 result("model quotes blocked by hook mid-text", True),
                 result(phrases[0], True, isSidechain=True)]
        self.assertEqual(afr.denied_calls(rows), 7)
        self.session(rows=rows)
        got = self.select()
        self.assertEqual(got["counts"]["denied"], 1)
        self.assertEqual(got["picks"][0]["denied"], 7)

    def test_idle_share_uses_same_main_summary_and_zero_cost(self):
        rows = [user(), assistant(minutes=1), assistant(minutes=61, mid="m2", write=100)]
        sid = self.session(rows=rows, units=999999)
        path = Path(self.record(sid)["summary"]["transcript"])
        sub = path.with_suffix("") / "subagents" / "agent-a.jsonl"
        self.save(sub, [assistant(mid="sub", write=999999)])
        usage = cost.summarise_session(cost.parse_session_usage(path), *cost.load_catalog_cost())
        pick = self.select()["picks"][0]
        self.assertEqual(pick["idle_rebuilds"], 1)
        self.assertAlmostEqual(pick["rebuild_share"], usage["idle_rebuild_units"] / usage["cost_units"])
        with mock.patch("cost.summarise_session", return_value={"idle_rebuilds": [],
                        "idle_rebuild_units": 0, "cost_units": 0}):
            self.assertEqual(self.select()["picks"][0]["rebuild_share"], 0)

    def test_rank_order_share_denials_units_then_id(self):
        for n, units, denied in [(4, 100, 1), (3, 100, 1), (2, 200, 1), (1, 1, 2)]:
            self.session(n, units=units, rows=[assistant()] + [result("The tool use was rejected", True)] * denied)
        idle = self.session(5, units=1, rows=[assistant(minutes=1), assistant(minutes=61, mid="m2")])
        picks = self.select()["picks"]
        self.assertEqual([p["session"][:8] for p in picks], [idle[:8], "00000001", "00000002", "00000003", "00000004"])
        self.assertEqual([p["rank"] for p in picks], [1, 2, 3, 4, 5])
        self.assertEqual(self.select()["counts"]["top_quartile"], 3)

    def test_seed_determinism_sorted_pools_draw_sizes_and_replay(self):
        for n in range(1, 101):
            self.session(n, units=n)
        a = self.select(seed=9)
        self.assertEqual(a, self.select(seed=9))
        self.rows.reverse()
        self.assertEqual(a, self.select(seed=9))
        self.assertNotEqual(a["picks"], self.select(seed=10)["picks"])
        self.assertEqual(len(a["picks"]), 22)
        self.assertEqual([p["reason"] for p in a["picks"]].count("random unflagged"), 5)
        self.assertEqual([p["reason"] for p in a["picks"]].count("random flagged"), 2)
        b = self.select(seed=None)
        self.assertIsInstance(b["seed"], int)
        self.assertEqual(b, self.select(seed=b["seed"]))
        rng = random.Random(9)
        expected = rng.sample([f"{n:08x}-aaaa-bbbb-cccc-dddddddddddd" for n in range(1, 76)], 5)
        self.assertEqual([p["session"] for p in a["picks"] if p["reason"] == "random unflagged"], expected)
        expected = rng.sample([f"{n:08x}-aaaa-bbbb-cccc-dddddddddddd" for n in range(76, 86)], 2)
        self.assertEqual([p["session"] for p in a["picks"] if p["reason"] == "random flagged"], expected)

    def floor_pool(self):
        for n in range(1, 20):
            self.session(n, units=200 - n, rows=[result("The tool use was rejected", True)])
        self.session(40, units=1, mode="claude-only")
        self.session(41, units=2, mode="vanilla")
        self.session(42, units=0, mode="vanilla")

    def test_mode_floor_two_replacements_are_15_then_14(self):
        self.floor_pool()
        with mock.patch("random.Random.sample", return_value=[]):
            got = self.select()
        floors = [p for p in got["picks"] if p["reason"] == "mode floor"]
        self.assertEqual({p["mode"] for p in got["picks"]}, set(afr.MODES))
        self.assertEqual({p["replaces"][:8] for p in floors}, {"0000000f", "0000000e"})
        self.assertEqual({p["session"][:8] for p in floors}, {"00000028", "00000029"})
        self.assertTrue(all(p["rank"] is None for p in floors))

    def test_floor_prefers_flagged_by_rank_and_single_missing(self):
        self.floor_pool()
        vanilla = self.record(f"{41:08x}-aaaa-bbbb-cccc-dddddddddddd")
        self.save(Path(vanilla["summary"]["transcript"]), [result("The tool use was rejected", True)])
        self.record(f"{42:08x}-aaaa-bbbb-cccc-dddddddddddd")["summary"]["cost_units"] = 999
        (self.feedback / "reported.md").write_text("(session `00000028`)")
        with mock.patch("random.Random.sample", return_value=[]):
            got = self.select()
        floors = [p for p in got["picks"] if p["reason"] == "mode floor"]
        self.assertEqual(len(floors), 1)
        self.assertEqual(floors[0]["session"][:8], "00000029")
        self.assertIsInstance(floors[0]["rank"], int)
        self.assertNotIn("claude-only", {p["mode"] for p in got["picks"]})

    def test_floor_never_removes_last_pick_of_its_mode(self):
        self.floor_pool()
        self.rows[28]["mode"] = self.rows[29]["mode"] = "vanilla"  # rank 15
        with mock.patch("random.Random.sample", return_value=[]):
            got = self.select()
        floor = next(p for p in got["picks"] if p["reason"] == "mode floor")
        self.assertEqual(floor["replaces"][:8], "0000000e")
        self.assertIn("0000000f", {p["session"][:8] for p in got["picks"]})

    def test_floor_adds_when_no_replaceable_rank_pick(self):
        self.session(1, mode="vanilla", units=10)
        self.session(2, mode="claude-only", units=1)
        with mock.patch("random.Random.sample", return_value=[]):
            got = self.select()
        self.assertEqual(len(got["picks"]), 2)
        self.assertEqual(got["picks"][1]["reason"], "mode floor")
        self.assertIsNone(got["picks"][1]["replaces"])
        synthetic = {**got, "picks": got["picks"] * 12}
        self.assertIn("total exceeds 22", afr.render_selection(synthetic))

    def test_empty_and_small_batches_never_pad(self):
        self.assertEqual(self.select()["counts"]["eligible"], 0)
        self.assertEqual(self.select()["q3_cost_units"], 0)
        self.session()
        self.assertEqual(len(self.select()["picks"]), 1)

    def test_local_midnight_and_aware_offsets(self):
        old = os.environ.get("TZ")
        with mock.patch.dict(os.environ, {"TZ": "Europe/Berlin"}):
            time.tzset()
            try:
                self.session(1, first="2026-10-19T22:00:00Z")
                self.session(2, first="2026-10-19T21:59:59Z")
                self.assertEqual(self.select(since="2026-10-20")["counts"]["eligible"], 1)
                self.assertEqual(self.select(until="2026-10-20T00:00:00+02:00")["counts"]["eligible"], 1)
            finally:
                if old is None:
                    os.environ.pop("TZ", None)
                else:
                    os.environ["TZ"] = old
                time.tzset()
        for kw in ({"since": "bad"}, {"until": "bad"}, {"since": stamp(2), "until": stamp(1)}):
            with self.assertRaises(ValueError):
                self.select(**kw)


class DigestTests(Fixtures):
    def test_cut_keeps_offered_report_picker_and_every_evidence_source_stops(self):
        rows = [user("<command-name>/next</command-name>"), assistant([picker()], minutes=1),
                result('Your questions have been answered: "What next?"="/field-report"', tid="picker"),
                command(), user("AFTER-CUT-PROMPT", 11),
                assistant([{"type": "text", "text": "AFTER-CUT-ANSWER"},
                           tool("Write", {"file_path": "feedback/report.md", "content": "HAND-REPORT"})],
                          minutes=71, mid="m2"), result("AFTER-CUT-ERROR", True, minutes=72)]
        sid = self.session(rows=rows)
        got = self.digest(sid)
        self.assertEqual((got["status"], got["cut_row"], got["rows"]), ("built", 3, 3))
        for text in (got["digest"], got["transcript"]):
            self.assertIn("/field-report", text)
            self.assertNotIn("[command /field-report", text)
            for forbidden in ("AFTER-CUT", "HAND-REPORT", "feedback/report.md"):
                self.assertNotIn(forbidden, text)
        self.assertEqual(got["events"], {"denied": 0, "idle": 0, "error": 0})
        self.assertIn("Whole session, including calls after the cut", got["digest"])
        self.assertIn(rate.NEXT_PROMPT_NO_COPY, got["digest"])
        self.assertEqual(self.commits.call_args.args[2], BASE + timedelta(minutes=10))
        self.assertEqual(self.commits.call_args.kwargs, {"slack_minutes": 0})

    def test_queued_command_cut_uses_original_json_row_index_and_origin_rule(self):
        queue = {"type": "attachment", "timestamp": stamp(5), "attachment": {
            "type": "queued_command", "commandMode": "prompt", "origin": {"kind": "human"},
            "prompt": "<command-name>/field-report</command-name>"}}
        sid = self.session(rows=[user("first", origin={"kind": "human"}), assistant(), queue,
                                 assistant([tool("Write", {"file_path": "feedback/report.md"})], mid="m2")])
        got = self.digest(sid)
        self.assertEqual(got["cut_row"], 2)
        self.assertEqual(got["status"], "built")
        # Origin detection considers the full transcript, including after the cut.
        path = Path(self.record(sid)["summary"]["transcript"])
        self.save(path, [user("not human"), assistant(), queue, user("later", origin={"kind": "human"})])
        got = self.digest(sid)
        self.assertNotIn("not human", got["digest"])
        self.assertNotIn("not human", got["transcript"])

    def test_no_cut_and_first_of_multiple_report_prompts(self):
        sid = self.session(rows=[user("mentions /field-report but not a command"), assistant()])
        self.assertIsNone(self.digest(sid)["cut_row"])
        self.save(Path(self.record(sid)["summary"]["transcript"]), [user(), command(), command(minutes=20)])
        self.assertEqual(self.digest(sid)["cut_row"], 1)

    def test_leak_guards_structural_writers_and_skill(self):
        for name in sorted(afr.WRITERS):
            with self.subTest(name=name):
                sid = self.session(len(self.rows) + 1, rows=[user(), assistant([tool(name, {"file_path": "x/feedback/y.md"})]), command()])
                got = self.digest(sid)
                self.assertEqual(got["status"], "dropped")
                self.assertIn("feedback", got["reason"])
        sid = self.session(99, rows=[user(), assistant([tool("Skill", {"skill": "field-report"})]), command()])
        self.assertEqual(self.digest(sid)["reason"], "Skill field-report call before the cut")
        sid = self.session(100, rows=[user("Write x/feedback/y.md, /field-report"), assistant([
            tool("Read", {"file_path": "x/feedback/y.md"}),
            tool("Write", {"file_path": "x/feedback-copy/y.md"}),
            tool("Skill", {"skill": "other"})])])
        self.assertEqual(self.digest(sid)["status"], "built")

    def test_skill_report_without_typed_command_is_dropped(self):
        sid = self.session(rows=[user("write the field report"),
                                 assistant([tool("Skill", {"skill": "field-report"})])])
        got = self.digest(sid)
        self.assertIsNone(got["cut_row"])
        self.assertEqual(got["status"], "dropped")
        self.assertEqual(got["reason"], "Skill field-report call before the cut")

    def test_handoff_tool_writes_after_cut_drop_but_before_cut_allowed(self):
        for name in sorted(afr.WRITERS):
            with self.subTest(name=name):
                sid = self.session(len(self.rows) + 1, rows=[user(), command(), assistant([
                    tool(name, {"file_path": "/x/HANDOFF.md"})])])
                got = self.digest(sid)
                self.assertTrue(got["handoff_after_cut"])
                self.assertEqual(got["status"], "dropped")
                self.assertEqual(got["reason"], "HANDOFF.md written after the /field-report prompt")
        sid = self.session(99, rows=[user(), assistant([tool("Write", {"file_path": "HANDOFF.md"})]), command()])
        self.assertEqual(self.digest(sid)["status"], "built")

    def test_handoff_bash_writes_and_mentions_or_reads(self):
        writes = ["python3 - <<'EOF'\np='HANDOFF.md'; do_work(p)\nEOF", 'H = "/x/HANDOFF.md"',
                  "echo done > HANDOFF.md", "echo done >> /x/HANDOFF.md", "tee -a HANDOFF.md",
                  "sed -i 's/a/b/' HANDOFF.md", "cp source HANDOFF.md", "mv source HANDOFF.md"]
        reads = ["cat HANDOFF.md", "sed -n 1,5p HANDOFF.md", "echo HANDOFF.md",
                 "python3 - <<'EOF'\nreport='Started from `HANDOFF.md` via `/next`'\nEOF"]
        for n, cmd in enumerate(writes + reads, 1):
            with self.subTest(command=cmd):
                sid = self.session(n, rows=[user(), command(), assistant([tool(args={"command": cmd})])])
                got = self.digest(sid)
                self.assertEqual(got["handoff_after_cut"], cmd in writes)
                self.assertEqual(got["status"], "dropped" if cmd in writes else "built")

    def test_numbers_whole_session_quota_and_section_order(self):
        sid = self.session(rows=[user(), assistant(), command(), assistant(minutes=70, mid="m2"),
                                result("The tool use was rejected", True, minutes=71)],
                           quota={"pools": {"fresh": {"start": 1, "end": 5, "delta": 4},
                                            "stale": {"delta": None}}})
        got = self.digest(sid)
        text = got["digest"]
        for value in (sid[:8], "mode: orchestrator", "claude-test", "2 main; 3 subagent",
                      "Context max: 10000", "USD at list price: 1.5", "Idle rebuilds: 1",
                      "gap minutes: 69", "Denied/blocked calls: 1", '"ask": 2', "Quota fresh"):
            self.assertIn(value, text)
        self.assertNotIn("Quota stale", text)
        self.assertEqual(got["events"], {"denied": 0, "idle": 0, "error": 0})
        headers = ["Numbers", "Context", "Prompts", "Pickers", "HANDOFF.md at session start",
                   "HANDOFF.md at session end", "Commits in the session window",
                   "Harness notes", "Errors", "Shell cwd", "Friction excerpts"]
        positions = [text.index("## " + h) for h in headers]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn(PRIVATE, text)

    def test_picker_structured_answers_fallback_rejection_and_unmatched(self):
        rows = [user(), assistant([picker("p1")]),
                result('"What next?"="wrong"', tid="p1", toolUseResult={"answers": {"What next?": "Continue"}}),
                assistant([picker("p2")], mid="m2", minutes=3),
                result('Your questions have been answered: "What next?"="/field-report"', tid="p2", minutes=4,
                       toolUseResult={"answers": {}}),
                assistant([picker("p3")], mid="m3", minutes=5),
                result("The user doesn't want to proceed" + "X" * 400, True, "p3", 6),
                assistant([picker("p4")], mid="m4", minutes=7)]
        got = self.digest(self.session(rows=rows))
        text = got["digest"].split("## Pickers\n")[1].split("## HANDOFF")[0]
        self.assertIn("Next\nWhat next?\nOptions: /field-report; Continue", text)
        self.assertIn("Answer: Continue", text)
        self.assertIn("Answer: /field-report", text)
        self.assertIn("(no answer: The user doesn't want to proceed", text)
        self.assertIn("(no answer: no result)", text)
        self.assertNotIn("wrong", text)
        self.assertNotIn("X" * 201, text)
        self.assertIn("answers: Continue", got["transcript"])
        self.assertIn("answers: /field-report", got["transcript"])

    def test_handoff_quotes_limits_unchanged_missing_and_end_state_cut(self):
        sid = self.session(rows=[user("<command-name>/next</command-name>"), assistant(), command()])
        start = self.root / "start.md"
        end = self.root / "end.md"
        start.write_text("## nested\nstart\n" + "x" * 10000)
        end.write_text("end state")
        self.record(sid)["start"]["handoff"] = str(start)
        self.rows.append({"event": "end", "session": sid, "handoff": str(end)})
        got = self.digest(sid)
        text = got["digest"]
        self.assertIn("> ## nested\n> start", text)
        self.assertIn("> end state", text)
        self.assertIn(rate.NEXT_PROMPT, text)
        self.assertIn(rate.TRUNCATED, text)
        start.write_text("end state")
        self.assertIn("(unchanged from the start copy)", self.digest(sid)["digest"])
        end.unlink()
        self.assertIn("(no end-state copy was saved)", self.digest(sid)["digest"])
        with mock.patch("rate.end_state", return_value=(None, None)) as state:
            self.digest(sid)
        self.assertEqual(state.call_args.args[0]["summary"]["last_call"], stamp(10))

    def test_no_cut_keeps_commit_slack_and_unblinded_text(self):
        sid = self.session(rows=[user("Use Codex and Cursor Auto"), assistant()])
        got = self.digest(sid)
        self.assertIn("Use Codex and Cursor Auto", got["digest"])
        self.assertEqual(self.commits.call_args.kwargs, {"slack_minutes": 10})

    def test_friction_filtered_indices_windows_merge_and_event_counts(self):
        rows = [user(), assistant([tool()], minutes=1)]
        rows += [{"type": "system", "content": "HOOK-SECRET"},
                 assistant([{"type": "text", "text": "SIDECHAIN-SECRET"}], isSidechain=True),
                 user("META-SECRET", isMeta=True)]
        rows += [result("Permission to use Bash was denied", True), assistant(minutes=3, mid="m2"),
                 result("ordinary error", True, minutes=4), assistant(minutes=64, mid="m3"), user("after", 65)]
        got = self.digest(self.session(rows=rows))
        self.assertEqual(got["events"], {"denied": 1, "error": 1, "idle": 1})
        excerpts = got["digest"].split("## Friction excerpts\n")[1]
        self.assertEqual(excerpts.count("USER: do the work"), 1)
        self.assertIn("### E1 denied at +2.0 min", excerpts)
        self.assertIn("### E2 error at +4.0 min", excerpts)
        self.assertIn("### E3 idle at +64.0 min", excerpts)
        for forbidden in ("HOOK-SECRET", "SIDECHAIN-SECRET", "META-SECRET"):
            self.assertNotIn(forbidden, got["digest"] + got["transcript"])

    def test_idle_uses_timestamps_when_api_message_rows_collapse(self):
        rows = [user(), assistant(minutes=1), assistant(minutes=2), assistant(minutes=62, mid="m2")]
        got = self.digest(self.session(rows=rows))
        self.assertEqual(got["events"]["idle"], 1)
        self.assertIn("idle at +62.0 min", got["digest"])

    def test_transcript_format_caps_matching_and_exclusions(self):
        visible = "v" * 5000 + "<system-reminder>REMINDER-SECRET</system-reminder>"
        blocks = [{"type": "thinking", "thinking": "THINKING-SECRET"}, {"type": "text", "text": visible},
                  tool(args={"command": "x\ny" + "a" * 1000}, tid="one"), tool(tid="two")]
        rows = [user("u" * 3000 + "<system-reminder>REMINDER-SECRET</system-reminder>"), assistant(blocks),
                result("later-result", True, "two"), result("other", False, "one"),
                user("not typed", origin={"kind": "tool"})]
        rows[0]["origin"] = {"kind": "human"}
        got = self.digest(self.session(rows=rows))
        lines = got["transcript"].splitlines()
        self.assertTrue(lines[0].startswith("USER [+0.0]: "))
        self.assertLessEqual(len(lines[0].split(": ", 1)[1]), 2000)
        self.assertTrue(lines[1].startswith("CLAUDE [+1.0]: "))
        self.assertEqual(len(lines[1].split(": ", 1)[1]), 4000)
        self.assertTrue(lines[2].endswith("-> ok, 5 chars"))
        self.assertTrue(lines[3].startswith("TOOL [+1.0] Bash") and "-> error, 12 chars: later-result" in lines[3])
        self.assertLessEqual(len(lines[2].split("Bash ", 1)[1].split(" -> ", 1)[0]), 160)
        for forbidden in ("REMINDER-SECRET", "THINKING-SECRET", "not typed"):
            self.assertNotIn(forbidden, got["transcript"])
        self.assertIn("RESULT error (12 chars): later-result", got["digest"])

    def test_transcript_ignores_sidechain_result_with_same_tool_id(self):
        sid = self.session(rows=[user(), assistant([tool()]), result("main"),
                                 result("SIDECHAIN-SECRET", True, isSidechain=True)])
        transcript = self.digest(sid)["transcript"]
        self.assertIn("-> ok, 4 chars", transcript)
        self.assertNotIn("SIDECHAIN-SECRET", transcript)

    def test_cap_hundreds_of_errors_long_prompts_preserves_numbers_and_pickers(self):
        rows = [user("initial"), assistant([picker()]), result('"What next?"="Continue"', tid="picker")]
        for n in range(400):
            rows.extend([user(str(n) + "p" * 4000, n + 3),
                         assistant([tool(args={"command": "a" * 5000})], n + 3, f"m{n + 2}"),
                         result("error " + "e" * 4000, True, minutes=n + 3)])
        got = self.digest(self.session(rows=rows))
        self.assertLessEqual(got["digest_chars"], 60000)
        self.assertEqual(got["digest_chars"], len(got["digest"]))
        self.assertIn("## Numbers", got["digest"])
        self.assertIn("## Pickers\n### Picker 1", got["digest"])
        self.assertIn("Answer: Continue", got["digest"])
        self.assertIn("400 more friction events: see the transcript file", got["digest"])
        self.assertGreater(got["transcript_chars"], 60000)
        self.assertEqual(got["events"]["error"], 400)

    def test_cap_drops_error_then_idle_then_denied_from_end(self):
        rows = [user()]
        for n in range(16):
            rows += [assistant([{"type": "text", "text": "x" * 2000}], minutes=n * 5 + k,
                               mid=f"m{n}-{k}") for k in range(8)]
            rows += [result("The tool use was rejected" if n == 0 else "ordinary error", True, minutes=n * 5 + 8)]
        got = self.digest(self.session(rows=rows))
        self.assertLessEqual(got["digest_chars"], 60000)
        self.assertIn("### E1 denied", got["digest"])
        self.assertIn("more friction events", got["digest"])
        self.assertNotIn("### E16 error", got["digest"])

    def test_hard_cap_many_labels_keeps_picker(self):
        rows = [user(str(n), n) for n in range(4000)]
        rows += [assistant([picker()]), result('"What next?"="Continue"', tid="picker")]
        got = self.digest(self.session(rows=rows))
        self.assertLessEqual(got["digest_chars"], 60000)
        self.assertIn("## Numbers", got["digest"])
        self.assertIn("Answer: Continue", got["digest"])

    def test_unreadable_transcript_is_dropped_without_paths(self):
        sid = self.session()
        path = Path(self.record(sid)["summary"]["transcript"])
        for change in (lambda: path.write_bytes(b"\xff"), path.unlink):
            change()
            got = self.digest(sid)
            self.assertEqual(got["status"], "dropped")
            self.assertEqual(got["reason"], "unreadable transcript")
            self.assertIsNone(got["digest"])


class DigestEvidenceTests(Fixtures):
    def section(self, digest, name):
        marks = list(re.finditer(r"^## (.+)$", digest, re.M))
        for i, match in enumerate(marks):
            if match.group(1) == name:
                end = marks[i + 1].start() if i + 1 < len(marks) else len(digest)
                return digest[match.start():end].rstrip("\n")
        self.fail(f"missing section {name}")

    def attachment(self, kind, minutes=0, uuid=None, parentUuid=None, **fields):
        row = {"type": "attachment", "timestamp": stamp(minutes),
               "attachment": {"type": kind, **fields}}
        if uuid is not None:
            row["uuid"] = uuid
        if parentUuid is not None:
            row["parentUuid"] = parentUuid
        return row

    def test_new_sections_zero_counts_on_plain_session(self):
        got = self.digest(self.session(rows=[user(origin={"kind": "human"}), assistant()]))
        harness = self.section(got["digest"], "Harness notes")
        errors = self.section(got["digest"], "Errors")
        cwd = self.section(got["digest"], "Shell cwd")
        for line in ("Context-guard messages: 0", "Silent-turn reminders: 0",
                     "Hook ask/deny decisions: 0", "File-changed notices: 0",
                     "IDE diagnostics after edits: 0", "Reminders inside tool results: 0",
                     "Other attachments: none"):
            self.assertIn(line, harness)
        self.assertIn("Error results: 0 (denied 0)", errors)
        self.assertIn("Error lines in ok results: 0", errors)
        self.assertIn("cwd moves: 0", cwd)
        self.assertIn("Shell cwd resets: 0", cwd)
        ctx = self.section(got["digest"], "Context")
        self.assertIn("Calls:", ctx)
        self.assertIn("Crossings:", ctx)
        self.assertIn("Drops over 30%:", ctx)

    def test_context_crossings_drops_dedupe_and_synthetic(self):
        rows = [user("start", origin={"kind": "human"}),
                assistant(minutes=1, mid="a", write=38_000),
                assistant([{"type": "text", "text": "dup"}], minutes=2, mid="a", write=99_000),
                assistant(minutes=3, mid="syn", write=200_000,
                          message={"id": "syn", "model": "<synthetic>", "content": [],
                                   "usage": {"input_tokens": 200_000, "cache_creation_input_tokens": 0,
                                             "cache_read_input_tokens": 0, "output_tokens": 1}}),
                assistant(minutes=40, mid="b", write=100_000),
                assistant(minutes=52, mid="c", write=120_000),
                assistant(minutes=80, mid="d", write=150_000),
                assistant(minutes=95, mid="e", write=40_000)]
        # Force exact context totals via usage overrides on later rows.
        for row, total in ((rows[1], 38_000), (rows[4], 100_000), (rows[5], 120_000),
                           (rows[6], 150_000), (rows[7], 40_000)):
            row["message"]["usage"] = {"input_tokens": total, "cache_creation_input_tokens": 0,
                                       "cache_read_input_tokens": 0, "output_tokens": 1}
        rows[2]["message"]["usage"] = {"input_tokens": 99_000, "cache_creation_input_tokens": 0,
                                       "cache_read_input_tokens": 0, "output_tokens": 1}
        got = self.digest(self.session(rows=rows))
        ctx = self.section(got["digest"], "Context")
        self.assertIn("start 38k", ctx)
        self.assertIn("max 150k", ctx)
        self.assertIn("100k at +40.0 min (P0)", ctx)
        self.assertIn("120k at +52.0 min (P0)", ctx)
        self.assertIn("150k at +80.0 min (P0)", ctx)
        self.assertIn("150k -> 40k at +95.0 min (P0)", ctx)
        self.assertNotIn("99k", ctx)
        self.assertNotIn("200k", ctx)

    def test_context_largest_results_and_prompt_context(self):
        big = "x" * 5000
        rows = [user("p0", origin={"kind": "human"}),
                assistant([tool("Read", {"file_path": "a.py"}, "r1")], minutes=1, mid="m1"),
                result(big, tid="r1", minutes=2),
                user("p1", 10, origin={"kind": "human"}),
                assistant([tool("Bash", {"command": "echo"}, "b1")], minutes=12, mid="m2", write=50),
                result("ok", tid="b1", minutes=13)]
        for row, total in ((rows[1], 38_000), (rows[4], 52_000)):
            row["message"]["usage"] = {"input_tokens": total, "cache_creation_input_tokens": 0,
                                       "cache_read_input_tokens": 0, "output_tokens": 1}
        got = self.digest(self.session(rows=rows))
        ctx = self.section(got["digest"], "Context")
        self.assertIn("At each typed prompt: P0 38k, P1 52k", ctx)
        self.assertIn('Read {"file_path": "a.py"} -> 5,000 chars', ctx)
        self.assertIn("Tool result chars by tool: Read 5,000; Bash 2", ctx)

    def test_harness_guard_silent_hook_and_cut(self):
        ask = json.dumps({"hookSpecificOutput": {
            "permissionDecision": "ask", "permissionDecisionReason": "secret guard blocked"}})
        rows = [
            user("go"),
            assistant([tool(args={"command": "sleep 1"}, tid="t0")], minutes=1),
            result("ok", tid="t0", minutes=2),
            self.attachment("hook_system_message", 52, uuid="sys-1",
                            hookEvent="UserPromptSubmit", hookName="UserPromptSubmit",
                            toolUseID="g1", content="context guard: ~133k tokens; hand off."),
            # Paired long form: different toolUseID, parentUuid points at the system message.
            self.attachment("hook_additional_context", 52, parentUuid="sys-1",
                            hookEvent="UserPromptSubmit", hookName="UserPromptSubmit",
                            toolUseID="hook-other",
                            content=["Context guard: ~133k tokens long form paired."]),
            # Orphan additional context: no matching system-message uuid.
            self.attachment("hook_additional_context", 55, parentUuid="missing",
                            hookEvent="UserPromptSubmit", hookName="UserPromptSubmit",
                            toolUseID="orphan",
                            content=["Context guard: orphan ~99k tokens."]),
            self.attachment("silent_turn_reminder", 60,
                            text="The user hasn't heard from you in a while"),
            self.attachment("hook_success", 61, hookEvent="PreToolUse", hookName="PreToolUse:Bash",
                            toolUseID="h1", stdout=ask),
            self.attachment("edited_text_file", 62, filename="/tmp/HANDOFF.md"),
            self.attachment("hook_additional_context", 63, hookEvent="PostToolUse",
                            hookName="PostToolUse:Edit", toolUseID="d1",
                            content=['<ide_diagnostics>[{"filePath": "/x/check.py"}]</ide_diagnostics>']),
            self.attachment("hook_system_message", 0, hookEvent="SessionStart",
                            hookName="SessionStart:startup", toolUseID="ss",
                            content="session mode: x"),
            command(minutes=100),
            self.attachment("silent_turn_reminder", 101,
                            text="after cut must not count"),
            result("Expected a filename\nrc=1", False, "after", 102),
        ]
        got = self.digest(self.session(rows=rows))
        harness = self.section(got["digest"], "Harness notes")
        self.assertIn("Context-guard messages: 2", harness)
        self.assertIn("guard +52.0 min (P0): context guard: ~133k tokens; hand off.", harness)
        self.assertIn("guard +55.0 min (P0): Context guard: orphan ~99k tokens.", harness)
        self.assertNotIn("long form paired", harness)
        self.assertIn("Silent-turn reminders: 1", harness)
        self.assertIn("silent-turn reminder +60.0 min (P0) after Bash", harness)
        self.assertIn("Hook ask/deny decisions: 1", harness)
        self.assertIn("hook ask +61.0 min (P0) PreToolUse:Bash: secret guard blocked", harness)
        self.assertIn("File-changed notices: 1 (HANDOFF.md)", harness)
        self.assertIn("IDE diagnostics after edits: 1 (check.py)", harness)
        self.assertNotIn("after cut", harness)
        self.assertNotIn("session mode", harness)
        self.assertIn("HARNESS [+52.0] guard: context guard: ~133k tokens; hand off.", got["transcript"])
        self.assertIn("HARNESS [+55.0] guard: Context guard: orphan ~99k tokens.", got["transcript"])
        self.assertNotIn("long form paired", got["transcript"])
        self.assertIn("HARNESS [+60.0] silent-turn reminder", got["transcript"])
        self.assertIn("HARNESS [+61.0] hook ask PreToolUse:Bash: secret guard blocked", got["transcript"])

    def test_errors_denied_ok_lines_merge_and_non_bash_ignored(self):
        rows = [
            user("go"),
            assistant([tool(args={"command": "true"}, tid="e1")], minutes=4),
            result("Exit code 1\nTraceback (most recent call last):\nKeyError: 'x'", True, "e1", 4),
            assistant([tool(args={"command": "deny"}, tid="e2")], minutes=9, mid="m2"),
            result("PreToolUse:Bash hook error: blocked", True, "e2", 9),
            assistant([tool("Edit", {"file_path": "a"}, "e3")], minutes=9, mid="m3"),
            result("<tool_use_error>File has not been read yet", True, "e3", 9),
            assistant([tool("Edit", {"file_path": "a"}, "e4")], minutes=9, mid="m4"),
            result("<tool_use_error>File has not been read yet", True, "e4", 10),
            assistant([tool(args={"command": "juce -o ; echo rc=$?"}, tid="ok1")], minutes=20, mid="m5"),
            result("Expected a filename after the -o|--output option\nrc=1", False, "ok1", 20),
            assistant([tool(args={"command": "py"}, tid="ok2")], minutes=21, mid="m6"),
            result("Traceback (most recent call last):\nValueError: boom", False, "ok2", 21),
            assistant([tool("Read", {"file_path": "x"}, "ok3")], minutes=22, mid="m7"),
            result("error: should be ignored in Read", False, "ok3", 22),
            command(minutes=50),
            assistant([tool(args={"command": "late"}, tid="late")], minutes=51, mid="m8"),
            result("late error", True, "late", 52),
        ]
        got = self.digest(self.session(rows=rows))
        errors = self.section(got["digest"], "Errors")
        self.assertIn("Error results: 4 (denied 1)", errors)
        self.assertIn("Error lines in ok results: 2", errors)
        self.assertIn("-> error: Exit code 1 | Traceback (most recent call last): | KeyError: 'x'", errors)
        self.assertIn("-> denied: PreToolUse:Bash hook error: blocked", errors)
        self.assertIn("-> error: <tool_use_error>File has not been read yet (x2)", errors)
        self.assertIn("-> ok with error lines: Expected a filename after the -o|--output option | rc=1", errors)
        self.assertIn("-> ok with error lines: ValueError: boom", errors)
        self.assertNotIn("should be ignored in Read", errors)
        self.assertNotIn("late error", errors)
        self.assertNotIn("Traceback (most recent call last): | ValueError", errors)

    def test_shell_cwd_moves_resets_relative_paths(self):
        root = str(self.root / "project")
        sub = str(Path(root) / "revision" / "step-1.5")
        rows = [
            {**user("go"), "cwd": root},
            {**assistant([tool(args={"command": "cd revision/step-1.5 && ls"}, tid="c1")], minutes=31),
             "cwd": root},
            {**result("ok\nShell cwd was reset to " + root, tid="c1", minutes=31), "cwd": root},
            {**user("next", 33), "cwd": sub},
            {**assistant([tool(args={"command": "cd .."}, tid="c2")], minutes=33, mid="m2"), "cwd": sub},
            {**result("ok", tid="c2", minutes=34), "cwd": sub},
            {**user("back", 35), "cwd": root},
            command(minutes=50),
            {**user([{"type": "tool_result", "tool_use_id": "late", "content":
                      "Shell cwd was reset to " + root, "is_error": False}], 51), "cwd": "/tmp"},
        ]
        got = self.digest(self.session(rows=rows))
        cwd = self.section(got["digest"], "Shell cwd")
        self.assertIn("Start cwd: " + afr._start_path(root), cwd)
        self.assertIn("cwd moves: 2", cwd)
        self.assertIn(". -> revision/step-1.5 after Bash", cwd)
        self.assertIn("revision/step-1.5 -> . after Bash", cwd)
        self.assertIn("Shell cwd resets: 1", cwd)
        self.assertIn("-> reset to " + afr._start_path(root), cwd)
        self.assertIn("; cwd reset", got["transcript"])
        self.assertNotIn("+51.0", cwd)

    def test_sidechain_ignored_in_new_sections(self):
        rows = [
            user("go", origin={"kind": "human"}),
            assistant([tool(tid="main")], minutes=1),
            result("ok", tid="main"),
            self.attachment("silent_turn_reminder", 2, text="side", isSidechain=True),
            {**assistant([tool(tid="side")], minutes=3, mid="s", isSidechain=True),
             "message": {"id": "s", "model": "claude-test", "content": [tool(tid="side")],
                         "usage": {"input_tokens": 500_000, "cache_creation_input_tokens": 0,
                                   "cache_read_input_tokens": 0, "output_tokens": 1}}},
            result("error side", True, "side", isSidechain=True),
            result("Shell cwd was reset to /tmp", tid="side2", isSidechain=True),
        ]
        # attachment helper doesn't pass isSidechain onto row - fix
        rows[3] = {"type": "attachment", "timestamp": stamp(2), "isSidechain": True,
                   "attachment": {"type": "silent_turn_reminder", "text": "side"}}
        got = self.digest(self.session(rows=rows))
        self.assertIn("Silent-turn reminders: 0", self.section(got["digest"], "Harness notes"))
        self.assertIn("Error results: 0", self.section(got["digest"], "Errors"))
        self.assertIn("Shell cwd resets: 0", self.section(got["digest"], "Shell cwd"))
        self.assertNotIn("500k", self.section(got["digest"], "Context"))

    def test_section_caps_keep_count_lines(self):
        rows = [user("go")]
        for n in range(40):
            rows += [assistant([tool(args={"command": f"cd /tmp/out{n} && ls " + "z" * 200},
                                     tid=f"t{n}")],
                               minutes=n + 1, mid=f"m{n}"),
                     result(f"Shell cwd was reset to /tmp/project\nerror: boom-{n}\n" + ("pad " * 100),
                            True, f"t{n}", minutes=n + 1)]
            rows.append(self.attachment(
                "hook_system_message", n + 1, hookEvent="UserPromptSubmit",
                hookName="UserPromptSubmit", toolUseID=f"g{n}",
                content=("context guard: ~" + str(100 + n) + "k tokens; " + "x" * 180)))
        for n, row in enumerate(r for r in rows if r.get("type") == "assistant"):
            row["message"]["usage"] = {"input_tokens": 100_000 + n * 1000,
                                       "cache_creation_input_tokens": 0,
                                       "cache_read_input_tokens": 0, "output_tokens": 1}
        root = "/tmp/project"
        for i, row in enumerate(rows):
            if row.get("type") in ("user", "assistant"):
                row["cwd"] = root if i % 2 == 0 else root + "/sub" + str(i)
        got = self.digest(self.session(rows=rows))
        for name, cap in (("Context", afr.CONTEXT_CAP), ("Harness notes", afr.HARNESS_CAP),
                          ("Errors", afr.ERRORS_CAP), ("Shell cwd", afr.CWD_CAP)):
            self.assertLessEqual(len(self.section(got["digest"], name)), cap, name)
        harness = self.section(got["digest"], "Harness notes")
        errors = self.section(got["digest"], "Errors")
        cwd = self.section(got["digest"], "Shell cwd")
        self.assertIn("Context-guard messages: 40", harness)
        self.assertIn("see transcript.txt", harness)
        self.assertIn("Error results: 40", errors)
        self.assertIn("see transcript.txt", errors)
        self.assertIn("Shell cwd resets: 40", cwd)
        self.assertIn("see transcript.txt", cwd)

    def test_queued_typed_prompt_appears_in_transcript(self):
        queue = {"type": "attachment", "timestamp": stamp(5), "attachment": {
            "type": "queued_command", "commandMode": "prompt", "origin": {"kind": "human"},
            "prompt": "queued while busy"}}
        rows = [user("first", origin={"kind": "human"}),
                assistant([tool()], minutes=1),
                result("ok", minutes=2),
                queue,
                assistant([tool(tid="t2")], minutes=6, mid="m2"),
                result("done", tid="t2", minutes=7)]
        got = self.digest(self.session(rows=rows))
        self.assertIn("USER [+0.0]: first", got["transcript"])
        self.assertIn("USER [+5.0]: queued while busy", got["transcript"])
        # USER line must precede any later TOOL from the same flow.
        self.assertLess(got["transcript"].index("USER [+5.0]: queued while busy"),
                        got["transcript"].index("TOOL [+6.0]"))

    def test_context_cap_many_prompts_keeps_pickers(self):
        rows = [user(f"prompt-{n} " + "x" * 20, n) for n in range(400)]
        rows += [assistant([picker()], minutes=400),
                 result('"What next?"="Continue"', tid="picker", minutes=401)]
        # Zero context so Calls drops the row; the prompt list alone must respect the cap.
        rows[-2]["message"]["usage"] = {"input_tokens": 0, "cache_creation_input_tokens": 0,
                                        "cache_read_input_tokens": 0, "output_tokens": 1}
        got = self.digest(self.session(rows=rows))
        self.assertLessEqual(got["digest_chars"], 60000)
        ctx = self.section(got["digest"], "Context")
        self.assertLessEqual(len(ctx), afr.CONTEXT_CAP)
        self.assertRegex(ctx, r"At each typed prompt: .* \(\d+ more\)")
        self.assertNotIn(rate.TRUNCATED, ctx)
        self.assertIn("## Pickers\n### Picker 1", got["digest"])
        self.assertIn("Answer: Continue", got["digest"])

    def test_hook_stdout_non_object_is_ignored(self):
        rows = [user("go")]
        for n, stdout in enumerate(("[]", "null", '"ordinary stdout"', "not-json-at-all",
                                    json.dumps({"hookSpecificOutput": []})), 1):
            rows.append(self.attachment(
                "hook_success", n, hookEvent="PreToolUse", hookName="PreToolUse:Bash",
                toolUseID=f"h{n}", stdout=stdout))
        ask = json.dumps({"hookSpecificOutput": {
            "permissionDecision": "ask", "permissionDecisionReason": "real ask"}})
        rows.append(self.attachment(
            "hook_success", 10, hookEvent="PreToolUse", hookName="PreToolUse:Bash",
            toolUseID="ok", stdout=ask))
        got = self.digest(self.session(rows=rows))
        harness = self.section(got["digest"], "Harness notes")
        self.assertIn("Hook ask/deny decisions: 1", harness)
        self.assertIn("real ask", harness)
        self.assertIn("HARNESS [+10.0] hook ask PreToolUse:Bash: real ask", got["transcript"])

    def test_cwd_reset_path_with_spaces(self):
        root = "/tmp/my project"
        rows = [
            {**user("go"), "cwd": root},
            {**assistant([tool(args={"command": "cd /tmp/elsewhere"}, tid="c1")], minutes=1),
             "cwd": root},
            {**result("ok\nShell cwd was reset to /tmp/my project", tid="c1", minutes=2),
             "cwd": root},
        ]
        got = self.digest(self.session(rows=rows))
        cwd = self.section(got["digest"], "Shell cwd")
        self.assertIn("Shell cwd resets: 1", cwd)
        self.assertIn("-> reset to /tmp/my project", cwd)
        self.assertNotIn("reset to /tmp/my\n", cwd + "\n")

    def test_context_tool_results_without_usage(self):
        rows = [
            user("go"),
            assistant([tool("Read", {"file_path": "big.py"}, "r1")], minutes=1, mid="m1"),
            result("x" * 5000, tid="r1", minutes=2),
        ]
        rows[1]["message"]["usage"] = {"input_tokens": 0, "cache_creation_input_tokens": 0,
                                       "cache_read_input_tokens": 0, "output_tokens": 1}
        got = self.digest(self.session(rows=rows))
        ctx = self.section(got["digest"], "Context")
        self.assertIn("Calls: 0; start -; max -; last -.", ctx)
        self.assertIn('Read {"file_path": "big.py"} -> 5,000 chars', ctx)
        self.assertIn("Tool result chars by tool: Read 5,000", ctx)

    def test_adversarial_cap_shrinks_new_sections_before_friction(self):
        # Full new sections (within their caps) would push past 60k; shrinking them
        # to count lines first keeps every friction window.
        rows = [user(f"P{n} " + "p" * 1200, minutes=n) for n in range(15)]
        rows += [assistant([picker()], minutes=15),
                 result('"What next?"="Continue"', tid="picker", minutes=16)]
        for n in range(12):
            rows += [assistant([{"type": "text", "text": "body " + "x" * 1800},
                                tool(args={"command": "echo x"}, tid=f"e{n}")],
                               minutes=100 + n * 5, mid=f"m{n}"),
                     result("ordinary error " + "e" * 700, True, f"e{n}", minutes=100 + n * 5 + 1)]
        for n in range(40):
            rows += [assistant([tool(args={"command": f"cmd {n}"}, tid=f"o{n}")],
                               minutes=200 + n, mid=f"mo{n}"),
                     result(f"error: fail-{n}\nrc=1\n" + "z" * 200, False, f"o{n}", minutes=200 + n)]
        for n in range(8):
            rows += [assistant([tool("Read", {"file_path": f"f{n}.py"}, f"r{n}")],
                               minutes=300 + n, mid=f"mr{n}"),
                     result("R" * 5000, False, f"r{n}", minutes=300 + n)]
        root = "/tmp/project"
        for n in range(15):
            rows += [{**assistant([tool(args={"command": f"cd /tmp/x{n}"}, tid=f"c{n}")],
                                  minutes=400 + n, mid=f"mc{n}"), "cwd": root},
                     {**result(f"Shell cwd was reset to {root}", tid=f"c{n}", minutes=400 + n),
                      "cwd": root}]
        for row in rows:
            if row.get("type") in ("user", "assistant") and "cwd" not in row:
                row["cwd"] = root
        for n in range(60):
            rows.append(self.attachment(
                "hook_system_message", n + 1, hookEvent="UserPromptSubmit",
                hookName="UserPromptSubmit", toolUseID=f"g{n}",
                content="context guard: ~120k tokens; " + ("y" * 350)))
        got = self.digest(self.session(rows=rows))
        self.assertLessEqual(got["digest_chars"], 60000)
        friction = self.section(got["digest"], "Friction excerpts")
        self.assertEqual(got["events"]["error"], 12)
        self.assertEqual(friction.count("### E"), got["events"]["error"] + got["events"]["idle"])
        self.assertNotIn("more friction events", friction)
        harness = self.section(got["digest"], "Harness notes")
        self.assertIn("Context-guard messages: 60", harness)
        self.assertNotIn("yyy", harness)
        self.assertFalse(any(line.startswith("guard ") for line in harness.splitlines()))


class CLITests(Fixtures):
    def test_select_json_text_seed_log_and_privacy(self):
        self.session()
        code, out, err = self.invoke("--select", "--json", "--seed", "2")
        self.assertEqual((code, err), (0, ""))
        got = json.loads(out)
        self.assertEqual(got["counts"]["eligible"], 1)
        self.assertEqual(got["seed"], 2)
        self.assertNotIn(PRIVATE, out)
        code, out, err = self.invoke("--select", "--seed", "2")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(len(out.splitlines()), 2)
        self.assertIn("00000001 orchestrator: rank", out)
        self.assertNotIn(PRIVATE, out + err)
        self.assertFalse(self.out.exists())

    def test_digest_exit_codes_files_unique_prefix_full_id_dedup_and_schema(self):
        good = self.session(1)
        bad = self.session(2, rows=[user(), assistant([tool("Skill", {"skill": "field-report"})])])
        code, out, err = self.invoke("--digest", good[:8], bad, "--out", str(self.out), "--json")
        self.assertEqual((code, err), (1, ""))
        sessions = json.loads(out)["sessions"]
        self.assertEqual([s["status"] for s in sessions], ["built", "dropped"])
        self.assertEqual(set(sessions[0]), {"session", "status", "reason", "cut_row", "rows", "handoff_after_cut",
                                         "digest_chars", "transcript_chars", "events", "digest", "transcript"})
        for key, filename in (("digest", "digest.md"), ("transcript", "transcript.txt")):
            self.assertEqual(Path(sessions[0][key]), self.out / good[:8] / filename)
            text = Path(sessions[0][key]).read_text(encoding="utf-8")
            self.assertEqual(len(text), sessions[0][key + "_chars"])
            self.assertNotIn(PRIVATE, text)
        self.assertFalse((self.out / bad[:8]).exists())
        self.assertNotIn(PRIVATE, out + err)
        code, out, err = self.invoke("--digest", good, good[:8], "--out", str(self.out), "--json")
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(out)["sessions"]), 1)
        code, out, err = self.invoke("--digest", good, "--out", str(self.out))
        self.assertEqual(code, 0)
        self.assertIn("00000001 built", out)

    def test_unreadable_session_does_not_stop_other_writes(self):
        good = self.session(1)
        bad = self.session(2)
        Path(self.record(bad)["summary"]["transcript"]).unlink()
        code, out, err = self.invoke("--digest", bad[:8], good[:8], "--out", str(self.out), "--json")
        self.assertEqual(code, 1)
        self.assertEqual([s["status"] for s in json.loads(out)["sessions"]], ["dropped", "built"])
        self.assertTrue((self.out / good[:8] / "digest.md").exists())

    def test_usage_errors(self):
        self.session(1)
        self.session(2)
        for args in (("--select", "--digest", "00000001"), ("--digest", "00000001"),
                     ("--select", "--since", "bad"), ("--select", "--until", "bad"),
                     ("--select", "--seed", "bad"), ("--select", "--auto"),
                     ("--digest", "000", "--out", str(self.out)),
                     ("--digest", "abc", "--out", str(self.out))):
            with self.subTest(args=args):
                code, out, err = self.invoke(*args)
                self.assertEqual(code, 2)
                self.assertNotIn(PRIVATE, out + err)
        self.assertFalse(self.out.exists())

    def test_delegate_entry_point_sets_digest_exit_code(self):
        sid = self.session(rows=[user(), assistant([tool("Skill", {"skill": "field-report"})])])
        self.save(self.log, self.rows)
        result = subprocess.run([sys.executable, str(SCRIPTS / "delegate"), "report", "--digest", sid[:8],
                                 "--out", str(self.out), "--log", str(self.log), "--json"],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(json.loads(result.stdout)["sessions"][0]["status"], "dropped")
        self.assertFalse(self.out.exists())

    def test_commit_slack_default_unchanged_and_zero_is_exact(self):
        self.commit_patcher.stop()
        with mock.patch("rate.subprocess.run", return_value=mock.Mock(returncode=0, stdout="")) as run:
            rate.session_commits(self.root, BASE, BASE)
            self.assertIn("--until=" + (BASE + timedelta(minutes=10)).isoformat(), run.call_args.args[0])
            rate.session_commits(self.root, BASE, BASE, slack_minutes=0)
            self.assertIn("--until=" + BASE.isoformat(), run.call_args.args[0])


if __name__ == "__main__":
    unittest.main()

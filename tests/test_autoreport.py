"""Automatic field-report dry runs: fake logs, transcripts and feedback only."""
import io
import json
import os
import random
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
        headers = ["Numbers", "Prompts", "Pickers", "HANDOFF.md at session start",
                   "HANDOFF.md at session end", "Commits in the session window", "Friction excerpts"]
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
        self.assertTrue(lines[3].endswith("-> error, 12 chars"))
        self.assertLessEqual(len(lines[2].split("Bash ", 1)[1].split(" -> ", 1)[0]), 160)
        for forbidden in ("REMINDER-SECRET", "THINKING-SECRET", "not typed", "later-result"):
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

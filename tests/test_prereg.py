"""Confirmatory analysis: fake logs/transcripts and synthetic git objects only."""
import hashlib
import io
import json
import math
import os
import random
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/delegate/scripts"
sys.path.insert(0, str(SCRIPTS))
import cost
import prereg as p
import rate

NOW = p.T0 + timedelta(days=3)
AFTER = p.HARD_CLOSE + p.FREEZE_DELAY


def user(ts="2026-11-02T12:00:00Z"):
    return {"type": "user", "timestamp": ts, "message": {"content": "do work"}}


def item(**fields):
    return {"start": 0, "type": "code", "size": "M", "status": "finished", "continuation": False, **fields}


def start(session="s", mode="claude-only", ts=None, **fields):
    return {"event": "start", "session": session, "ts": ts or "2026-11-02T12:00:00Z", "u": 0,
            "mode": mode, "weights": {"claude-only": .15, "orchestrator": .7, "vanilla": .15}, **fields}


def summary(session="s", **fields):
    return {"event": "summary", "session": session, "transcript": "/missing", "calls": 1,
            "usd_list_price": 2.0, "usd_unpriced_calls": 0, **fields}


def rating(session="s", **fields):
    return {"event": "rating", "session": session, "ts": "2026-11-03T12:00:00Z", "rubric": "v2",
            "items": [item()], "prompts": 1, "prompt_ts": ["2026-11-02T12:00:00Z"], **fields}


def write(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


class PreregTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="prereg-test-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.log, self.run_log = self.root / "sessions.jsonl", self.root / "runs.jsonl"
        self.run_log.write_text("")
        self.transcript = self.root / "transcript.jsonl"
        write(self.transcript, [user()])
        self.env = mock.patch.dict(os.environ, {"CLAUDE_SESSION_LOG": str(self.root / "experiment.jsonl"),
                                               "DELEGATE_CATALOG_LOCAL": "",
                                               "CLAUDE_DELEGATE_QUOTA_LOG": str(self.root / "quota.jsonl")})
        self.env.start()
        self.addCleanup(self.env.stop)

    def records(self, rows, close=p.HARD_CLOSE):
        return p.window_records(p.session_records(rows), close)

    def analysed(self, rows):
        records = self.records(rows)
        counts = {r["session"]: r["rating"]["prompts"] for r in records}
        return p.analysis_sessions(records, counts)

    def invoke(self, *args, now=NOW):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(p, "_now", return_value=now), redirect_stdout(out), redirect_stderr(err):
            try:
                p.main(["--log", str(self.run_log), "--repo-dir", str(self.root),
                        "--calibration", str(self.root / "no-calibration"), *args])
                code = 0
            except SystemExit as exc:
                code = exc.code
                if isinstance(code, str):
                    print(code, file=err)
        return code, out.getvalue(), err.getvalue()

    def test_window_strict_bounds_non_drawn_and_first_start_assignment(self):
        rows = [start("at-t0", ts=p.iso(p.T0)), start("in", ts=p.iso(p.T0 + timedelta(seconds=1))),
                start("at-close", ts=p.iso(p.HARD_CLOSE)), start("forced", forced=True),
                start("in", mode="vanilla", ts=p.iso(p.HARD_CLOSE), forced=True)]
        found = self.records(rows)
        self.assertEqual([r["session"] for r in found], ["in"])
        self.assertEqual(found[0]["start"]["mode"], "claude-only")

    def test_latest_summary_rating_end_in_file_order(self):
        rows = [start(), summary(usd_list_price=100), rating(prompts=4),
                {"event": "end", "session": "s", "reroll": False},
                summary(usd_list_price=2), rating(), {"event": "end", "session": "s", "reroll": True}]
        s = self.analysed(rows)[0]
        self.assertEqual(s["usd"], 2)
        self.assertTrue(s["record"]["end"]["reroll"])

    def sixty(self, n=60):
        rows = []
        for i in range(n):
            rows.extend([start(str(i)), summary(str(i)), rating(str(i))])
        return rows

    def test_close_59_date_provisional_and_60th_first_v2_distinct(self):
        rows = self.sixty(59)
        info = p.progress(rows, NOW)
        self.assertEqual((info["close"], info["close_by"], info["close_provisional"]),
                         (p.iso(p.HARD_CLOSE), "date", True))
        self.assertEqual(info["progress"]["rated_claude_only"], 59)
        rows += [rating("0", ts="2026-11-29T00:00:00Z"), start("last"), summary("last"),
                 rating("last", rubric="v1"), rating("last", ts="2026-11-04T01:00:00+01:00"),
                 rating("last", ts="2026-11-05T00:00:00Z")]
        info = p.progress(rows, NOW)
        self.assertEqual(info["close"], "2026-11-04T00:00:00Z")
        self.assertEqual(info["close_by"], "target")
        self.assertEqual(info["freeze"], "2026-11-11T00:00:00Z")
        self.assertFalse(info["close_provisional"])
        self.assertEqual(p.progress([], AFTER)["close_provisional"], False)

    def test_close_ignores_reratings_v1_other_arm_pre_t0_ghost_and_hard_close_starts(self):
        rows = self.sixty(59)
        rows += [rating("0"), rating("0"), start("v1"), rating("v1", rubric="v1"),
                 start("o", "orchestrator"), rating("o"),
                 start("early", ts=p.iso(p.T0)), rating("early"),
                 start("ghost"), summary("ghost", transcript=None, calls=0), rating("ghost"),
                 start("at", ts=p.iso(p.HARD_CLOSE)), rating("at"),
                 start("late", ts=p.iso(p.HARD_CLOSE + timedelta(seconds=1))), rating("late"),
                 start("forced", forced=True), rating("forced")]
        self.assertEqual(p.progress(rows, NOW)["progress"]["rated_claude_only"], 59)
        self.assertEqual(p.progress(rows, NOW)["close_by"], "date")

    def test_close_60th_rating_after_hard_close_and_log_order_with_offsets(self):
        rows = self.sixty(59)
        rows += [start("last"), rating("last", ts="2026-12-02T00:00:00+01:00")]
        self.assertEqual(p.progress(rows, NOW)["close"], p.iso(p.HARD_CLOSE))
        self.assertEqual(p.progress(rows, NOW)["close_by"], "target")
        rows = self.sixty(58)
        rows += [start("a"), start("b"), rating("a", ts="2026-11-09T04:00:00-04:00"),
                 rating("b", ts="2026-11-04T10:00:00+01:00")]
        self.assertEqual(p.progress(rows, NOW)["close"], "2026-11-04T09:00:00Z")

    def test_exclusions_each_reason_and_priority_unpriced_before_unrated(self):
        rows = [start("pending"), start("ghost"), summary("ghost", transcript=None, calls=0),
                start("unpriced"), summary("unpriced", usd_unpriced_calls=1),
                start("unrated"), summary("unrated"), rating("unrated", prompts=True),
                start("transcript_gone"), summary("transcript_gone"), rating("transcript_gone"),
                start("stale"), summary("stale"), rating("stale"),
                start("analysed"), summary("analysed"), rating("analysed")]
        sessions = p.analysis_sessions(self.records(rows), {"stale": 2, "analysed": 1})
        self.assertEqual({s["session"]: s["status"] for s in sessions},
                         {name: name for name in (*p.EXCLUSIONS, "analysed")})
        for usd in (None, "2", True, math.nan, math.inf):
            self.assertEqual(p.preliminary_status({"summary": summary(usd_list_price=usd)}), "unpriced")
        for bad in ({"rubric": "v1"}, {"items": {}}, {"prompts": 1.0}):
            self.assertEqual(p.preliminary_status({"summary": summary(), "rating": rating(**bad)}), "unrated")

    def test_stale_for_more_and_fewer_prompts_and_summary_else_start_path(self):
        rows = [start(transcript=str(self.transcript)), summary(transcript=None), rating(prompts=2)]
        records = self.records(rows)
        counts, _ = p.read_transcript_inputs(records, ({}, None))
        self.assertEqual(counts, {"s": 1})
        self.assertEqual(p.analysis_sessions(records, counts)[0]["status"], "stale")
        write(self.transcript, [user(), user(), user()])
        counts, _ = p.read_transcript_inputs(records, ({}, None))
        self.assertEqual(p.analysis_sessions(records, counts)[0]["status"], "stale")

    def test_transcript_gone_null_missing_unreadable(self):
        for path in (None, str(self.root / "missing"), str(self.root)):
            rows = [start(), summary(transcript=path), rating()]
            counts, _ = p.read_transcript_inputs(self.records(rows), ({}, None))
            self.assertEqual(p.analysis_sessions(self.records(rows), counts)[0]["status"], "transcript_gone")
        with mock.patch.object(rate, "json_rows", side_effect=PermissionError):
            counts, _ = p.read_transcript_inputs(self.records([start(), summary(), rating()]), ({}, None))
            self.assertIsNone(counts["s"])

    def test_ratio_edges_nearest_rank_infinity_share_and_zero_over_zero(self):
        self.assertEqual(p.ratio(2, 0), math.inf)
        self.assertEqual(p.ratio(0, 2), 0)
        self.assertIsNone(p.ratio(0, 0))
        found = p.interval(1, list(range(10000)))
        self.assertEqual((found["lo"], found["hi"]), (249, 9749))
        found = p.interval(math.inf, [1] * 9000 + [math.inf] * 1000)
        self.assertEqual((found["hi"], found["inf_share"]), (math.inf, .1))
        for observed, values in ((None, [1] * 10000), (1, [None] + [2] * 9999)):
            found = p.interval(observed, values)
            self.assertEqual((found["lo"], found["hi"], found["reading"], found["zero_over_zero"]),
                             (0, math.inf, "inconclusive", True))
        self.assertEqual(p.interval(2, [2] * 10000)["reading"], "more expensive")
        self.assertEqual(p.interval(.5, [.5] * 10000)["reading"], "orchestrator cheaper")
        self.assertEqual(p.interval(2, [2] * 10000, hypothesis="h2")["reading"], "above 1")

    def test_rng_all_sorted_arms_and_sorted_sessions_matches_independent_loop(self):
        sessions = [dict(session=s, mode=m, ts=p.instant(ts), usd=u, finished=f) for s, m, ts, u, f in (
            ("z", "claude-only", "2026-11-02T13:00:00+01:00", 2, 1),
            ("a", "claude-only", "2026-11-02T12:00:00Z", 4, 2),
            ("o", "orchestrator", "2026-11-03T00:00:00Z", 3, 2),
            ("v3", "vanilla", "2026-11-03T00:00:00Z", 7, 1),
            ("v1", "vanilla", "2026-11-02T00:00:00Z", 5, 2),
            ("v2", "vanilla", "2026-11-04T00:00:00Z", 6, 0))]
        rng = random.Random(0)
        expected = ([], [])
        for _ in range(10000):
            sums = []
            for mode in sorted(("orchestrator", "claude-only", "vanilla")):
                arm = sorted((s for s in sessions if s["mode"] == mode), key=lambda s: (s["ts"], s["session"]))
                draws = [arm[rng.randrange(len(arm))] for _ in range(len(arm))]
                sums.append((math.fsum(s["usd"] for s in draws), sum(s["finished"] for s in draws)))
            (uc, fc), (uo, fo), (uv, fv) = sums
            expected[0].append(uo * fc / (uc * fo))
            expected[1].append(uv * fc / (uc * fv) if fv else math.inf)
        self.assertEqual(p.session_replicates(p.session_tuples(sessions)), expected)

    def test_project_days_utc_dates_all_arms_and_independent_draw_order(self):
        rows = [start("c", ts="2026-11-02T23:30:00+01:00", repo_dir="b"), summary("c"), rating("c"),
                start("o", "orchestrator", ts="2026-11-03T00:30:00+01:00", repo_dir="b"), summary("o"), rating("o"),
                start("c2", ts="2026-11-04T00:00:00Z", repo_dir="b"), summary("c2", usd_list_price=5), rating("c2"),
                start("o2", "orchestrator", ts="2026-11-04T00:00:00Z", repo_dir="b"), summary("o2", usd_list_price=1), rating("o2"),
                start("v", "vanilla", repo_dir="a"), summary("v"), rating("v")]
        sessions = self.analysed(rows)
        self.assertEqual(sessions[0]["date"], sessions[1]["date"])
        prepared = p.project_day_tuples(sessions)
        self.assertEqual(len(prepared[1][0][0]), 1)
        self.assertEqual(len(prepared[1][0][1]), 1)
        expected, rng = ([], []), random.Random(0)
        for _ in range(10000):
            drawn = []
            for project in sorted({s["project"] for s in sessions}):
                dates = sorted({s["ts"].date() for s in sessions if s["project"] == project})
                for _ in range(len(dates)):
                    date = dates[rng.randrange(len(dates))]
                    drawn.extend(s for s in sessions if s["project"] == project and s["ts"].date() == date)
            totals = []
            for arm in sorted(("claude-only", "orchestrator", "vanilla")):
                totals.append((math.fsum(s["usd"] for s in drawn if s["mode"] == arm),
                               sum(s["finished"] for s in drawn if s["mode"] == arm)))
            (uc, fc), (uo, fo), (uv, fv) = totals
            for dest, num, den in ((expected[0], uo * fc, uc * fo), (expected[1], uv * fc, uc * fv)):
                dest.append(num / den if den else math.inf if num else None)
        self.assertEqual(p.project_day_replicates(prepared), expected)

    def test_no_continuations_item_costs_timed_calls_field_report_kept_and_failure_variant_only(self):
        transcript = [user("2026-11-02T12:00:00Z"), user("2026-11-02T13:00:00Z"), user("2026-11-02T14:00:00Z")]
        for i, hour in enumerate((12, 13, 14)):
            transcript.append({"type": "assistant", "timestamp": f"2026-11-02T{hour}:01:00Z",
                               "message": {"id": str(i), "model": "fake", "usage": {"input_tokens": 1000000}}})
        write(self.transcript, transcript)
        items = [item(continuation=True), item(start=1, type="field-report", continuation=True), item(start=2)]
        rows = [start(), summary(transcript=str(self.transcript), usd_list_price=9),
                rating(prompts=3, prompt_ts=[r["timestamp"] for r in transcript[:3]], items=items)]
        counts, costs = p.read_transcript_inputs(self.records(rows), ({}, {"models": {"fake": {
            "input": 2, "cache_read": 0, "cache_write_5m": 0, "cache_write_1h": 0, "output": 0}}}))
        sessions = p.analysis_sessions(self.records(rows), counts)
        kept, excluded = p.no_continuations(sessions, costs)
        self.assertEqual((kept[0]["usd"], kept[0]["finished"]), (7, 1))
        self.assertFalse(excluded["claude-only"])
        with mock.patch.object(rate, "item_costs", side_effect=ValueError("bad")):
            counts, costs = p.read_transcript_inputs(self.records(rows), ({}, None))
        self.assertEqual(p.analysis_sessions(self.records(rows), counts)[0]["status"], "analysed")
        self.assertEqual(p.no_continuations(sessions, costs)[1]["claude-only"], {"item_costs_failed": 1})
        costs["s"] = [{"usd": None}, {"usd": None}, {"usd": 2}]
        self.assertEqual(p.no_continuations(sessions, costs)[1]["claude-only"], {"continuation_unpriced": 1})
        costs["s"][0]["usd"] = 2
        self.assertEqual(p.no_continuations(sessions, costs)[0][0]["usd"], 7)  # field report None ignored

    def test_under_8h_exactly_stays_over_leaves_missing_stays_counted(self):
        rows = []
        for session, end in (("eight", "2026-11-02T20:00:00Z"), ("over", "2026-11-02T20:00:01Z"), ("missing", None)):
            rows += [start(session), summary(session, first_call="2026-11-02T12:00:00Z", last_call=end), rating(session)]
        kept, counts = p.under_8h(self.analysed(rows))
        self.assertEqual([s["session"] for s in kept], ["eight", "missing"])
        self.assertEqual(counts["claude-only"], {"over_8h": 1, "no_call_times": 1})

    def dry_rows(self):
        rows = []
        for sid, mode, usd, items in (("c1", "claude-only", 2, [item()]),
                                      ("c2", "claude-only", 4, [item(), item(status="partial")]),
                                      ("o1", "orchestrator", 3, [item(), item()]),
                                      ("v1", "vanilla", 1, [item(status="unclear")])):
            rows += [start(sid, mode), summary(sid, transcript=str(self.transcript), usd_list_price=usd), rating(sid, items=items)]
        return rows

    def test_cli_dry_run_hand_computed_primary_unclear_and_replicate_support(self):
        write(self.log, self.dry_rows())
        code, out, err = self.invoke("--outcomes", "--json", "--session-log", str(self.log))
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        arms = data["outcomes"]["arms"]
        self.assertEqual((arms["claude-only"]["usd"], arms["claude-only"]["finished"],
                          arms["orchestrator"]["usd"], arms["orchestrator"]["finished"]), (6, 2, 3, 2))
        self.assertEqual(data["outcomes"]["primary"]["h1"]["observed"], .5)
        self.assertEqual(data["outcomes"]["primary"]["h2"]["observed"], "inf")
        self.assertEqual(data["outcomes"]["sensitivity"]["unclear_as_finished"]["h2"]["observed"], 1 / 3)
        sessions = self.analysed(self.dry_rows())
        h1, _ = p.session_replicates(p.session_tuples(sessions))
        self.assertEqual(set(h1), {.75, .5, .375})
        self.assertNotIn("Infinity", out)
        self.assertNotIn("NaN", out)

    def test_guard_default_explicit_symlink_before_freeze_and_other_path_positive_control(self):
        write(self.log, self.dry_rows())
        link = self.root / "link"
        link.symlink_to(self.log)
        with mock.patch.dict(os.environ, {"CLAUDE_SESSION_LOG": str(self.log)}):
            for extra in ([], ["--session-log", str(self.log)], ["--session-log", str(link)]):
                with mock.patch.object(p, "outcomes", side_effect=AssertionError("outcome computed")), \
                        mock.patch.object(p, "read_transcript_inputs", side_effect=AssertionError("transcript opened")):
                    code, out, err = self.invoke("--outcomes", *extra)
                self.assertNotEqual(code, 0)
                self.assertIn("close is provisional at", err)
                for forbidden in ("usd", "$", "finished"):
                    self.assertNotIn(forbidden, (out + err).lower())
            other = self.root / "copy"
            other.write_bytes(self.log.read_bytes())
            code, out, err = self.invoke("--outcomes", "--session-log", str(other))
            self.assertEqual(code, 0, err)
            self.assertIn("not the experiment's log", out)
            self.assertIn("USD", out)
            self.assertIn("FINISHED", out)

    def test_guard_target_known_names_freeze_and_after_freeze_outcomes_warning(self):
        rows = self.dry_rows() + self.sixty()
        write(self.log, rows)
        with mock.patch.dict(os.environ, {"CLAUDE_SESSION_LOG": str(self.log)}):
            code, out, err = self.invoke("--outcomes", "--json")
            self.assertNotEqual(code, 0)
            self.assertIn("freeze 2026-11-10T12:00:00Z", err)
            code, out, err = self.invoke("--outcomes", "--json", now=AFTER)
            self.assertEqual(code, 0, err)
            self.assertIsNotNone(json.loads(out)["outcomes"])
            self.assertEqual(len(err.splitlines()), 1)
            self.assertIn("no rating row at or after", err)
            rows.append(rating("c1", ts=p.iso(AFTER)))
            write(self.log, rows)
            self.assertEqual(self.invoke("--outcomes", "--json", now=AFTER)[2], "")

    def test_progress_missing_transcripts_garbage_items_reads_counts_only(self):
        rows = [start(), summary(transcript="/definitely/missing", usd_list_price="garbage"), rating(items="garbage")]
        write(self.log, rows)
        with mock.patch.object(rate, "json_rows", side_effect=AssertionError("read")), \
                mock.patch.object(rate, "item_costs", side_effect=AssertionError("cost")):
            code, out, err = self.invoke("--json", "--session-log", str(self.log))
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertIsNone(data["outcomes"])
        self.assertEqual(set(data["progress"]), {"rated_claude_only", "arms"})
        self.assertEqual(data["progress"]["arms"]["claude-only"], {"drawn": 1, "ghosts": 0, "rated": 1})
        for arm in data["progress"]["arms"].values():
            self.assertEqual(set(arm), {"drawn", "ghosts", "rated"})
        for forbidden in ("usd", "items", "status", "finished"):
            self.assertNotIn(forbidden, out)

    def test_privacy_text_json_no_repo_repo_dir_or_cwd(self):
        rows = self.dry_rows()
        private = ("secret-repo-dir", "secret-owner/repo", "/fakehome/private-working-directory")
        for row in rows:
            if row["event"] == "start":
                row.update(repo_dir=private[0], repo=private[1], cwd=private[2])
        write(self.log, rows)
        for flags in ([], ["--json"], ["--outcomes"], ["--outcomes", "--json"]):
            code, out, err = self.invoke("--session-log", str(self.log), *flags)
            self.assertEqual(code, 0, err)
            for secret in private:
                self.assertNotIn(secret, out + err)
            if "--outcomes" in flags:
                self.assertIn(hashlib.sha256(private[0].encode()).hexdigest()[:8], out)

    def test_calibration_since_saved_at_rate_rules_and_raw_sha256(self):
        path = self.root / "calibration.json"
        self.assertFalse(p.read_calibration(path)["available"])
        data = {"saved_at": "2026-11-02T01:00:00+01:00", "since": "2026-10-26T18:01:00+01:00",
                "pools": [{"pool": "claude-7d", "headline": {"unit": "usd", "per_unit": .5}}]}
        for changed in ({}, {"since": "2026-10-26T17:01:01Z"}, {"saved_at": "2026-11-01T23:59:59Z"},
                        {"pools": []}, {"pools": [{"pool": "claude-7d", "headline": {"unit": "usd", "per_unit": 0}}]}):
            path.write_text(json.dumps({**data, **changed}) + "\n")
            result = p.read_calibration(path)
            self.assertEqual(result["available"], not changed)
            self.assertEqual(result["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())

    def test_allocation_unknown_weights_apart_and_tokens_join_zero_sessions(self):
        rows = self.dry_rows()
        rows += [start("o2", "orchestrator", weights=None), summary("o2", transcript=str(self.transcript)), rating("o2")]
        runs = [{"run": "worker", "ts": "2026-11-05T00:00:00Z", "session": "o1", "agent": "codex",
                 "rounds": [{"usage": {"input": 100, "cached_input": 40, "output": 20}}],
                 "reviews": [{"agent": "cursor", "usage": {"input": 60}}]}]
        counts, costs = p.read_transcript_inputs(self.records(rows), ({}, None))
        result = p.outcomes(rows, p.HARD_CLOSE, counts, costs, runs, {"available": True, "per_usd": .5}, None)
        self.assertEqual(result["delegate_tokens_per_orchestrator_session"]["codex"]["input"], 50)
        self.assertEqual(result["delegate_tokens_per_orchestrator_session"]["cursor"]["input"], 30)
        self.assertEqual(result["allocation"]["without_weights"]["orchestrator"], 1)
        self.assertEqual(result["allocation"]["expected"]["claude-only"], .6)
        self.assertEqual(result["pool_points"]["per_finished"]["claude-only"], 1.5)


    def test_outcomes_field_reports_weights_descriptives_exclusions_and_pure_inputs(self):
        rows = self.dry_rows()
        rows += [start("pending"), start("ghost"), summary("ghost", transcript=None, calls=0),
                 start("unpriced"), summary("unpriced", usd_unpriced_calls=1),
                 start("unrated"), summary("unrated"), start("gone"), summary("gone"), rating("gone"),
                 start("stale"), summary("stale"), rating("stale")]
        rows[0]["dirty"] = True
        rows += [{"event": "end", "session": "c1", "reroll": False},
                 {"event": "end", "session": "c1", "reroll": True}]
        rows[2]["items"] = [item(size="XL", continuation=True), item(type="docs", status="unclear"),
                              item(type="field-report", size="XL")]
        counts = {sid: 1 for sid in ("c1", "c2", "o1", "v1")}
        counts["stale"] = 0
        costs = {sid: [{"usd": 0} for _ in r["items"]] for sid in counts
                 for r in rows if r.get("event") == "rating" and r["session"] == sid}
        before = json.dumps([rows, counts, costs])
        out = p.outcomes(rows, p.HARD_CLOSE, counts, costs, [], {"available": False}, None)
        arm = out["arms"]["claude-only"]
        self.assertEqual((arm["window"], arm["analysed"], arm["ghosts"], arm["rerolls"], arm["dirty"]), (8, 2, 1, 1, 1))
        self.assertEqual(arm["excluded"], dict.fromkeys(p.EXCLUSIONS, 1))
        self.assertEqual((arm["usd"], arm["items"], arm["finished"], arm["finished_or_unclear"], arm["size_weighted"]),
                         (6, 4, 2, 3, 23))
        self.assertEqual(arm["by_type"], {"code": 3, "docs": 1})
        self.assertEqual(arm["by_size"], {"M": 3, "XL": 1})
        self.assertEqual(arm["continuation_share"], .25)
        self.assertEqual(arm["finished_share"], .5)
        self.assertEqual(json.dumps([rows, counts, costs]), before)
        self.assertIsNone(out["sensitivity"]["split"])

    def test_progress_rated_means_any_v2_not_latest_and_freeze_exact_instant(self):
        rows = [start(), summary(), rating(), rating(rubric="v1")]
        result = p.progress(rows, AFTER - timedelta(microseconds=1))
        self.assertFalse(result["freeze_reached"])
        self.assertEqual(result["progress"]["arms"]["claude-only"]["rated"], 1)
        self.assertEqual(result["progress"]["rated_claude_only"], 1)
        self.assertTrue(p.progress(rows, AFTER)["freeze_reached"])
        self.assertEqual(self.analysed(rows)[0]["status"], "unrated")


class SplitCommitTests(unittest.TestCase):
    """Write synthetic commits directly; never commit or switch the working checkout."""
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="prereg-git-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        self.head = None

    def git(self, *args, **kwargs):
        return subprocess.run(["git", "-c", "commit.gpgsign=false", "-C", str(self.root), *args],
                              check=True, text=True, capture_output=True, **kwargs).stdout.strip()

    def commit(self, timestamp, files, parents=None):
        for name, text in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        self.git("add", ".")
        tree = self.git("write-tree")
        parent_args = []
        for parent in (parents if parents is not None else ([self.head] if self.head else [])):
            parent_args.extend(("-p", parent))
        sha = self.git("commit-tree", tree, *parent_args, "-m", "synthetic fixture",
                       env={**os.environ, "GIT_COMMITTER_DATE": timestamp, "GIT_AUTHOR_DATE": timestamp})
        self.git("update-ref", "refs/heads/main", sha)
        self.head = sha
        return sha

    def split(self):
        return p.read_split_commit(p.HARD_CLOSE, repo_dir=self.root)

    def test_split_pre_t0_tests_only_biggest_rules_hooks_and_first_parent_only(self):
        self.commit("2026-10-29T00:00:00Z", {"rules/pre": "line\n" * 100})
        self.commit("2026-11-01T00:00:00Z", {"tests/big": "line\n" * 200})
        winner = self.commit("2026-11-02T00:00:00Z", {"rules/one": "line\n" * 10, "hooks/two": "line\n" * 5})
        main = self.commit("2026-11-03T00:00:00Z", {"rules/small": "line\n"})
        side = self.commit("2026-11-04T00:00:00Z", {"rules/side": "line\n" * 100}, parents=[winner])
        # Merge tree differs from first parent only by tests: side history must not be scored.
        (self.root / "rules/side").unlink()
        self.commit("2026-11-05T00:00:00Z", {"tests/merge": "merge\n"}, parents=[main, side])
        result, reason = self.split()
        self.assertIsNone(reason)
        self.assertEqual((result["sha"], result["lines"]), (winner, 15))
        self.assertEqual(result["committed_at"], "2026-11-02T00:00:00Z")

    def test_split_tie_earliest_timestamp_then_lower_sha(self):
        self.commit("2026-10-29T00:00:00Z", {"base": "a"})
        early = self.commit("2026-11-01T00:00:00Z", {"rules/a": "line\n" * 5})
        self.commit("2026-11-02T00:00:00Z", {"rules/b": "line\n" * 5})
        self.assertEqual(self.split()[0]["sha"], early)
        same_time = self.commit("2026-11-01T00:00:00Z", {"hooks/c": "line\n" * 5})
        self.assertEqual(self.split()[0]["sha"], min(early, same_time))

    def test_split_strict_bounds_root_skipped_none_missing_main_not_top_level(self):
        self.commit("2026-11-01T00:00:00Z", {"rules/root": "root\n" * 20})
        self.commit(p.iso(p.T0), {"rules/at": "line\n" * 30})
        self.commit(p.iso(p.HARD_CLOSE), {"hooks/close": "line\n" * 40})
        self.assertEqual(self.split(), (None, None))
        nested = self.root / "nested"
        nested.mkdir()
        self.assertIn("not a git top level", p.read_split_commit(p.HARD_CLOSE, repo_dir=nested)[1])
        empty = self.root / "empty"
        empty.mkdir()
        self.git("init", "-q", str(empty))
        self.assertIn("main history unavailable", p.read_split_commit(p.HARD_CLOSE, repo_dir=empty)[1])
        with mock.patch.object(p.subprocess, "run", side_effect=FileNotFoundError):
            self.assertIn("unavailable", self.split()[1])

    def test_split_provisional_flag_and_session_split_boundary(self):
        self.commit("2026-10-29T00:00:00Z", {"base": "a"})
        self.commit("2026-11-03T00:00:00Z", {"rules/a": "x\n"})
        split, _ = p.read_split_commit(p.HARD_CLOSE, repo_dir=self.root, provisional=True)
        self.assertTrue(split["provisional"])
        rows = []
        for sid, ts in (("before", "2026-11-02T23:59:59Z"), ("after", split["committed_at"])):
            for mode in p.ARMS:
                session = sid + mode
                rows += [start(session, mode, ts=ts), summary(session), rating(session)]
        counts = {r["session"]: 1 for r in rows if r["event"] == "rating"}
        costs = {session: [{"usd": 0}] for session in counts}
        result = p.outcomes(rows, p.HARD_CLOSE, counts, costs, [], {"available": False}, split)
        self.assertEqual(result["sensitivity"]["split"]["before"]["h1"]["observed"], 1)
        self.assertEqual(result["sensitivity"]["split"]["after"]["h1"]["observed"], 1)


if __name__ == "__main__":
    unittest.main()

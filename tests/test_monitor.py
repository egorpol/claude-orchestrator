"""Synthetic-log tests for quota windows, per-job costs and --unused plumbing."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/delegate/scripts"
sys.path.insert(0, str(SCRIPTS))
import monitor  # noqa: E402
import quota  # noqa: E402
import runs  # noqa: E402

CLI = SCRIPTS / "delegate"
UTC = timezone.utc


def ts(s):
    return datetime.fromisoformat(s)


def qrow(t, pools, reason="timer", run=None, job=None, **extra):
    row = {"ts": t if isinstance(t, str) else t.isoformat(), "reason": reason,
           "run": run, "pools": pools}
    if job is not None:
        row["job"] = job
    row.update(extra)
    return row


def pool(used, reset):
    return {"used_pct": used, "resets_at": reset if isinstance(reset, str) else reset.isoformat()}


class WindowTests(unittest.TestCase):
    def test_claude_7d_jitter_stays_one_window(self):
        anchor_a = ts("2026-10-20T21:59:59.8+00:00")
        anchor_b = ts("2026-10-20T22:00:00+00:00")
        rows = [
            qrow("2026-10-14T10:00:00+00:00", {"claude-7d": pool(10, anchor_a)}),
            qrow("2026-10-15T10:00:00+00:00", {"claude-7d": pool(20, anchor_b)}),
            qrow("2026-10-16T10:00:00+00:00", {"claude-7d": pool(30, anchor_a)}),
        ]
        wins = monitor.windows_for_pool(rows, "claude-7d", ts("2026-10-27T00:00:00+00:00"))
        self.assertEqual(len(wins), 1)
        self.assertEqual(wins[0]["peak"], 30)
        self.assertEqual(wins[0]["readings"], 3)
        self.assertEqual(wins[0]["resets_at"], anchor_a)

    def test_usage_drop_same_resets_opens_new_window(self):
        reset = ts("2026-10-26T22:00:00+00:00")
        rows = [
            qrow("2026-10-20T10:00:00+00:00", {"claude-7d": pool(99, reset)}),
            qrow("2026-10-20T11:00:00+00:00", {"claude-7d": pool(0, reset)}),
        ]
        now = ts("2026-10-27T00:00:00+00:00")
        wins = monitor.windows_for_pool(rows, "claude-7d", now)
        self.assertEqual(len(wins), 2)
        self.assertEqual(wins[0]["peak"], 99)
        self.assertEqual(wins[1]["peak"], 0)
        self.assertTrue(wins[1]["idle"])

    def test_early_flag(self):
        reset = ts("2026-10-20T22:00:00+00:00")
        rows = [
            qrow("2026-10-14T10:00:00+00:00", {"codex-7d": pool(17, reset)}),
            qrow("2026-10-16T08:00:00+00:00", {"codex-7d": pool(5, ts("2026-10-23T08:00:00+00:00"))}),
        ]
        wins = monitor.windows_for_pool(rows, "codex-7d", ts("2026-10-27T00:00:00+00:00"))
        self.assertTrue(wins[0]["early"])
        self.assertEqual(wins[0]["end"], wins[1]["start"])

    def test_codex_idle_drift_merges_no_expiry(self):
        start = ts("2026-10-15T00:00:00+00:00")
        rows = []
        for i in range(6):
            t = start + timedelta(minutes=10 * i)
            rows.append(qrow(t, {"codex-7d": pool(0, t + timedelta(days=7))}))
        wins = monitor.windows_for_pool(rows, "codex-7d", ts("2026-10-27T00:00:00+00:00"))
        self.assertEqual(len(wins), 1)
        self.assertTrue(wins[0]["idle"])
        self.assertEqual(wins[0]["merged"], 6)
        self.assertIsNone(wins[0]["expired"])
        summ = monitor.summarize_windows(wins)
        self.assertEqual(summ["windows"], 0)
        self.assertEqual(summ["idle_spans"], 1)
        self.assertEqual(summ["expired_sum"], 0.0)

    def test_lower_bound_gap_over_one_hour(self):
        reset = ts("2026-10-20T22:00:00+00:00")
        rows = [
            qrow("2026-10-14T10:00:00+00:00", {"claude-7d": pool(50, reset)}),
            qrow("2026-10-20T10:00:00+00:00", {"claude-7d": pool(60, reset)}),  # 12h before close
        ]
        wins = monitor.windows_for_pool(rows, "claude-7d", ts("2026-10-21T00:00:00+00:00"))
        self.assertTrue(wins[0]["lower_bound"])
        self.assertGreater(wins[0]["gap_h"], 1.0)
        self.assertEqual(wins[0]["expired"], 40.0)

    def test_open_window_out_of_totals(self):
        reset = ts("2026-11-01T22:00:00+00:00")
        rows = [qrow("2026-10-27T10:00:00+00:00", {"claude-7d": pool(12, reset)})]
        now = ts("2026-10-27T18:00:00+00:00")
        wins = monitor.windows_for_pool(rows, "claude-7d", now)
        self.assertFalse(wins[0]["closed"])
        summ = monitor.summarize_windows(wins)
        self.assertEqual(summ["windows"], 0)
        self.assertIsNone(summ["mean_peak"])

    def test_since_filters_summary(self):
        reset1 = ts("2026-10-15T22:00:00+00:00")
        reset2 = ts("2026-10-22T22:00:00+00:00")
        rows = [
            qrow("2026-10-10T10:00:00+00:00", {"claude-7d": pool(80, reset1)}),
            qrow("2026-10-16T10:00:00+00:00", {"claude-7d": pool(40, reset2)}),
            qrow("2026-10-20T10:00:00+00:00", {"claude-7d": pool(50, reset2)}),
        ]
        now = ts("2026-10-27T00:00:00+00:00")
        wins = monitor.windows_for_pool(rows, "claude-7d", now)
        all_sum = monitor.summarize_windows(wins)
        since = ts("2026-10-16T00:00:00+00:00")
        filtered = monitor.summarize_windows(wins, since=since)
        self.assertEqual(all_sum["windows"], 2)
        self.assertEqual(filtered["windows"], 1)
        self.assertEqual(filtered["mean_peak"], 50.0)

    def test_malformed_error_stale_skipped(self):
        reset = ts("2026-10-20T22:00:00+00:00")
        rows = [
            "not-json",
            {"ts": "bad", "pools": {"claude-7d": pool(10, reset)}},
            {"ts": "2026-10-14T10:00:00+00:00", "error": "boom",
             "pools": {"claude-7d": pool(10, reset)}},
            qrow("2026-10-14T11:00:00+00:00",
                 {"claude-7d": {**pool(20, reset), "stale": True}}),
            qrow("2026-10-14T12:00:00+00:00",
                 {"claude-7d": {**pool(30, reset), "stale_reset": True}}),
            qrow("2026-10-14T13:00:00+00:00", {"claude-7d": pool(40, reset)}),
        ]
        # load_rows would skip the string; feed mixed through windows via only dicts
        dict_rows = [r for r in rows if isinstance(r, dict)]
        wins = monitor.windows_for_pool(dict_rows, "claude-7d", ts("2026-10-27T00:00:00+00:00"))
        self.assertEqual(len(wins), 1)
        self.assertEqual(wins[0]["peak"], 40)
        self.assertEqual(wins[0]["readings"], 1)


class JobCostTests(unittest.TestCase):
    def setUp(self):
        self.reset = ts("2026-10-20T22:00:00+00:00")
        self.catalog = {
            "delegates": [
                {"agent": "codex", "model": "gpt-6.1-sol", "pools": ["codex-5h", "codex-7d"]},
                {"agent": "cursor", "model": "auto", "pools": ["cursor-auto"]},
            ]
        }

    def _run(self, start, minutes=10, agent="codex", model="gpt-6.1-sol", effort="xhigh",
             run="r1", n=1):
        return {
            "run": run, "agent": agent, "model": model, "effort": effort,
            "rounds": [{"n": n, "model": model, "effort": effort,
                        "started": start.isoformat(), "minutes": minutes}],
            "reviews": [],
        }

    def test_tagged_job_end_wins_over_lag(self):
        start = ts("2026-10-15T12:00:00+00:00")
        end = start + timedelta(minutes=10)
        qrows = [
            qrow(start, {"codex-5h": pool(10, self.reset), "codex-7d": pool(2, self.reset)},
                 reason="launch", run="r1", job="round-1"),
            qrow(end + timedelta(minutes=1),
                 {"codex-5h": pool(25, self.reset), "codex-7d": pool(4, self.reset)},
                 reason="job-end", run="r1", job="round-1"),
            qrow(end + timedelta(minutes=20),
                 {"codex-5h": pool(90, self.reset), "codex-7d": pool(20, self.reset)},
                 reason="timer"),
        ]
        result = monitor.job_costs(qrows, [self._run(start)], [], self.catalog)
        self.assertEqual(result["jobs"], 1)
        self.assertEqual(result["groups"][0]["n"], 1)
        self.assertEqual(result["groups"][0]["pools"]["codex-5h"]["median"], 15.0)
        self.assertEqual(result["groups"][0]["pools"]["codex-7d"]["median"], 2.0)

    def test_legacy_launch_match_within_180s(self):
        start = ts("2026-10-15T12:00:00+00:00")
        qrows = [
            qrow(start + timedelta(seconds=60),
                 {"codex-5h": pool(10, self.reset), "codex-7d": pool(2, self.reset)},
                 reason="launch", run="r1"),  # no job key
            qrow(start + timedelta(minutes=12),
                 {"codex-5h": pool(20, self.reset), "codex-7d": pool(3, self.reset)},
                 reason="timer"),
        ]
        result = monitor.job_costs(qrows, [self._run(start)], [], self.catalog)
        self.assertEqual(result["groups"][0]["pools"]["codex-5h"]["median"], 10.0)

    def test_overlap_other_job(self):
        start = ts("2026-10-15T12:00:00+00:00")
        other = start + timedelta(minutes=5)
        runs = [
            self._run(start, minutes=20, run="r1"),
            self._run(other, minutes=20, run="r2"),
        ]
        qrows = [
            qrow(start, {"codex-5h": pool(10, self.reset), "codex-7d": pool(2, self.reset)},
                 reason="launch", run="r1", job="round-1"),
            qrow(other, {"codex-5h": pool(10, self.reset), "codex-7d": pool(2, self.reset)},
                 reason="launch", run="r2", job="round-1"),
            qrow(start + timedelta(minutes=25),
                 {"codex-5h": pool(20, self.reset), "codex-7d": pool(3, self.reset)}),
        ]
        result = monitor.job_costs(qrows, runs, [], self.catalog)
        self.assertEqual(result["skipped"].get("overlap"), 2)

    def test_overlap_ask_both_and_launch_row(self):
        start = ts("2026-10-15T12:00:00+00:00")
        qrows = [
            qrow(start, {"codex-5h": pool(10, self.reset), "codex-7d": pool(2, self.reset)},
                 reason="launch", run="r1", job="round-1"),
            qrow(start + timedelta(minutes=12),
                 {"codex-5h": pool(20, self.reset), "codex-7d": pool(3, self.reset)}),
        ]
        sessions = [{"event": "delegate", "command": "ask", "agent": "both",
                     "ts": (start + timedelta(minutes=2)).isoformat()}]
        result = monitor.job_costs(qrows, [self._run(start)], sessions, self.catalog)
        self.assertEqual(result["skipped"].get("overlap"), 1)

        sessions2 = [{"event": "delegate", "command": "launch", "agent": "codex", "run": "other",
                      "ts": (start + timedelta(minutes=3)).isoformat()}]
        result2 = monitor.job_costs(qrows, [self._run(start)], sessions2, self.catalog)
        self.assertEqual(result2["skipped"].get("overlap"), 1)

    def test_own_launch_row_ignored(self):
        start = ts("2026-10-15T12:00:00+00:00")
        qrows = [
            qrow(start, {"codex-5h": pool(10, self.reset), "codex-7d": pool(2, self.reset)},
                 reason="launch", run="r1", job="round-1"),
            qrow(start + timedelta(minutes=12),
                 {"codex-5h": pool(20, self.reset), "codex-7d": pool(3, self.reset)}),
        ]
        sessions = [{"event": "delegate", "command": "launch", "agent": "codex", "run": "r1",
                     "ts": start.isoformat()}]
        result = monitor.job_costs(qrows, [self._run(start)], sessions, self.catalog)
        self.assertEqual(result["groups"][0]["n"], 1)
        self.assertNotIn("overlap", result["skipped"])

    def test_window_change_skip(self):
        start = ts("2026-10-15T12:00:00+00:00")
        qrows = [
            qrow(start, {"codex-5h": pool(10, self.reset), "codex-7d": pool(2, self.reset)},
                 reason="launch", run="r1", job="round-1"),
            qrow(start + timedelta(minutes=12),
                 {"codex-5h": pool(5, self.reset), "codex-7d": pool(1, self.reset)}),  # drop
        ]
        result = monitor.job_costs(qrows, [self._run(start)], [], self.catalog)
        self.assertEqual(result["skipped"].get("window changed"), 1)

    def test_median_p25_p75(self):
        start = ts("2026-10-15T12:00:00+00:00")
        runs_rows, qrows = [], []
        deltas = [2, 4, 6, 8]
        for i, d in enumerate(deltas, 1):
            st = start + timedelta(hours=i)
            runs_rows.append(self._run(st, run=f"r{i}"))
            qrows.append(qrow(st, {"codex-5h": pool(10, self.reset), "codex-7d": pool(1, self.reset)},
                              reason="launch", run=f"r{i}", job="round-1"))
            qrows.append(qrow(st + timedelta(minutes=12),
                              {"codex-5h": pool(10 + d, self.reset),
                               "codex-7d": pool(1 + d / 2, self.reset)}))
        result = monitor.job_costs(qrows, runs_rows, [], self.catalog)
        g = result["groups"][0]
        self.assertEqual(g["n"], 4)
        self.assertEqual(g["pools"]["codex-5h"]["median"], 5.0)
        self.assertEqual(g["pools"]["codex-5h"]["p25"], 3.5)
        self.assertEqual(g["pools"]["codex-5h"]["p75"], 6.5)

    def test_rounds_left(self):
        group = {"kind": "round", "n": 7, "pools": {
            "codex-5h": {"median": 15.0}, "codex-7d": {"median": 2.0}}}
        live = {"codex-5h": {"left_pct": 60}, "codex-7d": {"left_pct": 10}}
        # min(60/15, 10/2) = min(4, 5) = 4
        self.assertEqual(monitor.rounds_left(group, live), 4)
        self.assertIsNone(monitor.rounds_left({**group, "n": 2}, live))

    def test_malformed_timestamps_are_skipped(self):
        reset = self.reset
        qrows = [
            qrow("2026-10-14T10:00:00+00:00", {"claude-7d": pool(40, reset),
                                               "codex-5h": pool(10, reset),
                                               "codex-7d": pool(2, reset)}),
            qrow("2026-10-14T11:00:00+00:00",
                 {"claude-7d": {"used_pct": 50, "resets_at": "invalid"},
                  "codex-5h": pool(10, reset), "codex-7d": pool(2, reset)}),
            qrow("2026-10-15T12:00:00+00:00",
                 {"codex-5h": pool(10, reset), "codex-7d": pool(2, reset)},
                 reason="launch", run="good", job="round-1"),
            qrow("2026-10-15T12:12:00+00:00",
                 {"codex-5h": pool(20, reset), "codex-7d": pool(3, reset)},
                 reason="job-end", run="good", job="round-1"),
        ]
        runs_rows = [
            {"run": "bad", "agent": "codex", "model": "gpt-6.1-sol", "effort": "xhigh",
             "rounds": [{"n": 1, "model": "gpt-6.1-sol", "effort": "xhigh",
                         "started": "2026-13-01T00:00:00Z", "minutes": 10}],
             "reviews": []},
            self._run(ts("2026-10-15T12:00:00+00:00"), run="good"),
        ]
        sessions = [
            {"event": "delegate", "command": "ask", "agent": "both", "ts": "invalid"},
            {"event": "delegate", "command": "ask", "agent": "codex",
             "ts": "2026-10-20T00:00:00+00:00"},
        ]
        now = ts("2026-10-27T00:00:00+00:00")
        report = monitor.build_report(qrows, runs_rows, sessions, now, catalog=self.catalog)
        self.assertEqual(report["pools"]["claude-7d"]["summary"]["windows"], 1)
        self.assertEqual(report["pools"]["claude-7d"]["windows"][0]["peak"], 40)
        costs = report["costs"]
        self.assertEqual(costs["jobs"], 1)
        self.assertEqual(costs["groups"][0]["n"], 1)
        self.assertEqual(costs["groups"][0]["pools"]["codex-5h"]["median"], 10.0)

    def test_since_keeps_pre_cutoff_jobs_in_overlap_index(self):
        start_a = ts("2026-10-15T11:55:00+00:00")
        start_b = ts("2026-10-15T12:00:00+00:00")
        runs_rows = [
            self._run(start_a, minutes=15, run="a"),
            self._run(start_b, minutes=10, run="b"),
        ]
        qrows = [
            qrow(start_b, {"codex-5h": pool(10, self.reset), "codex-7d": pool(2, self.reset)},
                 reason="launch", run="b", job="round-1"),
            qrow(start_b + timedelta(minutes=12),
                 {"codex-5h": pool(20, self.reset), "codex-7d": pool(3, self.reset)},
                 reason="job-end", run="b", job="round-1"),
        ]
        since = ts("2026-10-15T12:00:00+00:00")
        result = monitor.job_costs(qrows, runs_rows, [], self.catalog, since=since)
        self.assertEqual(result["jobs"], 1)
        self.assertEqual(result["skipped"].get("overlap"), 1)
        self.assertEqual(result["groups"], [])

    def test_tagged_job_end_requires_ts_after_launch(self):
        day1 = ts("2026-10-15T12:00:00+00:00")
        day2 = ts("2026-10-16T12:00:00+00:00")
        runs_rows = [
            self._run(day1, run="common"),
            self._run(day2, run="common"),
        ]
        qrows = [
            qrow(day1, {"codex-5h": pool(10, self.reset), "codex-7d": pool(1, self.reset)},
                 reason="launch", run="common", job="round-1"),
            qrow(day1 + timedelta(minutes=12),
                 {"codex-5h": pool(20, self.reset), "codex-7d": pool(2, self.reset)},
                 reason="job-end", run="common", job="round-1"),
            qrow(day2, {"codex-5h": pool(30, self.reset), "codex-7d": pool(3, self.reset)},
                 reason="launch", run="common", job="round-1"),
            qrow(day2 + timedelta(minutes=12),
                 {"codex-5h": pool(50, self.reset), "codex-7d": pool(5, self.reset)},
                 reason="job-end", run="common", job="round-1"),
        ]
        result = monitor.job_costs(qrows, runs_rows, [], self.catalog)
        self.assertEqual(result["jobs"], 2)
        self.assertEqual(result["groups"][0]["n"], 2)
        self.assertEqual(result["groups"][0]["pools"]["codex-5h"]["median"], 15.0)

    def test_rounds_left_none_when_pool_errors(self):
        group = {"kind": "round", "n": 7, "pools": {
            "codex-5h": {"median": 15.0}, "codex-7d": {"median": 2.0}}}
        live = {"codex-5h": {"left_pct": 100}, "codex-7d": {"error": "provider missing"}}
        self.assertIsNone(monitor.rounds_left(group, live))


class TextFormatTests(unittest.TestCase):
    def test_text_rounds_cursor_peak_to_one_decimal(self):
        start = ts("2026-10-13T07:18:54+00:00")
        end = ts("2026-10-18T01:53:54+00:00")
        report = {
            "log": "/tmp/q.jsonl", "readings": 1,
            "first_ts": start, "last_ts": end,
            "pools": {
                "cursor-auto": {
                    "summary": {
                        "windows": 1, "early": 0, "lower_bound": 1,
                        "mean_peak": 53.7244, "expired_sum": 46.2756,
                        "idle_spans": 0, "idle_hours": 0.0,
                    },
                    "windows": [{
                        "start": start, "end": end, "last_seen": end - timedelta(hours=1.4),
                        "resets_at": end, "hours": 114.6, "peak": 53.7244,
                        "expired": 46.2756, "early": False, "gap_h": 1.4,
                        "lower_bound": True, "idle": False, "merged": 1,
                        "readings": 10, "closed": True,
                    }],
                },
            },
            "costs": {"jobs": 0, "skipped": {}, "groups": []},
        }
        # Only cursor-auto in the filter so the summary table is present.
        text = monitor.render_unused(report, pool_filter=["cursor-auto"])
        self.assertIn("53.7", text)
        self.assertIn("46.3", text)
        self.assertNotIn("53.7244", text)
        self.assertNotIn("46.2756", text)
        jsonable = monitor._jsonable({
            "log": report["log"], "run_log": "/tmp/r.jsonl", "session_log": "/tmp/s.jsonl",
            "now": "2026-10-27T18:40:37+00:00", "since": None, "readings": 1,
            "pools": report["pools"], "costs": report["costs"],
        })
        self.assertEqual(jsonable["pools"]["cursor-auto"]["windows"][0]["peak"], 53.7244)
        self.assertEqual(jsonable["pools"]["cursor-auto"]["summary"]["expired_sum"], 46.2756)


class CliPlumbingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="monitor-cli-")
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)
        self.log = self.tmp / "quota.jsonl"
        self.run_log = self.tmp / "runs.jsonl"
        self.session_log = self.tmp / "sessions.jsonl"
        self.run_log.write_text("")
        self.session_log.write_text("")
        reset = "2026-10-20T22:00:00+00:00"
        rows = [
            qrow("2026-10-14T10:00:00+00:00", {
                "claude-7d": pool(50, reset), "claude-5h": pool(10, "2026-10-14T15:00:00+00:00"),
                "codex-7d": pool(0, "2026-10-14T10:00:00+00:00"),
                "codex-5h": pool(0, "2026-10-14T15:00:00+00:00"),
                "cursor-auto": pool(20, "2026-10-18T01:00:00+00:00"),
                "cursor-premium": pool(30, "2026-10-18T01:00:00+00:00"),
            }),
            qrow("2026-10-21T00:00:00+00:00", {
                "claude-7d": pool(10, "2026-10-27T22:00:00+00:00"),
                "claude-5h": pool(5, "2026-10-21T05:00:00+00:00"),
                "codex-7d": pool(1, "2026-10-28T00:00:00+00:00"),
                "codex-5h": pool(1, "2026-10-21T05:00:00+00:00"),
                "cursor-auto": pool(5, "2026-11-01T00:00:00+00:00"),
                "cursor-premium": pool(5, "2026-11-01T00:00:00+00:00"),
            }),
        ]
        self.log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    def test_unused_json_subprocess(self):
        result = subprocess.run(
            [sys.executable, str(CLI), "quota", "--unused", "--json",
             "--log", str(self.log), "--run-log", str(self.run_log),
             "--session-log", str(self.session_log),
             "--now", "2026-10-27T18:40:37+00:00"],
            capture_output=True, text=True,
            env={**os.environ, "CLAUDE_DELEGATE_QUOTA_SNAPSHOT": "0"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertIn("pools", data)
        self.assertIn("costs", data)
        self.assertEqual(data["costs"]["jobs"], 0)
        self.assertIn("claude-7d", data["pools"])
        self.assertEqual(data["pools"]["claude-7d"]["summary"]["windows"], 1)

    def test_snapshot_job_writes_job(self):
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        export = self.tmp / "export.json"
        now = datetime.now(UTC).replace(microsecond=0)
        export.write_text(json.dumps({"snapshots": [
            {"provider_id": "claude_code", "timestamp": now.isoformat(), "status": "ok",
             "metrics": {"usage_five_hour": {"limit": 100, "remaining": 90},
                         "usage_seven_day": {"limit": 100, "used": 10}},
             "resets": {"usage_five_hour": (now + timedelta(hours=3)).isoformat(),
                        "usage_seven_day": (now + timedelta(days=2)).isoformat()}},
            {"provider_id": "codex", "timestamp": now.isoformat(), "status": "ok",
             "metrics": {"rate_limit_primary": {"limit": 100, "remaining": 90},
                         "rate_limit_secondary": {"used": 10}},
             "resets": {"rate_limit_primary": (now + timedelta(hours=3)).isoformat(),
                        "rate_limit_secondary": (now + timedelta(days=2)).isoformat()}},
            {"provider_id": "cursor", "timestamp": now.isoformat(), "status": "ok",
             "metrics": {"plan_auto_percent_used": {"used": 1},
                         "plan_api_percent_used": {"used": 2},
                         "billing_cycle_progress": {"used": 10}},
             "resets": {"billing_cycle_end": (now + timedelta(days=10)).isoformat()}},
        ]}))
        fake = bin_dir / "openusage"
        fake.write_text(f"#!{sys.executable}\nimport pathlib,sys\n"
                        f"print(pathlib.Path({str(export)!r}).read_text())\n")
        fake.chmod(0o755)
        out_log = self.tmp / "snap.jsonl"
        result = subprocess.run(
            [sys.executable, str(CLI), "quota", "--snapshot", "--reason", "launch",
             "--job", "round-1", "--log", str(out_log)],
            capture_output=True, text=True,
            env={**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
                 "HOME": str(self.tmp)},
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        row = json.loads(out_log.read_text().splitlines()[0])
        self.assertEqual(row["job"], "round-1")

    def test_finalize_spawns_job_end(self):
        root = self.tmp / "run"
        root.mkdir()
        (root / "started_at").write_text("2026-10-15T12:00:00+00:00\n")
        (root / "finished_at").write_text("2026-10-15T12:10:00+00:00\n")
        (root / "exit_code").write_text("0\n")
        (root / "meta.json").write_text(json.dumps({
            "name": "fixture", "agent": "codex", "model": "gpt-6.1-sol", "effort": "xhigh",
            "rounds": 1, "repo": str(self.tmp), "worktree": str(root / "wt"),
            "branch": "delegate/fixture", "base": "abc",
        }))
        (root / "wt").mkdir()
        out = io.StringIO()
        with mock.patch.object(runs.quota, "spawn_snapshot") as snap, \
             mock.patch.object(runs, "packet", return_value=["ok"]), redirect_stdout(out):
            runs.finalize(root)
        snap.assert_called_once_with("job-end", run="fixture", job="round-1")
        self.assertEqual(out.getvalue(), "ok\n")

        review = root / "review-1"
        review.mkdir()
        (review / "started_at").write_text("2026-10-15T13:00:00+00:00\n")
        (review / "finished_at").write_text("2026-10-15T13:05:00+00:00\n")
        (review / "exit_code").write_text("0\n")
        (review / "last-message.md").write_text("Verdict: APPROVE\n")
        (review / "meta.json").write_text(json.dumps({
            "agent": "cursor", "model": "auto", "effort": None, "n": 1, "round": 1,
        }))
        snap.reset_mock()
        with mock.patch.object(runs.quota, "spawn_snapshot") as snap2, \
             mock.patch.object(runs, "packet", return_value=["ok"]), redirect_stdout(io.StringIO()):
            runs.finalize(review)
        snap2.assert_called_once_with("job-end", run="fixture", job="review-1")
        meta = json.loads((review / "meta.json").read_text())
        self.assertEqual(meta["started"], "2026-10-15T13:00:00+00:00")

    def test_env_suppresses_job_end(self):
        with mock.patch.dict(os.environ, {"CLAUDE_DELEGATE_QUOTA_SNAPSHOT": "0"}), \
             mock.patch.object(quota.subprocess, "Popen") as popen:
            quota.spawn_snapshot("job-end", run="r", job="round-1")
            popen.assert_not_called()


class RouteRoundsTests(unittest.TestCase):
    def test_render_accepts_rows_without_rounds_keys(self):
        import route
        task = {"desc": "x", "mode": "write",
                "weights": {"intelligence": 1, "coding": 0, "speed": 0, "price": 0}}
        rows = [{"delegate": {"id": "x", "agent": "codex", "model": "m"},
                 "metrics": {"intelligence": 1, "coding": None, "speed": None, "price": None},
                 "score": 1, "quality": 1, "left": 50, "pace": 1.0, "source": "prior",
                 "excluded": False, "problems": [], "unknown_pools": []}]
        text = route.render("implement", task, rows)
        self.assertIn("ROUNDS", text)
        self.assertIn("rounds left", text.lower())


if __name__ == "__main__":
    unittest.main()

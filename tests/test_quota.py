"""Quota collectors use scratch transcripts and a fake openusage, never live data."""
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
import quota

CLI = SCRIPTS / "delegate"
FAKE = """import json, os, pathlib, sys, time
with pathlib.Path(os.environ['FAKE_QUOTA_CALLS']).open('a') as stream:
    stream.write(json.dumps({'args': sys.argv[1:], 'ts': time.time()}) + '\\n')
print(os.environ.get('FAKE_QUOTA_STDERR', ''), file=sys.stderr, flush=True)
time.sleep(float(os.environ.get('FAKE_QUOTA_SLEEP', '0')))
mode = os.environ.get('FAKE_QUOTA_MODE')
if mode == 'fail':
    sys.exit(1)
if mode == 'garbage':
    print('not json')
else:
    print(pathlib.Path(os.environ['FAKE_QUOTA_EXPORT']).read_text())
"""


def nano_iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + ".001441512Z"


def fixture(now):
    return {"snapshots": [
        {"provider_id": "claude_code", "timestamp": nano_iso(now), "status": "ok",
         "metrics": {"usage_five_hour": {"limit": 100, "remaining": 87.65432111},
                     "usage_seven_day": {"limit": 100, "used": 70}},
         "resets": {"usage_five_hour": (now + timedelta(hours=3)).isoformat(),
                    "usage_seven_day": (now + timedelta(days=2)).isoformat()}},
        {"provider_id": "codex", "timestamp": nano_iso(now - timedelta(seconds=180)),
         "status": "ok",
         "metrics": {"rate_limit_primary": {"limit": 200, "remaining": 170},
                     "rate_limit_secondary": {"used": 25}},
         "resets": {"rate_limit_primary": (now - timedelta(seconds=10)).isoformat()}},
        {"provider_id": "cursor", "timestamp": nano_iso(now), "status": "ok",
         "metrics": {"plan_auto_percent_used": {"used": 22},
                     "plan_api_percent_used": {"used": 34},
                     "billing_cycle_progress": {"used": 50}}, "resets": {}},
    ]}


class QuotaTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="quota-test-")
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.binary = self.bin / "openusage"
        self.binary.write_text(f"#!{sys.executable}\n" + FAKE)
        self.binary.chmod(0o755)
        self.export = self.tmp / "export.json"
        self.calls = self.tmp / "calls.jsonl"
        self.log = self.tmp / "readings/quota.jsonl"
        self.projects = self.tmp / "claude/projects"
        self.projects.mkdir(parents=True)
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.envelope = fixture(self.now)
        self.write_export()
        self.env = {"HOME": str(self.tmp), "PATH": str(self.bin),
                    "XDG_CACHE_HOME": str(self.tmp / "cache"),
                    "XDG_CONFIG_HOME": str(self.tmp / "config"),
                    "CLAUDE_CONFIG_DIR": str(self.projects.parent),
                    "FAKE_QUOTA_EXPORT": str(self.export), "FAKE_QUOTA_CALLS": str(self.calls)}

    def write_export(self):
        self.export.write_text(json.dumps(self.envelope))

    def cli(self, *args, code=0, env=None):
        result = subprocess.run([sys.executable, str(CLI), "quota", *map(str, args)],
                                env={**self.env, **(env or {})}, text=True, capture_output=True)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stdout + result.stderr)
        return result.stdout

    def collect(self, *args, **kwargs):
        return self.cli("--snapshot", "--log", self.log, *args, **kwargs)

    def rows(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def good_row(self, ts, reset=None):
        return {"ts": ts.isoformat(), "pools": {
            "claude-5h": {"used_pct": 10, "resets_at": reset.isoformat() if reset else None},
            "claude-7d": {"used_pct": 20}}}

    def write_rows(self, *rows):
        self.log.parent.mkdir(parents=True, exist_ok=True)
        self.log.write_text("".join(json.dumps(row) + "\n" for row in rows))

    def transcript(self, dt, name="session.jsonl"):
        path = self.projects / "project" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n")
        os.utime(path, (dt.timestamp(), dt.timestamp()))
        return path

    def test_good_row_preserves_provider_times_pool_errors_and_stale_reset(self):
        self.envelope["snapshots"].pop()  # missing Cursor provider
        self.write_export()
        output = self.collect("--reason", "session-start", "--session", "abc_123-x", "--run", "demo")
        row = self.rows()[0]
        self.assertEqual(json.loads(output), row)
        self.assertEqual(set(row), {"ts", "source", "reason", "session", "run", "pools"})
        self.assertEqual((row["source"], row["reason"], row["session"], row["run"]),
                         ("direct", "session-start", "abc_123-x", "demo"))
        ts = quota.parse_ts(row["ts"])
        self.assertEqual(ts.microsecond, 0)
        self.assertEqual(ts.utcoffset(), timedelta(0))
        self.assertLessEqual(ts.timestamp(), json.loads(self.calls.read_text())["ts"])
        self.assertEqual(set(row["pools"]), set(quota.POOLS))
        claude = row["pools"]["claude-5h"]
        self.assertEqual(claude["used_pct"], 12.3457)
        self.assertIsInstance(claude["used_pct"], float)
        self.assertEqual(claude["observed_at"], self.envelope["snapshots"][0]["timestamp"])
        self.assertEqual(claude["resets_at"], self.envelope["snapshots"][0]["resets"]["usage_five_hour"])
        self.assertFalse(claude["stale"])
        self.assertEqual(row["pools"]["claude-7d"]["used_pct"], 70.0)
        self.assertEqual(row["pools"]["codex-5h"]["used_pct"], 15.0)
        self.assertTrue(row["pools"]["codex-5h"]["stale"])
        self.assertTrue(row["pools"]["codex-5h"]["stale_reset"])
        self.assertEqual(row["pools"]["cursor-auto"], {"error": "provider missing from openusage export"})
        self.assertEqual(json.loads(self.calls.read_text())["args"][-2:], ["--source", "direct"])

    def test_missing_observed_at_is_stale(self):
        self.envelope["snapshots"][2].pop("timestamp")
        self.write_export()
        self.collect("--reason", "manual")
        entry = self.rows()[0]["pools"]["cursor-premium"]
        self.assertIsNone(entry["observed_at"])
        self.assertIsNone(entry["resets_at"])
        self.assertTrue(entry["stale"])

    def test_stale_threshold_is_strictly_more_than_120_seconds(self):
        self.envelope["snapshots"][0]["timestamp"] = (self.now - timedelta(seconds=120)).isoformat()
        result = quota._snapshot_pools(self.envelope, self.now)
        self.assertFalse(result["claude-5h"]["stale"])
        self.assertTrue(result["codex-5h"]["stale"])

    def test_invalid_session_is_null_and_rows_append(self):
        self.collect("--reason", "manual", "--session", "../x")
        self.collect("--reason", "backfill")
        self.assertEqual(len(self.rows()), 2)
        self.assertTrue(all(row["session"] is None and row["run"] is None for row in self.rows()))

    def test_snapshot_job_tag_is_written_when_valid(self):
        self.collect("--reason", "launch", "--job", "round-1", "--run", "demo")
        self.assertEqual(self.rows()[0]["job"], "round-1")
        self.collect("--reason", "job-end", "--job", "review-2", "--run", "demo")
        self.assertEqual(self.rows()[1]["job"], "review-2")
        self.collect("--reason", "launch", "--job", "not-a-job", "--run", "demo")
        self.assertNotIn("job", self.rows()[2])

    def test_missing_openusage_writes_error(self):
        self.binary.unlink()
        output = self.collect("--reason", "launch", "--session", "s", "--run", "r", code=1)
        row = self.rows()[0]
        self.assertEqual(json.loads(output), row)
        self.assertEqual(row["error"], "openusage not found")
        self.assertEqual(set(row), {"ts", "source", "reason", "session", "run", "error"})
        self.assertEqual((row["session"], row["run"]), ("s", "r"))

    def test_nonzero_openusage_writes_error_with_bounded_stderr_tail(self):
        stderr = "discarded-prefix" + "x" * 400 + "tail"
        self.collect("--reason", "manual", code=1,
                     env={"FAKE_QUOTA_MODE": "fail", "FAKE_QUOTA_STDERR": stderr})
        error = self.rows()[0]["error"]
        self.assertEqual(error, "openusage exited with status 1: " + stderr[-300:])

    def test_garbage_json_writes_error(self):
        self.collect("--reason", "manual", code=1,
                     env={"FAKE_QUOTA_MODE": "garbage", "FAKE_QUOTA_STDERR": "parse tail"})
        self.assertEqual(self.rows()[0]["error"], "invalid openusage JSON/export: parse tail")

    def test_timeout_writes_error(self):
        output = io.StringIO()
        with mock.patch.dict(os.environ, {**self.env, "FAKE_QUOTA_SLEEP": "0.3",
                                          "FAKE_QUOTA_STDERR": "timeout tail"}, clear=True), \
                redirect_stdout(output):
            code = quota.snapshot(reason="manual", log=self.log, timeout=0.1)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue()), self.rows()[0])
        self.assertEqual(self.rows()[0]["error"], "openusage timed out: timeout tail")

    def test_invalid_export_shape_writes_error(self):
        self.export.write_text("[]")
        self.collect("--reason", "manual", code=1)
        self.assertIn("invalid openusage JSON/export", self.rows()[0]["error"])

    def test_gate_without_last_good_polls_even_without_projects(self):
        self.projects.rmdir()
        self.collect("--if-active")
        self.assertEqual(self.rows()[0]["reason"], "timer")
        self.assertEqual(self.rows()[0]["gate"], {"last_good": None, "last_write": None})

    def test_gate_recent_recursive_subagent_activity_precedes_reset(self):
        good = self.now - timedelta(seconds=100)
        write = self.now - timedelta(seconds=10)
        self.write_rows(self.good_row(good, self.now - timedelta(seconds=20)))
        self.transcript(good - timedelta(seconds=10))
        self.transcript(write, "session/subagents/agent.jsonl")
        self.collect("--if-active")
        row = self.rows()[-1]
        self.assertEqual(row["reason"], "timer")
        self.assertEqual(row["gate"], {"last_good": good.isoformat(), "last_write": write.isoformat()})

    def test_gate_errors_and_malformed_rows_do_not_advance_watermark(self):
        good = self.now - timedelta(seconds=100)
        write = self.now - timedelta(seconds=60)
        self.write_rows(self.good_row(good),
                        {"ts": (self.now - timedelta(seconds=10)).isoformat(), "error": "failed"},
                        {"ts": self.now.isoformat(), "pools": {"claude-5h": {"used_pct": 1}}},
                        {"ts": "bad-date", "pools": {"claude-5h": {"used_pct": 1},
                                                       "claude-7d": {"used_pct": 2}}},
                        [], None)
        with self.log.open("a") as stream:
            stream.write("garbage\n{truncated")
        self.transcript(write)
        self.collect("--if-active")
        row = json.loads(self.log.read_text().splitlines()[-1])
        self.assertEqual(row["reason"], "timer")
        self.assertEqual(row["gate"]["last_good"], good.isoformat())
        self.collect("--if-active")  # the newly appended good row now suppresses polling
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)

    def test_gate_reset_polls_without_transcript_activity(self):
        good = self.now - timedelta(seconds=60)
        self.write_rows(self.good_row(good))
        row = self.rows()[0]
        row["pools"]["codex-5h"] = {"used_pct": 1, "resets_at": (self.now - timedelta(seconds=10)).isoformat()}
        self.write_rows(row)
        self.collect("--if-active")
        self.assertEqual(self.rows()[-1]["reason"], "reset")

    def test_gate_trailing_is_one_reading_after_activity_stops(self):
        good = self.now - timedelta(seconds=900)
        write = self.now - timedelta(seconds=1000)
        self.write_rows(self.good_row(good))
        self.transcript(write)
        self.collect("--if-active")
        self.assertEqual(self.rows()[-1]["reason"], "trailing")
        before = self.log.read_bytes()
        self.assertTrue(self.collect("--if-active").startswith("skip: "))
        self.assertEqual(self.log.read_bytes(), before)
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)

    def test_gate_skip_writes_nothing_and_does_not_fetch(self):
        good = self.now - timedelta(seconds=10)
        self.write_rows(self.good_row(good))
        self.transcript(self.now - timedelta(seconds=20))
        before = self.log.read_bytes()
        self.assertTrue(self.collect("--if-active").startswith("skip: "))
        self.assertEqual(self.log.read_bytes(), before)
        self.assertFalse(self.calls.exists())

    def test_gate_interval_boundaries_and_reset_precedes_trailing(self):
        write = self.now - timedelta(seconds=600)
        good = self.now - timedelta(seconds=500)
        self.transcript(write)
        with mock.patch.dict(os.environ, self.env, clear=True):
            for reset, expected in ((good, "trailing"), (self.now, "reset"),
                                    (self.now + timedelta(seconds=1), "trailing")):
                with self.subTest(reset=reset):
                    self.write_rows(self.good_row(good, reset))
                    self.assertEqual(quota._activity_gate(self.log, self.now)[0], expected)
            self.write_rows(self.good_row(self.now))  # trailing threshold equals the good ts
            self.assertIsNone(quota._activity_gate(self.log, self.now)[0])

    def test_gate_failed_fetch_keeps_activity_visible_for_next_attempt(self):
        good = self.now - timedelta(seconds=100)
        self.write_rows(self.good_row(good))
        self.transcript(self.now - timedelta(seconds=60))
        self.collect("--if-active", code=1, env={"FAKE_QUOTA_MODE": "fail"})
        self.assertEqual(self.rows()[-1]["gate"]["last_good"], good.isoformat())
        self.collect("--if-active")
        self.assertEqual(self.rows()[-1]["gate"]["last_good"], good.isoformat())
        self.assertEqual(len(self.rows()), 3)

    def test_simultaneous_collectors_fetch_exactly_once(self):
        self.transcript(self.now - timedelta(seconds=30))
        cmd = [sys.executable, str(CLI), "quota", "--snapshot", "--if-active", "--log", str(self.log)]
        env = {**self.env, "FAKE_QUOTA_SLEEP": "0.3"}
        children = [subprocess.Popen(cmd, env=env, text=True, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE) for _ in range(2)]
        outputs = []
        for child in children:
            stdout, stderr = child.communicate(timeout=5)
            self.assertEqual(child.returncode, 0, stdout + stderr)
            outputs.append(stdout)
        self.assertEqual(len(self.rows()), 1)
        self.assertNotIn("error", self.rows()[0])
        self.assertEqual(sum(output.startswith("skip: ") for output in outputs), 1)
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)

    def test_log_precedence_and_xdg_and_home_fallbacks(self):
        env_path = self.tmp / "env/quota.jsonl"
        self.collect("--reason", "manual", env={"CLAUDE_DELEGATE_QUOTA_LOG": str(env_path)})
        self.assertTrue(self.log.exists())
        self.assertFalse(env_path.exists())
        self.cli("--snapshot", "--reason", "manual", env={"CLAUDE_DELEGATE_QUOTA_LOG": str(env_path)})
        self.assertTrue(env_path.exists())
        self.cli("--snapshot", "--reason", "manual")
        self.assertTrue((self.tmp / "cache/claude-delegate/quota.jsonl").exists())
        self.cli("--snapshot", "--reason", "manual", env={"XDG_CACHE_HOME": ""})
        self.assertTrue((self.tmp / ".cache/claude-delegate/quota.jsonl").exists())

    def test_reason_required_and_internal_reasons_cannot_be_supplied(self):
        self.collect(code=2)
        for reason in ("reset", "trailing"):
            self.collect("--reason", reason, code=2)
        self.assertFalse(self.log.exists())
        self.assertFalse(self.calls.exists())

    def test_legacy_json_and_text_outputs_and_explicit_sources_are_unchanged(self):
        # No pace clock dependency: percentages, provider metadata and age remain comparable.
        for snap in self.envelope["snapshots"]:
            snap["resets"] = {}
            snap["metrics"].pop("billing_cycle_progress", None)
        self.write_export()
        expected = quota.pools(self.envelope, now=self.now)
        text = quota.render(expected, now=self.now)
        # age_min is stable for these fixtures; seconds are well away from its rounding boundary.
        self.assertEqual(json.loads(self.cli("--json")), expected)
        self.assertEqual(self.cli().rstrip(), text.rstrip())
        self.assertTrue(all(json.loads(line)["args"][-1] == "auto"
                            for line in self.calls.read_text().splitlines()))
        self.collect("--reason", "manual", "--source", "daemon")
        self.assertEqual(self.rows()[0]["source"], "daemon")
        self.assertEqual(json.loads(self.calls.read_text().splitlines()[-1])["args"][-1], "daemon")

    def test_cycle_window_clamps_short_months(self):
        reset = datetime(2026, 3, 31, tzinfo=timezone.utc)
        self.assertEqual(quota.cycle_window_start(reset),
                         datetime(2026, 2, 28, tzinfo=timezone.utc))

    def test_pace_floor_is_five_percent(self):
        # Nearly exhausted window: raw tlf << 0.05, so pace uses the 0.05 floor.
        reading = datetime(2026, 10, 26, 17, 0, tzinfo=timezone.utc)
        entry = {"used_pct": 0.0, "resets_at": (reading + timedelta(minutes=10)).isoformat(),
                 "stale": False}
        # window_h=5 → tlf = 10/300 ≈ 0.0333; floor 0.05 → pace = 1/0.05 = 20
        pace = quota._entry_pace("codex-5h", entry, reading)
        self.assertAlmostEqual(pace, 20.0, places=3)

    def _spare_row(self, ts, **pool_overrides):
        pools = {
            "claude-5h": {"used_pct": 5.0, "resets_at": "2026-10-26T22:00:00+00:00", "stale": False},
            "claude-7d": {"used_pct": 1.0, "resets_at": "2026-10-26T22:00:00+00:00", "stale": False},
            "codex-5h": {"used_pct": 90.0, "resets_at": "2026-10-26T20:55:14+01:00", "stale": False},
            "codex-7d": {"used_pct": 14.0, "resets_at": "2026-11-02T15:55:14+01:00", "stale": False},
            "cursor-auto": {"used_pct": 4.6956, "resets_at": "2026-11-18T01:53:54+00:00", "stale": False},
        }
        for pid, entry in pool_overrides.items():
            pools[pid] = {**pools.get(pid, {}), **entry}
        return {"ts": ts, "source": "direct", "reason": "session-start", "session": "x",
                "run": None, "pools": pools}

    def test_spare_pools_real_2026_10_26_fixture(self):
        # Independent datetime arithmetic (brief): cursor-auto pace 1.3224, codex-7d 0.8745.
        ts = "2026-10-26T17:42:27+00:00"
        hook = datetime(2026, 10, 26, 18, 0, tzinfo=timezone.utc)
        self.write_rows(self._spare_row(ts))
        info = quota.spare_pools(self.log, now=hook)
        self.assertIsNotNone(info)
        by_pid = {pid: (left, pace) for pid, left, pace in info["pools"]}
        self.assertEqual(set(by_pid), {"cursor-auto"})
        left, pace = by_pid["cursor-auto"]
        self.assertAlmostEqual(pace, 1.3224, delta=0.001)
        self.assertAlmostEqual(left / 100, 0.953044, delta=0.001)
        # Confirm the non-qualifying paces stay within the brief's ±0.001.
        seven = quota._entry_pace("codex-7d", self.rows()[0]["pools"]["codex-7d"],
                                  quota.parse_ts(ts))
        five = quota._entry_pace("codex-5h", self.rows()[0]["pools"]["codex-5h"],
                                 quota.parse_ts(ts))
        self.assertAlmostEqual(seven, 0.8745, delta=0.001)
        self.assertAlmostEqual(five, 0.2259, delta=0.001)
        line = quota.format_spare_line(info, now=hook)
        self.assertIn("Cursor Auto", line)
        self.assertNotIn("Codex 7d", line)
        self.assertNotIn("Codex 5h", line)

    def test_spare_line_absent_for_bad_or_stale_readings(self):
        hook = datetime(2026, 10, 26, 18, 0, tzinfo=timezone.utc)
        self.assertIsNone(quota.spare_pools(self.tmp / "missing.jsonl", now=hook))
        self.write_rows({"ts": "2026-10-26T17:00:00+00:00", "error": "failed"})
        self.assertIsNone(quota.spare_pools(self.log, now=hook))
        self.log.write_text("not json\n{}\n", encoding="utf-8")
        self.assertIsNone(quota.spare_pools(self.log, now=hook))
        # Older than 12 h.
        old = (hook - timedelta(hours=13)).isoformat()
        self.write_rows(self._spare_row(old))
        self.assertIsNone(quota.spare_pools(self.log, now=hook))
        # Reset already passed at hook time.
        self.write_rows(self._spare_row(
            "2026-10-26T17:42:27+00:00",
            **{"cursor-auto": {"used_pct": 1.0, "resets_at": "2026-10-26T17:50:00+00:00", "stale": False},
               "codex-7d": {"used_pct": 90.0, "stale": False}}))
        self.assertIsNone(quota.spare_pools(self.log, now=hook))
        # Pace ≤ 1 (codex-7d at the real fixture values).
        self.write_rows(self._spare_row(
            "2026-10-26T17:42:27+00:00",
            **{"cursor-auto": {"used_pct": 50.0, "stale": False}}))  # left 50% / ~0.72 ≈ 0.69
        self.assertIsNone(quota.spare_pools(self.log, now=hook))

    def test_spare_line_shows_codex_5h_beside_7d(self):
        hook = datetime(2026, 10, 26, 18, 0, tzinfo=timezone.utc)
        # Make codex-7d qualify: nearly unused near the end of its window.
        self.write_rows(self._spare_row(
            "2026-10-26T17:42:27+00:00",
            **{"codex-7d": {"used_pct": 1.0, "resets_at": "2026-10-27T18:00:00+00:00", "stale": False},
               "cursor-auto": {"used_pct": 90.0, "stale": False}}))
        info = quota.spare_pools(self.log, now=hook)
        self.assertEqual([p[0] for p in info["pools"]], ["codex-7d"])
        line = quota.format_spare_line(info, now=hook)
        self.assertIn("Codex 7d", line)
        self.assertIn("Codex 5h", line)
        self.assertRegex(line, r"Codex 5h 10% left, resets \d{2}:\d{2}")
        # 5h reset already passed at hook time → "reset since the reading".
        self.write_rows(self._spare_row(
            "2026-10-26T17:42:27+00:00",
            **{"codex-7d": {"used_pct": 1.0, "resets_at": "2026-10-27T18:00:00+00:00", "stale": False},
               "codex-5h": {"used_pct": 90.0, "resets_at": "2026-10-26T17:50:00+00:00", "stale": False},
               "cursor-auto": {"used_pct": 90.0, "stale": False}}))
        line = quota.format_spare_line(quota.spare_pools(self.log, now=hook), now=hook)
        self.assertIn("Codex 5h reset since the reading", line)

    def test_spare_selector_ignores_claude_and_prefers_newer_row(self):
        # Older: Cursor Auto spare (5% used). Newer: Claude errors, Cursor Auto 99% used (pace < 1).
        hook = datetime(2026, 10, 26, 18, 0, tzinfo=timezone.utc)
        older = self._spare_row(
            "2026-10-26T16:00:00+00:00",
            **{"cursor-auto": {"used_pct": 5.0, "resets_at": "2026-11-18T01:53:54+00:00", "stale": False},
               "codex-7d": {"used_pct": 90.0, "stale": False}})
        newer = {
            "ts": "2026-10-26T17:42:27+00:00", "source": "direct", "reason": "launch",
            "pools": {
                "claude-5h": {"error": "metric usage_five_hour missing"},
                "claude-7d": {"error": "metric usage_seven_day missing"},
                "codex-5h": {"used_pct": 90.0, "resets_at": "2026-10-26T20:55:14+01:00", "stale": False},
                "codex-7d": {"used_pct": 90.0, "resets_at": "2026-11-02T15:55:14+01:00", "stale": False},
                "cursor-auto": {"used_pct": 99.0, "resets_at": "2026-11-18T01:53:54+00:00", "stale": False},
            },
        }
        self.write_rows(older, newer)
        # _last_good still picks the older Claude-complete row.
        good, good_ts = quota._last_good(self.log)
        self.assertEqual(good["ts"], older["ts"])
        self.assertEqual(good_ts, quota.parse_ts(older["ts"]))
        info = quota.spare_pools(self.log, now=hook)
        line = quota.format_spare_line(info, now=hook) or ""
        self.assertNotIn("Cursor Auto", line)
        # Newer row won: 99% used → pace < 1, so no spare line at all.
        self.assertIsNone(info)

    def test_format_codex_5h_unknown_and_reset_cases(self):
        now = datetime(2026, 10, 26, 18, 0, tzinfo=timezone.utc)
        self.assertEqual(quota._format_codex_5h({"error": "metric missing"}, now), "Codex 5h unknown")
        self.assertEqual(quota._format_codex_5h(None, now), "Codex 5h unknown")
        self.assertEqual(quota._format_codex_5h({"used_pct": 10.0}, now), "Codex 5h unknown")
        self.assertEqual(
            quota._format_codex_5h({"used_pct": 10.0, "resets_at": "2026-10-26T17:50:00+00:00"}, now),
            "Codex 5h reset since the reading")
        self.assertRegex(
            quota._format_codex_5h({"used_pct": 10.0, "resets_at": "2026-10-26T20:55:14+01:00"}, now),
            r"Codex 5h 90% left, resets \d{2}:\d{2}")
        # Missing key in spare_pools → format still says unknown beside 7d.
        self.write_rows(self._spare_row(
            "2026-10-26T17:42:27+00:00",
            **{"codex-7d": {"used_pct": 1.0, "resets_at": "2026-10-27T18:00:00+00:00", "stale": False},
               "codex-5h": {"error": "metric missing"},
               "cursor-auto": {"used_pct": 90.0, "stale": False}}))
        line = quota.format_spare_line(quota.spare_pools(self.log, now=now), now=now)
        self.assertIn("Codex 5h unknown", line)


if __name__ == "__main__":
    unittest.main()

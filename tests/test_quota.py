"""Quota collectors use scratch transcripts and hybrid fakes, never live data."""
import _env  # noqa: F401  — isolate HOME/XDG/PATH before scripts/hooks
import importlib.machinery
import importlib.util
import io
import json
import math
import os
import time
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/delegate/scripts"
sys.path.insert(0, str(SCRIPTS))
import quota

CLI = SCRIPTS / "delegate"


def guard_stdout(test):
    """Error a test that prints to the suite's stdout.

    quota.snapshot() prints its row. Uncaptured, the row sits in the block
    buffer of a redirected stdout and lands in every verify log after OK.
    """
    stray = io.StringIO()
    patcher = mock.patch("sys.stdout", stray)
    patcher.start()
    test.addCleanup(lambda: test.assertEqual(stray.getvalue(), "", "test wrote to stdout"))
    test.addCleanup(patcher.stop)


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


class DispatchCheckTests(unittest.TestCase):
    def setUp(self):
        guard_stdout(self)
        patcher = mock.patch.dict(os.environ, {"DELEGATE_CATALOG_LOCAL": ""})
        patcher.start()
        self.addCleanup(patcher.stop)

    def entries(self, **five):
        return {"codex-5h": {"left_pct": 10, "age_min": 15, **five},
                "codex-7d": {"left_pct": 50, "age_min": 0},
                "cursor-auto": {"left_pct": 0, "age_min": None}}

    def test_dispatch_reads_codex_once_and_skips_full_fetch(self):
        with mock.patch.object(quota, "_codex_snapshot", return_value={}) as read, \
             mock.patch.object(quota, "fetch") as fetch, \
             mock.patch.object(quota, "pools", return_value=self.entries(age_min=999)):
            result = quota.check_dispatch()
        read.assert_called_once_with(timeout=15)
        fetch.assert_not_called()
        self.assertEqual(set(result["pools"]), set(quota.DISPATCH_POOLS))
        self.assertTrue(all(entry["state"] == "ok" for entry in result["pools"].values()))

    def test_missing_values_stay_unknown_without_refresh(self):
        with mock.patch.object(quota, "_codex_snapshot", return_value={}) as read, \
             mock.patch.object(quota, "pools", return_value=self.entries(age_min=None)):
            result = quota.check_dispatch()
        self.assertEqual(read.call_count, 1)
        self.assertEqual(result["pools"]["codex-5h"]["reason"], "snapshot timestamp missing")

    def test_read_failure_is_unknown_bounded_and_private(self):
        for error in (subprocess.TimeoutExpired("codex", 15),
                      subprocess.CalledProcessError(1, "codex", stderr="secret-account-id\n"),
                      ValueError("secret-credit-id")):
            with self.subTest(error=type(error)), \
                 mock.patch.object(quota, "_codex_snapshot", side_effect=error) as read:
                result = quota.check_dispatch()
            read.assert_called_once_with(timeout=15)
            self.assertTrue(all(entry["state"] == "unknown" for entry in result["pools"].values()))
            self.assertNotIn("secret", json.dumps(result))
            self.assertNotIn("\n", result["error"])

    def test_broken_catalog_raises_before_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "catalog.local.json"
            path.write_text("invalid json")
            with mock.patch.dict(os.environ, {"DELEGATE_CATALOG_LOCAL": str(path)}), \
                 mock.patch.object(quota, "_codex_snapshot") as read:
                with self.assertRaises(Exception):
                    quota.check_dispatch()
            read.assert_not_called()


class QuotaTests(unittest.TestCase):
    def setUp(self):
        guard_stdout(self)
        temp = tempfile.TemporaryDirectory(prefix="quota-test-")
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)
        self.log = self.tmp / "readings/quota.jsonl"
        self.projects = self.tmp / "claude/projects"
        self.projects.mkdir(parents=True)
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.envelope = fixture(self.now)
        self.calls = self.tmp / "calls.jsonl"
        self.fetch_mode = None  # None | "fail" | "timeout"
        self.fetch_sleep = 0.0
        self.env = {"HOME": str(self.tmp),
                    "XDG_CACHE_HOME": str(self.tmp / "cache"),
                    "XDG_CONFIG_HOME": str(self.tmp / "config"),
                    "CLAUDE_CONFIG_DIR": str(self.projects.parent),
                    "DELEGATE_CATALOG_LOCAL": "",
                    "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        env_patch = mock.patch.dict(os.environ, self.env, clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        hybrid = mock.patch.object(quota, "_hybrid_snapshot", side_effect=self._hybrid)
        hybrid.start()
        self.addCleanup(hybrid.stop)

    def _hybrid(self, timeout=90, log=None):
        with self.calls.open("a") as stream:
            stream.write(json.dumps({"timeout": timeout, "ts": time.time()}) + "\n")
        if self.fetch_sleep:
            time.sleep(self.fetch_sleep)
        if self.fetch_mode == "fail":
            raise subprocess.CalledProcessError(1, "quota reader")
        if self.fetch_mode == "timeout":
            raise subprocess.TimeoutExpired("quota reader", timeout)
        if self.fetch_mode == "shape":
            raise ValueError("bad shape")
        return json.loads(json.dumps(self.envelope))

    def collect(self, *args, code=0, fetch_mode=None, fetch_sleep=None):
        """In-process snapshot via argparse-shaped kwargs (or stdout capture)."""
        # Parse a minimal subset of the CLI flags used by these tests.
        kwargs = {"log": self.log}
        argv = list(args)
        if_active = False
        i = 0
        while i < len(argv):
            arg = argv[i]
            if arg == "--if-active":
                if_active = True
                i += 1
            elif arg == "--reason":
                kwargs["reason"] = argv[i + 1]; i += 2
            elif arg == "--session":
                kwargs["session"] = argv[i + 1]; i += 2
            elif arg == "--run":
                kwargs["run"] = argv[i + 1]; i += 2
            elif arg == "--job":
                kwargs["job"] = argv[i + 1]; i += 2
            elif arg == "--log":
                kwargs["log"] = Path(argv[i + 1]); i += 2
            else:
                raise AssertionError(f"unexpected collect arg: {arg}")
        kwargs["if_active"] = if_active
        old_mode, old_sleep = self.fetch_mode, self.fetch_sleep
        if fetch_mode is not None:
            self.fetch_mode = fetch_mode
        if fetch_sleep is not None:
            self.fetch_sleep = fetch_sleep
        output = io.StringIO()
        try:
            with redirect_stdout(output):
                status = quota.snapshot(**kwargs)
        finally:
            self.fetch_mode, self.fetch_sleep = old_mode, old_sleep
        self.assertEqual(status, code, output.getvalue())
        return output.getvalue()

    def cli_subprocess(self, *args, code=0, env=None):
        # Empty PATH: the script is absolute; a regression must not reach live agents.
        empty = self.tmp / "empty-path"
        empty.mkdir(exist_ok=True)
        result = subprocess.run(
            [sys.executable, str(CLI), "quota", *map(str, args)],
            env={**os.environ, "PATH": str(empty), **(env or {})},
            text=True, capture_output=True)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stdout + result.stderr)
        return result

    def rows(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def call_count(self):
        if not self.calls.exists():
            return 0
        return len(self.calls.read_text().splitlines())

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
        output = self.collect("--reason", "session-start", "--session", "abc_123-x", "--run", "demo")
        row = self.rows()[0]
        self.assertEqual(json.loads(output), row)
        self.assertEqual(set(row), {"ts", "source", "reader", "reason", "session", "run", "pools"})
        self.assertEqual((row["source"], row["reason"], row["session"], row["run"]),
                         ("hybrid", "session-start", "abc_123-x", "demo"))
        self.assertEqual(row["reader"], quota.HYBRID_READER)
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
        self.assertEqual(row["pools"]["cursor-auto"], {"error": "provider missing from quota reading"})

    def test_missing_observed_at_is_stale(self):
        self.envelope["snapshots"][2].pop("timestamp")
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
        # Advance past reuse so the second row is a fresh write of the same shape.
        with mock.patch.object(quota, "datetime", wraps=datetime) as clock:
            clock.now.return_value = self.now + timedelta(seconds=20)
            self.collect("--reason", "backfill")
        self.assertEqual(len(self.rows()), 2)
        self.assertTrue(all(row["session"] is None and row["run"] is None for row in self.rows()))

    def test_snapshot_job_tag_is_written_when_valid(self):
        self.collect("--reason", "launch", "--job", "round-1", "--run", "demo")
        self.assertEqual(self.rows()[0]["job"], "round-1")
        with mock.patch.object(quota, "datetime", wraps=datetime) as clock:
            clock.now.return_value = self.now + timedelta(seconds=20)
            self.collect("--reason", "job-end", "--job", "review-2", "--run", "demo")
            clock.now.return_value = self.now + timedelta(seconds=40)
            self.collect("--reason", "launch", "--job", "not-a-job", "--run", "demo")
        self.assertEqual(self.rows()[1]["job"], "review-2")
        self.assertNotIn("job", self.rows()[2])

    def test_total_reader_failure_writes_error(self):
        output = self.collect("--reason", "launch", "--session", "s", "--run", "r",
                              code=1, fetch_mode="fail")
        row = self.rows()[0]
        self.assertEqual(json.loads(output), row)
        self.assertEqual(row["error"], "quota reader exited with status 1")
        self.assertEqual(set(row), {"ts", "source", "reader", "reason", "session", "run", "error"})
        self.assertEqual((row["session"], row["run"]), ("s", "r"))

    def test_fetch_error_never_includes_stderr(self):
        exc = subprocess.CalledProcessError(1, "codex", stderr="secret-account-id\n")
        self.assertEqual(quota._fetch_error(exc, "codex app-server"),
                         "codex app-server exited with status 1")
        self.assertEqual(quota._fetch_error(exc), "quota reader exited with status 1")
        self.assertNotIn("secret", quota._fetch_error(exc))

    def test_timeout_writes_error(self):
        output = io.StringIO()
        self.fetch_mode = "timeout"
        with redirect_stdout(output):
            code = quota.snapshot(reason="manual", log=self.log, timeout=0.1)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue()), self.rows()[0])
        self.assertEqual(self.rows()[0]["error"], "quota reader timed out")

    def test_invalid_export_shape_writes_error(self):
        self.collect("--reason", "manual", code=1, fetch_mode="shape")
        self.assertEqual(self.rows()[0]["error"], "invalid quota reader JSON/export")

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
        before = self.log.read_bytes()
        # A wrongly allowed poll would reuse and leave call_count at 1; require a skip.
        self.assertTrue(self.collect("--if-active").startswith("skip: "))
        self.assertEqual(self.log.read_bytes(), before)
        self.assertEqual(self.call_count(), 1)

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
        self.assertEqual(self.call_count(), 1)

    def test_gate_skip_writes_nothing_and_does_not_fetch(self):
        good = self.now - timedelta(seconds=10)
        self.write_rows(self.good_row(good))
        self.transcript(self.now - timedelta(seconds=20))
        before = self.log.read_bytes()
        self.assertTrue(self.collect("--if-active").startswith("skip: "))
        self.assertEqual(self.log.read_bytes(), before)
        self.assertEqual(self.call_count(), 0)

    def test_gate_interval_boundaries_and_reset_precedes_trailing(self):
        write = self.now - timedelta(seconds=600)
        good = self.now - timedelta(seconds=500)
        self.transcript(write)
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
        self.collect("--if-active", code=1, fetch_mode="fail")
        self.assertEqual(self.rows()[-1]["gate"]["last_good"], good.isoformat())
        self.collect("--if-active")
        self.assertEqual(self.rows()[-1]["gate"]["last_good"], good.isoformat())
        self.assertEqual(len(self.rows()), 3)

    def test_simultaneous_collectors_fetch_exactly_once(self):
        self.transcript(self.now - timedelta(seconds=30))
        self.fetch_sleep = 0.3
        results = []

        def one():
            # Call record directly: redirect_stdout is not thread-safe.
            row, skip = quota.record(if_active=True, log=self.log)
            results.append((row, skip))

        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(lambda _: one(), range(2)))
        self.assertEqual(len(self.rows()), 1)
        self.assertNotIn("error", self.rows()[0])
        self.assertEqual(sum(skip is not None for _, skip in results), 1)
        self.assertEqual(self.call_count(), 1)

    def test_log_precedence_and_xdg_and_home_fallbacks(self):
        def bare_snapshot():
            # No log argument, so the env, XDG and home order picks the file.
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(quota.snapshot(reason="manual"), 0)
            self.assertEqual(json.loads(output.getvalue())["reason"], "manual")

        env_path = self.tmp / "env/quota.jsonl"
        with mock.patch.dict(os.environ, {"CLAUDE_DELEGATE_QUOTA_LOG": str(env_path)}):
            self.collect("--reason", "manual")
            self.assertTrue(self.log.exists())
            self.assertFalse(env_path.exists())
            bare_snapshot()
            self.assertTrue(env_path.exists())
        with mock.patch.dict(os.environ, {"CLAUDE_DELEGATE_QUOTA_LOG": ""}):
            # Fresh clock so reuse does not skip the XDG/home writes.
            with mock.patch.object(quota, "datetime", wraps=datetime) as clock:
                clock.now.return_value = self.now + timedelta(seconds=20)
                bare_snapshot()
                self.assertTrue((self.tmp / "cache/claude-delegate/quota.jsonl").exists())
                clock.now.return_value = self.now + timedelta(seconds=40)
                with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": ""}):
                    bare_snapshot()
                self.assertTrue((self.tmp / ".cache/claude-delegate/quota.jsonl").exists())

    def test_reason_required_and_internal_reasons_cannot_be_supplied(self):
        self.cli_subprocess("--snapshot", "--log", self.log, code=2)
        for reason in ("reset", "trailing"):
            self.cli_subprocess("--snapshot", "--reason", reason, "--log", self.log, code=2)
        self.assertFalse(self.log.exists())
        self.assertEqual(self.call_count(), 0)

    def test_source_flag_is_rejected(self):
        result = self.cli_subprocess("--source", "direct", "--json", code=2)
        self.assertIn("unrecognized arguments", result.stderr)
        result = self.cli_subprocess("--snapshot", "--reason", "manual", "--source", "direct",
                                     "--log", self.log, code=2)
        self.assertIn("unrecognized arguments", result.stderr)
        self.assertFalse(self.log.exists())

    def test_json_and_text_outputs(self):
        # No pace clock dependency: percentages, provider metadata and age remain comparable.
        for snap in self.envelope["snapshots"]:
            snap["resets"] = {}
            snap["metrics"].pop("billing_cycle_progress", None)
        expected = quota.pools(self.envelope, now=self.now)
        text = quota.render(expected, now=self.now)
        # age_min is stable for these fixtures; seconds are well away from its rounding boundary.
        loader = importlib.machinery.SourceFileLoader("quota_delegate_cli", str(CLI))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        cli = importlib.util.module_from_spec(spec)
        loader.exec_module(cli)

        def run_cli(*args):
            out = io.StringIO()
            with mock.patch.object(sys, "argv", [str(CLI), "quota", *args]), \
                    redirect_stdout(out):
                cli.main()
            return out.getvalue()

        self.assertEqual(json.loads(run_cli("--json")), expected)
        self.assertEqual(run_cli().rstrip(), text.rstrip())

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


class _ClaudeFakeRestorer:
    def __init__(self, tests, previous):
        self.tests, self.previous = tests, previous

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        path = self.tests.claude_config / "fake-claude.json"
        path.write_text(json.dumps(self.previous))


def _pty_available():
    import pty
    try:
        master, slave = pty.openpty()
    except OSError:
        return False
    for fd in (master, slave):
        try:
            os.close(fd)
        except OSError:
            pass
    return True


PTY_OK = _pty_available()


class HybridTests(unittest.TestCase):
    def setUp(self):
        guard_stdout(self)
        temporary = tempfile.TemporaryDirectory(prefix="hybrid-quota-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.cache = self.tmp / "cache"
        self.log = self.tmp / "quota.jsonl"
        self.codex_calls = self.tmp / "codex.calls"
        self.claude_calls = self.tmp / "claude.calls"
        self.cursor_env_log = self.tmp / "cursor-env.jsonl"
        self.cursor_tui_log = self.tmp / "cursor-tui.log"
        self.claude_config = self.tmp / "claude-config"
        self.claude_config.mkdir()
        self.catalog_local = self.tmp / "catalog.local.json"
        self.catalog_local.write_text(json.dumps({"cursor_usage_reader": True}))
        fakes = Path(__file__).parent / "fake_agents"
        for name in ("codex", "claude", "cursor-agent"):
            path = self.bin / name
            path.write_text(f"#!{sys.executable}\n" + (fakes / name).read_text().split("\n", 1)[1])
            path.chmod(0o755)
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.env = {"HOME": str(self.home), "PATH": str(self.bin),
                    "XDG_CACHE_HOME": str(self.cache), "XDG_CONFIG_HOME": str(self.tmp / "config"),
                    "XDG_DATA_HOME": "hidden-data", "XDG_ANYTHING": "hidden-anything",
                    "CODEX_HOME": "hidden-codex", "DELEGATE_CATALOG_LOCAL": str(self.catalog_local),
                    "CLAUDE_CONFIG_DIR": str(self.claude_config),
                    "FAKE_CODEX_LOG": str(self.codex_calls),
                    "FAKE_CURSOR_TUI": "normal", "FAKE_CURSOR_TUI_ENV": str(self.cursor_env_log),
                    "FAKE_CURSOR_TUI_LOG": str(self.cursor_tui_log)}
        patcher = mock.patch.dict(os.environ, self.env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.set_claude_fake(mode="ok", log=str(self.claude_calls))
        # CursorUsageTests skip without /dev/ptmx; HybridTests still need a cursor reading
        # so reuse/partial-failure coverage stays meaningful in pty-less sandboxes.
        if not PTY_OK:
            cursor = mock.patch.object(quota, "_cursor_call", side_effect=self._stub_cursor_call)
            cursor.start()
            self.addCleanup(cursor.stop)

    def _stub_cursor_call(self, timeout, workspace):
        path = os.environ.get("FAKE_CURSOR_TUI_ENV")
        if path:
            with Path(path).open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"argv": ["--mode", "ask"], "cwd": str(workspace)}) + "\n")
        mode = os.environ.get("FAKE_CURSOR_TUI", "normal")
        if mode == "no_meters":
            raise quota._ReaderError("cursor /usage: no meters on screen")
        now = datetime.now(timezone.utc)
        return {"provider_id": "cursor", "timestamp": now.isoformat(), "status": "ok",
                "whole_percent": True,
                "metrics": {"plan_auto_percent_used": {"used": 22},
                            "plan_api_percent_used": {"used": 34}},
                "resets": {"billing_cycle_end": (now + timedelta(days=10)).isoformat()}}

    def clear_cursor_state(self):
        path = quota._cursor_state_path(self.log)
        path.unlink(missing_ok=True)
        lock = Path(str(path) + ".lock")
        lock.unlink(missing_ok=True)

    def cursor_call_count(self):
        if not self.cursor_env_log.exists():
            return 0
        return len(self.cursor_env_log.read_text().splitlines())

    def snap_row(self, **kwargs):
        """In-process snapshot so HybridTests cursor stubs apply (subprocess cannot)."""
        output = io.StringIO()
        with redirect_stdout(output):
            code = quota.snapshot(log=self.log, **kwargs)
        text = output.getvalue()
        if text.startswith("skip: "):
            return code, None, text
        return code, json.loads(text), text

    def set_claude_fake(self, **kwargs):
        path = self.claude_config / "fake-claude.json"
        data = {}
        if path.exists():
            try:
                data = json.loads(path.read_text())
            except ValueError:
                data = {}
        data.update(kwargs)
        path.write_text(json.dumps(data))

    def claude_fake(self, **kwargs):
        previous = {}
        path = self.claude_config / "fake-claude.json"
        if path.exists():
            previous = json.loads(path.read_text())
        self.set_claude_fake(**kwargs)
        return _ClaudeFakeRestorer(self, previous)

    def claude_call_count(self):
        if not self.claude_calls.exists():
            return 0
        return len(self.claude_calls.read_text().splitlines())

    def clear_claude_state(self):
        path = quota._claude_state_path(self.log)
        path.unlink(missing_ok=True)
        lock = Path(str(path) + ".lock")
        if lock.is_dir():
            lock.rmdir()
        else:
            lock.unlink(missing_ok=True)

    def rows(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def raw_limits(self, primary=None, secondary=None):
        return {"accountId": "secret-account-id", "rateLimits": {
            "primary": primary or {"usedPercent": 32, "windowDurationMins": 300, "resetsAt": 1794020641},
            "secondary": secondary or {"usedPercent": 67, "windowDurationMins": 10080, "resetsAt": 1794386768},
            "credits": {"id": "secret-credit-id"}}}

    def use_limits(self, reply):
        path = self.tmp / "codex.json"
        path.write_text(json.dumps(reply))
        os.environ["FAKE_CODEX_FILE"] = str(path)

    def record_at(self, seconds, **kwargs):
        with mock.patch.object(quota, "datetime", wraps=datetime) as clock:
            clock.now.return_value = self.now + timedelta(seconds=seconds)
            return quota.record(log=self.log, **kwargs)[0]

    def test_rate_limited_claude_names_the_cause_without_the_raw_body(self):
        now = datetime.now(timezone.utc)
        snap = {"provider_id": "claude_code", "metrics": {}, "diagnostics": {
            "usage_api_error": 'oauth: API returned 429: {"body": "upstream-private"}'}}
        error = quota.pools({"snapshots": [snap]}, now=now)["claude-5h"]["error"]
        self.assertEqual(error, "metric usage_five_hour missing: usage API rate limited (429)")
        for other in ({"usage_api_error": "oauth: API returned 500"}, {}, [], None):
            snap["diagnostics"] = other
            self.assertEqual(quota.pools({"snapshots": [snap]}, now=now)["claude-5h"]["error"],
                             "metric usage_five_hour missing")

    def test_cleanup_failure_keeps_a_good_codex_reading(self):
        real_wait, stuck_procs = subprocess.Popen.wait, []

        def stuck(proc, timeout=None):
            if timeout == 0.2:  # the wait after the kill: a child the kernel has not released yet
                stuck_procs.append(proc)
                raise subprocess.TimeoutExpired("codex", timeout)
            return real_wait(proc, timeout=timeout)

        with mock.patch.object(quota.os, "killpg", side_effect=PermissionError), \
                mock.patch.object(subprocess.Popen, "wait", stuck):
            snap = quota._codex_snapshot(timeout=5)
        self.assertIn("rate_limit_primary", snap["metrics"])
        self.assertTrue(stuck_procs[0].stdout.closed)
        real_wait(stuck_procs[0], timeout=5)

    def test_windows_match_duration_and_private_fields_never_escape(self):
        reply = self.raw_limits()
        reply["rateLimits"]["primary"], reply["rateLimits"]["secondary"] = (
            reply["rateLimits"]["secondary"], reply["rateLimits"]["primary"])
        self.use_limits(reply)
        result = quota.pools(quota.fetch(log=self.log))
        self.assertEqual(result["codex-5h"]["left_pct"], 68)
        self.assertEqual(result["codex-7d"]["left_pct"], 33)
        self.assertEqual(result["codex-5h"]["resets_at"], "2026-11-07T03:04:01+00:00")
        # Same state folder as fetch: still inside the 3-minute Claude throttle.
        row, _ = quota.record(log=self.log)
        self.assertEqual(row["source"], "hybrid")
        self.assertEqual(row["reader"], quota.HYBRID_READER)
        self.assertEqual(row["pools"]["codex-5h"]["used_pct"], 32)
        self.assertNotIn("secret", json.dumps(row))
        self.assertNotIn("accountId", json.dumps(row))
        self.assertEqual(row["pools"]["claude-5h"]["used_pct"], 12.3457)
        self.assertEqual(row["pools"]["claude-5h"].get("carried"), "throttle")
        self.assertEqual(self.claude_call_count(), 1)

    def test_missing_null_and_invalid_windows_are_safe(self):
        for mode in ("missing", "null", "nonnumeric", "null-reset", "bad-result"):
            with self.subTest(mode=mode):
                reply = self.raw_limits()
                if mode == "missing":
                    del reply["rateLimits"]["primary"]
                elif mode == "null":
                    reply["rateLimits"]["primary"] = None
                elif mode == "nonnumeric":
                    reply["rateLimits"]["primary"]["usedPercent"] = "secret-credit-id"
                elif mode == "null-reset":
                    reply["rateLimits"]["primary"]["resetsAt"] = None
                else:
                    reply["rateLimits"] = "secret-account-id"
                self.use_limits(reply)
                result = quota.pools(quota.fetch())
                row, _ = quota.record(log=self.log, reason="manual")
                self.assertNotIn("secret", json.dumps(row))
                if mode == "null-reset":
                    self.assertEqual(result["codex-5h"]["left_pct"], 68)
                    self.assertIsNone(result["codex-5h"]["resets_at"])
                else:
                    self.assertIn("error", result["codex-5h"])
                if mode in ("missing", "null"):
                    self.assertIn("rate_limit_primary missing", result["codex-5h"]["error"])
                    self.assertEqual(result["codex-7d"]["left_pct"], 33)

    def test_each_child_failure_is_bounded_and_kills_descendants(self):
        for mode in ("garbage", "nonobject", "rpc-error", "eof", "eof-init", "silent",
                     "partial", "grandchild", "exit-grandchild", "chatty", "bad-result"):
            with self.subTest(mode=mode):
                pidfile = self.tmp / f"pids-{mode}"
                started = time.monotonic()
                with mock.patch.dict(os.environ, {"FAKE_CODEX_MODE": mode, "FAKE_CODEX_PIDS": str(pidfile)}):
                    try:
                        result = quota._codex_snapshot(timeout=1)
                    except Exception as exc:
                        self.assertNotIn(mode, ("garbage", "nonobject", "chatty"))
                        self.assertNotIn("secret", quota._fetch_error(exc, "codex app-server"))
                    else:
                        self.assertIn(mode, ("garbage", "nonobject", "chatty"))
                        self.assertIn("rate_limit_primary", result["metrics"])
                self.assertLess(time.monotonic() - started, 3)
                for pid in pidfile.read_text().splitlines():
                    status = Path("/proc") / pid / "stat"
                    # An orphan's zombie may await init's reaper; it cannot keep a pipe/process alive.
                    deadline = time.monotonic() + 0.5
                    while status.exists() and status.read_text().split()[2] != "Z" and time.monotonic() < deadline:
                        time.sleep(0.01)
                    if status.exists():
                        self.assertEqual(status.read_text().split()[2], "Z", f"surviving pid {pid}")

    def test_partial_failures_and_all_three_fail_through_all_entry_points(self):
        cases = (
            (True, False, False),
            (False, True, False),
            (False, False, True),
            (True, True, False),
            (True, False, True),
            (False, True, True),
            (True, True, True),
        )
        for codex_fails, claude_fails, cursor_fails in cases:
            with self.subTest(codex=codex_fails, claude=claude_fails, cursor=cursor_fails):
                self.log.unlink(missing_ok=True)
                self.clear_claude_state()
                self.clear_cursor_state()
                self.claude_calls.unlink(missing_ok=True)
                self.cursor_env_log.unlink(missing_ok=True)
                env = {
                    "FAKE_CODEX_MODE": "rpc-error" if codex_fails else "ok",
                    "FAKE_CURSOR_TUI": "no_meters" if cursor_fails else "normal",
                }
                with mock.patch.dict(os.environ, env), \
                        self.claude_fake(mode="fail" if claude_fails else "ok"):
                    if codex_fails and claude_fails and cursor_fails:
                        with self.assertRaises(ValueError) as failure:
                            quota.pools(quota.fetch(log=self.log))
                        self.assertNotIn("secret", str(failure.exception))
                    else:
                        entries = quota.pools(quota.fetch(log=self.log))
                        self.assertEqual("error" in entries["codex-5h"], codex_fails)
                        self.assertEqual("error" in entries["claude-5h"], claude_fails)
                        self.assertEqual("error" in entries["cursor-auto"], cursor_fails)
                        if not claude_fails:
                            self.assertIn("left_pct", entries["claude-5h"])
                        if cursor_fails and not claude_fails:
                            self.assertIn("cursor /usage", entries["cursor-auto"]["error"])
                            self.assertNotIn("error", entries["claude-5h"])
                self.clear_claude_state()
                self.clear_cursor_state()
                with mock.patch.dict(os.environ, env), \
                        self.claude_fake(mode="fail" if claude_fails else "ok"):
                    row, _ = quota.record(log=self.log)
                self.assertEqual("error" in row, codex_fails and claude_fails and cursor_fails)
                self.assertNotIn("secret", json.dumps(row))
                self.clear_claude_state()
                self.clear_cursor_state()
                with mock.patch.dict(os.environ, env), \
                        self.claude_fake(mode="fail" if claude_fails else "ok"), \
                        redirect_stdout(io.StringIO()):
                    code = quota.snapshot(log=self.log)
                self.assertEqual(code, int(codex_fails and claude_fails and cursor_fails))

    def test_partial_failure_does_not_suppress_next_attempt(self):
        # Codex always polls; Cursor's 30-minute cadence carries after a failed read.
        self.log.unlink(missing_ok=True)
        self.codex_calls.unlink(missing_ok=True)
        self.claude_calls.unlink(missing_ok=True)
        self.cursor_env_log.unlink(missing_ok=True)
        self.clear_claude_state()
        self.clear_cursor_state()
        with mock.patch.dict(os.environ, {"FAKE_CODEX_MODE": "rpc-error"}):
            quota.record(log=self.log)
            row = self.record_at(200)
        self.assertNotIn("reused_from", row)
        self.assertEqual(self.codex_calls.read_text().splitlines(), ["read", "read"])
        self.assertEqual(self.claude_call_count(), 2)
        self.assertEqual(self.cursor_call_count(), 1)  # second hybrid row carries Cursor

    def test_failed_cursor_does_not_discard_codex_or_claude(self):
        with mock.patch.dict(os.environ, {"FAKE_CURSOR_TUI": "no_meters"}):
            row, _ = quota.record(log=self.log)
        self.assertEqual(row["pools"]["codex-5h"]["used_pct"], 20)
        self.assertEqual(row["pools"]["claude-5h"]["used_pct"], 12.3457)
        self.assertIn("cursor /usage", row["pools"]["cursor-auto"]["error"])




    def test_cli_defaults_reject_source(self):
        default_log = self.cache / "claude-delegate/quota.jsonl"
        entries = quota.pools(quota.fetch(log=default_log))
        self.assertNotIn("secret", json.dumps(entries))
        code, row, raw = self.snap_row(reason="manual")
        self.assertEqual(code, 0)
        self.assertNotIn("secret", raw)
        self.assertEqual(self.codex_calls.read_text().splitlines(), ["read", "read"])
        self.assertEqual(self.cursor_call_count(), 2)
        # fetch on the default log folder; snapshot on self.log — two Claude state files.
        self.assertEqual(self.claude_call_count(), 2)
        self.assertEqual(row["source"], "hybrid")
        self.assertEqual(row["reader"], quota.HYBRID_READER)
        result = subprocess.run([sys.executable, str(CLI), "quota", "--source", "direct"],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 2)
        self.assertIn("unrecognized arguments", result.stderr)
        # Exercise route's parser without any ratings/network lookup.
        import runpy
        delegate = runpy.run_path(str(CLI))
        with mock.patch.object(sys, "argv", [str(CLI), "route", "implement", "--json"]), \
             mock.patch("quota.fetch", return_value={}) as fetch, \
             mock.patch("ratings.load", return_value={"models": {}}), \
             mock.patch("monitor.load_cost_groups", return_value={}), redirect_stdout(io.StringIO()):
            delegate["main"]()
        fetch.assert_called_once_with()
        with mock.patch.object(sys, "argv",
                               [str(CLI), "route", "implement", "--source", "direct", "--json"]), \
             mock.patch("quota.fetch", return_value={}), \
             mock.patch("ratings.load", return_value={"models": {}}), \
             mock.patch("monitor.load_cost_groups", return_value={}), \
             redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as exit_info:
            delegate["main"]()
        self.assertEqual(exit_info.exception.code, 2)

    def test_reuse_boundaries_chain_and_observed_times(self):
        with mock.patch.object(quota, "_hybrid_snapshot", return_value=fixture(self.now)) as read:
            first = self.record_at(0, reason="session-end", session="A")
            second = self.record_at(10, reason="session-start", session="B")
            self.assertEqual(second["reused_from"], first["ts"])
            boundary = self.record_at(15, reason="manual")
            self.assertEqual(boundary["pools"], first["pools"])
            self.assertEqual(boundary["reader"], first["reader"])
            self.assertEqual(boundary["reused_from"], first["ts"])
            self.record_at(20, reason="manual")
            self.assertEqual(read.call_count, 2)  # 0/10/20 does not extend the reading's age
        self.log.unlink()
        with mock.patch.object(quota, "_hybrid_snapshot", return_value=fixture(self.now)) as read:
            self.record_at(0)
            row = self.record_at(16)
            self.assertNotIn("reused_from", row)
            self.assertEqual(read.call_count, 2)

    def test_reuse_age_is_not_rounded_down_with_the_row_timestamp(self):
        with mock.patch.object(quota, "_hybrid_snapshot", return_value=fixture(self.now)) as read:
            self.record_at(0)
            row = self.record_at(15.1)
        self.assertNotIn("reused_from", row)
        self.assertEqual(read.call_count, 2)

    def test_reuse_keeps_its_own_gate(self):
        with mock.patch.object(quota, "_hybrid_snapshot", return_value=fixture(self.now)) as read:
            first = self.record_at(0, reason="manual")
            gate = {"last_good": first["ts"], "last_write": "new-write"}
            with mock.patch.object(quota, "_activity_gate", return_value=("timer", gate, None)):
                row = self.record_at(1, if_active=True)
        self.assertEqual(read.call_count, 1)
        self.assertEqual(row["gate"], gate)
        self.assertEqual(row["reason"], "timer")
        self.assertEqual(row["reused_from"], first["ts"])

    def test_nonreusable_readings_retry(self):
        base = {"ts": self.now.isoformat(), "source": "hybrid", "reader": "reader",
                "pools": {pid: {"used_pct": 1} for pid in quota.POOLS}}
        for alteration in ({"ts": (self.now + timedelta(seconds=1)).isoformat()},
                           {"error": "failed"}, {"source": "direct"},
                           {"pools": {"claude-5h": {"used_pct": 1}}}):
            with self.subTest(alteration=alteration):
                self.log.write_text(json.dumps({**base, **alteration}) + "\n")
                with mock.patch.object(quota, "_hybrid_snapshot", return_value=fixture(self.now)) as read:
                    self.record_at(0)
                read.assert_called_once_with(90, log=self.log)

    def test_end_pairs_poll_even_through_reuse(self):
        for end, key in (("job-end", "run"), ("session-end", "session")):
            for through_reuse in (False, True):
                with self.subTest(end=end, through_reuse=through_reuse):
                    self.log.unlink(missing_ok=True)
                    with mock.patch.object(quota, "_hybrid_snapshot", return_value=fixture(self.now)) as read:
                        self.record_at(0, reason="manual", **{key: "A"})
                        if through_reuse:
                            shared = self.record_at(1, reason="launch" if key == "run" else "session-start", **{key: "B"})
                            self.assertIn("reused_from", shared)
                        row = self.record_at(5, reason=end, **{key: "B" if through_reuse else "A"})
                    self.assertNotIn("reused_from", row)
                    self.assertEqual(read.call_count, 2)
                    self.assertEqual(row[key], "B" if through_reuse else "A")

    def test_tail_only_and_metadata_preserved(self):
        with mock.patch.object(quota, "_hybrid_snapshot", return_value=fixture(self.now)) as read:
            first = self.record_at(0, reason="manual", session="A", run="a", job="round-1")
            self.log.write_text("x" * (quota.REUSE_TAIL_BYTES * 2) + "\n" + self.log.read_text())
            second = self.record_at(1, reason="launch", session="B", run="b", job="review-2")
        self.assertEqual(read.call_count, 1)
        self.assertEqual((second["reason"], second["session"], second["run"], second["job"]),
                         ("launch", "B", "b", "review-2"))
        self.assertEqual(second["pools"], first["pools"])

    @unittest.skipUnless(PTY_OK, "no /dev/ptmx in this environment")
    def test_concurrent_collectors_each_write_but_poll_once(self):
        command = [sys.executable, str(CLI), "quota", "--snapshot", "--reason", "manual",
                   "--log", str(self.log)]
        with mock.patch.dict(os.environ, {"FAKE_CODEX_SLEEP": "0.2"}):
            children = [subprocess.Popen(command, text=True, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE)
                        for _ in range(4)]
            for child in children:
                stdout, stderr = child.communicate(timeout=30)
                self.assertEqual(child.returncode, 0, stdout + stderr)
        self.assertEqual(len(self.rows()), 4)
        self.assertEqual(sum("reused_from" in row for row in self.rows()), 3)
        self.assertEqual(self.cursor_call_count(), 1)
        self.assertEqual(self.codex_calls.read_text().splitlines(), ["read"])
        self.assertEqual(self.claude_call_count(), 1)
        self.assertTrue(all(row["pools"] == self.rows()[0]["pools"] for row in self.rows()))

    def test_claude_throttle_backoff_and_carry_reasons(self):
        first = self.record_at(0)
        self.assertNotIn("carried", first["pools"]["claude-5h"])
        self.assertEqual(self.claude_call_count(), 1)
        throttled = self.record_at(60)
        self.assertEqual(throttled["pools"]["claude-5h"]["carried"], "throttle")
        self.assertEqual(throttled["pools"]["claude-5h"]["used_pct"], first["pools"]["claude-5h"]["used_pct"])
        self.assertEqual(throttled["pools"]["claude-5h"]["observed_at"],
                         first["pools"]["claude-5h"]["observed_at"])
        self.assertEqual(self.claude_call_count(), 1)
        with self.claude_fake(mode="fail"):
            failed = self.record_at(200)
        self.assertEqual(failed["pools"]["claude-5h"]["carried"], "failed")
        self.assertEqual(self.claude_call_count(), 2)
        # 3-minute floor still holds after a failure (effective wait is max(3m, backoff)).
        with self.claude_fake(mode="ok"):
            backed = self.record_at(290)
        self.assertEqual(backed["pools"]["claude-5h"]["carried"], "backoff")
        self.assertEqual(self.claude_call_count(), 2)
        with self.claude_fake(mode="ok"):
            recovered = self.record_at(381)
        self.assertNotIn("carried", recovered["pools"]["claude-5h"])
        self.assertEqual(self.claude_call_count(), 3)
        good = json.loads(quota._claude_state_path(self.log).read_text())["last_good"]
        self.claude_calls.write_text("")
        offset = 20_000
        for index in range(len(quota.CLAUDE_BACKOFF_S)):
            wait = quota._claude_wait_s(index)
            quota._claude_state_path(self.log).write_text(json.dumps({
                "last_attempt_at": (self.now + timedelta(seconds=offset - wait - 1)).isoformat(),
                "failures": index, "last_good": good}))
            with self.claude_fake(mode="fail"):
                row = self.record_at(offset)
            self.assertEqual(row["pools"]["claude-5h"]["carried"], "failed")
            state = json.loads(quota._claude_state_path(self.log).read_text())
            self.assertEqual(state["failures"], index + 1)
            offset += wait + 5
        quota._claude_state_path(self.log).write_text(json.dumps({
            "last_attempt_at": (self.now + timedelta(seconds=offset - 1801)).isoformat(),
            "failures": len(quota.CLAUDE_BACKOFF_S), "last_good": good}))
        with self.claude_fake(mode="fail"):
            row = self.record_at(offset)
        self.assertEqual(row["pools"]["claude-5h"]["carried"], "failed")
        state = json.loads(quota._claude_state_path(self.log).read_text())
        self.assertEqual(state["failures"], len(quota.CLAUDE_BACKOFF_S) + 1)

    def test_claude_floor_holds_after_failure(self):
        good = {"timestamp": self.now.isoformat(),
                "metrics": {"usage_five_hour": {"limit": 100, "used": 5, "remaining": 95},
                            "usage_seven_day": {"limit": 100, "used": 68, "remaining": 32}},
                "resets": {"usage_five_hour": (self.now + timedelta(hours=3)).isoformat(),
                           "usage_seven_day": (self.now + timedelta(days=2)).isoformat()}}
        for failures, age, want in ((1, 90, 0), (1, 200, 1), (3, 200, 0), (3, 250, 1)):
            with self.subTest(failures=failures, age=age):
                self.claude_calls.write_text("")
                quota._claude_state_path(self.log).write_text(json.dumps({
                    "last_attempt_at": (self.now - timedelta(seconds=age)).isoformat(),
                    "failures": failures, "last_good": good}))
                with mock.patch.object(quota, "datetime", wraps=datetime) as clock:
                    clock.now.return_value = self.now
                    quota._claude_snapshot(log=self.log)
                self.assertEqual(self.claude_call_count(), want)

    def test_claude_failure_shapes_and_idle_and_scoped(self):
        shapes = (
            ("fail", "failed"),
            ("null-limits", "failed"),
            ("malformed-limits", "failed"),
            ("no-report", "failed"),
            ("bad-percent-bool", "failed"),
            ("bad-percent-neg", "failed"),
            ("bad-percent-str", "failed"),
            ("garbage-line", None),  # mixed with a good assistant line
        )
        for mode, carried in shapes:
            with self.subTest(mode=mode):
                self.log.unlink(missing_ok=True)
                self.clear_claude_state()
                self.claude_calls.unlink(missing_ok=True)
                if carried == "failed":
                    quota.record(log=self.log)
                    state_path = quota._claude_state_path(self.log)
                    good = json.loads(state_path.read_text())["last_good"]
                    state_path.write_text(json.dumps({
                        "last_attempt_at": (self.now - timedelta(minutes=10)).isoformat(),
                        "failures": 0, "last_good": good}))
                    self.log.unlink()
                    self.claude_calls.unlink(missing_ok=True)
                with self.claude_fake(mode=mode):
                    row, _ = quota.record(log=self.log)
                self.assertNotIn("secret", json.dumps(row))
                state_text = quota._claude_state_path(self.log).read_text()
                self.assertNotIn("secret", state_text)
                if carried:
                    self.assertEqual(row["pools"]["claude-5h"].get("carried"), carried)
                else:
                    self.assertNotIn("carried", row["pools"]["claude-5h"])
                    self.assertIn("used_pct", row["pools"]["claude-5h"])
        self.clear_claude_state()
        self.log.unlink(missing_ok=True)
        with self.claude_fake(mode="idle"):
            row, _ = quota.record(log=self.log)
        self.assertEqual(row["pools"]["claude-5h"]["used_pct"], 0)
        self.assertIsNone(row["pools"]["claude-5h"]["resets_at"])
        self.assertIn("error", row["pools"]["claude-7d"])
        self.clear_claude_state()
        self.log.unlink()
        with self.claude_fake(mode="session-only"):
            row, _ = quota.record(log=self.log)
        self.assertIn("used_pct", row["pools"]["claude-5h"])
        self.assertIn("error", row["pools"]["claude-7d"])

    def test_claude_timeout_grandchild_and_close_then_exit(self):
        for mode in ("silent", "exit-grandchild"):
            with self.subTest(mode=mode):
                self.clear_claude_state()
                pidfile = self.tmp / f"claude-pids-{mode}"
                pidfile.unlink(missing_ok=True)
                started = time.monotonic()
                with self.claude_fake(mode=mode, pids=str(pidfile)):
                    snap, reason, error = quota._claude_snapshot(timeout=1, log=self.log)
                self.assertLess(time.monotonic() - started, 3)
                self.assertIsNone(snap)
                self.assertIsNotNone(error)
                self.assertNotIn("secret", error)
                for pid in pidfile.read_text().splitlines():
                    status = Path("/proc") / pid / "stat"
                    deadline = time.monotonic() + 0.5
                    while status.exists() and status.read_text().split()[2] != "Z" and time.monotonic() < deadline:
                        time.sleep(0.01)
                    if status.exists():
                        self.assertEqual(status.read_text().split()[2], "Z", f"surviving pid {pid}")
        self.clear_claude_state()
        started = time.monotonic()
        with self.claude_fake(mode="close-then-exit"):
            snap, reason, error = quota._claude_snapshot(timeout=20, log=self.log)
        self.assertLess(time.monotonic() - started, 5)
        self.assertIsNotNone(snap)
        self.assertIsNone(reason)
        # Child exits after the report while a grandchild still holds the pipes.
        for mode in ("report-linger", "pad-linger"):
            with self.subTest(mode=mode):
                self.clear_claude_state()
                started = time.monotonic()
                with self.claude_fake(mode=mode):
                    snap, reason, error = quota._claude_snapshot(timeout=20, log=self.log)
                took = time.monotonic() - started
                self.assertLess(took, 5, f"{mode} took {took:.1f} s")
                self.assertIsNotNone(snap)
                self.assertIsNone(reason)
                self.assertIn("usage_five_hour", snap["metrics"])

    def test_claude_env_allowlist_cwd_and_argv(self):
        self.clear_claude_state()
        self.claude_calls.unlink(missing_ok=True)
        with mock.patch.dict(os.environ, {
                "ANTHROPIC_API_KEY": "sk-ant-secret", "CLAUDECODE": "1",
                "CLAUDE_CODE_SESSION_ID": "sess", "LD_LIBRARY_PATH": "/tmp/.mount_cursor",
                "FAKE_CLAUDE_X": "should-not-reach-fake"}):
            quota.record(log=self.log)
        call = json.loads(self.claude_calls.read_text().splitlines()[0])
        self.assertEqual(call["cwd"], str(self.log.parent))
        self.assertEqual(call["argv"][:2], ["-p", "/usage"])
        self.assertIn("--no-session-persistence", call["argv"])
        self.assertIn("--strict-mcp-config", call["argv"])
        self.assertEqual(call["argv"][call["argv"].index("--settings") + 1],
                         '{"disableAllHooks": true}')
        env = call["env"]
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("CLAUDECODE", env)
        self.assertNotIn("CLAUDE_CODE_SESSION_ID", env)
        self.assertNotIn("LD_LIBRARY_PATH", env)
        self.assertNotIn("FAKE_CLAUDE_X", env)
        self.assertEqual(env.get("HOME"), str(self.home))
        self.assertEqual(env.get("PATH"), str(self.bin))
        self.assertEqual(env.get("CLAUDE_CONFIG_DIR"), str(self.claude_config))
        self.assertNotIn("FAKE_CLAUDE_X", quota._claude_env())

    def test_claude_state_corrupt_future_and_unwritable(self):
        path = quota._claude_state_path(self.log)
        path.write_text("{not-json")
        row, _ = quota.record(log=self.log)
        self.assertIn("used_pct", row["pools"]["claude-5h"])
        self.clear_claude_state()
        path.write_text(json.dumps({
            "last_attempt_at": (self.now + timedelta(hours=1)).isoformat(),
            "failures": 3,
            "last_good": {"timestamp": self.now.isoformat(),
                          "metrics": {"usage_five_hour": {"limit": 100, "used": 1, "remaining": 99}},
                          "resets": {}}}))
        row, _ = quota.record(log=self.log)
        self.assertNotIn("carried", row["pools"]["claude-5h"])
        if os.geteuid() == 0:
            self.skipTest("chmod 0555 is writable as root")
        self.clear_claude_state()
        blocked = self.tmp / "blocked-log"
        blocked.mkdir()
        log = blocked / "quota.jsonl"
        blocked.chmod(0o555)
        try:
            snap, reason, error = quota._claude_snapshot(log=log)
        finally:
            blocked.chmod(0o755)
        self.assertIsNotNone(snap)
        self.assertIsNone(reason)
        self.assertIsNone(error)

    def test_unsaved_attempt_stamp_carries_without_calling(self):
        good = {"timestamp": self.now.isoformat(),
                "metrics": {"usage_five_hour": {"limit": 100, "used": 5, "remaining": 95},
                            "usage_seven_day": {"limit": 100, "used": 68, "remaining": 32}},
                "resets": {"usage_five_hour": (self.now + timedelta(hours=3)).isoformat(),
                           "usage_seven_day": (self.now + timedelta(days=2)).isoformat()}}
        path = quota._claude_state_path(self.log)
        stale = (self.now - timedelta(hours=2)).isoformat()
        full = OSError(28, "No space left on device")
        # Positive control: the same stale stamp with a working save makes one call.
        path.write_text(json.dumps({"last_attempt_at": stale, "failures": 0, "last_good": good}))
        self.claude_calls.write_text("")
        quota._claude_snapshot(log=self.log)
        self.assertEqual(self.claude_call_count(), 1)
        # A failed save with a saved reading carries it and never calls.
        path.write_text(json.dumps({"last_attempt_at": stale, "failures": 0, "last_good": good}))
        self.claude_calls.write_text("")
        with mock.patch.object(quota, "_save_claude_state", side_effect=full):
            first = quota._claude_snapshot(log=self.log)
            second = quota._claude_snapshot(log=self.log)
        self.assertEqual(self.claude_call_count(), 0)
        self.assertEqual(first[1:], ("failed", None))
        self.assertEqual(first[0]["metrics"]["usage_five_hour"]["used"], 5)
        self.assertEqual(first, second)
        # With nothing saved it still reads (decided 2026-11-07: nothing persists there).
        path.write_text(json.dumps({"last_attempt_at": stale, "failures": 0, "last_good": None}))
        with mock.patch.object(quota, "_save_claude_state", side_effect=full):
            snap, reason, error = quota._claude_snapshot(log=self.log)
        self.assertEqual(self.claude_call_count(), 1)
        self.assertIsNotNone(snap)
        self.assertEqual((reason, error), (None, None))

    def test_concurrent_claude_readers_call_once(self):
        self.clear_claude_state()
        self.claude_calls.unlink(missing_ok=True)
        with self.claude_fake(sleep=0.2):
            with ThreadPoolExecutor(max_workers=4) as executor:
                results = list(executor.map(
                    lambda _: quota._claude_snapshot(timeout=5, log=self.log), range(4)))
        self.assertEqual(self.claude_call_count(), 1)
        snaps = [snap for snap, reason, error in results if snap and not reason]
        carried = [reason for snap, reason, error in results if reason]
        self.assertEqual(len(snaps) + len(carried), 4)
        self.assertGreaterEqual(len(snaps), 1)

    def test_last_good_skips_carried_for_if_active(self):
        good_ts = self.now
        carried_ts = self.now + timedelta(minutes=10)
        later = self.now + timedelta(minutes=20)
        activity = self.now + timedelta(minutes=5)
        pools = {pid: {"used_pct": 1, "resets_at": (self.now + timedelta(hours=3)).isoformat()}
                 for pid in ("claude-5h", "claude-7d")}
        carried_pools = {pid: {**pools[pid], "carried": "backoff"} for pid in pools}
        self.log.write_text("".join(json.dumps(row) + "\n" for row in (
            {"ts": good_ts.isoformat(), "pools": pools},
            {"ts": carried_ts.isoformat(), "pools": carried_pools},
            {"ts": later.isoformat(), "pools": carried_pools},
        )))
        good, stamp = quota._last_good(self.log)
        self.assertEqual(stamp, good_ts)
        self.projects = self.tmp / "claude/projects"
        self.projects.mkdir(parents=True)
        transcript = self.projects / "x" / "y.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text("{}\n")
        os.utime(transcript, (activity.timestamp(), activity.timestamp()))
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.projects.parent)}):
            reason, gate, skip = quota._activity_gate(self.log, later)
        self.assertEqual(reason, "timer")
        self.assertIsNone(skip)

    def test_reuse_recomputes_stale_for_carried(self):
        observed = self.now
        pools = {pid: {"used_pct": 1, "resets_at": (self.now + timedelta(hours=3)).isoformat(),
                       "observed_at": observed.isoformat(), "stale": False}
                 for pid in quota.POOLS}
        for pid in ("claude-5h", "claude-7d"):
            pools[pid]["carried"] = "throttle"
            pools[pid]["stale"] = False
        # Carried at +110s (age 110, not yet stale); reused at +125s → age 125 → stale.
        first = {"ts": (self.now + timedelta(seconds=110)).isoformat(),
                 "source": "hybrid", "reader": quota.HYBRID_READER, "pools": pools}
        self.log.write_text(json.dumps(first) + "\n")
        row = self.record_at(125)
        self.assertEqual(row["reused_from"], first["ts"])
        self.assertTrue(row["pools"]["claude-5h"]["stale"])
        self.assertEqual(row["pools"]["claude-5h"]["carried"], "throttle")
        self.assertNotIn("carried", row["pools"]["codex-5h"])

    def test_garbage_last_good_keeps_codex_and_cursor(self):
        # Valid timestamps so rejection comes from the gauges, not the stamp check.
        shapes = (
            {"usage_five_hour": "x"},
            {"usage_five_hour": {"limit": 100, "used": "a", "remaining": "b"}},
            {"usage_seven_day": None},
        )
        for metrics in shapes:
            with self.subTest(metrics=metrics):
                self.log.unlink(missing_ok=True)
                self.clear_claude_state()
                self.claude_calls.unlink(missing_ok=True)
                quota._claude_state_path(self.log).write_text(json.dumps({
                    "last_attempt_at": (self.now - timedelta(seconds=10)).isoformat(),
                    "failures": 0,
                    "last_good": {"timestamp": self.now.isoformat(),
                                  "metrics": metrics, "resets": {}}}))
                code, row, _ = self.snap_row(reason="manual")
                self.assertEqual(code, 0)
                self.assertNotIn("error", row)
                self.assertIn("used_pct", row["pools"]["codex-5h"])
                self.assertIn("used_pct", row["pools"]["cursor-auto"])
                # Corrupt state counts as none: throttle without last_good errors Claude,
                # or a fresh call succeeds once the floor allows — either way others survive.
                self.assertTrue("error" in row["pools"]["claude-5h"]
                                or "used_pct" in row["pools"]["claude-5h"])


    def test_date_only_stamps_keep_codex_and_cursor(self):
        good = {"timestamp": self.now.isoformat(),
                "metrics": {"usage_five_hour": {"limit": 100, "used": 5, "remaining": 95},
                            "usage_seven_day": {"limit": 100, "used": 68, "remaining": 32}},
                "resets": {}}
        for label, patch in (("reset", {"resets": {"usage_five_hour": "2026-11-07"}}),
                             ("timestamp", {"timestamp": "2026-11-07"})):
            with self.subTest(label=label):
                self.log.unlink(missing_ok=True)
                self.clear_claude_state()
                quota._claude_state_path(self.log).write_text(json.dumps({
                    "last_attempt_at": (self.now - timedelta(seconds=10)).isoformat(),
                    "failures": 0, "last_good": {**good, **patch}}))
                code, row, _ = self.snap_row(reason="manual")
                self.assertEqual(code, 0)
                self.assertNotIn("error", row)
                self.assertIn("used_pct", row["pools"]["codex-5h"])
                self.assertIn("used_pct", row["pools"]["cursor-auto"])
        self.clear_claude_state()
        self.log.unlink(missing_ok=True)
        self.claude_calls.unlink(missing_ok=True)
        with self.claude_fake(mode="ok", session_reset="2026-11-07"):
            code, row, _ = self.snap_row(reason="manual")
        self.assertEqual(code, 0)
        self.assertNotIn("error", row)
        self.assertIn("used_pct", row["pools"]["claude-7d"])
        self.assertIn("used_pct", row["pools"]["codex-5h"])
        self.assertIn("used_pct", row["pools"]["cursor-auto"])
        # Date-only Cursor reset: pool numbers stay, no TypeError.
        self.clear_claude_state()
        self.clear_cursor_state()
        self.log.unlink(missing_ok=True)
        with mock.patch.object(quota, "_cursor_call", side_effect=lambda *a, **k: {
                "provider_id": "cursor", "timestamp": self.now.isoformat(), "status": "ok",
                "whole_percent": True,
                "metrics": {"plan_auto_percent_used": {"used": 22},
                            "plan_api_percent_used": {"used": 34}},
                "resets": {"billing_cycle_end": "2026-11-07"}}):
            code, row, _ = self.snap_row(reason="manual")
        self.assertEqual(code, 0)
        self.assertNotIn("error", row)
        self.assertIn("used_pct", row["pools"]["cursor-auto"])
        self.assertIn("used_pct", row["pools"]["codex-5h"])
        self.assertIsNone(quota.parse_ts("2026-11-07"))
        self.assertIsNone(quota.parse_ts("20261107"))
        no_zone = quota.parse_ts("2026-11-07T03:00:00")
        self.assertIsNotNone(no_zone)
        self.assertEqual(no_zone.utcoffset(), timedelta(0))


    def test_lock_as_directory_still_uses_state(self):
        good = {"timestamp": self.now.isoformat(),
                "metrics": {"usage_five_hour": {"limit": 100, "used": 5, "remaining": 95},
                            "usage_seven_day": {"limit": 100, "used": 68, "remaining": 32}},
                "resets": {}}
        state_path = quota._claude_state_path(self.log)
        lock = Path(str(state_path) + ".lock")

        def make_lock_dir():
            lock.unlink(missing_ok=True)
            if lock.is_dir():
                lock.rmdir()
            lock.mkdir()

        self.clear_claude_state()
        self.claude_calls.unlink(missing_ok=True)
        state_path.write_text(json.dumps({
            "last_attempt_at": (self.now - timedelta(seconds=10)).isoformat(),
            "failures": 5, "last_good": good}))
        make_lock_dir()
        snap, reason, error = quota._claude_snapshot(log=self.log)
        self.assertEqual(self.claude_call_count(), 0)
        self.assertIsNotNone(snap)
        self.assertEqual(reason, "backoff")

        self.clear_claude_state()
        self.claude_calls.unlink(missing_ok=True)
        state_path.write_text(json.dumps({
            "last_attempt_at": (self.now - timedelta(hours=1)).isoformat(),
            "failures": 0, "last_good": good}))
        make_lock_dir()
        with self.claude_fake(mode="fail"):
            snap, reason, error = quota._claude_snapshot(log=self.log)
        self.assertIsNotNone(snap)
        self.assertEqual(reason, "failed")

        self.clear_claude_state()
        self.claude_calls.unlink(missing_ok=True)
        make_lock_dir()
        first, _, _ = quota._claude_snapshot(log=self.log)
        quota._claude_snapshot(log=self.log)
        self.assertIsNotNone(first)
        self.assertEqual(self.claude_call_count(), 1)

        self.clear_claude_state()
        self.claude_calls.unlink(missing_ok=True)
        folder = self.log.parent
        state_path.write_text(json.dumps({
            "last_attempt_at": (self.now - timedelta(seconds=10)).isoformat(),
            "failures": 5, "last_good": good}))
        folder.chmod(0o555)
        try:
            snap, reason, error = quota._claude_snapshot(log=self.log)
        finally:
            folder.chmod(0o755)
        self.assertEqual(self.claude_call_count(), 0)
        self.assertEqual(reason, "backoff")

    def test_malformed_reset_costs_only_its_pool(self):
        self.clear_claude_state()
        self.claude_calls.unlink(missing_ok=True)
        with self.claude_fake(mode="bad-reset"):
            code, row, _ = self.snap_row(reason="manual")
        self.assertEqual(code, 0)
        self.assertNotIn("error", row)
        self.assertIn("error", row["pools"]["claude-5h"])
        self.assertIn("used_pct", row["pools"]["claude-7d"])
        self.assertIn("used_pct", row["pools"]["codex-5h"])
        self.assertIn("used_pct", row["pools"]["cursor-auto"])
        # A last_good poisoned with a bad reset counts as no state.
        self.clear_claude_state()
        good = {"timestamp": self.now.isoformat(),
                "metrics": {"usage_five_hour": {"limit": 100, "used": 5, "remaining": 95},
                            "usage_seven_day": {"limit": 100, "used": 68, "remaining": 32}},
                "resets": {"usage_five_hour": "not-an-iso-date"}}
        quota._claude_state_path(self.log).write_text(json.dumps({
            "last_attempt_at": (self.now - timedelta(seconds=10)).isoformat(),
            "failures": 0, "last_good": good}))
        self.log.unlink(missing_ok=True)
        code, row, _ = self.snap_row(reason="manual")
        self.assertEqual(code, 0)
        self.assertNotIn("error", row)
        self.assertIn("used_pct", row["pools"]["codex-5h"])
        self.assertIn("used_pct", row["pools"]["cursor-auto"])


    def test_interrupted_read_leaves_attempt_and_throttles(self):
        self.clear_claude_state()
        self.claude_calls.unlink(missing_ok=True)
        good = {"timestamp": self.now.isoformat(),
                "metrics": {"usage_five_hour": {"limit": 100, "used": 5, "remaining": 95},
                            "usage_seven_day": {"limit": 100, "used": 68, "remaining": 32}},
                "resets": {}}
        quota._claude_state_path(self.log).write_text(json.dumps({
            "failures": 0, "last_good": good}))
        real = quota._claude_call

        def boom(*_a, **_k):
            raise KeyboardInterrupt

        quota._claude_call = boom
        try:
            with self.assertRaises(KeyboardInterrupt):
                quota._claude_snapshot(log=self.log)
        finally:
            quota._claude_call = real
        saved = json.loads(quota._claude_state_path(self.log).read_text())
        self.assertIn("last_attempt_at", saved)
        self.assertEqual(saved.get("failures"), 0)
        self.assertEqual(saved["last_good"]["metrics"], good["metrics"])
        self.claude_calls.write_text("")
        snap, reason, error = quota._claude_snapshot(log=self.log)
        self.assertEqual(reason, "throttle")
        self.assertEqual(self.claude_call_count(), 0)
        self.assertIsNotNone(snap)

    def test_no_usable_pool_is_total_failure(self):
        self.clear_claude_state()
        self.clear_cursor_state()
        with self.claude_fake(mode="fail"), mock.patch.dict(os.environ, {
                "FAKE_CODEX_MODE": "rpc-error", "FAKE_CURSOR_TUI": "no_meters"}):
            code, row, _ = self.snap_row(reason="manual")
        self.assertEqual(code, 1)
        self.assertIn("error", row)
        # Usable Cursor keeps exit 0 even when Codex and Claude fail.
        self.clear_claude_state()
        self.clear_cursor_state()
        self.log.unlink(missing_ok=True)
        with self.claude_fake(mode="fail"), mock.patch.dict(os.environ, {"FAKE_CODEX_MODE": "rpc-error"}):
            code, row, _ = self.snap_row(reason="manual")
        self.assertEqual(code, 0)
        self.assertNotIn("error", row)
        self.assertIn("used_pct", row["pools"]["cursor-auto"])
        # A carried Claude reading is usable.
        self.clear_claude_state()
        self.log.unlink(missing_ok=True)
        good = {"timestamp": self.now.isoformat(),
                "metrics": {"usage_five_hour": {"limit": 100, "used": 5, "remaining": 95},
                            "usage_seven_day": {"limit": 100, "used": 68, "remaining": 32}},
                "resets": {"usage_five_hour": (self.now + timedelta(hours=3)).isoformat(),
                           "usage_seven_day": (self.now + timedelta(days=2)).isoformat()}}
        quota._claude_state_path(self.log).write_text(json.dumps({
            "last_attempt_at": self.now.isoformat(), "failures": 0, "last_good": good}))
        with mock.patch.dict(os.environ, {"FAKE_CODEX_MODE": "rpc-error"}):
            code, row, _ = self.snap_row(reason="manual")
        self.assertEqual(code, 0)
        self.assertNotIn("error", row)
        self.assertEqual(row["pools"]["claude-5h"].get("carried"), "throttle")
        self.assertIn("used_pct", row["pools"]["cursor-auto"])


    def test_quota_change_needs_two_observations(self):
        import report
        t0, t1 = "2026-11-07T08:00:00+00:00", "2026-11-07T08:01:00+00:00"

        def entry(used, observed, carried=None):
            row = {"used_pct": used, "resets_at": "2026-11-07T12:00:00+00:00",
                   "observed_at": observed, "stale": False}
            return {**row, "carried": carried} if carried else row

        def delta(first, last):
            rows = [{"ts": ts, "pools": {"claude-5h": item}}
                    for ts, item in (("2026-11-07T08:00:00+00:00", first),
                                     ("2026-11-07T08:02:00+00:00", last))]
            return report.quota_change(*rows)["pools"]["claude-5h"]["delta"]

        self.assertIsNone(delta(entry(10, t0), entry(10, t0, "throttle")))
        self.assertIsNone(delta(entry(10, t0, "throttle"), entry(10, t0, "backoff")))
        self.assertEqual(delta(entry(10, t0), entry(12, t1, "throttle")), 2)
        self.assertEqual(delta(entry(10, t0), entry(12, t1)), 2)
        self.assertEqual(delta(entry(10, None), entry(12, None)), 2)


class CachedQuotaTests(unittest.TestCase):
    """delegate quota --cached: log-only newest-per-pool, no live readers."""

    def setUp(self):
        guard_stdout(self)
        temporary = tempfile.TemporaryDirectory(prefix="quota-cached-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.log = self.tmp / "quota.jsonl"
        self.now = datetime(2026, 11, 15, 3, 0, 0, tzinfo=timezone.utc)
        self.env = {
            "HOME": str(self.tmp / "home"),
            "XDG_CACHE_HOME": str(self.tmp / "cache"),
            "XDG_CONFIG_HOME": str(self.tmp / "config"),
            "CLAUDE_CONFIG_DIR": str(self.tmp / "claude"),
            "DELEGATE_CATALOG_LOCAL": "",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        }
        (self.tmp / "home").mkdir()
        (self.tmp / "cache").mkdir()
        (self.tmp / "config").mkdir()
        (self.tmp / "claude").mkdir()
        env_patch = mock.patch.dict(os.environ, self.env, clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def write_lines(self, *lines):
        self.log.parent.mkdir(parents=True, exist_ok=True)
        text = ""
        for line in lines:
            if isinstance(line, dict):
                text += json.dumps(line) + "\n"
            else:
                text += line if line.endswith("\n") else line + "\n"
        self.log.write_text(text)

    def pool(self, used_pct, resets_at, observed_at=None, **extra):
        entry = {"used_pct": used_pct, "resets_at": resets_at}
        if observed_at is not None:
            entry["observed_at"] = observed_at
        entry.update(extra)
        return entry

    def all_pools(self, used=10.0, resets="2026-11-15T08:00:00+00:00",
                  observed="2026-11-15T01:00:00+00:00", **overrides):
        base = {pid: self.pool(used, resets, observed) for pid in quota.POOLS}
        for pid, entry in overrides.items():
            base[pid] = entry
        return base

    def cli(self, *args, code=0, env=None, path_extra=None):
        empty = self.tmp / "empty-path"
        empty.mkdir(exist_ok=True)
        path = str(empty)
        if path_extra:
            path = f"{path_extra}{os.pathsep}{path}"
        result = subprocess.run(
            [sys.executable, str(CLI), "quota", *map(str, args)],
            env={**os.environ, "PATH": path, **(env or {})},
            text=True, capture_output=True)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stdout + result.stderr)
        return result

    def test_newest_per_pool_adversarial(self):
        # Older row: good figures for every pool.
        old_obs = "2026-11-15T00:30:00+00:00"
        old = {"ts": "2026-11-15T00:40:00+00:00", "pools": self.all_pools(
            used=20.0, resets="2026-11-15T10:00:00+00:00", observed=old_obs,
            **{"codex-5h": self.pool(11.0, "2026-11-15T05:00:00+00:00", old_obs)})}
        # Middle: codex-5h error; cursor-auto carried with observed_at 40 min older than ts.
        mid_ts = "2026-11-15T02:00:00+00:00"
        cursor_obs = "2026-11-15T01:20:00+00:00"
        mid = {"ts": mid_ts, "pools": {
            "codex-5h": {"error": "codex app-server failed"},
            "cursor-auto": self.pool(33.0, "2026-11-18T01:53:54+00:00", cursor_obs,
                                     carried="throttle"),
            "claude-5h": self.pool(25.0, "2026-11-15T07:00:00+00:00", mid_ts),
        }}
        # Newest: top-level error must not hide older goods.
        newest = {"ts": "2026-11-15T02:30:00+00:00", "error": "quota readers failed",
                  "pools": self.all_pools(used=99.0)}
        self.write_lines(old, mid, newest)
        result = quota.cached_pools(log=self.log, now=self.now)
        self.assertEqual(result["codex-5h"]["left_pct"], 89.0)
        self.assertEqual(result["codex-5h"]["row_ts"], old["ts"])
        self.assertEqual(result["cursor-auto"]["left_pct"], 67.0)
        self.assertEqual(result["cursor-auto"]["row_ts"], mid_ts)
        self.assertEqual(result["cursor-auto"]["observed_at"], cursor_obs)
        self.assertEqual(result["cursor-auto"]["carried"], "throttle")
        # age from observed_at: (03:00 - 01:20) = 100 min
        self.assertEqual(result["cursor-auto"]["age_min"], 100)
        self.assertEqual(result["claude-5h"]["row_ts"], mid_ts)
        self.assertEqual(result["claude-5h"]["left_pct"], 75.0)
        # Pools only in the old row keep that row.
        self.assertEqual(result["codex-7d"]["row_ts"], old["ts"])
        self.assertEqual(result["claude-7d"]["row_ts"], old["ts"])
        self.assertEqual(result["cursor-premium"]["row_ts"], old["ts"])

    def test_skip_rules(self):
        good_obs = "2026-11-15T01:00:00+00:00"
        good = {"ts": "2026-11-15T01:10:00+00:00",
                "pools": self.all_pools(used=40.0, observed=good_obs)}
        self.write_lines(
            good,
            {"ts": "2026-11-15T01:20:00+00:00", "error": "top-level",
             "pools": self.all_pools(used=1.0)},
            {"ts": "2026-11-15T01:30:00+00:00", "pools": {
                "codex-5h": {"error": "per-pool"},
                "claude-5h": self.pool("12", "2026-11-15T08:00:00+00:00", good_obs),
                "claude-7d": self.pool(True, "2026-11-15T08:00:00+00:00", good_obs),
                "codex-7d": self.pool(False, "2026-11-15T08:00:00+00:00", good_obs),
            }},
            "{not json\n",
            '{"ts": "2026-11-15T01:40:00+00:00", "pools": ',  # truncated
            {"ts": "not-a-timestamp", "pools": self.all_pools(used=2.0)},
        )
        result = quota.cached_pools(log=self.log, now=self.now)
        for pid in quota.POOLS:
            self.assertEqual(result[pid]["left_pct"], 60.0, pid)
            self.assertEqual(result[pid]["row_ts"], good["ts"], pid)

    def test_non_string_timestamps_keep_older_good(self):
        good_obs = "2026-11-15T01:00:00+00:00"
        good = {"ts": "2026-11-15T01:10:00+00:00",
                "pools": self.all_pools(used=40.0, observed=good_obs)}
        # Numeric row ts: whole row skipped.
        bad_ts = {"ts": 1731632400, "pools": self.all_pools(used=1.0)}
        # Numeric observed_at: fall back to row ts; still usable.
        # Dict resets_at: counts as no reset (pace None).
        mixed = {"ts": "2026-11-15T02:00:00+00:00", "pools": {
            "claude-5h": self.pool(15.0, "2026-11-15T08:00:00+00:00", 1731632400),
            "codex-5h": self.pool(15.0, {"when": "soon"}, good_obs),
        }}
        self.write_lines(good, bad_ts, mixed)
        result = quota.cached_pools(log=self.log, now=self.now)
        # Bad ts row never hid the good figures for pools not in mixed.
        self.assertEqual(result["claude-7d"]["left_pct"], 60.0)
        self.assertEqual(result["claude-7d"]["row_ts"], good["ts"])
        # Numeric observed_at → age from row ts 02:00 → 60m; left from mixed.
        self.assertEqual(result["claude-5h"]["left_pct"], 85.0)
        self.assertEqual(result["claude-5h"]["age_min"], 60)
        self.assertEqual(result["claude-5h"]["observed_at"], 1731632400)
        # Dict resets_at → no reset, pace None; still preferred over older good.
        self.assertEqual(result["codex-5h"]["left_pct"], 85.0)
        self.assertIsNone(result["codex-5h"]["resets_at"])
        self.assertIsNone(result["codex-5h"]["pace"])

    def test_age_from_observed_at_and_row_ts_fallback(self):
        row_ts = "2026-11-15T02:03:01+00:00"
        observed = "2026-11-15T01:36:33+00:00"
        self.write_lines({
            "ts": row_ts,
            "pools": {
                "cursor-auto": self.pool(22.0, "2026-11-18T01:53:54+00:00", observed),
                "codex-5h": self.pool(10.0, "2026-11-15T07:00:00+00:00"),  # no observed_at
                "claude-5h": self.pool(10.0, "2026-11-15T07:00:00+00:00",
                                       "not-a-time"),  # unparseable → row ts
            },
        })
        result = quota.cached_pools(log=self.log, now=self.now)
        # 03:00 - 01:36:33 = 83.45 min → 83
        self.assertEqual(result["cursor-auto"]["age_min"], 83)
        # 03:00 - 02:03:01 = 56.983 min → 57
        self.assertEqual(result["codex-5h"]["age_min"], 57)
        self.assertEqual(result["claude-5h"]["age_min"], 57)
        self.assertIsNone(result["codex-5h"]["observed_at"])

    def test_stale_reset_nulls_pace(self):
        self.write_lines({
            "ts": "2026-11-15T01:00:00+00:00",
            "pools": {
                "codex-5h": self.pool(10.0, "2026-11-15T02:00:00+00:00",
                                      "2026-11-15T01:00:00+00:00"),
                "codex-7d": self.pool(10.0, self.now.isoformat(),
                                      "2026-11-15T01:00:00+00:00"),
            },
        })
        result = quota.cached_pools(log=self.log, now=self.now)
        for pid in ("codex-5h", "codex-7d"):
            self.assertTrue(result[pid]["stale_reset"], pid)
            self.assertIsNone(result[pid]["time_left_frac"], pid)
            self.assertIsNone(result[pid]["pace"], pid)

    def test_pace_hand_computed_for_window_and_cycle(self):
        # 5h pool: reset in 2.5 h → tlf = 2.5/5 = 0.5; left 80% → pace = 0.8/0.5 = 1.6
        # 7d pool: reset in 84 h → tlf = 84/168 = 0.5; left 50% → pace = 0.5/0.5 = 1.0
        # cycle at window start: reset 2026-12-15T03:00:00Z, start 2026-11-15T03:00:00Z;
        #         at now (= start) tlf = 1.0; left 75% → pace = 0.75/1.0 = 0.75
        reset_5h = (self.now + timedelta(hours=2, minutes=30)).isoformat()
        reset_7d = (self.now + timedelta(hours=84)).isoformat()
        reset_cycle = "2026-12-15T03:00:00+00:00"
        obs = self.now.isoformat()
        self.write_lines({
            "ts": obs,
            "pools": {
                "codex-5h": self.pool(20.0, reset_5h, obs),
                "codex-7d": self.pool(50.0, reset_7d, obs),
                "cursor-auto": self.pool(25.0, reset_cycle, obs, whole_percent=True),
            },
        })
        result = quota.cached_pools(log=self.log, now=self.now)
        self.assertAlmostEqual(result["codex-5h"]["time_left_frac"], 0.5, places=6)
        self.assertAlmostEqual(result["codex-5h"]["pace"], 1.6, places=6)
        self.assertAlmostEqual(result["codex-7d"]["time_left_frac"], 0.5, places=6)
        self.assertAlmostEqual(result["codex-7d"]["pace"], 1.0, places=6)
        self.assertAlmostEqual(result["cursor-auto"]["time_left_frac"], 1.0, places=6)
        self.assertAlmostEqual(result["cursor-auto"]["pace"], 0.75, places=6)
        self.assertTrue(result["cursor-auto"]["whole_percent"])

    def test_pace_mid_cycle_hand_computed(self):
        # reset 2026-12-15T03:00:00Z → calendar start 2026-11-15T03:00:00Z (30d).
        # Halfway: now = 2026-11-30T03:00:00Z → tlf = 15/30 = 0.5;
        # left 75% → pace = 0.75 / 0.5 = 1.5. Expected values computed by hand only.
        reset = "2026-12-15T03:00:00+00:00"
        now = datetime(2026, 11, 30, 3, 0, 0, tzinfo=timezone.utc)
        obs = now.isoformat()
        self.write_lines({
            "ts": obs,
            "pools": {"cursor-auto": self.pool(25.0, reset, obs)},
        })
        result = quota.cached_pools(log=self.log, now=now)
        self.assertAlmostEqual(result["cursor-auto"]["time_left_frac"], 0.5, places=6)
        self.assertAlmostEqual(result["cursor-auto"]["pace"], 1.5, places=6)

    def test_int_used_pct_and_torn_final_line(self):
        good = {"ts": "2026-11-15T01:10:00+00:00",
                "pools": self.all_pools(used=40, observed="2026-11-15T01:00:00+00:00")}
        self.write_lines(good)
        with self.log.open("a", encoding="utf-8") as stream:
            stream.write('{"ts": "2026-11-15T02:00:00+00:00", "pools": {')
        result = quota.cached_pools(log=self.log, now=self.now)
        for pid in quota.POOLS:
            self.assertEqual(result[pid]["left_pct"], 60.0, pid)
            self.assertEqual(result[pid]["row_ts"], good["ts"], pid)

    def test_nan_used_pct_keeps_older_good(self):
        good = {"ts": "2026-11-15T01:10:00+00:00",
                "pools": self.all_pools(used=40.0, observed="2026-11-15T01:00:00+00:00")}
        nan_row = {"ts": "2026-11-15T02:00:00+00:00",
                   "pools": self.all_pools(used=float("nan"),
                                           observed="2026-11-15T02:00:00+00:00")}
        self.write_lines(good, nan_row)
        # Python json round-trips NaN; confirm the fixture actually stores it.
        stored = json.loads(self.log.read_text().splitlines()[-1])
        self.assertTrue(math.isnan(stored["pools"]["codex-5h"]["used_pct"]))
        result = quota.cached_pools(log=self.log, now=self.now)
        for pid in quota.POOLS:
            self.assertEqual(result[pid]["left_pct"], 60.0, pid)
            self.assertEqual(result[pid]["row_ts"], good["ts"], pid)

    def test_missing_and_empty_file(self):
        missing = self.tmp / "absent.jsonl"
        for path in (missing, self.log):
            if path == self.log:
                path.write_text("")
            result = quota.cached_pools(log=path, now=self.now)
            for pid, spec in quota.POOLS.items():
                self.assertEqual(result[pid], {
                    "label": spec["label"], "provider": spec["provider"],
                    "error": f"no saved reading in {path}",
                })
        out = self.cli("--cached", "--json", "--log", missing, "--now", self.now.isoformat())
        self.assertEqual(out.returncode, 0)
        parsed = json.loads(out.stdout)
        self.assertTrue(all("error" in parsed[pid] for pid in quota.POOLS))

    def test_unreadable_log_directory(self):
        directory = self.tmp / "not-a-log"
        directory.mkdir()
        result = quota.cached_pools(log=directory, now=self.now)
        for pid in quota.POOLS:
            err = result[pid]["error"]
            self.assertTrue(err.startswith(f"cannot read {directory}: "), err)
            self.assertNotIn("Traceback", err)
        out = self.cli("--cached", "--json", "--log", directory, "--now",
                       self.now.isoformat())
        self.assertEqual(out.returncode, 0)
        parsed = json.loads(out.stdout)
        self.assertTrue(all(parsed[pid]["error"].startswith(f"cannot read {directory}: ")
                            for pid in quota.POOLS))

    def test_cli_json_text_now_and_refusals(self):
        obs = "2026-11-15T01:36:33+00:00"
        self.write_lines({
            "ts": "2026-11-15T02:03:01+00:00",
            "pools": self.all_pools(
                used=22.0, resets="2026-11-15T08:00:00+00:00", observed=obs,
                **{"cursor-auto": self.pool(22.0, "2026-11-18T01:53:54+00:00", obs)}),
        })
        now_s = self.now.isoformat()
        js = self.cli("--cached", "--json", "--log", self.log, "--now", now_s)
        data = json.loads(js.stdout)
        self.assertEqual(data["cursor-auto"]["age_min"], 83)
        self.assertEqual(data["cursor-auto"]["left_pct"], 78.0)
        self.assertIn("row_ts", data["cursor-auto"])

        text = self.cli("--cached", "--log", self.log, "--now", now_s)
        lines = text.stdout.splitlines()
        self.assertEqual(
            lines[0],
            f"Cached readings from {self.log} (newest per pool); no live read.")
        header = lines[1]
        self.assertIn("DATA AGE", header)
        cursor_line = next(line for line in lines if line.startswith("cursor-auto"))
        self.assertIn("83m", cursor_line)
        self.assertIn("78%", cursor_line)

        bad = self.cli("--cached", "--now", "not-a-time", "--log", self.log, code=1)
        self.assertIn("invalid --now", bad.stderr)

        for args in (("--cached", "--snapshot"),
                     ("--cached", "--snapshot", "--log", str(self.log)),
                     ("--cached", "--unused"),
                     ("--cached", "--if-active")):
            refused = self.cli(*args, code=1)
            stderr_lines = [line for line in refused.stderr.splitlines() if line.strip()]
            self.assertEqual(len(stderr_lines), 1, refused.stderr)
            self.assertIn("--cached", stderr_lines[0])
            self.assertEqual(refused.stdout, "")

    def test_one_now_shared_without_flag(self):
        self.write_lines({
            "ts": "2026-11-15T01:00:00+00:00",
            "pools": self.all_pools(used=10.0, observed="2026-11-15T01:00:00+00:00"),
        })
        loader = importlib.machinery.SourceFileLoader("quota_delegate_cli_now", str(CLI))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        cli = importlib.util.module_from_spec(spec)
        loader.exec_module(cli)
        captured = {}
        real_cached = quota.cached_pools
        real_render = quota.render

        def wrap_cached(*args, **kwargs):
            captured["cached_now"] = kwargs.get("now")
            return real_cached(*args, **kwargs)

        def wrap_render(*args, **kwargs):
            captured["render_now"] = kwargs.get("now")
            return real_render(*args, **kwargs)

        out = io.StringIO()
        with mock.patch.object(quota, "cached_pools", side_effect=wrap_cached), \
             mock.patch.object(quota, "render", side_effect=wrap_render), \
             mock.patch.object(sys, "argv",
                               [str(CLI), "quota", "--cached", "--log", str(self.log)]), \
             redirect_stdout(out):
            cli.main()
        self.assertIsNotNone(captured.get("cached_now"))
        self.assertEqual(captured["cached_now"], captured["render_now"])

    def test_cached_launches_no_reader_with_live_control(self):
        # Positive control: live quota records a stub call; --cached must not.
        self.write_lines({
            "ts": "2026-11-15T01:00:00+00:00",
            "pools": self.all_pools(used=10.0, observed="2026-11-15T01:00:00+00:00"),
        })
        calls = _env.CALLS
        before = calls.read_text() if calls.exists() else ""

        def stub_lines():
            if not calls.exists():
                return []
            return [line for line in calls.read_text()[len(before):].splitlines()
                    if line.strip()]

        def restore_calls():
            calls.write_text(before)
        self.addCleanup(restore_calls)

        empty = self.tmp / "empty-path"
        empty.mkdir(exist_ok=True)
        child_env = {**os.environ, "PATH": f"{_env.BIN}{os.pathsep}{empty}",
                     "HOME": str(self.tmp / "home"),
                     "XDG_CACHE_HOME": str(self.tmp / "cache"),
                     "XDG_CONFIG_HOME": str(self.tmp / "config"),
                     "CLAUDE_CONFIG_DIR": str(self.tmp / "claude"),
                     "DELEGATE_CATALOG_LOCAL": ""}
        live = subprocess.run(
            [sys.executable, str(CLI), "quota", "--json"],
            env=child_env, text=True, capture_output=True)
        live_calls = stub_lines()
        self.assertTrue(any("app-server" in line for line in live_calls), live_calls)

        calls.write_text(before)
        for args in (("--cached", "--log", str(self.log)),
                     ("--cached", "--json", "--log", str(self.log))):
            result = subprocess.run(
                [sys.executable, str(CLI), "quota", *args],
                env=child_env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(stub_lines(), [], f"cached launched readers: {stub_lines()}")

    def test_cached_no_side_effects_in_process(self):
        self.write_lines({
            "ts": "2026-11-15T01:00:00+00:00",
            "pools": self.all_pools(used=10.0, observed="2026-11-15T01:00:00+00:00"),
        })
        before_bytes = self.log.read_bytes()
        cache_root = Path(os.environ["XDG_CACHE_HOME"])
        config_root = Path(os.environ["XDG_CONFIG_HOME"])

        def tree_files(root):
            if not root.exists():
                return set()
            return {str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()}

        before_cache, before_config = tree_files(cache_root), tree_files(config_root)
        boom = AssertionError("cached path must not call this")
        patches = [
            mock.patch.object(subprocess, "Popen", side_effect=boom),
            mock.patch.object(subprocess, "run", side_effect=boom),
            mock.patch.object(quota, "fetch", side_effect=boom),
            mock.patch.object(quota, "_hybrid_snapshot", side_effect=boom),
            mock.patch.object(quota, "_codex_snapshot", side_effect=boom),
            mock.patch.object(quota, "_claude_snapshot", side_effect=boom),
            mock.patch.object(quota, "_cursor_snapshot", side_effect=boom),
            mock.patch.object(quota, "check_dispatch", side_effect=boom),
            mock.patch.object(quota, "spawn_snapshot", side_effect=boom),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

        with self.assertRaises(AssertionError):
            quota.fetch()

        result = quota.cached_pools(log=self.log, now=self.now)
        self.assertEqual(result["codex-5h"]["left_pct"], 90.0)

        loader = importlib.machinery.SourceFileLoader("quota_delegate_cli_side", str(CLI))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        cli = importlib.util.module_from_spec(spec)
        loader.exec_module(cli)
        for argv in ([str(CLI), "quota", "--cached", "--json", "--log", str(self.log),
                      "--now", self.now.isoformat()],
                     [str(CLI), "quota", "--cached", "--log", str(self.log),
                      "--now", self.now.isoformat()]):
            out = io.StringIO()
            with mock.patch.object(sys, "argv", argv), redirect_stdout(out):
                cli.main()
            self.assertTrue(out.getvalue())

        self.assertEqual(self.log.read_bytes(), before_bytes)
        self.assertEqual(tree_files(cache_root), before_cache)
        self.assertEqual(tree_files(config_root), before_config)


if __name__ == "__main__":
    unittest.main()

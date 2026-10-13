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


if __name__ == "__main__":
    unittest.main()

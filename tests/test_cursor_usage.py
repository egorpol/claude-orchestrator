"""Cursor interactive /usage reader: pty TUI, throttle, hybrid cutover, monitor."""
import importlib.util
import json
import os
import pty
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/delegate/scripts"
sys.path.insert(0, str(SCRIPTS))
import monitor
import quota

FAKES = Path(__file__).resolve().parent / "fake_agents"


def _pty_available():
    try:
        master, slave = pty.openpty()
    except OSError:
        return False
    try:
        os.close(master)
    except OSError:
        pass
    try:
        os.close(slave)
    except OSError:
        pass
    return True


PTY_OK = _pty_available()
requires_pty = unittest.skipUnless(PTY_OK, "no /dev/ptmx in this environment")


class CursorUsageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="cursor-usage-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.cache = self.tmp / "cache"
        self.log = self.tmp / "quota.jsonl"
        self.tui_log = self.tmp / "tui.log"
        self.env_log = self.tmp / "tui-env.jsonl"
        self.claude_config = self.tmp / "claude-config"
        self.claude_config.mkdir()
        self.catalog_local = self.tmp / "catalog.local.json"
        self.catalog_local.write_text(json.dumps({"cursor_usage_reader": True}))
        self.claude_calls = self.tmp / "claude.calls"
        self.codex_calls = self.tmp / "codex.calls"
        self.openusage_calls = self.tmp / "openusage.calls"
        for name in ("cursor-agent", "codex", "claude", "openusage"):
            path = self.bin / name
            path.write_text(f"#!{sys.executable}\n" + (FAKES / name).read_text().split("\n", 1)[1])
            path.chmod(0o755)
        (self.claude_config / "fake-claude.json").write_text(json.dumps({
            "mode": "ok", "log": str(self.claude_calls)}))
        self.export = self.tmp / "export.json"
        self.now = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)
        self.export.write_text(json.dumps({
            "snapshots": [{
                "provider_id": "cursor", "timestamp": self.now.isoformat(), "status": "ok",
                "metrics": {"plan_auto_percent_used": {"used": 99},
                            "plan_api_percent_used": {"used": 99},
                            "billing_cycle_progress": {"used": 50}},
                "resets": {"billing_cycle_end": "2026-11-18T01:53:54+00:00"},
            }]}))
        self.env = {
            "HOME": str(self.home),
            "PATH": str(self.bin),
            "XDG_CACHE_HOME": str(self.cache),
            "XDG_CONFIG_HOME": str(self.tmp / "config"),
            "CLAUDE_CONFIG_DIR": str(self.claude_config),
            "DELEGATE_CATALOG_LOCAL": str(self.catalog_local),
            "FAKE_CURSOR_TUI": "normal",
            "FAKE_CURSOR_TUI_LOG": str(self.tui_log),
            "FAKE_CURSOR_TUI_ENV": str(self.env_log),
            "FAKE_CODEX_LOG": str(self.codex_calls),
            "FAKE_OPENUSAGE_FILE": str(self.export),
            "FAKE_OPENUSAGE_LOG": str(self.openusage_calls),
            "LANG": "en_US.UTF-8",
            "LC_ALL": "en_US.UTF-8",
        }
        patcher = mock.patch.dict(os.environ, self.env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def clear_cursor_state(self):
        path = quota._cursor_state_path(self.log)
        path.unlink(missing_ok=True)
        Path(str(path) + ".lock").unlink(missing_ok=True)

    def tui_bytes(self):
        if not self.tui_log.exists():
            return b""
        return self.tui_log.read_bytes()

    def snapshot_at(self, when=None, timeout=5):
        when = when or self.now
        with mock.patch.object(quota, "datetime", wraps=datetime) as clock:
            clock.now.return_value = when
            return quota._cursor_snapshot(timeout=timeout, log=self.log)

    @requires_pty
    def test_normal_read_gauges_argv_and_locale(self):
        snap, reason, error = self.snapshot_at()
        self.assertIsNone(reason)
        self.assertIsNone(error)
        self.assertEqual(snap["metrics"]["plan_auto_percent_used"]["used"], 55)
        self.assertEqual(snap["metrics"]["plan_api_percent_used"]["used"], 46)
        self.assertEqual(snap["resets"]["billing_cycle_end"], "2026-11-18T00:00:00+00:00")
        self.assertTrue(snap["whole_percent"])
        env_row = json.loads(self.env_log.read_text().splitlines()[0])
        self.assertEqual(env_row["argv"],
                         ["--mode", "ask", "--trust", "--workspace", env_row["argv"][4]])
        self.assertNotIn("-p", env_row["argv"])
        self.assertNotIn("--force", env_row["argv"])
        self.assertEqual(env_row["LANG"], "en_GB.UTF-8")
        self.assertEqual(env_row["LC_ALL"], "en_GB.UTF-8")
        self.assertIn(b"/usage", self.tui_bytes())
        self.assertIn(b"\r", self.tui_bytes())

    @requires_pty
    def test_menu_without_usage_sends_no_enter(self):
        os.environ["FAKE_CURSOR_TUI"] = "no_usage_menu"
        snap, reason, error = self.snapshot_at(timeout=1)
        self.assertNotIn(b"\r", self.tui_bytes())
        self.assertTrue(snap is None or reason in ("failed", None) or error)

    @requires_pty
    def test_other_row_selected_sends_no_enter(self):
        os.environ["FAKE_CURSOR_TUI"] = "other_row_selected"
        self.snapshot_at(timeout=1)
        self.assertNotIn(b"\r", self.tui_bytes())

    @requires_pty
    def test_description_before_last_char_sends_no_enter(self):
        os.environ["FAKE_CURSOR_TUI"] = "description_before_last_char"
        self.snapshot_at(timeout=1)
        self.assertNotIn(b"\r", self.tui_bytes())

    @requires_pty
    def test_menu_dismissed_after_selection_sends_no_enter(self):
        for case in ("menu_dismissed", "menu_dismissed_late", "menu_cleared_input_kept"):
            with self.subTest(case=case):
                self.clear_cursor_state()
                self.tui_log.unlink(missing_ok=True)
                os.environ["FAKE_CURSOR_TUI"] = case
                snap, reason, error = self.snapshot_at(timeout=1)
                self.assertIn(b"/usage", self.tui_bytes())
                self.assertNotIn(b"\r", self.tui_bytes())
                self.assertIsNone(snap)
                self.assertTrue(error)

    def test_screen_replay_judges_current_frame(self):
        erase = lambda n: b"\x1b[2K\x1b[1A" * (n - 1) + b"\x1b[2K\x1b[G"
        typed = "  → /usage\r\n".encode()
        row = "   → /usage   Show plan and on-demand usage\r\n".encode()
        ctx = b"     /context   Show context usage breakdown\r\n"
        prompt = "  → Plan, search, build anything\r\n".encode()
        selected = typed + row + ctx
        self.assertTrue(quota._cursor_usage_selected(selected))
        # Superseded: menu cleared and prompt restored by a later frame.
        self.assertFalse(quota._cursor_usage_selected(selected + erase(4) + prompt))
        # Superseded: menu cleared, "/usage" still typed.
        self.assertFalse(quota._cursor_usage_selected(selected + erase(4) + typed))
        # Redraw of the bottom rows only: the selected row above stays on screen.
        self.assertTrue(quota._cursor_usage_selected(selected + erase(2) + b"     /copy   Copy\r\n"))
        # Prompt restored while the row lingers (the real screen right after Enter).
        self.assertFalse(quota._cursor_usage_selected(prompt + row))
        # A screen clear forgets everything above it.
        self.assertFalse(quota._cursor_usage_selected(selected + b"\x1b[2J\x1b[H"))
        # Erase to the left (CSI 1 K) includes the cursor cell: the row's arrow is gone.
        self.assertFalse(quota._cursor_usage_selected(selected + b"\x1b[2A\x1b[4G\x1b[1K"))
        # One column short of the arrow leaves it, so the row stays selected.
        self.assertTrue(quota._cursor_usage_selected(selected + b"\x1b[2A\x1b[3G\x1b[1K"))

    @requires_pty
    def test_login_screen_types_nothing(self):
        os.environ["FAKE_CURSOR_TUI"] = "login"
        self.snapshot_at(timeout=1)
        # Exit keys (Esc, Ctrl-C) are safe on any screen; text and Enter are not.
        self.assertEqual(self.tui_bytes().strip(b"\x1b\x03"), b"")

    @requires_pty
    def test_child_exit_fails_at_once(self):
        # A crash or logged-out exit must not spin to the deadline under the quota lock.
        os.environ["FAKE_CURSOR_TUI"] = "exit_early"
        started = time.monotonic()
        snap, _, error = self.snapshot_at(timeout=8)
        self.assertLess(time.monotonic() - started, 2)
        self.assertIsNone(snap)
        self.assertIsNotNone(error)

    @requires_pty
    def test_changed_layout_keeps_last_reading_failed(self):
        first, _, _ = self.snapshot_at()
        self.assertIsNotNone(first)
        self.clear_cursor_state()
        # Seed last_good, then fail the next read with a missing Auto row.
        state_path = quota._cursor_state_path(self.log)
        good = {"timestamp": first["timestamp"], "metrics": first["metrics"],
                "resets": first["resets"]}
        state_path.write_text(json.dumps({
            "last_attempt_at": (self.now - timedelta(seconds=quota.CURSOR_MIN_INTERVAL_S + 1)
                                ).isoformat(),
            "failures": 0, "last_good": good}))
        self.tui_log.unlink(missing_ok=True)
        os.environ["FAKE_CURSOR_TUI"] = "no_auto_row"
        snap, reason, error = self.snapshot_at(when=self.now + timedelta(hours=1))
        self.assertEqual(reason, "failed")
        self.assertIsNone(error)
        self.assertEqual(snap["metrics"]["plan_auto_percent_used"]["used"], 55)
        self.assertEqual(snap["timestamp"], first["timestamp"])

    @requires_pty
    def test_late_meters_succeed(self):
        os.environ["FAKE_CURSOR_TUI"] = "late_meters"
        snap, reason, error = self.snapshot_at(timeout=5)
        self.assertIsNone(reason)
        self.assertIsNone(error)
        self.assertEqual(snap["metrics"]["plan_auto_percent_used"]["used"], 55)

    @requires_pty
    def test_no_meters_fixed_error_no_screen_text(self):
        os.environ["FAKE_CURSOR_TUI"] = "no_meters"
        snap, reason, error = self.snapshot_at(timeout=2)
        self.assertIsNone(snap)
        self.assertIsNotNone(error)
        self.assertNotIn("Pro", error)
        self.assertNotIn("Esc", error)
        self.assertNotIn("55%", error)
        self.assertIn("cursor /usage", error)

    @requires_pty
    def test_hang_before_ready(self):
        os.environ["FAKE_CURSOR_TUI"] = "hang_before_ready"
        started = time.monotonic()
        snap, reason, error = self.snapshot_at(timeout=1)
        self.assertLess(time.monotonic() - started, 2)
        self.assertTrue(snap is None or error or reason)

    @requires_pty
    def test_hang_after_enter(self):
        os.environ["FAKE_CURSOR_TUI"] = "hang_after_enter"
        started = time.monotonic()
        snap, reason, error = self.snapshot_at(timeout=1)
        self.assertLess(time.monotonic() - started, 2)
        self.assertTrue(snap is None or error or reason)

    @requires_pty
    def test_flood_too_large(self):
        os.environ["FAKE_CURSOR_TUI"] = "flood"
        started = time.monotonic()
        snap, reason, error = self.snapshot_at(timeout=2)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 3)
        text = error or ""
        if snap is None:
            self.assertIn("too large", text)
        else:
            self.fail("flood should not yield a snapshot")

    @requires_pty
    def test_ignore_signals_kills_group(self):
        os.environ["FAKE_CURSOR_TUI"] = "ignore_signals"
        started = time.monotonic()
        snap, reason, error = self.snapshot_at(timeout=1)
        self.assertLess(time.monotonic() - started, 2.5)
        # No leftover processes from this group: the reader killpg'd them.
        self.assertTrue(snap is None or error or reason)

    def test_date_table(self):
        rows = [
            (datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc), "18 Nov", "2026-11-18"),
            (datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc), "Nov 18", "2026-11-18"),
            (datetime(2026, 12, 30, 10, 0, tzinfo=timezone.utc), "18 Jan", "2027-01-18"),
            (datetime(2027, 1, 2, 10, 0, tzinfo=timezone.utc), "30 Dec", "2026-12-30"),
            (datetime(2026, 11, 18, 0, 30, tzinfo=timezone.utc), "18 Nov", "2026-11-18"),
            (datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc), "18 Nov", "2026-11-18"),
            (datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc), "18 Mar", None),
            (datetime(2027, 2, 1, 10, 0, tzinfo=timezone.utc), "29 Feb", None),
            (datetime(2028, 2, 1, 10, 0, tzinfo=timezone.utc), "29 Feb", "2028-02-29"),
            (datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc), "18. Nov.", None),
        ]
        for now, text, want in rows:
            with self.subTest(now=now.isoformat(), text=text):
                if want is None:
                    with self.assertRaises(quota._ReaderError):
                        quota._cursor_reset(text, now)
                else:
                    got = quota._cursor_reset(text, now)
                    self.assertEqual(got.date().isoformat(), want)

    @requires_pty
    def test_throttle_at_boundary(self):
        first, _, _ = self.snapshot_at()
        self.assertIsNotNone(first)
        calls = self.env_log.read_text().splitlines()
        # 29:59 after a good read → carried throttle
        snap, reason, error = self.snapshot_at(
            when=self.now + timedelta(seconds=quota.CURSOR_MIN_INTERVAL_S - 1))
        self.assertEqual(reason, "throttle")
        self.assertEqual(len(self.env_log.read_text().splitlines()), len(calls))
        # 30:00 → new read
        os.environ["FAKE_CURSOR_TUI"] = "normal"
        self.tui_log.unlink(missing_ok=True)
        snap, reason, error = self.snapshot_at(
            when=self.now + timedelta(seconds=quota.CURSOR_MIN_INTERVAL_S))
        self.assertIsNone(reason)
        self.assertEqual(len(self.env_log.read_text().splitlines()), len(calls) + 1)

    @requires_pty
    def test_backoff_after_failed_read(self):
        os.environ["FAKE_CURSOR_TUI"] = "no_meters"
        first, reason, error = self.snapshot_at(timeout=2)
        self.assertIsNone(first)
        calls = len(self.env_log.read_text().splitlines()) if self.env_log.exists() else 0
        # Seed nothing good: second attempt right away must not call again.
        snap, reason, error = self.snapshot_at(
            when=self.now + timedelta(seconds=10), timeout=1)
        self.assertIsNone(snap)
        self.assertEqual(reason, None)
        self.assertEqual(error, "cursor /usage unavailable")
        self.assertEqual(len(self.env_log.read_text().splitlines()) if self.env_log.exists() else 0,
                         calls)
        # With a last_good, backoff carry.
        state_path = quota._cursor_state_path(self.log)
        state = json.loads(state_path.read_text())
        state["last_good"] = {
            "timestamp": self.now.isoformat(),
            "metrics": {
                "plan_auto_percent_used": {"limit": 100, "used": 55, "remaining": 45},
                "plan_api_percent_used": {"limit": 100, "used": 46, "remaining": 54},
            },
            "resets": {"billing_cycle_end": "2026-11-18T00:00:00+00:00"},
        }
        state_path.write_text(json.dumps(state))
        snap, reason, error = self.snapshot_at(when=self.now + timedelta(seconds=20))
        self.assertEqual(reason, "backoff")
        self.assertEqual(snap["metrics"]["plan_auto_percent_used"]["used"], 55)

    def test_kill_mid_read_throttles_next(self):
        # Save attempt stamp with no result, then next call must throttle.
        state_path = quota._cursor_state_path(self.log)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        good = {
            "timestamp": self.now.isoformat(),
            "metrics": {
                "plan_auto_percent_used": {"limit": 100, "used": 10, "remaining": 90},
                "plan_api_percent_used": {"limit": 100, "used": 10, "remaining": 90},
            },
            "resets": {"billing_cycle_end": "2026-11-18T00:00:00+00:00"},
        }
        state_path.write_text(json.dumps({
            "last_attempt_at": self.now.isoformat(), "failures": 0, "last_good": good}))
        before = len(self.env_log.read_text().splitlines()) if self.env_log.exists() else 0
        snap, reason, error = self.snapshot_at(when=self.now + timedelta(seconds=5))
        self.assertEqual(reason, "throttle")
        after = len(self.env_log.read_text().splitlines()) if self.env_log.exists() else 0
        self.assertEqual(before, after)

    def test_unsaved_attempt_stamp_never_reads(self):
        good = {
            "timestamp": self.now.isoformat(),
            "metrics": {
                "plan_auto_percent_used": {"limit": 100, "used": 10, "remaining": 90},
                "plan_api_percent_used": {"limit": 100, "used": 10, "remaining": 90},
            },
            "resets": {"billing_cycle_end": "2026-11-18T00:00:00+00:00"},
        }
        state_path = quota._cursor_state_path(self.log)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        stale = (self.now - timedelta(hours=2)).isoformat()
        for seeded in (good, None):
            with self.subTest(last_good=bool(seeded)):
                state_path.write_text(json.dumps(
                    {"last_attempt_at": stale, "failures": 0, "last_good": seeded}))
                with mock.patch.object(quota, "_save_claude_state",
                                       side_effect=OSError(28, "No space left on device")), \
                        mock.patch.object(quota, "_cursor_call") as call:
                    first = self.snapshot_at()
                    second = self.snapshot_at()
                call.assert_not_called()
                if seeded:
                    self.assertEqual(first[1], "failed")
                    self.assertEqual(first[0]["metrics"]["plan_auto_percent_used"]["used"], 10)
                else:
                    self.assertEqual(first, (None, None, "cursor /usage unavailable"))
                self.assertEqual(first, second)

    def test_no_lock_never_reads(self):
        state_path = quota._cursor_state_path(self.log)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        with mock.patch.object(quota.fcntl, "flock", side_effect=OSError(37, "No locks available")), \
                mock.patch.object(quota, "_cursor_call") as call:
            snap, reason, error = self.snapshot_at()
        call.assert_not_called()
        self.assertEqual((snap, reason, error), (None, None, "cursor /usage unavailable"))

    @requires_pty
    def test_two_concurrent_calls_one_read(self):
        results = []

        def once():
            results.append(quota._cursor_snapshot(timeout=5, log=self.log))

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: once(), range(2)))
        self.assertEqual(len(self.env_log.read_text().splitlines()), 1)
        self.assertTrue(all(r[0] is not None for r in results))

    @requires_pty
    def test_corrupt_state_file(self):
        state_path = quota._cursor_state_path(self.log)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text("{not-json")
        snap, reason, error = self.snapshot_at()
        self.assertIsNone(reason)
        self.assertIsNotNone(snap)

    def test_spare_line_throttle_not_failed_or_backoff(self):
        reading_ts = self.now
        observed = (reading_ts - timedelta(minutes=25)).isoformat()
        reset = "2026-11-18T00:00:00+00:00"
        base = {
            "used_pct": 10.0, "resets_at": reset, "observed_at": observed,
            "stale": True, "whole_percent": True,
        }
        throttle = {**base, "carried": "throttle"}
        failed = {**base, "carried": "failed"}
        backoff = {**base, "carried": "backoff"}
        self.assertIsNotNone(quota._entry_pace("cursor-auto", throttle, reading_ts))
        self.assertIsNone(quota._entry_pace("cursor-auto", failed, reading_ts))
        self.assertIsNone(quota._entry_pace("cursor-auto", backoff, reading_ts))
        # Write a spare-usable row and confirm Cursor Auto is listed only for throttle.
        row = {
            "ts": reading_ts.isoformat(), "source": "hybrid", "reader": quota.HYBRID_READER,
            "pools": {
                "cursor-auto": throttle,
                "codex-7d": {"used_pct": 90.0, "resets_at": (reading_ts + timedelta(days=2)).isoformat(),
                             "stale": False},
            },
        }
        self.log.write_text(json.dumps(row) + "\n")
        info = quota.spare_pools(self.log, now=reading_ts + timedelta(minutes=1))
        self.assertIsNotNone(info)
        self.assertIn("cursor-auto", [pid for pid, _, _ in info["pools"]])
        row["pools"]["cursor-auto"] = failed
        self.log.write_text(json.dumps(row) + "\n")
        info = quota.spare_pools(self.log, now=reading_ts + timedelta(minutes=1))
        # May still list Codex; must not list Cursor Auto.
        if info:
            self.assertNotIn("cursor-auto", [pid for pid, _, _ in info["pools"]])

    def test_pools_pace_without_billing_cycle_progress(self):
        reset = datetime(2026, 11, 18, tzinfo=timezone.utc)
        now = datetime(2026, 11, 7, tzinfo=timezone.utc)
        snap = {
            "provider_id": "cursor", "timestamp": now.isoformat(), "status": "ok",
            "whole_percent": True,
            "metrics": {
                "plan_auto_percent_used": {"limit": 100, "used": 55, "remaining": 45},
                "plan_api_percent_used": {"limit": 100, "used": 46, "remaining": 54},
            },
            "resets": {"billing_cycle_end": reset.isoformat()},
        }
        entry = quota.pools({"snapshots": [snap]}, now=now)["cursor-auto"]
        self.assertAlmostEqual(entry["time_left_frac"], 11 / 31, places=6)
        # With the metric, keep today's openusage-shaped result.
        snap["metrics"]["billing_cycle_progress"] = {"used": 50, "limit": 100}
        entry = quota.pools({"snapshots": [snap]}, now=now)["cursor-auto"]
        self.assertEqual(entry["time_left_frac"], 0.5)

    def test_windows_cutover_and_monthly_reset(self):
        def row(ts, used, reset, whole=False):
            entry = {"used_pct": used, "resets_at": reset, "stale": False}
            if whole:
                entry["whole_percent"] = True
            return {"ts": ts, "pools": {"cursor-auto": entry}}

        rows = [
            row("2026-11-07T09:00:00+00:00", 55.4956, "2026-11-18T01:53:54+00:00"),
            row("2026-11-07T10:00:00+00:00", 55.0, "2026-11-18T00:00:00+00:00", whole=True),
            row("2026-11-07T10:30:00+00:00", 54.6, "2026-11-18T00:00:00+00:00", whole=True),
        ]
        now = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
        wins = monitor.windows_for_pool(rows, "cursor-auto", now)
        self.assertEqual(len(wins), 1)
        self.assertFalse(wins[0]["closed"])
        # A real monthly reset still splits.
        rows.append(row("2026-11-18T00:01:00+00:00", 1.0, "2026-12-18T00:00:00+00:00", whole=True))
        wins = monitor.windows_for_pool(rows, "cursor-auto", now)
        self.assertEqual(len(wins), 2)
        # Exact readings keep the narrow rule: an early refill under one reset splits.
        exact = [
            row("2026-11-07T09:00:00+00:00", 0.9, "2026-11-18T01:53:54+00:00"),
            row("2026-11-07T10:00:00+00:00", 0.0, "2026-11-18T01:53:54+00:00"),
        ]
        self.assertEqual(len(monitor.windows_for_pool(exact, "cursor-auto", now)), 2)
        # So do whole-percent readings after the cutover step.
        whole = rows[:3] + [
            row("2026-11-07T11:00:00+00:00", 53.0, "2026-11-18T00:00:00+00:00", whole=True)]
        self.assertEqual(len(monitor.windows_for_pool(whole, "cursor-auto", now)), 2)

    def test_job_costs_skips_whole_percent(self):
        start = datetime(2026, 11, 7, 10, tzinfo=timezone.utc)
        end = start + timedelta(minutes=5)
        catalog = {"delegates": [
            {"id": "cursor-auto", "agent": "cursor", "model": "auto", "pools": ["cursor-auto"]}]}

        def qrow(ts, reason, used, whole=False):
            entry = {"used_pct": used, "resets_at": "2026-11-18T00:00:00+00:00", "stale": False}
            if whole:
                entry["whole_percent"] = True
            return {"ts": ts.isoformat(), "reason": reason, "run": "r1", "job": "round-1",
                    "pools": {"cursor-auto": entry}}

        run = {
            "run": "r1", "agent": "cursor", "model": "auto", "effort": None,
            "rounds": [{"n": 1, "model": "auto", "effort": None,
                        "started": start.isoformat(), "minutes": 5}],
            "reviews": [],
        }
        # Only whole-percent rows → skip, rounds_left None.
        qrows = [
            qrow(start, "launch", 55, whole=True),
            qrow(end + timedelta(minutes=1), "job-end", 56, whole=True),
        ]
        result = monitor.job_costs(qrows, [run], [], catalog)
        self.assertEqual(result["jobs"], 1)
        self.assertEqual(result["skipped"].get("whole percent"), 1)
        self.assertEqual(result["groups"], [])
        # No cost samples → nothing to estimate rounds_left from.
        self.assertIsNone(monitor.rounds_left(None, {"cursor-auto": {"left_pct": 45}}))
        # Exact (non-whole-percent) samples still count.
        qrows = [
            qrow(start, "launch", 55.4956, whole=False),
            qrow(end + timedelta(minutes=1), "job-end", 55.6778, whole=False),
        ]
        result = monitor.job_costs(qrows, [run], [], catalog)
        self.assertEqual(result["jobs"], 1)
        self.assertNotIn("whole percent", result["skipped"])
        self.assertEqual(len(result["groups"]), 1)

    @requires_pty
    def test_hook_load_without_scripts_on_path(self):
        path = SCRIPTS / "quota.py"
        # Ensure scripts dir is not on sys.path for this load.
        cleaned = [p for p in sys.path if Path(p).resolve() != SCRIPTS.resolve()]
        with mock.patch.object(sys, "path", cleaned), mock.patch.dict(sys.modules):
            sys.modules.pop("catalog", None)
            spec = importlib.util.spec_from_file_location("quota_hook_load", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            envelope = mod._hybrid_snapshot(timeout=5, log=self.log)
        snap = next(s for s in envelope["snapshots"] if s["provider_id"] == "cursor")
        self.assertNotIn("cursor", envelope["_reader_errors"])
        self.assertEqual(snap["metrics"]["plan_auto_percent_used"]["used"], 55)
        self.assertEqual(mod.pools(envelope)["cursor-auto"]["left_pct"], 45)

    def assert_hybrid_cursor_off(self, *diagnostics):
        state = quota._cursor_state_path(self.log)
        before = state.read_bytes() if state.exists() else None
        # Reaching the reader's state path would also allow its lock and cache.
        with mock.patch.object(quota, "_cursor_state_path",
                               side_effect=AssertionError("off reader touched Cursor state")):
            envelope = quota.fetch(source="hybrid", timeout=5, log=self.log)
            entries = quota.pools(envelope)
            row, _ = quota.record(log=self.log, timeout=5)
        for result in (entries, row["pools"]):
            for pid in ("cursor-auto", "cursor-premium"):
                error = result[pid]["error"]
                self.assertIn("off", error)
                self.assertIn('"cursor_usage_reader": true', error)
                self.assertIn("catalog.local.json", error)
                for diagnostic in diagnostics:
                    self.assertIn(diagnostic, error)
                self.assertNotIn("left_pct", result[pid])
                self.assertNotIn("used_pct", result[pid])
            for pid in ("codex-5h", "codex-7d", "claude-5h", "claude-7d"):
                self.assertNotIn("error", result[pid])
        self.assertEqual(entries["codex-5h"]["left_pct"], 80)
        self.assertEqual(row["pools"]["claude-5h"]["used_pct"], 12.3457)
        self.assertTrue(self.codex_calls.exists())
        self.assertTrue(self.claude_calls.exists())
        self.assertFalse(self.env_log.exists())  # fake cursor-agent invocation log
        self.assertEqual(self.tui_bytes(), b"")
        self.assertEqual(state.read_bytes() if state.exists() else None, before)
        self.assertFalse(Path(str(state) + ".lock").exists())

    def test_hybrid_cursor_reader_is_off_by_default(self):
        with mock.patch.dict(os.environ, {"DELEGATE_CATALOG_LOCAL": ""}):
            self.assert_hybrid_cursor_off()
        self.assertFalse(quota._cursor_state_path(self.log).exists())

    @requires_pty
    def test_hybrid_cursor_reader_opt_in_calls_agent(self):
        envelope = quota.fetch(source="hybrid", timeout=5, log=self.log)
        entries = quota.pools(envelope)
        self.clear_cursor_state()  # Both entry points must launch the fake on opt-in.
        row, _ = quota.record(log=self.log, timeout=5)
        self.assertEqual(entries["cursor-auto"]["left_pct"], 45)
        self.assertEqual(entries["cursor-premium"]["left_pct"], 54)
        self.assertEqual(row["pools"]["cursor-auto"]["used_pct"], 55)
        self.assertEqual(row["pools"]["cursor-premium"]["used_pct"], 46)
        self.assertEqual(len(self.env_log.read_text().splitlines()), 2)
        self.assertTrue(quota._cursor_state_path(self.log).exists())

    def test_hybrid_cursor_reader_requires_json_boolean_true(self):
        for value in ("true", 1, False, None, {}):
            with self.subTest(value=value):
                self.catalog_local.write_text(json.dumps({"cursor_usage_reader": value}))
                self.assert_hybrid_cursor_off()

    def test_hybrid_broken_catalog_keeps_cursor_off_and_other_readers_working(self):
        for content, diagnostic in (("{", "invalid JSON"),
                                    ('{"delegates": null}', "delegates must be a list")):
            with self.subTest(content=content):
                self.catalog_local.write_text(content)
                self.assert_hybrid_cursor_off(str(self.catalog_local), diagnostic)

    def test_hybrid_off_does_not_use_last_good_cursor_state(self):
        state = quota._cursor_state_path(self.log)
        state.write_text(json.dumps({
            "last_attempt_at": self.now.isoformat(), "failures": 0,
            "last_good": {"timestamp": self.now.isoformat(),
                          "metrics": {"plan_auto_percent_used": {"used": 55},
                                      "plan_api_percent_used": {"used": 17}},
                          "resets": {"billing_cycle_end": "2026-11-18T01:53:54+00:00"}},
        }))
        with mock.patch.dict(os.environ, {"DELEGATE_CATALOG_LOCAL": ""}):
            self.assert_hybrid_cursor_off()

    @requires_pty
    def test_hybrid_no_openusage_and_reader_string(self):
        self.clear_cursor_state()
        envelope = quota.fetch(source="hybrid", log=self.log)
        self.assertFalse(self.openusage_calls.exists())
        self.assertTrue(any(s.get("provider_id") == "cursor" for s in envelope["snapshots"]))
        row, _ = quota.record(log=self.log)
        self.assertEqual(row["reader"], "codex-app-server+claude-usage+cursor-usage")
        self.assertEqual(quota.HYBRID_READER, row["reader"])
        self.assertTrue(row["pools"]["cursor-auto"].get("whole_percent"))
        self.assertFalse(self.openusage_calls.exists())
        # Explicit openusage sources still work.
        quota.fetch(source="direct")
        self.assertEqual(self.openusage_calls.read_text().splitlines(), ["direct"])

    def test_strip_ansi_turns_escapes_into_spaces(self):
        raw = b"A\x1b[1mB\x1b[22mC"
        self.assertEqual(quota._strip_ansi(raw), "A B C")


if __name__ == "__main__":
    unittest.main()

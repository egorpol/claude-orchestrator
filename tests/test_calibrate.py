"""Tests for calibrate.py: snapshot pairs, NNLS, Codex rollouts and `cost --calibrate`."""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "delegate" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import calibrate  # noqa: E402
import cost  # noqa: E402

T0 = datetime(2026, 10, 15, 10, 0, tzinfo=timezone.utc)
OPUS = "claude-opus-5-5"
PRICES = {"models": {OPUS: {"input": 4.0, "output": 20.0, "cache_read": 0.2,
                            "cache_write_5m": 5.0, "cache_write_1h": 8.0}}}
SCALE = 2.5  # synthetic pool: points per list-price USD


def _entry(used, reset, observed=None, **extra):
    return {"used_pct": used, "resets_at": reset.isoformat() if reset else None,
            "observed_at": observed.isoformat() if observed else None, "stale": False, **extra}


def _row(ts, **pools):
    """A quota row; entries without observed_at were observed at the row's time."""
    for entry in pools.values():
        if "used_pct" in entry and entry["observed_at"] is None:
            entry["observed_at"] = ts.isoformat()
    return {"ts": ts.isoformat(), "source": "direct", "reason": "timer", "session": None, "run": None,
            "pools": {k.replace("_", "-"): v for k, v in pools.items()}}


def _assistant(mid, ts, cache_read=0, write_1h=0, write_5m=0, output=0, inp=10, model=OPUS):
    return {"type": "assistant", "timestamp": ts.isoformat().replace("+00:00", "Z"),
            "message": {"id": mid, "model": model, "usage": {
                "input_tokens": inp, "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": write_1h + write_5m,
                "cache_creation": {"ephemeral_1h_input_tokens": write_1h, "ephemeral_5m_input_tokens": write_5m},
                "output_tokens": output}}}


def _token_count(ts, total, last=None):
    keys = ("input_tokens", "cached_input_tokens", "output_tokens")
    usage = lambda v: dict(zip(keys, v))
    return {"timestamp": ts.isoformat().replace("+00:00", "Z"), "type": "event_msg",
            "payload": {"type": "token_count", "info": {"total_token_usage": usage(total),
                                                        "last_token_usage": usage(last or total)}}}


def _write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


class TestSnapshotPairs(unittest.TestCase):
    def test_pairs_within_a_window_tolerate_reset_jitter_and_offsets(self):
        reset = T0 + timedelta(hours=3)
        rows = [
            _row(T0, claude_5h=_entry(10, reset)),
            # Claude's resets_at moves by seconds between polls; observed_at carries an offset.
            _row(T0 + timedelta(minutes=10), claude_5h=_entry(
                13, reset + timedelta(seconds=20),
                (T0 + timedelta(minutes=10)).astimezone(timezone(timedelta(hours=2))))),
        ]
        pairs, stats = calibrate.snapshot_pairs(rows, "claude-5h")
        self.assertEqual(stats, {})
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["rise"], 3)
        self.assertEqual(pairs[0]["t1"] - pairs[0]["t0"], timedelta(minutes=10))

    def test_error_stale_and_new_window_break_pairs(self):
        reset = T0 + timedelta(hours=3)
        later = T0 + timedelta(hours=8)
        rows = [
            _row(T0, claude_5h=_entry(10, reset)),
            {"ts": (T0 + timedelta(minutes=5)).isoformat(), "error": "openusage timed out"},
            _row(T0 + timedelta(minutes=10), claude_5h=_entry(12, reset)),
            _row(T0 + timedelta(minutes=20), claude_5h=_entry(13, reset, stale=True)),
            _row(T0 + timedelta(minutes=30), claude_5h=_entry(14, reset)),
            _row(T0 + timedelta(minutes=40), claude_5h={"error": "metric usage_five_hour missing"}),
            _row(T0 + timedelta(minutes=50), claude_5h=_entry(15, reset)),
            _row(T0 + timedelta(minutes=60), claude_5h=_entry(2, later)),  # another window
            _row(T0 + timedelta(minutes=70), claude_5h=_entry(4, later)),
        ]
        pairs, stats = calibrate.snapshot_pairs(rows, "claude-5h")
        self.assertEqual([p["rise"] for p in pairs], [2])
        self.assertEqual(stats, {"error": 2, "stale": 1})

    def test_idle_reading_pairs_only_with_a_window_opened_after_it(self):
        t_idle = T0
        opened_after = T0 + timedelta(minutes=5) + timedelta(hours=5)
        opened_before = T0 - timedelta(minutes=30) + timedelta(hours=5)
        rows = [_row(t_idle, claude_5h=_entry(0, None)),
                _row(T0 + timedelta(minutes=10), claude_5h=_entry(6, opened_after))]
        pairs, _ = calibrate.snapshot_pairs(rows, "claude-5h")
        self.assertEqual([p["rise"] for p in pairs], [6])
        rows[1] = _row(T0 + timedelta(minutes=10), claude_5h=_entry(6, opened_before))
        pairs, _ = calibrate.snapshot_pairs(rows, "claude-5h")
        self.assertEqual(pairs, [])

    def test_repeated_provider_reading_and_drops_are_not_pairs(self):
        reset = T0 + timedelta(hours=3)
        rows = [_row(T0, claude_5h=_entry(10, reset)),
                _row(T0 + timedelta(minutes=1), claude_5h=_entry(10, reset, T0)),  # same observed_at
                _row(T0 + timedelta(minutes=10), claude_5h=_entry(8, reset, T0 + timedelta(minutes=10)))]
        pairs, stats = calibrate.snapshot_pairs(rows, "claude-5h")
        self.assertEqual(pairs, [])
        self.assertEqual(stats, {"negative": 1})


class TestNNLS(unittest.TestCase):
    def test_recovers_exact_weights(self):
        X = [[1, 0, 2], [0, 1, 1], [3, 1, 0], [1, 2, 5], [2, 2, 2]]
        w = [0.5, 3.0, 1.25]
        y = [sum(a * b for a, b in zip(r, w)) for r in X]
        for got, want in zip(calibrate.nnls(X, y), w):
            self.assertAlmostEqual(got, want, places=9)

    def test_negative_coefficient_is_clamped_and_collinear_columns_survive(self):
        X = [[1, 1], [2, 1], [3, 1], [4, 1]]
        y = [2 * r[0] - 1 for r in X]
        w = calibrate.nnls(X, y)
        self.assertEqual(w[1], 0.0)
        self.assertGreater(w[0], 0)
        w = calibrate.nnls([[1, 1], [2, 2]], [2, 4])  # identical columns
        self.assertAlmostEqual(w[0] + w[1], 2.0, places=9)

    def test_kkt_conditions_hold(self):
        import random
        rnd = random.Random(3)
        for _ in range(50):
            X = [[rnd.uniform(0, 1) for _ in range(3)] for _ in range(6)]
            y = [rnd.uniform(-1, 1) for _ in range(6)]
            w = calibrate.nnls(X, y)
            resid = [v - sum(a * b for a, b in zip(r, w)) for r, v in zip(X, y)]
            grad = [sum(r[j] * e for r, e in zip(X, resid)) for j in range(3)]
            for wj, gj in zip(w, grad):
                self.assertGreaterEqual(wj, 0)
                self.assertLess(gj, 1e-8)
                if wj > 0:
                    self.assertAlmostEqual(gj, 0, places=8)
        w = calibrate.nnls([[1, 2, 3]], [6])  # underdetermined
        self.assertAlmostEqual(sum(a * b for a, b in zip([1, 2, 3], w)), 6, places=9)


class TestCodexEvents(unittest.TestCase):
    def test_total_deltas_drop_repeats_and_split_cached_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            rows = [
                {"type": "turn_context", "payload": {"model": "gpt-6.1-sol"}},
                _token_count(T0 + timedelta(minutes=1), (1000, 600, 50)),
                _token_count(T0 + timedelta(minutes=1, seconds=1), (1000, 600, 50)),  # repeated event
                _token_count(T0 + timedelta(minutes=2), (2500, 1600, 80)),
                _token_count(T0 + timedelta(minutes=3), (300, 100, 10), last=(300, 100, 10)),  # restart
            ]
            _write_jsonl(home / "sessions/2026/10/15/rollout-a.jsonl", rows)
            _write_jsonl(home / "archived_sessions/rollout-a.jsonl", rows)  # archived copy
            events = calibrate.codex_events(home)
        self.assertEqual([e[2] for e in events], [
            {"uncached_input": 400, "cached_input": 600, "output": 50},
            {"uncached_input": 500, "cached_input": 1000, "output": 30},
            {"uncached_input": 200, "cached_input": 100, "output": 10},
        ])
        self.assertEqual({e[1] for e in events}, {"gpt-6.1-sol"})

    def test_resumed_file_missing_totals_and_larger_archived_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            rows = [
                # a resumed thread: the running total includes earlier rounds' files
                _token_count(T0, (5000, 4000, 300), last=(1000, 800, 20)),
                {"timestamp": "2026-10-15T10:00:30Z", "type": "event_msg",
                 "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": None}}}},
                _token_count(T0 + timedelta(minutes=1), (5500, 4300, 330)),
            ]
            _write_jsonl(home / "sessions/rollout-r.jsonl", rows[:1])
            _write_jsonl(home / "archived_sessions/rollout-r.jsonl", rows)
            events = calibrate.codex_events(home)
        self.assertEqual([e[2] for e in events], [
            {"uncached_input": 200, "cached_input": 800, "output": 20},
            {"uncached_input": 200, "cached_input": 300, "output": 30},
        ])


def _synthetic(tmp):
    """Quota log, Claude transcripts and Codex rollouts with known weights, no noise."""
    tmp = Path(tmp)
    reset5 = T0 + timedelta(hours=4)
    reset7 = T0 + timedelta(days=4)
    used = {"claude-5h": 1.0, "claude-7d": 20.0, "codex-5h": 3.0, "codex-7d": 30.0}
    w_claude = {k: SCALE * PRICES["models"][OPUS][k] for k in calibrate.CLAUDE_CLASSES}  # per Mtok
    w_codex = {"uncached_input": 4.0, "cached_input": 1.0, "output": 150.0}
    main, sub, rollout = [], [], [{"type": "turn_context", "payload": {"model": "gpt-6.1-sol"}}]
    codex_total = [0, 0, 0]

    def reading(t):
        # resets_at jitters by seconds, as Claude's does
        jitter = timedelta(seconds=(int(t.timestamp()) // 600) % 3 * 10)
        return _row(t, claude_5h=_entry(used["claude-5h"], reset5 + jitter, t),
                    claude_7d=_entry(used["claude-7d"], reset7, t),
                    codex_5h=_entry(used["codex-5h"], reset5, t),
                    codex_7d=_entry(used["codex-7d"], reset7, t))

    rows = [reading(T0)]
    for i in range(30):
        start = T0 + timedelta(minutes=10 * i)
        calls = [dict(cache_read=(i % 5 + 1) * 1_000_000, write_1h=((i * 7) % 4 + 1) * 50_000,
                      output=((i * 3) % 5 + 1) * 20_000)]
        if i % 3 == 0:
            calls.append(dict(cache_read=400_000, output=5_000))
        for k, c in enumerate(calls):
            ts = start + timedelta(minutes=2 + 3 * k)
            msg = _assistant(f"msg-{i}-{k}", ts, **c)
            (sub if k else main).append(msg)
            tokens = {"input": 10, "cache_read": c["cache_read"], "cache_write_5m": 0,
                      "cache_write_1h": c.get("write_1h", 0), "output": c["output"]}
            rise = sum(w_claude[cls] * n / 1e6 for cls, n in tokens.items())
            used["claude-5h"] += rise
            used["claude-7d"] += rise / 8
        if i % 2 == 0:
            delta = (((i % 4) + 1) * 300_000, ((i % 3) + 1) * 200_000, ((i % 5) + 1) * 2_000)
            codex_total = [a + b for a, b in zip(codex_total, (delta[0] + delta[1], delta[1], delta[2]))]
            rollout.append(_token_count(start + timedelta(minutes=4), tuple(codex_total)))
            rise = sum(w * n / 1e6 for w, n in zip(w_codex.values(), delta))
            used["codex-5h"] += rise
            used["codex-7d"] += rise / 10
        rows.append(reading(start + timedelta(minutes=10)))
    rows.append(reading(T0 + timedelta(minutes=305)))  # a quiet pair after the last call
    main.append(main[0])  # a duplicate id must count once
    projects = tmp / "claude/projects"
    _write_jsonl(projects / "-tmp-proj/sess.jsonl", main)
    _write_jsonl(projects / "-tmp-proj/sess/subagents/agent-1.jsonl", sub)
    _write_jsonl(tmp / "codex/sessions/2026/10/15/rollout-x.jsonl", rollout)
    log = tmp / "quota.jsonl"
    _write_jsonl(log, rows)
    return log, projects, tmp / "codex", w_claude, w_codex


class TestCalibrate(unittest.TestCase):
    def test_recovers_known_weights_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            log, projects, codex_home, w_claude, w_codex = _synthetic(tmp)
            result = calibrate.calibrate(log, projects, codex_home, PRICES)
            text = calibrate.render(result)
            json.dumps(result)  # the --json output must serialise
        fits = {f["pool"]: f for f in result["pools"]}

        c5 = fits["claude-5h"]
        self.assertEqual((c5["pairs"], c5["active_pairs"], c5["calls"]), (31, 30, 40))
        self.assertAlmostEqual(c5["headline"]["per_unit"], SCALE, places=6)
        self.assertAlmostEqual(c5["headline"]["sum_ratio"], SCALE, places=6)
        got = {c["class"]: c["per_mtok"] for c in c5["classes"]}
        self.assertEqual(set(got), {"cache_read", "cache_write_1h", "output"})
        for cls, w in got.items():
            self.assertAlmostEqual(w, w_claude[cls], places=5)
            self.assertAlmostEqual(w, next(c["list_implied"] for c in c5["classes"] if c["class"] == cls), places=5)
        # fresh input is far below 1% of USD: not fitted, carried at list price
        self.assertEqual([d["class"] for d in c5["dropped"]], ["input"])
        self.assertAlmostEqual(c5["offset_points"], 40 * 10 * w_claude["input"] / 1e6, places=9)
        self.assertLess(c5["class_fit"]["rmse"], 1e-6)
        self.assertEqual(c5["timing_rmse"][0], min(c5["timing_rmse"].values()))

        c7 = fits["claude-7d"]
        self.assertAlmostEqual(c7["headline"]["per_unit"], SCALE / 8, places=6)

        x5 = fits["codex-5h"]
        self.assertNotIn("headline", x5)
        for c in x5["classes"]:
            self.assertAlmostEqual(c["per_mtok"], w_codex[c["class"]], places=5)
        x7 = fits["codex-7d"]
        self.assertEqual(x7["headline"]["unit"], "codex-5h")
        self.assertAlmostEqual(x7["headline"]["per_unit"], 0.1, places=6)

        self.assertIn("list price: 2.50 points per USD", text)
        self.assertIn("via codex-5h weights: 0.100 points per codex-5h point", text)
        self.assertIn("input (0.0% of USD, 30 pairs)", text)
        self.assertNotIn("against the per-pair fit", text)

    def test_idle_rises_and_since_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            log, projects, codex_home, _, _ = _synthetic(tmp)
            rows = cost.load_jsonl(log)
            last = rows[-1]
            bump = json.loads(json.dumps(last))
            t = T0 + timedelta(minutes=320)
            bump["ts"] = t.isoformat()
            for entry in bump["pools"].values():
                entry["observed_at"] = t.isoformat()
            bump["pools"]["claude-5h"]["used_pct"] += 2  # use from another device
            _write_jsonl(log, rows + [bump])
            fits = {f["pool"]: f for f in calibrate.calibrate(log, projects, codex_home, PRICES)["pools"]}
            self.assertEqual((fits["claude-5h"]["idle_rises"], fits["claude-5h"]["idle_points"]), (1, 2))
            self.assertAlmostEqual(fits["claude-5h"]["headline"]["sum_ratio"], SCALE, places=6)
            since = T0 + timedelta(minutes=150)
            fits = {f["pool"]: f for f in calibrate.calibrate(log, projects, codex_home, PRICES, since)["pools"]}
            self.assertEqual(fits["claude-5h"]["pairs"], 17)

    def test_offset_when_every_class_is_dropped(self):
        tokens = {"input": 0, "cache_read": 0, "cache_write_5m": 0, "cache_write_1h": 0, "output": 50_000}  # $1
        events = [(T0 + timedelta(minutes=5), OPUS, tokens, 1.0, calibrate._class_usd(OPUS, tokens, PRICES))]
        fit = calibrate.fit_pool("claude-5h", [{"t0": T0, "t1": T0 + timedelta(minutes=10), "rise": 2.5}],
                                 events, PRICES)
        self.assertNotIn("classes", fit)
        self.assertAlmostEqual(fit["headline"]["per_unit"], 2.5, places=9)
        self.assertAlmostEqual(fit["offset_points"], 2.5, places=9)

    def test_timing_scores_every_lag_on_the_same_pairs(self):
        # A +1 min shift moves the first call into pair 2 and the second out of
        # both pairs; scoring only pairs with calls would rate that shift perfect.
        tokens = {"input": 0, "cache_read": 0, "cache_write_5m": 0, "cache_write_1h": 0, "output": 50_000}
        cusd = calibrate._class_usd(OPUS, tokens, PRICES)
        events = [(T0 + timedelta(seconds=30), OPUS, tokens, 1.0, cusd),
                  (T0 + timedelta(seconds=90), OPUS, tokens, 1.0, cusd)]
        pairs = [{"t0": T0, "t1": T0 + timedelta(minutes=1), "rise": 10},
                 {"t0": T0 + timedelta(minutes=1), "t1": T0 + timedelta(minutes=2), "rise": 1}]
        timing = calibrate.fit_pool("claude-5h", pairs, events, PRICES)["timing_rmse"]
        self.assertAlmostEqual(timing[0], 4.5, places=9)
        self.assertAlmostEqual(timing[1], (100 / 2) ** 0.5, places=9)

    def test_empty_log_reports_nothing_to_fit(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = calibrate.calibrate(Path(tmp) / "missing.jsonl", Path(tmp), Path(tmp), PRICES)
        self.assertIn("claude-5h: 0 pairs, none with calls; nothing to fit", calibrate.render(result))

    def test_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            log, projects, codex_home, _, _ = _synthetic(tmp)
            env = {"CLAUDE_CONFIG_DIR": str(projects.parent), "CODEX_HOME": str(codex_home)}
            out = io.StringIO()
            with mock.patch.dict(os.environ, env), redirect_stdout(out):
                cost.main(["--calibrate", "--quota-log", str(log), "--json"])
            fits = {f["pool"]: f for f in json.loads(out.getvalue())["pools"]}
            self.assertEqual(fits["claude-5h"]["calls"], 40)
            with self.assertRaises(SystemExit):
                cost.main(["--calibrate", "--by-mode"])


if __name__ == "__main__":
    unittest.main()

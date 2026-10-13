"""Tests for cost.py run-log and session transcript summarisation."""
import json
import io
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "delegate" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cost  # noqa: E402


def _run_record(**kwargs):
    base = {
        "v": 1,
        "ts": "2026-10-09T12:00:00Z",
        "repo": "toy",
        "repo_path": "/tmp/toy",
        "run": "codex-1",
        "agent": "codex",
        "model": "gpt-6.1-sol",
        "effort": "xhigh",
        "mode": "workspace-write",
        "links": [],
        "base": "abc",
        "outcome": "applied",
        "files": 1,
        "insertions": 2,
        "deletions": 0,
        "rounds": [{
            "n": 1, "model": "gpt-6.1-sol", "effort": "xhigh",
            "started": "2026-10-09T12:00:00Z", "minutes": 1.5,
            "exit": 0, "verify": 0,
            "usage": {"input": 100, "cached_input": 50, "cache_write": 10,
                      "output": 20, "reasoning_output": 5},
        }],
        "reviews": [],
    }
    base.update(kwargs)
    return base


class TestRunLog(unittest.TestCase):
    def test_summarise_synthetic_log(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            path = Path(f.name)
            f.write(json.dumps(_run_record()) + "\n")
            f.write(json.dumps(_run_record(
                run="cursor-1", agent="cursor", model="auto", outcome="discarded",
                rounds=[{
                    "n": 1, "model": "auto", "effort": None,
                    "started": "2026-10-09T13:00:00Z", "minutes": 0.5,
                    "exit": 0, "verify": 0,
                    "usage": {"input": 10, "cached_input": 200, "cache_write": 0,
                              "output": 5, "reasoning_output": None},
                }],
            )) + "\n")
            f.write(json.dumps(_run_record(
                ts="2026-09-01T00:00:00Z", run="old",
            )) + "\n")
        try:
            rows = cost.load_run_log(path, since="2026-10-01")
            self.assertEqual(len(rows), 2)
            groups = cost.summarise_runs(rows)
            self.assertEqual(groups[("codex", "gpt-6.1-sol")]["runs"], 1)
            self.assertEqual(groups[("codex", "gpt-6.1-sol")]["applied"], 1)
            self.assertEqual(groups[("codex", "gpt-6.1-sol")]["usage"]["input"], 100)
            self.assertEqual(groups[("cursor", "auto")]["discarded"], 1)
            self.assertEqual(groups[("cursor", "auto")]["usage"]["cached_input"], 200)
            text = cost.render_runs(rows, groups, since="2026-10-01", log_path=path)
            self.assertIn("gpt-6.1-sol", text)
            self.assertIn("2 runs", text)
        finally:
            path.unlink(missing_ok=True)

    def test_since_bare_date_is_local(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            path = Path(f.name)
            f.write(json.dumps(_run_record(
                ts="2026-10-10T23:09:00+00:00", run="late-utc",
            )) + "\n")
        self.addCleanup(path.unlink, missing_ok=True)

        old_tz = os.environ.get("TZ")

        def restore_tz():
            if old_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old_tz
            time.tzset()

        self.addCleanup(restore_tz)

        os.environ["TZ"] = "Europe/Berlin"
        time.tzset()
        self.assertEqual(len(cost.load_run_log(path, since="2026-10-11")), 1)

        os.environ["TZ"] = "UTC"
        time.tzset()
        self.assertEqual(len(cost.load_run_log(path, since="2026-10-11")), 0)

    def test_since_explicit_offset_honoured(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            path = Path(f.name)
            f.write(json.dumps(_run_record(
                ts="2026-10-10T23:09:00+00:00", run="late-utc",
            )) + "\n")
        self.addCleanup(path.unlink, missing_ok=True)
        # 23:09 UTC is before 2026-10-11T00:00+00:00 and after 2026-10-11T00:00+02:00
        self.assertEqual(len(cost.load_run_log(path, since="2026-10-11T00:00:00+00:00")), 0)
        self.assertEqual(len(cost.load_run_log(path, since="2026-10-11T00:00:00+02:00")), 1)


class TestSession(unittest.TestCase):
    def test_dedupe_and_skip_synthetic(self):
        # Two lines share message.id (duplicated usage); one synthetic; one other id.
        lines = [
            {"type": "user", "message": {"content": "hi"}},
            {
                "type": "assistant",
                "requestId": "req_a",
                "message": {
                    "id": "msg_1",
                    "model": "claude-opus-5-5",
                    "usage": {
                        "input_tokens": 10,
                        "cache_read_input_tokens": 1000,
                        "cache_creation_input_tokens": 100,
                        "output_tokens": 20,
                    },
                },
            },
            {
                "type": "assistant",
                "requestId": "req_a",
                "message": {
                    "id": "msg_1",
                    "model": "claude-opus-5-5",
                    "usage": {
                        "input_tokens": 10,
                        "cache_read_input_tokens": 1000,
                        "cache_creation_input_tokens": 100,
                        "output_tokens": 20,
                    },
                },
            },
            {
                "type": "assistant",
                "requestId": "req_synth",
                "message": {
                    "id": "msg_synth",
                    "model": "<synthetic>",
                    "usage": {
                        "input_tokens": 999,
                        "cache_read_input_tokens": 999,
                        "cache_creation_input_tokens": 999,
                        "output_tokens": 999,
                    },
                },
            },
            {
                "type": "assistant",
                "requestId": "req_b",
                "message": {
                    "id": "msg_2",
                    "model": "claude-opus-5-5",
                    "usage": {
                        "input_tokens": 5,
                        "cache_read_input_tokens": 2000,
                        "cache_creation_input_tokens": 50,
                        "output_tokens": 30,
                    },
                },
            },
        ]
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            path = Path(f.name)
            for obj in lines:
                f.write(json.dumps(obj) + "\n")
        try:
            calls = cost.parse_assistant_usage(path)
            self.assertEqual(len(calls), 2)
            summary = cost.summarise_session(
                calls, {"input": 1, "cache_read": 0.1, "cache_write": 2.0, "output": 5.0}, None
            )
            self.assertEqual(summary["calls"], 2)
            self.assertEqual(summary["input"], 15)
            self.assertEqual(summary["cache_read"], 3000)
            self.assertEqual(summary["cache_creation"], 150)
            self.assertEqual(summary["output"], 50)
            self.assertEqual(summary["context_first"], 10 + 1000 + 100)
            self.assertEqual(summary["context_last"], 5 + 2000 + 50)
            self.assertEqual(summary["context_max"], 5 + 2000 + 50)
            text = cost.render_session(path, summary)
            self.assertIn("calls: 2", text)
            self.assertNotIn("hi", text)  # privacy: no message text
            self.assertIn("next call re-reads", text)
        finally:
            path.unlink(missing_ok=True)

    def test_fallback_request_id(self):
        lines = [
            {
                "type": "assistant",
                "requestId": "req_only",
                "message": {
                    "model": "claude-sonnet-5-5",
                    "usage": {
                        "input_tokens": 1,
                        "cache_read_input_tokens": 2,
                        "cache_creation_input_tokens": 3,
                        "output_tokens": 4,
                    },
                },
            },
            {
                "type": "assistant",
                "requestId": "req_only",
                "message": {
                    "model": "claude-sonnet-5-5",
                    "usage": {
                        "input_tokens": 1,
                        "cache_read_input_tokens": 2,
                        "cache_creation_input_tokens": 3,
                        "output_tokens": 4,
                    },
                },
            },
        ]
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            path = Path(f.name)
            for obj in lines:
                f.write(json.dumps(obj) + "\n")
        try:
            calls = cost.parse_assistant_usage(path)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["output"], 4)
        finally:
            path.unlink(missing_ok=True)

    def test_resolve_session_prefix(self):
        import os
        from unittest import mock

        with tempfile.TemporaryDirectory() as cfg:
            proj = Path(cfg) / "projects" / "-tmp-toy"
            proj.mkdir(parents=True)
            for sid in ("55a11d16-aaaa", "55b00000-bbbb"):
                (proj / f"{sid}.jsonl").write_text("")
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": cfg}):
                self.assertEqual(cost.resolve_session("55a1").name, "55a11d16-aaaa.jsonl")
                self.assertEqual(cost.resolve_session("55a11d16-aaaa").name, "55a11d16-aaaa.jsonl")
                with self.assertRaises(SystemExit) as cm:
                    cost.resolve_session("55")
                self.assertIn("ambiguous", str(cm.exception))

    def test_resolve_latest_from_subfolder(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as cfg, tempfile.TemporaryDirectory() as work:
            sub = Path(work) / "upstream" / "drafts"
            sub.mkdir(parents=True)
            proj = Path(cfg) / "projects" / cost.munge_cwd(work)
            proj.mkdir(parents=True)
            (proj / "abc.jsonl").write_text("")
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": cfg}), \
                    mock.patch("cost.os.getcwd", return_value=str(sub)):
                self.assertEqual(cost.resolve_session("latest"), proj / "abc.jsonl")
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": cfg}), \
                    mock.patch("cost.os.getcwd", return_value="/nonexistent-delegate-test"):
                with self.assertRaises(SystemExit) as cm:
                    cost.resolve_session("latest")
                self.assertIn("or its parents", str(cm.exception))

    def test_idle_rebuilds_gap_and_no_timestamp(self):
        weights = {"input": 1, "cache_read": 0.1, "cache_write": 2.0, "output": 5.0}
        base = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)

        def assistant(mid, minutes, cache_creation, with_ts=True):
            ts = (base + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
            obj = {
                "type": "assistant",
                "requestId": mid,
                "message": {
                    "id": mid,
                    "model": "claude-opus-5-5",
                    "usage": {
                        "input_tokens": 1,
                        "cache_read_input_tokens": 100,
                        "cache_creation_input_tokens": cache_creation,
                        "output_tokens": 1,
                    },
                },
            }
            if with_ts:
                obj["timestamp"] = ts
            return obj

        lines = [
            assistant("m1", 0, 1000),
            assistant("m2", 59, 2000),          # 59 min — not idle
            assistant("m3", 59, 9999, with_ts=False),  # no timestamp — never counts, restarts the clock
            assistant("m4", 59 + 61, 3000),     # 61 min after m2, but m3 came between — not counted
            assistant("m5", 59 + 61 + 61, 5000),  # 61 min after m4 — idle rebuild
        ]
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            path = Path(f.name)
            for obj in lines:
                f.write(json.dumps(obj) + "\n")
        self.addCleanup(path.unlink, missing_ok=True)

        calls = cost.parse_assistant_usage(path)
        self.assertEqual(len(calls), 5)
        summary = cost.summarise_session(calls, weights, None)
        self.assertEqual(len(summary["idle_rebuilds"]), 1)
        self.assertEqual(summary["idle_rebuilds"][0]["gap_min"], 61)
        self.assertEqual(summary["idle_rebuilds"][0]["cache_creation"], 5000)
        self.assertEqual(summary["idle_rebuild_units"], 5000 * weights["cache_write"])
        text = cost.render_session(path, summary)
        self.assertIn("idle rebuilds (gap >= 60 min): 1", text)
        self.assertIn("61 min", text)
        self.assertIn("of cost units", text)
        payload = json.loads(cost.render_session(path, summary, as_json=True))
        self.assertIn("idle_rebuilds", payload)
        self.assertIn("idle_rebuild_units", payload)
        self.assertEqual(len(payload["idle_rebuilds"]), 1)

        # No gaps → none
        tight = [
            assistant("a", 0, 100),
            assistant("b", 10, 100),
        ]
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            path2 = Path(f.name)
            for obj in tight:
                f.write(json.dumps(obj) + "\n")
        self.addCleanup(path2.unlink, missing_ok=True)
        summary2 = cost.summarise_session(cost.parse_assistant_usage(path2), weights, None)
        self.assertEqual(summary2["idle_rebuilds"], [])
        self.assertEqual(summary2["idle_rebuild_units"], 0)
        self.assertIn("idle rebuilds (gap >= 60 min): none", cost.render_session(path2, summary2))


class TestListPrice(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "session.jsonl"
        self.agents = self.path.with_suffix("") / "subagents"
        self.agents.mkdir(parents=True)
        self.weights, self.prices = cost.load_catalog_cost()

    def assistant(self, mid, model="claude-opus-5-5", inp=10, read=1000,
                  write=100, output=20, split=None, **modifiers):
        usage = {"input_tokens": inp, "cache_read_input_tokens": read,
                 "cache_creation_input_tokens": write, "output_tokens": output,
                 **modifiers}
        if split is not None:
            usage["cache_creation"] = {
                "ephemeral_5m_input_tokens": split[0],
                "ephemeral_1h_input_tokens": split[1],
            }
        return {"type": "assistant", "timestamp": "2026-10-13T12:00:00Z",
                "message": {"id": mid, "model": model, "usage": usage}}

    def write(self, path, *rows):
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    def summary(self):
        return cost.summarise_session(cost.parse_session_usage(self.path), self.weights, self.prices)

    def test_all_subagent_files_deduplicated_main_first_and_meta_ignored(self):
        main = self.assistant("duplicate", split=(0, 100))
        fallback = self.assistant(None, model="claude-haiku-5-5", split=(100, 0))
        fallback["requestId"] = "request-only"
        second = self.assistant("second", model="claude-haiku-5-5")
        self.write(self.path, main, main)
        self.write(self.agents / "agent-a.jsonl", fallback, main)
        self.write(self.agents / "agent-b.jsonl", fallback, second, second)
        self.write(self.agents / "agent-b.meta.json", self.assistant("metadata"))
        self.write(self.agents / "other.jsonl", self.assistant("not-an-agent"))

        calls = cost.parse_session_usage(self.path)
        self.assertEqual([c["id"] for c in calls], ["duplicate", "request-only", "second"])
        self.assertEqual([c["source"] for c in calls], ["main", "subagents", "subagents"])
        self.assertEqual(calls[0]["timestamp"], main["timestamp"])
        combined = self.summary()
        historical = cost.summarise_session(cost.parse_assistant_usage(self.path), self.weights, None)
        for key in ("calls", "models", "input", "cache_read", "cache_creation", "output",
                    "context_first", "context_last", "context_max", "cost_units", "cost_parts",
                    "idle_rebuilds", "idle_rebuild_units"):
            self.assertEqual(combined[key], historical[key], key)
        self.assertEqual(combined["subagent_calls"], 2)
        self.assertEqual([(r["source"], r["model"], r["calls"]) for r in combined["by_model"]],
                         [("main", "claude-opus-5-5", 1), ("subagents", "claude-haiku-5-5", 2)])
        self.assertAlmostEqual(combined["usd"], combined["usd_main"] + combined["usd_subagents"])
        self.assertEqual(combined["usd_unpriced"], [])

    def test_cache_write_durations_and_unsplit_remainder(self):
        self.write(self.path,
                   self.assistant("split", split=(20, 70)),  # remaining ten at 5m
                   self.assistant("unsplit"))
        # Explicit null has the same interpretation as an absent split.
        with self.path.open("a", encoding="utf-8") as stream:
            null = self.assistant("explicit-null")
            null["message"]["usage"]["cache_creation"] = None
            stream.write(json.dumps(null) + "\n")
        calls = cost.parse_session_usage(self.path)
        self.assertEqual((calls[0]["cache_write_5m"], calls[0]["cache_write_1h"],
                          calls[0]["cache_write_unsplit"]), (20, 70, 10))
        summary = self.summary()
        row = summary["by_model"][0]
        self.assertEqual((row["cache_write_5m"], row["cache_write_1h"],
                          row["cache_write_unsplit"]), (20, 70, 210))
        expected = (30 * 4 + 3000 * 0.2 + 230 * 5 + 70 * 8 + 60 * 20) / 1e6
        self.assertAlmostEqual(summary["usd"], expected, places=12)
        self.assertEqual(summary["usd_subagents"], 0.0)

    def test_haiku_threshold_prices_all_classes_per_call(self):
        self.write(self.path,
                   self.assistant("base", model="claude-haiku-5-5", inp=100, read=99870,
                                  write=30, output=20, split=(10, 10)),
                   self.assistant("above", model="claude-haiku-5-5", inp=101, read=99870,
                                  write=30, output=20, split=(10, 10)))
        base, above = cost.parse_session_usage(self.path)
        base_usd = (100 * 0.1 + 99870 * 0.01 + 20 * 0.125 + 10 * 0.2 + 20 * 0.5) / 1e6
        above_usd = (101 * 0.5 + 99870 * 0.05 + 20 * 0.625 + 10 * 1.0 + 20 * 2.5) / 1e6
        self.assertAlmostEqual(cost.price_call(base, self.prices)[0], base_usd, places=12)
        self.assertAlmostEqual(cost.price_call(above, self.prices)[0], above_usd, places=12)
        summary = self.summary()
        self.assertAlmostEqual(summary["usd"], base_usd + above_usd, places=12)
        self.assertAlmostEqual(summary["by_model"][0]["usd"], base_usd + above_usd, places=12)

    def test_unpriced_modifiers_unknown_model_and_partial_group(self):
        self.write(self.path,
                   self.assistant("fast-a", speed="fast"), self.assistant("fast-b", speed="fast"),
                   self.assistant("regional", inference_geo="us"),
                   self.assistant("standard", speed="standard", inference_geo="global"),
                   self.assistant("default"))
        self.write(self.agents / "agent-a.jsonl", self.assistant("unknown", model="unknown"))
        summary = self.summary()
        self.assertEqual(summary["usd_unpriced"], [
            {"model": "claude-opus-5-5", "source": "main", "calls": 1, "reason": "inference_geo=us"},
            {"model": "claude-opus-5-5", "source": "main", "calls": 2, "reason": "speed=fast"},
            {"model": "unknown", "source": "subagents", "calls": 1, "reason": "no price"},
        ])
        expected = 2 * (10 * 4 + 1000 * 0.2 + 100 * 5 + 20 * 20) / 1e6
        self.assertAlmostEqual(summary["usd"], expected, places=12)
        main, agent = summary["by_model"]
        self.assertEqual((main["calls"], main["input"], main["cache_read"],
                          main["cache_write_unsplit"], main["output"]), (5, 50, 5000, 500, 100))
        self.assertEqual((agent["calls"], agent["input"], agent["cache_read"],
                          agent["cache_write_unsplit"], agent["output"]), (1, 10, 1000, 100, 20))
        self.assertIsNone(agent["usd"])
        text = cost.render_session(self.path, summary)
        for group in summary["usd_unpriced"]:
            self.assertIn(group["reason"], text)

    def test_model_exact_then_longest_prefix_and_date_suffix(self):
        self.write(self.path, self.assistant("dated", model="claude-haiku-5-5-20261001"))
        summary = self.summary()
        expected = (10 * 0.1 + 1000 * 0.01 + 100 * 0.125 + 20 * 0.5) / 1e6
        self.assertAlmostEqual(summary["usd"], expected, places=12)
        self.assertEqual(summary["by_model"][0]["model"], "claude-haiku-5-5-20261001")
        prices = {"models": {"claude-haiku": {"tag": "short"},
                             "claude-haiku-5-5": {"tag": "long"},
                             "claude-haiku-5-5-20261001": {"tag": "exact"}}}
        self.assertEqual(cost.model_price("claude-haiku-5-5-20261001", prices)["tag"], "exact")
        self.assertEqual(cost.model_price("claude-haiku-5-5-20261002", prices)["tag"], "long")
        self.assertIsNone(cost.model_price("claude-haiku-5-50", self.prices))

    def test_empty_or_all_unpriced_and_catalog_fallback(self):
        self.write(self.path)
        summary = self.summary()
        self.assertIsNone(summary["usd"])
        self.assertEqual((summary["usd_main"], summary["usd_subagents"]), (0.0, 0.0))
        self.assertEqual(summary["by_model"], [])
        self.assertEqual(summary["prices_as_of"], "2026-10-06")
        self.write(self.path, self.assistant("unknown", model=None))
        summary = self.summary()
        self.assertIsNone(summary["usd"])
        self.assertIsNone(summary["by_model"][0]["usd"])
        self.assertEqual(summary["usd_unpriced"][0]["reason"], "no price")
        for contents in (None, "bad json", "{}"):
            catalog = self.path.parent / "catalog.json"
            if contents is not None:
                catalog.write_text(contents, encoding="utf-8")
            with mock.patch("cost.CATALOG", catalog):
                weights, prices = cost.load_catalog_cost()
            self.assertEqual(weights, cost.DEFAULT_WEIGHTS)
            self.assertIsNone(prices)
            if catalog.exists():
                catalog.unlink()

    def test_cli_json_schema_precision_and_historical_text_lines(self):
        self.write(self.path, self.assistant("main"))
        buf = io.StringIO()
        with redirect_stdout(buf):
            cost.main(["--session", str(self.path), "--json"])
        payload = json.loads(buf.getvalue())
        self.assertNotIn("usd_per_mtok_input", payload)
        self.assertAlmostEqual(payload["usd"], 0.00114, places=12)
        self.assertEqual(set(payload["by_model"][0]),
                         {"model", "source", "calls", *cost.TOKEN_CLASSES, "usd"})
        text = cost.render_session(self.path, payload)
        old_lines = [line for line in text.splitlines()
                     if not line.startswith(("list price:", "by model:", "model ", "claude-opus"))]
        self.assertEqual(old_lines, [
            "session: session.jsonl  (claude-opus-5-5)", "calls: 1",
            "tokens: input 10  cache_creation 100  cache_read 1,000  output 20",
            "context/call: first 1.1k  last 1.1k  max 1.1k",
            "cost units: 410  (input 2.4%  cache_read 24.4%  cache_write 48.8%  output 24.4%)",
            "idle rebuilds (gap >= 60 min): none", "next call re-reads ≈ 1.1k tokens",
        ])
        self.assertIn("list price: $0.00  (main $0.00, subagents $0.00; prices as of 2026-10-06)", text)
        self.assertIn("by model: (prices as of 2026-10-06; USD)", text)


if __name__ == "__main__":
    unittest.main()

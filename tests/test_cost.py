"""Tests for cost.py run-log and session transcript summarisation."""
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()

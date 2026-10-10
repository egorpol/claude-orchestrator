"""Tests for cost.py run-log and session transcript summarisation."""
import json
import sys
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()

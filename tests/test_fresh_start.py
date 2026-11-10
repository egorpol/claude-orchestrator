"""Unit tests for skills/delegate/scripts/fresh_start.py (R2a/R2b shared scan)."""
import _env  # noqa: F401  — isolate HOME/XDG/PATH before scripts/hooks
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "delegate" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import fresh_start  # noqa: E402


def _assistant(ctx, *, entrypoint=None, sidechain=False, model="claude-opus-5-5"):
    # ctx = input + cache_read + cache_creation; split as 10 + (ctx-1010) + 1000
    usage = {
        "input_tokens": 10,
        "cache_read_input_tokens": max(0, ctx - 1010),
        "cache_creation_input_tokens": 1000,
    }
    rec = {"type": "assistant", "message": {"model": model, "usage": usage}}
    if entrypoint is not None:
        rec["entrypoint"] = entrypoint
    if sidechain:
        rec["isSidechain"] = True
    return rec


def _write(path, *records, mtime=None):
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


class FreshStartTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="fresh-start-")
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)

    def test_median_of_newest_five_excludes_current_and_sdk(self):
        # mtimes: higher = newer. Exclude current; skip sdk; median of 5.
        contexts = [40_000, 50_000, 52_000, 54_000, 60_000, 70_000]
        base = 1_700_000_000
        for i, ctx in enumerate(contexts):
            path = self.folder / f"s{i}.jsonl"
            _write(path,
                   {"type": "user", "entrypoint": "claude-vscode"},
                   _assistant(ctx, entrypoint="claude-vscode"),
                   mtime=base + i)
        current = self.folder / "current.jsonl"
        _write(current,
               {"type": "user", "entrypoint": "claude-vscode"},
               _assistant(99_000, entrypoint="claude-vscode"),
               mtime=base + 100)
        sdk = self.folder / "sdk.jsonl"
        _write(sdk,
               {"type": "user", "entrypoint": "sdk-cli"},
               _assistant(10_000, entrypoint="sdk-cli"),
               mtime=base + 99)
        # Newest other non-sdk: s5..s1 → 70k,60k,54k,52k,50k; median 54k
        self.assertEqual(
            fresh_start.median_fresh_start(self.folder, exclude=current), 54_000)

    def test_fewer_than_three_usable_returns_none(self):
        base = 1_700_000_000
        for i, ctx in enumerate((41_000, 42_000)):
            _write(self.folder / f"a{i}.jsonl", _assistant(ctx), mtime=base + i)
        self.assertIsNone(fresh_start.median_fresh_start(self.folder))

    def test_skips_sidechain_synthetic_corrupt_and_subfolder(self):
        main = self.folder / "main.jsonl"
        records = [
            {"type": "user", "entrypoint": "claude-vscode"},
            _assistant(5_000, sidechain=True),
            {"type": "assistant", "message": {"model": "<synthetic>",
                                             "usage": {"input_tokens": 9}}},
            "not-json",
            _assistant(51_000),
        ]
        main.write_text("\n".join(
            (r if isinstance(r, str) else json.dumps(r)) for r in records) + "\n",
            encoding="utf-8")
        self.assertEqual(fresh_start.first_call_context(main), 51_000)
        sub = self.folder / "subagents"
        sub.mkdir()
        _write(sub / "agent-1.jsonl", _assistant(999_000))
        # Only one usable top-level session → below min_usable
        for i, ctx in enumerate((50_000, 52_000)):
            _write(self.folder / f"p{i}.jsonl", _assistant(ctx), mtime=1_700_000_010 + i)
        # three usable: main + p0 + p1
        got = fresh_start.median_fresh_start(self.folder)
        self.assertEqual(got, 51_000)

    def test_corrupt_usage_value_skips_that_transcript(self):
        # Review 2026-11-10: a non-numeric usage value raised out of the median.
        base = 1_700_000_000
        for i, ctx in enumerate((50_000, 52_000, 54_000)):
            _write(self.folder / f"g{i}.jsonl", _assistant(ctx), mtime=base + i)
        bad = _assistant(1)
        bad["message"]["usage"]["input_tokens"] = "n/a"
        _write(self.folder / "bad.jsonl", bad, mtime=base + 9)
        self.assertIsNone(fresh_start.first_call_context(self.folder / "bad.jsonl"))
        self.assertEqual(fresh_start.median_fresh_start(self.folder), 52_000)

    def test_reads_only_up_to_the_byte_cap(self):
        path = self.folder / "big.jsonl"
        filler = json.dumps({"type": "user", "pad": "x" * 2000}) + "\n"
        _write(path, _assistant(51_000))
        self.assertEqual(fresh_start.first_call_context(path, max_bytes=100), None)  # line cut
        path.write_text(filler * 3 + json.dumps(_assistant(51_000)) + "\n", encoding="utf-8")
        self.assertEqual(fresh_start.first_call_context(path), 51_000)
        self.assertIsNone(fresh_start.first_call_context(path, max_bytes=len(filler) * 3))

    def test_cost_render_includes_fresh_line(self):
        import cost
        for i, ctx in enumerate((50_000, 52_000, 54_000)):
            _write(self.folder / f"o{i}.jsonl", _assistant(ctx), mtime=1_700_000_000 + i)
        path = self.folder / "session.jsonl"
        _write(path, _assistant(1_100))
        summary = {
            "models": ["claude-opus-5-5"], "calls": 1, "input": 10,
            "cache_creation": 100, "cache_read": 1000, "output": 20,
            "context_first": 1100, "context_last": 1100, "context_max": 1100,
            "cost_units": 410, "cost_parts": {
                "input": 10, "cache_read": 100, "cache_write": 200, "output": 100},
            "usd": 0.001, "usd_main": 0.001, "usd_subagents": 0.0,
            "prices_as_of": "2026-10-06", "by_model": [], "usd_unpriced": [],
            "idle_rebuilds": [],
        }
        text = cost.render_session(path, summary)
        self.assertIn("fresh session here starts ≈ 52k (median of up to 5 newest other sessions)", text)
        payload = json.loads(cost.render_session(path, summary, as_json=True))
        self.assertEqual(payload["fresh_start"], 52_000)


if __name__ == "__main__":
    unittest.main()

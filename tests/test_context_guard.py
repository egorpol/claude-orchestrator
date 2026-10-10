"""Tests for hooks/context-guard.py thresholds and per-session dedupe."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "context-guard.py"


class ContextGuardTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="context-guard-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.transcript = self.tmp / "session.jsonl"
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("CONTEXT_GUARD_")}
        self.env["CONTEXT_GUARD_STATE"] = str(self.tmp / "state")

    def prompt(self, context):
        usage = {"input_tokens": 10, "cache_read_input_tokens": context - 1010, "cache_creation_input_tokens": 1000}
        with self.transcript.open("a") as f:
            f.write(json.dumps({"type": "assistant", "message": {"model": "claude-opus-5-5", "usage": usage}}) + "\n")
        r = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(
            {"transcript_path": str(self.transcript), "session_id": "s1"}),
            env=self.env, capture_output=True, text=True, check=True)
        return json.loads(r.stdout) if r.stdout.strip() else None

    def test_fires_at_item_boundaries_from_120k_then_every_60k(self):
        self.assertIsNone(self.prompt(119_000))
        first = self.prompt(121_000)
        self.assertIn("~121k", first["systemMessage"])
        context = first["hookSpecificOutput"]["additionalContext"]
        self.assertIn("new work item", context)
        self.assertIn("/clear", context)
        self.assertIsNone(self.prompt(170_000))  # same level: no repeat
        self.assertIsNotNone(self.prompt(181_000))
        self.assertIsNone(self.prompt(200_000))

    def test_disable_and_bad_input_stay_silent(self):
        self.env["CONTEXT_GUARD_DISABLE"] = "1"
        self.assertIsNone(self.prompt(500_000))
        del self.env["CONTEXT_GUARD_DISABLE"]
        r = subprocess.run([sys.executable, str(HOOK)], input="not json", env=self.env,
                           capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout), (0, ""))


if __name__ == "__main__":
    unittest.main()

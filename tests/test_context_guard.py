"""Tests for hooks/context-guard.py thresholds, idle block, and per-session dedupe."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "context-guard.py"


def _ago(minutes=0, hours=0):
    dt = datetime.now(timezone.utc) - timedelta(minutes=minutes, hours=hours)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _future(minutes=30):
    dt = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


class ContextGuardTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="context-guard-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.transcript = self.tmp / "session.jsonl"
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("CONTEXT_GUARD_")}
        self.env["CONTEXT_GUARD_STATE"] = str(self.tmp / "state")

    def prompt(self, context, *, timestamp=None, session_id="s1", append=True,
               is_sidechain=False, model="claude-opus-5-5", text="next step", event=None):
        if append:
            usage = {
                "input_tokens": 10,
                "cache_read_input_tokens": context - 1010,
                "cache_creation_input_tokens": 1000,
            }
            rec = {"type": "assistant", "message": {"model": model, "usage": usage}}
            if timestamp is not None:
                rec["timestamp"] = timestamp
            if is_sidechain:
                rec["isSidechain"] = True
            with self.transcript.open("a") as f:
                f.write(json.dumps(rec) + "\n")
        r = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps({"transcript_path": str(self.transcript), "session_id": session_id,
                              "prompt": text, **({"hook_event_name": event} if event else {})}),
            env=self.env, capture_output=True, text=True, check=True,
        )
        return json.loads(r.stdout) if r.stdout.strip() else None

    def test_fires_at_item_boundaries_from_120k_then_every_60k(self):
        self.assertIsNone(self.prompt(119_000))
        first = self.prompt(121_000)
        self.assertIn("~121k", first["systemMessage"])
        context = first["hookSpecificOutput"]["additionalContext"]
        self.assertIn("new work item", context)
        self.assertIn("'/clear, then /next'", context)
        self.assertIsNone(self.prompt(170_000))  # same level: no repeat
        self.assertIsNotNone(self.prompt(181_000))
        self.assertIsNone(self.prompt(200_000))

    def test_other_event_names_stay_silent(self):
        # cursor-agent fires ~/.claude/settings.json hooks as beforeSubmitPrompt.
        self.assertIsNone(self.prompt(500_000, event="beforeSubmitPrompt"))
        self.assertIsNotNone(self.prompt(500_000, append=False, event="UserPromptSubmit"))

    def test_disable_and_bad_input_stay_silent(self):
        self.env["CONTEXT_GUARD_DISABLE"] = "1"
        self.assertIsNone(self.prompt(500_000))
        del self.env["CONTEXT_GUARD_DISABLE"]
        r = subprocess.run([sys.executable, str(HOOK)], input="not json", env=self.env,
                           capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_idle_block_then_resend_runs_size_guard(self):
        blocked = self.prompt(150_000, timestamp=_ago(hours=2))
        self.assertEqual(blocked["decision"], "block")
        reason = blocked["reason"]
        self.assertTrue(reason.startswith("context guard: idle "))
        self.assertRegex(reason, r"1[12]\d min")  # ~120 min
        self.assertIn("~150k", reason)
        self.assertIn("/clear, then /next", reason)
        # Same assistant record (resend): idle must pass; size guard fires at 150k.
        resent = self.prompt(150_000, append=False)
        self.assertNotIn("decision", resent or {})
        self.assertIn("~150k", resent["systemMessage"])

    def test_idle_blocks_again_after_new_assistant_record(self):
        first = self.prompt(150_000, timestamp=_ago(hours=2))
        self.assertEqual(first["decision"], "block")
        # New assistant record, then idle again — a new idle period.
        again = self.prompt(160_000, timestamp=_ago(hours=2))
        self.assertEqual(again["decision"], "block")
        self.assertIn("~160k", again["reason"])

    def test_idle_below_thresholds_does_not_block(self):
        # Large idle, small context.
        out = self.prompt(90_000, timestamp=_ago(hours=2))
        self.assertIsNone(out)
        # Short idle, large context — size guard only.
        out = self.prompt(300_000, timestamp=_ago(minutes=30))
        self.assertNotIn("decision", out or {})
        self.assertIn("~300k", out["systemMessage"])

    def test_idle_skips_bad_timestamps(self):
        for i, ts in enumerate((None, "not-a-date", _future(minutes=30))):
            self.transcript = self.tmp / f"bad-ts-{i}.jsonl"
            out = self.prompt(150_000, timestamp=ts, session_id=f"bad{i}")
            self.assertNotEqual((out or {}).get("decision"), "block")

    def test_idle_uses_main_thread_not_newer_sidechain(self):
        def write(context, timestamp, **extra):
            usage = {
                "input_tokens": 10,
                "cache_read_input_tokens": context - 1010,
                "cache_creation_input_tokens": 1000,
            }
            rec = {"type": "assistant", "message": {"model": "claude-opus-5-5", "usage": usage},
                   "timestamp": timestamp, **extra}
            with self.transcript.open("a") as f:
                f.write(json.dumps(rec) + "\n")

        write(150_000, _ago(hours=2))
        write(10_000, _ago(minutes=5), isSidechain=True)
        blocked = self.prompt(150_000, append=False)
        self.assertEqual(blocked["decision"], "block")
        self.assertRegex(blocked["reason"], r"1[12]\d min")

    def test_idle_env_overrides_and_disable(self):
        self.env["CONTEXT_GUARD_IDLE_MIN"] = "30"
        self.env["CONTEXT_GUARD_IDLE_CONTEXT"] = "80_000".replace("_", "")
        blocked = self.prompt(90_000, timestamp=_ago(minutes=45))
        self.assertEqual(blocked["decision"], "block")
        self.assertIn("~90k", blocked["reason"])
        self.env["CONTEXT_GUARD_DISABLE"] = "1"
        self.assertIsNone(self.prompt(200_000, timestamp=_ago(hours=3)))


    def test_idle_lets_notifications_and_commands_through(self):
        for text in ("<task-notification>\n<task-id>b1</task-id>", "  <<autonomous-loop-dynamic>>",
                     "/loop 2h check CI", ""):
            out = self.prompt(150_000, timestamp=_ago(hours=2), text=text)
            self.assertNotEqual((out or {}).get("decision"), "block", text)
        self.assertEqual(self.prompt(150_000, append=False)["decision"], "block")  # typed prompt

    def test_bad_env_values_fall_back_to_defaults(self):
        for name in ("CONTEXT_GUARD_FIRST", "CONTEXT_GUARD_STEP",
                     "CONTEXT_GUARD_IDLE_MIN", "CONTEXT_GUARD_IDLE_CONTEXT"):
            self.env[name] = "bad"
        blocked = self.prompt(150_000, timestamp=_ago(hours=2))  # check=True: exit 0 required
        self.assertEqual(blocked["decision"], "block")
        self.assertTrue(blocked["reason"].startswith("context guard:"))
        self.assertIn("~150k", self.prompt(150_000, append=False)["systemMessage"])


if __name__ == "__main__":
    unittest.main()

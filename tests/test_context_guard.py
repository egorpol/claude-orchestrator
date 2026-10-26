"""Tests for hooks/context-guard.py thresholds, idle block, and per-session dedupe."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "hooks" / "context-guard.py"


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
        self.session_log = self.tmp / "sessions.jsonl"
        self.quota_log = self.tmp / "quota.jsonl"
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith("CONTEXT_GUARD_") and k != "CLAUDE_SESSION_LOG"}
        self.env["CONTEXT_GUARD_STATE"] = str(self.tmp / "state")
        # Never read the real ~/.cache/claude-delegate/ session or quota logs.
        self.env["CLAUDE_SESSION_LOG"] = str(self.session_log)
        self.env["XDG_CACHE_HOME"] = str(self.tmp / "cache")

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

    def write_start(self, session="s1", mode="orchestrator", excluded=False):
        row = {"event": "start", "session": session, "mode": mode, "excluded": excluded,
               "ts": datetime.now(timezone.utc).isoformat()}
        with self.session_log.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row) + "\n")

    def write_delegate(self, session="s1"):
        with self.session_log.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"event": "delegate", "session": session,
                                     "command": "launch"}) + "\n")

    def write_spare_quota(self):
        now = datetime.now(timezone.utc)
        row = {"ts": (now - timedelta(minutes=30)).isoformat(), "pools": {
            "claude-5h": {"used_pct": 1.0, "resets_at": (now + timedelta(hours=4)).isoformat(),
                          "stale": False},
            "claude-7d": {"used_pct": 1.0, "resets_at": (now + timedelta(days=2)).isoformat(),
                          "stale": False},
            "codex-5h": {"used_pct": 90.0, "resets_at": (now + timedelta(hours=2)).isoformat(),
                         "stale": False},
            "codex-7d": {"used_pct": 90.0, "resets_at": (now + timedelta(days=6)).isoformat(),
                         "stale": False},
            "cursor-auto": {"used_pct": 1.0, "resets_at": (now + timedelta(days=20)).isoformat(),
                            "stale": False},
        }}
        self.quota_log.write_text(json.dumps(row) + "\n", encoding="utf-8")

    def write_tool_calls(self, n, *, sidechain=False, start_id=0):
        with self.transcript.open("a", encoding="utf-8") as stream:
            for i in range(n):
                rec = {"type": "assistant",
                       "message": {"id": f"m{start_id + i}",
                                   "content": [{"type": "tool_use", "id": f"toolu_{start_id + i}",
                                                "name": "Bash", "input": {}}]}}
                if sidechain:
                    rec["isSidechain"] = True
                stream.write(json.dumps(rec) + "\n")

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

    def _nudge_ready(self, session="s1", tools=30):
        self.write_start(session)
        self.write_spare_quota()
        self.write_tool_calls(tools)
        # Usage row so last_context finds something; stay under FIRST so size reminder is quiet.
        return self.prompt(50_000, session_id=session)

    def test_delegate_nudge_fires_once_at_threshold(self):
        first = self._nudge_ready(tools=30)
        self.assertIsNotNone(first)
        self.assertIn("Delegation nudge", first["hookSpecificOutput"]["additionalContext"])
        self.assertIn("Cursor Auto", first["hookSpecificOutput"]["additionalContext"])
        self.assertIn("delegate", first["systemMessage"])
        self.assertIsNone(self.prompt(50_000, append=False))  # once per session

    def test_delegate_nudge_silent_when_blocked_or_ineligible(self):
        cases = [
            ("claude-only", dict(mode="claude-only")),
            ("vanilla", dict(mode="vanilla")),
            ("excluded", dict(excluded=True)),
            ("no-start", None),
            ("has-delegate", "delegate"),
            ("sidechain", "sidechain"),
            ("below-threshold", "below"),
            ("no-spare", "no-spare"),
            ("disabled", "disabled"),
            ("idle-block", "idle"),
        ]
        for name, setup in cases:
            with self.subTest(name=name):
                self.setUp()  # fresh temp dir per case
                if setup == "disabled":
                    self.env["CONTEXT_GUARD_NUDGE"] = "0"
                    self.write_start()
                    self.write_spare_quota()
                    self.write_tool_calls(30)
                    self.assertIsNone(self.prompt(50_000))
                elif setup == "idle":
                    self.write_start()
                    self.write_spare_quota()
                    self.write_tool_calls(30)
                    blocked = self.prompt(150_000, timestamp=_ago(hours=2))
                    self.assertEqual(blocked["decision"], "block")
                    self.assertNotIn("Delegation nudge", json.dumps(blocked))
                elif setup is None:
                    self.write_spare_quota()
                    self.write_tool_calls(30)
                    self.assertIsNone(self.prompt(50_000))
                elif setup == "delegate":
                    self.write_start()
                    self.write_delegate()
                    self.write_spare_quota()
                    self.write_tool_calls(30)
                    self.assertIsNone(self.prompt(50_000))
                elif setup == "sidechain":
                    self.write_start()
                    self.write_spare_quota()
                    self.write_tool_calls(30, sidechain=True)
                    self.assertIsNone(self.prompt(50_000))
                elif setup == "below":
                    self.write_start()
                    self.write_spare_quota()
                    self.write_tool_calls(29)
                    self.assertIsNone(self.prompt(50_000))
                elif setup == "no-spare":
                    self.write_start()
                    self.write_tool_calls(30)
                    self.assertIsNone(self.prompt(50_000))
                else:
                    self.write_start(**setup)
                    self.write_spare_quota()
                    self.write_tool_calls(30)
                    self.assertIsNone(self.prompt(50_000))

    def test_delegate_nudge_merges_with_size_reminder(self):
        self.write_start()
        self.write_spare_quota()
        self.write_tool_calls(30)
        out = self.prompt(121_000)
        self.assertIsNotNone(out)
        self.assertNotIn("decision", out)
        context = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Context guard:", context)
        self.assertIn("Delegation nudge", context)
        self.assertIn("~121k", out["systemMessage"])
        self.assertIn("delegate", out["systemMessage"])
        self.assertIn(" | ", out["systemMessage"])

    def test_tool_call_count_rereads_partial_last_line(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("context_guard", HOOK)
        guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guard)
        guard.STATE = self.tmp / "state"
        guard.NUDGE_CALLS = 30
        session = "partial"
        complete = json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "id": "toolu_partial",
                                     "name": "Bash", "input": {}}]},
        })
        # First pass: truncated last line (no trailing newline) must not advance offset past it.
        self.transcript.write_bytes(complete.encode("utf-8")[:-10])  # cut mid-JSON, no \n
        self.assertEqual(guard._tool_call_count(self.transcript, session), 0)
        cache = json.loads((guard.STATE / f"{session}.tools").read_text(encoding="utf-8"))
        self.assertEqual(cache["offset"], 0)
        # Complete the line and recount: the id appears once.
        self.transcript.write_bytes(complete.encode("utf-8") + b"\n")
        self.assertEqual(guard._tool_call_count(self.transcript, session), 1)
        cache = json.loads((guard.STATE / f"{session}.tools").read_text(encoding="utf-8"))
        self.assertEqual(cache["ids"], ["toolu_partial"])
        self.assertEqual(guard._tool_call_count(self.transcript, session), 1)


if __name__ == "__main__":
    unittest.main()

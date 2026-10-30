"""Subprocess hook tests: a private PATH can reach only our fake desktop tools."""
import json
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


HOOK = Path(__file__).resolve().parents[1] / "hooks/attention.py"
FAKE = r'''import json
import os
import sys
import time
from pathlib import Path

tool = Path(sys.argv[0]).name
args = sys.argv[1:]
row = {"tool": tool, "argv": args}
method = args[2].rsplit(".", 1)[-1] if tool == "qdbus6" else "notify"
if method == "loadScript":
    script = Path(args[3])
    row["script"] = script.read_text(encoding="ascii")
    row["mode"] = script.stat().st_mode & 0o777
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(row) + "\n")
if method in os.environ.get("FAKE_TIMEOUT_ON", "").split(","):
    time.sleep(3)
if os.environ.get("FAKE_FAIL_ON") == method:
    sys.exit(1)
if method == "loadScript":
    print(os.environ.get("FAKE_SCRIPT_ID", "7"))
else:
    print("desktop tool output must stay captured")
'''


class AttentionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="attention-test-")
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)
        self.bin = self.tmp / "fake-bin"
        self.bin.mkdir()
        self.log = self.tmp / "calls.jsonl"
        self.transcript = self.tmp / "transcript.jsonl"
        # Never inherit CURSOR_*, the desktop bus, or a system PATH. Absolute Python
        # shebangs let the fakes run without adding any real executable directory.
        self.env = {"HOME": "/fakehome", "PATH": str(self.bin), "TMPDIR": str(self.tmp),
                    "FAKE_LOG": str(self.log), "CURSOR_WORKSPACE_LABEL": "orchestrator",
                    "CLAUDE_CODE_ENTRYPOINT": "claude-vscode"}
        for name in ("qdbus6", "notify-send"):
            exe = self.bin / name
            exe.write_text(f"#!{sys.executable}\n" + FAKE, encoding="utf-8")
            exe.chmod(0o755)

    def picker(self, **changes):
        return {"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion",
                "cwd": "/fakehome/projects/site", "tool_input": {"questions": [
                    {"question": "Which next?", "options": [{"label": "Continue"}]}]}, **changes}

    def invoke(self, data=None, raw=None):
        if self.log.exists():
            self.log.unlink()
        started = time.monotonic()
        result = subprocess.run([sys.executable, str(HOOK)],
                                input=raw if raw is not None else json.dumps(data),
                                env=self.env, capture_output=True, text=True, timeout=10)
        self.elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")
        self.assertEqual(list(self.tmp.glob("claude-attention-*.js")), [])
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def assert_flash(self, calls, script_id="7"):
        dbus = [call for call in calls if call["tool"] == "qdbus6"]
        self.assertEqual(len(dbus), 3, calls)
        load, run, unload = [call["argv"] for call in dbus]
        self.assertEqual(load[:3], ["org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting.loadScript"])
        self.assertEqual(run, ["org.kde.KWin", "/Scripting/Script" + script_id, "org.kde.kwin.Script.run"])
        self.assertEqual(unload, ["org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting.unloadScript", load[4]])
        self.assertRegex(load[4], r"^claude-attention-[0-9]+-[0-9]+$")
        self.assertFalse(Path(load[3]).exists())
        self.assertEqual(stat.S_IMODE(dbus[0]["mode"]), 0o600)
        return dbus[0]["script"]

    def assert_notification(self, calls, summary, body):
        notices = [call["argv"] for call in calls if call["tool"] == "notify-send"]
        self.assertEqual(notices, [["--app-name=Claude Code", "--", summary, body]])

    def launch(self, task="Bg123", **age):
        return {"type": "user", "timestamp": (datetime.now(timezone.utc) - timedelta(
            **(age or {"minutes": 10}))).isoformat().replace("+00:00", "Z"),
            "message": {"content": [{"type": "tool_result", "content":
                f"Command running in background with ID: {task}"}]}}

    def stop(self, rows=None, **changes):
        data = {"hook_event_name": "Stop", **changes}
        if rows is not None:
            self.transcript.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
            data["transcript_path"] = str(self.transcript)
        return self.invoke(data)

    def test_picker_flashes_in_order_with_private_removed_script_and_no_popup(self):
        calls = self.invoke(self.picker())
        self.assert_flash(calls)
        self.assertEqual(len(calls), 3)

    def test_handoff_option_flashes_and_always_notifies(self):
        data = self.picker()
        data["tool_input"]["questions"].append({"options": [{"label": "/clear, then /next"}]})
        calls = self.invoke(data)
        self.assert_flash(calls)
        self.assert_notification(calls, "orchestrator: ready for /clear, then /next", "Which next?")

    def test_clear_in_description_is_only_a_picker(self):
        data = self.picker()
        data["tool_input"]["questions"][0]["options"][0]["description"] = "/clear, then /next"
        calls = self.invoke(data)
        self.assert_flash(calls)
        self.assertEqual(len(calls), 3)

    def test_stop_without_transcript_flashes_without_popup(self):
        calls = self.stop()
        self.assert_flash(calls)
        self.assertEqual(len(calls), 3)

    def test_stop_hook_active_is_silent(self):
        self.assertEqual(self.stop(stop_hook_active=True), [])

    def test_stop_pending_background_task_is_silent(self):
        self.assertEqual(self.stop([self.launch()]), [])

    def test_stop_attachment_completes_background_task(self):
        self.assert_flash(self.stop([self.launch(), {"type": "attachment", "attachment": {
            "content": "<task-notification><task-id>Bg123</task-id></task-notification>"}}]))

    def test_stop_assistant_quoted_tag_does_not_complete_task(self):
        self.assertEqual(self.stop([self.launch(), {"type": "assistant", "message": {
            "content": [{"type": "text", "text": "<task-id>Bg123</task-id>"}]}}]), [])

    def test_stop_expired_task_does_not_mute_forever(self):
        self.assert_flash(self.stop([self.launch(hours=4)]))

    def test_stop_queue_operation_completes_task(self):
        self.assert_flash(self.stop([self.launch(), {"type": "queue-operation",
                                                    "content": "<task-id>Bg123</task-id>"}]))

    def test_stop_plain_user_text_completes_task(self):
        self.assert_flash(self.stop([self.launch(), {"type": "user", "message": {
            "content": "<task-id>Bg123</task-id>"}}]))

    def test_stop_tool_result_tags_do_not_complete_task(self):
        for row in ({"type": "user", "message": {"content": [{"type": "tool_result",
                     "content": "<task-id>Bg123</task-id>"}]}},
                    {"type": "tool_result", "content": "<task-id>Bg123</task-id>"}):
            with self.subTest(row=row):
                self.assertEqual(self.stop([self.launch(), row]), [])

    def test_stop_tracks_multiple_tasks_and_requires_later_notification(self):
        done = {"type": "attachment", "content": "<task-id>Bg123</task-id>"}
        self.assertEqual(self.stop([done, self.launch()]), [])
        self.assertEqual(self.stop([self.launch(), self.launch("other7"), done]), [])
        self.assert_flash(self.stop([self.launch(), done, self.launch("other7"),
                                    {"type": "attachment", "content": "<task-id>other7</task-id>"}]))

    def test_stop_launch_requires_user_tool_result_and_recent_timestamp(self):
        for changes in ({"type": "assistant"}, {"timestamp": "bad"}, {"timestamp": None},
                        {"message": {"content": "Command running in background with ID: Bg123"}}):
            with self.subTest(changes=changes):
                self.assert_flash(self.stop([{**self.launch(), **changes}]))

    def test_stop_launch_in_tool_result_text_blocks(self):
        row = self.launch()
        block = row["message"]["content"][0]
        block["content"] = [{"type": "text", "text": block["content"]}]
        self.assertEqual(self.stop([row]), [])

    def test_stop_unreadable_transcript_flashes(self):
        for path in (self.tmp / "missing", self.tmp):
            with self.subTest(path=path):
                self.assert_flash(self.stop(transcript_path=str(path)))

    def test_stop_skips_bad_lines(self):
        self.transcript.write_bytes(b"{bad\n\xff\nnull\n[]\n" + json.dumps(self.launch()).encode() + b"\n")
        self.assertEqual(self.stop(transcript_path=str(self.transcript)), [])

    def test_stop_recognizes_unicode_escaped_launch_and_completion(self):
        launch = json.dumps(self.launch()).replace("Command", r"\u0043ommand") + "\n"
        self.transcript.write_text(launch)
        self.assertEqual(self.stop(transcript_path=str(self.transcript)), [])
        done = json.dumps({"type": "attachment", "content": "<task-id>Bg123</task-id>"})
        self.transcript.write_text(launch + done.replace("<", r"\u003c") + "\n")
        self.assert_flash(self.stop(transcript_path=str(self.transcript)))

    def test_stop_skips_partial_first_tail_line(self):
        # The tail starts with valid-looking JSON, but it is part of an invalid,
        # larger line. It must not be mistaken for a launch.
        launch = json.dumps(self.launch()).encode()
        self.transcript.write_bytes(b"x" * 100 + launch + b"\n" + b" " * ((8 << 20) - len(launch) - 1))
        self.assert_flash(self.stop(transcript_path=str(self.transcript)))

    def test_stop_does_not_read_launch_outside_tail(self):
        self.transcript.write_bytes(json.dumps(self.launch()).encode() + b"\n" + b"x" * (9 << 20) + b"\n")
        self.assert_flash(self.stop(transcript_path=str(self.transcript)))

    def test_permission_prompt_flashes(self):
        calls = self.invoke({"hook_event_name": "Notification", "notification_type": "permission_prompt"})
        self.assert_flash(calls)
        self.assertEqual(len(calls), 3)

    def test_elicitation_dialog_flashes(self):
        self.assert_flash(self.invoke({"hook_event_name": "Notification",
                                      "notification_type": "elicitation_dialog"}))

    def test_idle_and_other_notifications_are_silent(self):
        for kind in ("idle_prompt", "auth_success", "unknown", None):
            with self.subTest(kind=kind):
                self.assertEqual(self.invoke({"hook_event_name": "Notification", "notification_type": kind}), [])

    def test_no_label_picker_notifies_with_cwd_basename(self):
        del self.env["CURSOR_WORKSPACE_LABEL"]
        calls = self.invoke(self.picker(cwd="/fakehome/projects/site/"))
        self.assert_notification(calls, "site: needs input", "Which next?")
        self.assertEqual(len(calls), 1)

    def test_no_label_stop_is_silent(self):
        self.env["CURSOR_WORKSPACE_LABEL"] = ""
        self.assertEqual(self.stop(), [])

    def test_missing_qdbus_picker_falls_back(self):
        (self.bin / "qdbus6").unlink()
        calls = self.invoke(self.picker())
        self.assert_notification(calls, "orchestrator: needs input", "Which next?")
        self.assertEqual(len(calls), 1)

    def test_failed_load_falls_back_unloads_and_removes_script(self):
        self.env["FAKE_FAIL_ON"] = "loadScript"
        calls = self.invoke(self.picker())
        self.assert_notification(calls, "orchestrator: needs input", "Which next?")
        self.assertEqual([call["argv"][2].rsplit(".", 1)[-1] for call in calls[:-1]],
                         ["loadScript", "unloadScript"])
        self.assertEqual(calls[0]["argv"][4], calls[1]["argv"][3])
        self.assertFalse(Path(calls[0]["argv"][3]).exists())

    def test_run_and_unload_failure_each_fall_back(self):
        for method in ("run", "unloadScript"):
            with self.subTest(method=method):
                self.env["FAKE_FAIL_ON"] = method
                calls = self.invoke(self.picker())
                self.assert_flash(calls)
                self.assert_notification(calls, "orchestrator: needs input", "Which next?")

    def test_noninteger_load_id_falls_back_and_cleans_up(self):
        for value in ("garbage", "7\n8", ""):
            with self.subTest(value=value):
                self.env["FAKE_SCRIPT_ID"] = value
                calls = self.invoke(self.picker())
                self.assertEqual(len(calls), 3)  # load, cleanup unload, popup; never run
                self.assert_notification(calls, "orchestrator: needs input", "Which next?")
                self.assertFalse(Path(calls[0]["argv"][3]).exists())

    def test_script_id_zero_is_valid(self):
        self.env["FAKE_SCRIPT_ID"] = "0"
        self.assert_flash(self.invoke(self.picker()), script_id="0")

    def test_load_timeout_falls_back_and_cleans_up(self):
        self.env["FAKE_TIMEOUT_ON"] = "loadScript"
        calls = self.invoke(self.picker())
        self.assert_notification(calls, "orchestrator: needs input", "Which next?")
        self.assertFalse(Path(calls[0]["argv"][3]).exists())
        self.assertLess(self.elapsed, 3)

    def test_hung_run_unload_and_popup_stay_under_install_timeout(self):
        # A hung load leaves no id to run, so three timeouts is the worst path.
        self.env["FAKE_TIMEOUT_ON"] = "run,unloadScript,notify"
        calls = self.invoke(self.picker())
        self.assertEqual([call["tool"] for call in calls], ["qdbus6"] * 3 + ["notify-send"])
        self.assertLess(self.elapsed, 4.5)

    def test_turn_end_failure_never_notifies(self):
        self.env["FAKE_FAIL_ON"] = "loadScript"
        calls = self.stop()
        self.assertTrue(calls)
        self.assertTrue(all(call["tool"] == "qdbus6" for call in calls))

    def test_permission_fallback_body_is_cut_to_200_characters(self):
        (self.bin / "qdbus6").unlink()
        calls = self.invoke({"hook_event_name": "Notification", "notification_type": "permission_prompt",
                             "message": "é" * 250})
        self.assert_notification(calls, "orchestrator: needs input", "é" * 200)

    def test_picker_and_handoff_body_use_first_question_cut_to_200(self):
        (self.bin / "qdbus6").unlink()
        for label, summary in (("Continue", "needs input"), ("/clear", "ready for /clear, then /next")):
            with self.subTest(label=label):
                data = self.picker(tool_input={"questions": [
                    {"question": "Q" * 250, "options": [{"label": label}]}, {"question": "second"}]})
                self.assert_notification(self.invoke(data), "orchestrator: " + summary, "Q" * 200)

    def test_missing_notify_send_is_silent(self):
        (self.bin / "notify-send").unlink()
        (self.bin / "qdbus6").unlink()
        self.assertEqual(self.invoke(self.picker()), [])

    def test_disable_switch_is_silent(self):
        self.env["ATTENTION_DISABLE"] = "1"
        self.assertEqual(self.invoke(self.picker()), [])

    def test_sdk_entrypoint_is_silent(self):
        self.env["CLAUDE_CODE_ENTRYPOINT"] = "sdk-cli"
        self.assertEqual(self.invoke(self.picker()), [])

    def test_vscode_entrypoint_flashes_in_all_session_modes(self):
        for mode in ("orchestrator", "claude-only", "vanilla"):
            with self.subTest(mode=mode):
                self.env["CLAUDE_SESSION_MODE"] = mode
                self.assert_flash(self.invoke(self.picker()))

    def test_adversarial_label_stays_one_ascii_json_literal(self):
        label = 'a"b\'c\\d</script>\n\u2028'
        self.env["CURSOR_WORKSPACE_LABEL"] = label
        script = self.assert_flash(self.invoke(self.picker()))
        literal = json.dumps(label, ensure_ascii=True)
        self.assertEqual(script.splitlines()[0], "const LABEL = " + literal + ";")
        self.assertEqual(script.count(literal), 1)
        self.assertNotIn("\n", literal)
        self.assertNotIn("\u2028", literal)
        self.assertIn(r"\n", literal)
        self.assertIn(r"\u2028", literal)

    def test_js_matches_only_cursor_inactive_exact_workspace_boundary(self):
        # Assert the actual JS conditions, not a separate reimplementation. In
        # particular, ' - orchestrator - Cursor' cannot match claude-orchestrator.
        script = self.assert_flash(self.invoke(self.picker()))
        for condition in ('workspace.windowList()', 'String(w.resourceClass).toLowerCase() === "cursor"',
                          '!w.active', 'w.caption === LABEL + " - Cursor"',
                          'w.caption.endsWith(" - " + LABEL + " - Cursor")',
                          'w.demandsAttention = true'):
            self.assertIn(condition, script)

    def test_concurrent_sessions_use_different_script_names(self):
        first = self.invoke(self.picker())[0]["argv"][4]
        second = self.invoke(self.picker())[0]["argv"][4]
        self.assertNotEqual(first, second)

    def test_invalid_empty_and_unknown_input_is_silent(self):
        for raw in ("{invalid", "", "null", "[]", '{"hook_event_name":"unknown"}',
                    '{"hook_event_name":"PreToolUse","tool_name":"Bash"}'):
            with self.subTest(raw=raw):
                self.assertEqual(self.invoke(raw=raw), [])

    def test_50mb_transcript_pending_tail_finishes_under_one_second(self):
        launch = json.dumps(self.launch()).encode() + b"\n"
        filler = b'{"type":"assistant"}\n'  # many short rows stress the bounded scan
        count, remainder = divmod((50 << 20) - len(launch), len(filler))
        with self.transcript.open("wb") as stream:
            stream.write(filler * count)
            stream.write(b" " * (remainder - 1) + b"\n")
            stream.write(launch)
        self.assertEqual(self.transcript.stat().st_size, 50 << 20)
        self.assertEqual(self.stop(transcript_path=str(self.transcript)), [])
        self.assertLess(self.elapsed, 1.0, f"50 MiB transcript took {self.elapsed:.3f}s")


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Desktop attention for PreToolUse (AskUserQuestion), Stop and Notification.

Pickers, completed turns and permission/elicitation prompts mark this workspace's
inactive Cursor window as demanding attention through KDE Plasma's qdbus6/KWin.
A picker offering /clear also announces a handoff with notify-send; other pickers
and permissions use it only if flashing fails. Completed turns never pop up.
Stop ignores recursive hooks and background tasks launched in the last 3 hours
that have no later task notification in the last 8 MiB of the transcript.

Env: CURSOR_WORKSPACE_LABEL (Cursor's default workspace title), ATTENTION_DISABLE=1
(off), CLAUDE_CODE_ENTRYPOINT=sdk-* (off for headless sessions). All session modes
are eligible; this hook adds no context or permission decision. Stdlib Python 3.9+,
1 s per subprocess (three hung calls stay under install.sh's 5 s timeout),
no stdout, exit 0 on errors, and no persistent state.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


TAIL_BYTES = 8 << 20
LAUNCH = re.compile(r"Command running in background with ID: ([A-Za-z0-9]+)")
FINISHED = re.compile(r"<task-id>([A-Za-z0-9]+)</task-id>")


def _timestamp(value):
    """Python 3.9's fromisoformat needs +00:00 instead of a trailing Z."""
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _result_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(block["text"] for block in content
                         if isinstance(block, dict) and block.get("type") == "text"
                         and isinstance(block.get("text"), str))
    return ""


def background_pending(path):
    """Track recent launches and later genuine notifications, in transcript order."""
    if not path:
        return False
    try:
        with open(path, "rb") as stream:
            size = stream.seek(0, os.SEEK_END)
            stream.seek(max(0, size - TAIL_BYTES))
            tail = stream.read(TAIL_BYTES)
        if size > TAIL_BYTES:
            tail = tail.partition(b"\n")[2]  # discard the possibly partial first line
    except (OSError, ValueError, TypeError):
        return False
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=3)
    pending = set()
    for raw in tail.splitlines():
        # Most rows cannot change pending tasks. Keep Unicode-escaped JSON eligible
        # too: even an ASCII letter in a launch/tag can be written as \uXXXX.
        if (b"Command running in background with ID:" not in raw
                and b"<task-id>" not in raw and b"\\u" not in raw):
            continue
        try:
            row = json.loads(raw)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(row, dict):
            continue
        kind = row.get("type")
        message = row.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if kind == "user" and isinstance(content, list):
            stamp = _timestamp(row.get("timestamp"))
            if stamp is not None and cutoff <= stamp <= now:
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        pending.update(LAUNCH.findall(_result_text(block.get("content"))))
        # Assistant quotes and tool_result text cannot complete tasks (real transcripts,
        # 2026-10-30). Only system task notifications and plain user text can do so.
        elif kind in ("attachment", "queue-operation"):
            pending.difference_update(FINISHED.findall(json.dumps(row)))
        elif kind == "user" and isinstance(content, str):
            pending.difference_update(FINISHED.findall(content))
    return bool(pending)


def reason_and_body(data):
    event = data.get("hook_event_name")
    if event == "PreToolUse" and data.get("tool_name") == "AskUserQuestion":
        tool_input = data.get("tool_input")
        questions = tool_input.get("questions") if isinstance(tool_input, dict) else None
        questions = questions if isinstance(questions, list) else []
        body = questions[0].get("question", "") if questions and isinstance(questions[0], dict) else ""
        for question in questions:
            options = question.get("options") if isinstance(question, dict) else None
            for option in options if isinstance(options, list) else []:
                label = option.get("label") if isinstance(option, dict) else None
                if isinstance(label, str) and "/clear" in label:
                    return "handoff", body
        return "picker", body
    if event == "Stop":
        if data.get("stop_hook_active") is True or background_pending(data.get("transcript_path")):
            return None, ""
        return "turn-end", ""
    if event == "Notification" and data.get("notification_type") in (
            "permission_prompt", "elicitation_dialog"):
        return "permission", data.get("message", "")
    return None, ""


def _run(argv):
    """Capture all output; even a broken desktop tool must not affect the session."""
    try:
        result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, timeout=1, check=True)
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None


def window_script(label):
    # Only this JSON string literal contains workspace data; the JS stays constant.
    return "const LABEL = " + json.dumps(label, ensure_ascii=True) + ";\n" + """\
for (const w of workspace.windowList()) {
    if (String(w.resourceClass).toLowerCase() === "cursor" && !w.active &&
        (w.caption === LABEL + " - Cursor" ||
         w.caption.endsWith(" - " + LABEL + " - Cursor"))) {
        w.demandsAttention = true;
    }
}
"""


def flash(label):
    qdbus = shutil.which("qdbus6") if label else None
    if not qdbus:
        return False
    name = f"claude-attention-{os.getpid()}-{time.time_ns()}"
    path, attempted, success = None, False, False
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="ascii", prefix="claude-attention-",
                                         suffix=".js", delete=False) as stream:
            path = Path(stream.name)
            stream.write(window_script(label))
        attempted = True
        script_id = _run([qdbus, "org.kde.KWin", "/Scripting",
                          "org.kde.kwin.Scripting.loadScript", str(path), name])
        if script_id is not None and re.fullmatch(r"[0-9]+", script_id):
            success = _run([qdbus, "org.kde.KWin", "/Scripting/Script" + script_id,
                            "org.kde.kwin.Script.run"]) is not None
    except (OSError, ValueError):
        pass
    finally:
        if attempted:
            unloaded = _run([qdbus, "org.kde.KWin", "/Scripting",
                             "org.kde.kwin.Scripting.unloadScript", name]) is not None
            success = success and unloaded
        if path is not None:
            try:
                path.unlink()
            except OSError:
                pass
    return success


def main():
    try:
        if (os.environ.get("ATTENTION_DISABLE") == "1"
                or os.environ.get("CLAUDE_CODE_ENTRYPOINT", "").startswith("sdk-")):
            return
        data = json.load(sys.stdin)
        if not isinstance(data, dict):
            return
        reason, body = reason_and_body(data)
        if reason is None:
            return
        label = os.environ.get("CURSOR_WORKSPACE_LABEL", "")
        flashed = flash(label)
        if reason == "handoff" or (reason in ("picker", "permission") and not flashed):
            notify = shutil.which("notify-send")
            if notify:
                name = label or Path(data.get("cwd") or "").name
                summary = (f"{name}: ready for /clear, then /next" if reason == "handoff"
                           else f"{name}: needs input")
                _run([notify, "--app-name=Claude Code", "--", summary,
                      body[:200] if isinstance(body, str) else ""])
    except Exception:  # no desktop failure may block a turn or decide a permission
        pass


if __name__ == "__main__":
    main()

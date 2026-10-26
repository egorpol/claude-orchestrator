#!/usr/bin/env python3
"""UserPromptSubmit hook: nudge toward a handoff once a session's context gets large.

Every API call re-reads the whole conversation, so a session's cost grows roughly
quadratically with its length (2026-10-09: one 13-hour session grew to 774k tokens
and would have cost ~60% less as one session per work item). The hook reads the
last assistant usage from the transcript tail and, each time the context crosses
FIRST, FIRST+STEP, FIRST+2*STEP, ... tokens, injects one reminder for Claude and
shows one line to the user.

It also blocks the first prompt after a long idle when the context is large (prompt
cache expired; a resend of the same prompt is allowed through). Prompts that start
with < or / (task notifications, /loop turns) always pass. On any error it
stays silent and exits 0 so a broken guard never gets in the way of a prompt.

In orchestrator sessions (not excluded) it also nudges once toward `delegate` when
main-thread tool calls reach CONTEXT_GUARD_NUDGE_CALLS and a delegate pool is ahead
of pace, if the session has no `delegate` log rows yet.

It runs on every prompt, which is where work items start. At 250k it fired too
late: in four 2026-10-10 sessions new items started at 87-219k, and none of them
got a reminder in time. When it did fire, each session wrote a handoff.

Env: CONTEXT_GUARD_FIRST (default 120000), CONTEXT_GUARD_STEP (60000),
CONTEXT_GUARD_IDLE_MIN (60), CONTEXT_GUARD_IDLE_CONTEXT (100000),
CONTEXT_GUARD_DISABLE=1, CONTEXT_GUARD_STATE (state dir),
CONTEXT_GUARD_NUDGE=0 (disable mid-session delegate nudge),
CONTEXT_GUARD_NUDGE_CALLS (default 30).
"""
import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:  # a bad setting must not break every prompt
        return default


FIRST = _env_int("CONTEXT_GUARD_FIRST", 120_000)
STEP = _env_int("CONTEXT_GUARD_STEP", 60_000)
IDLE_MIN = _env_int("CONTEXT_GUARD_IDLE_MIN", 60)
IDLE_CONTEXT = _env_int("CONTEXT_GUARD_IDLE_CONTEXT", 100_000)
NUDGE_CALLS = _env_int("CONTEXT_GUARD_NUDGE_CALLS", 30)
STATE = Path(os.environ.get("CONTEXT_GUARD_STATE",
                            Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
                            / "claude-delegate" / "context-guard"))
ROOT = Path(__file__).resolve().parent.parent


def _parse_ts(s):
    """Parse an ISO timestamp; Python 3.9 fromisoformat rejects a trailing Z."""
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def last_context(path):
    """Newest main-thread API call: (context tokens, timestamp str or None), or None."""
    size = path.stat().st_size
    with path.open("rb") as f:
        for window in (1 << 20, 8 << 20):  # tool-result lines can be large; widen once
            f.seek(max(0, size - window))
            lines = f.read().splitlines()
            for raw in reversed(lines):
                if b'"usage"' not in raw or b'"assistant"' not in raw:
                    continue
                try:
                    rec = json.loads(raw)
                except ValueError:  # first line of the window may be cut
                    continue
                msg = rec.get("message") or {}
                if rec.get("type") != "assistant" or rec.get("isSidechain") or msg.get("model") == "<synthetic>":
                    continue
                u = msg.get("usage") or {}
                ctx = (u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
                       + u.get("cache_creation_input_tokens", 0))
                return ctx, rec.get("timestamp")
            if window >= size:
                break
    return None


def _idle_block(session, ctx, ts_str):
    """Block once per idle period when cache has likely expired. Returns True if blocked."""
    ts = _parse_ts(ts_str)
    if ts is None:
        return False
    now = datetime.now(timezone.utc)
    if ts > now:
        return False
    idle_min = int((now - ts).total_seconds() // 60)
    if idle_min < IDLE_MIN or ctx < IDLE_CONTEXT:
        return False
    STATE.mkdir(parents=True, exist_ok=True)
    marker = STATE / f"{session}.idle"
    prev = marker.read_text().strip() if marker.exists() else ""
    if prev == ts_str:
        return False  # resend after a block for this assistant record
    marker.write_text(ts_str)
    k = ctx // 1000
    reason = (
        f"context guard: idle {idle_min} min, so the cache has expired and this prompt would rewrite ~{k}k "
        f"tokens at twice the input price.\n"
        f"If the handoff is current, /clear, then /next to continue from it "
        f"(a fresh session starts at ~35k); otherwise send the prompt again to go ahead here."
    )
    print(json.dumps({"decision": "block", "reason": reason}))
    return True


def session_log_path():
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return Path(os.environ.get("CLAUDE_SESSION_LOG", cache / "claude-delegate/sessions.jsonl"))


def _latest_start(path, session):
    latest = None
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if (isinstance(row, dict) and row.get("event") == "start"
                        and row.get("session") == session):
                    latest = row
    except (OSError, ValueError):
        pass
    return latest


def _has_delegate_row(path, session):
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                # Cheap prefilter before json.loads on every session-log line.
                if '"delegate"' not in line or '"event"' not in line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if (isinstance(row, dict) and row.get("event") == "delegate"
                        and row.get("session") == session):
                    return True
    except (OSError, ValueError):
        pass
    return False


def _load_quota():
    path = ROOT / "skills/delegate/scripts/quota.py"
    spec = importlib.util.spec_from_file_location("context_guard_quota", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _spare_info(log):
    try:
        quota = _load_quota()
        return quota.spare_pools(log.parent / "quota.jsonl"), quota
    except Exception:
        return None, None


def _tool_call_count(transcript, session):
    """Unique main-thread tool_use block ids; incremental offset cache in STATE."""
    cache_path = STATE / f"{session}.tools"
    offset, ids = 0, set()
    try:
        if cache_path.is_file():
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(cached, dict):
                offset = int(cached.get("offset") or 0)
                ids = set(cached.get("ids") or [])
    except (OSError, ValueError, TypeError):
        offset, ids = 0, set()
    if len(ids) >= NUDGE_CALLS:
        return len(ids)
    try:
        size = transcript.stat().st_size
        if offset > size:
            offset, ids = 0, set()
        with transcript.open("rb") as stream:
            stream.seek(offset)
            while True:
                raw = stream.readline()
                if not raw:
                    break
                # Mid-write last line (no trailing newline): leave offset so the next prompt
                # re-reads the whole line once it is complete.
                if not raw.endswith(b"\n"):
                    break
                offset = stream.tell()
                if b'"tool_use"' not in raw or b'"assistant"' not in raw:
                    continue
                if b'"isSidechain": true' in raw or b'"isSidechain":true' in raw:
                    continue
                try:
                    rec = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(rec, dict) or rec.get("type") != "assistant" or rec.get("isSidechain"):
                    continue
                message = rec.get("message")
                content = message.get("content") if isinstance(message, dict) else None
                if not isinstance(content, list):
                    continue
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        block_id = block.get("id")
                        if isinstance(block_id, str):
                            ids.add(block_id)
                if len(ids) >= NUDGE_CALLS:
                    break
        STATE.mkdir(parents=True, exist_ok=True)
        payload = {"offset": offset, "ids": sorted(ids)[:NUDGE_CALLS]}
        cache_path.write_text(json.dumps(payload), encoding="utf-8")
    except (OSError, ValueError, TypeError):
        return len(ids)
    return len(ids)


def _delegate_nudge(session, transcript):
    """One-shot mid-session nudge, or None. Cheap checks before the transcript scan."""
    if os.environ.get("CONTEXT_GUARD_NUDGE") == "0":
        return None
    marker = STATE / f"{session}.nudge"
    if marker.exists():
        return None
    log = session_log_path()
    start = _latest_start(log, session)
    if (not isinstance(start, dict) or start.get("mode") != "orchestrator"
            or start.get("excluded")):
        return None
    if _has_delegate_row(log, session):
        return None
    info, quota = _spare_info(log)
    if not info or quota is None:
        return None
    line = quota.format_spare_line(info)
    if not line:
        return None
    # Qualifying pool names for the nudge text (before the expensive scan).
    labels = []
    for pid, _, _ in info["pools"]:
        labels.append("Codex 7d" if pid == "codex-7d" else "Cursor Auto")
    if not labels:
        return None
    if _tool_call_count(Path(transcript), session) < NUDGE_CALLS:
        return None
    STATE.mkdir(parents=True, exist_ok=True)
    try:
        marker.write_text("1", encoding="utf-8")
    except OSError:
        return None
    named = " and ".join(labels) if len(labels) <= 2 else ", ".join(labels[:-1]) + ", and " + labels[-1]
    additional = (
        f"Delegation nudge: {named} {'is' if len(labels) == 1 else 'are'} ahead of pace. "
        f"Hand the next build, test or doc step to `delegate`, or say in one line why the "
        f"remaining work is judgement, data checks or small edits. ({line})"
    )
    system = f"context guard: {named} spare — consider `delegate` for the next build/test/doc step."
    return system, additional


def _size_reminder(session, ctx):
    level = 1 + (ctx - FIRST) // STEP
    STATE.mkdir(parents=True, exist_ok=True)
    marker = STATE / session
    seen = int(marker.read_text() or 0) if marker.exists() else 0
    if level <= seen:
        return None
    marker.write_text(str(level))
    k = ctx // 1000
    system = f"context guard: ~{k}k tokens; a new work item is cheaper after a handoff, /clear and /next."
    additional = (
        f"Context guard: this session's context is ~{k}k tokens and every call re-reads all of it "
        "(a fresh session starts at ~35k). Decide whether this prompt starts a new work item: the "
        "previous item is committed, merged or handed off, the user turns to a different topic, or a "
        "finished build turns into Q&A support or a hand-paste loop. If it does, do not start it here, even after 'go on'. "
        "First write or refresh the handoff (memory files plus a short HANDOFF.md: state, open PRs or "
        "delegate runs, decided cases, next step), then offer '/clear, then /next' in one line (/next "
        "continues from HANDOFF.md; a command can't run /clear itself). Start in place only if the user then says so. If the prompt continues the current "
        "item, carry on: batch independent tool calls, wait for background-run notifications instead "
        "of polling, hand mechanical parts with a clear spec (tests from a spec, multi-file doc "
        "refreshes) to `delegate`, keep probe and test output to counts and pass/fail lines, and "
        "read large files through grep and line ranges. If the item has a step boundary ahead (the "
        "next guided step, one of several deliverables), hand off there and list the rest."
    )
    return system, additional


def main():
    if os.environ.get("CONTEXT_GUARD_DISABLE") == "1":
        return
    data = json.load(sys.stdin)
    # cursor-agent runs ~/.claude/settings.json hooks under its own event names (beforeSubmitPrompt).
    if data.get("hook_event_name", "UserPromptSubmit") != "UserPromptSubmit":
        return
    transcript, session = data.get("transcript_path"), data.get("session_id") or "unknown"
    if not transcript or not Path(transcript).is_file():
        return
    found = last_context(Path(transcript))
    ctx, ts_str = (found if found else (None, None))
    # UserPromptSubmit also fires for task notifications, /loop turns and subagent reports, and a
    # held prompt never reaches Claude: hold only what looks typed (2026-10-12, hooks docs).
    prompt = str(data.get("prompt") or "").lstrip()
    typed = bool(prompt) and not prompt.startswith(("<", "/"))
    if ctx and typed and _idle_block(session, ctx, ts_str):
        return
    size = _size_reminder(session, ctx) if ctx and ctx >= FIRST else None
    nudge = _delegate_nudge(session, transcript)
    if not size and not nudge:
        return
    system_parts, context_parts = [], []
    if size:
        system_parts.append(size[0])
        context_parts.append(size[1])
    if nudge:
        system_parts.append(nudge[0])
        context_parts.append(nudge[1])
    print(json.dumps({
        "systemMessage": " | ".join(system_parts),
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "\n\n".join(context_parts),
        },
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception:  # a broken guard must never get in the way of a prompt
        pass

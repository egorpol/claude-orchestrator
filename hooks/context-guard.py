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

It runs on every prompt, which is where work items start. At 250k it fired too
late: in four 2026-10-10 sessions new items started at 87-219k, and none of them
got a reminder in time. When it did fire, each session wrote a handoff.

Env: CONTEXT_GUARD_FIRST (default 120000), CONTEXT_GUARD_STEP (60000),
CONTEXT_GUARD_IDLE_MIN (60), CONTEXT_GUARD_IDLE_CONTEXT (100000),
CONTEXT_GUARD_DISABLE=1, CONTEXT_GUARD_STATE (state dir).
"""
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
STATE = Path(os.environ.get("CONTEXT_GUARD_STATE",
                            Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
                            / "claude-delegate" / "context-guard"))


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
    if not found:
        return
    ctx, ts_str = found
    # UserPromptSubmit also fires for task notifications, /loop turns and subagent reports, and a
    # held prompt never reaches Claude: hold only what looks typed (2026-10-12, hooks docs).
    prompt = str(data.get("prompt") or "").lstrip()
    typed = bool(prompt) and not prompt.startswith(("<", "/"))
    if ctx and typed and _idle_block(session, ctx, ts_str):
        return
    if not ctx or ctx < FIRST:
        return
    level = 1 + (ctx - FIRST) // STEP
    STATE.mkdir(parents=True, exist_ok=True)
    marker = STATE / session
    seen = int(marker.read_text() or 0) if marker.exists() else 0
    if level <= seen:
        return
    marker.write_text(str(level))
    k = ctx // 1000
    print(json.dumps({
        "systemMessage": f"context guard: ~{k}k tokens; a new work item is cheaper after a handoff, /clear and /next.",
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": (
                f"Context guard: this session's context is ~{k}k tokens and every call re-reads all of it "
                "(a fresh session starts at ~35k). Decide whether this prompt starts a new work item: the "
                "previous item is committed, merged or handed off, the user turns to a different topic, or a "
                "finished build turns into Q&A support. If it does, do not start it here, even after 'go on'. "
                "First write or refresh the handoff (memory files plus a short HANDOFF.md: state, open PRs or "
                "delegate runs, decided cases, next step), then offer '/clear, then /next' in one line (/next "
                "continues from HANDOFF.md; a command can't run /clear itself). Start in place only if the user then says so. If the prompt continues the current "
                "item, carry on: batch independent tool calls, wait for background-run notifications instead "
                "of polling, and hand mechanical parts with a clear spec (tests from a spec, multi-file doc "
                "refreshes) to `delegate`."
            ),
        },
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception:  # a broken guard must never get in the way of a prompt
        pass

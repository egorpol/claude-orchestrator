#!/usr/bin/env python3
"""UserPromptSubmit hook: nudge toward a handoff once a session's context gets large.

Every API call re-reads the whole conversation, so a session's cost grows roughly
quadratically with its length (2026-10-09: one 13-hour session grew to 774k tokens
and would have cost ~60% less as one session per work item). The hook reads the
last assistant usage from the transcript tail and, each time the context crosses
FIRST, FIRST+STEP, FIRST+2*STEP, ... tokens, injects one reminder for Claude and
shows one line to the user. It never blocks and stays silent on any error.

Env: CONTEXT_GUARD_FIRST (default 250000), CONTEXT_GUARD_STEP (150000),
CONTEXT_GUARD_DISABLE=1, CONTEXT_GUARD_STATE (state dir).
"""
import json
import os
import sys
from pathlib import Path

FIRST = int(os.environ.get("CONTEXT_GUARD_FIRST", 250_000))
STEP = int(os.environ.get("CONTEXT_GUARD_STEP", 150_000))
STATE = Path(os.environ.get("CONTEXT_GUARD_STATE",
                            Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
                            / "claude-delegate" / "context-guard"))


def last_context(path):
    """Context size (input + cache read + cache write) of the newest main-thread API call."""
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
                return (u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
                        + u.get("cache_creation_input_tokens", 0))
            if window >= size:
                break
    return None


def main():
    if os.environ.get("CONTEXT_GUARD_DISABLE") == "1":
        return
    data = json.load(sys.stdin)
    transcript, session = data.get("transcript_path"), data.get("session_id") or "unknown"
    if not transcript or not Path(transcript).is_file():
        return
    ctx = last_context(Path(transcript))
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
        "systemMessage": f"context guard: this session is at ~{k}k tokens; every turn re-reads all of it.",
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": (
                f"Context guard: this session's context is ~{k}k tokens and every call re-reads all of it "
                "(a fresh session starts at ~35k). If the current work item is finished, write a handoff "
                "(memory files plus a short HANDOFF.md: state, open PRs or delegate runs, next step) and "
                "suggest that the user start a fresh session or run /clear. If the item is still in progress, "
                "carry on, but batch independent tool calls, wait for background-run notifications instead of "
                "polling, and send large reads to `delegate ask` instead of reading them here."
            ),
        },
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception:  # a broken guard must never get in the way of a prompt
        pass

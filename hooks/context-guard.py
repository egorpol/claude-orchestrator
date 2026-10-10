#!/usr/bin/env python3
"""UserPromptSubmit hook: nudge toward a handoff once a session's context gets large.

Every API call re-reads the whole conversation, so a session's cost grows roughly
quadratically with its length (2026-10-09: one 13-hour session grew to 774k tokens
and would have cost ~60% less as one session per work item). The hook reads the
last assistant usage from the transcript tail and, each time the context crosses
FIRST, FIRST+STEP, FIRST+2*STEP, ... tokens, injects one reminder for Claude and
shows one line to the user. It never blocks and stays silent on any error.

It runs on every prompt, which is where work items start. At 250k it fired too
late: in four 2026-10-10 sessions new items started at 87-219k, and none of them
got a reminder in time. When it did fire, each session wrote a handoff.

Env: CONTEXT_GUARD_FIRST (default 120000), CONTEXT_GUARD_STEP (60000),
CONTEXT_GUARD_DISABLE=1, CONTEXT_GUARD_STATE (state dir).
"""
import json
import os
import sys
from pathlib import Path

FIRST = int(os.environ.get("CONTEXT_GUARD_FIRST", 120_000))
STEP = int(os.environ.get("CONTEXT_GUARD_STEP", 60_000))
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
        "systemMessage": f"context guard: ~{k}k tokens; a new work item is cheaper after a handoff and /clear.",
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": (
                f"Context guard: this session's context is ~{k}k tokens and every call re-reads all of it "
                "(a fresh session starts at ~35k). Decide whether this prompt starts a new work item: the "
                "previous item is committed, merged or handed off, the user turns to a different topic, or a "
                "finished build turns into Q&A support. If it does, do not start it here, even after 'go on'. "
                "First write or refresh the handoff (memory files plus a short HANDOFF.md: state, open PRs or "
                "delegate runs, decided cases, next step), then offer /clear and 'continue from the handoff' in "
                "one line. Start in place only if the user then says so. If the prompt continues the current "
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

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

If the session has no `start` log row, its first prompt through the idle guard gets
the spare-pool line in Claude's context when a delegate pool is ahead of pace. A
missing `start` row also qualifies the session as orchestrator in any repo. In
eligible orchestrator sessions, the hook nudges once toward `delegate` when
main-thread tool calls reach CONTEXT_GUARD_NUDGE_CALLS, a pool is ahead of pace,
and the session has no `delegate` log rows yet. Excluded start rows suppress the
nudge, and all start rows leave the first-prompt line to the session-mode hook.

It runs on every prompt, which is where work items start. At 250k it fired too
late: in four 2026-10-10 sessions new items started at 87-219k, and none of them
got a reminder in time. When it did fire, each session wrote a handoff.

Env: CONTEXT_GUARD_FIRST (default 120000), CONTEXT_GUARD_STEP (60000),
CONTEXT_GUARD_IDLE_MIN (60), CONTEXT_GUARD_IDLE_CONTEXT (100000),
CONTEXT_GUARD_DISABLE=1, CONTEXT_GUARD_STATE (state dir),
CONTEXT_GUARD_NUDGE=0 (disable the first-prompt spare line and mid-session delegate nudge),
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


def _load_fresh_start():
    path = ROOT / "skills/delegate/scripts/fresh_start.py"
    spec = importlib.util.spec_from_file_location("context_guard_fresh_start", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fresh_parenthetical(transcript_path):
    """`(a fresh session here starts at ~Nk)` from peer transcripts, or empty."""
    try:
        mod = _load_fresh_start()
        path = Path(transcript_path)
        tokens = mod.median_fresh_start(path.parent, exclude=path)
        if tokens is None:
            return ""
        return f" (a fresh session here starts at ~{tokens // 1000}k)"
    except Exception:
        return ""


def _idle_block(session, ctx, ts_str, transcript_path=None):
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
    fresh = _fresh_parenthetical(transcript_path) if transcript_path else ""
    reason = (
        f"context guard: idle {idle_min} min, so the cache has expired and this prompt would rewrite ~{k}k "
        f"tokens at twice the input price.\n"
        f"If the handoff is current, /clear, then /next to continue from it"
        f"{fresh}; otherwise send the prompt again to go ahead here."
    )
    print(json.dumps({"decision": "block", "reason": reason}))
    return True


def session_log_path():
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return Path(os.environ.get("CLAUDE_SESSION_LOG", cache / "claude-delegate/sessions.jsonl"))


def _session_log_status(path, session):
    """Latest start row and whether the session has delegated, in one pass."""
    start, delegated = None, False
    escaped = json.dumps(session)[1:-1]
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                # The log writers escape IDs as JSON. Real session UUIDs match verbatim.
                if escaped not in line and (escaped == session or session not in line):
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict) or row.get("session") != session:
                    continue
                if row.get("event") == "start":
                    start = row
                elif row.get("event") == "delegate":
                    delegated = True
    except FileNotFoundError:
        pass
    return start, delegated


def _load_quota():
    path = ROOT / "skills/delegate/scripts/quota.py"
    spec = importlib.util.spec_from_file_location("context_guard_quota", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _spare_quota(log):
    try:
        quota = _load_quota()
        info = quota.spare_pools(log.parent / "quota.jsonl")
        return info, quota.format_spare_line(info)
    except Exception:
        return None, None


def _claim_marker(marker):
    """Record a one-time decision, even if two prompts check at the same time."""
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        marker.touch(exist_ok=False)
    except OSError:
        return False
    return True


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


def _delegate_nudge(marker, info, line):
    """One-shot mid-session nudge using the already checked quota reading."""
    labels = ["Codex 7d" if pid == "codex-7d" else "Cursor Auto" for pid, _, _ in info["pools"]]
    if not labels or not _claim_marker(marker):
        return None
    named = " and ".join(labels) if len(labels) <= 2 else ", ".join(labels[:-1]) + ", and " + labels[-1]
    additional = (
        f"Delegation nudge: {named} {'is' if len(labels) == 1 else 'are'} ahead of pace. "
        f"Hand the next build, test or doc step to `delegate`, or say in one line why the "
        f"remaining work is judgement, data checks or small edits. ({line})"
    )
    system = f"context guard: {named} spare — consider `delegate` for the next build/test/doc step."
    return system, additional


def _delegation_pushes(session, transcript):
    """Return the first-prompt spare line and mid-session nudge, if due."""
    if os.environ.get("CONTEXT_GUARD_NUDGE") == "0":
        return None, None
    spare_marker = STATE / f"{session}.spare"
    nudge_marker = STATE / f"{session}.nudge"
    first_prompt = not spare_marker.exists() and _claim_marker(spare_marker)
    nudge_due = (not nudge_marker.exists() and transcript is not None
                 and _tool_call_count(transcript, session) >= NUDGE_CALLS)
    if not first_prompt and not nudge_due:
        return None, None

    log = session_log_path()
    try:
        start, delegated = _session_log_status(log, session)
    except (OSError, ValueError):
        return None, None
    show_spare = first_prompt and start is None
    orchestrator = start is None or (start.get("mode") == "orchestrator" and not start.get("excluded"))
    nudge_due = nudge_due and orchestrator and not delegated
    if not show_spare and not nudge_due:
        return None, None

    info, line = _spare_quota(log)
    if not line:
        return None, None
    nudge = _delegate_nudge(nudge_marker, info, line) if nudge_due else None
    return (line if show_spare and nudge is None else None), nudge


def _size_reminder(session, ctx, transcript_path=None):
    level = 1 + (ctx - FIRST) // STEP
    STATE.mkdir(parents=True, exist_ok=True)
    marker = STATE / session
    seen = int(marker.read_text() or 0) if marker.exists() else 0
    if level <= seen:
        return None
    marker.write_text(str(level))
    k = ctx // 1000
    fresh = _fresh_parenthetical(transcript_path) if transcript_path else ""
    system = f"context guard: ~{k}k tokens; a new work item is cheaper after a handoff, /clear and /next."
    additional = (
        f"Context guard: this session's context is ~{k}k tokens and every call re-reads all of it"
        f"{fresh}. Decide whether this prompt starts a new work item: the "
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
    transcript_path, session_id = data.get("transcript_path"), data.get("session_id")
    session = session_id or "unknown"
    transcript = Path(transcript_path) if transcript_path else None
    if transcript is not None and not transcript.is_file():
        transcript = None
    found = last_context(transcript) if transcript is not None else None
    ctx, ts_str = (found if found else (None, None))
    # UserPromptSubmit also fires for task notifications, /loop turns and subagent reports, and a
    # held prompt never reaches Claude: hold only what looks typed (2026-10-12, hooks docs).
    prompt = str(data.get("prompt") or "").lstrip()
    typed = bool(prompt) and not prompt.startswith(("<", "/"))
    if ctx and typed and _idle_block(session, ctx, ts_str, transcript):
        return
    size = _size_reminder(session, ctx, transcript) if ctx and ctx >= FIRST else None
    spare, nudge = (None, None)
    if isinstance(session_id, str) and session_id:
        spare, nudge = _delegation_pushes(session, transcript)
    system_parts, context_parts = [], []
    if size:
        system_parts.append(size[0])
        context_parts.append(size[1])
    if spare:
        context_parts.append(spare)
    if nudge:
        system_parts.append(nudge[0])
        context_parts.append(nudge[1])
    if not context_parts:
        return
    output = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "\n\n".join(context_parts),
        },
    }
    if system_parts:
        output["systemMessage"] = " | ".join(system_parts)
    print(json.dumps(output))


if __name__ == "__main__":
    try:
        main()
    except Exception:  # a broken guard must never get in the way of a prompt
        pass

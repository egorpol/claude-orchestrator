#!/usr/bin/env python3
"""Collect exact session numbers and write a field-report skeleton, or a session summary row."""
import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
from bisect import bisect_left
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import cost  # noqa: E402
import quota  # noqa: E402
import rate  # noqa: E402
import runs  # noqa: E402

IDLE_SECONDS = 3600  # back-fill treats a session whose start and transcript are this quiet as over
END_GRACE_SECONDS = 600  # SessionEnd's own summary (quota reading first) may still be running


def _fail(message, code=1):
    print(f"report: {message}", file=sys.stderr)
    sys.exit(code)


def feedback_dir(explicit=None):
    chosen = explicit or os.environ.get("CLAUDE_ORCHESTRATOR_FEEDBACK")
    if chosen:
        return Path(chosen).expanduser().resolve()
    root = Path(__file__).resolve().parents[3]
    if (root / "skills/delegate/SKILL.md").is_file() and (root / ".git").exists():
        return (root / "feedback").resolve()
    _fail("set --feedback-dir or CLAUDE_ORCHESTRATOR_FEEDBACK", 2)


def _git(directory, *args):
    try:
        result = subprocess.run(
            ["git", "-C", str(directory), *args],
            capture_output=True, text=True, check=False,
        )
    except OSError:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def git_toplevel(directory):
    top = _git(directory, "rev-parse", "--show-toplevel")
    return Path(top).resolve() if top else None


def github_repo(remote):
    if not remote:
        return None
    if remote.startswith(("https://", "ssh://")):
        url = urlsplit(remote)
        if url.hostname != "github.com":
            return None
        path = url.path.strip("/")
    else:
        match = re.fullmatch(r"(?:[^@/:]+@)?github\.com:(.+)", remote)
        if not match:
            return None
        path = match.group(1).strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = path.split("/")
    return path if len(parts) == 2 and all(parts) else None


def repo_identity(explicit=None):
    cwd = Path.cwd().resolve()
    top = git_toplevel(cwd)
    directory = top or cwd
    remote = _git(top, "remote", "get-url", "origin") if top else None
    return explicit or github_repo(remote) or directory.name, directory


def _output_path(directory, name):
    path = directory / name
    if path.resolve().parent != directory:
        _fail(f"output path escapes feedback directory: {path}", 2)
    return path


def _json_rows(data):
    for line in data.splitlines(keepends=True):
        try:
            obj = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            obj = None
        yield line, obj if isinstance(obj, dict) else None


def _read_bytes(path):
    return path.read_bytes() if path.exists() else b""


def _encoded_row(row):
    return (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def _append_bytes(data, addition):
    if addition and data and not data.endswith(b"\n"):
        data += b"\n"
    return data + addition


def _same_session(logged, full):
    # Hand-written rows before 0.1.0 store an 8-char prefix of the session id.
    return isinstance(logged, str) and len(logged) >= 8 and full.startswith(logged)


def upsert_usage(data, row):
    lines = []
    replaced = False
    for line, obj in _json_rows(data):
        if (not replaced and obj and obj.get("kind") == "orchestrator_session"
                and _same_session(obj.get("session"), row["session"])):
            lines.append(_encoded_row(row))
            replaced = True
        else:
            lines.append(line)
    if replaced:
        return b"".join(lines), "row replaced"
    return _append_bytes(data, _encoded_row(row)), "row appended"


def session_id(path):
    # The filename normally is the UUID; sessionId also supports renamed PATHs.
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict) and obj.get("sessionId"):
                return obj["sessionId"]
    return path.stem


def _call_time(call):
    ts = cost._parse_ts(call.get("timestamp"))
    if ts is None:
        _fail("session's first and last calls need timestamps")
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def quota_end(no_quota):
    if no_quota:
        return None
    try:
        result = quota.pools(quota.fetch("auto"))
        return {pool: round(entry["left_pct"], 1) for pool, entry in result.items()
                if entry.get("left_pct") is not None}
    except Exception as exc:
        message = " ".join(str(exc).split()) or type(exc).__name__
        return {"error": message[:200]}


def matching_runs(directory, first_call):
    rows = cost.load_run_log(cost.DEFAULT_LOG, since=first_call.isoformat())
    tops = {}
    matched = []
    seen = set()
    for row in rows:
        matches = row.get("repo") == directory.name
        if not matches and row.get("repo_path"):
            path = Path(row["repo_path"]).expanduser().resolve()
            if path not in tops:
                tops[path] = path if path == directory else git_toplevel(path)
            matches = tops[path] == directory
        pair = (row.get("run"), row.get("ts"))
        if matches and pair not in seen:
            matched.append(row)
            seen.add(pair)
    return matched


def _meta_repo_matches(meta, directory):
    """True when meta.json `repo` is this checkout (not another repo that shares a basename)."""
    raw = meta.get("repo")
    if not raw:
        return False
    path = Path(raw).expanduser().resolve()
    if path == directory:
        return True
    top = git_toplevel(path)
    return top == directory


def _ts_in_window(value, first_call, last_call):
    ts = _aware(value) if isinstance(value, str) else None
    if ts is None:
        return False
    return first_call <= ts <= last_call


def _read_started(path):
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _live_run_touched(root, meta, session, first_call, last_call):
    """True when this session owns the run, or a round/review started in the session window."""
    if _same_session(meta.get("session"), session):
        return True
    for row in meta.get("history") or []:
        if _ts_in_window(row.get("started"), first_call, last_call):
            return True
    # Current round's start (not yet folded into history) uses the same started_at file.
    if _ts_in_window(_read_started(root / "started_at"), first_call, last_call):
        return True
    for review_dir in sorted(root.glob("review-*")):
        started = _read_started(review_dir / "started_at")
        if started is None and (review_dir / "meta.json").is_file():
            try:
                started = json.loads((review_dir / "meta.json").read_text(encoding="utf-8")).get("started")
            except (OSError, ValueError):
                started = None
        if _ts_in_window(started, first_call, last_call):
            return True
    return False


def _live_run_row(root, meta):
    """Display row for a live run; never written to runs.jsonl."""
    history = list(meta.get("history") or [])
    raw_rounds = meta.get("rounds")
    if isinstance(raw_rounds, int) and raw_rounds > 0:
        n_rounds = raw_rounds
    else:
        n_rounds = len(history) or 1
    # Placeholder list so render_skeleton's len(rounds) matches the current round count.
    rounds = [{"n": i} for i in range(1, n_rounds + 1)]
    verify = None
    hist_row = next((row for row in history if row.get("n") == n_rounds), None)
    if hist_row is not None and hist_row.get("verify") is not None:
        verify = hist_row.get("verify")
    else:
        try:
            raw = (root / "verify_code").read_text(encoding="utf-8").strip()
            verify = int(raw) if raw else None
        except (OSError, ValueError):
            verify = None
    row = {
        "run": meta.get("name") or root.name,
        "agent": meta.get("agent"),
        "model": meta.get("model"),
        "rounds": rounds,
        "verify": verify,
        "session": meta.get("session"),
        "live": True,
    }
    if meta.get("kind") == "working-tree":
        row["kind"] = meta["kind"]
        row["reviews"] = []
        for path in sorted(root.glob("review-*/meta.json"),
                           key=lambda p: int(p.parent.name.split("-")[1])):
            try:
                review = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(review, dict):
                review.setdefault("verdict", runs._verdict(path.parent))
                row["reviews"].append(review)
    return row


def matching_live_runs(directory, first_call, last_call, session, exclude=()):
    """Live run dirs under RUNS_ROOT/<repo>/ that this session launched or touched.

    A run already present in the run log (`exclude` names) is skipped so it is not listed twice.
    Live rows are for the skeleton only — do not append them to runs.jsonl.
    """
    exclude = set(exclude)
    matched = []
    base = runs.RUNS_ROOT / directory.name
    if not base.is_dir():
        return matched
    for meta_path in sorted(base.glob("*/meta.json")):
        root = meta_path.parent
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(meta, dict) or not _meta_repo_matches(meta, directory):
            continue
        name = meta.get("name") or root.name
        if name in exclude:
            continue
        if not _live_run_touched(root, meta, session, first_call, last_call):
            continue
        matched.append(_live_run_row(root, meta))
    return matched


def append_runs(data, rows, sid, repo):
    seen = {(obj.get("run"), obj.get("ts")) for _, obj in _json_rows(data) if obj}
    new = []
    for row in rows:
        pair = (row.get("run"), row.get("ts"))
        if pair not in seen:
            # A run cleaned up here may have been launched by an earlier session; keep that one.
            new.append({**row, "repo": repo, "session": row.get("session") or sid})
            seen.add(pair)
    return _append_bytes(data, b"".join(_encoded_row(row) for row in new)), len(new)


def session_asks(session):
    """`delegate ask` calls the session made, from the delegate rows in the session log."""
    path = runs.session_log_path()
    return [r for r in _log_rows(path) if r.get("event") == "delegate"
            and r.get("command") == "ask" and _same_session(r["session"], session)]


def _fmt_tokens(n):
    return f"{n / 1e6:.1f}M" if n >= 1e6 else cost._fmt_k(n)


def _rejection_prompts(rows):
    marker = "The user provided the following reason for the rejection:"
    for row in rows:
        if row.get("type") != "user" or row.get("isSidechain"):
            continue
        message = row.get("message")
        blocks = message.get("content") if isinstance(message, dict) else None
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if (not isinstance(block, dict) or block.get("type") != "tool_result"
                    or block.get("is_error") is not True):
                continue
            text = block.get("content")
            if isinstance(text, list):
                text = "\n".join(b["text"] for b in text if isinstance(b, dict)
                                 and b.get("type") == "text" and isinstance(b.get("text"), str))
            if isinstance(text, str) and text.partition(marker)[2].strip():
                yield {"timestamp": row.get("timestamp"), "rejection": True}
                break


def prompt_contexts(path, calls=None):
    """Context answering typed prompts and rejection reasons, plus typed-prompt guard events."""
    rows = rate.json_rows(path)
    prompt_rows = list(rate._typed_prompt_rows(rows))
    prompts = [prompt for _, prompt in prompt_rows]
    prompt_indices = {row_index: i for i, (row_index, _) in enumerate(prompt_rows)}
    calls = cost.parse_session_usage(path) if calls is None else calls
    main_calls = [c for c in calls if c["source"] == "main"]
    timed_calls = sorted(((ts, c) for c in main_calls
                          if (ts := _aware(c.get("timestamp"))) is not None),
                         key=lambda pair: pair[0])
    call_times = [ts for ts, _ in timed_calls]
    prompt_times = [_aware(p["timestamp"]) for p in prompts]
    rejections = list(_rejection_prompts(rows))
    entries = []
    # Keep typed prompts first so guard indices cannot refer to a rejection.
    for prompt in prompts + rejections:
        ts = _aware(prompt["timestamp"])
        # Legacy untimed prompts remain visible, using the first available call.
        index = bisect_left(call_times, ts) if ts is not None else 0
        call = timed_calls[min(index, len(timed_calls) - 1)][1] if timed_calls else \
            (main_calls[0] if main_calls else None)
        entries.append({"timestamp": prompt["timestamp"],
                        "context": sum(call[k] for k in ("input", "cache_read", "cache_creation"))
                        if call else 0, "guard": None})
        if prompt.get("rejection"):
            entries[-1]["rejection"] = True
    for row_index, row in enumerate(rows):
        if row.get("isSidechain"):
            continue
        guard = None
        content = row.get("content")
        attachment = row.get("attachment")
        if (row.get("type") == "attachment" and isinstance(attachment, dict)
                and attachment.get("type") == "hook_system_message"
                and attachment.get("hookEvent") == "UserPromptSubmit"):
            content = attachment.get("content")
            guard = "nudge"
        elif (row.get("type") == "system" and row.get("subtype") == "informational"
              and row.get("preventContinuation") and isinstance(content, str)
              and content.startswith("UserPromptSubmit operation blocked by hook:")):
            guard = "idle_hold"
        if not guard or not isinstance(content, str) or "context guard:" not in content:
            continue
        # The immediately delivered input may be a notification, not typed input.
        if guard == "nudge":
            index = None
            for previous_index in range(row_index - 1, -1, -1):
                previous = rows[previous_index]
                attached = previous.get("attachment")
                queued = (previous.get("type") == "attachment" and isinstance(attached, dict)
                          and attached.get("type") == "queued_command")
                if previous.get("type") == "user" and previous.get("isMeta"):
                    continue  # a skill or command expansion of the prompt before it
                if previous.get("type") == "user" or queued:
                    index = prompt_indices.get(previous_index)
                    break
                if previous.get("type") not in ("attachment", "system"):
                    break
        else:
            event_time = _aware(row.get("timestamp"))
            if event_time is None:
                continue
            index = next((i for i, ts in enumerate(prompt_times)
                          if ts is not None and ts >= event_time), None)
        if index is not None and entries[index]["guard"] != "idle_hold":
            entries[index]["guard"] = guard
    if rejections:
        entries.sort(key=lambda p: _aware(p["timestamp"]) or datetime.min.replace(tzinfo=timezone.utc))
    return entries


def render_prompt_contexts(prompts):
    tags = {None: "", "nudge": " (guard)", "idle_hold": " (idle hold)"}
    contexts = ", ".join(cost._fmt_k(p["context"])
                         + (" (rejection)" if p.get("rejection") else tags[p["guard"]])
                         for p in prompts)
    return f"Context at typed prompts: {contexts or 'none'}."


def render_skeleton(row, summary, runs, asks=()):
    total = summary["cost_units"] or 1
    parts = summary["cost_parts"]
    shares = ", ".join(f"{label} {100 * parts[key] / total:.1f}%" for key, label in (
        ("cache_read", "cache read"), ("cache_write", "cache write"), ("output", "output"),
    ))
    rebuilds = summary["idle_rebuilds"]
    idle = "none"
    if rebuilds:
        gaps = ", ".join(str(r["gap_min"]) for r in rebuilds)
        written = _fmt_tokens(sum(r["cache_creation"] for r in rebuilds))
        pct = 100 * summary["idle_rebuild_units"] / total
        idle = f"{len(rebuilds)} ({gaps} min; {written} written, {pct:.1f}% of cost units)"
    measured = row["quota_end"]
    quota_text = ", ".join(f"{pool} {left:g}%" for pool, left in measured.items()) \
        if measured and "error" not in measured else "not measured"
    run_text = "none"
    if runs:
        lines = []
        for r in runs:
            agent = r.get("agent", "?")
            model = r.get("model", "?")
            n_rounds = len(r.get("rounds") or [])
            working_tree = (r.get("kind") == "working-tree"
                            or ("kind" not in r and agent == "claude" and not n_rounds
                                and r.get("reviews")))
            if working_tree:
                reviews = ", ".join(f"{rv.get('agent', '?')} {rv.get('model', '?')} "
                                    f"{rv.get('verdict') or 'no verdict'}"
                                    for rv in r.get("reviews") or [])
                line = f"- `{r.get('run', '?')}`: review (working-tree): {reviews or 'no reviews'}"
            elif r.get("live"):
                line = (f"- `{r.get('run', '?')}`: {agent} {model}, {n_rounds} round(s)"
                        + (f", verify {r['verify']}" if r.get("verify") is not None else ""))
            else:
                line = (f"- `{r.get('run', '?')}`: {agent} {model}, "
                        f"{n_rounds} round(s), {r.get('outcome') or 'unknown'}")
            if r.get("live"):
                line += "; live: not applied or cleaned up"
            elif r.get("session") and not _same_session(r["session"], row["session"]):
                line += f", launched in session `{r['session'][:8]}`"
            lines.append(line)
        run_text = "\n" + "\n".join(lines)
    ask_text = f"{len(asks)} ({', '.join(a.get('agent') or '?' for a in asks)})" if asks else "none"
    return f"""# Field report: {row['item'] or 'TODO title'} ({row['date']})

Orchestrator: {row['model'] or 'unknown model'} (session `{row['session'][:8]}`). Started from <!-- handoff file or first prompt -->. <!-- items, delegates, commits -->. Machine-readable rows: `token_usage.jsonl`, `runs.jsonl`.

## Flow and cost

| # | Step | Outcome |
|---|---|---|
| 1 | <!-- step --> | <!-- outcome --> |

Session up to this log: {row['api_calls']} calls, {_fmt_tokens(row['cache_read'])} cache read, {_fmt_tokens(row['cache_write'])} cache write, {_fmt_tokens(row['output'])} output; context {cost._fmt_k(summary['context_first'])} → {cost._fmt_k(summary['context_last'])}; {cost._fmt_int(row['cost_units'])} cost units ({shares}). Idle rebuilds: {idle}.

{render_prompt_contexts(row['prompts'])}

Quota at the end: {quota_text}.

Delegate runs: {run_text}.

Delegate asks: {ask_text}.

## What worked

<!-- what to keep doing, with numbers -->

## Friction

<!-- what cost calls or rounds, and why -->

## Suggestions for claude-orchestrator

<!-- concrete changes to rules, SKILL.md, the CLI or the hook -->

## Not in this repo's scope

<!-- lessons for the project itself -->
"""


def _atomic_write(path, data):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=str(path.parent), prefix=f".{path.name}.",
                                         delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
        # NamedTemporaryFile creates 0600; keep the target's mode (or 0644 for a new file)
        os.chmod(str(temporary), path.stat().st_mode & 0o777 if path.exists() else 0o644)
        os.replace(str(temporary), str(path))
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def cmd_report(a):
    if a.select or a.digest:
        return cmd_automatic(a)
    if (a.backfill or a.exclude or a.transcript or a.log or a.trigger) and not a.auto:
        _fail("--backfill, --exclude, --transcript, --log and --trigger need --auto", 2)
    if a.auto:
        try:
            return cmd_auto(a)
        except OSError as exc:
            _fail(str(exc))
    try:
        directory = feedback_dir(a.feedback_dir)
        path = cost.resolve_session(a.session or os.environ.get("CLAUDE_CODE_SESSION_ID") or "latest")
        calls = cost.parse_session_usage(path)
        main_calls = [c for c in calls if c["source"] == "main"]
        if not main_calls:
            _fail(f"no assistant calls in {path}")
        first, last = _call_time(main_calls[0]), _call_time(main_calls[-1])
        repo, repo_dir = repo_identity(a.repo)
        slug = repo.rstrip("/").rsplit("/", 1)[-1].lower()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", a.topic):
            _fail("--topic must be a slug (letters, digits, hyphens or underscores)", 2)
        date = last.astimezone().date().isoformat()
        skeleton = _output_path(directory, f"{date}-{slug}-{a.topic}.md")
        if skeleton.exists() and not a.force:
            _fail(f"{skeleton} exists; use --force to replace it")
        usage_path = _output_path(directory, "token_usage.jsonl")
        runs_path = _output_path(directory, "runs.jsonl")
        weights, prices = cost.load_catalog_cost()
        summary = cost.summarise_session(calls, weights, prices)
        row = {
            "date": date, "repo": repo, "item": a.item,
            "kind": "orchestrator_session", "session": session_id(path),
            "model": main_calls[0]["model"], "source": "delegate report",
            "api_calls": summary["calls"], "fresh_input": summary["input"],
            "output": summary["output"], "cache_read": summary["cache_read"],
            "cache_write": summary["cache_creation"],
            "context_per_call": {"first": summary["context_first"], "last": summary["context_last"],
                                 "max": summary["context_max"]},
            "cost_units": round(summary["cost_units"]),
            "idle_rebuilds": summary["idle_rebuilds"], "quota_end": quota_end(a.no_quota),
            "usd_list_price": round(summary["usd"], 4) if summary["usd"] is not None else None,
            "subagent_calls": summary["subagent_calls"], "prices_as_of": summary["prices_as_of"],
            "prompts": prompt_contexts(path, calls),
        }
        usage, action = upsert_usage(_read_bytes(usage_path), row)
        logged_runs = matching_runs(repo_dir, first)
        live_runs = matching_live_runs(
            repo_dir, first, last, row["session"],
            exclude={r.get("run") for r in logged_runs})
        # Live runs are skeleton-only: cleanup writes the log row later; a second row would double-count.
        display_runs = list(logged_runs) + live_runs
        old_runs = _read_bytes(runs_path)
        run_data, copied = append_runs(old_runs, logged_runs, row["session"], repo)
        markdown = render_skeleton(row, summary, display_runs, session_asks(row["session"]))
        if not a.dry_run:
            directory.mkdir(parents=True, exist_ok=True)
            _atomic_write(usage_path, usage)
            if copied or not runs_path.exists():
                _atomic_write(runs_path, run_data)
            _atomic_write(skeleton, markdown.encode("utf-8"))
        result = {"skeleton": str(skeleton), "row_action": action, "runs_copied": copied,
                  "next": "fill in the <!-- --> sections"}
        if a.dry_run:
            result.update(dry_run=True, row=row)
        if a.json:
            print(json.dumps(result, ensure_ascii=False))
        elif a.dry_run:
            print(json.dumps(row, ensure_ascii=False))
            print(f"runs copied: {copied}")
            print(skeleton)
        else:
            print(skeleton)
            print(action)
            print(f"runs copied: {copied}")
            print(result["next"])
    except OSError as exc:
        _fail(str(exc))


# Session summaries for the session-mode experiment (hooks/session-mode.py). The log is
# append-only and each summary covers the whole transcript, so the last one per session wins.

@contextmanager
def _locked(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with Path(str(path) + ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _log_rows(path):
    return [obj for _, obj in _json_rows(_read_bytes(path))
            if obj and isinstance(obj.get("session"), str)]


def _append_log(path, rows):
    data = b"".join((json.dumps(row, ensure_ascii=True) + "\n").encode("utf-8") for row in rows)
    with path.open("ab", buffering=0) as stream:
        stream.write(data)


def _aware(value):
    ts = cost._parse_ts(value) if isinstance(value, str) else None
    return ts if ts is None or ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def find_transcript(session, hint=None):
    if hint and Path(hint).is_file():
        return Path(hint).resolve()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", session):
        return None
    matches = sorted((cost._claude_config_dir() / "projects").glob(f"*/{session}.jsonl"))
    return matches[0] if matches else None


def transcript_activity(transcript):
    """Newest write to the transcript or its subagent files, which the summary also counts."""
    if transcript is None:
        return None
    newest = None
    for path in (transcript, *Path(transcript).with_suffix("").glob("subagents/agent-*.jsonl")):
        try:
            stamp = path.stat().st_mtime
        except OSError:
            continue
        newest = stamp if newest is None else max(newest, stamp)
    return datetime.fromtimestamp(newest, timezone.utc) if newest is not None else None


def session_quota(path, session):
    """The session's first session-start and last session-end quota.jsonl readings."""
    start = end = None
    for _, row in _json_rows(_read_bytes(path)):
        if not row or row.get("session") != session or not isinstance(row.get("pools"), dict):
            continue
        if row.get("reason") == "session-start" and start is None:
            start = row
        elif row.get("reason") == "session-end":
            end = row
    return start, end


def _window(value):
    """Reset time rounded to the nearest minute: Claude's moves by fractions of a second between polls."""
    ts = _aware(value)
    return (ts.astimezone(timezone.utc) + timedelta(seconds=30)).replace(second=0, microsecond=0) \
        if ts else None


def quota_change(start, end):
    """used_pct at both ends; delta only for fresh readings in the same reset window."""
    if start is None and end is None:
        return None

    def reading(row, pool):
        entry = ((row or {}).get("pools") or {}).get(pool)
        good = (isinstance(entry, dict) and isinstance(entry.get("used_pct"), (int, float))
                and not entry.get("stale") and not entry.get("stale_reset"))
        return entry if good else None

    pools = {}
    for pool in sorted(set((start or {}).get("pools") or {}) | set((end or {}).get("pools") or {})):
        first, last = reading(start, pool), reading(end, pool)
        same = first and last and _window(first.get("resets_at")) is not None \
            and _window(first.get("resets_at")) == _window(last.get("resets_at"))
        pools[pool] = {"start": first["used_pct"] if first else None,
                       "end": last["used_pct"] if last else None,
                       "delta": round(last["used_pct"] - first["used_pct"], 4) if same else None}
    return {"start": (start or {}).get("ts"), "end": (end or {}).get("ts"), "pools": pools}


def summary_row(session, rows, transcript, trigger, weights, prices, quota_start=None, quota_end=None):
    """One row per session: cost incl. subagents, assignment, end, blocks, delegations, quota."""
    mine = [(i, row) for i, row in enumerate(rows) if row.get("session") == session]
    starts = [row for _, row in mine if row.get("event") == "start"]
    start = starts[-1] if starts else {}
    # The latest end wherever it is: a resume can log its start while this summary runs.
    ends = [row for _, row in mine if row.get("event") == "end"]
    end = ends[-1] if ends else {}
    delegated = {}
    for _, row in mine:
        if row.get("event") == "delegate" and isinstance(row.get("command"), str):
            delegated[row["command"]] = delegated.get(row["command"], 0) + 1
    activity = transcript_activity(transcript)  # before parsing: later writes make it due again
    calls = cost.parse_session_usage(transcript) if transcript else []
    summary = cost.summarise_session(calls, weights, prices)
    main_calls = [c for c in calls if c["source"] == "main"]
    times = sorted(t for t in (_aware(c.get("timestamp")) for c in main_calls) if t)
    units = sum(c["input"] * weights.get("input", 1) + c["cache_read"] * weights.get("cache_read", 0.1)
                + c["cache_creation"] * weights.get("cache_write", 2.0)
                + c["output"] * weights.get("output", 5.0) for c in calls)
    usd = summary["usd"]
    return {
        "event": "summary", "session": session,
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"), "trigger": trigger,
        "mode": start.get("mode"), "end_reason": end.get("reason"), "reroll": end.get("reroll"),
        "repo": start.get("repo"), "repo_dir": start.get("repo_dir"),
        "transcript": str(transcript) if transcript else None,
        "activity": activity.isoformat() if activity else None,
        "model": main_calls[0]["model"] if main_calls else None,
        "first_call": times[0].isoformat() if times else None,
        "last_call": times[-1].isoformat() if times else None,
        "calls": summary["calls"], "subagent_calls": summary["subagent_calls"],
        "tokens": {k: sum(c[k] for c in calls) for k in cost.TOKEN_CLASSES},
        "context_max": summary["context_max"],
        "cost_units": round(units), "cost_units_main": round(summary["cost_units"]),
        "usd_list_price": round(usd, 6) if usd is not None else None,
        "usd_subagents": round(summary["usd_subagents"], 6),
        "usd_unpriced_calls": sum(g["calls"] for g in summary["usd_unpriced"]),
        "prices_as_of": summary["prices_as_of"],
        "by_model": [{**g, "usd": round(g["usd"], 6) if g["usd"] is not None else None}
                     for g in summary["by_model"]],
        "blocked": sum(row.get("event") == "blocked" for _, row in mine),
        "delegate": delegated,
        "quota": quota_change(quota_start, quota_end),
    }


def due_for_backfill(rows, exclude=None, now=None):
    """Logged sessions with transcript writes no summary has seen, quiet for an hour.

    VS Code once started a new process on /clear without SessionEnd for the old
    session (docs/verified-behaviour.md), so a missing end row is expected. Each
    summary records the newest write it saw (`activity`), so a session that goes on
    after a back-fill, or a transcript that appears late, becomes due again.
    """
    now = now or datetime.now(timezone.utc)
    state = {}
    for i, row in enumerate(rows):
        entry = state.setdefault(row["session"], {})
        if row.get("event") == "start":
            entry["start_ts"] = _aware(row.get("ts"))
            entry["transcript"] = row.get("transcript") or entry.get("transcript")
        elif row.get("event") == "end":
            entry.update(end=i, end_ts=_aware(row.get("ts")))
        elif row.get("event") == "summary":
            entry.update(summary=i, seen=_aware(row.get("activity")))
    due = []
    for session, entry in state.items():
        if "start_ts" not in entry or session == exclude:
            continue
        if entry["start_ts"] is None or (now - entry["start_ts"]).total_seconds() < IDLE_SECONDS:
            continue
        transcript = find_transcript(session, entry.get("transcript"))
        activity = transcript_activity(transcript)
        if "summary" in entry and (activity is None or (entry["seen"] is not None and activity <= entry["seen"])):
            continue  # nothing written since the last summary saw the files
        if activity is not None and (now - activity).total_seconds() < IDLE_SECONDS:
            continue  # still being written, e.g. an older tab next to the new one
        if (entry.get("end", -1) > entry.get("summary", -1) and entry["end_ts"] is not None
                and (now - entry["end_ts"]).total_seconds() < END_GRACE_SECONDS):
            continue  # SessionEnd's own summary may still be running
        due.append((session, transcript))
    return due


def cmd_auto(a):
    log = runs.session_log_path(a.log)
    quota_log = log.parent / "quota.jsonl"
    weights, prices = cost.load_catalog_cost()
    if a.backfill:
        with _locked(log):
            rows = _log_rows(log)
            written = [summary_row(session, rows, transcript, "backfill", weights, prices,
                                   *session_quota(quota_log, session))
                       for session, transcript in due_for_backfill(rows, a.exclude)]
            if written and not a.dry_run:
                _append_log(log, written)
        for row in written:
            print(json.dumps(row, ensure_ascii=False) if a.json or a.dry_run
                  else f"{row['session']}: {row['calls']} calls, backfilled")
        if not written and not a.json:
            print("backfill: nothing due")
        return
    session, transcript = a.session or os.environ.get("CLAUDE_CODE_SESSION_ID"), a.transcript
    if not session:
        _fail("--auto needs --session or CLAUDE_CODE_SESSION_ID", 2)
    if Path(session).is_file():
        transcript, session = session, session_id(Path(session))
    end = None
    if not (a.no_quota or a.dry_run):
        try:  # take the reading first, as close to the session's end as possible
            end, _ = quota.record("session-end" if a.trigger == "end" else "manual",
                                  session=session, log=quota_log)
        except OSError:
            end = None
    with _locked(log):
        rows = _log_rows(log)
        if transcript is None:
            hints = [r.get("transcript") for r in rows if r.get("session") == session and r.get("event") == "start"]
            transcript = hints[-1] if hints else None
        start, logged_end = session_quota(quota_log, session)
        row = summary_row(session, rows, find_transcript(session, transcript), a.trigger or "manual", weights,
                          prices, start, end if end is not None else logged_end)
        if not a.dry_run:
            _append_log(log, [row])
    print(json.dumps(row, ensure_ascii=False))


def cmd_automatic(a):
    import autoreport

    if a.select and a.digest:
        _fail("--select and --digest cannot be combined", 2)
    if a.digest and not a.out:
        _fail("--digest needs --out", 2)
    if a.auto or a.backfill or a.exclude or a.transcript or a.trigger:
        _fail("automatic field-report dry runs cannot be combined with session summaries", 2)
    try:
        rows = rate.json_rows(runs.session_log_path(a.log))
        if a.select:
            result = autoreport.select_sessions(rows, feedback_dir(a.feedback_dir),
                                                a.since, a.until, a.seed)
            print(json.dumps(result, ensure_ascii=False) if a.json else autoreport.render_selection(result))
            return
        result = autoreport.write_digests(rows, a.digest, a.out)
    except ValueError as exc:
        _fail(str(exc), 2)
    except OSError:
        _fail("cannot read the session log or write automatic field-report evidence")
    if a.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        for session in result["sessions"]:
            detail = session["reason"] or f"{session['digest_chars']} digest chars, {session['transcript_chars']} transcript chars"
            print(f"{session['session'][:8]} {session['status']}: {detail}")
    if any(s["status"] == "dropped" for s in result["sessions"]):
        sys.exit(1)


def _add_args(p):
    p.add_argument("--session", metavar="ID|latest|PATH", help="default: CLAUDE_CODE_SESSION_ID or latest")
    p.add_argument("--repo", help="report repo name (default: GitHub owner/name or directory name)")
    p.add_argument("--item", help="report title and machine-readable item")
    p.add_argument("--topic", default="session", metavar="SLUG")
    p.add_argument("--feedback-dir", metavar="DIR", help="default: CLAUDE_ORCHESTRATOR_FEEDBACK or checkout feedback/")
    p.add_argument("--no-quota", action="store_true")
    p.add_argument("--force", action="store_true", help="replace an existing skeleton")
    p.add_argument("--dry-run", action="store_true", help="print the row, run count and skeleton path; write nothing")
    p.add_argument("--json", action="store_true")
    auto = p.add_argument_group("session summary (session-mode experiment)")
    auto.add_argument("--auto", action="store_true",
                      help="append a summary row for the session to the session log; no skeleton")
    auto.add_argument("--backfill", action="store_true",
                      help="summarise logged sessions that look over but have no summary")
    auto.add_argument("--exclude", metavar="ID", help="session to skip when back-filling (the new one)")
    auto.add_argument("--transcript", metavar="PATH", help="session transcript (default: from the log)")
    auto.add_argument("--log", metavar="PATH",
                      help="session log (default: CLAUDE_SESSION_LOG or ~/.cache/claude-delegate/sessions.jsonl)")
    auto.add_argument("--trigger", choices=["end", "manual"],
                      help="end: written by the SessionEnd hook (default manual)")
    afr = p.add_argument_group("automatic field reports (dry runs; no agent calls)")
    afr.add_argument("--select", action="store_true", help="select sessions and explain each pick")
    afr.add_argument("--digest", nargs="+", metavar="ID", help="build evidence for full session ids or unique prefixes")
    afr.add_argument("--since", metavar="DATE", help="first call at or after this time (bare date: local midnight)")
    afr.add_argument("--until", metavar="DATE", help="first call before this time")
    afr.add_argument("--seed", type=int, metavar="N", help="replay the selection's random draws")
    afr.add_argument("--out", metavar="DIR", help="write each digest and transcript under DIR/<prefix>/")
    p.set_defaults(fn=cmd_report)


def register(subparsers):
    parser = subparsers.add_parser("report", help="collect session numbers and write a field-report skeleton")
    _add_args(parser)
    return parser


def main(argv=None):
    parser = argparse.ArgumentParser(prog="report.py", description=__doc__)
    _add_args(parser)
    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Collect exact session numbers and write a field-report skeleton."""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import timezone
from pathlib import Path
from urllib.parse import urlsplit

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import cost  # noqa: E402
import quota  # noqa: E402


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


def append_runs(data, rows, sid, repo):
    seen = {(obj.get("run"), obj.get("ts")) for _, obj in _json_rows(data) if obj}
    new = []
    for row in rows:
        pair = (row.get("run"), row.get("ts"))
        if pair not in seen:
            new.append({**row, "repo": repo, "session": sid})
            seen.add(pair)
    return _append_bytes(data, b"".join(_encoded_row(row) for row in new)), len(new)


def _fmt_tokens(n):
    return f"{n / 1e6:.1f}M" if n >= 1e6 else cost._fmt_k(n)


def render_skeleton(row, summary, runs):
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
        run_text = "\n" + "\n".join(
            f"- `{r.get('run', '?')}`: {r.get('agent', '?')} {r.get('model', '?')}, "
            f"{len(r.get('rounds') or [])} round(s), {r.get('outcome') or 'unknown'}"
            for r in runs
        )
    return f"""# Field report: {row['item'] or 'TODO title'} ({row['date']})

Orchestrator: {row['model'] or 'unknown model'} (session `{row['session'][:8]}`). Started from <!-- handoff file or first prompt -->. <!-- items, delegates, commits -->. Machine-readable rows: `token_usage.jsonl`, `runs.jsonl`.

## Flow and cost

| # | Step | Outcome |
|---|---|---|
| 1 | <!-- step --> | <!-- outcome --> |

Session up to this log: {row['api_calls']} calls, {_fmt_tokens(row['cache_read'])} cache read, {_fmt_tokens(row['cache_write'])} cache write, {_fmt_tokens(row['output'])} output; context {cost._fmt_k(summary['context_first'])} → {cost._fmt_k(summary['context_last'])}; {cost._fmt_int(row['cost_units'])} cost units ({shares}). Idle rebuilds: {idle}.

Quota at the end: {quota_text}.

Delegate runs: {run_text}.

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
        }
        usage, action = upsert_usage(_read_bytes(usage_path), row)
        runs = matching_runs(repo_dir, first)
        old_runs = _read_bytes(runs_path)
        run_data, copied = append_runs(old_runs, runs, row["session"], repo)
        markdown = render_skeleton(row, summary, runs)
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

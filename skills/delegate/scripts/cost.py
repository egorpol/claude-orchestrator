#!/usr/bin/env python3
"""Summarise delegate run-log usage and the orchestrator's own session cost."""
import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import runs  # noqa: E402

DEFAULT_LOG = runs.RUNS_ROOT / "runs.jsonl"
CATALOG = SCRIPTS / "catalog.json"
DEFAULT_WEIGHTS = {"input": 1, "cache_read": 0.1, "cache_write": 2.0, "output": 5.0}


def _claude_config_dir():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))


def munge_cwd(cwd=None):
    """Claude Code project dir name: non [A-Za-z0-9] → '-'."""
    cwd = os.path.abspath(cwd or os.getcwd())
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def resolve_session(spec):
    """Return a transcript Path for ID | latest | PATH."""
    p = Path(spec)
    if p.is_file():
        return p.resolve()
    root = _claude_config_dir() / "projects"
    if spec == "latest":
        proj = root / munge_cwd()
        if not proj.is_dir():
            sys.exit(f"no project dir {proj}")
        files = sorted(proj.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
        if not files:
            sys.exit(f"no *.jsonl in {proj}")
        return files[0]
    matches = sorted(root.glob(f"*/{spec}.jsonl"))
    if not matches:
        # Accept a unique id prefix such as "55a11d16".
        matches = sorted(root.glob(f"*/{spec}*.jsonl"))
        ids = {m.stem for m in matches}
        if len(ids) > 1:
            sys.exit(f"session prefix {spec} is ambiguous: {', '.join(sorted(ids))}")
    if not matches:
        sys.exit(f"no transcript {spec}.jsonl under {root}")
    if len(matches) > 1:
        # Prefer the one matching cwd when several projects share an id (unlikely).
        munged = munge_cwd()
        for m in matches:
            if m.parent.name == munged:
                return m
    return matches[0]


def load_catalog_cost():
    try:
        cat = json.loads(CATALOG.read_text())
    except (OSError, json.JSONDecodeError):
        return DEFAULT_WEIGHTS, None
    oc = cat.get("orchestrator_cost") or {}
    weights = {**DEFAULT_WEIGHTS, **(oc.get("weights") or {})}
    return weights, oc.get("usd_per_mtok_input")


def parse_assistant_usage(path):
    """Dedupe assistant lines by message.id (fallback requestId); skip synthetic."""
    seen = set()
    calls = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") != "assistant":
                continue
            msg = obj.get("message") or {}
            if msg.get("model") == "<synthetic>":
                continue
            mid = msg.get("id") or obj.get("requestId")
            if not mid or mid in seen:
                continue
            seen.add(mid)
            u = msg.get("usage") or {}
            calls.append({
                "model": msg.get("model"),
                "input": int(u.get("input_tokens") or 0),
                "cache_read": int(u.get("cache_read_input_tokens") or 0),
                "cache_creation": int(u.get("cache_creation_input_tokens") or 0),
                "output": int(u.get("output_tokens") or 0),
                "timestamp": obj.get("timestamp"),
            })
    return calls


def idle_rebuilds(calls, cache_write_weight=2.0):
    """Calls whose timestamp is >= 3600 s after the previous call's (both timed)."""
    rebuilds = []
    prev = None
    for c in calls:
        ts = _parse_ts(c.get("timestamp"))
        if ts is None:  # an untimed call may have refreshed the cache: restart the clock
            prev = None
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if prev is not None:
            gap = (ts - prev).total_seconds()
            if gap >= 3600:
                rebuilds.append({
                    "gap_min": int(gap // 60),
                    "cache_creation": c["cache_creation"],
                })
        prev = ts
    units = sum(r["cache_creation"] * cache_write_weight for r in rebuilds)
    return rebuilds, units


def summarise_session(calls, weights, usd_per_mtok):
    n = len(calls)
    inp = sum(c["input"] for c in calls)
    cr = sum(c["cache_read"] for c in calls)
    cc = sum(c["cache_creation"] for c in calls)
    out = sum(c["output"] for c in calls)
    ctxs = [c["input"] + c["cache_read"] + c["cache_creation"] for c in calls]
    first = ctxs[0] if ctxs else 0
    last = ctxs[-1] if ctxs else 0
    mx = max(ctxs) if ctxs else 0
    cw_w = weights.get("cache_write", 2.0)
    parts = {
        "input": inp * weights.get("input", 1),
        "cache_read": cr * weights.get("cache_read", 0.1),
        "cache_write": cc * cw_w,
        "output": out * weights.get("output", 5.0),
    }
    total = sum(parts.values())
    models = sorted({c["model"] for c in calls if c.get("model")})
    rebuilds, rebuild_units = idle_rebuilds(calls, cw_w)
    return {
        "calls": n,
        "models": models,
        "input": inp,
        "cache_read": cr,
        "cache_creation": cc,
        "output": out,
        "context_first": first,
        "context_last": last,
        "context_max": mx,
        "cost_units": total,
        "cost_parts": parts,
        "usd_per_mtok_input": usd_per_mtok,
        "usd": (total / 1e6) * usd_per_mtok if usd_per_mtok is not None else None,
        "idle_rebuilds": rebuilds,
        "idle_rebuild_units": rebuild_units,
    }


def _fmt_int(n):
    return f"{n:,}"


def _fmt_k(n):
    if n >= 1000:
        return f"{n / 1000:.0f}k" if n >= 10000 else f"{n / 1000:.1f}k"
    return str(n)


def render_session(path, summary, as_json=False):
    if as_json:
        return json.dumps({"path": str(path), **summary}, indent=2)
    lines = [f"session: {path.name}  ({', '.join(summary['models']) or 'unknown model'})"]
    lines.append(f"calls: {summary['calls']}")
    lines.append(
        f"tokens: input {_fmt_int(summary['input'])}  "
        f"cache_creation {_fmt_int(summary['cache_creation'])}  "
        f"cache_read {_fmt_int(summary['cache_read'])}  "
        f"output {_fmt_int(summary['output'])}"
    )
    lines.append(
        f"context/call: first {_fmt_k(summary['context_first'])}  "
        f"last {_fmt_k(summary['context_last'])}  "
        f"max {_fmt_k(summary['context_max'])}"
    )
    total = summary["cost_units"] or 1
    parts = summary["cost_parts"]
    shares = "  ".join(
        f"{k} {100 * parts[k] / total:.1f}%"
        for k in ("input", "cache_read", "cache_write", "output")
    )
    cost_line = f"cost units: {_fmt_int(round(summary['cost_units']))}  ({shares})"
    if summary.get("usd") is not None:
        cost_line += f"  ≈ ${summary['usd']:.2f}"
    lines.append(cost_line)
    rebuilds = summary.get("idle_rebuilds") or []
    if not rebuilds:
        lines.append("idle rebuilds (gap >= 60 min): none")
    else:
        gaps = ", ".join(str(r["gap_min"]) for r in rebuilds)
        written = sum(r["cache_creation"] for r in rebuilds)
        total = summary["cost_units"] or 1
        pct = 100 * (summary.get("idle_rebuild_units") or 0) / total
        lines.append(
            f"idle rebuilds (gap >= 60 min): {len(rebuilds)} "
            f"({gaps} min; {_fmt_k(written)} written, {pct:.1f}% of cost units)"
        )
    lines.append(f"next call re-reads ≈ {_fmt_k(summary['context_last'])} tokens")
    return "\n".join(lines)


def _parse_ts(s):
    if not s:
        return None
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _add_usage(dst, u):
    if not u:
        return
    for k in ("input", "cached_input", "cache_write", "output", "reasoning_output"):
        v = u.get(k)
        if v is None:
            continue
        dst[k] = dst.get(k, 0) + int(v)


def load_run_log(path, since=None, repo=None):
    since_dt = None
    if since:
        # Bare YYYY-MM-DD / naive ISO → local midnight; explicit offsets (or Z) kept as given.
        since_dt = _parse_ts(since)
        if since_dt is None:
            since_dt = datetime.fromisoformat(since)
        if since_dt.tzinfo is None:
            since_dt = since_dt.astimezone()
    rows = []
    if not path.is_file():
        return rows
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if repo and obj.get("repo") != repo:
                continue
            if since_dt:
                ts = _parse_ts(obj.get("ts"))
                if ts is None:
                    continue
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts < since_dt:
                    continue
            rows.append(obj)
    return rows


def summarise_runs(rows):
    """Aggregate per agent+model."""
    groups = defaultdict(lambda: {
        "runs": 0, "rounds": 0, "minutes": 0.0,
        "usage": {}, "applied": 0, "discarded": 0, "other": 0,
    })
    for r in rows:
        key = (r.get("agent") or "?", r.get("model") or "?")
        g = groups[key]
        g["runs"] += 1
        outcome = r.get("outcome")
        if outcome == "applied":
            g["applied"] += 1
        elif outcome == "discarded":
            g["discarded"] += 1
        else:
            g["other"] += 1
        for rnd in r.get("rounds") or []:
            g["rounds"] += 1
            g["minutes"] += float(rnd.get("minutes") or 0)
            _add_usage(g["usage"], rnd.get("usage"))
        for rev in r.get("reviews") or []:
            # reviews are separate agent work; attribute under review agent+model
            rkey = (rev.get("agent") or key[0], rev.get("model") or key[1])
            rg = groups[rkey]
            rg["minutes"] += float(rev.get("minutes") or 0)
            _add_usage(rg["usage"], rev.get("usage"))
    return groups


def render_runs(rows, groups, since=None, repo=None, log_path=None, as_json=False):
    if as_json:
        out = []
        for (agent, model), g in sorted(groups.items()):
            out.append({"agent": agent, "model": model, **g})
        return json.dumps({"runs": len(rows), "since": since, "repo": repo,
                           "log": str(log_path), "by_agent_model": out}, indent=2)
    hdr = f"run log: {log_path}  ({len(rows)} runs"
    if since:
        hdr += f" since {since}"
    if repo:
        hdr += f" repo={repo}"
    hdr += ")"
    lines = [hdr]
    if not groups:
        lines.append("(no matching runs)")
        return "\n".join(lines)
    table = [("AGENT", "MODEL", "RUNS", "ROUNDS", "MIN", "IN", "CACHED", "OUT", "CW", "APPLIED", "DISC")]
    for (agent, model), g in sorted(groups.items(), key=lambda kv: (-kv[1]["runs"], kv[0])):
        u = g["usage"]
        table.append((
            agent, model, str(g["runs"]), str(g["rounds"]),
            f"{g['minutes']:.0f}",
            _fmt_int(u.get("input", 0)),
            _fmt_int(u.get("cached_input", 0)),
            _fmt_int(u.get("output", 0)),
            _fmt_int(u.get("cache_write", 0)) if u.get("cache_write") else "-",
            str(g["applied"]), str(g["discarded"]),
        ))
    widths = [max(len(row[i]) for row in table) for i in range(len(table[0]))]
    lines += ["  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip() for row in table]
    return "\n".join(lines)


def cmd_cost(a):
    if a.session:
        path = resolve_session(a.session)
        weights, usd = load_catalog_cost()
        calls = parse_assistant_usage(path)
        summary = summarise_session(calls, weights, usd)
        print(render_session(path, summary, as_json=a.json))
        return
    log = Path(a.log) if a.log else DEFAULT_LOG
    rows = load_run_log(log, since=a.since, repo=a.repo)
    groups = summarise_runs(rows)
    print(render_runs(rows, groups, since=a.since, repo=a.repo, log_path=log, as_json=a.json))


def _add_args(p):
    p.add_argument("--since", help="YYYY-MM-DD (local) or ISO timestamp; only runs at or after it")
    p.add_argument("--repo", help="filter by repo directory name")
    p.add_argument("--log", help=f"run log path (default {DEFAULT_LOG})")
    p.add_argument("--session", metavar="ID|latest|PATH",
                   help="orchestrator Claude Code transcript (deduped by message.id)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_cost)


def register(subparsers):
    s = subparsers.add_parser(
        "cost",
        help="summarise delegate run-log usage, or the orchestrator's own session cost",
    )
    _add_args(s)
    return s


def main(argv=None):
    p = argparse.ArgumentParser(prog="cost.py", description=__doc__)
    _add_args(p)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    main()

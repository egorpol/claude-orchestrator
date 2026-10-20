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
TOKEN_CLASSES = ("input", "cache_read", "cache_write_5m", "cache_write_1h",
                 "cache_write_unsplit", "output")
MODES = ("orchestrator", "claude-only", "vanilla")
DELEGATE_COMMANDS = ("launch", "followup", "review", "ask")
DELEGATE_TOKENS = ("input", "cached_input", "output")
# Pools with a fitted rate per list-price USD (`cost --calibrate --save`): --by-mode
# estimates their points per session. Reading deltas over a session also catch
# other sessions' use and the hours a session stays open.
ESTIMATED_POOLS = ("claude-5h", "claude-7d")


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
        # After a stray `cd` into a subfolder, cwd has no project dir of its own;
        # the session's dir belongs to one of its parents (2026-10-13 field report).
        cwd = Path(os.path.abspath(os.getcwd()))
        for d in (cwd, *cwd.parents):
            proj = root / munge_cwd(str(d))
            if proj.is_dir():
                break
        else:
            sys.exit(f"no project dir for {cwd} or its parents under {root}")
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
    """Return historical cost-unit weights and the dated list-price table (or None)."""
    try:
        cat = json.loads(CATALOG.read_text())
    except (OSError, json.JSONDecodeError):
        return DEFAULT_WEIGHTS, None
    oc = cat.get("orchestrator_cost") or {}
    weights = {**DEFAULT_WEIGHTS, **(oc.get("weights") or {})}
    return weights, oc.get("list_prices")


def parse_assistant_usage(path, source="main", seen=None):
    """Parse one transcript without pricing; optionally share deduplication across files.

    Records retain the timestamp, source, pricing modifiers and token classes for
    reuse in interval analyses. The original cache_creation total is kept for
    historical cost-unit/context summaries. First occurrence of an id wins.
    """
    if seen is None:
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
            total_write = int(u.get("cache_creation_input_tokens") or 0)
            split = u.get("cache_creation") or {}
            write_5m = int(split.get("ephemeral_5m_input_tokens") or 0)
            write_1h = int(split.get("ephemeral_1h_input_tokens") or 0)
            calls.append({
                "id": mid,
                "model": msg.get("model"),
                "source": source,
                "input": int(u.get("input_tokens") or 0),
                "cache_read": int(u.get("cache_read_input_tokens") or 0),
                "cache_creation": total_write,
                "cache_write_5m": write_5m,
                "cache_write_1h": write_1h,
                "cache_write_unsplit": max(0, total_write - write_5m - write_1h),
                "output": int(u.get("output_tokens") or 0),
                "timestamp": obj.get("timestamp"),
                "speed": u.get("speed"),
                "inference_geo": u.get("inference_geo"),
            })
    return calls


def parse_session_usage(path):
    """Parse main then agent-*.jsonl with shared ids, attributing duplicates to main.

    Main order is preserved for historical context/idle measurements. Consumers
    doing interval analysis can filter or sort these records by timestamp.
    """
    path = Path(path)
    seen = set()
    calls = parse_assistant_usage(path, seen=seen)
    for agent_path in sorted(path.with_suffix("").glob("subagents/agent-*.jsonl")):
        calls.extend(parse_assistant_usage(agent_path, source="subagents", seen=seen))
    return calls


def model_price(model, prices):
    """Exact model id first, otherwise the longest hyphen-delimited prefix."""
    models = (prices or {}).get("models") or {}
    if model in models:
        return models[model]
    matches = [k for k in models if model and model.startswith(k + "-")]
    return models[max(matches, key=len)] if matches else None


def price_call(call, prices):
    """Return (list-price USD, unpriced reason); apply prompt tiers per call."""
    for key, standard in (("speed", "standard"), ("inference_geo", "global")):
        value = call.get(key)
        if value not in (None, standard):
            return None, f"{key}={value}"
    rates = model_price(call.get("model"), prices)
    if rates is None:
        return None, "no price"
    prompt = call["input"] + call["cache_read"] + call["cache_creation"]
    threshold = rates.get("above_prompt_tokens")
    if threshold is not None and prompt > threshold:
        rates = rates["above"]
    usd = sum(call[k] * rates[k] for k in TOKEN_CLASSES if k != "cache_write_unsplit")
    usd += call["cache_write_unsplit"] * rates["cache_write_5m"]
    return usd / 1e6, None


def summarise_list_price(calls, prices):
    """Aggregate tokens (including unpriced calls) and USD by model/source."""
    groups = {}
    unpriced = defaultdict(int)
    source_usd = {"main": 0.0, "subagents": 0.0}
    priced_calls = 0
    for call in calls:
        source = call.get("source", "main")
        model = call.get("model")
        key = (source, model)
        if key not in groups:
            groups[key] = {"model": model, "source": source, "calls": 0,
                           **dict.fromkeys(TOKEN_CLASSES, 0), "usd": None}
        group = groups[key]
        group["calls"] += 1
        for token_class in TOKEN_CLASSES:
            group[token_class] += call[token_class]
        usd, reason = price_call(call, prices)
        if usd is None:
            unpriced[(source, model, reason)] += 1
        else:
            priced_calls += 1
            group["usd"] = (group["usd"] or 0.0) + usd
            source_usd[source] += usd
    # Model can be absent in a transcript; keep that group and its no-price reason.
    sort_key = lambda key: (key[0] != "main", key[1] or "", *key[2:])
    return {
        "subagent_calls": sum(c.get("source") == "subagents" for c in calls),
        "by_model": [groups[key] for key in sorted(groups, key=sort_key)],
        "usd": sum(source_usd.values()) if priced_calls else None,
        "usd_main": source_usd["main"],
        "usd_subagents": source_usd["subagents"],
        "usd_unpriced": [{"model": model, "source": source, "calls": count, "reason": reason}
                         for (source, model, reason), count in sorted(unpriced.items(),
                                                                    key=lambda kv: sort_key(kv[0]))],
        "prices_as_of": (prices or {}).get("as_of"),
    }


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


def summarise_session(calls, weights, prices=None):
    """Keep historical fields main-only; add list-price totals for the whole session."""
    pricing = summarise_list_price(calls, prices)
    calls = [c for c in calls if c.get("source", "main") == "main"]
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
        "idle_rebuilds": rebuilds,
        "idle_rebuild_units": rebuild_units,
        **pricing,
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
    lines.append(f"cost units: {_fmt_int(round(summary['cost_units']))}  ({shares})")
    usd = f"${summary['usd']:.2f}" if summary["usd"] is not None else "unpriced"
    dated = f"prices as of {summary['prices_as_of'] or 'unknown'}"
    lines.append(f"list price: {usd}  (main ${summary['usd_main']:.2f}, "
                 f"subagents ${summary['usd_subagents']:.2f}; {dated})")
    lines.append(f"by model: ({dated}; USD)")
    columns = ("model", "source", "calls", *TOKEN_CLASSES, "usd")
    table = [columns]
    for row in summary["by_model"]:
        table.append((row["model"] or "unknown model", row["source"], _fmt_int(row["calls"]),
                      *(_fmt_int(row[k]) for k in TOKEN_CLASSES),
                      f"${row['usd']:.2f}" if row["usd"] is not None else "unpriced"))
    widths = [max(len(row[i]) for row in table) for i in range(len(columns))]
    lines += ["  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip() for row in table]
    for group in summary["usd_unpriced"]:
        lines.append(f"unpriced: {group['model'] or 'unknown model'}  {group['source']}  "
                     f"{_fmt_int(group['calls'])} calls  ({group['reason']})")
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


def _since_dt(since):
    """Bare dates and naive ISO cutoffs use local time, as in the run report."""
    since_dt = None
    if since:
        # Bare YYYY-MM-DD / naive ISO → local midnight; explicit offsets (or Z) kept as given.
        since_dt = _parse_ts(since)
        if since_dt is None:
            since_dt = datetime.fromisoformat(since)
        if since_dt.tzinfo is None:
            since_dt = since_dt.astimezone()
    return since_dt


def load_jsonl(path):
    """Read object rows in file order, ignoring blank/broken lines and missing logs."""
    rows = []
    path = Path(path)
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
            if isinstance(obj, dict):
                rows.append(obj)
    return rows


def load_run_log(path, since=None, repo=None):
    since_dt = _since_dt(since)
    rows = []
    for obj in load_jsonl(path):
        if repo and obj.get("repo") != repo:
            continue
        if since_dt:
            ts = _row_ts(obj.get("ts"))
            if ts is None or ts < since_dt:
                continue
        rows.append(obj)
    return rows


def _row_ts(value):
    ts = _parse_ts(value)
    return ts.replace(tzinfo=timezone.utc) if ts is not None and ts.tzinfo is None else ts


def is_drawn(start):
    """A session's earliest start row was randomised: drawn, not forced, excluded or unassigned."""
    return (bool(start) and start.get("u") is not None
            and not any(start.get(key) for key in ("forced", "excluded", "unassigned")))


def _join_session_runs(state, run_rows):
    """Assign each distinct run once, using explicit provenance before legacy matching.

    Index all starts, including non-drawn/out-of-window sessions, so filtering the
    report cannot steal a run from its actual owner. First duplicate run row wins.
    """
    launches = defaultdict(list)
    for session, entry in state.items():
        start = entry.get("start")
        if start is None:
            continue
        ts = _row_ts(start.get("ts"))
        if ts is not None:
            for name in entry["launches"]:
                launches[(name, start.get("repo_dir"))].append((ts, session))
    joined = defaultdict(list)
    seen = set()
    for row in run_rows:
        pair = (row.get("run"), row.get("ts"))
        if pair in seen:
            continue
        seen.add(pair)
        session = row.get("session")
        if not session:
            ts = _row_ts(row.get("ts"))
            candidates = launches.get((row.get("run"), row.get("repo")), [])
            earlier = [candidate for candidate in candidates if ts is not None and candidate[0] <= ts]
            if earlier:
                # max is stable for equal timestamps: first session in file order wins.
                session = max(earlier, key=lambda candidate: candidate[0])[1]
        if session in state:
            joined[session].append(row)
    return joined


def _run_totals(rows):
    tokens = {}
    for row in rows:
        agent = row.get("agent") or "?"
        work = [(agent, rnd.get("usage")) for rnd in row.get("rounds") or []]
        work.extend((rev.get("agent") or agent, rev.get("usage"))
                    for rev in row.get("reviews") or [])
        for worker, usage in work:
            if not usage:
                continue
            totals = tokens.setdefault(worker, dict.fromkeys(DELEGATE_TOKENS, 0))
            for key in DELEGATE_TOKENS:
                totals[key] += usage.get(key) or 0
    return {"runs": len(rows), "applied": sum(r.get("outcome") == "applied" for r in rows),
            "discarded": sum(r.get("outcome") == "discarded" for r in rows),
            "delegate_tokens": {agent: tokens[agent] for agent in sorted(tokens)}}


def _points(values):
    return {"sum": sum(values), "n": len(values), "mean": sum(values) / len(values) if values else None}


def by_mode(session_rows, run_rows, since=None, repo=None, calibration=None):
    """Intent-to-treat arm totals and per-session records, without I/O or mutations.

    Sessions retain their first start's assignment and last summary/end in file
    order. Per-session records are the aggregation boundary for future ratings.
    `calibration` is `calibrate.usd_rates()`: estimated pool points are a
    session's list-price USD times the saved points per USD.
    """
    rates = {pool: rate["per_usd"] for pool, rate in ((calibration or {}).get("pools") or {}).items()
             if pool in ESTIMATED_POOLS}
    state = {}
    for row in session_rows:
        session = row.get("session")
        if not session:
            continue
        entry = state.setdefault(session, {"delegate": dict.fromkeys(DELEGATE_COMMANDS, 0),
                                           "launches": set()})
        event = row.get("event")
        if event == "start":
            entry.setdefault("start", row)
        elif event in ("summary", "end"):
            entry[event] = row
        elif event == "delegate":
            command = row.get("command")
            if command in entry["delegate"]:
                entry["delegate"][command] += 1
            if command == "launch" and row.get("run"):
                entry["launches"].add(row["run"])

    joined = _join_session_runs(state, run_rows)
    cutoff = _since_dt(since)
    not_drawn = dict.fromkeys(("excluded", "forced", "unassigned", "other"), 0)
    skipped = 0
    sessions = []
    configs = defaultdict(int)
    unknown = 0
    for session, entry in state.items():
        start = entry.get("start")
        if start is None:
            skipped += 1
            continue
        ts = _row_ts(start.get("ts"))
        if cutoff is not None and (ts is None or ts < cutoff):
            continue
        if repo and start.get("repo_dir") != repo:
            continue
        reason = next((key for key in ("excluded", "forced", "unassigned") if start.get(key)), None)
        if not is_drawn(start):
            not_drawn[reason or "other"] += 1
            continue
        summary = entry.get("summary")
        pending = summary is None
        summary = summary or {}
        weights = start.get("weights")
        if weights is None:
            unknown += 1
        else:
            configs[tuple(sorted(weights.items()))] += 1
        pools = {pool: reading.get("delta")
                 for pool, reading in ((summary.get("quota") or {}).get("pools") or {}).items()}
        usd = summary.get("usd_list_price")
        sessions.append({
            "session": session, "start": start.get("ts"),
            "start_date": ts.astimezone().date().isoformat() if ts is not None else None,
            "mode": start.get("mode"), "repo": start.get("repo"), "repo_dir": start.get("repo_dir"),
            "weights": weights, "reroll": bool((entry.get("end") or {}).get("reroll")),
            "pending": pending, "summarised": not pending,
            **{key: summary.get(key) or 0 for key in ("calls", "subagent_calls", "cost_units", "blocked")},
            "usd_list_price": usd,
            "delegate": dict(entry["delegate"]), "pools": pools,
            "est_pools": {pool: usd * rate for pool, rate in rates.items()} if usd is not None else {},
            **_run_totals(joined[session]),
        })

    pool_names = sorted({pool for session in sessions for pool in session["pools"]})
    arms = {}
    sum_fields = ("calls", "subagent_calls", "cost_units", "blocked", "runs", "applied", "discarded")
    for mode in MODES:
        mine = [session for session in sessions if session["mode"] == mode]
        summarised = sum(session["summarised"] for session in mine)
        usd = sum(session["usd_list_price"] for session in mine if session["usd_list_price"] is not None)
        priced = sum(session["usd_list_price"] is not None for session in mine)
        arm = {
            "sessions": len(mine), "rerolls": sum(session["reroll"] for session in mine),
            "pending": sum(session["pending"] for session in mine), "summarised": summarised,
            **{key: sum(session[key] for session in mine) for key in sum_fields},
            "usd_list_price": usd,
            "usd_null_sessions": sum(session["summarised"] and session["usd_list_price"] is None
                                     for session in mine),
            "usd_per_session": usd / priced if priced else None,
            "calls_per_session": sum(session["calls"] for session in mine) / summarised if summarised else None,
            "delegate": {command: sum(session["delegate"][command] for session in mine)
                         for command in DELEGATE_COMMANDS},
            "delegate_tokens": {}, "pools": {},
            "est_pools": {pool: _points([s["est_pools"][pool] for s in mine if pool in s["est_pools"]])
                          for pool in rates},
        }
        for agent in sorted({agent for session in mine for agent in session["delegate_tokens"]}):
            arm["delegate_tokens"][agent] = {
                key: sum(session["delegate_tokens"].get(agent, {}).get(key, 0) for session in mine)
                for key in DELEGATE_TOKENS}
        for pool in pool_names:
            arm["pools"][pool] = _points([session["pools"][pool] for session in mine
                                          if session["pools"].get(pool) is not None])
        arms[mode] = arm
    return {"log": None, "run_log": None, "since": since, "calibration": calibration, "arms": arms,
            "not_drawn": not_drawn, "skipped_no_start": skipped,
            "weights": {"configs": [{"weights": dict(config), "sessions": count}
                                    for config, count in sorted(configs.items())], "unknown": unknown},
            "sessions": sessions}


def _table_lines(table):
    widths = [max(len(row[i]) for row in table) for i in range(len(table[0]))]
    return ["  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip() for row in table]


def render_by_mode(result, as_json=False, sessions=False):
    if as_json:
        return json.dumps(result, indent=2)
    arms = result["arms"]
    count = sum(arm["sessions"] for arm in arms.values())
    since = f" since {result['since']}" if result["since"] else ""
    lines = [f"session log: {result['log']}  ({count} drawn sessions{since})"]
    table = [("MODE", "SESS", "REROLL", "PEND", "CALLS", "SUB", "UNITS", "USD", "USD/S",
              "LAUNCH", "RUNS", "APPLIED", "ASK", "BLOCKED")]
    money = lambda value: f"{value:.2f}" if value is not None else "-"
    for mode in MODES:
        arm = arms[mode]
        has_usd = arm["summarised"] > arm["usd_null_sessions"]
        table.append((mode, str(arm["sessions"]), str(arm["rerolls"]), str(arm["pending"]),
                      _fmt_int(arm["calls"]), _fmt_int(arm["subagent_calls"]), _fmt_int(arm["cost_units"]),
                      money(arm["usd_list_price"] if has_usd else None), money(arm["usd_per_session"]),
                      str(arm["delegate"]["launch"]), str(arm["runs"]), str(arm["applied"]),
                      str(arm["delegate"]["ask"]), str(arm["blocked"])))
    lines.extend(_table_lines(table))
    calibration = result.get("calibration") or {}
    read = sorted({pool for arm in arms.values() for pool in arm["pools"]})
    claude = [pool for pool in ESTIMATED_POOLS if pool in read or pool in (calibration.get("pools") or {})]
    others = [pool for pool in read if pool not in claude]
    if claude or others:
        lines.append("Claude pool points (sum, n): " + _estimate_note(calibration) if claude
                     else "pool points used, READ (sum, n):")
        lines.extend(_pool_lines(arms, claude, others, "sum", footnote=bool(claude)))
    else:
        lines.append("pool points used: no quota readings in the summaries yet")
    if result.get("calibration") is not None and not claude:  # estimates need no readings, only a fit
        lines.append("Claude pool points: " + _estimate_note(calibration))
    lines.append("not drawn: " + ", ".join(f"{key} {value}" for key, value in result["not_drawn"].items()))
    lines.append(f"skipped: {result['skipped_no_start']} sessions without a start row")
    configs = result["weights"]["configs"]
    if len(configs) >= 2:
        labels = [f"{json.dumps(config['weights'], sort_keys=True)} ({config['sessions']} sessions)"
                  for config in configs]
        lines.append("warning: draw weights differ: " + "; ".join(labels) + "; compare arms within one config")

    lines.append("session means and delegate outcomes:")
    table = [("MODE", "SUMM", "USD-NULL", "CALLS/S", "FOLLOWUP", "REVIEW", "DISCARDED")]
    for mode in MODES:
        arm = arms[mode]
        table.append((mode, str(arm["summarised"]), str(arm["usd_null_sessions"]),
                      money(arm["calls_per_session"]), str(arm["delegate"]["followup"]),
                      str(arm["delegate"]["review"]), str(arm["discarded"])))
    lines.extend(_table_lines(table))
    if claude or others:
        lines.append("pool points per measured session (mean):")
        lines.extend(_pool_lines(arms, claude, others, "mean"))
    if any(arm["delegate_tokens"] for arm in arms.values()):
        lines.append("delegate tokens by agent (rounds and reviews):")
        table = [("MODE", "AGENT", "IN", "CACHED", "OUT")]
        for mode in MODES:
            for agent, usage in arms[mode]["delegate_tokens"].items():
                table.append((mode, agent, *(_fmt_int(usage[key]) for key in DELEGATE_TOKENS)))
        lines.extend(_table_lines(table))
    if all("items" in arm for arm in arms.values()):
        lines.extend(_render_items(result))
    if sessions:
        lines.append("sessions:")
        points = lambda value: f"{value:.1f}" if value is not None else "-"
        table = [("SESSION", "START", "MODE", "FLAGS", "REPO", "CALLS", "USD",
                  *(label for pool in ESTIMATED_POOLS for label in (f"{pool.upper()} EST", "READ")),
                  "LAUNCH", "APPLIED", "FIN/ITEMS")]
        for session in result["sessions"]:
            flags = "/".join(key for key in ("reroll", "pending") if session[key]) or "-"
            table.append((session["session"][:8], session["start_date"] or "-", session["mode"], flags,
                          session["repo"] or session["repo_dir"] or "-",
                          _fmt_int(session["calls"]) if session["summarised"] else "-",
                          money(session["usd_list_price"]),
                          *(points(source.get(pool)) for pool in ESTIMATED_POOLS
                            for source in (session["est_pools"], session["pools"])),
                          str(session["delegate"]["launch"]), str(session["applied"]),
                          _fin_items(session.get("items"))))
        lines.extend(_table_lines(table))
    return "\n".join(lines)


def _estimate_note(calibration):
    rates = calibration.get("pools") or {}
    if not rates:
        where = f" (none usable at {calibration['path']})" if calibration.get("path") else ""
        return f"EST needs a saved fit: run `delegate cost --calibrate --save`{where}"
    local = lambda value: _row_ts(value).astimezone().date().isoformat() if _row_ts(value) else "?"
    span = calibration.get("span") or [None, None]
    fitted = ", ".join(f"{pool} {rates[pool]['per_usd']:.3g}" for pool in ESTIMATED_POOLS if pool in rates)
    return (f"EST = list-price USD x points per USD ({fitted}; fit saved "
            f"{local(calibration.get('saved_at'))}, readings {local(span[0])} to {local(span[1])})")


def _pool_lines(arms, claude, others, stat, footnote=False):
    """Claude pools as EST beside READ, then the other pools' reading deltas."""
    def cell(agg):
        if not agg or not agg["n"]:
            return "-"
        return f"{agg['sum']:.1f} ({agg['n']})" if stat == "sum" else f"{agg['mean']:.1f}"
    lines = []
    if claude:
        lines.extend(_table_lines([("MODE", *(label for pool in claude for label in (f"{pool} EST", "READ")))] + [
            (mode, *(cell(source.get(pool)) for pool in claude
                     for source in (arms[mode]["est_pools"], arms[mode]["pools"])))
            for mode in MODES]))
        if footnote:
            lines.append("READ: the SessionStart-to-SessionEnd delta in one reset window; it also counts other "
                         "sessions' use and the hours a session stays open")
    if others:
        if claude:
            lines.append("other pools, READ:")
        lines.extend(_table_lines([("MODE", *others)] + [
            (mode, *(cell(arms[mode]["pools"].get(pool)) for pool in others)) for mode in MODES]))
    return lines


def _fin_items(items):
    if items is None:
        return "-"
    items = [item for item in items if item["type"] != "field-report"]  # overhead, as in rate.attach_items
    return f"{sum(item['status'] == 'finished' for item in items)}/{len(items)}"


def _render_items(result):
    """Rated items per arm: the headline divides all USD of rated sessions by finished items."""
    arms = result["arms"]
    money = lambda value: f"{value:.2f}" if value is not None else "-"
    rubric = next(iter(arms.values()))["items"]["rubric"]
    lines = [f"items (rubric {rubric}, rated sessions only; USD/FIN = all USD of rated sessions / finished items):"]
    table = [("MODE", "RATED", "UNRATED", "ITEMS", "FIN", "PART", "UNCL", "CONT", "FIN%", "USD/FIN",
              "IF UNCL FIN", "END", "FR")]
    for mode in MODES:
        items = arms[mode]["items"]
        share = items["finished_share"]
        table.append((mode, str(items["rated_sessions"]), str(items["unrated_sessions"] + items["errors"]),
                      str(items["items"]), str(items["finished"]), str(items["partial"]), str(items["unclear"]),
                      str(items["continuations"]), f"{share * 100:.0f}" if share is not None else "-",
                      money(items["usd_per_finished"]), money(items["usd_per_finished_or_unclear"]),
                      str(items["end_state_sessions"]), str(items["field_reports"])))
    lines.extend(_table_lines(table))
    lines.append("IF UNCL FIN: USD/FIN with unclear items counted as finished, the range's other end; "
                 "END: rated sessions with an end-state HANDOFF.md; FR: field-report items, not in ITEMS")
    for key, label in (("by_size", "by size"), ("by_type", "by type")):
        groups = [g for g in result.get("item_order", {}).get(key, [])
                  if any(g in arm["items"][key] for arm in arms.values())]
        if not groups:
            continue
        lines.append(f"{label} (finished/items, USD per finished item from per-item call costs):")
        table = [("MODE", *groups)]
        for mode in MODES:
            buckets = arms[mode]["items"][key]
            table.append((mode, *(f"{buckets[g]['finished']}/{buckets[g]['items']} {money(buckets[g]['usd_per_finished'])}"
                                  if g in buckets else "-" for g in groups)))
        lines.extend(_table_lines(table))
    return lines


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
    if a.save and not a.calibrate:
        sys.exit("--save needs --calibrate")
    if a.calibrate:
        if a.by_mode or a.session is not None:
            sys.exit("--calibrate cannot be combined with --by-mode or --session")
        import calibrate
        calibrate.cmd_calibrate(a)
        return
    if a.by_mode:
        if a.session is not None:
            sys.exit("--by-mode cannot be combined with --session")
        log = runs.session_log_path(a.session_log)
        run_log = Path(a.log) if a.log else DEFAULT_LOG
        rows = load_jsonl(log)
        import calibrate  # here, not at the top: calibrate imports cost
        result = by_mode(rows, load_jsonl(run_log), since=a.since, repo=a.repo,
                         calibration=calibrate.usd_rates(calibrate.saved_path(a.calibration, a.quota_log)))
        result.update(log=str(log), run_log=str(run_log))
        import rate  # here, not at the top: rate imports cost
        weights, prices = load_catalog_cost()
        rate.attach_items(result, rows, weights, prices)
        print(render_by_mode(result, as_json=a.json, sessions=a.sessions))
        return
    if a.session:
        path = resolve_session(a.session)
        weights, prices = load_catalog_cost()
        calls = parse_session_usage(path)
        summary = summarise_session(calls, weights, prices)
        print(render_session(path, summary, as_json=a.json))
        return
    log = Path(a.log) if a.log else DEFAULT_LOG
    rows = load_run_log(log, since=a.since, repo=a.repo)
    groups = summarise_runs(rows)
    print(render_runs(rows, groups, since=a.since, repo=a.repo, log_path=log, as_json=a.json))


def _add_args(p):
    p.add_argument("--since", help="YYYY-MM-DD (local) or ISO timestamp; only runs/session starts at or after it")
    p.add_argument("--repo", help="filter by repo directory name")
    p.add_argument("--log", help=f"run log path (default {DEFAULT_LOG})")
    p.add_argument("--session", metavar="ID|latest|PATH",
                   help="orchestrator Claude Code transcript (deduped by message.id)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--by-mode", action="store_true", help="compare randomised session-mode arms")
    p.add_argument("--session-log", metavar="PATH",
                   help=f"session log path for --by-mode (default {runs.session_log_path()})")
    p.add_argument("--sessions", action="store_true", help="also print per-session rows with --by-mode")
    p.add_argument("--calibrate", action="store_true",
                   help="fit pool points per token class and per list-price USD from quota reading pairs")
    p.add_argument("--quota-log", metavar="PATH", help="quota readings for --calibrate (default: delegate quota's log)")
    p.add_argument("--save", action="store_true",
                   help="with --calibrate: store the fit with its date for --by-mode's estimated Claude pool points")
    p.add_argument("--calibration", metavar="PATH",
                   help="saved fit for --save and --by-mode (default: calibration.json next to the quota log)")
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

"""Quota windows (unused expiry) and per-job pool costs from local logs."""
import json
import math
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cost
import quota

TOL_S = 120
DROP = 0.5
GAP_H = 1.0
LAG = timedelta(minutes=30)
ASK = timedelta(minutes=10)
LAUNCH_MATCH_S = 180
ALL_POOLS = tuple(quota.POOLS)
# Text table without --pool: long windows only; 5h pools stay in the summary.
TABLE_POOLS = ("claude-7d", "codex-7d", "cursor-auto", "cursor-premium")
AGENT_POOLS = {
    "codex": ["codex-5h", "codex-7d"],
    "cursor": ["cursor-auto", "cursor-premium"],
}


def load_rows(path):
    """Object rows in file order; missing file → []."""
    return cost.load_jsonl(path)


def safe_parse_ts(value):
    """Parse a log timestamp; None for non-strings or invalid values."""
    if not isinstance(value, str):
        return None
    try:
        return quota.parse_ts(value)
    except (TypeError, ValueError):
        return None


def good_entry(row, pid):
    """(used_pct, resets_at) when the pool entry is usable for windows/costs."""
    if not isinstance(row, dict) or "error" in row:
        return None
    pools = row.get("pools")
    if not isinstance(pools, dict):
        return None
    entry = pools.get(pid)
    if not isinstance(entry, dict) or entry.get("stale") or entry.get("stale_reset"):
        return None
    try:
        used = float(entry["used_pct"])
    except (KeyError, TypeError, ValueError):
        return None
    if not 0.0 <= used <= 100.0:
        return None
    reset = safe_parse_ts(entry.get("resets_at"))
    if reset is None:
        return None
    return used, reset


def _reading_ts(row):
    if not isinstance(row, dict) or "error" in row:
        return None
    return safe_parse_ts(row.get("ts"))


def pool_readings(rows, pid):
    """(ts, used_pct, resets_at) for pool pid, sorted by ts (file order for ties)."""
    pts = []
    for row in rows:
        ts = _reading_ts(row)
        entry = good_entry(row, pid)
        if ts is None or entry is None:
            continue
        pts.append((ts, entry[0], entry[1]))
    # Stable sort: file order preserved for equal timestamps.
    indexed = list(enumerate(pts))
    indexed.sort(key=lambda it: (it[1][0], it[0]))
    return [p for _, p in indexed]


def windows_for_pool(rows, pid, now):
    """Build closed/open windows for one pool, merging consecutive idle spans."""
    pts = pool_readings(rows, pid)
    raw = []
    for ts, used, reset in pts:
        cur = raw[-1] if raw else None
        if (cur is not None
                and abs((reset - cur["resets_at"]).total_seconds()) <= TOL_S
                and used >= cur["last_used"] - DROP):
            cur["peak"] = max(cur["peak"], used)
            cur["last_used"] = used
            cur["last_seen"] = ts
            cur["readings"] += 1
        else:
            raw.append({
                "start": ts, "last_seen": ts, "resets_at": reset,
                "peak": used, "last_used": used, "readings": 1,
            })
    for i, w in enumerate(raw):
        nxt_start = raw[i + 1]["start"] if i + 1 < len(raw) else None
        w["end"] = min(w["resets_at"], nxt_start) if nxt_start is not None else w["resets_at"]
        w["closed"] = nxt_start is not None or w["resets_at"] <= now
        w["early"] = nxt_start is not None and nxt_start < w["resets_at"] - timedelta(seconds=TOL_S)
        w["gap_h"] = (w["end"] - w["last_seen"]).total_seconds() / 3600.0
        w["hours"] = (w["end"] - w["start"]).total_seconds() / 3600.0
        w["lower_bound"] = w["gap_h"] > GAP_H
        w["idle"] = w["peak"] == 0.0
        w["merged"] = 1
        w["expired"] = (100.0 - w["peak"]) if (w["closed"] and not w["idle"]) else None

    out = []
    for w in raw:
        if w["idle"] and out and out[-1]["idle"]:
            prev = out[-1]
            prev["end"] = w["end"]
            prev["closed"] = w["closed"]
            prev["last_seen"] = w["last_seen"]
            prev["readings"] += w["readings"]
            prev["merged"] += 1
            prev["hours"] = (prev["end"] - prev["start"]).total_seconds() / 3600.0
            prev["gap_h"] = (prev["end"] - prev["last_seen"]).total_seconds() / 3600.0
            prev["lower_bound"] = prev["gap_h"] > GAP_H
            prev["early"] = w["early"]  # last segment's early; idle spans aren't in expiry totals
            continue
        out.append({k: w[k] for k in (
            "start", "end", "last_seen", "resets_at", "hours", "peak", "expired",
            "early", "gap_h", "lower_bound", "idle", "merged", "readings", "closed",
        )})
    return out


def summarize_windows(wins, since=None):
    """Per-pool summary over closed windows with end ≥ since (all when since is None)."""
    def kept(w):
        if since is not None and w["end"] < since:
            return False
        return True

    closed = [w for w in wins if w["closed"] and not w["idle"] and kept(w)]
    idle = [w for w in wins if w["idle"] and w["closed"] and kept(w)]
    return {
        "windows": len(closed),
        "early": sum(1 for w in closed if w["early"]),
        "lower_bound": sum(1 for w in closed if w["lower_bound"]),
        "mean_peak": (sum(w["peak"] for w in closed) / len(closed)) if closed else None,
        "expired_sum": float(sum(w["expired"] for w in closed)) if closed else 0.0,
        "idle_spans": len(idle),
        "idle_hours": float(sum(w["hours"] for w in idle)),
    }


def filter_windows(wins, since=None):
    if since is None:
        return wins
    return [w for w in wins if (not w["closed"]) or w["end"] >= since]


def extract_jobs(run_rows, since=None):
    """Timed round/review jobs from the run log."""
    jobs = []
    for row in run_rows:
        run = row.get("run")
        agent = row.get("agent")
        for rd in row.get("rounds") or []:
            started = safe_parse_ts(rd.get("started"))
            minutes = rd.get("minutes")
            if started is None or not isinstance(minutes, (int, float)):
                continue
            if since is not None and started < since:
                continue
            n = rd.get("n", rd.get("round"))
            effort = rd["effort"] if "effort" in rd else row.get("effort")
            jobs.append({
                "run": run, "job": f"round-{n}", "kind": "round", "agent": agent,
                "model": rd.get("model") if rd.get("model") is not None else row.get("model"),
                "effort": effort,
                "start": started, "end": started + timedelta(minutes=minutes),
            })
        for rv in row.get("reviews") or []:
            started = safe_parse_ts(rv.get("started"))
            minutes = rv.get("minutes")
            if started is None or not isinstance(minutes, (int, float)):
                continue
            if since is not None and started < since:
                continue
            jobs.append({
                "run": run, "job": f"review-{rv.get('n')}", "kind": "review",
                "agent": rv.get("agent"), "model": rv.get("model"), "effort": rv.get("effort"),
                "start": started, "end": started + timedelta(minutes=minutes),
            })
    return jobs


def _pools_for_job(job, catalog):
    agent, model = job.get("agent"), job.get("model")
    if catalog:
        for d in catalog.get("delegates") or []:
            if d.get("agent") == agent and d.get("model") == model and d.get("pools"):
                return list(d["pools"])
    return list(AGENT_POOLS.get(agent) or [])


def _busy_intervals(jobs, session_rows):
    """(agent, start, end, ref) activities that can overlap a measured job."""
    busy = [(j["agent"], j["start"], j["end"], j) for j in jobs]
    for s in session_rows:
        if s.get("event") != "delegate":
            continue
        ts = safe_parse_ts(s.get("ts"))
        if ts is None:
            continue
        agent = s.get("agent") or "cursor"
        vendors = ["codex", "cursor"] if agent == "both" else [agent]
        if s.get("command") == "ask":
            for v in vendors:
                busy.append((v, ts, ts + ASK, None))
        else:
            for v in vendors:
                busy.append((v, ts, ts, ("launchrow", s.get("run"), ts)))
    return busy


def _quantiles(values):
    if len(values) == 1:
        v = values[0]
        return v, v, v
    q = statistics.quantiles(values, n=4, method="inclusive")
    return q[0], q[1], q[2]


def job_costs(quota_rows, run_rows, session_rows, catalog=None, since=None):
    """Group clean per-job pool deltas; return jobs count, skips and groups."""
    # Overlap index uses every timed job; since only filters which jobs are sampled.
    all_jobs = extract_jobs(run_rows, since=None)
    jobs = ([j for j in all_jobs if j["start"] >= since] if since is not None else all_jobs)
    indexed = []
    for i, row in enumerate(quota_rows):
        ts = _reading_ts(row)
        if ts is None:
            continue
        indexed.append((ts, i, row))
    indexed.sort(key=lambda t: (t[0], t[1]))
    qrows = [(ts, row) for ts, _, row in indexed]
    busy = _busy_intervals(all_jobs, session_rows)
    samples = defaultdict(list)
    skip = Counter()
    for j in jobs:
        pools = _pools_for_job(j, catalog)
        if not pools:
            skip["agent"] += 1
            continue
        cands = []
        for ts, row in qrows:
            if row.get("reason") != "launch" or row.get("run") != j["run"]:
                continue
            job_key = row.get("job")
            if job_key == j["job"]:
                pass
            elif "job" not in row and abs((ts - j["start"]).total_seconds()) <= LAUNCH_MATCH_S:
                pass
            else:
                continue
            if all(good_entry(row, p) for p in pools):
                cands.append((ts, row))
        if not cands:
            skip["no launch reading"] += 1
            continue
        before_ts, before = min(cands, key=lambda t: abs((t[0] - j["start"]).total_seconds()))
        after = None
        for ts, row in qrows:
            if (row.get("reason") == "job-end" and row.get("run") == j["run"]
                    and row.get("job") == j["job"] and ts >= before_ts
                    and all(good_entry(row, p) for p in pools)):
                after = (ts, row)
                break
        if after is None:
            for ts, row in qrows:
                if j["end"] <= ts <= j["end"] + LAG and all(good_entry(row, p) for p in pools):
                    after = (ts, row)
                    break
        if after is None:
            skip["no reading after"] += 1
            continue
        after_ts, after_row = after
        lo, hi = before_ts, after_ts
        overlapped = False
        for agent, s, e, ref in busy:
            if agent != j["agent"] or ref is j:
                continue
            if (isinstance(ref, tuple) and ref[0] == "launchrow"
                    and ref[1] == j["run"]
                    and abs((ref[2] - j["start"]).total_seconds()) <= LAUNCH_MATCH_S):
                continue
            if s <= hi and e >= lo:
                overlapped = True
                break
        if overlapped:
            skip["overlap"] += 1
            continue
        delta = {}
        changed = False
        for p in pools:
            before_e, after_e = good_entry(before, p), good_entry(after_row, p)
            ub, rb = before_e
            ua, ra = after_e
            if abs((ra - rb).total_seconds()) > TOL_S or ua < ub - DROP:
                changed = True
                break
            delta[p] = max(0.0, ua - ub)
        if changed:
            skip["window changed"] += 1
            continue
        key = (j["agent"], j["model"], j["effort"], j["kind"])
        samples[key].append(delta)

    groups = []
    for key in sorted(samples, key=lambda k: (-len(samples[k]),) + tuple("" if x is None else x for x in k)):
        ds = samples[key]
        agent, model, effort, kind = key
        pool_stats = {}
        for p in ds[0]:
            vals = [d[p] for d in ds]
            p25, med, p75 = _quantiles(vals)
            pool_stats[p] = {"median": med, "p25": p25, "p75": p75}
        groups.append({
            "agent": agent, "model": model, "effort": effort, "kind": kind,
            "n": len(ds), "pools": pool_stats,
        })
    return {"jobs": len(jobs), "skipped": dict(skip), "groups": groups}


def rounds_left(group, live_pools):
    """floor(min left_pct/median) over pools with median > 0; None if none or n < 3.

    Any pool with median > 0 that lacks a usable live left_pct makes the estimate None,
    so a missing/errored limiting pool cannot overstate what is left.
    """
    if not group or group.get("kind") != "round" or group.get("n", 0) < 3:
        return None
    ratios = []
    for pid, stats in (group.get("pools") or {}).items():
        med = stats.get("median")
        if med is None or med <= 0:
            continue
        entry = (live_pools or {}).get(pid)
        if not isinstance(entry, dict) or "error" in entry:
            return None
        left_raw = entry.get("left_pct")
        if left_raw is None:
            return None
        try:
            left = float(left_raw)
        except (TypeError, ValueError):
            return None
        ratios.append(left / med)
    if not ratios:
        return None
    return math.floor(min(ratios))


def find_round_group(groups, agent, model, effort):
    for g in groups or []:
        if (g.get("kind") == "round" and g.get("agent") == agent
                and g.get("model") == model and g.get("effort") == effort):
            return g
    return None


def annotate_route_rows(rows, groups, live_pools):
    """Add rounds_left and round_cost on each row (None when unknown / n < 3)."""
    for r in rows:
        d = r["delegate"]
        g = find_round_group(groups, d.get("agent"), d.get("model"), d.get("effort"))
        if g is None or g.get("n", 0) < 3:
            r["rounds_left"] = None
            r["round_cost"] = None
        else:
            r["round_cost"] = {pid: s["median"] for pid, s in g["pools"].items()}
            r["rounds_left"] = rounds_left(g, live_pools)
    return rows


def load_cost_groups(quota_log=None, run_log=None, session_log=None, catalog=None, since=None):
    """Best-effort load of cost groups; returns [] on any failure."""
    try:
        qpath = Path(quota_log) if quota_log is not None else quota.log_path()
        rpath = Path(run_log) if run_log is not None else cost.DEFAULT_LOG
        spath = Path(session_log) if session_log is not None else runs_session_log()
        result = job_costs(load_rows(qpath), load_rows(rpath), load_rows(spath),
                           catalog=catalog, since=since)
        return result.get("groups") or []
    except Exception:
        return []


def runs_session_log():
    import runs
    return runs.session_log_path()


def build_report(quota_rows, run_rows, session_rows, now, catalog=None, since=None,
                 pools=None, log=None, run_log=None, session_log=None):
    """Full --unused report structure (JSON-serialisable after _jsonable)."""
    pool_ids = list(pools) if pools is not None else list(ALL_POOLS)
    out_pools = {}
    for pid in pool_ids:
        wins = windows_for_pool(quota_rows, pid, now)
        out_pools[pid] = {
            "summary": summarize_windows(wins, since=since),
            "windows": filter_windows(wins, since=since),
        }
    costs = job_costs(quota_rows, run_rows, session_rows, catalog=catalog, since=since)
    used = 0
    first_ts = last_ts = None
    for row in quota_rows:
        if not isinstance(row, dict) or "error" in row:
            continue
        ts = _reading_ts(row)
        if ts is None:
            continue
        if not any(good_entry(row, pid) for pid in ALL_POOLS):
            continue
        used += 1
        if first_ts is None or ts < first_ts:
            first_ts = ts
        if last_ts is None or ts > last_ts:
            last_ts = ts
    return {
        "log": str(log) if log is not None else str(quota.log_path()),
        "run_log": str(run_log) if run_log is not None else str(cost.DEFAULT_LOG),
        "session_log": str(session_log) if session_log is not None else str(runs_session_log()),
        "now": _iso(now),
        "since": _iso(since) if since is not None else None,
        "readings": used,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "pools": out_pools,
        "costs": costs,
    }


def _iso(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _jsonable(report):
    """Convert datetimes in windows to ISO strings; drop helper keys."""
    pools = {}
    for pid, block in report["pools"].items():
        windows = []
        for w in block["windows"]:
            windows.append({
                "start": _iso(w["start"]),
                "end": _iso(w["end"]),
                "last_seen": _iso(w["last_seen"]),
                "resets_at": _iso(w["resets_at"]),
                "hours": w["hours"],
                "peak": w["peak"],
                "expired": w["expired"],
                "early": w["early"],
                "gap_h": w["gap_h"],
                "lower_bound": w["lower_bound"],
                "idle": w["idle"],
                "merged": w["merged"],
                "readings": w["readings"],
                "closed": w["closed"],
            })
        pools[pid] = {"summary": block["summary"], "windows": windows}
    return {
        "log": report["log"],
        "run_log": report["run_log"],
        "session_log": report["session_log"],
        "now": report["now"],
        "since": report["since"],
        "readings": report["readings"],
        "pools": pools,
        "costs": report["costs"],
    }


def _fmt_local(dt):
    return dt.astimezone().strftime("%a %d %b %H:%M")


def _text_pct(x):
    """At most one decimal for text tables; whole numbers stay bare (98, 1912)."""
    return f"{round(float(x), 1):g}"


def _note(w):
    parts = []
    if not w["closed"]:
        parts.append(f"open, resets {_fmt_local(w['resets_at'])} ({_text_pct(w['peak'])}% so far)")
    else:
        if w["idle"]:
            parts.append(f"idle ×{w['merged']}")
        if w["early"] and not w["idle"]:
            parts.append("early")
        if w["lower_bound"] and not w["idle"]:
            parts.append(f"last reading {w['gap_h']:.1f}h before close")
    return "; ".join(parts)


def _peak_cell(w):
    if w["idle"]:
        return "0"
    peak = _text_pct(w["peak"])
    if w["closed"] and w["lower_bound"]:
        return "≥" + peak
    return peak


def _expired_cell(w):
    if w["expired"] is None:
        return ""
    text = _text_pct(w["expired"])
    if w["lower_bound"]:
        return "≤" + text
    return text


def render_unused(report, pool_filter=None):
    """Text form of --unused."""
    lines = []
    first = report.get("first_ts")
    last = report.get("last_ts")
    span = ""
    if first is not None and last is not None:
        span = f", { _fmt_local(first) } – { _fmt_local(last) }"
    lines.append(f"log: {report['log']}  ({report['readings']} readings{span})")
    lines.append("")

    table_pids = list(pool_filter) if pool_filter is not None else list(TABLE_POOLS)
    hdr = ("POOL", "START", "END", "HOURS", "PEAK", "EXPIRED", "NOTE")
    rows = [hdr]
    for pid in table_pids:
        block = report["pools"].get(pid) or {"windows": []}
        for w in block["windows"]:
            rows.append((
                pid,
                _fmt_local(w["start"]),
                _fmt_local(w["end"]) if w["closed"] else _fmt_local(w["resets_at"]),
                f"{w['hours']:.1f}",
                _peak_cell(w),
                _expired_cell(w),
                _note(w),
            ))
    widths = [max(len(r[i]) for r in rows) for i in range(len(hdr))]
    lines += ["  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows]
    lines.append("")

    sum_pids = list(pool_filter) if pool_filter is not None else list(ALL_POOLS)
    shdr = ("POOL", "WINDOWS", "EARLY", "≥PEAK", "MEAN PEAK", "EXPIRED Σ", "IDLE", "IDLE H")
    srows = [shdr]
    for pid in sum_pids:
        s = report["pools"][pid]["summary"]
        mean = f"{s['mean_peak']:.2f}" if s["mean_peak"] is not None else "-"
        srows.append((
            pid, str(s["windows"]), str(s["early"]), str(s["lower_bound"]),
            mean, _text_pct(s["expired_sum"]), str(s["idle_spans"]), f"{s['idle_hours']:.1f}",
        ))
    sw = [max(len(r[i]) for r in srows) for i in range(len(shdr))]
    lines += ["  ".join(c.ljust(w) for c, w in zip(r, sw)).rstrip() for r in srows]
    lines.append("")

    costs = report["costs"]
    chdr = ["AGENT", "MODEL", "EFFORT", "KIND", "N"]
    # Collect pool columns in a stable order across groups.
    pool_cols = []
    seen = set()
    for g in costs["groups"]:
        for p in g["pools"]:
            if p not in seen:
                seen.add(p)
                pool_cols.append(p)
    chdr += pool_cols
    crows = [tuple(chdr)]
    for g in costs["groups"]:
        row = [
            str(g["agent"] or ""),
            str(g["model"] or ""),
            "-" if g["effort"] is None else str(g["effort"]),
            str(g["kind"]),
            str(g["n"]),
        ]
        for p in pool_cols:
            stats = g["pools"].get(p)
            row.append(f"{stats['median']:g}" if stats else "")
        crows.append(tuple(row))
    if len(crows) == 1:
        lines.append("(no clean per-job cost samples)")
    else:
        cw = [max(len(r[i]) for r in crows) for i in range(len(chdr))]
        lines += ["  ".join(c.ljust(w) for c, w in zip(r, cw)).rstrip() for r in crows]
    skipped = costs.get("skipped") or {}
    if skipped:
        bits = ", ".join(f"{k} {v}" for k, v in sorted(skipped.items(), key=lambda kv: (-kv[1], kv[0])))
        lines.append(f"skipped ({costs['jobs']} timed jobs): {bits}")
    else:
        lines.append(f"skipped: none ({costs['jobs']} timed jobs)")
    lines.append("")
    lines.append("EXPIRED is an upper bound on the share that went unused (100 − peak).")
    lines.append("Idle spans (peak 0) are listed but never counted as expired.")
    lines.append("Direct Codex/Cursor use outside `delegate` is invisible in these logs.")
    return "\n".join(lines)


def render_rounds_left_lines(groups, live_pools):
    """Lines for live `delegate quota` text; empty on bad input."""
    lines = []
    try:
        for g in groups or []:
            if g.get("kind") != "round" or g.get("n", 0) < 3:
                continue
            left = rounds_left(g, live_pools)
            if left is None:
                continue
            effort = g.get("effort")
            tag = f"{g['agent']} {g['model']}" + (f"@{effort}" if effort is not None else "")
            parts = [f"{pid} {stats['median']:g}" for pid, stats in g["pools"].items()]
            lines.append(
                f"per round (median of {g['n']} clean rounds): {tag} "
                + ", ".join(parts)
                + f" → about {left} left"
            )
    except Exception:
        return []
    return lines


def unused(log=None, run_log=None, session_log=None, since=None, pools=None,
           now=None, catalog=None, as_json=False):
    """Load logs and render --unused (text or JSON object)."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    qpath = Path(log).expanduser() if log is not None else quota.log_path()
    rpath = Path(run_log).expanduser() if run_log is not None else cost.DEFAULT_LOG
    spath = Path(session_log).expanduser() if session_log is not None else runs_session_log()
    since_dt = cost._since_dt(since) if since else None
    report = build_report(
        load_rows(qpath), load_rows(rpath), load_rows(spath), now,
        catalog=catalog, since=since_dt, pools=pools,
        log=qpath, run_log=rpath, session_log=spath,
    )
    if as_json:
        return _jsonable(report)
    return render_unused(report, pool_filter=pools)

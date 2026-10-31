#!/usr/bin/env python3
"""Frozen control-session analysis. Readers and CLI are the only I/O boundaries."""
import argparse
import hashlib
import json
import math
import random
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cost
import runs

T0 = datetime(2026, 10, 31, 0, 29, 58, tzinfo=timezone.utc)
PERIOD2_START = datetime(2026, 10, 26, 17, 20, 51, tzinfo=timezone.utc)
HARD_CLOSE = datetime(2026, 11, 30, tzinfo=timezone.utc)
TARGET = 60
FREEZE_DELAY = timedelta(days=7)
DRAWS = 10_000
SEED = 0
RANKS = (250, 9750)
SIZE_WEIGHTS = {"S": 1, "M": 3, "L": 8, "XL": 20}
LONG_SESSION = timedelta(hours=8)
CALIBRATION_SINCE = datetime(2026, 10, 26, 17, 1, tzinfo=timezone.utc)
CALIBRATION_AFTER = datetime(2026, 11, 2, tzinfo=timezone.utc)
ARMS = tuple(sorted(cost.MODES))
EXCLUSIONS = ("pending", "ghost", "unpriced", "unrated", "transcript_gone", "stale")


def _now():
    return datetime.now(timezone.utc)


def instant(value):
    ts = cost._row_ts(value)
    return ts.astimezone(timezone.utc) if ts is not None else None


def iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def session_records(rows):
    """First start and latest summary/rating/end in file order; no outcome access."""
    state = {}
    for row in rows:
        session = row.get("session")
        if not isinstance(session, str) or not session:
            continue
        record = state.setdefault(session, {"session": session, "rated": False})
        event = row.get("event")
        if event == "start":
            record.setdefault("start", row)
        elif event in ("summary", "rating", "end"):
            record[event] = row
        if event == "rating" and row.get("rubric") == "v2":
            record["rated"] = True
    return state


def ghost(record):
    summary = record.get("summary")
    return summary is not None and summary.get("transcript") is None and not summary.get("calls")


def close_info(rows, records, now):
    """Count first v2 ratings in append order, using the latest ghost state."""
    counted = set()
    close, close_by = HARD_CLOSE, "date"
    for row in rows:
        if row.get("event") != "rating" or row.get("rubric") != "v2":
            continue
        session = row.get("session")
        record = records.get(session, {})
        start = record.get("start", {})
        ts = instant(start.get("ts"))
        if (session in counted or not cost.is_drawn(start) or start.get("mode") != "claude-only"
                or ts is None or not T0 < ts < HARD_CLOSE or ghost(record)):
            continue
        counted.add(session)
        if len(counted) == TARGET:
            close, close_by = min(HARD_CLOSE, instant(row["ts"])), "target"
            break
    freeze = close + FREEZE_DELAY
    return {"t0": iso(T0), "close": iso(close), "close_by": close_by,
            "close_provisional": close_by == "date" and now < HARD_CLOSE,
            "freeze": iso(freeze), "freeze_reached": now >= freeze,
            "rated_claude_only": len(counted)}


def window_records(records, close):
    return [record for record in records.values()
            for start in [record.get("start", {})] for ts in [instant(start.get("ts"))]
            if cost.is_drawn(start) and start.get("mode") in ARMS and ts is not None and T0 < ts < close]


def progress(rows, now, *, experiment_log=False):
    records = session_records(rows)
    info = close_info(rows, records, now)
    arms = {mode: dict.fromkeys(("drawn", "ghosts", "rated"), 0) for mode in ARMS}
    for record in window_records(records, instant(info["close"])):
        arm = arms[record["start"]["mode"]]
        arm["drawn"] += 1
        arm["ghosts"] += ghost(record)
        arm["rated"] += record["rated"]
    count = info.pop("rated_claude_only")
    return {**info, "experiment_log": experiment_log,
            "progress": {"rated_claude_only": count, "arms": arms},
            "split_commit": None, "outcomes": None}


def preliminary_status(record):
    """Exclusions before transcript access, in their registered priority."""
    import rate  # rate imports cost
    summary, rating = record.get("summary"), record.get("rating", {})
    if summary is None:
        return "pending"
    if ghost(record):
        return "ghost"
    usd = summary.get("usd_list_price")
    if ((summary.get("usd_unpriced_calls") or 0) > 0 or isinstance(usd, bool)
            or not isinstance(usd, (int, float)) or not math.isfinite(usd)):
        return "unpriced"
    if (rating.get("rubric") != rate.RUBRIC or not isinstance(rating.get("items"), list)
            or type(rating.get("prompts")) is not int):
        return "unrated"
    return None


def transcript_path(record):
    return record["summary"].get("transcript") or record["start"].get("transcript")


def read_transcript_inputs(records, catalog_cost):
    """Read typed-prompt counts and pinned item_costs; errors contain no private paths."""
    import rate
    counts, costs = {}, {}
    for record in records:
        if preliminary_status(record) is not None:
            continue
        session, path = record["session"], transcript_path(record)
        try:
            if path is None:
                raise FileNotFoundError()
            counts[session] = len(rate.typed_prompts(rate.json_rows(path)))
        except (OSError, ValueError, TypeError):
            counts[session] = None
            continue
        if counts[session] != record["rating"]["prompts"]:
            continue
        try:
            costs[session] = rate.item_costs(record["rating"], path, *catalog_cost)
        except Exception:
            # A failure leaves this sensitivity only; never expose an exception's path/message.
            costs[session] = None
    return counts, costs


def analysis_sessions(records, prompt_counts):
    """Precompute one record per session; every window session gets exactly one status."""
    import rate
    result = []
    for record in records:
        start, summary = record["start"], record.get("summary", {})
        session = record["session"]
        status = preliminary_status(record)
        if status is None:
            count = prompt_counts.get(session)
            status = ("transcript_gone" if count is None else
                      "stale" if count != record["rating"]["prompts"] else "analysed")
        items = ([item for item in record["rating"]["items"] if item.get("type") != rate.FIELD_REPORT]
                 if status == "analysed" else [])
        ts = instant(start["ts"])
        result.append({"session": session, "mode": start["mode"], "ts": ts,
                       "project": start.get("repo_dir") or start.get("cwd") or "", "date": ts.date(),
                       "status": status, "usd": summary.get("usd_list_price") if status == "analysed" else 0,
                       "finished": sum(item.get("status") == "finished" for item in items),
                       "finished_or_unclear": sum(item.get("status") in ("finished", "unclear") for item in items),
                       "size_weighted": sum(SIZE_WEIGHTS[item["size"]] for item in items
                                            if item.get("status") == "finished"),
                       "items": items, "record": record})
    return result


def ratio(numerator, denominator):
    if denominator == 0:
        return math.inf if numerator > 0 else None
    return numerator / denominator


def hypotheses(totals):
    # Totals use ARMS order: claude-only, orchestrator, vanilla.
    (uc, fc), (uo, fo), (uv, fv) = totals
    return ratio(uo * fc, uc * fo), ratio(uv * fc, uc * fv)


def interval(observed, values, *, hypothesis="h1"):
    zero = observed is None or any(value is None for value in values)
    ordered = sorted(value for value in values if value is not None)
    lo, hi = (0, math.inf) if zero else (ordered[RANKS[0] - 1], ordered[RANKS[1] - 1])
    if hi < 1:
        reading = "orchestrator cheaper" if hypothesis == "h1" else "below 1"
    elif lo > 1:
        reading = "more expensive" if hypothesis == "h1" else "above 1"
    else:
        reading = "inconclusive"
    return {"observed": observed, "lo": lo, "hi": hi,
            "inf_share": sum(value == math.inf for value in values) / len(values),
            "zero_over_zero": zero, "reading": reading}


def session_tuples(sessions, measure="finished"):
    return tuple(tuple((s["usd"], s[measure]) for s in sorted(sessions, key=lambda s: (s["ts"], s["session"]))
                       if s["mode"] == mode) for mode in ARMS)


def session_replicates(arms):
    """All three arms consume the single stream in every replicate, even for H1."""
    rng = random.Random(SEED)
    h1, h2 = [], []
    for _ in range(DRAWS):
        totals = []
        for arm in arms:
            n = len(arm)
            drawn = [arm[rng.randrange(n)] for _ in range(n)]
            totals.append((math.fsum(s[0] for s in drawn), sum(s[1] for s in drawn)))
        a, b = hypotheses(totals)
        h1.append(a)
        h2.append(b)
    return h1, h2


def project_day_tuples(sessions):
    projects = []
    for project in sorted({s["project"] for s in sessions}):
        mine = [s for s in sessions if s["project"] == project]
        days = []
        for date in sorted({s["date"] for s in mine}):
            days.append(tuple(tuple((s["usd"], s["finished"]) for s in sorted(
                mine, key=lambda s: (s["ts"], s["session"])) if s["date"] == date and s["mode"] == mode)
                              for mode in ARMS))
        projects.append(tuple(days))
    return tuple(projects)


def project_day_replicates(projects):
    rng = random.Random(SEED)
    h1, h2 = [], []
    for _ in range(DRAWS):
        usd, finished = [[], [], []], [0, 0, 0]
        for dates in projects:
            d = len(dates)
            for _ in range(d):
                for arm, sessions in enumerate(dates[rng.randrange(d)]):
                    usd[arm].extend(s[0] for s in sessions)
                    finished[arm] += sum(s[1] for s in sessions)
        a, b = hypotheses([(math.fsum(usd[i]), finished[i]) for i in range(3)])
        h1.append(a)
        h2.append(b)
    return h1, h2


def estimate(sessions, measure="finished", *, project_days=False):
    arms = session_tuples(sessions, measure)
    observed = hypotheses([(math.fsum(s[0] for s in arm), sum(s[1] for s in arm)) for arm in arms])
    values = (project_day_replicates(project_day_tuples(sessions)) if project_days
              else session_replicates(arms))
    return {name: interval(obs, reps, hypothesis=name)
            for name, obs, reps in zip(("h1", "h2"), observed, values)}


def no_continuations(sessions, item_cost_results):
    import rate
    kept = []
    excluded = {mode: Counter() for mode in ARMS}
    for session in sessions:
        costs = item_cost_results.get(session["session"])
        if costs is None:
            excluded[session["mode"]]["item_costs_failed"] += 1
            continue
        items = session["record"]["rating"]["items"]
        dropped = [spent["usd"] for item, spent in zip(items, costs)
                   if item.get("continuation") and item.get("type") != rate.FIELD_REPORT]
        if any(usd is None for usd in dropped):
            excluded[session["mode"]]["continuation_unpriced"] += 1
            continue
        kept.append({**session, "usd": session["usd"] - math.fsum(dropped),
                     "finished": sum(item.get("status") == "finished" for item in session["items"]
                                     if not item.get("continuation"))})
    return kept, excluded


def under_8h(sessions):
    kept = []
    counts = {mode: {"over_8h": 0, "no_call_times": 0} for mode in ARMS}
    for session in sessions:
        summary = session["record"]["summary"]
        first, last = instant(summary.get("first_call")), instant(summary.get("last_call"))
        if first is None or last is None:
            counts[session["mode"]]["no_call_times"] += 1
        elif last - first > LONG_SESSION:
            counts[session["mode"]]["over_8h"] += 1
            continue
        kept.append(session)
    return kept, counts


def allocation(records):
    observed, expected = dict.fromkeys(ARMS, 0), dict.fromkeys(ARMS, 0.0)
    drawn = dict.fromkeys(ARMS, 0)
    missing = dict.fromkeys(ARMS, 0)
    for record in records:
        start = record["start"]
        weights = start.get("weights")
        drawn[start["mode"]] += 1
        if weights is None:
            missing[start["mode"]] += 1
            continue
        observed[start["mode"]] += 1
        for mode in ARMS:
            expected[mode] += weights.get(mode, 0)
    chi = math.fsum((observed[m] - expected[m]) ** 2 / expected[m] for m in ARMS if expected[m] > 0)
    return {"drawn": drawn, "observed": observed, "expected": expected, "without_weights": missing,
            "chi_squared": chi, "p_df2": math.exp(-chi / 2)}


def read_calibration(path):
    import calibrate
    out = {"available": False, "path": str(path), "sha256": None, "saved_at": None, "since": None}
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return {**out, "reason": "calibration file missing or unreadable"}
    rates = calibrate.usd_rates(path)
    out.update(sha256=hashlib.sha256(raw).hexdigest(), saved_at=rates["saved_at"], since=rates["since"])
    if instant(rates["since"]) != CALIBRATION_SINCE:
        return {**out, "reason": "since must equal " + iso(CALIBRATION_SINCE)}
    saved_at = instant(rates["saved_at"])
    if saved_at is None or saved_at < CALIBRATION_AFTER:
        return {**out, "reason": "saved_at must be at or after " + iso(CALIBRATION_AFTER)}
    rate = rates["pools"].get("claude-7d", {}).get("per_usd")
    if rate is None:
        return {**out, "reason": "no usable claude-7d rate"}
    return {**out, "available": True, "per_usd": rate}


def choose_split(commits, close, *, provisional=False):
    eligible = [c for c in commits if T0 < instant(c["committed_at"]) < close and c["lines"] > 0]
    if not eligible:
        return None
    chosen = min(eligible, key=lambda c: (-c["lines"], instant(c["committed_at"]), c["sha"]))
    return {**chosen, "provisional": provisional}


def read_split_commit(close, *, repo_dir=None, provisional=False):
    """Read only first-parent main history, without leaking git's path-bearing errors."""
    root = Path(repo_dir).resolve() if repo_dir else Path(__file__).resolve().parents[3]
    def git(*args):
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
        if result.returncode:
            raise ValueError("git command failed")
        return result.stdout.strip()
    try:
        if Path(git("rev-parse", "--show-toplevel")).resolve() != root:
            return None, "checkout is not a git top level"
    except (OSError, ValueError):
        return None, "git unavailable or checkout is not a git top level"
    try:
        log = git("log", "--first-parent", "--format=%H%x09%ct", "main")
    except (OSError, ValueError):
        return None, "main history unavailable"
    commits = []
    try:
        for line in log.splitlines():
            sha, epoch = line.split("\t")
            at = datetime.fromtimestamp(int(epoch), timezone.utc)
            if not T0 < at < close:
                continue
            try:
                git("rev-parse", "--verify", sha + "^1")
            except ValueError:
                continue  # root commit
            stat = git("diff", "--numstat", sha + "^1", sha, "--", "rules", "hooks")
            score = sum(int(n) for row in stat.splitlines() for n in row.split("\t")[:2] if n != "-")
            commits.append({"sha": sha, "committed_at": iso(at), "lines": score})
    except (OSError, ValueError):
        return None, "commit diff unavailable"
    return choose_split(commits, close, provisional=provisional), None


def outcomes(rows, close, prompt_counts, item_cost_results, run_rows, calibration, split_commit):
    """Pure confirmatory calculation; caller alone decides whether to permit an outcome look."""
    records = window_records(session_records(rows), close)
    sessions = analysis_sessions(records, prompt_counts)
    analysed = [s for s in sessions if s["status"] == "analysed"]
    arms = {}
    for mode in ARMS:
        mine = [s for s in sessions if s["mode"] == mode]
        live = [s for s in mine if s["status"] == "analysed"]
        items = [item for s in live for item in s["items"]]
        usd = math.fsum(s["usd"] for s in live)
        finished = sum(s["finished"] for s in live)
        unclear = sum(s["finished_or_unclear"] for s in live)
        weighted = sum(s["size_weighted"] for s in live)
        arms[mode] = {"window": len(mine), "analysed": len(live), "ghosts": sum(s["status"] == "ghost" for s in mine),
                      "rerolls": sum(bool(s["record"].get("end", {}).get("reroll")) for s in mine),
                      "dirty": sum(s["record"]["start"].get("dirty") is True for s in mine),
                      "excluded": {reason: sum(s["status"] == reason for s in mine) for reason in EXCLUSIONS},
                      "usd": usd, "finished": finished, "items": len(items),
                      "finished_or_unclear": unclear, "size_weighted": weighted,
                      "usd_per_finished": ratio(usd, finished), "finished_share": ratio(finished, len(items)),
                      "usd_per_finished_or_unclear": ratio(usd, unclear), "usd_per_size_weighted": ratio(usd, weighted),
                      "by_type": dict(sorted(Counter(item["type"] for item in items).items())),
                      "by_size": dict(sorted(Counter(item["size"] for item in items).items())),
                      "continuation_share": ratio(sum(bool(item.get("continuation")) for item in items), len(items))}
    no_cont, no_cont_excluded = no_continuations(analysed, item_cost_results)
    short, duration_counts = under_8h(analysed)
    sensitivity = {"unclear_as_finished": estimate(analysed, "finished_or_unclear"),
                   "size_weighted": estimate(analysed, "size_weighted"),
                   "project_days": estimate(analysed, project_days=True),
                   "no_continuations": {**estimate(no_cont), "excluded": no_cont_excluded},
                   "under_8h": {**estimate(short), "counts": duration_counts}, "split": None}
    if split_commit is not None:
        at = instant(split_commit["committed_at"])
        sensitivity["split"] = {"before": estimate([s for s in analysed if s["ts"] < at]),
                                "after": estimate([s for s in analysed if s["ts"] >= at])}
    projects = []
    for project in sorted({s["project"] for s in sessions}):
        mine = [s for s in analysed if s["project"] == project]
        defined = all(any(s["mode"] == mode for s in mine) for mode in ("orchestrator", "claude-only"))
        # Project estimates are descriptive ratios; the prereg specifies intervals for the pooled analyses.
        totals = session_tuples(mine)
        observed = hypotheses([(math.fsum(s[0] for s in arm), sum(s[1] for s in arm)) for arm in totals])[0]
        projects.append({"project": hashlib.sha256(project.encode()).hexdigest()[:8],
                         "defined": defined, "h1": observed if defined else None})
    # Reuse by_mode's provenance join with all starts/delegates, so filtering cannot steal legacy runs.
    join_rows = [row for row in rows if row.get("event") in ("start", "delegate", "end")]
    joined = cost.by_mode(join_rows, run_rows)
    orchestrator = {s["session"] for s in analysed if s["mode"] == "orchestrator"}
    tokens = {agent: {key: ratio(sum(s["delegate_tokens"].get(agent, {}).get(key, 0)
                                   for s in joined["sessions"] if s["session"] in orchestrator), len(orchestrator))
                      for key in cost.DELEGATE_TOKENS} for agent in ("codex", "cursor")}
    pool = dict(calibration)
    if pool.get("available"):
        pool["per_finished"] = {m: (ratio(arms[m]["usd"], arms[m]["finished"]) * pool["per_usd"]
                                     if arms[m]["usd_per_finished"] is not None else None) for m in ARMS}
    return {"arms": arms, "primary": estimate(analysed), "sensitivity": sensitivity,
            "allocation": allocation(records), "pool_points": pool,
            "delegate_tokens_per_orchestrator_session": tokens, "projects": projects}


def json_safe(value):
    if isinstance(value, float) and not math.isfinite(value):
        return "inf" if value > 0 else None
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


def render(result, *, as_json=False):
    if as_json:
        return json.dumps(json_safe(result), indent=2, allow_nan=False)
    lines = ["preregistered control-session experiment", "T0: " + result["t0"],
             f"progress: {result['progress']['rated_claude_only']}/{TARGET} rated claude-only",
             f"close: {result['close']} ({result['close_by']}{', provisional' if result['close_provisional'] else ''})",
             f"freeze: {result['freeze']} ({'reached' if result['freeze_reached'] else 'not reached'})",
             "experiment's log" if result["experiment_log"] else "not the experiment's log"]
    split = result["split_commit"]
    lines.append((f"split commit: {split['sha'][:8]} {split['committed_at']} {split['lines']} lines"
                  + (" (provisional)" if split["provisional"] else "")) if split else
                 "split commit: " + (result.get("split_unavailable") or "none"))
    lines += cost._table_lines([("MODE", "DRAWN", "GHOST", "RATED")] + [
        (mode, *(str(result["progress"]["arms"][mode][key]) for key in ("drawn", "ghosts", "rated"))) for mode in ARMS])
    out = result["outcomes"]
    if out is None:
        return "\n".join(lines)
    def fmt(value):
        return "undefined" if value is None else "inf" if value == math.inf else f"{value:.6g}"
    lines += ["", "Outcomes"] + cost._table_lines([("MODE", "WINDOW", "ANALYSED", "USD", "FINISHED", "ITEMS", "USD/F")] + [
        (m, str(a["window"]), str(a["analysed"]), fmt(a["usd"]), str(a["finished"]), str(a["items"]), fmt(a["usd_per_finished"]))
        for m, a in out["arms"].items()])
    table = [("ANALYSIS", "H", "R", "LOW", "HIGH", "INF SHARE", "0/0", "READING")]
    def add_estimate(label, estimate):
        for h in ("h1", "h2"):
            a = estimate[h]
            table.append((label, h.upper(), fmt(a["observed"]), fmt(a["lo"]), fmt(a["hi"]),
                          fmt(a["inf_share"]), str(a["zero_over_zero"]), a["reading"]))
    add_estimate("primary", out["primary"])
    for name, analysis in out["sensitivity"].items():
        if name == "split":
            if analysis:
                for half, estimate_ in analysis.items():
                    add_estimate("split " + half, estimate_)
            continue
        add_estimate(name, analysis)
    lines += ["", "Ratios and 95% intervals"] + cost._table_lines(table)
    lines += ["", "Exclusions and assignment"] + cost._table_lines(
        [("MODE", "REROLL", "DIRTY", *EXCLUSIONS)] + [
            (m, str(a["rerolls"]), str(a["dirty"]),
             *(str(a["excluded"][reason]) for reason in EXCLUSIONS)) for m, a in out["arms"].items()])
    lines += ["", "Secondary measures"] + cost._table_lines(
        [("MODE", "FINISHED/ITEMS", "F+UNCLEAR", "SIZE F", "USD/(F+U)", "USD/SIZE F", "CONT/ITEMS")] + [
            (m, fmt(a["finished_share"]), fmt(a["finished_or_unclear"]), fmt(a["size_weighted"]),
             fmt(a["usd_per_finished_or_unclear"]), fmt(a["usd_per_size_weighted"]), fmt(a["continuation_share"]))
            for m, a in out["arms"].items()])
    allocation_ = out["allocation"]
    lines += ["", "Allocation (test uses sessions with recorded weights)"] + cost._table_lines(
        [("MODE", "DRAWN", "WITH WEIGHTS", "EXPECTED", "NO WEIGHTS")] + [
            (m, str(allocation_["drawn"][m]), str(allocation_["observed"][m]),
             fmt(allocation_["expected"][m]), str(allocation_["without_weights"][m])) for m in ARMS])
    lines.append(f"chi-squared: {fmt(allocation_['chi_squared'])}; p (df=2): {fmt(allocation_['p_df2'])}")
    pool = out["pool_points"]
    lines += ["", "Claude 7d pool points", "calibration: " + pool.get("path", "unavailable"),
              "SHA-256: " + (pool.get("sha256") or "unavailable"),
              "saved_at: " + (pool.get("saved_at") or "unknown") + "; since: " + (pool.get("since") or "unknown")]
    if pool.get("available"):
        lines.append("points per USD: " + fmt(pool["per_usd"]))
        lines += cost._table_lines([("MODE", "POINTS/FINISHED")] + [
            (m, fmt(pool["per_finished"][m])) for m in ARMS])
    else:
        lines.append("unavailable: " + pool.get("reason", "no usable fit"))
    lines += ["", "Mean delegate tokens per analysed orchestrator session"] + cost._table_lines(
        [("AGENT", *cost.DELEGATE_TOKENS)] + [(agent, *(fmt(tokens[key]) for key in cost.DELEGATE_TOKENS))
            for agent, tokens in out["delegate_tokens_per_orchestrator_session"].items()])
    lines += ["", "Descriptive items"] + cost._table_lines([("MODE", "GROUP", "VALUE", "ITEMS")] + [
        (mode, key, group, str(count)) for mode, arm in out["arms"].items()
        for key in ("by_type", "by_size") for group, count in arm[key].items()])
    lines += ["", "Projects"] + cost._table_lines([("PROJECT", "H1", "DEFINED")] + [
        (project["project"], fmt(project["h1"]), str(project["defined"])) for project in out["projects"]])
    lines += ["", "Sensitivity exclusions"] + cost._table_lines(
        [("MODE", "ITEM COSTS FAILED", "CONT UNPRICED", "OVER 8H", "NO CALL TIMES")] + [
            (mode, str(out["sensitivity"]["no_continuations"]["excluded"][mode].get("item_costs_failed", 0)),
             str(out["sensitivity"]["no_continuations"]["excluded"][mode].get("continuation_unpriced", 0)),
             str(out["sensitivity"]["under_8h"]["counts"][mode]["over_8h"]),
             str(out["sensitivity"]["under_8h"]["counts"][mode]["no_call_times"])) for mode in ARMS])
    return "\n".join(lines)


def cmd_prereg(args):
    log = runs.session_log_path(args.session_log)
    experiment_log = log.resolve() == runs.session_log_path().resolve()
    rows = cost.load_jsonl(log)
    now = _now()
    result = progress(rows, now, experiment_log=experiment_log)
    split, unavailable = read_split_commit(instant(result["close"]), repo_dir=args.repo_dir,
                                          provisional=result["close_provisional"])
    result.update(split_commit=split, split_unavailable=unavailable)
    if args.outcomes and experiment_log and not result["freeze_reached"]:
        print(render(result, as_json=args.json))
        if result["close_provisional"]:
            sys.exit("freeze not yet known: the close is provisional at " + result["close"])
        sys.exit("outcomes held until freeze " + result["freeze"])
    if args.outcomes:
        import calibrate
        records = window_records(session_records(rows), instant(result["close"]))
        counts, costs = read_transcript_inputs(records, cost.load_catalog_cost())
        calibration = read_calibration(calibrate.saved_path(args.calibration, args.quota_log))
        result["outcomes"] = outcomes(rows, instant(result["close"]), counts, costs,
                                      cost.load_jsonl(args.log or cost.DEFAULT_LOG), calibration, split)
        if experiment_log and not any(row.get("event") == "rating" and instant(row.get("ts")) is not None
                                      and instant(row["ts"]) >= instant(result["freeze"]) for row in rows):
            print("warning: no rating row at or after the freeze; run the last default delegate rate", file=sys.stderr)
    print(render(result, as_json=args.json))


def _add_args(parser):
    parser.add_argument("--outcomes", action="store_true")
    parser.add_argument("--json", action="store_true")
    for name in ("session-log", "log", "repo-dir", "calibration", "quota-log"):
        parser.add_argument("--" + name)
    parser.set_defaults(fn=cmd_prereg)


def register(subparsers):
    parser = subparsers.add_parser("prereg", help="frozen confirmatory analysis (counts only before freeze)")
    _add_args(parser)
    return parser


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    _add_args(parser)
    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    main()

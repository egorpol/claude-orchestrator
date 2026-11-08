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
CALIBRATION_SHA256 = "af55a932fe98043d5f263651597cdb49eb10716ff051b1fc02a6cf193a9020bb"
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
    """Read typed-prompt counts, /next flags and pinned item_costs; errors omit private paths."""
    import rate
    counts, costs, first_prompts = {}, {}, {}
    for record in records:
        if preliminary_status(record) is not None:
            continue
        session, path = record["session"], transcript_path(record)
        try:
            if path is None:
                raise FileNotFoundError()
            prompts = rate.typed_prompts(rate.json_rows(path))
            counts[session] = len(prompts)
            first_prompts[session] = (None if not prompts
                                      else prompts[0]["text"] == rate.NEXT_PROMPT)
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
    return counts, costs, first_prompts


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


def no_handoffs(sessions, item_cost_results):
    """Drop continuation and partial USD once; finished matches no_continuations."""
    import rate
    kept = []
    excluded = {mode: Counter() for mode in ARMS}
    for session in sessions:
        costs = item_cost_results.get(session["session"])
        if costs is None:
            excluded[session["mode"]]["item_costs_failed"] += 1
            continue
        items = session["record"]["rating"]["items"]
        cont_unpriced = partial_unpriced = False
        dropped = []
        for item, spent in zip(items, costs):
            if item.get("type") == rate.FIELD_REPORT:
                continue
            continuation = bool(item.get("continuation"))
            partial = item.get("status") == "partial"
            if not continuation and not partial:
                continue
            usd = spent["usd"]
            if usd is None:
                if continuation:
                    cont_unpriced = True
                else:
                    partial_unpriced = True
            else:
                dropped.append(usd)
        if cont_unpriced:
            excluded[session["mode"]]["continuation_unpriced"] += 1
            continue
        if partial_unpriced:
            excluded[session["mode"]]["partial_unpriced"] += 1
            continue
        kept.append({**session, "usd": session["usd"] - math.fsum(dropped),
                     "finished": sum(item.get("status") == "finished" for item in session["items"]
                                     if not item.get("continuation"))})
    return kept, excluded


CROSSOVER_GAP = timedelta(minutes=60)


def crossover_links(records, window, close):
    """Link forced startups after T0 to window donors; without-donor is window-only.

    A forced session may start after the close; only its donor must be in the window.
    """
    all_records = list(records.values()) if isinstance(records, dict) else list(records)
    by_session = {r["session"]: r for r in all_records}
    forced, donors = [], []
    for record in all_records:
        start = record.get("start") or {}
        ts = instant(start.get("ts"))
        if ts is None or not (ts > window):
            continue
        if start.get("forced"):
            forced.append(record)
            continue
        if (window < ts < close and cost.is_drawn(start)
                and start.get("mode") in ("claude-only", "vanilla") and start.get("repo_dir")):
            donors.append(record)
    forced.sort(key=lambda r: (instant(r["start"]["ts"]), r["session"]))
    linked_donors = set()
    links = {}  # donor session -> forced session
    without = {"startup": 0, "not_startup": 0, "total": 0}
    for record in forced:
        start = record["start"]
        forced_ts = instant(start["ts"])
        in_window = window < forced_ts < close

        def count_without(kind):
            if in_window:
                without[kind] += 1
                without["total"] += 1

        if start.get("source") != "startup":
            count_without("not_startup")
            continue
        repo = start.get("repo_dir")
        if not repo:
            count_without("startup")
            continue
        candidates = []
        for donor in donors:
            if donor["session"] in linked_donors:
                continue
            dstart = donor["start"]
            if dstart.get("repo_dir") != repo:
                continue
            donor_ts = instant(dstart["ts"])
            if donor_ts is None or not donor_ts < forced_ts:
                continue
            summary = donor.get("summary") or {}
            last = instant(summary.get("last_call"))
            if last is None or forced_ts > last + CROSSOVER_GAP:
                continue
            candidates.append(donor)
        if not candidates:
            count_without("startup")
            continue
        chosen = max(candidates, key=lambda d: (instant(d["start"]["ts"]), d["session"]))
        links[chosen["session"]] = record["session"]
        linked_donors.add(chosen["session"])
    return links, without, by_session


def crossovers_credited(analysed, links, forced_status, donor_modes):
    """Add each analysable linked forced session's USD and finished to its donor.

    forced_status maps forced session id -> analysis_sessions entry.
    donor_modes maps donor session id -> arm name.
    """
    analysed_ids = {s["session"]: dict(s) for s in analysed}
    arms = {mode: {"donors": 0, "donors_analysed": 0, "forced_unanalysable": 0} for mode in ARMS}
    for donor_id, forced_id in links.items():
        donor_mode = donor_modes.get(donor_id)
        if donor_mode not in arms:
            continue
        arms[donor_mode]["donors"] += 1
        if donor_id in analysed_ids:
            arms[donor_mode]["donors_analysed"] += 1
        forced = forced_status.get(forced_id)
        if forced is None or forced.get("status") != "analysed":
            arms[donor_mode]["forced_unanalysable"] += 1
            continue
        if donor_id not in analysed_ids:
            continue
        credited = analysed_ids[donor_id]
        credited["usd"] = credited["usd"] + forced["usd"]
        credited["finished"] = credited["finished"] + forced["finished"]
    return list(analysed_ids.values()), arms


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
    if out["sha256"] != CALIBRATION_SHA256:
        return {**out, "reason": "SHA-256 must equal " + CALIBRATION_SHA256}
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


def outcomes(rows, close, prompt_counts, item_cost_results, run_rows, calibration, split_commit,
             *, first_prompts=None, guesses=None):
    """Pure confirmatory calculation; caller alone decides whether to permit an outcome look."""
    import blinding
    first_prompts = first_prompts or {}
    guesses = guesses or {}
    all_records = session_records(rows)
    records = window_records(all_records, close)
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
    no_hand, no_hand_excluded = no_handoffs(analysed, item_cost_results)
    short, duration_counts = under_8h(analysed)
    links, without_donor, _ = crossover_links(all_records, T0, close)
    donor_modes = {donor: all_records[donor]["start"]["mode"] for donor in links}
    forced_status = {}
    for forced_id in links.values():
        forced_record = all_records.get(forced_id)
        if forced_record is None:
            continue
        forced_status[forced_id] = analysis_sessions([forced_record], prompt_counts)[0]
    credited, cross_arms = crossovers_credited(analysed, links, forced_status, donor_modes)
    sensitivity = {"unclear_as_finished": estimate(analysed, "finished_or_unclear"),
                   "size_weighted": estimate(analysed, "size_weighted"),
                   "project_days": estimate(analysed, project_days=True),
                   "no_continuations": {**estimate(no_cont), "excluded": no_cont_excluded},
                   "no_handoffs": {**estimate(no_hand), "excluded": no_hand_excluded},
                   "crossovers_credited": estimate(credited),
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
    rating_packet = {}
    for row in rows:
        if row.get("event") == "rating" and isinstance(row.get("session"), str):
            rating_packet[row["session"]] = row.get("packet_sha256")
    pairs, no_guess, packet_changed = [], {mode: 0 for mode in ARMS}, {mode: 0 for mode in ARMS}
    for session in analysed:
        row = guesses.get(session["session"])
        if row is None:
            no_guess[session["mode"]] += 1
            continue
        guessed = blinding.LETTERS.get(row.get("guess"))
        if guessed is None:
            no_guess[session["mode"]] += 1
            continue
        pairs.append((session["mode"], guessed))
        if row.get("packet_sha256") != rating_packet.get(session["session"]):
            packet_changed[session["mode"]] += 1
    next_share = {}
    for mode in ARMS:
        live = [s for s in analysed if s["mode"] == mode]
        with_prompts = [s for s in live if first_prompts.get(s["session"]) is not None]
        bare_next = sum(first_prompts.get(s["session"]) is True for s in with_prompts)
        next_share[mode] = {"with_prompts": len(with_prompts), "next": bare_next,
                            "share": ratio(bare_next, len(with_prompts))}
    return {"arms": arms, "primary": estimate(analysed), "sensitivity": sensitivity,
            "allocation": allocation(records), "pool_points": pool,
            "delegate_tokens_per_orchestrator_session": tokens, "projects": projects,
            "crossovers": {"arms": cross_arms, "forced_without_donor": without_donor},
            "blinding": {"arm_guess": {**blinding.summarise(pairs), "no_guess": no_guess,
                                      "packet_changed": packet_changed},
                         "next_share": next_share}}


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
    cross = out["crossovers"]
    lines += ["", "Crossovers"] + cost._table_lines(
        [("MODE", "DONORS", "DONORS ANALYSED", "FORCED UNANALYSABLE")] + [
            (mode, str(cross["arms"][mode]["donors"]), str(cross["arms"][mode]["donors_analysed"]),
             str(cross["arms"][mode]["forced_unanalysable"])) for mode in ARMS])
    without = cross["forced_without_donor"]
    lines.append(f"forced without donor: {without['total']} "
                 f"(startup {without['startup']}, not startup {without['not_startup']})")
    guess = out["blinding"]["arm_guess"]
    guess_header = ("TRUE\\GUESS",) + ARMS
    guess_rows = [guess_header] + [
        (arm, *(str(guess["table"][arm][g]) for g in ARMS)) for arm in ARMS]
    lines += ["", "Blinding: arm guess"] + cost._table_lines(guess_rows)
    lines.append(f"n={guess['n']}; accuracy={fmt(guess['accuracy'])}; "
                 f"majority={fmt(guess['majority_accuracy'])}; kappa={fmt(guess['kappa'])}")
    lines += cost._table_lines([("MODE", "NO GUESS", "PACKET CHANGED")] + [
        (mode, str(guess["no_guess"][mode]), str(guess["packet_changed"][mode])) for mode in ARMS])
    nxt = out["blinding"]["next_share"]
    lines += ["", "Blinding: first prompt /next"] + cost._table_lines(
        [("MODE", "WITH PROMPTS", "NEXT", "SHARE")] + [
            (mode, str(nxt[mode]["with_prompts"]), str(nxt[mode]["next"]), fmt(nxt[mode]["share"]))
            for mode in ARMS])
    lines += ["", "Sensitivity exclusions"] + cost._table_lines(
        [("MODE", "ITEM COSTS FAILED", "CONT UNPRICED", "NH ITEM COSTS", "NH CONT UNPRICED",
          "NH PARTIAL UNPRICED", "OVER 8H", "NO CALL TIMES")] + [
            (mode,
             str(out["sensitivity"]["no_continuations"]["excluded"][mode].get("item_costs_failed", 0)),
             str(out["sensitivity"]["no_continuations"]["excluded"][mode].get("continuation_unpriced", 0)),
             str(out["sensitivity"]["no_handoffs"]["excluded"][mode].get("item_costs_failed", 0)),
             str(out["sensitivity"]["no_handoffs"]["excluded"][mode].get("continuation_unpriced", 0)),
             str(out["sensitivity"]["no_handoffs"]["excluded"][mode].get("partial_unpriced", 0)),
             str(out["sensitivity"]["under_8h"]["counts"][mode]["over_8h"]),
             str(out["sensitivity"]["under_8h"]["counts"][mode]["no_call_times"])) for mode in ARMS])
    return "\n".join(lines)


def default_guess_log(session_log):
    return Path(session_log).resolve().parent / "arm-guesses.jsonl"


def _prepare_needs_rating(record, prompt_counts):
    """True when a linked forced session fails analysis only for rating currency."""
    status = preliminary_status(record)
    if status in ("pending", "ghost", "unpriced"):
        return False
    if status == "unrated":
        # A gone transcript (probe count None) would still fail as transcript_gone.
        return prompt_counts.get(record["session"]) is not None
    if status is not None:
        return False
    count = prompt_counts.get(record["session"])
    if count is None:
        return False  # transcript_gone: not a rating-only failure
    return count != record["rating"]["prompts"]


def cmd_prepare(args, log, rows, result, experiment_log):
    import ask
    import blinding
    import rate
    close = instant(result["close"])
    all_records = session_records(rows)
    window = window_records(all_records, close)
    links, _, _ = crossover_links(all_records, T0, close)
    catalog_cost = cost.load_catalog_cost()
    forced_records = [all_records[fid] for fid in links.values() if fid in all_records]
    counts, _, _ = read_transcript_inputs([*window, *forced_records], catalog_cost)
    # Unrated forced sessions are skipped by read_transcript_inputs; probe their transcripts too.
    for record in forced_records:
        session = record["session"]
        if session in counts or preliminary_status(record) not in (None, "unrated"):
            continue
        path = transcript_path(record) if record.get("summary") is not None else None
        try:
            if path is None:
                raise FileNotFoundError()
            counts[session] = len(rate.typed_prompts(rate.json_rows(path)))
        except (OSError, ValueError, TypeError):
            counts[session] = None

    rated = rating_failed = rating_present = 0
    failed = False
    for donor_id, forced_id in sorted(links.items(), key=lambda pair: pair[1]):
        record = all_records.get(forced_id)
        if record is None or not _prepare_needs_rating(record, counts):
            if record is not None and preliminary_status(record) is None:
                count = counts.get(forced_id)
                if count is not None and count == record.get("rating", {}).get("prompts"):
                    rating_present += 1
            continue
        try:
            selected = rate.select_sessions(rows, [forced_id])
            if not selected:
                raise ValueError("session not selectable")
            sel = selected[0]
            if "error" in sel:
                raise ValueError(sel["error"])
            packet = rate.build_packet(sel)
            n = len(sel["prompts"])
            items = (rate.rate_packet(packet, n, "cursor", "auto", 600,
                                      rate.field_report_prompts(sel["prompts"])) if n else [])
            rate.append_rating(log, rate.rating_row(sel, packet, items, "cursor", "auto"))
            rated += 1
            rows = cost.load_jsonl(log)
            all_records = session_records(rows)
        except (ValueError, OSError, ask.AskFailed) as exc:
            print(f"prereg: {forced_id[:8]}: {type(exc).__name__}", file=sys.stderr)
            rating_failed += 1
            failed = True

    rows = cost.load_jsonl(log)
    all_records = session_records(rows)
    window = window_records(all_records, close)
    forced_records = [all_records[fid] for fid in links.values() if fid in all_records]
    counts, _, _ = read_transcript_inputs([*window, *forced_records], catalog_cost)
    sessions = analysis_sessions(window, counts)
    analysed = sorted((s for s in sessions if s["status"] == "analysed"),
                      key=lambda s: (s["ts"], s["session"]))
    guess_path = Path(args.guess_log) if args.guess_log else default_guess_log(log)
    existing = blinding.read_guesses(guess_path)
    guessed = guess_failed = guess_present = 0
    for session in analysed:
        if session["session"] in existing:
            guess_present += 1
            continue
        try:
            selected = rate.select_sessions(rows, [session["session"]])
            if not selected:
                raise ValueError("session not selectable")
            sel = selected[0]
            if "error" in sel:
                raise ValueError(sel["error"])
            packet = rate.build_packet(sel)
            letter = blinding.guess_packet(packet, "auto", 600)
            rate.append_rating(guess_path, blinding.guess_row(session["session"], letter, packet, "auto"))
            guessed += 1
            existing[session["session"]] = True
        except (ValueError, OSError, ask.AskFailed) as exc:
            print(f"prereg: {session['session'][:8]}: {type(exc).__name__}", file=sys.stderr)
            guess_failed += 1
            failed = True
    print(f"prepare: rated {rated}, rating_failed {rating_failed}, rating_present {rating_present}, "
          f"guessed {guessed}, guess_failed {guess_failed}, guess_present {guess_present}")
    if failed:
        sys.exit(1)


def cmd_prereg(args):
    log = runs.session_log_path(args.session_log)
    experiment_log = log.resolve() == runs.session_log_path().resolve()
    rows = cost.load_jsonl(log)
    now = _now()
    result = progress(rows, now, experiment_log=experiment_log)
    split, unavailable = read_split_commit(instant(result["close"]), repo_dir=args.repo_dir,
                                          provisional=result["close_provisional"])
    result.update(split_commit=split, split_unavailable=unavailable)
    gated = (args.outcomes or args.prepare) and experiment_log and not result["freeze_reached"]
    if gated:
        print(render(result, as_json=args.json))
        if result["close_provisional"]:
            sys.exit("freeze not yet known: the close is provisional at " + result["close"])
        sys.exit(("outcomes" if args.outcomes else "prepare") + " held until freeze " + result["freeze"])
    if args.prepare:
        cmd_prepare(args, log, rows, result, experiment_log)
        return
    if args.outcomes:
        import calibrate
        import blinding
        close = instant(result["close"])
        all_records = session_records(rows)
        window = window_records(all_records, close)
        links, _, _ = crossover_links(all_records, T0, close)
        forced_records = [all_records[fid] for fid in links.values() if fid in all_records]
        counts, costs, first_prompts = read_transcript_inputs(
            [*window, *forced_records], cost.load_catalog_cost())
        calibration = read_calibration(calibrate.saved_path(args.calibration, args.quota_log))
        guess_path = Path(args.guess_log) if args.guess_log else default_guess_log(log)
        guesses = blinding.read_guesses(guess_path)
        result["outcomes"] = outcomes(rows, close, counts, costs,
                                      cost.load_jsonl(args.log or cost.DEFAULT_LOG), calibration, split,
                                      first_prompts=first_prompts, guesses=guesses)
        if experiment_log and not any(row.get("event") == "rating" and instant(row.get("ts")) is not None
                                      and instant(row["ts"]) >= instant(result["freeze"]) for row in rows):
            print("warning: no rating row at or after the freeze; run the last default delegate rate", file=sys.stderr)
    print(render(result, as_json=args.json))


def _add_args(parser):
    exclusive = parser.add_mutually_exclusive_group()
    exclusive.add_argument("--outcomes", action="store_true")
    exclusive.add_argument("--prepare", action="store_true")
    parser.add_argument("--guess-log", metavar="PATH")
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

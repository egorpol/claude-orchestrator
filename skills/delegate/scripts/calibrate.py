"""`delegate cost --calibrate`: pool points per token class, from pairs of quota readings.

A pair is two consecutive good readings of one pool in the same reset window
(`resets_at` rounded to the minute: Claude's value moves between polls), or an
idle reading (no window, 0%) followed by the first reading of a window opened
after it. Rows with `error` or `stale` break a pair. The rise in `used_pct` over
a pair is regressed on the tokens of every call made in it (all Claude Code
transcripts, subagents and scratch projects included; Codex pools on
`~/.codex` rollouts) with non-negative least squares. Claude pools also get a
one-column fit on list-price USD, the stable headline; classes with too little
volume to fit are carried at list price times that fit.
"""
import bisect
import json
import math
import os
import random
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cost
import quota

POOLS = {"claude-5h": "claude", "claude-7d": "claude", "codex-5h": "codex", "codex-7d": "codex"}
REFERENCE = {"codex-7d": "codex-5h"}  # no list prices: a 7d pool is fitted on its 5h pool's weights
CLAUDE_CLASSES = ("input", "cache_read", "cache_write_5m", "cache_write_1h", "output")
CODEX_CLASSES = ("uncached_input", "cached_input", "output")
MIN_USD_SHARE = 0.01   # a priced column below this share of the pool's USD is not fitted
MIN_PAIRS = 10         # nor one with tokens in fewer pairs
CORR_FLAG = 0.8
SUM_RATIO_FLAG = 0.15  # relative gap between the sum ratio and the per-pair fit worth a warning
BOOTSTRAP = 200
LAGS_MIN = (-1, 0, 1)


def _row_time(row):
    ts = quota._safe_ts(row.get("ts")) if isinstance(row.get("ts"), str) else None
    return ts or datetime.min.replace(tzinfo=timezone.utc)


def _round_min(dt):
    return (dt + timedelta(seconds=30)).replace(second=0, microsecond=0)


def snapshot_pairs(rows, pool):
    """Pairs of good readings of `pool` as {t0, t1, rise} dicts, plus counts of dropped readings."""
    window_h = quota.POOLS.get(pool, {}).get("window_h")
    pairs, prev = [], None
    stats = defaultdict(int)
    for row in sorted(rows, key=_row_time):
        entry = (row.get("pools") or {}).get(pool) if isinstance(row.get("pools"), dict) else None
        if "error" in row or not isinstance(entry, dict) or "error" in entry:
            stats["error"] += 1
            prev = None
            continue
        if entry.get("stale") or entry.get("stale_reset"):
            stats["stale"] += 1
            prev = None
            continue
        observed = quota._safe_ts(entry.get("observed_at"))
        reset = quota._safe_ts(entry.get("resets_at"))
        used = entry.get("used_pct")
        idle = reset is None and used == 0  # no window open: nothing used since the last reset
        if observed is None or not isinstance(used, (int, float)) or (reset is None and not idle):
            stats["error"] += 1
            prev = None
            continue
        cur = {"t": observed, "window": _round_min(reset) if reset else None, "used": float(used)}
        if prev and cur["t"] <= prev["t"]:  # the same provider reading polled twice
            continue
        same = prev is not None and prev["window"] == cur["window"]
        # From idle into a window that opened after the idle reading: all of its use is in the pair.
        opened = (prev is not None and prev["window"] is None and cur["window"] is not None and window_h
                  and cur["window"] - timedelta(hours=window_h) >= prev["t"] - timedelta(minutes=1))
        if same or opened:
            if cur["used"] < prev["used"]:
                stats["negative"] += 1
            else:
                pairs.append({"t0": prev["t"], "t1": cur["t"], "rise": cur["used"] - prev["used"]})
        prev = cur
    return pairs, dict(stats)


def _jsonl_files(roots):
    """`*.jsonl` under roots. No mtime cutoff: a synced or restored file can be older than its calls."""
    for root in roots:
        if root.is_dir():
            yield from (path for path in sorted(root.rglob("*.jsonl")) if path.is_file())


def _class_usd(model, tokens, prices):
    """List-price USD per class of one call, or None for a model without a price."""
    rates = cost.model_price(model, prices)
    if not rates:
        return None
    prompt = sum(tokens[k] for k in ("input", "cache_read", "cache_write_5m", "cache_write_1h"))
    if rates.get("above_prompt_tokens") is not None and prompt > rates["above_prompt_tokens"]:
        rates = rates["above"]
    return {cls: n * rates[cls] / 1e6 for cls, n in tokens.items()}


def claude_events(projects, prices=None):
    """(ts, model, {class: tokens}, usd or None, {class: usd} or None) per deduplicated call."""
    seen, events = set(), []
    for path in _jsonl_files([projects]):
        try:
            calls = cost.parse_assistant_usage(path, seen=seen)
        except OSError:
            continue
        for call in calls:
            ts = cost._row_ts(call.get("timestamp"))
            if ts is None:
                continue
            model = call.get("model") or "?"
            tokens = {k: call[k] for k in CLAUDE_CLASSES}
            tokens["cache_write_5m"] += call["cache_write_unsplit"]  # priced as 5m too
            usd, _ = cost.price_call(call, prices)
            events.append((ts, model, tokens, usd, _class_usd(model, tokens, prices) if usd is not None else None))
    events.sort(key=lambda e: e[0])
    return events


def codex_events(codex_home):
    """(ts, model, {class: tokens}, None, None) per Codex call, from rollout `token_count` events.

    Deltas of `total_token_usage` per file drop the repeated events Codex writes.
    A file's first event, and the first after a counter restart, take
    `last_token_usage`: a resumed thread's file starts at the thread's running
    total. `input_tokens` includes the cached part, `output_tokens` the
    reasoning. Of two copies of a rollout (sessions/, archived_sessions/), the
    larger is read.
    """
    keys = ("input_tokens", "cached_input_tokens", "output_tokens")
    copies = {}
    for path in _jsonl_files([codex_home / "sessions", codex_home / "archived_sessions"]):
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if path.name not in copies or size > copies[path.name][0]:
            copies[path.name] = (size, path)
    events = []
    for _, path in sorted(copies.values()):
        prev, model = None, None
        try:
            stream = path.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with stream:
            for line in stream:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
                if obj.get("type") == "turn_context":
                    model = payload.get("model") or model
                    continue
                info = payload.get("info") if payload.get("type") == "token_count" else None
                if obj.get("type") != "event_msg" or not isinstance(info, dict):
                    continue
                total, last = info.get("total_token_usage"), info.get("last_token_usage")
                if not isinstance(total, dict) or not all(isinstance(total.get(k), int) for k in keys):
                    continue  # no usable running total: keep the watermark
                cur = tuple(total[k] for k in keys)
                delta = None if prev is None else tuple(a - b for a, b in zip(cur, prev))
                if delta is None or min(delta) < 0:
                    delta = tuple(int(last.get(k) or 0) for k in keys) if isinstance(last, dict) else cur
                prev = cur
                ts = cost._row_ts(obj.get("timestamp"))
                if ts is None or not any(delta):
                    continue
                events.append((ts, model or "?", {"uncached_input": max(0, delta[0] - delta[1]),
                                                   "cached_input": delta[1], "output": delta[2]}, None, None))
    events.sort(key=lambda e: e[0])
    return events


def nnls(X, y):
    """Lawson-Hanson non-negative least squares on the normal equations; few columns."""
    n = len(X[0]) if X else 0
    scale = [math.sqrt(sum(r[j] ** 2 for r in X)) or 1.0 for j in range(n)]
    G = [[sum(r[i] * r[j] for r in X) / (scale[i] * scale[j]) for j in range(n)] for i in range(n)]
    h = [sum(r[i] * v for r, v in zip(X, y)) / scale[i] for i in range(n)]

    def solve(cols):
        m = len(cols)
        M = [[G[a][b] for b in cols] + [h[a]] for a in cols]
        for i in range(m):
            p = max(range(i, m), key=lambda k: abs(M[k][i]))
            M[i], M[p] = M[p], M[i]
            if abs(M[i][i]) < 1e-12:
                return None
            for k in range(m):
                if k != i:
                    f = M[k][i] / M[i][i]
                    M[k] = [a - f * b for a, b in zip(M[k], M[i])]
        return {c: M[i][m] / M[i][i] for i, c in enumerate(cols)}

    x, passive, blocked = [0.0] * n, [], set()
    for _ in range(3 * n + 10):
        grad = [h[i] - sum(G[i][j] * x[j] for j in range(n)) for i in range(n)]
        free = [i for i in range(n) if i not in passive and i not in blocked and grad[i] > 1e-9]
        if not free:
            break
        new, before = max(free, key=grad.__getitem__), x[:]
        passive.append(new)
        while passive:
            z = solve(passive)
            if z is None:  # collinear with the passive set: leave it at zero
                blocked.add(passive.pop())
                break
            if all(v > 0 for v in z.values()):
                x = [z.get(i, 0.0) for i in range(n)]
                break
            alpha = min(x[i] / (x[i] - v) if x[i] - v > 0 else 0.0 for i, v in z.items() if v <= 0)
            x = [x[i] + alpha * (z[i] - x[i]) if i in z else 0.0 for i in range(n)]
            passive = [i for i in passive if x[i] > 1e-12]
        if new not in passive and x == before:  # dropped again without progress: don't cycle on it
            blocked.add(new)
    return [v / s for v, s in zip(x, scale)]


def _bucket(pairs, events, lag_min=0):
    """Events with t0 < ts + lag <= t1, per pair."""
    shift = timedelta(minutes=lag_min)
    times = [e[0] + shift for e in events]
    return [events[bisect.bisect_right(times, p["t0"]):bisect.bisect_right(times, p["t1"])] for p in pairs]


def _pct(values, q):
    values = sorted(values)
    return values[int(q * (len(values) - 1))]


def _fit(X, y, offset=None, seed=0):
    """NNLS of y - offset on X, with bootstrap ranges and residual statistics.

    Rounding: readings in whole percents turn a true rise with fractional part f
    into its floor or ceiling, a variance of f(1 - f); `rounding_rmse` is what
    that alone would give at the fitted rises.
    """
    offset = offset or [0.0] * len(y)
    target = [v - o for v, o in zip(y, offset)]
    w = nnls(X, target)
    rnd = random.Random(seed)
    draws = []
    for _ in range(BOOTSTRAP):
        idx = [rnd.randrange(len(X)) for _ in X]
        draws.append(nnls([X[i] for i in idx], [target[i] for i in idx]))
    boot = [{"p5": _pct([d[j] for d in draws], 0.05), "p95": _pct([d[j] for d in draws], 0.95),
             "zero_share": sum(d[j] == 0 for d in draws) / len(draws)} for j in range(len(w))]
    pred = [o + sum(a * b for a, b in zip(r, w)) for r, o in zip(X, offset)]
    active = [i for i, (r, o) in enumerate(zip(X, offset)) if any(r) or o]
    n = len(active) or 1
    return {"weights": w, "boot": boot, "pred": pred, "fitted_points": sum(pred),
            "rmse": math.sqrt(sum((y[i] - pred[i]) ** 2 for i in active) / n) if active else None,
            "rounding_rmse": math.sqrt(sum((pred[i] % 1) * (1 - pred[i] % 1) for i in active) / n)
            if active else None}


def _corr(a, b):
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    den = math.sqrt(sum((p - ma) ** 2 for p in a) * sum((q - mb) ** 2 for q in b))
    return sum((p - ma) * (q - mb) for p, q in zip(a, b)) / den if den else None


def _stats(fit):
    return {k: fit[k] for k in ("fitted_points", "rmse", "rounding_rmse")}


def fit_pool(pool, pairs, events, prices=None, seed=0, reference=None):
    """Fit one pool: a one-column headline, class weights, residuals and identifiability."""
    vendor = POOLS[pool]
    classes = CLAUDE_CLASSES if vendor == "claude" else CODEX_CLASSES
    buckets = _bucket(pairs, events)
    y = [p["rise"] for p in pairs]
    active = [i for i, b in enumerate(buckets) if b]
    idle_up = [y[i] for i, b in enumerate(buckets) if not b and y[i] > 0]
    out = {"pool": pool, "pairs": len(pairs), "active_pairs": len(active),
           "points": sum(y), "idle_rises": len(idle_up), "idle_points": sum(idle_up),
           "calls": sum(len(b) for b in buckets)}
    if not active:
        return out
    paired = [e for b in buckets for e in b]
    models = defaultdict(lambda: {"calls": 0, "usd": 0.0})
    for _, model, _, usd, _ in paired:
        models[model]["calls"] += 1
        models[model]["usd"] += usd or 0.0
    out["models"] = {m: dict(v) for m, v in sorted(models.items(), key=lambda kv: -kv[1]["calls"])}

    # Headline: one well-identified column. Claude: list-price USD. A Codex 7d
    # pool: the tokens weighted by its 5h pool's class fit, in 5h points.
    def headline_matrix(bks):
        if vendor == "claude":
            return [[sum(e[3] or 0.0 for e in b)] for b in bks]
        if reference:
            return [[sum(e[2][c] * reference["weights"].get(c, 0.0) for e in b for c in classes) / 1e6]
                    for b in bks]
        return None

    head_X = headline_matrix(buckets)
    w_usd = None
    if head_X is not None:
        head = _fit(head_X, y, seed=seed)
        total = sum(r[0] for r in head_X)
        # Points of pairs with calls over all units. Unlike the per-pair fit, it is
        # not diluted when rises straddle pairs; rises with no local calls stay out.
        out["headline"] = {"unit": "usd" if vendor == "claude" else reference["pool"],
                           "total": total, "per_unit": head["weights"][0],
                           "sum_ratio": sum(v for v, r in zip(y, head_X) if r[0]) / total if total else None,
                           "p5": head["boot"][0]["p5"], "p95": head["boot"][0]["p95"], **_stats(head)}
        if vendor == "claude":
            w_usd = head["weights"][0]
            out["unpriced_calls"] = sum(e[3] is None for e in paired)

    usd_by_col = defaultdict(float)
    for _, model, _, _, cusd in paired:
        for cls, v in (cusd or {}).items():
            usd_by_col[(model, cls)] += v
    total_usd = sum(usd_by_col.values())
    per_model = vendor == "claude"  # Codex has no list prices: one column per class over all models
    cols = defaultdict(lambda: {"tokens": 0, "pairs": 0})
    for b in buckets:
        hit = set()
        for _, model, tokens, _, _ in b:
            for cls in classes:
                if tokens[cls]:
                    key = (model if per_model else None, cls)
                    cols[key]["tokens"] += tokens[cls]
                    hit.add(key)
        for key in hit:
            cols[key]["pairs"] += 1
    keep, dropped = [], []
    for key, col in sorted(cols.items(), key=lambda kv: (kv[0][0] or "", classes.index(kv[0][1]))):
        share = usd_by_col[key] / total_usd if per_model and total_usd else None
        entry = {"model": key[0], "class": key[1], "tokens": col["tokens"], "pairs": col["pairs"], "usd_share": share}
        (dropped if col["pairs"] < MIN_PAIRS or (per_model and (share or 0) < MIN_USD_SHARE) else keep).append(entry)
    out["dropped"] = dropped
    drop_keys = {(d["model"], d["class"]) for d in dropped}

    def class_matrix(bks):
        X, offset = [], []
        for b in bks:
            row, off = [0.0] * len(keep), 0.0
            for _, model, tokens, _, cusd in b:
                for j, col in enumerate(keep):
                    if col["model"] in (None, model):
                        row[j] += tokens[col["class"]] / 1e6
                if w_usd and cusd:  # unfittable classes cost what list price says
                    off += w_usd * sum(v for cls, v in cusd.items() if (model, cls) in drop_keys)
            X.append(row)
            offset.append(off)
        return X, offset

    X, offset = class_matrix(buckets)
    out["offset_points"] = sum(offset)
    if keep:
        fit = _fit(X, y, offset, seed)
        for j, col in enumerate(keep):
            col.update(per_mtok=fit["weights"][j], **fit["boot"][j])
            rates = cost.model_price(col["model"], prices) if col["model"] and w_usd is not None else None
            if rates:
                col["list_implied"] = w_usd * rates[col["class"]]
        out["classes"] = keep
        out["class_fit"] = _stats(fit)
        act = [X[i] for i in active]
        out["correlated"] = []
        for j in range(len(keep)):
            for k in range(j + 1, len(keep)):
                r = _corr([row[j] for row in act], [row[k] for row in act])
                if r is not None and abs(r) >= CORR_FLAG:
                    out["correlated"].append({"a": keep[j]["class"], "b": keep[k]["class"], "r": r})
        resid = sorted(range(len(y)), key=lambda i: -abs(y[i] - fit["pred"][i]))[:3]
        out["largest_residuals"] = [{"t0": pairs[i]["t0"].isoformat(), "t1": pairs[i]["t1"].isoformat(),
                                     "rise": y[i], "fitted": fit["pred"][i]} for i in resid]

    # Timing: refit at shifted call times; the best fit should be at lag 0. Every
    # lag is scored on the same pairs, those with calls at any lag: a shift that
    # empties a pair must not drop that pair's rise from the score.
    timing, lag_X = {}, {}
    for lag in LAGS_MIN:
        bks = _bucket(pairs, events, lag)
        X = headline_matrix(bks)
        offset = None
        if X is None and keep:
            X, offset = class_matrix(bks)
        if X is not None:
            lag_X[lag] = (X, offset or [0.0] * len(y))
    scored = [i for i in range(len(y)) if any(any(X[i]) or off[i] for X, off in lag_X.values())]
    for lag, (X, off) in lag_X.items():
        target = [v - o for v, o in zip(y, off)]
        w = nnls(X, target)
        errs = [(target[i] - sum(a * b for a, b in zip(X[i], w))) ** 2 for i in scored]
        timing[lag] = math.sqrt(sum(errs) / len(errs)) if errs else None
    out["timing_rmse"] = timing
    return out


def calibrate(quota_log, projects, codex_home, prices=None, since=None, seed=0):
    rows = cost.load_jsonl(quota_log)
    result = {"quota_log": str(quota_log), "readings": len(rows), "pools": []}
    pair_sets = {}
    for pool in POOLS:
        pairs, stats = snapshot_pairs(rows, pool)
        if since:
            pairs = [p for p in pairs if p["t0"] >= since]
        pair_sets[pool] = (pairs, stats)
    all_pairs = [p for pairs, _ in pair_sets.values() for p in pairs]
    first = min((p["t0"] for p in all_pairs), default=None)
    if first:
        result["span"] = [first.isoformat(), max(p["t1"] for p in all_pairs).isoformat()]
    events = {"claude": claude_events(projects, prices) if first else [],
              "codex": codex_events(codex_home) if first else []}
    fits = {}
    for pool, (pairs, stats) in pair_sets.items():
        ref = fits.get(REFERENCE.get(pool), {})
        reference = ({"pool": ref["pool"], "weights": {c["class"]: c["per_mtok"] for c in ref["classes"]}}
                     if ref.get("classes") else None)
        fit = fit_pool(pool, pairs, events[POOLS[pool]], prices, seed, reference) if pairs else {"pool": pool, "pairs": 0}
        fit["broken"] = stats
        fits[pool] = fit
        result["pools"].append(fit)
    return result


def _f(v, nd=2):
    if v is None:
        return "-"
    if abs(v) >= 100:
        return f"{v:,.0f}"
    if abs(v) >= 10:
        return f"{v:.1f}"
    return f"{v:.{nd}f}"


def _n(n, word):
    text = str(int(n)) if float(n).is_integer() else _f(n, 0)
    return f"{text} {word}{'' if text == '1' else 's'}"


def _local(iso):
    return cost._parse_ts(iso).astimezone().strftime("%m-%d %H:%M")


def _fit_line(stats):
    return (f"fits {_f(stats['fitted_points'], 0)} points, RMSE {_f(stats['rmse'])} over pairs with calls "
            f"(whole-percent rounding alone: {_f(stats['rounding_rmse'])})")


def render(result, as_json=False):
    if as_json:
        return json.dumps(result, indent=2)
    lines = [f"calibration from {result['readings']} readings in {result['quota_log']}"]
    if "span" in result:
        lines[0] += f" ({_local(result['span'][0])} to {_local(result['span'][1])})"
    lines.append(f"weights are pool points per unit; p5-p95 from {BOOTSTRAP} bootstrap resamples of the pairs "
                 "(resampled independently, though neighbours share a reading: ranges are on the narrow side)")
    for fit in result["pools"]:
        lines.append("")
        broken = ", ".join(f"{v} {k}" for k, v in sorted(fit.get("broken", {}).items()))
        broken = f"; readings dropped: {broken}" if broken else ""
        if not fit.get("active_pairs"):
            lines.append(f"{fit['pool']}: {_n(fit.get('pairs', 0), 'pair')}, none with calls; nothing to fit{broken}")
            continue
        lines.append(f"{fit['pool']}: {_n(fit['pairs'], 'pair')}, {fit['active_pairs']} with calls "
                     f"({fit['calls']:,} calls); {_n(fit['points'], 'point')} risen{broken}")
        head = fit.get("headline")
        if head and head["unit"] == "usd":
            ppu = head["per_unit"]
            lines.append(f"  list price: {_f(ppu)} points per USD (p5-p95 {_f(head['p5'])}-{_f(head['p95'])}); "
                         f"1 point = ${_f(1 / ppu) if ppu else '-'}, full pool = ${_f(100 / ppu) if ppu else '-'}; "
                         + _fit_line(head))
        elif head:
            lines.append(f"  via {head['unit']} weights: {_f(head['per_unit'], 3)} points per {head['unit']} point "
                         f"(p5-p95 {_f(head['p5'], 3)}-{_f(head['p95'], 3)}); " + _fit_line(head))
        if head and head["sum_ratio"] is not None:
            ratio, nd = head["sum_ratio"], 2 if head["unit"] == "usd" else 3
            line = f"  sum ratio (points in pairs with calls / all units): {_f(ratio, nd)}"
            if head["per_unit"] and abs(ratio / head["per_unit"] - 1) > SUM_RATIO_FLAG:
                line += (f", {ratio / head['per_unit'] - 1:+.0%} against the per-pair fit; cause not established "
                         "(rises booked across pairs, calls missing from the logs, or class weights unlike "
                         "the headline's)")
            lines.append(line)
        models = fit.get("models", {})
        if fit.get("classes"):
            fitted_models = {c["model"] for c in fit["classes"]}
            label = "class" if None in fitted_models or len(fitted_models) > 1 else f"{next(iter(fitted_models))} class"
            table = [(label, "per Mtok", "p5-p95", "zero in boot", "list-implied")]
            for c in fit["classes"]:
                name = c["class"] if len(fitted_models) == 1 else f"{c['model']} {c['class']}"
                table.append((name, _f(c["per_mtok"], 3), f"{_f(c['p5'], 3)}-{_f(c['p95'], 3)}",
                              f"{c['zero_share']:.0%}", _f(c.get("list_implied"), 3)))
            widths = [max(len(r[i]) for r in table) for i in range(len(table[0]))]
            lines += ["  " + "  ".join(v.ljust(w) for v, w in zip(r, widths)).rstrip() for r in table]
            lines.append("  classes " + _fit_line(fit["class_fit"]))
        if fit["pool"].startswith("codex") and models:
            calls = sum(m["calls"] for m in models.values())
            lines.append("  models: " + ", ".join(f"{m} {v['calls'] / calls:.0%}" for m, v in models.items())
                         + " of calls (classes summed over models)")
        if fit.get("dropped"):
            parts, fitted_models = [], {c["model"] for c in fit.get("classes", [])}
            whole = defaultdict(list)  # models none of whose classes were fitted
            for d in fit["dropped"]:
                if d["model"] is not None and d["model"] not in fitted_models:
                    whole[d["model"]].append(d)
                    continue
                share = f"{d['usd_share']:.1%} of USD, " if d["usd_share"] is not None else ""
                parts.append(f"{d['class']} ({share}{_n(d['pairs'], 'pair')})")
            for model, ds in whole.items():
                share = sum(d["usd_share"] or 0 for d in ds)
                parts.append(f"{model}, all classes ({share:.1%} of USD, {_n(models.get(model, {}).get('calls', 0), 'call')})")
            how = (f"<{MIN_USD_SHARE:.0%} of USD or <{MIN_PAIRS} pairs; carried at list price, "
                   f"{_n(fit.get('offset_points', 0.0), 'point')}" if head and head["unit"] == "usd"
                   else f"<{MIN_PAIRS} pairs")
            lines.append(f"  not fitted ({how}): " + "; ".join(parts))
        if fit.get("correlated"):
            lines.append("  correlated classes: " + ", ".join(f"{c['a']}~{c['b']} r={c['r']:.2f}" for c in fit["correlated"])
                         + " (their split is weakly identified; see p5-p95)")
        if fit.get("unpriced_calls"):
            lines.append(f"  unpriced calls (no list price): {fit['unpriced_calls']}")
        lines.append(f"  rises with no local calls: {_n(fit['idle_rises'], 'pair')}, {_n(fit['idle_points'], 'point')} "
                     "(other devices, the web app, or a reading lag)")
        if fit.get("timing_rmse"):
            lines.append("  timing: RMSE with call times shifted " + ", ".join(
                f"{lag:+d} min {_f(v)}" for lag, v in fit["timing_rmse"].items()))
        if fit.get("largest_residuals"):
            lines.append("  largest residuals: " + "; ".join(
                f"{_local(r['t0'])}-{_local(r['t1'])[-5:]} rose {_f(r['rise'], 0)}, fitted {_f(r['fitted'], 1)}"
                for r in fit["largest_residuals"]))
    return "\n".join(lines)


def cmd_calibrate(a):
    _, prices = cost.load_catalog_cost()
    codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
    result = calibrate(quota.log_path(a.quota_log), cost._claude_config_dir() / "projects", codex_home,
                       prices, since=cost._since_dt(a.since))
    print(render(result, as_json=a.json))

"""Rank delegates for a task type: AA ratings weighted per task x live quota."""
import math

METRICS = ("intelligence", "coding", "speed", "price")


def _positive_price(v):
    """Return v when it is a positive finite int/float (not bool); else None.

    Hand-written catalog.local.json priors can put a string, bool, zero or
    negative here; treating those as absent keeps rank from raising.
    """
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if not math.isfinite(v) or v <= 0:
        return None
    return v


def _ratings(d, aa_models):
    """Return (metrics dict, price_blended, cost_per_task, source tag).

    metrics["price"] is filled later, once the ranking's price unit is chosen.
    """
    slug = d.get("aa")
    if slug and slug in aa_models:
        m = aa_models[slug]
        return ({"intelligence": m.get("intelligence"), "coding": m.get("coding"),
                 "speed": m.get("tps"), "price": None},
                _positive_price(m.get("price_blended")),
                _positive_price(m.get("cost_per_task")),
                ("aa*" if m.get("coding_is_proxy") else "aa") + ("~" if d.get("aa_proxy") else ""))
    p = d.get("prior")
    if p:
        return ({"intelligence": p.get("intelligence"), "coding": p.get("coding"),
                 "speed": p.get("tps"), "price": None},
                _positive_price(p.get("price_blended")),
                _positive_price(p.get("cost_per_task")), "prior")
    return ({k: None for k in METRICS}, None, None,
            f"missing AA slug {slug!r}" if slug else "unrated")


def _quota(d, pools):
    """Return (left_pct|None, pace|None, problems, unknown_pools).

    A pool missing from `pools` is unknown: it neither excludes the delegate nor
    counts as full. Pools present but with an `error` key are reported in
    problems; left stays None when no healthy pool reported a percentage.
    """
    lefts, paces, problems, unknown = [], [], [], []
    for pid in d.get("pools", []):
        e = pools.get(pid)
        if not e:
            unknown.append(pid)
            continue
        if "error" in e:
            problems.append(f"{pid}: {e['error']}")
            continue
        lefts.append(e["left_pct"])
        # pace only counts for long windows; a 5h window refills too soon to matter
        if e.get("pace") is not None and (pid.endswith("-7d") or pid.startswith("cursor")):
            paces.append(e["pace"])
    left = min(lefts) if lefts else None
    pace = min(paces) if paces else None
    return left, pace, problems, unknown


def _prefer_list(task_name, catalog, prefer):
    if prefer is None:
        prefer = catalog.get("prefer")
    if prefer is None:
        return []
    if isinstance(prefer, dict):
        return list(prefer.get(task_name) or [])
    return list(prefer)


def _price_unit(rows):
    """Whole-table unit: cost_per_task when every priced row has one, else blended.

    A row is priced when it has a truthy price_blended or cost_per_task. Unrated
    rows (neither) do not block the per-task unit. Mixing $/task and $/1M in one
    ranking would make the (best/value)^0.35 normalisation meaningless.
    """
    priced = [r for r in rows if r["_blended"] or r["_cpt"]]
    if priced and all(r["_cpt"] for r in priced):
        return "cost_per_task"
    return "price_blended"


def rank(task_name, catalog, pools, aa_models, min_quota=None, agents=None, prefer=None):
    task = catalog["tasks"][task_name]
    w = task["weights"]
    min_quota = catalog.get("min_quota_pct", 10) if min_quota is None else min_quota
    pref = _prefer_list(task_name, catalog, prefer)
    pref_index = {did: i for i, did in enumerate(pref)}
    rows = []
    for d in catalog["delegates"]:
        if agents and d["agent"] not in agents:
            continue
        metrics, blended, cpt, source = _ratings(d, aa_models)
        left, pace, problems, unknown = _quota(d, pools)
        rows.append({"delegate": d, "metrics": metrics, "source": source,
                     "_blended": blended, "_cpt": cpt,
                     "left": left, "pace": pace, "problems": problems,
                     "unknown_pools": unknown,
                     "quota_read": left is not None and not unknown and not problems,
                     "preferred": d["id"] in pref_index})

    price_unit = _price_unit(rows)
    # Warn only when the table fell back despite at least one row having a task cost.
    fallback_ids = []
    if price_unit == "price_blended":
        any_cpt = any(r["_cpt"] for r in rows)
        missing = [r["delegate"]["id"] for r in rows
                   if (r["_blended"] or r["_cpt"]) and not r["_cpt"]]
        if any_cpt and missing:
            fallback_ids = missing
    for r in rows:
        r["price_unit"] = price_unit
        r["metrics"]["price"] = r["_cpt"] if price_unit == "cost_per_task" else r["_blended"]
        del r["_blended"]
        del r["_cpt"]
        if fallback_ids:
            r["price_fallback_ids"] = fallback_ids

    # normalise each metric to 0..1 against the best candidate. Speed and price
    # span 5-100x across models, so they are damped; otherwise a tiny model's
    # price would outweigh every capability difference.
    best = {}
    for k in METRICS:
        vals = [r["metrics"][k] for r in rows if r["metrics"][k]]
        if vals:
            best[k] = min(vals) if k == "price" else max(vals)
    for r in rows:
        q, wsum = 0.0, 0.0
        for k in METRICS:
            v = r["metrics"][k]
            if not w.get(k) or not v or k not in best:
                continue
            if k == "price":
                norm = (best[k] / v) ** 0.35
            elif k == "speed":
                norm = (v / best[k]) ** 0.5
            else:
                norm = v / best[k]
            q += w[k] * norm
            wsum += w[k]
        r["quality"] = q / wsum if wsum else 0.0
        if r["left"] is None:
            # unknown / errored quota: neither trust nor exclude (visible as "?")
            r["quota_factor"] = 0.7
        else:
            bonus = 1 + 0.15 * max(0.0, min(1.0, (r["pace"] or 1) - 1))
            r["quota_factor"] = math.sqrt(r["left"] / 100) * bonus
        if r["delegate"]["agent"] == "claude-subagent":
            # spends the same pool the orchestrating Claude session runs on
            r["quota_factor"] *= catalog.get("orchestrator_pool_penalty", 0.8)
        r["excluded"] = r["left"] is not None and r["left"] < min_quota
        r["score"] = 0.0 if r["excluded"] else 100 * r["quality"] * r["quota_factor"]

    # Preference leads only with healthy readings for every pool and enough quota.
    # Unread preferences sort by score; exhausted pools stay excluded (score → 0).
    def sort_key(r):
        if r["preferred"] and r["quota_read"] and not r["excluded"]:
            return (0, pref_index[r["delegate"]["id"]])
        return (1, -r["score"])

    rows.sort(key=sort_key)
    return task, rows


def launch_hint(d, task):
    ro = " --read-only" if task["mode"] == "read-only" else ""
    if d["agent"] == "codex":
        eff = f" --effort {d['effort']}" if d.get("effort") else ""
        return f"delegate launch codex --model {d['model']}{eff}{ro} --brief <brief.md> [--link data/cache]"
    if d["agent"] == "cursor":
        return f"delegate launch cursor --model {d['model']}{ro} --brief <brief.md> [--link data/cache]"
    if d["agent"] == "claude-subagent":
        kind = "Explore" if task["mode"] == "read-only" else "general-purpose"
        return f"Agent tool: subagent_type={kind}, model={d['model']}"
    return "?"


def render(task_name, task, rows):
    def fmt(v, spec):
        return format(v, spec) if v is not None else "-"
    price_unit = (rows[0].get("price_unit") if rows else None) or "price_blended"
    price_hdr = "$/TASK" if price_unit == "cost_per_task" else "$/1M"
    price_spec = ".3f" if price_unit == "cost_per_task" else ".2f"
    out = [f"task: {task_name} ({task['desc']}; {task['mode']})  weights: "
           + ", ".join(f"{k} {v}" for k, v in task["weights"].items() if v)]
    hdr = ("DELEGATE", "SCORE", "QUAL", "INT", "COD", "TOK/S", price_hdr, "QUOTA", "PACE", "ROUNDS", "SRC")
    table = [hdr]
    for r in rows:
        m = r["metrics"]
        tag = r["delegate"]["id"]
        if r["excluded"]:
            tag += " (excluded)"
        elif r.get("preferred"):
            tag += " (pref)" if r["quota_read"] else " (pref, quota ?)"
        rounds = r.get("rounds_left")
        table.append((
            tag,
            f"{r['score']:.0f}", f"{r['quality']:.2f}",
            fmt(m["intelligence"], ".1f"), fmt(m["coding"], ".1f"),
            fmt(m["speed"], ".0f"), fmt(m["price"], price_spec),
            f"{r['left']:.0f}%" if r["left"] is not None else "?",
            f"{r['pace']:.1f}x" if r["pace"] is not None else "",
            str(rounds) if rounds is not None else "-",
            r["source"],
        ))
    widths = [max(len(row[i]) for row in table) for i in range(len(hdr))]
    out += ["  ".join(c.ljust(wd) for c, wd in zip(row, widths)).rstrip() for row in table]
    out.append("")
    out.append("QUAL = weighted AA metrics vs the best candidate (speed^0.5, price^0.35 damped); "
               "price is AA cost per task, or $/1M blended for the whole table when a priced "
               "delegate lacks one; "
               "SCORE = QUAL x sqrt(quota left) x pace bonus (<=+15%), x0.8 for Claude subagents (shared pool). "
               "pref = catalog/CLI preference (outranks score only when every pool was read and clears min_quota). "
               "QUOTA ? = unknown pool (neither excluded nor treated as full).")
    out.append("ROUNDS = rounds left in the delegate's tightest pool at its median per-round cost "
               "from the logs (n >= 3).")
    out.append("SRC: aa = AA API, aa* = AA page (coding = proxy from Terminal-Bench 4.0 + SciCode), "
               "~ = rating of a stand-in model (aa_proxy), prior = hand-set guess in catalog.json.")
    top = next((r for r in rows if not r["excluded"]), None)
    if top:
        d = top["delegate"]
        out.append("")
        out.append(f"pick: {d['id']}  ->  {launch_hint(d, task)}")
        if d.get("notes"):
            out.append(f"note: {d['notes']}")
    for r in rows:
        for p in r["problems"]:
            out.append(f"warning: {r['delegate']['id']}: {p}")
        for pid in r.get("unknown_pools") or []:
            out.append(f"quota ?: {r['delegate']['id']}: {pid} (unknown pool)")
    fallback_ids = rows[0].get("price_fallback_ids") if rows else None
    if fallback_ids:
        out.append("price: $/1M blended for the whole table; no AA cost per task for: "
                   + ", ".join(fallback_ids))
    return "\n".join(out)

"""Remaining quota per delegation pool, read from `openusage export`."""
import json
import re
import subprocess
from datetime import datetime, timezone

# pool id -> where openusage keeps it. window_h is the window length when the
# pool rolls on a fixed period; billing-cycle pools use billing_cycle_progress.
POOLS = {
    "claude-5h": {"provider": "claude_code", "metric": "usage_five_hour",
                  "reset": "usage_five_hour", "window_h": 5, "label": "Claude 5h"},
    "claude-7d": {"provider": "claude_code", "metric": "usage_seven_day",
                  "reset": "usage_seven_day", "window_h": 168, "label": "Claude 7d"},
    "codex-5h": {"provider": "codex", "metric": "rate_limit_primary",
                 "reset": "rate_limit_primary", "window_h": 5, "label": "Codex 5h"},
    "codex-7d": {"provider": "codex", "metric": "rate_limit_secondary",
                 "reset": "rate_limit_secondary", "window_h": 168, "label": "Codex 7d"},
    "cursor-auto": {"provider": "cursor", "metric": "plan_auto_percent_used",
                    "reset": "billing_cycle_end", "cycle": True,
                    "label": "Cursor Auto"},
    "cursor-premium": {"provider": "cursor", "metric": "plan_api_percent_used",
                       "reset": "billing_cycle_end", "cycle": True,
                       "label": "Cursor premium (API)"},
    # cursor-models (Cursor Grok + Composer): openusage has no metric — intentionally absent.
    # route._quota treats a missing pool as unknown (QUOTA ?), not excluded.
}


def parse_ts(s):
    if not s:
        return None
    # openusage emits nanoseconds; fromisoformat accepts at most microseconds
    m = re.fullmatch(r"([^.]+?)(?:\.(\d+))?(Z|[+-]\d\d:?\d\d)?", s.strip())
    if not m:
        return None
    head, frac, tz = m.groups()
    tz = "+00:00" if tz in (None, "Z") else tz
    return datetime.fromisoformat(head + (f".{frac[:6]}" if frac else "") + tz)


def fetch(source="auto", timeout=90):
    out = subprocess.run(
        ["openusage", "export", "--output", "-", "--format", "json", "--source", source],
        capture_output=True, text=True, timeout=timeout, check=True,
    ).stdout
    return json.loads(out)


def pools(envelope, now=None):
    """Return {pool_id: {...}} with remaining %, reset time and pace."""
    now = now or datetime.now(timezone.utc)
    snaps = {s.get("provider_id"): s for s in envelope.get("snapshots", [])}
    result = {}
    for pid, spec in POOLS.items():
        snap = snaps.get(spec["provider"])
        entry = {"label": spec["label"], "provider": spec["provider"]}
        result[pid] = entry
        if not snap:
            entry["error"] = "provider missing from openusage export"
            continue
        m = (snap.get("metrics") or {}).get(spec["metric"])
        if not m:
            entry["error"] = f"metric {spec['metric']} missing"
            continue
        limit = m.get("limit") or 100
        if m.get("remaining") is not None:
            remaining = m["remaining"]
        else:
            remaining = limit - (m.get("used") or 0)
        entry["left_pct"] = max(0.0, min(100.0, 100.0 * remaining / limit))
        entry["status"] = snap.get("status")
        snap_ts = parse_ts(snap.get("timestamp"))
        entry["age_min"] = round((now - snap_ts).total_seconds() / 60) if snap_ts else None
        reset = parse_ts((snap.get("resets") or {}).get(spec["reset"]))
        entry["resets_at"] = reset.isoformat() if reset else None
        # time_left_frac: share of the window still ahead of us. Comparing it
        # with left_pct tells whether the pool is being under- or over-used.
        tlf = None
        if spec.get("cycle"):
            prog = (snap.get("metrics") or {}).get("billing_cycle_progress")
            if prog and prog.get("used") is not None:
                tlf = max(0.0, 1 - prog["used"] / (prog.get("limit") or 100))
        elif reset and spec.get("window_h"):
            tlf = max(0.0, min(1.0, (reset - now).total_seconds() / 3600 / spec["window_h"]))
        if reset and reset < now:
            # snapshot predates the reset: the pool has refilled since
            entry["stale_reset"] = True
        entry["time_left_frac"] = tlf
        entry["pace"] = (entry["left_pct"] / 100) / max(tlf, 0.05) if tlf is not None else None
    return result


def human_delta(dt, now):
    secs = (dt - now).total_seconds()
    if secs < 0:
        return "passed"
    d, rem = divmod(int(secs), 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"in {d}d{h}h"
    if h:
        return f"in {h}h{m:02d}m"
    return f"in {m}m"


def render(result, now=None):
    now = now or datetime.now(timezone.utc)
    rows = [("POOL", "LEFT", "RESETS (local)", "", "PACE", "DATA AGE")]
    for pid, e in result.items():
        if "error" in e:
            rows.append((pid, "?", e["error"], "", "", ""))
            continue
        reset = parse_ts(e["resets_at"]) if e.get("resets_at") else None
        reset_s = reset.astimezone().strftime("%a %d %b %H:%M") if reset else "-"
        rel = human_delta(reset, now) if reset else ""
        if e.get("stale_reset"):
            rel = "passed (refilled?)"
        pace = f"{e['pace']:.1f}x" if e.get("pace") is not None else ""
        age = f"{e['age_min']}m" if e.get("age_min") is not None else ""
        rows.append((pid, f"{e['left_pct']:.0f}%", reset_s, rel, pace, age))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    lines = ["  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows]
    lines.append("")
    lines.append("PACE = share left / share of window left. >1: spare quota that resets "
                 "unused unless spent; <1: burning faster than the window refills.")
    return "\n".join(lines)

"""Remaining quota per delegation pool, read from `openusage export`."""
import fcntl
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
                    "label": "Cursor Auto+Models"},
    "cursor-premium": {"provider": "cursor", "metric": "plan_api_percent_used",
                       "reset": "billing_cycle_end", "cycle": True,
                       "label": "Cursor premium (API)"},
    # plan_auto_percent_used covers Auto plus the "Cursor Models" (Grok, Composer):
    # it equals their summed spend / $450 (checked 2026-10-10). Named Claude/GPT
    # models bill plan_api. route._quota treats a pool missing here as unknown.
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


def spawn_snapshot(reason, session=None, run=None, log=None, delegate=None):
    """Start a detached collector; measurements must never block a session/run."""
    if reason == "launch" and os.environ.get("CLAUDE_DELEGATE_QUOTA_SNAPSHOT") == "0":
        return
    delegate = delegate or Path(__file__).resolve().parent / "delegate"
    cmd = [sys.executable, str(delegate), "quota", "--snapshot", "--reason", reason]
    for flag, value in (("--session", session), ("--run", run), ("--log", log)):
        if value is not None:
            cmd.extend([flag, str(value)])
    try:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        pass


def _safe_ts(value):
    try:
        return parse_ts(value)
    except (AttributeError, TypeError, ValueError):
        return None


def _last_good(path):
    """Ignore failed attempts and malformed rows when finding the watermark."""
    latest, latest_ts = None, None
    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict) or "error" in row:
                    continue
                entries = row.get("pools")
                if not isinstance(entries, dict) or not all(
                        isinstance(entries.get(pid), dict) and "used_pct" in entries[pid]
                        for pid in ("claude-5h", "claude-7d")):
                    continue
                ts = _safe_ts(row.get("ts"))
                if ts:
                    latest, latest_ts = row, ts
    except FileNotFoundError:
        pass
    return latest, latest_ts


def _last_write():
    projects = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"
    newest = None
    for path in projects.rglob("*.jsonl"):
        try:
            if not path.is_file():
                continue
            stamp = path.stat().st_mtime
        except OSError:  # a transcript may disappear during the walk
            continue
        newest = stamp if newest is None else max(newest, stamp)
    return datetime.fromtimestamp(newest, timezone.utc) if newest is not None else None


def _activity_gate(path, now):
    row, good = _last_good(path)
    write = _last_write()
    gate = {"last_good": good.isoformat() if good else None,
            "last_write": write.isoformat() if write else None}
    if good is None:
        return "timer", gate, None
    if write and write > good:
        return "timer", gate, None
    for entry in row["pools"].values():
        reset = _safe_ts(entry.get("resets_at")) if isinstance(entry, dict) else None
        if reset and good < reset <= now:
            return "reset", gate, None
    if write and good < write + timedelta(seconds=600) <= now:
        return "trailing", gate, None
    return None, gate, "no activity, reset, or trailing reading due"


def _snapshot_pools(envelope, ts):
    snaps = {s.get("provider_id"): s for s in envelope.get("snapshots", [])}
    result = {}
    for pid, entry in pools(envelope, now=ts).items():
        if "error" in entry:
            result[pid] = {"error": entry["error"]}
            continue
        observed = snaps[POOLS[pid]["provider"]].get("timestamp")
        observed_ts = _safe_ts(observed)
        result[pid] = {"used_pct": round(float(100 - entry["left_pct"]), 4),
                       "resets_at": entry["resets_at"], "observed_at": observed,
                       "stale": observed_ts is None or (ts - observed_ts).total_seconds() > 120}
        if entry.get("stale_reset"):
            result[pid]["stale_reset"] = True
    return result


def _fetch_error(exc):
    if isinstance(exc, FileNotFoundError):
        cause = "openusage not found"
    elif isinstance(exc, subprocess.TimeoutExpired):
        cause = "openusage timed out"
    elif isinstance(exc, subprocess.CalledProcessError):
        cause = f"openusage exited with status {exc.returncode}"
    elif isinstance(exc, (ValueError, TypeError, AttributeError, KeyError)):
        cause = "invalid openusage JSON/export"
    else:
        cause = f"openusage failed: {str(exc)[:200]}"
    stderr = getattr(exc, "stderr", None) or ""
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    return cause + (": " + stderr.strip()[-300:] if stderr.strip() else "")


def _fetch_snapshot(source, timeout):
    # Keep fetch()'s legacy behavior, but retain stderr on a JSON decoding error.
    output = subprocess.run(
        ["openusage", "export", "--output", "-", "--format", "json", "--source", source],
        capture_output=True, text=True, timeout=timeout, check=True,
    )
    try:
        return json.loads(output.stdout)
    except ValueError as exc:
        exc.stderr = output.stderr
        raise


def snapshot(reason=None, session=None, run=None, if_active=False, log=None,
             source="direct", timeout=90):
    """Append one durable quota reading, serializing the gate and provider poll."""
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    path = Path(log or os.environ.get("CLAUDE_DELEGATE_QUOTA_LOG")
                or cache / "claude-delegate/quota.jsonl").expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    with Path(str(path) + ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        gate = None
        if if_active:
            reason, gate, skip = _activity_gate(path, datetime.now(timezone.utc))
            if skip:
                print("skip: " + skip)
                return 0
        ts = datetime.now(timezone.utc).replace(microsecond=0)
        row = {"ts": ts.isoformat(), "source": source, "reason": reason,
               "session": session if isinstance(session, str)
               and re.fullmatch(r"[A-Za-z0-9_-]+", session) else None, "run": run}
        if gate is not None:
            row["gate"] = gate
        try:
            row["pools"] = _snapshot_pools(_fetch_snapshot(source, timeout), ts)
        except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError, KeyError) as exc:
            row["error"] = _fetch_error(exc)
        line = json.dumps(row)
        with path.open("ab+") as stream:
            # Keep a truncated/malformed final line from swallowing this reading.
            if stream.tell():
                stream.seek(-1, os.SEEK_END)
                if stream.read(1) != b"\n":
                    stream.write(b"\n")
            stream.write((line + "\n").encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        print(line)
        return 1 if "error" in row else 0


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

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


def log_path(log=None):
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return Path(log or os.environ.get("CLAUDE_DELEGATE_QUOTA_LOG")
                or cache / "claude-delegate/quota.jsonl").expanduser()


def record(reason=None, session=None, run=None, if_active=False, log=None,
           source="direct", timeout=90):
    """Append one durable quota reading; return (row, None), or (None, why) when gated off."""
    path = log_path(log)
    path.parent.mkdir(parents=True, exist_ok=True)
    with Path(str(path) + ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        gate = None
        if if_active:
            reason, gate, skip = _activity_gate(path, datetime.now(timezone.utc))
            if skip:
                return None, skip
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
        return row, None


def snapshot(reason=None, session=None, run=None, if_active=False, log=None,
             source="direct", timeout=90):
    """`delegate quota --snapshot`: record a reading, print it; exit status 1 on a fetch error."""
    row, skip = record(reason, session, run, if_active, log, source, timeout)
    if skip:
        print("skip: " + skip)
        return 0
    print(json.dumps(row))
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


# SessionStart / context-guard spare line: only these pools are delegate targets.
SPARE_POOLS = ("codex-7d", "cursor-auto")
SPARE_MAX_AGE_H = 12


def cycle_window_start(reset):
    """Calendar month that ends at reset; day clamped (03-31 → 02-28/29)."""
    year, month = reset.year, reset.month
    if month == 1:
        year, month = year - 1, 12
    else:
        month -= 1
    # days-in-month without importing calendar: day 0 of next month.
    if month == 12:
        last = (reset.replace(year=year + 1, month=1, day=1) - timedelta(days=1)).day
    else:
        last = (reset.replace(year=year, month=month + 1, day=1) - timedelta(days=1)).day
    return reset.replace(year=year, month=month, day=min(reset.day, last))


def _entry_pace(pid, entry, reading_ts):
    """Pace at reading_ts from a quota.jsonl pool entry; None if unknown."""
    if not isinstance(entry, dict) or "used_pct" not in entry or entry.get("stale"):
        return None
    try:
        used = float(entry["used_pct"])
    except (TypeError, ValueError):
        return None
    if not (0.0 <= used <= 100.0):
        return None
    reset = _safe_ts(entry.get("resets_at"))
    if reset is None:
        return None
    left_frac = max(0.0, (100.0 - used) / 100.0)
    spec = POOLS.get(pid) or {}
    if spec.get("cycle"):
        start = cycle_window_start(reset)
        window = (reset - start).total_seconds()
        if window <= 0:
            return None
        tlf = max(0.0, min(1.0, (reset - reading_ts).total_seconds() / window))
    elif spec.get("window_h"):
        tlf = max(0.0, min(1.0, (reset - reading_ts).total_seconds() / 3600 / spec["window_h"]))
    else:
        return None
    return left_frac / max(tlf, 0.05)


def _last_spare(path):
    """Newest row usable for the spare line: no top-level error, at least one SPARE_POOLS used_pct.

    Independent of `_last_good`, which requires Claude pools and would skip launch rows whose
    Claude metrics are missing while Codex/Cursor are fine.
    """
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
                if not isinstance(entries, dict):
                    continue
                if not any(isinstance(entries.get(pid), dict) and "used_pct" in entries[pid]
                           for pid in SPARE_POOLS):
                    continue
                ts = _safe_ts(row.get("ts"))
                if ts:
                    latest, latest_ts = row, ts
    except FileNotFoundError:
        pass
    return latest, latest_ts


def spare_pools(log, now=None):
    """Pools ahead of pace from the newest spare-usable reading, or None when no line should print.

    Returns {"age_s": float, "pools": [(pid, left_pct, pace), ...], "codex_5h": entry|None}
    for the SessionStart / nudge formatter. Errors and stale claims yield None.
    """
    try:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        path = Path(log) if log is not None else log_path()
        row, reading_ts = _last_spare(path)
        if row is None or reading_ts is None:
            return None
        age_s = (now - reading_ts).total_seconds()
        if age_s < 0 or age_s > SPARE_MAX_AGE_H * 3600:
            return None
        entries = row.get("pools")
        if not isinstance(entries, dict):
            return None
        listed = []
        for pid in SPARE_POOLS:
            entry = entries.get(pid)
            reset = _safe_ts(entry.get("resets_at")) if isinstance(entry, dict) else None
            if reset is None or reset <= now:
                continue
            pace = _entry_pace(pid, entry, reading_ts)
            if pace is None or pace <= 1:
                continue
            left_pct = max(0.0, 100.0 - float(entry["used_pct"]))
            listed.append((pid, left_pct, pace))
        if not listed:
            return None
        return {"age_s": age_s, "pools": listed,
                "codex_5h": entries.get("codex-5h") if isinstance(entries.get("codex-5h"), dict) else None}
    except Exception:
        return None


def format_spare_line(info, now=None):
    """One SessionStart / nudge line, or None."""
    if not info:
        return None
    try:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        age_s = info["age_s"]
        if age_s < 3600:
            age = f"{max(0, int(age_s // 60))}m ago"
        else:
            age = f"{int(age_s // 3600)}h ago"
        parts = []
        for pid, left_pct, pace in info["pools"]:
            label = "Codex 7d" if pid == "codex-7d" else "Cursor Auto"
            bit = f"{label} {left_pct:.0f}% left (pace {pace:.1f}x"
            if pid == "codex-7d":
                bit += f"; {_format_codex_5h(info.get('codex_5h'), now)}"
            bit += ")"
            parts.append(bit)
        if not parts:
            return None
        return (f"Delegate pools ahead of pace (reading {age}): "
                + ", ".join(parts)
                + ". Check `delegate quota` at dispatch.")
    except Exception:
        return None


def _format_codex_5h(entry, now):
    """Codex 5h aside beside a qualifying Codex 7d; always a short phrase."""
    if not isinstance(entry, dict) or "error" in entry or "used_pct" not in entry:
        return "Codex 5h unknown"
    try:
        left = max(0.0, 100.0 - float(entry["used_pct"]))
    except (TypeError, ValueError):
        return "Codex 5h unknown"
    if "resets_at" not in entry:
        return "Codex 5h unknown"
    reset = _safe_ts(entry.get("resets_at"))
    if reset is None:
        return "Codex 5h unknown"
    if reset <= now or entry.get("stale_reset"):
        return "Codex 5h reset since the reading"
    local = reset.astimezone()
    return f"Codex 5h {left:.0f}% left, resets {local.strftime('%H:%M')}"


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

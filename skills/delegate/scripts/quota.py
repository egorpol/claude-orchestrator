"""Remaining quota per delegation pool, read from Codex and openusage."""
import fcntl
import json
import math
import os
import re
import selectors
import signal
import subprocess
import sys
import time
import uuid
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

# Dispatch checks only these pools; Cursor dispatches do not need a quota read.
DISPATCH_POOLS = ("codex-5h", "codex-7d")
# At ~11 points per 355 s round, 15 s is about half a whole-percent gauge step.
REUSE_MAX_S = 15
REUSE_TAIL_BYTES = 256 * 1024


def check_dispatch(catalog_path=None):
    """Classify fresh Codex pools as ok/low/unknown; failures never block dispatch.

    Load the catalog outside the read's try so broken local configuration raises.
    """
    # Import inside the function: hooks load this module via
    # spec_from_file_location without the scripts dir on sys.path; a top-level
    # `import catalog` would break spare_pools on SessionStart.
    scripts = Path(__file__).resolve().parent
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import catalog as catalog_mod  # noqa: E402
    catalog = catalog_mod.load(catalog_path or scripts / "catalog.json")
    threshold = catalog.get("min_quota_pct", 10)
    try:
        entries = pools({"snapshots": [_codex_snapshot(timeout=15)]})
    except Exception as exc:
        error = _fetch_error(exc, "codex app-server")
        return {"catalog": catalog, "threshold": threshold, "error": error,
                "pools": {pid: {"state": "unknown", "reason": error}
                          for pid in DISPATCH_POOLS}}

    checked = {}
    for pid in DISPATCH_POOLS:
        entry = dict(entries.get(pid) or {})
        reason = None
        if entry.get("error"):
            reason = entry["error"]
        elif entry.get("age_min") is None:
            reason = "snapshot timestamp missing"
        elif entry.get("left_pct") is None:
            reason = "remaining quota missing"
        entry["state"] = ("unknown" if reason else
                          "low" if entry["left_pct"] < threshold else "ok")
        if reason:
            entry["reason"] = " ".join(reason.split())
        checked[pid] = entry
    return {"catalog": catalog, "threshold": threshold, "error": None, "pools": checked}


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


def fetch(source="hybrid", timeout=90):
    if source == "hybrid":
        return _hybrid_snapshot(timeout)
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
            entry["error"] = (envelope.get("_reader_errors") or {}).get(
                spec["provider"], "provider missing from openusage export")
            continue
        m = (snap.get("metrics") or {}).get(spec["metric"])
        if not m:
            entry["error"] = f"metric {spec['metric']} missing"
            # Fixed text only: the diagnostic carries a raw upstream response body.
            diagnostics = snap.get("diagnostics")
            if isinstance(diagnostics, dict) and "429" in str(diagnostics.get("usage_api_error")):
                entry["error"] += ": usage API rate limited (429)"
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


def spawn_snapshot(reason, session=None, run=None, log=None, delegate=None, job=None):
    """Start a detached collector; measurements must never block a session/run."""
    if reason in ("launch", "job-end") and os.environ.get("CLAUDE_DELEGATE_QUOTA_SNAPSHOT") == "0":
        return
    delegate = delegate or Path(__file__).resolve().parent / "delegate"
    cmd = [sys.executable, str(delegate), "quota", "--snapshot", "--reason", reason]
    for flag, value in (("--session", session), ("--run", run), ("--log", log), ("--job", job)):
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


class _ReaderError(ValueError):
    """An authored, safe error: never constructed from a Codex reply or stderr."""


def _fetch_error(exc, reader="openusage"):
    if isinstance(exc, _ReaderError):
        return " ".join(str(exc).split())[:600]
    if isinstance(exc, FileNotFoundError):
        cause = f"{reader} not found"
    elif isinstance(exc, subprocess.TimeoutExpired):
        cause = f"{reader} timed out"
    elif isinstance(exc, subprocess.CalledProcessError):
        cause = f"{reader} exited with status {exc.returncode}"
    elif isinstance(exc, (ValueError, TypeError, AttributeError, KeyError, OverflowError)):
        cause = f"invalid {reader} JSON/export"
    else:
        cause = f"{reader} failed"
        if reader == "openusage":
            cause += ": " + " ".join(str(exc).split())[:200]
    # Codex can include account/credit ids anywhere, including RPC errors and stderr.
    stderr = (getattr(exc, "stderr", None) or "") if reader == "openusage" else ""
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    tail = " ".join(stderr.strip()[-300:].split())
    return cause + (": " + tail if tail else "")


def _codex_snapshot(timeout=15):
    """A bounded JSON-lines RPC exchange, with no raw reply data in errors."""
    deadline = time.monotonic() + min(15, timeout)
    proc = subprocess.Popen(["codex", "app-server", "--stdio"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            start_new_session=True, bufsize=0)
    try:
        os.set_blocking(proc.stdout.fileno(), False)
        os.set_blocking(proc.stdin.fileno(), False)
        pending = b""
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)

            def send(message):
                data = (json.dumps(message) + "\n").encode()
                while data:
                    if time.monotonic() >= deadline:
                        raise subprocess.TimeoutExpired("codex app-server", timeout)
                    try:
                        count = os.write(proc.stdin.fileno(), data)
                        data = data[count:]
                    except BlockingIOError:
                        # Requests are tiny, but a child that never reads must still time out.
                        with selectors.DefaultSelector() as writable:
                            writable.register(proc.stdin, selectors.EVENT_WRITE)
                            writable.select(max(0, deadline - time.monotonic()))
                    except BrokenPipeError:
                        raise _ReaderError("codex app-server closed stdin") from None

            def reply(request_id):
                nonlocal pending
                while True:
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        try:
                            message = json.loads(line)
                        except (ValueError, UnicodeError):
                            continue
                        if not isinstance(message, dict) or message.get("id") != request_id:
                            continue
                        if "error" in message:
                            raise _ReaderError("codex app-server RPC error")
                        if not isinstance(message.get("result"), dict):
                            raise _ReaderError("codex app-server invalid result")
                        return message["result"], datetime.now(timezone.utc)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise subprocess.TimeoutExpired("codex app-server", timeout)
                    try:
                        chunk = os.read(proc.stdout.fileno(), 65536)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        raise _ReaderError("codex app-server EOF before reply")
                    pending += chunk
                    if len(pending) > 1024 * 1024:
                        raise _ReaderError("codex app-server reply too large")

            send({"id": 1, "method": "initialize", "params": {
                "clientInfo": {"name": "claude-delegate-quota", "version": "1"}}})
            reply(1)
            send({"method": "initialized"})
            send({"id": 2, "method": "account/rateLimits/read"})
            result, arrived = reply(2)
        limits = result.get("rateLimits")
        if not isinstance(limits, dict):
            raise _ReaderError("codex app-server rate limits missing")
        snap = {"provider_id": "codex", "timestamp": arrived.isoformat(),
                "status": "ok", "metrics": {}, "resets": {}}
        for window in (limits.get("primary"), limits.get("secondary")):
            if not isinstance(window, dict):
                continue
            for pid in DISPATCH_POOLS:
                spec = POOLS[pid]
                if window.get("windowDurationMins") != spec["window_h"] * 60:
                    continue
                used, reset = window.get("usedPercent"), window.get("resetsAt")
                if (not isinstance(used, (int, float)) or isinstance(used, bool)
                        or not math.isfinite(used)):
                    raise _ReaderError("codex app-server invalid usedPercent")
                if reset is not None:
                    if not isinstance(reset, (int, float)) or isinstance(reset, bool):
                        raise _ReaderError("codex app-server invalid resetsAt")
                    try:
                        reset = datetime.fromtimestamp(reset, timezone.utc).isoformat()
                    except (ValueError, OverflowError, OSError):
                        raise _ReaderError("codex app-server invalid resetsAt") from None
                snap["metrics"][spec["metric"]] = {
                    "limit": 100, "used": used, "remaining": 100 - used}
                snap["resets"][spec["reset"]] = reset
        return snap
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=min(0.05, max(0, deadline - time.monotonic())))
        except subprocess.TimeoutExpired:
            pass
        # Even an exited leader may have descendants holding stdout open.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            pass  # killed but not released yet: that must not cost a good reading
        proc.stdout.close()


def _cache_path():
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache").expanduser()


def _openusage_env():
    # Resolve cache and targets against the real environment before hiding HOME/XDG.
    home = _cache_path() / "claude-delegate/openusage-home"
    real = Path.home()
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    home.chmod(0o700)
    (home / ".config").mkdir(mode=0o700, exist_ok=True)
    for name in (".claude", ".claude.json", ".cursor", ".config/Cursor"):
        target, link = real / name, home / name
        if not target.exists():
            if link.is_symlink():
                link.unlink(missing_ok=True)
            continue
        if link.is_symlink() and os.readlink(link) == str(target):
            continue
        temporary = link.with_name(link.name + "." + uuid.uuid4().hex)
        try:
            temporary.symlink_to(target)
            os.replace(temporary, link)
        finally:
            temporary.unlink(missing_ok=True)
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("XDG_") and key != "CODEX_HOME"}
    env["HOME"] = str(home)
    return env


def _hybrid_snapshot(timeout):
    snapshots, errors = [], {}
    try:
        snapshots.append(_codex_snapshot(timeout=min(15, timeout)))
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError, KeyError, OverflowError) as exc:
        errors["codex"] = _fetch_error(exc, "codex app-server")
    try:
        output = subprocess.run(
            ["openusage", "export", "--output", "-", "--format", "json", "--source", "direct"],
            env=_openusage_env(), capture_output=True, text=True, timeout=timeout, check=True)
        try:
            envelope = json.loads(output.stdout)
            other = [snap for snap in envelope["snapshots"] if snap.get("provider_id") != "codex"]
            # Validate the other side separately: malformed exports cannot discard Codex.
            pools({"snapshots": other})
        except (ValueError, TypeError, AttributeError, KeyError) as exc:
            exc.stderr = output.stderr
            raise
        snapshots.extend(other)
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError, KeyError, OverflowError) as exc:
        error = _fetch_error(exc)
        errors.update(claude_code=error, cursor=error)
    if "codex" in errors and "claude_code" in errors:
        raise _ReaderError(errors["codex"] + "; " + errors["claude_code"])
    return {"snapshots": snapshots, "_reader_errors": errors}


def _fetch_snapshot(source, timeout):
    if source == "hybrid":
        return _hybrid_snapshot(timeout)
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
    cache = _cache_path()
    return Path(log or os.environ.get("CLAUDE_DELEGATE_QUOTA_LOG")
                or cache / "claude-delegate/quota.jsonl").expanduser()


def _reuse_reading(path, row, now):
    """Use only the bounded tail, and never turn a start/end measurement into zero."""
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - REUSE_TAIL_BYTES))
            if size > REUSE_TAIL_BYTES:
                stream.readline()  # discard the first, potentially truncated row
            lines = stream.read().splitlines()
    except FileNotFoundError:
        return None
    rows = []
    for line in lines:
        try:
            previous = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if isinstance(previous, dict):
            rows.append(previous)
    if not rows:
        return None
    latest = rows[-1]
    entries = latest.get("pools")
    if (latest.get("source") != "hybrid" or "error" in latest or "reader" not in latest
            or not isinstance(entries, dict) or not all(
                isinstance(entries.get(pid), dict) and "used_pct" in entries[pid] for pid in POOLS)):
        return None
    origin = latest.get("reused_from", latest.get("ts"))
    observed = _safe_ts(origin)
    latest_ts = _safe_ts(latest.get("ts"))
    if (observed is None or latest_ts is None or latest_ts > now
            or not 0 <= (now - observed).total_seconds() <= REUSE_MAX_S):
        return None
    # Require the original row to be in the tail; otherwise pair history is incomplete.
    if not any(p.get("ts") == origin and "reused_from" not in p for p in rows):
        return None
    key = {"job-end": "run", "session-end": "session"}.get(row["reason"])
    if key and row.get(key) is not None and any(
            p.get(key) == row[key] and p.get("reused_from", p.get("ts")) == origin for p in rows):
        return None
    return latest, origin


def record(reason=None, session=None, run=None, if_active=False, log=None,
           source="hybrid", timeout=90, job=None):
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
        now = datetime.now(timezone.utc)
        ts = now.replace(microsecond=0)
        row = {"ts": ts.isoformat(), "source": source,
               "reader": "codex-app-server+openusage-direct" if source == "hybrid" else f"openusage-{source}",
               "reason": reason,
               "session": session if isinstance(session, str)
               and re.fullmatch(r"[A-Za-z0-9_-]+", session) else None, "run": run}
        if isinstance(job, str) and re.fullmatch(r"(round|review)-[0-9]+", job):
            row["job"] = job
        if gate is not None:
            row["gate"] = gate
        reusable = _reuse_reading(path, row, now) if source == "hybrid" else None
        try:
            if reusable:
                previous, origin = reusable
                row.update(pools=previous["pools"], reader=previous["reader"], reused_from=origin)
            else:
                row["pools"] = _snapshot_pools(_fetch_snapshot(source, timeout), ts)
        except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError, KeyError, OverflowError) as exc:
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
             source="hybrid", timeout=90, job=None):
    """`delegate quota --snapshot`: record a reading, print it; exit status 1 on a fetch error."""
    row, skip = record(reason, session, run, if_active, log, source, timeout, job=job)
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


def render(result, now=None, rounds_lines=None):
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
    for line in rounds_lines or []:
        lines.append(line)
    return "\n".join(lines)

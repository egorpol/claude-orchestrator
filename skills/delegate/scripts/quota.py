"""Remaining quota per delegation pool, read from Codex, Claude /usage and Cursor /usage."""
import fcntl
import json
import math
import os
import pty
import re
import select
import selectors
import signal
import struct
import subprocess
import sys
import termios
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# pool id -> where each reader keeps it. window_h is the window length when the
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
# Claude /usage: at least 3 minutes between attempts; failure backoff never undercuts that.
CLAUDE_MIN_INTERVAL_S = 180
CLAUDE_BACKOFF_S = (60, 120, 240, 480, 960, 1800)  # effective waits: 3, 3, 4, 8, 16, 30 min
CLAUDE_USAGE_SETTINGS = '{"disableAllHooks": true}'
CLAUDE_USAGE_ARGS = (
    "-p", "/usage",
    "--output-format", "stream-json",
    "--verbose",
    "--no-session-persistence",
    "--strict-mcp-config",
    "--settings", CLAUDE_USAGE_SETTINGS,
)
CLAUDE_ENV_ALLOW = frozenset({
    "HOME", "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "CLAUDE_CONFIG_DIR",
    "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY", "no_proxy",
    "ALL_PROXY", "all_proxy", "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE",
})
# Cursor interactive /usage: at least 30 minutes between attempts (success or failure).
CURSOR_MIN_INTERVAL_S = 1800
_ANSI_RE = re.compile(
    rb'\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*(\x07|\x1b\\)|\x1b[()][0-9A-Za-z]|\x1b[=>]')
_CURSOR_MONTHS = {name: index for index, name in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}
# The TUI redraws by erasing lines (ESC[2K ESC[1A ... ESC[G) and rewriting them, so a
# row from a superseded frame stays in the byte stream after the screen dropped it.
_TERM_SEQ_RE = re.compile(
    r"\x1b\[([0-9;?<>=]*)[ -/]*([@-~])|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b[()][0-9A-Za-z]|\x1b[@-Z\\-_=>]")
_CURSOR_INPUT_RE = re.compile(r"\s*→\s+/usage\s*")
_CURSOR_ROW_RE = re.compile(r"\s*→\s+/usage\s+Show plan and on-demand usage\s*")
_CURSOR_PROMPT_RE = re.compile(r"build\s*anything")
HYBRID_READER = "codex-app-server+claude-usage+cursor-usage"


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
    # A missing zone means UTC for a datetime; for a date-only string, appending
    # +00:00 makes fromisoformat treat '+' as the date/time separator and return
    # a naive midnight — reject that rather than invent a time.
    tz = "+00:00" if tz in (None, "Z") else tz
    try:
        parsed = datetime.fromisoformat(head + (f".{frac[:6]}" if frac else "") + tz)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def fetch(source="hybrid", timeout=90, log=None):
    if source == "hybrid":
        return _hybrid_snapshot(timeout, log=log)
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
            elif reset:
                start = cycle_window_start(reset)
                window = (reset - start).total_seconds()
                if window > 0:
                    tlf = max(0.0, min(1.0, (reset - now).total_seconds() / window))
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
                # A carried Claude gauge is not a fresh watermark for --if-active.
                if any(entries[pid].get("carried") for pid in ("claude-5h", "claude-7d")):
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
    carried = envelope.get("_carried") or {}
    result = {}
    for pid, entry in pools(envelope, now=ts).items():
        if "error" in entry:
            result[pid] = {"error": entry["error"]}
            continue
        provider = POOLS[pid]["provider"]
        observed = snaps[provider].get("timestamp")
        observed_ts = _safe_ts(observed)
        result[pid] = {"used_pct": round(float(100 - entry["left_pct"]), 4),
                       "resets_at": entry["resets_at"], "observed_at": observed,
                       "stale": observed_ts is None or (ts - observed_ts).total_seconds() > 120}
        if snaps[provider].get("whole_percent"):
            result[pid]["whole_percent"] = True
        if entry.get("stale_reset"):
            result[pid]["stale_reset"] = True
        reason = carried.get(provider)
        if reason:
            result[pid]["carried"] = reason
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
    # Hybrid reads Claude via /usage, so never expose the Claude OAuth token here.
    home = _cache_path() / "claude-delegate/openusage-home"
    real = Path.home()
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    home.chmod(0o700)
    (home / ".config").mkdir(mode=0o700, exist_ok=True)
    for name in (".claude", ".claude.json"):
        link = home / name
        # Drop only leftover symlinks from older installs; never a real file.
        if link.is_symlink():
            link.unlink(missing_ok=True)
    for name in (".cursor", ".config/Cursor"):
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


def _claude_state_path(log=None):
    return log_path(log).parent / "claude-usage.json"


def _load_claude_state(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, UnicodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_claude_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex)
    try:
        fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        temporary.unlink(missing_ok=True)


def _claude_env():
    return {key: value for key, value in os.environ.items() if key in CLAUDE_ENV_ALLOW}


def _claude_finite_number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _claude_resets_at_ok(resets_at):
    """None, or an ISO string that parses; keep the string as written when valid."""
    if resets_at is None:
        return True
    if not isinstance(resets_at, str):
        return False
    return _safe_ts(resets_at) is not None


def _claude_gauge_ok(gauge):
    """A metrics value safe for pools(): numeric limit/used/remaining only."""
    if not isinstance(gauge, dict):
        return False
    limit = gauge.get("limit", 100)
    if not _claude_finite_number(limit) or limit <= 0:
        return False
    used, remaining = gauge.get("used"), gauge.get("remaining")
    if used is not None and not _claude_finite_number(used):
        return False
    if remaining is not None and not _claude_finite_number(remaining):
        return False
    return used is not None or remaining is not None


def _claude_last_good_ok(good):
    """Normalize a persisted last_good, or None when it is corrupt / unusable."""
    if not isinstance(good, dict):
        return None
    stamp = good.get("timestamp")
    if not isinstance(stamp, str) or _safe_ts(stamp) is None:
        return None
    metrics = good.get("metrics")
    if not isinstance(metrics, dict):
        return None
    clean_metrics = {}
    for key in ("usage_five_hour", "usage_seven_day"):
        gauge = metrics.get(key)
        if gauge is None:
            continue
        if not _claude_gauge_ok(gauge):
            return None
        clean_metrics[key] = {
            "limit": gauge.get("limit", 100),
            "used": gauge.get("used"),
            "remaining": gauge.get("remaining"),
        }
    if not clean_metrics:
        return None
    resets = good.get("resets")
    if resets is None:
        resets = {}
    if not isinstance(resets, dict):
        return None
    clean_resets = {}
    for key, value in resets.items():
        if not _claude_resets_at_ok(value):
            return None
        clean_resets[key] = value
    return {"timestamp": stamp, "metrics": clean_metrics, "resets": clean_resets}


def _claude_limit_row(row):
    """Map one /usage limits row to (metric_key, reset_key, used, resets_at) or None."""
    if not isinstance(row, dict):
        return None
    kind = row.get("kind")
    if kind == "session":
        metric, reset = "usage_five_hour", "usage_five_hour"
    elif kind == "weekly_all":
        metric, reset = "usage_seven_day", "usage_seven_day"
    else:
        return None
    used = row.get("percent")
    if not _claude_finite_number(used) or used < 0:
        return None
    resets_at = row.get("resets_at")
    if not _claude_resets_at_ok(resets_at):
        return None
    return metric, reset, used, resets_at


def _parse_claude_usage(stdout):
    """Build an openusage-shaped claude_code snapshot from stream-json /usage output."""
    report = None
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if not isinstance(message, dict) or message.get("type") != "assistant":
            continue
        if "usage_report" in message:
            report = message.get("usage_report")
    if not isinstance(report, dict):
        raise _ReaderError("claude /usage report missing")
    rate_limits = report.get("rate_limits")
    if rate_limits is None:
        raise _ReaderError("claude /usage unavailable")
    if not isinstance(rate_limits, dict):
        raise _ReaderError("claude /usage unavailable")
    limits = rate_limits.get("limits")
    if not isinstance(limits, list):
        raise _ReaderError("claude /usage unavailable")
    snap = {"provider_id": "claude_code", "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": "ok", "metrics": {}, "resets": {}}
    for row in limits:
        parsed = _claude_limit_row(row)
        if parsed is None:
            continue
        metric, reset, used, resets_at = parsed
        snap["metrics"][metric] = {"limit": 100, "used": used, "remaining": 100 - used}
        snap["resets"][reset] = resets_at
    if not snap["metrics"]:
        raise _ReaderError("claude /usage unavailable")
    return snap


def _claude_call(timeout, cwd):
    """One bounded `claude -p /usage` read; kill the process group on the way out."""
    deadline = time.monotonic() + min(20, timeout)
    env = _claude_env()
    proc = subprocess.Popen(
        ["claude", *CLAUDE_USAGE_ARGS], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=str(cwd), env=env, start_new_session=True, bufsize=0)
    pending = b""
    try:
        os.set_blocking(proc.stdout.fileno(), False)
        os.set_blocking(proc.stderr.fileno(), False)
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            selector.register(proc.stderr, selectors.EVENT_READ)
            stderr_size = 0

            def take(stream, chunk):
                nonlocal pending, stderr_size
                if stream is proc.stdout:
                    pending += chunk
                    if len(pending) > 1024 * 1024:
                        raise _ReaderError("claude /usage reply too large")
                else:
                    stderr_size += len(chunk)
                    if stderr_size > 1024 * 1024:
                        raise _ReaderError("claude /usage reply too large")

            def drain():
                # After the child exits, pull every buffered byte even if a grandchild
                # still holds the write end (select would otherwise wait until EOF).
                for key in list(selector.get_map().values()):
                    while True:
                        try:
                            chunk = os.read(key.fd, 65536)
                        except BlockingIOError:
                            break
                        if not chunk:
                            selector.unregister(key.fileobj)
                            break
                        take(key.fileobj, chunk)

            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired("claude /usage", timeout)
                # Short select so a dead child is noticed while pipes stay open.
                if selector.get_map():
                    events = selector.select(min(remaining, 0.05))
                else:
                    events = []
                    time.sleep(min(0.05, remaining))
                for key, _ in events:
                    try:
                        chunk = os.read(key.fd, 65536)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    take(key.fileobj, chunk)
                if proc.poll() is not None:
                    drain()
                    break
            if proc.returncode:
                raise subprocess.CalledProcessError(proc.returncode, "claude /usage")
            try:
                text = pending.decode("utf-8")
            except UnicodeError as exc:
                raise _ReaderError("claude /usage unavailable") from exc
            return _parse_claude_usage(text)
    finally:
        try:
            proc.wait(timeout=min(0.05, max(0, deadline - time.monotonic())))
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            pass
        proc.stdout.close()
        proc.stderr.close()


def _claude_carry_from_state(state, reason):
    good = _claude_last_good_ok(state.get("last_good"))
    if good is None:
        return None
    return ({"provider_id": "claude_code", "timestamp": good["timestamp"], "status": "ok",
             "metrics": good["metrics"], "resets": good["resets"]},
            reason)


def _claude_wait_s(failures):
    """Seconds to wait after last_attempt before another call."""
    if failures <= 0:
        return CLAUDE_MIN_INTERVAL_S
    backoff = CLAUDE_BACKOFF_S[min(failures, len(CLAUDE_BACKOFF_S)) - 1]
    return max(CLAUDE_MIN_INTERVAL_S, backoff)


def _claude_snapshot(timeout=20, log=None):
    """Return (snapshot|None, carried_reason|None, error|None) under a private lock."""
    state_path = _claude_state_path(log)
    lock_path = Path(str(state_path) + ".lock")
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    lock = None
    try:
        try:
            lock = lock_path.open("a")
            fcntl.flock(lock, fcntl.LOCK_EX)
        except OSError:
            if lock is not None:
                try:
                    lock.close()
                except OSError:
                    pass
            lock = None

        # Load and save whether or not the lock was taken: a missing lock must not
        # mean an unthrottled reader. _save_claude_state is atomic; races can lose
        # one attempt stamp, which is better than no throttle at all.
        state = _load_claude_state(state_path)
        now = datetime.now(timezone.utc)
        last_attempt = _safe_ts(state.get("last_attempt_at"))
        failures = state.get("failures")
        if not isinstance(failures, int) or failures < 0:
            failures = 0
        last_good = _claude_last_good_ok(state.get("last_good"))
        # A stamp in the future (clock change) counts as no throttle.
        if last_attempt is not None and last_attempt <= now:
            wait_s = _claude_wait_s(failures)
            skip_reason = "backoff" if failures > 0 else "throttle"
            if (now - last_attempt).total_seconds() < wait_s:
                if last_good is not None:
                    return ({"provider_id": "claude_code", "timestamp": last_good["timestamp"],
                             "status": "ok", "metrics": last_good["metrics"],
                             "resets": last_good["resets"]}, skip_reason, None)
                return (None, None, "claude /usage unavailable")

        attempt = {"last_attempt_at": now.isoformat(), "failures": failures,
                   "last_good": last_good}
        try:
            # Mark the attempt before the call so a kill mid-read still throttles.
            _save_claude_state(state_path, attempt)
        except OSError:
            pass
        try:
            snap = _claude_call(timeout, state_path.parent)
        except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError,
                KeyError, OverflowError) as exc:
            error = _fetch_error(exc, "claude /usage")
            attempt["failures"] = failures + 1
            try:
                _save_claude_state(state_path, attempt)
            except OSError:
                pass
            carried = _claude_carry_from_state(attempt, "failed")
            if carried:
                return (*carried, None)
            return None, None, error
        # Persist only gauges and resets: never session/account fields from the report.
        good = _claude_last_good_ok({"timestamp": snap["timestamp"],
                                     "metrics": snap["metrics"], "resets": snap["resets"]})
        try:
            _save_claude_state(state_path, {
                "last_attempt_at": attempt["last_attempt_at"], "failures": 0,
                "last_good": good})
        except OSError:
            pass
        return snap, None, None
    finally:
        if lock is not None:
            try:
                lock.close()
            except OSError:
                pass


def _strip_ansi(data: bytes) -> str:
    """Replace each ANSI escape sequence with one space."""
    return _ANSI_RE.sub(b" ", data).decode("utf-8", errors="replace")


def _screen_lines(data: bytes) -> list:
    """Replay the TUI's line redraws (erase, cursor up/down, column) into screen lines.

    Absolute moves, screen clears and scrolls start a fresh screen, so an unknown
    redraw style hides old rows instead of keeping them.
    """
    text = data.decode("utf-8", errors="replace")
    lines, row, col = [""], 0, 0

    def put(chars):
        nonlocal col
        line = lines[row]
        if len(line) < col:
            line += " " * (col - len(line))
        lines[row] = line[:col] + chars + line[col + len(chars):]
        col += len(chars)

    def plain(chunk):
        nonlocal row, col
        run = []
        for ch in chunk:
            if ch >= " " and ch != "\x7f":
                run.append(ch)
                continue
            if run:
                put("".join(run))
                run = []
            if ch == "\r":
                col = 0
            elif ch == "\n":
                row += 1
                while len(lines) <= row:
                    lines.append("")
        if run:
            put("".join(run))

    pos = 0
    for match in _TERM_SEQ_RE.finditer(text):
        plain(text[pos:match.start()])
        pos = match.end()
        final, params = match.group(2), match.group(1) or ""
        if final is None or params[:1] in ("?", ">", "<", "="):
            continue
        try:
            count = int(params.split(";")[0] or 0)
        except ValueError:
            count = 0
        step = max(count, 1)
        if final == "A":
            row = max(0, row - step)
        elif final == "B":
            row += step
            while len(lines) <= row:
                lines.append("")
        elif final == "C":
            col += step
        elif final == "D":
            col = max(0, col - step)
        elif final == "G":
            col = step - 1
        elif final == "K":
            if count == 2:
                lines[row] = ""
            elif count == 1:  # erase to the left, through the cursor cell
                lines[row] = " " * (col + 1) + lines[row][col + 1:]
            else:
                lines[row] = lines[row][:col]
        elif final == "J" and count == 0:
            lines[row] = lines[row][:col]
            del lines[row + 1:]
        elif final in "EFHJSTf":
            lines, row, col = [""], 0, 0
    plain(text[pos:])
    return lines


def _cursor_usage_selected(data: bytes) -> bool:
    """True when the current screen shows "/usage" typed, its menu row selected, no prompt."""
    lines = [line.rstrip() for line in _screen_lines(data)]
    return (any(_CURSOR_INPUT_RE.fullmatch(line) for line in lines)
            and any(_CURSOR_ROW_RE.fullmatch(line) for line in lines)
            and not any(_CURSOR_PROMPT_RE.search(line) for line in lines))


def _cursor_reset(text: str, now: datetime) -> datetime:
    """Map a screen date ('18 Nov' or 'Nov 18') to 00:00:00 UTC within the window."""
    if not isinstance(text, str):
        raise _ReaderError("cursor /usage unavailable")
    raw = text.strip()
    match = re.fullmatch(r"(\d{1,2})\s+([A-Za-z]{3})", raw)
    if match:
        day, month_name = int(match.group(1)), match.group(2)
    else:
        match = re.fullmatch(r"([A-Za-z]{3})\s+(\d{1,2})", raw)
        if not match:
            raise _ReaderError("cursor /usage unavailable")
        month_name, day = match.group(1), int(match.group(2))
    month = _CURSOR_MONTHS.get(month_name)
    if month is None:
        raise _ReaderError("cursor /usage unavailable")
    # Floor at midnight of (now - 3 days) so a lagging "30 Dec" on 2 Jan 10:00 still fits.
    lo = (now - timedelta(days=3)).replace(hour=0, minute=0, second=0, microsecond=0)
    hi = now + timedelta(days=35)
    candidates = []
    for year in (now.year - 1, now.year, now.year + 1):
        try:
            stamp = datetime(year, month, day, tzinfo=timezone.utc)
        except ValueError:
            continue
        if lo <= stamp <= hi:
            candidates.append(stamp)
    if not candidates:
        raise _ReaderError("cursor /usage unavailable")
    return candidates[0]


def _parse_cursor_screen(text: str, now: datetime) -> dict:
    """Build a cursor snapshot from stripped /usage screen text after Enter."""
    if not isinstance(text, str):
        raise _ReaderError("cursor /usage unavailable")
    auto = re.search(r"Auto\s+(\d+)%\s+used", text)
    api = re.search(r"API\s+(\d+)%\s+used", text)
    resets = re.search(r"Resets\s+(\d{1,2}\s+[A-Za-z]{3}|[A-Za-z]{3}\s+\d{1,2})", text)
    if not auto or not api or not resets:
        raise _ReaderError("cursor /usage unavailable")
    try:
        auto_used = int(auto.group(1))
        api_used = int(api.group(1))
    except (TypeError, ValueError):
        raise _ReaderError("cursor /usage unavailable") from None
    if not (0 <= auto_used <= 100 and 0 <= api_used <= 100):
        raise _ReaderError("cursor /usage unavailable")
    reset = _cursor_reset(resets.group(1), now)
    return {
        "provider_id": "cursor",
        "timestamp": now.isoformat(),
        "status": "ok",
        "whole_percent": True,
        "metrics": {
            "plan_auto_percent_used": {
                "limit": 100, "used": auto_used, "remaining": 100 - auto_used},
            "plan_api_percent_used": {
                "limit": 100, "used": api_used, "remaining": 100 - api_used},
        },
        "resets": {"billing_cycle_end": reset.isoformat()},
    }


def _cursor_state_path(log=None):
    return log_path(log).parent / "cursor-usage.json"


def _cursor_workspace():
    path = _cache_path() / "claude-delegate/cursor-usage-ws"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def _cursor_last_good_ok(good):
    """Normalize a persisted Cursor last_good, or None when corrupt / unusable."""
    if not isinstance(good, dict):
        return None
    stamp = good.get("timestamp")
    if not isinstance(stamp, str) or _safe_ts(stamp) is None:
        return None
    metrics = good.get("metrics")
    if not isinstance(metrics, dict):
        return None
    clean_metrics = {}
    for key in ("plan_auto_percent_used", "plan_api_percent_used"):
        gauge = metrics.get(key)
        if gauge is None:
            continue
        if not _claude_gauge_ok(gauge):
            return None
        clean_metrics[key] = {
            "limit": gauge.get("limit", 100),
            "used": gauge.get("used"),
            "remaining": gauge.get("remaining"),
        }
    if "plan_auto_percent_used" not in clean_metrics or "plan_api_percent_used" not in clean_metrics:
        return None
    resets = good.get("resets")
    if not isinstance(resets, dict):
        return None
    end = resets.get("billing_cycle_end")
    if not _claude_resets_at_ok(end) or end is None:
        return None
    return {"timestamp": stamp, "metrics": clean_metrics,
            "resets": {"billing_cycle_end": end}}


def _cursor_carry_from_state(state, reason):
    good = _cursor_last_good_ok(state.get("last_good"))
    if good is None:
        return None
    return ({"provider_id": "cursor", "timestamp": good["timestamp"], "status": "ok",
             "whole_percent": True, "metrics": good["metrics"], "resets": good["resets"]},
            reason)


def _cursor_call(timeout, workspace):
    """One bounded pty read of cursor-agent's interactive /usage screen."""
    # Import inside the function: hooks load quota via spec_from_file_location
    # without the scripts dir on sys.path (same pattern as check_dispatch).
    scripts = Path(__file__).resolve().parent
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import ask as ask_mod  # noqa: E402

    deadline = time.monotonic() + min(30, timeout)
    env = ask_mod.clean_env()
    env["TERM"] = "xterm-256color"
    env["LANG"] = "en_GB.UTF-8"
    env["LC_ALL"] = "en_GB.UTF-8"
    ws = str(workspace)
    master, slave = pty.openpty()
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 45, 130, 0, 0))
        os.set_blocking(master, False)
        proc = subprocess.Popen(
            ["cursor-agent", "--mode", "ask", "--trust", "--workspace", ws],
            stdin=slave, stdout=slave, stderr=slave,
            cwd=ws, env=env, start_new_session=True, close_fds=True)
    except Exception:
        try:
            os.close(master)
        except OSError:
            pass
        try:
            os.close(slave)
        except OSError:
            pass
        raise
    try:
        try:
            os.close(slave)
        except OSError:
            pass
        slave = -1
        collected = b""
        step_mark = 0
        closed = False  # the child is gone: no wait can succeed, so don't spin to the deadline

        def remaining():
            return deadline - time.monotonic()

        def read_more(limit_s=None):
            nonlocal collected, closed
            end = time.monotonic() + (remaining() if limit_s is None else min(limit_s, remaining()))
            while time.monotonic() < end:
                wait = min(0.05, end - time.monotonic())
                if wait <= 0:
                    break
                try:
                    ready, _, _ = select.select([master], [], [], wait)
                except (OSError, ValueError):
                    break
                if not ready:
                    continue
                try:
                    chunk = os.read(master, 65536)
                except BlockingIOError:
                    continue
                except OSError:
                    closed = True
                    return
                if not chunk:
                    closed = True
                    return
                collected += chunk
                if len(collected) > 1024 * 1024:
                    raise _ReaderError("cursor /usage reply too large")

        def text_since(mark):
            return _strip_ansi(collected[mark:])

        def wait_until(check):
            while remaining() > 0:
                if check():
                    return
                if closed:
                    break
                read_more(min(0.05, remaining()))
            raise _ReaderError("cursor /usage unavailable")

        def wait_for(pattern):
            nonlocal step_mark
            step_mark = len(collected)
            compiled = re.compile(pattern)
            wait_until(lambda: compiled.search(text_since(step_mark)))

        def write_bytes(data):
            view = memoryview(data)
            while view:
                if remaining() <= 0:
                    raise subprocess.TimeoutExpired("cursor /usage", timeout)
                try:
                    count = os.write(master, view)
                    view = view[count:]
                except BlockingIOError:
                    read_more(0.05)
                except OSError as exc:
                    raise _ReaderError("cursor /usage unavailable") from exc

        # 1. Wait for the ready banner; type nothing before it appears.
        wait_for(r"build\s*anything")
        # 2. Type /usage one character at a time, 50 ms apart.
        for index, char in enumerate("/usage"):
            write_bytes(char.encode())
            if index + 1 < len("/usage"):
                pause_until = time.monotonic() + 0.05
                while time.monotonic() < pause_until and remaining() > 0:
                    read_more(min(0.05, pause_until - time.monotonic(), remaining()))
        # 3. Judge the current screen, never accumulated text: "/usage" typed, its row
        #    selected, no restored prompt, and still so after a 100 ms settle, since a
        #    later frame can clear the menu.
        def usage_selected():
            return _cursor_usage_selected(collected)

        while True:
            wait_until(usage_selected)
            read_more(min(0.1, remaining()))
            if usage_selected():
                break
        # 4. Enter only once that row is selected.
        write_bytes(b"\r")
        # 5. Wait for the panel, then allow meters a short grace period.
        wait_for(r"Esc to close")
        if not (re.search(r"Auto\s+\d+%\s+used", text_since(step_mark))
                and re.search(r"API\s+\d+%\s+used", text_since(step_mark))
                and re.search(r"Resets\s+", text_since(step_mark))):
            read_more(min(2.0, remaining()))
        return _parse_cursor_screen(text_since(step_mark), datetime.now(timezone.utc))
    finally:
        try:
            os.write(master, b"\x1b")
        except OSError:
            pass
        for _ in range(3):
            try:
                os.write(master, b"\x03")
            except OSError:
                break
            time.sleep(0.02)
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            proc.wait(timeout=min(0.5, max(0.05, deadline - time.monotonic() + 0.5)))
        except subprocess.TimeoutExpired:
            pass
        try:
            os.close(master)
        except OSError:
            pass
        if slave >= 0:
            try:
                os.close(slave)
            except OSError:
                pass


def _cursor_snapshot(timeout=30, log=None):
    """Return (snapshot|None, carried_reason|None, error|None) under a private lock."""
    state_path = _cursor_state_path(log)
    lock_path = Path(str(state_path) + ".lock")
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    lock = None
    try:
        try:
            lock = lock_path.open("a")
            fcntl.flock(lock, fcntl.LOCK_EX)
        except OSError:
            if lock is not None:
                try:
                    lock.close()
                except OSError:
                    pass
            lock = None
            # No lock, no cadence guarantee: carry instead of launching the TUI.
            carried = _cursor_carry_from_state(_load_claude_state(state_path), "failed")
            if carried:
                return (*carried, None)
            return None, None, "cursor /usage unavailable"

        state = _load_claude_state(state_path)
        now = datetime.now(timezone.utc)
        last_attempt = _safe_ts(state.get("last_attempt_at"))
        failures = state.get("failures")
        if not isinstance(failures, int) or failures < 0:
            failures = 0
        last_good = _cursor_last_good_ok(state.get("last_good"))
        # A stamp in the future (clock change) counts as no throttle.
        if last_attempt is not None and last_attempt <= now:
            skip_reason = "backoff" if failures > 0 else "throttle"
            if (now - last_attempt).total_seconds() < CURSOR_MIN_INTERVAL_S:
                if last_good is not None:
                    return ({"provider_id": "cursor", "timestamp": last_good["timestamp"],
                             "status": "ok", "whole_percent": True,
                             "metrics": last_good["metrics"],
                             "resets": last_good["resets"]}, skip_reason, None)
                return (None, None, "cursor /usage unavailable")

        attempt = {"last_attempt_at": now.isoformat(), "failures": failures,
                   "last_good": last_good}
        try:
            # Mark the attempt before the call so a kill mid-read still throttles.
            _save_claude_state(state_path, attempt)
        except OSError:
            # An unsaved stamp would let the next call read again at once.
            carried = _cursor_carry_from_state(attempt, "failed")
            if carried:
                return (*carried, None)
            return None, None, "cursor /usage unavailable"
        try:
            snap = _cursor_call(timeout, _cursor_workspace())
        except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError,
                KeyError, OverflowError) as exc:
            error = _fetch_error(exc, "cursor /usage")
            attempt["failures"] = failures + 1
            try:
                _save_claude_state(state_path, attempt)
            except OSError:
                pass
            carried = _cursor_carry_from_state(attempt, "failed")
            if carried:
                return (*carried, None)
            return None, None, error
        good = _cursor_last_good_ok({"timestamp": snap["timestamp"],
                                     "metrics": snap["metrics"], "resets": snap["resets"]})
        try:
            _save_claude_state(state_path, {
                "last_attempt_at": attempt["last_attempt_at"], "failures": 0,
                "last_good": good})
        except OSError:
            pass
        return snap, None, None
    finally:
        if lock is not None:
            try:
                lock.close()
            except OSError:
                pass


def _hybrid_snapshot(timeout, log=None):
    snapshots, errors, carried = [], {}, {}
    try:
        snapshots.append(_codex_snapshot(timeout=min(15, timeout)))
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError, KeyError, OverflowError) as exc:
        errors["codex"] = _fetch_error(exc, "codex app-server")
    try:
        snap, reason, error = _claude_snapshot(timeout=min(20, timeout), log=log)
        if snap is not None:
            snapshots.append(snap)
            if reason:
                carried["claude_code"] = reason
        elif error:
            errors["claude_code"] = error
        else:
            errors["claude_code"] = "claude /usage unavailable"
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError, KeyError, OverflowError) as exc:
        errors["claude_code"] = _fetch_error(exc, "claude /usage")
    try:
        snap, reason, error = _cursor_snapshot(timeout=min(30, timeout), log=log)
        if snap is not None:
            snapshots.append(snap)
            if reason:
                carried["cursor"] = reason
        elif error:
            errors["cursor"] = error
        else:
            errors["cursor"] = "cursor /usage unavailable"
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError, KeyError, OverflowError) as exc:
        errors["cursor"] = _fetch_error(exc, "cursor /usage")
    envelope = {"snapshots": snapshots, "_reader_errors": errors, "_carried": carried}
    # A provider present with empty metrics is not a usable reading.
    entries = pools(envelope)
    if all("error" in entry for entry in entries.values()):
        parts = [errors[key] for key in ("codex", "claude_code", "cursor") if key in errors]
        if not parts:
            parts = [entry["error"] for entry in entries.values() if entry.get("error")]
        raise _ReaderError("; ".join(parts) if parts else "quota readers failed")
    return envelope


def _fetch_snapshot(source, timeout, log=None):
    if source == "hybrid":
        return _hybrid_snapshot(timeout, log=log)
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


def _reuse_pools(entries, ts):
    """Copy pools; recompute stale for carried Claude gauges against the new row time."""
    result = {}
    for pid, entry in entries.items():
        if not isinstance(entry, dict):
            result[pid] = entry
            continue
        copied = dict(entry)
        if copied.get("carried") and "used_pct" in copied:
            observed_ts = _safe_ts(copied.get("observed_at"))
            copied["stale"] = (observed_ts is None
                               or (ts - observed_ts).total_seconds() > 120)
        result[pid] = copied
    return result


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
               "reader": HYBRID_READER if source == "hybrid" else f"openusage-{source}",
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
                row.update(pools=_reuse_pools(previous["pools"], ts),
                           reader=previous["reader"], reused_from=origin)
            else:
                row["pools"] = _snapshot_pools(_fetch_snapshot(source, timeout, log=log), ts)
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
    if not isinstance(entry, dict) or "used_pct" not in entry:
        return None
    if entry.get("stale"):
        # Cursor's 30-minute cadence marks nearly every row stale; keep throttle
        # carries on the spare line, but not failed/backoff carries.
        if entry.get("carried") != "throttle":
            return None
        observed_ts = _safe_ts(entry.get("observed_at"))
        if observed_ts is None:
            return None
        if (reading_ts - observed_ts).total_seconds() > CURSOR_MIN_INTERVAL_S + 300:
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

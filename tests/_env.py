"""Isolate the unittest suite from the caller's real home, agents and network.

Every tests/test*.py module must start with `import _env` (after an optional
docstring and `from __future__ import ...`). Some script modules fix paths at
import time (runs.RUNS_ROOT, ratings.CACHE).

Applied once per process: a temp HOME/XDG/Claude/Codex tree, recording stubs
for codex/cursor-agent/claude first on PATH, repo knobs cleared, HTTP proxied
at a closed local port, and in-process blocks on non-loopback
connect/connect_ex/getaddrinfo. A stub call or blocked network attempt fails
the process after the tests, prints each recorded line and keeps the temp
root; otherwise the temp root is removed at exit.

Child processes (the delegate CLI and hooks started with subprocess) inherit
only the proxy variables, not the socket patches. UDP sendto and C extensions
that talk to the network without those Python entry points are outside this
guard.
"""
import atexit
import ipaddress
import json
import os
import shutil
import socket
import sys
import tempfile
from pathlib import Path

_APPLIED = False

# Repo knobs. Leave TMPDIR and every GIT_* variable alone.
_UNSET = (
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_SESSION_LOG",
    "SECRET_GUARD_DISABLE",
    "SECRET_GUARD_BUDGET_S",
    "DELEGATE_CATALOG_LOCAL",
    "CLAUDE_SESSION_MODE",
    "CLAUDE_SESSION_MODE_SEED",
    "CLAUDE_DELEGATE_ROOT",
    "VIRTUAL_ENV",
    "SANDBOX_GUARD_DISABLE",
    "DELEGATE_BACKUP_DIRS",
    "CWD_GUARD_DISABLE",
    "CURSOR_WORKSPACE_LABEL",
    "CONTEXT_GUARD_STATE",
    "CONTEXT_GUARD_NUDGE",
    "CONTEXT_GUARD_DISABLE",
    "CONTEXT_GUARD_FIRST",
    "CONTEXT_GUARD_STEP",
    "CONTEXT_GUARD_IDLE_MIN",
    "CONTEXT_GUARD_IDLE_CONTEXT",
    "CONTEXT_GUARD_NUDGE_CALLS",
    "CLAUDE_SESSION_SUMMARY",
    "CLAUDE_SESSION_RULES_DIR",
    "CLAUDE_SESSION_QUOTA",
    "CLAUDE_SESSION_MODE_CONFIG",
    "CLAUDE_PROJECT_DIR",
    "CLAUDE_ORCHESTRATOR_FEEDBACK",
    "CLAUDE_DELEGATE_QUOTA_SNAPSHOT",
    "CLAUDE_DELEGATE_QUOTA_LOG",
    "CLAUDE_CODE_ENTRYPOINT",
    "ATTENTION_DISABLE",
    "ARTIFICIAL_ANALYSIS_API_KEY",
)

_AGENTS = ("codex", "cursor-agent", "claude")
_STUB = '#!/bin/sh\necho "$(basename "$0") $*" >> "{calls}"\nexit 1\n'
_CLOSED_PROXY = "http://127.0.0.1:9"

# Filled by apply(); useful for assertions in the hermetic guard tests.
ROOT = None
CALLS = None
BIN = None

_ORIG_CONNECT = socket.socket.connect
_ORIG_CONNECT_EX = socket.socket.connect_ex
_ORIG_GETADDRINFO = socket.getaddrinfo


def _finish():
    """Fail the process if a stub or network attempt was recorded; else clean up."""
    lines = []
    if CALLS is not None and CALLS.exists():
        lines = [line for line in CALLS.read_text().splitlines() if line.strip()]
    if not lines:
        if ROOT is not None:
            shutil.rmtree(ROOT, ignore_errors=True)
        return
    sys.stdout.flush()
    sys.stderr.write(
        "HERMETIC FAIL: stub or network attempt recorded "
        "(real agent/network must not run):\n"
    )
    for line in lines:
        sys.stderr.write(f"  {line}\n")
    sys.stderr.write(f"  temp root kept: {ROOT}\n")
    sys.stderr.flush()
    # unittest already chose an exit status; override so a leak cannot pass.
    os._exit(1)


def _record(kind, detail):
    """Append a line to CALLS, then raise. The record is what fails the run."""
    line = f"{kind} {detail}"
    if CALLS is not None:
        with open(CALLS, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    raise OSError(f"hermetic blocked: {line}")


def _decode_host(host):
    if host is None:
        return None
    if isinstance(host, bytes):
        return host.decode("utf-8", "surrogateescape")
    return host


def _host_allowed(host):
    """True for AF_UNIX callers (handled separately), loopback, localhost, empty."""
    host = _decode_host(host)
    if host is None or host == "":
        return True
    if host == "localhost":
        return True
    text = host.strip("[]")
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return False
    if ip.is_loopback:
        return True
    mapped = getattr(ip, "ipv4_mapped", None)
    return mapped is not None and mapped.is_loopback


def _address_host_port(address):
    if isinstance(address, (str, bytes)):
        return _decode_host(address), ""
    if not address:
        return None, ""
    host = address[0]
    port = address[1] if len(address) > 1 else ""
    return host, port


def _guarded_connect(self, address):
    if getattr(self, "family", None) == socket.AF_UNIX:
        return _ORIG_CONNECT(self, address)
    host, port = _address_host_port(address)
    if _host_allowed(host):
        return _ORIG_CONNECT(self, address)
    _record("network", f"{_decode_host(host)}:{port}")


def _guarded_connect_ex(self, address):
    if getattr(self, "family", None) == socket.AF_UNIX:
        return _ORIG_CONNECT_EX(self, address)
    host, port = _address_host_port(address)
    if _host_allowed(host):
        return _ORIG_CONNECT_EX(self, address)
    _record("network", f"{_decode_host(host)}:{port}")


def _guarded_getaddrinfo(host, port, *args, **kwargs):
    if _host_allowed(host):
        return _ORIG_GETADDRINFO(host, port, *args, **kwargs)
    _record("network", f"{_decode_host(host)}:{port}")


def _install_network_guard():
    socket.socket.connect = _guarded_connect
    socket.socket.connect_ex = _guarded_connect_ex
    socket.getaddrinfo = _guarded_getaddrinfo


def apply():
    """Poison the process environment once. Safe to call repeatedly."""
    global _APPLIED, ROOT, CALLS, BIN
    if _APPLIED:
        return
    _APPLIED = True

    ROOT = Path(tempfile.mkdtemp(prefix="delegate-hermetic-"))
    dirs = {
        "HOME": ROOT / "home",
        "XDG_CACHE_HOME": ROOT / "cache",
        "XDG_CONFIG_HOME": ROOT / "config",
        "XDG_STATE_HOME": ROOT / "state",
        "CLAUDE_CONFIG_DIR": ROOT / "claude",
        "CODEX_HOME": ROOT / "codex",
    }
    for path in dirs.values():
        path.mkdir(parents=True)
    for key, path in dirs.items():
        os.environ[key] = str(path)

    optin = dirs["XDG_CONFIG_HOME"] / "claude-orchestrator"
    optin.mkdir(parents=True)
    (optin / "catalog.local.json").write_text(
        json.dumps({"cursor_usage_reader": True}) + "\n", encoding="utf-8"
    )

    CALLS = ROOT / "stub-calls"
    BIN = ROOT / "bin"
    BIN.mkdir()
    for name in _AGENTS:
        stub = BIN / name
        stub.write_text(_STUB.format(calls=CALLS), encoding="utf-8")
        stub.chmod(0o755)
    os.environ["PATH"] = f"{BIN}{os.pathsep}{os.environ.get('PATH', '')}"

    for key in _UNSET:
        os.environ.pop(key, None)

    for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                "all_proxy", "ALL_PROXY"):
        os.environ[key] = _CLOSED_PROXY
    for key in ("no_proxy", "NO_PROXY"):
        os.environ.pop(key, None)

    _install_network_guard()
    atexit.register(_finish)


apply()

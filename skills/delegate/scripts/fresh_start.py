"""Median first-call context of recent interactive sessions in a project folder.

Shared by the context-guard hook (importlib) and `delegate cost --session`.
Stdlib only; never imports cost/runs. Any I/O or parse error yields None.
"""
import json
import statistics
from pathlib import Path

MAX_FILES = 20
MAX_BYTES = 4 * 1024 * 1024
MIN_USABLE = 3
MEDIAN_OF = 5


def first_call_context(path, max_bytes=MAX_BYTES):
    """Context tokens of the first main assistant call, or None if unusable/sdk."""
    try:
        with Path(path).open("rb") as stream:
            return _first_call_in(stream, max_bytes)
    except (OSError, TypeError, ValueError):
        return None  # unreadable, or a corrupt usage value: skip this transcript


def _first_call_in(stream, max_bytes):
    """Scan lines until the first main call, reading at most `max_bytes`."""
    first_entrypoint = None
    read = 0
    while read < max_bytes:
        raw = stream.readline(max_bytes - read)  # a line cut at the cap fails json.loads
        if not raw:
            break
        read += len(raw)
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        ep = rec.get("entrypoint")
        if first_entrypoint is None and isinstance(ep, str) and ep:
            first_entrypoint = ep
        if rec.get("type") != "assistant" or rec.get("isSidechain"):
            continue
        msg = rec.get("message")
        if not isinstance(msg, dict) or msg.get("model") == "<synthetic>":
            continue
        usage = msg.get("usage")
        if not isinstance(usage, dict):
            continue
        if first_entrypoint is not None and first_entrypoint.startswith("sdk"):
            return None
        return (int(usage.get("input_tokens") or 0)
                + int(usage.get("cache_read_input_tokens") or 0)
                + int(usage.get("cache_creation_input_tokens") or 0))
    return None


def median_fresh_start(folder, exclude=None, newest=MEDIAN_OF, max_files=MAX_FILES,
                       min_usable=MIN_USABLE):
    """Median first-call context of up to `newest` other sessions, or None.

    Looks at at most `max_files` newest `*.jsonl` files directly in `folder`
    (by mtime). Skips `exclude`, sdk entrypoints, and unreadable transcripts.
    Returns None when fewer than `min_usable` contexts are available.
    """
    folder = Path(folder)
    exclude_resolved = None
    if exclude is not None:
        try:
            exclude_resolved = Path(exclude).resolve()
        except OSError:
            exclude_resolved = Path(exclude)
    try:
        candidates = [p for p in folder.glob("*.jsonl") if p.is_file()]
    except OSError:
        return None
    try:
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    values = []
    for path in candidates[:max_files]:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if exclude_resolved is not None and resolved == exclude_resolved:
            continue
        ctx = first_call_context(path)
        if ctx is None:
            continue
        values.append(ctx)
        if len(values) >= newest:
            break
    if len(values) < min_usable:
        return None
    sample = values[:newest]
    return int(statistics.median(sample))

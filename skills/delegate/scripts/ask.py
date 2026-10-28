#!/usr/bin/env python3
"""One-shot read-only ask: cheap delegate reads files; orchestrator keeps the short answer."""
import argparse
import json
import os
import re
import selectors
import signal
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import runs  # noqa: E402
import quota  # noqa: E402

DEFAULTS = {"cursor": ("auto", None), "codex": ("gpt-6.1-sol", "medium")}


class AskFailed(Exception):
    """A delegate call failed; str() is the stderr tail, .code the exit code."""

    def __init__(self, message, code):
        super().__init__(message)
        self.code = code or 1


class OutsideRead(AskFailed):
    """A confined Cursor call attempted an unobservable or out-of-workspace tool."""

    def __init__(self, message):
        super().__init__(message, 4)


def clean_env():
    """Scrub IDE/AppImage/Claude vars the same way runs.py does for launchers."""
    env = os.environ.copy()
    for k in list(env):
        if k not in runs.KEEP and (k in runs.UNSET_EXACT or any(k.startswith(p) for p in runs.UNSET_PREFIX)):
            del env[k]
    # same PATH hygiene as runs.clean_env_lines: Cursor's AppImage mounts must not shadow tools
    for var in ("PATH", "XDG_DATA_DIRS"):
        if var in env:
            env[var] = os.pathsep.join(p for p in env[var].split(os.pathsep) if p and "/.mount_" not in p)
    return env


def build_prompt(question, max_words, files):
    lines = [
        f"Answer in at most {max_words} words.",
        "Cite path:line for claims.",
        'Say "not found" rather than guess.',
        "This is read-only; do not modify, create or delete any files.",
    ]
    if files:
        lines.append("Focus on these files:")
        for f in files:
            lines.append(f"- {f}")
    lines.append("")
    lines.append(question)
    return "\n".join(lines)


def _tail(text, n=10):
    lines = (text or "").splitlines()
    return "\n".join(lines[-n:])


def _usage_bits(usage):
    if not usage:
        return "in=? out=? cached=?"
    inp = usage.get("input", usage.get("inputTokens", usage.get("input_tokens")))
    out = usage.get("output", usage.get("outputTokens", usage.get("output_tokens")))
    cached = usage.get("cached_input", usage.get("cacheReadTokens",
                     usage.get("cached_input_tokens")))
    parts = []
    parts.append(f"in={inp if inp is not None else '?'}")
    parts.append(f"out={out if out is not None else '?'}")
    parts.append(f"cached={cached if cached is not None else '?'}")
    return " ".join(parts)


def run_cursor(prompt, model, directory, timeout, confine=False):
    if confine:
        return _run_cursor_confined(prompt, model, directory, timeout)
    cmd = [
        "cursor-agent", "-p",
        "--model", model,
        "--mode", "ask",
        "--trust",
        "--workspace", str(directory),
        "--output-format", "json",
        prompt,
    ]
    t0 = time.monotonic()
    try:
        r = subprocess.run(
            cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=timeout, env=clean_env(), cwd=str(directory),
        )
    except subprocess.TimeoutExpired as e:
        err = (e.stderr or "") if isinstance(e.stderr, str) else (e.stderr or b"").decode("utf-8", "replace")
        raise AskFailed(_tail(err) or f"cursor-agent timed out after {timeout}s", 124)
    elapsed = time.monotonic() - t0
    if r.returncode != 0:
        raise AskFailed(_tail(r.stderr) or f"cursor-agent exited {r.returncode}", r.returncode)
    answer, usage = _parse_cursor(r.stdout)
    return answer, usage, elapsed, model


def _inside_cursor_workspace(path, workspace, kind):
    if not isinstance(path, str) or not path or "\0" in path:
        raise OutsideRead(f"cursor-agent: {kind} missing or invalid path")
    if path.startswith("~"):
        raise OutsideRead(f"cursor-agent: {kind} outside workspace: {path}")
    try:
        resolved = os.path.realpath(os.path.join(workspace, path))
    except (OSError, ValueError):
        raise OutsideRead(f"cursor-agent: {kind} invalid path")
    if resolved != workspace and not resolved.startswith(workspace + os.sep):
        raise OutsideRead(f"cursor-agent: {kind} outside workspace: {path}")
    return resolved


def _check_cursor_glob(tool, workspace, subtype):
    kind = "globToolCall"
    args = tool["args"]
    searched = workspace
    if "targetDirectory" in args:
        searched = _inside_cursor_workspace(args["targetDirectory"], workspace, kind)
    pattern = args.get("globPattern")
    if not isinstance(pattern, str) or "\0" in pattern:
        raise OutsideRead(f"cursor-agent: {kind} missing or invalid globPattern")
    if os.path.isabs(pattern) or pattern.startswith("~") or ".." in pattern.split(os.sep):
        raise OutsideRead(f"cursor-agent: {kind} unsafe globPattern: {pattern}")
    if subtype != "completed":
        return
    result = tool.get("result")
    if not isinstance(result, dict) or "error" in result or not isinstance(result.get("success"), dict):
        raise OutsideRead(f"cursor-agent: {kind} missing or invalid success result")
    success = result["success"]
    files = success.get("files")
    if not isinstance(files, list):
        raise OutsideRead(f"cursor-agent: {kind} missing or invalid files list")
    for path in files:
        _inside_cursor_workspace(path, searched, kind)
    path = success.get("path", "")
    if path != "":
        _inside_cursor_workspace(path, workspace, kind)


def _cursor_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate stream JSON key")
        obj[key] = value
    return obj


def _cursor_constant(value):
    raise ValueError("non-JSON stream constant")


def _check_cursor_event(line, workspace):
    try:
        event = json.loads(line, object_pairs_hook=_cursor_object, parse_constant=_cursor_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise OutsideRead("cursor-agent: invalid stream JSON")
    if not isinstance(event, dict) or event.get("type") not in (
            "system", "user", "assistant", "thinking", "tool_call", "result"):
        raise OutsideRead("cursor-agent: unknown stream event type")
    if event["type"] != "tool_call":
        return event
    call = event.get("tool_call")
    if not isinstance(call, dict):
        raise OutsideRead("cursor-agent: missing or invalid tool_call")
    kinds = [k for k in call if k.endswith("ToolCall")]
    if len(kinds) != 1:
        names = ", ".join(kinds) or "none"
        raise OutsideRead(f"cursor-agent: expected one ToolCall kind, got {names}")
    kind = kinds[0]
    if event.get("subtype") not in ("started", "completed"):
        raise OutsideRead(f"cursor-agent: {kind} unknown tool event subtype")
    if kind not in ("readToolCall", "grepToolCall", "globToolCall", "getMcpToolsToolCall"):
        raise OutsideRead(f"cursor-agent: {kind} forbidden tool")
    tool = call[kind]
    if not isinstance(tool, dict) or not isinstance(tool.get("args"), dict):
        raise OutsideRead(f"cursor-agent: {kind} missing or invalid args")
    args = tool["args"]
    if kind in ("readToolCall", "grepToolCall"):
        _inside_cursor_workspace(args.get("path"), workspace, kind)
    elif kind == "globToolCall":
        _check_cursor_glob(tool, workspace, event["subtype"])
    return event


def _cursor_stream(stdout, workspace, deadline):
    pending, result = b"", None
    with selectors.DefaultSelector() as selector:
        selector.register(stdout, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise subprocess.TimeoutExpired("cursor-agent", 0)
            chunk = os.read(stdout.fileno(), 65536)
            pending += chunk
            lines = pending.split(b"\n")
            pending = lines.pop() if chunk else b""
            for line in lines:
                if line.strip():
                    event = _check_cursor_event(line, workspace)
                    if event["type"] == "result":
                        result = event
            if not chunk:
                return result


def _stop_cursor(process):
    pgid = process.pid
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    finally:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait()


def _run_cursor_confined(prompt, model, directory, timeout):
    cmd = [
        "cursor-agent", "-p",
        "--model", model,
        "--mode", "ask",
        "--trust",
        "--workspace", str(directory),
        "--output-format", "stream-json",
        prompt,
    ]
    workspace = os.path.realpath(directory)
    t0 = time.monotonic()
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as stderr:
        with subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=stderr, env=clean_env(), cwd=str(directory),
                              start_new_session=True) as process:
            try:
                result = _cursor_stream(process.stdout, workspace, t0 + timeout)
                process.wait(timeout=max(0, timeout - (time.monotonic() - t0)))
            except subprocess.TimeoutExpired:
                _stop_cursor(process)
                stderr.seek(0)
                raise AskFailed(_tail(stderr.read()) or f"cursor-agent timed out after {timeout}s", 124)
            finally:
                _stop_cursor(process)
            elapsed = time.monotonic() - t0
            if process.returncode != 0:
                stderr.seek(0)
                raise AskFailed(_tail(stderr.read()) or f"cursor-agent exited {process.returncode}",
                                process.returncode)
    if result is None:
        raise AskFailed("cursor-agent: missing result event", 1)
    if result.get("is_error"):
        raise AskFailed("cursor-agent: result reported an error", 1)
    answer, usage = _parse_cursor(json.dumps(result))
    return answer, usage, elapsed, model


def _parse_cursor(stdout):
    text = (stdout or "").strip()
    if not text:
        return "", None
    # Prefer the last JSON object (stream-json / multi-line noise).
    obj = None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        for line in reversed(text.splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
    if not obj:
        return text, None
    answer = obj.get("result")
    if answer is None:
        answer = obj.get("text") or ""
    usage = obj.get("usage")
    if usage is None and isinstance(obj.get("result"), dict):
        usage = obj["result"].get("usage")
    # Normalise Cursor keys → compact footer
    if usage:
        usage = {
            "input": usage.get("inputTokens", usage.get("input")),
            "output": usage.get("outputTokens", usage.get("output")),
            "cached_input": usage.get("cacheReadTokens", usage.get("cached_input")),
        }
    return str(answer).strip(), usage


def run_codex(prompt, model, effort, directory, timeout):
    with tempfile.NamedTemporaryFile(prefix="delegate-ask-", suffix=".md",
                                     delete=False, mode="w", encoding="utf-8") as tf:
        out_path = Path(tf.name)
    cmd = [
        "codex", "exec",
        "-m", model,
        "-c", f'model_reasoning_effort="{effort}"',
        "-s", "read-only",
        "--skip-git-repo-check",
        "-C", str(directory),
        "--json",
        "-o", str(out_path),
        "-",
    ]
    t0 = time.monotonic()
    try:
        r = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True,
            timeout=timeout, env=clean_env(), cwd=str(directory),
        )
    except subprocess.TimeoutExpired as e:
        err = (e.stderr or "") if isinstance(e.stderr, str) else (e.stderr or b"").decode("utf-8", "replace")
        try:
            out_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise AskFailed(_tail(err) or f"codex timed out after {timeout}s", 124)
    elapsed = time.monotonic() - t0
    try:
        answer = out_path.read_text(encoding="utf-8").strip() if out_path.is_file() else ""
    finally:
        try:
            out_path.unlink(missing_ok=True)
        except OSError:
            pass
    if r.returncode != 0:
        raise AskFailed(_tail(r.stderr) or f"codex exited {r.returncode}", r.returncode)
    usage = _parse_codex_usage(r.stdout)
    return answer, usage, elapsed, model


def _parse_codex_usage(stdout):
    """Sum turn.completed.usage; Codex input_tokens includes cached_input_tokens."""
    totals = {"input": 0, "output": 0, "cached_input": 0}
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        # Events may be nested under type/item
        typ = obj.get("type") or obj.get("event")
        usage = None
        if typ == "turn.completed" or typ == "event" and obj.get("event") == "turn.completed":
            usage = obj.get("usage") or (obj.get("item") or {}).get("usage")
        if usage is None and "usage" in obj and "input_tokens" in (obj.get("usage") or {}):
            # bare usage object on a completed turn line
            if typ and "turn" in str(typ) and "completed" in str(typ):
                usage = obj["usage"]
        if not usage:
            # walk one level for {type: turn.completed, usage: ...}
            item = obj.get("item") or {}
            if item.get("type") == "turn.completed" or obj.get("type") == "turn.completed":
                usage = obj.get("usage") or item.get("usage")
        if not usage:
            continue
        cached = int(usage.get("cached_input_tokens") or 0)
        raw_in = int(usage.get("input_tokens") or 0)
        totals["input"] += max(0, raw_in - cached)
        totals["cached_input"] += cached
        totals["output"] += int(usage.get("output_tokens") or 0)
    if totals["input"] == 0 and totals["output"] == 0 and totals["cached_input"] == 0:
        return None
    return totals


def _run_one(agent, prompt, a, directory):
    model = a.model or DEFAULTS[agent][0]
    if agent == "cursor":
        return run_cursor(prompt, model, directory, a.timeout)
    effort = a.effort or DEFAULTS[agent][1] or "medium"
    return run_codex(prompt, model, effort, directory, a.timeout)


def _footer(agent, model, elapsed, usage):
    return f"({agent} {model} · {elapsed:.0f}s · {_usage_bits(usage)})"


def _normalize_pick_prefix(line):
    """Strip leading markdown list/heading markers and ** / __ emphasis for Pick: matching."""
    s = line
    while True:
        stripped = s.lstrip(" \t")
        if stripped != s:
            s = stripped
            continue
        if s and s[0] in "#>-*":
            s = s[1:]
            continue
        break
    return s.replace("**", "").replace("__", "").strip()


def extract_pick(answer):
    """First Pick: value in answer, or None. Tolerates markdown wrappers around Pick,
    and a Pick glued to a preamble sentence (Cursor's result joins its messages)."""
    if not answer:
        return None
    for line in answer.splitlines():
        s = _normalize_pick_prefix(line)
        m = re.match(r"(?i)^pick\s*:(.*)$", s) or re.search(r"[.!?]\s*Pick\s*:(.*)$", s)
        if m:
            return m.group(1).strip()
    return None


def _fallback_for_both(a):
    """Only the cheap quota read runs on a healthy both call."""
    if getattr(a, "no_fallback", False):
        return None
    check = quota.check_dispatch(SCRIPTS / "catalog.json")
    if check["error"] is not None:
        print(f"ask: Codex quota unknown ({check['error']}); keeping Codex.", flush=True)
        return None
    catalog, threshold = check["catalog"], check["threshold"]
    empty, unknown = [], []
    for pid, entry in check["pools"].items():
        if entry["state"] == "unknown":
            unknown.append(pid)
        elif entry["state"] == "low":
            empty.append((pid, entry["left_pct"]))
    if not empty:
        if unknown:
            print(f"ask: Codex quota unknown ({', '.join(unknown)}); keeping Codex.", flush=True)
        return None
    trigger = ", ".join(f"{pid} {left:g}% left" for pid, left in empty)
    prefix = f"ask: {trigger} below {threshold:g}% threshold; "
    # The expensive discovery and ratings load are deliberately below the trigger.
    import frontier
    half = (DEFAULTS["codex"][0], a.effort or DEFAULTS["codex"][1])
    try:
        report = frontier.load_report(catalog, default_codex=half)
    except Exception as exc:
        print(prefix + f"keeping Codex: fallback ratings unavailable ({exc}).", flush=True)
        return None
    for warning in report["warnings"]:
        print(f"ask: {warning}", file=sys.stderr)
    selection = report["fallback"]
    if not selection:
        print(prefix + f"keeping Codex: {report['fallback_reason']}.", flush=True)
        return None
    print(prefix + "fallback " + frontier.fallback_description(selection), flush=True)
    return selection["row"]


def cmd_ask(a):
    question = a.question
    if question == "-":
        question = sys.stdin.read()
    question = (question or "").strip()
    if not question:
        sys.exit("ask: empty question")
    directory = Path(a.dir).resolve() if a.dir else Path.cwd()
    agent = a.agent or "cursor"
    if agent not in DEFAULTS and agent != "both":
        sys.exit(f"unknown agent {agent!r}")
    if agent == "both" and a.model:
        sys.exit("ask: --model needs a single --agent")
    prompt = build_prompt(question, a.max_words, a.files or [])
    runs.log_delegation("ask", agent=agent)
    if agent != "both":
        try:
            answer, usage, elapsed, model = _run_one(agent, prompt, a, directory)
        except AskFailed as e:
            print(str(e), file=sys.stderr)
            sys.exit(e.code)
        print(answer)
        print(_footer(agent, model, elapsed, usage))
        return
    # Second opinions: same prompt to both vendors in parallel, one block each.
    # A failure in one still prints the other's answer.
    fallback = _fallback_for_both(a)
    agents = ["codex", "cursor"]
    with ThreadPoolExecutor(len(agents)) as pool:
        futures = {ag: (pool.submit(run_cursor, prompt, fallback["model"], directory, a.timeout)
                        if ag == "codex" and fallback else
                        pool.submit(_run_one, ag, prompt, a, directory)) for ag in agents}
    code = 0
    picks = []  # (agent, value_or_None | False for failed)
    any_pick = False
    for i, ag in enumerate(agents):
        if i:
            print()
        label = "fallback" if ag == "codex" and fallback else ag
        title = f"fallback (cursor {fallback['model']})" if label == "fallback" else ag
        print(f"## {title}")
        try:
            answer, usage, elapsed, model = futures[ag].result()
        except AskFailed as e:
            print(f"(failed, exit {e.code}) {e}")
            code = code or e.code
            picks.append((label, False))
            continue
        print(answer)
        print(_footer("cursor" if label == "fallback" else ag, model, elapsed, usage))
        pick = extract_pick(answer)
        picks.append((label, pick))
        if pick is not None:
            any_pick = True
    if any_pick:
        print("\n## picks")
        for ag, value in picks:
            if value is False:
                print(f"Pick ({ag}): (failed)")
            elif value is None:
                print(f"Pick ({ag}): (no Pick line found)")
            else:
                print(f"Pick ({ag}): {value}")
    if code:
        sys.exit(code)


def _add_args(p):
    p.add_argument("question", help="question text, or - to read stdin")
    p.add_argument("--agent", choices=["cursor", "codex", "both"], default="cursor",
                   help="both: same question to codex and cursor in parallel (second opinions)")
    p.add_argument("--model", help="cursor default auto; codex default gpt-6.1-sol; not with --agent both")
    p.add_argument("--effort", help="codex reasoning effort (default medium)")
    p.add_argument("--no-fallback", action="store_true", help="both: keep Codex even when its quota is empty")
    p.add_argument("--dir", help="workspace directory (default: cwd)")
    p.add_argument("--max-words", type=int, default=200)
    p.add_argument("--timeout", type=float, default=600, metavar="S")
    p.add_argument("--files", action="append", metavar="F", help="file to focus on (repeatable)")
    p.set_defaults(fn=cmd_ask)


def register(subparsers):
    s = subparsers.add_parser(
        "ask",
        help="cheap read-only one-shot: ask cursor/codex about files without filling orchestrator context",
    )
    _add_args(s)
    return s


def main(argv=None):
    p = argparse.ArgumentParser(prog="ask.py", description=__doc__)
    _add_args(p)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    main()

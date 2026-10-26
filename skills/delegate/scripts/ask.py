#!/usr/bin/env python3
"""One-shot read-only ask: cheap delegate reads files; orchestrator keeps the short answer."""
import argparse
import json
import os
import re
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

DEFAULTS = {"cursor": ("auto", None), "codex": ("gpt-6.1-sol", "medium")}


class AskFailed(Exception):
    """A delegate call failed; str() is the stderr tail, .code the exit code."""

    def __init__(self, message, code):
        super().__init__(message)
        self.code = code or 1


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


def run_cursor(prompt, model, directory, timeout):
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
    """First Pick: value in answer, or None. Tolerates markdown wrappers around Pick."""
    if not answer:
        return None
    for line in answer.splitlines():
        s = _normalize_pick_prefix(line)
        m = re.match(r"(?i)^pick\s*:(.*)$", s)
        if m:
            return m.group(1).strip()
    return None


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
    agents = ["codex", "cursor"]
    with ThreadPoolExecutor(len(agents)) as pool:
        futures = {ag: pool.submit(_run_one, ag, prompt, a, directory) for ag in agents}
    code = 0
    picks = []  # (agent, value_or_None | False for failed)
    any_pick = False
    for i, ag in enumerate(agents):
        if i:
            print()
        print(f"## {ag}")
        try:
            answer, usage, elapsed, model = futures[ag].result()
        except AskFailed as e:
            print(f"(failed, exit {e.code}) {e}")
            code = code or e.code
            picks.append((ag, False))
            continue
        print(answer)
        print(_footer(ag, model, elapsed, usage))
        pick = extract_pick(answer)
        picks.append((ag, pick))
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

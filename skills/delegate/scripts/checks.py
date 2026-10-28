"""Pure helpers for static warnings on --verify pipelines and brief paths.

No git, no I/O (beyond Path.home for ~/ expansion). Documented limits:
- Heredocs are not handled.
- A later `set +o pipefail` after a pipefail string match is not tracked — any
  occurrence of the substring `pipefail` quiets masked_pipes.
- A closing `)` inside a `#` comment within `$(…)` can end the opaque scan
  early (rare in verify commands); comments inside substitutions are not tracked.
- A backslash-newline inside an operator (`|\\<newline>&`, `>\\<newline>|`,
  `|\\<newline>|`) is not joined before the operator is read, so the check can
  miss or invent a pipe there.
- Nested substitutions share one quote state, so a quote inside `$( $(…) )` can
  hide a later top-level pipe.
  None of the 88 recorded verify commands (2026-10-28) has a newline or a `$(`.
"""
import os
import re
import shlex
from pathlib import Path

PASS_THROUGH = frozenset("tail head tee cat sort uniq cut tr sed wc column".split())

_FAILURE_RES = [re.compile(p) for p in (
    r"^FAILED\b",
    r"^FAIL[:\s]",
    r"^ERROR[:\s]",
    r"Traceback \(most recent call last\)",
    r"\b[1-9]\d* (?:failed|failures?|errors?)\b",
    r"\b[1-9]\d* failure\(s\)",
    r"^not ok\b",
    r"\bAssertionError\b",
)]
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Path token: start of text or after whitespace / opener; strip trailing punctuation later.
_PATH_TOKEN = re.compile(
    r"(?:^|[\s`\'\"(\[<=])"
    r"(~/[^`\'\"<>()\[\]{},;|\*\s]*|/[^`\'\"<>()\[\]{},;|\*\s]*)"
)
_SCRATCHPAD = re.compile(r"^/tmp/claude-[^/]+/")


def stage_name(stage):
    """Basename of a pipeline stage's command after leading NAME=value assignments."""
    text = stage.strip()
    if not text:
        return None
    try:
        words = shlex.split(text)
    except ValueError:
        words = text.split()
    for word in words:
        if _ASSIGN.match(word):
            continue
        return os.path.basename(word)
    return None


def _is_pass_through(stage):
    name = stage_name(stage)
    return bool(name) and name in PASS_THROUGH


def masked_pipes(cmd):
    """Last stage text of each pipeline whose status is masked by a pass-through filter."""
    if not cmd or "pipefail" in cmd:
        return []
    found = []
    n = len(cmd)
    i = 0
    stages = []
    buf = []
    quote = None

    def flush_stage():
        stage = "".join(buf).strip(" \t(){}")
        buf.clear()
        if stage or stages:
            stages.append(stage)

    def end_pipeline():
        flush_stage()
        if len(stages) >= 2 and stages[-1] and _is_pass_through(stages[-1]):
            found.append(stages[-1])
        stages.clear()

    while i < n:
        c = cmd[i]
        # Outside single quotes, backslash-newline joins lines (discarded).
        if quote != "'" and c == "\\" and i + 1 < n and cmd[i + 1] == "\n":
            i += 2
            continue
        # Outside quotes, other backslash escapes keep the pair as text.
        if quote is None and c == "\\" and i + 1 < n:
            buf.append(cmd[i:i + 2])
            i += 2
            continue
        # Inside double quotes, backslash escapes the next character.
        if quote == '"' and c == "\\" and i + 1 < n:
            buf.append(cmd[i:i + 2])
            i += 2
            continue
        if quote is None and c == "#" and (not buf or buf[-1].isspace()):
            end = cmd.find("\n", i)
            i = n if end < 0 else end
            continue
        if quote != "'" and (cmd.startswith("$(", i) or c == "`"):
            tick = c == "`"
            j = i + (1 if tick else 2)
            depth = 0
            q = None
            while j < n:
                d = cmd[j]
                if tick:
                    if d == "\\" and j + 1 < n:
                        j += 2
                        continue
                    if d == "`":
                        break
                    j += 1
                    continue
                if q == '"' and d == "\\" and j + 1 < n:
                    j += 2
                    continue
                if q is None and d == "\\" and j + 1 < n:
                    j += 2
                    continue
                if d in ("'", '"'):
                    if q is None:
                        q = d
                    elif q == d:
                        q = None
                    j += 1
                    continue
                if q is not None:
                    j += 1
                    continue
                if d == "(":
                    depth += 1
                elif d == ")":
                    if depth == 0:
                        break
                    depth -= 1
                j += 1
            buf.append(cmd[i:j + 1] if j < n else cmd[i:])
            i = j + 1 if j < n else n
            continue
        if c in ("'", '"'):
            if quote is None:
                quote = c
            elif quote == c:
                quote = None
            buf.append(c)
            i += 1
            continue
        if quote is not None:
            buf.append(c)
            i += 1
            continue
        if c in "(){}":
            i += 1
            continue
        # List separators and pipes (redirect forms like 2>&1 and >| stay in the stage).
        if c == ">" and i + 1 < n and cmd[i + 1] == "|":
            buf.append(">|")
            i += 2
            continue
        if c == "&" and i + 1 < n and cmd[i + 1] == ">":
            buf.append(c)
            i += 1
            continue
        if c == "&" and buf and buf[-1] in "><":
            buf.append(c)
            i += 1
            continue
        if c == "&" and i + 1 < n and cmd[i + 1] == "&":
            end_pipeline()
            i += 2
            continue
        if c == "|" and i + 1 < n and cmd[i + 1] == "|":
            end_pipeline()
            i += 2
            continue
        if c == "|" and i + 1 < n and cmd[i + 1] == "&":
            flush_stage()
            i += 2
            continue
        if c == "|":
            flush_stage()
            i += 1
            continue
        if c == "\n":
            # Newline after | or |& (only whitespace since) continues the pipeline.
            if stages and not "".join(buf).strip():
                i += 1
                continue
            end_pipeline()
            i += 1
            continue
        if c in ";&":
            end_pipeline()
            i += 1
            continue
        buf.append(c)
        i += 1
    end_pipeline()
    return found


def failure_line(text):
    """First line matching a measured failure pattern, stripped and cut to 160 chars."""
    if not text:
        return None
    for line in text.splitlines():
        if line.startswith("$ "):
            continue
        for pat in _FAILURE_RES:
            if pat.search(line):
                return line.strip()[:160]
    return None


def find_brief_paths(text, repo):
    """Absolute paths in a brief that need a warning: (as_written, kind, rel)."""
    if not text:
        return []
    repo_n = os.path.normpath(str(repo))
    home = str(Path.home())
    seen = set()
    out = []
    for m in _PATH_TOKEN.finditer(text):
        raw = m.group(1).rstrip(".:!,;?")
        if not raw or raw in seen:
            continue
        expanded = os.path.normpath(home + raw[1:] if raw.startswith("~/") else raw)
        if _SCRATCHPAD.match(expanded):
            seen.add(raw)
            out.append((raw, "scratchpad", ""))
            continue
        if expanded == repo_n:
            seen.add(raw)
            out.append((raw, "checkout", "."))
            continue
        prefix = repo_n + os.sep
        if expanded.startswith(prefix):
            rel = expanded[len(prefix):].replace(os.sep, "/")
            seen.add(raw)
            out.append((raw, "checkout", rel))
    return out

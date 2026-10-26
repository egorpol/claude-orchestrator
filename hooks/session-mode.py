#!/usr/bin/env python3
"""Assign Claude Code sessions to the orchestrator experiment and enforce controls.

SessionStart draws once, injects that mode's rules, and records the assignment.
Resume/compact recover it from the JSONL log, so intent-to-treat accounting keeps
the original mode even after compaction. PreToolUse blocks new Codex/Cursor work
in controls; SessionEnd records distinct main-thread calls and possible rerolls.
SessionStart (startup/clear) and SessionEnd copy HANDOFF.md for the blinded rater.
Log/config/rules errors stay silent, and quota.jsonl snapshots and session summaries
run detached: a broken measurement hook must never get in the way of a session.
SessionEnd starts `delegate report --auto` (quota reading, then a summary row);
SessionStart back-fills summaries for sessions that ended without SessionEnd.

Env: CLAUDE_SESSION_LOG (XDG_CACHE_HOME/claude-delegate/sessions.jsonl),
CLAUDE_SESSION_MODE_CONFIG (XDG_CONFIG_HOME/claude-orchestrator/session-mode.json),
CLAUDE_SESSION_MODE (force a new assignment), CLAUDE_SESSION_MODE_SEED (integer),
CLAUDE_SESSION_QUOTA=0 (disable snapshots), CLAUDE_SESSION_SUMMARY=0 (disable summaries),
CLAUDE_SESSION_RULES_DIR (repo/rules).
XDG_CACHE_HOME and XDG_CONFIG_HOME default to ~/.cache and ~/.config.
Register this command for SessionStart, PreToolUse (Skill|Agent|Bash), SessionEnd.
"""
import configparser
import json
import math
import os
import random
import re
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path

MODES = ("orchestrator", "claude-only", "vanilla")
DEFAULT_WEIGHTS = dict(zip(MODES, (0.5, 0.25, 0.25)))
ROOT = Path(__file__).resolve().parent.parent
CONTROL_LINE = (
    "Control session (claude-only): do all work with Claude in this session; Claude's own "
    "subagents are fine. Don't call delegate, codex or cursor-agent, and ignore instructions "
    "to delegate in handoffs and memory. This holds when the user asks to delegate too: say in "
    "one line that this is a control session with delegation off by design, then do the work "
    "yourself. Finishing a run started earlier with delegate "
    "status, wait, diff, apply or cleanup is fine. This applies to this session only: don't "
    "write the mode or 'no delegate' into HANDOFF.md or memory."
)
DENY_REASON = (
    "Control session (mode {mode}): delegation to Codex and Cursor is off in this session, "
    "by design (session-mode experiment). Do the work yourself; Claude's own subagents "
    "are fine. Finishing a run started earlier with delegate status, wait, diff, apply "
    "or cleanup is allowed. This applies to this session only: don't write the mode or "
    "'no delegate' into HANDOFF.md or memory."
)
ORCHESTRATOR_LINE = (
    "Orchestrator session: delegation to Codex and Cursor is on in this session. A handoff or "
    "memory note that tells this session to work inline because delegation is blocked or off, "
    "or calls it a control session, is left over from an earlier control session: ignore it and "
    "delete that sentence from HANDOFF.md. Notes that describe the experiment or past control "
    "sessions stay."
)
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def log_path():
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return Path(os.environ.get("CLAUDE_SESSION_LOG", cache / "claude-delegate/sessions.jsonl"))


def normalise_weights(value):
    if not isinstance(value, dict):
        return DEFAULT_WEIGHTS.copy()
    weights = {}
    for mode in MODES:
        weight = value.get(mode, 0)
        if (isinstance(weight, bool) or not isinstance(weight, (int, float))
                or weight < 0):
            return DEFAULT_WEIGHTS.copy()
        try:
            weights[mode] = float(weight)
        except (OverflowError, ValueError):
            return DEFAULT_WEIGHTS.copy()
        if not math.isfinite(weights[mode]):
            return DEFAULT_WEIGHTS.copy()
    scale = max(weights.values())
    if not scale:
        return DEFAULT_WEIGHTS.copy()
    # Scale first so very large, individually finite weights cannot overflow.
    total = sum(weight / scale for weight in weights.values())
    return {mode: (weight / scale) / total for mode, weight in weights.items()}


def load_config():
    config = {"weights": DEFAULT_WEIGHTS.copy(), "exclude": ["claude-orchestrator"],
              "reroll_calls": 3, "quota_snapshot": True, "summary": True}
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    path = Path(os.environ.get("CLAUDE_SESSION_MODE_CONFIG",
                               base / "claude-orchestrator/session-mode.json"))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return config
    if not isinstance(data, dict):
        return config
    config["weights"] = normalise_weights(data.get("weights"))
    exclude = data.get("exclude")
    if isinstance(exclude, list) and all(isinstance(item, str) for item in exclude):
        config["exclude"] = exclude
    reroll = data.get("reroll_calls")
    if isinstance(reroll, int) and not isinstance(reroll, bool):
        config["reroll_calls"] = reroll
    for key in ("quota_snapshot", "summary"):
        if isinstance(data.get(key), bool):
            config[key] = data[key]
    return config


def draw_mode(rng, weights=None):
    """Return (mode, unrounded u); use fixed-order, normalised cumulative weights."""
    weights = normalise_weights(DEFAULT_WEIGHTS if weights is None else weights)
    u, cumulative = rng.random(), 0.0
    for mode in MODES:
        cumulative += weights[mode]
        if u < cumulative and weights[mode] > 0:
            return mode, u
    # Floating-point addition can fall just short of one; never select a zero weight.
    return next(mode for mode in reversed(MODES) if weights[mode] > 0), u


def _common_git_dir(git):
    """The main repo's git dir for a linked worktree's `.git` file (gitdir:, then commondir)."""
    try:
        text = git.read_text(encoding="utf-8").strip()
        if not text.startswith("gitdir:"):
            return None
        gitdir = (git.parent / text[len("gitdir:"):].strip()).resolve()
        try:
            return (gitdir / (gitdir / "commondir").read_text(encoding="utf-8").strip()).resolve()
        except OSError:
            return gitdir
    except (OSError, ValueError):
        return None


def repo_root(cwd):
    """The nearest git root, including linked worktrees, without invoking git."""
    try:
        directory = Path(cwd).resolve()
        for root in (directory, *directory.parents):
            git = root / ".git"
            if git.is_dir() or git.is_file():
                return root
    except (OSError, TypeError, ValueError):
        pass
    return None


def repo_info(cwd):
    """Find the nearest git root; a linked worktree takes its main repo's origin."""
    try:
        root = repo_root(cwd)
        if root is not None:
            git = root / ".git"
            repo = None
            # Worktrees of an excluded repo must stay excluded, so read the main repo's config.
            common = git if git.is_dir() else _common_git_dir(git)
            if common is not None:
                try:
                    config = configparser.ConfigParser(interpolation=None)
                    config.read(common / "config", encoding="utf-8")
                    url = config.get('remote "origin"', "url", fallback="").strip()
                    match = re.fullmatch(r"(?:git@github\.com:|https://github\.com/)"
                                         r"([^/\s]+)/([^/\s]+?)(?:\.git)?/?", url)
                    if match:
                        repo = "/".join(match.groups())
                except (OSError, ValueError, configparser.Error):
                    pass
            return repo, root.name
    except (OSError, TypeError, ValueError):
        pass
    return None, None


def nested_handoff(base):
    """One folder down, where /next looks next: private/ first, else a single match."""
    preferred = base / "private" / "HANDOFF.md"
    if preferred.is_file():
        return preferred
    matches = [p for p in base.glob("*/HANDOFF.md")
               if p.is_file() and not p.parent.name.startswith(".")]
    # Several unrelated handoffs: a wrong copy would mislead the rater more than none.
    return matches[0] if len(matches) == 1 else None


def snapshot_handoff(cwd, path, session, end=False):
    """Best-effort, bounded start- or end-time copy; never let measurement break a session."""
    try:
        if not isinstance(session, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", session):
            return None
        directory = Path(cwd).resolve()
        root = repo_root(cwd)
        source = (root or directory) / "HANDOFF.md"
        if not source.is_file():
            source = directory / "HANDOFF.md"
        if not source.is_file():
            source = nested_handoff(root or directory)
        if source is None:
            return None
        with source.open("rb") as stream:
            data = stream.read(64 * 1024)
        target = path.parent / "handoffs" / (session + (".end.md" if end else ".md"))
        target.parent.mkdir(parents=True, exist_ok=True)
        if end:
            # A resumed session ends again: the latest end state wins, replaced atomically.
            temp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
            temp.write_bytes(data)
            os.replace(temp, target)
            return str(target)
        try:
            # Duplicate starts must not overwrite the first start-time evidence.
            with target.open("xb") as stream:
                stream.write(data)
        except FileExistsError:
            if not target.is_file():
                return None
        return str(target)
    except Exception:
        return None


def latest_start(path, session):
    latest = None
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if (isinstance(row, dict) and row.get("event") == "start"
                        and row.get("session") == session and row.get("mode") in MODES):
                    latest = row
    except (OSError, ValueError):
        pass
    return latest


def append_row(path, event, session, **fields):
    try:
        row = {"event": event, "session": session,
               "ts": datetime.now().astimezone().isoformat(timespec="seconds"), **fields}
        line = json.dumps(row, ensure_ascii=True) + "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        # O_APPEND plus one unbuffered write keeps simultaneous /clear rows separate.
        with path.open("ab", buffering=0) as stream:
            stream.write(line.encode("utf-8"))
        return True
    except (OSError, TypeError, ValueError):
        return False


def _load_quota():
    """Load skills/delegate/scripts/quota.py the same way quota_snapshot does."""
    import importlib.util
    path = ROOT / "skills/delegate/scripts/quota.py"
    spec = importlib.util.spec_from_file_location("session_quota", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _spare_line(path):
    """Spare-pool SessionStart line from the newest good reading next to the session log."""
    try:
        quota = _load_quota()
        info = quota.spare_pools(path.parent / "quota.jsonl")
        return quota.format_spare_line(info)
    except Exception:
        return None


def session_context(mode, path=None):
    """Build additionalContext. Orchestrator: mode, spare, delegation, hygiene (preview order)."""
    if mode == "vanilla":
        return None
    directory = Path(os.environ.get("CLAUDE_SESSION_RULES_DIR", ROOT / "rules"))
    parts = []
    if mode == "orchestrator":
        parts.append(ORCHESTRATOR_LINE)
        spare = _spare_line(path) if path is not None else None
        if spare:
            parts.append(spare)
        for filename in ("delegation.md", "hygiene.md"):
            try:
                text = (directory / filename).read_text(encoding="utf-8").strip()
                if text:
                    parts.append(text)
            except (OSError, ValueError):
                pass
    else:
        parts.append(CONTROL_LINE)
        try:
            text = (directory / "hygiene.md").read_text(encoding="utf-8").strip()
            if text:
                parts.append(text)
        except (OSError, ValueError):
            pass
    reference = directory / "reference.md"
    if reference.is_file():
        parts.append(
            "Topic tips (handoffs, decisions, data checks, shell, edits, reads): read the matching "
            f"section of {reference.resolve()} before that kind of work."
        )
    return "\n\n".join(parts) or None


def quota_enabled(config):
    return os.environ.get("CLAUDE_SESSION_QUOTA") != "0" and config["quota_snapshot"]


def summary_enabled(config):
    return os.environ.get("CLAUDE_SESSION_SUMMARY") != "0" and config["summary"]


def quota_snapshot(path, session, suffix, config):
    if not quota_enabled(config):
        return
    # Ignore malformed session ids even though they no longer become filenames.
    if not isinstance(session, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", session):
        return
    delegate = ROOT / "skills/delegate/scripts/delegate"
    try:
        if not delegate.is_file():
            return
        quota = _load_quota()
        quota.spawn_snapshot("session-" + suffix, session=session,
                             log=path.parent / "quota.jsonl", delegate=delegate)
    except (OSError, ValueError):
        pass


def spawn_summary(path, session, args):
    """Start `delegate report --auto ARGS` detached; parsing a long transcript takes seconds."""
    if not isinstance(session, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", session):
        return
    delegate = ROOT / "skills/delegate/scripts/delegate"
    try:
        if delegate.is_file():
            subprocess.Popen([sys.executable, str(delegate), "report", "--auto", "--log", str(path), *args],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
    except (OSError, ValueError):
        pass


def _substitution_end(text, start, backtick=False):
    """Find a substitution's close, retaining quote state across nested $(...)."""
    quote, stack, i = None, [None], start
    while i < len(text):
        char = text[i]
        if char == "\\" and quote != "'":
            i += 2
            continue
        if backtick and char == "`":
            return i
        if char in ("'", '"'):
            if quote is None:
                quote = char
            elif quote == char:
                quote = None
        elif not backtick and quote != "'" and text.startswith("$(", i):
            stack.append(quote)
            quote = None
            i += 1
        elif not backtick and quote is None:
            if char == "(":
                stack.append(None)
            elif char == ")":
                quote = stack.pop()
                if not stack:
                    return i
        i += 1
    return len(text)


def _shell_segments(text):
    """Split shell command positions, without treating quoted prose as commands."""
    parts, buffer, quote, i = [], [], None, 0
    while i < len(text):
        char = text[i]
        if char == "\\" and quote != "'" and i + 1 < len(text):
            # A continued line is removed by the shell, rather than being a word break.
            if text[i + 1] != "\n":
                buffer.extend(text[i:i + 2])
            i += 2
            continue
        if quote is None and char == "#" and (not buffer or buffer[-1].isspace()):
            end = text.find("\n", i)
            i = len(text) if end < 0 else end
            continue
        if quote != "'" and (text.startswith("$(", i) or char == "`"):
            backtick = char == "`"
            start = i + (1 if backtick else 2)
            end = _substitution_end(text, start, backtick)
            parts.extend(_shell_segments(text[start:end]))
            buffer.append(" __substitution__ ")
            i = end + 1
            continue
        if char in ("'", '"'):
            if quote is None:
                quote = char
            elif quote == char:
                quote = None
        elif quote is None and char in ";&|\n()":
            parts.append("".join(buffer))
            buffer = []
            i += 1
            continue
        buffer.append(char)
        i += 1
    parts.append("".join(buffer))
    return parts


def _tokens(segment):
    try:
        return shlex.split(segment, comments=True)
    except ValueError:
        return segment.split()


def _skip_options(words, index, takes_value=()):
    while index < len(words) and words[index].startswith("-"):
        option = words[index]
        index += 1
        if option == "--":
            break
        if option in takes_value:
            index += 1
    return index


def _runs_delegate(words, depth):
    if depth > 32:
        return False
    i = 0
    while i < len(words):
        if ASSIGNMENT.match(words[i]):
            i += 1
            continue
        name = Path(words[i]).name
        i += 1
        if name == "env":
            while i < len(words) and words[i].startswith("-"):
                option = words[i]
                i += 1
                if option == "--":
                    break
                # env -S is itself a command string, wherever it appears among options.
                if option in ("-S", "--split-string"):
                    return i < len(words) and _runs_delegate(_tokens(words[i]) + words[i + 1:], depth + 1)
                if option.startswith("--split-string=") or option.startswith("-S"):
                    text = option.split("=", 1)[1] if option.startswith("--") else option[2:]
                    return _runs_delegate(_tokens(text) + words[i:], depth + 1)
                if option in ("-u", "--unset", "-C", "--chdir", "-a", "--argv0"):
                    i += 1
        elif name == "timeout":
            i = _skip_options(words, i, ("-s", "--signal", "-k", "--kill-after")) + 1
        elif name == "eval":
            return bash_delegates(" ".join(words[i:]), depth + 1)
        elif name in ("nohup", "setsid", "exec", "command", "time", "nice", "sudo", "doas"):
            operands = {"exec": ("-a",), "time": ("-f", "--format", "-o", "--output"),
                        "nice": ("-n", "--adjustment"),
                        "sudo": ("-u", "--user", "-g", "--group", "-C", "-D", "-h", "-p", "-r", "-t", "-T", "-U"),
                        "doas": ("-u", "-C")}.get(name, ())
            i = _skip_options(words, i, operands)
        elif name in ("bash", "sh", "zsh"):
            while i < len(words) and words[i].startswith(("-", "+")):
                option = words[i]
                i += 1
                if option == "--":
                    break
                if option in ("-o", "+o", "-O", "+O", "--rcfile", "--init-file"):
                    i += 1
                    continue
                if option.startswith("-") and not option.startswith("--") and "c" in option:
                    return i < len(words) and bash_delegates(words[i], depth + 1)
            return False
        elif name in ("python", "python3"):
            # Options like -m/-c are Python code/modules, not script command words.
            if i >= len(words) or words[i].startswith("-"):
                return False
            # Re-enter with SCRIPT as the command word (e.g. python3 /path/delegate ask).
        else:
            return name in ("codex", "cursor-agent") or (
                name == "delegate" and i < len(words)
                and words[i] in ("launch", "followup", "review", "ask"))
    return False


def bash_delegates(command, depth=0):
    if depth > 32:  # malformed/deeply nested input must not exhaust the hook stack
        return False
    return any(_runs_delegate(_tokens(segment), depth) for segment in _shell_segments(command))


def blocked_detail(data):
    tool, inputs = data.get("tool_name"), data.get("tool_input")
    if not isinstance(inputs, dict):
        return None
    if tool == "Skill":
        skill = inputs.get("skill")
        if isinstance(skill, str):
            name = skill.lstrip("/")
            if name == "delegate" or name.startswith("codex:"):
                return skill
    elif tool == "Agent":
        agent = inputs.get("subagent_type")
        if isinstance(agent, str) and agent.startswith("codex:"):
            return agent
    elif tool == "Bash":
        command = inputs.get("command")
        if isinstance(command, str) and bash_delegates(command):
            return command[:200]
    return None


def count_calls(transcript):
    if not isinstance(transcript, str) or not transcript:
        return None
    ids = set()
    try:
        with Path(transcript).open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict) or row.get("type") != "assistant" or row.get("isSidechain"):
                    continue
                message = row.get("message")
                if isinstance(message, dict) and isinstance(message.get("id"), str):
                    ids.add(message["id"])
    except FileNotFoundError:
        # Claude Code writes no transcript before the first message (12 of 12 such ends, 2026-10-20).
        return 0
    except (OSError, ValueError):
        return None
    return len(ids)


def start_session(data, path, config, previous):
    source = data.get("source")
    if source not in ("startup", "clear", "resume", "compact"):
        return
    repo, repo_dir = repo_info(data.get("cwd"))
    u, forced, excluded, inherited, unassigned = None, False, False, False, False
    if previous is not None:
        # Duplicate startup notifications also keep the session's immutable assignment.
        mode, inherited = previous["mode"], True
        forced, excluded, unassigned = (bool(previous.get(key))
                                       for key in ("forced", "excluded", "unassigned"))
    elif source in ("resume", "compact"):
        mode, inherited, unassigned = "orchestrator", True, True
    elif os.environ.get("CLAUDE_SESSION_MODE") in MODES:
        # An explicit experimental override beats the automatic repo exclusion.
        mode, forced = os.environ["CLAUDE_SESSION_MODE"], True
    elif any(name in config["exclude"] for name in (repo_dir, repo, repo.split("/")[-1] if repo else None)):
        mode, excluded = "orchestrator", True
    else:
        mode, u = draw_mode(random.Random(_env_int("CLAUDE_SESSION_MODE_SEED", None)), config["weights"])
    session = data["session_id"]
    handoff = (snapshot_handoff(data.get("cwd"), path, session)
               if source in ("startup", "clear") else (previous or {}).get("handoff"))
    append_row(path, "start", session, source=source, mode=mode,
               weights=config["weights"],
               u=round(u, 6) if u is not None else None, forced=forced, excluded=excluded,
               inherited=inherited, unassigned=unassigned, cwd=data.get("cwd"), repo=repo,
               repo_dir=repo_dir, model=data.get("model"), transcript=data.get("transcript_path"),
               handoff=handoff)
    specific = {"hookEventName": "SessionStart"}
    context = session_context(mode, path)
    if context is not None:
        specific["additionalContext"] = context
    output = {"hookSpecificOutput": specific}
    if source in ("startup", "clear"):
        labels = {"orchestrator": "orchestrator", "claude-only": "claude-only (control: delegation off)",
                  "vanilla": "vanilla (control: no rules, delegation off)"}
        label = "orchestrator (repo excluded from the draw)" if excluded else labels[mode]
        output["systemMessage"] = f"session mode: {label}" + (" (forced)" if forced else "")
    print(json.dumps(output))
    if source in ("startup", "clear"):
        quota_snapshot(path, session, "start", config)
        if summary_enabled(config):
            spawn_summary(path, session, ["--backfill", "--exclude", session])


def main():
    data = json.load(sys.stdin)
    if not isinstance(data, dict) or not isinstance(data.get("session_id"), str) or not data["session_id"]:
        return
    event, session = data.get("hook_event_name"), data["session_id"]
    if event not in ("SessionStart", "PreToolUse", "SessionEnd"):
        return
    path, config = log_path(), load_config()
    previous = latest_start(path, session)
    mode = previous["mode"] if previous else None
    if event == "SessionStart":
        start_session(data, path, config, previous)
    elif event == "PreToolUse" and mode in ("claude-only", "vanilla"):
        detail = blocked_detail(data)
        if detail is not None:
            if not append_row(path, "blocked", session, mode=mode, tool=data.get("tool_name"), detail=detail):
                return  # fail open rather than enforce an unlogged block
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "permissionDecision": "deny",
                "permissionDecisionReason": DENY_REASON.format(mode=mode)}}))
    elif event == "SessionEnd":
        calls = count_calls(data.get("transcript_path"))
        # Inherited assignments retain their provenance; inherited-unassigned ones don't draw.
        drawn = (previous is not None and (previous.get("u") is not None or previous.get("inherited"))
                 and not any(previous.get(key) for key in ("forced", "excluded", "unassigned")))
        reroll = bool(data.get("reason") == "clear" and calls is not None
                      and calls <= config["reroll_calls"] and drawn)
        handoff = snapshot_handoff(data.get("cwd"), path, session, end=True) if previous else None
        append_row(path, "end", session, reason=data.get("reason"), mode=mode, calls=calls, reroll=reroll,
                   handoff=handoff)
        if summary_enabled(config):
            # The summary takes the session-end quota reading itself, so the row can embed it.
            transcript = data.get("transcript_path")
            spawn_summary(path, session, ["--trigger", "end", "--session", session,
                                          *(["--transcript", transcript] if isinstance(transcript, str)
                                            and transcript else []),
                                          *([] if quota_enabled(config) else ["--no-quota"])])
        else:
            quota_snapshot(path, session, "end", config)


if __name__ == "__main__":
    try:
        main()
    except Exception:  # a broken measurement hook must never break a session
        pass

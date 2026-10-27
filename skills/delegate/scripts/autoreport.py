#!/usr/bin/env python3
"""Select automatic field-report evidence and build it without calling an agent."""
import json
import random
import re
from collections import Counter
from pathlib import Path

import cost
import rate

REPORTED = re.compile(r"\(session `([0-9a-f]{8})`\)")
DENIED = re.compile(
    r"PreToolUse:\S+ hook error|Permission for this action was denied|"
    r"Permission to use \S+ (?:has been|was) denied|The user doesn't want to proceed|"
    r"The tool use was rejected")
HANDOFF_WRITE = re.compile(
    r"""['"][^'"\s]*HANDOFF\.md['"]|(?:>>?|\btee\b(?:\s+-a)?|\bsed\s+-i\S*.*?|\bcp\b.*?|\bmv\b.*?)\s+[^\s;|&]*HANDOFF\.md\b""")
WRITERS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
MODES = ("orchestrator", "claude-only", "vanilla")
PRIORITY = {"denied": 0, "idle": 1, "error": 2}
CONTEXT_CAP, HARNESS_CAP, ERRORS_CAP, CWD_CAP = 3000, 4000, 8000, 2500
ERROR_LINE = re.compile(
    r"^(?:Traceback \(most recent call last\):|(?:[\w./-]+: )?(?:error|fatal|ERROR|FATAL|Error)"
    r"(?:\[[\w-]+\])?:\s|.*\bcommand not found\b|.*No such file or directory|"
    r"(?:rc|exit|exit_code|status)[=:] ?[1-9]\d*\s*$|\w+(?:Error|Exception): )")
EXIT_STATUS = re.compile(r"^(?:rc|exit|exit_code|status)[=:] ?[1-9]\d*\s*$")
EXCEPTION_LINE = re.compile(r"\w+(?:Error|Exception): ")
SYSTEM_REMINDER = re.compile(r"<system-reminder>(.*?)</system-reminder>", re.S)
CWD_RESET = re.compile(r"Shell cwd was reset to (.+)$", re.M)
ATTACHMENT_NOISE = {
    "total_tokens_reminder", "deferred_tools_record", "deferred_tools_delta", "prompt_snapshot",
    "skill_listing", "instructions", "environment", "model", "date", "session_context",
    "credential_org", "remote_session_change", "auto_mode", "mcp_instructions_delta",
    "agent_listing_delta", "command_permissions", "queued_command",
}


def _blocks(row):
    message = row.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _tools(rows):
    for row in rows:
        if row.get("type") == "assistant":
            for block in _blocks(row):
                if block.get("type") == "tool_use":
                    yield block


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b["text"] for b in content
                        if isinstance(b, dict) and isinstance(b.get("text"), str))
    return ""


def _denied(block):
    return bool(block.get("is_error") and DENIED.match(_text(block.get("content")).strip()))


def denied_calls(rows):
    return sum(_denied(b) for row in rows if not row.get("isSidechain")
               for b in _blocks(row) if b.get("type") == "tool_result")


def _bound(value, name):
    if value is None:
        return None
    ts = cost._parse_ts(value)
    if ts is None:
        raise ValueError(f"invalid --{name} date: {value}")
    return ts if ts.tzinfo else ts.astimezone()


def _mode(record):
    return record["summary"].get("mode") or record["start"].get("mode") or "unlabelled"


def reported_ids(directory):
    ids = set()
    for path in sorted(Path(directory).glob("*.md")):
        if path.is_file():
            with path.open(encoding="utf-8") as stream:
                ids.update(REPORTED.findall(stream.read(3000)))
    return ids


def select_sessions(rows, feedback, since=None, until=None, seed=None):
    """Return a reproducible batch, with the signals and reason for every pick."""
    first, last = _bound(since, "since"), _bound(until, "until")
    if first is not None and last is not None and first >= last:
        raise ValueError("--since must be before --until")
    eligible = []
    for record in rate.session_records(rows).values():
        summary = record["summary"]
        ts = rate._aware(summary.get("first_call"))
        path = summary.get("transcript")
        if (not summary or ts is None or first is not None and ts < first
                or last is not None and ts >= last or not isinstance(path, str)
                or not Path(path).exists()):
            continue
        eligible.append(record)
    q3 = sorted(r["summary"].get("cost_units", 0) for r in eligible)[int(.75 * len(eligible))] \
        if eligible else 0
    reported = reported_ids(feedback)
    weights, prices = cost.load_catalog_cost()
    counts = dict.fromkeys(("eligible", "reported", "unreported", "flagged", "denied", "idle",
                            "top_quartile"), 0)
    counts["eligible"] = len(eligible)
    candidates = []
    for record in eligible:
        session, summary = record["session"], record["summary"]
        if session[:8] in reported:
            counts["reported"] += 1
            continue
        counts["unreported"] += 1
        path = summary["transcript"]
        try:
            denied = denied_calls(rate.json_rows(path))
            usage = cost.summarise_session(cost.parse_session_usage(path), weights, prices)
        except (OSError, ValueError):
            raise ValueError(f"session {session[:8]}: unreadable transcript usage") from None
        idle = len(usage["idle_rebuilds"])
        units = summary.get("cost_units", 0)
        top = units >= q3
        flagged = bool(denied or idle or top)
        counts["denied"] += denied > 0
        counts["idle"] += idle > 0
        counts["top_quartile"] += top
        counts["flagged"] += flagged
        candidates.append({"session": session, "mode": _mode(record), "reason": None,
                           "rank": None, "flagged": flagged, "denied": denied,
                           "idle_rebuilds": idle, "rebuild_share": usage["idle_rebuild_units"]
                           / usage["cost_units"] if usage["cost_units"] else 0,
                           "cost_units": units, "replaces": None})
    flagged = sorted((p for p in candidates if p["flagged"]), key=lambda p: (
        -p["rebuild_share"], -p["denied"], -p["cost_units"], p["session"]))
    for rank, pick in enumerate(flagged, 1):
        pick["rank"] = rank
    seed = random.SystemRandom().randrange(2 ** 63) if seed is None else seed
    rng = random.Random(seed)
    picks = [dict(p, reason="rank") for p in flagged[:15]]
    for pool, n, reason in (([p for p in candidates if not p["flagged"]], 5, "random unflagged"),
                            (flagged[15:], 2, "random flagged")):
        pool = sorted(pool, key=lambda p: p["session"])
        picks.extend(dict(p, reason=reason) for p in rng.sample(pool, min(n, len(pool))))
    for mode in MODES:
        coverage = Counter(p["mode"] for p in picks)
        pool = [p for p in candidates if p["mode"] == mode]
        if coverage[mode] or not pool:
            continue
        best = min(pool, key=lambda p: (not p["flagged"], p["rank"] or 0,
                                        -p["cost_units"], p["session"]))
        replaceable = [p for p in picks if p["reason"] == "rank" and coverage[p["mode"]] > 1]
        replacement = dict(best, reason="mode floor")
        if replaceable:
            old = max(replaceable, key=lambda p: p["rank"])
            replacement["replaces"] = old["session"]
            picks[picks.index(old)] = replacement
        else:
            picks.append(replacement)
    return {"since": first.isoformat() if first else None, "until": last.isoformat() if last else None,
            "seed": seed, "counts": counts, "q3_cost_units": q3,
            "flagged_by_mode": dict(sorted(Counter(p["mode"] for p in flagged).items())),
            "picks": picks}


def render_selection(result):
    counts = ", ".join(f"{key} {value}" for key, value in result["counts"].items())
    lines = [f"{counts}; q3 {result['q3_cost_units']}; seed {result['seed']}; "
             f"picks {len(result['picks'])}" + (" (total exceeds 22)" if len(result["picks"]) > 22 else "")]
    for p in result["picks"]:
        signals = (f"denied {p['denied']}, idle {p['idle_rebuilds']} "
                   f"({p['rebuild_share']:.1%}), cost units {p['cost_units']}, "
                   f"top quartile {p['cost_units'] >= result['q3_cost_units']}")
        replaces = f", replaces {p['replaces'][:8]}" if p["replaces"] else ""
        lines.append(f"{p['session'][:8]} {p['mode']}: {p['reason']}, rank {p['rank']}; {signals}{replaces}")
    return "\n".join(lines)


def _leak_reason(rows, prompts):
    if any(p["text"].startswith("[command /field-report") for _, p in prompts):
        return "typed /field-report prompt before the cut"
    for block in _tools(rows):
        name, args = block.get("name"), block.get("input") or {}
        if name == "Skill" and args.get("skill") == "field-report":
            return "Skill field-report call before the cut"
        if name in WRITERS and "feedback" in Path(str(args.get("file_path", ""))).parts:
            return "write to feedback before the cut"
    return None


def handoff_written(rows):
    for block in _tools(rows):
        name, args = block.get("name"), block.get("input") or {}
        if name in WRITERS and str(args.get("file_path", "")).endswith("HANDOFF.md"):
            return True
        if name == "Bash" and HANDOFF_WRITE.search(str(args.get("command", ""))):
            return True
    return False


def _clean(text):
    return re.sub(r"<(system-reminder|ide_selection|hook-context|hook_context)\b[^>]*>.*?</\1>",
                  "", text, flags=re.S).strip()


def _short(args, limit):
    return rate._truncate(" ".join(_clean(json.dumps(args, ensure_ascii=False)).split()), limit)


def _minutes(row, first):
    ts = rate._aware(row.get("timestamp"))
    return f"{(ts - first).total_seconds() / 60:.1f}" if ts and first else "?"


def _k(n):
    return f"{n / 1000:.0f}k"


def _one_line(text, limit):
    text = " | ".join(line.strip() for line in _clean(text).splitlines())
    return re.sub(r"[ \t]+", " ", text).strip()[:limit]


def _collapse(text, limit):
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _prompt_at(prompts, row_index):
    found = None
    for i, (idx, _) in enumerate(prompts):
        if idx <= row_index:
            found = i
        else:
            break
    return f"P{found}" if found is not None else "P-"


def _show_path(path, start):
    if not path:
        return "-"
    if start and path == start:
        return "."
    if start:
        try:
            return str(Path(path).resolve().relative_to(Path(start).resolve()))
        except (ValueError, OSError):
            pass
    home = str(Path.home())
    if path == home:
        return "~"
    if path.startswith(home + "/"):
        return "~" + path[len(home):]
    return path


def _start_path(path):
    if not path:
        return "-"
    home = str(Path.home())
    if path == home:
        return "~"
    if path.startswith(home + "/"):
        return "~" + path[len(home):]
    return path


def _fit_section(title, counts, items, limit, more_word):
    head = f"## {title}\n" + "\n".join(counts)
    for n in range(len(items), -1, -1):
        body = [head, *items[:n]]
        if n < len(items):
            body.append(f"({len(items) - n} more {more_word}: see transcript.txt)")
        text = "\n".join(body)
        if len(text) <= limit:
            return text
    return head


def _guard_system_uuids(rows):
    return {row.get("uuid") for row in rows
            if not row.get("isSidechain") and row.get("type") == "attachment"
            and (row.get("attachment") or {}).get("type") == "hook_system_message"
            and (row.get("attachment") or {}).get("hookEvent") == "UserPromptSubmit"
            and row.get("uuid")}


def _hook_decision(stdout):
    try:
        payload = json.loads(stdout or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None, None
    if not isinstance(payload, dict):
        return None, None
    out = payload.get("hookSpecificOutput")
    if not isinstance(out, dict):
        return None, None
    return out.get("permissionDecision"), out.get("permissionDecisionReason")


def _tool_result_evidence(rows, prompts, first):
    uses = _tool_uses_by_id(rows)
    results, by_tool = [], Counter()
    for row in rows:
        if row.get("isSidechain") or row.get("type") not in ("user", "assistant"):
            continue
        for block in _blocks(row):
            if block.get("type") != "tool_result":
                continue
            text = _text(block.get("content"))
            use = uses.get(block.get("tool_use_id"))
            name = use[1].get("name") if use else "?"
            by_tool[name] += len(text)
            results.append((len(text), row, use, block))
    results.sort(key=lambda item: (-item[0],))
    items = []
    for length, row, use, _ in results[:8]:
        if use is None:
            continue
        index = next((i for i, r in enumerate(rows) if r is row), 0)
        use_row, use_block = use
        use_index = next((i for i, r in enumerate(rows) if r is use_row), index)
        items.append(f"+{_minutes(use_row, first)} min ({_prompt_at(prompts, use_index)}) "
                     f"{use_block.get('name')} {_short(use_block.get('input'), 120)} "
                     f"-> {length:,} chars")
    top_tools = "; ".join(f"{name} {total:,}" for name, total in by_tool.most_common(8)) or "none"
    return items, top_tools


def _tool_uses_by_id(rows):
    return {b["id"]: (row, b) for row in rows
            if not row.get("isSidechain") and row.get("type") == "assistant"
            for b in _blocks(row) if b.get("type") == "tool_use" and b.get("id")}


def _last_tool_before(rows, index):
    for row in reversed(rows[:index]):
        if row.get("isSidechain") or row.get("type") != "assistant":
            continue
        for block in reversed(_blocks(row)):
            if block.get("type") == "tool_use":
                return block
    return None


def _ok_error_lines(text):
    lines = [line for line in text.splitlines() if "Shell cwd was reset to" not in line]
    matches = []
    has_exc = any(EXCEPTION_LINE.search(line) for line in lines)
    for i, line in enumerate(lines):
        if not ERROR_LINE.search(line):
            continue
        if line.startswith("Traceback (most recent call last):") and has_exc:
            continue
        display = line.strip()
        if EXIT_STATUS.match(display):
            prev = next((lines[j].strip() for j in range(i - 1, -1, -1) if lines[j].strip()), None)
            if prev:
                display = f"{prev} | {display}"
        matches.append(display[:200])
        if len(matches) >= 3:
            break
    return matches


def _context_calls(rows):
    seen, calls = set(), []
    for index, row in enumerate(rows):
        if row.get("isSidechain") or row.get("type") != "assistant":
            continue
        message = row.get("message") or {}
        if message.get("model") == "<synthetic>":
            continue
        mid = message.get("id") or row.get("requestId")
        if not mid or mid in seen:
            continue
        seen.add(mid)
        usage = message.get("usage") or {}
        ctx = sum(int(usage.get(key) or 0) for key in (
            "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        if ctx:
            calls.append({"index": index, "row": row, "ctx": ctx})
    return calls


def _crossing_thresholds(max_ctx):
    thresholds = [100_000, 120_000, 150_000, 200_000, 250_000, 300_000]
    t = 400_000
    while t <= max_ctx:
        thresholds.append(t)
        t += 100_000
    return thresholds


def _section_context(rows, prompts, first, summary_only=False):
    calls = _context_calls(rows)
    items, top_tools = _tool_result_evidence(rows, prompts, first)
    intro = ("(Main-thread calls before the cut; context = input + cache read + cache write "
             "tokens.)")
    if calls:
        start, last = calls[0], calls[-1]
        peak = max(calls, key=lambda c: (c["ctx"], -c["index"]))
        at_prompt = []
        for i, (idx, _) in enumerate(prompts):
            after = next((c for c in calls if c["index"] > idx), None)
            at_prompt.append(f"P{i} {_k(after['ctx']) if after else '-'}")
        crossings = []
        for threshold in _crossing_thresholds(peak["ctx"]):
            hit = next((c for c in calls if c["ctx"] >= threshold), None)
            if hit:
                crossings.append(f"{threshold // 1000}k at +{_minutes(hit['row'], first)} min "
                                 f"({_prompt_at(prompts, hit['index'])})")
        drops = []
        for prev, cur in zip(calls, calls[1:]):
            if cur["ctx"] < prev["ctx"] * 0.7:
                drops.append(f"{_k(prev['ctx'])} -> {_k(cur['ctx'])} at "
                             f"+{_minutes(cur['row'], first)} min "
                             f"({_prompt_at(prompts, cur['index'])})")
        calls_line = (f"Calls: {len(calls)}; start {_k(start['ctx'])}; max {_k(peak['ctx'])} at "
                      f"+{_minutes(peak['row'], first)} min ({_prompt_at(prompts, peak['index'])}); "
                      f"last {_k(last['ctx'])}.")
    else:
        at_prompt = [f"P{i} -" for i in range(len(prompts))]
        crossings, drops = [], []
        calls_line = "Calls: 0; start -; max -; last -."
    list_full = [at_prompt, crossings, drops]
    list_meta = [
        ("At each typed prompt: ", "(none)"),
        ("Crossings: ", "none"),
        ("Drops over 30%: ", "none"),
    ]
    # Full lists first (byte-identical for real sessions); shrink only when over the cap.
    kept = [list(parts) for parts in list_full]
    tail = "Tool result chars by tool: " + top_tools

    def count_lines(kept_parts):
        lines = [intro, calls_line]
        for (label, empty), parts, full in zip(list_meta, kept_parts, list_full):
            if parts == full:
                lines.append(label + (", ".join(parts) if parts else empty))
            elif not full:
                lines.append(label + empty)
            elif not parts:
                lines.append(label + f"({len(full)} more)")
            else:
                lines.append(label + ", ".join(parts) + f", ({len(full) - len(parts)} more)")
        lines.append("Largest tool results:")
        return lines

    def render(kept_parts, with_items):
        counts = count_lines(kept_parts)
        if summary_only or not with_items:
            return _fit_section("Context", counts + [tail], [], CONTEXT_CAP, "tool results")
        body = _fit_section("Context", counts, items, CONTEXT_CAP - len(tail) - 1, "tool results")
        return body + "\n" + tail

    def head_len(kept_parts):
        return len("## Context\n" + "\n".join(count_lines(kept_parts) + [tail]))

    # Binary-search each variable list so the count block fits; real sessions stay full.
    if head_len(kept) > CONTEXT_CAP:
        for i, full in enumerate(list_full):
            if not full or head_len(kept) <= CONTEXT_CAP:
                continue
            lo, hi, best = 0, len(full), 0
            while lo <= hi:
                mid = (lo + hi) // 2
                trial = [list(parts) for parts in kept]
                trial[i] = full[:mid]
                if head_len(trial) <= CONTEXT_CAP:
                    best, lo = mid, mid + 1
                else:
                    hi = mid - 1
            kept[i] = full[:best]
    text = render(kept, with_items=not summary_only)
    if len(text) > CONTEXT_CAP:
        text = render([[], [], []], with_items=False)
    return text if len(text) <= CONTEXT_CAP else rate._truncate(text, CONTEXT_CAP)


def _section_harness(rows, prompts, first, summary_only=False):
    system_uuids, items = _guard_system_uuids(rows), []
    guards = silent = decisions = edited = ide = reminders = 0
    edited_names, ide_names, other = [], [], Counter()
    for index, row in enumerate(rows):
        if row.get("isSidechain"):
            continue
        if row.get("type") == "attachment":
            att = row.get("attachment") or {}
            kind, hook = att.get("type"), att.get("hookName") or ""
            if kind in ATTACHMENT_NOISE or (isinstance(hook, str) and hook.startswith("SessionStart")):
                continue
            if isinstance(att.get("hookEvent"), str) and att["hookEvent"].startswith("SessionStart"):
                continue
            stamp = f"+{_minutes(row, first)} min ({_prompt_at(prompts, index)})"
            if kind == "hook_system_message" and att.get("hookEvent") == "UserPromptSubmit":
                guards += 1
                items.append(f"guard {stamp}: {_collapse(str(att.get('content') or ''), 200)}")
            elif kind == "hook_additional_context" and att.get("hookEvent") == "UserPromptSubmit":
                if row.get("parentUuid") in system_uuids:
                    continue
                guards += 1
                content = att.get("content")
                text = " ".join(content) if isinstance(content, list) else str(content or "")
                items.append(f"guard {stamp}: {_collapse(text, 200)}")
            elif kind == "silent_turn_reminder":
                silent += 1
                prev = _last_tool_before(rows, index)
                if prev:
                    items.append(f"silent-turn reminder {stamp} after {prev.get('name')} "
                                 f"{_short(prev.get('input'), 100)}")
                else:
                    items.append(f"silent-turn reminder {stamp}")
            elif kind == "hook_success":
                decision, reason = _hook_decision(att.get("stdout"))
                if decision in ("ask", "deny"):
                    decisions += 1
                    items.append(f"hook {decision} {stamp} {hook}: {_collapse(str(reason or ''), 200)}")
            elif kind == "hook_system_message":
                items.append(f"hook message {stamp} {hook}: "
                             f"{_collapse(str(att.get('content') or ''), 200)}")
            elif kind == "edited_text_file":
                edited += 1
                name = Path(str(att.get("filename") or "")).name
                if name and name not in edited_names and len(edited_names) < 5:
                    edited_names.append(name)
            elif kind == "hook_additional_context" and att.get("hookEvent") == "PostToolUse":
                content = att.get("content")
                text = " ".join(content) if isinstance(content, list) else str(content or "")
                if "<ide_diagnostics>" in text:
                    ide += 1
                    for path in re.findall(r'"filePath"\s*:\s*"([^"]+)"', text):
                        name = Path(path).name
                        if name and name not in ide_names and len(ide_names) < 5:
                            ide_names.append(name)
            elif kind not in ("hook_success", "hook_additional_context"):
                other[kind or "?"] += 1
            continue
        if row.get("type") not in ("user", "assistant"):
            continue
        for block in _blocks(row):
            if block.get("type") != "tool_result":
                continue
            raw = _text(block.get("content"))
            for rem in SYSTEM_REMINDER.findall(raw):
                reminders += 1
                use = _tool_uses_by_id(rows).get(block.get("tool_use_id"))
                name = use[1].get("name") if use else "?"
                items.append(f"tool reminder +{_minutes(row, first)} min "
                             f"({_prompt_at(prompts, index)}) {name}: {_collapse(rem, 200)}")
    edited_s = f"File-changed notices: {edited}" + (f" ({', '.join(edited_names)})" if edited_names else "")
    ide_s = f"IDE diagnostics after edits: {ide}" + (f" ({', '.join(ide_names)})" if ide_names else "")
    other_s = ("Other attachments: " + ", ".join(f"{k} {n}" for k, n in sorted(other.items()))
               if other else "Other attachments: none")
    counts = [f"Context-guard messages: {guards}", f"Silent-turn reminders: {silent}",
              f"Hook ask/deny decisions: {decisions}", edited_s, ide_s,
              f"Reminders inside tool results: {reminders}", other_s]
    if summary_only:
        return _fit_section("Harness notes", counts, [], HARNESS_CAP, "harness notes")
    # Other attachments stays last; item lines sit before it.
    head, tail = counts[:-1], counts[-1]
    body = _fit_section("Harness notes", head, items, HARNESS_CAP - len(tail) - 1, "harness notes")
    return body + "\n" + tail


def _section_errors(rows, prompts, first, summary_only=False):
    uses = _tool_uses_by_id(rows)
    items, err_n, denied_n, ok_n = [], 0, 0, 0
    for index, row in enumerate(rows):
        if row.get("isSidechain") or row.get("type") != "user":
            continue
        for block in _blocks(row):
            if block.get("type") != "tool_result":
                continue
            use = uses.get(block.get("tool_use_id"))
            name = use[1].get("name") if use else "?"
            args = _short((use[1].get("input") if use else None) or {}, 160)
            use_row = use[0] if use else row
            use_index = next((i for i, r in enumerate(rows) if r is use_row), index)
            stamp = f"+{_minutes(use_row, first)} min ({_prompt_at(prompts, use_index)})"
            if block.get("is_error"):
                err_n += 1
                kind = "denied" if _denied(block) else "error"
                denied_n += kind == "denied"
                text = _one_line(_text(block.get("content")), 300)
                key = (name, kind, text)
                if items and items[-1]["key"] == key:
                    items[-1]["n"] += 1
                else:
                    items.append({"key": key, "n": 1, "stamp": stamp, "name": name,
                                  "args": args, "kind": kind, "text": text})
            elif name == "Bash":
                lines = _ok_error_lines(_text(block.get("content")))
                if lines:
                    ok_n += 1
                    text = " | ".join(lines)
                    key = (name, "ok", text)
                    if items and items[-1]["key"] == key:
                        items[-1]["n"] += 1
                    else:
                        items.append({"key": key, "n": 1, "stamp": stamp, "name": name,
                                      "args": args, "kind": "ok", "text": text})
    rendered = []
    for item in items:
        if item["kind"] == "ok":
            line = (f"{item['stamp']} {item['name']} {item['args']} "
                    f"-> ok with error lines: {item['text']}")
        else:
            line = (f"{item['stamp']} {item['name']} {item['args']} "
                    f"-> {item['kind']}: {item['text']}")
        if item["n"] > 1:
            line += f" (x{item['n']})"
        rendered.append(line)
    counts = [f"Error results: {err_n} (denied {denied_n})", f"Error lines in ok results: {ok_n}"]
    if summary_only:
        return _fit_section("Errors", counts, [], ERRORS_CAP, "errors")
    return _fit_section("Errors", counts, rendered, ERRORS_CAP, "errors")


def _section_cwd(rows, prompts, first, summary_only=False):
    main_rows = [(i, r) for i, r in enumerate(rows)
                 if r.get("type") in ("user", "assistant") and not r.get("isSidechain")
                 and not r.get("isMeta")]
    start = next((r.get("cwd") for _, r in main_rows if r.get("cwd")), None)
    moves, prev = [], None
    for index, row in main_rows:
        cwd = row.get("cwd")
        if not cwd:
            continue
        if prev is not None and cwd != prev:
            bash = None
            for _, earlier in reversed(main_rows[:main_rows.index((index, row)) + 1]):
                if earlier.get("type") != "assistant":
                    continue
                for block in reversed(_blocks(earlier)):
                    if block.get("type") == "tool_use" and block.get("name") == "Bash":
                        bash = block
                        break
                if bash:
                    break
            after = f" after Bash {_short(bash.get('input'), 160)}" if bash else ""
            moves.append(f"+{_minutes(row, first)} min ({_prompt_at(prompts, index)}) "
                         f"{_show_path(prev, start)} -> {_show_path(cwd, start)}{after}")
        prev = cwd
    uses = _tool_uses_by_id(rows)
    resets = []
    for index, row in enumerate(rows):
        if row.get("isSidechain") or row.get("type") != "user":
            continue
        for block in _blocks(row):
            if block.get("type") != "tool_result":
                continue
            text = _text(block.get("content"))
            match = CWD_RESET.search(text)
            if not match:
                continue
            use = uses.get(block.get("tool_use_id"))
            args = _short((use[1].get("input") if use else None) or {}, 160)
            use_row = use[0] if use else row
            use_index = next((i for i, r in enumerate(rows) if r is use_row), index)
            resets.append(f"+{_minutes(use_row, first)} min ({_prompt_at(prompts, use_index)}) "
                          f"Bash {args} -> reset to {_start_path(match.group(1).strip())}")
    move_items = moves[:10] + ([f"({len(moves) - 10} more)"] if len(moves) > 10 else [])
    reset_items = resets[:10] + ([f"({len(resets) - 10} more)"] if len(resets) > 10 else [])
    counts = [f"Start cwd: {_start_path(start)}", f"cwd moves: {len(moves)}"]
    if summary_only:
        counts += [f"Shell cwd resets: {len(resets)}"]
        return _fit_section("Shell cwd", counts, [], CWD_CAP, "cwd events")
    # Interleave: moves under their count, then resets count + items, under one cap.
    head = f"## Shell cwd\nStart cwd: {_start_path(start)}\ncwd moves: {len(moves)}"
    mid = "\n".join([head, *move_items, f"Shell cwd resets: {len(resets)}"])
    items = reset_items
    # Prefer keeping move lines; fit reset items into remaining budget.
    for n in range(len(items), -1, -1):
        body = [mid, *items[:n]]
        if n < len(items):
            body.append(f"({len(items) - n} more cwd events: see transcript.txt)")
        text = "\n".join(body)
        if len(text) <= CWD_CAP:
            return text
    # If moves alone overflow, trim move items next.
    base = f"## Shell cwd\nStart cwd: {_start_path(start)}\ncwd moves: {len(moves)}"
    for n in range(len(move_items), -1, -1):
        body = [base, *move_items[:n], f"Shell cwd resets: {len(resets)}"]
        if n < len(move_items):
            body.append(f"({len(move_items) - n} more cwd events: see transcript.txt)")
        text = "\n".join(body)
        if len(text) <= CWD_CAP:
            return text
    return (f"## Shell cwd\nStart cwd: {_start_path(start)}\ncwd moves: {len(moves)}\n"
            f"Shell cwd resets: {len(resets)}")


def _results(rows):
    return {b["tool_use_id"]: (row, b) for row in rows for b in _blocks(row)
            if b.get("type") == "tool_result" and b.get("tool_use_id")}


def _answers(result):
    if result is None:
        return {}
    row, block = result
    answers = dict(re.findall(r'"([^"\n]*)"\s*=\s*"([^"\n]*)"', _text(block.get("content"))))
    structured = row.get("toolUseResult")
    if isinstance(structured, dict) and isinstance(structured.get("answers"), dict):
        answers.update(structured["answers"])
    return answers


def _picker(block, result):
    args = block.get("input") or {}
    structured = result[0].get("toolUseResult") if result else None
    questions = args.get("questions") or (structured.get("questions", [])
                                         if isinstance(structured, dict) else [])
    answers = _answers(result)
    text = _clean(_text(result[1].get("content"))) if result else "no result"
    failed = result is None or result[1].get("is_error") or DENIED.match(text)
    lines, chosen = [], []
    for q in questions:
        answer = answers.get(q.get("question"), answers.get(q.get("header")))
        if answer is not None and not failed:
            answer = ", ".join(map(str, answer)) if isinstance(answer, list) else str(answer)
            chosen.append(answer)
        else:
            answer = f"(no answer: {text[:200]})"
        lines.extend([str(q.get("header", "")), str(q.get("question", "")),
                      "Options: " + "; ".join(str(o.get("label", "")) for o in q.get("options", [])),
                      "Answer: " + answer])
    return _clean("\n".join(lines)), "; ".join(chosen)


def _main(rows):
    return [r for r in rows if r.get("type") in ("user", "assistant")
            and not r.get("isSidechain") and not r.get("isMeta")]


def friction_events(rows):
    events, previous = [], None
    for i, row in enumerate(rows):
        for block in _blocks(row):
            if block.get("type") == "tool_result" and block.get("is_error"):
                events.append({"index": i, "kind": "denied" if _denied(block) else "error"})
        if row.get("type") == "assistant":
            ts = rate._aware(row.get("timestamp"))
            if ts is not None:
                if previous is not None and (ts - previous).total_seconds() >= 3600:
                    events.append({"index": i, "kind": "idle"})
                previous = ts
    return events


def _row_text(row, prompt, tool_limit=300):
    lines = []
    if prompt is not None:
        lines.append("USER: " + rate._truncate(prompt["text"], 2000))
    if row.get("type") == "assistant":
        text = _clean("\n".join(b.get("text", "") for b in _blocks(row) if b.get("type") == "text"))
        if text:
            lines.append("CLAUDE: " + rate._truncate(text, 2000))
    for block in _blocks(row):
        kind = block.get("type")
        if row.get("type") == "assistant" and kind == "tool_use":
            lines.append(f"TOOL {block.get('name')}: {_short(block.get('input'), tool_limit)}")
        elif kind == "tool_result":
            text = _text(block.get("content"))
            lines.append(f"RESULT {'error' if block.get('is_error') else 'ok'} ({len(text)} chars): "
                         + _clean(text)[:500])
    return "\n".join(lines)


def compressed_transcript(rows, prompts, first):
    results = _results(_main(rows))
    system_uuids = _guard_system_uuids(rows)
    lines = []
    for i, row in enumerate(rows):
        if row.get("isSidechain") or row.get("isMeta"):
            continue
        minutes = _minutes(row, first)
        if i in prompts:
            lines.append(f"USER [+{minutes}]: {rate._truncate(prompts[i]['text'], 2000)}")
        if row.get("type") == "attachment":
            att = row.get("attachment") or {}
            kind, hook = att.get("type"), att.get("hookName") or ""
            if isinstance(hook, str) and hook.startswith("SessionStart"):
                continue
            if isinstance(att.get("hookEvent"), str) and att["hookEvent"].startswith("SessionStart"):
                continue
            if kind == "hook_system_message" and att.get("hookEvent") == "UserPromptSubmit":
                lines.append(f"HARNESS [+{minutes}] guard: "
                             f"{_collapse(str(att.get('content') or ''), 160)}")
            elif (kind == "hook_additional_context" and att.get("hookEvent") == "UserPromptSubmit"
                  and row.get("parentUuid") not in system_uuids):
                content = att.get("content")
                text = " ".join(content) if isinstance(content, list) else str(content or "")
                lines.append(f"HARNESS [+{minutes}] guard: {_collapse(text, 160)}")
            elif kind == "silent_turn_reminder":
                lines.append(f"HARNESS [+{minutes}] silent-turn reminder")
            elif kind == "hook_success":
                decision, reason = _hook_decision(att.get("stdout"))
                if decision == "ask":
                    lines.append(f"HARNESS [+{minutes}] hook ask {hook}: "
                                 f"{_collapse(str(reason or ''), 160)}")
            continue
        if row.get("type") != "assistant":
            continue
        for block in _blocks(row):
            if block.get("type") == "text":
                text = _clean(block.get("text", ""))
                if text:
                    lines.append(f"CLAUDE [+{minutes}]: {rate._truncate(text, 4000)}")
            elif block.get("type") == "tool_use":
                result = results.get(block.get("id"))
                text = _text(result[1].get("content")) if result else ""
                status = "error" if result and result[1].get("is_error") else "ok"
                suffix = " (no result)" if result is None else ""
                if status == "error" and result is not None:
                    suffix += ": " + _one_line(text, 160)
                if result is not None and "Shell cwd was reset to" in text:
                    suffix += "; cwd reset"
                if block.get("name") == "AskUserQuestion":
                    _, chosen = _picker(block, result)
                    if chosen:
                        suffix += "; answers: " + " ".join(chosen.split())
                lines.append(f"TOOL [+{minutes}] {block.get('name')} "
                             f"{_short(block.get('input'), 160)} -> {status}, {len(text)} chars{suffix}")
    return "\n".join(lines) + "\n"


def _read_copy(path):
    if not isinstance(path, str):
        return None
    try:
        with Path(path).open(encoding="utf-8", errors="replace") as stream:
            text = rate._truncate(stream.read(rate.HANDOFF_LIMIT + 1), rate.HANDOFF_LIMIT)
    except (OSError, ValueError):
        return None
    return "\n".join("> " + line if line else ">" for line in text.split("\n"))


def _numbers(record, usage, denied, cut):
    summary = record["summary"]
    gaps = ", ".join(str(r["gap_min"]) for r in usage["idle_rebuilds"]) or "none"
    share = usage["idle_rebuild_units"] / usage["cost_units"] if usage["cost_units"] else 0
    lines = ["## Numbers", "(Whole session, including calls after the cut.)" if cut is not None
             else "(Whole session.)", f"Session: {record['session'][:8]}; mode: {_mode(record)}",
             f"Date: {summary.get('first_call', 'unknown')}; model: {summary.get('model')}",
             f"Calls: {summary.get('calls', 0)} main; {summary.get('subagent_calls', 0)} subagent",
             f"Context max: {summary.get('context_max', 0)}; cost units: {summary.get('cost_units', 0)}; "
             f"USD at list price: {summary.get('usd_list_price')}",
             f"Idle rebuilds: {len(usage['idle_rebuilds'])}; gap minutes: {gaps}; "
             f"share of cost units: {share:.2%}", f"Denied/blocked calls: {denied}",
             "Delegate commands: " + json.dumps(summary.get("delegate") or {}, sort_keys=True)]
    pools = (summary.get("quota") or {}).get("pools") or {}
    for pool, entry in sorted(pools.items()):
        if isinstance(entry, dict) and entry.get("delta") is not None:
            lines.append(f"Quota {pool}: start {entry.get('start')}; end {entry.get('end')}; "
                         f"delta {entry['delta']}")
    return "\n".join(lines)


def _digest(record, prompts, usage, all_denied, cut, end_handoff):
    rows = record["rows"]
    main = _main(rows)
    events = friction_events(main)
    first = rate._aware(prompts[0][1]["timestamp"]) if prompts else None
    lookup = {id(rows[i]): p for i, p in prompts}
    handoff, end = _read_copy(record["start"].get("handoff")), _read_copy(end_handoff)
    texts = [p["text"] for _, p in prompts]
    if handoff is None:
        texts = [rate.NEXT_PROMPT_NO_COPY + t[len(rate.NEXT_PROMPT):]
                 if t.startswith(rate.NEXT_PROMPT) else t for t in texts]
    labels = [f"### P{i} ({_minutes(p, first)} min)" for i, (_, p) in enumerate(prompts)]
    results = _results(main)
    pickers = []
    for block in _tools(main):
        if block.get("name") == "AskUserQuestion":
            text, _ = _picker(block, results.get(block.get("id")))
            pickers.append(f"### Picker {len(pickers) + 1}\n{text}")
    windows = []
    for n, event in enumerate(events, 1):
        event["label"] = f"### E{n} {event['kind']} at +{_minutes(main[event['index']], first)} min"
        lo, hi = max(0, event["index"] - 3), min(len(main), event["index"] + 4)
        if windows and lo <= windows[-1]["hi"]:
            windows[-1]["hi"] = max(windows[-1]["hi"], hi)
            windows[-1]["events"].append(event)
        else:
            windows.append({"lo": lo, "hi": hi, "events": [event]})
    for window in windows:
        window["text"] = "\n".join(e["label"] for e in window["events"]) + "\n" + "\n".join(
            _row_text(row, lookup.get(id(row))) for row in main[window["lo"]:window["hi"]])
    numbers = _numbers(record, usage, all_denied, cut)
    _, last = rate.session_window(record)
    cwd = record["start"].get("cwd") or record["start"].get("repo_dir") or next(
        (r["cwd"] for r in rows if r.get("cwd")), None)
    commits = rate.session_commits(cwd, first, last, slack_minutes=0 if cut is not None else 10)
    tail = ["## HANDOFF.md at session start\n" + (handoff or "(no start-time copy was saved)"),
            "## HANDOFF.md at session end\n" + ("(no end-state copy was saved)" if end is None
                else "(unchanged from the start copy)" if end == handoff else end),
            "## Commits in the session window\n" + commits]
    omitted = 0
    shrink_new = False

    def parts():
        friction = "\n\n".join(w["text"] for w in windows)
        if omitted:
            friction += f"\n({omitted} more friction events: see the transcript file)"
        return [
            numbers,
            _section_context(rows, prompts, first, shrink_new),
            "## Prompts\n" + "\n\n".join(label + "\n" + text for label, text in zip(labels, texts)),
            "## Pickers\n" + ("\n\n".join(pickers) or "(none)"),
            *tail,
            _section_harness(rows, prompts, first, shrink_new),
            _section_errors(rows, prompts, first, shrink_new),
            _section_cwd(rows, prompts, first, shrink_new),
            "## Friction excerpts\n" + (friction or "(none)"),
        ]

    def render():
        return "\n\n".join(parts()) + "\n"

    digest = render()
    if len(digest) > rate.PACKET_LIMIT:
        shrink_new = True
        digest = render()
    while len(digest) > rate.PACKET_LIMIT and windows:
        drop = max(range(len(windows)), key=lambda i: (
            min(PRIORITY[e["kind"]] for e in windows[i]["events"]), windows[i]["hi"]))
        omitted += len(windows.pop(drop)["events"])
        digest = render()
    while len(digest) > rate.PACKET_LIMIT and any(texts):
        i = max(range(len(texts)), key=lambda i: len(texts[i]))
        texts[i] = rate._truncate(texts[i], max(0, len(texts[i]) - len(digest) + rate.PACKET_LIMIT))
        digest = render()
    if len(digest) > rate.PACKET_LIMIT:
        # Reserve Numbers, Context and Pickers; Prompts truncated next; the rest as one blob.
        sections = parts()
        budget = rate.PACKET_LIMIT - len(sections[0]) - len(sections[1]) - 5
        sections[3] = rate._truncate(sections[3], max(0, budget - 100))
        budget -= len(sections[3])
        sections[2] = rate._truncate(sections[2], min(len(sections[2]), max(0, budget // 2)))
        rest = rate._truncate("\n\n".join(sections[4:]), max(0, budget - len(sections[2]) - 4))
        digest = "\n\n".join(sections[:4] + [rest]) + "\n"
        digest = rate._truncate(digest, rate.PACKET_LIMIT)
    return digest, {kind: sum(e["kind"] == kind for e in events) for kind in PRIORITY}


def build_digest(record, starts, weights=None, prices=None):
    """Return evidence text or a dropped result; no output files are written here."""
    result = {"session": record["session"], "status": "dropped", "reason": None, "cut_row": None,
              "rows": 0, "handoff_after_cut": False, "digest_chars": 0, "transcript_chars": 0,
              "events": dict.fromkeys(PRIORITY, 0), "digest": None, "transcript": None}
    path = record["summary"].get("transcript") or record["start"].get("transcript")
    try:
        if not path:
            raise ValueError("missing transcript")
        rows = rate.json_rows(path)
        if weights is None:
            weights, prices = cost.load_catalog_cost()
        usage = cost.summarise_session(cost.parse_session_usage(path), weights, prices)
    except (OSError, ValueError):
        result["reason"] = "unreadable transcript"
        return result
    prompts = list(rate._typed_prompt_rows(rows))
    cut = next((i for i, p in prompts if p["text"].startswith("[command /field-report")), None)
    kept = rows[:cut] if cut is not None else rows
    prompts = [(i, p) for i, p in prompts if cut is None or i < cut]
    result.update(cut_row=cut, rows=len(kept),
                  handoff_after_cut=cut is not None and handoff_written(rows[cut:]))
    reason = _leak_reason(kept, prompts)
    if result["handoff_after_cut"]:
        reason = "HANDOFF.md written after the /field-report prompt"
    if reason:
        result["reason"] = reason
        return result
    record = {**record, "rows": kept, "prompts": [p for _, p in prompts],
              "summary": dict(record["summary"])}
    if cut is not None:
        record["summary"]["last_call"] = rows[cut].get("timestamp")
    end, _ = rate.end_state(record, starts)
    digest, events = _digest(record, prompts, usage, denied_calls(rows), cut, end)
    first = rate._aware(prompts[0][1]["timestamp"]) if prompts else None
    transcript = compressed_transcript(kept, dict(prompts), first)
    result.update(status="built", digest=digest, transcript=transcript, digest_chars=len(digest),
                  transcript_chars=len(transcript), events=events)
    return result


def write_digests(rows, sessions, out):
    # Reuse rate's full-id/unique-prefix resolution, including its error semantics.
    records = rate.select_sessions(rows, sessions=sessions)
    if len({r['session'][:8] for r in records}) != len(records):
        raise ValueError("selected sessions share an 8-character prefix")
    starts = [r for r in rows if r.get("event") == "start"]
    weights, prices = cost.load_catalog_cost()
    built = []
    for record in records:
        session = record["session"]
        if not re.fullmatch(r"[0-9a-fA-F-]{8,}", session):
            raise ValueError("invalid session id")
        result = build_digest(record, starts, weights, prices)
        if result["status"] == "built":
            directory = Path(out) / session[:8]
            directory.mkdir(parents=True, exist_ok=True)
            for key, name in (("digest", "digest.md"), ("transcript", "transcript.txt")):
                path = directory / name
                path.write_text(result[key], encoding="utf-8")
                result[key] = str(path)
        built.append(result)
    return {"sessions": built}

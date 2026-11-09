"""Replace secret-shaped strings in rater packets. Stdlib only; no hooks/ import."""
import re

# Built from parts so this file's source is not itself a hit when scanned.
_BEGIN = "-----" + "BEGIN "
_PRIV = "PRIVATE " + "KEY"
_END = "-----" + "END "
_GH_PAT = "github" + "_pat_"
_SK_ANT = "sk-" + "ant-"
_SK = "sk-"
_AKIA = "AKIA"
_ASIA = "ASIA"
_XOX = "xox"
_AIZA = "AIza"
_SK_LIVE = "sk_" + "live_"
_RK_LIVE = "rk_" + "live_"

# Same seven patterns as hooks/secret-guard.py TOKEN_RULES (asserted in tests).
TOKEN_RULES = [
    ("GitHub token", re.compile(
        r"(?:gh[pousr]_[A-Za-z0-9]{36,}|" + re.escape(_GH_PAT) + r"[A-Za-z0-9_]{22,})")),
    ("AWS access key", re.compile(
        r"(?:" + _AKIA + r"|" + _ASIA + r")[0-9A-Z]{16}")),
    ("Anthropic key", re.compile(
        re.escape(_SK_ANT) + r"[A-Za-z0-9_-]{20,}")),
    ("OpenAI key", re.compile(
        r"\b" + re.escape(_SK) + r"(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{20,}")),
    ("Slack token", re.compile(
        _XOX + r"[abposr]-[A-Za-z0-9-]{10,}")),
    ("Google API key", re.compile(
        _AIZA + r"[0-9A-Za-z_-]{35}")),
    ("Stripe key", re.compile(
        r"(?:" + re.escape(_SK_LIVE) + r"|" + re.escape(_RK_LIVE) + r")[A-Za-z0-9]{20,}")),
]

SECRET = "[secret]"


def _line_endswith_dashed(text, line_start, line_end):
    """True when text[line_start:line_end].rstrip('\\r') ends with -----; returns check_end or -1."""
    check_end = line_end
    while check_end > line_start and text[check_end - 1] == "\r":
        check_end -= 1
    if check_end < line_start + 5:
        return -1
    if text.find("-----", check_end - 5, check_end) != check_end - 5:
        return -1
    return check_end


def _last_priv_on_line(text, line_start, line_end):
    last = -1
    pos = line_start
    while pos < line_end:
        found = text.find(_PRIV, pos, line_end)
        if found < 0:
            break
        last = found
        pos = found + len(_PRIV)
    return last


def _end_line_match(text, line_start, line_end):
    """First -----END on the line that the reference would accept, or -1.

    Reference: some -----END on the line has PRIVATE KEY after it on the same line,
    and text[end_pos:line_end].rstrip('\\r') ends with -----. Because rstrip only
    trims the line end, every end_pos shares that endswith result; the first END
    with PRIVATE KEY after it and end_pos far enough left to include the dashes
    is therefore the same as checking each END in order.
    """
    check_end = _line_endswith_dashed(text, line_start, line_end)
    if check_end < 0:
        return -1
    last_priv = _last_priv_on_line(text, line_start, line_end)
    if last_priv < 0:
        return -1
    cursor = line_start
    while cursor < line_end:
        end_pos = text.find(_END, cursor, line_end)
        if end_pos < 0:
            break
        if end_pos <= check_end - 5 and last_priv >= end_pos:
            return line_end + 1 if line_end < len(text) else line_end
        cursor = end_pos + len(_END)
    return -1


def _private_key_spans(text):
    """Linear BEGIN…PRIVATE KEY through END line (or EOF). No [^\n]* backtracking.

    Caches the current line's newline (or len) so a long line without \\n is not
    re-scanned on every PRIVATE KEY hit. END matching follows the reference rule.
    """
    spans = []
    n = len(text)
    start = 0
    line_start = 0
    line_nl = None  # index of \\n on this line, or n if none; None = unknown
    begin_searched_to = 0
    pending_begin = -1
    while True:
        pos = text.find(_PRIV, start)
        if pos < 0:
            break
        while True:
            if line_nl is None:
                found_nl = text.find("\n", line_start)
                line_nl = n if found_nl < 0 else found_nl
            if line_nl >= pos:
                break
            line_start = line_nl + 1
            line_nl = None
            begin_searched_to = line_start
            pending_begin = -1
        if begin_searched_to < pos:
            found = text.find(_BEGIN, begin_searched_to, pos)
            begin_searched_to = pos
            if found >= 0 and pending_begin < 0:
                pending_begin = found
        begin_at = pending_begin if 0 <= pending_begin < pos else -1
        if begin_at < 0:
            start = pos + len(_PRIV)
            continue
        # Header ends at the newline after this PRIVATE KEY (same as reference).
        if line_nl is None:
            found_nl = text.find("\n", pos)
            line_nl = n if found_nl < 0 else found_nl
        header_nl = line_nl if line_nl < n else -1
        search_from = n if header_nl < 0 else header_nl + 1
        end_at = -1
        walk = search_from
        while walk < n:
            walk_nl = text.find("\n", walk)
            walk_end = n if walk_nl < 0 else walk_nl
            matched = _end_line_match(text, walk, walk_end)
            if matched >= 0:
                end_at = matched
                break
            walk = n if walk_nl < 0 else walk_nl + 1
        if end_at < 0:
            spans.append((begin_at, n))
            break
        spans.append((begin_at, end_at))
        start = end_at
        line_start = end_at
        line_nl = None
        begin_searched_to = end_at
        pending_begin = -1
    return spans


def _token_spans(text):
    spans = []
    for _label, pattern in TOKEN_RULES:
        for match in pattern.finditer(text):
            spans.append((match.start(), match.end()))
    return spans


def _merge(spans):
    if not spans:
        return []
    spans = sorted(spans)
    merged = [list(spans[0])]
    for start, end in spans[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def mask_secrets(text):
    """Replace API-token shapes and private-key blocks with [secret]. Identity if none."""
    if not text or not isinstance(text, str):
        return text
    spans = _merge(_private_key_spans(text) + _token_spans(text))
    if not spans:
        return text
    parts = []
    prev = 0
    for start, end in spans:
        parts.append(text[prev:start])
        parts.append(SECRET)
        prev = end
    parts.append(text[prev:])
    return "".join(parts)

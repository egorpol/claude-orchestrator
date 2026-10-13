#!/usr/bin/env python3
"""Stop secrets from leaving the machine on a push.

Three entry points, one file:

* Claude Code PreToolUse (Bash): JSON on stdin, no argv. Cheap substring prefilter,
  then parse; findings or a scan that cannot finish return permissionDecision "ask"
  (never "allow", which would skip the user's own permission rules). A commit-making
  git segment before a push in the same Bash call returns "deny".
* git pre-push: argv = remote name and URL; stdin lines are the ref updates. Findings
  or errors go to stderr and exit 1 (override with git push --no-verify).
* ``secret-guard.py --check [DIR]``: scan full history of all refs plus uncommitted
  changes; exit 1 on findings, else one-line all-clear.

Env: SECRET_GUARD_BUDGET_S (default 20 for push hooks; bounds --check only when set),
SECRET_GUARD_DISABLE=1 (exit 0 silently, same convention as CONTEXT_GUARD_DISABLE).

Guiding principle: false positives cost one click; a miss leaks a secret. Where
emulating git exactly is hard, scan more, not less.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import subprocess
import sys
import time
from pathlib import Path

# --- shell parsers copied from hooks/session-mode.py; must stay identical ---


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


# --- end copied parsers ---

ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
ZERO_SHA = "0" * 40
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
PLACEHOLDER_USERS = frozenset(
    {"user", "username", "you", "me", "name", "example", "runner"}
)
COMMIT_VERBS = frozenset({"commit", "merge", "cherry-pick", "revert", "am", "rebase"})
SHELL_KEYWORDS = frozenset({"if", "then", "elif", "else", "do", "while", "until", "!", "{"})
# Distinct from None (which means "use default 20 s"). Only an explicit env bounds --check.
_UNLIMITED = object()

# Built from parts so this file's source is not itself a hit when scanned.
_BEGIN = "-----" + "BEGIN "
_PRIV = "PRIVATE " + "KEY"
_PRIV_SHOWN = "PRIVATE " + "KEY header"
_GH_PAT = "github" + "_pat_"
_SK_ANT = "sk-" + "ant-"
_SK = "sk-"
_AKIA = "AKIA"
_ASIA = "ASIA"
_XOX = "xox"
_AIZA = "AIza"
_SK_LIVE = "sk_" + "live_"
_RK_LIVE = "rk_" + "live_"

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
# Path prefixes split so this file's source is not itself a hit.
_HOME_NIX = "/" + "home" + "/"
_HOME_MAC = "/" + "Users" + "/"
_HOME_WIN_BS = "\\" + "Users" + "\\"
_HOME_WIN_SL = "/" + "Users" + "/"
_USER = r"[A-Za-z0-9._-]+"
# Char before the leading / must not be a word character, `.` or `-`.
# re.ASCII so (?i) Windows matching agrees with a lower() prefilter (no long-ſ).
HOME_PATH = re.compile(
    r"(?<![A-Za-z0-9._-])(?:"
    + re.escape(_HOME_NIX) + r"(" + _USER + r")/"
    + r"|" + re.escape(_HOME_MAC) + r"(" + _USER + r")/"
    + r"|(?i:C:" + re.escape(_HOME_WIN_BS) + r")(" + _USER + r")(?i:\\)"
    + r"|(?i:C:" + re.escape(_HOME_WIN_SL) + r")(" + _USER + r")(?i:/)"
    + r")",
    re.ASCII,
)
# Literal prefilters: a rule's regex runs only when the text contains one of these.
# Built from parts so this file's source is not itself a hit. Folded literals are
# checked against text.lower() (Windows home paths).
_HINT_GH = "gh"
_WIN_FOLD_BS = "c:" + "\\" + "users" + "\\"
_WIN_FOLD_SL = "c:" + "/" + "users" + "/"
# Private-key headers are found linearly (no [^\n]* regex — that backtracks).
# (kind, pattern, literals, fold_literals)
SCAN_RULES = [
    ("GitHub token", TOKEN_RULES[0][1], (_HINT_GH, _GH_PAT), ()),
    ("AWS access key", TOKEN_RULES[1][1], (_AKIA, _ASIA), ()),
    ("Anthropic key", TOKEN_RULES[2][1], (_SK_ANT,), ()),
    ("OpenAI key", TOKEN_RULES[3][1], (_SK,), ()),
    ("Slack token", TOKEN_RULES[4][1], (_XOX,), ()),
    ("Google API key", TOKEN_RULES[5][1], (_AIZA,), ()),
    ("Stripe key", TOKEN_RULES[6][1], (_SK_LIVE, _RK_LIVE), ()),
    ("home path", HOME_PATH, (_HOME_NIX, _HOME_MAC), (_WIN_FOLD_BS, _WIN_FOLD_SL)),
]
SENSITIVE_NAMES = frozenset({
    ".env", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", ".netrc", ".pgpass",
})
SENSITIVE_SUFFIXES = (".pem", ".p12", ".pfx", ".key")
ENV_OK = frozenset({".env.example", ".env.sample", ".env.template"})
_OID = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$", re.I)
_DIFF_FLAGS = (
    "--no-ext-diff", "--no-textconv", "--no-color", "--root", "--no-relative",
    "--src-prefix=a/", "--dst-prefix=b/",
)
_WT_DIFF_FLAGS = (
    "--no-ext-diff", "--no-textconv", "--no-color", "--no-relative",
    "--src-prefix=a/", "--dst-prefix=b/",
)


def _split_lf(text):
    """Split on LF only — str.splitlines also breaks on \\r, \\v, etc."""
    if not text:
        return []
    return text.split("\n")


def _dequote_path(path):
    """Undo git's C-style quoted path (when not using -z)."""
    if len(path) >= 2 and path[0] == '"' and path[-1] == '"':
        out, i, s = [], 1, path
        while i < len(s) - 1:
            if s[i] == "\\" and i + 1 < len(s) - 1:
                nxt = s[i + 1]
                escapes = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f",
                           '"': '"', "\\": "\\", "a": "\a", "v": "\v"}
                if nxt in escapes:
                    out.append(escapes[nxt])
                    i += 2
                    continue
                if nxt in "0123456789":
                    j = i + 1
                    while j < len(s) - 1 and j < i + 4 and s[j] in "0123456789":
                        j += 1
                    out.append(chr(int(s[i + 1:j], 8) & 0xFF))
                    i = j
                    continue
            out.append(s[i])
            i += 1
        return "".join(out)
    return path


def _is_glob_ref(ref):
    return any(ch in ref for ch in "*?[")


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _mask(value):
    text = value if isinstance(value, str) else str(value)
    if len(text) <= 4:
        return text + "…" + str(len(text))
    return text[:4] + "…" + str(len(text))


def redact(text):
    """Mask any token/key rule match in an output string (never leak full secrets)."""
    if not text or not isinstance(text, str):
        return text
    out = text
    for _label, pattern in TOKEN_RULES:
        out = pattern.sub(lambda m: _mask(m.group(0)), out)
    if _BEGIN in out and _PRIV in out:
        out = re.sub(
            re.escape(_BEGIN) + r"[^\n]*" + re.escape(_PRIV) + r"[^\n]*",
            _PRIV_SHOWN,
            out,
        )
    return out


class BudgetExceeded(Exception):
    pass


class GitError(Exception):
    pass


class Guard:
    def __init__(self, budget_s=_UNLIMITED):
        if budget_s is _UNLIMITED:
            # --check: unbounded unless SECRET_GUARD_BUDGET_S is explicitly set.
            if "SECRET_GUARD_BUDGET_S" in os.environ:
                self.budget_s = _env_float("SECRET_GUARD_BUDGET_S", 20.0)
            else:
                self.budget_s = None
        elif budget_s is None:
            self.budget_s = _env_float("SECRET_GUARD_BUDGET_S", 20.0)
        else:
            self.budget_s = budget_s
        self.started = time.monotonic()
        self.findings = []
        self._seen = set()
        self._scan_hits = 0  # per-push hits, counting before global dedupe
        self.git_opts = []
        self._home_roots = self._extra_home_roots()
        self.home_paths = True  # per repo, see load_repo_config
        self._remerge = None  # None=untried, True/False after probe
        self.log_marker = secrets.token_hex(16)

    def load_repo_config(self, cwd, git_opts=None):
        """`git config secretGuard.homePaths false` turns the home-path rule off in that repo
        (a private repo full of its owner's paths); tokens, keys and file names stay on.
        `-c` overrides on the scanned command are ignored, so a command can't switch it off
        for its own push."""
        opts, words = [], list(git_opts if git_opts is not None else self.git_opts)
        i = 0
        while i < len(words):
            if words[i] == "-c":
                i += 2
                continue
            if not words[i].startswith("-c"):
                opts.append(words[i])
            i += 1
        proc = self.git(["config", "--type=bool", "--get", "secretGuard.homePaths"], cwd,
                        check=False, git_opts=opts)
        self.home_paths = proc.stdout.strip() != "false"

    def begin_push_scan(self):
        self._scan_hits = 0

    def _extra_home_roots(self):
        roots = []
        home = str(Path.home())
        for prefix in (_HOME_NIX, _HOME_MAC):
            if home.startswith(prefix) or home.lower().startswith(prefix.lower()):
                return roots
        if re.match(r"(?i)^C:" + re.escape("\\Users\\"), home) or re.match(
                r"(?i)^C:" + re.escape("/Users/"), home):
            return roots
        if home and home not in ("/",):
            roots.append(home.rstrip("/\\"))
        return roots

    def remaining(self):
        if self.budget_s is None:
            return None
        return max(0.05, self.budget_s - (time.monotonic() - self.started))

    def _tick(self):
        if self.budget_s is not None and (time.monotonic() - self.started) > self.budget_s:
            raise BudgetExceeded("time budget exceeded")

    def git(self, args, cwd, check=True, git_opts=None):
        self._tick()
        opts = list(git_opts if git_opts is not None else self.git_opts)
        cmd = ["git", *opts, *args]
        timeout = self.remaining()
        try:
            proc = subprocess.run(
                cmd, cwd=cwd, capture_output=True,
                env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"},
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise BudgetExceeded("time budget exceeded") from exc
        except OSError as exc:
            raise GitError(str(exc)) from exc
        self._tick()
        stdout = (proc.stdout or b"").decode("utf-8", errors="replace")
        stderr = (proc.stderr or b"").decode("utf-8", errors="replace")
        proc.stdout, proc.stderr = stdout, stderr
        if check and proc.returncode != 0:
            err = (stderr or stdout or "git failed").strip().splitlines()
            raise GitError(err[0] if err else "git failed")
        return proc

    def add(self, label, path, line, commit, value, mask=True):
        if label == "private key":
            shown = _PRIV_SHOWN
        else:
            shown = _mask(value) if mask else value
        commit = commit or "-"
        if _OID.fullmatch(str(commit)):
            commit = commit[:12]
        key = (label, path or "-", line or "-", commit, shown)
        self._scan_hits += 1  # count before dedupe so every push destination can be named
        if key in self._seen:
            return
        self._seen.add(key)
        self.findings.append({
            "label": label, "path": path or "-", "line": line or "-",
            "commit": commit, "value": shown,
        })

    def _find_private_key_hits(self, text):
        """Linear PRIVATE KEY header search — avoids quadratic [^\n]* backtracking."""
        hits = []
        start = 0
        while True:
            self._tick()
            pos = text.find(_PRIV, start)
            if pos < 0:
                break
            line_start = text.rfind("\n", 0, pos) + 1
            begin_at = text.find(_BEGIN, line_start, pos)
            if begin_at >= 0:
                line_end = text.find("\n", pos)
                if line_end < 0:
                    line_end = len(text)
                value = text[begin_at:line_end].strip()
                hits.append((begin_at, "private key", value, True))
            start = pos + len(_PRIV)
        return hits

    def scan_text(self, text, path, commit, start_line=1):
        if not text:
            return
        self._tick()
        # Per-rule regexes with literal prefilters (a leading lookbehind / \\b / big
        # alternation in one combined regex was slower than separate rules).
        hits = []  # (pos, label, value, mask)
        folded = None

        def hay_fold():
            nonlocal folded
            if folded is None:
                folded = text.lower()
            return folded

        if _PRIV in text:
            hits.extend(self._find_private_key_hits(text))

        for kind, pattern, lits, fold_lits in SCAN_RULES:
            if kind == "home path" and not self.home_paths:
                continue
            ok = any(lit in text for lit in lits) if lits else False
            if not ok and fold_lits:
                hay = hay_fold()
                ok = any(lit in hay for lit in fold_lits)
            if not ok:
                continue
            self._tick()
            for match in pattern.finditer(text):
                value = match.group(0)
                label, mask = kind, True
                if kind == "home path":
                    name = next(g for g in match.groups() if g is not None)
                    if name.lower() in PLACEHOLDER_USERS:
                        continue
                    mask = False
                elif kind == "OpenAI key" and value.startswith(_SK_ANT):
                    continue
                elif value.startswith(_SK_ANT) and kind in ("OpenAI key", "Anthropic key"):
                    label = "Anthropic key"
                hits.append((match.start(), label, value, mask))

        hits.sort(key=lambda h: h[0])
        # Count newlines only up to each match (kept from round-3 orchestrator fix).
        lineno, last = start_line, 0
        for pos, label, value, mask in hits:
            self._tick()
            lineno += text.count("\n", last, pos)
            last = pos
            self.add(label, path, lineno, commit, value, mask=mask)
        # Extra $HOME roots (rare) still need a linear pass when configured.
        if self._home_roots and self.home_paths:
            for offset, line in enumerate(_split_lf(text)):
                self._scan_extra_homes(line, path, start_line + offset, commit)

    def _scan_line(self, line, path, lineno, commit):
        """Scan one line (used for diff hunks where we already have the line)."""
        self.scan_text(line, path, commit, start_line=lineno)

    def _scan_extra_homes(self, line, path, lineno, commit):
        for root in self._home_roots:
            idx = 0
            while True:
                pos = line.find(root, idx)
                if pos < 0:
                    break
                if pos > 0 and (line[pos - 1].isalnum() or line[pos - 1] in "._-"):
                    idx = pos + 1
                    continue
                end = pos + len(root)
                if end < len(line) and line[end] not in "/\\":
                    idx = pos + 1
                    continue
                stop = end
                while stop < len(line) and not line[stop].isspace() and line[stop] not in "\"'":
                    stop += 1
                self.add("home path", path, lineno, commit, line[pos:stop], mask=False)
                idx = pos + 1

    def scan_path_name(self, relpath, commit):
        name = Path(relpath).name
        lower = name.lower()
        if name in SENSITIVE_NAMES or lower in SENSITIVE_NAMES:
            self.add("sensitive file", relpath, "-", commit, name, mask=False)
            return
        if name.startswith(".env.") and name not in ENV_OK and lower not in ENV_OK:
            self.add("sensitive file", relpath, "-", commit, name, mask=False)
            return
        # e.g. "weird .env" (git-quoted paths); still a dotenv file by suffix.
        if lower.endswith(".env") and name not in ENV_OK and lower not in ENV_OK:
            self.add("sensitive file", relpath, "-", commit, name, mask=False)
            return
        if any(lower.endswith(suf) for suf in SENSITIVE_SUFFIXES):
            self.add("sensitive file", relpath, "-", commit, name, mask=False)

    def scan_diff(self, diff_text, commit):
        """Parse a unified diff; +++ / --- are headers only before the first @@."""
        path = None
        binary = False
        state = "between"  # between | headers | hunk
        line_no = 0
        for line in _split_lf(diff_text):
            self._tick()
            if line.startswith("diff --git "):
                path = None
                binary = False
                state = "headers"
                line_no = 0
                continue
            if state == "headers":
                if line.startswith("Binary files ") or line.startswith("GIT binary patch"):
                    binary = True
                    continue
                if line.startswith("--- ") or line.startswith("+++ "):
                    if line.startswith("+++ "):
                        rest = line[4:]
                        if rest.startswith("b/"):
                            path = _dequote_path(rest[2:])
                        elif rest != "/dev/null":
                            path = _dequote_path(rest)
                    continue
                if line.startswith("@@"):
                    state = "hunk"
                    line_no = _hunk_new_start(line)
                    continue
                continue
            if state == "hunk":
                if line.startswith("@@"):
                    line_no = _hunk_new_start(line)
                    continue
                if binary or path is None:
                    continue
                if line.startswith("+"):
                    self._scan_line(line[1:], path, line_no, commit)
                    line_no += 1
                elif line.startswith(" "):
                    line_no += 1
                elif line.startswith("-"):
                    pass
                continue

    def rev_list(self, cwd, args, git_opts=None):
        proc = self.git(["rev-list", *args], cwd, check=False, git_opts=git_opts)
        if proc.returncode != 0:
            msg = (proc.stderr or proc.stdout or "rev-list failed").strip().splitlines()
            raise GitError(msg[0] if msg else "rev-list failed")
        return [line for line in proc.stdout.splitlines() if line]

    def remote_has_refs(self, cwd, remote, git_opts=None):
        proc = self.git(
            ["for-each-ref", "--format=%(refname)", f"refs/remotes/{remote}"],
            cwd, check=False, git_opts=git_opts,
        )
        return bool(proc.stdout.strip())

    def remote_has_pushurl(self, cwd, remote, git_opts=None):
        if not remote or _looks_like_url_or_path(remote):
            return False
        proc = self.git(
            ["config", "--get", f"remote.{remote}.pushurl"],
            cwd, check=False, git_opts=git_opts,
        )
        return proc.returncode == 0 and bool(proc.stdout.strip())

    def remote_is_mirror(self, cwd, remote, git_opts=None):
        if not remote or _looks_like_url_or_path(remote):
            return False
        proc = self.git(
            ["config", "--bool", "--get", f"remote.{remote}.mirror"],
            cwd, check=False, git_opts=git_opts,
        )
        return proc.returncode == 0 and proc.stdout.strip() == "true"

    def full_history_target(self, cwd, remote, git_opts=None):
        if not remote or _looks_like_url_or_path(remote):
            return True
        if not self.remote_has_refs(cwd, remote, git_opts):
            return True
        if self.remote_has_pushurl(cwd, remote, git_opts):
            return True
        return False

    def scan_rev_range(self, cwd, rev_args, git_opts=None, no_walk=False):
        """Streamed git log -p over rev_args; names via a separate name-status pass."""
        if not rev_args:
            return
        opts = list(git_opts if git_opts is not None else self.git_opts)
        self.load_repo_config(cwd, git_opts=opts)
        self._scan_name_status(cwd, rev_args, git_opts=opts, no_walk=no_walk)
        merge_flag = self._merge_diff_flag(cwd, git_opts=opts)
        marker = self.log_marker
        fmt = f"%x00{marker}%x00%H%x00%B%x00"
        base = ["log", "-p", *_DIFF_FLAGS, f"--format={fmt}"]
        if no_walk:
            base.append("--no-walk")
        try:
            self._run_log_parse(cwd, base + ([merge_flag] if merge_flag else []) + list(rev_args), opts)
        except GitError:
            if merge_flag == "--remerge-diff":
                self._remerge = False
                self._run_log_parse(
                    cwd,
                    base + ["--diff-merges=first-parent"] + list(rev_args),
                    opts,
                )
            else:
                raise
        # Octopus merges (3+ parents): remerge-diff does not cover them — first-parent.
        # Run in pre-push too (keep --no-walk for collected commits). Errors propagate.
        octopus = [
            "log", "-p", *_DIFF_FLAGS, f"--format={fmt}",
            "--min-parents=3", "--diff-merges=first-parent",
        ]
        if no_walk:
            octopus.append("--no-walk")
        octopus.extend(rev_args)
        self._run_log_parse(cwd, octopus, opts)

    def _run_log_parse(self, cwd, cmd_args, opts):
        cmd = ["git", *opts, *cmd_args]
        timeout = self.remaining()
        try:
            proc = subprocess.run(
                cmd, cwd=cwd, capture_output=True,
                env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"},
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise BudgetExceeded("time budget exceeded") from exc
        except OSError as exc:
            raise GitError(str(exc)) from exc
        self._tick()
        raw = (proc.stdout or b"").decode("utf-8", errors="replace")
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode("utf-8", errors="replace")
            msg = (err or raw or "git log failed").strip().split("\n")
            raise GitError(msg[0] if msg else "git log failed")
        self._parse_log_stream(raw)

    def _merge_diff_flag(self, cwd, git_opts=None):
        if self._remerge is True:
            return "--remerge-diff"
        if self._remerge is False:
            return "--diff-merges=first-parent"
        proc = self.git(
            ["log", "-1", "--remerge-diff", "--format=%H"],
            cwd, check=False, git_opts=git_opts,
        )
        if proc.returncode == 0:
            self._remerge = True
            return "--remerge-diff"
        self._remerge = False
        return "--diff-merges=first-parent"

    def _scan_name_status(self, cwd, rev_args, git_opts=None, no_walk=False):
        """Added/copied/renamed destinations from name-status -z (independent of hunks)."""
        opts = list(git_opts if git_opts is not None else self.git_opts)
        args = [
            "log", "-z", "--name-status", "--format=format:%H",
            "--diff-filter=ACR", "--root", "--no-relative",
            "--diff-merges=first-parent",
        ]
        if no_walk:
            args.append("--no-walk")
        args.extend(rev_args)
        proc = self.git(args, cwd, check=False, git_opts=opts)
        if proc.returncode != 0:
            msg = (proc.stderr or proc.stdout or "name-status failed").strip().split("\n")
            raise GitError(msg[0] if msg else "name-status failed")
        self._parse_name_status_z(proc.stdout)

    def _parse_name_status_z(self, raw):
        pos, n = 0, len(raw)
        while pos < n:
            nl = raw.find("\n", pos)
            if nl < 0:
                break
            commit = raw[pos:nl]
            pos = nl + 1
            if not _OID.fullmatch(commit):
                continue
            while pos < n:
                if raw[pos] == "\0":
                    pos += 1
                    break
                z = raw.find("\0", pos)
                if z < 0:
                    raise GitError("malformed name-status record")
                status = raw[pos:z]
                pos = z + 1
                if not status:
                    break
                z = raw.find("\0", pos)
                if z < 0:
                    raise GitError("malformed name-status path")
                path1 = _dequote_path(raw[pos:z])
                pos = z + 1
                if status[0] in ("C", "R"):
                    z = raw.find("\0", pos)
                    if z < 0:
                        raise GitError("malformed name-status rename")
                    path2 = _dequote_path(raw[pos:z])
                    pos = z + 1
                    self.scan_path_name(path2, commit)
                elif status[0] == "A":
                    self.scan_path_name(path1, commit)

    def _parse_log_stream(self, raw):
        """Records framed as \\0MARKER\\0HASH\\0MESSAGE\\0PATCH until next marker."""
        if not raw:
            return
        marker = self.log_marker
        sep = "\0" + marker + "\0"
        parts = raw.split(sep)
        # Nonempty text before the first framing marker is a parse error.
        if parts[0]:
            raise GitError("malformed log stream (unframed preamble)")
        for part in parts[1:]:
            self._tick()
            z1 = part.find("\0")
            if z1 < 0:
                raise GitError("malformed log record (missing hash terminator)")
            commit = part[:z1]
            rest = part[z1 + 1:]
            z2 = rest.find("\0")
            if z2 < 0:
                raise GitError("malformed log record (missing message terminator)")
            message, patch = rest[:z2], rest[z2 + 1:]
            if not _OID.fullmatch(commit):
                raise GitError(f"malformed commit id in log stream: {commit[:20]!r}")
            self.scan_text(message, "(commit message)", commit)
            if patch:
                self.scan_diff(patch, commit)

    def scan_working_tree(self, cwd, git_opts=None):
        self.load_repo_config(cwd, git_opts=git_opts)
        head = self.git(["rev-parse", "--verify", "HEAD"], cwd, check=False, git_opts=git_opts)
        base = "HEAD" if head.returncode == 0 else EMPTY_TREE
        # Staged vs BASE, then index→worktree (avoids scanning staged twice).
        for args, label in (
            (["diff", "--cached", base, *_WT_DIFF_FLAGS], "(staged)"),
            (["diff", *_WT_DIFF_FLAGS], "(working tree)"),
        ):
            diff = self.git(args, cwd, check=True, git_opts=git_opts)
            self._scan_wt_names(diff.stdout, label)
            self.scan_diff(diff.stdout, label)
        # Name-status for staged/worktree additions without relying on +++ headers.
        for ns, label in (
            (["diff", "--cached", base, "--name-status", "-z", "--diff-filter=ACR",
              "--no-relative"], "(staged)"),
            (["diff", "--name-status", "-z", "--diff-filter=ACR", "--no-relative"],
             "(working tree)"),
        ):
            proc = self.git(ns, cwd, check=True, git_opts=git_opts)
            self._parse_wt_name_status_z(proc.stdout, label)
        untracked = self.git(
            ["ls-files", "--others", "--exclude-standard", "-z"],
            cwd, check=True, git_opts=git_opts,
        )
        for rel in untracked.stdout.split("\0"):
            if not rel:
                continue
            self.scan_path_name(rel, "(untracked)")
            path = Path(cwd) / rel
            try:
                if not path.is_file():
                    continue
                if _is_probably_binary(path):
                    continue
                self.scan_text(
                    path.read_text(encoding="utf-8", errors="replace"),
                    rel, "(untracked)",
                )
            except OSError as exc:
                raise GitError(f"unreadable {rel}: {exc}") from exc

    def _scan_wt_names(self, diff_text, label):
        """Best-effort path names from a working-tree diff (+++ headers)."""
        pending_new = False
        state = "between"
        for line in _split_lf(diff_text):
            if line.startswith("diff --git "):
                pending_new = False
                state = "headers"
                continue
            if state != "headers":
                if line.startswith("@@"):
                    state = "hunk"
                continue
            if line.startswith("new file mode") or (
                    line.startswith("--- ") and line[4:].strip() in ("/dev/null", '"/dev/null"')):
                pending_new = True
            elif line.startswith("+++ "):
                rest = line[4:]
                path = rest[2:] if rest.startswith("b/") else (
                    None if rest in ("/dev/null", '"/dev/null"') else rest)
                if path and pending_new:
                    self.scan_path_name(_dequote_path(path), label)
                pending_new = False
            elif line.startswith("@@"):
                state = "hunk"

    def _parse_wt_name_status_z(self, raw, label):
        """Parse `git diff --name-status -z` (no commit hashes)."""
        pos, n = 0, len(raw)
        while pos < n:
            if raw[pos] == "\0":
                pos += 1
                continue
            z = raw.find("\0", pos)
            if z < 0:
                break
            status = raw[pos:z]
            pos = z + 1
            if not status:
                continue
            z = raw.find("\0", pos)
            if z < 0:
                break
            path1 = _dequote_path(raw[pos:z])
            pos = z + 1
            if status[0] in ("C", "R"):
                z = raw.find("\0", pos)
                if z < 0:
                    break
                path2 = _dequote_path(raw[pos:z])
                pos = z + 1
                self.scan_path_name(path2, label)
            elif status[0] == "A":
                self.scan_path_name(path1, label)


def _hunk_new_start(line):
    try:
        plus = line.split("+", 1)[1]
        return int(plus.split(",")[0].split()[0])
    except (IndexError, ValueError):
        return 0


def _is_probably_binary(path, sample=8192):
    data = path.read_bytes()[:sample]  # OSError propagates to caller
    return b"\0" in data


def _looks_like_url_or_path(remote):
    if not remote:
        return False
    if "://" in remote or remote.startswith("git@"):
        return True
    if remote.startswith("/") or remote.startswith("./") or remote.startswith("../"):
        return True
    if remote.startswith("~") or "\\" in remote:
        return True
    if ":" in remote and not remote.startswith("-"):
        return True
    return False


def resolve_cwd(path, base):
    if not path:
        return base
    text = os.path.expanduser(path)
    candidate = Path(text) if os.path.isabs(text) else Path(base) / text
    try:
        resolved = candidate.resolve()
        if resolved.exists():
            return str(resolved)
    except OSError:
        pass
    return base


def _skip_keywords(words):
    while words and words[0] in SHELL_KEYWORDS:
        words = words[1:]
    return words


def _skip_wrappers(words, depth=0):
    """Strip leading env assignments and env/command/nohup/time/timeout wrappers.

    Returns (remaining_words, depth, split_string_or_None, chdir_or_None).
    ``env -C DIR`` / ``--chdir`` applies DIR only to the child command.
    """
    if depth > 32:
        return words, depth, None, None
    i = 0
    while i < len(words):
        if ASSIGNMENT.match(words[i]):
            i += 1
            continue
        name = Path(words[i]).name
        if name == "env":
            i += 1
            split_string = None
            chdir = None
            while i < len(words) and words[i].startswith("-"):
                opt = words[i]
                i += 1
                if opt == "--":
                    break
                if opt in ("-S", "--split-string"):
                    if i < len(words):
                        split_string = words[i]
                        i += 1
                    break
                if opt.startswith("--split-string="):
                    split_string = opt.split("=", 1)[1]
                    break
                if opt.startswith("-S") and opt != "-S" and not opt.startswith("--"):
                    split_string = opt[2:]
                    break
                if opt in ("-C", "--chdir"):
                    if i < len(words):
                        chdir = words[i]
                        i += 1
                    continue
                if opt.startswith("--chdir="):
                    chdir = opt.split("=", 1)[1]
                    continue
                if opt in ("-u", "--unset", "-a", "--argv0"):
                    i += 1
            if split_string is not None or chdir is not None:
                return words[i:], depth, split_string, chdir
            continue
        if name in ("command", "nohup", "time", "nice", "setsid", "exec"):
            i += 1
            while i < len(words) and words[i].startswith("-"):
                opt = words[i]
                i += 1
                if opt == "--":
                    break
                if name == "time" and opt in ("-f", "--format", "-o", "--output"):
                    i += 1
                elif name == "nice" and opt in ("-n", "--adjustment"):
                    i += 1
                elif name == "exec" and opt in ("-a",):
                    i += 1
            continue
        if name == "timeout":
            i += 1
            while i < len(words) and words[i].startswith("-"):
                opt = words[i]
                i += 1
                if opt == "--":
                    break
                if opt in ("-s", "--signal", "-k", "--kill-after"):
                    i += 1
            if i < len(words):
                i += 1
            continue
        break
    return words[i:], depth, None, None


def _nested_shell(words):
    """Return (kind, string, remaining, chdir) for eval / bash -c / env -S; else None.

    kind is 'eval' (cwd propagates to caller), 'shell' (isolated), or 'env_s'.
    chdir is set when ``env -C`` / ``--chdir`` wrapped the child.
    """
    words = _skip_keywords(words)
    words, _, env_s, chdir = _skip_wrappers(words)
    if env_s is not None or chdir is not None:
        # env -C alone (no -S): remaining words are the child command.
        if env_s is None and chdir is not None:
            return "env_c", " ".join(shlex.quote(w) for w in words), [], chdir
        return "env_s", env_s or "", words, chdir
    if not words:
        return None
    name = Path(words[0]).name
    if name == "eval":
        return "eval", " ".join(words[1:]), [], None
    if name in ("bash", "sh", "zsh"):
        i = 1
        while i < len(words) and words[i].startswith(("-", "+")):
            opt = words[i]
            i += 1
            if opt == "--":
                break
            if opt in ("-o", "+o", "-O", "+O", "--rcfile", "--init-file"):
                i += 1
                continue
            if opt.startswith("-") and not opt.startswith("--") and "c" in opt:
                text = words[i] if i < len(words) else ""
                return "shell", text, words[i + 1:] if i < len(words) else [], None
    return None


def _parse_git_global(words):
    """Return (c_dirs, git_opts for subprocesses, index of subcommand)."""
    i = 1
    c_dirs = []
    git_opts = []
    while i < len(words):
        w = words[i]
        if w == "-C" and i + 1 < len(words):
            c_dirs.append(words[i + 1])
            i += 2
            continue
        if w.startswith("--git-dir="):
            git_opts.append(w)
            i += 1
            continue
        if w == "--git-dir" and i + 1 < len(words):
            git_opts.extend([w, words[i + 1]])
            i += 2
            continue
        if w.startswith("--work-tree="):
            git_opts.append(w)
            i += 1
            continue
        if w == "--work-tree" and i + 1 < len(words):
            git_opts.extend([w, words[i + 1]])
            i += 2
            continue
        if w == "-c" and i + 1 < len(words):
            git_opts.extend([w, words[i + 1]])
            i += 2
            continue
        if w.startswith("-c") and len(w) > 2:
            git_opts.append(w)
            i += 1
            continue
        if w in ("--no-pager", "-P"):
            i += 1
            continue
        if w.startswith("-") and w not in ("--",):
            if w in ("--bare", "--literal-pathspecs", "--glob-pathspecs",
                     "--noglob-pathspecs", "--icase-pathspecs", "--no-optional-locks"):
                i += 1
                continue
            break
        break
    return c_dirs, git_opts, i


def _apply_c_dirs(base, c_dirs):
    cwd = base
    for d in c_dirs:
        cwd = resolve_cwd(d, cwd)
    return cwd


def default_remote(cwd, guard, git_opts=None):
    try:
        branch = guard.git(
            ["rev-parse", "--abbrev-ref", "HEAD"], cwd, git_opts=git_opts,
        ).stdout.strip()
    except GitError:
        branch = ""
    for key in (
        f"branch.{branch}.pushRemote" if branch and branch != "HEAD" else None,
        "remote.pushDefault",
        f"branch.{branch}.remote" if branch and branch != "HEAD" else None,
    ):
        if not key:
            continue
        proc = guard.git(["config", "--get", key], cwd, check=False, git_opts=git_opts)
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    return "origin"


def _is_deletion_refspec(spec, delete_flag):
    """True when this refspec sends no objects (:dst with non-empty dst, or --delete)."""
    s = spec[1:] if spec.startswith("+") else spec
    if delete_flag:
        return True
    if s.startswith(":") and len(s) > 1:
        return True  # :dst
    return False


def parse_git_push(words, cwd, guard):
    """Parse a git push argv (words[0]==git). Return dict or None if not a push."""
    c_dirs, git_opts, i = _parse_git_global(words)
    if i >= len(words) or words[i] != "push":
        return None
    work = _apply_c_dirs(cwd, c_dirs)
    i += 1
    remote = None
    repo_opt = None
    refspecs = []
    flags = {"dry_run": False, "delete_only": False, "mirror": False, "has_glob": False}
    delete_flag = False
    takes_val = {"-o", "--push-option", "--receive-pack", "--exec"}
    while i < len(words):
        w = words[i]
        if w in ("--dry-run", "-n"):
            flags["dry_run"] = True
            i += 1
            continue
        if w in ("--delete", "-d"):
            delete_flag = True
            i += 1
            continue
        if w == "--mirror":
            flags["mirror"] = True
            i += 1
            continue
        if w in ("--all", "--branches", "--tags"):
            # Covered by the union scan; keep going.
            i += 1
            continue
        if w == "--repo" and i + 1 < len(words):
            repo_opt = words[i + 1]
            i += 2
            continue
        if w.startswith("--repo="):
            repo_opt = w.split("=", 1)[1]
            i += 1
            continue
        if w == "--":
            # Positional remote (if not yet set) then refspecs, after end-of-options.
            i += 1
            while i < len(words):
                if remote is None:
                    remote = words[i]
                else:
                    refspecs.append(words[i])
                i += 1
            break
        if w.startswith("--") and "=" in w:
            i += 1
            continue
        if w in takes_val:
            i += 2 if i + 1 < len(words) else i + 1
            continue
        if w == "-u":
            i += 1
            continue
        if w.startswith("-"):
            i += 1
            continue
        if remote is None:
            remote = w
            i += 1
            continue
        refspecs.append(w)
        i += 1

    if flags["dry_run"]:
        return {
            "cwd": work, "remote": remote or repo_opt, "refs": [], "flags": flags,
            "kind": "git-push", "git_opts": git_opts,
        }

    if remote is None:
        remote = repo_opt or default_remote(work, guard, git_opts)

    explicit = []
    idx = 0
    while idx < len(refspecs):
        spec = refspecs[idx]
        s = spec[1:] if spec.startswith("+") else spec
        if s == "tag" and idx + 1 < len(refspecs):
            explicit.append(f"refs/tags/{refspecs[idx + 1]}")
            idx += 2
            continue
        if _is_deletion_refspec(spec, delete_flag):
            idx += 1
            continue
        # bare `:` / `+:` — not a deletion; covered by the HEAD∪branches∪tags union
        if s in (":", ""):
            idx += 1
            continue
        if ":" in s:
            src, _dst = s.split(":", 1)
            if src and _is_glob_ref(src):
                flags["has_glob"] = True
            elif src:
                explicit.append(src)
        elif _is_glob_ref(s):
            flags["has_glob"] = True
        else:
            explicit.append(s)
        idx += 1

    if delete_flag:
        flags["delete_only"] = True
    elif refspecs:
        flags["delete_only"] = all(
            _is_deletion_refspec(spec, False) for spec in refspecs
        )

    if guard.remote_is_mirror(work, remote, git_opts):
        flags["mirror"] = True

    return {
        "cwd": work,
        "remote": remote,
        "refs": explicit,
        "flags": flags,
        "kind": "git-push",
        "git_opts": git_opts,
    }


def is_git_commit_verb(words):
    words = _skip_keywords(list(words))
    words, _, env_s, _chdir = _skip_wrappers(words)
    if env_s is not None:
        return any(is_git_commit_verb(_tokens(seg)) for seg in _shell_segments(env_s))
    if not words or Path(words[0]).name != "git":
        return False
    _c, _opts, i = _parse_git_global(words)
    if i >= len(words):
        return False
    return words[i] in COMMIT_VERBS


def parse_gh(words, cwd, guard):
    words = _skip_keywords(list(words))
    words, _, env_s, _chdir = _skip_wrappers(words)
    if env_s is not None:
        return None  # handled via nested re-parse
    if not words or Path(words[0]).name != "gh":
        return None
    args = words[1:]
    if len(args) >= 2 and args[0] == "repo" and args[1] == "create":
        if any(a == "--push" or a.startswith("--push=") for a in args):
            source = cwd
            i = 0
            while i < len(args):
                a = args[i]
                if a in ("--source", "-s") and i + 1 < len(args):
                    source = resolve_cwd(args[i + 1], cwd)
                    i += 2
                    continue
                if a.startswith("--source="):
                    source = resolve_cwd(a.split("=", 1)[1], cwd)
                    i += 1
                    continue
                i += 1
            return {
                "cwd": source, "remote": None, "refs": [],
                "flags": {"full_history": True, "mirror": True},
                "kind": "gh-repo-create-push",
                "target": "new remote (gh repo create --push)",
                "git_opts": [],
            }
    if len(args) >= 2 and args[0] == "pr" and args[1] == "create":
        remote = default_remote(cwd, guard)
        return {
            "cwd": cwd, "remote": remote, "refs": [], "flags": {},
            "kind": "gh-pr-create", "target": remote or "upstream", "git_opts": [],
        }
    if len(args) >= 2 and args[0] == "repo" and args[1] == "edit":
        vis = None
        for i, a in enumerate(args[2:], start=2):
            if a.startswith("--visibility="):
                vis = a.split("=", 1)[1]
            elif a == "--visibility" and i + 1 < len(args):
                vis = args[i + 1]
        if vis == "public":
            return {
                "cwd": cwd, "remote": None, "refs": [],
                "flags": {"full_history": True, "mirror": True},
                "kind": "gh-visibility-public",
                "target": "local clone (gh repo edit --visibility public)",
                "git_opts": [],
            }
    return None


def might_push(command):
    if not isinstance(command, str) or not command:
        return False
    lower = command.lower()
    return "push" in lower or "gh" in lower or "visibility" in lower


def analyze_command(command, cwd, guard):
    """Return (pushes, commit_before_push).

    Inside bash/sh -c, cd applies to later segments of that string only.
    eval cd changes propagate to the caller (like a real shell).
    A cd inside ( … ), $( … ) or backticks does not change later parent segments.
    env -C / --chdir applies only to the child command (and its nested walk).
    """
    state = {"seen_commit": False, "commit_before_push": False, "pushes": []}

    def handle_one(segment, work, depth=0):
        if depth > 32:
            return work
        raw = _tokens(segment)
        if not raw:
            return work
        nested = _nested_shell(raw)
        if nested is not None:
            kind, text, rest, chdir = nested
            child = resolve_cwd(chdir, work) if chdir else work
            if kind in ("env_s", "env_c"):
                if kind == "env_s":
                    combined = list(_tokens(text)) + list(rest)
                    text = " ".join(shlex.quote(w) for w in combined)
                walk(text, child, depth + 1)
                return work
            nested_work = walk(text, child, depth + 1)
            if kind == "eval":
                return nested_work
            return work
        words = _skip_keywords(raw)
        words, _, env_s, chdir = _skip_wrappers(words)
        if env_s is not None or chdir is not None:
            child = resolve_cwd(chdir, work) if chdir else work
            if env_s is not None:
                combined = list(_tokens(env_s)) + list(words)
                text = " ".join(shlex.quote(w) for w in combined)
            else:
                text = " ".join(shlex.quote(w) for w in words)
            walk(text, child, depth + 1)
            return work
        if not words:
            return work
        if Path(words[0]).name == "cd":
            if len(words) >= 2:
                return resolve_cwd(words[1], work)
            return work
        if is_git_commit_verb(words):
            state["seen_commit"] = True
            return work
        push = None
        if Path(words[0]).name == "git":
            _c, _opts, idx = _parse_git_global(words)
            if idx < len(words) and words[idx] == "stash":
                return work
            if idx < len(words) and words[idx] == "push":
                push = parse_git_push(words, work, guard)
        if push is None:
            push = parse_gh(words, work, guard)
        if push is not None:
            if state["seen_commit"]:
                state["commit_before_push"] = True
            state["pushes"].append(push)
        return work

    def walk(text, work, depth=0):
        """Like _shell_segments, but (…)/$(…)/`…` isolate directory changes."""
        if depth > 32:
            return work
        buffer, quote, i = [], None, 0
        while i < len(text):
            char = text[i]
            if char == "\\" and quote != "'" and i + 1 < len(text):
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
                walk(text[start:end], work, depth + 1)  # isolated
                buffer.append(" __substitution__ ")
                i = end + 1
                continue
            if char in ("'", '"'):
                if quote is None:
                    quote = char
                elif quote == char:
                    quote = None
            elif quote is None and char == "(":
                seg = "".join(buffer)
                buffer = []
                if seg.strip():
                    work = handle_one(seg, work, depth)
                end = _substitution_end(text, i + 1, backtick=False)
                walk(text[i + 1:end], work, depth + 1)  # isolated
                i = end + 1
                continue
            elif quote is None and char in ";&|\n)":
                seg = "".join(buffer)
                buffer = []
                if seg.strip():
                    work = handle_one(seg, work, depth)
                i += 1
                continue
            buffer.append(char)
            i += 1
        seg = "".join(buffer)
        if seg.strip():
            work = handle_one(seg, work, depth)
        return work

    walk(command, cwd, 0)
    return state["pushes"], state["commit_before_push"]


def format_reason(findings, target, extra_prefix=None, guard=None):
    if guard is not None:
        guard._tick()
    n = len(findings)
    commits = {
        f["commit"] for f in findings
        if f["commit"] not in ("-", "(working tree)", "(untracked)", "(staged)")
        and not str(f["commit"]).startswith("(")
    }
    commit_n = len(commits)
    parts = []
    if extra_prefix:
        parts.append(extra_prefix)
    noun = "finding" if n == 1 else "findings"
    cnoun = "commit" if commit_n == 1 else "commits"
    if commit_n:
        parts.append(f"secret guard: {n} {noun} in {commit_n} {cnoun}")
    else:
        parts.append(f"secret guard: {n} {noun}")
    if target:
        parts.append(f"target {redact(str(target))}")
    lines = [redact(", ".join(parts) + ":")]

    # Group by label/path/commit/value; line numbers as a set (O(1) membership).
    groups = []
    index = {}
    for finding in findings:
        if guard is not None:
            guard._tick()
        key = (finding["label"], finding["path"], finding["commit"], finding["value"])
        if key not in index:
            index[key] = len(groups)
            groups.append({"key": key, "lines": set(), "order": []})
        line = finding["line"]
        g = groups[index[key]]
        if line not in g["lines"]:
            g["lines"].add(line)
            g["order"].append(line)

    shown = 0
    for group in groups:
        if guard is not None:
            guard._tick()
        if shown >= 10:
            break
        label, path, commit, value = group["key"]
        nums = [x for x in group["order"] if x != "-"]
        if not nums:
            loc = f"{redact(path)}:-"
        else:
            head = nums[:3]
            more = len(nums) - len(head)
            loc = f"{redact(path)}:" + ",".join(str(x) for x in head)
            if more > 0:
                loc += f" +{more} more"
        lines.append(redact(f"{label}  {loc}  {commit}  {value}"))
        shown += 1
    more_groups = len(groups) - shown
    if more_groups > 0:
        lines.append(f"and {more_groups} more")
    return "\n".join(lines)


def print_ask(reason):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": redact(reason),
        }
    }))


def print_deny(reason):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": redact(reason),
        }
    }))


def push_rev_args(guard, push):
    """Build rev-list / log revision args for one push descriptor (ends with --)."""
    flags = push.get("flags") or {}
    if flags.get("dry_run") or flags.get("delete_only"):
        return None
    cwd = push["cwd"]
    remote = push.get("remote")
    git_opts = push.get("git_opts") or []
    if flags.get("mirror") or flags.get("has_glob"):
        # Wildcard sources may name refs outside branches/tags — scan --all.
        tips = ["--all"]
    elif flags.get("full_history"):
        tips = ["HEAD", "--branches", "--tags"]
    else:
        explicit = [r for r in (push.get("refs") or []) if not _is_glob_ref(r)]
        tips = ["HEAD", "--branches", "--tags", *explicit]

    full = flags.get("full_history") or guard.full_history_target(cwd, remote, git_opts)
    args = list(tips)
    if not full and remote and not _looks_like_url_or_path(remote):
        args += ["--not", f"--remotes={remote}"]
    args.append("--")
    return args


def run_push_scans(guard, pushes):
    for push in pushes:
        cwd = push["cwd"]
        git_opts = push.get("git_opts") or []
        guard.git_opts = git_opts
        probe = guard.git(
            ["rev-parse", "--is-inside-work-tree"], cwd, check=False, git_opts=git_opts,
        )
        if probe.returncode != 0:
            raise GitError(f"{cwd} is not a git repository")
        flags = push.get("flags") or {}
        if flags.get("dry_run") or flags.get("delete_only"):
            continue
        remote = push.get("remote")
        target = push.get("target") or remote or "(unresolved)"
        rev_args = push_rev_args(guard, push)
        if not rev_args:
            continue
        guard.begin_push_scan()
        commits = guard.rev_list(cwd, rev_args, git_opts=git_opts)
        if commits:
            guard.scan_rev_range(cwd, rev_args, git_opts=git_opts)
        # Unborn HEAD with no branches: empty commits is a silent skip.
        push["_target"] = target
        push["_commit_count"] = len(commits)
        push["_had_findings"] = guard._scan_hits > 0


def _ask_could_not_finish(exc, repo_dir=None):
    name = type(exc).__name__
    script = Path(__file__).resolve()
    repo = repo_dir or os.getcwd()
    hint = f"python3 {script} --check {repo}"
    print_ask(
        f"secret guard could not finish: {name}: {exc}; "
        f"scan without a time limit: {hint}; push anyway?"
    )


def claude_main(data):
    if os.environ.get("SECRET_GUARD_DISABLE") == "1":
        return 0
    if not isinstance(data, dict):
        return 0
    if data.get("hook_event_name") and data.get("hook_event_name") != "PreToolUse":
        return 0
    if data.get("tool_name") and data.get("tool_name") != "Bash":
        return 0
    inputs = data.get("tool_input") or {}
    command = inputs.get("command") if isinstance(inputs, dict) else None
    cwd = data.get("cwd") or os.getcwd()
    if not might_push(command or ""):
        return 0
    guard = Guard(budget_s=None)  # default 20 s (or SECRET_GUARD_BUDGET_S)
    try:
        pushes, commit_before = analyze_command(command, cwd, guard)
        if not pushes:
            return 0
        if commit_before:
            print_deny(
                "secret guard: this Bash call creates a commit and then pushes; "
                "run the push as its own Bash call so the new commit can be scanned"
            )
            return 0
        try:
            run_push_scans(guard, pushes)
        except GitError as exc:
            msg = str(exc)
            if "is not a git repository" in msg.lower():
                print_ask(f"secret guard could not finish: {msg}")
                return 0
            _ask_could_not_finish(exc, repo_dir=cwd)
            return 0
        except Exception as exc:
            _ask_could_not_finish(exc, repo_dir=cwd)
            return 0
        if not guard.findings:
            return 0
        targets = []
        for p in pushes:
            if p.get("_had_findings"):
                t = p.get("_target") or p.get("remote") or "remote"
                if t not in targets:
                    targets.append(t)
        if not targets:
            targets = [pushes[0].get("_target") or pushes[0].get("remote") or "remote"]
        target = ", ".join(str(t) for t in targets)
        print_ask(format_reason(guard.findings, target, guard=guard))
        return 0
    except BudgetExceeded as exc:
        _ask_could_not_finish(exc, repo_dir=cwd)
        return 0
    except GitError as exc:
        msg = str(exc)
        if "is not a git repository" in msg.lower():
            print_ask(f"secret guard could not finish: {msg}")
            return 0
        _ask_could_not_finish(exc, repo_dir=cwd)
        return 0
    except Exception as exc:
        _ask_could_not_finish(exc, repo_dir=cwd)
        return 0


def git_pre_push_main(remote, url):
    if os.environ.get("SECRET_GUARD_DISABLE") == "1":
        return 0
    cwd = os.getcwd()
    guard = Guard(budget_s=None)
    try:
        lines = sys.stdin.read().splitlines()
        # Prefer per-line ranges; also fall back to union vs remote when needed.
        batches = []
        seen = set()
        for line in lines:
            parts = line.split()
            if len(parts) < 4:
                continue
            _lref, local_sha, _rref, remote_sha = parts[:4]
            if re.fullmatch(r"0+", local_sha):
                continue
            use_range = False
            if remote_sha and not re.fullmatch(r"0+", remote_sha):
                probe = guard.git(
                    ["cat-file", "-e", f"{remote_sha}^{{commit}}"], cwd, check=False,
                )
                if probe.returncode == 0:
                    use_range = True
            if use_range:
                batch = guard.rev_list(cwd, [f"{remote_sha}..{local_sha}"])
            elif guard.full_history_target(cwd, remote):
                batch = guard.rev_list(cwd, [local_sha])
            else:
                batch = guard.rev_list(
                    cwd, [local_sha, "--not", f"--remotes={remote}"],
                )
            for c in batch:
                if c not in seen:
                    seen.add(c)
                    batches.append(c)
        if batches:
            # Only the listed commits — do not walk their ancestors.
            guard.scan_rev_range(cwd, batches + ["--"], no_walk=True)
        if guard.findings:
            text = format_reason(guard.findings, remote or url, guard=guard)
            print(text, file=sys.stderr)
            print("override: git push --no-verify", file=sys.stderr)
            return 1
        return 0
    except Exception as exc:
        script = Path(__file__).resolve()
        hint = f"python3 {script} --check {cwd}"
        print(redact(
            f"secret guard could not finish: {type(exc).__name__}: {exc}; "
            f"scan without a time limit: {hint}"
        ), file=sys.stderr)
        print("override: git push --no-verify", file=sys.stderr)
        return 1


def check_main(directory):
    cwd = str(Path(directory or os.getcwd()).resolve())
    guard = Guard(budget_s=_UNLIMITED)
    try:
        probe = guard.git(["rev-parse", "--is-inside-work-tree"], cwd, check=False)
        if probe.returncode != 0:
            print(redact(f"secret guard: not a git repository: {cwd}"), file=sys.stderr)
            return 1
        commits = guard.rev_list(cwd, ["--all", "--"])
        if commits:
            guard.scan_rev_range(cwd, ["--all", "--"])
        guard.scan_working_tree(cwd)
    except Exception as exc:
        script = Path(__file__).resolve()
        hint = f"python3 {script} --check {cwd}"
        print(redact(
            f"secret guard could not finish: {type(exc).__name__}: {exc}; "
            f"scan without a time limit: {hint}"
        ), file=sys.stderr)
        return 1
    if guard.findings:
        print(format_reason(guard.findings, cwd, guard=guard))
        return 1
    print(f"secret guard: all clear ({len(commits)} commits + working tree)")
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--check":
        directory = argv[1] if len(argv) > 1 else os.getcwd()
        return check_main(directory)
    if len(argv) >= 2 and not argv[0].startswith("-"):
        return git_pre_push_main(argv[0], argv[1])
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return 0
        data = json.loads(raw)
    except (ValueError, OSError):
        return 0
    return claude_main(data)


if __name__ == "__main__":
    sys.exit(main() or 0)

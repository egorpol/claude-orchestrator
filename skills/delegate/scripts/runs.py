"""Delegate runs: git worktree + brief + background agent + log + last message.

A run lives in RUNS_ROOT/<repo>/<name>/. The current round's job files sit in
the run root; `followup` archives them to round-<k>/ first. Reviews are jobs in
review-<n>/ that read the worktree without changing it.
"""
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import checks
import quota

SCRIPTS = Path(__file__).resolve().parent
RUNS_ROOT = Path(os.environ.get("CLAUDE_DELEGATE_ROOT",
                                Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
                                / "claude-delegate" / "runs"))
DEFAULTS = {"codex": ("gpt-6.1-sol", "xhigh"), "cursor": ("auto", None)}
ROUND_FILES = ("brief.md", "prompt.md", "run.sh", "log", "last-message.md", "exit_code", "pid",
               "started_at", "finished_at", "finalized_at", "verify.log", "verify_code", "verify-adhoc.log",
               "warnings")
# Snapshot refuses to hash a stray data dump into .git/objects (and the review patch).
UNTRACKED_MAX_FILES = 100
UNTRACKED_MAX_BYTES = 10 * 1024 * 1024  # 10 MiB

# Claude Code here runs inside Cursor's AppImage; these leak into children and
# can break other binaries, or make a delegate think it is talking to an IDE.
UNSET_EXACT = {"LD_LIBRARY_PATH", "APPDIR", "APPIMAGE", "ARGV0", "OWD", "PERLLIB",
               "GSETTINGS_SCHEMA_DIR", "QT_PLUGIN_PATH", "CHROME_DESKTOP",
               "CLAUDECODE", "CLAUDE_PID", "AI_AGENT"}
UNSET_PREFIX = ("CLAUDE_CODE_", "VSCODE_", "ELECTRON_", "CURSOR_")
KEEP = {"CURSOR_API_KEY", "CURSOR_API_ENDPOINT"}


def session_log_path(explicit=None):
    """The session-mode experiment's log (hooks/session-mode.py); quota.jsonl sits next to it."""
    if explicit:
        return Path(explicit).expanduser()
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return Path(os.environ.get("CLAUDE_SESSION_LOG") or cache / "claude-delegate/sessions.jsonl")


def log_delegation(command, **fields):
    """Record launch/followup/review/ask under the calling Claude session, for the session summary."""
    session = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not session:
        return
    row = {"event": "delegate", "session": session,
           "ts": datetime.now().astimezone().isoformat(timespec="seconds"), "command": command, **fields}
    try:
        path = session_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # One unbuffered O_APPEND write, like the hook's rows, so concurrent writers don't interleave.
        with path.open("ab", buffering=0) as stream:
            stream.write((json.dumps(row, ensure_ascii=True) + "\n").encode("utf-8"))
    except OSError:
        pass

CONTRACT = """

---
## Delegation contract (appended by the launcher; it overrides anything above that conflicts)
- You are working in a git worktree at {wt} on branch `{branch}`, created from {base} for this task. {scope}
- Do NOT run `git commit`, `git push`, `git merge`, `git rebase`, `git stash`, `git reset`, `git checkout <branch>` or create branches or tags. Leave every change uncommitted; the orchestrator reviews the diff and merges it.
{links}{snapshot}- If a constraint in the brief would make you drop existing behaviour, data or a fallback, or real data contradicts an assumption in the brief, do not comply silently: keep the existing behaviour where you can and flag the conflict in your final message.
- Verify against real data or tests where the brief says how (dry runs, test commands) and report the actual output, not what you expect it to be.
- End with a final message containing: (1) what you changed, file by file; (2) the commands you ran to verify and their real results; (3) open questions, conflicts, and anything you skipped or were unsure about.
- Keep those prose sections and append this short machine-readable block at the very end:
## Digest
files: <path> (+a/-d); ...
verify: <command> -> <result>
conflicts: <one line each, or none>
risks: <path:line - why, or none>
"""

SNAPSHOT_BULLET = (
    "- The base `{base}` is a snapshot commit of the orchestrator's uncommitted work on top of "
    "`{snapshot_of}` (`git show --stat HEAD` lists it). Treat that work as the starting point: "
    "don't redo or revert it. Your own changes are what `git diff HEAD` shows.\n"
)

FOLLOWUP_NOTE = """

---
Follow-up round {round}. The delegation contract from the start of this session still applies: same worktree, no commits, flag conflicts, same final-message structure. The worktree already contains your earlier changes; build on them.
Keep the prose sections and append the same `## Digest` block (files, verify, conflicts, risks) at the very end.
"""

REVIEW_PROMPT = """# Review request (read-only)

Another coding agent made changes in this git worktree ({wt}) relative to commit {base}. Review them. This is a read-only task: do not modify, create or delete any files.

The full diff is in `.delegate/review-{n}.patch` (path relative to the worktree root; new files appear as additions). Read it, then read the surrounding code in the worktree where you need context.{shell_note}
The run's verify command and the last 80 lines of its latest verify output are in `.delegate/review-{n}.verify.log`. Read that file to check verification claims against actual output.{omit_note}

## The brief(s) the other agent was given
<brief>
{brief}
</brief>
{focus}
## What to check
1. Spec compliance: does the change do what the brief asks, no more and no less? Did it drop existing behaviour, data or fallbacks?
2. Correctness: bugs, edge cases, error handling, behaviour on the real data the brief mentions.
3. Tests: do they exercise the new behaviour meaningfully?
4. The brief itself: you judge the patch against the brief, so a wrong brief passes unnoticed. Is any requirement or acceptance criterion wrong for the domain, or does it contradict an existing repo convention (columns every stage stamps, how tests compare outputs)? If the change meets the brief by breaking a convention or by encoding a wrong criterion, report it as a finding against the brief.
Report only material issues; skip style nits.

## Output (use exactly this structure)
## Verdict
Verdict: APPROVE or CHANGES REQUESTED
## Findings
- [high|medium|low] `path:line`: the problem. Evidence: what you saw. Fix: the smallest change that resolves it.
(write "None." when there are no material findings)
## Spec compliance
- one line per requirement in the brief: met / partly / not met, with the reason
Keep these prose sections and append at the very end:
## Digest
files: none (read-only review)
verify: <command or supplied verify log> -> <result>
conflicts: <one line each, or none>
risks: <path:line - why, or none>
"""

# Shared checks/output tail; derived so it cannot drift from REVIEW_PROMPT.
_REVIEW_CHECKS_MARK = "## The brief(s) the other agent was given"
REVIEW_PROMPT_CHECKS = REVIEW_PROMPT[REVIEW_PROMPT.index(_REVIEW_CHECKS_MARK):]


def git(*args, cwd=None, check=True):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                       env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    if check and r.returncode != 0:
        sys.exit(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout if "-z" in args else r.stdout.strip()


def _foreign_venv(path_entry, repo):
    """True for a virtualenv bin dir that belongs to some other project."""
    p = Path(path_entry)
    return (p.name == "bin" and (p.parent / "pyvenv.cfg").exists()
            and repo not in p.resolve().parents)


def clean_env_lines(repo):
    """Shell lines that strip IDE/AppImage/Claude variables for the delegate."""
    unset = sorted(k for k in os.environ
                   if k not in KEEP and (k in UNSET_EXACT or k.startswith(UNSET_PREFIX)))
    venv = os.environ.get("VIRTUAL_ENV")
    if venv and repo not in Path(venv).resolve().parents:
        unset.append("VIRTUAL_ENV")
    lines = []
    if unset:
        lines.append("unset " + " ".join(unset))
    for var in ("PATH", "XDG_DATA_DIRS"):
        val = os.environ.get(var)
        if val:
            kept = [p for p in val.split(":")
                    if p and "/.mount_" not in p and not (var == "PATH" and _foreign_venv(p, repo))]
            lines.append(f"export {var}={shlex.quote(':'.join(kept))}")
    # test runs (agent or --verify) must not leave __pycache__/*.pyc in the worktree: untracked
    # files count as changes, so they would land in changes.patch and get applied
    lines.append("export PYTHONDONTWRITEBYTECODE=1")
    return lines


def build_cmd(agent, model, effort, wt, job, read_only, unsandboxed=False, resume=None, web_search=False):
    q = shlex.quote
    if agent == "codex":
        sandbox = "read-only" if read_only else "workspace-write"
        if resume:
            # `exec resume` has no -s/-C: the sandbox goes in via config and
            # run.sh already cd's into the worktree.
            cmd = ["codex", "exec", "resume", "-m", model, "-c", f'sandbox_mode="{sandbox}"']
        else:
            cmd = ["codex", "exec", "-m", model, "-s", sandbox, "-C", str(wt), "--color", "never"]
        if effort:
            cmd += ["-c", f'model_reasoning_effort="{effort}"']
        if web_search:
            # the native Responses tool runs server-side, so it works under -s read-only (tested 2026-10-13)
            cmd += ["-c", 'web_search="live"']
        cmd += ["--json", "-o", str(job / "last-message.md")]
        cmd += [resume, "-"] if resume else ["-"]
        return " ".join(q(c) for c in cmd) + f" < {q(str(job / 'prompt.md'))}"
    if agent == "cursor":
        # Headless with --sandbox enabled, shell commands auto-run confined to
        # the workspace (writes elsewhere get EPERM, no network). --force
        # ("Run Everything") silently bypasses that sandbox (verified
        # 2026-10-09), so it is opt-in only. --mode ask cannot run shell at all.
        cmd = ["cursor-agent", "-p", "--output-format", "stream-json", "--model", model,
               "--trust", "--workspace", str(wt)]
        cmd += ["--sandbox", "disabled", "--force"] if unsandboxed else ["--sandbox", "enabled"]
        if read_only:
            cmd += ["--mode", "ask"]
        if resume:
            cmd += ["--resume", resume]
        return " ".join(q(c) for c in cmd) + f' "$(cat {q(str(job / "prompt.md"))})" < /dev/null'
    sys.exit(f"unknown agent {agent!r}")


def _echoed_verify(cmd):
    """Prefix every physical line with `$ ` so failure_line skips the whole echo."""
    lines = cmd.splitlines() or [cmd]
    return "\n".join("$ " + line for line in lines)


def verify_bash_fragment(verify):
    """Inner `{ echo; bash -c }` used by run.sh's verify step and by --preflight."""
    q = shlex.quote
    return f"{{ echo {q(_echoed_verify(verify))}; bash -c {q(verify)}; }}"


def run_verify_in_worktree(repo, wt, verify):
    """Run --verify the same way run.sh does: scrubbed env, cwd=wt, bash -c, stdin closed.

    Returns (rc, combined_output, seconds). rc is the verify exit code, or 1 if killed by a signal.
    """
    q = shlex.quote
    script = "\n".join([*clean_env_lines(repo),
                        f"cd {q(str(wt))} || exit 1",
                        f"{verify_bash_fragment(verify)} 2>&1"])
    t0 = time.monotonic()
    r = subprocess.run(["bash", "-c", script], stdin=subprocess.DEVNULL,
                       capture_output=True, text=True)
    elapsed = time.monotonic() - t0
    rc = r.returncode if r.returncode >= 0 else 1
    return rc, (r.stdout or "") + (r.stderr or ""), elapsed


def job_script(job, label, repo, wt, cmd, agent, verify=None, digest_base=None):
    """run.sh for one agent job: clean env, run, record exit code, verify."""
    q = shlex.quote
    J = lambda f: q(str(job / f))
    digest = (f"git diff --binary {digest_base} -- . ':(exclude).delegate' | sha256sum | cut -d' ' -f1"
              if digest_base else None)
    lines = ["#!/usr/bin/env bash",
             f"# delegate {label}: generated {datetime.now().isoformat(timespec='seconds')}",
             "# Clean environment, run the agent in its worktree, record exit code and last message.",
             *clean_env_lines(repo),
             f"echo $$ > {J('pid')}",
             f"date -Is > {J('started_at')}",
             f"cd {q(str(wt))} || {{ echo 1 > {J('exit_code')}; date -Is > {J('finished_at')}; "
             f"python3 {q(str(SCRIPTS / 'delegate'))} _finalize {q(str(job))}; exit 1; }}"]
    if digest:
        lines.append(f"{digest} > {J('pre_digest')}")
    lines += [f"{cmd} > {J('log')} 2>&1", "rc=$?", f'echo "$rc" > {J("exit_code")}']
    if agent == "cursor":
        lines.append(f"python3 {q(str(SCRIPTS / 'delegate'))} _cursor-last {q(str(job))}")
    if digest:
        lines.append(f"{digest} > {J('post_digest')}")
    if verify:
        lines.append(f"{verify_bash_fragment(verify)} > {J('verify.log')} 2>&1; "
                     f"echo $? > {J('verify_code')}")
    lines += [f"date -Is > {J('finished_at')}",
              f"python3 {q(str(SCRIPTS / 'delegate'))} _finalize {q(str(job))}", 'exit "$rc"', ""]
    return "\n".join(lines)


def start(job, detach, name):
    sys.stdout.flush()
    if detach:
        subprocess.Popen(["bash", str(job / "run.sh")], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        print("detached; check with: delegate status " + name)
        return
    os.execvp("bash", ["bash", str(job / "run.sh")])


def start_jobs(jobs, detach, name):
    """Start one or more jobs. Several without --detach: wait, then print each packet."""
    if len(jobs) == 1:
        start(jobs[0], detach, name)
        return
    sys.stdout.flush()
    procs = []
    for job in jobs:
        procs.append(subprocess.Popen(
            ["bash", str(job / "run.sh")], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True))
    if detach:
        print("detached; check with: delegate status " + name)
        return
    for p in procs:
        p.wait()
    failed = False
    for job in jobs:
        print("\n".join(packet(job)))
        if state(job) != "done":
            failed = True
    sys.exit(1 if failed else 0)


def find_run(ref):
    p = Path(ref).expanduser()
    if (p / "meta.json").exists():
        return p.resolve()
    hits = sorted(RUNS_ROOT.glob(f"*/{ref}/meta.json"))
    if len(hits) == 1:
        return hits[0].parent
    if not hits:
        sys.exit(f"no run named {ref!r} under {RUNS_ROOT}")
    sys.exit(f"ambiguous run name {ref!r}: " + ", ".join(str(h.parent) for h in hits))


def load_meta(root):
    return json.loads((root / "meta.json").read_text())


def save_meta(root, meta):
    path = root / "meta.json"
    tmp = path.with_name(f".meta.json.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(meta, indent=2) + "\n")
    os.replace(tmp, path)


def _exclude_file(repo):
    exclude = Path(git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=repo)) / "info" / "exclude"
    exclude.parent.mkdir(exist_ok=True)
    return exclude


def _needs_link_warning(repo, path):
    """True when --link should warn: not ignored, or only matched by our `/<path>` info/exclude line."""
    r = subprocess.run(["git", "check-ignore", "-v", "--no-index", "--", path], cwd=repo,
                       capture_output=True, text=True,
                       env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    if r.returncode != 0:
        return True
    expected = "/" + path.strip("/")
    for line in r.stdout.splitlines():
        # <source>:<linenum>:<pattern>\t<path>
        tab = line.find("\t")
        head = line if tab < 0 else line[:tab]
        parts = head.rsplit(":", 2)
        if len(parts) != 3:
            return True
        source, _num, pattern = parts
        if pattern.startswith("!"):
            return True  # deciding match is a negation → path is not ignored
        src = source.replace("\\", "/")
        if src.endswith("/info/exclude") or src.endswith("info/exclude"):
            if pattern == expected:
                continue  # tool's own form: still warn; do not treat as independently ignored
            return False  # some other exclude pattern
        return False  # .gitignore or other source
    return True


def static_warnings(repo, brief_text, verify):
    """One `warning: …` line per static issue; never changes any exit code."""
    out = []
    for stage in checks.masked_pipes(verify or ""):
        name = checks.stage_name(stage) or stage.split()[0]
        out.append(
            f"warning: --verify pipes into `{stage}` without pipefail, so a failing check can "
            f"exit 0 (the status is {name}'s). Prefix `set -o pipefail; ` or end with an "
            f"asserting stage such as `| grep -q '^OK$'`.")
    path_warns = []
    for path, kind, rel in checks.find_brief_paths(brief_text or "", repo):
        if kind == "scratchpad":
            path_warns.append(
                f"warning: the brief names {path}, in a Claude session scratchpad: the "
                f"delegate's sandbox may not read it, and it is deleted with the session. "
                f"Copy it into the repo (git-ignored) and pass --link, or describe it in the brief.")
        elif kind == "checkout" and rel == ".":
            path_warns.append(
                f"warning: the brief names the main checkout {path}; the delegate works in "
                f"its own worktree, so write repo-relative paths.")
        elif kind == "checkout" and _needs_link_warning(repo, rel):
            path_warns.append(
                f"warning: the brief names {path}, inside the main checkout; in the worktree "
                f"it is `{rel}`. Write repo-relative paths: the absolute one reads the main "
                f"checkout's copy, and writes there leave the worktree.")
    if len(path_warns) > 5:
        more = len(path_warns) - 5
        path_warns = path_warns[:5] + [
            f"warning: … and {more} more absolute path(s) in the brief"]
    out.extend(path_warns)
    return out


def _write_warnings(job, warnings):
    if warnings:
        (job / "warnings").write_text("\n".join(warnings) + "\n")


def _print_warnings(warnings):
    for line in warnings:
        print(line, file=sys.stderr)


def _dispatch_quota(agent, model, effort, fallback=False):
    """Keep explicit choices; only a default Codex reviewer may switch vendor."""
    if agent != "codex":
        return agent, model, effort, []
    check = quota.check_dispatch()
    low = [(pid, entry) for pid, entry in check["pools"].items() if entry["state"] == "low"]
    if low and fallback:
        import frontier
        try:
            report = frontier.load_report(check["catalog"], default_codex=(model, effort))
            for warning in report["warnings"]:
                print(f"review: {warning}", file=sys.stderr)
            selection = report["fallback"]
        except Exception:
            selection = None
        if selection:
            trigger = ", ".join(f"{pid} {entry['left_pct']:g}% left" for pid, entry in low)
            print(f"review: {trigger} below {check['threshold']:g}% threshold; fallback "
                  + frontier.fallback_description(selection), flush=True)
            return "cursor", selection["row"]["model"], None, []
    unknown = [f"{pid} unknown ({entry['reason']})" for pid, entry in check["pools"].items()
               if entry["state"] == "unknown"]
    if unknown:
        print(f"quota: {', '.join(unknown)}; keeping codex", file=sys.stderr)
    warnings = []
    if low:
        now = datetime.now(timezone.utc)
        parts = []
        for pid, entry in low:
            reset = quota._safe_ts(entry.get("resets_at"))
            when = (f"{reset.astimezone():%Y-%m-%d %H:%M %Z} {quota.human_delta(reset, now)}"
                    if reset else "unknown")
            parts.append(f"{pid} {entry['left_pct']:g}% left (below {check['threshold']:g}%), "
                         f"resets {when}")
        warnings.append(f"warning: {'; '.join(parts)}; consider cursor")
    return agent, model, effort, warnings


def _verify_log_tail(path, limit=1024 * 1024):
    """Last `limit` bytes of a verify.log, decoded lossily."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - limit))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _exclude(repo, patterns):
    """Append exact paths to the shared info/exclude. Preserve existing bytes; return newly added lines."""
    exclude = _exclude_file(repo)
    raw = exclude.read_bytes() if exclude.exists() else b""
    present = set(raw.splitlines())  # splitlines strips \\r?\\n
    added = []
    out = raw
    for p in patterns:
        pb = p.encode()
        if pb in present:
            continue
        if out and not out.endswith(b"\n"):
            out += b"\n"
        out += pb + b"\n"
        present.add(pb)
        added.append(p)
    if added:
        exclude.write_bytes(out)
    return added


def _remove_excluded(repo, patterns):
    """Drop exclude lines whose content (sans terminator) equals an owned pattern; keep all other bytes."""
    if not patterns:
        return
    exclude = _exclude_file(repo)
    if not exclude.exists():
        return
    raw = exclude.read_bytes()
    drop = {p.encode() for p in patterns}
    kept = []
    for part in raw.splitlines(keepends=True):
        if part.rstrip(b"\r\n") in drop:
            continue
        kept.append(part)
    new = b"".join(kept)
    if new != raw:
        exclude.write_bytes(new)


def _iter_live_linkers(repo, link, skip_root=None):
    """Yield (run_root, meta) for live runs of repo that list link in meta['links']."""
    repo_s = str(Path(repo).resolve())
    skip = Path(skip_root).resolve() if skip_root is not None else None
    for meta_path in RUNS_ROOT.glob("*/*/meta.json"):
        other = meta_path.parent
        if skip is not None and other.resolve() == skip:
            continue
        try:
            m = json.loads(meta_path.read_text())
            if str(Path(m.get("repo", "")).resolve()) != repo_s:
                continue
        except (OSError, ValueError):
            continue
        if not Path(m.get("worktree", "")).is_dir():
            continue
        if link in m.get("links", []):
            yield other, m


def _release_excluded(repo, root, meta):
    """Transfer owned exclude lines to a live sibling linker, or remove them; then clear meta['excluded']."""
    owned = list(meta.get("excluded", []))
    if "excluded" not in meta and not owned:
        return  # legacy: no key → change nothing
    to_remove = []
    for pattern in owned:
        link = pattern.lstrip("/")
        transferred = False
        for other_root, other_meta in _iter_live_linkers(repo, link, skip_root=root):
            excl = list(other_meta.get("excluded", []))
            if pattern not in excl:
                excl.append(pattern)
                other_meta["excluded"] = excl
                save_meta(other_root, other_meta)
            transferred = True
            break
        if not transferred:
            to_remove.append(pattern)
    _remove_excluded(repo, to_remove)
    meta["excluded"] = []
    save_meta(root, meta)


def add_links(repo, wt, paths, existing=()):
    """Validate links first, then install them and share launch/followup exclusions.

    Returns exclude lines newly appended for paths that need a warning (not independently
    ignored). `/.delegate` is never returned.
    """
    for link in paths:
        p = Path(link)
        if p.is_absolute() or ".." in p.parts or not p.parts or p.parts[0] in (".git", ".delegate"):
            sys.exit(f"--link {link}: expected a repo-relative input path outside .git and .delegate")
        src, dst = repo / p, wt / p
        if not src.exists():
            sys.exit(f"--link {link}: {src} does not exist")
        if dst.exists() or dst.is_symlink():
            if link in existing and dst.is_symlink() and dst.resolve() == src.resolve():
                continue
            sys.exit(f"--link {link}: {dst} already exists in the worktree (tracked path?)")
    recordable = []
    for link in paths:
        pattern = "/" + link.strip("/")
        if _needs_link_warning(repo, link):
            print(f"WARNING: --link {link} is not gitignored in the main checkout; until cleanup, "
                  f".git/info/exclude hides it from git status there too", file=sys.stderr)
            recordable.append(pattern)
    for link in paths:
        dst = wt / link
        if not dst.is_symlink():
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.symlink_to(repo / link)
    added = _exclude(repo, ["/" + l.strip("/") for l in paths] + ["/.delegate"])
    return [p for p in added if p in recordable]


def _abandon_launch(repo, root, wt, branch, excluded=()):
    """Drop a half-created launch (bad --link or --preflight); undo exclude lines this attempt added."""
    git("worktree", "remove", "--force", str(wt), cwd=repo, check=False)
    git("branch", "-D", branch, cwd=repo, check=False)
    shutil.rmtree(root, ignore_errors=True)
    _remove_excluded(repo, excluded)


def _uncommitted_counts(repo):
    """(tracked_paths, untracked_paths) from `git status --porcelain --untracked-files=all`."""
    tracked = untracked = 0
    for line in git("status", "--porcelain", "--untracked-files=all", cwd=repo).splitlines():
        if not line.strip():
            continue
        if line.startswith("??"):
            untracked += 1
        else:
            tracked += 1
    return tracked, untracked


def _snapshot_contract_bullet(base9, snapshot_of9):
    return SNAPSHOT_BULLET.format(base=base9, snapshot_of=snapshot_of9)


def launch(a):
    repo = Path(git("rev-parse", "--show-toplevel", cwd=a.repo or os.getcwd()))
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = a.name or f"{a.agent}-{stamp}"
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        sys.exit(f"bad run name {name!r}: use letters, digits, . _ -")
    root = Path(a.root).expanduser().resolve() if a.root else RUNS_ROOT / repo.name / name
    if root.exists() and any(root.iterdir()):
        sys.exit(f"{root} already exists and is not empty")
    branch = f"delegate/{name}"
    if git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", cwd=repo, check=False):
        sys.exit(f"branch {branch} already exists")
    if a.unsandboxed and a.agent != "cursor":
        sys.exit("--unsandboxed applies to cursor only (codex: pick -s yourself in run.sh)")
    if getattr(a, "web_search", False) and a.agent != "codex":
        sys.exit("--web-search applies to codex only")
    include_uncommitted = getattr(a, "include_uncommitted", False)
    all_untracked = getattr(a, "all_untracked", False)
    if include_uncommitted and a.base:
        sys.exit("--include-uncommitted cannot be combined with --base; "
                 "the snapshot's parent is always HEAD")
    preflight = getattr(a, "preflight", False)
    if preflight:
        if not a.verify:
            sys.exit("--preflight requires --verify; a preflight is always a foreground check")
        if a.dry_run or a.detach:
            sys.exit("--preflight cannot be combined with --dry-run or --detach; "
                     "a preflight is always a foreground check")
    head_sha = git("rev-parse", "HEAD", cwd=repo)
    base_sha = git("rev-parse", a.base or "HEAD", cwd=repo)
    brief = Path(a.brief).expanduser().read_text()
    model = a.model or DEFAULTS[a.agent][0]
    effort = a.effort if a.effort is not None else (None if a.model else DEFAULTS[a.agent][1])
    wt = root / "wt"
    links = [p.strip("/") for p in a.link]
    tracked_n, untracked_n = _uncommitted_counts(repo)
    entries = list_untracked(repo)
    will_snapshot = False
    snapshot_of = None
    if include_uncommitted:
        if working_tree_has_changes(repo, head_sha, untracked=entries):
            will_snapshot = True
        else:
            print("note: no uncommitted changes; launching from HEAD", file=sys.stderr)
    elif tracked_n or untracked_n:
        print(f"note: {repo} has uncommitted changes ({tracked_n} tracked, {untracked_n} untracked "
              f"path(s)); the delegate starts from {base_sha[:9]} and will not see them; "
              f"--include-uncommitted snapshots them", file=sys.stderr)
    if will_snapshot:
        print_untracked_summary(entries)
        refuse_large_untracked(entries, all_untracked)
        validate_working_tree_links(repo, links)
    warnings = static_warnings(repo, brief, a.verify)
    if not a.dry_run and not preflight:
        _, _, _, quota_warnings = _dispatch_quota(a.agent, model, effort)
        warnings.extend(quota_warnings)
    _print_warnings(warnings)

    scope = ("This is a read-only task: do not modify, create or delete any files."
             if a.read_only else "Work only inside this worktree; do not modify files outside it.")
    links_txt = "".join(f"- `{l}` is a symlink to the main checkout's copy; treat it as read-only input.\n"
                        for l in links)

    def build_prompt(base9, snapshot_txt):
        return brief.rstrip() + CONTRACT.format(
            wt=wt, branch=branch, base=base9, scope=scope, links=links_txt, snapshot=snapshot_txt)

    cmd = build_cmd(a.agent, model, effort, wt, root, a.read_only, a.unsandboxed,
                    web_search=getattr(a, "web_search", False))
    run_sh = job_script(root, f"run {name}", repo, wt, cmd, a.agent, verify=a.verify)

    if a.dry_run:
        if will_snapshot:
            print(f"[dry run] would snapshot uncommitted work on top of {head_sha[:9]}")
            snapshot_txt = _snapshot_contract_bullet("SNAPSHOT", head_sha[:9])
            prompt = build_prompt("SNAPSHOT", snapshot_txt)
            print(f"[dry run] would create {root} and worktree {wt} on {branch} from SNAPSHOT "
                  f"(snapshot of {head_sha[:9]} + uncommitted)\n")
        else:
            prompt = build_prompt(base_sha[:9], "")
            print(f"[dry run] would create {root} and worktree {wt} on {branch} from {base_sha[:9]}\n")
        print(run_sh)
        print("---- prompt.md ----")
        print(prompt)
        return

    if will_snapshot:
        base_sha = snapshot_working_tree(
            repo, message=f"delegate launch snapshot: uncommitted work on top of {head_sha[:9]}")
        snapshot_of = head_sha
        print(f"snapshot: {base_sha[:9]} = HEAD {head_sha[:9]} + {tracked_n} tracked change(s), "
              f"{untracked_n} untracked file(s)")

    snapshot_txt = (_snapshot_contract_bullet(base_sha[:9], snapshot_of[:9]) if snapshot_of else "")
    prompt = build_prompt(base_sha[:9], snapshot_txt)
    meta = {"name": name, "agent": a.agent, "model": model, "effort": effort,
            "session": os.environ.get("CLAUDE_CODE_SESSION_ID") or None,
            "read_only": a.read_only, "unsandboxed": a.unsandboxed,
            "web_search": getattr(a, "web_search", False), "verify": a.verify,
            "repo": str(repo), "worktree": str(wt), "branch": branch, "base": base_sha,
            "links": links, "rounds": 1, "created": datetime.now().isoformat(timespec="seconds"),
            "brief_source": str(Path(a.brief).expanduser().resolve())}
    if snapshot_of:
        meta["snapshot_of"] = snapshot_of

    root.mkdir(parents=True, exist_ok=True)
    excluded = []
    try:
        # Guard from worktree creation through meta.json: without meta, cleanup cannot find orphans.
        git("worktree", "add", "-b", branch, str(wt), base_sha, cwd=repo)
        excluded = add_links(repo, wt, links)
        if preflight:
            rc, output, elapsed = run_verify_in_worktree(repo, wt, a.verify)
        else:
            meta["excluded"] = excluded
            (root / "brief.md").write_text(brief)
            (root / "prompt.md").write_text(prompt)
            (root / "run.sh").write_text(run_sh)
            _write_warnings(root, warnings)
            save_meta(root, meta)
    except BaseException:  # leave no orphan worktree/branch behind: there is no meta.json for cleanup to find
        _abandon_launch(repo, root, wt, branch, excluded)
        raise
    if preflight:
        _abandon_launch(repo, root, wt, branch, excluded)
        print("\n".join(output.rstrip().splitlines()[-30:]))
        status = ("pass" if rc == 0 else f"FAIL rc={rc}")
        print(f"preflight: verify {status} in {elapsed:.1f} s at base {base_sha[:9]}")
        if rc == 0:
            hit = checks.failure_line(output)
            if hit:
                print(f"WARNING: verify exited 0 but printed a failure line: {hit}")
        if rc != 0:
            print("hint: a check of the not-yet-built work is expected to fail at base, while an error "
                  "before any check runs (path, import, missing input or link) means the checker or a "
                  "--link is broken.")
        sys.exit(rc)

    print(f"run:      {root}")
    if snapshot_of:
        print(f"worktree: {wt}  (branch {branch}, base {base_sha[:9]} "
              f"(snapshot of {snapshot_of[:9]} + uncommitted))")
    else:
        print(f"worktree: {wt}  (branch {branch}, base {base_sha[:9]})")
    print(f"agent:    {a.agent} {model}" + (f" @{effort}" if effort else "") + (" read-only" if a.read_only else ""))
    print(f"log:      {root / 'log'}")
    print(f"last msg: {root / 'last-message.md'}")
    if a.verify:
        print(f"verify:   {a.verify}")
    quota.spawn_snapshot("launch", run=name, session=os.environ.get("CLAUDE_CODE_SESSION_ID"),
                         job="round-1")
    log_delegation("launch", run=name, agent=a.agent)
    start(root, a.detach, name)


def session_id(job, agent):
    """The agent's session/chat id, read from a finished job's log."""
    log = job / "log"
    if not log.exists():
        return None
    text = log.read_text(errors="replace")
    if agent == "codex":
        for ev in _events(job):
            if ev.get("type") == "thread.started" and ev.get("thread_id"):
                return ev["thread_id"]
        m = re.search(r"^session id: (\S+)", text, re.M)
        return m.group(1) if m else None
    for line in text.splitlines():
        try:
            sid = json.loads(line).get("session_id")
        except (ValueError, AttributeError):
            continue
        if sid:
            return sid
    return None


def followup(a):
    root = find_run(a.run)
    if state(root) in BUSY or _finalizing(root):
        sys.exit("delegate is still running")
    meta = load_meta(root)  # after the guard, or finalize's history could be saved back stale
    if meta.get("kind") == "working-tree":
        sys.exit("a working-tree run has nothing to resume")
    wt = Path(meta["worktree"])
    if not wt.exists():
        sys.exit("worktree was removed; start a new run instead")
    brief_text = Path(a.brief).expanduser().read_text().rstrip() if a.brief else ""
    text = brief_text
    if a.from_review:
        rv = root / f"review-{a.from_review}" / "last-message.md"
        if not rv.exists():
            sys.exit(f"no review {a.from_review} with a last message in {root}")
        text = (text + "\n\n" if text else "") + "## Review findings to address\n\n" + rv.read_text().rstrip()
    if not text:
        sys.exit("give --brief and/or --from-review")
    k = meta.get("rounds", 1)
    sid = None if a.fresh else session_id(root, meta["agent"])
    if not a.fresh and not sid:
        sys.exit(f"no session id found in {root / 'log'}; use --fresh to start a new session in the same worktree")
    model = a.model or meta["model"]
    effort = a.effort if a.effort is not None else (None if a.model else meta.get("effort"))
    verify = a.verify if a.verify is not None else meta.get("verify")
    warnings = static_warnings(Path(meta["repo"]), brief_text, verify)
    if not a.dry_run:
        _, _, _, quota_warnings = _dispatch_quota(meta["agent"], model, effort)
        warnings.extend(quota_warnings)
    _print_warnings(warnings)

    archive = root / f"round-{k}"
    if a.dry_run:
        print(f"[dry run] round {k + 1}: {meta['agent']} {model}"
              + (f", resuming session {sid}" if sid else ", fresh session") + f"\n---- brief ----\n{text}")
        return
    new_links = [p.strip("/") for p in a.link]
    links = list(dict.fromkeys(meta.get("links", []) + new_links))
    added = add_links(Path(meta["repo"]), wt, new_links, existing=meta.get("links", []))
    meta["links"] = links
    meta["excluded"] = list(dict.fromkeys(meta.get("excluded", []) + added))
    if (root / "exit_code").exists() and not any(r.get("n", r.get("round")) == k for r in meta.get("history", [])):
        meta.setdefault("history", []).append(_round_record(root, meta, k))
    archive.mkdir(exist_ok=True)
    for f in ROUND_FILES:
        if (root / f).exists():
            shutil.move(str(root / f), str(archive / f))
    _write_warnings(root, warnings)
    if a.fresh:
        scope = ("This is a read-only task: do not modify, create or delete any files."
                 if meta.get("read_only") else "Work only inside this worktree; do not modify files outside it.")
        links_txt = "".join(f"- `{l}` is a symlink to the main checkout's copy; treat it as read-only input.\n"
                            for l in meta.get("links", []))
        snapshot_txt = ""
        if meta.get("snapshot_of"):
            snapshot_txt = _snapshot_contract_bullet(meta["base"][:9], meta["snapshot_of"][:9])
        prompt = (text + "\n\nAn earlier attempt's changes are already in this worktree (see `git diff "
                  f"{meta['base'][:9]}`); build on them rather than starting over."
                  + CONTRACT.format(wt=wt, branch=meta["branch"], base=meta["base"][:9], scope=scope,
                                    links=links_txt, snapshot=snapshot_txt))
    else:
        prompt = text + FOLLOWUP_NOTE.format(round=k + 1)
        prompt += "".join(f"- `{l}` is a symlink to the main checkout's copy; treat it as read-only input.\n"
                          for l in new_links)
    cmd = build_cmd(meta["agent"], model, effort, wt, root, meta.get("read_only"), meta.get("unsandboxed"),
                    resume=sid, web_search=meta.get("web_search", False))
    (root / "brief.md").write_text(text + "\n")
    (root / "prompt.md").write_text(prompt)
    (root / "run.sh").write_text(job_script(root, f"run {meta['name']} round {k + 1}", Path(meta["repo"]), wt,
                                            cmd, meta["agent"], verify=verify))
    meta.update(rounds=k + 1, model=model, effort=effort, verify=verify, resumed=bool(sid))
    save_meta(root, meta)
    print(f"round {k} archived to {archive}")
    print(f"round {k + 1}: {meta['agent']} {model}" + (f" @{effort}" if effort else "")
          + (f", resuming session {sid}" if sid else ", fresh session"))
    quota.spawn_snapshot("launch", run=meta["name"], session=os.environ.get("CLAUDE_CODE_SESSION_ID"),
                         job=f"round-{k + 1}")
    log_delegation("followup", run=meta["name"], agent=meta["agent"])
    start(root, a.detach, meta["name"])


def _unquote_git_path(s):
    """Unquote a Git C-style path (`\"…\"` with backslash / octal byte escapes)."""
    if len(s) < 2 or s[0] != '"':
        return s
    buf = bytearray()
    i, end = 1, len(s) - (1 if s[-1] == '"' else 0)
    while i < end:
        if s[i] != "\\" or i + 1 >= end:
            buf.extend(s[i].encode("utf-8", "surrogateescape"))
            i += 1
            continue
        n = s[i + 1]
        simple = {"\\": b"\\", '"': b'"', "a": b"\a", "b": b"\b", "t": b"\t",
                  "n": b"\n", "v": b"\v", "f": b"\f", "r": b"\r"}
        if n in simple:
            buf.extend(simple[n])
            i += 2
        elif n in "0123456789":
            j = i + 1
            while j < end and j < i + 4 and s[j] in "0123456789":
                j += 1
            buf.append(int(s[i + 1:j], 8) & 0xFF)
            i = j
        else:
            buf.extend(n.encode("utf-8", "surrogateescape"))
            i += 2
    return buf.decode("utf-8", "surrogateescape")


def _path_from_diff_ab_line(rest):
    """Path from a `+++` / `---` payload: unquote (or strip trailing TAB), then drop a/ or b/."""
    if rest == "/dev/null":
        return None
    if rest.startswith('"'):
        path = _unquote_git_path(rest)
    else:
        path = rest[:-1] if rest.endswith("\t") else rest
    if path.startswith(("a/", "b/")):
        path = path[2:]
    return path


def _diff_section_path(section):
    """Best path name for a `diff --git` section (rename to / +++ / --- / equal header halves)."""
    lines = section.splitlines()
    rename_to = plus = minus = None
    header = lines[0] if lines else ""
    for line in lines:
        if line.startswith("rename to "):
            rename_to = line[len("rename to "):]
        elif line.startswith("+++ "):
            plus = _path_from_diff_ab_line(line[4:])
        elif line.startswith("--- "):
            minus = _path_from_diff_ab_line(line[4:])
    if rename_to is not None:
        return _unquote_git_path(rename_to)
    if plus is not None:
        return plus
    if minus is not None:
        return minus
    rest = header[len("diff --git "):] if header.startswith("diff --git ") else ""
    if rest.startswith('"'):
        # two C-quoted paths — unquote first, then strip a/ / b/ (same order as +++ / ---)
        paths, i = [], 0
        while i < len(rest) and len(paths) < 2:
            if rest[i] != '"':
                i += 1
                continue
            j = i + 1
            while j < len(rest):
                if rest[j] == "\\" and j + 1 < len(rest):
                    j += 2
                    continue
                if rest[j] == '"':
                    break
                j += 1
            paths.append(_unquote_git_path(rest[i:j + 1]))
            i = j + 1
        if len(paths) == 2:
            a = paths[0][2:] if paths[0].startswith("a/") else paths[0]
            b = paths[1][2:] if paths[1].startswith("b/") else paths[1]
            return b if a == b else b
    elif len(rest) % 2 == 1 and rest[len(rest) // 2] == " ":
        mid = len(rest) // 2
        left, right = rest[:mid], rest[mid + 1:]
        if left.startswith("a/") and right.startswith("b/") and left[2:] == right[2:]:
            return left[2:]
    return "?"


def _trim_review_patch(patch, max_kb):
    """Replace oversized per-file diff sections with a header + note. Returns (trimmed, omitted).

    omitted is a list of (path, size_kb) for sections that were replaced. max_kb <= 0 disables.
    """
    if max_kb <= 0 or not patch:
        return patch, []
    cap = max_kb * 1024
    sections, cur = [], []
    for line in patch.splitlines(keepends=True):
        if line.startswith("diff --git ") and cur:
            sections.append("".join(cur))
            cur = [line]
        else:
            cur.append(line)
    if cur:
        sections.append("".join(cur))
    out, omitted = [], []
    for section in sections:
        raw = section.encode("utf-8", "surrogateescape")
        if len(raw) <= cap:
            out.append(section)
            continue
        header = section.splitlines(keepends=True)[0]
        if not header.endswith("\n"):
            header += "\n"
        path = _diff_section_path(section)
        size_kb = (len(raw) + 1023) // 1024
        omitted.append((path, size_kb))
        out.append(header + f"# delegate: diff omitted from the review patch "
                   f"({size_kb} KB > {max_kb} KB); read the file in the worktree.\n")
    return "".join(out), omitted


def list_untracked(repo):
    """Non-ignored untracked paths with lstat sizes: [(path, size), ...]."""
    raw = git("ls-files", "--others", "--exclude-standard", "-z", cwd=repo)
    entries = []
    for path in filter(None, raw.split("\0")):
        try:
            size = (repo / path).lstat().st_size
        except OSError:
            size = 0
        entries.append((path, size))
    return entries


def print_untracked_summary(entries, limit=20):
    total = sum(size for _, size in entries)
    print(f"untracked: {len(entries)} file(s), {total} bytes total")
    for path, size in entries[:limit]:
        print(f"  {path} ({size} bytes)")
    if len(entries) > limit:
        print(f"  ... {len(entries) - limit} more")
    return total


def refuse_large_untracked(entries, all_untracked):
    """Exit before writing objects when untracked volume is high, unless --all-untracked."""
    if all_untracked:
        return
    total = sum(size for _, size in entries)
    if len(entries) <= UNTRACKED_MAX_FILES and total <= UNTRACKED_MAX_BYTES:
        return
    largest = sorted(entries, key=lambda item: -item[1])[:10]
    listing = ", ".join(f"{path} ({size} bytes)" for path, size in largest)
    sys.exit(
        f"refusing to snapshot: {len(entries)} untracked file(s), {total} bytes "
        f"(limits: {UNTRACKED_MAX_FILES} files, {UNTRACKED_MAX_BYTES} bytes). "
        f"Largest: {listing}. Add them to .gitignore, or pass --all-untracked.")


def working_tree_has_changes(repo, base_sha, untracked=None):
    """True when the snapshot-vs-base review would be non-empty.

    Always asks for untracked files explicitly: `status.showUntrackedFiles=no` would
    otherwise make an untracked-only tree look clean.
    """
    if untracked:
        return True
    porcelain = git("status", "--porcelain", "--untracked-files=all", cwd=repo)
    if porcelain.strip():
        return True
    head = git("rev-parse", "HEAD", cwd=repo)
    if base_sha == head:
        return False
    r = subprocess.run(["git", "diff", "--quiet", base_sha, head], cwd=repo,
                       capture_output=True, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    return r.returncode != 0


def validate_working_tree_links(repo, links):
    """Same path checks as add_links, plus refuse non-gitignored paths (already in the snapshot)."""
    for link in links:
        p = Path(link)
        if p.is_absolute() or ".." in p.parts or not p.parts or p.parts[0] in (".git", ".delegate"):
            sys.exit(f"--link {link}: expected a repo-relative input path outside .git and .delegate")
        src = repo / p
        if not src.exists():
            sys.exit(f"--link {link}: {src} does not exist")
        # Index-aware check-ignore matches what `git add -A` skips: tracked paths count as not
        # ignored, and a delegate-owned info/exclude line (another run's --link) counts as ignored.
        r = subprocess.run(["git", "check-ignore", "-q", "--", link], cwd=repo, capture_output=True,
                           env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
        if r.returncode != 0:
            sys.exit(f"--link {link}: not gitignored (or tracked), so the snapshot already holds it; "
                     f"drop --link")


def copy_temp_index(repo):
    """Copy the real index to a temp file (caller must unlink). Does not touch the real index."""
    index_path = Path(git("rev-parse", "--git-path", "index", cwd=repo))
    if not index_path.is_absolute():
        index_path = Path(repo) / index_path
    fd, tmp_name = tempfile.mkstemp(prefix="delegate-index-")
    os.close(fd)
    tmp_index = Path(tmp_name)
    if index_path.exists():
        shutil.copy2(index_path, tmp_index)
    else:
        tmp_index.write_bytes(b"")
    return tmp_index


def snapshot_working_tree(repo, message="delegate working-tree snapshot"):
    """Dangling commit of the working tree; does not touch the real index, HEAD or any ref.

    Copies the real index to a temp GIT_INDEX_FILE, `git add -A`, write-tree, commit-tree -p HEAD.
    Used by `review --working-tree` and `launch --include-uncommitted`.
    """
    tmp_index = copy_temp_index(repo)
    try:
        env = {**os.environ, "GIT_INDEX_FILE": str(tmp_index), "GIT_OPTIONAL_LOCKS": "0"}
        r = subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, env=env)
        if r.returncode != 0:
            sys.exit(f"git add -A (temp index) failed: {r.stderr.decode('utf-8', 'replace').strip()}")
        tree = subprocess.run(["git", "write-tree"], cwd=repo, capture_output=True, text=True, env=env)
        if tree.returncode != 0:
            sys.exit(f"git write-tree failed: {tree.stderr.strip()}")
        head = git("rev-parse", "HEAD", cwd=repo)
        commit = subprocess.run(
            ["git", "commit-tree", tree.stdout.strip(), "-p", head, "-m", message],
            cwd=repo, capture_output=True, text=True,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
        if commit.returncode != 0:
            sys.exit(f"git commit-tree failed: {commit.stderr.strip()}")
        return commit.stdout.strip()
    finally:
        tmp_index.unlink(missing_ok=True)


def _working_tree_review_prompt(wt, base, n, agent, brief, focus, omit_note):
    """Review prompt for a main-checkout snapshot; drops the verify-log sentence (no verify)."""
    shell_note = ("" if agent == "cursor" else
                  " You may run read-only commands (tests, dry runs, `git diff`). The sandbox is read-only, so "
                  "a command that fails only because it cannot write is not a finding; say so instead.")
    opening = (
        f"# Review request (read-only)\n\n"
        f"The orchestrator (Claude) made these uncommitted changes in the main checkout; "
        f"this worktree is a snapshot of them, relative to commit {base}. Review them. "
        f"This is a read-only task: do not modify, create or delete any files.\n\n"
        f"The full diff is in `.delegate/review-{n}.patch` (path relative to the worktree root; "
        f"new files appear as additions). Read it, then read the surrounding code in the worktree "
        f"where you need context.{shell_note}\n"
        f"{omit_note}\n"
    )
    return opening + REVIEW_PROMPT_CHECKS.format(brief=brief, focus=focus)


def _abandon_working_tree(repo, root, wt, excluded=()):
    git("worktree", "remove", "--force", str(wt), cwd=repo, check=False)
    shutil.rmtree(root, ignore_errors=True)
    _remove_excluded(repo, excluded)


def _default_working_tree_reviewers(a):
    """Default both-vendor pair without consulting quota (used by --dry-run)."""
    if a.model:
        sys.exit("--model needs a single --agent (default is both reviewers)")
    codex_effort = a.effort if a.effort is not None else DEFAULTS["codex"][1]
    return [("codex", DEFAULTS["codex"][0], codex_effort),
            ("cursor", DEFAULTS["cursor"][0], None)]


def _reviewers_for_working_tree(a):
    """[(agent, model, effort), ...] — both vendors by default; optional Codex→Cursor fallback."""
    if a.agent:
        agent = a.agent
        model = a.model or DEFAULTS[agent][0]
        effort = a.effort if a.effort is not None else (None if a.model else DEFAULTS[agent][1])
        return [(agent, model, effort)]
    reviewers = _default_working_tree_reviewers(a)
    # Lazy: ask imports runs.
    import ask as ask_mod
    fallback = ask_mod._fallback_for_both(a)
    if fallback:
        return [("cursor", fallback["model"], None), ("cursor", reviewers[1][1], None)]
    return reviewers


def review_working_tree(a):
    """Read-only cross-vendor review of uncommitted work in the main checkout."""
    if not a.brief:
        sys.exit("--working-tree requires --brief (the change's intent or spec)")
    if a.run:
        sys.exit("give a run name or --working-tree, not both")
    repo = Path(git("rev-parse", "--show-toplevel", cwd=getattr(a, "repo", None) or os.getcwd()))
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = a.name or f"wt-{stamp}"
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        sys.exit(f"bad run name {name!r}: use letters, digits, . _ -")
    root = Path(a.root).expanduser().resolve() if getattr(a, "root", None) else RUNS_ROOT / repo.name / name
    if root.exists() and any(root.iterdir()):
        sys.exit(f"{root} already exists and is not empty")
    base_sha = git("rev-parse", a.base or "HEAD", cwd=repo)
    brief = Path(a.brief).expanduser().read_text()
    warnings = static_warnings(repo, brief, None)
    _print_warnings(warnings)
    links = [p.strip("/") for p in (a.link or [])]
    validate_working_tree_links(repo, links)
    entries = list_untracked(repo)
    print_untracked_summary(entries)
    refuse_large_untracked(entries, getattr(a, "all_untracked", False))
    if not working_tree_has_changes(repo, base_sha, untracked=entries):
        sys.exit("no changes to review")
    focus = f"\n## Extra focus from the orchestrator\n{a.focus}\n" if a.focus else ""

    if a.dry_run:
        # No quota/ratings: dry-run must not write the ratings cache.
        if a.agent:
            reviewers = _reviewers_for_working_tree(a)
        else:
            reviewers = _default_working_tree_reviewers(a)
            if not getattr(a, "no_fallback", False):
                print("note: at launch, the Codex half is swapped for a Cursor model if "
                      "codex-5h or codex-7d is under min_quota_pct")
        print(f"[dry run] would snapshot {repo} (base {base_sha[:9]}) into {root}; "
              f"reviewers: " + ", ".join(
                  f"{ag} {mo}" + (f" @{ef}" if ef else "") for ag, mo, ef in reviewers))
        for i, (agent, model, effort) in enumerate(reviewers, 1):
            prompt = _working_tree_review_prompt(root / "wt", base_sha[:9], i, agent, brief.rstrip(),
                                                 focus, "")
            cmd = build_cmd(agent, model, effort, root / "wt", root / f"review-{i}", read_only=True)
            print(f"\n[dry run] review {i} by {agent} {model}: {cmd}\n---- prompt.md ----\n{prompt}")
        return

    reviewers = _reviewers_for_working_tree(a)
    if a.agent:
        _, _, _, quota_warnings = _dispatch_quota(*reviewers[0])
        _print_warnings(quota_warnings)
        warnings = warnings + quota_warnings
    snapshot = snapshot_working_tree(repo)
    root.mkdir(parents=True, exist_ok=True)
    wt = root / "wt"
    excluded = []
    try:
        git("worktree", "add", "--detach", str(wt), snapshot, cwd=repo)
        excluded = add_links(repo, wt, links)
    except BaseException:
        _abandon_working_tree(repo, root, wt, excluded)
        raise
    meta = {"name": name, "kind": "working-tree", "agent": "claude", "model": None, "effort": None,
            "session": os.environ.get("CLAUDE_CODE_SESSION_ID") or None,
            "read_only": True, "verify": None, "repo": str(repo), "worktree": str(wt),
            "base": base_sha, "snapshot": snapshot, "links": links, "excluded": excluded,
            "rounds": 0, "created": datetime.now().isoformat(timespec="seconds"),
            "brief_source": str(Path(a.brief).expanduser().resolve())}
    (root / "brief.md").write_text(brief)
    save_meta(root, meta)

    _, patch = make_patch(root, meta)
    if not patch.strip():
        _abandon_working_tree(repo, root, wt, excluded)
        sys.exit("no changes to review")
    max_kb = getattr(a, "patch_max_kb", 200)
    review_patch, omitted = _trim_review_patch(patch, max_kb)
    omit_line = ""
    if omitted:
        bits = ", ".join(f"{p} ({s} KB)" for p, s in omitted)
        omit_line = f"review patch: omitted {len(omitted)} large file diff(s): {bits}"
        print(omit_line)
    omit_note = ""
    if omitted:
        files = ", ".join(f"{p} ({s} KB)" for p, s in omitted)
        omit_note = (f"\nLarge file diffs omitted from the review patch (read them in the worktree instead): "
                     f"{files}.\n")

    (wt / ".delegate").mkdir(exist_ok=True)
    for old in (wt / ".delegate").glob("review-*.patch"):
        old.unlink()
    jobs = []
    for n, (agent, model, effort) in enumerate(reviewers, 1):
        job = root / f"review-{n}"
        job.mkdir()
        write_patch(wt / ".delegate" / f"review-{n}.patch", review_patch)
        # Stub only: working-tree runs have no verify; the prompt omits the verify-log sentence.
        (wt / ".delegate" / f"review-{n}.verify.log").write_text(
            "$ (no verify command stored)\n(no verify output recorded)\n")
        prompt = _working_tree_review_prompt(wt, base_sha[:9], n, agent, brief.rstrip(), focus, omit_note)
        cmd = build_cmd(agent, model, effort, wt, job, read_only=True)
        (job / "prompt.md").write_text(prompt)
        (job / "run.sh").write_text(job_script(job, f"review {name} #{n}", repo, wt, cmd, agent,
                                               digest_base=base_sha))
        _write_warnings(job, warnings)
        save_meta(job, {"agent": agent, "model": model, "effort": effort, "n": n, "round": 0})
        print(f"review {n} of {name} (working-tree) by {agent} {model}"
              + (f" @{effort}" if effort else ""))
        print(f"result:   {job / 'last-message.md'}")
        quota.spawn_snapshot("launch", run=name, session=os.environ.get("CLAUDE_CODE_SESSION_ID"),
                             job=f"review-{n}")
        jobs.append(job)
    log_agent = "both" if len(reviewers) > 1 else reviewers[0][0]
    log_delegation("review", run=name, agent=log_agent)
    print(f"run:      {root}")
    print(f"worktree: {wt}  (snapshot {snapshot[:9]}, base {base_sha[:9]})")
    start_jobs(jobs, a.detach, name)


def review(a):
    if getattr(a, "brief", None) and not getattr(a, "working_tree", False):
        sys.exit("--brief is only valid with --working-tree (a run's review uses the run's own briefs)")
    if getattr(a, "working_tree", False):
        return review_working_tree(a)
    if not a.run:
        sys.exit("give a run name, or --working-tree")
    root = find_run(a.run)
    if state(root) in BUSY or _finalizing(root):
        sys.exit("delegate is still running")
    meta = load_meta(root)  # after the guard, or finalize's history could be saved back stale
    if meta.get("kind") == "working-tree":
        sys.exit(f"{meta['name']} is a working-tree review run; start a new "
                 f"`delegate review --working-tree` after fixes")
    wt = Path(meta["worktree"])
    if not wt.exists():
        sys.exit("worktree was removed")
    agent = a.agent or ("cursor" if meta["agent"] == "codex" else "codex")
    model = a.model or DEFAULTS[agent][0]
    effort = a.effort if a.effort is not None else (None if a.model else DEFAULTS[agent][1])
    _, patch = make_patch(root, meta)
    if not patch.strip():
        sys.exit("no changes to review")
    warnings = []
    if not a.dry_run:
        # An explicit --model is a choice too: only a fully default reviewer swaps vendor.
        agent, model, effort, warnings = _dispatch_quota(agent, model, effort,
                                                         fallback=not (a.agent or a.model))
        _print_warnings(warnings)
    max_kb = getattr(a, "patch_max_kb", 200)
    review_patch, omitted = _trim_review_patch(patch, max_kb)
    omit_line = ""
    if omitted:
        bits = ", ".join(f"{p} ({s} KB)" for p, s in omitted)
        omit_line = f"review patch: omitted {len(omitted)} large file diff(s): {bits}"
    n = 1 + sum(1 for _ in root.glob("review-*"))
    job = root / f"review-{n}"
    # every round's brief: the original task is the spec, follow-ups amend it
    briefs = [p for p in sorted(root.glob("round-*/brief.md"), key=lambda p: int(p.parent.name.split("-")[1]))]
    briefs += [root / "brief.md"] if (root / "brief.md").exists() else []
    if len(briefs) > 1:
        brief = "\n\n".join(f"### Round {i} ({'original task' if i == 1 else 'follow-up'})\n{p.read_text().rstrip()}"
                            for i, p in enumerate(briefs, 1))
    else:
        brief = briefs[0].read_text().rstrip() if briefs else "(brief not found)"
    shell_note = ("" if agent == "cursor" else
                  " You may run read-only commands (tests, dry runs, `git diff`). The sandbox is read-only, so "
                  "a command that fails only because it cannot write is not a finding; say so instead.")
    focus = f"\n## Extra focus from the orchestrator\n{a.focus}\n" if a.focus else ""
    omit_note = ""
    if omitted:
        files = ", ".join(f"{p} ({s} KB)" for p, s in omitted)
        omit_note = (f"\nLarge file diffs omitted from the review patch (read them in the worktree instead): "
                     f"{files}.\n")
    prompt = REVIEW_PROMPT.format(wt=wt, base=meta["base"][:9], n=n, shell_note=shell_note,
                                  brief=brief, focus=focus, omit_note=omit_note)
    cmd = build_cmd(agent, model, effort, wt, job, read_only=True)
    if a.dry_run:
        if omit_line:
            print(omit_line)
        print(f"[dry run] review {n} by {agent} {model}: {cmd}\n---- prompt.md ----\n{prompt}")
        return
    job.mkdir()
    _write_warnings(job, warnings)
    (wt / ".delegate").mkdir(exist_ok=True)
    for old in (wt / ".delegate").glob("review-*.patch"):
        old.unlink()  # a stale diff would only confuse the reviewer
    write_patch(wt / ".delegate" / f"review-{n}.patch", review_patch)
    if omit_line:
        print(omit_line)
    verify_log = root / "verify.log"
    if not verify_log.exists():
        previous = sorted(root.glob("round-*/verify.log"), key=lambda p: int(p.parent.name.split("-")[1]))
        verify_log = previous[-1] if previous else verify_log
    verify_tail = ("\n".join(verify_log.read_text(errors="replace").splitlines()[-80:])
                   if verify_log.exists() else "(no verify output recorded)")
    (wt / ".delegate" / f"review-{n}.verify.log").write_text(
        f"$ {meta.get('verify') or '(no verify command stored)'}\n{verify_tail}\n")
    (job / "prompt.md").write_text(prompt)
    (job / "run.sh").write_text(job_script(job, f"review {meta['name']} #{n}", Path(meta["repo"]), wt, cmd,
                                           agent, digest_base=meta["base"]))
    save_meta(job, {"agent": agent, "model": model, "effort": effort, "n": n,
                    "round": meta.get("rounds", 1)})
    print(f"review {n} of {meta['name']} (round {meta.get('rounds', 1)}) by {agent} {model}"
          + (f" @{effort}" if effort else ""))
    print(f"result:   {job / 'last-message.md'}")
    quota.spawn_snapshot("launch", run=meta["name"], session=os.environ.get("CLAUDE_CODE_SESSION_ID"),
                         job=f"review-{n}")
    log_delegation("review", run=meta["name"], agent=agent)
    start(job, a.detach, meta["name"])


def verify(a):
    root = find_run(a.run)
    meta = load_meta(root)
    if not Path(meta["worktree"]).is_dir():
        sys.exit("worktree was removed; cannot run verify (saved output is in verify.log)")
    cmd = a.cmd or meta.get("verify")
    if not cmd:
        sys.exit("no verify command stored for this run; pass --cmd")
    rc, out, _ = run_verify_in_worktree(Path(meta["repo"]), meta["worktree"], cmd)
    if a.cmd is None:
        (root / "verify.log").write_text(out)
        (root / "verify_code").write_text(f"{rc}\n")
    else:
        (root / "verify-adhoc.log").write_text(out)
    print("\n".join(out.rstrip().splitlines()[-30:]))
    print(f"verify: {'pass' if rc == 0 else f'FAIL rc={rc}'}")
    if rc == 0:
        hit = checks.failure_line(out)
        if hit:
            print(f"WARNING: verify exited 0 but printed a failure line: {hit}")
    sys.exit(rc)


BUSY = ("running", "verifying")


def _pid_alive(job):
    try:
        os.kill(int((job / "pid").read_text().strip()), 0)
        return True
    except PermissionError:
        return True
    except (OSError, ValueError):
        return False


def _finalizing(job):
    """run.sh wrote finished_at but is still in _finalize (meta.json, packet).

    The 120 s bound keeps runs from before the finalized_at marker from looking busy
    when their old PID has been reused."""
    if (not (job / "exit_code").exists() or not (job / "finished_at").exists()
            or (job / "finalized_at").exists() or not _pid_alive(job)):
        return False
    try:
        return time.time() - (job / "finished_at").stat().st_mtime < 120
    except OSError:
        return False


def state(job):
    if (job / "exit_code").exists():
        # the agent has exited; until finished_at exists, run.sh is still verifying/finalising
        if not (job / "finished_at").exists() and _pid_alive(job):
            return "verifying"
        rc = (job / "exit_code").read_text().strip()
        return "done" if rc == "0" else f"failed rc={rc}"
    pid_f = job / "pid"
    if not pid_f.exists():
        # working-tree run roots have no build round; not busy / not failed
        if (job / "meta.json").exists():
            try:
                if load_meta(job).get("kind") == "working-tree":
                    return "ready"
            except (OSError, ValueError, KeyError):
                pass
        return "not started"
    try:
        os.kill(int(pid_f.read_text().strip()), 0)
        return "running"
    except (ProcessLookupError, ValueError):
        return "died (no exit code)"
    except PermissionError:
        return "running"


def _elapsed(job):
    minutes = _minutes(job)
    if minutes is None:
        return "?"
    m, s = divmod(round(minutes * 60), 60)
    return f"{m}m{s:02d}s"


def _minutes(job):
    try:
        start_t = datetime.fromisoformat((job / "started_at").read_text().strip())
        end_f = job / "finished_at"
        end = datetime.fromisoformat(end_f.read_text().strip()) if end_f.exists() else datetime.now(start_t.tzinfo)
        return round(max(0, (end - start_t).total_seconds()) / 60, 4)
    except (OSError, ValueError, TypeError):
        return None


def _read(job, name):
    p = job / name
    return p.read_text(errors="replace").strip() if p.exists() else None


def _code(job, name):
    try:
        return int(_read(job, name))
    except (ValueError, TypeError):
        return None


def _events(job):
    log = job / "log"
    if log.exists():
        with log.open(errors="replace") as stream:
            for line in stream:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if isinstance(ev, dict) and isinstance(ev.get("type"), str):
                    yield ev


def usage(job, agent):
    """Normalise one job's usage; old Codex totals remain explicitly cumulative."""
    if agent == "cursor":
        reported = [e["usage"] for e in _events(job)
                    if e["type"] == "result" and isinstance(e.get("usage"), dict)]
        if not reported:
            return None
        u = reported[-1]
        return {"input": u.get("inputTokens", 0), "cached_input": u.get("cacheReadTokens", 0),
                "cache_write": u.get("cacheWriteTokens"), "output": u.get("outputTokens", 0),
                "reasoning_output": None}
    # Codex reports running thread totals (a resumed round's numbers include the earlier rounds,
    # field data 2026-10-10), so the last event is this job's thread total; see _round_record.
    reported = [e["usage"] for e in _events(job)
                if e["type"] == "turn.completed" and isinstance(e.get("usage"), dict)]
    if reported:
        u = reported[-1]
        return {"input": u.get("input_tokens", 0) - u.get("cached_input_tokens", 0),
                "cached_input": u.get("cached_input_tokens", 0),
                "cache_write": u.get("cache_write_input_tokens"),
                "output": u.get("output_tokens", 0),
                "reasoning_output": u.get("reasoning_output_tokens")}
    text = _read(job, "log") or ""
    totals = re.findall(r"tokens used\s*:?\s*([\d,]+)", text, re.I)
    return {"total_reported": int(totals[-1].replace(",", ""))} if totals else None


def _verdict(job):
    # Accept both the new Verdict: line and the original ## Verdict + next line.
    m = re.search(r"verdict\W*\s*(APPROVE|CHANGES REQUESTED)", _read(job, "last-message.md") or "", re.I)
    return m.group(1).upper() if m else None


def _round_record(job, meta, n):
    record = {"n": n, "model": meta.get("model"), "effort": meta.get("effort"),
              "started": _read(job, "started_at"), "minutes": _minutes(job),
              "exit": _code(job, "exit_code"), "verify": _code(job, "verify_code"),
              "usage": usage(job, meta["agent"])}
    if meta["agent"] == "codex" and meta.get("resumed"):
        # A resumed Codex round reports thread totals: keep them, and store this round's delta as usage.
        record["resumed"] = True
        prev = next((r for r in meta.get("history", []) if r.get("n", r.get("round")) == n - 1), {})
        base, u = prev.get("usage_thread") or prev.get("usage"), record["usage"]
        if u and base and "input" in u and "input" in base:
            record["usage_thread"] = u
            record["usage"] = {k: max(0, v - (base.get(k) or 0)) if isinstance(v, int) else v
                               for k, v in u.items()}
    return record


def _context(job):
    meta = load_meta(job)
    if job.name.startswith("review-") and (job.parent / "meta.json").exists():
        return job.parent, load_meta(job.parent), meta
    return job, meta, None


def finalize(job):
    """Record this finished job once, then print the notification's final packet."""
    root, meta, review_meta = _context(job)
    if _code(job, "exit_code") is None or not (job / "finished_at").exists():
        sys.exit(f"job is not finished: {job}")
    if review_meta is not None:
        review_meta.update(minutes=_minutes(job), usage=usage(job, review_meta["agent"]),
                           verdict=_verdict(job), started=_read(job, "started_at"))
        save_meta(job, review_meta)
        job_id = job.name  # review-N
    else:
        n = meta.get("rounds", 1)
        history = [r for r in meta.get("history", []) if r.get("n", r.get("round")) != n]
        history.append(_round_record(job, meta, n))
        meta["history"] = sorted(history, key=lambda r: r.get("n", r.get("round", 0)))
        save_meta(root, meta)
        job_id = f"round-{n}"
        if Path(meta["worktree"]).is_dir():
            # the packet lists changes.patch; written only by diff/review/apply, it lagged a followup
            try:
                make_patch(root, meta)
            except (Exception, SystemExit) as e:  # finalized_at below must still be written
                (root / "changes.patch").unlink(missing_ok=True)
                print(f"WARNING: changes.patch not written: {e}", file=sys.stderr)
    try:
        quota.spawn_snapshot("job-end", run=meta["name"], job=job_id)
    except Exception:
        pass
    lines = packet(job)  # runs git in the worktree, so it comes before the marker
    # last write: wait and the command guards key off this marker; a broken stdout can't skip it
    (job / "finalized_at").write_text(
        datetime.now().astimezone().isoformat(timespec="seconds") + "\n")
    print("\n".join(lines))


def _numstat(text):
    """Parse git's NUL format, including the three-field rename representation."""
    fields = iter(text.split("\0"))
    rows = []
    for field in fields:
        if not field:
            continue
        parts = field.split("\t", 2)
        if len(parts) != 3:
            continue
        added, deleted, path = parts
        if not path:
            old, path = next(fields, ""), next(fields, "")
            path = f"{old} => {path}"
        rows.append({"path": path, "insertions": int(added) if added.isdigit() else 0,
                     "deletions": int(deleted) if deleted.isdigit() else 0,
                     "binary": added == "-" or deleted == "-"})
    return rows


def change_stats(root, meta):
    """Read-only numstat against base, adding untracked files without staging."""
    wt = Path(meta["worktree"])
    if wt.is_dir():
        rows = _numstat(git("diff", "--numstat", "-z", meta["base"], "--", ".",
                            ":(exclude).delegate", cwd=wt, check=False))
        known = {r["path"] for r in rows}
        untracked = git("ls-files", "--others", "--exclude-standard", "-z", "--", ".",
                        ":(exclude).delegate", cwd=wt, check=False)
        for path in filter(None, untracked.split("\0")):
            if path in known:
                continue
            # --no-index handles binary and missing-newline files exactly as git does.
            stat = git("diff", "--no-index", "--numstat", "-z", "--", "/dev/null", path,
                       cwd=wt, check=False)
            extra = _numstat(stat)
            for r in extra:
                r["path"] = path
            rows.extend(extra)
        return rows
    patch = root / "changes.patch"
    if patch.exists():
        cwd = meta["repo"] if Path(meta["repo"]).is_dir() else root
        return _numstat(git("apply", "--numstat", "-z", str(patch), cwd=cwd, check=False))
    return []


def _one_line(text):
    return " ".join(str(text).split())


def _concerns(job):
    text = _read(job, "last-message.md") or ""
    digest = re.search(r"^## Digest\s*$([\s\S]*)", text, re.M)
    if digest:
        values = {}
        key = None
        for line in digest.group(1).splitlines():
            match = re.match(r"^(files|verify|conflicts|risks):\s*(.*)", line)
            if match:
                key = match[1]
                values.setdefault(key, []).append(match[2])
            elif line.strip() and key:
                values[key].append(line.strip())
        if any(k in values for k in ("conflicts", "risks")):
            lines = []
            for k in ("conflicts", "risks"):
                value = _one_line("; ".join(values.get(k, ["see last-message.md"])))
                lines.append(f"{k}: {value}")
            return lines
    section = re.search(r"^#{1,6}[^\n]*(?:open.?questions|conflicts|open.?points)[^\n]*\n([\s\S]*?)(?=^#{1,6} |\Z)",
                        text, re.M | re.I)
    if section:
        lines = [l.strip() for l in section.group(1).splitlines() if l.strip()][:3]
        if lines:
            return ["conflicts/risks: " + _one_line(l) for l in lines]
    return ["conflicts/risks: see last-message.md"]


def _usage_line(u):
    if u is None:
        return "usage: unavailable"
    if "total_reported" in u:
        return f"usage: {u['total_reported']:,} total reported (legacy Codex; cumulative session total)"
    return (f"usage: input {u['input']:,}, cached {u['cached_input']:,}, "
            f"cache write {u['cache_write'] if u['cache_write'] is not None else '?'}, "
            f"output {u['output']:,}, reasoning {u['reasoning_output'] if u['reasoning_output'] is not None else '?'}")


def _job_usage(job, meta, review_meta):
    if review_meta is None:
        n = meta.get("rounds", 1)
        for r in meta.get("history", []):
            if r.get("n", r.get("round")) == n and r.get("usage"):
                return r["usage"]
    return usage(job, (review_meta or meta)["agent"])


PACKET_MAX_LINES = 20


def _fit_file_rows(row_lines, budget, cap=8):
    """As many per-file rows as fit in budget (≤ cap); `... N more` counts in the budget."""
    if budget <= 0 or not row_lines:
        return []
    if len(row_lines) <= min(cap, budget):
        return row_lines
    n_show = min(cap, budget - 1)
    if n_show <= 0:
        return [f"  ... {len(row_lines)} more file(s)"]
    return row_lines[:n_show] + [f"  ... {len(row_lines) - n_show} more file(s)"]


def packet(job):
    """Rebuild a bounded notification packet entirely from existing artifacts.

    Never returns more than PACKET_MAX_LINES lines: fixed lines (header, usage,
    summary, review, warnings, failure line, concerns, artifacts) are reserved
    first; per-file rows fill whatever budget remains.
    """
    root, meta, review_meta = _context(job)
    jm = review_meta or meta
    if review_meta is not None:
        label = f"review {job.name.split('-')[-1]}"
    elif meta.get("kind") == "working-tree":
        label = "working-tree"
    else:
        label = f"round {meta.get('rounds', 1)}"
    rc = _code(job, "exit_code")
    verify_state = _verify_state(job) or "verify none"
    model_s = jm.get("model") if jm.get("model") is not None else "-"
    prefix = [f"== {_one_line(meta['name'])} | {_one_line(jm['agent'])} {_one_line(model_s)} | {label} | "
              f"{state(job)} | {_elapsed(job)} | exit {rc if rc is not None else '?'} | {verify_state}",
              _usage_line(_job_usage(job, meta, review_meta))]
    file_row_lines = []
    mid = []
    if review_meta is not None:
        text = _read(job, "last-message.md") or ""
        counts = {s: len(re.findall(r"^\s*[-*]\s*\[" + s + r"\]", text, re.M | re.I))
                  for s in ("high", "medium", "low")}
        prefix.append(f"verdict: {_verdict(job) or 'unclear'} | findings: "
                      + ", ".join(f"{s} {count}" for s, count in counts.items()))
        pre, post = _read(job, "pre_digest"), _read(job, "post_digest")
        if pre is not None and post is not None and pre != post:
            prefix.append("WARNING: reviewer changed the worktree")
    else:
        rows = change_stats(root, meta)
        totals = (sum(r["insertions"] for r in rows), sum(r["deletions"] for r in rows))
        suffix_note = " (worktree removed; saved patch)" if not Path(meta["worktree"]).is_dir() else ""
        if not Path(meta["worktree"]).is_dir() and not (root / "changes.patch").exists():
            suffix_note = " (worktree removed; no saved patch)"
        prefix.append(f"files: {len(rows)} | +{totals[0]}/-{totals[1]} total{suffix_note}"
                      + (" | applied" if (root / "applied").exists() else ""))
        for r in sorted(rows, key=lambda r: (-(r["insertions"] + r["deletions"]), r["path"])):
            file_row_lines.append(
                f"  {_one_line(r['path'])}: " + ("binary" if r["binary"] else
                f"+{r['insertions']}/-{r['deletions']}"))
        reviews = sorted(root.glob("review-*/meta.json"), key=lambda p: int(p.parent.name.split("-")[1]))
        if reviews:
            mid.append("latest " + _review_summary(reviews[-1].parent))
    tail = []
    warn_file = job / "warnings"
    if warn_file.exists():
        wlines = [l for l in warn_file.read_text(errors="replace").splitlines() if l.strip()]
        for i, line in enumerate(wlines):
            if i >= 3:
                tail.append(f"WARNING: {len(wlines) - 3} more in {job}/warnings")
                break
            rest = line[len("warning: "):] if line.startswith("warning: ") else line
            tail.append("WARNING: " + rest)
    if review_meta is None and _code(job, "verify_code") == 0 and (job / "verify.log").exists():
        hit = checks.failure_line(_verify_log_tail(job / "verify.log"))
        if hit:
            tail.append(f"WARNING: verify exited 0 but printed a failure line: {hit}")
    tail.extend(_concerns(job))
    paths = f"artifacts: {job / 'last-message.md'} | {job / 'log'} | {job / 'verify.log'}"
    if review_meta is not None:
        paths += f" | {Path(meta['worktree']) / '.delegate' / (job.name + '.verify.log')}"
    elif (root / "changes.patch").exists():
        paths += f" | {root / 'changes.patch'}"
    tail.append(paths)
    budget = PACKET_MAX_LINES - len(prefix) - len(mid) - len(tail)
    return prefix + _fit_file_rows(file_row_lines, budget) + mid + tail


def _verify_state(job):
    f = job / "verify_code"
    if not f.exists():
        return ""
    rc = f.read_text().strip()
    return "verify pass" if rc == "0" else f"verify FAIL rc={rc}"


def _review_summary(job):
    meta = json.loads((job / "meta.json").read_text()) if (job / "meta.json").exists() else {}
    parts = [f"{job.name}: {meta.get('agent', '?')} {meta.get('model', '?')} [{state(job)} {_elapsed(job)}]"]
    last = job / "last-message.md"
    if last.exists():
        parts.append(_verdict(job) or "verdict unclear")
    pre, post = job / "pre_digest", job / "post_digest"
    if pre.exists() and post.exists() and pre.read_text() != post.read_text():
        parts.append("WARNING: reviewer changed the worktree")
    return ", ".join(parts)


def _render_cursor_line(line):
    try:
        ev = json.loads(line)
    except ValueError:
        return line
    t = ev.get("type")
    if t == "assistant":
        text = "".join(c.get("text", "") for c in (ev.get("message") or {}).get("content", []))
        return "assistant: " + text.strip().replace("\n", " ")[:200]
    if t == "tool_call":
        call = ev.get("tool_call") or {}
        kind = next(iter(call), "?")
        args = (call.get(kind) or {}).get("args") or {}
        brief = args.get("path") or args.get("command") or args.get("pattern") or ""
        return f"tool {ev.get('subtype', '')}: {kind} {' '.join(str(brief).split())[:120]}"
    if t == "result":
        return f"result: {'error' if ev.get('is_error') else 'ok'} in {ev.get('duration_ms', '?')}ms"
    return None


def _render_codex_event(ev):
    item = ev.get("item") or {}
    kind = item.get("type") or ev["type"]
    if kind in ("agent_message", "reasoning"):
        return kind + ": " + _one_line(item.get("text", ""))[:200]
    if kind == "command_execution":
        return f"command: {_one_line(item.get('command', ''))[:160]} | exit {item.get('exit_code', '?')}"
    if kind == "file_change":
        return "files: " + "; ".join(_one_line(c.get("path", "?")) for c in item.get("changes", []))[:200]
    return kind


def log_tail(job, agent, n):
    log = job / "log"
    if not log.exists() or n <= 0:
        return []
    lines = log.read_text(errors="replace").splitlines()
    if agent == "cursor":
        lines = [r for r in (_render_cursor_line(l) for l in lines) if r]
    elif agent == "codex":
        events = list(_events(job))
        if events:
            lines = [_render_codex_event(e) for e in events]
    return lines[-n:]


def _run_jobs(root):
    """A run's jobs: the root plus every review-* directory that has meta.json.

    Working-tree runs have no build round at the root; wait only on their reviews.
    """
    jobs = []
    try:
        kind = load_meta(root).get("kind") if (root / "meta.json").exists() else None
    except (OSError, ValueError):
        kind = None
    if kind != "working-tree":
        jobs.append(root)
    for meta_path in sorted(root.glob("review-*/meta.json"),
                            key=lambda p: int(p.parent.name.split("-")[1])):
        jobs.append(meta_path.parent)
    return jobs


def _job_busy(job):
    """True while the job is running/verifying, or not-started with a fresh meta.json."""
    st = state(job)
    if st in BUSY:
        return True
    if st == "not started":
        try:
            return (time.time() - (job / "meta.json").stat().st_mtime) < 60
        except OSError:
            return False
    return _finalizing(job)


def _started_sort_key(job):
    try:
        dt = datetime.fromisoformat((job / "started_at").read_text().strip())
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return dt.timestamp()
    except (OSError, ValueError):
        try:
            return job.stat().st_mtime
        except OSError:
            return 0.0


def wait(a):
    """Block until every job of each RUN is idle; print bounded packets, no poll noise."""
    roots = [find_run(r) for r in a.runs]
    timeout = a.timeout
    interval = a.interval
    deadline = time.monotonic() + timeout
    ever_busy = []
    seen = set()

    while True:
        busy_now = []
        for root in roots:
            for job in _run_jobs(root):
                if _job_busy(job):
                    busy_now.append(job)
                    key = str(job.resolve())
                    if key not in seen:
                        seen.add(key)
                        ever_busy.append(job)
        if not busy_now:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            for job in busy_now:
                print(f"{packet(job)[0]} (still busy after {timeout:g} s)")
            sys.exit(3)
        time.sleep(min(interval, remaining))

    # Working-tree runs have several peer reviews; printing only the busy or the
    # latest ones would hide a review that failed before wait attached. Normal
    # runs print the jobs that were busy, else the single latest job.
    busy_keys = {str(job.resolve()) for job in ever_busy}
    to_print = []
    for root in roots:
        jobs = _run_jobs(root)
        try:
            kind = load_meta(root).get("kind")
        except (OSError, ValueError):
            kind = None
        if kind == "working-tree":
            to_print.extend(jobs)
        elif ever_busy:
            to_print.extend(job for job in jobs if str(job.resolve()) in busy_keys)
        elif jobs:
            to_print.append(max(jobs, key=_started_sort_key))

    failed = False
    for job in to_print:
        print("\n".join(packet(job)))
        if state(job) != "done":
            failed = True
    sys.exit(1 if failed else 0)


def status(a):
    roots = [find_run(r) for r in a.runs] if a.runs else sorted(
        (p.parent for p in RUNS_ROOT.glob("*/*/meta.json")), key=lambda p: p.stat().st_mtime)
    if not roots:
        print(f"no runs under {RUNS_ROOT}")
        return
    for root in roots:
        if a.runs and not a.full:
            print("\n".join(packet(root)))
            continue
        if root.name.startswith("review-"):  # review meta has no worktree; show packet, log tail, message
            print("\n".join(packet(root)))
            for l in log_tail(root, load_meta(root)["agent"], a.log):
                print("   " + l[:240])
            if (root / "last-message.md").exists():
                print("   -- last message --\n" + (root / "last-message.md").read_text().rstrip())
            continue
        meta = load_meta(root)
        wt = Path(meta["worktree"])
        changes = git("status", "--porcelain", cwd=wt, check=False).splitlines() if wt.exists() else []
        bits = [f"{len(changes)} changed path(s)" if wt.exists() else "worktree removed"]
        if meta.get("rounds", 1) > 1:
            bits.append(f"round {meta['rounds']}")
        if _verify_state(root):
            bits.append(_verify_state(root))
        if (_code(root, "verify_code") == 0 and (root / "verify.log").exists()
                and checks.failure_line(_verify_log_tail(root / "verify.log"))):
            bits.append("WARNING: verify printed a failure line")
        warn_file = root / "warnings"
        if warn_file.exists():
            nwarn = sum(1 for l in warn_file.read_text(errors="replace").splitlines() if l.strip())
            if nwarn:
                bits.append(f"{nwarn} warning(s)")
        if (root / "applied").exists():
            bits.append("applied")
            if wt.exists() and time.time() - (root / "applied").stat().st_mtime > 86400:
                # Field incident: a day-old applied worktree with edits looked like unmerged work.
                bits.append(f"STALE worktree: applied over a day ago; `delegate cleanup {meta['name']}`")
        model_s = meta["model"] if meta.get("model") is not None else "-"
        print(f"== {meta['name']}  {meta['agent']} {model_s}  [{state(root)} {_elapsed(root)}]  "
              + ", ".join(bits))
        if not a.runs:
            continue
        print(f"   {root}")
        reviews = sorted(root.glob("review-*"), key=lambda p: int(p.name.split("-")[1]))
        for rv in reviews:
            print("   " + _review_summary(rv))
        if a.runs:
            for c in changes[:40]:
                print("   " + c)
            tail = log_tail(root, meta["agent"], a.log)
            if tail:
                print("   -- log tail --")
                for l in tail:
                    print("   " + l[:240])
            if (root / "verify_code").exists() and (root / "verify_code").read_text().strip() != "0":
                print("   -- verify.log (tail) --")
                for l in (root / "verify.log").read_text(errors="replace").splitlines()[-15:]:
                    print("   " + l[:240])
            last = root / "last-message.md"
            if last.exists():
                print("   -- last message --")
                print(last.read_text().rstrip())


def cursor_last(job):
    """Extract the final answer from cursor-agent stream-json into last-message.md.

    The stream's `result` concatenates every assistant message, including
    narration between tool calls; keep only what came after the last tool call.
    """
    result, tail = None, []
    for line in (job / "log").read_text(errors="replace").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "result":
            result = ev.get("result")
        elif ev.get("type") == "tool_call":
            tail = []
        elif ev.get("type") == "assistant":
            tail.append("".join(c.get("text", "") for c in (ev.get("message") or {}).get("content", [])))
    text = "".join(tail).strip() or result or "(no final message found in log)"
    (job / "last-message.md").write_text(text.rstrip() + "\n")


def _link_pathspecs(meta):
    """Literal exclude pathspecs so link paths with glob chars cannot swallow other files."""
    return [f":(exclude,literal){l}" for l in meta.get("links", [])]


def make_patch(root, meta):
    wt = Path(meta["worktree"])
    pathspecs = _link_pathspecs(meta)
    # intent-to-add: new files show up in the diff; nothing is committed. Literal excludes keep
    # --link paths out even when the info/exclude line was removed (and avoid glob false matches).
    if pathspecs:
        r = subprocess.run(["git", "add", "-A", "-N", "--", ".", *pathspecs], cwd=wt,
                           capture_output=True, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
        if r.returncode != 0:
            err = r.stderr.decode("utf-8", "replace")
            # An ignored link under info/exclude makes git add exit 1 even with :(exclude,literal);
            # other paths are still staged.
            if "ignored by one of your .gitignore files" not in err:
                sys.exit(f"git add -A -N failed: {err.strip()}")
    else:
        git("add", "-A", "-N", cwd=wt)
    head = git("rev-parse", "HEAD", cwd=wt)
    # working-tree reviews check out meta["snapshot"]; base is only the diff base
    if head != meta["base"] and head != meta.get("snapshot"):
        print(f"WARNING: worktree HEAD {head[:9]} != base {meta['base'][:9]}: the delegate made commits "
              f"(contract breach). The patch below covers them too.", file=sys.stderr)
    # Bytes, not text=True: universal newlines would turn \r\n into \n and break CRLF files.
    diff_cmd = ["git", "diff", "--binary", meta["base"]]
    if pathspecs:
        diff_cmd += ["--", ".", *pathspecs]
    patch = subprocess.run(diff_cmd, cwd=wt, capture_output=True, check=True).stdout.decode(
        "utf-8", "surrogateescape")
    write_patch(root / "changes.patch", patch)
    stat_args = (meta["base"], "--", ".", *pathspecs) if pathspecs else (meta["base"],)
    return git("diff", "--stat", *stat_args, cwd=wt), patch


def write_patch(path, patch):
    path.write_bytes(patch.encode("utf-8", "surrogateescape"))


def read_patch(path):
    return path.read_bytes().decode("utf-8", "surrogateescape")


def diff(a):
    root = find_run(a.run)
    meta = load_meta(root)
    if Path(meta["worktree"]).is_dir():
        stat, patch = make_patch(root, meta)
    elif (root / "changes.patch").exists():
        patch = read_patch(root / "changes.patch")
        cwd = meta["repo"] if Path(meta["repo"]).is_dir() else root
        stat = git("apply", "--stat", str(root / "changes.patch"), cwd=cwd)
    else:
        sys.exit("worktree was removed and no changes.patch was saved")
    print(stat or "(no changes)")
    print(f"\npatch: {root / 'changes.patch'}  ({len(patch.splitlines())} lines)")
    if a.full:
        print(patch)


def patch_paths(patch_path, repo):
    """Pre- and post-image paths a patch touches (rename/copy sources included).

    Runs `git apply --numstat` with cwd=repo: without that, git drops paths outside the
    caller's cwd, so a subdirectory apply would omit root-level files from the temp index.
    """
    r = subprocess.run(["git", "apply", "--numstat", "-z", str(patch_path)],
                       cwd=repo, capture_output=True)
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", "replace").strip()
        sys.exit(f"git apply --numstat failed: {err}")
    paths = set()
    data, i = r.stdout, 0
    while i < len(data):
        z = data.find(b"\0", i)
        if z < 0:
            break
        record = data[i:z].decode("utf-8", "surrogateescape")
        i = z + 1
        parts = record.split("\t", 2)
        if len(parts) == 3 and parts[2]:
            paths.add(parts[2])
    # numstat lists only the destination of a rename; pick up sources from the patch body
    text = Path(patch_path).read_bytes().decode("utf-8", "surrogateescape")
    for line in text.splitlines():
        for prefix in ("rename from ", "rename to ", "copy from ", "copy to "):
            if line.startswith(prefix):
                paths.add(_unquote_git_path(line[len(prefix):]))
                break
    return sorted(paths)


def _paths_in_index_or_worktree(repo, paths):
    """Filter to paths that exist on disk or are already tracked (so `git add` won't fail).

    Uses GIT_LITERAL_PATHSPECS so a patch path with `[`, `*` or `?` cannot match other files.
    """
    if not paths:
        return []
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_LITERAL_PATHSPECS": "1"}
    r = subprocess.run(["git", "ls-files", "-z", "--", *paths], cwd=repo,
                       capture_output=True, env=env)
    tracked = set(filter(None, r.stdout.decode("utf-8", "surrogateescape").split("\0")))
    kept = []
    for path in paths:
        if path in tracked or (Path(repo) / path).exists() or (Path(repo) / path).is_symlink():
            kept.append(path)
    return kept


def _unmerged_paths(repo, env):
    """Unique paths from `git ls-files -u -z` under the given index env."""
    raw = subprocess.run(["git", "ls-files", "-u", "-z"], cwd=repo, capture_output=True, env=env).stdout
    paths, seen, i = [], set(), 0
    while i < len(raw):
        z = raw.find(b"\0", i)
        if z < 0:
            break
        rec = raw[i:z].decode("utf-8", "surrogateescape")
        i = z + 1
        tab = rec.find("\t")
        if tab < 0:
            continue
        path = rec[tab + 1:]
        if path and path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


def _conflict_file_note(repo, path):
    """What git left in the working file for a conflicted path."""
    p = Path(repo) / path
    try:
        data = p.read_bytes()
    except OSError:
        return "working file missing after conflict"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return "no conflict markers (binary); working file still holds the pre-apply (ours) content"
    if "<<<<<<<" in text:
        return "conflict markers are in the working file"
    return "no conflict markers; working file still holds the pre-apply (ours) content"


def _apply_three_way(repo, patch_path, check=False):
    """`git apply --3way` via a temp index so unstaged main-tree edits don't fail the index match.

    The real index is never written. On conflicts, markers (if any) are left in the working files
    and the caller must not mark the run applied. LC_ALL=C keeps conflict detection on English
    git messages (`--check --3way` exits 0 on conflicts and only the text distinguishes them).
    """
    paths = patch_paths(patch_path, repo)
    tmp_index = copy_temp_index(repo)
    try:
        env = {**os.environ, "GIT_INDEX_FILE": str(tmp_index), "GIT_OPTIONAL_LOCKS": "0",
               "GIT_LITERAL_PATHSPECS": "1"}
        to_add = _paths_in_index_or_worktree(repo, paths)
        if to_add:
            r = subprocess.run(["git", "add", "--", *to_add], cwd=repo, capture_output=True, env=env)
            if r.returncode != 0:
                sys.exit(f"git add (temp index) failed: {r.stderr.decode('utf-8', 'replace').strip()}")
        # LC_ALL=C: conflict phrases are English even when the user's locale translates git.
        apply_env = {**env, "LC_ALL": "C"}
        args = ["git", "apply"] + (["--check"] if check else []) + ["--3way", str(patch_path)]
        r = subprocess.run(args, cwd=repo, capture_output=True, text=True, env=apply_env)
        sys.stdout.write(r.stdout)
        sys.stderr.write(r.stderr)
        combined = (r.stdout or "") + (r.stderr or "")
        conflicts = [] if check else _unmerged_paths(repo, env)
        would_conflict = "with conflicts" in combined
        if check:
            if r.returncode or would_conflict:
                sys.exit("git apply --check --3way failed; merge by hand from the patch, or fix conflicts first")
            return True
        if conflicts or r.returncode or would_conflict:
            shown = conflicts or sorted({
                m.group(1) for m in re.finditer(r"Applied patch to '([^']+)' with conflicts", combined)})
            if shown:
                print("conflicted paths:")
                for path in shown:
                    print(f"  {path}: {_conflict_file_note(repo, path)}")
            else:
                print("3-way apply failed without conflicted paths; see git's message above")
            print("the patch is not marked applied; resolve the working files, then apply by hand or retry")
            sys.exit(f"git apply --3way failed (rc={r.returncode or 1})")
        return True
    finally:
        tmp_index.unlink(missing_ok=True)


def apply(a):
    root = find_run(a.run)
    if state(root) in BUSY or _finalizing(root):
        sys.exit("delegate is still running")
    meta = load_meta(root)  # after the guard, or finalize's history could be saved back stale
    if meta.get("kind") == "working-tree":
        sys.exit("a working-tree run has nothing to apply")
    _, patch = make_patch(root, meta)
    if not patch.strip():
        sys.exit("nothing to apply")
    patch_file = root / "changes.patch"
    if a.three_way:
        _apply_three_way(meta["repo"], patch_file, check=a.check)
    else:
        args = ["git", "apply"] + (["--check"] if a.check else [])
        r = subprocess.run(args + [str(patch_file)], cwd=meta["repo"], capture_output=True, text=True)
        sys.stdout.write(r.stdout)
        sys.stderr.write(r.stderr)
        if r.returncode:
            sys.exit(f"git apply failed (rc={r.returncode}); try --3way, or merge by hand from {patch_file}")
    if not a.check:
        (root / "applied").write_text(_digest(patch) + "\n")
    print(("patch applies cleanly to " if a.check else "applied to working tree of ") + meta["repo"])
    if not a.check:
        log_run(root, meta, "applied")
        print(f"logged to {RUNS_ROOT / 'runs.jsonl'}. Once committed: `delegate cleanup {meta['name']}`. "
              "If this completes the work item: write a handoff and start a fresh orchestrator session.")


def log_run(root, meta, outcome):
    """Append the cost interface's record, reading legacy accounting without backfilling."""
    history = {r.get("n", r.get("round")): r for r in meta.get("history", [])}
    rounds = []
    for n in range(1, meta.get("rounds", 1) + 1):
        job = root if n == meta.get("rounds", 1) else root / f"round-{n}"
        old = history.get(n, {})
        if _code(job, "exit_code") is None and old.get("exit") is None:
            continue
        info = {**meta, "resumed": old.get("resumed", meta.get("resumed") if n == meta.get("rounds", 1) else False),
                **{k: old[k] for k in ("model", "effort") if k in old}}
        record = _round_record(job, info, n)
        for key in record:
            if key in old:
                record[key] = old[key]
        rounds.append(record)
    reviews = []
    for path in sorted(root.glob("review-*/meta.json"), key=lambda p: int(p.parent.name.split("-")[1])):
        job, rv = path.parent, load_meta(path.parent)
        reviews.append({"n": int(job.name.split("-")[1]), "agent": rv["agent"], "model": rv["model"],
                        "effort": rv.get("effort"),
                        "started": rv.get("started", _read(job, "started_at")),
                        "minutes": rv.get("minutes", _minutes(job)),
                        "verdict": rv.get("verdict", _verdict(job)),
                        "usage": rv.get("usage", usage(job, rv["agent"]))})
    rows = change_stats(root, meta)
    mode = ("read-only" if meta.get("read_only") else "workspace-write") if meta["agent"] == "codex" else (
        "ask" if meta.get("read_only") else "unsandboxed" if meta.get("unsandboxed") else "sandbox")
    record = {"v": 1, "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "session": meta.get("session"),
              "repo": Path(meta["repo"]).name, "repo_path": meta["repo"], "run": meta["name"],
              "agent": meta["agent"], "model": meta["model"], "effort": meta.get("effort"),
              "mode": mode, "links": meta.get("links", []), "base": meta["base"], "outcome": outcome,
              "files": len(rows), "insertions": sum(r["insertions"] for r in rows),
              "deletions": sum(r["deletions"] for r in rows), "rounds": rounds, "reviews": reviews}
    if meta.get("snapshot_of"):
        record["snapshot_of"] = meta["snapshot_of"]
    RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    with (RUNS_ROOT / "runs.jsonl").open("a") as stream:
        stream.write(json.dumps(record) + "\n")
    (root / "logged").write_text(outcome + "\n")


def _digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _iter_status_paths(wt):
    """Exact paths from `git status --porcelain=v1 -z --untracked-files=all` (destination for renames)."""
    r = subprocess.run(["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"], cwd=wt,
                       capture_output=True, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    data, i = r.stdout, 0
    while i + 3 <= len(data):
        code = data[i:i + 2]
        if data[i + 2:i + 3] != b" ":
            break
        i += 3
        z = data.find(b"\0", i)
        if z < 0:
            path_b, i = data[i:], len(data)
        else:
            path_b, i = data[i:z], z + 1
        path = path_b.decode("utf-8", "surrogateescape")
        # Rename/copy: an extra NUL-terminated source path follows.
        if code[:1] in b"RC" or code[1:2] in b"RC":
            z2 = data.find(b"\0", i)
            i = len(data) if z2 < 0 else z2 + 1
        yield path


def _dirty_without_links(wt, meta):
    """Status paths excluding recorded --link entries that are still real symlinks."""
    links = set(meta.get("links", []))
    kept = []
    for path in _iter_status_paths(wt):
        if path in links and (wt / path).is_symlink():
            continue
        kept.append(path)
    return "\n".join(kept)


def cleanup(a):
    root = find_run(a.run)
    if state(root) in BUSY or _finalizing(root) or any(
            state(rv) in BUSY or _finalizing(rv) for rv in root.glob("review-*")):
        sys.exit("delegate or a review is still running")
    meta = load_meta(root)  # after the guard, or finalize's history could be saved back stale
    wt = Path(meta["worktree"])
    working_tree = meta.get("kind") == "working-tree"
    if wt.exists() and not working_tree:
        dirty = _dirty_without_links(wt, meta)
        applied = root / "applied"
        if dirty and not a.force:
            if not (applied.exists() and applied.read_text().strip() == _digest(make_patch(root, meta)[1])):
                sys.exit(f"worktree has changes that were not applied (or changed since apply); "
                         f"apply them or pass --force:\n{dirty}")
    if not (root / "logged").exists() and not (root / "applied").exists():  # apply already logged it
        # Read-only runs, reviews and runs merged by hand never pass through apply.
        if working_tree:
            outcome = "reviewed"
        else:
            changed = wt.exists() and bool(_dirty_without_links(wt, meta))
            outcome = ("discarded" if changed else "read-only" if meta.get("read_only") else "no-changes")
        log_run(root, meta, outcome)
    if wt.exists():
        for l in meta.get("links", []):
            if (wt / l).is_symlink():
                (wt / l).unlink()
        # safe by now: changes were applied, absent, explicitly discarded, or a working-tree snapshot
        git("worktree", "remove", "--force", str(wt), cwd=meta["repo"])
    # Transfer or remove owned exclude lines, then clear meta before any --purge.
    _release_excluded(Path(meta["repo"]), root, meta)
    if meta.get("branch") and not a.keep_branch:
        git("branch", "-D", meta["branch"], cwd=meta["repo"], check=False)
    if a.purge:
        shutil.rmtree(root)
        print(f"removed {root}")
    else:
        print(f"worktree removed; run record kept in {root}")

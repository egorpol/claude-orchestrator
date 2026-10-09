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
from datetime import datetime
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
RUNS_ROOT = Path(os.environ.get("CLAUDE_DELEGATE_ROOT",
                                Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
                                / "claude-delegate" / "runs"))
DEFAULTS = {"codex": ("gpt-6.1-sol", "xhigh"), "cursor": ("auto", None)}
ROUND_FILES = ("brief.md", "prompt.md", "run.sh", "log", "last-message.md", "exit_code", "pid",
               "started_at", "finished_at", "verify.log", "verify_code")

# Claude Code here runs inside Cursor's AppImage; these leak into children and
# can break other binaries, or make a delegate think it is talking to an IDE.
UNSET_EXACT = {"LD_LIBRARY_PATH", "APPDIR", "APPIMAGE", "ARGV0", "OWD", "PERLLIB",
               "GSETTINGS_SCHEMA_DIR", "QT_PLUGIN_PATH", "CHROME_DESKTOP",
               "CLAUDECODE", "CLAUDE_PID", "AI_AGENT"}
UNSET_PREFIX = ("CLAUDE_CODE_", "VSCODE_", "ELECTRON_", "CURSOR_")
KEEP = {"CURSOR_API_KEY", "CURSOR_API_ENDPOINT"}

CONTRACT = """

---
## Delegation contract (appended by the launcher; it overrides anything above that conflicts)
- You are working in a git worktree at {wt} on branch `{branch}`, created from {base} for this task. {scope}
- Do NOT run `git commit`, `git push`, `git merge`, `git rebase`, `git stash`, `git reset`, `git checkout <branch>` or create branches or tags. Leave every change uncommitted; the orchestrator reviews the diff and merges it.
{links}- If a constraint in the brief would make you drop existing behaviour, data or a fallback, or real data contradicts an assumption in the brief, do not comply silently: keep the existing behaviour where you can and flag the conflict in your final message.
- Verify against real data or tests where the brief says how (dry runs, test commands) and report the actual output, not what you expect it to be.
- End with a final message containing: (1) what you changed, file by file; (2) the commands you ran to verify and their real results; (3) open questions, conflicts, and anything you skipped or were unsure about.
"""

FOLLOWUP_NOTE = """

---
Follow-up round {round}. The delegation contract from the start of this session still applies: same worktree, no commits, flag conflicts, same final-message structure. The worktree already contains your earlier changes; build on them.
"""

REVIEW_PROMPT = """# Review request (read-only)

Another coding agent made changes in this git worktree ({wt}) relative to commit {base}. Review them. This is a read-only task: do not modify, create or delete any files.

The full diff is in `.delegate/review-{n}.patch` (path relative to the worktree root; new files appear as additions). Read it, then read the surrounding code in the worktree where you need context.{shell_note}

## The brief(s) the other agent was given
<brief>
{brief}
</brief>
{focus}
## What to check
1. Spec compliance: does the change do what the brief asks, no more and no less? Did it drop existing behaviour, data or fallbacks?
2. Correctness: bugs, edge cases, error handling, behaviour on the real data the brief mentions.
3. Tests: do they exercise the new behaviour meaningfully?
Report only material issues; skip style nits.

## Output (use exactly this structure)
## Verdict
APPROVE or CHANGES REQUESTED
## Findings
- [high|medium|low] `path:line`: the problem. Evidence: what you saw. Fix: the smallest change that resolves it.
(write "None." when there are no material findings)
## Spec compliance
- one line per requirement in the brief: met / partly / not met, with the reason
"""


def git(*args, cwd=None, check=True):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        sys.exit(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout.strip()


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
    return lines


def build_cmd(agent, model, effort, wt, job, read_only, unsandboxed=False, resume=None):
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
        cmd += ["-o", str(job / "last-message.md")]
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
             f"cd {q(str(wt))} || exit 1",
             f"echo $$ > {J('pid')}",
             f"date -Is > {J('started_at')}"]
    if digest:
        lines.append(f"{digest} > {J('pre_digest')}")
    lines += [f"{cmd} > {J('log')} 2>&1", "rc=$?", f'echo "$rc" > {J("exit_code")}']
    if agent == "cursor":
        lines.append(f"python3 {q(str(SCRIPTS / 'delegate'))} _cursor-last {q(str(job))}")
    if digest:
        lines.append(f"{digest} > {J('post_digest')}")
    if verify:
        lines.append(f"{{ echo {q('$ ' + verify)}; bash -c {q(verify)}; }} > {J('verify.log')} 2>&1; "
                     f"echo $? > {J('verify_code')}")
    lines += [f"date -Is > {J('finished_at')}", 'exit "$rc"', ""]
    return "\n".join(lines)


def start(job, detach, name):
    sys.stdout.flush()
    if detach:
        subprocess.Popen(["bash", str(job / "run.sh")], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        print("detached; check with: delegate status " + name)
        return
    os.execvp("bash", ["bash", str(job / "run.sh")])


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
    (root / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")


def _exclude(repo, patterns):
    """Add exact paths to the shared info/exclude (a `dir/` rule misses symlinks)."""
    exclude = Path(git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=repo)) / "info" / "exclude"
    exclude.parent.mkdir(exist_ok=True)
    lines = exclude.read_text().splitlines() if exclude.exists() else []
    for p in patterns:
        if p not in lines:
            lines.append(p)
    exclude.write_text("\n".join(lines) + "\n")


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
    base_sha = git("rev-parse", a.base or "HEAD", cwd=repo)
    brief = Path(a.brief).expanduser().read_text()
    model = a.model or DEFAULTS[a.agent][0]
    effort = a.effort if a.effort is not None else (None if a.model else DEFAULTS[a.agent][1])
    wt = root / "wt"
    if git("status", "--porcelain", cwd=repo):
        print(f"note: {repo} has uncommitted changes; the delegate starts from {base_sha[:9]} and will not see them",
              file=sys.stderr)

    scope = ("This is a read-only task: do not modify, create or delete any files."
             if a.read_only else "Work only inside this worktree; do not modify files outside it.")
    links_txt = "".join(f"- `{l}` is a symlink to the main checkout's copy; treat it as read-only input.\n"
                        for l in a.link)
    prompt = brief.rstrip() + CONTRACT.format(wt=wt, branch=branch, base=base_sha[:9], scope=scope,
                                              links=links_txt)
    cmd = build_cmd(a.agent, model, effort, wt, root, a.read_only, a.unsandboxed)
    run_sh = job_script(root, f"run {name}", repo, wt, cmd, a.agent, verify=a.verify)
    meta = {"name": name, "agent": a.agent, "model": model, "effort": effort,
            "read_only": a.read_only, "unsandboxed": a.unsandboxed, "verify": a.verify,
            "repo": str(repo), "worktree": str(wt), "branch": branch, "base": base_sha,
            "links": a.link, "rounds": 1, "created": datetime.now().isoformat(timespec="seconds"),
            "brief_source": str(Path(a.brief).expanduser().resolve())}

    if a.dry_run:
        print(f"[dry run] would create {root} and worktree {wt} on {branch} from {base_sha[:9]}\n")
        print(run_sh)
        print("---- prompt.md ----")
        print(prompt)
        return

    root.mkdir(parents=True, exist_ok=True)
    git("worktree", "add", "-b", branch, str(wt), base_sha, cwd=repo)
    for l in a.link:
        src, dst = repo / l, wt / l
        if not src.exists():
            sys.exit(f"--link {l}: {src} does not exist")
        if dst.exists() or dst.is_symlink():
            sys.exit(f"--link {l}: {dst} already exists in the worktree (tracked path?)")
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.symlink_to(src)
    _exclude(repo, ["/" + l.strip("/") for l in a.link] + ["/.delegate"])
    (root / "brief.md").write_text(brief)
    (root / "prompt.md").write_text(prompt)
    (root / "run.sh").write_text(run_sh)
    save_meta(root, meta)

    print(f"run:      {root}")
    print(f"worktree: {wt}  (branch {branch}, base {base_sha[:9]})")
    print(f"agent:    {a.agent} {model}" + (f" @{effort}" if effort else "") + (" read-only" if a.read_only else ""))
    print(f"log:      {root / 'log'}")
    print(f"last msg: {root / 'last-message.md'}")
    if a.verify:
        print(f"verify:   {a.verify}")
    start(root, a.detach, name)


def session_id(job, agent):
    """The agent's session/chat id, read from a finished job's log."""
    log = job / "log"
    if not log.exists():
        return None
    text = log.read_text(errors="replace")
    if agent == "codex":
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
    meta = load_meta(root)
    if state(root) == "running":
        sys.exit("delegate is still running")
    wt = Path(meta["worktree"])
    if not wt.exists():
        sys.exit("worktree was removed; start a new run instead")
    text = Path(a.brief).expanduser().read_text().rstrip() if a.brief else ""
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

    archive = root / f"round-{k}"
    if a.dry_run:
        print(f"[dry run] round {k + 1}: {meta['agent']} {model}"
              + (f", resuming session {sid}" if sid else ", fresh session") + f"\n---- brief ----\n{text}")
        return
    archive.mkdir(exist_ok=True)
    for f in ROUND_FILES:
        if (root / f).exists():
            shutil.move(str(root / f), str(archive / f))
    if a.fresh:
        scope = "Work only inside this worktree; do not modify files outside it."
        links_txt = "".join(f"- `{l}` is a symlink to the main checkout's copy; treat it as read-only input.\n"
                            for l in meta.get("links", []))
        prompt = (text + "\n\nAn earlier attempt's changes are already in this worktree (see `git diff "
                  f"{meta['base'][:9]}`); build on them rather than starting over."
                  + CONTRACT.format(wt=wt, branch=meta["branch"], base=meta["base"][:9], scope=scope,
                                    links=links_txt))
    else:
        prompt = text + FOLLOWUP_NOTE.format(round=k + 1)
    cmd = build_cmd(meta["agent"], model, effort, wt, root, meta.get("read_only"), meta.get("unsandboxed"),
                    resume=sid)
    (root / "brief.md").write_text(text + "\n")
    (root / "prompt.md").write_text(prompt)
    (root / "run.sh").write_text(job_script(root, f"run {meta['name']} round {k + 1}", Path(meta["repo"]), wt,
                                            cmd, meta["agent"], verify=verify))
    meta.setdefault("history", []).append({"round": k, "model": meta["model"], "effort": meta.get("effort"),
                                           "session": session_id(archive, meta["agent"])})
    meta.update(rounds=k + 1, model=model, effort=effort, verify=verify)
    save_meta(root, meta)
    print(f"round {k} archived to {archive}")
    print(f"round {k + 1}: {meta['agent']} {model}" + (f" @{effort}" if effort else "")
          + (f", resuming session {sid}" if sid else ", fresh session"))
    start(root, a.detach, meta["name"])


def review(a):
    root = find_run(a.run)
    meta = load_meta(root)
    if state(root) == "running":
        sys.exit("delegate is still running")
    wt = Path(meta["worktree"])
    if not wt.exists():
        sys.exit("worktree was removed")
    agent = a.agent or ("cursor" if meta["agent"] == "codex" else "codex")
    model = a.model or DEFAULTS[agent][0]
    effort = a.effort if a.effort is not None else (None if a.model else DEFAULTS[agent][1])
    _, patch = make_patch(root, meta)
    if not patch.strip():
        sys.exit("no changes to review")
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
    prompt = REVIEW_PROMPT.format(wt=wt, base=meta["base"][:9], n=n, shell_note=shell_note,
                                  brief=brief, focus=focus)
    cmd = build_cmd(agent, model, effort, wt, job, read_only=True)
    if a.dry_run:
        print(f"[dry run] review {n} by {agent} {model}: {cmd}\n---- prompt.md ----\n{prompt}")
        return
    job.mkdir()
    (wt / ".delegate").mkdir(exist_ok=True)
    for old in (wt / ".delegate").glob("review-*.patch"):
        old.unlink()  # a stale diff would only confuse the reviewer
    (wt / ".delegate" / f"review-{n}.patch").write_text(patch)
    (job / "prompt.md").write_text(prompt)
    (job / "run.sh").write_text(job_script(job, f"review {meta['name']} #{n}", Path(meta["repo"]), wt, cmd,
                                           agent, digest_base=meta["base"]))
    (job / "meta.json").write_text(json.dumps({"agent": agent, "model": model, "effort": effort,
                                               "round": meta.get("rounds", 1)}, indent=2) + "\n")
    print(f"review {n} of {meta['name']} (round {meta.get('rounds', 1)}) by {agent} {model}"
          + (f" @{effort}" if effort else ""))
    print(f"result:   {job / 'last-message.md'}")
    start(job, a.detach, meta["name"])


def verify(a):
    root = find_run(a.run)
    meta = load_meta(root)
    cmd = a.cmd or meta.get("verify")
    if not cmd:
        sys.exit("no verify command stored for this run; pass --cmd")
    r = subprocess.run(["bash", "-c", cmd], cwd=meta["worktree"], capture_output=True, text=True)
    out = f"$ {cmd}\n{r.stdout}{r.stderr}"
    if cmd == meta.get("verify"):  # ad-hoc --cmd runs only print; status tracks the run's own check
        (root / "verify.log").write_text(out)
        (root / "verify_code").write_text(f"{r.returncode}\n")
    print(out.rstrip())
    print(f"\nverify: {'pass' if r.returncode == 0 else f'FAIL rc={r.returncode}'}")
    sys.exit(r.returncode)


def state(job):
    if (job / "exit_code").exists():
        rc = (job / "exit_code").read_text().strip()
        return "done" if rc == "0" else f"failed rc={rc}"
    pid_f = job / "pid"
    if not pid_f.exists():
        return "not started"
    try:
        os.kill(int(pid_f.read_text().strip()), 0)
        return "running"
    except (ProcessLookupError, ValueError):
        return "died (no exit code)"
    except PermissionError:
        return "running"


def _elapsed(job):
    try:
        start_t = datetime.fromisoformat((job / "started_at").read_text().strip())
    except (FileNotFoundError, ValueError):
        return ""
    end_f = job / "finished_at"
    end = datetime.fromisoformat(end_f.read_text().strip()) if end_f.exists() else datetime.now(start_t.tzinfo)
    m, s = divmod(int((end - start_t).total_seconds()), 60)
    return f"{m}m{s:02d}s"


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
        m = re.search(r"verdict\W*\s*(APPROVE|CHANGES REQUESTED)", last.read_text(), re.I)
        parts.append(m.group(1).upper() if m else "verdict unclear")
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


def log_tail(job, agent, n):
    log = job / "log"
    if not log.exists() or n <= 0:
        return []
    lines = log.read_text(errors="replace").splitlines()
    if agent == "cursor":
        lines = [r for r in (_render_cursor_line(l) for l in lines) if r]
    return lines[-n:]


def status(a):
    roots = [find_run(r) for r in a.runs] if a.runs else sorted(
        (p.parent for p in RUNS_ROOT.glob("*/*/meta.json")), key=lambda p: p.stat().st_mtime)
    if not roots:
        print(f"no runs under {RUNS_ROOT}")
        return
    for root in roots:
        meta = load_meta(root)
        wt = Path(meta["worktree"])
        changes = git("status", "--porcelain", cwd=wt, check=False).splitlines() if wt.exists() else []
        bits = [f"{len(changes)} changed path(s)" if wt.exists() else "worktree removed"]
        if meta.get("rounds", 1) > 1:
            bits.append(f"round {meta['rounds']}")
        if _verify_state(root):
            bits.append(_verify_state(root))
        if (root / "applied").exists():
            bits.append("applied")
        print(f"== {meta['name']}  {meta['agent']} {meta['model']}  [{state(root)} {_elapsed(root)}]  "
              + ", ".join(bits))
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


def make_patch(root, meta):
    wt = Path(meta["worktree"])
    git("add", "-A", "-N", cwd=wt)  # intent-to-add: new files show up in the diff; nothing is committed
    head = git("rev-parse", "HEAD", cwd=wt)
    if head != meta["base"]:
        print(f"WARNING: worktree HEAD {head[:9]} != base {meta['base'][:9]}: the delegate made commits "
              f"(contract breach). The patch below covers them too.", file=sys.stderr)
    patch = subprocess.run(["git", "diff", "--binary", meta["base"]], cwd=wt,
                           capture_output=True, text=True, check=True).stdout
    (root / "changes.patch").write_text(patch)
    return git("diff", "--stat", meta["base"], cwd=wt), patch


def diff(a):
    root = find_run(a.run)
    meta = load_meta(root)
    stat, patch = make_patch(root, meta)
    print(stat or "(no changes)")
    print(f"\npatch: {root / 'changes.patch'}  ({len(patch.splitlines())} lines)")
    if a.full:
        print(patch)


def apply(a):
    root = find_run(a.run)
    meta = load_meta(root)
    if state(root) == "running":
        sys.exit("delegate is still running")
    _, patch = make_patch(root, meta)
    if not patch.strip():
        sys.exit("nothing to apply")
    args = ["git", "apply"] + (["--check"] if a.check else []) + (["--3way"] if a.three_way else [])
    r = subprocess.run(args + [str(root / "changes.patch")], cwd=meta["repo"], capture_output=True, text=True)
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    if r.returncode:
        sys.exit(f"git apply failed (rc={r.returncode}); try --3way, or merge by hand from {root / 'changes.patch'}")
    if not a.check:
        (root / "applied").write_text(_digest(patch) + "\n")
    print(("patch applies cleanly to " if a.check else "applied to working tree of ") + meta["repo"])


def _digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def cleanup(a):
    root = find_run(a.run)
    meta = load_meta(root)
    if state(root) == "running" or any(state(rv) == "running" for rv in root.glob("review-*")):
        sys.exit("delegate or a review is still running")
    wt = Path(meta["worktree"])
    if wt.exists():
        dirty = git("status", "--porcelain", cwd=wt, check=False)
        applied = root / "applied"
        if dirty and not a.force:
            if not (applied.exists() and applied.read_text().strip() == _digest(make_patch(root, meta)[1])):
                sys.exit(f"worktree has changes that were not applied (or changed since apply); "
                         f"apply them or pass --force:\n{dirty}")
        for l in meta.get("links", []):
            if (wt / l).is_symlink():
                (wt / l).unlink()
        # safe by now: changes were applied, absent, or explicitly discarded
        git("worktree", "remove", "--force", str(wt), cwd=meta["repo"])
    if not a.keep_branch:
        git("branch", "-D", meta["branch"], cwd=meta["repo"], check=False)
    if a.purge:
        shutil.rmtree(root)
        print(f"removed {root}")
    else:
        print(f"worktree removed; run record kept in {root}")

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
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
RUNS_ROOT = Path(os.environ.get("CLAUDE_DELEGATE_ROOT",
                                Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
                                / "claude-delegate" / "runs"))
DEFAULTS = {"codex": ("gpt-6.1-sol", "xhigh"), "cursor": ("auto", None)}
ROUND_FILES = ("brief.md", "prompt.md", "run.sh", "log", "last-message.md", "exit_code", "pid",
               "started_at", "finished_at", "verify.log", "verify_code", "verify-adhoc.log")

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
- Keep those prose sections and append this short machine-readable block at the very end:
## Digest
files: <path> (+a/-d); ...
verify: <command> -> <result>
conflicts: <one line each, or none>
risks: <path:line - why, or none>
"""

FOLLOWUP_NOTE = """

---
Follow-up round {round}. The delegation contract from the start of this session still applies: same worktree, no commits, flag conflicts, same final-message structure. The worktree already contains your earlier changes; build on them.
Keep the prose sections and append the same `## Digest` block (files, verify, conflicts, risks) at the very end.
"""

REVIEW_PROMPT = """# Review request (read-only)

Another coding agent made changes in this git worktree ({wt}) relative to commit {base}. Review them. This is a read-only task: do not modify, create or delete any files.

The full diff is in `.delegate/review-{n}.patch` (path relative to the worktree root; new files appear as additions). Read it, then read the surrounding code in the worktree where you need context.{shell_note}
The run's verify command and the last 80 lines of its latest verify output are in `.delegate/review-{n}.verify.log`. Read that file to check verification claims against actual output.

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
        lines.append(f"{{ echo {q('$ ' + verify)}; bash -c {q(verify)}; }} > {J('verify.log')} 2>&1; "
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


def add_links(repo, wt, paths, existing=()):
    """Validate links first, then install them and share launch/followup exclusions."""
    for link in paths:
        p = Path(link)
        if p.is_absolute() or ".." in p.parts or not p.parts or p.parts[0] in (".git", ".delegate"):
            sys.exit(f"--link {link}: expected a repo-relative input path")
        src, dst = repo / p, wt / p
        if not src.exists():
            sys.exit(f"--link {link}: {src} does not exist")
        if dst.exists() or dst.is_symlink():
            if link in existing and dst.is_symlink() and dst.resolve() == src.resolve():
                continue
            sys.exit(f"--link {link}: {dst} already exists in the worktree (tracked path?)")
    for link in paths:
        dst = wt / link
        if not dst.is_symlink():
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.symlink_to(repo / link)
    _exclude(repo, ["/" + l.strip("/") for l in paths] + ["/.delegate"])


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
    add_links(repo, wt, a.link)
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
    meta = load_meta(root)
    if state(root) in BUSY:
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
    links = list(dict.fromkeys(meta.get("links", []) + a.link))
    add_links(Path(meta["repo"]), wt, a.link, existing=meta.get("links", []))
    meta["links"] = links
    if (root / "exit_code").exists() and not any(r.get("n", r.get("round")) == k for r in meta.get("history", [])):
        meta.setdefault("history", []).append(_round_record(root, meta, k))
    archive.mkdir(exist_ok=True)
    for f in ROUND_FILES:
        if (root / f).exists():
            shutil.move(str(root / f), str(archive / f))
    if a.fresh:
        scope = ("This is a read-only task: do not modify, create or delete any files."
                 if meta.get("read_only") else "Work only inside this worktree; do not modify files outside it.")
        links_txt = "".join(f"- `{l}` is a symlink to the main checkout's copy; treat it as read-only input.\n"
                            for l in meta.get("links", []))
        prompt = (text + "\n\nAn earlier attempt's changes are already in this worktree (see `git diff "
                  f"{meta['base'][:9]}`); build on them rather than starting over."
                  + CONTRACT.format(wt=wt, branch=meta["branch"], base=meta["base"][:9], scope=scope,
                                    links=links_txt))
    else:
        prompt = text + FOLLOWUP_NOTE.format(round=k + 1)
        prompt += "".join(f"- `{l}` is a symlink to the main checkout's copy; treat it as read-only input.\n"
                          for l in a.link)
    cmd = build_cmd(meta["agent"], model, effort, wt, root, meta.get("read_only"), meta.get("unsandboxed"),
                    resume=sid)
    (root / "brief.md").write_text(text + "\n")
    (root / "prompt.md").write_text(prompt)
    (root / "run.sh").write_text(job_script(root, f"run {meta['name']} round {k + 1}", Path(meta["repo"]), wt,
                                            cmd, meta["agent"], verify=verify))
    meta.update(rounds=k + 1, model=model, effort=effort, verify=verify, resumed=bool(sid))
    save_meta(root, meta)
    print(f"round {k} archived to {archive}")
    print(f"round {k + 1}: {meta['agent']} {model}" + (f" @{effort}" if effort else "")
          + (f", resuming session {sid}" if sid else ", fresh session"))
    start(root, a.detach, meta["name"])


def review(a):
    root = find_run(a.run)
    meta = load_meta(root)
    if state(root) in BUSY:
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
    write_patch(wt / ".delegate" / f"review-{n}.patch", patch)
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
    start(job, a.detach, meta["name"])


def verify(a):
    root = find_run(a.run)
    meta = load_meta(root)
    if not Path(meta["worktree"]).is_dir():
        sys.exit("worktree was removed; cannot run verify (saved output is in verify.log)")
    cmd = a.cmd or meta.get("verify")
    if not cmd:
        sys.exit("no verify command stored for this run; pass --cmd")
    r = subprocess.run(["bash", "-c", cmd], cwd=meta["worktree"], capture_output=True, text=True)
    out = f"$ {cmd}\n{r.stdout}{r.stderr}"
    if a.cmd is None:
        (root / "verify.log").write_text(out)
        (root / "verify_code").write_text(f"{r.returncode}\n")
    else:
        (root / "verify-adhoc.log").write_text(out)
    print("\n".join(out.rstrip().splitlines()[-30:]))
    print(f"verify: {'pass' if r.returncode == 0 else f'FAIL rc={r.returncode}'}")
    sys.exit(r.returncode)


BUSY = ("running", "verifying")


def _pid_alive(job):
    try:
        os.kill(int((job / "pid").read_text().strip()), 0)
        return True
    except PermissionError:
        return True
    except (OSError, ValueError):
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
                           verdict=_verdict(job))
        save_meta(job, review_meta)
    else:
        n = meta.get("rounds", 1)
        history = [r for r in meta.get("history", []) if r.get("n", r.get("round")) != n]
        history.append(_round_record(job, meta, n))
        meta["history"] = sorted(history, key=lambda r: r.get("n", r.get("round", 0)))
        save_meta(root, meta)
    print("\n".join(packet(job)))


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


def packet(job):
    """Rebuild a bounded notification packet entirely from existing artifacts."""
    root, meta, review_meta = _context(job)
    jm = review_meta or meta
    label = f"review {job.name.split('-')[-1]}" if review_meta is not None else f"round {meta.get('rounds', 1)}"
    rc = _code(job, "exit_code")
    verify_state = _verify_state(job) or "verify none"
    lines = [f"== {_one_line(meta['name'])} | {_one_line(jm['agent'])} {_one_line(jm['model'])} | {label} | "
             f"{state(job)} | {_elapsed(job)} | exit {rc if rc is not None else '?'} | {verify_state}",
             _usage_line(_job_usage(job, meta, review_meta))]
    if review_meta is not None:
        text = _read(job, "last-message.md") or ""
        counts = {s: len(re.findall(r"^\s*[-*]\s*\[" + s + r"\]", text, re.M | re.I))
                  for s in ("high", "medium", "low")}
        lines.append(f"verdict: {_verdict(job) or 'unclear'} | findings: "
                     + ", ".join(f"{s} {count}" for s, count in counts.items()))
        pre, post = _read(job, "pre_digest"), _read(job, "post_digest")
        if pre is not None and post is not None and pre != post:
            lines.append("WARNING: reviewer changed the worktree")
    else:
        rows = change_stats(root, meta)
        totals = (sum(r["insertions"] for r in rows), sum(r["deletions"] for r in rows))
        suffix = " (worktree removed; saved patch)" if not Path(meta["worktree"]).is_dir() else ""
        if not Path(meta["worktree"]).is_dir() and not (root / "changes.patch").exists():
            suffix = " (worktree removed; no saved patch)"
        lines.append(f"files: {len(rows)} | +{totals[0]}/-{totals[1]} total{suffix}"
                     + (" | applied" if (root / "applied").exists() else ""))
        for r in sorted(rows, key=lambda r: (-(r["insertions"] + r["deletions"]), r["path"]))[:8]:
            lines.append(f"  {_one_line(r['path'])}: " + ("binary" if r["binary"] else
                         f"+{r['insertions']}/-{r['deletions']}"))
        if len(rows) > 8:
            lines.append(f"  ... {len(rows) - 8} more file(s)")
        reviews = sorted(root.glob("review-*/meta.json"), key=lambda p: int(p.parent.name.split("-")[1]))
        if reviews:
            lines.append("latest " + _review_summary(reviews[-1].parent))
    lines.extend(_concerns(job))
    paths = f"artifacts: {job / 'last-message.md'} | {job / 'log'} | {job / 'verify.log'}"
    if review_meta is not None:
        paths += f" | {Path(meta['worktree']) / '.delegate' / (job.name + '.verify.log')}"
    else:
        paths += f" | {root / 'changes.patch'}"
    lines.append(paths)
    return lines


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
        if (root / "applied").exists():
            bits.append("applied")
            if wt.exists() and time.time() - (root / "applied").stat().st_mtime > 86400:
                # Field incident: a day-old applied worktree with edits looked like unmerged work.
                bits.append(f"STALE worktree: applied over a day ago; `delegate cleanup {meta['name']}`")
        print(f"== {meta['name']}  {meta['agent']} {meta['model']}  [{state(root)} {_elapsed(root)}]  "
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


def make_patch(root, meta):
    wt = Path(meta["worktree"])
    git("add", "-A", "-N", cwd=wt)  # intent-to-add: new files show up in the diff; nothing is committed
    head = git("rev-parse", "HEAD", cwd=wt)
    if head != meta["base"]:
        print(f"WARNING: worktree HEAD {head[:9]} != base {meta['base'][:9]}: the delegate made commits "
              f"(contract breach). The patch below covers them too.", file=sys.stderr)
    # Bytes, not text=True: universal newlines would turn \r\n into \n and break CRLF files.
    patch = subprocess.run(["git", "diff", "--binary", meta["base"]], cwd=wt,
                           capture_output=True, check=True).stdout.decode("utf-8", "surrogateescape")
    write_patch(root / "changes.patch", patch)
    return git("diff", "--stat", meta["base"], cwd=wt), patch


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


def apply(a):
    root = find_run(a.run)
    meta = load_meta(root)
    if state(root) in BUSY:
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
                        "minutes": rv.get("minutes", _minutes(job)),
                        "verdict": rv.get("verdict", _verdict(job)),
                        "usage": rv.get("usage", usage(job, rv["agent"]))})
    rows = change_stats(root, meta)
    mode = ("read-only" if meta.get("read_only") else "workspace-write") if meta["agent"] == "codex" else (
        "ask" if meta.get("read_only") else "unsandboxed" if meta.get("unsandboxed") else "sandbox")
    record = {"v": 1, "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "repo": Path(meta["repo"]).name, "repo_path": meta["repo"], "run": meta["name"],
              "agent": meta["agent"], "model": meta["model"], "effort": meta.get("effort"),
              "mode": mode, "links": meta.get("links", []), "base": meta["base"], "outcome": outcome,
              "files": len(rows), "insertions": sum(r["insertions"] for r in rows),
              "deletions": sum(r["deletions"] for r in rows), "rounds": rounds, "reviews": reviews}
    RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    with (RUNS_ROOT / "runs.jsonl").open("a") as stream:
        stream.write(json.dumps(record) + "\n")
    (root / "logged").write_text(outcome + "\n")


def _digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def cleanup(a):
    root = find_run(a.run)
    meta = load_meta(root)
    if state(root) in BUSY or any(state(rv) in BUSY for rv in root.glob("review-*")):
        sys.exit("delegate or a review is still running")
    wt = Path(meta["worktree"])
    if wt.exists():
        dirty = git("status", "--porcelain", cwd=wt, check=False)
        applied = root / "applied"
        if dirty and not a.force:
            if not (applied.exists() and applied.read_text().strip() == _digest(make_patch(root, meta)[1])):
                sys.exit(f"worktree has changes that were not applied (or changed since apply); "
                         f"apply them or pass --force:\n{dirty}")
    if not (root / "logged").exists() and not (root / "applied").exists():  # apply already logged it
        # Read-only runs, reviews and runs merged by hand never pass through apply.
        changed = wt.exists() and bool(git("status", "--porcelain", cwd=wt, check=False))
        log_run(root, meta, "discarded" if changed else "read-only" if meta.get("read_only") else "no-changes")
    if wt.exists():
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

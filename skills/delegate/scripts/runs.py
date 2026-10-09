"""Delegate runs: git worktree + brief + background agent + log + last message."""
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
RUNS_ROOT = Path(os.environ.get("CLAUDE_DELEGATE_ROOT",
                                Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
                                / "claude-delegate" / "runs"))

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


def build_cmd(agent, model, effort, wt, root, read_only, unsandboxed=False):
    q = shlex.quote
    if agent == "codex":
        cmd = ["codex", "exec", "-m", model]
        if effort:
            cmd += ["-c", f'model_reasoning_effort="{effort}"']
        cmd += ["-s", "read-only" if read_only else "workspace-write",
                "-C", str(wt), "-o", str(root / "last-message.md"), "--color", "never", "-"]
        return " ".join(q(c) for c in cmd) + f" < {q(str(root / 'prompt.md'))}"
    if agent == "cursor":
        # Headless with --sandbox enabled, shell commands auto-run confined to
        # the workspace (writes elsewhere get EPERM, no network). --force
        # ("Run Everything") silently bypasses that sandbox (verified
        # 2026-10-09), so it is opt-in only.
        cmd = ["cursor-agent", "-p", "--output-format", "stream-json", "--model", model,
               "--trust", "--workspace", str(wt)]
        cmd += ["--sandbox", "disabled", "--force"] if unsandboxed else ["--sandbox", "enabled"]
        if read_only:
            cmd += ["--mode", "ask"]
        return " ".join(q(c) for c in cmd) + f' "$(cat {q(str(root / "prompt.md"))})" < /dev/null'
    sys.exit(f"unknown agent {agent!r}")


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
    base_sha = git("rev-parse", a.base or "HEAD", cwd=repo)
    brief = Path(a.brief).expanduser().read_text()
    model = a.model or {"codex": "gpt-6.1-sol", "cursor": "composer-2.5"}[a.agent]
    effort = a.effort if a.effort is not None else ("xhigh" if a.agent == "codex" and not a.model else None)
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
    if a.unsandboxed and a.agent != "cursor":
        sys.exit("--unsandboxed applies to cursor only (codex: pick -s yourself in run.sh)")
    cmd = build_cmd(a.agent, model, effort, wt, root, a.read_only, a.unsandboxed)
    q = shlex.quote
    post = (f"python3 {q(str(SCRIPTS / 'delegate'))} _cursor-last {q(str(root))}\n"
            if a.agent == "cursor" else "")
    run_sh = "\n".join([
        "#!/usr/bin/env bash",
        f"# delegate run {name}: generated {datetime.now().isoformat(timespec='seconds')}",
        "# Clean environment, run the agent in its worktree, record exit code and last message.",
        *clean_env_lines(repo),
        f"cd {q(str(wt))} || exit 1",
        f"echo $$ > {q(str(root / 'pid'))}",
        f"date -Is > {q(str(root / 'started_at'))}",
        f"{cmd} > {q(str(root / 'log'))} 2>&1",
        "rc=$?",
        f"echo \"$rc\" > {q(str(root / 'exit_code'))}",
        post + f"date -Is > {q(str(root / 'finished_at'))}",
        'exit "$rc"',
        "",
    ])
    meta = {"name": name, "agent": a.agent, "model": model, "effort": effort,
            "read_only": a.read_only, "unsandboxed": a.unsandboxed, "repo": str(repo), "worktree": str(wt), "branch": branch,
            "base": base_sha, "links": a.link, "created": datetime.now().isoformat(timespec="seconds"),
            "brief_source": str(Path(a.brief).expanduser().resolve())}

    if a.dry_run:
        print(f"[dry run] would create {root} and worktree {wt} on {branch} from {base_sha[:9]}\n")
        print(run_sh)
        print("---- prompt.md ----")
        print(prompt)
        return

    root.mkdir(parents=True, exist_ok=True)
    git("worktree", "add", "-b", branch, str(wt), base_sha, cwd=repo)
    if a.link:
        exclude = Path(git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=repo)) / "info" / "exclude"
        exclude.parent.mkdir(exist_ok=True)
        lines = exclude.read_text().splitlines() if exclude.exists() else []
        for l in a.link:
            src, dst = repo / l, wt / l
            if not src.exists():
                sys.exit(f"--link {l}: {src} does not exist")
            if dst.exists() or dst.is_symlink():
                sys.exit(f"--link {l}: {dst} already exists in the worktree (tracked path?)")
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.symlink_to(src)
            # exact path, no trailing slash: a `dir/` ignore rule does not match a symlink
            pattern = "/" + l.strip("/")
            if pattern not in lines:
                lines.append(pattern)
        exclude.write_text("\n".join(lines) + "\n")
    (root / "brief.md").write_text(brief)
    (root / "prompt.md").write_text(prompt)
    (root / "run.sh").write_text(run_sh)
    (root / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")

    print(f"run:      {root}")
    print(f"worktree: {wt}  (branch {branch}, base {base_sha[:9]})")
    print(f"agent:    {a.agent} {model}" + (f" @{effort}" if effort else "") + (" read-only" if a.read_only else ""))
    print(f"log:      {root / 'log'}")
    print(f"last msg: {root / 'last-message.md'}")
    sys.stdout.flush()
    if a.detach:
        subprocess.Popen(["bash", str(root / "run.sh")], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        print("detached; check with: delegate status " + name)
        return
    os.execvp("bash", ["bash", str(root / "run.sh")])


def state(root):
    if (root / "exit_code").exists():
        rc = (root / "exit_code").read_text().strip()
        return "done" if rc == "0" else f"failed rc={rc}"
    pid_f = root / "pid"
    if not pid_f.exists():
        return "not started"
    try:
        os.kill(int(pid_f.read_text().strip()), 0)
        return "running"
    except (ProcessLookupError, ValueError):
        return "died (no exit code)"
    except PermissionError:
        return "running"


def _elapsed(root):
    try:
        start = datetime.fromisoformat((root / "started_at").read_text().strip())
    except (FileNotFoundError, ValueError):
        return ""
    end_f = root / "finished_at"
    end = datetime.fromisoformat(end_f.read_text().strip()) if end_f.exists() else datetime.now(start.tzinfo)
    m, s = divmod(int((end - start).total_seconds()), 60)
    return f"{m}m{s:02d}s"


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


def log_tail(root, meta, n):
    log = root / "log"
    if not log.exists():
        return []
    lines = log.read_text(errors="replace").splitlines()
    if meta["agent"] == "cursor":
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
        where = f"{len(changes)} changed path(s)" if wt.exists() else "worktree removed"
        if (root / "applied").exists():
            where += ", applied"
        print(f"== {meta['name']}  {meta['agent']} {meta['model']}  [{state(root)} {_elapsed(root)}]  {where}")
        print(f"   {root}")
        if a.runs:
            for c in changes[:40]:
                print("   " + c)
            tail = log_tail(root, meta, a.log)
            if tail:
                print("   -- log tail --")
                for l in tail:
                    print("   " + l[:240])
            last = root / "last-message.md"
            if last.exists():
                print("   -- last message --")
                print(last.read_text().rstrip())


def cursor_last(root):
    """Extract the final answer from cursor-agent stream-json into last-message.md.

    The stream's `result` concatenates every assistant message, including
    narration between tool calls; keep only what came after the last tool call.
    """
    result, tail = None, []
    for line in (root / "log").read_text(errors="replace").splitlines():
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
    (root / "last-message.md").write_text(text.rstrip() + "\n")


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
    import hashlib
    return hashlib.sha256(text.encode()).hexdigest()


def cleanup(a):
    root = find_run(a.run)
    meta = load_meta(root)
    if state(root) == "running":
        sys.exit("delegate is still running")
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
        import shutil
        shutil.rmtree(root)
        print(f"removed {root}")
    else:
        print(f"worktree removed; run record kept in {root}")

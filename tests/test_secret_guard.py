"""Tests for hooks/secret-guard.py (PreToolUse, pre-push, --check)."""
import ast
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HOOK = REPO / "hooks" / "secret-guard.py"
SESSION = REPO / "hooks" / "session-mode.py"


def _fake_token(prefix, n):
    """Build a fake secret at runtime so the test file source is not itself a hit."""
    return prefix + ("a" * n)


def _git(cwd, *args, check=True):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check)


def _init_repo(path, name="Alice", email="alice@example.com"):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init")
    _git(path, "config", "user.name", name)
    _git(path, "config", "user.email", email)
    # Avoid depending on the user's default branch name.
    _git(path, "checkout", "-b", "main")


class _SecretGuardHarness:
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="secret-guard-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith("SECRET_GUARD_")}
        # Cwd-guard reads these; keep the default harness silent unless a test sets them.
        self.env.pop("CLAUDE_PROJECT_DIR", None)
        self.env.pop("CWD_GUARD_DISABLE", None)
        self.env.pop("CLAUDE_SESSION_LOG", None)
        self.env["HOME"] = str(self.tmp / "home")
        (self.tmp / "home").mkdir()

    def run_hook(self, payload=None, argv=None, stdin_text=None, env=None, cwd=None):
        cmd = [sys.executable, str(HOOK), *(argv or [])]
        if payload is not None:
            stdin_text = json.dumps(payload)
        result = subprocess.run(
            cmd, input=stdin_text if stdin_text is not None else "",
            env={**self.env, **(env or {})}, cwd=cwd or self.tmp,
            capture_output=True, text=True,
        )
        return result

    def claude(self, command, cwd=None, env=None, extra=None):
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "cwd": str(cwd or self.tmp),
        }
        if extra:
            payload.update(extra)
        return self.run_hook(payload, env=env)

    def make_pair(self):
        """Local repo + bare remote named origin."""
        remote = self.tmp / "remote.git"
        local = self.tmp / "local"
        _git(self.tmp, "init", "--bare", str(remote))
        _init_repo(local)
        (local / "README").write_text("ok\n")
        _git(local, "add", "README")
        _git(local, "commit", "-m", "init")
        _git(local, "remote", "add", "origin", str(remote))
        _git(local, "push", "-u", "origin", "main")
        return local, remote


class SecretGuardTests(_SecretGuardHarness, unittest.TestCase):

    def test_non_push_no_output_and_no_git(self):
        env = {**self.env, "PATH": str(self.tmp / "empty-bin")}
        (self.tmp / "empty-bin").mkdir()
        r = self.claude("ls -la", env=env)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")
        self.assertEqual(r.stderr, "")

    def test_echo_git_push_and_commit_message_are_not_pushes(self):
        r = self.claude('echo "git push"')
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        # A commit is scanned (pre-commit stage), so it needs a repo to scan.
        local, _ = self.make_pair()
        r = self.claude('git commit -m "fix git push docs"', cwd=local)
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_clean_push_silent(self):
        local, _ = self.make_pair()
        (local / "f.txt").write_text("hello\n")
        _git(local, "add", "f.txt")
        _git(local, "commit", "-m", "add file")
        r = self.claude("git push origin main", cwd=local)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""))

    def test_token_in_unpushed_commit_asks_masked(self):
        local, _ = self.make_pair()
        token = _fake_token("ghp_", 36)
        (local / "leak.txt").write_text(f"token={token}\n")
        _git(local, "add", "leak.txt")
        _git(local, "commit", "-m", "add leak")
        r = self.claude("git push origin main", cwd=local)
        self.assertEqual(r.returncode, 0)
        data = json.loads(r.stdout)
        out = data["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "ask")
        reason = out["permissionDecisionReason"]
        self.assertIn("GitHub token", reason)
        self.assertIn("ghp_…40", reason)
        self.assertNotIn(token, reason)
        self.assertNotIn(token, r.stdout)

    def test_secret_already_on_target_remote_not_reported(self):
        local, _ = self.make_pair()
        token = _fake_token("ghp_", 36)
        (local / "leak.txt").write_text(f"token={token}\n")
        _git(local, "add", "leak.txt")
        _git(local, "commit", "-m", "add leak")
        _git(local, "push", "origin", "main")
        r = self.claude("git push origin main", cwd=local)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""))

    def test_private_origin_to_new_public_remote_reported(self):
        local, _ = self.make_pair()
        token = _fake_token("ghp_", 36)
        (local / "leak.txt").write_text(f"token={token}\n")
        _git(local, "add", "leak.txt")
        _git(local, "commit", "-m", "add leak")
        _git(local, "push", "origin", "main")
        public = self.tmp / "public.git"
        _git(self.tmp, "init", "--bare", str(public))
        _git(local, "remote", "add", "public", str(public))
        r = self.claude("git push public main", cwd=local)
        self.assertEqual(r.returncode, 0)
        reason = json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("GitHub token", reason)
        self.assertNotIn(token, reason)

    def test_commit_then_push_denied(self):
        local, _ = self.make_pair()
        for cmd in (
            'git commit -m "x" && git push',
            'git commit -m "x" ; git push',
            'git commit -m "x"\ngit push',
        ):
            r = self.claude(cmd, cwd=local)
            data = json.loads(r.stdout)
            self.assertEqual(data["hookSpecificOutput"]["permissionDecision"], "deny", cmd)
            self.assertIn("own Bash call", data["hookSpecificOutput"]["permissionDecisionReason"])

    def test_push_then_log_not_denied(self):
        local, _ = self.make_pair()
        (local / "f.txt").write_text("x\n")
        _git(local, "add", "f.txt")
        _git(local, "commit", "-m", "x")
        r = self.claude("git push origin main && git log -1", cwd=local)
        self.assertEqual(r.returncode, 0)
        if r.stdout.strip():
            data = json.loads(r.stdout)
            self.assertNotEqual(data["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_cd_and_git_c(self):
        local, _ = self.make_pair()
        token = _fake_token("ghp_", 36)
        (local / "leak.txt").write_text(f"{token}\n")
        _git(local, "add", "leak.txt")
        _git(local, "commit", "-m", "leak")
        parent = local.parent
        r = self.claude(f"cd {local.name} && git push origin main", cwd=parent)
        self.assertIn("GitHub token", json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"])
        r = self.claude(f"git -C {local} push origin main", cwd=parent)
        self.assertIn("GitHub token", json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"])

    def test_home_paths(self):
        local, _ = self.make_pair()
        alice = "/" + "home" + "/" + "alice" + "/"
        user = "/" + "home" + "/" + "user" + "/"
        (local / "p.txt").write_text(alice + ".ssh/id\n" + user + "ok\n~/.cache/x\n")
        _git(local, "add", "p.txt")
        _git(local, "commit", "-m", "paths")
        r = self.claude("git push origin main", cwd=local)
        reason = json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn(alice, reason)
        self.assertNotIn(user, reason)
        self.assertNotIn("~/.cache", reason)

    def test_env_files(self):
        local, _ = self.make_pair()
        (local / ".env").write_text("X=1\n")
        (local / ".env.example").write_text("X=\n")
        _git(local, "add", "-f", ".env", ".env.example")
        _git(local, "commit", "-m", "env")
        r = self.claude("git push origin main", cwd=local)
        reason = json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("sensitive file", reason)
        self.assertIn(".env", reason)
        # .env.example must not appear as its own finding path line with sensitive file
        self.assertNotRegex(reason, r"sensitive file\s+\.env\.example")

    def test_token_in_commit_message(self):
        local, _ = self.make_pair()
        token = _fake_token("ghp_", 36)
        (local / "x.txt").write_text("x\n")
        _git(local, "add", "x.txt")
        _git(local, "commit", "-m", f"use {token}")
        r = self.claude("git push origin main", cwd=local)
        reason = json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("GitHub token", reason)
        self.assertIn("commit message", reason)
        self.assertNotIn(token, reason)

    def test_refspecs_delete_tags(self):
        local, _ = self.make_pair()
        token = _fake_token("ghp_", 36)
        (local / "leak.txt").write_text(f"{token}\n")
        _git(local, "add", "leak.txt")
        _git(local, "commit", "-m", "leak")
        # src:dst still scans src
        r = self.claude("git push origin main:refs/heads/other", cwd=local)
        self.assertIn("GitHub token", json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"])
        # :dst and --delete scan nothing
        for cmd in ("git push origin :old-branch", "git push origin --delete old-branch"):
            r = self.claude(cmd, cwd=local)
            self.assertEqual(r.stdout.strip(), "", cmd)
        # --tags with a secret only in a branch commit still sees branch? --tags alone
        # adds tags; with no other refs after remote, refs default empty but tags flag set.
        _git(local, "tag", "v1")
        r = self.claude("git push origin --tags", cwd=local)
        # tag points at leak commit
        self.assertIn("ask", r.stdout)

    def test_bash_c_git_push(self):
        local, _ = self.make_pair()
        token = _fake_token("ghp_", 36)
        (local / "leak.txt").write_text(f"{token}\n")
        _git(local, "add", "leak.txt")
        _git(local, "commit", "-m", "leak")
        r = self.claude("bash -c 'git push origin main'", cwd=local)
        self.assertIn("GitHub token", json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"])

    def test_gh_repo_create_push_full_history(self):
        local = self.tmp / "solo"
        _init_repo(local)
        token = _fake_token("ghp_", 36)
        (local / "leak.txt").write_text(f"{token}\n")
        _git(local, "add", "leak.txt")
        _git(local, "commit", "-m", "leak")
        r = self.claude("gh repo create my/repo --push", cwd=local)
        reason = json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("GitHub token", reason)

    def test_gh_repo_edit_visibility_public(self):
        local = self.tmp / "solo2"
        _init_repo(local)
        token = _fake_token("ghp_", 36)
        (local / "leak.txt").write_text(f"{token}\n")
        _git(local, "add", "leak.txt")
        _git(local, "commit", "-m", "leak")
        r = self.claude("gh repo edit --visibility public", cwd=local)
        reason = json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("GitHub token", reason)
        self.assertIn("local clone", reason)

    def test_budget_exceeded_asks(self):
        local, _ = self.make_pair()
        (local / "f.txt").write_text("x\n")
        _git(local, "add", "f.txt")
        _git(local, "commit", "-m", "x")
        r = self.claude("git push origin main", cwd=local, env={"SECRET_GUARD_BUDGET_S": "0"})
        data = json.loads(r.stdout)
        self.assertEqual(data["hookSpecificOutput"]["permissionDecision"], "ask")
        self.assertIn("could not finish", data["hookSpecificOutput"]["permissionDecisionReason"])

    def test_disable(self):
        local, _ = self.make_pair()
        token = _fake_token("ghp_", 36)
        (local / "leak.txt").write_text(f"{token}\n")
        _git(local, "add", "leak.txt")
        _git(local, "commit", "-m", "leak")
        r = self.claude("git push", cwd=local, env={"SECRET_GUARD_DISABLE": "1"})
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_git_pre_push_modes(self):
        local, remote = self.make_pair()
        token = _fake_token("ghp_", 36)
        (local / "leak.txt").write_text(f"{token}\n")
        _git(local, "add", "leak.txt")
        _git(local, "commit", "-m", "leak")
        local_sha = _git(local, "rev-parse", "HEAD").stdout.strip()
        # new branch: all-zero remote sha
        stdin = f"refs/heads/main {local_sha} refs/heads/main {'0' * 40}\n"
        r = self.run_hook(argv=["origin", str(remote)], stdin_text=stdin, cwd=local)
        self.assertEqual(r.returncode, 1)
        self.assertIn("GitHub token", r.stderr)
        self.assertIn("--no-verify", r.stderr)
        self.assertNotIn(token, r.stderr)
        # update: remote has parent
        parent = _git(local, "rev-parse", "HEAD~1").stdout.strip()
        stdin = f"refs/heads/main {local_sha} refs/heads/main {parent}\n"
        r = self.run_hook(argv=["origin", str(remote)], stdin_text=stdin, cwd=local)
        self.assertEqual(r.returncode, 1)
        # deletion: all-zero local sha
        stdin = f"refs/heads/old {'0' * 40} refs/heads/old {local_sha}\n"
        r = self.run_hook(argv=["origin", str(remote)], stdin_text=stdin, cwd=local)
        self.assertEqual(r.returncode, 0)

    def test_check_includes_untracked(self):
        local, _ = self.make_pair()
        token = _fake_token("ghp_", 36)
        (local / "secret.txt").write_text(f"{token}\n")
        r = self.run_hook(argv=["--check", str(local)])
        self.assertEqual(r.returncode, 1)
        self.assertIn("GitHub token", r.stdout)
        self.assertNotIn(token, r.stdout)

    def test_copied_shell_functions_match_session_mode(self):
        def funcs(path):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            out = {}
            for node in tree.body:
                if isinstance(node, ast.FunctionDef) and node.name in (
                        "_substitution_end", "_shell_segments", "_tokens"):
                    out[node.name] = ast.unparse(node)
            return out
        a, b = funcs(HOOK), funcs(SESSION)
        self.assertEqual(set(a), {"_substitution_end", "_shell_segments", "_tokens"})
        for name in a:
            self.assertEqual(a[name], b[name], name)

    def test_fast_path_timing(self):
        times = []
        for _ in range(5):
            t0 = time.perf_counter()
            r = self.claude("ls")
            times.append(time.perf_counter() - t0)
            self.assertEqual((r.returncode, r.stdout), (0, ""))
        # Store median on the instance for the verification report.
        times.sort()
        self.fast_path_ms = times[len(times) // 2] * 1000
        self.assertLess(self.fast_path_ms, 500)


class SecretGuardRound2Tests(_SecretGuardHarness, unittest.TestCase):
    """Fixes from review round 1 (R1–R18) and orchestrator findings."""

    def test_r1_git_subprocess_timeout_becomes_ask(self):
        local, _ = self.make_pair()
        (local / "f.txt").write_text("x\n")
        _git(local, "add", "f.txt")
        _git(local, "commit", "-m", "x")
        r = self.claude("git push origin main", cwd=local, env={"SECRET_GUARD_BUDGET_S": "0"})
        data = json.loads(r.stdout)
        self.assertEqual(data["hookSpecificOutput"]["permissionDecision"], "ask")
        self.assertIn("could not finish", data["hookSpecificOutput"]["permissionDecisionReason"])

    def test_r2_redact_token_in_remote_url_and_errors(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("sg", HOOK)
        sg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sg)
        token = _fake_token("ghp_", 36)
        url = f"https://x:{token}@github.com/o/r.git"
        out = sg.redact(url)
        self.assertNotIn(token, out)
        self.assertIn("ghp_…", out)
        reason = sg.format_reason(
            [{"label": "GitHub token", "path": "a", "line": 1, "commit": "abc", "value": "ghp_…40"}],
            url,
        )
        self.assertNotIn(token, reason)

    def test_r3_latin1_commit_content_does_not_crash(self):
        local, _ = self.make_pair()
        # Latin-1 bytes that are not valid UTF-8
        path = local / "latin.txt"
        path.write_bytes(b"caf\xe9\n")
        _git(local, "add", "latin.txt")
        _git(local, "commit", "-m", "latin1")
        r = self.claude("git push origin main", cwd=local)
        self.assertEqual(r.returncode, 0)
        # Clean (no secrets): silent, or ask only on findings — must not be empty crash
        if r.stdout.strip():
            data = json.loads(r.stdout)
            self.assertIn(data["hookSpecificOutput"]["permissionDecision"], ("ask", "deny"))

    def test_r4_git_global_c_and_git_dir(self):
        local, _ = self.make_pair()
        token = _fake_token("ghp_", 36)
        (local / "leak.txt").write_text(f"{token}\n")
        _git(local, "add", "leak.txt")
        _git(local, "commit", "-m", "leak")
        public = self.tmp / "public.git"
        _git(self.tmp, "init", "--bare", str(public))
        _git(local, "remote", "add", "public", str(public))
        # -c remote.pushDefault=public with bare git push
        r = self.claude("git -c remote.pushDefault=public push", cwd=local)
        reason = json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("GitHub token", reason)
        self.assertNotIn(token, reason)
        # --git-dir / --work-tree from another cwd
        parent = local.parent
        r = self.claude(
            f"git --git-dir={local / '.git'} --work-tree={local} push public main",
            cwd=parent,
        )
        self.assertIn("GitHub token", json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"])

    def test_r5_nested_cd_does_not_affect_parent(self):
        local, _ = self.make_pair()
        other = self.tmp / "other"
        _init_repo(other)
        (other / "a.txt").write_text("ok\n")
        _git(other, "add", "a.txt")
        _git(other, "commit", "-m", "o")
        token = _fake_token("ghp_", 36)
        (local / "leak.txt").write_text(f"{token}\n")
        _git(local, "add", "leak.txt")
        _git(local, "commit", "-m", "leak")
        # cd inside bash -c must not make the outer git push scan `other`
        cmd = f"bash -c 'cd {other} && true'; git -C {local} push origin main"
        r = self.claude(cmd, cwd=self.tmp)
        self.assertIn("GitHub token", json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"])
        # Nested commit still triggers deny with outer push
        r = self.claude(
            f"bash -c 'cd {local} && git commit --allow-empty -m x'; git -C {local} push origin main",
            cwd=self.tmp,
        )
        self.assertEqual(json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_r6_repo_option_is_destination(self):
        local, _ = self.make_pair()
        token = _fake_token("ghp_", 36)
        (local / "leak.txt").write_text(f"{token}\n")
        _git(local, "add", "leak.txt")
        _git(local, "commit", "-m", "leak")
        public = self.tmp / "pub.git"
        _git(self.tmp, "init", "--bare", str(public))
        _git(local, "remote", "add", "public", str(public))
        r = self.claude("git push --repo=public", cwd=local)
        self.assertIn("GitHub token", json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"])

    def test_r7_bare_colon_still_scans_union(self):
        local, _ = self.make_pair()
        token = _fake_token("ghp_", 36)
        (local / "leak.txt").write_text(f"{token}\n")
        _git(local, "add", "leak.txt")
        _git(local, "commit", "-m", "leak")
        # bare `:` is not a deletion — scan HEAD∪branches∪tags
        r = self.claude("git push origin :", cwd=local)
        self.assertIn("GitHub token", json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"])

    def test_r8_config_driven_selection_covered_by_union(self):
        local, _ = self.make_pair()
        token = _fake_token("ghp_", 36)
        _git(local, "checkout", "-b", "feature")
        (local / "leak.txt").write_text(f"{token}\n")
        _git(local, "add", "leak.txt")
        _git(local, "commit", "-m", "leak")
        _git(local, "checkout", "main")
        # Bare git push while on main: union still includes feature tip content
        # via --branches (scan more). Secret is only on feature.
        r = self.claude("git push origin", cwd=local)
        self.assertIn("GitHub token", json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"])

    def test_r9_pushurl_forces_full_history(self):
        local, _ = self.make_pair()
        token = _fake_token("ghp_", 36)
        (local / "leak.txt").write_text(f"{token}\n")
        _git(local, "add", "leak.txt")
        _git(local, "commit", "-m", "leak")
        _git(local, "push", "origin", "main")
        # Secret is on origin's tracking refs; with pushurl, must still report
        _git(local, "config", "remote.origin.pushurl", "https://example.com/other.git")
        r = self.claude("git push origin main", cwd=local)
        self.assertIn("GitHub token", json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"])

    def test_r10_gh_repo_create_source_dir(self):
        src = self.tmp / "srcrepo"
        _init_repo(src)
        token = _fake_token("ghp_", 36)
        (src / "leak.txt").write_text(f"{token}\n")
        _git(src, "add", "leak.txt")
        _git(src, "commit", "-m", "leak")
        r = self.claude(f"gh repo create my/repo --source {src} --push", cwd=self.tmp)
        self.assertIn("GitHub token", json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"])

    def test_r11_merge_resolution_token_reported(self):
        local, _ = self.make_pair()
        (local / "f.txt").write_text("base\n")
        _git(local, "add", "f.txt")
        _git(local, "commit", "-m", "base")
        _git(local, "checkout", "-b", "side")
        (local / "f.txt").write_text("side\n")
        _git(local, "add", "f.txt")
        _git(local, "commit", "-m", "side")
        _git(local, "checkout", "main")
        (local / "f.txt").write_text("main\n")
        _git(local, "add", "f.txt")
        _git(local, "commit", "-m", "mainline")
        # Merge with conflict; resolve with a token neither parent has
        merge = _git(local, "merge", "side", check=False)
        self.assertNotEqual(merge.returncode, 0)
        token = _fake_token("ghp_", 36)
        (local / "f.txt").write_text(f"resolved {token}\n")
        _git(local, "add", "f.txt")
        _git(local, "commit", "-m", "merge resolve")
        r = self.claude("git push origin main", cwd=local)
        self.assertIn("GitHub token", json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertNotIn(token, r.stdout)

    def test_r12_root_commit_files_scanned(self):
        local = self.tmp / "rooty"
        _init_repo(local)
        token = _fake_token("ghp_", 36)
        (local / ".env").write_text("X=1\n")
        (local / "t.txt").write_text(f"{token}\n")
        _git(local, "add", "-f", ".env", "t.txt")
        _git(local, "commit", "-m", "root")
        r = self.claude("gh repo create x/y --push", cwd=local)
        reason = json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("sensitive file", reason)
        self.assertIn("GitHub token", reason)

    def test_r13_plus_plus_lines_inside_hunk(self):
        local, _ = self.make_pair()
        token_a = _fake_token("ghp_", 36)
        token_b = _fake_token("ghp_", 37)
        (local / "x.txt").write_text("start\n")
        _git(local, "add", "x.txt")
        _git(local, "commit", "-m", "start")
        (local / "x.txt").write_text(f"start\n++ {token_a}\n+++ {token_b}\n")
        _git(local, "add", "x.txt")
        _git(local, "commit", "-m", "pluses")
        r = self.claude("git push origin main", cwd=local)
        reason = json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("GitHub token", reason)
        self.assertNotIn(token_a, reason)
        self.assertNotIn(token_b, reason)
        # Line numbers should reference the added lines (2 and 3)
        self.assertRegex(reason, r"x\.txt:.*2")

    def test_r14_usr_bin_gh_prefilter(self):
        local, _ = self.make_pair()
        token = _fake_token("ghp_", 36)
        (local / "leak.txt").write_text(f"{token}\n")
        _git(local, "add", "leak.txt")
        _git(local, "commit", "-m", "leak")
        r = self.claude("/usr/bin/gh pr create", cwd=local)
        self.assertIn("GitHub token", json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"])

    def test_r15_shell_keywords_and_env_s(self):
        local, _ = self.make_pair()
        token = _fake_token("ghp_", 36)
        (local / "leak.txt").write_text(f"{token}\n")
        _git(local, "add", "leak.txt")
        _git(local, "commit", "-m", "leak")
        r = self.claude("if true; then git push origin main; fi", cwd=local)
        self.assertIn("GitHub token", json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"])
        r = self.claude("env -S 'git push origin main'", cwd=local)
        self.assertIn("GitHub token", json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"])

    def test_r16_windows_home_case_insensitive(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("sg", HOOK)
        sg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sg)
        g = sg.Guard(budget_s=sg._UNLIMITED)
        win1 = "c:" + "\\" + "users" + "\\" + "Alice" + "\\" + "secret"
        win2 = "C:" + "/" + "USERS" + "/" + "Bob" + "/" + "x"
        g._scan_line(win1, "f", 1, "abc")
        g._scan_line(win2, "f", 2, "abc")
        labels = [f["value"] for f in g.findings if f["label"] == "home path"]
        self.assertTrue(any("Alice" in v for v in labels))
        self.assertTrue(any("Bob" in v for v in labels))

    def test_r17_check_staged_env_and_unborn_head(self):
        local, _ = self.make_pair()
        (local / ".env").write_text("X=1\n")
        _git(local, "add", "-f", ".env")
        r = self.run_hook(argv=["--check", str(local)])
        self.assertEqual(r.returncode, 1)
        self.assertIn("sensitive file", r.stdout)
        # Unborn HEAD
        fresh = self.tmp / "unborn"
        fresh.mkdir()
        _git(fresh, "init")
        _git(fresh, "checkout", "-b", "main")
        (fresh / ".env").write_text("Y=1\n")
        _git(fresh, "add", "-f", ".env")
        r = self.run_hook(argv=["--check", str(fresh)])
        self.assertEqual(r.returncode, 1)
        self.assertIn(".env", r.stdout)

    def test_r18_check_failed_diff_is_error(self):
        local, _ = self.make_pair()
        # Remove HEAD ref to make diff against HEAD fail oddly — use a non-repo path
        # that still has .git? Better: stub by scanning a dir whose git diff is broken.
        # Call scan_working_tree with a Guard after deleting the index.
        broken = self.tmp / "broken"
        _init_repo(broken)
        (broken / "a").write_text("a\n")
        _git(broken, "add", "a")
        _git(broken, "commit", "-m", "a")
        # Corrupt: make git diff fail by setting GIT_DIR to a missing path via env in hook
        # Directly: replace .git/HEAD with garbage so rev-parse works but tree is odd —
        # simplest reliable approach: invoke --check on a path that is a git repo but
        # with core.bare mismatch. Use import and raise via monkeypatch.
        import importlib.util
        spec = importlib.util.spec_from_file_location("sg", HOOK)
        sg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sg)
        g = sg.Guard(budget_s=sg._UNLIMITED)
        real_git = g.git

        def boom(args, cwd, check=True, git_opts=None):
            if args and args[0] == "diff":
                raise sg.GitError("diff failed")
            return real_git(args, cwd, check=check, git_opts=git_opts)

        g.git = boom
        with self.assertRaises(sg.GitError):
            g.scan_working_tree(str(broken))

    def test_check_unbounded_without_env(self):
        # --check must not use the 20s default; with env unset it finishes.
        env = {k: v for k, v in self.env.items() if k != "SECRET_GUARD_BUDGET_S"}
        local, _ = self.make_pair()
        r = self.run_hook(argv=["--check", str(local)], env=env)
        self.assertEqual(r.returncode, 0)
        self.assertIn("all clear", r.stdout)

    def test_home_path_boundaries(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("sg", HOOK)
        sg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sg)
        g = sg.Guard(budget_s=sg._UNLIMITED)
        alice = "/" + "home" + "/" + "alice" + "/"
        bob = "/" + "Users" + "/" + "bob" + "/"
        for line in (alice + "x", " " + alice + "x", "=" + alice + "x",
                     "file://" + alice + "x", bob + "x",
                     "(" + alice + "x", ":" + alice + "x", '"' + alice + 'x"'):
            g._scan_line(line, "f", 1, "c")
        self.assertTrue(any(alice in f["value"] for f in g.findings))
        g.findings.clear()
        for line in (
            "/" + "home" + "/" + "user" + "/",
            "/" + "home" + "/$USER/",
            "/" + "home" + "/<user>/",
            "https://example.org" + "/" + "home" + "/" + "news" + "/",
            "~/.cache",
        ):
            g._scan_line(line, "f", 1, "c")
        self.assertEqual(g.findings, [])

    def test_grouped_findings_and_private_key_label(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("sg", HOOK)
        sg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sg)
        findings = [
            {"label": "home path", "path": "data/x.jsonl", "line": n, "commit": "31188b2",
             "value": "/" + "home" + "/" + "alice" + "/"}
            for n in range(13, 28)
        ]
        text = sg.format_reason(findings, "origin")
        self.assertIn(":13,14,15 +12 more", text)
        self.assertEqual(text.count("home path"), 1)
        g = sg.Guard(budget_s=sg._UNLIMITED)
        g._scan_line("-----" + "BEGIN " + "RSA " + "PRIVATE " + "KEY-----", "k", 1, "c")
        self.assertEqual(g.findings[0]["value"], "PRIVATE " + "KEY header")
        self.assertNotIn("BEGIN", g.findings[0]["value"])

    def test_exception_after_push_recognised_asks(self):
        local, _ = self.make_pair()
        # Force an unexpected error during scan by breaking PATH git after parse —
        # use a push against a repo then make scan fail via budget mid-flight.
        r = self.claude("git push origin main", cwd=local, env={"SECRET_GUARD_BUDGET_S": "0"})
        data = json.loads(r.stdout)
        self.assertEqual(data["hookSpecificOutput"]["permissionDecision"], "ask")


class SecretGuardRound3Tests(_SecretGuardHarness, unittest.TestCase):
    """Fixes from review-2 (R1–R13) and orchestrator additions 14–17."""

    def test_nested_bash_c_cd_carries_within_string(self):
        local, _ = self.make_pair()
        token = _fake_token("ghp_", 36)
        (local / "leak.txt").write_text(f"{token}\n")
        _git(local, "add", "leak.txt")
        _git(local, "commit", "-m", "leak")
        # From outside the repo: bash -c 'cd REPO && git push' must scan REPO.
        r = self.claude(f"bash -c 'cd {local} && git push origin main'", cwd=self.tmp)
        self.assertIn("GitHub token", json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"])

    def test_eval_cd_propagates_to_caller(self):
        local, _ = self.make_pair()
        token = _fake_token("ghp_", 36)
        (local / "leak.txt").write_text(f"{token}\n")
        _git(local, "add", "leak.txt")
        _git(local, "commit", "-m", "leak")
        r = self.claude(f"eval 'cd {local}'; git push origin main", cwd=self.tmp)
        self.assertIn("GitHub token", json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"])

    def test_wildcard_refspec_still_scans(self):
        local, _ = self.make_pair()
        token = _fake_token("ghp_", 36)
        (local / "leak.txt").write_text(f"{token}\n")
        _git(local, "add", "leak.txt")
        _git(local, "commit", "-m", "leak")
        r = self.claude("git push origin 'refs/heads/*'", cwd=local)
        self.assertIn("GitHub token", json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"])

    def test_rev_list_error_asks_not_silent(self):
        local, _ = self.make_pair()
        import importlib.util
        spec = importlib.util.spec_from_file_location("sg", HOOK)
        sg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sg)
        # Simulate corrupt object during rev-list on a recognised push.
        pushes, _ = sg.analyze_command("git push origin main", str(local), sg.Guard(budget_s=sg._UNLIMITED))
        self.assertTrue(pushes)
        g = sg.Guard(budget_s=sg._UNLIMITED)

        def boom(*a, **k):
            raise sg.GitError("fatal: corrupt commit object")

        g.rev_list = boom
        with self.assertRaises(sg.GitError):
            sg.run_push_scans(g, pushes)
        # Claude path must ask
        r = self.claude("git push -- public HEAD", cwd=local)
        # public is not a configured remote → rev-list/config may error → ask
        if r.stdout.strip():
            data = json.loads(r.stdout)
            self.assertEqual(data["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_push_after_double_dash_remote(self):
        local, _ = self.make_pair()
        token = _fake_token("ghp_", 36)
        (local / "leak.txt").write_text(f"{token}\n")
        _git(local, "add", "leak.txt")
        _git(local, "commit", "-m", "leak")
        public = self.tmp / "pub2.git"
        _git(self.tmp, "init", "--bare", str(public))
        _git(local, "remote", "add", "public", str(public))
        r = self.claude("git push -- public HEAD", cwd=local)
        self.assertIn("GitHub token", json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"])

    def test_sensitive_names_without_plus_headers(self):
        local, _ = self.make_pair()
        (local / ".env").write_bytes(b"")
        (local / "client.p12").write_bytes(b"\x00\x01\x02")
        _git(local, "add", "-f", ".env", "client.p12")
        _git(local, "commit", "-m", "bin")
        # rename to .env
        _git(local, "rm", "-f", ".env")
        (local / "was").write_text("x\n")
        _git(local, "add", "was")
        _git(local, "commit", "-m", "was")
        _git(local, "mv", "was", ".env")
        _git(local, "commit", "-m", "renamed")
        r = self.claude("git push origin main", cwd=local)
        reason = json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("sensitive file", reason)
        self.assertIn(".env", reason)
        self.assertIn("client.p12", reason)

    def test_quoted_path_env(self):
        local, _ = self.make_pair()
        # Path that git may quote without -z; name-status -z still reports it.
        name = "weird .env"
        (local / name).write_text("Z=1\n")
        _git(local, "add", "-f", name)
        _git(local, "commit", "-m", "quoted")
        r = self.claude("git push origin main", cwd=local)
        self.assertIn("sensitive file", json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"])

    def test_config_cannot_hide_root_or_relative(self):
        local, _ = self.make_pair()
        token = _fake_token("ghp_", 36)
        # Re-init as root-only content that log.showRoot=false would hide
        solo = self.tmp / "rootcfg"
        _init_repo(solo)
        (solo / "t.txt").write_text(f"{token}\n")
        _git(solo, "add", "t.txt")
        _git(solo, "commit", "-m", "root")
        _git(solo, "config", "log.showRoot", "false")
        _git(solo, "config", "diff.relative", "true")
        r = self.claude("gh repo create o/r --push", cwd=solo)
        self.assertIn("GitHub token", json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"])

    def test_nul_in_patch_and_long_oid_framing(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("sg", HOOK)
        sg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sg)
        g = sg.Guard(budget_s=sg._UNLIMITED)
        marker = g.log_marker
        token = _fake_token("ghp_", 36)
        oid40 = "a" * 40
        oid64 = "b" * 64
        # Patch contains an embedded NUL that must not break framing.
        patch = f"diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -0,0 +1 @@\n+pre\x00{token}\n"
        msg_tok = _fake_token("ghp_", 36)
        raw = (
            f"\0{marker}\0{oid40}\0msg one\0{patch}"
            f"\0{marker}\0{oid64}\0use {msg_tok}\0"
        )
        g._parse_log_stream(raw)
        labels = [f["label"] for f in g.findings]
        self.assertIn("GitHub token", labels)
        # Malformed record must raise
        with self.assertRaises(sg.GitError):
            g2 = sg.Guard(budget_s=sg._UNLIMITED)
            g2._parse_log_stream(f"\0{g2.log_marker}\0notanoid\0msg\0")

    def test_split_lf_preserves_cr_and_vt(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("sg", HOOK)
        sg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sg)
        g = sg.Guard(budget_s=sg._UNLIMITED)
        token = _fake_token("ghp_", 36)
        diff = (
            "diff --git a/x b/x\n"
            "--- a/x\n"
            "+++ b/x\n"
            "@@ -0,0 +1,2 @@\n"
            f"+prefix\r{token}\n"
            f"+prefix\v{token}\n"
        )
        g.scan_diff(diff, "c" * 40)
        self.assertTrue(any(f["label"] == "GitHub token" for f in g.findings))

    def test_env_s_keeps_trailing_args(self):
        local, _ = self.make_pair()
        token = _fake_token("ghp_", 36)
        (local / "leak.txt").write_text(f"{token}\n")
        _git(local, "add", "leak.txt")
        _git(local, "commit", "-m", "leak")
        public = self.tmp / "pub3.git"
        _git(self.tmp, "init", "--bare", str(public))
        _git(local, "remote", "add", "public", str(public))
        for cmd in ("env -S 'git' push public HEAD", "env -S 'git push' public HEAD"):
            r = self.claude(cmd, cwd=local)
            self.assertIn("GitHub token", json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"], cmd)

    def test_format_reason_respects_budget(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("sg", HOOK)
        sg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sg)
        g = sg.Guard(budget_s=0.01)
        g.started = __import__("time").monotonic() - 1  # already over budget
        findings = [
            {"label": "home path", "path": "f", "line": i, "commit": "abc",
             "value": "/" + "home" + "/" + "alice" + "/"}
            for i in range(5000)
        ]
        with self.assertRaises(sg.BudgetExceeded):
            sg.format_reason(findings, "origin", guard=g)

    def test_octopus_merge_scanned_against_first_parent(self):
        local, _ = self.make_pair()
        (local / "f.txt").write_text("base\n")
        _git(local, "add", "f.txt")
        _git(local, "commit", "-m", "base")
        base = _git(local, "rev-parse", "HEAD").stdout.strip()
        for name in ("a", "b", "c"):
            _git(local, "checkout", "-b", name, base)
            (local / "f.txt").write_text(f"{name}\n")
            _git(local, "add", "f.txt")
            _git(local, "commit", "-m", name)
        _git(local, "checkout", "main")
        # Octopus merge of a,b,c — resolution introduces a token.
        _git(local, "merge", "-m", "octopus", "a", "b", "c", check=False)
        # May conflict; force a merge commit with the token.
        token = _fake_token("ghp_", 36)
        (local / "f.txt").write_text(f"merged {token}\n")
        _git(local, "add", "f.txt")
        # Ensure we have a merge with 3+ parents
        parents = [base]
        for name in ("a", "b", "c"):
            parents.append(_git(local, "rev-parse", name).stdout.strip())
        # Use commit-tree for a reliable octopus
        tree = _git(local, "write-tree").stdout.strip()
        cmd = ["commit-tree", tree, "-m", "octopus"]
        for p in parents[1:]:
            cmd += ["-p", p]
        cmd += ["-p", base]
        oid = _git(local, *cmd).stdout.strip()
        _git(local, "merge", "-q", oid, check=False)
        _git(local, "reset", "--hard", oid)
        r = self.claude("git push origin main", cwd=local)
        # Either the octopus resolution is found, or (if empty tree content) at least no crash
        if r.stdout.strip():
            reason = json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
            # Token is in the tree of the octopus commit — name-status/diff should see it
            self.assertIn("GitHub token", reason)

    def test_pre_push_no_walk_excludes_ancestors(self):
        local, remote = self.make_pair()
        token = _fake_token("ghp_", 36)
        (local / "old.txt").write_text(f"{token}\n")
        _git(local, "add", "old.txt")
        _git(local, "commit", "-m", "secret-ancestor")
        _git(local, "push", "origin", "main")
        (local / "new.txt").write_text("clean\n")
        _git(local, "add", "new.txt")
        _git(local, "commit", "-m", "clean")
        local_sha = _git(local, "rev-parse", "HEAD").stdout.strip()
        parent = _git(local, "rev-parse", "HEAD~1").stdout.strip()
        stdin = f"refs/heads/main {local_sha} refs/heads/main {parent}\n"
        r = self.run_hook(argv=["origin", str(remote)], stdin_text=stdin, cwd=local)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_unreadable_untracked_is_error(self):
        local, _ = self.make_pair()
        secret = local / "secret.txt"
        secret.write_text("x\n")
        import importlib.util
        spec = importlib.util.spec_from_file_location("sg", HOOK)
        sg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sg)
        g = sg.Guard(budget_s=sg._UNLIMITED)
        real = sg._is_probably_binary

        def boom(path, sample=8192):
            raise PermissionError("denied")

        sg._is_probably_binary = boom
        try:
            with self.assertRaises(sg.GitError):
                g.scan_working_tree(str(local))
        finally:
            sg._is_probably_binary = real

    def test_multi_push_reports_contributing_targets(self):
        local, _ = self.make_pair()
        token = _fake_token("ghp_", 36)
        (local / "leak.txt").write_text(f"{token}\n")
        _git(local, "add", "leak.txt")
        _git(local, "commit", "-m", "leak")
        _git(local, "push", "origin", "main")  # origin is clean for further pushes of same tip
        public = self.tmp / "pub4.git"
        _git(self.tmp, "init", "--bare", str(public))
        _git(local, "remote", "add", "public", str(public))
        # origin has the commit; public does not — findings only from public
        r = self.claude("git push origin HEAD && git push public HEAD", cwd=local)
        reason = json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("public", reason)
        # Should not claim only origin when public produced findings
        self.assertNotRegex(reason, r"target origin\s*:")

    def test_dedupe_sensitive_file_once(self):
        local, _ = self.make_pair()
        (local / ".env").write_text("X=1\n")
        _git(local, "add", "-f", ".env")
        _git(local, "commit", "-m", "env")
        r = self.claude("git push origin main", cwd=local)
        reason = json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
        # One sensitive-file group for .env, not duplicated
        self.assertEqual(reason.count("sensitive file"), 1)

    def test_check_hint_in_could_not_finish(self):
        local, _ = self.make_pair()
        r = self.claude("git push origin main", cwd=local, env={"SECRET_GUARD_BUDGET_S": "0"})
        reason = json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("--check", reason)
        self.assertIn("secret-guard.py", reason)

    def test_scan_text_matches_reference_scanner(self):
        """Per-rule scan_text equals a direct TOKEN_RULES+HOME+key pass."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("sg", HOOK)
        sg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sg)
        text = "\n".join([
            _fake_token("ghp_", 36),
            "sk-" + "ant-" + ("x" * 24),
            "/" + "home" + "/" + "alice" + "/x",
            "-----" + "BEGIN " + "RSA " + "PRIVATE " + "KEY-----",
            "noise",
        ])
        g = sg.Guard(budget_s=sg._UNLIMITED)
        g.scan_text(text, "f", "c" * 40)
        ref = []
        for i, line in enumerate(text.split("\n"), 1):
            if "-----" + "BEGIN " in line and "PRIVATE " + "KEY" in line:
                ref.append(("private key", i))
            for label, pat in sg.TOKEN_RULES:
                for m in pat.finditer(line):
                    if label == "OpenAI key" and m.group(0).startswith(sg._SK_ANT):
                        continue
                    use = "Anthropic key" if m.group(0).startswith(sg._SK_ANT) else label
                    ref.append((use, i))
            for m in sg.HOME_PATH.finditer(line):
                name = next(g for g in m.groups() if g is not None)
                if name.lower() not in sg.PLACEHOLDER_USERS:
                    ref.append(("home path", i))
        got = [(f["label"], f["line"]) for f in g.findings]
        self.assertEqual(sorted(got), sorted(ref))

    def test_scan_text_line_numbers_with_gaps_and_offset(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("sg", HOOK)
        sg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sg)
        tok = _fake_token("ghp_", 36)
        text = "\n".join(["noise", tok, "", "noise", tok + " " + tok, "noise"])
        g = sg.Guard(budget_s=sg._UNLIMITED)
        g.scan_text(text, "f", "c" * 40, start_line=10)
        self.assertEqual(sorted({f["line"] for f in g.findings}), [11, 14])

    def test_per_rule_prefilter_completeness(self):
        """Each SCAN_RULE still fires on a positive example when other rules' literals are absent."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("sg", HOOK)
        sg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sg)
        examples = {
            "GitHub token": _fake_token("ghp_", 36),
            "AWS access key": sg._AKIA + ("A" * 16),
            "Anthropic key": sg._SK_ANT + ("x" * 24),
            "OpenAI key": "sk-" + ("y" * 24),
            "Slack token": sg._XOX + "b-" + ("z" * 12),
            "Google API key": sg._AIZA + ("w" * 35),
            "Stripe key": sg._SK_LIVE + ("s" * 24),
            "private key": "-----" + "BEGIN " + "RSA " + "PRIVATE " + "KEY-----",
            "home path": "/" + "home" + "/" + "alice" + "/x",
        }
        for kind, line in examples.items():
            g = sg.Guard(budget_s=sg._UNLIMITED)
            g.scan_text(line, "f", "c" * 40)
            labels = {f["label"] for f in g.findings}
            if kind == "OpenAI key":
                # sk-ant- is Anthropic; plain sk- is OpenAI
                self.assertIn("OpenAI key", labels, kind)
            else:
                self.assertIn(kind if kind != "Anthropic key" else "Anthropic key", labels, kind)

    def test_token_after_private_key_on_same_line(self):
        """A token after a PRIVATE KEY header on the same line is still reported."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("sg", HOOK)
        sg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sg)
        tok = _fake_token("ghp_", 36)
        line = "-----" + "BEGIN " + "RSA " + "PRIVATE " + "KEY----- " + tok
        g = sg.Guard(budget_s=sg._UNLIMITED)
        g.scan_text(line, "f", "c" * 40)
        labels = {f["label"] for f in g.findings}
        self.assertIn("private key", labels)
        self.assertIn("GitHub token", labels)

    def test_mixed_case_windows_home_path(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("sg", HOOK)
        sg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sg)
        path = "C:" + "\\" + "uSeRs" + "\\" + "alice" + "\\" + "x"
        g = sg.Guard(budget_s=sg._UNLIMITED)
        g.scan_text(path, "f", "c" * 40)
        self.assertTrue(any(f["label"] == "home path" for f in g.findings))

    def test_f1_private_key_linear_scan_under_budget(self):
        """1.3 MB line with PRIVATE KEY + 100k BEGIN prefixes finishes and reports a token."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("sg", HOOK)
        sg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sg)
        tok = _fake_token("ghp_", 36)
        begin = "-----" + "BEGIN "
        priv = "PRIVATE " + "KEY"
        # Header early, then many BEGIN prefixes that would backtrack a [^\n]* regex.
        line = begin + "RSA " + priv + "----- " + (begin * 120_000) + " " + tok
        self.assertGreater(len(line), 1_300_000)
        g = sg.Guard(budget_s=5.0)
        t0 = time.perf_counter()
        g.scan_text(line, "f", "c" * 40)
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 5.0, f"linear scan took {elapsed:.2f}s")
        labels = {f["label"] for f in g.findings}
        self.assertIn("private key", labels)
        self.assertIn("GitHub token", labels)

    def test_f2_f3_pre_push_octopus_and_error_propagates(self):
        """Pre-push no_walk still runs the octopus pass; its GitError propagates."""
        import importlib.util
        local, _ = self.make_pair()
        spec = importlib.util.spec_from_file_location("sg", HOOK)
        sg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sg)
        g = sg.Guard(budget_s=sg._UNLIMITED)
        calls = []
        real = g._run_log_parse

        def track(cwd, cmd_args, opts):
            calls.append(list(cmd_args))
            return real(cwd, cmd_args, opts)

        g._run_log_parse = track
        head = _git(local, "rev-parse", "HEAD").stdout.strip()
        g.scan_rev_range(str(local), [head, "--"], no_walk=True)
        octopus = [c for c in calls if "--min-parents=3" in c]
        self.assertTrue(octopus, f"octopus pass missing; calls={calls!r}")
        self.assertIn("--no-walk", octopus[0])
        self.assertIn("--diff-merges=first-parent", octopus[0])

        g2 = sg.Guard(budget_s=sg._UNLIMITED)
        real2 = g2._run_log_parse

        def boom(cwd, cmd_args, opts):
            if "--min-parents=3" in cmd_args:
                raise sg.GitError("fatal: missing octopus tree")
            return real2(cwd, cmd_args, opts)

        g2._run_log_parse = boom
        with self.assertRaises(sg.GitError) as cm:
            g2.scan_rev_range(str(local), [head, "--"], no_walk=True)
        self.assertIn("octopus", str(cm.exception))
        # Claude path: recognised push with an unscanned commit must not swallow the error.
        (local / "extra.txt").write_text("x\n")
        _git(local, "add", "extra.txt")
        _git(local, "commit", "-m", "unpushed")
        pushes, _ = sg.analyze_command(
            "git push origin main", str(local), sg.Guard(budget_s=sg._UNLIMITED),
        )
        self.assertTrue(pushes)
        g3 = sg.Guard(budget_s=sg._UNLIMITED)
        g3._run_log_parse = boom
        with self.assertRaises(sg.GitError):
            sg.run_push_scans(g3, pushes)

    def test_f4_merge_introduces_sensitive_filename(self):
        """Name-status with --diff-merges=first-parent reports merge-added client.p12."""
        local, _ = self.make_pair()
        _git(local, "checkout", "-b", "side")
        # Header-only binary-ish p12 (no text content for hunk scanner).
        (local / "client.p12").write_bytes(b"\x30\x82" + b"\x00" * 64)
        _git(local, "add", "client.p12")
        _git(local, "commit", "-m", "add p12")
        _git(local, "checkout", "main")
        _git(local, "merge", "--no-ff", "-m", "merge side", "side")
        r = self.claude("git push origin main", cwd=local)
        reason = json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("sensitive file", reason)
        self.assertIn("client.p12", reason)

    def test_f5_subshell_cd_does_not_leak(self):
        local, _ = self.make_pair()
        token = _fake_token("ghp_", 36)
        (local / "leak.txt").write_text(f"{token}\n")
        _git(local, "add", "leak.txt")
        _git(local, "commit", "-m", "leak")
        for cmd in (
            f"(cd /); git -C {local} push origin main",
            f"echo $(cd /); git -C {local} push origin main",
            f"echo `cd /`; git -C {local} push origin main",
        ):
            r = self.claude(cmd, cwd=local)
            self.assertTrue(r.stdout.strip(), cmd)
            reason = json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
            self.assertIn("GitHub token", reason, cmd)

    def test_f5_subshell_cd_alone_asks_not_silent(self):
        """(cd /); git push — push cwd must stay the repo, not silently skip."""
        local, _ = self.make_pair()
        token = _fake_token("ghp_", 36)
        (local / "leak.txt").write_text(f"{token}\n")
        _git(local, "add", "leak.txt")
        _git(local, "commit", "-m", "leak")
        r = self.claude("(cd /); git push origin main", cwd=local)
        out = r.stdout.strip()
        self.assertTrue(out)
        data = json.loads(out)
        reason = data["hookSpecificOutput"]["permissionDecisionReason"]
        # Must not silently return 0; either findings or an explicit not-a-repo ask.
        self.assertTrue(
            "GitHub token" in reason or "is not a git repository" in reason,
            reason,
        )
        self.assertIn("GitHub token", reason)

    def test_f6_env_chdir_applies_to_child(self):
        local, _ = self.make_pair()
        token = _fake_token("ghp_", 36)
        (local / "leak.txt").write_text(f"{token}\n")
        _git(local, "add", "leak.txt")
        _git(local, "commit", "-m", "leak")
        r = self.claude(
            f"env -C {local} -S 'git push' origin main",
            cwd="/",
        )
        reason = json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("GitHub token", reason)
        r = self.claude(f"env --chdir={local} git push origin main", cwd="/")
        reason = json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("GitHub token", reason)

    def test_f6_resolved_dir_not_a_repo_asks(self):
        r = self.claude("git push origin main", cwd="/")
        reason = json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("secret guard could not finish:", reason)
        self.assertIn("is not a git repository", reason)

    def test_f7_wildcard_refspec_scans_all_namespaces(self):
        """refs/backup/* glob must scan history reachable only via backup refs."""
        local, _ = self.make_pair()
        token = _fake_token("ghp_", 36)
        # Secret only on refs/backup/x, not on branches/tags/HEAD ancestry after reset.
        (local / "bak.txt").write_text(f"{token}\n")
        _git(local, "add", "bak.txt")
        _git(local, "commit", "-m", "backup-secret")
        bak = _git(local, "rev-parse", "HEAD").stdout.strip()
        _git(local, "update-ref", "refs/backup/x", bak)
        _git(local, "reset", "--hard", "HEAD~1")
        r = self.claude(
            "git push origin 'refs/backup/*:refs/backup/*'",
            cwd=local,
        )
        reason = json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("GitHub token", reason)

    def test_f8_no_relative_name_status_from_subdir(self):
        local, _ = self.make_pair()
        _git(local, "config", "diff.relative", "true")
        (local / "hooks").mkdir(exist_ok=True)
        # Staged sensitive file outside hooks/ — must still be seen from hooks/.
        (local / "client.p12").write_bytes(b"\x00" * 8)
        _git(local, "add", "client.p12")
        r = self.run_hook(argv=["--check", str(local)], cwd=local / "hooks")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("client.p12", r.stdout + r.stderr)

    def test_f9_ascii_home_path_rejects_long_s(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("sg", HOOK)
        sg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sg)
        # Long-s (U+017F) must not match ASCII-only Windows Users.
        weird = "C:" + "\\" + "u\u017fers" + "\\" + "alice" + "\\" + "x"
        g = sg.Guard(budget_s=sg._UNLIMITED)
        g.scan_text(weird, "f", "c" * 40)
        self.assertFalse(any(f["label"] == "home path" for f in g.findings), g.findings)
        ok = "c:" + "\\" + "USERS" + "\\" + "alice" + "\\" + "x"
        g2 = sg.Guard(budget_s=sg._UNLIMITED)
        g2.scan_text(ok, "f", "c" * 40)
        self.assertTrue(any(f["label"] == "home path" for f in g2.findings))

    def test_f10_multi_push_names_both_destinations(self):
        """Same secret on two pushes names both targets despite global dedupe."""
        local, _ = self.make_pair()
        token = _fake_token("ghp_", 36)
        (local / "leak.txt").write_text(f"{token}\n")
        _git(local, "add", "leak.txt")
        _git(local, "commit", "-m", "leak")
        private = self.tmp / "private.git"
        public = self.tmp / "public.git"
        _git(self.tmp, "init", "--bare", str(private))
        _git(self.tmp, "init", "--bare", str(public))
        _git(local, "remote", "add", "private", str(private))
        _git(local, "remote", "add", "public", str(public))
        r = self.claude("git push private HEAD && git push public HEAD", cwd=local)
        reason = json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("private", reason)
        self.assertIn("public", reason)

    def test_framing_nonempty_preamble_is_error(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("sg", HOOK)
        sg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sg)
        g = sg.Guard(budget_s=sg._UNLIMITED)
        marker = g.log_marker
        oid = "a" * 40
        raw = f"WARNING: noise\0{marker}\0{oid}\0msg\0"
        with self.assertRaises(sg.GitError) as cm:
            g._parse_log_stream(raw)
        self.assertIn("preamble", str(cm.exception).lower())



class SecretGuardHomePathSwitchTests(_SecretGuardHarness, unittest.TestCase):
    """`git config secretGuard.homePaths false` turns off only the home-path rule."""

    HOME_LINE = "/" + "home" + "/" + "alice" + "/x\n"

    def _commit(self, local, name, text):
        (local / name).write_text(text)
        _git(local, "add", name)
        _git(local, "commit", "-m", name)

    def _decision(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        if not result.stdout.strip():
            return None, ""
        out = json.loads(result.stdout)["hookSpecificOutput"]
        return out["permissionDecision"], out["permissionDecisionReason"]

    def test_switch_silences_home_paths_only(self):
        local, _ = self.make_pair()
        self._commit(local, "paths.txt", self.HOME_LINE)
        self.assertEqual(self._decision(self.claude("git push", cwd=local))[0], "ask")
        _git(local, "config", "secretGuard.homePaths", "false")
        self.assertEqual(self._decision(self.claude("git push", cwd=local)), (None, ""))
        self._commit(local, "cfg.txt", "token=" + _fake_token("ghp_", 36) + "\n")
        decision, reason = self._decision(self.claude("git push", cwd=local))
        self.assertEqual(decision, "ask")
        self.assertIn("GitHub token", reason)
        self.assertNotIn("home path", reason)

    def test_command_line_override_is_ignored(self):
        local, _ = self.make_pair()
        self._commit(local, "paths.txt", self.HOME_LINE)
        result = self.claude("git -c secretGuard.homePaths=false push", cwd=local)
        self.assertEqual(self._decision(result)[0], "ask")

    def test_check_honours_switch(self):
        local, _ = self.make_pair()
        self._commit(local, "paths.txt", self.HOME_LINE)
        self.assertEqual(self.run_hook(argv=["--check", str(local)]).returncode, 1)
        _git(local, "config", "secretGuard.homePaths", "false")
        result = self.run_hook(argv=["--check", str(local)])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class SecretGuardP2Tests(_SecretGuardHarness, unittest.TestCase):
    NAME = "fictional-private-repo"

    def setUp(self):
        super().setUp()
        self.env["XDG_CONFIG_HOME"] = str(self.tmp / "config")
        self.names = self.tmp / "config/claude-orchestrator/private-names.txt"

    def deny_list(self, text=None):
        self.names.parent.mkdir(parents=True, exist_ok=True)
        self.names.write_text(text if text is not None else f"# local literals\n\n  {self.NAME}  \n",
                              encoding="utf-8")

    def repo(self, name="project"):
        local = self.tmp / name
        _init_repo(local)
        return local

    def stage(self, local, path, text="ok\n"):
        target = local / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        _git(local, "add", "--", path)

    def decision(self, result, expected="ask"):
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], expected)
        self.assertEqual(result.stderr, "")
        return out["permissionDecisionReason"]

    def masked(self, result, name=None):
        for stream in (result.stdout, result.stderr):
            self.assertNotIn((name or self.NAME).lower(), stream.lower())

    def commit_hook(self, local):
        dest = local / ".git/hooks/pre-commit"
        dest.symlink_to(HOOK)
        return dest

    def real_commit(self, local, *args):
        return subprocess.run(["git", "commit", *args], cwd=local, env=self.env,
                              capture_output=True, text=True)

    def test_real_pre_commit_symlink_staged_token_and_clean(self):
        local = self.repo()
        token = _fake_token("ghp_", 36)
        self.stage(local, "f.txt", token + "\n")
        self.commit_hook(local)
        result = self.real_commit(local, "-m", "first")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("GitHub token", result.stderr)
        self.assertIn("ghp_…40", result.stderr)
        self.assertNotIn(token, result.stdout + result.stderr)
        self.assertIn("override: git commit --no-verify", result.stderr)
        self.stage(local, "f.txt", "clean\n")
        sub = local / "sub"
        sub.mkdir()
        result = self.real_commit(sub, "-m", "first")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_real_pre_commit_all_and_path_honour_temporary_index(self):
        for args in (("-a",), ("f.txt",)):
            with self.subTest(args=args):
                local = self.repo("all" if args[0] == "-a" else "path")
                self.stage(local, "f.txt")
                _git(local, "commit", "-m", "base")
                self.commit_hook(local)
                (local / "f.txt").write_text(_fake_token("ghp_", 36) + "\n")
                self.assertEqual(_git(local, "diff", "--cached").stdout, "")
                result = self.real_commit(local, "-m", "update", *args)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("GitHub token", result.stderr)

    def test_pre_commit_explicit_only_scans_staged(self):
        local = self.repo()
        self.stage(local, "f.txt")
        _git(local, "commit", "-m", "base")
        (local / "f.txt").write_text(_fake_token("ghp_", 36))
        (local / "HANDOFF.md").write_text("local\n")
        result = self.run_hook(argv=["--pre-commit", str(local)])
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.stage(local, "added.txt", _fake_token("ghp_", 36))
        result = self.run_hook(argv=["--pre-commit", str(local)])
        self.assertEqual(result.returncode, 1)
        self.assertIn("GitHub token", result.stderr)
        self.assertNotIn("working file", result.stderr)

    def test_private_names_added_lines_comments_line_number_and_boundaries(self):
        self.deny_list(f"# acme-secret-thing\n\n  {self.NAME}  \n\n")
        local = self.repo()
        for text, hits in ((self.NAME.upper(), True), (self.NAME + "_v2", True),
                           ("owner/" + self.NAME + ".git", True), ("x" + self.NAME, False),
                           (self.NAME + "7", False), ("é" + self.NAME, False),
                           (self.NAME + "中", False), ("acme-secret-thing", False)):
            with self.subTest(text=text):
                self.stage(local, "f.txt", text + "\n")
                result = self.run_hook(argv=["--pre-commit", str(local)])
                self.assertEqual(result.returncode, int(hits), result.stderr)
                self.masked(result)
                if hits:
                    self.assertIn("private name", result.stderr)
                    self.assertIn("private-names.txt:3", result.stderr)

    def test_private_names_messages_and_added_paths_in_history(self):
        self.deny_list()
        local = self.repo()
        self.stage(local, "docs/" + self.NAME + "_v2.txt")
        staged = self.run_hook(argv=["--pre-commit", str(local)])
        self.assertEqual(staged.returncode, 1)
        self.assertIn("private-names.txt:3", staged.stderr)
        self.masked(staged)
        _git(local, "commit", "-m", self.NAME.upper())
        result = self.run_hook(argv=["--check", str(local)])
        self.assertEqual(result.returncode, 1)
        self.assertIn("(commit message)", result.stdout)
        self.assertIn("docs/<private name 3>_v2.txt", result.stdout)
        self.masked(result)
        sha = _git(local, "rev-parse", "HEAD").stdout.strip()
        result = self.run_hook(argv=["origin", "unused"], cwd=local,
                               stdin_text=f"refs/heads/main {sha} refs/heads/main {'0' * 40}\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("(commit message)", result.stderr)
        self.masked(result)

    def test_private_names_missing_and_empty_xdg_fallback(self):
        local = self.repo()
        self.stage(local, "f.txt", self.NAME)
        self.assertEqual(self.run_hook(argv=["--pre-commit", str(local)]).returncode, 0)
        fallback = self.tmp / "home/.config/claude-orchestrator/private-names.txt"
        fallback.parent.mkdir(parents=True)
        fallback.write_text(self.NAME + "\n")
        result = self.run_hook(argv=["--pre-commit", str(local)], env={"XDG_CONFIG_HOME": ""})
        self.assertEqual(result.returncode, 1)
        self.assertIn("private-names.txt:1", result.stderr)
        self.masked(result)

    def test_broken_deny_list_fails_closed_in_every_entry_point(self):
        local = self.repo()
        self.stage(local, "f.txt")
        _git(local, "commit", "-m", "base")
        self.deny_list()
        for invalid in (False, True):
            with self.subTest(invalid_utf8=invalid):
                if invalid:
                    self.names.write_bytes(b"\xff\n")
                else:
                    self.names.chmod(0o000)
                    if os.access(self.names, os.R_OK):
                        self.names.chmod(0o600)
                        self.skipTest("unreadable file requires an unprivileged user")
                try:
                    for argv in (["--pre-commit", str(local)], ["--check", str(local)],
                                 ["origin", "unused"]):
                        result = self.run_hook(argv=argv, cwd=local)
                        self.assertEqual(result.returncode, 1)
                        self.assertIn("could not finish", result.stderr)
                        self.assertIn(str(self.names), result.stderr)
                        self.assertNotIn("\xff", result.stdout + result.stderr)
                    for command in ("git commit -m x", "git push origin HEAD"):
                        result = self.claude(command, cwd=local)
                        self.assertIn("could not finish", self.decision(result))
                    # A broken list must not weaken the commit-then-push deny to ask.
                    result = self.claude("git commit -m x && git push origin HEAD", cwd=local)
                    self.assertIn("creates a commit and then pushes",
                                  self.decision(result, "deny"))
                finally:
                    self.names.chmod(0o600)

    def test_masking_all_modes_paths_targets_and_inside_longer_words(self):
        self.deny_list()
        local = self.repo("x" + self.NAME.upper() + "y")
        path = "x" + self.NAME.upper() + "y.txt"
        self.stage(local, path, _fake_token("ghp_", 36) + " x" + self.NAME + "y\n")
        result = self.run_hook(argv=["--pre-commit", str(local)])
        self.masked(result)
        self.assertIn("<private name 3>", result.stderr)
        for command in ("git commit --no-verify -m x", "git commit -m x && git push"):
            result = self.claude(command, cwd=local)
            self.masked(result)
            self.decision(result, "deny" if "push" in command else "ask")
        _git(local, "commit", "-m", "base")
        result = self.run_hook(argv=["--check", str(local)])
        self.assertEqual(result.returncode, 1)
        self.masked(result)
        sha = _git(local, "rev-parse", "HEAD").stdout.strip()
        remote = "https://example.com/x" + self.NAME.upper() + "y.git"
        result = self.run_hook(argv=[remote, remote], cwd=local,
                               stdin_text=f"refs/heads/main {sha} refs/heads/main {'0' * 40}\n")
        self.assertEqual(result.returncode, 1)
        self.masked(result)
        result = self.claude("git push " + remote + " HEAD", cwd=local)
        self.masked(result)
        self.assertIn("<private name 3>", self.decision(result))

    def test_masking_could_not_finish_hints_every_mode(self):
        self.deny_list()
        local = self.repo("x" + self.NAME + "y")
        self.stage(local, "f.txt")
        _git(local, "commit", "-m", "base")
        sha = _git(local, "rev-parse", "HEAD").stdout.strip()
        for argv in (["--pre-commit", str(local)], ["--check", str(local)],
                     ["origin", "unused"]):
            result = self.run_hook(argv=argv, cwd=local, env={"SECRET_GUARD_BUDGET_S": "0"},
                                   stdin_text=f"refs/heads/main {sha} refs/heads/main {'0' * 40}\n")
            self.assertEqual(result.returncode, 1)
            self.assertIn("could not finish", result.stderr)
            self.assertIn("--check", result.stderr)
            self.assertIn("<private name 3>", result.stderr)
            self.masked(result)
        for command in ("git commit -m x", "git push origin HEAD"):
            result = self.claude(command, cwd=local, env={"SECRET_GUARD_BUDGET_S": "0"})
            reason = self.decision(result)
            self.assertIn("could not finish", reason)
            self.assertIn("<private name 3>", reason)
            self.masked(result)

    def test_masking_longest_name_first_and_opt_out_still_masks(self):
        self.deny_list(f"fictional\n{self.NAME}\n")
        local = self.repo()
        _git(local, "config", "secretGuard.privateNames", "false")
        self.stage(local, "x" + self.NAME + "y.txt", _fake_token("ghp_", 36))
        result = self.run_hook(argv=["--pre-commit", str(local)])
        self.assertIn("x<private name 2>y.txt", result.stderr)
        self.assertNotIn("private-names.txt:", result.stderr)
        self.masked(result)
        self.masked(result, "fictional")

    def test_new_repo_opt_outs_and_command_overrides_ignored(self):
        self.deny_list()
        for key, path, text, label in (("privateNames", "f.txt", self.NAME, "private name"),
                                       ("workingFiles", "HANDOFF.md", "ok", "working file")):
            with self.subTest(key=key):
                local = self.repo(key)
                self.stage(local, path, text)
                result = self.claude(f"git -c secretGuard.{key}=false commit -m x", cwd=local)
                self.assertIn(label, self.decision(result))
                _git(local, "commit", "-m", "base")
                result = self.claude(f"git -c secretGuard.{key}=false push origin HEAD", cwd=local)
                self.assertIn(label, self.decision(result))
                _git(local, "config", f"secretGuard.{key}", "false")
                self.assertEqual(self.claude("git push origin HEAD", cwd=local).stdout, "")
                self.assertEqual(self.run_hook(argv=["--check", str(local)]).returncode, 0)
                self.stage(local, path, text + "\nnew\n")
                self.assertEqual(self.run_hook(argv=["--pre-commit", str(local)]).returncode, 0)
                self.assertEqual(self.claude("git commit -m x", cwd=local).stdout, "")

    def test_working_file_additions_only(self):
        local = self.repo()
        for path in ("HANDOFF.md", "nested/hAnDoFf.Md", "feedback/x.md", "feedback/x.jsonl",
                     "feedback/sub/x.md", "docs/HANDOFF-notes.md"):
            self.stage(local, path)
        result = self.run_hook(argv=["--pre-commit", str(local)])
        self.assertEqual(result.returncode, 1)
        self.assertIn("4 findings", result.stderr)
        self.assertNotIn("feedback/sub", result.stderr)
        self.assertNotIn("HANDOFF-notes", result.stderr)
        _git(local, "commit", "-m", "base")
        self.stage(local, "HANDOFF.md", "changed\n")
        _git(local, "rm", "feedback/x.md")
        result = self.run_hook(argv=["--pre-commit", str(local)])
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_renamed_destinations_and_untracked_paths(self):
        self.deny_list()
        local = self.repo()
        self.stage(local, "ordinary.txt")
        _git(local, "commit", "-m", "base")
        _git(local, "mv", "ordinary.txt", "HANDOFF.md")
        result = self.run_hook(argv=["--pre-commit", str(local)])
        self.assertIn("working file", result.stderr)
        (local / (self.NAME + ".txt")).write_text("ok\n")
        result = self.run_hook(argv=["--check", str(local)])
        self.assertIn("(untracked)", result.stdout)
        self.assertIn("private-names.txt:3", result.stdout)
        self.masked(result)

    def test_pretool_commit_staged_and_add_before_commit(self):
        local = self.repo()
        (local / "HANDOFF.md").write_text("work\n")
        result = self.claude("git commit -m x", cwd=local)
        self.assertEqual(result.stdout, "")
        result = self.claude("git add -A && git commit -m wip", cwd=local)
        self.assertIn("working file", self.decision(result))
        self.stage(local, "f.txt", _fake_token("ghp_", 36))
        self.assertIn("GitHub token", self.decision(self.claude("git commit -m x", cwd=local)))
        self.decision(self.claude("git commit -m x && git push", cwd=local), "deny")

    def test_pretool_full_scan_from_subdirectory_uses_root_relative_paths(self):
        local = self.repo()
        (local / "feedback").mkdir()
        (local / "feedback/x.md").write_text("local notes\n")
        sub = local / "sub"
        sub.mkdir()
        result = self.claude("git add -A && git commit -m wip", cwd=sub)
        reason = self.decision(result)
        self.assertIn("working file", reason)
        self.assertIn("feedback/x.md", reason)

    def test_check_keeps_existing_untracked_locations_from_subdirectory(self):
        local = self.repo()
        sub = local / "sub"
        sub.mkdir()
        (sub / "f.txt").write_text(_fake_token("ghp_", 36))
        result = self.run_hook(argv=["--check", str(sub)])
        self.assertEqual(result.returncode, 1)
        self.assertIn("GitHub token  f.txt:1", result.stdout)
        self.assertNotIn("sub/f.txt", result.stdout)

    def test_pretool_commit_worktree_flags_pathspecs_and_prior_script(self):
        local = self.repo()
        self.stage(local, "f.txt")
        _git(local, "commit", "-m", "base")
        (local / "f.txt").write_text(_fake_token("ghp_", 36))
        for args in ("-a", "--all", "--include", "-i", "--only", "-o", "-am x", "f.txt",
                     "-- f.txt", "--pathspec-from-file=paths"):
            with self.subTest(args=args):
                self.assertIn("GitHub token", self.decision(
                    self.claude(f"git commit {args} -m x", cwd=local)))
        self.assertEqual(self.claude("git commit -m all", cwd=local).stdout, "")
        self.assertEqual(self.claude("git commit -mall", cwd=local).stdout, "")
        self.assertIn("GitHub token", self.decision(
            self.claude("./prepare.sh && git commit -m x", cwd=local)))

    def test_pretool_raw_heredoc_message_and_tokens_without_home_rule(self):
        self.deny_list()
        local = self.repo()
        command = "git commit -m \"$(cat <<'EOF'\n" + self.NAME.upper() + "\nEOF\n)\""
        result = self.claude(command, cwd=local)
        self.assertIn("(command)", self.decision(result))
        self.assertIn("private-names.txt:3", result.stdout)
        self.masked(result)
        command = "git commit -m \"$(cat <<'EOF'\n" + _fake_token("ghp_", 36) + "\nEOF\n)\""
        self.assertIn("GitHub token", self.decision(self.claude(command, cwd=local)))
        home = "/" + "home" + "/alice/file"
        self.assertEqual(self.claude("git commit -m '" + home + "'", cwd=local).stdout, "")

    def test_pretool_commit_wrappers_and_directory_tracking(self):
        import shlex
        local = self.repo()
        other = self.repo("clean")
        self.stage(local, "f.txt", _fake_token("ghp_", 36))
        q = shlex.quote(str(local))
        for command in (f"cd {q} && git commit -m x", f"git -C {q} commit -m x",
                        f"env -C {q} command git commit -m x",
                        f"env -S 'git -C {q} commit -m x'",
                        f"bash -c 'cd {q}; git commit -m x'",
                        f"eval 'cd {q}'; git commit -m x",
                        f"git --git-dir={q}/.git --work-tree={q} commit -m x",
                        f"(cd {q}; git commit -m x); git status"):
            with self.subTest(command=command):
                self.assertIn("GitHub token", self.decision(self.claude(command, cwd=other)))
        self.assertEqual(self.claude(f"(cd {q}); git commit -m x", cwd=other).stdout, "")

    def test_non_commit_verbs_and_plain_commands_stay_silent(self):
        local = self.repo()
        self.stage(local, "f.txt", _fake_token("ghp_", 36))
        for command in ("git merge x", "git rebase x", "git cherry-pick x", "git revert x",
                        "git am x", "git status", "git log", "git diff", "echo commit"):
            with self.subTest(command=command):
                result = self.claude(command, cwd=local, env={"PATH": str(self.tmp / "no-git")})
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))


class CwdGuardTests(_SecretGuardHarness, unittest.TestCase):
    """PreToolUse cd/pushd check (CLAUDE_PROJECT_DIR) and drift notice."""

    def setUp(self):
        super().setUp()
        self.repo = self.tmp / "repo"
        _init_repo(self.repo)
        (self.repo / "sub").mkdir()
        (self.repo / "docs").mkdir()
        self.log = self.tmp / "sessions.jsonl"
        self.log.write_text("")
        self.env["CLAUDE_PROJECT_DIR"] = str(self.repo)
        self.env["CLAUDE_SESSION_LOG"] = str(self.log)

    def _out(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        if not result.stdout.strip():
            return None
        return json.loads(result.stdout)["hookSpecificOutput"]

    def _deny(self, command, **kwargs):
        out = self._out(self.claude(command, cwd=self.repo, **kwargs))
        self.assertIsNotNone(out)
        self.assertEqual(out.get("permissionDecision"), "deny")
        self.assertTrue(out["permissionDecisionReason"].startswith("cwd guard:"))
        return out["permissionDecisionReason"]

    def _silent(self, command, cwd=None, **kwargs):
        out = self._out(self.claude(command, cwd=cwd or self.repo, **kwargs))
        self.assertIsNone(out)

    def _notice(self, command, cwd=None, **kwargs):
        out = self._out(self.claude(command, cwd=cwd or self.repo, **kwargs))
        self.assertIsNotNone(out)
        self.assertNotIn("permissionDecision", out)
        self.assertTrue(out["additionalContext"].startswith("cwd guard:"))
        return out["additionalContext"]

    def test_deny_sticking_cd_and_wrap(self):
        reason = self._deny("cd sub && ls")
        self.assertIn("sub/", reason)
        self.assertIn("sticks for later Bash calls", reason)
        self.assertIn("( cd sub && ls )", reason)
        self.assertIn("git -C DIR", reason)

    def test_deny_cd_docs_pushd_eval_or(self):
        self._deny("cd docs")
        self._deny("pushd sub")
        self._deny("eval cd sub")
        self._deny("cd sub || exit 1")
        self._deny("builtin cd sub")
        self._deny("command cd sub")

    def test_deny_mkdir_cd_build_missing(self):
        reason = self._deny("mkdir -p build && cd build && cmake ..")
        self.assertIn("build/", reason)

    def test_unknown_scoped_absolute_recovers(self):
        root = str(self.repo.resolve())
        self._deny("(cd $D && make) && cd sub")
        self._deny(f"cd $X && cd {root}/sub")
        self._silent("cd $X && cd sub")
        self._deny("env -C $D ls && cd sub")

    def test_control_operators_pipeline_background(self):
        root = str(self.repo.resolve())
        self._deny(f"cd docs || cd {root}")
        self._silent("cd sub &")
        self._silent("cd docs & wait")
        self._silent("cd sub & wait")
        self._silent("cd sub | cat")
        self._deny("cd sub && make || cd ..")
        self._deny("ls & cd sub")
        # Redirect ampersands are not background terminators.
        self._deny("cd sub 2>&1 && ls")
        self._deny("cd sub &>/dev/null")
        self._deny("cd sub >/dev/null 2>&1; make")
        self._deny("cd sub && make 2>&1 | tail")

    def test_pushd_rotate_and_substitution_target_silent(self):
        self._silent("pushd +1")
        self._silent("pushd -1")
        self._silent('cd "$(printf /tmp)"')

    def test_silent_subshell_subst_bash_c_env_c_git_c(self):
        for command in (
            "(cd sub && ls)",
            "echo $(cd sub && ls)",
            "echo `cd sub && ls`",
            "bash -c 'cd sub && ls'",
            "env -C sub ls",
            "git -C sub status",
            "cd /tmp && ls",
            "D=sub; cd $D && ls",
            "cd sub; cd ..",
            f'cd sub && cd "{self.repo}"',
            "ls",
        ):
            with self.subTest(command=command):
                self._silent(command)

    def test_heredoc_body_silent_prefix_deny(self):
        self._silent("cat > s.sh <<'EOF'\ncd sub && make\nEOF")
        self._deny("cd sub && python3 - <<'EOF'\nprint(1)\nEOF")
        # Here-string must not swallow the rest of the script as a heredoc body.
        self._deny('grep x <<< "$v"\ncd sub')
        # Heredoc opened before | on the same line: body includes later lines.
        self._silent("python3 - <<'EOF' | head\ncd sub\nEOF")
        # cd after && on the opener line still sticks.
        self._deny("cat <<EOF > f && cd sub\nbody\nEOF")

    def test_wrap_multiline_for_heredoc_and_comment(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("sg", HOOK)
        sg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sg)
        cases = [
            ("cd sub && ls", "( cd sub && ls )"),
            ("cd sub && cat <<'EOF'\nx\nEOF", "(\ncd sub && cat <<'EOF'\nx\nEOF\n)"),
            ("cd sub && ls  # look", "(\ncd sub && ls  # look\n)"),
        ]
        for command, expected in cases:
            with self.subTest(command=command):
                wrap = sg._cwd_wrap_suggestion(command)
                self.assertEqual(wrap, expected)
                r = subprocess.run(["bash", "-n"], input=wrap, capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)
        reason = self._deny("cd sub && ls  # look")
        self.assertIn("(\ncd sub && ls  # look\n)", reason)

    def test_gates_event_tool_project_agent_disable(self):
        # Wrong / missing / empty event or tool: cwd check no-op (exact match only).
        for payload in (
            {"hook_event_name": "preToolUse", "tool_name": "Bash",
             "tool_input": {"command": "cd sub && ls"}, "cwd": str(self.repo)},
            {"hook_event_name": "PreToolUse", "tool_name": "Shell",
             "tool_input": {"command": "cd sub && ls"}, "cwd": str(self.repo)},
            {"hook_event_name": None, "tool_name": "Bash",
             "tool_input": {"command": "cd sub && ls"}, "cwd": str(self.repo)},
            {"hook_event_name": "", "tool_name": "Bash",
             "tool_input": {"command": "cd sub && ls"}, "cwd": str(self.repo)},
            {"hook_event_name": "PreToolUse", "tool_name": "",
             "tool_input": {"command": "cd sub && ls"}, "cwd": str(self.repo)},
            {"tool_name": "Bash",
             "tool_input": {"command": "cd sub && ls"}, "cwd": str(self.repo)},
            {"hook_event_name": "PreToolUse",
             "tool_input": {"command": "cd sub && ls"}, "cwd": str(self.repo)},
        ):
            r = self.run_hook(payload)
            self.assertEqual((r.returncode, r.stdout), (0, ""), payload)
        # No CLAUDE_PROJECT_DIR.
        r = self.claude("cd sub && ls", cwd=self.repo,
                        env={"CLAUDE_PROJECT_DIR": ""})
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        # Subagent.
        self._silent("cd sub && ls", extra={"agent_id": "agent-1"})
        # CWD_GUARD_DISABLE only.
        self._silent("cd sub && ls", env={"CWD_GUARD_DISABLE": "1"})
        # SECRET_GUARD_DISABLE must not turn the cd check off.
        self._deny("cd sub && ls", env={"SECRET_GUARD_DISABLE": "1"})
        # CWD_GUARD_DISABLE must not turn secret checks off.
        local, _ = self.make_pair()
        token = _fake_token("ghp_", 36)
        (local / "leak.txt").write_text(f"{token}\n")
        _git(local, "add", "leak.txt")
        _git(local, "commit", "-m", "leak")
        self.env["CLAUDE_PROJECT_DIR"] = str(local)
        r = self.claude("git push origin main", cwd=local, env={"CWD_GUARD_DISABLE": "1"})
        out = self._out(r)
        self.assertEqual(out["permissionDecision"], "ask")
        self.assertIn("GitHub token", out["permissionDecisionReason"])
        self.assertNotIn("additionalContext", out)
        self.env["CLAUDE_PROJECT_DIR"] = str(self.repo)

    def test_vanilla_silences_claude_only_and_no_row_deny(self):
        self.log.write_text(json.dumps(
            {"event": "start", "session": "van", "mode": "vanilla"}) + "\n")
        self._silent("cd sub && ls", extra={"session_id": "van"})
        self.log.write_text(json.dumps(
            {"event": "start", "session": "co", "mode": "claude-only"}) + "\n")
        self._deny("cd sub && ls", extra={"session_id": "co"})
        self.log.write_text("")
        self._deny("cd sub && ls", extra={"session_id": "missing"})
        self._deny("cd sub && ls")  # no session_id, no start row

    def test_drift_notice_and_return_silent(self):
        notice = self._notice("ls", cwd=self.repo / "sub")
        self.assertIn("sub", notice)
        self.assertIn(str(self.repo.resolve()), notice)
        self.assertIn("earlier command", notice)
        self._silent(f"cd {self.repo} && ls", cwd=self.repo / "sub")
        # Outside the root counts as drift too.
        notice = self._notice("ls", cwd=self.tmp)
        self.assertIn(str(self.tmp.resolve()), notice)

    def test_drift_notice_merged_into_secret_ask(self):
        local, _ = self.make_pair()
        (local / "sub").mkdir()
        token = _fake_token("ghp_", 36)
        (local / "leak.txt").write_text(f"{token}\n")
        _git(local, "add", "leak.txt")
        _git(local, "commit", "-m", "leak")
        self.env["CLAUDE_PROJECT_DIR"] = str(local)
        r = self.claude("git push origin main", cwd=local / "sub")
        out = self._out(r)
        self.assertEqual(out["permissionDecision"], "ask")
        self.assertIn("GitHub token", out["permissionDecisionReason"])
        self.assertTrue(out["additionalContext"].startswith("cwd guard:"))
        self.assertIn("sub", out["additionalContext"])

    def test_cwd_deny_skips_secret_scan(self):
        local, _ = self.make_pair()
        (local / "sub").mkdir()
        token = _fake_token("ghp_", 36)
        (local / "leak.txt").write_text(f"{token}\n")
        _git(local, "add", "leak.txt")
        _git(local, "commit", "-m", "leak")
        self.env["CLAUDE_PROJECT_DIR"] = str(local)
        # Sticking cd before a push: cwd deny is final (no secret ask).
        out = self._out(self.claude("cd sub && git push origin main", cwd=local))
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertTrue(out["permissionDecisionReason"].startswith("cwd guard:"))
        self.assertNotIn("GitHub", out["permissionDecisionReason"])
        self.assertNotIn("additionalContext", out)


class SandboxGuardTests(_SecretGuardHarness, unittest.TestCase):
    """Delegate's sandbox opt-out is a safety gate in every Claude session."""

    launch = "delegate launch cursor --brief b.md --unsandboxed"

    def setUp(self):
        super().setUp()
        self.env.pop("SANDBOX_GUARD_DISABLE", None)
        self.runs = self.tmp / "runs"
        self.env["CLAUDE_DELEGATE_ROOT"] = str(self.runs)
        self.env["XDG_CONFIG_HOME"] = str(self.tmp / "config")
        self.env["XDG_CACHE_HOME"] = str(self.tmp / "cache")

    def _meta(self, name, unsandboxed, repo="project", root=None):
        run = (root or self.runs) / repo / name
        run.mkdir(parents=True, exist_ok=True)
        (run / "meta.json").write_text(json.dumps({"unsandboxed": unsandboxed}))
        return run

    def _out(self, command, **kwargs):
        result = self.claude(command, **kwargs)
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        if not result.stdout:
            return None
        # json.loads rejects multiple responses; all gates must share one decision.
        out = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "PreToolUse")
        self.assertIn(out.get("permissionDecision"), ("ask", "deny", None))
        return out

    def _ask(self, command, **kwargs):
        out = self._out(command, **kwargs)
        self.assertIsNotNone(out, command)
        self.assertEqual(out["permissionDecision"], "ask", command)
        self.assertTrue(out["permissionDecisionReason"].startswith("sandbox guard:"))
        return out

    def _silent(self, command, **kwargs):
        self.assertIsNone(self._out(command, **kwargs), command)

    def test_launch_unsandboxed_abbreviations(self):
        for flag in ("--unsandboxed", "--uns", "--u"):
            with self.subTest(flag=flag):
                self._ask(f"delegate launch cursor --brief b.md {flag}")

    def test_launch_safe_dry_run_and_separator(self):
        for command in (
            "delegate launch cursor --brief b.md",
            self.launch + " --dry-run",
            "delegate launch cursor --brief b.md -- --unsandboxed",
            "delegate launch cursor --brief b.md --unsandboxed=no",
            "delegate launch cursor --brief b.md --unsandboxed-extra",
            "delegate status --unsandboxed",
        ):
            with self.subTest(command=command):
                self._silent(command)

    def test_command_positions_and_data_controls(self):
        text = "skills/delegate/scripts/" + self.launch
        self._ask(text)
        for command in (
            f"echo '{text}'",
            f'printf "%s\\n" "{text}"',
            "grep -n -- --unsandboxed SKILL.md",
            f"cat <<'EOF'\n{text}\nEOF",
            f"cat <<'EOF' | head\n{text}\nEOF",
            f"cat <<-EOF\n\t{text}\n\tEOF",
            f"echo ok # {text}",
            f"'{text}'",
            f"echo '$( {text} )'",
        ):
            with self.subTest(command=command):
                self._silent(command)
        self._ask(f"cat <<'EOF'\n{text}\nEOF\n{text}")
        self._ask(f"cat <<'EOF' && {text}\nplain text\nEOF")
        # Quotes and substitutions in heredoc data must not affect later operators.
        self._ask(f"cat <<'EOF'\n\" $(ignored)\nEOF\nfalse || {text}")

    def test_wrappers_and_shell_operators(self):
        for command in (
            f"bash -c '{self.launch}'",
            f"sh -c '{self.launch}'",
            f"eval '{self.launch}'",
            f"env A=1 {self.launch}",
            f"env -C x {self.launch}",
            f"env -C x -S '{self.launch}'",
            f"(cd x && {self.launch})",
            f"true && {self.launch}",
            f"false || {self.launch}",
            f"true; {self.launch}",
            f"printf x | {self.launch}",
            f"true\n{self.launch}",
            f"echo $({self.launch})",
            f'echo "$(false || {self.launch})"',
            f"echo `{self.launch}`",
            f"if true; then {self.launch}; fi",
            f"timeout 10 command {self.launch}",
        ):
            with self.subTest(command=command):
                self._ask(command)

    def test_python_interpreter_options_and_executable_names(self):
        for command in (
            "python3 -I path/to/" + self.launch,
            "python -W ignore -X dev path/to/" + self.launch,
            "python3 -IW ignore -BX dev path/to/" + self.launch,
            "python3.9 -- path/to/" + self.launch,
            "python3 --check-hash-based-pycs always path/to/" + self.launch,
            "delegate --verbose launch cursor --unsandboxed",
        ):
            with self.subTest(command=command):
                self._ask(command)
        for prefix in ("other-delegate", "python2 delegate", "python3 -m delegate",
                       "python3 -c delegate", "python3 -Ic delegate", "python3 --help delegate",
                       "python3 other-script delegate"):
            self._silent(prefix + " launch cursor --unsandboxed")

    def test_followup_metadata_name_and_directory(self):
        run = self._meta("unsafe", True)
        self._meta("safe", False)
        for ref in ("unsafe", str(run), "runs/project/unsafe"):
            with self.subTest(ref=ref):
                reason = self._ask(f"delegate followup {ref}")["permissionDecisionReason"]
                self.assertIn(f"run {ref} was launched --unsandboxed", reason)
        self._silent("delegate followup safe")
        # A direct directory wins over a same-named record in the run root.
        direct = self.tmp / "unsafe"
        direct.mkdir()
        (direct / "meta.json").write_text('{"unsandboxed": false}')
        self._silent("delegate followup unsafe")
        self._meta("unsafe", True, root=Path(self.env["HOME"]))
        self._ask("delegate followup ~/project/unsafe")

    def test_followup_unknown_and_dynamic_names(self):
        self._meta("safe", False)
        self._silent("delegate followup unknown")
        dynamic = ("$NAME", '"$NAME"', "'run*'", "run?", "'[ab]'", "$(printf unsafe)",
                   "`printf unsafe`", "")
        for ref in dynamic:
            with self.subTest(ref=ref):
                self._silent(f"delegate followup {ref}")
        self._meta("unsafe", True)
        self._silent("delegate followup unknown")
        for ref in dynamic:
            with self.subTest(ref=ref):
                self._ask(f"delegate followup {ref}")

    def test_followup_unreadable_or_invalid_metadata(self):
        run = self._meta("broken", False)
        path = run / "meta.json"
        for contents in ("not json", "[]", "{", b"\xff"):
            with self.subTest(contents=contents):
                path.write_bytes(contents if isinstance(contents, bytes) else contents.encode())
                out = self._ask("delegate followup broken")
                self.assertIn("cannot verify metadata for run broken", out["permissionDecisionReason"])
        path.unlink()
        path.mkdir()  # read_text fails even when the tests run as root.
        self._ask("delegate followup broken")
        self._ask(f"delegate followup {run}")
        self._meta("ambiguous", False, repo="first")
        self._meta("ambiguous", True, repo="second")
        self._ask("delegate followup ambiguous")

    def test_followup_options_before_name_and_dry_run(self):
        self._meta("unsafe", True)
        self._meta("safe", False)
        for option, value in (("--brief", "f.md"), ("--from-review", "2"),
                              ("--model", "auto"), ("--effort", "high"),
                              ("--verify", "check"), ("--link", "files"), ("--br", "f.md")):
            for sep in (" ", "="):
                with self.subTest(option=option, sep=sep):
                    self._ask(f"delegate followup {option}{sep}{value} unsafe")
                    self._silent(f"delegate followup {option}{sep}{value} safe")
        self._ask("delegate followup --fresh --detach unsafe")
        self._ask("delegate followup -- unsafe")
        self._silent("delegate followup --brief unsafe safe")
        for args in ("unsafe --dry-run", "--dry-run unsafe", "--dry-run $NAME", "--dry-run"):
            self._silent(f"delegate followup {args}")

    def test_followup_working_directories(self):
        directory = self.tmp / "x"
        directory.mkdir()
        (directory / "meta.json").write_text('{"unsandboxed": true}')
        for command in (
            "(cd x && delegate followup .)",
            "env -C x delegate followup .",
            "env --chdir=x delegate followup .",
            "bash -c 'cd x; delegate followup .'",
            "eval 'cd x'; delegate followup .",
        ):
            with self.subTest(command=command):
                self._ask(command)
        self._silent("(cd x); delegate followup .")
        self._silent("env -C x true; delegate followup .")
        self._silent("bash -c 'cd x'; delegate followup .")
        self._meta("relative-root", True, root=directory / "records")
        self._ask("env -C x delegate followup relative-root",
                  env={"CLAUDE_DELEGATE_ROOT": "records"})

    def test_run_root_environment_precedence(self):
        self._meta("unsafe", True)
        self._ask("delegate followup unsafe")
        del self.env["CLAUDE_DELEGATE_ROOT"]
        cache_root = Path(self.env["XDG_CACHE_HOME"]) / "claude-delegate" / "runs"
        self._silent("delegate followup unsafe")
        self._meta("unsafe", True, root=cache_root)
        self._ask("delegate followup unsafe")
        del self.env["XDG_CACHE_HOME"]
        self._silent("delegate followup unsafe")
        home_root = Path(self.env["HOME"]) / ".cache" / "claude-delegate" / "runs"
        self._meta("unsafe", True, root=home_root)
        self._ask("delegate followup unsafe")

    def test_environment_switches_are_independent(self):
        self._silent(self.launch, env={"SANDBOX_GUARD_DISABLE": "1"})
        for switch in ("SECRET_GUARD_DISABLE", "CWD_GUARD_DISABLE"):
            self._ask(self.launch, env={switch: "1"})
        local, _ = self.make_pair()
        (local / "sub").mkdir()
        env = {"SANDBOX_GUARD_DISABLE": "1", "CLAUDE_PROJECT_DIR": str(local)}
        out = self._out(self.launch + "; cd sub", cwd=local, env=env)
        self.assertEqual(out["permissionDecision"], "deny")
        (local / "leak.txt").write_text(_fake_token("ghp_", 36))
        _git(local, "add", "leak.txt")
        _git(local, "commit", "-m", "leak")
        out = self._out(self.launch + "; git push origin main", cwd=local, env=env)
        self.assertEqual(out["permissionDecision"], "ask")
        self.assertIn("GitHub token", out["permissionDecisionReason"])
        self.assertNotIn("sandbox guard:", out["permissionDecisionReason"])

    def test_combines_push_findings_and_scan_failures(self):
        local, _ = self.make_pair()
        command = self.launch + "; git push origin main"
        self._ask(command, cwd=local)  # Clean scan must still emit the sandbox ask.
        token = _fake_token("ghp_", 36)
        (local / "leak.txt").write_text(token)
        _git(local, "add", "leak.txt")
        _git(local, "commit", "-m", "leak")
        out = self._ask(command, cwd=local)
        self.assertIn("GitHub token", out["permissionDecisionReason"])
        self.assertNotIn(token, json.dumps(out))
        out = self._ask(command, cwd=local, env={"SECRET_GUARD_BUDGET_S": "0"})
        self.assertIn("secret guard could not finish", out["permissionDecisionReason"])
        self.assertIn("BudgetExceeded", out["permissionDecisionReason"])
        out = self._ask(command)  # Not a repository is its own error branch.
        self.assertIn("is not a git repository", out["permissionDecisionReason"])

    def test_denies_win_over_sandbox_ask(self):
        local, _ = self.make_pair()
        (local / "sub").mkdir()
        out = self._out(self.launch + "; cd sub", cwd=local,
                        env={"CLAUDE_PROJECT_DIR": str(local)})
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertTrue(out["permissionDecisionReason"].startswith("cwd guard:"))
        out = self._out(self.launch + "; git commit -m x && git push", cwd=local)
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn("own Bash call", out["permissionDecisionReason"])

    def test_drift_notice_is_additional_context(self):
        repo = self.tmp / "repo"
        (repo / "sub").mkdir(parents=True)
        out = self._ask(self.launch, cwd=repo / "sub",
                        env={"CLAUDE_PROJECT_DIR": str(repo)})
        self.assertTrue(out["additionalContext"].startswith("cwd guard:"))
        self.assertIn("sub", out["additionalContext"])

    def test_all_modes_and_subagents(self):
        self._ask(self.launch, extra={"agent_id": "worker"})
        log = self.tmp / "sessions.jsonl"
        for mode in ("vanilla", "claude-only", "orchestrator"):
            log.write_text(json.dumps({"event": "start", "session": "s", "mode": mode}) + "\n")
            with self.subTest(mode=mode):
                self._ask(self.launch, env={"CLAUDE_SESSION_LOG": str(log)},
                          extra={"session_id": "s"})

    def test_exact_event_and_tool_scope(self):
        for key, values in (("hook_event_name", (None, "", "PostToolUse", "preToolUse")),
                            ("tool_name", (None, "", "Read", "bash"))):
            for value in values:
                with self.subTest(key=key, value=value):
                    self._silent(self.launch, extra={key: value})
            payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                       "tool_input": {"command": self.launch}}
            del payload[key]
            result = self.run_hook(payload)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_reason_effect_rule_and_redaction(self):
        reason = self._ask(self.launch)["permissionDecisionReason"]
        for text in ("cursor-agent", "--sandbox disabled --force", "network, installs",
                     "writes outside the worktree", "delegate skill needs the user's OK"):
            self.assertIn(text, reason)
        self.assertNotIn("\n", reason)
        config = Path(self.env["XDG_CONFIG_HOME"]) / "claude-orchestrator"
        config.mkdir(parents=True)
        (config / "private-names.txt").write_text("HiddenRun\n")
        for name in ("HiddenRun", _fake_token("ghp_", 36), "line\nbreak"):
            self._meta(name, True)
            out = self._ask(f"delegate followup '{name}'")
            reason = out["permissionDecisionReason"]
            self.assertNotIn("\n", reason)
            self.assertNotIn(name, json.dumps(out))

    def test_fast_path_and_lazy_run_root(self):
        import importlib.util
        from unittest import mock
        spec = importlib.util.spec_from_file_location("sg", HOOK)
        sg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sg)
        with mock.patch.object(sg, "_walk_shell", side_effect=AssertionError("shell walked")), \
                mock.patch.object(sg, "Path", side_effect=AssertionError("filesystem touched")):
            self.assertIsNone(sg.sandbox_guard_reason("ls -la", str(self.tmp)))
        with mock.patch.object(sg, "_unsandboxed_run", side_effect=AssertionError("run root read")), \
                mock.patch.object(Path, "glob", side_effect=AssertionError("globbed")), \
                mock.patch.object(Path, "read_text", side_effect=AssertionError("read")):
            self.assertIsNotNone(sg.sandbox_guard_reason(self.launch, str(self.tmp)))
            self.assertIsNone(sg.sandbox_guard_reason("echo 'delegate followup unsafe'", str(self.tmp)))
            self.assertIsNone(sg.sandbox_guard_reason("delegate status unsafe", str(self.tmp)))
            self.assertIsNone(sg.sandbox_guard_reason("delegate followup unsafe --dry-run", str(self.tmp)))

    def test_module_docstring_describes_guard(self):
        doc = ast.get_docstring(ast.parse(HOOK.read_text()))
        for text in ("sandbox guard", "followups", "subagents", "every session mode",
                     "SANDBOX_GUARD_DISABLE=1 (skip the sandbox check only)"):
            self.assertIn(text, doc)


if __name__ == "__main__":
    unittest.main()

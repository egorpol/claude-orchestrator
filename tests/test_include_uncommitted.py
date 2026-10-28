"""delegate launch --include-uncommitted and apply --3way via a temp index."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "delegate" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import runs

CLI = SCRIPTS / "delegate"
FAKES = Path(__file__).resolve().parent / "fake_agents"


class IncludeUncommittedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="delegate-include-uc-")
        self.addCleanup(self.temp.cleanup)
        self.tmp = Path(self.temp.name)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.storage = self.tmp / "runs"
        self.env = {**os.environ, "PATH": str(FAKES) + os.pathsep + os.environ["PATH"],
                    "CLAUDE_DELEGATE_ROOT": str(self.storage), "CLAUDECODE": "fixture",
                    "CLAUDE_DELEGATE_QUOTA_SNAPSHOT": "0",
                    "CLAUDE_SESSION_LOG": str(self.tmp / "sessions.jsonl"),
                    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        self.env.pop("FAKE_AGENT_EXIT", None)
        self.env.pop("CLAUDE_CODE_SESSION_ID", None)
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        self.git("init", "-q")
        self.git("config", "user.name", "Include UC Test")
        self.git("config", "user.email", "test@example.invalid")
        (self.repo / "tracked.txt").write_text("base\n")
        (self.repo / "other.txt").write_text("other\n")
        (self.repo / ".gitignore").write_text("ignored.txt\ndata/cache\n")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.head = self.git("rev-parse", "HEAD")
        self.brief = self.tmp / "brief.md"
        self.brief.write_text("# Task\nMake a deterministic change.\n")
        for p in (patch.object(runs, "RUNS_ROOT", self.storage),
                  patch.dict(os.environ, {"CLAUDE_SESSION_LOG": self.env["CLAUDE_SESSION_LOG"]})):
            p.start()
            self.addCleanup(p.stop)

    def git(self, *args, cwd=None):
        r = subprocess.run(["git", *args], cwd=cwd or self.repo, env=self.env,
                           text=True, capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def git_raw(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=cwd or self.repo, env=self.env,
                              text=True, capture_output=True)

    def cli(self, *args, code=0, extra_env=None):
        r = self.cli_raw(*args, extra_env=extra_env)
        self.assertEqual(r.returncode, code, f"{args}\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}")
        self.assertNotIn("Traceback", r.stdout + r.stderr)
        return r

    def cli_raw(self, *args, extra_env=None):
        return subprocess.run([sys.executable, str(CLI), *map(str, args)], cwd=self.repo,
                              env={**self.env, **(extra_env or {})}, text=True, capture_output=True)

    def root(self, name):
        return self.storage / self.repo.name / name

    def count_objects(self):
        return self.git("count-objects", "-v")

    def dirty_main(self):
        """Modified tracked, staged new file, untracked file, gitignored file."""
        (self.repo / "tracked.txt").write_text("base\nmodified\n")
        (self.repo / "staged.txt").write_text("staged new\n")
        self.git("add", "staged.txt")
        (self.repo / "untracked.txt").write_text("untracked\n")
        (self.repo / "ignored.txt").write_text("secret dump\n")

    def main_state(self):
        return (self.git("rev-parse", "HEAD"),
                self.git("diff", "--cached"),
                self.git("status", "--porcelain", "--untracked-files=all"))

    def test_include_uncommitted_snapshots_dirty_tree_and_apply_cleanup(self):
        """Flag on dirty tree: base=S, worktree holds dirty states, main untouched, patch is delegate-only."""
        self.dirty_main()
        before = self.main_state()
        r = self.cli("launch", "codex", "--brief", self.brief, "--name", "snap",
                     "--include-uncommitted")
        self.assertIn("snapshot:", r.stdout)
        self.assertIn("uncommitted", r.stdout)
        root = self.root("snap")
        meta = runs.load_meta(root)
        wt = Path(meta["worktree"])
        self.assertEqual(meta["base"], self.git("rev-parse", "HEAD", cwd=wt))
        self.assertEqual(meta["snapshot_of"], self.head)
        self.assertNotIn("snapshot", meta)
        self.assertEqual(self.git("rev-parse", f"{meta['base']}^"), self.head)
        # Snapshot commit holds the dirty states (agent already appended in the worktree by now).
        self.assertEqual(self.git("show", f"{meta['base']}:tracked.txt"), "base\nmodified")
        self.assertEqual(self.git("show", f"{meta['base']}:staged.txt"), "staged new")
        self.assertEqual(self.git("show", f"{meta['base']}:untracked.txt"), "untracked")
        self.assertNotEqual(self.git_raw("cat-file", "-e", f"{meta['base']}:ignored.txt").returncode, 0)
        self.assertEqual(self.main_state(), before)
        prompt = (root / "prompt.md").read_text()
        self.assertIn("snapshot commit of the orchestrator's uncommitted work", prompt)
        self.assertIn(meta["base"][:9], prompt)
        self.assertIn(self.head[:9], prompt)
        # fake agent appends; patch must hold only that edit relative to S
        patch = (root / "changes.patch").read_text(errors="replace")
        self.assertIn("codex round", patch)
        self.assertIn("tracked.txt", patch)
        self.assertNotIn("+modified", patch)
        self.assertNotIn("untracked", patch)
        self.cli("apply", "snap")
        self.assertTrue((root / "applied").exists())
        self.assertIn("codex round", (self.repo / "tracked.txt").read_text())
        self.assertEqual(self.git("rev-parse", "HEAD"), self.head)
        self.cli("cleanup", "snap")
        self.assertFalse(wt.exists())
        self.assertNotEqual(self.git_raw("rev-parse", "--verify", "delegate/snap").returncode, 0)
        row = json.loads((self.storage / "runs.jsonl").read_text().splitlines()[0])
        self.assertEqual(row["snapshot_of"], self.head)
        self.assertEqual(row["outcome"], "applied")

    def test_include_uncommitted_rejects_base(self):
        """--include-uncommitted --base exits with no run dir, worktree or branch."""
        self.dirty_main()
        r = self.cli_raw("launch", "codex", "--brief", self.brief, "--name", "bad-base",
                         "--include-uncommitted", "--base", self.head)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--base", r.stderr)
        self.assertFalse(self.root("bad-base").exists())
        self.assertNotEqual(self.git_raw("rev-parse", "--verify", "delegate/bad-base").returncode, 0)

    def test_untracked_limit_refuses_without_objects_all_untracked_passes(self):
        """Over the untracked limit: no new objects / worktree / branch; --all-untracked passes."""
        before = self.count_objects()
        for i in range(101):
            (self.repo / f"dump-{i}.bin").write_bytes(b"x")
        r = self.cli_raw("launch", "codex", "--brief", self.brief, "--name", "too-many",
                         "--include-uncommitted")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("101 untracked", r.stderr)
        self.assertIn("--all-untracked", r.stderr)
        self.assertFalse(self.root("too-many").exists())
        self.assertNotEqual(self.git_raw("rev-parse", "--verify", "delegate/too-many").returncode, 0)
        self.assertEqual(self.count_objects(), before)
        self.cli("launch", "codex", "--brief", self.brief, "--name", "allowed",
                 "--include-uncommitted", "--all-untracked")
        self.assertTrue(self.root("allowed").exists())
        self.cli("cleanup", "allowed", "--force", "--purge")

    def test_non_ignored_link_refused_with_include(self):
        """Non-ignored --link with the flag uses validate_working_tree_links' message."""
        self.dirty_main()
        (self.repo / "live.txt").write_text("live\n")
        r = self.cli_raw("launch", "codex", "--brief", self.brief, "--name", "badlink",
                         "--include-uncommitted", "--link", "live.txt")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not gitignored", r.stderr)
        self.assertIn("snapshot already holds it", r.stderr)
        self.assertFalse(self.root("badlink").exists())

    def test_clean_tree_with_flag_is_noop(self):
        """Clean tree with the flag: note printed, base == HEAD, no snapshot_of."""
        r = self.cli("launch", "codex", "--brief", self.brief, "--name", "clean",
                     "--include-uncommitted")
        self.assertIn("no uncommitted changes", r.stderr)
        meta = runs.load_meta(self.root("clean"))
        self.assertEqual(meta["base"], self.head)
        self.assertNotIn("snapshot_of", meta)
        self.cli("cleanup", "clean", "--force", "--purge")

    def test_dry_run_with_flag_writes_no_objects(self):
        """--dry-run with the flag: no new objects, prints would snapshot."""
        self.dirty_main()
        before = self.count_objects()
        r = self.cli("launch", "codex", "--brief", self.brief, "--name", "dry",
                     "--include-uncommitted", "--dry-run")
        self.assertIn("would snapshot", r.stdout)
        self.assertIn("SNAPSHOT", r.stdout)
        self.assertIn("snapshot commit of the orchestrator's uncommitted work", r.stdout)
        self.assertFalse(self.root("dry").exists())
        self.assertEqual(self.count_objects(), before)

    def test_preflight_with_flag_sees_untracked_then_rolls_back(self):
        """--preflight with the flag: verify that sees untracked passes; no worktree or branch left."""
        self.dirty_main()
        r = self.cli("launch", "codex", "--brief", self.brief, "--name", "pf",
                     "--include-uncommitted",
                     "--verify", "test -f untracked.txt && test -f staged.txt",
                     "--preflight")
        self.assertIn("preflight: verify pass", r.stdout)
        self.assertFalse(self.root("pf").exists())
        self.assertNotEqual(self.git_raw("rev-parse", "--verify", "delegate/pf").returncode, 0)

    def test_dirty_note_without_flag_names_counts_and_flag(self):
        """Without the flag on a dirty tree: note shows both counts and names the flag; base == HEAD."""
        self.dirty_main()
        r = self.cli("launch", "codex", "--brief", self.brief, "--name", "note")
        self.assertRegex(r.stderr, r"\d+ tracked, \d+ untracked path\(s\)")
        self.assertIn("--include-uncommitted snapshots them", r.stderr)
        meta = runs.load_meta(self.root("note"))
        self.assertEqual(meta["base"], self.head)
        self.assertNotIn("snapshot_of", meta)
        self.cli("cleanup", "note", "--force", "--purge")

    def test_followup_fresh_renders_snapshot_bullet(self):
        """followup --fresh of a snapshot run renders the snapshot bullet."""
        self.dirty_main()
        self.cli("launch", "codex", "--brief", self.brief, "--name", "fu",
                 "--include-uncommitted")
        follow = self.tmp / "follow.md"
        follow.write_text("# Follow\nContinue.\n")
        self.cli("followup", "fu", "--brief", follow, "--fresh")
        prompt = (self.root("fu") / "prompt.md").read_text()
        self.assertIn("snapshot commit of the orchestrator's uncommitted work", prompt)
        meta = runs.load_meta(self.root("fu"))
        self.assertIn(meta["base"][:9], prompt)
        self.assertIn(meta["snapshot_of"][:9], prompt)
        self.cli("cleanup", "fu", "--force", "--purge")

    def _launch_ns(self, name, **kwargs):
        base = dict(agent="codex", brief=str(self.brief), name=name, repo=str(self.repo),
                    root=None, base=None, link=[], read_only=False, unsandboxed=False,
                    web_search=False, detach=False, dry_run=False, verify=None, preflight=False,
                    model=None, effort=None, include_uncommitted=False, all_untracked=False)
        base.update(kwargs)
        return argparse.Namespace(**base)

    def test_save_meta_failure_rolls_back_worktree_and_branch(self):
        """A write failure after worktree add leaves no branch, worktree, run dir or exclude lines."""
        live = self.repo / "live-data"
        live.mkdir()
        (live / "x").write_text("x\n")
        args = self._launch_ns("orphan", link=["live-data"])
        with patch.dict(os.environ, self.env, clear=True), \
             patch.object(runs, "save_meta", side_effect=OSError("disk full")), \
             self.assertRaises(OSError):
            runs.launch(args)
        self.assertFalse(self.root("orphan").exists())
        self.assertNotIn("orphan", self.git("worktree", "list"))
        self.assertEqual(self.git("branch", "--list", "delegate/orphan"), "")
        exclude = self.repo / ".git" / "info" / "exclude"
        if exclude.exists():
            self.assertNotIn("/live-data", exclude.read_text())


class ApplyThreeWayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="delegate-apply-3way-")
        self.addCleanup(self.temp.cleanup)
        self.tmp = Path(self.temp.name)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.storage = self.tmp / "runs"
        self.env = {**os.environ, "PATH": str(FAKES) + os.pathsep + os.environ["PATH"],
                    "CLAUDE_DELEGATE_ROOT": str(self.storage), "CLAUDECODE": "fixture",
                    "CLAUDE_DELEGATE_QUOTA_SNAPSHOT": "0",
                    "CLAUDE_SESSION_LOG": str(self.tmp / "sessions.jsonl"),
                    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        self.env.pop("FAKE_AGENT_EXIT", None)
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        self.git("init", "-q")
        self.git("config", "user.name", "Apply 3way Test")
        self.git("config", "user.email", "test@example.invalid")
        (self.repo / "tracked.txt").write_text("line1\nline2\nline3\nline4\nline5\nline6\nline7\n")
        (self.repo / "keep.txt").write_text("keep\n")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.brief = self.tmp / "brief.md"
        self.brief.write_text("# Task\nEdit.\n")
        for p in (patch.object(runs, "RUNS_ROOT", self.storage),
                  patch.dict(os.environ, {"CLAUDE_SESSION_LOG": self.env["CLAUDE_SESSION_LOG"]})):
            p.start()
            self.addCleanup(p.stop)

    def git(self, *args, cwd=None):
        r = subprocess.run(["git", *args], cwd=cwd or self.repo, env=self.env,
                           text=True, capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def cli(self, *args, code=0):
        r = subprocess.run([sys.executable, str(CLI), *map(str, args)], cwd=self.repo,
                           env=self.env, text=True, capture_output=True)
        self.assertEqual(r.returncode, code, f"{args}\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}")
        return r

    def cli_raw(self, *args):
        return subprocess.run([sys.executable, str(CLI), *map(str, args)], cwd=self.repo,
                              env=self.env, text=True, capture_output=True)

    def root(self, name):
        return self.storage / self.repo.name / name

    def index_bytes(self):
        path = Path(self.git("rev-parse", "--git-path", "index"))
        if not path.is_absolute():
            path = self.repo / path
        return path.read_bytes() if path.exists() else b""

    def launch_and_edit_line6(self, name="ed"):
        """Launch, then rewrite the worktree file so the patch only changes line 6."""
        self.cli("launch", "codex", "--brief", self.brief, "--name", name)
        meta = runs.load_meta(self.root(name))
        wt = Path(meta["worktree"])
        # Drop the fake agent's appends; install a single-line edit for a clean 3-way fixture.
        (wt / "tracked.txt").write_text("line1\nline2\nline3\nline4\nline5\nline6-del\nline7\n")
        for extra in ("new-codex.txt",):
            p = wt / extra
            if p.exists():
                p.unlink()
        return meta

    def test_three_way_clean_with_unstaged_elsewhere_leaves_unstaged(self):
        """Unstaged main-tree edit elsewhere in a patched file: --3way clean, unstaged, index untouched."""
        self.launch_and_edit_line6("clean3")
        index_before = self.index_bytes()
        (self.repo / "tracked.txt").write_text("line1\nline2\nline3-edit\nline4\nline5\nline6\nline7\n")
        self.cli("apply", "clean3", "--3way")
        self.assertTrue((self.root("clean3") / "applied").exists())
        self.assertEqual(self.index_bytes(), index_before)
        # Don't strip: porcelain " M path" loses its leading space under str.strip().
        status = subprocess.run(
            ["git", "status", "--porcelain", "--", "tracked.txt"],
            cwd=self.repo, env=self.env, text=True, capture_output=True).stdout
        self.assertTrue(status.startswith(" M "), status)
        self.assertIn("line6-del", (self.repo / "tracked.txt").read_text())
        self.assertIn("line3-edit", (self.repo / "tracked.txt").read_text())
        self.cli("cleanup", "clean3", "--force", "--purge")

    def test_three_way_overlapping_conflict_no_applied(self):
        """Overlapping edit: markers, conflicted path printed, non-zero, no applied, index untouched."""
        self.launch_and_edit_line6("conflict")
        index_before = self.index_bytes()
        (self.repo / "tracked.txt").write_text("line1\nline2\nline3\nline4\nline5-main\nline6\nline7\n")
        r = self.cli_raw("apply", "conflict", "--3way")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("tracked.txt", r.stdout + r.stderr)
        self.assertIn("conflict", (r.stdout + r.stderr).lower())
        self.assertFalse((self.root("conflict") / "applied").exists())
        self.assertEqual(self.index_bytes(), index_before)
        text = (self.repo / "tracked.txt").read_text()
        self.assertIn("<<<<<<<", text)
        self.cli("cleanup", "conflict", "--force", "--purge")

    def test_three_way_add_and_delete_on_dirty_tree(self):
        """Patch that adds one file and deletes another, --3way on a dirty tree, works."""
        self.cli("launch", "codex", "--brief", self.brief, "--name", "addel")
        meta = runs.load_meta(self.root("addel"))
        wt = Path(meta["worktree"])
        (wt / "tracked.txt").write_text("line1\nline2\nline3\nline4\nline5\nline6\nline7\n")
        (wt / "new-codex.txt").unlink(missing_ok=True)
        (wt / "keep.txt").unlink()
        (wt / "fresh.txt").write_text("fresh\n")
        (self.repo / "tracked.txt").write_text("line1\nline2\nline3-edit\nline4\nline5\nline6\nline7\n")
        self.cli("apply", "addel", "--3way")
        self.assertFalse((self.repo / "keep.txt").exists())
        self.assertEqual((self.repo / "fresh.txt").read_text(), "fresh\n")
        self.assertIn("line3-edit", (self.repo / "tracked.txt").read_text())
        self.cli("cleanup", "addel", "--force", "--purge")

    def test_three_way_preserves_pre_staged_blob(self):
        """A file the user staged before apply stays staged with the same blob."""
        self.launch_and_edit_line6("staged")
        (self.repo / "keep.txt").write_text("keep staged\n")
        self.git("add", "keep.txt")
        blob = self.git("rev-parse", ":keep.txt")
        (self.repo / "tracked.txt").write_text("line1\nline2\nline3-edit\nline4\nline5\nline6\nline7\n")
        self.cli("apply", "staged", "--3way")
        self.assertEqual(self.git("rev-parse", ":keep.txt"), blob)
        status = subprocess.run(
            ["git", "status", "--porcelain", "--", "keep.txt"],
            cwd=self.repo, env=self.env, text=True, capture_output=True).stdout
        self.assertTrue(status.startswith("M ") or status.startswith("MM"), status)
        self.cli("cleanup", "staged", "--force", "--purge")

    def test_check_three_way_leaves_files_and_index_unchanged(self):
        """--check --3way leaves working files and index unchanged."""
        self.launch_and_edit_line6("check3")
        (self.repo / "tracked.txt").write_text("line1\nline2\nline3-edit\nline4\nline5\nline6\nline7\n")
        before_text = (self.repo / "tracked.txt").read_text()
        before_index = self.index_bytes()
        before_status = self.git("status", "--porcelain")
        self.cli("apply", "check3", "--check", "--3way")
        self.assertEqual((self.repo / "tracked.txt").read_text(), before_text)
        self.assertEqual(self.index_bytes(), before_index)
        self.assertEqual(self.git("status", "--porcelain"), before_status)
        self.assertFalse((self.root("check3") / "applied").exists())
        self.cli("cleanup", "check3", "--force", "--purge")

    def test_three_way_from_subdirectory_cwd(self):
        """apply --3way with cwd in a subdirectory still merges a root-level file with an unstaged edit."""
        (self.repo / "subdir").mkdir()
        self.launch_and_edit_line6("subcwd")
        (self.repo / "tracked.txt").write_text("line1\nline2\nline3-edit\nline4\nline5\nline6\nline7\n")
        r = subprocess.run(
            [sys.executable, str(CLI), "apply", "subcwd", "--3way"],
            cwd=self.repo / "subdir", env=self.env, text=True, capture_output=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.root("subcwd") / "applied").exists())
        text = (self.repo / "tracked.txt").read_text()
        self.assertIn("line3-edit", text)
        self.assertIn("line6-del", text)
        self.cli("cleanup", "subcwd", "--force", "--purge")

    def test_three_way_literal_pathspecs_skip_glob_matches(self):
        """A patch path with glob chars must not add an unrelated glob-matching untracked file."""
        data = self.repo / "data"
        data.mkdir()
        bracket = data / "a[1].txt"
        bracket.write_text("bracket-base\n")
        self.git("add", "data/a[1].txt")
        self.git("commit", "-qm", "bracket")
        self.cli("launch", "codex", "--brief", self.brief, "--name", "glob")
        meta = runs.load_meta(self.root("glob"))
        wt = Path(meta["worktree"])
        (wt / "tracked.txt").write_text("line1\nline2\nline3\nline4\nline5\nline6\nline7\n")
        (wt / "new-codex.txt").unlink(missing_ok=True)
        (wt / "data" / "a[1].txt").write_text("bracket-del\n")
        canary = b"unrelated-canary-not-in-patch\n"
        (data / "a1.txt").write_bytes(canary)
        blob = hashlib.sha1(b"blob %d\0" % len(canary) + canary).hexdigest()
        self.assertNotEqual(
            subprocess.run(["git", "cat-file", "-t", blob], cwd=self.repo, env=self.env,
                           capture_output=True).returncode, 0)
        index_before = self.index_bytes()
        (self.repo / "tracked.txt").write_text(
            "line1\nline2\nline3-edit\nline4\nline5\nline6\nline7\n")
        self.cli("apply", "glob", "--3way")
        self.assertEqual(self.index_bytes(), index_before)
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all", "--", "data/a1.txt"],
            cwd=self.repo, env=self.env, text=True, capture_output=True).stdout
        self.assertTrue(status.startswith("??"), status)
        self.assertEqual(self.git("ls-files", "--", "data/a1.txt"), "")
        self.assertNotEqual(
            subprocess.run(["git", "cat-file", "-t", blob], cwd=self.repo, env=self.env,
                           capture_output=True).returncode, 0,
            "unrelated glob match must not be hashed into .git/objects")
        self.assertEqual((data / "a[1].txt").read_text(), "bracket-del\n")
        self.cli("cleanup", "glob", "--force", "--purge")

    def _git_speaks_german(self):
        env = {**self.env, "LANGUAGE": "de", "LANG": "de_DE.UTF-8", "LC_ALL": "de_DE.UTF-8"}
        r = subprocess.run(["git"], env=env, capture_output=True, text=True)
        return "Verwendung" in (r.stdout + r.stderr)

    def test_check_three_way_conflict_under_german_locale(self):
        """--check --3way on an overlapping edit fails even when git's messages are German."""
        if not self._git_speaks_german():
            self.skipTest("German git translations not installed")
        self.launch_and_edit_line6("de-check")
        (self.repo / "tracked.txt").write_text(
            "line1\nline2\nline3\nline4\nline5-main\nline6\nline7\n")
        de_env = {**self.env, "LANGUAGE": "de", "LANG": "de_DE.UTF-8", "LC_ALL": "de_DE.UTF-8"}
        r = subprocess.run(
            [sys.executable, str(CLI), "apply", "de-check", "--check", "--3way"],
            cwd=self.repo, env=de_env, text=True, capture_output=True)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("applies cleanly", r.stdout)
        self.assertFalse((self.root("de-check") / "applied").exists())
        self.cli("cleanup", "de-check", "--force", "--purge")

    def test_three_way_binary_conflict_keeps_ours_without_markers(self):
        """Binary file changed on both sides: non-zero, no applied, ours content, no markers."""
        (self.repo / "blob.bin").write_bytes(b"\x00\x01\x02\x03")
        self.git("add", "blob.bin")
        self.git("commit", "-qm", "binary")
        self.cli("launch", "codex", "--brief", self.brief, "--name", "bin")
        meta = runs.load_meta(self.root("bin"))
        wt = Path(meta["worktree"])
        (wt / "tracked.txt").write_text("line1\nline2\nline3\nline4\nline5\nline6\nline7\n")
        (wt / "new-codex.txt").unlink(missing_ok=True)
        (wt / "blob.bin").write_bytes(b"\x00\x01\x02\xff")
        ours = b"\x00\x01\xaa\x03"
        (self.repo / "blob.bin").write_bytes(ours)
        index_before = self.index_bytes()
        r = self.cli_raw("apply", "bin", "--3way")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse((self.root("bin") / "applied").exists())
        self.assertEqual(self.index_bytes(), index_before)
        self.assertEqual((self.repo / "blob.bin").read_bytes(), ours)
        self.assertNotIn(b"<<<<<<<", (self.repo / "blob.bin").read_bytes())
        combined = r.stdout + r.stderr
        self.assertIn("blob.bin", combined)
        self.assertIn("no conflict markers", combined.lower())
        self.assertIn("ours", combined.lower())
        self.cli("cleanup", "bin", "--force", "--purge")


if __name__ == "__main__":
    unittest.main()

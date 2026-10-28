"""delegate review --working-tree: snapshot of main-checkout dirty state."""
import argparse
import io
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


class WorkingTreeReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="delegate-wt-review-")
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
        self.git("config", "user.name", "WT Review Test")
        self.git("config", "user.email", "test@example.invalid")
        (self.repo / "tracked.txt").write_text("base\n")
        (self.repo / ".gitignore").write_text("ignored.txt\ndata/cache\n")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD")
        self.brief = self.tmp / "brief.md"
        self.brief.write_text("# Intent\nReview the uncommitted edits.\n")
        for p in (patch.object(runs, "RUNS_ROOT", self.storage),
                  patch.dict(os.environ, {"CLAUDE_SESSION_LOG": self.env["CLAUDE_SESSION_LOG"]})):
            p.start()
            self.addCleanup(p.stop)

    def git(self, *args, cwd=None):
        r = subprocess.run(["git", *args], cwd=cwd or self.repo, env=self.env,
                           text=True, capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def cli(self, *args, code=0, extra_env=None):
        r = subprocess.run([sys.executable, str(CLI), *map(str, args)], cwd=self.repo,
                           env={**self.env, **(extra_env or {})}, text=True, capture_output=True)
        self.assertEqual(r.returncode, code, f"{args}\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}")
        self.assertNotIn("Traceback", r.stdout + r.stderr)
        return r.stdout

    def cli_raw(self, *args, extra_env=None):
        return subprocess.run([sys.executable, str(CLI), *map(str, args)], cwd=self.repo,
                              env={**self.env, **(extra_env or {})}, text=True, capture_output=True)

    def root(self, name):
        return self.storage / self.repo.name / name

    def object_files(self):
        obj = self.repo / ".git" / "objects"
        return {p.relative_to(obj) for p in obj.rglob("*") if p.is_file()}

    def dirty_main(self):
        """One modified tracked, one staged, one untracked, one gitignored."""
        (self.repo / "tracked.txt").write_text("base\nmodified\n")
        (self.repo / "staged.txt").write_text("staged new\n")
        self.git("add", "staged.txt")
        (self.repo / "untracked.txt").write_text("untracked\n")
        (self.repo / "ignored.txt").write_text("secret dump\n")

    def test_dirty_tree_patch_and_index_untouched(self):
        self.dirty_main()
        index = Path(self.git("rev-parse", "--git-path", "index"))
        if not index.is_absolute():
            index = self.repo / index
        index_before = index.read_bytes()
        head_before = self.git("rev-parse", "HEAD")
        out = self.cli("review", "--working-tree", "--brief", self.brief, "--name", "wt1",
                       "--no-fallback")
        self.assertIn("review 1", out)
        self.assertIn("review 2", out)
        root = self.root("wt1")
        meta = runs.load_meta(root)
        self.assertEqual(meta["kind"], "working-tree")
        self.assertEqual(meta["agent"], "claude")
        self.assertEqual(meta["base"], self.base)
        self.assertIn("snapshot", meta)
        self.assertFalse(meta.get("branch"))
        patch = (root / "wt/.delegate/review-1.patch").read_text(errors="replace")
        self.assertIn("tracked.txt", patch)
        self.assertIn("+modified", patch)
        self.assertIn("staged.txt", patch)
        self.assertIn("+staged new", patch)
        self.assertIn("untracked.txt", patch)
        self.assertIn("+untracked", patch)
        self.assertNotIn("ignored.txt", patch)
        self.assertNotIn("secret dump", patch)
        self.assertEqual(index.read_bytes(), index_before)
        self.assertEqual(self.git("rev-parse", "HEAD"), head_before)
        # both default reviewers
        self.assertEqual(runs.load_meta(root / "review-1")["agent"], "codex")
        self.assertEqual(runs.load_meta(root / "review-1")["effort"], "xhigh")
        self.assertEqual(runs.load_meta(root / "review-2")["agent"], "cursor")
        self.assertEqual(runs.load_meta(root / "review-2")["model"], "auto")
        prompt = (root / "review-1" / "prompt.md").read_text()
        self.assertIn("orchestrator (Claude) made these uncommitted changes", prompt)
        self.assertNotIn("verify command and the last 80 lines", prompt)

    def test_base_older_commit(self):
        (self.repo / "tracked.txt").write_text("base\nv2\n")
        self.git("add", "tracked.txt")
        self.git("commit", "-qm", "v2")
        older = self.base
        (self.repo / "tracked.txt").write_text("base\nv2\ndirty\n")
        out = self.cli("review", "--working-tree", "--brief", self.brief, "--name", "wt-base",
                       "--base", older, "--agent", "cursor", "--no-fallback")
        self.assertIn("review 1", out)
        meta = runs.load_meta(self.root("wt-base"))
        self.assertEqual(meta["base"], older)
        patch = (self.root("wt-base") / "wt/.delegate/review-1.patch").read_text()
        self.assertIn("+v2", patch)
        self.assertIn("+dirty", patch)

    def test_clean_tree_exits(self):
        r = self.cli_raw("review", "--working-tree", "--brief", self.brief, "--name", "clean",
                         "--no-fallback")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no changes to review", r.stderr)
        self.assertFalse(self.root("clean").exists())

    def test_status_wait_cleanup_and_cost(self):
        self.dirty_main()
        out = self.cli("review", "--working-tree", "--brief", self.brief, "--name", "wt-life",
                       "--no-fallback")
        root = self.root("wt-life")
        self.assertEqual(runs.state(root), "ready")
        # non-detach dual review prints both packets itself
        self.assertIn("review 1", out)
        self.assertIn("review 2", out)
        status = self.cli("status", "wt-life")
        self.assertIn("working-tree", status)
        self.assertEqual(runs.state(root / "review-1"), "done")
        self.assertEqual(runs.state(root / "review-2"), "done")
        self.assertIn("review 1", self.cli("status", str(root / "review-1")))
        self.assertIn("review 2", self.cli("status", str(root / "review-2")))
        wait = self.cli("wait", "wt-life", "--interval", "0.05", "--timeout", "5")
        self.assertIn("review", wait)
        apply = self.cli_raw("apply", "wt-life")
        self.assertNotEqual(apply.returncode, 0)
        self.assertIn("nothing to apply", apply.stderr)
        follow = self.cli_raw("followup", "wt-life", "--brief", self.brief)
        self.assertNotEqual(follow.returncode, 0)
        self.assertIn("nothing to resume", follow.stderr)
        self.cli("cleanup", "wt-life")  # no --force
        self.assertFalse((root / "wt").exists())
        record = json.loads((self.storage / "runs.jsonl").read_text().splitlines()[0])
        self.assertEqual(record["outcome"], "reviewed")
        self.assertEqual(record["agent"], "claude")
        self.assertEqual(record["rounds"], [])
        self.assertEqual(len(record["reviews"]), 2)
        # cost / unused must read the row without error
        cost = subprocess.run([sys.executable, str(CLI), "cost", "--repo", self.repo.name],
                              cwd=self.repo, env=self.env, text=True, capture_output=True)
        self.assertNotIn("Traceback", cost.stdout + cost.stderr, cost.stderr)
        unused = subprocess.run([sys.executable, str(CLI), "quota", "--unused", "--since", "1970-01-01"],
                                cwd=self.repo, env=self.env, text=True, capture_output=True)
        self.assertNotIn("Traceback", unused.stdout + unused.stderr, unused.stderr)

    def test_untracked_limit_refuses_without_writing_objects(self):
        before = self.object_files()
        for i in range(101):
            (self.repo / f"dump-{i}.bin").write_bytes(b"x")
        r = self.cli_raw("review", "--working-tree", "--brief", self.brief, "--name", "too-many",
                         "--no-fallback")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("101 untracked", r.stderr)
        self.assertIn("--all-untracked", r.stderr)
        self.assertFalse(self.root("too-many").exists())
        self.assertEqual(self.object_files(), before)
        # same with --all-untracked succeeds
        self.cli("review", "--working-tree", "--brief", self.brief, "--name", "allowed",
                 "--all-untracked", "--agent", "cursor", "--no-fallback")
        self.assertTrue(self.root("allowed").exists())
        self.cli("cleanup", "allowed", "--purge")

    def test_untracked_size_limit(self):
        before = self.object_files()
        big = self.repo / "huge.bin"
        # sparse-ish: seek past 10 MiB without writing all zeros densely on all FS
        with big.open("wb") as f:
            f.truncate(runs.UNTRACKED_MAX_BYTES + 1)
        r = self.cli_raw("review", "--working-tree", "--brief", self.brief, "--name", "too-big",
                         "--no-fallback")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--all-untracked", r.stderr)
        self.assertFalse(self.root("too-big").exists())
        self.assertEqual(self.object_files(), before)

    def test_agent_cursor_only(self):
        self.dirty_main()
        self.cli("review", "--working-tree", "--brief", self.brief, "--name", "one",
                 "--agent", "cursor", "--no-fallback")
        root = self.root("one")
        self.assertTrue((root / "review-1").is_dir())
        self.assertFalse((root / "review-2").exists())
        self.assertEqual(runs.load_meta(root / "review-1")["agent"], "cursor")

    def test_brief_required_and_forbidden_on_run(self):
        self.dirty_main()
        r = self.cli_raw("review", "--working-tree", "--name", "nobrief", "--no-fallback")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--brief", r.stderr)
        self.cli("launch", "codex", "--brief", self.brief, "--name", "built")
        r2 = self.cli_raw("review", "built", "--brief", self.brief)
        self.assertNotEqual(r2.returncode, 0)
        self.assertIn("--brief is only valid with --working-tree", r2.stderr)

    def test_link_and_cleanup_releases(self):
        cache = self.repo / "data" / "cache"
        cache.mkdir(parents=True)
        (cache / "input").write_text("fixture\n")
        self.dirty_main()
        self.cli("review", "--working-tree", "--brief", self.brief, "--name", "linked",
                 "--link", "data/cache", "--agent", "cursor", "--no-fallback")
        root = self.root("linked")
        wt = root / "wt"
        self.assertTrue((wt / "data/cache").is_symlink())
        self.assertEqual((wt / "data/cache").resolve(), cache)
        self.assertEqual(runs.load_meta(root)["links"], ["data/cache"])
        # already gitignored → not in meta["excluded"] (same as launch); symlink still released
        self.cli("cleanup", "linked")
        self.assertFalse(wt.exists())
        self.assertEqual(runs.load_meta(root).get("excluded"), [])

    def test_run_review_prompt_byte_identical(self):
        """delegate review NAME prompt stays the REVIEW_PROMPT template for the same inputs."""
        self.assertTrue(runs.REVIEW_PROMPT.endswith(runs.REVIEW_PROMPT_CHECKS))
        self.cli("launch", "codex", "--brief", self.brief, "--name", "prompt-id",
                 "--verify", "true")
        root = self.root("prompt-id")
        meta = runs.load_meta(root)
        wt = Path(meta["worktree"])
        brief = (root / "brief.md").read_text().rstrip()
        # other vendor is cursor → empty shell_note (matches review() for agent=cursor)
        expected = runs.REVIEW_PROMPT.format(
            wt=wt, base=meta["base"][:9], n=1, shell_note="",
            brief=brief, focus="", omit_note="")
        self.cli("review", "prompt-id")  # default: other vendor = cursor
        actual = (root / "review-1" / "prompt.md").read_text()
        self.assertEqual(actual, expected)

    def _wt_args(self, **kwargs):
        base = dict(no_fallback=False, effort=None, agent=None, model=None,
                    brief=str(self.brief), run=None, name="wt", base=None, link=[],
                    all_untracked=False, focus=None, patch_max_kb=200, detach=True,
                    dry_run=False, root=None, repo=str(self.repo), working_tree=True)
        base.update(kwargs)
        return argparse.Namespace(**base)

    def test_fallback_swap_via_quota(self):
        """Real ask._fallback_for_both with quota/frontier patched; no network."""
        self.dirty_main()
        pools = {
            "codex-5h": {"left_pct": 5, "age_min": 0},
            "codex-7d": {"left_pct": 50, "age_min": 0},
        }
        selection = {
            "row": {"model": "deepseek-fallback-test", "agent": "cursor",
                    "intelligence": 40.0, "cost_per_task": 0.1, "pool": "cursor-premium"},
            "cap": 0.2,
            "replaces": {"model": "gpt-6.1-sol", "effort": "medium", "cost_per_task": 0.1},
        }
        report = {"fallback": selection, "fallback_reason": "", "warnings": []}
        started = []
        with patch("quota.fetch", return_value={"ok": True}), \
             patch("quota.pools", return_value=pools), \
             patch("frontier.load_report", return_value=report), \
             patch.object(runs.quota, "spawn_snapshot"), \
             patch.object(runs, "start_jobs", side_effect=lambda jobs, d, n: started.extend(jobs)), \
             patch("sys.stdout", io.StringIO()):
            runs.review_working_tree(self._wt_args(name="fb"))
        root = self.root("fb")
        self.assertEqual(len(started), 2)
        self.assertEqual(runs.load_meta(root / "review-1")["agent"], "cursor")
        self.assertEqual(runs.load_meta(root / "review-1")["model"], "deepseek-fallback-test")
        self.assertEqual(runs.load_meta(root / "review-2")["agent"], "cursor")
        self.assertEqual(runs.load_meta(root / "review-2")["model"], "auto")

    def test_dry_run_writes_nothing(self):
        self.dirty_main()
        before = self.object_files()
        out = self.cli("review", "--working-tree", "--brief", self.brief, "--name", "dry",
                       "--dry-run")
        self.assertIn("[dry run]", out)
        self.assertIn("prompt.md", out)
        self.assertIn("untracked:", out)
        self.assertIn("swapped for a Cursor model", out)
        self.assertIn("codex gpt-6.1-sol", out)
        self.assertIn("cursor auto", out)
        self.assertFalse(self.root("dry").exists())
        self.assertEqual(self.object_files(), before)
        # in-process: dry-run must not call the fallback helper (would refresh ratings cache)
        buf = io.StringIO()
        with patch("ask._fallback_for_both", side_effect=AssertionError("fallback must not run")), \
             patch("sys.stdout", buf):
            runs.review_working_tree(self._wt_args(name="dry-ip", dry_run=True, detach=False))
        self.assertIn("[dry run]", buf.getvalue())
        self.assertFalse(self.root("dry-ip").exists())

    def test_wait_prints_all_working_tree_reviews_when_idle(self):
        """A failed earlier review is not hidden when none were busy during wait."""
        self.dirty_main()
        self.cli("review", "--working-tree", "--brief", self.brief, "--name", "wait-hide",
                 "--no-fallback")
        root = self.root("wait-hide")
        (root / "review-1" / "exit_code").write_text("1\n")
        # Make review-2 look started later so the old "latest only" path would pick it.
        later = "2099-01-01T00:00:00+00:00\n"
        (root / "review-2" / "started_at").write_text(later)
        r = self.cli_raw("wait", "wait-hide", "--interval", "0.05", "--timeout", "2")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("review 1", r.stdout)
        self.assertIn("review 2", r.stdout)
        self.assertIn("failed rc=1", r.stdout)

    def test_wait_prints_review_that_failed_before_wait_attached(self):
        """review-1 already failed while review-2 was still busy: both packets, exit 1."""
        self.dirty_main()
        self.cli("review", "--working-tree", "--brief", self.brief, "--name", "wait-early",
                 "--no-fallback")
        root = self.root("wait-early")
        (root / "review-1" / "exit_code").write_text("1\n")
        real_busy = runs._job_busy
        first_poll = {"review-2"}

        def busy(job):
            if job.name in first_poll:
                first_poll.discard(job.name)
                return True
            return real_busy(job)

        buf = io.StringIO()
        with patch.object(runs, "_job_busy", side_effect=busy), patch("sys.stdout", buf), \
             self.assertRaises(SystemExit) as cm:
            runs.wait(argparse.Namespace(runs=[str(root)], timeout=5, interval=0.05))
        out = buf.getvalue()
        self.assertEqual(cm.exception.code, 1, out)
        self.assertIn("review 1", out)
        self.assertIn("review 2", out)
        self.assertIn("failed rc=1", out)

    def test_refuse_non_ignored_link(self):
        folder = self.repo / "live-input"
        folder.mkdir()
        (folder / "data").write_text("x\n")
        (self.repo / "tracked.txt").write_text("base\ndirty\n")
        before = self.object_files()
        r = self.cli_raw("review", "--working-tree", "--brief", self.brief, "--name", "badlink",
                         "--link", "live-input", "--agent", "cursor", "--no-fallback")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("so the snapshot already holds it", r.stderr)
        self.assertFalse(self.root("badlink").exists())
        self.assertEqual(self.object_files(), before)

    def test_link_ignored_only_by_info_exclude(self):
        """Another run's `/<path>` exclude line hides the path from `git add -A`, so it may be linked."""
        folder = self.repo / "excl-input"
        folder.mkdir()
        (folder / "data").write_text("x\n")
        with (self.repo / ".git" / "info" / "exclude").open("a") as f:
            f.write("/excl-input\n")
        (self.repo / "tracked.txt").write_text("base\ndirty\n")
        self.cli("review", "--working-tree", "--brief", self.brief, "--name", "excl",
                 "--link", "excl-input", "--agent", "cursor", "--no-fallback")
        self.assertTrue((self.root("excl") / "wt" / "excl-input").is_symlink())
        self.cli("cleanup", "excl", "--purge")

    def test_untracked_only_with_show_untracked_no(self):
        self.git("config", "status.showUntrackedFiles", "no")
        (self.repo / "only-new.txt").write_text("fresh\n")
        # porcelain without --untracked-files=all is empty under this setting
        bare = subprocess.run(["git", "status", "--porcelain"], cwd=self.repo, env=self.env,
                              text=True, capture_output=True)
        self.assertEqual(bare.stdout.strip(), "")
        self.cli("review", "--working-tree", "--brief", self.brief, "--name", "hidden-u",
                 "--agent", "cursor", "--no-fallback")
        patch = (self.root("hidden-u") / "wt/.delegate/review-1.patch").read_text()
        self.assertIn("only-new.txt", patch)
        self.assertIn("+fresh", patch)


if __name__ == "__main__":
    unittest.main()

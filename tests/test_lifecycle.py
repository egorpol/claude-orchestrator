"""End-to-end delegate lifecycle; no installed agents or agent quota required."""
import importlib.machinery
import importlib.util
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
CODEX_USAGE = {"input": 210, "cached_input": 90, "cache_write": 0, "output": 30, "reasoning_output": 12}
CURSOR_USAGE = {"input": 110, "cached_input": 900, "cache_write": 30, "output": 25, "reasoning_output": None}


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="delegate-lifecycle-")
        self.addCleanup(self.temp.cleanup)
        self.tmp = Path(self.temp.name)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.storage = self.tmp / "runs"
        self.env = {**os.environ, "PATH": str(FAKES) + os.pathsep + os.environ["PATH"],
                    "CLAUDE_DELEGATE_ROOT": str(self.storage), "CLAUDECODE": "fixture",
                    "VSCODE_FAKE": "fixture", "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_CONFIG_NOSYSTEM": "1"}
        self.env.pop("FAKE_AGENT_EXIT", None)
        self.git("init", "-q")
        self.git("config", "user.name", "Lifecycle Test")
        self.git("config", "user.email", "test@example.invalid")
        (self.repo / "tracked.txt").write_text("base\n")
        (self.repo / ".gitignore").write_text("data/cache\nother-cache\n")
        self.git("add", ".")
        self.git("commit", "-qm", "test fixture base")
        self.brief = self.tmp / "brief.md"
        self.brief.write_text("# Task\nMake a deterministic change.\n")
        self.follow_brief = self.tmp / "follow.md"
        self.follow_brief.write_text("# Followup\nContinue the change.\n")
        p = patch.object(runs, "RUNS_ROOT", self.storage)
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

    def root(self, name):
        return self.storage / self.repo.name / name

    def launch(self, agent="codex", name="test", *args, code=0, extra_env=None):
        return self.cli("launch", agent, "--brief", self.brief, "--name", name,
                        *args, code=code, extra_env=extra_env)

    def last_packet(self, output):
        packet = output[output.rfind("== "):].strip()
        self.assertTrue(packet.startswith("== "), output)
        self.assertLessEqual(len(packet.splitlines()), 20)
        self.assertTrue(packet.splitlines()[-1].startswith("artifacts:"))
        return packet

    def snapshot(self, root):
        return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in root.rglob("*") if p.is_file() and not p.is_symlink()}

    def lifecycle(self, agent):
        name = agent + "-life"
        expected = CODEX_USAGE if agent == "codex" else CURSOR_USAGE
        output = self.launch(agent, name, "--verify", "printf 'verified\\n'")
        root = self.root(name)
        wt = root / "wt"
        packet = self.last_packet(output)
        self.assertIn("round 1 | done", packet)
        self.assertIn("exit 0 | verify pass", packet)
        self.assertIn(f"new-{agent}.txt: +1/-0", packet)  # untracked file, before diff stages it
        self.assertIn("files: 2 | +2/-0 total", packet)
        self.assertIn("conflicts: none", packet)
        self.assertIn("risks: none", packet)
        meta = runs.load_meta(root)
        self.assertEqual([r["n"] for r in meta["history"]], [1])
        self.assertEqual(meta["history"][0]["usage"], expected)
        self.assertEqual(meta["history"][0]["verify"], 0)
        self.assertEqual(set(meta["history"][0]), {"n", "model", "effort", "started", "minutes", "exit", "verify", "usage"})
        self.assertNotIn("Earlier narration", (root / "last-message.md").read_text())
        before = self.snapshot(root)
        index = Path(self.git("rev-parse", "--git-path", "index", cwd=wt))
        if not index.is_absolute():
            index = wt / index
        index_before = (index.read_bytes(), index.stat().st_mtime_ns)
        self.assertEqual(self.cli("status", name).strip(), packet)
        full = self.cli("status", name, "--full")
        self.assertIn("-- log tail --", full)
        self.assertIn((root / "last-message.md").read_text().strip(), full)
        self.assertEqual(self.snapshot(root), before)
        self.assertEqual((index.read_bytes(), index.stat().st_mtime_ns), index_before)
        self.assertFalse((root / "changes.patch").exists())
        if agent == "codex":
            self.assertIn("command: git diff --check | exit 0", full)
            self.assertIn("files: tracked.txt; new-codex.txt", full)
            self.assertIn("future_item", full)
            self.assertNotIn("Reading additional input from stdin", full)
            self.assertNotIn('"item"', full)
        else:
            self.assertIn("tool completed: readToolCall tracked.txt", full)
        self.cli("_finalize", root)
        self.assertEqual(len(runs.load_meta(root)["history"]), 1)  # retry-safe finalization

        for link in ("data/cache", "other-cache"):
            (self.repo / link).mkdir(parents=True)
            (self.repo / link / "input").write_text("read-only fixture input\n")
        output = self.cli("followup", name, "--brief", self.follow_brief,
                          "--link", "data/cache", "--link", "other-cache", "--link", "data/cache")
        self.assertIn("round 2 | done", self.last_packet(output))
        meta = runs.load_meta(root)
        self.assertEqual([r["n"] for r in meta["history"]], [1, 2])
        self.assertEqual([r["usage"] for r in meta["history"]], [expected, expected])
        self.assertEqual(meta["links"], ["data/cache", "other-cache"])
        self.assertTrue((wt / "data/cache").is_symlink())
        self.assertEqual((wt / "data/cache").resolve(), self.repo / "data/cache")
        self.assertIn("/data/cache", (self.repo / ".git/info/exclude").read_text())
        self.assertNotIn("data/cache", self.git("status", "--porcelain", cwd=wt))
        self.assertTrue((root / "round-1/log").exists())
        self.assertIn("fake-" + agent, (root / "run.sh").read_text())

        output = self.cli("review", name)
        review_packet = self.last_packet(output)
        self.assertIn("review 1 | done", review_packet)
        rv = root / "review-1"
        rv_meta = runs.load_meta(rv)
        verdict = "CHANGES REQUESTED" if agent == "codex" else "APPROVE"
        review_usage = CURSOR_USAGE if agent == "codex" else CODEX_USAGE
        self.assertIn(f"verdict: {verdict}", review_packet)
        self.assertIn("high 1, medium 1, low 1" if agent == "codex" else "high 0, medium 0, low 0", review_packet)
        self.assertEqual(rv_meta["verdict"], verdict)
        self.assertEqual(rv_meta["usage"], review_usage)
        self.assertIsInstance(rv_meta["minutes"], (int, float))
        self.assertEqual(self.cli("status", rv).strip(), review_packet)
        verify_packet = wt / ".delegate/review-1.verify.log"
        self.assertIn("verified", verify_packet.read_text())
        self.assertIn(".delegate/review-1.verify.log", (rv / "prompt.md").read_text())
        self.assertEqual((rv / "pre_digest").read_bytes(), (rv / "post_digest").read_bytes())
        self.assertEqual((wt / "tracked.txt").read_text(), f"base\n{agent} round\n{agent} round\n")
        self.assertIn(verdict, self.last_packet(self.cli("status", name)))
        self.assertEqual(len(self.cli("status").splitlines()), 1)

        diff = self.cli("diff", name, "--full")
        self.assertIn(f"new-{agent}.txt", diff)
        self.assertIn("diff --git", diff)
        self.cli("apply", name, "--check")
        self.assertFalse((self.storage / "runs.jsonl").exists())
        applied = self.cli("apply", name)
        self.assertIn(f"logged to {self.storage / 'runs.jsonl'}.", applied)
        self.assertIn("write a handoff and start a fresh orchestrator session.", applied)
        self.assertEqual((self.repo / "tracked.txt").read_text(), (wt / "tracked.txt").read_text())
        records = [json.loads(l) for l in (self.storage / "runs.jsonl").read_text().splitlines()]
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["v"], 1)
        self.assertEqual(record["outcome"], "applied")
        self.assertEqual(record["mode"], "workspace-write" if agent == "codex" else "sandbox")
        self.assertEqual((record["files"], record["insertions"], record["deletions"]), (2, 4, 0))
        self.assertEqual(record["rounds"], meta["history"])
        self.assertEqual(record["reviews"][0]["usage"], review_usage)
        self.assertEqual(record["reviews"][0]["verdict"], verdict)
        self.assertEqual(record["repo_path"], str(self.repo))
        self.assertEqual(record["links"], meta["links"])
        self.cli("cleanup", name)
        self.assertFalse(wt.exists())
        self.assertTrue((root / "log").exists())
        self.assertIn(f"new-{agent}.txt", self.cli("diff", name))
        removed_verify = subprocess.run([sys.executable, str(CLI), "verify", name], cwd=self.repo,
                                        env=self.env, text=True, capture_output=True)
        self.assertNotEqual(removed_verify.returncode, 0)
        self.assertIn("worktree was removed", removed_verify.stderr)
        self.assertNotIn("Traceback", removed_verify.stderr)
        self.last_packet(self.cli("status", name))
        self.cli("cleanup", name, "--purge")
        self.assertFalse(root.exists())
        self.assertEqual(len((self.storage / "runs.jsonl").read_text().splitlines()), 1)

    def test_codex_lifecycle(self):
        self.lifecycle("codex")

    def test_cursor_lifecycle(self):
        self.lifecycle("cursor")

    def test_discard_safety_and_purge_accounting(self):
        self.launch()
        root = self.root("test")
        rejected = subprocess.run([sys.executable, str(CLI), "cleanup", "test", "--purge"],
                                  cwd=self.repo, env=self.env, text=True, capture_output=True)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("changes that were not applied", rejected.stderr)
        self.assertTrue((root / "wt").exists())
        self.assertFalse((self.storage / "runs.jsonl").exists())
        self.cli("cleanup", "test", "--purge", "--force")
        self.assertFalse(root.exists())
        record = json.loads((self.storage / "runs.jsonl").read_text())
        self.assertEqual(record["outcome"], "discarded")
        self.assertEqual((record["files"], record["insertions"]), (2, 2))
        self.assertEqual(record["rounds"][0]["usage"], CODEX_USAGE)

    def test_verify_tails_and_adhoc_output(self):
        cmd = "for i in {1..100}; do echo verify-$i; done"
        self.launch("codex", "verify", "--verify", cmd)
        root = self.root("verify")
        output = self.cli("verify", "verify")
        self.assertEqual(len(output.splitlines()), 31)
        self.assertTrue(output.startswith("verify-71\n"))
        self.assertTrue(output.endswith("verify: pass\n"))
        self.assertIn("verify-1\n", (root / "verify.log").read_text())
        original = (root / "verify.log").read_bytes()
        output = self.cli("verify", "verify", "--cmd", cmd + "; exit 2", code=2)
        self.assertIn("verify: FAIL rc=2", output)
        self.assertEqual(len(output.splitlines()), 31)
        self.assertEqual((root / "verify.log").read_bytes(), original)
        self.assertIn("verify-1\n", (root / "verify-adhoc.log").read_text())
        self.assertEqual((root / "verify_code").read_text().strip(), "0")
        self.cli("review", "verify")
        rv_log = (root / "wt/.delegate/review-1.verify.log").read_text().splitlines()
        self.assertEqual(len(rv_log), 81)
        self.assertEqual(rv_log[1], "verify-21")

    def test_failures_still_finalize(self):
        output = self.launch("codex", "failed", "--verify", "echo failing; exit 3",
                             code=7, extra_env={"FAKE_AGENT_EXIT": "7"})
        packet = self.last_packet(output)
        self.assertIn("failed rc=7", packet)
        self.assertIn("verify FAIL rc=3", packet)
        entry = runs.load_meta(self.root("failed"))["history"][0]
        self.assertEqual((entry["exit"], entry["verify"]), (7, 3))
        self.assertEqual(entry["usage"], CODEX_USAGE)

    def test_read_only_agents_and_fresh_followup(self):
        for agent in ("codex", "cursor"):
            with self.subTest(agent=agent):
                self.launch(agent, agent, "--read-only")
                root = self.root(agent)
                self.assertEqual(self.git("status", "--porcelain", cwd=root / "wt"), "")
                self.cli("followup", agent, "--brief", self.follow_brief, "--fresh")
                self.assertIn("read-only task", (root / "prompt.md").read_text())
                self.assertEqual(self.git("status", "--porcelain", cwd=root / "wt"), "")
                self.assertEqual(len(runs.load_meta(root)["history"]), 2)

    def test_followup_backfills_round_of_legacy_launcher(self):
        # runs started before _finalize existed have no history entry for their current round
        self.launch("codex", "legacy")
        root = self.root("legacy")
        meta = runs.load_meta(root)
        meta["history"] = []
        runs.save_meta(root, meta)
        self.cli("followup", "legacy", "--brief", self.follow_brief)
        self.assertEqual([r["n"] for r in runs.load_meta(root)["history"]], [1, 2])

    def test_full_status_of_review_job(self):
        self.launch("codex", "rv")
        self.cli("review", "rv")
        full = self.cli("status", "rv/review-1", "--full")
        self.assertIn("review 1 | done", full)
        self.assertIn("-- last message --", full)

    def test_state_reports_verifying_until_finished(self):
        job = self.tmp / "job"
        job.mkdir()
        (job / "exit_code").write_text("0\n")
        (job / "pid").write_text(f"{os.getpid()}\n")
        self.assertEqual(runs.state(job), "verifying")
        (job / "finished_at").write_text("now\n")
        self.assertEqual(runs.state(job), "done")

    def test_followup_preserves_model_accounting_and_existing_links(self):
        (self.repo / "data/cache").mkdir(parents=True)
        (self.repo / "data/cache/input").write_text("fixture\n")
        self.launch("codex", "linked", "--link", "data/cache")
        self.cli("followup", "linked", "--brief", self.follow_brief, "--link", "data/cache",
                 "--model", "another-model")
        meta = runs.load_meta(self.root("linked"))
        self.assertEqual(meta["links"], ["data/cache"])
        self.assertEqual([(r["n"], r["model"], r["effort"]) for r in meta["history"]],
                         [(1, "gpt-6.1-sol", "xhigh"), (2, "another-model", None)])

    def test_legacy_codex_status_read_only(self):
        root = self.root("legacy")
        root.mkdir(parents=True)
        base = self.git("rev-parse", "HEAD")
        runs.save_meta(root, {"name": "legacy", "agent": "codex", "model": "old-model", "rounds": 2,
                              "repo": str(self.repo), "worktree": str(root / "gone"), "base": base,
                              "history": [{"round": 1, "model": "old-model", "session": "old-session"}]})
        (root / "log").write_text("OpenAI Codex\nsession id: old-session\nthinking\nlegacy answer\ntokens used\n12,345\n")
        (root / "exit_code").write_text("0\n")
        (root / "started_at").write_text("2026-10-09T12:00:00+02:00\n")
        (root / "finished_at").write_text("2026-10-09T12:01:30+02:00\n")
        (root / "last-message.md").write_text("## Open questions / conflicts\n\n- one\n- two\n- three\n- four\n")
        (root / "changes.patch").write_text("diff --git a/new.txt b/new.txt\nnew file mode 100644\n--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1 @@\n+new\n")
        before = self.snapshot(root)
        compact = self.last_packet(self.cli("status", "legacy"))
        self.assertIn("usage: 12,345 total reported (legacy Codex; cumulative session total)", compact)
        self.assertIn("1m30s", compact)
        self.assertIn("new.txt: +1/-0", compact)
        self.assertIn("conflicts/risks: - three", compact)
        self.assertNotIn("four", compact)
        full = self.cli("status", "legacy", "--full")
        self.assertIn("legacy answer", full)
        self.assertIn("- four", full)
        self.assertEqual(runs.session_id(root, "codex"), "old-session")
        self.assertEqual(runs.usage(root, "codex"), {"total_reported": 12345})
        self.assertEqual(self.snapshot(root), before)

    def test_packet_bounds_top_churn_and_binary_files(self):
        self.launch()
        root = self.root("test")
        wt = root / "wt"
        for i in range(12):
            (wt / f"extra-{i}.txt").write_text("line\n" * (i + 1))
        (wt / "binary.dat").write_bytes(b"\0\x01\x02")
        (wt / "weird\tname.txt").write_text("one\n")
        (wt / "trailing\n").write_text("one\n")
        (wt / "\tleading.txt").write_text("one\n")
        self.cli("review", "test")
        last = root / "last-message.md"
        last.write_text("## Open questions / conflicts\n- a\n- b\n- c\n- d\n")
        before = self.snapshot(root)
        compact = self.last_packet(self.cli("status", "test"))
        self.assertIn("extra-11.txt: +12/-0", compact)
        self.assertNotIn("extra-0.txt:", compact)
        self.assertIn("files: 18 | +83/-0 total", compact)
        self.assertEqual(self.snapshot(root), before)
        stats = runs.change_stats(root, runs.load_meta(root))
        self.assertTrue(next(r for r in stats if r["path"] == "binary.dat")["binary"])
        self.assertIn("weird\tname.txt", [r["path"] for r in stats])
        self.assertIn("trailing\n", [r["path"] for r in stats])
        self.assertIn("\tleading.txt", [r["path"] for r in stats])

    def test_digest_preserves_multiple_conflicts_and_risks(self):
        self.launch()
        root = self.root("test")
        (root / "last-message.md").write_text(
            "## Open questions / conflicts\nwrong fallback\n## Digest\nfiles: tracked.txt (+1/-0)\n"
            "verify: git diff --check -> pass\nconflicts: first\n- second\nconflicts: third\n"
            "risks: tracked.txt:1 - first risk\n- tracked.txt:2 - second risk\n")
        compact = self.last_packet(self.cli("status", "test"))
        self.assertIn("conflicts: first; - second; third", compact)
        self.assertIn("risks: tracked.txt:1 - first risk; - tracked.txt:2 - second risk", compact)
        self.assertNotIn("wrong fallback", compact)

    def test_cleanup_rejects_changes_since_apply(self):
        self.launch()
        self.cli("apply", "test")
        root = self.root("test")
        with (root / "wt/tracked.txt").open("a") as f:
            f.write("unapplied later change\n")
        result = subprocess.run([sys.executable, str(CLI), "cleanup", "test"], cwd=self.repo,
                                env=self.env, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("changed since apply", result.stderr)
        self.assertTrue((root / "wt").exists())
        self.cli("cleanup", "test", "--force", "--purge")
        self.assertEqual(len((self.storage / "runs.jsonl").read_text().splitlines()), 1)

    def test_dry_run_flags_and_optional_registration(self):
        codex = self.launch("codex", "x", "--dry-run")
        cursor = self.launch("cursor", "x", "--dry-run")
        self.assertIn("--json", codex)
        self.assertIn("-o ", codex)
        self.assertIn("--sandbox enabled", cursor)
        self.assertNotIn("--force", cursor)
        self.assertIn("export PYTHONDONTWRITEBYTECODE=1", cursor)
        self.assertFalse(self.storage.exists())
        self.assertIn("--sandbox disabled --force", self.launch("cursor", "x", "--unsandboxed", "--dry-run"))
        self.assertNotIn("_finalize", self.cli("--help"))
        for name in ("cost", "ask"):
            if (CLI.parent / (name + ".py")).exists():
                # the real module sits next to the CLI and wins over PYTHONPATH; check it registered
                self.assertIn(name, self.cli("--help"))
                continue
            module = self.tmp / (name + ".py")
            module.write_text("def register(sub):\n    s = sub.add_parser('" + name + "')\n    s.set_defaults(fn=lambda a: print('registered " + name + "'))\n")
            self.assertEqual(self.cli(name, extra_env={"PYTHONPATH": str(self.tmp)}).strip(), "registered " + name)

    def test_route_prefer_is_only_forwarded_when_given(self):
        loader = importlib.machinery.SourceFileLoader("delegate_lifecycle_cli", str(CLI))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        delegate = importlib.util.module_from_spec(spec)
        loader.exec_module(delegate)
        catalog = {"tasks": {"implement": {}}, "delegates": []}
        with patch.object(delegate, "load_catalog", return_value=catalog), \
             patch.object(delegate.quota, "fetch"), patch.object(delegate.quota, "pools", return_value={}), \
             patch.object(delegate.ratings, "load", return_value={"models": {}}), \
             patch.object(delegate.route, "rank", return_value=({}, [])) as rank, \
             patch.object(delegate.route, "render", return_value=""), patch("builtins.print"):
            with patch.object(sys, "argv", ["delegate", "route", "implement"]):
                delegate.main()
            self.assertNotIn("prefer", rank.call_args.kwargs)
            with patch.object(sys, "argv", ["delegate", "route", "implement", "--prefer", "cursor-auto,codex-sol"]):
                delegate.main()
            self.assertEqual(rank.call_args.kwargs["prefer"], ["cursor-auto", "codex-sol"])


if __name__ == "__main__":
    unittest.main()

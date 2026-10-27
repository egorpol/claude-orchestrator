"""End-to-end delegate lifecycle; no installed agents or agent quota required."""
import argparse
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
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
                    "CLAUDE_DELEGATE_QUOTA_SNAPSHOT": "0",
                    "CLAUDE_SESSION_LOG": str(self.tmp / "sessions.jsonl"),
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
        for p in (patch.object(runs, "RUNS_ROOT", self.storage),
                  patch.dict(os.environ, {"CLAUDE_SESSION_LOG": self.env["CLAUDE_SESSION_LOG"]})):
            p.start()
            self.addCleanup(p.stop)
        # Runs from inside Claude Code inherit its session id; tests set one where they need it.
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        self.env.pop("CLAUDE_CODE_SESSION_ID", None)

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
        self.assertIn("session", meta)
        self.assertIsNone(meta["session"])
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
        if agent == "codex":  # resumed thread totals are kept next to the per-round delta
            self.assertTrue(meta["history"][1]["resumed"])
            self.assertEqual(meta["history"][1]["usage_thread"]["input"], 2 * expected["input"])
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
        self.assertIn("session", record)
        self.assertIsNone(record["session"])
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

    def test_launch_session_provenance_survives_followup_from_another_session(self):
        self.launch(extra_env={"CLAUDE_CODE_SESSION_ID": "launch-session"})
        self.assertEqual(runs.load_meta(self.root("test"))["session"], "launch-session")
        self.cli("followup", "test", "--brief", self.follow_brief,
                 extra_env={"CLAUDE_CODE_SESSION_ID": "later-session"})
        self.cli("apply", "test", extra_env={"CLAUDE_CODE_SESSION_ID": "apply-session"})
        record = json.loads((self.storage / "runs.jsonl").read_text())
        self.assertEqual(record["session"], "launch-session")

    def test_old_launch_meta_without_session_logs_null(self):
        self.launch()
        root = self.root("test")
        meta = runs.load_meta(root)
        del meta["session"]
        runs.save_meta(root, meta)
        self.cli("apply", "test", extra_env={"CLAUDE_CODE_SESSION_ID": "apply-session"})
        self.assertIsNone(json.loads((self.storage / "runs.jsonl").read_text())["session"])

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

    def test_crlf_base_applies_byte_for_byte(self):
        # Field incident 2026-10-10: text-mode capture dropped the \r, so CRLF files never applied.
        (self.repo / "tracked.txt").write_bytes(b"base\r\nsecond\r\n")
        self.git("commit", "-qam", "crlf base")
        self.launch()
        root = self.root("test")
        self.cli("review", "test")
        self.assertIn(b"second\r\n", (root / "wt/.delegate/review-1.patch").read_bytes())
        self.cli("apply", "test")
        self.assertEqual((self.repo / "tracked.txt").read_bytes(), b"base\r\nsecond\r\ncodex round\n")

    def test_status_flags_day_old_applied_worktree(self):
        self.launch()
        self.assertIn("delegate cleanup test", self.cli("apply", "test"))
        self.assertNotIn("STALE", self.cli("status"))
        applied = self.root("test") / "applied"
        old = applied.stat().st_mtime - 2 * 86400
        os.utime(applied, (old, old))
        self.assertIn("STALE worktree", self.cli("status"))

    def test_cleanup_logs_runs_that_skip_apply_once(self):
        self.launch("codex", "ro", "--read-only")
        self.cli("cleanup", "ro")
        self.launch("cursor", "merged")
        self.cli("apply", "merged")
        self.cli("cleanup", "merged")
        records = [json.loads(l) for l in (self.storage / "runs.jsonl").read_text().splitlines()]
        self.assertEqual([(r["run"], r["outcome"]) for r in records], [("ro", "read-only"), ("merged", "applied")])
        self.assertEqual(records[0]["rounds"][0]["usage"], CODEX_USAGE)

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

    def _fixture_run(self, name):
        root = self.root(name)
        root.mkdir(parents=True)
        runs.save_meta(root, {
            "name": name, "agent": "codex", "model": "gpt-6.1-sol", "rounds": 1,
            "repo": str(self.repo), "worktree": str(root / "gone"),
            "base": self.git("rev-parse", "HEAD"), "history": [],
        })
        (root / "started_at").write_text("2026-10-11T00:00:00+00:00\n")
        return root

    def test_wait_holds_through_verifying(self):
        # Invariant 1: wait never returns while a job is verifying.
        root = self._fixture_run("wait-verifying")
        (root / "exit_code").write_text("0\n")
        (root / "pid").write_text(f"{os.getpid()}\n")
        self.assertEqual(runs.state(root), "verifying")
        threading.Timer(0.15, lambda: (root / "finished_at").write_text(
            "2026-10-11T00:01:00+00:00\n")).start()
        t0 = time.monotonic()
        out = self.cli("wait", "wait-verifying", "--interval", "0.05", "--timeout", "5")
        self.assertGreaterEqual(time.monotonic() - t0, 0.12)
        self.assertIn("| done |", out.splitlines()[0])

    def test_wait_returns_at_once_on_dead_job(self):
        # Invariant 2: wait never hangs on a dead job (died, no exit_code).
        root = self._fixture_run("wait-dead")
        dead = subprocess.Popen(["true"])
        dead.wait()
        (root / "pid").write_text(f"{dead.pid}\n")
        self.assertEqual(runs.state(root), "died (no exit code)")
        t0 = time.monotonic()
        out = self.cli("wait", "wait-dead", "--interval", "0.05", "--timeout", "30", code=1)
        self.assertLess(time.monotonic() - t0, 10)  # far below the timeout; CLI startup can be slow under load
        self.assertIn("died (no exit code)", out.splitlines()[0])

    def test_wait_picks_up_review_started_during_poll(self):
        # Invariant 3: re-list review-* every poll; a review that appears while
        # the round was the only busy job is also waited for.
        root = self._fixture_run("wait-review")
        (root / "pid").write_text(f"{os.getpid()}\n")
        self.assertEqual(runs.state(root), "running")

        def start_review_then_finish_root():
            # Create the busy review before clearing the root so a concurrent
            # poll never sees an empty busy set mid-transition.
            rv = root / "review-1"
            rv.mkdir()
            runs.save_meta(rv, {"name": "wait-review", "agent": "cursor", "model": "auto"})
            (rv / "pid").write_text(f"{os.getpid()}\n")
            (rv / "started_at").write_text("2026-10-11T00:02:00+00:00\n")
            (root / "exit_code").write_text("0\n")
            (root / "finished_at").write_text("2026-10-11T00:01:00+00:00\n")

        def finish_review():
            rv = root / "review-1"
            (rv / "exit_code").write_text("0\n")
            (rv / "finished_at").write_text("2026-10-11T00:03:00+00:00\n")

        threading.Timer(0.1, start_review_then_finish_root).start()
        threading.Timer(0.35, finish_review).start()
        t0 = time.monotonic()
        out = self.cli("wait", "wait-review", "--interval", "0.05", "--timeout", "5")
        self.assertGreaterEqual(time.monotonic() - t0, 0.3)
        self.assertIn("review 1", out)
        self.assertIn("| done |", out)

    def test_wait_timeout_prints_one_line_exit_3(self):
        # Invariant 4: timeout returns exit 3 and does not print full packets.
        root = self._fixture_run("wait-timeout")
        (root / "pid").write_text(f"{os.getpid()}\n")
        out = self.cli("wait", "wait-timeout", "--interval", "0.05", "--timeout", "0.2", code=3)
        lines = [l for l in out.splitlines() if l.strip()]
        self.assertEqual(len(lines), 1)
        self.assertIn("still busy after 0.2 s", lines[0])
        self.assertTrue(lines[0].startswith("== "))
        self.assertNotIn("artifacts:", out)

    def test_wait_detached_launch_e2e(self):
        self.launch("codex", "wait-e2e", "--detach")
        out = self.cli("wait", "wait-e2e", "--interval", "0.05", "--timeout", "30")
        self.assertIn("done", out.splitlines()[0])

    def test_rejected_link_leaves_no_worktree_or_branch(self):
        (self.repo / ".delegate/x").mkdir(parents=True)
        self.launch("cursor", "badlink", "--link", ".delegate/x", code=1)
        self.assertFalse(self.root("badlink").exists())
        self.assertNotIn("badlink", self.git("worktree", "list"))
        self.assertEqual(self.git("branch", "--list", "delegate/badlink"), "")
        self.launch("cursor", "badlink")  # the name is free again

    def _assert_preflight_clean(self, name):
        self.assertFalse(self.root(name).exists())
        self.assertNotIn(name, self.git("worktree", "list"))
        self.assertEqual(self.git("branch", "--list", f"delegate/{name}"), "")
        runs_log = self.storage / "runs.jsonl"
        self.assertFalse(runs_log.exists() and runs_log.read_text().strip())
        session = Path(self.env["CLAUDE_SESSION_LOG"])
        self.assertFalse(session.exists() and session.read_text().strip())

    def test_preflight_pass_rolls_back_and_skips_agent(self):
        base = self.git("rev-parse", "HEAD")[:9]
        # A session id makes log_delegation write rows, so the "no session row" check below bites.
        out = self.launch("codex", "pf-ok", "--verify", "printf 'ok\\n'", "--preflight",
                          extra_env={"CLAUDE_CODE_SESSION_ID": "pf-session"})
        self.assertIn("$ printf 'ok\\n'", out)
        self.assertIn("ok", out)
        self.assertRegex(out, rf"preflight: verify pass in \d+\.\d s at base {re.escape(base)}")
        self.assertNotIn("hint:", out)
        self._assert_preflight_clean("pf-ok")
        self.launch("codex", "pf-ok")  # same name launches normally after preflight
        self.assertTrue((self.root("pf-ok") / "log").exists())
        self.assertIn("fake-codex-thread", (self.root("pf-ok") / "log").read_text())

    def test_preflight_fail_with_untracked_writes_still_rolls_back(self):
        cmd = "printf 'dirty\\n' > leftover.txt; echo boom >&2; exit 7"
        out = self.launch("codex", "pf-dirty", "--verify", cmd, "--preflight", code=7)
        self.assertIn("$ " + cmd, out)
        self.assertIn("boom", out)
        self.assertRegex(out, r"preflight: verify FAIL rc=7 in \d+\.\d s at base ")
        self.assertIn("hint:", out)
        self.assertIn("checker or a --link is broken", out)
        self._assert_preflight_clean("pf-dirty")
        # worktree --force removed leftover.txt with the tree; main checkout stayed clean
        self.assertNotIn("leftover.txt", self.git("status", "--porcelain", "--untracked-files=all"))

    def test_preflight_sees_linked_content_and_scrubs_parent_env(self):
        (self.repo / "data/cache").mkdir(parents=True)
        (self.repo / "data/cache/input").write_text("from-main\n")
        cmd = ("test \"$(cat data/cache/input)\" = from-main && "
               "test -z \"${VSCODE_FAKE+x}\" && printf 'linked-and-clean\\n'")
        out = self.launch("codex", "pf-link", "--link", "data/cache", "--verify", cmd, "--preflight")
        self.assertIn("linked-and-clean", out)
        self.assertIn("preflight: verify pass", out)
        self._assert_preflight_clean("pf-link")

    def _launch_args(self, name, preflight=True, verify="true"):
        return argparse.Namespace(
            agent="codex", brief=str(self.brief), name=name, repo=str(self.repo),
            root=None, base=None, link=[], read_only=False, unsandboxed=False, web_search=False,
            detach=False, dry_run=False, verify=verify, preflight=preflight, model=None, effort=None)

    def test_preflight_keyboard_interrupt_rolls_back(self):
        args = self._launch_args("pf-int")
        with patch.dict(os.environ, self.env, clear=True), \
             patch.object(runs, "run_verify_in_worktree", side_effect=KeyboardInterrupt), \
             self.assertRaises(KeyboardInterrupt):
            runs.launch(args)
        self._assert_preflight_clean("pf-int")

    def test_preflight_refuses_without_verify_or_with_dry_run_detach(self):
        r = subprocess.run([sys.executable, str(CLI), "launch", "codex", "--brief", str(self.brief),
                            "--name", "pf-bad", "--preflight"], cwd=self.repo, env=self.env,
                           text=True, capture_output=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("requires --verify", r.stderr)
        self.assertIn("foreground check", r.stderr)
        for extra in ("--dry-run", "--detach"):
            r = subprocess.run([sys.executable, str(CLI), "launch", "codex", "--brief", str(self.brief),
                                "--name", "pf-bad", "--verify", "true", "--preflight", extra],
                               cwd=self.repo, env=self.env, text=True, capture_output=True)
            self.assertEqual(r.returncode, 1, extra)
            self.assertIn("foreground check", r.stderr)
            self.assertIn(extra, r.stderr)
        self._assert_preflight_clean("pf-bad")

    def test_setup_exception_rolls_back_launch_and_preflight(self):
        for name, preflight in (("setup-err", False), ("pf-setup-err", True)):
            with patch.dict(os.environ, self.env, clear=True), \
                 patch.object(runs, "add_links", side_effect=PermissionError("denied")), \
                 self.assertRaises(PermissionError):
                runs.launch(self._launch_args(name, preflight=preflight))
            self._assert_preflight_clean(name)

    def test_preflight_skips_agent_start_and_quota_snapshot(self):
        with patch.dict(os.environ, {**self.env, "CLAUDE_CODE_SESSION_ID": "pf-session"}, clear=True), \
             patch.object(runs, "start") as start, \
             patch.object(runs.quota, "spawn_snapshot") as snapshot, \
             patch("builtins.print"), self.assertRaises(SystemExit) as exit_:
            runs.launch(self._launch_args("pf-direct"))
        self.assertEqual(exit_.exception.code, 0)
        start.assert_not_called()
        snapshot.assert_not_called()
        self._assert_preflight_clean("pf-direct")

    def test_preflight_does_not_start_agent(self):
        out = self.launch("cursor", "pf-noagent", "--verify", "printf 'only-verify\\n'", "--preflight")
        self.assertIn("only-verify", out)
        self.assertIn("preflight: verify pass", out)
        self._assert_preflight_clean("pf-noagent")
        # no run root means no log / fake-agent marker; also confirm help exposes the flag
        self.assertIn("--preflight", self.cli("launch", "--help"))

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
        self.assertNotIn("web_search", codex)
        self.assertIn('web_search="live"', self.launch("codex", "x", "--web-search", "--read-only", "--dry-run"))
        self.launch("cursor", "x", "--web-search", "--dry-run", code=1)
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

    def test_review_started_and_effort_reach_runs_log(self):
        self.launch()
        self.cli("review", "test")
        rv = self.root("test") / "review-1"
        rv_meta = runs.load_meta(rv)
        self.assertIn("started", rv_meta)
        self.assertTrue(rv_meta["started"])
        self.assertIn("effort", rv_meta)
        self.cli("apply", "test")
        record = json.loads((self.storage / "runs.jsonl").read_text().splitlines()[0])
        review = record["reviews"][0]
        self.assertEqual(review["started"], rv_meta["started"])
        self.assertEqual(review["effort"], rv_meta.get("effort"))
        self.assertRegex(review["started"], r"^\d{4}-\d{2}-\d{2}")

    def test_quota_snapshot_spawn_is_detached_optional_and_best_effort(self):
        with patch.dict(os.environ, self.env, clear=True), \
             patch.object(runs.quota.subprocess, "Popen") as popen:
            runs.quota.spawn_snapshot("launch", run="fixture", session="claude-session")
            popen.assert_not_called()
            os.environ["CLAUDE_DELEGATE_QUOTA_SNAPSHOT"] = "1"
            runs.quota.spawn_snapshot("launch", run="fixture", session="claude-session")
            popen.assert_called_once_with(
                [sys.executable, str(CLI), "quota", "--snapshot", "--reason", "launch",
                 "--session", "claude-session", "--run", "fixture"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True)
            popen.return_value.wait.assert_not_called()
            popen.return_value.communicate.assert_not_called()
            popen.reset_mock()
            popen.side_effect = OSError("fixture spawn failure")
            runs.quota.spawn_snapshot("launch", run="fixture")
            self.assertNotIn("--session", popen.call_args.args[0])

    def test_run_entrypoints_snapshot_before_agent_start_and_skip_dry_runs(self):
        self.launch()  # completed fake run, with snapshots disabled
        loader = importlib.machinery.SourceFileLoader("delegate_snapshot_cli", str(CLI))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        delegate = importlib.util.module_from_spec(spec)
        loader.exec_module(delegate)
        spawn = runs.quota.spawn_snapshot
        collected = []

        def snapshot(*args, **kwargs):
            # Scope Popen mocking to the collector, allowing fixture git commands to run.
            with patch.object(runs.quota.subprocess, "Popen") as popen:
                spawn(*args, **kwargs)
                collected.append(popen.call_args.args[0])

        expected_jobs = ("round-1", "review-1", "round-2")

        def start(job, detach, name):
            self.assertEqual(collected[-1],
                             [sys.executable, str(CLI), "quota", "--snapshot", "--reason", "launch",
                              "--session", "parent-session", "--run", name,
                              "--job", expected_jobs[agent_start.call_count - 1]])

        commands = (("launch", "codex", "--brief", str(self.brief), "--name", "spawn",
                     "--repo", str(self.repo)),
                    ("review", "test"),
                    ("followup", "test", "--brief", str(self.follow_brief)))
        with patch.dict(os.environ, {**self.env, "CLAUDE_DELEGATE_QUOTA_SNAPSHOT": "1",
                                     "CLAUDE_CODE_SESSION_ID": "parent-session"}, clear=True), \
             patch.object(runs.quota, "spawn_snapshot", side_effect=snapshot), \
             patch.object(runs, "start", side_effect=start) as agent_start, patch("builtins.print"):
            for command in commands:
                with patch.object(sys, "argv", ["delegate", *command, "--dry-run"]):
                    delegate.main()
            self.assertEqual(collected, [])
            agent_start.assert_not_called()
            for count, command in enumerate(commands, 1):
                with patch.object(sys, "argv", ["delegate", *command]):
                    delegate.main()
                self.assertEqual(len(collected), count)
                self.assertEqual(agent_start.call_count, count)
        rows = [json.loads(line) for line in (self.tmp / "sessions.jsonl").read_text().splitlines()]
        self.assertEqual([(r["event"], r["session"], r["command"], r["run"], r["agent"]) for r in rows],
                         [("delegate", "parent-session", "launch", "spawn", "codex"),
                          ("delegate", "parent-session", "review", "test", "cursor"),
                          ("delegate", "parent-session", "followup", "test", "codex")])


    def test_link_untracked_warns_hides_in_main_and_cleanup_restores(self):
        # (a) untracked-not-ignored in main: warn, record, hide during run, show after cleanup
        path = self.repo / "tests/data/mei"
        path.mkdir(parents=True)
        (path / "sample").write_text("fixture\n")
        before = self.git("status", "--porcelain", "--untracked-files=all")
        self.assertIn("tests/data/mei", before)
        r = subprocess.run(
            [sys.executable, str(CLI), "launch", "codex", "--brief", str(self.brief),
             "--name", "link-a", "--link", "tests/data/mei"],
            cwd=self.repo, env=self.env, text=True, capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("WARNING: --link tests/data/mei is not gitignored", r.stderr)
        meta = runs.load_meta(self.root("link-a"))
        self.assertEqual(meta["excluded"], ["/tests/data/mei"])
        self.assertIn("/tests/data/mei", (self.repo / ".git/info/exclude").read_text())
        during = self.git("status", "--porcelain", "--untracked-files=all")
        self.assertNotIn("tests/data/mei", during)
        self.cli("cleanup", "link-a", "--force")
        after = self.git("status", "--porcelain", "--untracked-files=all")
        self.assertIn("tests/data/mei", after)
        self.assertNotIn("/tests/data/mei", (self.repo / ".git/info/exclude").read_text())

    def test_link_deleted_exclude_keeps_symlink_out_of_patch_and_cleanup(self):
        # (b) user deletes the exclude line mid-run: patch has no symlink; cleanup without --force
        path = self.repo / "tests/data/mei"
        path.mkdir(parents=True)
        (path / "sample").write_text("fixture\n")
        self.launch("codex", "link-b", "--link", "tests/data/mei")
        root = self.root("link-b")
        exclude = self.repo / ".git/info/exclude"
        exclude.write_text("".join(l + "\n" for l in exclude.read_text().splitlines()
                                   if l != "/tests/data/mei"))
        self.cli("diff", "link-b")
        patch = (root / "changes.patch").read_text()
        self.assertNotIn("tests/data/mei", patch)
        self.assertNotIn("new file mode 120000", patch)
        check = subprocess.run(
            [sys.executable, str(CLI), "apply", "link-b", "--check"],
            cwd=self.repo, env=self.env, text=True, capture_output=True)
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertNotIn("tests/data/mei", check.stdout + check.stderr)
        # unapplied agent changes still block; discard those but prove links alone do not
        self.cli("cleanup", "link-b", "--force")
        # re-run: after apply, cleanup without force must succeed with deleted exclude
        path.mkdir(parents=True, exist_ok=True)
        (path / "sample").write_text("fixture\n")
        self.launch("codex", "link-b2", "--link", "tests/data/mei")
        self.cli("apply", "link-b2")
        exclude = self.repo / ".git/info/exclude"
        exclude.write_text("".join(l + "\n" for l in exclude.read_text().splitlines()
                                   if l != "/tests/data/mei"))
        self.cli("cleanup", "link-b2")  # no --force
        self.assertFalse((self.root("link-b2") / "wt").exists())

    def test_link_shared_exclude_kept_until_last_live_run(self):
        # (c) two live runs link the same path: first cleanup transfers ownership, second removes
        path = self.repo / "tests/data/mei"
        path.mkdir(parents=True)
        (path / "sample").write_text("fixture\n")
        self.launch("codex", "link-c1", "--link", "tests/data/mei", "--read-only")
        r2 = subprocess.run(
            [sys.executable, str(CLI), "launch", "codex", "--brief", str(self.brief),
             "--name", "link-c2", "--link", "tests/data/mei", "--read-only"],
            cwd=self.repo, env=self.env, text=True, capture_output=True)
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertIn("WARNING: --link tests/data/mei is not gitignored", r2.stderr)  # (iii)
        self.assertEqual(runs.load_meta(self.root("link-c2")).get("excluded", []), [])
        exclude = self.repo / ".git/info/exclude"
        self.assertIn("/tests/data/mei", exclude.read_text())
        self.cli("cleanup", "link-c1")
        self.assertIn("/tests/data/mei", exclude.read_text())
        self.assertIn("/tests/data/mei", runs.load_meta(self.root("link-c2"))["excluded"])
        self.cli("cleanup", "link-c2")
        self.assertNotIn("/tests/data/mei", exclude.read_text())

    def test_link_already_gitignored_no_warning_nothing_recorded(self):
        # (d) path already in .gitignore: no warning, excluded list empty for that path
        (self.repo / "data/cache").mkdir(parents=True)
        (self.repo / "data/cache/input").write_text("x\n")
        r = subprocess.run(
            [sys.executable, str(CLI), "launch", "codex", "--brief", str(self.brief),
             "--name", "link-d", "--link", "data/cache"],
            cwd=self.repo, env=self.env, text=True, capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("WARNING: --link data/cache", r.stderr)
        meta = runs.load_meta(self.root("link-d"))
        self.assertEqual(meta.get("excluded", []), [])
        self.cli("cleanup", "link-d", "--force")

    def test_link_preserves_user_preexisting_exclude_line(self):
        # (e) user's own exclude line for the same path survives cleanup
        path = self.repo / "tests/data/mei"
        path.mkdir(parents=True)
        (path / "sample").write_text("fixture\n")
        exclude = self.repo / ".git/info/exclude"
        exclude.parent.mkdir(exist_ok=True)
        exclude.write_text("# user note\n/tests/data/mei\n")
        self.launch("codex", "link-e", "--link", "tests/data/mei", "--read-only")
        meta = runs.load_meta(self.root("link-e"))
        self.assertEqual(meta.get("excluded", []), [])  # line was already present
        self.cli("cleanup", "link-e")
        text = exclude.read_text()
        self.assertIn("/tests/data/mei", text)
        self.assertIn("# user note", text)

    def test_link_user_line_after_owner_cleanup_survives_next_run(self):
        # ownership (i): A removes its line; user adds /cache; B does not take ownership
        cache = self.repo / "cache"
        cache.mkdir()
        (cache / "x").write_text("d\n")
        self.launch("codex", "own-a", "--link", "cache", "--read-only")
        self.cli("cleanup", "own-a")
        self.assertNotIn("/cache", (self.repo / ".git/info/exclude").read_text())
        exclude = self.repo / ".git/info/exclude"
        raw = exclude.read_bytes() if exclude.exists() else b""
        if raw and not raw.endswith(b"\n"):
            raw += b"\n"
        exclude.write_bytes(raw + b"/cache\n")
        self.launch("codex", "own-b", "--link", "cache", "--read-only")
        self.assertEqual(runs.load_meta(self.root("own-b")).get("excluded", []), [])
        self.cli("cleanup", "own-b")
        self.assertIn("/cache", exclude.read_text())

    def test_link_transfer_survives_purge_of_owner(self):
        # ownership (ii): A transfers then --purge; B still removes the line
        path = self.repo / "tests/data/mei"
        path.mkdir(parents=True)
        (path / "sample").write_text("fixture\n")
        self.launch("codex", "purge-a", "--link", "tests/data/mei", "--read-only")
        self.launch("codex", "purge-b", "--link", "tests/data/mei", "--read-only")
        self.cli("cleanup", "purge-a", "--purge")
        self.assertFalse(self.root("purge-a").exists())
        self.assertIn("/tests/data/mei", (self.repo / ".git/info/exclude").read_text())
        self.assertIn("/tests/data/mei", runs.load_meta(self.root("purge-b"))["excluded"])
        self.cli("cleanup", "purge-b")
        self.assertNotIn("/tests/data/mei", (self.repo / ".git/info/exclude").read_text())

    def test_cleanup_blocks_on_nonsymlink_at_link_path(self):
        # finding 1: a regular file at a recorded link path is an unapplied change
        path = self.repo / "tests/data/mei"
        path.mkdir(parents=True)
        (path / "sample").write_text("fixture\n")
        self.launch("codex", "link-file", "--link", "tests/data/mei", "--read-only")
        wt = self.root("link-file") / "wt"
        exclude = self.repo / ".git/info/exclude"
        exclude.write_text("".join(l + "\n" for l in exclude.read_text().splitlines()
                                   if l != "/tests/data/mei"))
        (wt / "tests/data/mei").unlink()
        (wt / "tests/data/mei").write_text("replaced-with-file\n")
        rejected = subprocess.run(
            [sys.executable, str(CLI), "cleanup", "link-file"],
            cwd=self.repo, env=self.env, text=True, capture_output=True)
        self.assertNotEqual(rejected.returncode, 0, rejected.stderr)
        self.assertIn("changes that were not applied", rejected.stderr)
        self.assertIn("tests/data/mei", rejected.stderr)
        self.cli("cleanup", "link-file", "--force")

    def test_literal_link_pathspec_keeps_glob_lookalike_changes(self):
        # finding 2: link with [ ] must not glob-exclude a real changed file
        link = self.repo / "tests/test_[a]sk"
        link.mkdir(parents=True)
        (link / "in").write_text("linked\n")
        ask = self.repo / "tests/test_ask.py"
        ask.parent.mkdir(parents=True, exist_ok=True)
        ask.write_text("keep\n")
        self.git("add", "tests/test_ask.py")
        self.git("commit", "-qm", "ask fixture")
        self.launch("codex", "link-glob", "--link", "tests/test_[a]sk")
        wt = self.root("link-glob") / "wt"
        (wt / "tests/test_ask.py").write_text("keep\nchanged\n")
        self.cli("diff", "link-glob")
        patch = (self.root("link-glob") / "changes.patch").read_text()
        self.assertIn("tests/test_ask.py", patch)
        self.assertIn("+changed", patch)
        self.assertNotIn("test_[a]sk", patch)
        self.cli("cleanup", "link-glob", "--force")

    def test_cleanup_ignores_untracked_parent_of_link_symlink(self):
        # finding 5: link under a new parent dir; exclude removed; only the symlink is ignorable
        src = self.repo / "newdir/sub/link"
        src.mkdir(parents=True)
        (src / "x").write_text("d\n")
        self.launch("codex", "link-deep", "--link", "newdir/sub/link", "--read-only")
        exclude = self.repo / ".git/info/exclude"
        exclude.write_text("".join(l + "\n" for l in exclude.read_text().splitlines()
                                   if l != "/newdir/sub/link"))
        self.cli("cleanup", "link-deep")  # no --force
        self.assertFalse((self.root("link-deep") / "wt").exists())

    def test_exclude_edits_preserve_bytes(self):
        # finding 6: CRLF and no-final-newline round-trip apart from the removed line
        exclude = self.repo / ".git/info/exclude"
        exclude.parent.mkdir(exist_ok=True)
        crlf = b"# note\r\n/keep\r\n/drop-me\r\n"
        exclude.write_bytes(crlf)
        runs._exclude(self.repo, ["/added"])
        self.assertEqual(exclude.read_bytes(), b"# note\r\n/keep\r\n/drop-me\r\n/added\n")
        runs._remove_excluded(self.repo, ["/drop-me"])
        self.assertEqual(exclude.read_bytes(), b"# note\r\n/keep\r\n/added\n")
        exclude.write_bytes(b"/alone")
        runs._exclude(self.repo, ["/second"])
        self.assertEqual(exclude.read_bytes(), b"/alone\n/second\n")
        exclude.write_bytes(b"/a\n/b")
        runs._remove_excluded(self.repo, ["/b"])
        self.assertEqual(exclude.read_bytes(), b"/a\n")
        exclude.write_bytes(b"/a\n/b")
        runs._remove_excluded(self.repo, ["/a"])
        self.assertEqual(exclude.read_bytes(), b"/b")

    def test_review_patch_omits_large_file_diffs(self):
        self.launch("codex", "bigpatch")
        wt = self.root("bigpatch") / "wt"
        (wt / "small.txt").write_text("tiny\n")
        (wt / "big.bin").write_bytes(b"x" * (300 * 1024))
        out = self.cli("review", "bigpatch")
        self.assertIn("review patch: omitted 1 large file diff(s): big.bin", out)
        review = (wt / ".delegate/review-1.patch").read_text(errors="replace")
        self.assertIn("diff --git", review)
        self.assertIn("small.txt", review)
        self.assertIn("+tiny", review)
        self.assertIn("diff --git a/big.bin b/big.bin", review)
        self.assertIn("# delegate: diff omitted from the review patch", review)
        self.assertNotIn("+" + "x" * 100, review)
        self.assertIn("big.bin", (self.root("bigpatch") / "review-1/prompt.md").read_text())
        self.cli("diff", "bigpatch")
        full = (self.root("bigpatch") / "changes.patch").read_text(errors="replace")
        self.assertIn("small.txt", full)
        self.assertGreater(len(full.encode()), 300 * 1024)
        # --patch-max-kb 0 keeps both in the review copy
        self.cli("review", "bigpatch", "--patch-max-kb", "0")
        review0 = (wt / ".delegate/review-2.patch").read_text(errors="replace")
        self.assertIn("small.txt", review0)
        self.assertNotIn("diff omitted", review0)
        self.assertGreater(len(review0.encode()), 300 * 1024)

    def test_review_patch_omitted_names_for_space_and_rename(self):
        # rename-to and quoted header still parse; live review names must be exact
        self.assertEqual(runs._diff_section_path(
            "diff --git a/oldbig.bin b/renamed big.bin\n"
            "similarity index 99%\nrename from oldbig.bin\nrename to renamed big.bin\n"),
            "renamed big.bin")
        self.assertEqual(runs._diff_section_path(
            'diff --git "a/q\\303\\274" "b/q\\303\\274"\n'
            "Binary files differ\n"), "qü")
        self.launch("codex", "bigname")
        wt = self.root("bigname") / "wt"
        (wt / "big file.txt").write_bytes(b"y" * (300 * 1024))
        out = self.cli("review", "bigname")
        self.assertRegex(out, r"review patch: omitted 1 large file diff\(s\): big file\.txt \(\d+ KB\)")
        self.assertNotIn("big file.txt\t", out)
        self.assertIn("big file.txt", (wt / ".delegate/review-1.patch").read_text(errors="replace"))

    def test_omitted_names_from_real_git_diff_space_and_cafe(self):
        # rounds 1–2: real git +++ lines (TAB for spaces; C-quote for non-ASCII)
        tmp = tempfile.TemporaryDirectory(prefix="delegate-omit-names-")
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=self.env)
        subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True, env=self.env)
        subprocess.run(["git", "config", "user.email", "t@e.invalid"], cwd=repo, check=True, env=self.env)
        subprocess.run(["git", "config", "core.quotePath", "true"], cwd=repo, check=True, env=self.env)
        (repo / "seed").write_text("s\n")
        subprocess.run(["git", "add", "seed"], cwd=repo, check=True, env=self.env)
        subprocess.run(["git", "commit", "-qm", "seed"], cwd=repo, check=True, env=self.env)
        base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, env=self.env,
                              capture_output=True, text=True).stdout.strip()
        (repo / "a b.txt").write_bytes(b"s" * (210 * 1024))
        (repo / "café.txt").write_bytes(b"c" * (210 * 1024))
        subprocess.run(["git", "add", "-A", "-N", "--", "a b.txt", "café.txt"], cwd=repo,
                       check=True, env=self.env)
        patch = subprocess.run(["git", "diff", "--binary", base], cwd=repo, check=True,
                               capture_output=True, env=self.env).stdout.decode("utf-8", "surrogateescape")
        self.assertIn("\t\n", patch)  # space-name +++ line ends with TAB before newline
        self.assertIn(r"\303\251", patch)  # café C-quoted
        trimmed, omitted = runs._trim_review_patch(patch, 200)
        names = [p for p, _ in omitted]
        self.assertEqual(set(names), {"a b.txt", "café.txt"})
        self.assertNotIn("\t", "".join(names))
        self.assertNotIn("b/café.txt", names)
        self.assertIn("# delegate: diff omitted", trimmed)

    def test_link_warning_on_gitignore_negation(self):
        # round 3 #3: !pattern match means not ignored → warn and record
        (self.repo / ".gitignore").write_text("data*\n!data2\n")
        self.git("add", ".gitignore")
        self.git("commit", "-qm", "gitignore negation")
        (self.repo / "data2").mkdir()
        (self.repo / "data2" / "x").write_text("d\n")
        r = subprocess.run(
            [sys.executable, str(CLI), "launch", "codex", "--brief", str(self.brief),
             "--name", "link-neg", "--link", "data2", "--read-only"],
            cwd=self.repo, env=self.env, text=True, capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("WARNING: --link data2 is not gitignored", r.stderr)
        self.assertEqual(runs.load_meta(self.root("link-neg"))["excluded"], ["/data2"])
        self.cli("cleanup", "link-neg")
        self.assertNotIn("/data2", (self.repo / ".git/info/exclude").read_text())

    def test_link_trailing_slash_normalised(self):
        # round 3 #4: --link data/ stores data; pathspec still excludes the symlink
        (self.repo / "data").mkdir()
        (self.repo / "data" / "x").write_text("d\n")
        self.launch("codex", "link-slash", "--link", "data/")
        meta = runs.load_meta(self.root("link-slash"))
        self.assertEqual(meta["links"], ["data"])
        self.assertNotIn("data/", meta["links"])
        exclude = self.repo / ".git/info/exclude"
        exclude.write_text("".join(l + "\n" for l in exclude.read_text().splitlines()
                                   if l != "/data"))
        self.cli("diff", "link-slash")
        patch = (self.root("link-slash") / "changes.patch").read_text()
        self.assertNotIn("diff --git a/data ", patch)
        self.assertNotIn("new file mode 120000", patch)
        self.cli("cleanup", "link-slash", "--force")


if __name__ == "__main__":
    unittest.main()

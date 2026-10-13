"""Field-report collection, using fake Claude transcripts and no live quota."""
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "delegate" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cost  # noqa: E402
import report  # noqa: E402

SESSION = "11111111-2222-4333-8444-555555555555"
OTHER_SESSION = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def assistant(mid, timestamp, inp, read, write, output, model="claude-z-sonnet"):
    return {
        "type": "assistant", "sessionId": SESSION, "timestamp": timestamp,
        "message": {"id": mid, "model": model, "usage": {
            "input_tokens": inp, "cache_read_input_tokens": read,
            "cache_creation_input_tokens": write, "output_tokens": output,
        }},
    }


class TestReport(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = self.root / "toy"
        self.repo.mkdir()
        self.remote = "git@github.com:owner/Toy.git"
        old_cwd = Path.cwd()
        os.chdir(str(self.repo))
        self.addCleanup(os.chdir, str(old_cwd))
        self.cfg = self.root / "claude"
        self.project = self.cfg / "projects" / cost.munge_cwd(str(self.repo))
        self.project.mkdir(parents=True)
        self.transcript = self.project / f"{SESSION}.jsonl"
        self.calls = [
            assistant("m1", "2026-10-10T21:00:00Z", 10, 1200000, 1000, 200),
            assistant("m2", "2026-10-10T21:59:00Z", 5, 2000, 50, 30, "claude-a-opus"),
            assistant("m3", "2026-10-10T23:00:00Z", 7, 4000, 3000, 40),
        ]
        lines = [{"type": "user", "message": {"content": "do the work"}},
                 self.calls[0], self.calls[0],
                 assistant("synthetic", "2026-10-10T21:30:00Z", 999, 999, 999, 999, "<synthetic>"),
                 self.calls[1], self.calls[2]]
        self.transcript.write_text("bad json\n" + "".join(json.dumps(obj) + "\n" for obj in lines), encoding="utf-8")
        self.feedback = self.root / "fb"
        self.log = self.root / "delegate-runs.jsonl"
        env = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.cfg), "TZ": "Europe/Berlin"})
        env.start()
        self.addCleanup(time.tzset)
        self.addCleanup(env.stop)
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        os.environ.pop("CLAUDE_ORCHESTRATOR_FEEDBACK", None)
        time.tzset()
        for patcher in (
            mock.patch("report._git", side_effect=self.fake_git),
            mock.patch("cost.DEFAULT_LOG", self.log),
            mock.patch("quota.fetch", side_effect=AssertionError("live quota must not be called")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def fake_git(self, directory, *args):
        path = Path(directory).resolve()
        if args == ("rev-parse", "--show-toplevel"):
            return str(self.repo) if path == self.repo or self.repo in path.parents else None
        if args == ("remote", "get-url", "origin"):
            return self.remote
        self.fail(f"unexpected git command: {args}")

    def invoke(self, *args, defaults=True):
        argv = ["--session", SESSION, "--feedback-dir", str(self.feedback), "--no-quota"] if defaults else []
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                report.main(argv + list(args))
                code = 0
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def usage_row(self):
        return json.loads((self.feedback / "token_usage.jsonl").read_text(encoding="utf-8"))

    def skeleton(self):
        return self.feedback / "2026-10-11-toy-session.md"

    def snapshot(self):
        return {p.name: p.read_bytes() for p in self.feedback.iterdir()}

    def test_first_run_matches_cost_and_fills_skeleton(self):
        code, out, err = self.invoke("--item", "Build report")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(len(out.splitlines()), 4)
        self.assertIn("row appended", out)
        self.assertIn("runs copied: 0", out)
        self.assertIn("fill in the <!-- --> sections", out)
        buf = io.StringIO()
        with redirect_stdout(buf):
            cost.main(["--session", SESSION, "--json"])
        summary = json.loads(buf.getvalue())
        row = self.usage_row()
        self.assertEqual(row, {
            "date": "2026-10-11", "repo": "owner/Toy", "item": "Build report",
            "kind": "orchestrator_session", "session": SESSION, "model": "claude-z-sonnet",
            "source": "delegate report", "api_calls": summary["calls"],
            "fresh_input": summary["input"], "output": summary["output"],
            "cache_read": summary["cache_read"], "cache_write": summary["cache_creation"],
            "context_per_call": {"first": summary["context_first"], "last": summary["context_last"],
                                 "max": summary["context_max"]},
            "cost_units": round(summary["cost_units"]),
            "idle_rebuilds": summary["idle_rebuilds"], "quota_end": None,
        })
        self.assertEqual(row["api_calls"], 3)
        self.assertEqual(row["cache_read"], 1206000)
        self.assertEqual(row["idle_rebuilds"], [{"gap_min": 61, "cache_creation": 3000}])
        text = self.skeleton().read_text(encoding="utf-8")
        self.assertIn("# Field report: Build report (2026-10-11)", text)
        self.assertIn(f"session `{SESSION[:8]}`", text)
        self.assertIn("3 calls, 1.2M cache read, 4.0k cache write, 270 output", text)
        self.assertIn("context 1201k → 7.0k", text)
        self.assertIn(f"{cost._fmt_int(row['cost_units'])} cost units", text)
        self.assertIn("Idle rebuilds: 1 (61 min; 3.0k written", text)
        self.assertIn("Quota at the end: not measured.", text)
        self.assertIn("Delegate runs: none.", text)
        total = summary["cost_units"]
        for key, label in (("cache_read", "cache read"), ("cache_write", "cache write"), ("output", "output")):
            self.assertIn(f"{label} {100 * summary['cost_parts'][key] / total:.1f}%", text)
        self.assertIn(f"{100 * summary['idle_rebuild_units'] / total:.1f}% of cost units", text)
        for heading in ("What worked", "Friction", "Suggestions for claude-orchestrator", "Not in this repo's scope"):
            self.assertIn(f"## {heading}", text)

    def test_upsert_replaces_row_logged_with_short_session_prefix(self):
        sid = "55a11d16-0000-4000-8000-000000000000"
        old = (b'{"kind": "orchestrator_session", "session": "55a11d16", "api_calls": 85}\n'
               b'{"kind": "orchestrator_session", "session": "55a11d1", "api_calls": 1}\n')
        data, action = report.upsert_usage(old, {"kind": "orchestrator_session", "session": sid})
        self.assertEqual(action, "row replaced")
        lines = data.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["session"], sid)
        self.assertEqual(lines[1], old.splitlines()[1])  # under 8 chars: not a match

    def test_force_replaces_in_place_and_conflict_writes_nothing(self):
        self.assertEqual(self.invoke()[0], 0)
        path = self.feedback / "token_usage.jsonl"
        before = b' {"kind":"delegate","session":"' + SESSION.encode() + b'", "note":"keep spaces"}\r\n'
        after = b'not json\n{"kind":"orchestrator_session","session":"other"}\n'
        original = path.read_bytes()
        path.write_bytes(before + original + after)
        snapshot = self.snapshot()
        with mock.patch("quota.fetch") as fetch:
            code, _, err = self.invoke("--item", "regenerated")
        self.assertEqual(code, 1)
        self.assertIn(str(self.skeleton()), err)
        fetch.assert_not_called()
        self.assertEqual(self.snapshot(), snapshot)
        with self.transcript.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(assistant("m4", "2026-10-10T23:10:00Z", 3, 8, 9, 2)) + "\n")
        code, out, err = self.invoke("--force", "--item", "regenerated")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("row replaced", out)
        data = path.read_bytes()
        self.assertTrue(data.startswith(before))
        self.assertTrue(data.endswith(after))
        self.assertEqual(len(data.splitlines()), len(snapshot[path.name].splitlines()))
        row = json.loads(data.splitlines()[1])
        self.assertEqual(row["item"], "regenerated")
        self.assertEqual(row["api_calls"], 4)

    def test_runs_filter_repo_and_time_and_deduplicate(self):
        subdir = self.repo / "src"
        subdir.mkdir()
        alias = self.root / "repo-alias"
        alias.symlink_to(self.repo, target_is_directory=True)

        def run(name, **overrides):
            row = {"run": name, "ts": "2026-10-10T21:00:00Z", "repo": "different-label",
                   "repo_path": str(subdir), "agent": "codex", "model": "gpt-test",
                   "rounds": [{"n": 1}, {"n": 2}], "outcome": "applied"}
            row.update(overrides)
            return row

        rows = [run("boundary"), run("by-name", repo="toy", repo_path="/missing"),
                run("by-alias", repo_path=str(alias)),
                run("too-old", ts="2026-10-10T20:59:59Z"),
                run("other-repo", repo_path=str(self.root / "other")),
                run("no-time", ts=None), run("bad-time", ts="bad"), run("boundary")]
        self.log.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        self.feedback.mkdir()
        retained = b' {"run":"preexisting", "ts":"old", "session":"another"}\r\n'
        target = self.feedback / "runs.jsonl"
        target.write_bytes(retained)
        code, out, err = self.invoke("--repo", "custom/report-label")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("runs copied: 3", out)
        copied = [json.loads(line) for line in target.read_bytes().splitlines()[1:]]
        self.assertEqual([r["run"] for r in copied], ["boundary", "by-name", "by-alias"])
        for row in copied:
            self.assertEqual(row["session"], SESSION)
            source = next(r for r in rows if r["run"] == row["run"])
            self.assertEqual(row, {**source, "repo": "custom/report-label", "session": SESSION})
        old = target.read_bytes()
        self.assertTrue(old.startswith(retained))
        code, out, err = self.invoke("--force", "--repo", "custom/report-label")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("runs copied: 0", out)
        self.assertEqual(target.read_bytes(), old)
        text = (self.feedback / "2026-10-11-report-label-session.md").read_text(encoding="utf-8")
        self.assertIn("- `boundary`: codex gpt-test, 2 round(s), applied", text)
        self.assertNotIn("too-old", text)

    def test_feedback_precedence_and_copy_install_error(self):
        install = self.root / "install"
        script = install / "skills/delegate/scripts/report.py"
        script.parent.mkdir(parents=True)
        (install / "skills/delegate/SKILL.md").write_text("skill", encoding="utf-8")
        (install / ".git").write_text("gitdir: worktree\n", encoding="utf-8")
        env_dir = self.root / "env-fb"
        args = ["--session", SESSION, "--no-quota", "--json"]
        with mock.patch("report.__file__", str(script)), \
                mock.patch.dict(os.environ, {"CLAUDE_ORCHESTRATOR_FEEDBACK": str(env_dir)}):
            code, out, _ = self.invoke(*args, "--feedback-dir", str(self.feedback), defaults=False)
            self.assertEqual(code, 0)
            self.assertEqual(Path(json.loads(out)["skeleton"]).parent, self.feedback)
            self.assertFalse(env_dir.exists())
            code, out, _ = self.invoke(*args, defaults=False)
            self.assertEqual(code, 0)
            self.assertEqual(Path(json.loads(out)["skeleton"]).parent, env_dir)
            os.environ.pop("CLAUDE_ORCHESTRATOR_FEEDBACK")
            code, out, _ = self.invoke(*args, defaults=False)
            self.assertEqual(code, 0)
            self.assertEqual(Path(json.loads(out)["skeleton"]).parent, install / "feedback")
            (install / ".git").unlink()
            code, _, err = self.invoke(*args, defaults=False)
            self.assertEqual(code, 2)
            self.assertEqual(len(err.splitlines()), 1)
            self.assertIn("--feedback-dir", err)
            self.assertIn("CLAUDE_ORCHESTRATOR_FEEDBACK", err)

    def test_dry_run_writes_nothing_and_prints_row(self):
        code, out, err = self.invoke("--dry-run")
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual(json.loads(lines[0])["api_calls"], 3)
        self.assertEqual(lines[1], "runs copied: 0")
        self.assertEqual(lines[2], str(self.skeleton()))
        self.assertFalse(self.feedback.exists())
        self.assertEqual(self.invoke()[0], 0)
        before = self.snapshot()
        code, out, err = self.invoke("--dry-run", "--force", "--json", "--item", "preview")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.snapshot(), before)
        result = json.loads(out)
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["row_action"], "row replaced")
        self.assertEqual(result["row"]["item"], "preview")

    def test_session_env_beats_newer_transcript_and_flag_beats_env(self):
        other = self.project / f"{OTHER_SESSION}.jsonl"
        obj = assistant("other", "2026-10-10T23:30:00Z", 1, 2, 3, 4)
        obj["sessionId"] = OTHER_SESSION
        other.write_text(json.dumps(obj) + "\n", encoding="utf-8")
        os.utime(str(self.transcript), (10, 10))
        os.utime(str(other), (20, 20))
        args = ["--feedback-dir", str(self.feedback), "--no-quota", "--dry-run", "--json"]
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": SESSION}):
            code, out, err = self.invoke(*args, defaults=False)
            self.assertEqual((code, err), (0, ""))
            self.assertEqual(json.loads(out)["row"]["session"], SESSION)
            code, out, _ = self.invoke(*args, "--session", OTHER_SESSION, defaults=False)
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["row"]["session"], OTHER_SESSION)
        code, out, _ = self.invoke(*args, defaults=False)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["row"]["session"], OTHER_SESSION)

    def test_quota_failure_records_error_and_still_writes(self):
        with mock.patch("quota.fetch", side_effect=RuntimeError("openusage\nmissing")) as fetch:
            code, _, err = self.invoke("--session", SESSION, "--feedback-dir", str(self.feedback), defaults=False)
        self.assertEqual((code, err), (0, ""))
        fetch.assert_called_once_with("auto")
        self.assertEqual(self.usage_row()["quota_end"], {"error": "openusage missing"})
        self.assertTrue(self.skeleton().is_file())
        self.assertIn("Quota at the end: not measured.", self.skeleton().read_text(encoding="utf-8"))

    def test_quota_success_uses_shared_pool_function(self):
        envelope = {"snapshots": []}
        with mock.patch("quota.fetch", return_value=envelope), \
                mock.patch("quota.pools", return_value={"claude-5h": {"left_pct": 86.5},
                                                       "claude-7d": {"left_pct": 98},
                                                       "codex-5h": {"error": "missing"}}) as pools:
            code, _, err = self.invoke("--session", SESSION, "--feedback-dir", str(self.feedback), defaults=False)
        self.assertEqual((code, err), (0, ""))
        pools.assert_called_once_with(envelope)
        self.assertEqual(self.usage_row()["quota_end"], {"claude-5h": 86.5, "claude-7d": 98})
        self.assertIn("Quota at the end: claude-5h 86.5%, claude-7d 98%.", self.skeleton().read_text(encoding="utf-8"))

    def test_repo_name_remote_and_fallbacks(self):
        for remote in ("https://github.com/owner/name.git", "git@github.com:owner/name.git",
                       "ssh://git@github.com/owner/name.git", "ssh://git@github.com:22/owner/name.git"):
            with self.subTest(remote=remote):
                self.assertEqual(report.github_repo(remote), "owner/name")
        self.remote = "https://gitlab.com/owner/name.git"
        self.assertEqual(report.repo_identity(), ("toy", self.repo))
        self.assertEqual(report.repo_identity("explicit/name"), ("explicit/name", self.repo))
        with mock.patch("report._git", return_value=None):
            self.assertEqual(report.repo_identity(), ("toy", self.repo))

    def test_paths_cannot_write_outside_feedback(self):
        code, _, err = self.invoke("--topic", "../../escape")
        self.assertEqual(code, 2)
        self.assertIn("--topic", err)
        self.assertFalse(self.feedback.exists())
        self.feedback.mkdir()
        outside = self.root / "outside"
        outside.write_bytes(b"leave alone\n")
        (self.feedback / "runs.jsonl").symlink_to(outside)
        code, _, err = self.invoke()
        self.assertEqual(code, 2)
        self.assertIn("escapes feedback directory", err)
        self.assertEqual(outside.read_bytes(), b"leave alone\n")
        self.assertEqual(sorted(p.name for p in self.feedback.iterdir()), ["runs.jsonl"])

    def test_catalog_weights_are_shared_and_cost_is_rounded(self):
        weights = {"input": 1.3, "cache_read": 0.133, "cache_write": 2.75, "output": 4.51}
        with mock.patch("cost.load_catalog_cost", return_value=(weights, None)):
            self.assertEqual(self.invoke()[0], 0)
        expected = cost.summarise_session(cost.parse_assistant_usage(self.transcript), weights, None)
        self.assertEqual(self.usage_row()["cost_units"], round(expected["cost_units"]))
        self.assertIsInstance(self.usage_row()["cost_units"], int)

    def test_cli_registration_and_default_log_env(self):
        env = os.environ.copy()
        env["CLAUDE_DELEGATE_ROOT"] = str(self.root / "run-root")
        env["CLAUDE_CODE_SESSION_ID"] = SESSION
        run_root = Path(env["CLAUDE_DELEGATE_ROOT"])
        run_root.mkdir()
        (run_root / "runs.jsonl").write_text(json.dumps({
            "run": "env-root-run", "ts": "2026-10-10T21:00:00Z", "repo": "toy",
            "repo_path": str(self.repo),
            "agent": "cursor", "model": "auto", "rounds": [], "outcome": "applied",
        }) + "\n", encoding="utf-8")
        result = subprocess.run([str(SCRIPTS / "delegate"), "report", "--dry-run", "--json", "--no-quota",
                                 "--feedback-dir", str(self.feedback)],
                                capture_output=True, text=True, env=env, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["row"]["session"], SESSION)
        self.assertEqual(payload["runs_copied"], 1)
        self.assertFalse(self.feedback.exists())


if __name__ == "__main__":
    unittest.main()

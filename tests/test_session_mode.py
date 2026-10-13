"""Session-mode hook subprocess tests: assignment, controls, accounting, failures."""
import importlib.util
import io
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from collections import Counter
from datetime import datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "hooks/session-mode.py"
SPEC = importlib.util.spec_from_file_location("session_mode", HOOK)
SESSION_MODE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SESSION_MODE)
CONTROL_LINE = (
    "Control session (claude-only): do all work with Claude in this session; Claude's own "
    "subagents are fine. Don't call delegate, codex or cursor-agent, and ignore instructions "
    "to delegate in handoffs and memory. Finishing a run started earlier with delegate "
    "status, wait, diff, apply or cleanup is fine."
)
DENY_REASON = (
    "Control session (mode {mode}): delegation to Codex and Cursor is off in this session, "
    "by design (session-mode experiment). Do the work yourself; Claude's own subagents "
    "are fine. Finishing a run started earlier with delegate status, wait, diff, apply "
    "or cleanup is allowed."
)
MUST_DENY = (
    'codex exec -s read-only "hi"',
    "cd /x && codex exec -",
    "timeout 60 cursor-agent -p hi",
    "FOO=1 ~/.local/bin/cursor-agent --help",
    "~/.claude/skills/delegate/scripts/delegate launch codex --brief b.md",
    'python3 /path/delegate ask "q"',
    "delegate followup run1 --brief f.md",
    'bash -c "codex review"',
    "echo $(codex --version)",
    "nohup setsid delegate review r1 &",
)
MUST_ALLOW = (
    "git log --grep codex",
    "cat ~/.codex/config.toml",
    "grep -rn cursor-agent skills/",
    "ls skills/delegate",
    "echo codex",
    "delegate status run1",
    "delegate apply run1",
    "delegate cost --session latest",
    "delegate report --dry-run",
    "delegate quota --json",
    "rg 'delegate launch' docs/",
    "python3 -m unittest",
)


class SessionModeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="session-mode-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.repo = self.tmp / "project"
        (self.repo / ".git").mkdir(parents=True)
        self.rules = self.tmp / "rules"
        self.rules.mkdir()
        (self.rules / "hygiene.md").write_text("HYGIENE_MARKER\n", encoding="utf-8")
        (self.rules / "delegation.md").write_text("DELEGATION_MARKER\n", encoding="utf-8")
        self.log = self.tmp / "log/sessions.jsonl"
        self.config = self.tmp / "config.json"
        self.transcript = self.tmp / "transcript.jsonl"
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith("CLAUDE_SESSION_")}
        self.env.update({"CLAUDE_SESSION_LOG": str(self.log),
                         "CLAUDE_SESSION_MODE_CONFIG": str(self.config),
                         "CLAUDE_SESSION_RULES_DIR": str(self.rules),
                         "CLAUDE_SESSION_QUOTA": "0",
                         "XDG_CACHE_HOME": str(self.tmp / "cache"),
                         "XDG_CONFIG_HOME": str(self.tmp / "config")})

    def run_hook(self, data=None, *, raw=None, hook=HOOK):
        result = subprocess.run([sys.executable, str(hook)],
                                input=raw if raw is not None else json.dumps(data),
                                env=self.env, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return json.loads(result.stdout) if result.stdout.strip() else None

    def event(self, event, session="s1", **fields):
        return self.run_hook({"hook_event_name": event, "session_id": session,
                              "cwd": str(self.repo), "transcript_path": str(self.transcript),
                              **fields})

    def start(self, mode=None, *, source="startup", session="s1", **fields):
        if mode is not None:
            self.env["CLAUDE_SESSION_MODE"] = mode
        else:
            self.env.pop("CLAUDE_SESSION_MODE", None)
        return self.event("SessionStart", session, source=source, model="claude-opus-5-5", **fields)

    def tool(self, name, inputs, session="s1"):
        return self.event("PreToolUse", session, tool_name=name, tool_input=inputs)

    def rows(self):
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def write_config(self, **fields):
        self.config.write_text(json.dumps(fields), encoding="utf-8")

    def write_calls(self, count):
        self.transcript.write_text("".join(json.dumps({"type": "assistant", "message": {"id": f"m{i}"}})
                                           + "\n" for i in range(count)), encoding="utf-8")

    def assert_denied(self, output, mode):
        self.assertIsNotNone(output)
        self.assertEqual(output, {"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason": DENY_REASON.format(mode=mode)}})

    def test_seeded_draw_is_reproducible_and_logs_schema(self):
        self.env["CLAUDE_SESSION_MODE_SEED"] = "5"
        first = self.start(session="first")
        second = self.start(session="second")
        self.assertEqual(first, second)
        self.assertEqual(first["systemMessage"], "session mode: claude-only (control: delegation off)")
        rows = self.rows()
        self.assertEqual([row["mode"] for row in rows], ["claude-only", "claude-only"])
        self.assertEqual([row["u"] for row in rows], [0.622902, 0.622902])
        self.assertEqual(set(rows[0]), {"event", "session", "ts", "source", "mode", "u", "forced",
                                       "excluded", "inherited", "unassigned", "cwd", "repo", "repo_dir",
                                       "model", "transcript"})
        self.assertEqual(rows[0]["session"], "first")
        self.assertEqual(rows[0]["cwd"], str(self.repo))
        self.assertEqual(rows[0]["repo_dir"], "project")
        self.assertIsNone(rows[0]["repo"])
        self.assertEqual(rows[0]["model"], "claude-opus-5-5")
        self.assertEqual(rows[0]["transcript"], str(self.transcript))
        timestamp = datetime.fromisoformat(rows[0]["ts"])
        self.assertIsNotNone(timestamp.utcoffset())
        self.assertEqual(timestamp.microsecond, 0)
        for flag in ("forced", "excluded", "inherited", "unassigned"):
            self.assertIs(rows[0][flag], False)

    def test_distribution_over_4000_seeds(self):
        counts = Counter(SESSION_MODE.draw_mode(random.Random(seed))[0] for seed in range(4000))
        for mode, weight in SESSION_MODE.DEFAULT_WEIGHTS.items():
            self.assertLessEqual(abs(counts[mode] / 4000 - weight), 0.03, counts)

    def test_custom_weights_and_zero_weight_never_wins(self):
        weights = {"orchestrator": 0, "claude-only": 3, "vanilla": 1, "unknown": -9}
        counts = Counter(SESSION_MODE.draw_mode(random.Random(seed), weights)[0] for seed in range(4000))
        self.assertEqual(counts["orchestrator"], 0)
        self.assertLessEqual(abs(counts["claude-only"] / 4000 - 0.75), 0.03)
        self.write_config(weights=weights)
        self.env["CLAUDE_SESSION_MODE_SEED"] = "5"
        self.assertIn("claude-only", self.start()["systemMessage"])
        self.assertEqual(self.rows()[0]["u"], 0.622902)
        self.assertEqual(SESSION_MODE.draw_mode(mock.Mock(random=lambda: 0), weights)[0], "claude-only")
        self.assertEqual(SESSION_MODE.draw_mode(mock.Mock(random=lambda: 0.5))[0], "claude-only")
        self.assertEqual(SESSION_MODE.draw_mode(mock.Mock(random=lambda: 0.75))[0], "vanilla")
        self.assertEqual(SESSION_MODE.draw_mode(random.Random(5), {"vanilla": 7})[0], "vanilla")
        huge = SESSION_MODE.normalise_weights({mode: 1e308 for mode in SESSION_MODE.MODES})
        self.assertAlmostEqual(sum(huge.values()), 1)

    def test_bad_config_keys_fall_back_individually(self):
        self.env["CLAUDE_SESSION_MODE_SEED"] = "5"
        for i, weights in enumerate((None, [], {}, {"orchestrator": 0}, {"vanilla": -1},
                                     {"vanilla": "1"}, {"vanilla": True},
                                     {"vanilla": float("nan")}, {"vanilla": float("inf")})):
            with self.subTest(weights=weights):
                self.write_config(weights=weights, exclude="bad", reroll_calls=True, quota_snapshot="bad")
                self.assertIn("claude-only", self.start(session=f"bad{i}")["systemMessage"])
                with mock.patch.dict(os.environ, self.env, clear=True):
                    config = SESSION_MODE.load_config()
                self.assertEqual(config["exclude"], ["claude-orchestrator"])
                self.assertEqual(config["reroll_calls"], 3)
                self.assertIs(config["quota_snapshot"], True)
        self.write_config(weights="bad", exclude=[], reroll_calls=0, quota_snapshot=False)
        with mock.patch.dict(os.environ, self.env, clear=True):
            config = SESSION_MODE.load_config()
        self.assertEqual(config["exclude"], [])
        self.assertEqual(config["reroll_calls"], 0)
        self.assertIs(config["quota_snapshot"], False)

    def test_missing_broken_and_unreadable_config_use_defaults(self):
        self.env["CLAUDE_SESSION_MODE_SEED"] = "5"
        for i, text in enumerate(("garbage", "null", "[]", "42")):
            self.config.write_text(text, encoding="utf-8")
            self.assertIn("claude-only", self.start(session=f"broken{i}")["systemMessage"])
        self.config.unlink()
        self.config.mkdir()  # reliably unreadable as a file even when tests run as root
        self.assertIn("claude-only", self.start()["systemMessage"])

    def test_invalid_seed_and_unknown_forced_mode_are_ignored(self):
        self.env["CLAUDE_SESSION_MODE_SEED"] = "not-an-int"
        self.start("unknown")
        row = self.rows()[-1]
        self.assertIn(row["mode"], SESSION_MODE.MODES)
        self.assertIs(row["forced"], False)
        self.assertIsInstance(row["u"], float)
        self.env["CLAUDE_SESSION_MODE_SEED"] = "5"
        self.assertIn("claude-only", self.start("unknown", session="seeded")["systemMessage"])

    def test_forced_modes_and_per_mode_context(self):
        labels = {"orchestrator": "orchestrator", "claude-only": "claude-only (control: delegation off)",
                  "vanilla": "vanilla (control: no rules, delegation off)"}
        for mode in SESSION_MODE.MODES:
            with self.subTest(mode=mode):
                output = self.start(mode, session=mode)
                self.assertEqual(output["systemMessage"], f"session mode: {labels[mode]} (forced)")
                specific = output["hookSpecificOutput"]
                self.assertEqual(specific["hookEventName"], "SessionStart")
                if mode == "vanilla":
                    self.assertNotIn("additionalContext", specific)
                else:
                    expected = "HYGIENE_MARKER\n\n" + ("DELEGATION_MARKER" if mode == "orchestrator" else CONTROL_LINE)
                    self.assertEqual(specific["additionalContext"], expected)
                row = self.rows()[-1]
                self.assertIs(row["forced"], True)
                self.assertIsNone(row["u"])

    def test_missing_rules_inject_what_exists(self):
        (self.rules / "hygiene.md").unlink()
        output = self.start("orchestrator")
        self.assertEqual(output["hookSpecificOutput"]["additionalContext"], "DELEGATION_MARKER")
        (self.rules / "delegation.md").unlink()
        self.assertEqual(self.start("claude-only", session="control")["hookSpecificOutput"]["additionalContext"], CONTROL_LINE)
        self.assertNotIn("additionalContext", self.start("orchestrator", session="empty")["hookSpecificOutput"])

    @unittest.skipUnless(all((ROOT / "rules" / name).is_file() for name in ("hygiene.md", "delegation.md")),
                         "split rules are supplied by the main checkout")
    def test_real_repo_rules_exist_and_are_nonempty(self):
        for name in ("hygiene.md", "delegation.md"):
            self.assertTrue((ROOT / "rules" / name).read_text(encoding="utf-8").strip())

    def test_excluded_repo_and_forced_override(self):
        self.repo = self.tmp / "claude-orchestrator"
        (self.repo / ".git").mkdir(parents=True)
        self.env["CLAUDE_SESSION_MODE_SEED"] = "5"
        output = self.start()
        self.assertEqual(output["systemMessage"], "session mode: orchestrator (repo excluded from the draw)")
        row = self.rows()[-1]
        self.assertIs(row["excluded"], True)
        self.assertIsNone(row["u"])
        output = self.start("vanilla", session="override")
        self.assertEqual(output["systemMessage"], "session mode: vanilla (control: no rules, delegation off) (forced)")
        self.assertIs(self.rows()[-1]["excluded"], False)
        self.write_config(exclude=[])
        self.assertIn("claude-only", self.start(session="included")["systemMessage"])

    def test_github_origin_parsing_and_exclusion_by_full_or_short_name(self):
        nested = self.repo / "src/package"
        nested.mkdir(parents=True)
        urls = ("git@github.com:owner/name.git", "https://github.com/owner/name.git",
                "https://github.com/owner/name", "https://github.com/owner/name.git/")
        for i, url in enumerate(urls):
            with self.subTest(url=url):
                (self.repo / ".git/config").write_text(f'[remote "origin"]\n\turl = {url}\n', encoding="utf-8")
                self.write_config(exclude=["owner/name" if i % 2 else "name"])
                self.start(session=f"remote{i}", cwd=str(nested))
                row = self.rows()[-1]
                self.assertEqual((row["repo"], row["repo_dir"]), ("owner/name", "project"))
                self.assertIs(row["excluded"], True)
        (self.repo / ".git/config").write_text('[remote "origin"]\nurl = https://example.com/owner/name.git\n', encoding="utf-8")
        self.start("orchestrator", session="other")
        self.assertIsNone(self.rows()[-1]["repo"])

    def test_worktree_git_file_and_nearest_repo(self):
        nested = self.repo / "worktree"
        nested.mkdir()
        (nested / ".git").write_text("gitdir: /unreadable/git/worktrees/worktree\n", encoding="utf-8")
        self.start("orchestrator", cwd=str(nested))
        self.assertEqual((self.rows()[-1]["repo"], self.rows()[-1]["repo_dir"]), (None, "worktree"))
        self.start("orchestrator", session="no-repo", cwd=self.tmp.anchor)
        self.assertEqual((self.rows()[-1]["repo"], self.rows()[-1]["repo_dir"]), (None, None))

    def test_linked_worktree_takes_main_origin_and_stays_excluded(self):
        main = self.tmp / "checkout"
        (main / ".git/worktrees/wt1").mkdir(parents=True)
        (main / ".git/config").write_text('[remote "origin"]\n\turl = git@github.com:egorpol/claude-orchestrator.git\n',
                                          encoding="utf-8")
        (main / ".git/worktrees/wt1/commondir").write_text("../..\n", encoding="utf-8")
        wt = self.tmp / "runs/wt1"
        wt.mkdir(parents=True)
        (wt / ".git").write_text(f"gitdir: {main}/.git/worktrees/wt1\n", encoding="utf-8")
        output = self.start(session="wt", cwd=str(wt))
        self.assertEqual(output["systemMessage"], "session mode: orchestrator (repo excluded from the draw)")
        row = self.rows()[-1]
        self.assertEqual((row["repo"], row["repo_dir"], row["excluded"]), ("egorpol/claude-orchestrator", "wt1", True))
        # a relative gitdir resolves against the worktree, and a missing commondir falls back to gitdir itself
        (main / ".git/worktrees/wt1/commondir").unlink()
        (main / ".git/worktrees/wt1/config").write_text('[remote "origin"]\n\turl = https://github.com/o/n\n',
                                                         encoding="utf-8")
        (wt / ".git").write_text("gitdir: ../../checkout/.git/worktrees/wt1\n", encoding="utf-8")
        self.start("orchestrator", session="wt-rel", cwd=str(wt))
        self.assertEqual(self.rows()[-1]["repo"], "o/n")

    def test_sudo_doas_and_eval_wrappers_are_denied(self):
        self.start("claude-only")
        for command in ("sudo cursor-agent -p hi", "sudo -u egor -E codex exec -", "doas -u root codex exec -",
                        "eval 'codex exec -'", "eval delegate launch codex --brief b.md"):
            with self.subTest(command=command):
                self.assertIn("permissionDecision", json.dumps(self.tool("Bash", {"command": command})))
        for command in ("sudo ls", "eval echo codex", "eval 'delegate status r1'"):
            with self.subTest(command=command):
                self.assertNotIn("permissionDecision", json.dumps(self.tool("Bash", {"command": command})))

    def test_resume_and_compact_inherit_and_reinject_without_system_message(self):
        for mode in SESSION_MODE.MODES:
            original = self.start(mode, session=mode)["hookSpecificOutput"]
            for source in ("resume", "compact"):
                with self.subTest(mode=mode, source=source):
                    output = self.start("vanilla" if mode != "vanilla" else "orchestrator",
                                        session=mode, source=source)
                    self.assertNotIn("systemMessage", output)
                    self.assertEqual(output["hookSpecificOutput"], original)
                    row = self.rows()[-1]
                    self.assertEqual(row["mode"], mode)
                    self.assertIs(row["inherited"], True)
                    self.assertIs(row["forced"], True)
                    self.assertIsNone(row["u"])

    def test_unassigned_resume_and_compact_stay_unassigned(self):
        for source in ("resume", "compact"):
            output = self.start("vanilla", source=source)
            self.assertNotIn("systemMessage", output)
            self.assertEqual(output["hookSpecificOutput"]["additionalContext"], "HYGIENE_MARKER\n\nDELEGATION_MARKER")
            row = self.rows()[-1]
            self.assertEqual(row["mode"], "orchestrator")
            self.assertIs(row["inherited"], True)
            self.assertIs(row["unassigned"], True)
            self.assertIs(row["forced"], False)

    def test_duplicate_startup_or_clear_does_not_change_assignment(self):
        self.env["CLAUDE_SESSION_MODE_SEED"] = "5"
        self.start()
        for source in ("startup", "clear"):
            self.start("vanilla", source=source)
            self.assertEqual(self.rows()[-1]["mode"], "claude-only")
            self.assertIs(self.rows()[-1]["inherited"], True)

    def test_lookup_uses_latest_start_and_ignores_other_and_broken_rows(self):
        self.start("orchestrator")
        self.start("vanilla", session="other")
        latest = dict(self.rows()[0], mode="claude-only")
        with self.log.open("a", encoding="utf-8") as stream:
            stream.write("not json\n[]\n" + json.dumps(latest) + "\n")
            stream.write(json.dumps({"event": "end", "session": "s1", "mode": "vanilla"}) + "\n")
        output = self.start(source="resume")
        self.assertIn(CONTROL_LINE, output["hookSpecificOutput"]["additionalContext"])
        self.assert_denied(self.tool("Bash", {"command": "codex exec -"}), "claude-only")

    def test_all_required_bash_denials_and_allows_in_both_controls(self):
        for mode in ("claude-only", "vanilla"):
            self.start(mode, session=mode)
            for command in MUST_DENY:
                with self.subTest(mode=mode, command=command):
                    output = self.tool("Bash", {"command": command}, session=mode)
                    self.assert_denied(output, mode)
                    self.assertEqual(self.rows()[-1]["detail"], command)
            before = len(self.rows())
            for command in MUST_ALLOW:
                with self.subTest(mode=mode, command=command):
                    self.assertIsNone(self.tool("Bash", {"command": command}, session=mode))
            self.assertEqual(len(self.rows()), before)
        blocks = [row for row in self.rows() if row["event"] == "blocked"]
        self.assertEqual(len(blocks), 2 * len(MUST_DENY))
        self.assertEqual(set(blocks[0]), {"event", "session", "ts", "mode", "tool", "detail"})
        self.assertEqual(blocks[0]["tool"], "Bash")

    def test_bash_wrappers_separators_quotes_and_shlex_fallback(self):
        deny = (
            "env -i -u SECRET FOO=x timeout --signal TERM --kill-after 5 60 nice -n 4 /bin/codex exec -",
            "command -- /bin/cursor-agent", "exec -a worker codex exec -", "time -p codex --help",
            "nice --adjustment=5 nohup setsid -f delegate launch codex", "env -S 'codex exec -'",
            "env -i -u SECRET -S 'codex exec -'", "env --split-string='cursor-agent -p hi'",
            "env -i -Scodex exec -", "bash -o pipefail -c 'codex review'",
            "true;codex exec -", "false || cursor-agent -p hi", "echo hi | codex exec -",
            "echo hi & delegate ask hi", "echo hi\ncodex exec -", "(codex review)",
            "echo `codex --version`", 'echo "$(codex --version)"',
            'echo "$(echo $(codex --version))"', "sh -c 'echo hi; delegate review r1'",
            'zsh -lc "cursor-agent -p hi"', "python /path/delegate launch codex",
            'codex exec "unterminated', "codex \\\nexec -",
        )
        allow = (
            "echo 'hi; codex review'", 'echo "delegate launch"', "echo '$(codex --version)'",
            "echo hi # codex; delegate launch codex", "echo 'cursor-agent | codex'",
            "delegate wait r1", "delegate diff r1", "delegate cleanup r1", "python3 -c 'print(1)'",
            "timeout --signal TERM 10 delegate status r1", "env FOO=1 delegate report --dry-run",
            'echo "unterminated',
        )
        for command in deny:
            with self.subTest(command=command):
                self.assertTrue(SESSION_MODE.bash_delegates(command))
        for command in allow:
            with self.subTest(command=command):
                self.assertFalse(SESSION_MODE.bash_delegates(command))

    def test_orchestrator_and_unknown_mode_deny_nothing(self):
        for session in ("known", "unknown"):
            if session == "known":
                self.start("orchestrator", session=session)
            for command in MUST_DENY + MUST_ALLOW:
                self.assertIsNone(self.tool("Bash", {"command": command}, session=session))
            self.assertIsNone(self.tool("Skill", {"skill": "delegate"}, session=session))
            self.assertIsNone(self.tool("Agent", {"subagent_type": "codex:worker"}, session=session))
        self.assertEqual(len(self.rows()), 1)

    def test_skill_and_agent_rules_in_both_controls(self):
        for mode in ("claude-only", "vanilla"):
            self.start(mode, session=mode)
            for skill in ("delegate", "/delegate", "codex:review", "/codex:worker"):
                self.assert_denied(self.tool("Skill", {"skill": skill, "args": "status r1"}, session=mode), mode)
                self.assertEqual(self.rows()[-1]["detail"], skill)
                self.assertEqual(self.rows()[-1]["tool"], "Skill")
            for skill in ("field-report", "/field-report", "other", "delegate-other", "codex"):
                self.assertIsNone(self.tool("Skill", {"skill": skill}, session=mode))
            self.assert_denied(self.tool("Agent", {"subagent_type": "codex:worker"}, session=mode), mode)
            self.assertEqual(self.rows()[-1]["detail"], "codex:worker")
            self.assertEqual(self.rows()[-1]["tool"], "Agent")
            for inputs in ({}, {"prompt": "use codex", "description": "delegate"},
                           {"subagent_type": "general-purpose"}, {"subagent_type": "codex"}):
                self.assertIsNone(self.tool("Agent", inputs, session=mode))

    def test_detail_is_cut_to_200_characters(self):
        self.start("vanilla")
        command = "codex exec " + "x" * 250
        self.assert_denied(self.tool("Bash", {"command": command}), "vanilla")
        self.assertEqual(self.rows()[-1]["detail"], command[:200])

    def test_end_deduplicates_main_assistant_ids_and_skips_sidechains(self):
        self.env["CLAUDE_SESSION_MODE_SEED"] = "5"
        self.start()
        records = [
            {"type": "assistant", "message": {"id": "one"}},
            {"type": "assistant", "message": {"id": "one", "content": [{"text": "block two"}]}},
            {"type": "assistant", "message": {"id": "two"}},
            {"type": "assistant", "message": {"id": "side"}, "isSidechain": True},
            {"type": "user", "message": {"id": "user"}},
            {"type": "assistant", "message": {}}, {"type": "assistant", "message": None}, [],
        ]
        self.transcript.write_text("not json\n" + "".join(json.dumps(row) + "\n" for row in records), encoding="utf-8")
        self.assertIsNone(self.event("SessionEnd", reason="prompt_input_exit"))
        row = self.rows()[-1]
        self.assertEqual(set(row), {"event", "session", "ts", "reason", "mode", "calls", "reroll"})
        self.assertEqual((row["event"], row["mode"], row["calls"]), ("end", "claude-only", 2))
        self.assertEqual(row["reason"], "prompt_input_exit")
        self.assertIs(row["reroll"], False)

    def test_missing_transcript_and_unknown_end_have_null_values(self):
        self.start("vanilla")
        self.event("SessionEnd", reason="clear")
        self.assertIsNone(self.rows()[-1]["calls"])
        self.assertIs(self.rows()[-1]["reroll"], False)
        self.event("SessionEnd", session="unknown", reason="logout")
        self.assertIsNone(self.rows()[-1]["mode"])
        self.assertIsNone(self.rows()[-1]["calls"])
        self.assertIs(self.rows()[-1]["reroll"], False)

    def test_reroll_only_for_small_drawn_sessions_cleared(self):
        self.env["CLAUDE_SESSION_MODE_SEED"] = "5"
        for count in (0, 3, 4):
            session = f"calls{count}"
            self.start(session=session)
            self.write_calls(count)
            self.event("SessionEnd", session=session, reason="clear")
            self.assertIs(self.rows()[-1]["reroll"], count <= 3)
        self.write_calls(0)
        for reason in ("prompt_input_exit", "other", "logout"):
            self.event("SessionEnd", session="calls0", reason=reason)
            self.assertIs(self.rows()[-1]["reroll"], False)
        self.write_config(reroll_calls=0)
        self.write_calls(1)
        self.event("SessionEnd", session="calls0", reason="clear")
        self.assertIs(self.rows()[-1]["reroll"], False)

    def test_reroll_preserves_draw_provenance_across_resume_and_compact(self):
        self.env["CLAUDE_SESSION_MODE_SEED"] = "5"
        self.start()
        for source in ("resume", "compact"):
            self.start("vanilla", source=source)
            self.write_calls(1)
            self.event("SessionEnd", reason="clear")
            self.assertIs(self.rows()[-1]["reroll"], True)
            self.assertEqual(self.rows()[-1]["mode"], "claude-only")

    def test_forced_excluded_and_unassigned_sessions_never_reroll(self):
        self.write_calls(0)
        for kind in ("forced", "excluded", "unassigned"):
            if kind == "forced":
                self.start("claude-only", session=kind)
            elif kind == "excluded":
                self.write_config(exclude=["project"])
                self.start(session=kind)
            else:
                self.start(session=kind, source="resume")
            self.start(session=kind, source="compact")
            self.event("SessionEnd", session=kind, reason="clear")
            self.assertIs(self.rows()[-1]["reroll"], False)

    def test_bad_stdin_unknown_events_and_bad_fields_stay_silent(self):
        for raw in ("garbage", "", "[]", "null", "42", "{}", '{"session_id": 4}'):
            self.assertIsNone(self.run_hook(raw=raw))
        self.assertIsNone(self.event("Other"))
        self.assertIsNone(self.start(source="other"))
        self.assertIsNone(self.event("SessionStart", session="", source="startup"))
        self.start("vanilla")
        for name in ("Skill", "Agent", "Bash", "Read"):
            for inputs in (None, [], {"skill": 42, "subagent_type": {}, "command": []}):
                self.assertIsNone(self.tool(name, inputs))

    def test_unwritable_log_keeps_start_context_and_other_events_silent(self):
        self.log.parent.mkdir()
        self.log.mkdir()  # a directory at the logfile path is reliably unwritable
        output = self.start("orchestrator")
        self.assertEqual(output["hookSpecificOutput"]["additionalContext"], "HYGIENE_MARKER\n\nDELEGATION_MARKER")
        self.assertIsNone(self.tool("Bash", {"command": "codex exec -"}))
        self.assertIsNone(self.event("SessionEnd", reason="clear"))
        # A known control whose log becomes unwritable also fails open, with no stdout.
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(SESSION_MODE, "latest_start", return_value={"mode": "vanilla"}), \
                mock.patch("sys.stdin", io.StringIO(json.dumps({
                    "hook_event_name": "PreToolUse", "session_id": "s1", "tool_name": "Bash",
                    "tool_input": {"command": "codex exec -"}}))), \
                mock.patch("sys.stdout", io.StringIO()) as output:
            SESSION_MODE.main()
        self.assertEqual(output.getvalue(), "")

    def test_default_xdg_paths(self):
        self.env.pop("CLAUDE_SESSION_LOG")
        self.env.pop("CLAUDE_SESSION_MODE_CONFIG")
        self.log = self.tmp / "cache/claude-delegate/sessions.jsonl"
        config = self.tmp / "config/claude-orchestrator/session-mode.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"weights": {"vanilla": 1}}', encoding="utf-8")
        self.assertEqual(self.start()["systemMessage"], "session mode: vanilla (control: no rules, delegation off)")
        self.assertEqual(self.rows()[0]["mode"], "vanilla")

    def test_quota_snapshots_are_detached_and_only_start_for_new_sources(self):
        fake_root = self.tmp / "fake-root"
        hook = fake_root / "hooks/session-mode.py"
        hook.parent.mkdir(parents=True)
        shutil.copyfile(HOOK, hook)
        delegate = fake_root / "skills/delegate/scripts/delegate"
        delegate.parent.mkdir(parents=True)
        delegate.write_text(
            "import json, os, sys, time\n"
            "time.sleep(0.1)\n"
            "print(json.dumps({'args': sys.argv[1:], 'pid': os.getpid(), "
            "'sid': os.getsid(0), 'stdin': sys.stdin.read()}))\n", encoding="utf-8")
        self.env.pop("CLAUDE_SESSION_QUOTA")
        self.env["CLAUDE_SESSION_MODE"] = "orchestrator"
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(SESSION_MODE, "ROOT", fake_root), \
                mock.patch.object(SESSION_MODE.subprocess, "Popen") as popen:
            SESSION_MODE.quota_snapshot(self.log, "mocked", "start", {"quota_snapshot": True})
            args, kwargs = popen.call_args
            self.assertEqual(args[0], [sys.executable, str(delegate), "quota", "--json"])
            self.assertIs(kwargs["start_new_session"], True)
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
            popen.return_value.wait.assert_not_called()
            popen.return_value.communicate.assert_not_called()
        for event, fields, suffix in (("SessionStart", {"source": "startup"}, "start"),
                                      ("SessionEnd", {"reason": "clear"}, "end")):
            self.run_hook({"hook_event_name": event, "session_id": "snapshot", "cwd": str(self.repo),
                           "transcript_path": str(self.transcript), **fields}, hook=hook)
            path = self.log.parent / "quota" / f"snapshot-{suffix}.json"
            deadline = time.monotonic() + 3
            while True:
                try:
                    snapshot = json.loads(path.read_text(encoding="utf-8"))
                    break
                except (OSError, ValueError):
                    if time.monotonic() >= deadline:
                        self.fail(f"snapshot did not finish: {path}")
                    time.sleep(0.02)
            self.assertEqual(snapshot["args"], ["quota", "--json"])
            self.assertEqual(snapshot["pid"], snapshot["sid"])
            self.assertEqual(snapshot["stdin"], "")
        for source in ("resume", "compact"):
            self.run_hook({"hook_event_name": "SessionStart", "session_id": source,
                           "source": source, "cwd": str(self.repo)}, hook=hook)
            self.assertFalse((self.log.parent / "quota" / f"{source}-start.json").exists())
        self.write_config(quota_snapshot=False)
        self.run_hook({"hook_event_name": "SessionStart", "session_id": "disabled",
                       "source": "startup", "cwd": str(self.repo)}, hook=hook)
        self.assertFalse((self.log.parent / "quota/disabled-start.json").exists())
        self.write_config(quota_snapshot=True)
        self.env["CLAUDE_SESSION_QUOTA"] = "0"
        self.run_hook({"hook_event_name": "SessionStart", "session_id": "env-disabled",
                       "source": "startup", "cwd": str(self.repo)}, hook=hook)
        self.assertFalse((self.log.parent / "quota/env-disabled-start.json").exists())


if __name__ == "__main__":
    unittest.main()

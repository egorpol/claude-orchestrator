"""Installer tests isolate home/config and fake both openusage and systemctl."""
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

INSTALL = Path(__file__).resolve().parents[1] / "install.sh"
REPO = INSTALL.parent
RULES_LINE = f"@{REPO}/rules/orchestrator.md"
GUARD = f'python3 "{REPO}/hooks/context-guard.py"'
SECRET = f'python3 "{REPO}/hooks/secret-guard.py"'
MODE = f'python3 "{REPO}/hooks/session-mode.py"'


class InstallTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="install-test-")
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)
        self.bin = self.tmp / "fake-bin"
        self.bin.mkdir()
        self.calls = self.tmp / "systemctl.calls"
        self.env = {"HOME": str(self.tmp / "home"),
                    "XDG_CONFIG_HOME": str(self.tmp / "config"),
                    "CLAUDE_CONFIG_DIR": str(self.tmp / "claude"),
                    "PATH": f"{self.bin}:/usr/bin:/bin", "FAKE_SYSTEMCTL_CALLS": str(self.calls)}
        self.units = self.tmp / "config/systemd/user"
        self.service = self.units / "claude-delegate-quota.service"
        self.timer = self.units / "claude-delegate-quota.timer"
        self.exe("openusage", "#!/bin/sh\nprintf 'fake-openusage\\n'\n")
        self.exe("systemctl", "#!/bin/sh\n"
                 "printf '%s\\n' \"$*\" >> \"$FAKE_SYSTEMCTL_CALLS\"\n"
                 "if [ \"$2\" = is-enabled ]; then exit ${FAKE_TIMER_ENABLED:-1}; fi\n")

    def exe(self, name, contents):
        path = self.bin / name
        path.write_text(contents)
        path.chmod(0o755)

    def install(self, *args, env=None, code=0):
        result = subprocess.run(["/bin/bash", str(INSTALL), *args],
                                env={**self.env, **(env or {})}, capture_output=True, text=True)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result.stdout

    @property
    def settings(self):
        return self.tmp / "claude/settings.json"

    @property
    def user_md(self):
        return self.tmp / "claude/CLAUDE.md"

    def test_hooks_alone_register_context_and_secret_guard(self):
        self.install("--rules")
        self.install("--hooks")
        hooks = json.loads(self.settings.read_text())["hooks"]
        self.assertEqual(set(hooks), {"UserPromptSubmit", "PreToolUse"})
        self.assertEqual(hooks["UserPromptSubmit"], [
            {"hooks": [{"type": "command", "command": GUARD, "timeout": 5}]}])
        self.assertEqual(hooks["PreToolUse"], [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": SECRET, "timeout": 30}]}])
        self.assertIn(RULES_LINE, self.user_md.read_text())

    def test_hooks_and_experiment_register_once_and_keep_existing_settings(self):
        self.settings.parent.mkdir(parents=True)
        other = {"hooks": [{"type": "command", "command": "other-hook"}]}
        self.settings.write_text(json.dumps({"theme": "dark", "hooks": {"UserPromptSubmit": [other]}}))
        self.install("--hooks", "--experiment")
        backups = sorted(self.settings.parent.glob("settings.json.bak-*"))
        self.assertEqual(len(backups), 2)  # one per change, never over the first
        self.install("--hooks", "--experiment")
        settings = json.loads(self.settings.read_text())
        self.assertEqual(settings["theme"], "dark")
        hooks = settings["hooks"]
        self.assertEqual(hooks["UserPromptSubmit"][0], other)
        expected = [
            ("UserPromptSubmit", None, GUARD, 5),
            ("PreToolUse", "Bash", SECRET, 30),
            ("SessionStart", None, MODE, 10),
            ("PreToolUse", "Skill|Agent|Bash", MODE, 5),
            ("SessionEnd", None, MODE, 10),
        ]
        for event, matcher, command, timeout in expected:
            ours = [g for g in hooks[event] if any(h["command"] == command for h in g["hooks"])]
            self.assertEqual(ours, [{**({"matcher": matcher} if matcher else {}),
                                     "hooks": [{"type": "command", "command": command, "timeout": timeout}]}],
                             (event, command))
        # The re-run changed nothing, so it made no backup.
        self.assertEqual(sorted(self.settings.parent.glob("settings.json.bak-*")), backups)

    def test_hooks_replace_stale_registrations_and_tolerate_null_groups(self):
        self.settings.parent.mkdir(parents=True)
        mixed = {"matcher": "Read", "hooks": [{"type": "command", "command": MODE},
                                                {"type": "command", "command": "mine"}]}
        self.settings.write_text(json.dumps({"hooks": {
            "PreToolUse": [mixed], "UserPromptSubmit": [{"hooks": None}],
            "SessionEnd": [{"hooks": [{"type": "command", "command": MODE, "timeout": 30}]}]}}))
        self.install("--hooks", "--experiment")
        hooks = json.loads(self.settings.read_text())["hooks"]
        self.assertEqual(hooks["PreToolUse"], [
            {"matcher": "Read", "hooks": [{"type": "command", "command": "mine"}]},
            {"matcher": "Bash", "hooks": [{"type": "command", "command": SECRET, "timeout": 30}]},
            {"matcher": "Skill|Agent|Bash", "hooks": [{"type": "command", "command": MODE, "timeout": 5}]}])
        self.assertEqual(hooks["UserPromptSubmit"][0], {"hooks": None})
        self.assertEqual(hooks["SessionEnd"], [{"hooks": [{"type": "command", "command": MODE, "timeout": 10}]}])

    def test_git_hook_links_idempotent_refuses_and_force_backup(self):
        repo = self.tmp / "proj"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        dest = Path(subprocess.run(
            ["git", "rev-parse", "--git-path", "hooks/pre-push"],
            cwd=repo, check=True, capture_output=True, text=True,
        ).stdout.strip())
        if not dest.is_absolute():
            dest = repo / dest
        out = self.install(f"--git-hook={repo}")
        self.assertIn("pre-push", out)
        self.assertTrue(dest.is_symlink())
        self.assertEqual(dest.resolve(), REPO / "hooks/secret-guard.py")
        self.install(f"--git-hook={repo}")  # idempotent
        self.assertTrue(dest.is_symlink())
        dest.unlink()
        dest.write_text("#!/bin/sh\nexit 0\n")
        dest.chmod(0o755)
        result = subprocess.run(["/bin/bash", str(INSTALL), f"--git-hook={repo}"],
                                env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("exists and is not this secret-guard", result.stdout + result.stderr)
        self.assertTrue(dest.is_file() and not dest.is_symlink())
        self.install(f"--git-hook={repo}", "--force")
        self.assertTrue(dest.is_symlink())
        self.assertEqual(dest.resolve(), REPO / "hooks/secret-guard.py")
        backups = list((self.tmp / "claude/skill-backups").glob("pre-push-*"))
        self.assertEqual(len(backups), 1)

    def test_both_git_hooks_link_and_rerun_is_idempotent(self):
        repo = self.tmp / "both-hooks"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        out = self.install(f"--git-hook={repo}")
        for hook in ("pre-push", "pre-commit"):
            self.assertIn(f"{hook} ->", out)
            dest = repo / ".git/hooks" / hook
            self.assertTrue(dest.is_symlink())
            self.assertEqual(dest.resolve(), REPO / "hooks/secret-guard.py")
        out = self.install(f"--git-hook={repo}")
        self.assertIn("pre-push already linked", out)
        self.assertIn("pre-commit already linked", out)
        self.assertFalse((self.tmp / "claude/skill-backups").exists())

    def test_foreign_pre_commit_is_independent_and_force_backs_it_up(self):
        repo = self.tmp / "foreign-hook"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        dest = repo / ".git/hooks/pre-commit"
        contents = "#!/bin/sh\nexit 0\n"
        dest.write_text(contents)
        dest.chmod(0o755)
        out = self.install(f"--git-hook={repo}", code=1)
        self.assertIn("pre-commit:", out)
        self.assertIn("exists and is not this secret-guard", out)
        self.assertEqual(dest.read_text(), contents)
        self.assertTrue((repo / ".git/hooks/pre-push").is_symlink())
        self.install(f"--git-hook={repo}", "--force")
        self.assertTrue(dest.is_symlink())
        backups = list((self.tmp / "claude/skill-backups").glob("pre-commit-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), contents)

    def test_check_reports_deny_list_missing_entries_and_worktree_warning(self):
        config = self.tmp / "config"
        config.mkdir()
        # TMPDIR is inside the checkout: stop discovery above our scratch config.
        env = {"GIT_CEILING_DIRECTORIES": str(self.tmp)}
        path = config / "claude-orchestrator/private-names.txt"
        out = self.install("--check", env=env)
        self.assertIn("private-name deny list missing", out)
        self.assertIn(str(path), out)
        path.parent.mkdir()
        names = ("fictional-private-repo", "acme-secret-thing")
        path.write_text(f"# only local names\n\n  {names[0]}  \n{names[1]}\n")
        out = self.install("--check", env=env)
        self.assertIn(str(path), out)
        self.assertIn("2 entries", out)
        self.assertNotIn("inside a git work tree", out)
        for name in names:
            self.assertNotIn(name.lower(), out.lower())
        subprocess.run(["git", "init"], cwd=config, check=True, capture_output=True)
        out = self.install("--check", env=env)
        self.assertIn("inside a git work tree", out)
        self.assertIn("one git add could publish it", out)
        for name in names:
            self.assertNotIn(name.lower(), out.lower())

    def test_check_deny_list_empty_xdg_falls_back_to_home(self):
        path = self.tmp / "home/.config/claude-orchestrator/private-names.txt"
        path.parent.mkdir(parents=True)
        path.write_text("fictional-private-repo\n")
        out = self.install("--check", env={"XDG_CONFIG_HOME": ""})
        self.assertIn(str(path), out)
        self.assertIn("1 entries", out)
        self.assertNotIn("fictional-private-repo", out)

    def test_hooks_refuse_malformed_settings_without_a_traceback(self):
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text("{broken")
        result = subprocess.run(["/bin/bash", str(INSTALL), "--hooks"], env=self.env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("is not valid JSON", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.settings.read_text(), "{broken")
        self.assertEqual(list(self.settings.parent.glob("settings.json.bak-*")), [])

    def test_check_ignores_mentions_that_are_not_registrations_or_imports(self):
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text(json.dumps({"notes": "hooks/session-mode.py"}))
        self.user_md.write_text(f"# see {RULES_LINE} for the rules\n")
        output = self.install("--check")
        self.assertIn("session-mode hook not registered", output)
        self.assertIn("orchestrator rules not imported", output)
        self.install("--rules")
        self.assertTrue(self.user_md.read_text().endswith(f"\n\n{RULES_LINE}\n"))

    def test_import_removal_keeps_crlf_and_other_lines(self):
        self.user_md.parent.mkdir(parents=True)
        self.user_md.write_bytes(f"# mine\r\n\r\n  {RULES_LINE}  \r\n# after\r\n\r\n".encode())
        self.install("--experiment")
        self.assertEqual(self.user_md.read_bytes(), b"# mine\r\n\r\n# after\r\n\r\n")

    def test_experiment_replaces_the_rules_import_and_rules_then_skip(self):
        self.user_md.parent.mkdir(parents=True)
        self.user_md.write_text("# mine\n")
        self.install("--rules")
        self.assertEqual(self.user_md.read_text(), f"# mine\n\n{RULES_LINE}\n")
        output = self.install("--experiment")
        self.assertIn("import removed", output)
        self.assertEqual(self.user_md.read_text(), "# mine\n")
        self.assertIn("rules not imported", self.install("--rules"))
        self.assertEqual(self.user_md.read_text(), "# mine\n")

    def test_rules_and_experiment_together_never_import(self):
        self.install("--rules", "--experiment")
        self.assertFalse(self.user_md.exists())
        self.assertIn("SessionStart", json.loads(self.settings.read_text())["hooks"])

    def test_experiment_leaves_an_empty_user_md_when_the_import_was_its_only_line(self):
        self.install("--rules")
        self.install("--experiment")
        self.assertEqual(self.user_md.read_text(), "")

    def test_check_reports_session_mode_import_conflict_and_next(self):
        output = self.install("--check")
        self.assertIn("session-mode hook not registered", output)
        self.assertIn("orchestrator rules not imported", output)
        self.assertIn("/next command not installed", output)
        self.install("--rules")
        output = self.install("--check")
        self.assertIn("orchestrator rules imported", output)
        self.assertIn("/next command in", output)
        self.install("--experiment")
        output = self.install("--check")
        self.assertIn("session-mode hook registered", output)
        self.assertNotIn("also imported", output)
        with self.user_md.open("a") as stream:
            stream.write(RULES_LINE + "\n")
        self.assertIn("also imported", self.install("--check"))

    def test_next_command_is_linked_copied_and_never_overwritten(self):
        target = self.tmp / "claude/commands/next.md"
        self.install()
        self.assertTrue(target.is_symlink())
        self.assertEqual(target.resolve(), REPO / "commands/next.md")
        self.install("--copy")
        self.assertFalse(target.is_symlink())
        self.assertEqual(target.read_text(), (REPO / "commands/next.md").read_text())
        self.assertIn("exists and is not a symlink", self.install(code=1))
        self.install("--force")
        self.assertTrue(target.is_symlink())
        self.assertEqual(len(list((self.tmp / "claude/skill-backups").glob("next.md-*"))), 1)

    def test_timer_units_commands_and_reinstallation_are_idempotent(self):
        self.install("--timer")
        service, timer = self.service.read_text(), self.timer.read_text()
        self.assertIn("[Service]\nType=oneshot\nNice=10\n", service)
        exec_line = next(line for line in service.splitlines() if line.startswith("ExecStart="))
        exec_args = shlex.split(exec_line.split("=", 1)[1])
        expected_python = subprocess.run(
            ["python3", "-c", "import os, sys; print(os.path.abspath(sys.executable))"],
            env=self.env, text=True, capture_output=True, check=True).stdout.strip()
        self.assertEqual(exec_args, [expected_python,
                                    str(self.tmp / "claude/skills/delegate/scripts/delegate"),
                                    "quota", "--snapshot", "--if-active"])
        environment = next(line for line in service.splitlines() if line.startswith("Environment="))
        self.assertEqual(shlex.split(environment.split("=", 1)[1]),
                         [f"PATH={self.bin}:/usr/local/bin:/usr/bin:/bin",
                          f"CLAUDE_CONFIG_DIR={self.tmp / 'claude'}"])
        for line in ("OnCalendar=*:0/10", "AccuracySec=1min", "Persistent=false",
                     "[Install]", "WantedBy=timers.target"):
            self.assertIn(line + "\n", timer)
        calls = ["--user daemon-reload", "--user enable --now claude-delegate-quota.timer"]
        self.assertEqual(self.calls.read_text().splitlines(), calls)
        self.install("--timer")
        self.assertEqual(self.service.read_text(), service)
        self.assertEqual(self.timer.read_text(), timer)
        self.assertEqual(self.calls.read_text().splitlines(), calls * 2)

    def test_install_without_timer_creates_no_units_or_systemctl_calls(self):
        self.install()
        self.assertFalse(self.units.exists())
        self.assertFalse(self.calls.exists())
        self.assertTrue((self.tmp / "claude/skills/delegate").is_symlink())

    def test_check_reports_enabled_and_disabled_timer_without_installing(self):
        self.assertIn("quota timer not enabled", self.install("--check"))
        self.assertIn("quota timer enabled", self.install("--check", env={"FAKE_TIMER_ENABLED": "0"}))
        self.assertEqual(self.calls.read_text().splitlines(),
                         ["--user is-enabled claude-delegate-quota.timer"] * 2)
        self.assertFalse(self.units.exists())
        self.assertFalse((self.tmp / "claude/skills").exists())

    def test_without_systemctl_writes_units_warns_and_check_uses_unit_file(self):
        # A private PATH excludes the real systemctl/openusage but supplies installer utilities.
        (self.bin / "systemctl").unlink()
        (self.bin / "openusage").unlink()
        for name in ("dirname", "mkdir", "basename", "ln", "readlink", "grep", "git", "head"):
            (self.bin / name).symlink_to(shutil.which(name))
        (self.bin / "python3").symlink_to(sys.executable)
        env = {"PATH": str(self.bin)}
        self.assertIn("quota timer unit not installed", self.install("--check", env=env))
        output = self.install("--timer", env=env)
        self.assertIn("systemctl not found", output)
        self.assertIn("openusage not found", output)
        self.assertTrue(self.service.exists())
        self.assertTrue(self.timer.exists())
        self.assertIn('Environment="PATH=/usr/local/bin:/usr/bin:/bin"', self.service.read_text())
        self.assertFalse(self.calls.exists())
        self.assertIn("quota timer unit exists", self.install("--check", env=env))

    def test_timer_carries_custom_cache_and_log_paths(self):
        self.install("--timer", env={"XDG_CACHE_HOME": str(self.tmp / "cache"),
                                    "CLAUDE_DELEGATE_QUOTA_LOG": str(self.tmp / "q.jsonl")})
        environment = next(line for line in self.service.read_text().splitlines()
                           if line.startswith("Environment="))
        self.assertEqual(shlex.split(environment.split("=", 1)[1])[1:],
                         [f"CLAUDE_CONFIG_DIR={self.tmp / 'claude'}", f"XDG_CACHE_HOME={self.tmp / 'cache'}",
                          f"CLAUDE_DELEGATE_QUOTA_LOG={self.tmp / 'q.jsonl'}"])

    def test_help_documents_timer_and_removal(self):
        output = self.install("--help")
        self.assertIn("--timer", output)
        self.assertIn("systemctl --user disable --now claude-delegate-quota.timer", output)
        self.assertIn("delete claude-delegate-quota.{service,timer}", output)
        self.assertIn("--force", output)
        self.assertIn("session-mode.json", output)
        self.assertIn("--experiment  opt in to control sessions", output)
        self.assertIn("skipped while the session-mode hook is registered", output)
        self.assertNotIn("set -euo", output)
        self.assertFalse(self.units.exists())


if __name__ == "__main__":
    unittest.main()

"""Installer tests isolate home/config and fake cursor-agent and systemctl."""
import _env  # noqa: F401  — isolate HOME/XDG/PATH before scripts/hooks
import configparser
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
GUARD = f'[ ! -f "{REPO}/hooks/context-guard.py" ] || python3 "{REPO}/hooks/context-guard.py"'
SECRET = f'[ ! -f "{REPO}/hooks/secret-guard.py" ] || python3 "{REPO}/hooks/secret-guard.py"'
MODE = f'[ ! -f "{REPO}/hooks/session-mode.py" ] || python3 "{REPO}/hooks/session-mode.py"'
ATTENTION = f'[ ! -f "{REPO}/hooks/attention.py" ] || python3 "{REPO}/hooks/attention.py"'


class InstallTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="install-test-")
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)
        self.bin = self.tmp / "fake-bin"
        self.bin.mkdir()
        self.calls = self.tmp / "systemctl.calls"
        self.env = {"HOME": str(self.tmp / "home"),
                    "DELEGATE_CATALOG_LOCAL": "",
                    "XDG_CONFIG_HOME": str(self.tmp / "config"),
                    "CLAUDE_CONFIG_DIR": str(self.tmp / "claude"),
                    "PATH": f"{self.bin}:{_env.BIN}:/usr/bin:/bin",
                    "FAKE_SYSTEMCTL_CALLS": str(self.calls)}
        self.units = self.tmp / "config/systemd/user"
        self.service = self.units / "claude-delegate-quota.service"
        self.timer = self.units / "claude-delegate-quota.timer"
        self.rate_service = self.units / "claude-delegate-rate.service"
        self.rate_timer = self.units / "claude-delegate-rate.timer"
        self.exe("codex", "#!/bin/sh\nexit 99\n")
        self.exe("claude", "#!/bin/sh\nprintf 'fake-claude\\n'\n")
        self.exe("cursor-agent", "#!/bin/sh\ncase \"$1\" in\n"
                 "status) echo 'Logged in as installer-test' ;;\n"
                 "--version) echo fake-cursor-agent ;;\n"
                 "*) echo 'unexpected cursor-agent call' >&2; exit 99 ;;\nesac\n")
        self.exe("systemctl", "#!/bin/sh\n"
                 "printf '%s\\n' \"$*\" >> \"$FAKE_SYSTEMCTL_CALLS\"\n"
                 "if [ \"$2\" = is-enabled ]; then\n"
                 "case \"$3\" in\n"
                 "claude-delegate-rate.timer) exit ${FAKE_RATE_TIMER_ENABLED:-${FAKE_TIMER_ENABLED:-1}} ;;\n"
                 "*) exit ${FAKE_TIMER_ENABLED:-1} ;;\nesac\nfi\n")

    def exe(self, name, contents):
        path = self.bin / name
        path.write_text(contents)
        path.chmod(0o755)

    def private_path_without(self, *names):
        # Exclude real system tools/agents even when testing a missing dependency.
        for name in names:
            (self.bin / name).unlink()
        for name in ("dirname", "mkdir", "basename", "ln", "readlink", "grep", "git", "head"):
            (self.bin / name).symlink_to(shutil.which(name))
        (self.bin / "python3").symlink_to(sys.executable)
        return {"PATH": str(self.bin)}

    def unit(self, path):
        unit = configparser.ConfigParser(interpolation=None)
        unit.optionxform = str
        unit.read(path)
        return unit

    def install(self, *args, env=None, code=0, checkout=REPO):
        result = subprocess.run(["/bin/bash", str(checkout / "install.sh"), *args],
                                env={**self.env, **(env or {})}, capture_output=True, text=True)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result.stdout

    def checkout(self, name="checkout"):
        checkout = self.tmp / name
        checkout.mkdir()
        shutil.copy2(INSTALL, checkout / "install.sh")
        for directory in ("skills", "commands", "rules", "hooks"):
            shutil.copytree(REPO / directory, checkout / directory, symlinks=True,
                            ignore=shutil.ignore_patterns("__pycache__"))
        return checkout

    def tree_state(self, directory):
        return {str(p.relative_to(directory)): (
            p.lstat().st_mtime_ns, p.lstat().st_mode,
            os.readlink(p) if p.is_symlink() else p.read_bytes() if p.is_file() else None)
            for p in directory.rglob("*")}

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

    def test_attention_registers_merges_backs_up_and_is_idempotent(self):
        self.settings.parent.mkdir(parents=True)
        other = {"matcher": "Read", "hooks": [{"type": "command", "command": "mine"}]}
        original = json.dumps({"theme": "dark", "hooks": {"PreToolUse": [other],
                                                         "SessionStart": [{"hooks": None}]}})
        self.settings.write_text(original)
        self.assertIn("attention hook registered", self.install("--attention"))
        text = self.settings.read_text()
        settings = json.loads(text)
        self.assertEqual(text, json.dumps(settings, indent=2) + "\n")
        self.assertEqual(settings["theme"], "dark")
        hooks = settings["hooks"]
        self.assertEqual(set(hooks), {"SessionStart", "Stop", "PreToolUse", "Notification"})
        self.assertEqual(hooks["SessionStart"], [{"hooks": None}])
        command = {"type": "command", "command": ATTENTION, "timeout": 5}
        self.assertEqual(hooks["Stop"], [{"hooks": [command]}])
        self.assertEqual(hooks["PreToolUse"], [other, {"matcher": "AskUserQuestion", "hooks": [command]}])
        self.assertEqual(hooks["Notification"], [
            {"matcher": "permission_prompt|elicitation_dialog", "hooks": [command]}])
        backups = sorted(self.settings.parent.glob("settings.json.bak-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), original)
        self.install("--attention")
        self.assertEqual(self.settings.read_text(), text)
        self.assertEqual(sorted(self.settings.parent.glob("settings.json.bak-*")), backups)
        self.assertIn("attention hook registered", self.install("--check"))

    def test_attention_check_and_help_before_install(self):
        self.assertIn("optional: attention hook not registered (./install.sh --attention)",
                      self.install("--check"))
        self.assertFalse(self.settings.exists())
        self.assertIn("--attention", self.install("--help"))

    def test_attention_keeps_guard_experiment_and_rules_behaviour(self):
        self.install("--hooks", "--experiment")
        original = json.loads(self.settings.read_text())["hooks"]
        self.install("--attention")
        hooks = json.loads(self.settings.read_text())["hooks"]
        for event, groups in original.items():
            self.assertEqual([g for g in hooks[event] if not any(
                h.get("command") == ATTENTION for h in g.get("hooks") or [])], groups)
        self.assertFalse(self.user_md.exists())

    def test_attention_replaces_stale_registration_keeps_other_hooks(self):
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text(json.dumps({"hooks": {
            "Stop": [{"matcher": "old", "hooks": [{"type": "command", "command": ATTENTION},
                                                    {"type": "command", "command": "mine"}]}],
            "PreToolUse": [{"hooks": None}]}}))
        self.install("--attention")
        hooks = json.loads(self.settings.read_text())["hooks"]
        self.assertEqual(hooks["Stop"], [
            {"matcher": "old", "hooks": [{"type": "command", "command": "mine"}]},
            {"hooks": [{"type": "command", "command": ATTENTION, "timeout": 5}]}])
        self.assertEqual(hooks["PreToolUse"][0], {"hooks": None})

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

    def test_hook_commands_pass_stdout_and_status_then_skip_missing_checkout(self):
        # Exercise the actual registered commands, including shell metacharacters
        # in the checkout path, without editing any hook in the working tree.
        checkout = self.checkout("checkout with 'quotes' $dollar `tick` \\slash")
        self.install("--hooks", "--attention", "--experiment", checkout=checkout)
        groups = json.loads(self.settings.read_text())["hooks"]
        commands = [h["command"] for event in groups.values() for group in event
                    for h in group["hooks"]]
        self.assertEqual(len(commands), 8)
        guard = groups["UserPromptSubmit"][0]["hooks"][0]["command"]
        shells = [shutil.which(name) for name in ("sh", "bash", "zsh", "fish")
                  if shutil.which(name)]
        for status in (0, 2, 7):
            (checkout / "hooks/context-guard.py").write_text(
                'import sys\nprint(\'{"decision": "block"}\')\n' + f'sys.exit({status})\n')
            for shell in shells:
                with self.subTest(shell=shell, status=status):
                    result = subprocess.run([shell, "-c", guard], env=self.env,
                                            input="{}", capture_output=True, text=True)
                    self.assertEqual((result.returncode, result.stdout, result.stderr),
                                     (status, '{"decision": "block"}\n', ""))
        checkout.rename(checkout.with_name("moved checkout"))
        for shell in shells:
            for command in commands:
                with self.subTest(shell=shell, command=command):
                    result = subprocess.run([shell, "-c", command], env=self.env,
                                            input="{}", capture_output=True, text=True)
                    self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_legacy_hooks_migrate_even_beside_current_registrations(self):
        expected = [
            ("UserPromptSubmit", "context-guard.py", 5),
            ("PreToolUse", "secret-guard.py", 30),
            ("SessionStart", "session-mode.py", 10),
            ("PreToolUse", "session-mode.py", 5),
            ("SessionEnd", "session-mode.py", 10),
            ("Stop", "attention.py", 5),
            ("PreToolUse", "attention.py", 5),
            ("Notification", "attention.py", 5),
        ]
        self.settings.parent.mkdir(parents=True)
        hooks = {}
        mine = {"type": "command", "command": "echo mine", "timeout": 12}
        for event, script, _ in expected:
            old = f'python3 "{REPO}/hooks/{script}"'
            hooks.setdefault(event, []).append({"matcher": "old", "extra": "keep", "hooks": [
                {"type": "command", "command": old, "timeout": 99}, mine]})
        self.settings.write_text(json.dumps({"before": True, "hooks": hooks, "after": [1, 2]}))
        self.install("--hooks", "--attention", "--experiment")
        settings = json.loads(self.settings.read_text())
        self.assertEqual(list(settings), ["before", "hooks", "after"])
        for event, script, timeout in expected:
            found = [h for group in settings["hooks"][event] for h in group["hooks"]
                     if script in h["command"]]
            self.assertEqual(len(found), 1, (event, script))
            self.assertTrue(found[0]["command"].startswith("[ ! -f "))
            self.assertEqual(found[0]["timeout"], timeout)
        self.assertEqual(sum(group == {"matcher": "old", "extra": "keep", "hooks": [mine]}
                             for groups in settings["hooks"].values() for group in groups), 8)
        # A partial previous migration can leave legacy and current forms together.
        settings["hooks"]["UserPromptSubmit"].append({"matcher": "stale", "hooks": [
            {"type": "command", "command": f'python3 "{REPO}/hooks/context-guard.py"'}]})
        self.settings.write_text(json.dumps(settings))
        self.install("--hooks", "--attention", "--experiment")
        groups = json.loads(self.settings.read_text())["hooks"]["UserPromptSubmit"]
        self.assertEqual(sum("context-guard.py" in h["command"] for g in groups for h in g["hooks"]), 1)
        state = self.tree_state(self.settings.parent)
        self.install("--hooks", "--attention", "--experiment")
        self.assertEqual(self.tree_state(self.settings.parent), state)

    def test_check_and_rules_recognise_legacy_and_guarded_hooks(self):
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text(json.dumps({"hooks": {
            event: [{"hooks": [{"type": "command", "command": f'python3 "{REPO}/hooks/{script}"'}]}]
            for event, script in (("SessionStart", "session-mode.py"),
                                  ("Stop", "attention.py"),
                                  ("UserPromptSubmit", "context-guard.py"),
                                  ("PreToolUse", "secret-guard.py"))}}))
        for migrate in (False, True):
            if migrate:
                self.install("--hooks", "--attention", "--experiment")
            output = self.install("--check")
            for name in ("session-mode", "attention", "context-guard", "secret-guard"):
                self.assertIn(f"{name} hook registered", output)
            self.assertIn("rules not imported", self.install("--rules"))
            self.assertFalse(self.user_md.exists())

    def assert_hooks_from(self, checkout):
        expected = {("UserPromptSubmit", "context-guard.py"), ("PreToolUse", "secret-guard.py"),
                    ("SessionStart", "session-mode.py"), ("PreToolUse", "session-mode.py"),
                    ("SessionEnd", "session-mode.py"), ("Stop", "attention.py"),
                    ("PreToolUse", "attention.py"), ("Notification", "attention.py")}
        actual = []
        for event, groups in json.loads(self.settings.read_text())["hooks"].items():
            for group in groups:
                for hook in group["hooks"]:
                    for _, name in expected:
                        if name in hook["command"] and (event, name) not in actual:
                            matches = [h for g in groups for h in g["hooks"] if name in h["command"]]
                            self.assertEqual(len(matches), 1, (event, name))
                            self.assertIn(str(checkout / "hooks" / name), matches[0]["command"])
                            actual.append((event, name))
        self.assertEqual(set(actual), expected)

    def test_hooks_follow_a_renamed_checkout_without_duplicates(self):
        first = self.checkout("clone A")
        self.install("--hooks", "--attention", "--experiment", checkout=first)
        second = self.tmp / "clone B"
        first.rename(second)
        for flag in ("--hooks", "--attention", "--experiment"):
            self.install(flag, checkout=second)
            self.assert_hooks_from(second)
        state = json.loads((self.settings.parent / ".claude-orchestrator-install.json").read_text())
        self.assertEqual(state["checkouts"], [str(first), str(second)])

    def test_hooks_follow_the_latest_of_two_existing_clones(self):
        first, second = self.checkout("clone A"), self.checkout("clone B")
        for checkout in (first, second, first):
            self.install("--hooks", "--attention", "--experiment", checkout=checkout)
            self.assert_hooks_from(checkout)
        self.assertTrue(first.is_dir())
        self.assertTrue(second.is_dir())
        state = self.tree_state(self.settings.parent)
        self.install("--hooks", "--attention", "--experiment", checkout=first)
        self.assertEqual(self.tree_state(self.settings.parent), state)

    def test_uninstall_from_another_checkout_removes_all_earlier_owned_paths(self):
        for scenario in ("exists", "moved", "deleted"):
            with self.subTest(scenario=scenario):
                first = self.checkout(f"{scenario} A")
                config, home = self.tmp / f"{scenario} config", self.tmp / f"{scenario} home"
                config.mkdir()
                settings, md = config / "settings.json", config / "CLAUDE.md"
                mine = {"hooks": [{"type": "command", "command": "echo my stop", "timeout": 8}]}
                foreign = '@elsewhere/rules/orchestrator.md'
                settings.write_text(json.dumps({"before": True, "hooks": {"Stop": [mine]}, "after": [1]}))
                md.write_bytes(f"# mine\r\n{foreign}\r\nkeep this\r\n".encode())
                env = {"HOME": str(home), "CLAUDE_CONFIG_DIR": str(config)}
                self.install("--hooks", "--attention", "--rules", "--bin", checkout=first, env=env)
                data = json.loads(settings.read_text())
                data["hooks"]["SessionStart"] = [{"matcher": "stale", "hooks": [{
                    "type": "command", "command": f'python3 "{first}/hooks/session-mode.py"', "timeout": 99}]}]
                settings.write_text(json.dumps(data))
                second = self.tmp / f"{scenario} B"
                if scenario == "moved":
                    first.rename(second)
                else:
                    second = self.checkout(second.name)
                    if scenario == "deleted":
                        shutil.rmtree(first)
                self.install("--uninstall", checkout=second, env=env)
                self.assertEqual(json.loads(settings.read_text()), {
                    "before": True, "hooks": {"Stop": [mine]}, "after": [1]})
                self.assertEqual(md.read_bytes(), f"# mine\r\n{foreign}\r\nkeep this\r\n".encode())
                self.assertFalse(any((config / "skills").iterdir()))
                self.assertFalse(any((config / "commands").iterdir()))
                self.assertFalse((home / ".local/bin/delegate").is_symlink())
                self.assertFalse((config / ".claude-orchestrator-install.json").exists())
                state = self.tree_state(config)
                self.install("--uninstall", checkout=second, env=env)
                self.assertEqual(self.tree_state(config), state)

    def test_foreign_legacy_hook_survives_install_and_uninstall(self):
        self.settings.parent.mkdir(parents=True)
        foreign = {"matcher": "mine", "hooks": [{"type": "command", "timeout": 99,
                   "command": 'python3 "/elsewhere/hooks/context-guard.py"'}]}
        own_mode = {"hooks": [{"type": "command", "command": 'python3 "/elsewhere/hooks/session-mode.py"'}]}
        self.settings.write_text(json.dumps({"hooks": {"UserPromptSubmit": [foreign], "SessionStart": [own_mode]}}))
        self.install("--hooks", "--rules")
        self.assertIn(foreign, json.loads(self.settings.read_text())["hooks"]["UserPromptSubmit"])
        self.assertIn(RULES_LINE, self.user_md.read_text())
        self.assertIn("session-mode hook not registered", self.install("--check"))
        self.install("--uninstall")
        self.assertEqual(json.loads(self.settings.read_text()), {
            "hooks": {"UserPromptSubmit": [foreign], "SessionStart": [own_mode]}})

    def test_rules_replace_an_earlier_checkout_import_in_place(self):
        first, second = self.checkout("rules A"), self.checkout("rules B")
        self.user_md.parent.mkdir(parents=True)
        self.user_md.write_bytes(b"# mine\r\n@/elsewhere/rules/orchestrator.md\r\n")
        self.install("--rules", checkout=first)
        self.install("--rules", checkout=second)
        text = self.user_md.read_bytes()
        self.assertEqual(text, b"# mine\r\n@/elsewhere/rules/orchestrator.md\r\n\n" +
                         f"@{second}/rules/orchestrator.md\n".encode())
        state = self.tree_state(self.settings.parent)
        self.install("--rules", checkout=second)
        self.assertEqual(self.tree_state(self.settings.parent), state)
        self.install("--experiment", checkout=second)
        self.assertEqual(self.user_md.read_bytes(), b"# mine\r\n@/elsewhere/rules/orchestrator.md\r\n")

    def test_session_mode_at_an_earlier_recorded_path_prevents_rules_import(self):
        first, second = self.checkout("session A"), self.checkout("session B")
        self.install("--experiment", checkout=first)
        data = json.loads(self.settings.read_text())
        data["hooks"]["SessionStart"][0]["hooks"][0]["command"] = f'python3 "{first}/hooks/session-mode.py"'
        self.settings.write_text(json.dumps(data))
        self.assertIn("rules not imported", self.install("--rules", checkout=second))
        self.assertFalse(self.user_md.exists())
        self.user_md.write_bytes(f"# mine\r\n\r\n@{first}/rules/orchestrator.md\r\n".encode())
        self.install("--experiment", checkout=second)
        self.assertEqual(self.user_md.read_bytes(), b"# mine\r\n")

    def test_guarded_hooks_prove_an_earlier_path_without_an_ownership_record(self):
        first, second = self.checkout("old A"), self.checkout("new B")
        self.install("--hooks", "--attention", "--experiment", checkout=first)
        (self.settings.parent / ".claude-orchestrator-install.json").unlink()
        shutil.rmtree(first)
        self.install("--hooks", "--attention", "--experiment", checkout=second)
        self.assert_hooks_from(second)
        state = json.loads((self.settings.parent / ".claude-orchestrator-install.json").read_text())
        self.assertIn(str(first), state["checkouts"])

    def test_uninstall_upgrades_guarded_hook_provenance_without_a_record(self):
        first, second = self.checkout("old A"), self.checkout("new B")
        self.install("--hooks", "--attention", "--rules", "--bin", checkout=first)
        (self.settings.parent / ".claude-orchestrator-install.json").unlink()
        shutil.rmtree(first)
        self.install("--uninstall", checkout=second)
        self.assertEqual(json.loads(self.settings.read_text()), {"hooks": {}})
        self.assertEqual(self.user_md.read_bytes(), b"")
        self.assertFalse(any((self.tmp / "claude/skills").iterdir()))
        self.assertFalse(any((self.tmp / "claude/commands").iterdir()))
        self.assertFalse((self.tmp / "home/.local/bin/delegate").is_symlink())

    def test_uninstall_after_an_update_removes_a_pre_record_install(self):
        # 0.3.0 and older wrote unguarded hooks and no ownership record; a git pull
        # followed by --uninstall from the same checkout must still remove its links.
        checkout = self.checkout("pre-record")
        self.install("--hooks", "--attention", "--bin", checkout=checkout)
        data = json.loads(self.settings.read_text())
        for groups in data["hooks"].values():
            for group in groups:
                for hook in group["hooks"]:
                    hook["command"] = hook["command"].split(" || ", 1)[1]
        self.settings.write_text(json.dumps(data))
        (self.settings.parent / ".claude-orchestrator-install.json").unlink()
        mine = self.tmp / "claude/skills/mine"
        mine.symlink_to(checkout / "skills/delegate")
        self.install("--uninstall", checkout=checkout)
        self.assertEqual(json.loads(self.settings.read_text()), {"hooks": {}})
        self.assertEqual([p.name for p in (self.tmp / "claude/skills").iterdir()], ["mine"])
        self.assertFalse(any((self.tmp / "claude/commands").iterdir()))
        self.assertFalse((self.tmp / "home/.local/bin/delegate").is_symlink())

    def test_uninstall_whitespace_only_settings_is_a_noop(self):
        self.settings.parent.mkdir(parents=True)
        self.settings.write_bytes(b" \n\t\r\n")
        state = self.tree_state(self.tmp)
        self.install("--uninstall")
        self.assertEqual(self.tree_state(self.tmp), state)

    def test_uninstall_drops_only_event_keys_it_emptied(self):
        self.settings.parent.mkdir(parents=True)
        mine = {"hooks": [{"type": "command", "command": "echo mine"}]}
        originally_empty_group = {"hooks": []}
        self.settings.write_text(json.dumps({"before": 1, "hooks": {
            "AlreadyEmpty": [],
            "UserPromptSubmit": [{"hooks": [{"type": "command", "command": GUARD}]}],
            "Stop": [mine, {"hooks": [{"type": "command", "command": ATTENTION}]}],
            "SessionStart": [originally_empty_group]}, "after": 2}))
        self.install("--uninstall")
        expected = {"before": 1, "hooks": {"AlreadyEmpty": [], "Stop": [mine],
                    "SessionStart": [originally_empty_group]}, "after": 2}
        self.assertEqual(self.settings.read_text(), json.dumps(expected, indent=2) + "\n")
        state = self.tree_state(self.settings.parent)
        self.install("--uninstall")
        self.assertEqual(self.tree_state(self.settings.parent), state)

    def test_uninstall_retains_provenance_until_earlier_git_links_are_removed(self):
        first, second = self.checkout("git A"), self.checkout("git B")
        project = self.tmp / "project"
        project.mkdir()
        subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True)
        self.install("--hooks", f"--git-hook={project}", checkout=first)
        shutil.rmtree(first)
        self.install("--uninstall", checkout=second)
        state = self.settings.parent / ".claude-orchestrator-install.json"
        self.assertTrue(state.exists())
        for name in ("pre-commit", "pre-push"):
            self.assertTrue((project / ".git/hooks" / name).is_symlink())
        self.install("--uninstall", f"--git-hook={project}", checkout=second)
        for name in ("pre-commit", "pre-push"):
            self.assertFalse((project / ".git/hooks" / name).is_symlink())
        self.assertFalse(state.exists())
        snapshot = self.tree_state(self.tmp)
        self.install("--uninstall", f"--git-hook={project}", checkout=second)
        self.assertEqual(self.tree_state(self.tmp), snapshot)

    def test_uninstall_preserves_user_replacements_of_recorded_links(self):
        first, second = self.checkout("links A"), self.checkout("links B")
        self.install("--bin", checkout=first)
        foreign = self.tmp / "my delegate"
        foreign.mkdir()
        skill = self.tmp / "claude/skills/delegate"
        skill.unlink()
        skill.symlink_to(foreign)
        binary = self.tmp / "home/.local/bin/delegate"
        binary.unlink()
        binary.symlink_to(foreign / "my command")
        command = self.tmp / "claude/commands/next.md"
        command.unlink()
        command.write_text("my replacement command\n")
        self.install("--uninstall", checkout=second)
        self.assertEqual(os.readlink(skill), str(foreign))
        self.assertEqual(os.readlink(binary), str(foreign / "my command"))
        self.assertEqual(command.read_text(), "my replacement command\n")
        self.assertFalse((self.settings.parent / ".claude-orchestrator-install.json").exists())

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

    def test_check_names_the_aa_key_source_without_the_key(self):
        key_file = self.tmp / "config/claude-orchestrator/aa-api-key"
        out = self.install("--check")
        self.assertIn(f"no AA API key in ARTIFICIAL_ANALYSIS_API_KEY or {key_file}", out)
        self.assertNotIn("leaderboard", out)
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_text(" \n")
        self.assertIn("no AA API key", self.install("--check"))
        key_file.write_text("fictional-file-key\n")
        out = self.install("--check")
        self.assertIn(f"AA API key in {key_file}", out)
        self.assertNotIn("fictional-file-key", out)
        out = self.install("--check", env={"ARTIFICIAL_ANALYSIS_API_KEY": "fictional-env-key"})
        self.assertIn("AA API key in ARTIFICIAL_ANALYSIS_API_KEY", out)
        self.assertNotIn("fictional-env-key", out)

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

    def assert_backed_up(self, target, output):
        backups = list((self.settings.parent / "skill-backups").glob(f"{target.name}-*"))
        self.assertEqual(len(backups), 1, output)
        self.assertTrue(any("warn" in line and str(backups[0]) in line
                            for line in output.splitlines()), output)
        return backups[0]

    def test_install_backs_up_foreign_skill_and_command_links(self):
        skill = self.settings.parent / "skills/delegate"
        command = self.settings.parent / "commands/next.md"
        foreign_skill = self.tmp / "my skill"
        foreign_skill.mkdir()
        (foreign_skill / "SKILL.md").write_text("my skill\n")
        foreign_command = self.tmp / "my command"
        foreign_command.write_text("my command\n")
        for target, old in ((skill, foreign_skill), (command, foreign_command)):
            target.parent.mkdir(parents=True)
            target.symlink_to(old)

        output = self.install()
        self.assertEqual((foreign_skill / "SKILL.md").read_text(), "my skill\n")
        self.assertEqual(foreign_command.read_text(), "my command\n")
        for target, old in ((skill, foreign_skill), (command, foreign_command)):
            with self.subTest(target=target):
                backup = self.assert_backed_up(target, output)
                self.assertEqual(os.readlink(backup), str(old))
                self.assertEqual(target.resolve(), REPO / target.relative_to(self.settings.parent))

    def test_install_backs_up_dangling_and_looping_links(self):
        skill = self.settings.parent / "skills/delegate"
        command = self.settings.parent / "commands/next.md"
        for target, old in ((skill, "../missing"), (command, command.name)):
            target.parent.mkdir(parents=True)
            target.symlink_to(old)

        output = self.install()
        for target, old in ((skill, "../missing"), (command, command.name)):
            with self.subTest(target=target):
                backup = self.assert_backed_up(target, output)
                self.assertEqual(os.readlink(backup), old)
                self.assertEqual(target.resolve(), REPO / target.relative_to(self.settings.parent))

    def test_install_bin_backs_up_regular_file_foreign_link_and_directory(self):
        foreign = self.tmp / "other-delegate"
        foreign.write_text("other binary\n")
        for kind in ("file", "link", "directory"):
            with self.subTest(kind=kind):
                home = self.tmp / f"home-{kind}"
                binary = home / ".local/bin/delegate"
                binary.parent.mkdir(parents=True)
                if kind == "file":
                    binary.write_text("my script\n")
                elif kind == "link":
                    binary.symlink_to(foreign)
                else:
                    binary.mkdir()
                    (binary / "my-file").write_text("my file\n")

                output = self.install("--bin", env={"HOME": str(home)})
                backup = self.assert_backed_up(binary, output)
                self.assertTrue(binary.is_symlink())
                self.assertEqual(binary.resolve(), REPO / "skills/delegate/scripts/delegate")
                if kind == "file":
                    self.assertEqual(backup.read_text(), "my script\n")
                    backup.unlink()
                elif kind == "link":
                    self.assertEqual(os.readlink(backup), str(foreign))
                    self.assertEqual(foreign.read_text(), "other binary\n")
                    backup.unlink()
                else:
                    self.assertEqual((backup / "my-file").read_text(), "my file\n")

    def test_install_replaces_recorded_links_with_notice_and_no_backup(self):
        checkout = self.checkout("older checkout")
        self.install("--bin", checkout=checkout)
        config = self.settings.parent
        output = self.install("--bin")
        for relative in ("skills/delegate", "skills/field-report", "commands/next.md"):
            with self.subTest(relative=relative):
                notices = [line for line in output.splitlines() if str(checkout / relative) in line]
                self.assertEqual(len(notices), 1, output)
                self.assertEqual((config / relative).resolve(), REPO / relative)
        self.assertFalse((config / "skill-backups").exists())

    def test_install_bin_replaces_recorded_link_without_backup(self):
        config = self.settings.parent
        self.install("--bin")
        binary = self.tmp / "home/.local/bin/delegate"
        old = self.tmp / "older checkout/skills/delegate/scripts/delegate"
        binary.unlink()
        binary.symlink_to(old)
        receipt = config / ".claude-orchestrator-install.json"
        data = json.loads(receipt.read_text())
        data["links"][str(binary)] = str(old)
        receipt.write_text(json.dumps(data, indent=2) + "\n")

        output = self.install("--bin")
        self.assertEqual(sum(str(old) in line for line in output.splitlines()), 1, output)
        self.assertEqual(binary.resolve(), REPO / "skills/delegate/scripts/delegate")
        self.assertFalse((config / "skill-backups").exists())

    def test_install_link_rerun_keeps_links_and_receipt_unchanged(self):
        self.install("--bin")
        state = self.tree_state(self.tmp)
        output = self.install("--bin")
        self.assertNotIn("warn", output[output.index("skills ("):])
        self.assertFalse((self.settings.parent / "skill-backups").exists())
        self.assertEqual(self.tree_state(self.tmp), state)

    def test_copy_rerun_changes_nothing_and_keeps_manifest_outside_loaded_text(self):
        self.install("--copy")
        config = self.settings.parent
        manifest = config / ".claude-orchestrator-copies.json"
        self.assertTrue(manifest.is_file())
        self.assertEqual(set(json.loads(manifest.read_text())),
                         {"skills/delegate", "skills/field-report", "commands/next.md"})
        state = self.tree_state(config)
        output = self.install("--copy")
        self.assertNotIn("--force", output)
        self.assertEqual(self.tree_state(config), state)
        self.assertFalse((config / "skill-backups").exists())
        for kind in ("skills", "commands"):
            self.assertEqual({str(p.relative_to(config / kind)) for p in (config / kind).rglob("*")},
                             {str(p.relative_to(REPO / kind)) for p in (REPO / kind).rglob("*")
                              if "__pycache__" not in p.parts})

    def test_copy_refreshes_upstream_skills_and_commands_and_ignores_runtime_caches(self):
        checkout = self.checkout()
        self.install("--copy", checkout=checkout)
        delegate = checkout / "skills/delegate"
        command = checkout / "commands/next.md"
        (delegate / "SKILL.md").write_text("upstream skill update\n")
        (delegate / "new empty directory").mkdir()
        (delegate / "reference/commands.md").unlink()
        command.write_text("upstream command update\n")
        cache = self.tmp / "claude/skills/delegate/scripts/__pycache__"
        cache.mkdir()
        (cache / "cache.pyc").write_bytes(b"runtime cache")
        self.assertNotIn("--force", self.install("--copy", checkout=checkout))
        target = self.tmp / "claude/skills/delegate"
        self.assertEqual((target / "SKILL.md").read_text(), "upstream skill update\n")
        self.assertTrue((target / "new empty directory").is_dir())
        self.assertFalse((target / "reference/commands.md").exists())
        self.assertFalse((target / "scripts/__pycache__").exists())
        self.assertEqual((self.tmp / "claude/commands/next.md").read_bytes(), command.read_bytes())
        self.assertFalse((self.tmp / "claude/skill-backups").exists())
        state = self.tree_state(self.settings.parent)
        self.install("--copy", checkout=checkout)
        self.assertEqual(self.tree_state(self.settings.parent), state)

    def test_refreshing_an_unedited_copy_does_not_add_a_backup(self):
        checkout = self.checkout()
        target = self.tmp / "claude/skills/delegate"
        target.mkdir(parents=True)
        (target / "SKILL.md").write_text("my preexisting skill\n")
        self.install("--copy", "--force", checkout=checkout)
        backups = self.tmp / "claude/skill-backups"
        state = self.tree_state(backups)
        stamp = backups.stat().st_mtime_ns
        (checkout / "skills/delegate/SKILL.md").write_text("new upstream skill\n")
        (checkout / "commands/next.md").write_text("new upstream command\n")
        self.install("--copy", checkout=checkout)
        self.assertEqual((target / "SKILL.md").read_text(), "new upstream skill\n")
        self.assertEqual((self.tmp / "claude/commands/next.md").read_text(), "new upstream command\n")
        self.assertEqual(self.tree_state(backups), state)
        self.assertEqual(backups.stat().st_mtime_ns, stamp)

    def test_copy_preserves_user_edits_until_force_and_backs_them_up(self):
        checkout = self.checkout()
        self.install("--copy", checkout=checkout)
        skill = self.tmp / "claude/skills/delegate/SKILL.md"
        command = self.tmp / "claude/commands/next.md"
        skill.write_text("my edited skill\n")
        command.write_text("my edited command\n")
        (checkout / "skills/delegate/SKILL.md").write_text("new upstream skill\n")
        (checkout / "commands/next.md").write_text("new upstream command\n")
        state = self.tree_state(self.settings.parent)
        self.assertIn("--force", self.install("--copy", checkout=checkout, code=1))
        self.assertEqual(self.tree_state(self.settings.parent), state)
        self.install("--copy", "--force", checkout=checkout)
        backups = self.tmp / "claude/skill-backups"
        self.assertEqual(next(backups.glob("delegate-*/SKILL.md")).read_text(), "my edited skill\n")
        self.assertEqual(next(backups.glob("next.md-*")).read_text(), "my edited command\n")
        self.assertEqual(skill.read_text(), "new upstream skill\n")
        self.assertEqual(command.read_text(), "new upstream command\n")
        state = self.tree_state(self.settings.parent)
        self.install("--copy", checkout=checkout)
        self.assertEqual(self.tree_state(self.settings.parent), state)

    def test_copy_does_not_adopt_identical_unrecorded_files_without_force(self):
        skills = self.tmp / "claude/skills"
        commands = self.tmp / "claude/commands"
        commands.mkdir(parents=True)
        shutil.copytree(REPO / "skills", skills, ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(REPO / "commands/next.md", commands / "next.md")
        state = self.tree_state(self.settings.parent)
        self.assertIn("--force", self.install("--copy", code=1))
        self.assertEqual(self.tree_state(self.settings.parent), state)
        self.install("--uninstall")
        self.assertEqual(self.tree_state(self.settings.parent), state)
        self.install("--copy", "--force")
        self.assertEqual(len(list((self.tmp / "claude/skill-backups").iterdir())), 3)
        self.install("--copy")

    def test_copy_detects_added_files_empty_directories_modes_and_symlink_edits(self):
        checkout = self.checkout()
        extra = checkout / "skills/delegate/test-link"
        extra.symlink_to("SKILL.md")
        self.install("--copy", checkout=checkout)
        target = self.tmp / "claude/skills/delegate"
        mutations = [lambda: (target / "user-added").write_text("mine"),
                     lambda: (target / "user empty directory").mkdir(),
                     lambda: (target / "SKILL.md").chmod(0o600),
                     lambda: (target / "test-link").unlink()]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                mutate()
                state = self.tree_state(self.settings.parent)
                self.install("--copy", checkout=checkout, code=1)
                self.assertEqual(self.tree_state(self.settings.parent), state)
                self.install("--copy", "--force", checkout=checkout)

    def test_uninstall_hooks_rules_links_bin_and_timers_preserves_other_data(self):
        self.settings.parent.mkdir(parents=True)
        mine = {"type": "command", "command": "echo user-stop", "timeout": 4}
        self.settings.write_text(json.dumps({"model": "mine", "hooks": {
            "Stop": [{"matcher": "mine", "extra": 1, "hooks": [mine]}]}, "after": {"keep": True}}))
        self.user_md.write_bytes(b"# mine\r\nkeep this line\r\n")
        self.install("--hooks", "--attention", "--rules", "--bin", "--timer")
        self.install("--experiment")
        # Restore an import in the middle: uninstall reuses the CRLF-safe removal.
        self.user_md.write_bytes(f"# mine\r\n\r\n {RULES_LINE} \r\nkeep this line\r\n".encode())
        settings = json.loads(self.settings.read_text())
        # Legacy registrations at arbitrary events/matchers must also be removed.
        untouched = [{"hooks": None}, {"hooks": []}, None, {"hooks": [None, {"command": []}]}]
        settings["hooks"]["CustomEvent"] = untouched + [{"matcher": "anything", "extra": True, "hooks": [
            *({"type": "command", "command": f'python3 "{REPO}/hooks/{name}"', "timeout": 99}
              for name in ("context-guard.py", "secret-guard.py", "session-mode.py", "attention.py")), mine]}]
        self.settings.write_text(json.dumps(settings))
        before_settings = self.settings.read_bytes()
        before_md = self.user_md.read_bytes()
        expected = {**settings, "hooks": {}}
        expected["hooks"]["Stop"] = [{"matcher": "mine", "extra": 1, "hooks": [mine]}]
        expected["hooks"]["CustomEvent"] = untouched + [
            {"matcher": "anything", "extra": True, "hooks": [mine]}]
        foreign = self.tmp / "foreign skill"
        foreign.mkdir()
        foreign_link = self.tmp / "claude/skills/foreign"
        foreign_link.symlink_to(foreign)
        extra_link = self.tmp / "claude/skills/renamed-skill"
        extra_link.symlink_to(REPO / "skills/delegate")
        foreign_command = self.tmp / "claude/commands/mine.md"
        foreign_command.write_text("keep my command\n")
        settings_backups = set(self.settings.parent.glob("settings.json.bak-*"))
        md_backups = set(self.settings.parent.glob("CLAUDE.md.bak-*"))
        output = self.install("--uninstall")
        self.assertIn("./install.sh --uninstall --git-hook=DIR", output)
        self.assertEqual(json.loads(self.settings.read_text()), expected)
        self.assertEqual(list(json.loads(self.settings.read_text())), list(settings))
        self.assertEqual(self.settings.read_text(), json.dumps(expected, indent=2) + "\n")
        self.assertEqual(self.user_md.read_bytes(), b"# mine\r\n\r\nkeep this line\r\n")
        added_settings = set(self.settings.parent.glob("settings.json.bak-*")) - settings_backups
        added_md = set(self.settings.parent.glob("CLAUDE.md.bak-*")) - md_backups
        self.assertEqual(len(added_settings), 1)
        self.assertEqual(len(added_md), 1)
        self.assertEqual(added_settings.pop().read_bytes(), before_settings)
        self.assertEqual(added_md.pop().read_bytes(), before_md)
        self.assertEqual(set((self.tmp / "claude/skills").iterdir()), {foreign_link, extra_link})
        self.assertTrue(foreign_link.is_symlink())
        self.assertTrue(extra_link.is_symlink())  # A user-created link was never recorded.
        self.assertEqual(list((self.tmp / "claude/commands").iterdir()), [foreign_command])
        self.assertFalse((self.tmp / "home/.local/bin/delegate").is_symlink())
        for unit in (self.service, self.timer, self.rate_service, self.rate_timer):
            self.assertFalse(unit.exists())
        self.assertEqual(self.calls.read_text().splitlines()[-3:], [
            "--user disable --now claude-delegate-quota.timer claude-delegate-quota.service",
            "--user disable --now claude-delegate-rate.timer claude-delegate-rate.service",
            "--user daemon-reload"])
        state = self.tree_state(self.tmp)
        self.install("--uninstall")
        self.assertEqual(self.tree_state(self.tmp), state)

    def test_uninstall_preserves_copied_skills_commands_and_user_edits_in_backups(self):
        checkout = self.checkout()
        self.install("--copy", "--bin", checkout=checkout)
        delegate = self.tmp / "claude/skills/delegate"
        (delegate / "SKILL.md").write_text("my edited copy\n")
        command = self.tmp / "claude/commands/next.md"
        command.write_text("my edited command\n")
        # Ownership survives removal of the upstream item and moving the clone.
        shutil.rmtree(checkout / "skills/field-report")
        checkout.rename(self.tmp / "moved")
        checkout = self.tmp / "moved"
        self.install("--uninstall", checkout=checkout)
        backups = self.tmp / "claude/skill-backups"
        self.assertEqual(len(list(backups.iterdir())), 3)
        self.assertEqual(next(backups.glob("delegate-*/SKILL.md")).read_text(), "my edited copy\n")
        self.assertEqual(next(backups.glob("next.md-*")).read_text(), "my edited command\n")
        self.assertTrue(next(backups.glob("field-report-*/SKILL.md")).is_file())
        self.assertFalse(delegate.exists())
        self.assertFalse(command.exists())
        self.assertFalse((self.tmp / "claude/.claude-orchestrator-copies.json").exists())
        self.assertFalse((self.tmp / "home/.local/bin/delegate").is_symlink())
        state = self.tree_state(self.tmp)
        self.install("--uninstall", checkout=checkout)
        self.assertEqual(self.tree_state(self.tmp), state)

    def test_uninstall_leaves_foreign_bin_file_or_link_including_symlink_loops(self):
        binary = self.tmp / "home/.local/bin/delegate"
        binary.parent.mkdir(parents=True)
        binary.write_text("my binary\n")
        foreign = self.tmp / "other-delegate"
        foreign.write_text("other binary\n")
        self.install()
        self.install("--uninstall")
        self.assertEqual(binary.read_text(), "my binary\n")
        binary.unlink()
        binary.symlink_to(foreign)
        self.install("--uninstall")
        self.assertTrue(binary.is_symlink())
        self.assertEqual(binary.resolve(), foreign)
        binary.unlink()
        binary.symlink_to(binary.name)
        self.install("--uninstall")
        self.assertTrue(binary.is_symlink())
        self.assertEqual(os.readlink(binary), binary.name)

    def test_uninstall_git_hooks_only_removes_our_links_and_respects_hooks_path(self):
        project = self.tmp / "project"
        project.mkdir()
        subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True)
        subprocess.run(["git", "config", "core.hooksPath", ".githooks"], cwd=project,
                       check=True, capture_output=True)
        self.install(f"--git-hook={project}")
        hooks = project / ".githooks"
        # Plain uninstall deliberately leaves per-repository hooks alone.
        self.install("--uninstall")
        self.assertTrue((hooks / "pre-commit").is_symlink())
        (hooks / "pre-commit").unlink()
        (hooks / "pre-commit").write_text("my hook\n")
        self.install("--uninstall", f"--git-hook={project}")
        self.assertFalse((hooks / "pre-push").is_symlink())
        self.assertEqual((hooks / "pre-commit").read_text(), "my hook\n")
        (hooks / "pre-commit").unlink()
        foreign = self.tmp / "foreign-hook"
        foreign.write_text("other hook\n")
        (hooks / "pre-push").symlink_to(foreign)
        (hooks / "pre-commit").symlink_to(REPO / "hooks/secret-guard.py")
        self.install("--uninstall", f"--git-hook={project}")
        self.assertTrue((hooks / "pre-push").is_symlink())
        self.assertFalse((hooks / "pre-commit").is_symlink())
        state = self.tree_state(self.tmp)
        self.install("--uninstall", f"--git-hook={project}")
        self.assertEqual(self.tree_state(self.tmp), state)

    def test_uninstall_timer_failure_warns_and_still_removes_all_units(self):
        self.install("--timer")
        self.exe("systemctl", '#!/bin/sh\nprintf "%s\\n" "$*" >> "$FAKE_SYSTEMCTL_CALLS"\nexit 17\n')
        output = self.install("--uninstall")
        self.assertIn("warn", output)
        self.assertIn("could not disable/stop", output)
        self.assertIn("daemon-reload failed", output)
        self.assertFalse(any(self.units.iterdir()))
        state = self.tree_state(self.tmp)
        self.install("--uninstall")
        self.assertEqual(self.tree_state(self.tmp), state)

    def test_uninstall_without_systemctl_removes_units_with_warning(self):
        self.install("--timer")
        env = self.private_path_without("systemctl", "cursor-agent", "codex", "claude")
        (self.bin / "rm").symlink_to(shutil.which("rm"))
        output = self.install("--uninstall", env=env)
        self.assertIn("systemctl not found", output)
        self.assertFalse(any(self.units.iterdir()))

    def test_uninstall_empty_installation_is_noop_and_help_documents_it(self):
        state = self.tree_state(self.tmp)
        self.assertIn("--uninstall --git-hook=DIR", self.install("--uninstall"))
        self.assertEqual(self.tree_state(self.tmp), state)
        help_text = self.install("--help")
        self.assertIn("--uninstall", help_text)
        self.assertIn("copied skills/commands are moved", help_text)
        self.assertIn("add --git-hook=DIR", help_text)

    def test_install_timer_systemctl_failure_warns_and_continues(self):
        self.exe("systemctl", '#!/bin/sh\nprintf "%s\\n" "$*" >> "$FAKE_SYSTEMCTL_CALLS"\nexit 1\n')
        project = self.tmp / "project"
        subprocess.run(["git", "init", str(project)], check=True, capture_output=True)
        output = self.install("--timer", f"--git-hook={project}")
        warnings = [line for line in output.splitlines() if "warn" in line]
        self.assertTrue(any("systemctl --user daemon-reload" in line for line in warnings))
        for unit in (self.timer, self.rate_timer):
            self.assertTrue(unit.is_file())
            command = f"systemctl --user enable --now {unit.name}"
            self.assertTrue(any(command in line for line in warnings), output)
        self.assertEqual(self.calls.read_text().splitlines(), [
            "--user daemon-reload", f"--user enable --now {self.timer.name}",
            f"--user enable --now {self.rate_timer.name}"])
        self.assertEqual((project / ".git/hooks/pre-commit").resolve(),
                         REPO / "hooks/secret-guard.py")
        self.assertIn("done. Claude Code", output)

    def test_install_timer_rate_enable_failure_leaves_quota_enabled(self):
        self.exe("systemctl", '#!/bin/sh\n'
                 '[ "$2" != enable ] || [ "$4" != claude-delegate-rate.timer ]\n')
        output = self.install("--timer")
        self.assertIn("quota timer enabled", output)
        self.assertNotIn("rate timer enabled", output)
        self.assertTrue(any("warn" in line and f"systemctl --user enable --now {self.rate_timer.name}" in line
                            for line in output.splitlines()), output)

    def test_timer_units_commands_and_reinstallation_are_idempotent(self):
        output = self.install("--timer")
        service, timer = self.service.read_text(), self.timer.read_text()
        rate_service, rate_timer = self.rate_service.read_bytes(), self.rate_timer.read_bytes()
        self.assertEqual({p.name for p in self.units.iterdir()}, {
            "claude-delegate-quota.service", "claude-delegate-quota.timer",
            "claude-delegate-rate.service", "claude-delegate-rate.timer"})
        for kind in ("quota", "rate"):
            self.assertIn(f"{kind} timer units written to {self.units}", output)
            self.assertIn(f"{kind} timer enabled", output)
        self.assertIn("[Service]\nType=oneshot\nNice=10\n", service)
        exec_line = next(line for line in service.splitlines() if line.startswith("ExecStart="))
        exec_args = shlex.split(exec_line.split("=", 1)[1])
        expected_python = subprocess.run(
            ["python3", "-c", "import os, sys; print(os.path.abspath(sys.executable))"],
            env=self.env, text=True, capture_output=True, check=True).stdout.strip()
        self.assertEqual(exec_args, [expected_python,
                                    str(self.tmp / "claude/skills/delegate/scripts/delegate"),
                                    "quota", "--snapshot", "--if-active"])
        rate = self.unit(self.rate_service)["Service"]
        self.assertEqual(shlex.split(rate["ExecStart"]), [expected_python,
                         str(self.tmp / "claude/skills/delegate/scripts/delegate"), "rate"])
        self.assertEqual(rate["StandardOutput"], "null")
        self.assertEqual(rate["StandardError"], "journal")
        environment = next(line for line in service.splitlines() if line.startswith("Environment="))
        self.assertEqual(shlex.split(environment.split("=", 1)[1]),
                         [f"PATH={self.bin}:/usr/local/bin:/usr/bin:/bin",
                          f"CLAUDE_CONFIG_DIR={self.tmp / 'claude'}"])
        self.assertEqual(rate["Environment"], environment.split("=", 1)[1])
        # The original quota units must remain byte-identical, including whitespace.
        self.assertEqual(service,
                         "[Unit]\nDescription=Claude delegate quota snapshot\n\n"
                         "[Service]\nType=oneshot\nNice=10\n"
                         f'ExecStart="{expected_python}" "{exec_args[1]}" quota --snapshot --if-active\n'
                         f'Environment="PATH={self.bin}:/usr/local/bin:/usr/bin:/bin" '
                         f'"CLAUDE_CONFIG_DIR={self.tmp / "claude"}"\n')
        self.assertEqual(timer,
                         "[Unit]\nDescription=Poll quota while Claude Code is active\n\n"
                         "[Timer]\nOnCalendar=*:0/10\nAccuracySec=1min\nPersistent=false\n\n"
                         "[Install]\nWantedBy=timers.target\n")
        for line in ("OnCalendar=*:0/10", "AccuracySec=1min", "Persistent=false",
                     "[Install]", "WantedBy=timers.target"):
            self.assertIn(line + "\n", timer)
        calls = ["--user daemon-reload", "--user enable --now claude-delegate-quota.timer",
                 "--user enable --now claude-delegate-rate.timer"]
        self.assertEqual(self.calls.read_text().splitlines(), calls)
        self.install("--timer")
        self.assertEqual(self.service.read_text(), service)
        self.assertEqual(self.timer.read_text(), timer)
        self.assertEqual(self.rate_service.read_bytes(), rate_service)
        self.assertEqual(self.rate_timer.read_bytes(), rate_timer)
        self.assertEqual(self.calls.read_text().splitlines(), calls * 2)

    def test_rate_timer_allows_seven_hour_catchup_without_overlap_timeout_or_wake_backlog(self):
        # A first run over 462 sessions can take 4–7 hours. A oneshot stays activating
        # until it exits; defer overdue calendar fires so completion doesn't replay one.
        self.install("--timer")
        service = self.unit(self.rate_service)
        self.assertEqual(service["Service"]["Type"], "oneshot")
        self.assertEqual(service["Service"]["TimeoutStartSec"], "infinity")
        self.assertNotIn("RuntimeMaxSec", service["Service"])
        self.assertNotIn("TimeoutSec", service["Service"])
        self.assertNotIn("JobTimeoutSec", service["Unit"])
        self.assertNotIn("JobRunningTimeoutSec", service["Unit"])
        self.assertEqual(service["Service"].get("Restart", "no"), "no")
        self.assertEqual(service["Service"].get("RemainAfterExit", "no"), "no")
        timer = self.unit(self.rate_timer)
        self.assertEqual(timer["Timer"]["OnCalendar"], "hourly")
        self.assertEqual(timer["Timer"]["Persistent"], "false")
        self.assertEqual(timer["Timer"]["DeferReactivation"], "true")
        self.assertEqual(timer["Timer"].get("Unit", self.rate_timer.with_suffix(".service").name),
                         self.rate_service.name)
        self.assertEqual(timer["Install"]["WantedBy"], "timers.target")

    def test_rate_and_quota_timers_find_cursor_agent_in_a_different_folder(self):
        agent_dir = self.tmp / "cursor bin"
        agent_dir.mkdir()
        (self.bin / "cursor-agent").rename(agent_dir / "cursor-agent")
        self.install("--timer", env={"PATH": f"{agent_dir}:{self.env['PATH']}"})
        for service in (self.service, self.rate_service):
            environment = dict(value.split("=", 1) for value in
                               shlex.split(self.unit(service)["Service"]["Environment"]))
            self.assertEqual(environment["PATH"].split(":")[0], str(agent_dir))
            found = subprocess.run(["/bin/sh", "-c", "command -v cursor-agent"],
                                   env=environment, capture_output=True, text=True, check=True)
            self.assertEqual(found.stdout.strip(), str(agent_dir / "cursor-agent"))
        quota_path = dict(value.split("=", 1) for value in
                          shlex.split(self.unit(self.service)["Service"]["Environment"]))["PATH"]
        rate_path = dict(value.split("=", 1) for value in
                         shlex.split(self.unit(self.rate_service)["Service"]["Environment"]))["PATH"]
        self.assertIn(str(self.bin), quota_path.split(":"))  # codex/claude still in fake-bin
        self.assertEqual(rate_path.split(":"),
                         [str(agent_dir), "/usr/local/bin", "/usr/bin", "/bin"])

    def test_quota_timer_finds_codex_in_a_different_folder(self):
        codex_dir = self.tmp / "codex-bin"
        codex_dir.mkdir()
        (self.bin / "codex").rename(codex_dir / "codex")
        self.install("--timer", env={"PATH": f"{codex_dir}:{self.env['PATH']}"})
        environment = dict(item.split("=", 1) for item in
                           shlex.split(self.unit(self.service)["Service"]["Environment"]))
        found = subprocess.run(["/bin/sh", "-c", "command -v codex"], env=environment,
                               capture_output=True, text=True, check=True)
        self.assertEqual(found.stdout.strip(), str(codex_dir / "codex"))
        self.assertIn(str(self.bin), environment["PATH"].split(":"))

    def test_missing_codex_warns_and_installs_timer(self):
        output = self.install("--timer", env=self.private_path_without("codex"))
        self.assertIn("codex not found; quota timer", output)
        self.assertTrue(self.service.exists())
        self.assertIn("--user enable --now claude-delegate-quota.timer", self.calls.read_text())

    def test_quota_timer_finds_claude_in_a_different_folder(self):
        claude_dir = self.tmp / "claude-bin"
        claude_dir.mkdir()
        (self.bin / "claude").rename(claude_dir / "claude")
        self.install("--timer", env={"PATH": f"{claude_dir}:{self.env['PATH']}"})
        environment = dict(item.split("=", 1) for item in
                           shlex.split(self.unit(self.service)["Service"]["Environment"]))
        found = subprocess.run(["/bin/sh", "-c", "command -v claude"], env=environment,
                               capture_output=True, text=True, check=True)
        self.assertEqual(found.stdout.strip(), str(claude_dir / "claude"))
        self.assertIn(str(self.bin), environment["PATH"].split(":"))

    def test_missing_claude_warns_and_installs_timer(self):
        output = self.install("--timer", env=self.private_path_without("claude"))
        self.assertIn("claude not found; quota timer", output)
        self.assertTrue(self.service.exists())
        self.assertIn("--user enable --now claude-delegate-quota.timer", self.calls.read_text())

    def test_missing_cursor_agent_warns_but_still_installs_and_enables_both_timers(self):
        output = self.install("--timer", env=self.private_path_without("cursor-agent"))
        self.assertIn("cursor-agent not found; quota and rate timers", output)
        self.assertTrue(self.rate_service.exists())
        self.assertTrue(self.rate_timer.exists())
        self.assertEqual(self.calls.read_text().splitlines(), [
            "--user daemon-reload", "--user enable --now claude-delegate-quota.timer",
            "--user enable --now claude-delegate-rate.timer"])

    def test_agents_in_separate_directories_dedupe_timer_paths(self):
        codex_dir = self.tmp / "codex-only"
        claude_dir = self.tmp / "claude-only"
        cursor_dir = self.tmp / "cursor-only"
        for directory, name in ((codex_dir, "codex"), (claude_dir, "claude"),
                                (cursor_dir, "cursor-agent")):
            directory.mkdir()
            (self.bin / name).rename(directory / name)
        path = f"{codex_dir}:{claude_dir}:{cursor_dir}:{self.env['PATH']}"
        self.install("--timer", env={"PATH": path})
        quota_dirs = dict(value.split("=", 1) for value in
                          shlex.split(self.unit(self.service)["Service"]["Environment"]))["PATH"].split(":")
        rate_dirs = dict(value.split("=", 1) for value in
                         shlex.split(self.unit(self.rate_service)["Service"]["Environment"]))["PATH"].split(":")
        self.assertEqual(quota_dirs, [str(cursor_dir), str(claude_dir), str(codex_dir),
                                      "/usr/local/bin", "/usr/bin", "/bin"])
        self.assertEqual(rate_dirs, [str(cursor_dir), "/usr/local/bin", "/usr/bin", "/bin"])
        self.assertEqual(len(quota_dirs), len(set(quota_dirs)))
        self.assertEqual(len(rate_dirs), len(set(rate_dirs)))

    def test_installed_rate_command_without_drawn_sessions_does_nothing(self):
        log = self.tmp / "sessions.jsonl"
        rows = [{"event": "start", "session": "not-drawn", "mode": "orchestrator"},
                {"event": "summary", "session": "not-drawn", "last_call": "2026-10-01T00:00:00Z"}]
        log.write_text("".join(json.dumps(row) + "\n" for row in rows))
        original = log.read_bytes()
        self.install("--timer", env={"CLAUDE_SESSION_LOG": str(log)})
        service = self.unit(self.rate_service)["Service"]
        environment = dict(value.split("=", 1) for value in shlex.split(service["Environment"]))
        # The fake cursor-agent rejects every call except prerequisite checks.
        result = subprocess.run(shlex.split(service["ExecStart"]),
                                env={**self.env, **environment, "PYTHONDONTWRITEBYTECODE": "1",
                                     "DELEGATE_BACKUP_DIRS": ""},
                                capture_output=True, text=True)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertEqual(log.read_bytes(), original)
        self.assertFalse(self.settings.exists())  # --experiment was never installed

    def test_install_without_timer_creates_no_units_or_systemctl_calls(self):
        self.install()
        self.assertFalse(self.units.exists())
        self.assertFalse(self.calls.exists())
        self.assertTrue((self.tmp / "claude/skills/delegate").is_symlink())

    def test_check_reports_enabled_and_disabled_timer_without_installing(self):
        disabled = self.install("--check", "--timer")
        enabled = self.install("--check", env={"FAKE_TIMER_ENABLED": "0"})
        for kind in ("quota", "rate"):
            self.assertIn(f"{kind} timer not enabled", disabled)
            self.assertIn(f"{kind} timer enabled", enabled)
        self.assertEqual(self.calls.read_text().splitlines(),
                         ["--user is-enabled claude-delegate-quota.timer",
                          "--user is-enabled claude-delegate-rate.timer"] * 2)
        self.assertFalse(self.units.exists())
        self.assertFalse((self.tmp / "claude/skills").exists())

    def test_check_reports_each_timer_status_independently(self):
        output = self.install("--check", env={"FAKE_RATE_TIMER_ENABLED": "0"})
        self.assertIn("quota timer not enabled", output)
        self.assertIn("rate timer enabled", output)
        output = self.install("--check", env={"FAKE_TIMER_ENABLED": "0",
                                               "FAKE_RATE_TIMER_ENABLED": "1"})
        self.assertIn("quota timer enabled", output)
        self.assertIn("rate timer not enabled", output)
        self.assertFalse(self.units.exists())
        self.assertFalse(self.settings.parent.exists())

    def test_without_systemctl_writes_units_warns_and_check_uses_unit_file(self):
        env = self.private_path_without("systemctl", "cursor-agent", "codex", "claude")
        before = self.install("--check", env=env)
        for kind in ("quota", "rate"):
            self.assertIn(f"{kind} timer unit not installed", before)
        self.assertFalse(self.units.exists())
        self.assertFalse(self.settings.parent.exists())
        output = self.install("--timer", env=env)
        self.assertIn("systemctl not found", output)
        self.assertIn("claude not found", output)
        self.assertIn("cursor-agent not found", output)
        self.assertTrue(self.service.exists())
        self.assertTrue(self.timer.exists())
        for service in (self.service, self.rate_service):
            self.assertIn('Environment="PATH=/usr/local/bin:/usr/bin:/bin"', service.read_text())
        for kind in ("quota", "rate"):
            self.assertIn(f"systemctl not found; {kind} timer units written but not enabled", output)
        self.assertFalse(self.calls.exists())
        files = {p.relative_to(self.tmp): p.read_bytes() for p in self.tmp.rglob("*") if p.is_file()}
        after = self.install("--check", env=env)
        for kind in ("quota", "rate"):
            self.assertIn(f"{kind} timer unit exists", after)
        self.assertEqual({p.relative_to(self.tmp): p.read_bytes()
                          for p in self.tmp.rglob("*") if p.is_file()}, files)
        self.rate_timer.unlink()
        mixed = self.install("--check", env=env)
        self.assertIn("quota timer unit exists", mixed)
        self.assertIn("rate timer unit not installed", mixed)
        self.assertFalse(self.rate_timer.exists())

    def test_timer_carries_custom_cache_and_log_paths(self):
        self.install("--timer", env={"XDG_CACHE_HOME": str(self.tmp / "cache"),
                                    "CLAUDE_DELEGATE_QUOTA_LOG": str(self.tmp / "q.jsonl"),
                                    "CLAUDE_SESSION_LOG": str(self.tmp / "sessions.jsonl")})
        environment = next(line for line in self.service.read_text().splitlines()
                           if line.startswith("Environment="))
        self.assertEqual(shlex.split(environment.split("=", 1)[1])[1:],
                         [f"CLAUDE_CONFIG_DIR={self.tmp / 'claude'}", f"XDG_CACHE_HOME={self.tmp / 'cache'}",
                          f"CLAUDE_DELEGATE_QUOTA_LOG={self.tmp / 'q.jsonl'}"])
        rate = shlex.split(self.unit(self.rate_service)["Service"]["Environment"])
        self.assertEqual(rate[1:], [f"CLAUDE_CONFIG_DIR={self.tmp / 'claude'}",
                                   f"XDG_CACHE_HOME={self.tmp / 'cache'}",
                                   f"CLAUDE_SESSION_LOG={self.tmp / 'sessions.jsonl'}"])

    def test_rate_timer_omits_unset_environment_and_quotes_custom_session_log(self):
        self.install("--timer", env={"CLAUDE_CONFIG_DIR": "", "XDG_CACHE_HOME": "",
                                    "CLAUDE_SESSION_LOG": ""})
        rate = self.unit(self.rate_service)["Service"]
        self.assertEqual(shlex.split(rate["Environment"]),
                         [f"PATH={self.bin}:/usr/local/bin:/usr/bin:/bin"])
        log = self.tmp / 'session logs %s "quoted".jsonl'
        self.install("--timer", env={"CLAUDE_SESSION_LOG": str(log)})
        rate = self.unit(self.rate_service)["Service"]
        self.assertIn(f"CLAUDE_SESSION_LOG={str(log).replace('%', '%%')}",
                      shlex.split(rate["Environment"]))

    def test_help_documents_timer_and_removal(self):
        output = self.install("--help")
        self.assertIn("--timer", output)
        self.assertIn("systemctl --user disable --now claude-delegate-quota.timer", output)
        self.assertIn("delete claude-delegate-quota.{service,timer}", output)
        self.assertIn("systemctl --user disable --now claude-delegate-rate.timer", output)
        self.assertIn("delete claude-delegate-rate.{service,timer}", output)
        self.assertIn("hourly timer for default delegate rate", output)
        self.assertIn("--force", output)
        self.assertIn("session-mode.json", output)
        self.assertIn("--experiment  opt in to control sessions", output)
        self.assertIn("skipped while the session-mode hook is registered", output)
        self.assertNotIn("set -euo", output)
        self.assertFalse(self.units.exists())


if __name__ == "__main__":
    unittest.main()

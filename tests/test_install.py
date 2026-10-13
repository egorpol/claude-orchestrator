"""Installer tests isolate home/config and fake both openusage and systemctl."""
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

INSTALL = Path(__file__).resolve().parents[1] / "install.sh"


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

    def install(self, *args, env=None):
        result = subprocess.run(["/bin/bash", str(INSTALL), *args],
                                env={**self.env, **(env or {})}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

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
        self.assertNotIn("set -euo", output)
        self.assertFalse(self.units.exists())


if __name__ == "__main__":
    unittest.main()

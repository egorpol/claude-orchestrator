"""Guard the real delegate skill's small core and on-demand topic structure."""
import hashlib
from pathlib import Path
import re
import subprocess
import sys
import unittest


SKILL = Path(__file__).resolve().parents[1] / "skills" / "delegate"
CORE = SKILL / "SKILL.md"
REFERENCE = SKILL / "reference"
TOPICS = {"commands.md", "sessions.md", "briefs.md", "quota.md", "launch.md",
          "review.md", "agents.md"}


class SkillSplitTests(unittest.TestCase):
    def test_core_byte_budget(self):
        self.assertLessEqual(len(CORE.read_bytes()), 12_288)

    def test_trigger_frontmatter_is_unchanged(self):
        # The first four lines, including line endings, as shipped on 2026-11-06;
        # re-pinned 2026-11-08 when "(openusage)" left the description (reader removed).
        frontmatter = b"".join(CORE.read_bytes().splitlines(keepends=True)[:4])
        self.assertEqual(hashlib.sha256(frontmatter).hexdigest(),
                         "863aa609e6da5cd6c8f25c5df6200859bb904bc8ce2d208d0e9c57bc9d5e5f65")

    def test_reference_files_and_core_pointers_match(self):
        named = set(re.findall(r"reference/([\w./-]+\.md)", CORE.read_text(encoding="utf-8")))
        files = {p.relative_to(REFERENCE).as_posix()
                 for p in REFERENCE.rglob("*") if p.is_file()}
        self.assertEqual(files, TOPICS)
        self.assertEqual(named, files)
        for name in named:
            with self.subTest(topic=name):
                self.assertTrue((REFERENCE / name).is_file())
                self.assertIn("~/.claude/skills/delegate/reference/" + name,
                              CORE.read_text(encoding="utf-8"))

    def test_topics_open_with_read_guidance_and_path_scope(self):
        for topic in sorted(REFERENCE.glob("*.md")):
            with self.subTest(topic=topic.name):
                lines = topic.read_text(encoding="utf-8").splitlines()
                self.assertTrue(lines[0].startswith("# "))
                self.assertEqual(lines[1], "")
                self.assertTrue(lines[2].startswith("Read "))
                self.assertIn("relative to the skill folder", lines[2])

    def test_every_public_subcommand_is_documented(self):
        result = subprocess.run([sys.executable, str(SKILL / "scripts" / "delegate"), "--help"],
                                text=True, capture_output=True, check=True)
        listed = re.search(r"\{([a-z][a-z,-]+)\}", result.stdout)
        self.assertIsNotNone(listed, result.stdout)
        commands = listed.group(1).split(",")
        documents = [CORE, *sorted(REFERENCE.glob("*.md"))]
        text = "\n".join(p.read_text(encoding="utf-8") for p in documents)
        for command in commands:
            with self.subTest(command=command):
                self.assertRegex(text, r"\bdelegate\s+" + re.escape(command) + r"\b")

    def test_core_keeps_essential_rules(self):
        core = CORE.read_text(encoding="utf-8")
        for essential in ("--force", "--model auto", "run_in_background", "poll",
                          "never commit", "read-only", "planted failure", "--preflight"):
            with self.subTest(essential=essential):
                self.assertIn(essential, core)

    def test_steps_keep_numbers_and_read_first_paths(self):
        steps = re.findall(r"^(\d+)\. (.*)$", CORE.read_text(encoding="utf-8"), re.M)
        self.assertEqual([int(number) for number, _ in steps], list(range(10)))
        expected = ["briefs.md", "quota.md", "briefs.md", "launch.md"] + ["review.md"] * 6
        for (number, step), topic in zip(steps, expected):
            with self.subTest(step=number):
                self.assertTrue(step.startswith(
                    "Read `~/.claude/skills/delegate/reference/" + topic + "` first;"))
                moved = (REFERENCE / topic).read_text(encoding="utf-8")
                self.assertRegex(moved, r"(?m)^" + number + r"\. ")

    def test_command_tables_keep_full_and_quick_forms(self):
        full = (REFERENCE / "commands.md").read_text(encoding="utf-8")
        self.assertEqual(len(re.findall(r"^\| `delegate ", full, re.M)), 24)
        quick = [line for line in CORE.read_text(encoding="utf-8").splitlines()
                 if line.startswith("| `delegate ")]
        self.assertEqual(len(quick), 14)
        for row in quick:
            self.assertLessEqual(len(row), 120, row)


if __name__ == "__main__":
    unittest.main()

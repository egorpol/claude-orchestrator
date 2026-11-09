"""Static and live guards that the suite stays inside tests/_env.py's sandbox."""
import _env  # noqa: F401  — must be the first statement after docstring/__future__

import ast
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent / "skills" / "delegate" / "scripts"


def _is_env_import(node):
    if isinstance(node, ast.Import):
        return any(alias.name == "_env" or alias.name.startswith("_env.")
                   for alias in node.names)
    if isinstance(node, ast.ImportFrom):
        return node.module == "_env" or (
            node.module is not None and node.module.startswith("_env.")
        )
    return False


def _first_statement_index(body):
    """Index of the first statement after an optional docstring and __future__ imports."""
    i = 0
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        i = 1
    while (i < len(body) and isinstance(body[i], ast.ImportFrom)
           and body[i].module == "__future__"):
        i += 1
    return i


def env_import_first_error(path):
    """None if `import _env` is the first real statement; else an error string."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    i = _first_statement_index(tree.body)
    if i >= len(tree.body):
        return f"{path.name}: missing `import _env`"
    node = tree.body[i]
    if not _is_env_import(node):
        return (f"{path.name}: first statement after docstring/__future__ "
                f"must be `import _env` (line {node.lineno})")
    return None


class HermeticEnvImportGuardTests(unittest.TestCase):
    def test_every_discoverable_test_module_imports_env_first(self):
        # discover's default pattern is test*.py (underscore optional).
        errors = []
        for path in sorted(TESTS.glob("test*.py")):
            err = env_import_first_error(path)
            if err:
                errors.append(err)
        self.assertEqual(errors, [], "\n".join(errors))

    def test_planted_bypasses_fail_the_guard(self):
        planted = {
            "test_zz_append.py": 'import sys\nsys.path.append("x")\nimport _env\n',
            "test_zz_slice.py": 'import sys\nsys.path[:0] = ["x"]\nimport _env\n',
            "test_zz_helper.py": "import helper_mod\nimport _env\n",
            "test_zz_call.py": "import os\nos.getcwd()\nimport _env\n",
            "test_zz_insert.py": 'import sys\nsys.path.insert(0, "x")\nimport _env\n',
            "test_zz_missing.py": "import os\n",
            "testzz_nounderscore.py": 'import sys\nsys.path.insert(0, "x")\nimport _env\n',
        }
        with tempfile.TemporaryDirectory(prefix="hermetic-guard-bad-") as tmp:
            for name, text in planted.items():
                path = Path(tmp) / name
                path.write_text(text, encoding="utf-8")
                err = env_import_first_error(path)
                self.assertIsNotNone(err, name)
                self.assertIn(path.stem, err)

    def test_planted_near_misses_pass_the_guard(self):
        planted = {
            "test_zz_future.py": (
                '"""Doc."""\nfrom __future__ import annotations\n'
                "import _env  # noqa\nimport os\n"
            ),
            "test_zz_comment.py": "# comment\nimport _env\nimport os\n",
            "test_zz_after.py": (
                "# comment\nimport _env\nimport sys\nsys.path.insert(0, \"x\")\n"
            ),
        }
        with tempfile.TemporaryDirectory(prefix="hermetic-guard-ok-") as tmp:
            for name, text in planted.items():
                path = Path(tmp) / name
                path.write_text(text, encoding="utf-8")
                self.assertIsNone(env_import_first_error(path), name)


class HermeticEnvIsolationTests(unittest.TestCase):
    def test_dir_vars_point_under_temp_root(self):
        self.assertIsNotNone(_env.ROOT)
        root = str(_env.ROOT)
        for key in ("HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME",
                    "CLAUDE_CONFIG_DIR", "CODEX_HOME"):
            self.assertIn(key, os.environ, key)
            self.assertTrue(os.environ[key].startswith(root + os.sep), key)

    def test_cursor_reader_opted_in(self):
        path = (Path(os.environ["XDG_CONFIG_HOME"]) / "claude-orchestrator"
                / "catalog.local.json")
        self.assertTrue(path.is_file())
        self.assertIn("cursor_usage_reader", path.read_text(encoding="utf-8"))

    def test_runs_root_under_isolated_cache(self):
        sys.path.insert(0, str(SCRIPTS))
        import runs  # after _env; RUNS_ROOT is fixed at import time
        cache = Path(os.environ["XDG_CACHE_HOME"])
        self.assertEqual(runs.RUNS_ROOT, cache / "claude-delegate" / "runs")


class HermeticEnvPositiveControlTests(unittest.TestCase):
    def _discover(self, folder, env=None):
        run_env = {**os.environ, "PYTHONPATH": str(TESTS)}
        if env:
            run_env.update(env)
        return subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", str(folder), "-v"],
            cwd=str(folder), env=run_env, capture_output=True, text=True, timeout=60,
        )

    def test_planted_codex_leak_fails_the_run(self):
        with tempfile.TemporaryDirectory(prefix="hermetic-plant-leak-") as tmp:
            folder = Path(tmp)
            (folder / "test_planted_leak.py").write_text(
                "import _env\n"
                "import subprocess\n"
                "import unittest\n"
                "class T(unittest.TestCase):\n"
                "    def test_leak(self):\n"
                "        subprocess.run(['codex', 'app-server'], capture_output=True)\n",
                encoding="utf-8",
            )
            # A leak keeps the child's temp root; put it inside this folder.
            result = self._discover(folder, env={"TMPDIR": str(folder)})
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        combined = result.stdout + result.stderr
        self.assertIn("codex", combined)

    def test_clean_planted_module_passes(self):
        with tempfile.TemporaryDirectory(prefix="hermetic-plant-clean-") as tmp:
            folder = Path(tmp)
            (folder / "test_planted_clean.py").write_text(
                "import _env\n"
                "import tempfile\n"
                "import unittest\n"
                "from pathlib import Path\n"
                "class T(unittest.TestCase):\n"
                "    def test_clean(self):\n"
                "        d = Path(tempfile.mkdtemp())\n"
                "        (d / 'x').write_text('x')\n",
                encoding="utf-8",
            )
            result = self._discover(folder, env={"TMPDIR": str(folder)})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_planted_network_leak_with_cleared_env_fails(self):
        with tempfile.TemporaryDirectory(prefix="hermetic-plant-net-") as tmp:
            folder = Path(tmp)
            (folder / "test_planted_net_leak.py").write_text(
                "import _env\n"
                "import unittest\n"
                "import urllib.error\n"
                "import urllib.request\n"
                "from unittest import mock\n"
                "import os\n"
                "class T(unittest.TestCase):\n"
                "    def test_leak(self):\n"
                "        with mock.patch.dict(os.environ, {}, clear=True):\n"
                "            try:\n"
                "                urllib.request.urlopen('http://192.0.2.1/', timeout=2)\n"
                "            except Exception:\n"
                "                pass\n",
                encoding="utf-8",
            )
            result = self._discover(folder, env={"TMPDIR": str(folder)})
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("192.0.2.1", result.stdout + result.stderr)

    def test_planted_network_near_misses_pass(self):
        with tempfile.TemporaryDirectory(prefix="hermetic-plant-netok-") as tmp:
            folder = Path(tmp)
            (folder / "test_planted_net_ok.py").write_text(
                "import _env\n"
                "import socket\n"
                "import unittest\n"
                "class T(unittest.TestCase):\n"
                "    def test_loopback_oserror_or_connect(self):\n"
                "        # Prefer OSError; a sandbox may accept discard-port connects.\n"
                "        try:\n"
                "            socket.create_connection(('127.0.0.1', 9), timeout=2)\n"
                "        except OSError:\n"
                "            pass\n"
                "    def test_getaddrinfo_localhost(self):\n"
                "        socket.getaddrinfo('localhost', 9)\n"
                "    def test_af_unix_socketpair(self):\n"
                "        a, b = socket.socketpair()\n"
                "        a.close()\n"
                "        b.close()\n",
                encoding="utf-8",
            )
            result = self._discover(folder, env={"TMPDIR": str(folder)})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

"""Unit tests for catalog.local.json merge, load, and `delegate catalog`."""
import copy
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "delegate" / "scripts"
sys.path.insert(0, str(SCRIPTS))
CLI = SCRIPTS / "delegate"

import catalog  # noqa: E402
import cost  # noqa: E402
import quota  # noqa: E402


def _base(**overrides):
    data = {
        "min_quota_pct": 10,
        "orchestrator_pool_penalty": 0.8,
        "aa_aliases": {"cursor:x": "y"},
        "prefer": {
            "implement": ["cursor-auto", "codex-sol"],
            "review": ["codex-sol"],
        },
        "delegates": [
            {"id": "codex-sol", "agent": "codex", "model": "gpt-6.1-sol",
             "effort": "xhigh", "aa": "gpt-6-1-sol-xhigh", "pools": ["codex-5h"]},
            {"id": "codex-astra", "agent": "codex", "model": "gpt-6-astra",
             "effort": "xhigh", "aa": "gpt-6-astra-xhigh", "pools": ["codex-5h"]},
            {"id": "codex-terra", "agent": "codex", "model": "gpt-5.6-terra",
             "effort": "max", "aa": "gpt-5-6-terra", "pools": ["codex-5h"]},
            {"id": "cursor-auto", "agent": "cursor", "model": "auto",
             "aa": "grok-4-7-high", "aa_proxy": True, "pools": ["cursor-auto"]},
            {"id": "cursor-grok", "agent": "cursor", "model": "grok-4.7-high",
             "aa": "grok-4-7-high", "pools": ["cursor-auto"]},
            {"id": "claude-sonnet", "agent": "claude-subagent", "model": "sonnet",
             "aa": "claude-sonnet-5-5-high", "pools": ["claude-5h"]},
            {"id": "claude-haiku", "agent": "claude-subagent", "model": "haiku",
             "aa": "claude-haiku-5-5", "pools": ["claude-5h"]},
        ],
        "tasks": {
            "implement": {"weights": {"intelligence": 0.4, "coding": 0.5, "speed": 0.05, "price": 0.05}},
            "review": {"weights": {"intelligence": 0.45, "coding": 0.45, "speed": 0.05, "price": 0.05}},
        },
    }
    data.update(overrides)
    return data


class MergeTests(unittest.TestCase):
    def test_override_one_delegate_effort_keeps_fields_and_order(self):
        base = _base()
        local = {"delegates": [{"id": "codex-sol", "effort": "high"}]}
        merged, overrides, warnings = catalog.merge(base, local)
        self.assertEqual([d["id"] for d in merged["delegates"]],
                         [d["id"] for d in base["delegates"]])
        sol = next(d for d in merged["delegates"] if d["id"] == "codex-sol")
        self.assertEqual(sol["effort"], "high")
        self.assertEqual(sol["model"], "gpt-6.1-sol")
        self.assertEqual(sol["aa"], "gpt-6-1-sol-xhigh")
        self.assertEqual(sol["pools"], ["codex-5h"])
        self.assertEqual(overrides, ["delegates[codex-sol].effort"])
        self.assertEqual(warnings, [])

    def test_new_delegate_without_model_errors(self):
        with self.assertRaises(catalog.CatalogError) as ctx:
            catalog.merge(_base(), {"delegates": [{"id": "my-new", "agent": "codex"}]})
        self.assertIn("agent and model", str(ctx.exception))

    def test_remove_unknown_id_errors(self):
        with self.assertRaises(catalog.CatalogError) as ctx:
            catalog.merge(_base(), {"delegates": [{"id": "nope", "remove": True}]})
        self.assertIn("unknown delegate", str(ctx.exception))

    def test_duplicate_local_ids_error(self):
        with self.assertRaises(catalog.CatalogError) as ctx:
            catalog.merge(_base(), {"delegates": [
                {"id": "codex-sol", "effort": "high"},
                {"id": "codex-sol", "effort": "low"},
            ]})
        self.assertIn("duplicate", str(ctx.exception))

    def test_null_deletes_aa_aliases_but_null_delegates_errors_on_validate(self):
        merged, overrides, _ = catalog.merge(_base(), {"aa_aliases": None})
        self.assertNotIn("aa_aliases", merged)
        self.assertEqual(overrides, ["aa_aliases"])
        catalog.validate(merged)

        merged2, _, _ = catalog.merge(_base(), {"delegates": None})
        with self.assertRaises(catalog.CatalogError) as ctx:
            catalog.validate(merged2)
        self.assertIn("delegates", str(ctx.exception))

        merged3, _, _ = catalog.merge(_base(), {"tasks": None})
        with self.assertRaises(catalog.CatalogError) as ctx:
            catalog.validate(merged3)
        self.assertIn("tasks", str(ctx.exception))

    def test_prefer_implement_replace_leaves_review(self):
        merged, overrides, _ = catalog.merge(
            _base(), {"prefer": {"implement": ["codex-sol"]}})
        self.assertEqual(merged["prefer"]["implement"], ["codex-sol"])
        self.assertEqual(merged["prefer"]["review"], ["codex-sol"])
        self.assertEqual(overrides, ["prefer.implement"])

    def test_array_replaces_never_concatenates(self):
        merged, _, _ = catalog.merge(
            _base(), {"delegates": [{"id": "codex-sol", "pools": ["codex-7d"]}]})
        sol = next(d for d in merged["delegates"] if d["id"] == "codex-sol")
        self.assertEqual(sol["pools"], ["codex-7d"])

    def test_underscore_note_keys_ignored(self):
        merged, overrides, warnings = catalog.merge(
            _base(), {"_note": "x", "min_quota_pct": 20})
        self.assertEqual(merged["min_quota_pct"], 20)
        self.assertEqual(overrides, ["min_quota_pct"])
        self.assertNotIn("_note", merged)
        self.assertEqual(warnings, [])

    def test_base_not_mutated(self):
        base = _base()
        snapshot = copy.deepcopy(base)
        catalog.merge(base, {"min_quota_pct": 99, "delegates": [
            {"id": "codex-sol", "effort": "low"},
            {"id": "claude-haiku", "remove": True},
            {"id": "my-new", "agent": "codex", "model": "m"},
        ]})
        self.assertEqual(base, snapshot)

    def test_min_quota_pct_bool_errors(self):
        merged, _, _ = catalog.merge(_base(), {"min_quota_pct": True})
        with self.assertRaises(catalog.CatalogError) as ctx:
            catalog.validate(merged)
        self.assertIn("min_quota_pct", str(ctx.exception))

    def test_new_delegate_appended_and_remove(self):
        merged, overrides, _ = catalog.merge(_base(), {"delegates": [
            {"id": "my-new", "agent": "codex", "model": "m", "effort": "high"},
            {"id": "claude-haiku", "remove": True},
        ]})
        ids = [d["id"] for d in merged["delegates"]]
        self.assertEqual(ids[-1], "my-new")
        self.assertNotIn("claude-haiku", ids)
        self.assertEqual(len(ids), 7)
        self.assertIn("delegates[+my-new]", overrides)
        self.assertIn("delegates[-claude-haiku]", overrides)


class LocalPathTests(unittest.TestCase):
    def test_env_cases(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            os.environ.pop("DELEGATE_CATALOG_LOCAL", None)
            os.environ.pop("XDG_CONFIG_HOME", None)
            with mock.patch.object(Path, "home", return_value=Path("/fakehome")):
                self.assertEqual(
                    catalog.local_path(),
                    Path("/fakehome/.config/claude-orchestrator/catalog.local.json"))

        with mock.patch.dict(os.environ, {"DELEGATE_CATALOG_LOCAL": ""}):
            self.assertIsNone(catalog.local_path())

        with mock.patch.dict(os.environ, {"DELEGATE_CATALOG_LOCAL": "/tmp/mine.json"}):
            self.assertEqual(catalog.local_path(), Path("/tmp/mine.json"))

        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": ""}, clear=False):
            os.environ.pop("DELEGATE_CATALOG_LOCAL", None)
            # Empty XDG counts as unset.
            with mock.patch.object(Path, "home", return_value=Path("/fakehome")):
                self.assertEqual(
                    catalog.local_path(),
                    Path("/fakehome/.config/claude-orchestrator/catalog.local.json"))

        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "/xdg"}):
            os.environ.pop("DELEGATE_CATALOG_LOCAL", None)
            self.assertEqual(
                catalog.local_path(),
                Path("/xdg/claude-orchestrator/catalog.local.json"))


class TypoWarningTests(unittest.TestCase):
    def test_unknown_top_level_key(self):
        _, overrides, warnings = catalog.merge(_base(), {"min_quota_percent": 20})
        self.assertEqual(overrides, [])
        self.assertEqual(warnings, ["unknown top-level key 'min_quota_percent'"])

    def test_unknown_delegate_field(self):
        _, _, warnings = catalog.merge(
            _base(), {"delegates": [{"id": "codex-sol", "typo_field": 1}]})
        self.assertTrue(any("typo_field" in w for w in warnings))

    def test_aa_proxy_on_other_delegate_no_warn(self):
        _, _, warnings = catalog.merge(
            _base(), {"delegates": [{"id": "codex-sol", "aa_proxy": True}]})
        self.assertEqual(warnings, [])

    def test_prior_and_remove_no_warn(self):
        _, _, warnings = catalog.merge(_base(), {"delegates": [
            {"id": "codex-sol", "prior": {"intelligence": 1}},
        ]})
        self.assertEqual(warnings, [])

    def test_prefer_unknown_task(self):
        _, _, warnings = catalog.merge(
            _base(), {"prefer": {"no-such-task": ["codex-sol"]}})
        self.assertTrue(any("no-such-task" in w for w in warnings))

    def test_new_task_allowed(self):
        merged, overrides, warnings = catalog.merge(_base(), {
            "tasks": {"bulk-label": {"weights": {"intelligence": 1, "coding": 0, "speed": 0, "price": 0}}}
        })
        self.assertIn("bulk-label", merged["tasks"])
        self.assertFalse(any("bulk-label" in w and "unknown" in w for w in warnings))
        catalog.validate(merged)


class LoadIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="catalog-load-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.base = self.dir / "catalog.json"
        self.base.write_text(json.dumps(_base()), encoding="utf-8")
        self.local = self.dir / "local.json"
        # Reset once-per-process warning flag between tests.
        catalog._warnings_emitted = False

    def test_missing_local_returns_base(self):
        with mock.patch.dict(os.environ, {"DELEGATE_CATALOG_LOCAL": str(self.dir / "missing.json")}):
            got = catalog.load(self.base)
        self.assertEqual(got["min_quota_pct"], 10)

    def test_check_dispatch_takes_min_quota_from_local(self):
        self.local.write_text(json.dumps({"min_quota_pct": 33}), encoding="utf-8")
        with mock.patch.dict(os.environ, {"DELEGATE_CATALOG_LOCAL": str(self.local)}), \
             mock.patch.object(quota, "fetch", return_value={}), \
             mock.patch.object(quota, "pools", return_value={
                 "codex-5h": {"left_pct": 50, "age_min": 0},
                 "codex-7d": {"left_pct": 50, "age_min": 0}}):
            result = quota.check_dispatch(self.base)
        self.assertEqual(result["threshold"], 33)
        self.assertIsNone(result["error"])

    def test_check_dispatch_raises_on_broken_local(self):
        self.local.write_text("{", encoding="utf-8")
        with mock.patch.dict(os.environ, {"DELEGATE_CATALOG_LOCAL": str(self.local)}):
            with self.assertRaises(catalog.CatalogError) as ctx:
                quota.check_dispatch(self.base)
        self.assertIn(str(self.local), str(ctx.exception))
        self.assertIn("invalid JSON", str(ctx.exception))

    def test_load_catalog_cost_merges_local_weights(self):
        self.local.write_text(json.dumps({
            "orchestrator_cost": {"weights": {"input": 9}}
        }), encoding="utf-8")
        # Stub base needs orchestrator_cost for a meaningful merge; use real CATALOG
        # shape via a temp base that includes it.
        rich = _base()
        rich["orchestrator_cost"] = {"weights": {"input": 1, "output": 5},
                                     "list_prices": {"as_of": "x", "models": {}}}
        self.base.write_text(json.dumps(rich), encoding="utf-8")
        with mock.patch.dict(os.environ, {"DELEGATE_CATALOG_LOCAL": str(self.local)}), \
             mock.patch.object(cost, "CATALOG", self.base):
            weights, prices = cost.load_catalog_cost()
        self.assertEqual(weights["input"], 9)
        self.assertEqual(weights["output"], 5)
        self.assertEqual(prices["as_of"], "x")

    def test_load_catalog_cost_falls_back_on_broken_local(self):
        self.local.write_text("{", encoding="utf-8")
        rich = _base()
        rich["orchestrator_cost"] = {"weights": {"input": 1}, "list_prices": {"as_of": "y"}}
        self.base.write_text(json.dumps(rich), encoding="utf-8")
        cost._catalog_cost_local_warned = False
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"DELEGATE_CATALOG_LOCAL": str(self.local)}), \
             mock.patch.object(cost, "CATALOG", self.base), \
             redirect_stderr(err):
            weights, prices = cost.load_catalog_cost()
        self.assertEqual(weights["input"], 1)
        self.assertEqual(prices["as_of"], "y")
        self.assertIn(str(self.local), err.getvalue())
        self.assertEqual(err.getvalue().count("\n"), 1)


class FollowUpDefectTests(unittest.TestCase):
    """Regression tests for the six follow-up review findings."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="catalog-fu-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.base = self.dir / "catalog.json"
        self.base.write_text(json.dumps(_base()), encoding="utf-8")
        self.local = self.dir / "local.json"
        catalog._warnings_emitted = False
        cost._catalog_cost_local_warned = False

    def test_object_patch_over_non_object_recurses(self):
        merged, overrides, _ = catalog.merge(
            {"prefer": ["codex-sol"]}, {"prefer": {"review": None}})
        self.assertEqual(merged["prefer"], {})
        self.assertIn("prefer.review", overrides)
        nested, _, _ = catalog.merge(
            {"a": 1}, {"a": {"b": {"c": None, "d": 2}}})
        self.assertEqual(nested, {"a": {"b": {"d": 2}}})

    def test_existing_directory_path_is_error(self):
        with mock.patch.dict(os.environ, {"DELEGATE_CATALOG_LOCAL": str(self.dir)}):
            with self.assertRaises(catalog.CatalogError) as ctx:
                catalog.load(self.base)
            self.assertIn(str(self.dir), str(ctx.exception))
            self.assertIn("not a readable file", str(ctx.exception))
            with self.assertRaises(catalog.CatalogError) as ctx2:
                catalog.load_details(self.base)
            self.assertIn(str(self.dir), str(ctx2.exception))

    def test_invalid_utf8_is_catalog_error(self):
        self.local.write_bytes(b'{"min_quota_pct": \xff}')
        with mock.patch.dict(os.environ, {"DELEGATE_CATALOG_LOCAL": str(self.local)}):
            with self.assertRaises(catalog.CatalogError) as ctx:
                catalog.load(self.base)
        self.assertIn(str(self.local), str(ctx.exception))
        self.assertIn("UTF-8", str(ctx.exception))
        self.assertNotIsInstance(ctx.exception, UnicodeDecodeError)

    def test_ratings_loads_catalog_before_network(self):
        self.local.write_text("{", encoding="utf-8")
        # Import the CLI module the same way tests/test_frontier.py does.
        import importlib.machinery
        loader = importlib.machinery.SourceFileLoader(
            "delegate_cli_fu", str(CLI))
        cli = importlib.util.module_from_spec(
            importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(cli)
        order = []

        def boom_ratings(*_a, **_k):
            order.append("ratings")
            raise RuntimeError("network unavailable")

        def track_load():
            order.append("catalog")
            return catalog.load(self.base)

        with mock.patch.dict(os.environ, {"DELEGATE_CATALOG_LOCAL": str(self.local)}), \
             mock.patch.object(cli, "load_catalog", side_effect=track_load), \
             mock.patch.object(cli.ratings, "load", side_effect=boom_ratings):
            with self.assertRaises(catalog.CatalogError) as ctx:
                cli.cmd_ratings(mock.Mock(refresh=False, grep=None, json=False))
        self.assertEqual(order, ["catalog"])
        self.assertIn("invalid JSON", str(ctx.exception))

    def test_load_catalog_cost_warns_once_per_process(self):
        self.local.write_text("{", encoding="utf-8")
        rich = _base()
        rich["orchestrator_cost"] = {"weights": {"input": 1}, "list_prices": {"as_of": "y"}}
        self.base.write_text(json.dumps(rich), encoding="utf-8")
        cost._catalog_cost_local_warned = False
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"DELEGATE_CATALOG_LOCAL": str(self.local)}), \
             mock.patch.object(cost, "CATALOG", self.base), \
             redirect_stderr(err):
            w1, _ = cost.load_catalog_cost()
            w2, _ = cost.load_catalog_cost()
        self.assertEqual(w1["input"], 1)
        self.assertEqual(w2["input"], 1)
        self.assertEqual(err.getvalue().count("\n"), 1)
        self.assertEqual(err.getvalue().count(str(self.local)), 1)

    def test_remove_false_is_control_not_field(self):
        merged, overrides, _ = catalog.merge(_base(), {
            "delegates": [{"id": "codex-sol", "remove": False, "effort": "high"}]})
        sol = next(d for d in merged["delegates"] if d["id"] == "codex-sol")
        self.assertNotIn("remove", sol)
        self.assertEqual(sol["effort"], "high")
        self.assertEqual(overrides, ["delegates[codex-sol].effort"])
        with self.assertRaises(catalog.CatalogError) as ctx:
            catalog.merge(_base(), {
                "delegates": [{"id": "codex-sol", "remove": "yes"}]})
        self.assertIn("remove must be a boolean", str(ctx.exception))


class CliCatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="catalog-cli-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.local = self.dir / "catalog.local.json"
        catalog._warnings_emitted = False

    def _run(self, *args, env=None):
        base_env = {**os.environ, "DELEGATE_CATALOG_LOCAL": ""}
        if env:
            base_env.update(env)
        return subprocess.run(
            [sys.executable, str(CLI), "catalog", *args],
            text=True, capture_output=True, env=base_env)

    def test_text_no_local(self):
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("local: off (DELEGATE_CATALOG_LOCAL is empty)", r.stdout)
        self.assertIn("no overrides", r.stdout)

    def test_text_with_overrides(self):
        self.local.write_text(json.dumps({
            "min_quota_pct": 20,
            "delegates": [{"id": "codex-sol", "effort": "high", "aa": "gpt-6-1-sol-high"}],
            "prefer": {"implement": ["codex-sol"]},
            "_note": "x",
        }), encoding="utf-8")
        r = self._run(env={"DELEGATE_CATALOG_LOCAL": str(self.local)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"local: {self.local}", r.stdout)
        lines = [ln for ln in r.stdout.splitlines() if ln and not ln.startswith("local:")]
        self.assertEqual(lines, [
            "min_quota_pct",
            "delegates[codex-sol].effort",
            "delegates[codex-sol].aa",
            "prefer.implement",
        ])

    def test_json_mode(self):
        self.local.write_text(json.dumps({"min_quota_pct": 20}), encoding="utf-8")
        r = self._run("--json", env={"DELEGATE_CATALOG_LOCAL": str(self.local)})
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout)
        self.assertEqual(data["min_quota_pct"], 20)
        self.assertEqual(len(data["delegates"]), 7)
        self.assertIn("local:", r.stderr)
        self.assertIn("min_quota_pct", r.stderr)

    def test_broken_local_exits_nonzero(self):
        self.local.write_text("{", encoding="utf-8")
        r = self._run(env={"DELEGATE_CATALOG_LOCAL": str(self.local)})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(str(self.local), r.stderr or r.stdout)
        self.assertNotIn("Traceback", r.stderr + r.stdout)

    def test_typo_warning_no_overrides(self):
        self.local.write_text(json.dumps({"min_quota_percent": 20}), encoding="utf-8")
        r = self._run(env={"DELEGATE_CATALOG_LOCAL": str(self.local)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("no overrides", r.stdout)
        self.assertIn("delegate: catalog.local.json:", r.stderr)
        self.assertIn("min_quota_percent", r.stderr)


class HookStyleQuotaLoadTests(unittest.TestCase):
    def test_quota_loads_like_hooks_under_isolated_python(self):
        """hooks load quota.py via spec_from_file_location without scripts on path."""
        script = r"""
import importlib.util, sys
from pathlib import Path
path = Path(%r)
spec = importlib.util.spec_from_file_location("hook_quota", path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
# Missing log: spare_pools must not raise (ImportError from catalog would).
result = mod.spare_pools(Path("/nonexistent/quota.jsonl"))
assert result is None
print("ok")
""" % str(SCRIPTS / "quota.py")
        r = subprocess.run(
            [sys.executable, "-I", "-c", script],
            text=True, capture_output=True,
            env={"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "/tmp"),
                 "DELEGATE_CATALOG_LOCAL": ""})
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(r.stdout.strip(), "ok")


if __name__ == "__main__":
    unittest.main()

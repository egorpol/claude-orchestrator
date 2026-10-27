"""Exact AA matching and frontier reports, entirely from synthetic local data."""
import importlib.machinery
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "delegate" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import frontier


def rating(name, intelligence=40, task=1, blended=2, creator="OpenAI"):
    return {"name": name, "intelligence": intelligence, "cost_per_task": task,
            "price_blended": blended, "creator": creator}


def row(model, intelligence, task, blended, agent="cursor", creator="OpenAI"):
    return dict(agent=agent, model=model, effort=None, aa=model,
                intelligence=intelligence, cost_per_task=task, price_blended=blended,
                creator=creator, pool=frontier.pool_for(agent, model, creator))


def report(models, ids=(), delegates=(), aliases=None, cache=None):
    catalog = {"delegates": list(delegates), "aa_aliases": aliases or {}}
    env = {"models": models, "source": "fixture", "fetched_at": 100}
    return frontier.build_report(catalog, env, ids, cache or {}, ("gpt-6.1-sol", "medium"))


class MatchingTest(unittest.TestCase):
    def test_exact_family_version_effort_and_bare_slug(self):
        models = {
            "deepseek-v4-1-flash-1017": rating("DeepSeek V4.1 Flash 1017 (Max)"),
            "gemini-4-argon": rating("Gemini 4 Argon (High)"),
            "gpt-6-1-sol-medium": rating("GPT-6.1 Sol (Medium)"),
            "gpt-6-1-sol": rating("GPT-6.1 Sol (Max)"),
            "kimi-k3-1-pro": rating("Kimi K3.1 Pro (Max)"),
            "glm-5-3": rating("GLM 5.3 (High)"),
            "kimi-k2-7-code": rating("Kimi K2.7 Code"),
        }
        index, warnings = frontier.aa_index(models)
        self.assertEqual(warnings, [])
        cases = [("deepseek-v41-flash-1017-max", "deepseek-v4-1-flash-1017"),
                 ("deepseek-v41-flash-1017-none", None), ("gemini-4-argon-high", "gemini-4-argon"),
                 ("gemini-4-argon", None), ("kimi-k3-max", None), ("glm-5.2-high", None),
                 ("kimi-k2.7-code", "kimi-k2-7-code"), ("kimi-k2.7-code-high", None)]
        for model, expected in cases:
            with self.subTest(model=model):
                self.assertEqual(frontier.match_aa("cursor", model, None, models, index, {}), expected)
        for effort, slug in (("medium", "gpt-6-1-sol-medium"), ("max", "gpt-6-1-sol"), ("high", None)):
            self.assertEqual(frontier.match_aa("codex", "gpt-6.1-sol", effort, models, index, {}), slug)

    def test_effort_synonyms_and_first_parenthetical_token(self):
        models = {"a": rating("A (Extra High, Default Fallback)"),
                  "b-non-reasoning": rating("B"), "c": rating("C (Non-reasoning)"),
                  "d-extra-high": rating("D"), "e-high": rating("E (Low, Default Fallback)")}
        result = report(models, ["a-extra-high", "b-none", "c-non-reasoning", "d-xhigh", "e-low"])
        self.assertEqual([r["aa"] for r in result["runnable"] if r["agent"] == "cursor"],
                         ["a", "b-non-reasoning", "c", "d-extra-high", "e-high"])
        self.assertEqual(frontier.split_effort("a-extra-high"), ("a", "xhigh"))

    def test_alias_overrides_automatic_match_and_missing_alias_warns(self):
        models = {"a-high": rating("A (High)"), "other": rating("Other"),
                  "gpt-6-1-sol-medium": rating("GPT-6.1 Sol (Medium)")}
        result = report(models, ["a-high"], aliases={"cursor:a-high": "other",
                       "codex:gpt-6.1-sol@medium": "other"})
        self.assertTrue(all(r["aa"] == "other" for r in result["runnable"]))
        missing = report(models, ["a-high"], aliases={"cursor:a-high": "gone"})
        self.assertIsNone(missing["runnable"][0]["aa"])
        self.assertIn("missing slug 'gone'", " ".join(missing["warnings"]))

    def test_ambiguous_key_matches_nothing_but_alias_can_resolve_it(self):
        models = {"a-1-high": rating("A1 (High)"), "a1": rating("A1 (High)")}
        result = report(models, ["a1-high"])
        self.assertIsNone(result["runnable"][0]["aa"])
        self.assertIn("ambiguous AA key a1@high", result["warnings"][0])
        fixed = report(models, ["a1-high"], aliases={"cursor:a1-high": "a1"})
        self.assertEqual(fixed["runnable"][0]["aa"], "a1")

    def test_cursor_parser_and_skipped_tiers(self):
        text = "ignore - Not a model\n\x1b[1mAvailable models\x1b[0m\n\nauto - Auto\na-high - A\na-high-fast - A Fast\na-high - Duplicate\nfooter\n"
        ids = frontier.parse_cursor_models(text)
        self.assertEqual(ids, ["auto", "a-high", "a-high-fast"])
        result = report({"a": rating("A (High)")}, ids)
        self.assertEqual(result["skipped"], {"cursor_auto": 1, "cursor_fast": 1})
        self.assertEqual([r["model"] for r in result["runnable"] if r["agent"] == "cursor"], ["a-high"])
        self.assertEqual(frontier.parse_cursor_models("a-high - A"), [])

    def test_codex_cache_efforts_and_catalog_only_expansion(self):
        models = {"gpt-6-1-sol-medium": rating("Sol (Medium)"),
                  "gpt-6-1-sol": rating("Sol (Max)"), "cached-high": rating("Cached (High)"),
                  "cached-low": rating("Cached (Low)")}
        cache = {"models": [{"slug": "cached", "supported_reasoning_levels": [{"effort": "high"}]}]}
        delegates = [{"id": "sol", "agent": "codex", "model": "gpt-6.1-sol", "effort": "max"}]
        result = report(models, delegates=delegates, cache=cache)
        self.assertEqual([(r["model"], r["effort"]) for r in result["runnable"]],
                         [("cached", "high"), ("gpt-6.1-sol", "medium"), ("gpt-6.1-sol", "max")])

    def test_claude_by_slug_proxy_excluded_and_unrated_families_collapsed(self):
        delegates = [{"id": "haiku", "agent": "claude-subagent", "model": "haiku", "aa": "haiku"},
                     {"id": "auto", "agent": "cursor", "model": "auto", "aa": "haiku", "aa_proxy": True}]
        result = report({"haiku": rating("Haiku (Max)")},
                        ["auto", "glm-5.2-high", "glm-5.2-low", "glm-5.2-high-fast"], delegates)
        self.assertEqual(result["not_rated"]["cursor"], ["glm-5.2"])
        self.assertEqual(len([r for r in result["runnable"] if r["agent"] == "claude"]), 1)
        self.assertNotIn("auto", [r["model"] for r in result["runnable"]])

    def test_not_rated_requires_no_matching_row_in_the_family(self):
        models = {"shared-high": rating("Shared (High)"),
                  "gpt-6-1-sol-medium": rating("Sol (Medium)"),
                  "matched-high": rating("Matched (High)", intelligence=None)}
        cache = {"models": [{"slug": model, "supported_reasoning_levels": [
            {"effort": "high"}, {"effort": "ultra"}]} for model in ("shared", "unrated", "matched")]}
        result = report(models, ["shared-high", "shared-low", "unrated-high", "unrated-low",
                                 "matched-high", "matched-low"], cache=cache)
        self.assertEqual(result["not_rated"], {"cursor": ["unrated"], "codex": ["unrated"]})
        self.assertTrue(any(r["model"] == "shared-low" and r["aa"] is None for r in result["runnable"]))
        self.assertTrue(any(r["model"] == "shared" and r["effort"] == "ultra" and r["aa"] is None
                            for r in result["runnable"]))
        text = frontier.render(result, {"delegates": []})
        for agent in ("cursor", "codex"):
            self.assertIn(f"Not rated ({agent}): unrated\n", text)


class FrontierTest(unittest.TestCase):
    def test_units_are_independent_and_missing_task_is_allowed_on_blended(self):
        rows = [row("a", 40, 1, 10), row("b", 41, 2, 1), row("c", 39, None, 0.1),
                row("unrated", None, 0, 0)]
        self.assertEqual([r["model"] for r in frontier.pareto(rows, "cost_per_task")], ["a", "b"])
        self.assertEqual([r["model"] for r in frontier.pareto(rows, "price_blended")], ["c", "b"])

    def test_ties_and_strict_dominance(self):
        rows = [row("a", 40, 1, 1), row("b", 41, 1, 1), row("c", 41, 2, 2),
                row("identical", 41, 1, 1)]
        self.assertEqual([r["model"] for r in frontier.pareto(rows, "cost_per_task")], ["b", "identical"])

    def test_pools(self):
        for model, creator, expected in [("grok-4.7-high", "xAI", "cursor-auto"),
                                         ("cursor-grok-4.6-high", "xAI", "cursor-auto"),
                                         ("composer-2.5", "Cursor", "cursor-auto"),
                                         ("auto", None, "cursor-auto"),
                                         ("gpt-test", "OpenAI", "cursor-premium"),
                                         ("claude-test", "Anthropic", "cursor-premium"),
                                         ("gemini", "Google", "cursor-premium?"),
                                         ("unrated", None, "cursor-premium?")]:
            self.assertEqual(frontier.pool_for("cursor", model, creator), expected)
        self.assertEqual(frontier.pool_for("codex", "gpt", None), "codex")
        self.assertEqual(frontier.pool_for("claude", "haiku", None), "claude")

    def test_catalog_missing_hints_and_task_preferred_over_blended(self):
        models = {"gpt-6-1-terra" + suffix: rating("Terra") for suffix in ("", "-low", "-medium", "-high", "-xhigh", "-max")}
        models.update({"task": rating("Task", task=2, blended=1),
                       "token": rating("Token", task=None, blended=2),
                       "safe": rating("Safe", task=0.1, blended=5)})
        delegates = [{"id": s, "model": "gpt-5.6-terra" if s == "gone" else s,
                      "aa": s, "aa_proxy": s == "task"} for s in ("gone", "task", "token", "safe")]
        rows = [row("cheap", 45, 1, 1.5), row("cheaper", 41, 0.5, 3)]
        checks = frontier.catalog_checks({"delegates": delegates}, models, rows)
        by_id = {c["id"]: c for c in checks}
        self.assertEqual(by_id["gone"]["problem"], "aa-missing")
        self.assertEqual(len(by_id["gone"]["similar"]), 5)
        self.assertTrue(all("terra" in s for s in by_id["gone"]["similar"]))
        self.assertEqual(by_id["task"]["unit"], "cost_per_task")
        self.assertEqual(by_id["task"]["by"]["model"], "cheaper")
        self.assertTrue(by_id["task"]["proxy"])
        self.assertEqual(by_id["token"]["unit"], "price_blended")
        self.assertNotIn("safe", by_id)  # cheaper per token says nothing about per-task dominance

    def test_fallback_cap_exclusions_and_tiebreak(self):
        old = dict(row("gpt-6.1-sol", 48, 0.214, 1, "codex"), effort="medium")
        rows = [old, row("deepseek-max", 45, 0.262, 2), row("same-int", 45, 0.4, 1),
                row("too-expensive", 60, 0.429, 1), row("claude-high", 90, 0.1, 1, creator="Anthropic"),
                row("grok-4.7-high", 91, 0.1, 1), row("composer-2.5", 92, 0.1, 1),
                row("auto", 93, 0.1, 1), row("faster-fast", 94, 0.1, 1),
                row("no-task", 95, None, 1)]
        selection, reason = frontier.pick_fallback(rows, "gpt-6.1-sol", "medium")
        self.assertIsNone(reason)
        self.assertEqual(selection["row"]["model"], "deepseek-max")
        self.assertEqual(selection["cap"], 0.428)
        self.assertIsNone(frontier.pick_fallback(rows, "gpt-6.1-sol", "high")[0])
        self.assertIn("no eligible", frontier.pick_fallback([old], "gpt-6.1-sol", "medium")[1])
        old["cost_per_task"] = None
        self.assertIn("no AA $/task", frontier.pick_fallback(rows, "gpt-6.1-sol", "medium")[1])

    def test_old_cache_text_and_last_fallback_line(self):
        catalog = {"delegates": [{"id": "auto", "agent": "cursor", "model": "auto", "aa": "m", "aa_proxy": True}]}
        result = report({"m": rating("M (High)", task=None)}, ["m-high"], catalog["delegates"])
        text = frontier.render(result, catalog)
        self.assertIn("delegate ratings --refresh", text)
        self.assertIn("auto ~ (proxy)", text)
        self.assertTrue(text.splitlines()[-1].startswith("ask --agent both if Codex empty:"))


class DiscoveryAndCliTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        loader = importlib.machinery.SourceFileLoader("frontier_delegate_cli", str(SCRIPTS / "delegate"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        cls.cli = importlib.util.module_from_spec(spec)
        loader.exec_module(cls.cli)

    def test_codex_home_and_report_discovery_are_local_fakes(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "models_cache.json").write_text(json.dumps({"client_version": "fixture", "models": []}))
            with mock.patch.dict(os.environ, {"CODEX_HOME": tmp}), \
                 mock.patch.object(frontier.subprocess, "run", return_value=mock.Mock(stdout="Available models\na-high - A\n")) as run, \
                 mock.patch("ratings.load", return_value={"source": "fixture", "fetched_at": 1,
                            "models": {"a": rating("A (High)")}}):
                result = frontier.load_report({"delegates": []}, refresh=True)
            self.assertEqual(result["codex_cache_client_version"], "fixture")
            self.assertEqual(run.call_args.args[0], ["cursor-agent", "models"])
            self.assertEqual(result["runnable"][0]["aa"], "a")

    def test_route_frontier_json_refresh_and_no_quota_or_rank(self):
        result = report({})
        out = io.StringIO()
        with mock.patch.object(self.cli, "load_catalog", return_value={"delegates": []}), \
             mock.patch.object(frontier, "load_report", return_value=result) as load, \
             mock.patch.object(self.cli.quota, "fetch", side_effect=AssertionError("quota read")), \
             mock.patch.object(self.cli.route, "rank", side_effect=AssertionError("ranking")), \
             mock.patch.object(sys, "argv", ["delegate", "route", "--frontier", "--json", "--refresh"]), \
             mock.patch("sys.stdout", out):
            self.cli.main()
        self.assertEqual(json.loads(out.getvalue()), result)
        self.assertTrue(load.call_args.kwargs["refresh"])
        self.assertTrue({"source", "fetched_at", "runnable", "frontiers", "catalog", "fallback", "not_rated", "warnings"} <= result.keys())

    def test_route_requires_exactly_task_or_frontier(self):
        for args in (["route"], ["route", "research", "--frontier"]):
            with mock.patch.object(sys, "argv", ["delegate"] + args), mock.patch("sys.stderr", io.StringIO()):
                with self.assertRaises(SystemExit) as exc:
                    self.cli.main()
                self.assertEqual(exc.exception.code, 2)

    def test_ratings_task_column_handles_old_rows(self):
        data = rating("M")
        data.update(coding=40, tps=100, price_in=1, price_out=4)
        with mock.patch.object(self.cli, "load_catalog", return_value={"delegates": [{"id": "m", "aa": "m"}, {"id": "missing"}]}), \
             mock.patch.object(self.cli.ratings, "load", return_value={"source": "fixture", "fetched_at": 1, "models": {"m": data}}), \
             mock.patch.object(sys, "argv", ["delegate", "ratings"]), mock.patch("sys.stdout", io.StringIO()) as out:
            self.cli.main()
        self.assertIn("$/TASK", out.getvalue())
        self.assertIn("1.000", out.getvalue())


if __name__ == "__main__":
    unittest.main()

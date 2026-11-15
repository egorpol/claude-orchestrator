"""Tests for route.rank preferences and unknown pools."""
import _env  # noqa: F401  — isolate HOME/XDG/PATH before scripts/hooks
import copy
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
os.environ["DELEGATE_CATALOG_LOCAL"] = ""

import ratings  # noqa: E402
import route  # noqa: E402


def _catalog():
    return {
        "min_quota_pct": 10,
        "orchestrator_pool_penalty": 0.8,
        "prefer": {
            "implement": ["cursor-auto", "codex-sol"],
            "review": ["codex-sol", "cursor-auto"],
        },
        "delegates": [
            {
                "id": "codex-sol",
                "agent": "codex",
                "model": "gpt-6.1-sol",
                "effort": "xhigh",
                "prior": {"intelligence": 50, "coding": 50, "tps": 50, "price_blended": 5},
                "pools": ["codex-5h"],
            },
            {
                "id": "codex-astra",
                "agent": "codex",
                "model": "gpt-6-astra",
                "effort": "xhigh",
                # higher quality so score would win without prefer
                "prior": {"intelligence": 90, "coding": 90, "tps": 40, "price_blended": 20},
                "pools": ["codex-5h"],
            },
            {
                "id": "cursor-auto",
                "agent": "cursor",
                "model": "auto",
                "prior": {"intelligence": 40, "coding": 40, "tps": 80, "price_blended": 1},
                "pools": ["cursor-auto"],
            },
            {
                "id": "cursor-grok",
                "agent": "cursor",
                "model": "grok-4.7-high",
                "prior": {"intelligence": 60, "coding": 55, "tps": 70, "price_blended": 2},
                "pools": ["cursor-models"],
            },
        ],
        "tasks": {
            "implement": {
                "desc": "new features",
                "mode": "write",
                "weights": {"intelligence": 0.4, "coding": 0.5, "speed": 0.05, "price": 0.05},
            },
            "review": {
                "desc": "review",
                "mode": "read-only",
                "weights": {"intelligence": 0.45, "coding": 0.45, "speed": 0.05, "price": 0.05},
            },
        },
    }


def _pools_ok():
    return {
        "codex-5h": {"left_pct": 80, "pace": 1.0},
        "cursor-auto": {"left_pct": 70, "pace": 1.2},
    }


class TestPrefer(unittest.TestCase):
    def test_prefer_from_catalog_orders_first(self):
        cat = _catalog()
        _, rows = route.rank("implement", cat, _pools_ok(), {}, prefer=None)
        ids = [r["delegate"]["id"] for r in rows if not r["excluded"]]
        self.assertEqual(ids[:2], ["cursor-auto", "codex-sol"])
        self.assertTrue(rows[0]["preferred"])
        self.assertTrue(rows[1]["preferred"])
        # higher-scoring astra follows preferences
        self.assertIn("codex-astra", ids)
        self.assertGreater(ids.index("codex-astra"), ids.index("codex-sol"))

    def test_prefer_cli_overrides_catalog(self):
        cat = _catalog()
        _, rows = route.rank(
            "implement", cat, _pools_ok(), {}, prefer=["codex-astra", "cursor-auto"]
        )
        ids = [r["delegate"]["id"] for r in rows if not r["excluded"]]
        self.assertEqual(ids[:2], ["codex-astra", "cursor-auto"])

    def test_prefer_list_applies_to_all_tasks(self):
        cat = _catalog()
        cat["prefer"] = ["codex-astra"]
        _, rows = route.rank("review", cat, _pools_ok(), {}, prefer=None)
        self.assertEqual(rows[0]["delegate"]["id"], "codex-astra")
        self.assertTrue(rows[0]["preferred"])

    def test_render_marks_pref(self):
        cat = _catalog()
        task, rows = route.rank("implement", cat, _pools_ok(), {}, prefer=None)
        text = route.render("implement", task, rows)
        self.assertIn("cursor-auto (pref)", text)
        self.assertIn("codex-sol (pref)", text)
        self.assertNotIn("codex-astra (pref)", text)


class TestPreferExclusion(unittest.TestCase):
    def test_exhausted_preferred_stays_excluded(self):
        cat = _catalog()
        pools = {
            "codex-5h": {"left_pct": 80, "pace": 1.0},
            "cursor-auto": {"left_pct": 5, "pace": 0.2},  # below min_quota 10
        }
        _, rows = route.rank("implement", cat, pools, {}, prefer=None)
        by_id = {r["delegate"]["id"]: r for r in rows}
        self.assertTrue(by_id["cursor-auto"]["excluded"])
        self.assertTrue(by_id["cursor-auto"]["preferred"])
        # first pick must not be the exhausted preferred
        top = next(r for r in rows if not r["excluded"])
        self.assertEqual(top["delegate"]["id"], "codex-sol")
        text = route.render("implement", cat["tasks"]["implement"], rows)
        self.assertIn("cursor-auto (excluded)", text)
        self.assertNotIn("cursor-auto (pref)", text)


class TestPreferQuotaRead(unittest.TestCase):
    def test_unread_first_preference_yields_to_read_preference(self):
        for entry in (None, {"error": "reader off"}):
            with self.subTest(entry=entry):
                cat = _catalog()
                pools = _pools_ok()
                pools.pop("cursor-auto")
                if entry is not None:
                    pools["cursor-auto"] = entry
                task, rows = route.rank("implement", cat, pools, {})
                self.assertEqual(rows[0]["delegate"]["id"], "codex-sol")
                cursor = next(r for r in rows if r["delegate"]["id"] == "cursor-auto")
                self.assertFalse(cursor["excluded"])
                self.assertEqual(cursor["quota_factor"], 0.7)
                text = route.render("implement", task, rows)
                self.assertIn("cursor-auto (pref, quota ?)", text)
                self.assertIn("pick: codex-sol", text)
                self.assertIn("only when every pool was read and clears min_quota", text)

    def test_partially_read_preference_does_not_lead(self):
        for entry in (None, {"error": "unavailable"}):
            with self.subTest(entry=entry):
                cat = _catalog()
                cursor = next(d for d in cat["delegates"] if d["id"] == "cursor-auto")
                cursor["pools"].append("cursor-premium")
                pools = _pools_ok()
                if entry is not None:
                    pools["cursor-premium"] = entry
                task, rows = route.rank("implement", cat, pools, {})
                self.assertEqual(rows[0]["delegate"]["id"], "codex-sol")
                row = next(r for r in rows if r["delegate"]["id"] == "cursor-auto")
                self.assertEqual(row["left"], 70)
                self.assertFalse(row["excluded"])
                self.assertIn("cursor-auto (pref, quota ?)", route.render("implement", task, rows))

    def test_all_preferences_unread_sort_by_score(self):
        for prefer, pools in ((None, {}), (["cursor-auto", "cursor-grok"], {}),
                              (["cursor-auto", "cursor-grok"],
                               {"codex-5h": {"left_pct": 80, "pace": 1.0}})):
            with self.subTest(prefer=prefer, pools=pools):
                cat = _catalog()
                _, rows = route.rank("implement", cat, pools, {}, prefer=prefer)
                self.assertEqual(rows[0]["delegate"]["id"], "codex-astra")
                self.assertEqual([r["score"] for r in rows],
                                 sorted((r["score"] for r in rows), reverse=True))


class TestUnknownPool(unittest.TestCase):
    def test_unknown_pool_is_neutral_not_excluded(self):
        cat = _catalog()
        pools = _pools_ok()  # no cursor-models
        _, rows = route.rank("implement", cat, pools, {}, prefer=[])
        grok = next(r for r in rows if r["delegate"]["id"] == "cursor-grok")
        self.assertIsNone(grok["left"])
        self.assertFalse(grok["excluded"])
        self.assertEqual(grok["quota_factor"], 0.7)
        self.assertIn("cursor-models", grok["unknown_pools"])
        self.assertGreater(grok["score"], 0)
        text = route.render("implement", cat["tasks"]["implement"], rows)
        self.assertIn("quota ?: cursor-grok: cursor-models (unknown pool)", text)
        # QUOTA column shows ?
        self.assertRegex(text, r"cursor-grok\s+\d+\s+[\d.]+\s+.*\s+\?")


class TestRealCatalog(unittest.TestCase):
    def test_cursor_usage_reader_is_off_by_default(self):
        cat = json.loads((SCRIPTS / "catalog.json").read_text())
        self.assertIs(cat["cursor_usage_reader"], False)

    def test_catalog_has_prefer_and_grok(self):
        cat = json.loads((SCRIPTS / "catalog.json").read_text())
        self.assertEqual(cat["prefer"]["implement"], ["cursor-auto", "codex-sol"])
        ids = {d["id"] for d in cat["delegates"]}
        self.assertIn("cursor-grok", ids)
        grok = next(d for d in cat["delegates"] if d["id"] == "cursor-grok")
        self.assertEqual(grok["model"], "grok-4.7-high")
        self.assertEqual(grok["pools"], ["cursor-auto"])
        self.assertEqual(grok["aa"], "grok-4-7-high")
        self.assertIn("orchestrator_cost", cat)


class TestRoundsColumn(unittest.TestCase):
    def test_render_rounds_column_and_footnote(self):
        cat = _catalog()
        task, rows = route.rank("implement", cat, _pools_ok(), {}, prefer=[])
        for r in rows:
            r["rounds_left"] = 4 if r["delegate"]["id"] == "codex-sol" else None
            r["round_cost"] = {"codex-5h": 15.0} if r["delegate"]["id"] == "codex-sol" else None
        text = route.render("implement", task, rows)
        self.assertRegex(text, r"ROUNDS")
        self.assertRegex(text, r"codex-sol\s+\d+.*\s+4\s+")
        self.assertIn("rounds left in the delegate's tightest pool", text)
        # Rows without the new keys still render.
        for r in rows:
            r.pop("rounds_left", None)
            r.pop("round_cost", None)
        text2 = route.render("implement", task, rows)
        self.assertIn("ROUNDS", text2)
        self.assertIn("-", text2)


def _aa_fixture_models():
    pages = sorted((Path(__file__).resolve().parent / "fixtures").glob("aa-free-page-*.json"))
    with mock.patch.object(ratings, "_get", side_effect=[p.read_text(encoding="utf-8") for p in pages]):
        return ratings.from_api("invented-key")


def _shipped_catalog():
    return json.loads((SCRIPTS / "catalog.json").read_text())


def _half_quota_pools(cat):
    return {p: {"left_pct": 50.0, "pace": 1.0}
            for p in ("codex-5h", "codex-7d", "cursor-auto", "claude-5h", "claude-7d")}


class TestQuotaOnly(unittest.TestCase):
    def test_equal_quality_ignores_priors_and_retains_prefer(self):
        cat = _catalog()
        task, rows = route.rank("implement", cat, _pools_ok(), None)
        self.assertEqual([r["delegate"]["id"] for r in rows[:2]], ["cursor-auto", "codex-sol"])
        for row in rows:
            self.assertEqual(row["quality"], 1.0)
            self.assertAlmostEqual(row["score"], 100 * row["quota_factor"])
            self.assertIn("unrated", row["source"])
            self.assertTrue(all(v is None for v in row["metrics"].values()))
        self.assertIn("ranking by quota only", route.render("implement", task, rows))

    def test_quota_exclusion_subagent_penalty_and_filters_still_apply(self):
        cat = _catalog()
        cat["delegates"].append({"id": "sub", "agent": "claude-subagent", "model": "sonnet",
                                  "pools": ["claude-5h"]})
        pools = _pools_ok()
        pools["cursor-auto"]["left_pct"] = 5
        pools["claude-5h"] = {"left_pct": 100}
        _, rows = route.rank("implement", cat, pools, None)
        by_id = {r["delegate"]["id"]: r for r in rows}
        self.assertEqual(rows[0]["delegate"]["id"], "codex-sol")
        self.assertTrue(by_id["cursor-auto"]["excluded"])
        self.assertEqual(by_id["cursor-auto"]["score"], 0)
        self.assertAlmostEqual(by_id["sub"]["score"], 80)
        _, filtered = route.rank("implement", cat, pools, None, agents={"cursor"}, min_quota=0,
                                  prefer=["cursor-auto"])
        self.assertTrue(all(r["delegate"]["agent"] == "cursor" for r in filtered))
        self.assertEqual(filtered[0]["delegate"]["id"], "cursor-auto")
        self.assertFalse(filtered[0]["excluded"])

    def test_null_coding_weight_is_renormalised(self):
        cat = _shipped_catalog()
        models = _aa_fixture_models()
        pools = _half_quota_pools(cat)
        _, original = route.rank("implement", cat, pools, models, prefer=[])
        cat["tasks"]["implement"]["weights"]["coding"] = 0
        _, without = route.rank("implement", cat, pools, models, prefer=[])
        self.assertEqual([(r["delegate"]["id"], r["quality"]) for r in original],
                         [(r["delegate"]["id"], r["quality"]) for r in without])


class TestRatingsCli(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        loader = importlib.machinery.SourceFileLoader("ratings_delegate_cli", str(SCRIPTS / "delegate"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        cls.cli = importlib.util.module_from_spec(spec)
        loader.exec_module(cls.cli)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for patcher in (
                mock.patch.object(ratings, "CACHE", self.root / "aa.json"),
                mock.patch.dict(os.environ, {"ARTIFICIAL_ANALYSIS_API_KEY": "",
                                            "XDG_CONFIG_HOME": str(self.root / "config")}),
                mock.patch.object(self.cli, "load_catalog", return_value=_shipped_catalog()),
                mock.patch.object(self.cli.quota, "fetch", return_value={}),
                mock.patch.object(self.cli.quota, "pools", return_value=_half_quota_pools(None)),
                mock.patch("monitor.load_cost_groups", return_value={}),
                mock.patch("monitor.annotate_route_rows")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def invoke(self, args):
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with mock.patch.object(sys, "argv", ["delegate"] + args), \
             mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            try:
                self.cli.main()
            except SystemExit as exc:
                # Mirror the interpreter's handling of sys.exit(message).
                if isinstance(exc.code, str):
                    print(exc.code, file=sys.stderr)
                    code = 1
                else:
                    code = exc.code or 0
        return code, out.getvalue(), err.getvalue()

    def test_keyless_route_text_and_json_have_one_key_line_and_no_request(self):
        for as_json in (False, True):
            with self.subTest(json=as_json), \
                 mock.patch.object(ratings, "_get", side_effect=AssertionError("network")) as fetch:
                args = ["route", "implement", "--prefer", "codex-terra"] + (["--json"] if as_json else [])
                code, out, err = self.invoke(args)
            self.assertEqual(code, 0)
            fetch.assert_not_called()
            self.assertEqual(err.splitlines(), [ratings.missing_key_message()])
            self.assertNotIn("ARTIFICIAL_ANALYSIS_API_KEY", out)
            self.assertNotIn("Coding Index", out)
            if as_json:
                rows = json.loads(out)
                self.assertEqual(rows[0]["id"], "codex-terra")
                self.assertTrue(all(r["quality"] == 1.0 and "unrated" in r["source"] for r in rows))
                self.assertTrue(any(r["score"] > 0 for r in rows))
                self.assertEqual(set(rows[0]), {"id", "score", "quality", "quota_left", "pace", "excluded",
                                               "metrics", "source", "price_unit", "rounds_left", "round_cost", "launch"})
            else:
                self.assertIn("pick: codex-terra", out)
                self.assertIn("ranking by quota only", out)

    def test_keyless_frontier_and_ratings_exit_cleanly_without_request_or_discovery(self):
        import frontier
        for args in (["route", "--frontier"], ["ratings"], ["ratings", "--json"]):
            with self.subTest(args=args), \
                 mock.patch.object(ratings, "_get", side_effect=AssertionError("network")) as fetch, \
                 mock.patch.object(frontier, "cursor_models", side_effect=AssertionError("discovery")) as discover:
                code, out, err = self.invoke(args)
            self.assertNotEqual(code, 0)
            self.assertEqual(out, "")
            self.assertEqual(err.splitlines(), [ratings.missing_key_message()])
            self.assertIn(str(ratings.key_file()), err)
            fetch.assert_not_called()
            discover.assert_not_called()

    def test_keyed_text_attribution_coding_note_and_pure_json(self):
        fixture_dir = Path(__file__).resolve().parent / "fixtures"
        pages = [p.read_text() for p in sorted(fixture_dir.glob("aa-free-page-*.json"))]
        key = "invented-cli-secret"
        with mock.patch.dict(os.environ, {"ARTIFICIAL_ANALYSIS_API_KEY": key}), \
             mock.patch.object(ratings, "_get", side_effect=pages) as fetch:
            for args in (["ratings"], ["route", "implement"], ["ratings", "--json"],
                         ["ratings", "--grep", "Invented Sol", "--json"],
                         ["route", "implement", "--json"]):
                with self.subTest(args=args):
                    code, out, err = self.invoke(args)
                    self.assertEqual(code, 0)
                    self.assertEqual(err, "")
                    self.assertNotIn(key, out + err + ratings.CACHE.read_text())
                    if "--json" in args:
                        parsed = json.loads(out)
                        self.assertTrue(parsed)
                        self.assertNotIn(ratings.ATTRIBUTION, out)
                        self.assertNotIn("Coding Index", out)
                        if args == ["ratings", "--json"]:
                            self.assertEqual(set(parsed), {d["id"] for d in _shipped_catalog()["delegates"]})
                        if "--grep" in args:
                            self.assertEqual(set(parsed), {"gpt-6-1-sol-xhigh"})
                    else:
                        self.assertEqual(out.count(ratings.ATTRIBUTION), 1)
                        self.assertNotIn("aa*", out)
                        self.assertNotIn("coding = proxy", out)
                        if args[0] == "route":
                            notes = [line for line in out.splitlines() if "Coding Index" in line]
                            self.assertEqual(len(notes), 1)
                            self.assertIn("no longer publishes", notes[0])
                            self.assertIn("weight is spread over the other metrics", notes[0])
                        else:
                            self.assertNotIn("proxy", out)
                            for d in _shipped_catalog()["delegates"]:
                                self.assertIn(d["id"], out)
            self.assertEqual(fetch.call_count, 4)


class TestPriceUnit(unittest.TestCase):
    def test_all_priced_rows_use_cost_per_task(self):
        cat = _shipped_catalog()
        models = _aa_fixture_models()
        _, rows = route.rank("bulk-label", cat, _half_quota_pools(cat), models, prefer=[])
        self.assertTrue(rows)
        for r in rows:
            self.assertEqual(r["price_unit"], "cost_per_task")
            slug = r["delegate"].get("aa")
            self.assertEqual(r["metrics"]["price"], models[slug]["cost_per_task"])

    def test_one_priced_row_lacking_task_cost_falls_back_to_blended(self):
        cat = _shipped_catalog()
        models = copy.deepcopy(_aa_fixture_models())
        models["claude-haiku-5-5"]["cost_per_task"] = None
        # HEAD blended scores: rank with only blended available (no task costs at all).
        models_blended = copy.deepcopy(models)
        for m in models_blended.values():
            m["cost_per_task"] = None
        _, want_rows = route.rank(
            "bulk-label", cat, _half_quota_pools(cat), models_blended, prefer=[])
        want = {r["delegate"]["id"]: r["score"] for r in want_rows}
        _, rows = route.rank("bulk-label", cat, _half_quota_pools(cat), models, prefer=[])
        for r in rows:
            self.assertEqual(r["price_unit"], "price_blended")
            self.assertAlmostEqual(r["score"], want[r["delegate"]["id"]])
            slug = r["delegate"].get("aa")
            self.assertEqual(r["metrics"]["price"], models[slug]["price_blended"])

    def test_prior_with_cost_per_task_keeps_per_task_unit(self):
        cat = _shipped_catalog()
        cat["delegates"] = list(cat["delegates"]) + [{
            "id": "prior-x", "agent": "codex", "model": "x", "pools": ["codex-5h"],
            "prior": {"intelligence": 45, "coding": 40, "tps": 60,
                      "price_blended": 1.0, "cost_per_task": 0.2},
        }]
        models = _aa_fixture_models()
        _, rows = route.rank("implement", cat, _half_quota_pools(cat), models, prefer=[])
        for r in rows:
            self.assertEqual(r["price_unit"], "cost_per_task")
        prior = next(r for r in rows if r["delegate"]["id"] == "prior-x")
        self.assertEqual(prior["metrics"]["price"], 0.2)

    def test_unrated_delegate_does_not_block_per_task_unit(self):
        cat = _shipped_catalog()
        cat["delegates"] = list(cat["delegates"]) + [{
            "id": "unrated-z", "agent": "codex", "model": "z", "pools": ["codex-5h"],
        }]
        models = _aa_fixture_models()
        _, rows = route.rank("implement", cat, _half_quota_pools(cat), models, prefer=[])
        for r in rows:
            self.assertEqual(r["price_unit"], "cost_per_task")
        unrated = next(r for r in rows if r["delegate"]["id"] == "unrated-z")
        self.assertIsNone(unrated["metrics"]["price"])

    def test_agents_filter_drops_blended_only_prior(self):
        cat = _shipped_catalog()
        cat["delegates"] = list(cat["delegates"]) + [{
            "id": "prior-y", "agent": "codex", "model": "y", "pools": ["codex-5h"],
            "prior": {"intelligence": 45, "coding": 40, "tps": 60, "price_blended": 1.0},
        }]
        models = _aa_fixture_models()
        pools = _half_quota_pools(cat)
        _, mixed = route.rank("implement", cat, pools, models, prefer=[])
        for r in mixed:
            self.assertEqual(r["price_unit"], "price_blended")
        # Filter out the codex prior; remaining shipped rows all have task costs.
        _, filtered = route.rank(
            "implement", cat, pools, models, prefer=[], agents={"cursor", "claude-subagent"})
        self.assertTrue(filtered)
        self.assertFalse(any(r["delegate"]["id"] == "prior-y" for r in filtered))
        for r in filtered:
            self.assertEqual(r["price_unit"], "cost_per_task")

    def test_render_price_header_and_fallback_line(self):
        cat = _shipped_catalog()
        models = _aa_fixture_models()
        pools = _half_quota_pools(cat)
        task, rows = route.rank("bulk-label", cat, pools, models, prefer=[])
        text = route.render("bulk-label", task, rows)
        self.assertIn("$/TASK", text.splitlines()[1])
        self.assertNotIn("price: $/1M blended for the whole table", text)
        self.assertIn("price is AA cost per task", text)

        models2 = copy.deepcopy(models)
        models2["claude-haiku-5-5"]["cost_per_task"] = None
        task2, rows2 = route.rank("bulk-label", cat, pools, models2, prefer=[])
        text2 = route.render("bulk-label", task2, rows2)
        self.assertIn("$/1M", text2.splitlines()[1])
        self.assertIn(
            "price: $/1M blended for the whole table; no AA cost per task for: claude-haiku",
            text2)

        # Priors with only blended (no task cost anywhere) fall back quietly.
        task3, rows3 = route.rank("implement", _catalog(), _pools_ok(), {}, prefer=[])
        text3 = route.render("implement", task3, rows3)
        self.assertIn("$/1M", text3.splitlines()[1])
        self.assertNotIn("price: $/1M blended for the whole table", text3)

    def test_bad_prior_cost_per_task_falls_back_to_blended(self):
        pools = {"codex-5h": {"left_pct": 50.0, "pace": 1.0}}
        base = {
            "min_quota_pct": 10,
            "orchestrator_pool_penalty": 0.8,
            "tasks": {"implement": {
                "desc": "new features", "mode": "write",
                "weights": {"intelligence": 0.4, "coding": 0.5, "speed": 0.05, "price": 0.05},
            }},
        }
        valid = {
            "id": "prior-ok", "agent": "codex", "model": "ok", "pools": ["codex-5h"],
            "prior": {"intelligence": 45, "coding": 40, "tps": 60,
                      "price_blended": 2.0, "cost_per_task": 0.3},
        }
        for bad in (-0.5, "0.5", True, float("nan"), 0):
            with self.subTest(bad=bad):
                cat = dict(base)
                cat["delegates"] = [
                    valid,
                    {"id": "prior-bad", "agent": "codex", "model": "bad", "pools": ["codex-5h"],
                     "prior": {"intelligence": 45, "coding": 40, "tps": 60,
                               "price_blended": 2.0, "cost_per_task": bad}},
                ]
                task, rows = route.rank("implement", cat, pools, {}, prefer=[])
                for r in rows:
                    self.assertEqual(r["price_unit"], "price_blended")
                text = route.render("implement", task, rows)
                self.assertIn(
                    "price: $/1M blended for the whole table; no AA cost per task for: prior-bad",
                    text)

    def test_bad_only_price_blended_does_not_block_per_task_unit(self):
        cat = {
            "min_quota_pct": 10,
            "orchestrator_pool_penalty": 0.8,
            "delegates": [
                {"id": "prior-ok", "agent": "codex", "model": "ok", "pools": ["codex-5h"],
                 "prior": {"intelligence": 45, "coding": 40, "tps": 60,
                           "price_blended": 2.0, "cost_per_task": 0.3}},
                {"id": "prior-neg", "agent": "codex", "model": "neg", "pools": ["codex-5h"],
                 "prior": {"intelligence": 45, "coding": 40, "tps": 60,
                           "price_blended": -1}},
            ],
            "tasks": {"implement": {
                "desc": "new features", "mode": "write",
                "weights": {"intelligence": 0.4, "coding": 0.5, "speed": 0.05, "price": 0.05},
            }},
        }
        pools = {"codex-5h": {"left_pct": 50.0, "pace": 1.0}}
        _, rows = route.rank("implement", cat, pools, {}, prefer=[])
        for r in rows:
            self.assertEqual(r["price_unit"], "cost_per_task")
        neg = next(r for r in rows if r["delegate"]["id"] == "prior-neg")
        self.assertIsNone(neg["metrics"]["price"])


if __name__ == "__main__":
    unittest.main()

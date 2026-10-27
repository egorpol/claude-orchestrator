"""Tests for ratings.from_page against a fake leaderboard payload."""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "delegate" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import ratings  # noqa: E402


def _page(models):
    payload = "".join(json.dumps(m) for m in models)
    return "<script>self.__next_f.push(" + json.dumps([1, payload]) + ")</script>"


def _model(i, scores=None, **flat):
    m = {"slug": f"m{i}", "name": f"M{i}", "modelCreatorName": "X", "intelligenceIndex": 40 + i,
         "price1mInputTokens": 1.0, "price1mOutputTokens": 4.0, "medianOutputTokensPerSecond": 100}
    if scores is not None:
        m["scores"] = scores
    m.update(flat)
    return m


class FromPageTest(unittest.TestCase):
    def parse(self, models):
        with mock.patch.object(ratings, "_get", return_value=_page(models)):
            return ratings.from_page()

    def test_nested_scores_give_coding_proxy(self):
        models = self.parse([_model(i, {"terminalbench-4-0": 0.3, "scicode": 0.5}) for i in range(20)])
        self.assertAlmostEqual(models["m0"]["coding"], 40.0)
        self.assertTrue(models["m0"]["coding_is_proxy"])
        self.assertEqual(models["m0"]["price_blended"], 1.75)

    def test_old_flat_keys_still_read(self):
        models = self.parse([_model(i, terminalBench40=0.2, scicode=0.4) for i in range(20)])
        self.assertAlmostEqual(models["m3"]["coding"], 30.0)

    def test_cost_per_task_from_verified_page_field_only(self):
        models = self.parse([_model(i, scicode=0.4, intelligenceIndexCostPerTask=0.123) for i in range(20)])
        self.assertEqual(models["m0"]["cost_per_task"], 0.123)
        missing = self.parse([_model(i, scicode=0.4) for i in range(20)])
        self.assertIsNone(missing["m0"]["cost_per_task"])

    def test_api_never_guesses_cost_per_task(self):
        data = {"data": [{"slug": "m", "evaluations": {}, "pricing": {"intelligenceIndexCostPerTask": 7},
                          "intelligenceIndexCostPerTask": 9}]}
        with mock.patch.object(ratings, "_get", return_value=json.dumps(data)):
            self.assertIsNone(ratings.from_api("fake")["m"]["cost_per_task"])

    def test_no_coding_scores_anywhere_raises(self):
        with self.assertRaisesRegex(RuntimeError, "Terminal-Bench"):
            self.parse([_model(i, {"omniscience-accuracy": 0.5}) for i in range(20)])


class CacheTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache = Path(tmp.name) / "aa.json"
        patcher = mock.patch.object(ratings, "CACHE", self.cache)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict("os.environ", {"ARTIFICIAL_ANALYSIS_API_KEY": ""})
        env.start()
        self.addCleanup(env.stop)

    def test_schema_1_or_missing_refreshes_even_when_recent(self):
        for schema in (None, 1):
            old = {"fetched_at": time.time(), "source": "old", "models": {"old": {}}}
            if schema:
                old["schema"] = schema
            self.cache.write_text(json.dumps(old))
            with mock.patch.object(ratings, "from_page", return_value={"new": {"cost_per_task": 1}}) as fetch:
                env = ratings.load()
            fetch.assert_called_once()
            self.assertEqual(env["schema"], 2)
            self.assertIn("new", env["models"])
            self.assertEqual(json.loads(self.cache.read_text())["schema"], 2)

    def test_old_cache_remains_fallback_when_refresh_fails(self):
        old = {"fetched_at": time.time(), "source": "old", "models": {"old": {}}}
        for schema in (None, 1):
            if schema:
                old["schema"] = schema
            self.cache.write_text(json.dumps(old))
            logs = []
            with mock.patch.object(ratings, "from_page", side_effect=RuntimeError("offline")):
                self.assertEqual(ratings.load(log=logs.append), old)
            self.assertIn("using cache", logs[0])
            self.assertEqual(json.loads(self.cache.read_text()), old)

    def test_recent_schema_2_cache_does_not_refresh(self):
        env = {"schema": 2, "fetched_at": time.time(), "source": "fixture", "models": {}}
        self.cache.write_text(json.dumps(env))
        with mock.patch.object(ratings, "from_page", side_effect=AssertionError("network")):
            self.assertEqual(ratings.load(), env)

    def test_api_refresh_merges_page_task_costs_by_exact_slug_only(self):
        api = {"data": [{"slug": slug, "name": "API name", "evaluations": {
            "artificial_analysis_intelligence_index": 50,
            "artificial_analysis_coding_index": 60}} for slug in ("m0", "m-1", "m2", "api-only")]}
        page = _page([_model(i, scicode=0.4, intelligenceIndexCostPerTask=0 if i == 2 else 0.123)
                      for i in range(20)])

        def get(url, *args):
            return {ratings.API_URL: json.dumps(api), ratings.PAGE_URL: page}[url]

        logs = []
        with mock.patch.dict("os.environ", {"ARTIFICIAL_ANALYSIS_API_KEY": "fake"}), \
             mock.patch.object(ratings, "_get", side_effect=get) as fetch:
            env = ratings.load(refresh=True, log=logs.append)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(env["source"], "api")
        self.assertEqual(set(env["models"]), {"m0", "m-1", "m2", "api-only"})
        self.assertEqual(env["models"]["m0"]["cost_per_task"], 0.123)
        self.assertEqual(env["models"]["m2"]["cost_per_task"], 0)
        self.assertIsNone(env["models"]["m-1"]["cost_per_task"])
        self.assertIsNone(env["models"]["api-only"]["cost_per_task"])
        self.assertEqual(env["models"]["m0"]["name"], "API name")
        self.assertEqual(env["models"]["m0"]["intelligence"], 50)
        self.assertEqual(env["models"]["m0"]["coding"], 60)
        self.assertFalse(env["models"]["m0"]["coding_is_proxy"])
        self.assertEqual(logs, [])
        self.assertEqual(json.loads(self.cache.read_text()), env)

    def test_api_refresh_keeps_api_models_and_warns_once_if_page_fails(self):
        self.cache.write_text(json.dumps({"schema": 2, "fetched_at": 0, "source": "old",
                                          "models": {"old": {"cost_per_task": 1}}}))
        api = {"data": [{"slug": "m0", "name": "API model"}]}

        def get(url, *args):
            if url == ratings.PAGE_URL:
                raise RuntimeError("page offline")
            self.assertEqual(url, ratings.API_URL)
            return json.dumps(api)

        logs = []
        with mock.patch.dict("os.environ", {"ARTIFICIAL_ANALYSIS_API_KEY": "fake"}), \
             mock.patch.object(ratings, "_get", side_effect=get):
            env = ratings.load(refresh=True, log=logs.append)
        self.assertEqual(env["source"], "api")
        self.assertEqual(set(env["models"]), {"m0"})
        self.assertIsNone(env["models"]["m0"]["cost_per_task"])
        self.assertEqual(len(logs), 1)
        self.assertIn("warning:", logs[0])
        self.assertIn("page offline", logs[0])
        self.assertIn("keeping API models", logs[0])
        self.assertEqual(json.loads(self.cache.read_text()), env)


if __name__ == "__main__":
    unittest.main()

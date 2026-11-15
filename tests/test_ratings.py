"""Free API reading and caching against entirely invented model data."""
import _env  # noqa: F401 — isolate HOME/XDG/PATH before scripts/hooks
import io
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "delegate" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import frontier  # noqa: E402
import ratings  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _pages():
    return [p.read_text(encoding="utf-8") for p in sorted(FIXTURES.glob("aa-free-page-*.json"))]


def _page(rows=(), more=False):
    return json.dumps({"tier": "free", "pagination": {"has_more": more}, "data": list(rows)})


class FreeApiTest(unittest.TestCase):
    def models(self):
        with mock.patch.object(ratings, "_get", side_effect=_pages()):
            return ratings.from_api("invented-key")

    def test_pagination_and_documented_fields(self):
        pages = _pages()
        # has_more, rather than total_pages, controls when to stop.
        first = json.loads(pages[0])
        first["pagination"]["total_pages"] = 1
        pages[0] = json.dumps(first)
        with mock.patch.object(ratings.urllib.request, "urlopen",
                               side_effect=[io.BytesIO(p.encode()) for p in pages]) as fetch:
            models = ratings.from_api("invented-key")
        self.assertEqual(fetch.call_count, 4)
        for i, call in enumerate(fetch.call_args_list, 1):
            request = call.args[0]
            self.assertEqual(request.full_url, f"{ratings.API_URL}?page={i}")
            self.assertEqual(request.get_header("X-api-key"), "invented-key")
            self.assertEqual(request.get_header("User-agent"), ratings.UA)
            self.assertTrue(ratings.UA.startswith("claude-delegate/"))
            self.assertNotIn("Mozilla", ratings.UA)
        self.assertEqual(len(models), 8)
        sol = models["gpt-6-1-sol-xhigh"]
        self.assertEqual(sol, {
            "name": "Invented Sol (Xhigh)", "creator": "Synthetic Lab",
            "intelligence": 11, "coding": None, "coding_is_proxy": False,
            "tps": 1011.125, "price_in": 2, "price_out": 8,
            "price_blended": 3.5, "cost_per_task": 12.34567, "deprecated": False,
        })
        self.assertIsNone(models["invented-comet"]["intelligence"])
        self.assertIsNone(models["invented-comet"]["cost_per_task"])
        self.assertTrue(all(m["coding"] is None and m["coding_is_proxy"] is False
                            for m in models.values()))

    def test_page_cap_rejects_partial_results(self):
        with mock.patch.object(ratings, "_get", return_value=_page(more=True)) as fetch:
            with self.assertRaisesRegex(ratings.RatingsError, "page limit"):
                ratings.from_api("invented-key")
        self.assertEqual(fetch.call_count, ratings.MAX_PAGES)
        self.assertLessEqual(fetch.call_count, 10)

    def test_key_does_not_follow_a_redirect(self):
        # urllib copies ordinary headers to a redirect's target, whatever its host.
        with mock.patch.object(ratings.urllib.request, "urlopen", return_value=io.BytesIO(b"{}")) as fetch:
            ratings._get(f"{ratings.API_URL}?page=1", {"x-api-key": "invented-key"})
        request = fetch.call_args.args[0]
        moved = urllib.request.HTTPRedirectHandler().redirect_request(
            request, io.BytesIO(), 302, "Found", {}, "https://elsewhere.invalid/")
        self.assertEqual(request.get_header("X-api-key"), "invented-key")
        self.assertIsNone(moved.get_header("X-api-key"))
        self.assertEqual(moved.get_header("User-agent"), ratings.UA)

    def test_null_optional_fields_and_no_guessed_fields(self):
        row = {"slug": "invented-null", "evaluations": {
            "artificial_analysis_coding_index": 777}, "pricing": None, "performance": None,
            "artificial_analysis_intelligence_index_cost": {"cost_per_task": None},
            "median_output_tokens_per_second": 999, "intelligenceIndexCostPerTask": 999,
            "deprecated": True}
        with mock.patch.object(ratings, "_get", return_value=_page([row])):
            model = ratings.from_api("invented-key")[row["slug"]]
        for key in ("intelligence", "coding", "tps", "price_in", "price_out", "price_blended", "cost_per_task"):
            self.assertIsNone(model[key])
        self.assertFalse(model["coding_is_proxy"])
        self.assertFalse(model["deprecated"])

    def test_speed_falls_back_to_performance_v3(self):
        # AA nulls `performance` on 2026-12-10; `performance_v3` reports a mean instead.
        rows = [{"slug": "invented-old", "performance": {"median_output_tokens_per_second": 48.0},
                 "performance_v3": {"mean_output_tokens_per_second": 44.0}},
                {"slug": "invented-v3", "performance": None,
                 "performance_v3": {"mean_output_tokens_per_second": 44.0}}]
        with mock.patch.object(ratings, "_get", return_value=_page(rows)):
            models = ratings.from_api("invented-key")
        self.assertEqual(models["invented-old"]["tps"], 48.0)
        self.assertEqual(models["invented-v3"]["tps"], 44.0)

    def test_invalid_pagination_fails(self):
        for data in ({"data": []}, {"pagination": {"has_more": "true"}, "data": []}):
            with self.subTest(data=data), mock.patch.object(ratings, "_get", return_value=json.dumps(data)):
                with self.assertRaises((ratings.RatingsError, KeyError)):
                    ratings.from_api("invented-key")

    def test_fixture_rates_every_shipped_delegate(self):
        """Invented ratings cover each shipped slug and its own model/effort."""
        models = self.models()
        index, warnings = frontier.aa_index(models)
        self.assertEqual(warnings, [])
        shipped = json.loads((SCRIPTS / "catalog.json").read_text())["delegates"]
        for d in shipped:
            with self.subTest(delegate=d["id"]):
                self.assertIn(d["aa"], models)
                self.assertGreater(models[d["aa"]]["cost_per_task"], 0)
                if d["agent"] in ("codex", "cursor") and not d.get("aa_proxy"):
                    self.assertEqual(frontier.match_aa(d["agent"], d["model"], d.get("effort"),
                                                       models, index, {}), d["aa"])


class CacheTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache = Path(tmp.name) / "cache" / "aa.json"
        self.config = Path(tmp.name) / "config"
        self.keyfile = self.config / "claude-orchestrator" / "aa-api-key"
        for patcher in (mock.patch.object(ratings, "CACHE", self.cache),
                        mock.patch.dict(os.environ, {"ARTIFICIAL_ANALYSIS_API_KEY": "",
                                                      "XDG_CONFIG_HOME": str(self.config)})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def cache_env(self, schema=ratings.SCHEMA, age=0):
        env = {"schema": schema, "fetched_at": time.time() - age,
               "source": "api" if schema == ratings.SCHEMA else "leaderboard-page",
               "models": {"invented-cache": {"intelligence": 17}}}
        self.cache.parent.mkdir(parents=True, exist_ok=True)
        self.cache.write_text(json.dumps(env))
        return env

    def write_key(self, key="file-invented-key"):
        self.keyfile.parent.mkdir(parents=True, exist_ok=True)
        self.keyfile.write_text(f"  {key}\n")
        self.keyfile.chmod(0o600)

    def test_key_from_env_file_and_env_wins(self):
        for env_key, file_key, expected in (("env-invented-key", None, "env-invented-key"),
                                            ("", "file-invented-key", "file-invented-key"),
                                            ("env-invented-key", "file-invented-key", "env-invented-key")):
            with self.subTest(env=env_key, file=file_key):
                if file_key:
                    self.write_key(file_key)
                with mock.patch.dict(os.environ, {"ARTIFICIAL_ANALYSIS_API_KEY": env_key}), \
                     mock.patch.object(ratings, "_get", return_value=_page()) as fetch:
                    env = ratings.load(refresh=True)
                self.assertEqual(fetch.call_args.args[1], {"x-api-key": expected})
                self.assertEqual(env["schema"], ratings.SCHEMA)
                self.assertEqual(set(env), {"schema", "fetched_at", "source", "models"})
                self.assertNotIn(expected, self.cache.read_text())

    def test_default_key_path(self):
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": ""}):
            self.assertEqual(ratings.key_file(), Path.home() / ".config" / "claude-orchestrator" / "aa-api-key")

    def test_unreadable_key_file_is_named_and_offline(self):
        self.keyfile.mkdir(parents=True)
        with mock.patch.object(ratings, "_get", side_effect=AssertionError("network")) as fetch:
            with self.assertRaisesRegex(ratings.RatingsError, "Cannot read AA API key file"):
                ratings.load()
        fetch.assert_not_called()

    def test_key_in_no_output_log_error_or_cache(self):
        key = "do-not-echo-this-invented-secret"
        with mock.patch.dict(os.environ, {"ARTIFICIAL_ANALYSIS_API_KEY": key}), \
             mock.patch("sys.stdout", io.StringIO()) as out, \
             mock.patch("sys.stderr", io.StringIO()) as err:
            with mock.patch.object(ratings, "_get", side_effect=_pages()):
                ratings.load()
            self.assertNotIn(key, self.cache.read_text())
            logs = []
            for failure in (RuntimeError(key), urllib.error.HTTPError(ratings.API_URL, 429, key, {}, None)):
                with mock.patch.object(ratings, "_get", side_effect=failure):
                    ratings.load(refresh=True, log=logs.append)
                    self.cache.unlink()
                    with self.assertRaises(ratings.RatingsError) as raised:
                        ratings.load(log=logs.append)
                self.assertNotIn(key, str(raised.exception))
                self.cache_env()
            self.assertNotIn(key, "\n".join(logs) + out.getvalue() + err.getvalue() + self.cache.read_text())

    def test_old_schemas_are_neither_hits_nor_fallbacks(self):
        for schema in (None, 1, 2):
            with self.subTest(schema=schema):
                old = self.cache_env(schema=schema)
                with mock.patch.object(ratings, "_get", side_effect=AssertionError("network")) as fetch:
                    with self.assertRaisesRegex(ratings.MissingAPIKey, "ARTIFICIAL_ANALYSIS_API_KEY"):
                        ratings.load()
                    fetch.assert_not_called()
                with mock.patch.dict(os.environ, {"ARTIFICIAL_ANALYSIS_API_KEY": "invented-key"}), \
                     mock.patch.object(ratings, "_get", side_effect=RuntimeError("offline")):
                    with self.assertRaises(ratings.RatingsError):
                        ratings.load()
                self.assertEqual(json.loads(self.cache.read_text()), old)
                with mock.patch.dict(os.environ, {"ARTIFICIAL_ANALYSIS_API_KEY": "invented-key"}), \
                     mock.patch.object(ratings, "_get", return_value=_page()) as fetch:
                    env = ratings.load()
                fetch.assert_called_once()
                self.assertEqual(env["schema"], ratings.SCHEMA)
                self.assertEqual(env["models"], {})

    def test_fresh_cache_hit_with_or_without_key(self):
        env = self.cache_env()
        for key in ("", "invented-key"):
            with mock.patch.dict(os.environ, {"ARTIFICIAL_ANALYSIS_API_KEY": key}), \
                 mock.patch.object(ratings, "_get", side_effect=AssertionError("network")) as fetch:
                self.assertEqual(ratings.load(), env)
                fetch.assert_not_called()

    def test_stale_keyless_cache_warns_with_date_and_makes_no_request(self):
        env = self.cache_env(age=ratings.TTL_S + 1)
        logs = []
        with mock.patch.object(ratings, "_get", side_effect=AssertionError("network")) as fetch:
            self.assertEqual(ratings.load(log=logs.append), env)
        fetch.assert_not_called()
        self.assertEqual(len(logs), 1)
        self.assertIn(time.strftime("%Y-%m-%d %H:%M", time.localtime(env["fetched_at"])), logs[0])
        self.assertIn("using cache", logs[0])

    def test_429_and_other_failures_use_only_new_schema_cache(self):
        for failure in (urllib.error.HTTPError(ratings.API_URL, 429, "limited", {}, None),
                        urllib.error.HTTPError(ratings.API_URL, 403, "denied", {}, None),
                        OSError("offline"), ValueError("bad JSON")):
            for age in (0, ratings.TTL_S + 1):
                with self.subTest(failure=type(failure).__name__, age=age):
                    old = self.cache_env(age=age)
                    logs = []
                    with mock.patch.dict(os.environ, {"ARTIFICIAL_ANALYSIS_API_KEY": "invented-key"}), \
                         mock.patch.object(ratings, "_get", side_effect=failure) as fetch:
                        self.assertEqual(ratings.load(refresh=True, log=logs.append), old)
                    fetch.assert_called_once()
                    self.assertEqual(json.loads(self.cache.read_text()), old)
                    self.assertEqual(len(logs), 1)
                    self.assertIn("using cache from", logs[0])
                    self.assertIn(time.strftime("%Y-%m-%d %H:%M", time.localtime(old["fetched_at"])), logs[0])

    def test_missing_key_without_cache_is_actionable_and_offline(self):
        with mock.patch.object(ratings, "_get", side_effect=AssertionError("network")) as fetch:
            with self.assertRaises(ratings.MissingAPIKey) as exc:
                ratings.load()
        self.assertEqual(str(exc.exception), ratings.missing_key_message())
        self.assertIn(str(self.keyfile), str(exc.exception))
        fetch.assert_not_called()
        self.assertFalse(self.cache.exists())

    def test_successful_refresh_replaces_cache_and_second_load_hits(self):
        self.cache_env(age=ratings.TTL_S + 1)
        with mock.patch.dict(os.environ, {"ARTIFICIAL_ANALYSIS_API_KEY": "invented-key"}), \
             mock.patch.object(ratings, "_get", side_effect=_pages()) as fetch:
            env = ratings.load()
            self.assertEqual(ratings.load(), env)
        self.assertEqual(fetch.call_count, 4)
        self.assertEqual(len(env["models"]), 8)
        self.assertEqual(json.loads(self.cache.read_text()), env)


if __name__ == "__main__":
    unittest.main()

"""Tests for route.rank preferences and unknown pools."""
import json
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "delegate" / "scripts"
sys.path.insert(0, str(SCRIPTS))

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


if __name__ == "__main__":
    unittest.main()

"""Tests for ratings.from_page against a fake leaderboard payload."""
import json
import sys
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

    def test_no_coding_scores_anywhere_raises(self):
        with self.assertRaisesRegex(RuntimeError, "Terminal-Bench"):
            self.parse([_model(i, {"omniscience-accuracy": 0.5}) for i in range(20)])


if __name__ == "__main__":
    unittest.main()

"""Pure arm-guess helpers: no network, no real cursor-agent."""
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/delegate/scripts"
sys.path.insert(0, str(SCRIPTS))
import ask
import blinding
import prereg as p


class BlindingTests(unittest.TestCase):
    def test_parse_guess_prose_fence_and_rejects(self):
        self.assertEqual(blinding.parse_guess('{"guess": "B"}'), "B")
        self.assertEqual(blinding.parse_guess('I think\n```json\n{"guess": "A"}\n```\ndone'), "A")
        self.assertEqual(blinding.parse_guess('prefix {"guess": "C"} suffix'), "C")
        for bad in ("no json", '{"guess": "D"}', '{"guess": "a"}', "[]", '{"items": []}',
                    '{"guess": ["A"]}', '{"guess": {}}'):
            with self.assertRaises(ValueError):
                blinding.parse_guess(bad)

    def test_guess_packet_retries_once_then_raises(self):
        calls = []

        def fake(prompt, model, directory, timeout, confine=False):
            calls.append(prompt)
            if len(calls) == 1:
                return "not json", None, None, None
            return '{"guess": "A"}', None, None, None

        with mock.patch.object(ask, "run_cursor", side_effect=fake):
            self.assertEqual(blinding.guess_packet("PACKET"), "A")
        self.assertEqual(len(calls), 2)
        self.assertIn("Validation error:", calls[1])
        self.assertIn("PACKET", calls[0])

        def always_bad(*_a, **_k):
            return "still bad", None, None, None

        with mock.patch.object(ask, "run_cursor", side_effect=always_bad):
            with self.assertRaises(ValueError):
                blinding.guess_packet("PACKET")

    def test_read_guesses_first_row_and_text_hash(self):
        tmp = tempfile.TemporaryDirectory(prefix="guess-log-")
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "arm-guesses.jsonl"
        current = blinding.text_sha256()
        rows = [
            {"session": "s1", "text_sha256": "old", "guess": "A"},
            {"session": "s1", "text_sha256": current, "guess": "B"},
            {"session": "s1", "text_sha256": current, "guess": "C"},
            {"session": "s2", "text_sha256": current, "guess": "D"},
            {"session": "s2b", "text_sha256": current, "guess": ["A"]},
            {"session": "s2c", "text_sha256": current, "guess": {}},
            "not-json",
            {"session": "s3", "text_sha256": current, "guess": "A"},
        ]
        path.write_text("".join((r if isinstance(r, str) else json.dumps(r)) + "\n" for r in rows))
        found = blinding.read_guesses(path)
        self.assertEqual(found["s1"]["guess"], "B")
        self.assertEqual(found["s3"]["guess"], "A")
        self.assertNotIn("s2", found)
        self.assertNotIn("s2b", found)
        self.assertNotIn("s2c", found)
        self.assertEqual(blinding.read_guesses(Path(tmp.name) / "missing.jsonl"), {})

    def test_summarise_kappa_majority_and_undefined(self):
        # Hand-computed 3×3 in ARMS order (claude-only, orchestrator, vanilla):
        # true\guess  co  orch van
        # claude-only  2    1    0
        # orchestrator 0    3    1
        # vanilla      1    0    2
        pairs = ([("claude-only", "claude-only")] * 2
                 + [("claude-only", "orchestrator")]
                 + [("orchestrator", "orchestrator")] * 3
                 + [("orchestrator", "vanilla")]
                 + [("vanilla", "claude-only")]
                 + [("vanilla", "vanilla")] * 2)
        n = 10
        correct = 2 + 3 + 2
        accuracy = correct / n
        true_counts = {"claude-only": 3, "orchestrator": 4, "vanilla": 3}
        guess_counts = {"claude-only": 3, "orchestrator": 4, "vanilla": 3}
        e = sum((true_counts[a] / n) * (guess_counts[a] / n) for a in p.ARMS)
        kappa = (accuracy - e) / (1 - e)
        found = blinding.summarise(pairs)
        self.assertEqual(found["n"], n)
        self.assertEqual(found["accuracy"], accuracy)
        self.assertEqual(found["majority_accuracy"], 4 / 10)
        self.assertAlmostEqual(found["kappa"], kappa)
        self.assertEqual(found["table"]["claude-only"]["claude-only"], 2)
        self.assertEqual(found["table"]["orchestrator"]["vanilla"], 1)
        self.assertIsNone(blinding.summarise([])["kappa"])
        # e = 1 when every true and guess mass sits on one arm.
        all_same = [("claude-only", "claude-only")] * 5
        one = blinding.summarise(all_same)
        self.assertEqual(one["accuracy"], 1.0)
        self.assertIsNone(one["kappa"])
        self.assertEqual(
            hashlib.sha256(blinding.ARM_GUESS_TEXT.encode()).hexdigest(),
            blinding.text_sha256(),
        )


if __name__ == "__main__":
    unittest.main()

"""Secret masking for rater packets; patterns must stay equal to secret-guard's TOKEN_RULES."""
import importlib.util
import itertools
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/delegate/scripts"
sys.path.insert(0, str(SCRIPTS))

import masking  # noqa: E402
import rate  # noqa: E402

FAKE_KEY = "sk-" + "ant-" + "api03-" + "Ab1" * 32
KEY_HEAD = "-----" + "BEGIN RSA " + "PRIVATE " + "KEY-----"
KEY_TAIL = "-----" + "END RSA " + "PRIVATE " + "KEY-----"
KEY_BODY = "MIIEowIBAAKCAQEA" + "Qz9" * 20
# A REDACT word inside the token body: redact-then-mask would leak the tail.
REDACT_TOKEN = "sk-" + "ant-abcdcursor" + "Q" * 30


def load_secret_guard():
    path = ROOT / "hooks/secret-guard.py"
    spec = importlib.util.spec_from_file_location("secret_guard_for_masking", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _private_key_spans_reference(text):
    """Pre-linearisation scan kept as the differential oracle (may be quadratic)."""
    begin, priv, end = masking._BEGIN, masking._PRIV, masking._END
    spans = []
    start = 0
    while True:
        pos = text.find(priv, start)
        if pos < 0:
            break
        line_start = text.rfind("\n", 0, pos) + 1
        begin_at = text.find(begin, line_start, pos)
        if begin_at < 0:
            start = pos + len(priv)
            continue
        header_nl = text.find("\n", pos)
        search_from = len(text) if header_nl < 0 else header_nl + 1
        end_at = -1
        cursor = search_from
        while cursor < len(text):
            end_pos = text.find(end, cursor)
            if end_pos < 0:
                break
            line_end = text.find("\n", end_pos)
            if line_end < 0:
                line_end = len(text)
            line = text[end_pos:line_end]
            if priv in line and line.rstrip("\r").endswith("-----"):
                end_at = line_end + 1 if line_end < len(text) else line_end
                break
            cursor = end_pos + len(end)
        if end_at < 0:
            spans.append((begin_at, len(text)))
            break
        spans.append((begin_at, end_at))
        start = end_at
    return spans


class MaskingTests(unittest.TestCase):
    def test_token_patterns_match_secret_guard(self):
        guard = load_secret_guard()
        self.assertEqual(len(masking.TOKEN_RULES), len(guard.TOKEN_RULES))
        for (label_a, pat_a), (label_b, pat_b) in zip(masking.TOKEN_RULES, guard.TOKEN_RULES):
            self.assertEqual(label_a, label_b)
            self.assertEqual(pat_a.pattern, pat_b.pattern)
            self.assertEqual(pat_a.flags, pat_b.flags)

    def test_identity_when_no_hit(self):
        text = "plain handoff with no secrets at all\nsecond line\n"
        self.assertIs(masking.mask_secrets(text), text)
        self.assertEqual(masking.mask_secrets(text), text)

    def test_anthropic_key_and_unterminated_block(self):
        text = "before " + FAKE_KEY + " after"
        self.assertEqual(masking.mask_secrets(text), "before [secret] after")
        self.assertNotIn("sk-" + "ant-", masking.mask_secrets(text))
        block = KEY_HEAD + "\n" + KEY_BODY + "\n"
        out = masking.mask_secrets("head\n" + block)
        self.assertEqual(out, "head\n[secret]")
        self.assertNotIn(KEY_BODY[:8], out)

    def test_text_after_end_line_survives(self):
        text = KEY_HEAD + "\n" + KEY_BODY + "\n" + KEY_TAIL + "\nTAILWORD9 stays\n"
        out = masking.mask_secrets(text)
        self.assertIn("[secret]", out)
        self.assertIn("TAILWORD9", out)
        self.assertNotIn(KEY_BODY[:8], out)

    def test_prompt_key_at_1980_leaves_no_prefix(self):
        prompt = "a" * 1980 + FAKE_KEY
        truncated_old = rate._truncate(prompt, rate.PROMPT_LIMIT)
        self.assertTrue(truncated_old.startswith("a" * 1980))
        self.assertIn("sk-" + "ant-", truncated_old)
        masked = rate._truncate(masking.mask_secrets(prompt), rate.PROMPT_LIMIT)
        self.assertNotIn("sk-" + "ant-", masked)
        self.assertIn("[secret]", masked)

    def test_build_packet_control_and_identity(self):
        with tempfile.TemporaryDirectory() as d:
            start, end = Path(d, "start.md"), Path(d, "end.md")
            start.write_text("# Handoff\n\n" + KEY_HEAD + "\n" + KEY_BODY + "\n" + KEY_TAIL
                             + "\nTAILWORD9 stays\n", encoding="utf-8")
            end.write_text("x" * 7900 + "\n" + KEY_HEAD + "\n" + KEY_BODY * 10 + "\n",
                           encoding="utf-8")
            record = {"session": "control", "start": {"cwd": None, "handoff": str(start)},
                      "summary": {}, "rows": [], "end_handoff": str(end), "end_source": None,
                      "prompts": [{"text": "a" * 1980 + FAKE_KEY, "timestamp": "2026-11-08T10:00:00Z"},
                                  {"text": "key " + FAKE_KEY + " end",
                                   "timestamp": "2026-11-08T10:01:00Z"}]}
            packet = rate.build_packet(record)
            self.assertIn("[secret]", packet)
            self.assertNotIn("sk-" + "ant-", packet)
            self.assertNotIn("Ab1Ab1Ab1", packet)
            self.assertNotIn(KEY_BODY[:16], packet)
            self.assertIn("TAILWORD9", packet)

            clean_start = Path(d, "clean.md")
            clean_start.write_text("# clean handoff\nnext step: finish\n", encoding="utf-8")
            clean = {"session": "clean", "start": {"cwd": None, "handoff": str(clean_start)},
                     "summary": {}, "rows": [], "end_handoff": None, "end_source": None,
                     "prompts": [{"text": "do the work", "timestamp": "2026-11-08T10:00:00Z"}]}
            # No-hit packet must match redact+truncate assembly without masking side effects.
            packet = rate.build_packet(clean)
            self.assertNotIn("[secret]", packet)
            self.assertIn("do the work", packet)

    def test_mask_before_redact_keeps_token_with_redact_word_inside(self):
        leaked = rate.redact(REDACT_TOKEN)
        self.assertIn("Q" * 10, leaked)  # redact-then-mask would leave this
        with mock.patch("rate.session_commits", return_value="deadbeef " + REDACT_TOKEN):
            record = {"session": "redact-in-token", "start": {"cwd": "/tmp"}, "summary": {},
                      "rows": [], "end_handoff": None, "end_source": None,
                      "prompts": [{"text": "use " + REDACT_TOKEN + " please",
                                   "timestamp": "2026-11-08T10:00:00Z"}]}
            packet = rate.build_packet(record)
        self.assertIn("[secret]", packet)
        self.assertNotIn("Q" * 10, packet)
        self.assertNotIn("sk-" + "ant-", packet)
        self.assertNotIn("abcdcursor", packet)

    def test_private_key_spans_match_reference(self):
        cases = [
            "",
            "no keys here\n",
            KEY_HEAD + "\n" + KEY_BODY + "\n" + KEY_TAIL + "\nafter\n",
            KEY_HEAD + "\r\n" + KEY_BODY + "\r\n" + KEY_TAIL + "\r\n",
            "x" * 100 + KEY_HEAD + "\n" + KEY_BODY + "\n",  # unterminated
            KEY_HEAD + "\n" + KEY_BODY + "\n" + KEY_TAIL + "\n" + KEY_HEAD + "\n" + KEY_BODY + "\n" + KEY_TAIL,
            "-----" + "END x \n" + KEY_HEAD + "\n" + KEY_BODY + "\n" + KEY_TAIL + "\n",
            "PRIVATE " + "KEY " * 50,  # no BEGIN
            KEY_HEAD + "\n" + ("-----" + "END x ") * 20 + "\n" + KEY_TAIL + "\n",
            # PRIV before END on the terminator line must still close before AFTER.
            KEY_HEAD + "\nbody\n" + "PRIVATE " + "KEY x " + "-----" + "END PRIVATE " + "KEY-----\nAFTER\n",
        ]
        # Many END markers on one line after a real header.
        cases.append(KEY_HEAD + "\n" + ("-----" + "END x ") * 30 + KEY_TAIL + "\n")
        for text in cases:
            with self.subTest(text=text[:60]):
                self.assertEqual(masking._private_key_spans(text),
                                 _private_key_spans_reference(text))

    def test_private_key_spans_exhaustive_tokens(self):
        tokens = [masking._BEGIN, masking._PRIV, masking._END, "-----", "\n", "\r", "x"]
        prefix = masking._BEGIN + masking._PRIV + "\n"
        suffix = "\nx"
        # Bare sequences hide END-vs-unterminated (span ends at EOF either way).
        # Suffix and prefix+suffix put a matched END before trailing text.
        for length in range(0, 7):
            for combo in itertools.product(tokens, repeat=length):
                mid = "".join(combo)
                for text in (mid, mid + suffix, prefix + mid + suffix):
                    self.assertEqual(masking._private_key_spans(text),
                                     _private_key_spans_reference(text), text)
        # Positive control: with END matching disabled, framed sequences must diverge.
        differed = False
        with mock.patch.object(masking, "_end_line_match", return_value=-1):
            for length in range(0, 7):
                for combo in itertools.product(tokens, repeat=length):
                    text = prefix + "".join(combo) + suffix
                    if masking._private_key_spans(text) != _private_key_spans_reference(text):
                        differed = True
                        break
                if differed:
                    break
        self.assertTrue(differed, "framed exhaustive cases must exercise END matching")

    def test_private_key_scan_linear_on_repeated_priv(self):
        text = ("PRIVATE " + "KEY ") * (4_000_000 // 12)
        started = time.perf_counter()
        masking._private_key_spans(text)
        self.assertLess(time.perf_counter() - started, 3.0)

    def test_private_key_scan_linear_on_repeated_end(self):
        text = KEY_HEAD + "\n" + ("-----" + "END x ") * (4_000_000 // 12)
        started = time.perf_counter()
        masking._private_key_spans(text)
        self.assertLess(time.perf_counter() - started, 3.0)


if __name__ == "__main__":
    unittest.main()

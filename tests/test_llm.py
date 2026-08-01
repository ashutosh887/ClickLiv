"""Provider selection and the no-key no-op. The live-call path is exercised for real
by make decline, not faked here."""

from __future__ import annotations

import os
import unittest

from clickliv.llm import BEDROCK_MODEL, narrate, provider

KEYS = ("OPENAI_API_KEY", "AWS_BEARER_TOKEN_BEDROCK", "OPENAI_MODEL")


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.pop(k, None) for k in KEYS}

    def tearDown(self):
        for key, value in self.saved.items():
            if value is not None:
                os.environ[key] = value
            else:
                os.environ.pop(key, None)

    def test_no_key_is_a_no_op(self):
        self.assertIsNone(provider())
        self.assertEqual(narrate("anything"), (None, "none"))

    def test_openai_wins_when_both_are_set(self):
        os.environ["OPENAI_API_KEY"] = "test"
        os.environ["AWS_BEARER_TOKEN_BEDROCK"] = "test"
        os.environ["OPENAI_MODEL"] = "gpt-5.2"
        self.assertEqual(provider(), ("openai", "gpt-5.2"))

    def test_bedrock_remains_the_fallback(self):
        os.environ["AWS_BEARER_TOKEN_BEDROCK"] = "test"
        self.assertEqual(provider(), ("bedrock", BEDROCK_MODEL))


if __name__ == "__main__":
    unittest.main()

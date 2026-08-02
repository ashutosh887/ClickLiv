"""Provider selection and the no-key no-op. The live-call path is exercised for real
by make decline, not faked here."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from clickliv.llm import BEDROCK_MODEL, DEFAULT_GOOGLE_MODEL, narrate, providers

KEYS = ("GOOGLE_KEY", "GOOGLE_MODEL", "OPENAI_API_KEY", "OPENAI_MODEL",
        "AWS_BEARER_TOKEN_BEDROCK")


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
        self.assertEqual(providers(), [])
        self.assertEqual(narrate("anything"), (None, "none"))

    def test_google_wins_when_all_are_set(self):
        os.environ["GOOGLE_KEY"] = "test"
        os.environ["OPENAI_API_KEY"] = "test"
        os.environ["AWS_BEARER_TOKEN_BEDROCK"] = "test"
        self.assertEqual(providers(), [("google", DEFAULT_GOOGLE_MODEL),
                                       ("openai", "gpt-5.2"),
                                       ("bedrock", BEDROCK_MODEL)])

    def test_google_model_is_read_from_the_environment(self):
        os.environ["GOOGLE_KEY"] = "test"
        os.environ["GOOGLE_MODEL"] = "gemini-3-pro-preview"
        self.assertEqual(providers(), [("google", "gemini-3-pro-preview")])

    def test_openai_is_the_first_fallback(self):
        os.environ["OPENAI_API_KEY"] = "test"
        os.environ["OPENAI_MODEL"] = "gpt-5.2"
        os.environ["AWS_BEARER_TOKEN_BEDROCK"] = "test"
        self.assertEqual(providers(), [("openai", "gpt-5.2"),
                                       ("bedrock", BEDROCK_MODEL)])

    def test_bedrock_remains_the_fallback(self):
        os.environ["AWS_BEARER_TOKEN_BEDROCK"] = "test"
        self.assertEqual(providers(), [("bedrock", BEDROCK_MODEL)])

    def test_a_dead_provider_falls_through_to_the_next(self):
        os.environ["GOOGLE_KEY"] = "test"
        os.environ["OPENAI_API_KEY"] = "test"
        bodies = [None, {"output": [{"type": "message", "content": [
            {"type": "output_text", "text": "from the fallback"}]}]}]
        with mock.patch("clickliv.llm.post", side_effect=bodies):
            self.assertEqual(narrate("anything"), ("from the fallback", "gpt-5.2 via OpenAI"))

    def test_every_provider_dead_reads_as_no_key(self):
        os.environ["GOOGLE_KEY"] = "test"
        os.environ["AWS_BEARER_TOKEN_BEDROCK"] = "test"
        with mock.patch("clickliv.llm.post", return_value=None):
            self.assertEqual(narrate("anything"), (None, "none"))

    def test_google_text_is_read_across_parts(self):
        os.environ["GOOGLE_KEY"] = "test"
        body = {"candidates": [{"content": {"parts": [
            {"thoughtSignature": "opaque"}, {"text": "asset ended"}]}}],
            "usageMetadata": {"promptTokenCount": 9, "candidatesTokenCount": 3}}
        with mock.patch("clickliv.llm.post", return_value=body):
            text, label = narrate("anything")
        self.assertEqual(text, "asset ended")
        self.assertEqual(label, f"{DEFAULT_GOOGLE_MODEL} via Google AI Studio")


if __name__ == "__main__":
    unittest.main()

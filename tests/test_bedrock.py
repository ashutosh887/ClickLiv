"""bedrock.narrate must be a true no-op with no key set. The live-call path is
exercised for real by make decline, not faked here."""

from __future__ import annotations

import os
import unittest

from clickliv.bedrock import narrate


class NarrateNoKeyTests(unittest.TestCase):
    def setUp(self):
        self.saved = os.environ.pop("AWS_BEARER_TOKEN_BEDROCK", None)

    def tearDown(self):
        if self.saved is not None:
            os.environ["AWS_BEARER_TOKEN_BEDROCK"] = self.saved

    def test_returns_none_without_making_a_request(self):
        self.assertIsNone(narrate("anything"))


if __name__ == "__main__":
    unittest.main()

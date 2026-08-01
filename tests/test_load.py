"""Pins EXPECTED so a drift in what reconcile() diffs against is a reviewed change,
not an accident. Verified against local Docker and Cloud alike (D32)."""

from __future__ import annotations

import unittest

from clickliv.load import EXPECTED


class ExpectedCountsTests(unittest.TestCase):
    def test_matches_the_measured_tuning_data(self):
        self.assertEqual(EXPECTED, {
            "raw_rows": 905_558,
            "sessions": 10_866,
            "users": 9_618,
            "raw_content_ids": 3_357,
            "content_rows": 33_463,
            "join_orphans": 0,
        })


if __name__ == "__main__":
    unittest.main()

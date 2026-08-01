"""EXPECTED is the ground truth reconcile() diffs against; a silent edit here would
silently change what "correct" means for every future load. Pinned so that drift is
a deliberate, reviewed change, not an accident. Verified against both local Docker
and a real ClickHouse Cloud service in this session; join_orphans = 0 specifically
depends on load.reload_dictionary_everywhere actually running before raw_events is
enriched (D30/multi-replica note), not just on the input CSVs being unchanged.
"""

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

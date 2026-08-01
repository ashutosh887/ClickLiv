"""gates.compare() is pure: given two fingerprints, no live ClickHouse involved."""

from __future__ import annotations

import contextlib
import io
import unittest

from clickliv.gates import compare


class CompareTests(unittest.TestCase):
    def test_identical_fingerprints_pass(self):
        first = {"minute_occupancy": (96818, 123), "minute_deltas": (33748, 456)}
        second = dict(first)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(compare(first, second))

    def test_a_single_differing_hash_fails_the_whole_gate(self):
        first = {"minute_occupancy": (96818, 123), "minute_deltas": (33748, 456)}
        second = {"minute_occupancy": (96818, 999), "minute_deltas": (33748, 456)}
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(compare(first, second))

    def test_row_count_drift_also_fails(self):
        first = {"active_intervals": (32164, 1)}
        second = {"active_intervals": (32163, 1)}
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(compare(first, second))


if __name__ == "__main__":
    unittest.main()

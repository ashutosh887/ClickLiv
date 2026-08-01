"""held_out_day/split_day operate on plain CSV files, no ClickHouse needed."""

from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from clickliv.gate_c import DAY_MS, held_out_day, split_day

HEADER = ["video_session_id", "event_timestamp", "event_type"]


def write_csv(path: Path, rows: list[list]) -> None:
    with path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(HEADER)
        writer.writerows(rows)


class HeldOutDayTests(unittest.TestCase):
    def test_returns_the_latest_calendar_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "raw.csv"
            write_csv(path, [
                ["s1", 0, "VideoPlay"],
                ["s2", DAY_MS, "VideoPlay"],
                ["s3", 3 * DAY_MS, "VideoPlay"],
            ])
            self.assertEqual(held_out_day(path), 3)

    def test_empty_csv_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "raw.csv"
            write_csv(path, [])
            with self.assertRaises(SystemExit):
                held_out_day(path)


class SplitDayTests(unittest.TestCase):
    def test_only_rows_for_the_requested_day_are_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "raw.csv"
            out = Path(tmp) / "slice.csv"
            write_csv(src, [
                ["s1", 0, "VideoPlay"],
                ["s2", DAY_MS, "VideoPlay"],
                ["s3", DAY_MS + 5000, "VideoPlay"],
            ])
            written = split_day(src, 1, out)
            self.assertEqual(written, 2)
            with out.open(newline="") as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual({r["video_session_id"] for r in rows}, {"s2", "s3"})

    def test_a_day_with_no_events_writes_only_the_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "raw.csv"
            out = Path(tmp) / "slice.csv"
            write_csv(src, [["s1", 0, "VideoPlay"]])
            written = split_day(src, 99, out)
            self.assertEqual(written, 0)


if __name__ == "__main__":
    unittest.main()

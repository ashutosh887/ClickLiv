"""Pins EXPECTED so a drift in what reconcile() diffs against is a reviewed change,
not an accident, and pins the CSV shape rules the unseen day depends on."""

from __future__ import annotations

import gzip
import os
import tempfile
import unittest
from pathlib import Path

from clickliv.load import EXPECTED, RAW_TYPES, open_text, shape

HEADER = ("content_id,video_session_id,user_id,event_type,event,event_timestamp,"
          "platform,app_version,country,audio_language,subtitle_language,"
          "player_version,session_start_epoch")

ROW = "1,s1,u1,VideoPlay,Play,1785693600000,IPHONE,4.1.0,india,hin,OFF,2.0.1,1785693600000"


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


class ShapeTests(unittest.TestCase):
    """The unseen CSV decides the input schema, so these rules are what stop a fresh
    day from silently defaulting a column away."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.previous = os.environ.pop("CSV_RENAME", None)
        self.addCleanup(lambda: os.environ.__setitem__("CSV_RENAME", self.previous)
                        if self.previous is not None else None)

    def write(self, name: str, text: str) -> Path:
        path = Path(self.tmp.name) / name
        if name.endswith(".gz"):
            with gzip.open(path, "wt", newline="") as fh:
                fh.write(text)
        else:
            path.write_text(text)
        return path

    def test_the_tuning_header_is_read_as_is(self):
        sh = shape(self.write("a.csv", f"{HEADER}\n{ROW}\n"), RAW_TYPES)
        self.assertEqual(sh.delimiter, ",")
        self.assertFalse(sh.gzipped)
        self.assertIn("`country` String", sh.structure())

    def test_a_missing_column_fails_loudly_instead_of_defaulting(self):
        header = HEADER.replace(",country", "")
        with self.assertRaises(SystemExit) as caught:
            shape(self.write("b.csv", f"{header}\n{ROW}\n"), RAW_TYPES)
        self.assertIn("country", str(caught.exception))

    def test_a_renamed_column_is_mapped_back(self):
        os.environ["CSV_RENAME"] = "geo=country"
        sh = shape(self.write("c.csv", f"{HEADER.replace('country', 'geo')}\n{ROW}\n"),
                   RAW_TYPES)
        self.assertIn("country", sh.header)

    def test_extra_columns_are_declared_and_ignored(self):
        sh = shape(self.write("d.csv", f"ingest_ts,{HEADER}\nx,{ROW}\n"), RAW_TYPES)
        self.assertIn("`ignored_0` String", sh.structure())
        self.assertEqual(len(sh.header), 14)

    def test_a_semicolon_file_is_detected_and_the_setting_is_passed(self):
        sh = shape(self.write("e.csv", HEADER.replace(",", ";") + "\n"), RAW_TYPES)
        self.assertEqual(sh.delimiter, ";")
        self.assertEqual(sh.settings()["format_csv_delimiter"], ";")

    def test_gzip_is_read_transparently(self):
        path = self.write("f.csv.gz", f"{HEADER}\n{ROW}\n")
        sh = shape(path, RAW_TYPES)
        self.assertTrue(sh.gzipped)
        with open_text(path) as fh:
            self.assertEqual(len(fh.readlines()), 2)


if __name__ == "__main__":
    unittest.main()

"""Pins what the sealed dataset is allowed to arrive as: any container, any delimiter,
a byte order mark, CRLF, quoted commas and newlines, and a shouted or renamed header.
Every check here runs without a server."""

from __future__ import annotations

import bz2
import gzip
import io
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from clickliv import load

HEADER = ("content_id,video_session_id,user_id,event_type,event,event_timestamp,"
          "platform,app_version,country,audio_language,subtitle_language,"
          "player_version,session_start_epoch")

ROW = "1,s1,u1,VideoPlay,Play,1785693600000,IPHONE,4.1.0,india,hin,OFF,2.0.1,1785693600000"

CONTENT = "content_id,title,video_type,category\n1,opening night,live,sport\n"


def beats(session: str, start: int, count: int, step: int) -> str:
    return "".join(
        f"1,{session},u1,VideoHeartbeat,buffer-health,{start + i * step},IPHONE,4.1.0,"
        f"india,hin,OFF,2.0.1,{start}\n" for i in range(count))


class Files(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        load.UNPACKED.clear()
        self.addCleanup(load.UNPACKED.clear)
        for key in ("CSV_RENAME", "RAW_CSV", "CONTENT_CSV"):
            previous = os.environ.pop(key, None)
            if previous is not None:
                self.addCleanup(os.environ.__setitem__, key, previous)
            else:
                self.addCleanup(os.environ.pop, key, None)

    def write(self, name: str, text: str) -> Path:
        path = self.root / name
        path.write_bytes(text.encode("utf-8"))
        return path


class ContainerTests(Files):
    """Organizers ship whatever their exporter produced. Every one of these has to load
    without anyone unpacking anything by hand."""

    def payload(self) -> bytes:
        return f"{HEADER}\n{ROW}\n".encode("utf-8")

    def read_back(self, path: Path) -> list[str]:
        plain = load.unpack(path)
        with load.open_text(plain) as fh:
            return fh.read().splitlines()

    def test_plain_and_gzip_are_read_where_they_lie(self):
        plain = self.write("events.csv", f"{HEADER}\n{ROW}\n")
        self.assertEqual(load.unpack(plain), plain)
        packed = self.root / "events.csv.gz"
        packed.write_bytes(gzip.compress(self.payload()))
        self.assertEqual(load.unpack(packed), packed)
        self.assertTrue(load.shape(packed, load.RAW_TYPES).gzipped)

    def test_bzip2_is_unpacked(self):
        path = self.root / "events.csv.bz2"
        path.write_bytes(bz2.compress(self.payload()))
        self.assertEqual(len(self.read_back(path)), 2)

    def test_zip_is_unpacked_and_macos_junk_is_ignored(self):
        path = self.root / "events.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("sealed/events.csv", self.payload())
            archive.writestr("__MACOSX/._events.csv", b"\x00")
        self.assertEqual(len(self.read_back(path)), 2)

    def test_tar_gz_is_unpacked(self):
        path = self.root / "events.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            info = tarfile.TarInfo("events.csv")
            info.size = len(self.payload())
            archive.addfile(info, io.BytesIO(self.payload()))
        self.assertEqual(len(self.read_back(path)), 2)

    def test_a_gzip_inside_a_zip_still_ends_up_readable(self):
        path = self.root / "events.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("events.csv.gz", gzip.compress(self.payload()))
        self.assertEqual(len(self.read_back(path)), 2)

    def test_two_data_files_in_one_archive_stop_the_run(self):
        path = self.root / "events.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("monday.csv", self.payload())
            archive.writestr("tuesday.csv", self.payload())
        with self.assertRaises(SystemExit) as caught:
            load.unpack(path)
        self.assertIn("monday.csv", str(caught.exception))

    @unittest.skipUnless(shutil.which("zstd"), "no zstd binary on this machine")
    def test_zstd_is_unpacked(self):
        path = self.root / "events.csv.zst"
        subprocess.run([shutil.which("zstd"), "-q", "-f", "-o", str(path)],
                       input=self.payload(), check=True)
        self.assertEqual(len(self.read_back(path)), 2)


class HeaderTests(Files):
    def test_a_bom_does_not_become_part_of_the_first_column_name(self):
        sh = load.shape(self.write("a.csv", f"﻿{HEADER}\n{ROW}\n"), load.RAW_TYPES)
        self.assertEqual(sh.header[0], "content_id")

    def test_crlf_semicolons_shouting_and_a_rename_all_bind(self):
        os.environ["CSV_RENAME"] = "geo=country"
        header = HEADER.replace("country", "GEO").replace("platform", "PLATFORM")
        text = f"﻿{header};extra\r\n{ROW};x\r\n".replace(",", ";", header.count(","))
        sh = load.shape(self.write("b.csv", text), load.RAW_TYPES)
        self.assertEqual(sh.delimiter, ";")
        for name in load.RAW_TYPES:
            self.assertIn(name, sh.header)
        self.assertIn("`ignored_13` String", sh.structure(load.RAW_TYPES))

    def test_quoted_commas_and_newlines_stay_inside_one_row(self):
        row = ROW.replace("opening", "x") + ""
        text = (f"{HEADER},note\n"
                f'{row},"free text, with a comma\nand a second line"\n')
        sh = load.shape(self.write("c.csv", text), load.RAW_TYPES)
        self.assertEqual(len(list(load.data_rows(sh))), 1)


class CadenceTests(unittest.TestCase):
    """The heartbeat cadence against GRACE_SECONDS. A slower cadence than the grace puts
    a hole in every session and collapses the peak into a plausible wrong number."""

    def measure(self, step_ms: int, grace: str) -> tuple[list, list]:
        previous = os.environ.get("GRACE_SECONDS")
        os.environ["GRACE_SECONDS"] = grace
        problems, warnings = [], []
        try:
            load.cadence_check({"histogram": {step_ms: 500}, "sampled_sessions": 10,
                                "cadence_basis": load.HEARTBEAT}, problems, warnings)
        finally:
            os.environ.pop("GRACE_SECONDS")
            if previous is not None:
                os.environ["GRACE_SECONDS"] = previous
        return problems, warnings

    def test_forty_second_beats_pass_a_forty_second_grace(self):
        problems, _ = self.measure(40_000, "40")
        self.assertEqual(problems, [])

    def test_sixty_second_beats_fail_a_forty_second_grace(self):
        problems, _ = self.measure(60_000, "40")
        self.assertEqual(len(problems), 1)
        self.assertIn("make sweep", problems[0])

    def test_a_grace_far_above_the_cadence_warns(self):
        problems, warnings = self.measure(5_000, "40")
        self.assertEqual(problems, [])
        self.assertTrue(any("far above" in note for note in warnings))


class PreflightTests(Files):
    """Nothing here touches a server: preflight is what runs while the demo is still up."""

    def prepare(self, events: str, content: str = CONTENT) -> None:
        os.environ["RAW_CSV"] = str(self.write("events.csv", events))
        os.environ["CONTENT_CSV"] = str(self.write("content.csv", content))

    def test_a_clean_day_passes(self):
        self.prepare(f"{HEADER}\n{ROW}\n" + beats("s1", 1785693600000, 30, 40_000))
        self.assertTrue(load.preflight())

    def test_seconds_where_milliseconds_belong_fail(self):
        self.prepare(f"{HEADER}\n{ROW.replace('1785693600000', '1785693600')}\n")
        self.assertFalse(load.preflight())

    def test_an_unknown_content_id_fails_before_anything_is_dropped(self):
        self.prepare(f"{HEADER}\n{ROW.replace('1,s1', '999,s1')}\n"
                     + beats("s2", 1785693600000, 30, 40_000).replace("1,s2", "999,s2"))
        self.assertFalse(load.preflight())

    def test_a_renamed_event_vocabulary_fails(self):
        rows = beats("s1", 1785693600000, 30, 40_000).replace("VideoHeartbeat", "Beat")
        self.prepare(f"{HEADER}\n" + rows)
        self.assertFalse(load.preflight())

    def test_a_missing_benchmark_dimension_only_warns(self):
        rows = (f"{HEADER}\n{ROW.replace('IPHONE', 'HOLOLENS')}\n"
                + beats("s1", 1785693600000, 30, 40_000).replace("IPHONE", "HOLOLENS"))
        self.prepare(rows)
        self.assertTrue(load.preflight())


if __name__ == "__main__":
    unittest.main()

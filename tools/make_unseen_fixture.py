"""Generate an adversarial 'fresh day' in the sealed dataset's schema, carrying every
case the tuning data never shows, and the same day again in every container and CSV
quirk the organizers might ship. Deterministic: same bytes on every machine."""

from __future__ import annotations

import bz2
import csv
import gzip
import io
import random
import shutil
import subprocess
import sys
import tarfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVENTS_OUT = ROOT / "fixtures" / "unseen_events.csv"
CONTENT_OUT = ROOT / "fixtures" / "unseen_content.csv"

HEADER = ["content_id", "video_session_id", "user_id", "event_type", "event",
          "event_timestamp", "platform", "app_version", "country", "audio_language",
          "subtitle_language", "player_version", "session_start_epoch"]

DAY_START = int(datetime(2026, 8, 2, 18, 0, tzinfo=timezone.utc).timestamp() * 1000)
DAY_END = DAY_START + 45 * 60_000
HEARTBEAT_MS = 40_000

PLATFORMS = ["ANDROID_PHONE", "IPHONE", "SONY_ANDROID_TV", "Mweb", "FIRE_TV",
             "VISION_PRO"]
COUNTRIES = ["india", "india", "india", "singapore", "unknown"]
LANGUAGES = ["hin", "eng", "tam"]
SUBTITLES = ["OFF", "unk", "eng"]
BEATS = ["network-activity", "buffer-health", "BufferStart", "BufferEnd", "Seek",
         "video-resize", "upshift"]

CONTENT = [
    (90000001, "opening night", "live", "sport"),
    (90000002, "second innings", "live", "sport"),
    (90000003, "long drama ep1", "vod", "drama"),
    (90000004, "long drama ep2", "vod", "drama"),
    (90000005, "clip reel", "shortform", "clips"),
    (-1, "rejected row", "vod", "none"),
]

CONTENT_IDS = [row[0] for row in CONTENT if row[0] > 0]


class Session:
    """One session's rows, built by appending events in logical order."""

    def __init__(self, name: str, ordinal: int, rng: random.Random, start: int):
        self.sid = name
        self.user = f"user-{rng.randrange(1, 40):03d}"
        self.content = CONTENT_IDS[ordinal % len(CONTENT_IDS)]
        self.platform = PLATFORMS[ordinal % len(PLATFORMS)]
        self.country = COUNTRIES[ordinal % len(COUNTRIES)]
        self.audio = LANGUAGES[ordinal % len(LANGUAGES)]
        self.subtitle = SUBTITLES[ordinal % len(SUBTITLES)]
        self.start = start
        self.rows: list[list] = []

    def add(self, ts: int, event_type: str, event: str) -> None:
        self.rows.append([self.content, self.sid, self.user, event_type, event, ts,
                          self.platform, "4.1.0", self.country, self.audio,
                          self.subtitle, "2.0.1", self.start])

    def beats(self, rng: random.Random, first: int, last: int) -> int:
        """A quarter of the rows repeat the millisecond before them, the way a quarter of
        the tuning data's heartbeats do. Intervals take min and max per segment, so a
        repeat must not move a single answer."""
        ts = first
        while ts <= last:
            self.add(ts, "VideoHeartbeat", rng.choice(BEATS))
            if rng.random() < 0.25:
                self.add(ts, "VideoHeartbeat", rng.choice(BEATS))
            ts += HEARTBEAT_MS
        return ts - HEARTBEAT_MS


def plain(session: Session, rng: random.Random, minutes: int) -> None:
    session.add(session.start, "VideoSessionStart", "VideoSessionStart")
    session.add(session.start + 1_500, "VideoPlay", "Play")
    last = session.beats(rng, session.start + HEARTBEAT_MS,
                         session.start + minutes * 60_000)
    session.add(last + 5_000, "VideoSessionEnd", "VideoSessionEnd")


def open_at_end(session: Session, rng: random.Random) -> None:
    """Still playing when the file runs out. The batch sessionizer closes it on grace
    alone, and the incremental path is what absorbs its next heartbeat."""
    session.add(session.start, "VideoSessionStart", "VideoSessionStart")
    session.add(session.start + 1_500, "VideoPlay", "Play")
    session.beats(rng, session.start + HEARTBEAT_MS, DAY_END)


def background_never_returns(session: Session, rng: random.Random) -> None:
    """AppBackgrounded with no AppForegrounded, and heartbeats that keep arriving after
    it. Foreground-only means none of that tail counts."""
    session.add(session.start, "VideoSessionStart", "VideoSessionStart")
    session.add(session.start + 1_500, "VideoPlay", "Play")
    session.beats(rng, session.start + HEARTBEAT_MS, session.start + 300_000)
    session.add(session.start + 320_000, "AppBackgrounded", "AppBackgrounded")
    session.beats(rng, session.start + 360_000, session.start + 720_000)


def duplicate_start(session: Session, rng: random.Random) -> None:
    session.add(session.start, "VideoSessionStart", "VideoSessionStart")
    session.add(session.start, "VideoSessionStart", "VideoSessionStart")
    session.add(session.start + 1_500, "VideoPlay", "Play")
    last = session.beats(rng, session.start + HEARTBEAT_MS, session.start + 480_000)
    session.add(last + 4_000, "VideoSessionEnd", "VideoSessionEnd")


def heartbeat_gap(session: Session, rng: random.Random) -> None:
    """Six silent minutes, far past the 90 second gap threshold. Those minutes must not
    be billed as watched."""
    session.add(session.start, "VideoSessionStart", "VideoSessionStart")
    session.add(session.start + 1_500, "VideoPlay", "Play")
    session.beats(rng, session.start + HEARTBEAT_MS, session.start + 240_000)
    resume = session.start + 600_000
    session.add(resume, "VideoHeartbeat", "resume")
    last = session.beats(rng, resume + HEARTBEAT_MS, resume + 240_000)
    session.add(last + 3_000, "VideoSessionEnd", "VideoSessionEnd")


def pause_resume(session: Session, rng: random.Random) -> None:
    session.add(session.start, "VideoSessionStart", "VideoSessionStart")
    session.add(session.start + 1_500, "VideoPlay", "Play")
    session.beats(rng, session.start + HEARTBEAT_MS, session.start + 200_000)
    session.add(session.start + 220_000, "VideoHeartbeat", "pause")
    session.add(session.start + 280_000, "VideoHeartbeat", "resume")
    last = session.beats(rng, session.start + 320_000, session.start + 600_000)
    session.add(last + 2_000, "VideoSessionEnd", "VideoSessionEnd")


def switches_content(session: Session, rng: random.Random) -> None:
    """One session, two content ids. The rollup takes the tuple in effect at the end of
    each minute rather than assuming a session watches one thing."""
    session.add(session.start, "VideoSessionStart", "VideoSessionStart")
    session.add(session.start + 1_500, "VideoPlay", "Play")
    last = session.beats(rng, session.start + HEARTBEAT_MS, session.start + 300_000)
    session.content = CONTENT_IDS[(CONTENT_IDS.index(session.content) + 1) % len(CONTENT_IDS)]
    session.add(last + 5_000, "VideoPlay", "Play")
    last = session.beats(rng, last + HEARTBEAT_MS + 5_000, last + 300_000)
    session.add(last + 3_000, "VideoSessionEnd", "VideoSessionEnd")


def errored(session: Session, rng: random.Random) -> None:
    session.add(session.start, "VideoSessionStart", "VideoSessionStart")
    session.add(session.start + 1_500, "VideoPlay", "Play")
    last = session.beats(rng, session.start + HEARTBEAT_MS, session.start + 160_000)
    session.add(last + 6_000, "VideoError", "VideoError")


SHAPES = (
    ("plain", plain, 12),
    ("open", open_at_end, 4),
    ("bgonly", background_never_returns, 2),
    ("dupstart", duplicate_start, 2),
    ("gap", heartbeat_gap, 2),
    ("pause", pause_resume, 2),
    ("switch", switches_content, 2),
    ("error", errored, 1),
)


def build(rng: random.Random) -> list[list]:
    rows: list[list] = []
    ordinal = 0
    for name, shape, count in SHAPES:
        for index in range(1, count + 1):
            offset = rng.randrange(0, 18) * 60_000
            start = DAY_START + offset
            session = Session(f"{name}-{index:02d}", ordinal, rng, start)
            ordinal += 1
            if shape is plain:
                shape(session, rng, rng.randrange(6, 22))
            else:
                shape(session, rng)
            rows.extend(session.rows)

    rows.sort(key=lambda r: r[HEADER.index("event_timestamp")])
    late = [rows.pop(rng.randrange(0, len(rows))) for _ in range(len(rows) // 20)]
    return rows + late


CONTENT_HEADER = ["content_id", "title", "video_type", "category"]

SHUFFLED = ["event_timestamp", "video_session_id", "user_id", "content_id", "event_type",
            "event", "platform", "country", "app_version", "audio_language",
            "subtitle_language", "player_version", "session_start_epoch"]

RESPELLED = {"country": "geo", "platform": "PLATFORM", "event_type": "Event_Type"}

NOTE = 'free text, with a comma\nand a second line'


def encode(header: list, rows: list[list], delimiter: str = ",",
           terminator: str = "\n", bom: bool = False) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=delimiter, lineterminator=terminator)
    writer.writerow(header)
    writer.writerows(rows)
    return (("﻿" if bom else "") + buf.getvalue()).encode("utf-8")


def respelled_events(rows: list[list]) -> tuple[list, list[list]]:
    """Columns reordered, two we do not use bolted on, country renamed, two names
    shouted, and a quoted field carrying both a comma and a newline."""
    index = [HEADER.index(name) for name in SHUFFLED]
    header = ["ingest_ts"] + [RESPELLED.get(name, name) for name in SHUFFLED] + ["note"]
    return header, [["2026-08-02T18:00:00Z"] + [row[i] for i in index] + [NOTE]
                    for row in rows]


def respelled_content(rows: list[tuple]) -> tuple[list, list[list]]:
    header = ["content_id", "TITLE", "video_type", "category", "extra_flag"]
    return header, [[cid, f'{title}, part "one"\nand a wrapped line', kind, category, "y"]
                    for cid, title, kind, category in rows]


def write_zip(path: Path, member: str, payload: bytes) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member, payload)
        archive.writestr(f"__MACOSX/._{Path(member).name}", b"\x00\x00")


def write_tar(path: Path, member: str, payload: bytes) -> None:
    with tarfile.open(path, "w:gz") as archive:
        info = tarfile.TarInfo(member)
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))


def write_zstd(path: Path, payload: bytes) -> bool:
    tool = shutil.which("zstd")
    if not tool:
        return False
    subprocess.run([tool, "-q", "-f", "-o", str(path)], input=payload, check=True)
    return True


def variants(directory: Path, rows: list[list]) -> list[Path]:
    """The same day again, in every container and every CSV quirk we might be handed."""
    directory.mkdir(parents=True, exist_ok=True)
    events = encode(HEADER, rows)
    content = encode(CONTENT_HEADER, CONTENT)
    header, mangled = respelled_events(rows)
    quirky_events = encode(header, mangled, delimiter=";", terminator="\r\n", bom=True)
    header, mangled = respelled_content(CONTENT)
    quirky_content = encode(header, mangled, delimiter=";", terminator="\r\n", bom=True)

    written = []
    for name, payload in (("events", events), ("content", content)):
        (directory / f"{name}.csv").write_bytes(payload)
        (directory / f"{name}.csv.gz").write_bytes(gzip.compress(payload))
        (directory / f"{name}.csv.bz2").write_bytes(bz2.compress(payload))
        write_zip(directory / f"{name}.zip", f"sealed/{name}.csv", payload)
        write_tar(directory / f"{name}.tar.gz", f"{name}.csv", payload)
        written += [directory / f"{name}.csv", directory / f"{name}.csv.gz",
                    directory / f"{name}.csv.bz2", directory / f"{name}.zip",
                    directory / f"{name}.tar.gz"]
        if write_zstd(directory / f"{name}.csv.zst", payload):
            written.append(directory / f"{name}.csv.zst")

    for name, payload in (("events", quirky_events), ("content", quirky_content)):
        (directory / f"quirky_{name}.csv").write_bytes(payload)
        write_zip(directory / f"quirky_{name}.zip", f"{name}.csv", payload)
        written += [directory / f"quirky_{name}.csv", directory / f"quirky_{name}.zip"]
    return written


def main() -> int:
    rng = random.Random(20260802)
    rows = build(rng)
    EVENTS_OUT.parent.mkdir(parents=True, exist_ok=True)

    with EVENTS_OUT.open("w", newline="") as fh:
        writer = csv.writer(fh, quoting=csv.QUOTE_NONNUMERIC)
        writer.writerow(HEADER)
        writer.writerows(rows)

    with CONTENT_OUT.open("w", newline="") as fh:
        writer = csv.writer(fh, quoting=csv.QUOTE_NONNUMERIC)
        writer.writerow(CONTENT_HEADER)
        writer.writerows(CONTENT)

    ts = HEADER.index("event_timestamp")
    sessions = {r[HEADER.index("video_session_id")] for r in rows}
    out_of_order = sum(1 for a, b in zip(rows, rows[1:]) if b[ts] < a[ts])
    print(f"{EVENTS_OUT.relative_to(ROOT)}   {len(rows):,} rows, {len(sessions)} sessions")
    print(f"{CONTENT_OUT.relative_to(ROOT)}  {len(CONTENT)} rows, one with a negative id")
    print(f"span      {datetime.fromtimestamp(min(r[ts] for r in rows) / 1000, timezone.utc)}"
          f" to {datetime.fromtimestamp(max(r[ts] for r in rows) / 1000, timezone.utc)}")
    print(f"countries {sorted({r[HEADER.index('country')] for r in rows})}")
    print(f"platforms {sorted({r[HEADER.index('platform')] for r in rows})}")
    print(f"out of timestamp order: {out_of_order} row transitions")

    if "--variants" in sys.argv:
        index = sys.argv.index("--variants") + 1
        directory = Path(sys.argv[index]) if index < len(sys.argv) else ROOT / "fixtures/variants"
        print()
        for path in variants(directory, rows):
            print(f"{str(path):<52}{path.stat().st_size:>10,} bytes")
        print(f"\nthe quirky pair is semicolon delimited, CRLF, byte order marked, "
              f"reordered, carries two columns we ignore and quoted fields holding a "
              f"comma and a newline, and calls country geo:")
        print(f"  make unseen RAW={directory}/quirky_events.zip "
              f"CONTENT={directory}/quirky_content.zip \\\n"
              f"              CSV_RENAME=geo=country OUT=/tmp/unseen-quirky DB=clickliv_quirky")
    return 0


if __name__ == "__main__":
    sys.exit(main())

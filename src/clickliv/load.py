"""CSV ingestion. Content must land before events (D12), and that is asserted, not assumed."""

from __future__ import annotations

import os
import time
from pathlib import Path

from .ch import ClickHouse, ClickHouseError

CONTENT_STRUCTURE = "content_id Int64, title String, video_type String, category String"

RAW_STRUCTURE = (
    "content_id UInt64, video_session_id String, user_id String, event_type String, "
    "event String, event_timestamp Int64, platform String, app_version String, "
    "country String, audio_language String, subtitle_language String, "
    "player_version String, session_start_epoch Int64"
)

CONTENT_INSERT = f"""
INSERT INTO content_meta
SELECT toUInt64(content_id), title, video_type, category
FROM input('{CONTENT_STRUCTURE}')
WHERE content_id >= 0
FORMAT CSVWithNames
"""

RAW_INSERT = f"""
INSERT INTO raw_events
SELECT
    video_session_id,
    fromUnixTimestamp64Milli(event_timestamp, 'UTC'),
    user_id,
    content_id,
    event_type,
    event,
    platform,
    app_version,
    country,
    audio_language,
    subtitle_language,
    player_version,
    fromUnixTimestamp64Milli(session_start_epoch, 'UTC')
FROM input('{RAW_STRUCTURE}')
FORMAT CSVWithNames
"""

INSERT_SETTINGS = {
    "input_format_parallel_parsing": 1,
    "max_insert_block_size": 1_000_000,
    "min_insert_block_size_rows": 1_000_000,
    "date_time_input_format": "best_effort",
}

EXPECTED = {
    "raw_rows": 905_558,
    "sessions": 10_866,
    "users": 9_618,
    "raw_content_ids": 3_357,
    "content_rows": 33_463,
    "join_orphans": 0,
}


def content_csv() -> Path:
    return Path(os.environ.get("CONTENT_CSV", "data/ch-hackathon-content-data.csv"))


def raw_csv() -> Path:
    return Path(os.environ.get("RAW_CSV", "data/ch-hackathon-raw-data.csv"))


def load(ch: ClickHouse) -> None:
    for path in (content_csv(), raw_csv()):
        if not path.exists():
            raise SystemExit(f"missing input: {path}")

    ch.command("TRUNCATE TABLE content_meta")
    ch.command("TRUNCATE TABLE raw_events")

    t0 = time.time()
    ch.insert_csv(CONTENT_INSERT, content_csv(), settings=INSERT_SETTINGS)
    n_content = int(ch.scalar("SELECT count() FROM content_meta"))
    print(f"content_meta  {n_content:>9,} rows  {time.time() - t0:5.1f}s")

    with content_csv().open("rb") as fh:
        source_rows = sum(1 for _ in fh) - 1
    if source_rows != n_content:
        print(f"  rejected {source_rows - n_content} row(s) with a negative content_id")

    if n_content == 0:
        raise ClickHouseError(
            "content_meta is empty. Loading events now would enrich against an empty "
            "dictionary and silently produce unlabelled rows. See D12."
        )
    ch.command("SYSTEM RELOAD DICTIONARY content_dict")

    t1 = time.time()
    ch.insert_csv(RAW_INSERT, raw_csv(), settings=INSERT_SETTINGS)
    n_raw = int(ch.scalar("SELECT count() FROM raw_events"))
    print(f"raw_events    {n_raw:>9,} rows  {time.time() - t1:5.1f}s")


def reconcile(ch: ClickHouse) -> bool:
    """Diff what landed against the measured tuning CSVs. A mismatch means the input changed."""
    actual = ch.query("""
    SELECT
        (SELECT count() FROM raw_events)                                    AS raw_rows,
        (SELECT uniqExact(video_session_id) FROM raw_events)                AS sessions,
        (SELECT uniqExact(user_id) FROM raw_events)                         AS users,
        (SELECT uniqExact(content_id) FROM raw_events)                      AS raw_content_ids,
        (SELECT count() FROM content_meta)                                  AS content_rows,
        (SELECT count() FROM (
            SELECT DISTINCT content_id FROM raw_events
            WHERE NOT dictHas('content_dict', content_id)))                 AS join_orphans
    """).dicts()[0]

    ok = True
    print(f"\n{'check':<18}{'measured':>12}{'FINDINGS.md':>14}")
    for key, want in EXPECTED.items():
        got = int(actual[key])
        flag = "" if got == want else "  MISMATCH"
        ok &= got == want
        print(f"{key:<18}{got:>12,}{want:>14,}{flag}")
    return ok

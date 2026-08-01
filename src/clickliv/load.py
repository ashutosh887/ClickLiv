"""CSV ingestion. Content must land before events (D12), and that is asserted, not assumed."""

from __future__ import annotations

import os
import time
from pathlib import Path

from . import otel
from .ch import ClickHouse, ClickHouseError

CONTENT_STRUCTURE = "content_id Int64, title String, video_type String, category String"

RAW_STRUCTURE = (
    "content_id UInt64, video_session_id String, user_id String, event_type String, "
    "event String, event_timestamp Int64, platform String, app_version String, "
    "country String, audio_language String, subtitle_language String, "
    "player_version String, session_start_epoch Int64"
)

CONTENT_PROJECTION = "toUInt64(content_id), title, video_type, category"

RAW_PROJECTION = """
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
"""


def content_insert(source: str) -> str:
    return (f"INSERT INTO content_meta SELECT {CONTENT_PROJECTION} "
            f"FROM {source} WHERE content_id >= 0")


def raw_insert(source: str) -> str:
    return f"INSERT INTO raw_events SELECT {RAW_PROJECTION} FROM {source}"


CONTENT_INSERT = content_insert(f"input('{CONTENT_STRUCTURE}')") + "\nFORMAT CSVWithNames"

RAW_INSERT = raw_insert(f"input('{RAW_STRUCTURE}')") + "\nFORMAT CSVWithNames"

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


def ingest(ch: ClickHouse, table: str, statement: str, path: Path) -> int:
    """One traced insert. Visible lag is the delay from acknowledgement to the rows being queryable."""
    with otel.span(f"ingest.{table}", **{"ingest.source": path.name,
                                         "ingest.bytes": path.stat().st_size}) as span:
        started = time.time()
        ch.insert_csv(statement, path, settings=INSERT_SETTINGS)
        acknowledged = time.time()
        rows = int(ch.scalar(f"SELECT count() FROM {table}"))
        otel.note(span, **{
            "ingest.rows": rows,
            "ingest.duration_ms": round((acknowledged - started) * 1000, 1),
            "ingest.visible_lag_ms": round((time.time() - acknowledged) * 1000, 1),
        })
    print(f"{table:<14}{rows:>9,} rows  {acknowledged - started:5.1f}s")
    return rows


def load(ch: ClickHouse) -> None:
    for path in (content_csv(), raw_csv()):
        if not path.exists():
            raise SystemExit(f"missing input: {path}")

    ch.command("TRUNCATE TABLE content_meta")
    ch.command("TRUNCATE TABLE raw_events")

    n_content = ingest(ch, "content_meta", CONTENT_INSERT, content_csv())

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
    ingest(ch, "raw_events", RAW_INSERT, raw_csv())


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

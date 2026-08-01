"""CSV ingestion. Content must land before events (D12), and that is asserted, not assumed.
The header of the file in hand decides the input schema, so a fresh day cannot silently
default a column away. See docs/unseen-day.md."""

from __future__ import annotations

import csv
import gzip
import os
import time
from dataclasses import dataclass
from pathlib import Path

from . import otel
from .ch import ClickHouse, ClickHouseError

CONTENT_TYPES = {
    "content_id": "Int64", "title": "String",
    "video_type": "String", "category": "String",
}

RAW_TYPES = {
    "content_id": "UInt64", "video_session_id": "String", "user_id": "String",
    "event_type": "String", "event": "String", "event_timestamp": "Int64",
    "platform": "String", "app_version": "String", "country": "String",
    "audio_language": "String", "subtitle_language": "String",
    "player_version": "String", "session_start_epoch": "Int64",
}

CONTENT_STRUCTURE = ", ".join(f"{n} {t}" for n, t in CONTENT_TYPES.items())

RAW_STRUCTURE = ", ".join(f"{n} {t}" for n, t in RAW_TYPES.items())

DELIMITERS = (",", "\t", ";", "|")

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


INSERT_SETTINGS = {
    "input_format_parallel_parsing": 1,
    "max_insert_block_size": 1_000_000,
    "min_insert_block_size_rows": 1_000_000,
    "date_time_input_format": "best_effort",
    "input_format_with_names_use_header": 0,
}

EXPECTED = {
    "raw_rows": 905_558,
    "sessions": 10_866,
    "users": 9_618,
    "raw_content_ids": 3_357,
    "content_rows": 33_463,
    "join_orphans": 0,
}

INVARIANTS = ("join_orphans",)


def content_csv() -> Path:
    return Path(os.environ.get("CONTENT_CSV", "data/ch-hackathon-content-data.csv"))


def raw_csv() -> Path:
    return Path(os.environ.get("RAW_CSV", "data/ch-hackathon-raw-data.csv"))


@dataclass(frozen=True)
class Shape:
    """What a CSV actually looks like: header after renaming, delimiter, compression."""

    path: Path
    header: tuple[str, ...]
    delimiter: str
    gzipped: bool

    def structure(self, types: dict[str, str]) -> str:
        return ", ".join(
            f"`{name}` {types[name]}" if name in types else f"`ignored_{i}` String"
            for i, name in enumerate(self.header))

    def settings(self) -> dict:
        extra = {} if self.delimiter == "," else {"format_csv_delimiter": self.delimiter}
        return {**INSERT_SETTINGS, **extra}

    def describe(self, types: dict[str, str]) -> str:
        extra = [name for name in self.header if name not in types]
        return (f"{self.path.name:<34}{len(self.header)} columns"
                f"{'' if self.delimiter == ',' else f', delimiter {self.delimiter!r}'}"
                f"{', gzip' if self.gzipped else ''}"
                f"{'' if not extra else f', ignoring {len(extra)} extra: ' + ', '.join(extra)}")


def renames() -> dict[str, str]:
    pairs = [p.strip() for p in os.environ.get("CSV_RENAME", "").split(",") if p.strip()]
    return dict(p.split("=", 1) for p in pairs)


def open_text(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rt", newline="", encoding="utf-8")
    return path.open(newline="", encoding="utf-8")


def shape(path: Path, types: dict[str, str]) -> Shape:
    """Read the real header and fail loudly on a missing column. Without this a renamed
    or dropped column inserts a silent default and the answers are quietly wrong."""
    with open_text(path) as fh:
        line = fh.readline()
    if not line.strip():
        raise SystemExit(f"{path} has no header row")
    delimiter = max(DELIMITERS, key=line.count)
    mapping = renames()
    header = tuple(mapping.get(name.strip(), name.strip())
                   for name in next(csv.reader([line], delimiter=delimiter)))
    missing = [name for name in types if name not in header]
    if missing:
        raise SystemExit(
            f"{path} is missing required column(s): {', '.join(missing)}\n"
            f"header found: {', '.join(header)}\n"
            f"map a renamed column with CSV_RENAME=their_name=our_name,...")
    return Shape(path, header, delimiter, path.suffix == ".gz")


def ingest(ch: ClickHouse, table: str, statement: str, sh: Shape) -> int:
    """One traced insert. Visible lag is the delay from acknowledgement to the rows being queryable."""
    path = sh.path
    with otel.span(f"ingest.{table}", **{"ingest.source": path.name,
                                         "ingest.bytes": path.stat().st_size}) as span:
        started = time.time()
        ch.insert_csv(statement, path, settings=sh.settings(), gzipped=sh.gzipped)
        acknowledged = time.time()
        rows = int(ch.scalar(f"SELECT count() FROM {table}"))
        otel.note(span, **{
            "ingest.rows": rows,
            "ingest.duration_ms": round((acknowledged - started) * 1000, 1),
            "ingest.visible_lag_ms": round((time.time() - acknowledged) * 1000, 1),
        })
    print(f"{table:<14}{rows:>9,} rows  {acknowledged - started:5.1f}s")
    return rows


def reload_dictionary_everywhere(ch: ClickHouse) -> None:
    """ON CLUSTER reload reaches every replica at once (D32); falls back to a plain
    reload where there is no Keeper, i.e. the local single-node target."""
    try:
        ch.command(f"SYSTEM RELOAD DICTIONARY ON CLUSTER default {ch.config.database}.content_dict")
    except ClickHouseError:
        ch.command("SYSTEM RELOAD DICTIONARY content_dict")


def load(ch: ClickHouse) -> None:
    for path in (content_csv(), raw_csv()):
        if not path.exists():
            raise SystemExit(f"missing input: {path}")

    content_shape = shape(content_csv(), CONTENT_TYPES)
    raw_shape = shape(raw_csv(), RAW_TYPES)
    print(content_shape.describe(CONTENT_TYPES))
    print(raw_shape.describe(RAW_TYPES))

    ch.command("TRUNCATE TABLE content_meta")
    ch.command("TRUNCATE TABLE raw_events")

    n_content = ingest(ch, "content_meta", content_insert(
        f"input('{content_shape.structure(CONTENT_TYPES)}')") + "\nFORMAT CSVWithNames",
        content_shape)

    with open_text(content_csv()) as fh:
        source_rows = sum(1 for _ in fh) - 1
    if source_rows != n_content:
        print(f"  rejected {source_rows - n_content} row(s) with a negative content_id")

    if n_content == 0:
        raise ClickHouseError(
            "content_meta is empty. Loading events now would enrich against an empty "
            "dictionary and silently produce unlabelled rows. See D12."
        )
    reload_dictionary_everywhere(ch)
    ingest(ch, "raw_events", raw_insert(
        f"input('{raw_shape.structure(RAW_TYPES)}')") + "\nFORMAT CSVWithNames", raw_shape)


RECONCILE_QUERY = """
    SELECT
        (SELECT count() FROM raw_events)                                    AS raw_rows,
        (SELECT uniqExact(video_session_id) FROM raw_events)                AS sessions,
        (SELECT uniqExact(user_id) FROM raw_events)                         AS users,
        (SELECT uniqExact(content_id) FROM raw_events)                      AS raw_content_ids,
        (SELECT count() FROM content_meta)                                  AS content_rows,
        (SELECT count() FROM (
            SELECT DISTINCT content_id FROM raw_events
            WHERE NOT dictHas('content_dict', content_id)))                 AS join_orphans
"""


def reconcile(ch: ClickHouse, retries: int = 3, retry_wait: float = 2.0) -> bool:
    """Diff what landed against the measured tuning CSVs. Retries only a lone
    join_orphans mismatch (defense in depth after reload_dictionary_everywhere)."""
    for attempt in range(retries):
        actual = ch.query(RECONCILE_QUERY).dicts()[0]
        mismatched = {k for k, want in EXPECTED.items() if int(actual[k]) != want}
        if not mismatched or mismatched != {"join_orphans"} or attempt == retries - 1:
            break
        reload_dictionary_everywhere(ch)
        time.sleep(retry_wait)

    ok = True
    drifted = False
    print(f"\n{'check':<18}{'measured':>12}{'FINDINGS.md':>14}")
    for key, want in EXPECTED.items():
        got = int(actual[key])
        if key in INVARIANTS:
            ok &= got == want
            flag = "" if got == want else "  MISMATCH"
        else:
            drifted |= got != want
            flag = "" if got == want else "  differs (expected on a new day)"
        print(f"{key:<18}{got:>12,}{want:>14,}{flag}")

    if int(actual["raw_rows"]) == 0 or int(actual["sessions"]) == 0:
        print("nothing loaded")
        ok = False
    print("input matches the tuning data" if not drifted else
          "input differs from the tuning data; day-invariant checks still enforced")
    return ok

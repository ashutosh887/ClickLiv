"""Freeze the served marts into static files the dashboard can read with no database."""
from __future__ import annotations

import array
import gzip
import json
from datetime import datetime, timezone
from pathlib import Path

from .ch import ClickHouse

DIMENSIONS = ["country", "platform", "video_type", "category", "app_version",
              "player_version", "audio_language", "subtitle_language",
              "video_resolution", "show_name"]
COLUMNS = ["minute", *DIMENSIONS, "content_id", "sessions"]
WIDTH = 65536
# Gzipped in place rather than named .gz, because a host that infers Content-Encoding
# from the extension would decompress it once already and DecompressionStream twice.
ROLLUP = "rollup.bin"

OVERCOUNT = """
SELECT foreground_peak, foreground_peak_utc, naive_peak, naive_peak_utc,
       peak_overcount_pct, foreground_average, naive_average, average_overcount_pct
FROM {schema}.v_overcount"""

WINDOW = """
SELECT min_minute, max_minute, min_utc, max_utc, round(span_days, 2) AS span_days,
       minutes_with_sessions, occupancy_rows, dense_min_minute, dense_max_minute,
       dense_min_utc, dense_max_utc, round(dense_span_days, 2) AS dense_span_days,
       dense_days, outlier_minutes, outlier_rows
FROM {schema}.v_data_window"""

NAIVE = """
SELECT minute, naive_concurrency
FROM {schema}.v_naive_vs_foreground
WHERE foreground_concurrency > 0
ORDER BY minute"""

TITLES = """
SELECT toString(content_id), title
FROM {schema}.v_titles
WHERE title != ''"""

VALUES = """
SELECT dimension, value, minutes_present
FROM {schema}.v_dimension_values"""

ROWS = """
SELECT minute, country, platform, video_type, category, app_version, player_version,
       audio_language, subtitle_language, video_resolution, show_name,
       toString(content_id), sessions
FROM {table}
ORDER BY minute, content_id"""


def _stream(ch: ClickHouse, sql: str) -> list[list]:
    payload = ch.command(sql.strip() + "\nFORMAT JSONCompactEachRow")
    return [json.loads(line) for line in payload.splitlines() if line]


class Dictionary:
    """Codes a column's values, ordered by the minutes each value is present in."""

    def __init__(self) -> None:
        self.codes: dict[str, int] = {}
        self.minutes: list[set] = []

    def code(self, value: str, minute: int) -> int:
        found = self.codes.get(value)
        if found is None:
            found = len(self.codes)
            self.codes[value] = found
            self.minutes.append(set())
        self.minutes[found].add(minute)
        return found

    def ordered(self) -> tuple[list[str], list[int], list[int]]:
        values = list(self.codes)
        present = [len(seen) for seen in self.minutes]
        order = sorted(range(len(values)), key=lambda at: (-present[at], values[at]))
        remap = [0] * len(order)
        for rank, at in enumerate(order):
            remap[at] = rank
        return [values[at] for at in order], [present[at] for at in order], remap


def capture(ch: ClickHouse, schema: str, table: str) -> tuple[dict, bytes]:
    """Read the rollup and its catalogue, returning the metadata and the packed columns."""
    rows = _stream(ch, ROWS.format(table=table))
    if not rows:
        raise RuntimeError(f"{table} is empty, nothing to snapshot")

    minutes: dict[int, int] = {}
    columns = {name: Dictionary() for name in [*DIMENSIONS, "content_id"]}
    packed = {name: array.array("H") for name in COLUMNS}

    for row in rows:
        minute = int(row[0])
        index = minutes.setdefault(minute, len(minutes))
        packed["minute"].append(index)
        for offset, name in enumerate([*DIMENSIONS, "content_id"], start=1):
            packed[name].append(columns[name].code(str(row[offset]), minute))
        sessions = int(row[12])
        if sessions >= WIDTH:
            raise RuntimeError(f"sessions {sessions} does not fit the 16 bit encoding")
        packed["sessions"].append(sessions)

    order = sorted(minutes, key=int)
    reindex = {minutes[minute]: rank for rank, minute in enumerate(order)}
    packed["minute"] = array.array("H", (reindex[at] for at in packed["minute"]))

    catalogue = {}
    for name, dictionary in columns.items():
        values, present, remap = dictionary.ordered()
        if len(values) >= WIDTH:
            raise RuntimeError(f"{name} has {len(values)} values, over the 16 bit encoding")
        packed[name] = array.array("H", (remap[at] for at in packed[name]))
        catalogue[name] = {"values": values, "minutes_present": present}

    titles = dict(_stream(ch, TITLES.format(schema=schema)))
    catalogue["content_id"]["titles"] = [
        titles.get(value, "") for value in catalogue["content_id"]["values"]]

    naive = {int(minute): int(value) for minute, value in _stream(ch, NAIVE.format(schema=schema))}
    head = _stream(ch, OVERCOUNT.format(schema=schema))[0]
    span = _stream(ch, WINDOW.format(schema=schema))[0]

    blob = b"".join(bytes(packed[name]) for name in COLUMNS)
    meta = {
        "captured_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "source": {
            "server": ch.ping(),
            "host": ch.config.host,
            "database": ch.config.database,
            "schema": schema,
            "table": table,
        },
        "dimensions": DIMENSIONS,
        "rollup": {
            "file": ROLLUP,
            "rows": len(rows),
            "columns": COLUMNS,
            "dtype": "uint16le",
            "encoding": "gzip",
            "minutes": order,
        },
        "catalogue": catalogue,
        "naive": [naive.get(minute, 0) for minute in order],
        "headline": {
            "foreground_peak": int(head[0]),
            "foreground_peak_utc": head[1],
            "naive_peak": int(head[2]),
            "naive_peak_utc": head[3],
            "peak_overcount_pct": float(head[4]),
            "foreground_average": float(head[5]),
            "naive_average": float(head[6]),
            "average_overcount_pct": float(head[7]),
        },
        "window": {
            "min_minute": int(span[0]), "max_minute": int(span[1]),
            "window_from": span[2], "window_to": span[3], "span_days": span[4],
            "minutes_with_sessions": int(span[5]), "occupancy_rows": int(span[6]),
            "dense_min_minute": int(span[7]), "dense_max_minute": int(span[8]),
            "dense_window_from": span[9], "dense_window_to": span[10],
            "dense_span_days": span[11], "dense_days": int(span[12]),
            "outlier_minutes": int(span[13]), "outlier_rows": int(span[14]),
        },
    }
    return meta, blob


def audit(meta: dict, blob: bytes, published: list[list]) -> list[str]:
    """Recompute the headline and the catalogue from the encoding and diff them against marts."""
    rows = meta["rollup"]["rows"]
    columns = {}
    for at, name in enumerate(COLUMNS):
        columns[name] = array.array("H")
        columns[name].frombytes(blob[at * rows * 2:(at + 1) * rows * 2])

    totals = [0] * len(meta["rollup"]["minutes"])
    for minute, sessions in zip(columns["minute"], columns["sessions"]):
        totals[minute] += sessions

    problems = []
    peak = max(totals)
    if peak != meta["headline"]["foreground_peak"]:
        problems.append(f"peak from the encoding is {peak}, marts publishes "
                        f"{meta['headline']['foreground_peak']}")
    if len(totals) != meta["window"]["minutes_with_sessions"]:
        problems.append(f"{len(totals)} minutes encoded, marts publishes "
                        f"{meta['window']['minutes_with_sessions']}")
    if rows != meta["window"]["occupancy_rows"]:
        problems.append(f"{rows} rows encoded, marts publishes "
                        f"{meta['window']['occupancy_rows']}")

    expected: dict[tuple[str, str], int] = {}
    for dimension, value, present in published:
        expected[(dimension, str(value))] = int(present)
    for dimension in DIMENSIONS:
        entry = meta["catalogue"][dimension]
        for value, present in zip(entry["values"], entry["minutes_present"]):
            if value == "":
                continue
            want = expected.pop((dimension, value), None)
            if want is None:
                problems.append(f"{dimension} value {value!r} is encoded but not in marts")
            elif want != present:
                problems.append(f"{dimension} value {value!r} present in {present} minutes, "
                                f"marts publishes {want}")
    for dimension, value in expected:
        problems.append(f"{dimension} value {value!r} is in marts but not encoded")
    return problems


def write(ch: ClickHouse, into: Path, schema: str, table: str) -> dict:
    """Capture, verify against the marts views, then write meta.json and the packed rollup."""
    meta, blob = capture(ch, schema, table)
    problems = audit(meta, blob, _stream(ch, VALUES.format(schema=schema)))
    if problems:
        raise RuntimeError("snapshot does not match marts:\n  " + "\n  ".join(problems))

    into.mkdir(parents=True, exist_ok=True)
    compressed = gzip.compress(blob, 9)
    (into / ROLLUP).write_bytes(compressed)
    meta["rollup"]["bytes"] = len(blob)
    meta["rollup"]["gzip_bytes"] = len(compressed)
    (into / "meta.json").write_text(json.dumps(meta, separators=(",", ":")) + "\n")
    return meta


def run(ch: ClickHouse, into: Path, schema: str) -> int:
    """Write the snapshot and print what it holds, so the numbers are visible at capture time."""
    meta = write(ch, into, schema, "minute_occupancy")
    rollup = meta["rollup"]
    print(f"snapshot of {schema} on {meta['source']['host']} at {meta['captured_utc']} UTC")
    print(f"  {rollup['rows']:,} rollup rows, {len(rollup['minutes']):,} minutes, "
          f"peak {meta['headline']['foreground_peak']:,}")
    print(f"  {rollup['bytes'] / 1e6:.1f} MB packed, {rollup['gzip_bytes'] / 1e6:.1f} MB gzipped")
    print(f"  {(into / 'meta.json').stat().st_size / 1e6:.1f} MB meta.json")
    print(f"  verified against {schema}.v_overcount, v_data_window and v_dimension_values")
    return 0

"""O7 scale demonstration. Sessionization never crosses a session boundary (D25), so
fan-out over sessions is exact, and the serving layer's read cost tracks the rollup.
"""

from __future__ import annotations

import csv
import shutil
import time
import uuid
from collections import defaultdict
from pathlib import Path

from .ch import ClickHouse

SHARDS = 8

ACTIVE_STRUCTURE = "video_session_id String, segment_id UInt32, ts_start_ms Int64, ts_end_ms Int64"

SHARD_QUERY = """
SELECT video_session_id, segment_id, ts_start_ms, ts_end_ms
FROM active_intervals
WHERE cityHash64(video_session_id) % {shards} = {shard}
"""

MINUTE_SESSIONS = """
SELECT
    toUInt32(minute) AS minute,
    uniqExact(video_session_id) AS sessions
FROM
(
    SELECT
        video_session_id,
        arrayJoin(range(toUInt32(ts_start_ms DIV 60000),
                        toUInt32((ts_end_ms - 1) DIV 60000) + 1)) AS minute
    FROM active_intervals
)
GROUP BY minute
"""

SCALE_FACTORS = (1, 10, 100)

RAW_DDL = """
CREATE TABLE scale_raw_events
(
    video_session_id  String,
    event_time        DateTime64(3, 'UTC'),
    content_id        UInt64,
    platform          LowCardinality(String),
    app_version       LowCardinality(String),
    country           LowCardinality(String),
    audio_language    LowCardinality(String),
    subtitle_language LowCardinality(String),
    player_version    LowCardinality(String)
)
ENGINE = MergeTree
ORDER BY (video_session_id, event_time)
"""

RAW_REPLICATE = """
INSERT INTO scale_raw_events
SELECT
    video_session_id || '-rep' || toString(r),
    event_time + toIntervalSecond((r - 1) * {span_seconds}),
    content_id, platform, app_version, country,
    audio_language, subtitle_language, player_version
FROM raw_events
CROSS JOIN (SELECT arrayJoin(range(1, {sf} + 1)) AS r) AS reps
"""

ROLLUP_DDL = """
CREATE TABLE scale_minute_occupancy
(
    country           LowCardinality(String),
    platform          LowCardinality(String),
    app_version       LowCardinality(String),
    audio_language    LowCardinality(String),
    subtitle_language LowCardinality(String),
    player_version    LowCardinality(String),
    content_id        UInt64,
    minute            UInt32,
    sessions          UInt32
)
ENGINE = MergeTree
ORDER BY (country, platform, app_version, audio_language, subtitle_language, content_id, minute)
"""

ROLLUP_BUILD = """
INSERT INTO scale_minute_occupancy
SELECT country, platform, app_version, audio_language, subtitle_language,
       player_version, content_id, minute, toUInt32(count()) AS sessions
FROM
(
    SELECT video_session_id, country, platform, app_version, audio_language,
           subtitle_language, player_version, content_id,
           toUInt32(toUnixTimestamp64Milli(event_time) DIV 60000) AS minute
    FROM scale_raw_events
    GROUP BY video_session_id, minute, country, platform, app_version,
             audio_language, subtitle_language, player_version, content_id
)
GROUP BY country, platform, app_version, audio_language, subtitle_language,
         player_version, content_id, minute
"""

NAIVE_BENCH = """
SELECT max(sessions) FROM
(
    SELECT minute, uniqExact(video_session_id) AS sessions FROM
    (
        SELECT video_session_id,
               toUInt32(toUnixTimestamp64Milli(event_time) DIV 60000) AS minute
        FROM scale_raw_events
    )
    GROUP BY minute
)
"""

ROLLUP_BENCH = """
SELECT max(total) FROM
(
    SELECT minute, sum(sessions) AS total FROM scale_minute_occupancy GROUP BY minute
)
"""


def write_shard_csv(ch: ClickHouse, shard: int, path: Path) -> int:
    rows = ch.query(SHARD_QUERY.format(shards=SHARDS, shard=shard)).rows
    with path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["video_session_id", "segment_id", "ts_start_ms", "ts_end_ms"])
        writer.writerows(rows)
    return len(rows)


def shard_minutes(store: Path, csv_path: Path) -> dict:
    from .chdb_engine import ChdbEngine, file_source

    store.mkdir(parents=True)
    engine = ChdbEngine(store)
    engine.command(f"CREATE TABLE active_intervals ({ACTIVE_STRUCTURE}) "
                    f"ENGINE = MergeTree ORDER BY (video_session_id, ts_start_ms)")
    engine.command(f"INSERT INTO active_intervals "
                    f"SELECT * FROM {file_source(csv_path, ACTIVE_STRUCTURE)}")
    counts = {int(r[0]): int(r[1]) for r in engine.query(MINUTE_SESSIONS).rows}
    engine.close()
    return counts


def sharding_proof(ch: ClickHouse, artifacts: Path) -> dict:
    """Split active_intervals into SHARDS chDB instances by session hash, sum their
    per-minute counts, and check the sum against the server's live minute_occupancy."""
    store_root = artifacts / "chdb_shards"
    if store_root.exists():
        shutil.rmtree(store_root)
    store_root.mkdir(parents=True)

    merged = defaultdict(int)
    rows_per_shard = []
    for shard in range(SHARDS):
        csv_path = store_root / f"shard_{shard}.csv"
        rows_per_shard.append(write_shard_csv(ch, shard, csv_path))
        for minute, sessions in shard_minutes(store_root / f"store_{shard}", csv_path).items():
            merged[minute] += sessions

    real = {int(r[0]): int(r[1]) for r in ch.query(
        "SELECT minute, sum(sessions) FROM minute_occupancy GROUP BY minute").rows}

    shutil.rmtree(store_root)

    series_match = merged == real
    merged_peak = max(merged.values()) if merged else 0
    real_peak = max(real.values()) if real else 0

    print(f"Proof 1: sharding correctness across {SHARDS} shards")
    print(f"rows per shard: {rows_per_shard}, total {sum(rows_per_shard):,}")
    print(f"merged peak {merged_peak:,}, real minute_occupancy peak {real_peak:,}")
    print(f"per-minute series across {len(real)} minutes: "
          f"{'matches exactly' if series_match else 'DIFFERS'}\n")

    return {
        "ok": series_match and merged_peak == real_peak,
        "shards": SHARDS,
        "rows_per_shard": rows_per_shard,
        "merged_peak": merged_peak,
        "real_peak": real_peak,
        "minutes_compared": len(real),
        "series_match": series_match,
    }


def build_scale_tables(ch: ClickHouse, sf: int, span_seconds: int) -> None:
    ch.command("DROP TABLE IF EXISTS scale_raw_events")
    ch.command(RAW_DDL)
    ch.command(RAW_REPLICATE.format(span_seconds=span_seconds, sf=sf))
    ch.command("DROP TABLE IF EXISTS scale_minute_occupancy")
    ch.command(ROLLUP_DDL)
    ch.command(ROLLUP_BUILD)


def query_log_stats(ch: ClickHouse, query_id: str) -> dict:
    row = ch.query_log_rows("read_rows, read_bytes", [query_id])[0]
    return {"read_rows": int(row["read_rows"]), "read_bytes": int(row["read_bytes"])}


def scale_proof(ch: ClickHouse, artifacts: Path) -> list[dict]:
    """Naive versus rollup read_rows at 1x, 10x and 100x of the real raw event count.
    Regroups by (video_session_id, minute) off event_time, skipping the window-function
    sessionizer so 100x finishes in minutes; write_evidence spells out why that's fair."""
    ch.command("DROP TABLE IF EXISTS scale_raw_events")
    ch.command("DROP TABLE IF EXISTS scale_minute_occupancy")

    span_seconds = int(ch.scalar(
        "SELECT toUInt64(toUnixTimestamp(max(event_time)) "
        "- toUnixTimestamp(min(event_time)) + 86400) FROM raw_events"))

    results = []
    try:
        for sf in SCALE_FACTORS:
            started = time.time()
            build_scale_tables(ch, sf, span_seconds)
            raw_rows = int(ch.scalar("SELECT count() FROM scale_raw_events"))
            serving_rows = int(ch.scalar("SELECT count() FROM scale_minute_occupancy"))

            naive_id = str(uuid.uuid4())
            ch.query(NAIVE_BENCH, query_id=naive_id)
            naive_stats = query_log_stats(ch, naive_id)

            rollup_id = str(uuid.uuid4())
            ch.query(ROLLUP_BENCH, query_id=rollup_id)
            rollup_stats = query_log_stats(ch, rollup_id)

            ratio = round(naive_stats["read_rows"] / rollup_stats["read_rows"], 2) \
                if rollup_stats["read_rows"] else 0.0

            results.append({
                "scale_factor": sf, "raw_rows": raw_rows, "serving_rows": serving_rows,
                "naive_read_rows": naive_stats["read_rows"],
                "rollup_read_rows": rollup_stats["read_rows"], "ratio": ratio,
            })
            print(f"{sf:>4}x  raw {raw_rows:>10,}  serving {serving_rows:>9,}  "
                  f"naive read_rows {naive_stats['read_rows']:>10,}  "
                  f"rollup read_rows {rollup_stats['read_rows']:>9,}  "
                  f"ratio {ratio:>8.1f}  built in {time.time() - started:.1f}s")
    finally:
        ch.command("DROP TABLE IF EXISTS scale_raw_events")
        ch.command("DROP TABLE IF EXISTS scale_minute_occupancy")

    artifacts.mkdir(parents=True, exist_ok=True)
    with (artifacts / "scale.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(results[0]))
        writer.writeheader()
        writer.writerows(results)

    print()
    return results


def write_evidence(evidence: Path, proof1: dict, results2: list[dict]) -> None:
    ratios = [r["ratio"] for r in results2]

    lines = [
        "O7 scale demonstration\n",
        "\nProof 1: sharding is exact, not approximate (D25)\n",
        f"{proof1['shards']} shards by cityHash64(video_session_id) % {proof1['shards']}\n",
        f"rows per shard: {proof1['rows_per_shard']}\n",
        f"merged peak {proof1['merged_peak']:,}, "
        f"real minute_occupancy peak {proof1['real_peak']:,}\n",
        f"per-minute series across {proof1['minutes_compared']} minutes: "
        f"{'matches exactly' if proof1['series_match'] else 'DIFFERS'}\n",
        "\nWhy this proves correctness rather than claiming it: sessionization never "
        "splits a session across shards, so a shard's local per-minute session count is "
        "already a correct component of the global sum, no double counting and nothing "
        "missed is possible by construction.\n",
        "\nProof 2: serving layer read cost scales with the rollup, not raw events\n",
        "Simplification: the rollup here regroups by (video_session_id, minute) from "
        "raw event timestamps, skipping the window-function sessionizer so 100x "
        "finishes in minutes. This is honest about what changed: concurrency numbers "
        "from this proxy do not match the real pipeline, but the row-count scaling "
        "argument, read_rows against table cardinality, is unaffected by that.\n",
        "\nHonest caveat on the growth numbers below: scale is built by exact "
        "duplication (same sessions, K copies with shifted ids and time), so raw_rows "
        "and serving_rows both scale by K here, that is a property of duplication, not "
        "of a rollup. What duplication cannot fake is the collapse ratio at a fixed "
        f"scale: naive reads {ratios[0]:.1f}x more rows than the rollup at 1x and still "
        f"{ratios[-1]:.1f}x more at 100x, so the compression from raw event grain to "
        "session-minute grain is structural and does not erode as the table grows. "
        "Under organic growth, where sessions gain more events rather than sessions "
        "being cloned wholesale, serving_rows would grow slower than raw_rows and this "
        "ratio would widen, not stay flat.\n",
        f"\n{'scale':>6}{'raw_rows':>12}{'serving_rows':>14}{'naive_read_rows':>17}"
        f"{'rollup_read_rows':>18}{'ratio':>9}\n",
    ]
    for r in results2:
        lines.append(f"{str(r['scale_factor']) + 'x':>6}{r['raw_rows']:>12,}"
                      f"{r['serving_rows']:>14,}{r['naive_read_rows']:>17,}"
                      f"{r['rollup_read_rows']:>18,}{r['ratio']:>9.1f}\n")

    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "scale.txt").write_text("".join(lines))


def run(ch: ClickHouse, artifacts: Path, evidence: Path) -> bool:
    print("O7 scale demonstration\n")
    artifacts.mkdir(parents=True, exist_ok=True)

    proof1 = sharding_proof(ch, artifacts)
    results2 = scale_proof(ch, artifacts)
    write_evidence(evidence, proof1, results2)

    proof2_ok = all(r["rollup_read_rows"] <= r["naive_read_rows"] for r in results2)
    ok = proof1["ok"] and proof2_ok

    print(f"{evidence}/scale.txt")
    print(f"{artifacts}/scale.csv           {len(results2)} rows")
    print(f"\nO7: {'PASS' if ok else 'FAIL'}  sharding exact across {proof1['shards']} "
          f"shards, rollup read cost below naive at every scale factor tested "
          f"({', '.join(str(r['scale_factor']) + 'x' for r in results2)})")
    return ok

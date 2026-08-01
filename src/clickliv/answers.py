"""Answers, latencies and pipeline evidence for the benchmark set. No pipeline
evidence, no credit: every number here is traceable to a query_id in system.query_log.
"""

from __future__ import annotations

import csv
import json
import uuid
from pathlib import Path

from .ch import ClickHouse, ClickHouseError

BENCHMARKS = [
    {"label": "day_peak_no_filter", "grain_minutes": 1440,
     "country": "", "platform": "", "video_type": "", "content_id": 0},
    {"label": "hour_peak_no_filter", "grain_minutes": 60,
     "country": "", "platform": "", "video_type": "", "content_id": 0},
    {"label": "minute_peak_no_filter", "grain_minutes": 1,
     "country": "", "platform": "", "video_type": "", "content_id": 0},
    {"label": "day_peak_platform_android_phone", "grain_minutes": 1440,
     "country": "", "platform": "ANDROID_PHONE", "video_type": "", "content_id": 0},
    {"label": "day_peak_platform_sony_android_tv", "grain_minutes": 1440,
     "country": "", "platform": "SONY_ANDROID_TV", "video_type": "", "content_id": 0},
    {"label": "day_peak_video_type_live", "grain_minutes": 1440,
     "country": "", "platform": "", "video_type": "live", "content_id": 0},
    {"label": "day_peak_iphone_india", "grain_minutes": 1440,
     "country": "india", "platform": "IPHONE", "video_type": "", "content_id": 0},
    {"label": "day_peak_vod_mweb", "grain_minutes": 1440,
     "country": "", "platform": "Mweb", "video_type": "vod", "content_id": 0},
]

EVIDENCE_LABEL = "day_peak_no_filter"

CALL_ARGS = (
    "grain_minutes = {grain_minutes}, country = '{country}', platform = '{platform}', "
    "video_type = '{video_type}', content_id = {content_id}, "
    "minute_from = {minute_from}, minute_to = {minute_to}"
)


def marts(ch: ClickHouse) -> str:
    """A scratch run must answer from its own marts, never from the primary one."""
    from .cli import marts_database
    return marts_database()


def minute_bounds(ch: ClickHouse) -> tuple[int, int]:
    lo, hi = ch.query("SELECT min(minute), max(minute) FROM minute_occupancy").rows[0]
    if lo is None or hi is None:
        raise SystemExit(
            "minute_occupancy is empty, so there is no minute range to answer over. "
            "The load or the sessionizer produced nothing; see docs/unseen-day.md.")
    return int(lo), int(hi)


def run_benchmark(ch: ClickHouse, spec: dict, minute_from: int, minute_to: int) -> dict:
    query_id = str(uuid.uuid4())
    args = CALL_ARGS.format(**spec, minute_from=minute_from, minute_to=minute_to)
    rows = ch.query(
        f"SELECT max(peak_concurrency) AS peak, "
        f"sum(average_concurrency * minutes_in_bucket) / sum(minutes_in_bucket) AS avg, "
        f"sum(minutes_in_bucket) AS active_minutes "
        f"FROM {marts(ch)}.v_concurrency({args})", query_id=query_id).rows[0]
    peak, avg, active_minutes = rows
    return {
        "query_label": spec["label"],
        "query_id": query_id,
        "grain_minutes": spec["grain_minutes"],
        "country": spec["country"], "platform": spec["platform"],
        "video_type": spec["video_type"], "content_id": spec["content_id"],
        "minute_from": minute_from, "minute_to": minute_to,
        "peak_concurrency": int(peak) if peak is not None else 0,
        "average_concurrency": round(float(avg), 4) if avg is not None else 0.0,
        "average_denominator": "active minutes (minute_occupancy has no zero rows)",
        "active_minutes": int(active_minutes) if active_minutes is not None else 0,
    }


def query_log_rows(ch: ClickHouse, query_ids: list[str]) -> list[dict]:
    rows = ch.query_log_rows(
        "query_id, query_duration_ms, read_rows, read_bytes, "
        "result_rows, memory_usage, event_time", query_ids)
    return sorted(rows, key=lambda r: r["event_time"])


def oracle_match(ch: ClickHouse, artifacts: Path) -> dict:
    occupancy_peak = int(ch.scalar("SELECT max(sessions_total) FROM "
        "(SELECT minute, sum(sessions) AS sessions_total FROM minute_occupancy "
        "GROUP BY minute)"))
    intersections = int(ch.scalar(
        "SELECT maxIntersections(ts_start_ms, ts_end_ms) FROM active_intervals"))
    reference_path = artifacts / "reference.json"
    reference = json.loads(reference_path.read_text()) if reference_path.exists() else {}
    return {
        "occupancy_peak": occupancy_peak,
        "max_intersections_instantaneous_peak": intersections,
        "python_reference_instantaneous_peak": reference.get("instantaneous_peak", ""),
        "python_reference_peak_concurrency": reference.get("peak_concurrency", ""),
        "note": "instantaneous <= occupancy always; they measure different things (D1/O3)",
    }


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def capture_explain(ch: ClickHouse, spec: dict, minute_from: int, minute_to: int,
                     evidence: Path) -> None:
    args = CALL_ARGS.format(**spec, minute_from=minute_from, minute_to=minute_to)
    query = f"SELECT * FROM {marts(ch)}.v_concurrency({args})"
    plan = ch.query(f"EXPLAIN indexes = 1 {query}").rows
    text = "-- EXPLAIN indexes = 1\n" + "\n".join(r[0] for r in plan)
    try:
        analyzed = ch.query(f"EXPLAIN ANALYZE {query}").rows
        text += "\n\n-- EXPLAIN ANALYZE\n" + "\n".join(r[0] for r in analyzed)
    except ClickHouseError as exc:
        text += (f"\n\n-- EXPLAIN ANALYZE unavailable on this server "
                  f"(runnable EXPLAIN ANALYZE needs ClickHouse 26.7+): {exc}")
    (evidence / f"explain_{spec['label']}.txt").write_text(text + "\n")


def run(ch: ClickHouse, artifacts: Path, answers_dir: Path = Path("answers"),
        evidence_dir: Path = Path("evidence")) -> bool:
    answers_dir.mkdir(parents=True, exist_ok=True)
    evidence_dir.mkdir(parents=True, exist_ok=True)

    minute_from, minute_to = minute_bounds(ch)
    results = [run_benchmark(ch, spec, minute_from, minute_to) for spec in BENCHMARKS]

    write_csv(answers_dir / "benchmark_answers.csv", [
        {k: v for k, v in r.items() if k != "query_id"} for r in results])
    write_csv(answers_dir / "latencies.csv", query_log_rows(
        ch, [r["query_id"] for r in results]))

    representative = next(s for s in BENCHMARKS if s["label"] == EVIDENCE_LABEL)
    capture_explain(ch, representative, minute_from, minute_to, evidence_dir)
    write_csv(evidence_dir / "query_log.csv", query_log_rows(
        ch, [r["query_id"] for r in results]))
    write_csv(evidence_dir / "oracle_match.csv", [oracle_match(ch, artifacts)])

    print(f"{answers_dir}/benchmark_answers.csv   {len(results)} rows")
    print(f"{answers_dir}/latencies.csv           {len(results)} rows")
    print(f"{evidence_dir}/query_log.csv          {len(results)} rows")
    print(f"{evidence_dir}/explain_{EVIDENCE_LABEL}.txt")
    print(f"{evidence_dir}/oracle_match.csv       1 row")
    return True

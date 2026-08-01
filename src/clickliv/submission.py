"""Format-agnostic submission bundle (O2) and a measured serving SLO (O6b), both
driven off one benchmark run so the answers and the latencies describe the same queries."""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .answers import BENCHMARKS, minute_bounds, run_benchmark, write_csv
from .ch import ClickHouse

TARGET_P99_MS = 100

COUNTED_TABLES = ("raw_events", "active_intervals", "minute_occupancy", "minute_deltas")

BUNDLE_FILES = ("benchmark_answers.csv", "benchmark_answers.json", "README.txt")

LOG_COLUMNS = ("query_id, query_duration_ms, read_rows, read_bytes, "
               "result_rows, memory_usage")

README = """ClickLiv submission bundle
==========================

The grading answer-file format was never published, so the same answers are emitted
in every plausible shape from one source of truth.

benchmark_answers.csv   one row per benchmark query.
benchmark_answers.json  the same rows, same column order, as a JSON array.
manifest.json           what produced these numbers: server version, row counts,
                        minute range, thresholds, git commit, and a SHA-256 plus byte
                        size for every other file here.
README.txt              this file.

Columns that carry the answer
-----------------------------

peak_concurrency        answers "peak": the maximum concurrent sessions seen in any
                        single minute inside the bucket.
average_concurrency     answers "average": session-minutes divided by minutes, that
                        is sum(average_concurrency * minutes_in_bucket) over
                        sum(minutes_in_bucket).
average_denominator     names the denominator explicitly. It is active minutes, not
                        wall-clock minutes: minute_occupancy stores no zero rows, so a
                        minute with no sessions is absent rather than present as a 0.
                        Dividing by the wall-clock span instead would give a smaller
                        number for the same data.
active_minutes          the denominator's value, so the average can be recomputed by hand.

grain_minutes 1440 is a day bucket, 60 an hour, 1 a single minute. Empty country,
platform and video_type mean unfiltered; content_id 0 means all content.

Latency
-------

Every latency reported anywhere in this project is server-side
system.query_log.query_duration_ms, looked up by the query_id the client generated
before sending the query. It is not client wall clock, so it excludes network round
trip and Python overhead. See evidence/serving_slo.txt and evidence/serving_slo.csv.
"""


def git_commit() -> str:
    try:
        done = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                              text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def minute_to_utc(minute: int) -> str:
    return datetime.fromtimestamp(minute * 60, timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def file_digest(path: Path) -> dict:
    data = path.read_bytes()
    return {"file": path.name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def percentile(values: list[float], q: float) -> float:
    """Nearest-rank percentile, correct for the small sample counts here."""
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))]


def collect_samples(ch: ClickHouse, minute_from: int, minute_to: int,
                    repetitions: int) -> tuple[list[dict], list[dict]]:
    """Run the benchmark set repetitions times; the first pass is also the published answers."""
    answers: list[dict] = []
    samples: list[dict] = []
    for repetition in range(1, repetitions + 1):
        for spec in BENCHMARKS:
            result = run_benchmark(ch, spec, minute_from, minute_to)
            if repetition == 1:
                answers.append(result)
            samples.append({"repetition": repetition,
                            "query_label": result["query_label"],
                            "query_id": result["query_id"]})
    return answers, samples


def sample_latencies(ch: ClickHouse, samples: list[dict], attempts: int = 8) -> list[dict]:
    """Union repeated query_log reads: Cloud routes each request to either replica, and a
    replica logs only its own queries, so one read sees roughly half the samples."""
    wanted = [s["query_id"] for s in samples]
    logged: dict[str, dict] = {}
    for _ in range(attempts):
        for row in ch.query_log_rows(LOG_COLUMNS, wanted, retries=2):
            logged[row["query_id"]] = row
        if len(logged) >= len(wanted):
            break
    rows = []
    for sample in samples:
        row = logged.get(sample["query_id"])
        if row is None:
            continue
        rows.append({**sample,
                     "query_duration_ms": int(row["query_duration_ms"]),
                     "read_rows": int(row["read_rows"]),
                     "read_bytes": int(row["read_bytes"]),
                     "result_rows": int(row["result_rows"]),
                     "memory_usage": int(row["memory_usage"])})
    return rows


def manifest(ch: ClickHouse, artifacts: Path, submission_dir: Path,
             minute_from: int, minute_to: int, repetitions: int) -> dict:
    reference_path = artifacts / "reference.json"
    reference = json.loads(reference_path.read_text()) if reference_path.exists() else {}
    return {
        "clickhouse_version": ch.ping(),
        "host": ch.config.host,
        "database": ch.config.database,
        "git_commit": git_commit(),
        "gap_seconds": int(os.environ.get("GAP_SECONDS", "90")),
        "grace_seconds": int(os.environ.get("GRACE_SECONDS", "40")),
        "row_counts": {table: int(ch.scalar(f"SELECT count() FROM {table}"))
                       for table in COUNTED_TABLES},
        "minute_from": minute_from,
        "minute_to": minute_to,
        "minute_from_utc": minute_to_utc(minute_from),
        "minute_to_utc": minute_to_utc(minute_to),
        "benchmarks": len(BENCHMARKS),
        "latency_repetitions": repetitions,
        "python_reference_peak_concurrency": reference.get("peak_concurrency", ""),
        "files": [file_digest(submission_dir / name) for name in BUNDLE_FILES],
    }


def slo_report(latencies: list[dict], repetitions: int, passed: bool,
               percentiles: dict) -> str:
    expected = repetitions * len(BENCHMARKS)
    lines = [
        "-- O6b: serving SLO\n",
        f"target: p99 <= {TARGET_P99_MS} ms\n",
        "This target is ours and self-imposed. No SLA was ever published upstream, so\n",
        "rather than leave the serving latency unstated we set a number and measure it.\n\n",
        "measured server-side from system.query_log.query_duration_ms, looked up by\n",
        "query_id. Not client wall clock: network round trip and Python overhead are\n",
        "excluded, so this is what the server itself spent.\n\n",
        f"samples: {len(latencies)} of {expected} "
        f"({len(BENCHMARKS)} benchmark queries x {repetitions} repetitions)\n",
        *([] if len(latencies) == expected else [
            f"{expected - len(latencies)} sample(s) were not recoverable from "
            "system.query_log and are excluded rather than guessed at\n"]),
        f"min   {percentiles['min']:>8.1f} ms\n",
        f"p50   {percentiles['p50']:>8.1f} ms\n",
        f"p95   {percentiles['p95']:>8.1f} ms\n",
        f"p99   {percentiles['p99']:>8.1f} ms\n",
        f"max   {percentiles['max']:>8.1f} ms\n\n",
        f"{'PASS' if passed else 'FAIL'}: measured p99 of {percentiles['p99']:.1f} ms is "
        f"{'within' if passed else 'over'} the {TARGET_P99_MS} ms target\n\n",
        "percentiles are nearest-rank over every sample. The per-sample rows are in\n",
        "evidence/serving_slo.csv so these numbers can be recomputed, not just believed.\n",
    ]
    return "".join(lines)


def run(ch: ClickHouse, artifacts: Path, submission_dir: Path = Path("submission"),
        evidence_dir: Path = Path("evidence"), repetitions: int = 5) -> bool:
    submission_dir.mkdir(parents=True, exist_ok=True)
    evidence_dir.mkdir(parents=True, exist_ok=True)

    minute_from, minute_to = minute_bounds(ch)
    answers, samples = collect_samples(ch, minute_from, minute_to, repetitions)
    rows = [{k: v for k, v in a.items() if k != "query_id"} for a in answers]

    write_csv(submission_dir / "benchmark_answers.csv", rows)
    (submission_dir / "benchmark_answers.json").write_text(json.dumps(rows, indent=2) + "\n")
    (submission_dir / "README.txt").write_text(README)
    (submission_dir / "manifest.json").write_text(json.dumps(
        manifest(ch, artifacts, submission_dir, minute_from, minute_to, repetitions),
        indent=2) + "\n")

    latencies = sample_latencies(ch, samples)
    if not latencies:
        (evidence_dir / "serving_slo.txt").write_text(
            "-- O6b: serving SLO\nno sample was recoverable from system.query_log on this\n"
            "server, so no latency is reported rather than a guessed one. The answers\n"
            "above are unaffected.\n")
        print(f"{submission_dir}/benchmark_answers.csv    {len(rows)} rows")
        print(f"{evidence_dir}/serving_slo.txt          no query_log samples recovered")
        return True

    durations = [float(r["query_duration_ms"]) for r in latencies]
    percentiles = {
        "min": min(durations), "max": max(durations),
        "p50": percentile(durations, 0.50),
        "p95": percentile(durations, 0.95),
        "p99": percentile(durations, 0.99),
    }
    passed = percentiles["p99"] <= TARGET_P99_MS

    write_csv(evidence_dir / "serving_slo.csv", latencies)
    (evidence_dir / "serving_slo.txt").write_text(
        slo_report(latencies, repetitions, passed, percentiles))

    print(f"{submission_dir}/benchmark_answers.csv    {len(rows)} rows")
    print(f"{submission_dir}/benchmark_answers.json   {len(rows)} objects")
    print(f"{submission_dir}/manifest.json            {len(BUNDLE_FILES)} files hashed")
    print(f"{submission_dir}/README.txt")
    print(f"{evidence_dir}/serving_slo.csv          {len(latencies)} samples")
    print(f"{evidence_dir}/serving_slo.txt          "
          f"{'PASS' if passed else 'FAIL'}, p50 {percentiles['p50']:.1f} ms, "
          f"p99 {percentiles['p99']:.1f} ms vs {TARGET_P99_MS} ms target")
    return passed

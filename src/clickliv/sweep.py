"""D3 sensitivity sweep. Turns two guessed thresholds into two defended ones."""

from __future__ import annotations

import csv
import os
from pathlib import Path

from .ch import ClickHouse

GAPS = (60, 90, 120)
GRACES = (20, 40, 60)

OCCUPANCY = """
SELECT
    max(sessions) AS peak,
    argMax(minute, sessions) AS peak_minute,
    sum(sessions) AS session_minutes,
    sum(sessions) / (max(minute) - min(minute) + 1) AS average
FROM
(
    SELECT minute, uniqExact(video_session_id) AS sessions
    FROM
    (
        SELECT
            video_session_id,
            arrayJoin(range(toUInt32(ts_start_ms DIV 60000),
                            toUInt32((ts_end_ms - 1) DIV 60000) + 1)) AS minute
        FROM {source}
    )
    GROUP BY minute
)
"""

NAIVE = """
CREATE OR REPLACE VIEW naive_intervals AS
SELECT
    video_session_id,
    min(toUnixTimestamp64Milli(event_time)) AS ts_start_ms,
    max(toUnixTimestamp64Milli(event_time)) + 1 AS ts_end_ms
FROM raw_events
GROUP BY video_session_id
"""


def measure(ch: ClickHouse, source: str) -> dict:
    return ch.query(OCCUPANCY.format(source=source)).dicts()[0]


def run(ch: ClickHouse, artifacts: Path, run_sessionize) -> None:
    ch.command(NAIVE)
    naive = measure(ch, "naive_intervals")
    print(f"naive open-session baseline: peak {int(naive['peak']):,} "
          f"at minute {naive['peak_minute']}, average {float(naive['average']):.2f}, "
          f"{int(naive['session_minutes']):,} session-minutes\n")

    original = (os.environ["GAP_SECONDS"], os.environ["GRACE_SECONDS"])
    results = []
    print(f"{'gap':>5}{'grace':>7}{'segments':>11}{'peak':>8}{'peak minute':>14}"
          f"{'average':>10}{'session-min':>13}{'vs naive peak':>15}")
    try:
        for gap in GAPS:
            for grace in GRACES:
                os.environ["GAP_SECONDS"] = str(gap)
                os.environ["GRACE_SECONDS"] = str(grace)
                run_sessionize(ch)
                segments = int(ch.scalar("SELECT count() FROM active_intervals"))
                row = measure(ch, "active_intervals")
                peak = int(row["peak"])
                overcount = 100 * (int(naive["peak"]) / peak - 1)
                results.append({
                    "gap_seconds": gap, "grace_seconds": grace, "segments": segments,
                    "peak": peak, "peak_minute": row["peak_minute"],
                    "average": round(float(row["average"]), 4),
                    "session_minutes": int(row["session_minutes"]),
                    "naive_peak_overcount_pct": round(overcount, 2),
                })
                print(f"{gap:>5}{grace:>7}{segments:>11,}{peak:>8,}{row['peak_minute']:>14}"
                      f"{float(row['average']):>10.2f}{int(row['session_minutes']):>13,}"
                      f"{overcount:>14.1f}%")
    finally:
        os.environ["GAP_SECONDS"], os.environ["GRACE_SECONDS"] = original
        run_sessionize(ch)

    artifacts.mkdir(parents=True, exist_ok=True)
    with (artifacts / "sweep.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(results[0]))
        writer.writeheader()
        writer.writerows(results)

    peaks = [r["peak"] for r in results]
    print(f"\npeak ranges {min(peaks):,} to {max(peaks):,} across the grid, "
          f"a spread of {100 * (max(peaks) / min(peaks) - 1):.1f}% "
          f"around {[r['peak'] for r in results if r['gap_seconds'] == 90 and r['grace_seconds'] == 40][0]:,} "
          f"at the chosen 90s/40s")

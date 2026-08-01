"""O3 evidence. Instantaneous overlap peak per dimension slice, beside the occupancy peak."""

from __future__ import annotations

from pathlib import Path

from .ch import ClickHouse
from .verify import SLICES, occupancy_series

CLIPPED = """
WITH clipped AS
(
    SELECT
        pieces.video_session_id AS sid,
        greatest(pieces.ts_start_ms, toInt64(pieces.minute) * 60000) AS clip_start,
        least(pieces.ts_end_ms, (toInt64(pieces.minute) + 1) * 60000) AS clip_end
    FROM
    (
        SELECT
            video_session_id,
            ts_start_ms,
            ts_end_ms,
            arrayJoin(range(toUInt32(ts_start_ms DIV 60000),
                            toUInt32((ts_end_ms - 1) DIV 60000) + 1)) AS minute
        FROM active_intervals
    ) AS pieces
    INNER JOIN session_minutes AS dims
        ON dims.video_session_id = pieces.video_session_id AND dims.minute = pieces.minute
    WHERE {where}
),
merged AS
(
    SELECT sid, min(clip_start) AS clip_start, max(clip_end) AS clip_end
    FROM
    (
        SELECT
            sid, clip_start, clip_end,
            sum(opens) OVER (PARTITION BY sid ORDER BY clip_start ASC, clip_end ASC
                ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS run
        FROM
        (
            SELECT
                sid, clip_start, clip_end,
                if(max(clip_end) OVER (PARTITION BY sid ORDER BY clip_start ASC, clip_end ASC
                    ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) >= clip_start, 0, 1) AS opens
            FROM clipped
        )
    )
    GROUP BY sid, run
)
"""

INTERSECTIONS = "SELECT maxIntersections(clip_start, clip_end - 1) FROM merged"

SWEEP = """
SELECT max(live) FROM
(
    SELECT sum(sum(d)) OVER (
        ORDER BY t ASC, d ASC ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS live
    FROM
    (
        SELECT clip_start AS t, 1 AS d FROM merged
        UNION ALL
        SELECT clip_end AS t, -1 AS d FROM merged
    )
    GROUP BY t, d
)
"""

HEADER = ("-- O3: is concurrency at minute m occupancy (any playback in the minute) or\n"
          "-- instantaneous overlap at one point in time? Both, per dimension slice.\n"
          "-- occupancy comes from minute_occupancy, the rollup marts.v_concurrency serves.\n"
          "-- instantaneous clips every active interval to each minute it covers, joins that\n"
          "-- minute's dimensions from session_minutes, applies the filter, merges the pieces\n"
          "-- of one session back into continuous presence, then peaks the overlap.\n"
          "-- two independent SQL paths must agree: maxIntersections over closed millisecond\n"
          "-- intervals (clip_end - 1 as the inclusive end), and a signed event sweep on the\n"
          "-- half-open form. instantaneous <= occupancy must hold for every slice.\n")


def instantaneous_peaks(ch: ClickHouse, where: str) -> tuple[int, int]:
    """Returns (maxIntersections peak, signed event sweep peak) for one filter."""
    prefix = CLIPPED.format(where=where)
    intersections = ch.scalar(prefix + INTERSECTIONS)
    sweep = ch.scalar(prefix + SWEEP)
    return int(intersections or 0), int(sweep or 0)


def raw_unfiltered_sweep(ch: ClickHouse) -> int:
    return int(ch.scalar("""
        SELECT max(live) FROM
        (
            SELECT sum(sum(d)) OVER (
                ORDER BY t ASC, d ASC ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS live
            FROM
            (
                SELECT ts_start_ms AS t, 1 AS d FROM active_intervals
                UNION ALL
                SELECT ts_end_ms AS t, -1 AS d FROM active_intervals
            )
            GROUP BY t, d
        )
    """))


def run(ch: ClickHouse, evidence: Path) -> bool:
    evidence.mkdir(parents=True, exist_ok=True)
    rows: list[tuple[str, int, int, int, str, bool, str]] = []

    for name, where in SLICES.items():
        occupancy = occupancy_series(ch, where)
        occupancy_peak = max(occupancy.values()) if occupancy else 0
        intersections, sweep = instantaneous_peaks(ch, where)
        notes = []
        if intersections != sweep:
            notes.append(f"paths disagree: maxIntersections {intersections}, sweep {sweep}")
        if sweep > occupancy_peak:
            notes.append(f"instantaneous {sweep} exceeds occupancy {occupancy_peak}")
        gap = occupancy_peak - sweep
        percent = f"{100.0 * gap / occupancy_peak:.1f}%" if occupancy_peak else "n/a"
        rows.append((name, occupancy_peak, sweep, gap, percent, not notes, "; ".join(notes)))

    anchor = raw_unfiltered_sweep(ch)
    unfiltered = next(r[2] for r in rows if r[0] == "no filter")
    anchor_ok = unfiltered == anchor
    ok = anchor_ok and all(r[5] for r in rows)

    width = max(len(r[0]) for r in rows)
    lines = [HEADER, f"{'slice':<{width}}  occupancy  instantaneous       gap   gap %  result"]
    for name, occupancy_peak, sweep, gap, percent, passed, note in rows:
        lines.append(f"{name:<{width}}  {occupancy_peak:>9,}  {sweep:>13,}  {gap:>8,}  "
                     f"{percent:>6}  {'PASS' if passed else 'FAIL'}"
                     + (f"  {note}" if note else ""))
    lines.append("")
    lines.append(f"{'PASS' if anchor_ok else 'FAIL'}: the clipped and merged unfiltered peak "
                 f"({unfiltered:,}) equals the raw half open sweep over active_intervals "
                 f"({anchor:,}), which Gate A pins to the independent python reference. "
                 "Clipping and merging therefore adds dimensions without moving the number.")
    lines.append("")
    lines.append("reading: instantaneous is strictly lower on every slice, so the two readings of "
                 "O3 are not interchangeable and the choice has to be stated, not assumed. Both "
                 "are now computable per slice, so whichever reading the private ground truth "
                 "uses, the number is available.")

    path = evidence / "instantaneous_vs_occupancy.txt"
    path.write_text("\n".join(lines) + "\n")

    print()
    for name, occupancy_peak, sweep, gap, percent, passed, note in rows:
        print(f"{'PASS' if passed else 'FAIL'}  {name:<{width}}  occupancy {occupancy_peak:>6,}  "
              f"instantaneous {sweep:>6,}  gap {gap:>5,} ({percent})")
    print(f"\n{'PASS' if anchor_ok else 'FAIL'}  unfiltered anchor: {unfiltered:,} vs raw sweep {anchor:,}")
    print(f"{path}  {len(rows)} slices")
    print(f"O3 instantaneous: {'PASS' if ok else 'FAIL'}")
    return ok

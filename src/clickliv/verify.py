"""Gate A. Five independent paths to the same numbers, diffed row for row: the python
reference, SQL occupancy, SQL deltas, a half-open sweep and maxIntersections."""

from __future__ import annotations

import json
from pathlib import Path

from .ch import ClickHouse

SLICES = {
    "no filter": "1",
    "platform ANDROID_PHONE": "platform = 'ANDROID_PHONE'",
    "platform SONY_ANDROID_TV": "platform = 'SONY_ANDROID_TV'",
    "video_type live": "video_type = 'live'",
    "audio_language hin": "audio_language = 'hin'",
    "IPHONE in india": "platform = 'IPHONE' AND country = 'india'",
    "vod on Mweb": "video_type = 'vod' AND platform = 'Mweb'",
}


def load_reference_tables(ch: ClickHouse, artifacts: Path) -> None:
    ch.insert_csv("INSERT INTO ref_intervals FORMAT CSVWithNames",
                  artifacts / "reference_intervals.csv")
    ch.insert_csv("INSERT INTO ref_rollup FORMAT CSVWithNames",
                  artifacts / "reference_rollup.csv")


def occupancy_series(ch: ClickHouse, where: str) -> dict[int, int]:
    rows = ch.query(f"""
        SELECT minute, toInt64(sum(sessions))
        FROM minute_occupancy WHERE {where}
        GROUP BY minute HAVING sum(sessions) != 0
    """).rows
    return {int(m): int(c) for m, c in rows}


def delta_series(ch: ClickHouse, where: str) -> dict[int, int]:
    rows = ch.query(f"""
        SELECT m, cum FROM
        (
            SELECT m, sum(sum(d)) OVER (
                ORDER BY m ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS cum
            FROM
            (
                SELECT arrayJoin(range(lo, hi + 2)) AS m, toInt64(0) AS d
                FROM (SELECT min(minute) AS lo, max(minute) AS hi FROM minute_occupancy)
                UNION ALL
                SELECT minute AS m, toInt64(delta) AS d
                FROM minute_deltas WHERE {where}
            )
            GROUP BY m
        )
        WHERE cum != 0
    """).rows
    return {int(m): int(c) for m, c in rows}


def diff_tables(ch: ClickHouse, left: str, right: str, columns: str) -> tuple[int, int]:
    only_left = int(ch.scalar(
        f"SELECT count() FROM (SELECT {columns} FROM {left} EXCEPT SELECT {columns} FROM {right})"))
    only_right = int(ch.scalar(
        f"SELECT count() FROM (SELECT {columns} FROM {right} EXCEPT SELECT {columns} FROM {left})"))
    return only_left, only_right


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []

    def check(self, name: str, passed: bool, detail: str = "") -> None:
        self.rows.append((name, passed, detail))

    def render(self) -> bool:
        width = max(len(name) for name, _, _ in self.rows)
        print()
        for name, passed, detail in self.rows:
            print(f"{'PASS' if passed else 'FAIL'}  {name:<{width}}  {detail}")
        ok = all(passed for _, passed, _ in self.rows)
        print(f"\nGate A: {'PASS' if ok else 'FAIL'}  "
              f"({sum(p for _, p, _ in self.rows)}/{len(self.rows)} checks)")
        return ok


def run(ch: ClickHouse, artifacts: Path) -> bool:
    reference = json.loads((artifacts / "reference.json").read_text())
    report = Report()

    only_sql, only_ref = diff_tables(
        ch, "active_intervals", "ref_intervals",
        "video_session_id, segment_id, ts_start_ms, ts_end_ms")
    report.check("intervals: SQL == python reference", only_sql == 0 and only_ref == 0,
                 f"{only_sql} only in SQL, {only_ref} only in reference")

    rollup_columns = ("minute, platform, app_version, country, audio_language, "
                      "subtitle_language, player_version, content_id, video_resolution, "
                      "video_type, category, show_name, sessions")
    only_sql, only_ref = diff_tables(
        ch,
        f"(SELECT {rollup_columns.replace('sessions', 'toUInt32(sum(sessions)) AS sessions')} "
        f"FROM minute_occupancy GROUP BY {rollup_columns.replace(', sessions', '')})",
        "ref_rollup", rollup_columns)
    report.check("rollup: occupancy == python reference", only_sql == 0 and only_ref == 0,
                 f"{only_sql} only in SQL, {only_ref} only in reference")

    for name, where in SLICES.items():
        occupancy = occupancy_series(ch, where)
        deltas = delta_series(ch, where)
        mismatches = [m for m in set(occupancy) | set(deltas)
                      if occupancy.get(m, 0) != deltas.get(m, 0)]
        peak = max(occupancy.values()) if occupancy else 0
        report.check(f"deltas == occupancy, {name}", not mismatches,
                     f"{len(occupancy)} minutes, peak {peak}"
                     + (f", {len(mismatches)} mismatched" if mismatches else ""))

    intersections = int(ch.scalar(
        "SELECT maxIntersections(ts_start_ms, ts_end_ms) FROM active_intervals"))
    sweep = int(ch.scalar("""
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
    expected = int(reference["instantaneous_peak"])
    report.check("half-open sweep == python instantaneous peak", sweep == expected,
                 f"sweep {sweep}, reference {expected}")
    report.check("maxIntersections >= half-open sweep", intersections >= sweep,
                 f"maxIntersections {intersections}, sweep {sweep}, "
                 f"difference {intersections - sweep}")

    occupancy_peak = int(reference["peak_concurrency"])
    report.check("instantaneous peak <= occupancy peak", sweep <= occupancy_peak,
                 f"{sweep} <= {occupancy_peak}, gap {occupancy_peak - sweep}")

    return report.render()

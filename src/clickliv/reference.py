"""Ground truth for Gate A. Reads the raw CSV directly and owes ClickHouse nothing."""

from __future__ import annotations

import csv
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from .load import (CONTENT_OPTIONAL, CONTENT_TYPES, RAW_OPTIONAL, RAW_TYPES, open_text,
                   shape)

PAUSE = frozenset({"pause", "speed-pause", "adpause"})
RESUME = frozenset({"resume", "speed-resume", "adresume"})
STOP_TYPES = frozenset({"VideoError", "VideoSessionEnd"})

VOCABULARY = {
    "event_type": ("VideoSessionStart", "VideoPlay", "AppBackgrounded",
                   "AppForegrounded", *sorted(STOP_TYPES)),
    "event": tuple(sorted(PAUSE | RESUME)),
}

DIM_NAMES = (
    "platform", "app_version", "country", "audio_language",
    "subtitle_language", "player_version", "content_id", "video_resolution",
)

CONTENT_DIMS = ("video_type", "category", "show_name")


MINUTE_MS = 60_000


def thresholds() -> tuple[int, int]:
    """Milliseconds. Integer arithmetic end to end so no float rounding can split the paths."""
    return (round(float(os.environ.get("GAP_SECONDS", "90")) * 1000),
            round(float(os.environ.get("GRACE_SECONDS", "40")) * 1000))


@dataclass(slots=True)
class Event:
    ts: int
    kind: int
    dims: int


OTHER, START, PLAY, FG, PAUSED, BG, STOP = range(7)


def classify(event_type: str, event: str) -> int:
    """Kind doubles as the same-timestamp tie-break rank: deactivating events apply last."""
    if event_type == "VideoSessionStart":
        return START
    if event_type == "VideoPlay" or event.lower() in RESUME:
        return PLAY
    if event.lower() in PAUSE:
        return PAUSED
    if event_type == "AppBackgrounded":
        return BG
    if event_type == "AppForegrounded":
        return FG
    if event_type in STOP_TYPES:
        return STOP
    return OTHER


class DimPool:
    """Interns dimension tuples to ints; after finalize the id is the tuple's sorted
    rank, usable as the last term of the tie-break key."""

    def __init__(self) -> None:
        self.ids: dict[tuple, int] = {}
        self.tuples: list[tuple] = []

    def intern(self, values: tuple) -> int:
        got = self.ids.get(values)
        if got is None:
            got = len(self.tuples)
            self.ids[values] = got
            self.tuples.append(values)
        return got

    def finalize(self) -> list[int]:
        order = sorted(range(len(self.tuples)), key=lambda i: self.tuples[i])
        remap = [0] * len(self.tuples)
        for rank, old in enumerate(order):
            remap[old] = rank
        self.tuples = [self.tuples[old] for old in order]
        self.ids = {values: rank for rank, values in enumerate(self.tuples)}
        return remap


def rows_of(path: Path, types: dict[str, str], optional: dict[str, str] | None = None):
    """Same header, delimiter, gzip and optional-column handling the loader uses, so both
    paths read the file the same way."""
    sh = shape(path, types, optional)
    with open_text(path) as fh:
        reader = csv.reader(fh, delimiter=sh.delimiter)
        next(reader)
        for values in reader:
            yield dict(zip(sh.header, values))


def read_events(path: Path, pool: DimPool) -> dict[str, list[Event]]:
    sessions: dict[str, list[Event]] = {}
    for row in rows_of(path, RAW_TYPES, RAW_OPTIONAL):
        dims = pool.intern((
            sys.intern(row["platform"]),
            sys.intern(row["app_version"]),
            sys.intern(row["country"]),
            sys.intern(row["audio_language"]),
            sys.intern(row["subtitle_language"]),
            sys.intern(row["player_version"]),
            int(row["content_id"]),
            sys.intern(row.get("video_resolution", "")),
        ))
        event = Event(
            ts=int(row["event_timestamp"]),
            kind=classify(row["event_type"], row["event"]),
            dims=dims,
        )
        sessions.setdefault(row["video_session_id"], []).append(event)
    remap = pool.finalize()
    for events in sessions.values():
        for event in events:
            event.dims = remap[event.dims]
        events.sort(key=lambda e: (e.ts, e.kind, e.dims))
    return sessions


def active_intervals(events: list[Event], gap: int, grace: int) -> list[tuple[int, int]]:
    """Active while playing, foregrounded, and heartbeat-fresh; any exit closes the
    segment at that event, a gap or stream end closes it one heartbeat later."""
    out: list[tuple[float, float]] = []
    playing = True
    foreground = True
    start: float | None = None
    last: float | None = None

    for event in events:
        if start is not None and last is not None and event.ts - last > gap:
            out.append((start, last + grace))
            start = None

        kind = event.kind
        if kind == START:
            playing, foreground = True, True
        elif kind == PLAY:
            playing = True
        elif kind == PAUSED:
            playing = False
        elif kind == BG:
            foreground = False
        elif kind == FG:
            foreground = True
        elif kind == STOP:
            playing = False

        if playing and foreground:
            if start is None:
                start = event.ts
        elif start is not None:
            out.append((start, event.ts))
            start = None
        last = event.ts

    if start is not None and last is not None:
        out.append((start, last + grace))
    return [(a, b) for a, b in out if b > a]


def covered_minutes(intervals: list[tuple[int, int]]) -> list[int]:
    minutes: set[int] = set()
    for a, b in intervals:
        for minute in range(a // MINUTE_MS, (b - 1) // MINUTE_MS + 1):
            minutes.add(minute)
    return sorted(minutes)


def minute_dims(events: list[Event]) -> dict[int, int]:
    """Last dimension tuple seen in each minute that has events."""
    out: dict[int, int] = {}
    for event in events:
        out[event.ts // MINUTE_MS] = event.dims
    return out


def resolve(minutes: list[int], per_minute: dict[int, int]) -> list[tuple[int, int]]:
    """Attach to each covered minute the tuple in effect at the end of it."""
    timeline = sorted(set(minutes) | set(per_minute))
    wanted = set(minutes)
    current = None
    out = []
    for minute in timeline:
        if minute in per_minute:
            current = per_minute[minute]
        if minute in wanted and current is not None:
            out.append((minute, current))
    return out


def instantaneous_peak(intervals: list[tuple[int, int]]) -> tuple[int, int]:
    points = []
    for a, b in intervals:
        points.append((a, 1))
        points.append((b, -1))
    points.sort()
    best, best_at, live = 0, 0, 0
    for at, delta in points:
        live += delta
        if live > best:
            best, best_at = live, at
    return best, best_at


def build(raw_path: Path, content_path: Path) -> dict:
    gap, grace = thresholds()
    pool = DimPool()
    sessions = read_events(raw_path, pool)

    if not sessions:
        raise SystemExit(f"{raw_path} produced no events; nothing to build a reference from")

    content: dict[int, tuple[str, str, str]] = {}
    for row in rows_of(content_path, CONTENT_TYPES, CONTENT_OPTIONAL):
        cid = int(row["content_id"])
        if cid >= 0:
            content[cid] = (row["video_type"], row["category"],
                            row.get("show_name", ""))

    rollup: dict[tuple, int] = {}
    all_intervals: list[tuple[float, float]] = []
    segments = 0
    empty_sessions = 0
    session_minutes = 0
    minutes_without_events = 0

    first_minute = min(e.ts // MINUTE_MS for events in sessions.values() for e in events)
    last_minute = max(e.ts // MINUTE_MS for events in sessions.values() for e in events)

    labelled: list[tuple[str, int, int, int]] = []

    for session_id, events in sessions.items():
        intervals = active_intervals(events, gap, grace)
        segments += len(intervals)
        if not intervals:
            empty_sessions += 1
            continue
        all_intervals.extend(intervals)
        for index, (a, b) in enumerate(intervals, start=1):
            labelled.append((session_id, index, a, b))
        per_minute = minute_dims(events)
        minutes = covered_minutes(intervals)
        minutes_without_events += sum(1 for m in minutes if m not in per_minute)
        for minute, dims in resolve(minutes, per_minute):
            session_minutes += 1
            values = pool.tuples[dims]
            key = (minute,) + values + content.get(values[6], ("", "", ""))
            rollup[key] = rollup.get(key, 0) + 1

    totals: dict[int, int] = {}
    for key, count in rollup.items():
        totals[key[0]] = totals.get(key[0], 0) + count

    if not totals:
        raise SystemExit(
            f"{raw_path} has {len(sessions)} session(s) but none of them is ever active. "
            "Check GAP_SECONDS/GRACE_SECONDS and that event_type/event use the expected "
            "vocabulary; see docs/unseen-day.md.")

    peak_minute = max(totals, key=lambda m: (totals[m], -m))
    inst_peak, inst_at = instantaneous_peak(all_intervals)

    return {
        "params": {"gap_ms": gap, "grace_ms": grace},
        "sessions": len(sessions),
        "segments": segments,
        "sessions_with_no_active_time": empty_sessions,
        "session_minutes": session_minutes,
        "covered_minutes_without_events": minutes_without_events,
        "minutes_in_span": last_minute - first_minute + 1,
        "minutes_with_activity": len(totals),
        "rollup_rows": len(rollup),
        "peak_concurrency": totals[peak_minute],
        "peak_minute": peak_minute,
        "average_over_span": sum(totals.values()) / (last_minute - first_minute + 1),
        "average_over_active_minutes": sum(totals.values()) / len(totals),
        "instantaneous_peak": inst_peak,
        "instantaneous_peak_at": inst_at,
        "_rollup": rollup,
        "_totals": totals,
        "_intervals": labelled,
    }


def write(result: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    rollup = result.pop("_rollup")
    totals = result.pop("_totals")
    intervals = result.pop("_intervals")

    with (out_dir / "reference_intervals.csv").open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["video_session_id", "segment_id", "ts_start_ms", "ts_end_ms"])
        writer.writerows(sorted(intervals))

    with (out_dir / "reference_rollup.csv").open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["minute", *DIM_NAMES, *CONTENT_DIMS, "sessions"])
        for key in sorted(rollup):
            writer.writerow([*key, rollup[key]])

    with (out_dir / "reference_totals.csv").open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["minute", "sessions"])
        for minute in sorted(totals):
            writer.writerow([minute, totals[minute]])

    (out_dir / "reference.json").write_text(json.dumps(result, indent=2) + "\n")
    for key, value in result.items():
        print(f"{key:<32}{value}")

"""Carve a small deterministic fixture out of the real CSVs, whole sessions only."""

from __future__ import annotations

import csv
import hashlib
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW_CSV = ROOT / "data" / "ch-hackathon-raw-data.csv"
CONTENT_CSV = ROOT / "data" / "ch-hackathon-content-data.csv"
FIXTURES = ROOT / "fixtures"
EVENTS_OUT = FIXTURES / "events.csv"
CONTENT_OUT = FIXTURES / "content.csv"

BUCKETS = 48
BUCKET = 0
GAP_MS = 90_000
PAUSE_EVENTS = {"pause", "speed-pause", "AdPause"}
RESUME_EVENTS = {"resume", "speed-resume", "AdResume"}


def bucket_of(value: str) -> int:
    """Stable across machines and Python versions, unlike hash()."""
    digest = hashlib.blake2b(value.encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big") % BUCKETS


def field_index(path: Path) -> dict[str, int]:
    with path.open(newline="") as fh:
        header = next(csv.reader(fh))
    return {name: i for i, name in enumerate(header)}


def chosen_sessions(index: dict[str, int]) -> set[str]:
    """A session is in when any of its users hashes into the bucket, so no session is split."""
    user, session = index["user_id"], index["video_session_id"]
    keep: set[str] = set()
    with RAW_CSV.open(newline="") as fh:
        reader = csv.reader(fh)
        next(reader)
        for row in reader:
            if bucket_of(row[user]) == BUCKET:
                keep.add(row[session])
    return keep


def write_events(index: dict[str, int], sessions: set[str]) -> dict:
    """Lines are copied verbatim, so the fixture is a literal subset of the source file."""
    session_col = index["video_session_id"]
    per_session: dict[str, list[tuple[int, str, str]]] = defaultdict(list)
    users: dict[str, set[str]] = defaultdict(set)
    platforms: dict[str, set[str]] = defaultdict(set)
    content_ids: set[str] = set()
    written = 0

    with RAW_CSV.open(newline="") as source, EVENTS_OUT.open("w", newline="") as out:
        header = next(source)
        out.write(header)
        for line in source:
            row = next(csv.reader([line]))
            sid = row[session_col]
            if sid not in sessions:
                continue
            out.write(line)
            written += 1
            content_ids.add(row[index["content_id"]])
            uid = row[index["user_id"]]
            users[uid].add(sid)
            platforms[uid].add(row[index["platform"]])
            per_session[sid].append(
                (int(row[index["event_timestamp"]]),
                 row[index["event_type"]],
                 row[index["event"]]))

    return {"events": written, "sessions": per_session, "users": users,
            "platforms": platforms, "content_ids": content_ids}


def write_content(index: dict[str, int], content_ids: set[str]) -> int:
    column = index["content_id"]
    written = 0
    with CONTENT_CSV.open(newline="") as source, CONTENT_OUT.open("w", newline="") as out:
        header = next(source)
        out.write(header)
        for line in source:
            if next(csv.reader([line]))[column] in content_ids:
                out.write(line)
                written += 1
    return written


def variety(stats: dict) -> dict[str, int]:
    """Every case the sessionizer has to handle must be present, not assumed."""
    counts = dict.fromkeys(
        ("paused", "resumed", "backgrounded", "foregrounded", "errored", "ended",
         "multi_minute", "multi_interval", "multi_session_users", "multi_device_users"), 0)
    for events in stats["sessions"].values():
        stamps = sorted(t for t, _, _ in events)
        types = {kind for _, kind, _ in events}
        names = {name for _, _, name in events}
        counts["paused"] += bool(names & PAUSE_EVENTS)
        counts["resumed"] += bool(names & RESUME_EVENTS) or "VideoPlay" in types
        counts["backgrounded"] += "AppBackgrounded" in types
        counts["foregrounded"] += "AppForegrounded" in types
        counts["errored"] += "VideoError" in types
        counts["ended"] += "VideoSessionEnd" in types
        counts["multi_minute"] += stamps[-1] - stamps[0] > 120_000
        counts["multi_interval"] += any(b - a > GAP_MS for a, b in zip(stamps, stamps[1:]))
    counts["multi_session_users"] = sum(1 for s in stats["users"].values() if len(s) > 1)
    counts["multi_device_users"] = sum(1 for p in stats["platforms"].values() if len(p) > 1)
    return counts


def main() -> int:
    for path in (RAW_CSV, CONTENT_CSV):
        if not path.exists():
            raise SystemExit(f"missing source: {path}")
    FIXTURES.mkdir(exist_ok=True)

    raw_index = field_index(RAW_CSV)
    sessions = chosen_sessions(raw_index)
    stats = write_events(raw_index, sessions)
    content_rows = write_content(field_index(CONTENT_CSV), stats["content_ids"])

    print(f"rule  user_id blake2b bucket {BUCKET} of {BUCKETS}, whole sessions\n")
    for path, rows in ((EVENTS_OUT, stats["events"]), (CONTENT_OUT, content_rows)):
        print(f"{path.relative_to(ROOT)!s:<21}{rows:>8,} rows"
              f"{path.stat().st_size / 1e6:>8.2f} MB")
    print(f"\n{'sessions':<21}{len(stats['sessions']):>8,}")
    print(f"{'users':<21}{len(stats['users']):>8,}")
    print(f"{'content ids':<21}{len(stats['content_ids']):>8,}")

    missing = []
    for name, count in variety(stats).items():
        print(f"{name:<21}{count:>8,}" + ("   MISSING" if not count else ""))
        if not count:
            missing.append(name)
    if content_rows != len(stats["content_ids"]):
        missing.append("content rows for every referenced content_id")
    if missing:
        print("\nfixture is not representative: " + ", ".join(missing))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

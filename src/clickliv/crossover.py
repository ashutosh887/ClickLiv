"""Reproduces the problem statement's own dimension-crossover example with real
numbers, through marts.v_concurrency rather than the raw tables. See D30."""

from __future__ import annotations

import uuid
from pathlib import Path

from .answers import marts
from .ch import ClickHouse

SLICES = [
    {"label": "unfiltered",
     "country": "", "platform": "", "video_type": "", "content_id": 0},
    {"label": "platform=ANDROID_PHONE",
     "country": "", "platform": "ANDROID_PHONE", "video_type": "", "content_id": 0},
    {"label": "video_type=live",
     "country": "", "platform": "", "video_type": "live", "content_id": 0},
    {"label": "platform=ANDROID_PHONE, video_type=live",
     "country": "", "platform": "ANDROID_PHONE", "video_type": "live", "content_id": 0},
    {"label": "platform=SONY_ANDROID_TV",
     "country": "", "platform": "SONY_ANDROID_TV", "video_type": "", "content_id": 0},
]

CALL_ARGS = (
    "grain_minutes = 1, country = '{country}', platform = '{platform}', "
    "video_type = '{video_type}', content_id = {content_id}, "
    "minute_from = {minute_from}, minute_to = {minute_to}"
)


def peak_for_slice(ch: ClickHouse, spec: dict, minute_from: int, minute_to: int) -> dict:
    query_id = str(uuid.uuid4())
    args = CALL_ARGS.format(**spec, minute_from=minute_from, minute_to=minute_to)
    peak_minute, peak = ch.query(
        f"SELECT argMax(bucket_minute, peak_concurrency), max(peak_concurrency) "
        f"FROM {marts()}.v_concurrency({args})", query_id=query_id).rows[0]
    return {"label": spec["label"], "query_id": query_id,
            "peak_minute": int(peak_minute), "peak": int(peak)}


def run(ch: ClickHouse, evidence: Path) -> bool:
    minute_from, minute_to = ch.query(
        "SELECT min(minute), max(minute) FROM minute_occupancy").rows[0]
    rows = [peak_for_slice(ch, spec, int(minute_from), int(minute_to)) for spec in SLICES]

    distinct_minutes = {r["peak_minute"] for r in rows}
    lines = [
        "-- the problem statement's own example, reproduced through marts.v_concurrency\n",
        "-- 'platform and a content might peak at one minute, while platform + country\n",
        "-- might reach its peak at an entirely different minute'\n\n",
    ]
    for r in rows:
        lines.append(f"{r['label']:<42} peak {r['peak']:>6,}  at minute {r['peak_minute']}\n")
    lines.append(
        f"\n{len(distinct_minutes)} distinct peak minutes across {len(rows)} slices: "
        f"peak minute is not fixed across dimension combinations, exactly as the\n"
        f"problem statement's example says. D6 (filter, sum across excluded dims, then\n"
        f"max over minutes, never max first) is why marts.v_concurrency gets this right\n"
        f"automatically: each slice above is a real query against the served view, not\n"
        f"a hand-picked number.\n")
    (evidence / "dimension_crossover.txt").write_text("".join(lines))

    ch.command("SYSTEM FLUSH LOGS")
    print(f"evidence/dimension_crossover.txt  {len(rows)} slices, "
          f"{len(distinct_minutes)} distinct peak minutes")
    return len(distinct_minutes) > 1

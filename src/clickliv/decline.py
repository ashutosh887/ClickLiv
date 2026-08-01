"""Optional decline alerting (D30). Detection is a deterministic threshold on marts;
narration is one optional Bedrock call on top, off unless a key is set."""

from __future__ import annotations

import uuid
from pathlib import Path

from . import llm
from .ch import ClickHouse

DROP_THRESHOLD_PCT = 50.0
MIN_BASE_CONCURRENCY = 50

QUERY = """
SELECT minute, s, prev_s, round(100 * (1 - s / prev_s), 1) AS drop_pct
FROM
(
    SELECT minute, concurrency AS s,
           lagInFrame(concurrency, 1) OVER (ORDER BY minute) AS prev_s
    FROM marts.v_occupancy_minute(
        country = '', platform = '', video_type = '', content_id = 0,
        minute_from = {minute_from}, minute_to = {minute_to})
)
WHERE prev_s >= {min_base} AND s <= prev_s * (1 - {threshold} / 100)
ORDER BY drop_pct DESC
"""


def run(ch: ClickHouse, evidence: Path) -> bool:
    minute_from, minute_to = ch.query(
        "SELECT min(minute), max(minute) FROM minute_occupancy").rows[0]
    query_id = str(uuid.uuid4())
    rows = ch.query(QUERY.format(
        minute_from=int(minute_from), minute_to=int(minute_to),
        min_base=MIN_BASE_CONCURRENCY, threshold=DROP_THRESHOLD_PCT),
        query_id=query_id).dicts()

    lines = [
        f"-- optional problem-statement use case: alert on concurrency decline\n"
        f"-- rule: minute-over-minute drop >= {DROP_THRESHOLD_PCT:.0f}%, "
        f"base concurrency >= {MIN_BASE_CONCURRENCY}\n"
        f"-- deterministic threshold on marts.v_occupancy_minute, not an LLM call\n"
        f"-- (see module docstring for why); query_id {query_id}\n\n",
    ]
    if not rows:
        lines.append("no decline events crossed the threshold on the tuning data\n")
    for r in rows:
        lines.append(
            f"minute {r['minute']}: {int(r['prev_s'])} -> {int(r['s'])} sessions "
            f"({r['drop_pct']}% drop). possible causes per the problem statement: "
            f"asset ended, system issue, or content not engaging; the alert flags "
            f"the minute, a human or a downstream LLM call decides which.\n")

    narration = None
    if rows:
        narration, label = llm.narrate(
            "In one or two sentences, given these concurrency-decline alerts from a "
            "streaming platform (minute, before, after, percent drop), suggest which "
            "of the three named causes (asset ended, system issue, disengaging "
            "content) is most likely and why, from the pattern alone: "
            + "; ".join(f"minute {r['minute']}: {int(r['prev_s'])}->{int(r['s'])} "
                         f"({r['drop_pct']}%)" for r in rows),
            span_name="llm.decline_narration")
        if narration:
            lines.append(f"\nnarration ({label}, one call, optional, "
                          f"off by default): {narration}\n")

    (evidence / "decline_alerts.txt").write_text("".join(lines))

    ch.command("SYSTEM FLUSH LOGS")
    print(f"evidence/decline_alerts.txt       {len(rows)} decline event(s) flagged"
          f"{', narrated' if narration else ''}")
    return True

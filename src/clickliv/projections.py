"""Projection evidence. content_id sits last in minute_occupancy's ORDER BY (D7), so a
content_id filter only gets generic exclusion search on the base table; a projection
reordered by (content_id, minute) makes it a real prefix instead.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from .ch import ClickHouse

PROJECTION = "proj_content_minute"

QUERY = "SELECT sum(sessions) FROM minute_occupancy WHERE content_id = {content_id}"


def busiest_content_id(ch: ClickHouse) -> int:
    return int(ch.scalar(
        "SELECT content_id FROM minute_occupancy "
        "GROUP BY content_id ORDER BY count() DESC LIMIT 1"))


def explain(ch: ClickHouse, query: str, settings: dict | None = None) -> str:
    rows = ch.query(f"EXPLAIN indexes = 1, projections = 1 {query}",
                     settings=settings).rows
    return "\n".join(r[0] for r in rows)


def run(ch: ClickHouse, evidence: Path) -> bool:
    content_id = busiest_content_id(ch)
    query = QUERY.format(content_id=content_id)

    before = explain(ch, query, {"optimize_use_projections": 0})
    after = explain(ch, query)
    forced = explain(ch, query, {"force_optimize_projection_name": PROJECTION})

    query_id = str(uuid.uuid4())
    ch.query(query, query_id=query_id)
    rows = ch.query_log_rows("projections, read_rows", [query_id])
    used = (rows[0]["projections"], rows[0]["read_rows"])

    text = (
        f"-- query: {query}\n"
        f"-- content_id {content_id} chosen as the busiest, {ch.scalar(f'SELECT count() FROM minute_occupancy WHERE content_id = {content_id}')} rows\n\n"
        f"-- before, optimize_use_projections = 0, reads the base table\n{before}\n\n"
        f"-- after, default settings, the planner picks {PROJECTION} on its own\n{after}\n\n"
        f"-- forced, force_optimize_projection_name = '{PROJECTION}'\n{forced}\n\n"
        f"-- system.query_log for the query above: projections={used[0]}, "
        f"read_rows={used[1]}\n"
    )
    (evidence / "projections.txt").write_text(text)
    print(f"evidence/projections.txt        content_id {content_id}, "
          f"query_log.projections={used[0]}")
    return True

"""Read the pipeline's own telemetry back out of ClickStack. Its store is ClickHouse too."""

from __future__ import annotations

import os

from .ch import ClickHouse, Config

WATERFALL = """
SELECT SpanName, count() AS spans, round(sum(Duration) / 1e6, 1) AS ms
FROM otel_traces
WHERE ServiceName = 'clickliv' AND TraceId = '{trace}' AND SpanName != 'clickhouse.query'
GROUP BY SpanName ORDER BY ms DESC
"""

QUERIES = """
SELECT toUInt64OrZero(SpanAttributes['clickhouse.query_duration_ms'])  AS server_ms,
       toUInt64OrZero(SpanAttributes['clickhouse.read_rows'])          AS read_rows,
       toUInt64OrZero(SpanAttributes['clickhouse.read_bytes'])         AS read_bytes,
       substring(SpanAttributes['db.statement'], 1, 60)                AS statement
FROM otel_traces
WHERE ServiceName = 'clickliv' AND TraceId = '{trace}' AND SpanName = 'clickhouse.query'
ORDER BY read_rows DESC LIMIT 10
"""


def client() -> ClickHouse:
    return ClickHouse(Config(
        host=os.environ.get("CLICKSTACK_CH_HOST", "localhost"),
        port=int(os.environ.get("CLICKSTACK_CH_PORT", "8124")),
        user="clickliv", password="clickliv", database="default"))


def table(rows: list[dict]) -> None:
    if not rows:
        print("  no spans")
        return
    headers = list(rows[0])
    widths = [max(len(h), *(len(str(r[h])) for r in rows)) for h in headers]
    print("  " + "  ".join(h.ljust(w) for h, w in zip(headers, widths)))
    for row in rows:
        print("  " + "  ".join(str(row[h]).ljust(w) for h, w in zip(headers, widths)))


def report() -> int:
    ch = client()
    trace = ch.query(
        "SELECT TraceId FROM otel_traces WHERE ServiceName = 'clickliv' "
        "AND SpanName != 'clickliv.obs' ORDER BY Timestamp DESC LIMIT 1").rows
    if not trace:
        print("no clickliv traces in ClickStack. Is CLICKSTACK_OTLP set?")
        return 1
    trace_id = trace[0][0]
    print(f"trace {trace_id}\n\nstages")
    table(ch.query(WATERFALL.format(trace=trace_id)).dicts())
    print("\nqueries by rows read, server side")
    table(ch.query(QUERIES.format(trace=trace_id)).dicts())
    return 0

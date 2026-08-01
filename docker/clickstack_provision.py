"""Provision the ClickStack demo dashboard and saved searches over the HyperDX API."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from http.cookiejar import CookieJar

DASHBOARD = "ClickLiv pipeline telemetry"

INTRO = (
    "## ClickLiv pipeline telemetry\n\n"
    "Every panel below is real telemetry from this project's own pipeline. The CLI is "
    "traced by a stdlib OTLP exporter, so each run emits one trace: a root span for the "
    "command, a span per pipeline stage, a span per ingest, and a span per ClickHouse "
    "query. Before export the tracer reads `system.query_log` and attaches what the "
    "server itself recorded, so the query numbers are server side, not client wall clock.\n\n"
    "ClickStack stores all of it in ClickHouse, which is also the engine the pipeline "
    "runs on. Same database on both sides of the integration."
)

STAGES = """
SELECT SpanName AS stage,
       count() AS runs,
       round(max(Duration) / 1e6, 1) AS slowest_ms
FROM default.otel_traces
WHERE ServiceName = 'clickliv'
  AND (SpanName LIKE 'stage.%' OR SpanName LIKE 'ingest.%')
GROUP BY stage
ORDER BY slowest_ms DESC
"""

COMMANDS = """
SELECT SpanName AS command,
       count() AS runs,
       round(max(Duration) / 1e6, 1) AS slowest_ms
FROM default.otel_traces
WHERE ServiceName = 'clickliv' AND SpanName LIKE 'clickliv.%'
GROUP BY command
ORDER BY slowest_ms DESC
"""

QUERIES = """
SELECT toUInt64OrZero(SpanAttributes['clickhouse.query_duration_ms']) AS server_ms,
       toUInt64OrZero(SpanAttributes['clickhouse.read_rows'])         AS read_rows,
       toUInt64OrZero(SpanAttributes['clickhouse.memory_usage'])      AS memory_bytes,
       substring(SpanAttributes['db.statement'], 1, 90)               AS statement
FROM default.otel_traces
WHERE ServiceName = 'clickliv' AND SpanName = 'clickhouse.query'
ORDER BY read_rows DESC
LIMIT 20
"""

TIMELINE = """
SELECT toStartOfMinute(Timestamp) AS minute, count() AS spans
FROM default.otel_traces
WHERE ServiceName = 'clickliv'
GROUP BY minute
ORDER BY minute
"""

COUNTERS = (
    ("Spans collected", "SELECT count() AS spans FROM default.otel_traces "
                        "WHERE ServiceName = 'clickliv'"),
    ("Traced runs", "SELECT uniqExact(TraceId) AS runs FROM default.otel_traces "
                    "WHERE ServiceName = 'clickliv'"),
    ("ClickHouse queries traced", "SELECT count() AS queries FROM default.otel_traces "
                                  "WHERE ServiceName = 'clickliv' "
                                  "AND SpanName = 'clickhouse.query'"),
)

SEARCHES = (
    ("ClickLiv pipeline spans", "ServiceName = 'clickliv'",
     "Timestamp, SpanName, round(Duration / 1e6, 1) AS ms, ServiceName"),
    ("ClickLiv ClickHouse queries, server side",
     "ServiceName = 'clickliv' AND SpanName = 'clickhouse.query'",
     "Timestamp, SpanAttributes['clickhouse.query_duration_ms'] AS server_ms, "
     "SpanAttributes['clickhouse.read_rows'] AS read_rows, "
     "SpanAttributes['db.statement'] AS statement"),
)


class Client:
    """Session authenticated HyperDX API client on the standard library."""

    def __init__(self, base: str, email: str, password: str):
        self.base = base.rstrip("/")
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))
        self.call("POST", "/login/password", {"email": email, "password": password})

    def call(self, method: str, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            f"{self.base}{path}", data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=60) as response:
                raw = response.read().decode()
        except urllib.error.HTTPError as exc:
            raise SystemExit(f"{method} {path} {exc.code} {exc.read().decode()[:600]}")
        return json.loads(raw) if raw.startswith(("{", "[")) else raw


def sql_tile(name: str, sql: str, connection: str, source: str,
             display: str, box: tuple[int, int, int, int]) -> dict:
    x, y, w, h = box
    return {"id": name.lower().replace(" ", "-").replace(",", ""),
            "x": x, "y": y, "w": w, "h": h,
            "config": {"name": name, "configType": "sql", "sqlTemplate": sql.strip(),
                       "connection": connection, "source": source,
                       "displayType": display}}


def tiles(connection: str, source: str) -> list[dict]:
    layout = [{"id": "intro", "x": 0, "y": 0, "w": 24, "h": 5,
               "config": {"name": "What this is", "source": source,
                          "displayType": "markdown", "markdown": INTRO,
                          "select": "", "where": ""}}]
    for index, (name, sql) in enumerate(COUNTERS):
        layout.append(sql_tile(name, sql, connection, source, "number",
                               (index * 8, 5, 8, 4)))
    layout.append(sql_tile("Pipeline stage timings", STAGES, connection, source,
                           "table", (0, 9, 12, 10)))
    layout.append(sql_tile("Commands traced end to end", COMMANDS, connection, source,
                           "table", (12, 9, 12, 10)))
    layout.append(sql_tile("ClickHouse queries by rows read, server side", QUERIES,
                           connection, source, "table", (0, 19, 24, 11)))
    layout.append(sql_tile("Spans per minute", TIMELINE, connection, source,
                           "line", (0, 30, 24, 8)))
    return layout


def main() -> int:
    base = os.environ.get("HYPERDX_URL", "http://localhost:8080") + "/api"
    client = Client(base, os.environ["HYPERDX_EMAIL"], os.environ["HYPERDX_PASSWORD"])
    source = next(s for s in client.call("GET", "/sources") if s["kind"] == "trace")
    connection = source["connection"]

    for existing in client.call("GET", "/dashboards"):
        if existing["name"] == DASHBOARD:
            client.call("DELETE", f"/dashboards/{existing['id']}")
    dashboard = client.call("POST", "/dashboards", {
        "name": DASHBOARD, "tags": [], "tiles": tiles(connection, source["id"])})
    client.call("PUT", "/favorites",
                {"resourceType": "dashboard", "resourceId": dashboard["id"]})
    print(f"dashboard {dashboard['id']} {DASHBOARD}, starred")

    saved = {s["name"]: s["id"] for s in client.call("GET", "/saved-search")}
    for name, where, select in SEARCHES:
        if name in saved:
            client.call("DELETE", f"/saved-search/{saved[name]}")
        search = client.call("POST", "/saved-search", {
            "name": name, "tags": [], "source": source["id"], "select": select,
            "where": where, "whereLanguage": "sql",
            "orderBy": "Timestamp DESC"})
        client.call("PUT", "/favorites",
                    {"resourceType": "savedSearch", "resourceId": search["id"]})
        print(f"saved search {search['id']} {name}, starred")
    return 0


if __name__ == "__main__":
    sys.exit(main())

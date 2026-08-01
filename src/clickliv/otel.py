"""OTLP tracing over the stdlib. Query spans carry server-side metrics, never client wall clock (D14)."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager

from .ch import redact

SERVER_METRICS = ("query_duration_ms", "read_rows", "read_bytes", "result_rows", "memory_usage")


def attribute(key: str, value) -> dict:
    if isinstance(value, bool):
        payload = {"boolValue": value}
    elif isinstance(value, int):
        payload = {"intValue": str(value)}
    elif isinstance(value, float):
        payload = {"doubleValue": value}
    else:
        payload = {"stringValue": str(value)}
    return {"key": key, "value": payload}


def note(record: dict | None, **attributes) -> None:
    if record is not None:
        record["attributes"] += [attribute(k, v) for k, v in attributes.items()]


class Tracer:
    """A no-op unless CLICKSTACK_OTLP is set, so the default pipeline is byte identical."""

    def __init__(self, endpoint: str | None = None, key: str | None = None,
                 service: str = "clickliv"):
        self.endpoint = (endpoint or "").rstrip("/")
        self.key = key or ""
        self.service = service
        self.trace_id = uuid.uuid4().hex
        self.spans: list[dict] = []
        self.stack: list[str] = []
        self.by_query: dict[str, dict] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.endpoint)

    @contextmanager
    def span(self, name: str, **attributes):
        if not self.enabled:
            yield None
            return
        record = self.open(name, attributes)
        self.stack.append(record["spanId"])
        try:
            yield record
        except BaseException as exc:
            record["status"] = {"code": 2, "message": str(exc)[:400]}
            raise
        finally:
            self.stack.pop()
            record["endTimeUnixNano"] = str(time.time_ns())

    def open(self, name: str, attributes: dict, kind: int = 1,
             start_ns: int | None = None) -> dict:
        record = {
            "traceId": self.trace_id,
            "spanId": uuid.uuid4().hex[:16],
            "name": name,
            "kind": kind,
            "startTimeUnixNano": str(start_ns if start_ns is not None else time.time_ns()),
            "attributes": [attribute(k, v) for k, v in attributes.items()],
        }
        if self.stack:
            record["parentSpanId"] = self.stack[-1]
        self.spans.append(record)
        return record

    def attach(self, ch) -> None:
        if self.enabled:
            ch.observer = self.observe

    def observe(self, sql: str, query_id: str, start_ns: int, end_ns: int,
                error: str | None) -> None:
        record = self.open("clickhouse.query", {
            "db.system": "clickhouse",
            "db.query_id": query_id,
            "db.statement": redact(sql)[:400],
        }, kind=3, start_ns=start_ns)
        record["endTimeUnixNano"] = str(end_ns)
        if error:
            record["status"] = {"code": 2, "message": error[:400]}
        self.by_query[query_id] = record

    def enrich(self, ch) -> None:
        """Replace client timings with what the server recorded for the same query_id."""
        if not self.by_query:
            return
        ch.observer = None
        ids = ",".join(f"'{q}'" for q in self.by_query)
        try:
            ch.command("SYSTEM FLUSH LOGS")
            rows = ch.query(
                f"SELECT query_id, {', '.join(SERVER_METRICS)} FROM system.query_log "
                f"WHERE type != 'QueryStart' AND event_date >= today() - 1 "
                f"AND query_id IN ({ids})").dicts()
        except Exception as exc:
            print(f"clickstack: query_log enrichment skipped, {exc}")
            return
        for row in rows:
            record = self.by_query.get(row["query_id"])
            if record is None:
                continue
            record["attributes"] += [
                attribute(f"clickhouse.{name}", int(row[name])) for name in SERVER_METRICS
            ]

    def export(self, ch) -> None:
        if not self.enabled or not self.spans:
            return
        self.enrich(ch)
        payload = json.dumps({"resourceSpans": [{
            "resource": {"attributes": [
                attribute("service.name", self.service),
                attribute("deployment.environment", ch.config.host),
            ]},
            "scopeSpans": [{"scope": {"name": "clickliv"}, "spans": self.spans}],
        }]}).encode()
        request = urllib.request.Request(
            f"{self.endpoint}/v1/traces", data=payload, method="POST",
            headers={"Content-Type": "application/json", "authorization": self.key})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                response.read()
        except (urllib.error.URLError, OSError) as exc:
            print(f"clickstack: {len(self.spans)} spans not delivered, {exc}")
            return
        print(f"clickstack: {len(self.spans)} spans, trace {self.trace_id}")


TRACER = Tracer()


def span(name: str, **attributes):
    return TRACER.span(name, **attributes)

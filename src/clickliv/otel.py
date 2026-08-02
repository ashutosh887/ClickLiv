"""OTLP tracing over the stdlib. Query spans carry server-side metrics, never client wall clock (D14)."""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field

from .ch import redact

SERVER_METRICS = ("query_duration_ms", "read_rows", "read_bytes", "result_rows", "memory_usage")


@dataclass
class Sink:
    """One OTLP/HTTP JSON destination. ClickStack observes the pipeline, Langfuse the LLM calls."""

    name: str
    url: str
    headers: dict = field(default_factory=dict)


def sinks_from_env() -> list[Sink]:
    sinks = []
    clickstack = os.environ.get("CLICKSTACK_OTLP")
    if clickstack:
        sinks.append(Sink("clickstack", f"{clickstack.rstrip('/')}/v1/traces",
                          {"authorization": os.environ.get("CLICKSTACK_KEY", "")}))
    host = os.environ.get("LANGFUSE_HOST")
    public, secret = os.environ.get("LANGFUSE_PUBLIC_KEY"), os.environ.get("LANGFUSE_SECRET_KEY")
    if host and public and secret:
        token = base64.b64encode(f"{public}:{secret}".encode()).decode()
        sinks.append(Sink("langfuse", f"{host.rstrip('/')}/api/public/otel/v1/traces",
                          {"Authorization": f"Basic {token}",
                           "x-langfuse-ingestion-version": "4"}))
    return sinks


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
    """A no-op unless a sink is configured, so the default pipeline is byte identical."""

    def __init__(self, sinks: list[Sink] | None = None, service: str = "clickliv"):
        self.sinks = sinks or []
        self.service = service
        self.trace_id = uuid.uuid4().hex
        self.spans: list[dict] = []
        self.stack: list[str] = []
        self.by_query: dict[str, dict] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.sinks)

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
        statement = redact(sql)[:400]
        record = self.open("clickhouse.query", {
            "db.system": "clickhouse",
            "db.query_id": query_id,
            "db.statement": statement,
            "langfuse.observation.input": statement,
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
        try:
            rows = ch.query_log_rows(f"query_id, {', '.join(SERVER_METRICS)}",
                                     list(self.by_query), retries=12, wait=2.5)
        except Exception as exc:
            print(f"clickstack: WARNING query_log enrichment failed, "
                  f"{len(self.by_query)} query spans ship without server metrics, {exc}")
            return
        enriched = 0
        for row in rows:
            record = self.by_query.get(row["query_id"])
            if record is None:
                continue
            metrics = {name: int(row[name]) for name in SERVER_METRICS}
            record["attributes"] += [
                attribute(f"clickhouse.{name}", value) for name, value in metrics.items()
            ]
            note(record, **{"langfuse.observation.output": json.dumps(metrics)})
            enriched += 1
        missing = len(self.by_query) - enriched
        if missing:
            print(f"clickstack: WARNING {missing} of {len(self.by_query)} query spans "
                  f"have no system.query_log row, their latency is client side")

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
        for sink in self.sinks:
            self.deliver(sink, payload)

    def flush(self, ch) -> None:
        """Ship and start a new trace, so a long lived server emits one trace per request."""
        if not self.enabled or not self.spans:
            return
        self.export(ch)
        self.spans.clear()
        self.by_query.clear()
        self.trace_id = uuid.uuid4().hex

    def deliver(self, sink: Sink, payload: bytes) -> None:
        request = urllib.request.Request(
            sink.url, data=payload, method="POST",
            headers={"Content-Type": "application/json", **sink.headers})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200]
            print(f"{sink.name}: {len(self.spans)} spans rejected, {exc.code} {detail}")
            return
        except (urllib.error.URLError, OSError) as exc:
            print(f"{sink.name}: {len(self.spans)} spans not delivered, {exc}")
            return
        print(f"{sink.name}: {len(self.spans)} spans, trace {self.trace_id}")


TRACER = Tracer()


def span(name: str, **attributes):
    return TRACER.span(name, **attributes)


def generation(name: str, model: str, prompt: str):
    """An LLM span Langfuse renders as a generation, priced from the usage attributes."""
    return TRACER.span(name, **{
        "langfuse.observation.type": "generation",
        "gen_ai.request.model": model,
        "gen_ai.prompt": prompt,
    })


def completed(record: dict | None, output: str, usage: dict | None = None) -> None:
    usage = usage or {}
    note(record, **{"gen_ai.completion": output})
    for key, attribute_name in (("input_tokens", "gen_ai.usage.input_tokens"),
                                ("output_tokens", "gen_ai.usage.output_tokens")):
        if usage.get(key) is not None:
            note(record, **{attribute_name: int(usage[key])})

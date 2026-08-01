# The MCP surface, where a model can ask

`make mcp` runs `src/clickliv/mcp.py`, a Streamable HTTP MCP server on port 8765 at
`/mcp`, on the standard library like the rest of the project. It exposes four
pre-vetted, parameterized tools over the `marts` views and nothing else:
`concurrency_peak`, `concurrency_series`, `top_slices`, `list_dimensions`. Set
`MCP_PORT` to move it; `docker/librechat.yaml` expects 8765.

## The guardrails

The model never emits SQL. Filter values are checked against an allowlist of real
dimension values and integers against explicit bounds, and whatever survives reaches
ClickHouse as a bound query parameter, never as text spliced into a statement. The
server also connects as `marts_agent` rather than as the pipeline's own user, so the
query budget is enforced by ClickHouse and not by this project's good intentions.
Checked live against the Cloud service rather than argued:

```
marts_agent SELECT ON clickliv.minute_occupancy   Code 497, not enough privileges
marts_agent SELECT ON clickliv.raw_events         Code 497
marts_agent SELECT ON clickliv.active_intervals   Code 497
marts_agent SELECT ON system.query_log            Code 497
marts_agent SET max_execution_time = 600          Code 164, readonly = 1 CONST
platform = "ANDROID_PHONE' OR 1=1 --"             tool error, before any SQL is built
```

The role and the settings profile behind those refusals are described in
[serving.md](serving.md#rbac-and-the-query-budget).

## Every answer carries its own receipt

Every answer the server returns ends with its `query_id`, the rows the server read, the
server-side elapsed time, and the user it ran as, so a reader can go and check it. The
rows-read figure comes out of the response's own statistics block; verified byte
identical to `system.query_log.read_rows` for the same `query_id`, 96,818 rows both
ways on the unfiltered day-grain call.

## The proven round trip

Proven end to end through LibreChat itself, not just against the server directly: asked
"what is the peak foreground-only concurrency, and what was it for platform
ANDROID_PHONE", `gpt-5.2` called `concurrency_peak` on the `clickliv-marts` surface
twice and answered 2,692 and 1,704, both exact. `system.query_log` shows the matching
two queries from `marts_agent` against `marts.v_concurrency` in the same window. Full
transcript and the cross-check in `evidence/conversational_layer.txt`. In the UI, the
MCP tool picker must have `clickliv-marts` turned on per conversation; it is off by
default until chosen.

## LibreChat v0.8.7 talks to two MCP surfaces, and says which one it used

`make chat-up` brings it up from `docker compose --profile chat`, with OpenAI `gpt-5.2`
as the model provider and MongoDB for its own state. Meilisearch
and the RAG API, the two optional sidecars, are left out on purpose: neither is needed
to chat over MCP. `docker/librechat.yaml` wires in `clickliv-marts`, the guardrailed
server above, and `clickhouse-official`, the official ClickHouse MCP server
(`ghcr.io/clickhouse/mcp-clickhouse:0.4.1`, `CLICKHOUSE_ALLOW_WRITE_ACCESS=false` and
`CLICKHOUSE_ALLOW_DROP=false`, on 8766). The guardrailed server is the default, because
its answers are the numbers the pipeline publishes and its budget is enforced
server-side; the official server is the labelled escape hatch for schema exploration
and ad hoc aggregates that have no mart behind them, and its instructions require the
model to show the SQL it ran, so a reader can tell an ad hoc query from a published
mart.

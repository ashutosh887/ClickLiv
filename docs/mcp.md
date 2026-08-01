# The MCP surface, where a model can ask

`make mcp` runs `src/clickliv/mcp.py`, a Streamable HTTP MCP server on port 8765 at
`/mcp`, on the standard library like the rest of the project. It exposes four
pre-vetted, parameterized tools over the `marts` views and nothing else:
`concurrency_peak`, `concurrency_series`, `top_slices`, `list_dimensions`. Set
`MCP_PORT` to move it; `docker/librechat.yaml` expects 8765.

It binds every interface, because the LibreChat container has to reach it. On Linux
`host.docker.internal` resolves to the docker bridge address, not to loopback, so a
loopback-bound server is unreachable from the container no matter how correct the
hostname is. That is not a theoretical point: it is how the guardrailed surface came
to attach zero tools on the EC2 box while its instructions were still injected, so the
model answered from the escape hatch and said the answer came from the marts surface.
Port 8765 must therefore stay closed at the firewall; the demo security group allows
only 22, 80 and 443, and an off-box request to 8765 times out. `MCP_HOST` narrows the
bind again where the container is not in the way.

## A question has to answer itself

The tools default to the whole dataset. `concurrency_peak` with no arguments at all
returns the busiest minute in the extract, so "what was the busiest time" is one call
with an empty argument object and a missing time range is never a reason to withhold
an answer. Leaving a filter out means no filter on that dimension. There is no `ALL`
value to pass, but `ALL`, `ANY`, `NONE`, `NULL`, `*`, `%` and the empty string are all
accepted and all mean the same thing, in any case, because a model writing a filter by
hand reaches for one of those long before it reaches for the empty string, and an
unrecognised sentinel returns zero rows rather than an error.

Values match case insensitively, with one deliberate subtlety: an exact value always
wins, and the case fold only applies when the value matches nothing at all. `LIVE`
finds `live`, but `hin` and `HIN` are two different real slices of `audio_language`
and stay that way. Folding them together would quietly turn the 1,614 headline into
1,899.

`list_dimensions` returns both the accepted values and the window the data actually
covers, in epoch minutes and in UTC. The dataset is a fixed historical extract running
from 2026-07-14 15:43 UTC to 2026-07-26 11:30 UTC, so a range built from `now()` finds
nothing; the tool description says so and the model is told to call it before naming
any date.

A question about a named programme goes through the `title` argument on
`concurrency_peak` and `concurrency_series`, which resolves the title against
`marts.v_titles` and returns the content_id behind it. If the dataset holds no such
title the call fails and says so, and if the title is ambiguous the error lists the
near matches with their ids. It never falls through to the unfiltered total, which is
what used to happen: asked how many watched a show the dataset does not contain, the
answer was the whole-dataset peak with the show's name on it and a source citation
underneath.

## The guardrails

The model never emits SQL. Filter values are checked against an allowlist of real
dimension values and integers against explicit bounds, and whatever survives reaches
ClickHouse as a bound query parameter, never as text spliced into a statement. That
holds for the title lookup too: the title is bound into
`positionCaseInsensitive(title, {needle:String})`, never concatenated. The server
connects as `marts_agent` rather than as the pipeline's own user, so the query budget
is enforced by ClickHouse and not by this project's good intentions. Checked live
against the Cloud service rather than argued, and re-checked after every rebuild of
the `marts` database:

```
marts_agent SELECT ON clickliv.minute_occupancy   Code 497, not enough privileges
marts_agent SELECT ON clickliv.raw_events         Code 497
marts_agent SELECT ON clickliv.active_intervals   Code 497
marts_agent SELECT ON system.query_log            Code 497
marts_agent SET max_execution_time = 600          Code 164, readonly = 1 CONST
platform = "ANDROID_PHONE' OR 1=1 --"             tool error, before any SQL is built
platform = "NOPE"                                 tool error, names the ten real values
```

The role and the settings profile behind those refusals are described in
[serving.md](serving.md#rbac-and-the-query-budget).

## What the marts database publishes

The MCP tools read the first three. The rest exist so that a model exploring the
schema on the escape hatch learns the conventions instead of guessing them, and every
view and column carries a ClickHouse `COMMENT`, so `SHOW CREATE` and `DESCRIBE` teach
the call syntax and the sentinel rule.

| view | what it is for |
| --- | --- |
| `v_occupancy_minute` | per-minute concurrency, the four common dimensions, six parameters |
| `v_concurrency` | the same bucketed to a grain, seven parameters |
| `v_occupancy_full` | all eight sort key dimensions, eleven parameters |
| `v_concurrency_full` | all eight plus a grain, twelve parameters |
| `v_data_window` | the window the data covers, in epoch minutes and UTC |
| `v_dimension_values` | every value every string dimension takes, 225 of them |
| `v_titles` | every content_id that carries sessions, with its title |
| `v_naive_vs_foreground` | the foreground count against the naive one, minute by minute |
| `v_overcount` | the same comparison as a single row |

The shorter pair is kept separate rather than widened because the Vercel functions,
the Cloud dashboard and this MCP server all call it with six parameters, and a
ClickHouse parameterized view has no defaults, so widening the signature in place
would break every caller the moment the SQL is applied.

`v_dimension_values` deliberately carries no concurrency figure. A peak per value has
to sum across the other dimensions before the maximum is taken, and a `GROUP BY` there
would take the maximum first and publish a number that is quietly too small. Peaks per
value come from `top_slices` or from the concurrency views with the dimension
filtered.

`v_overcount` puts the project's headline claim behind a query instead of behind
prose: 3,743 naive against 2,692 foreground, 39.0% on the peak and 49.0% on the
average, and the two peaks land in different minutes. Asked in chat how much counting
every open session would overcount, the model reads that one row.

## Every answer carries its own receipt

Every answer the server returns ends with its `query_id`, the rows the server read, the
server-side elapsed time, and the user it ran as, so a reader can go and check it. The
rows-read figure comes out of the response's own statistics block; verified byte
identical to `system.query_log.read_rows` for the same `query_id`, 96,818 rows both
ways on the unfiltered day-grain call.

## The proven round trip

Proven end to end through LibreChat itself, not just against the server directly, and
with `clickliv-marts` requested alone so there is no escape hatch to fall back on.
Asked "What has been the most busiest time?", `gpt-5.2` made one call to
`concurrency_peak` with an empty argument object and answered 2,692 at 2026-07-26
10:56 UTC. Asked how live compares to vod it called the tool twice and answered 425
and 2222, where it previously reported both as zero. Asked about a programme the
dataset does not contain it refused rather than reporting the total. Full transcripts,
including the failures these replaced, in `evidence/conversational_layer.txt`.

In the UI, the MCP tool picker must have `clickliv-marts` turned on per conversation;
it is off by default until chosen. LibreChat's `GET /api/mcp/connection/status` reports
`disconnected` for both servers until the first tool call of a session establishes the
per-user connection, on both the laptop and the EC2 box, so it is a false negative;
`GET /api/mcp/tools` is the endpoint that tells you whether the tools are really
attached.

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

Both servers carry long `serverInstructions`, and they are load bearing rather than
decorative. The guardrailed one names the busiest-time phrasings and says to call the
tool with no arguments, forbids asking the user for a time window or for valid filter
values, and hands off explicitly when the question filters on a dimension the tools do
not take. The escape hatch one states the dataset window, states the sentinel rule,
and carries a copyable example of the parameterized call syntax, because parameters go
inside the parentheses as `name = value` and a model that has not been shown that puts
them in a `WHERE` clause and gets "unknown expression identifier" instead of an answer.

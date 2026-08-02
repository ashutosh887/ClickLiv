# The MCP surface, where a model can ask

`make mcp` runs `src/clickliv/mcp.py`, a Streamable HTTP MCP server on port 8765 at
`/mcp`, on the standard library like the rest of the project. It exposes five
pre-vetted, parameterized tools over the `marts` views and nothing else:
`concurrency_peak`, `concurrency_series`, `top_slices`, `overcount`,
`list_dimensions`. Set `MCP_PORT` to move it; `docker/librechat.yaml` expects 8765.
On the EC2 box it runs as the `clickliv-mcp` systemd unit.

It binds every interface, because the LibreChat container has to reach it. On Linux
`host.docker.internal` resolves to the docker bridge address, not to loopback, so a
loopback-bound server is unreachable from the container no matter how correct the
hostname is. That is not a theoretical point: it is how the guardrailed surface came
to attach zero tools on the EC2 box while its instructions were still injected, so the
model answered from the escape hatch and said the answer came from the marts surface.
Port 8765 must therefore stay closed at the firewall; the demo security group allows
only 22, 80 and 443, and an off-box request to 8765 times out. `MCP_HOST` narrows the
bind again where the container is not in the way.

## Nothing here is baked in against one dataset

The accepted filter values are read out of `marts.v_dimension_values` at runtime, held
for five minutes, and re-read immediately whenever a value misses, so a replacement
dataset with new platforms needs no code change and no redeploy. The JSON schema each
tool advertises has its `enum` filled from that same read at `tools/list` time rather
than from a list in the source. There is no hardcoded content id, no hardcoded
dimension value, no assumed time window and no assumed cardinality anywhere in the
server; every window comes from `marts.v_data_window` and every value from the data.

## A question has to answer itself

The tools default to the whole dataset. `concurrency_peak` with no arguments at all
returns the busiest minute in the extract, so "what was the busiest time" is one call
with an empty argument object and a missing time range is never a reason to withhold
an answer. Leaving a filter out means no filter on that dimension. There is no `ALL`
value to pass, but `ALL`, `ANY`, `NONE`, `NULL`, `*`, `%` and the empty string are all
accepted and all mean the same thing, in any case, because a model writing a filter by
hand reaches for one of those long before it reaches for the empty string, and an
unrecognised sentinel returns zero rows rather than an error.

An empty match is never returned as a number. When nothing matches, the answer says so,
repeats the filters it was actually given after canonicalisation, states the window the
data covers in both epoch minutes and UTC, and tells the model to name the part of the
question the data does not cover. That is what turns a silent zero into a refusal.

Every concurrency answer also states its unit, that it counts video sessions in the
foreground at the same moment and not distinct people or accounts, so "how many people
watched" gets a real number with the ambiguity named rather than a number that quietly
answers a different question. User-level concurrency is deliberately not served; the
evidence that it would be safe to serve is in `evidence/user_level.txt`.

Values match case insensitively, with two deliberate subtleties. An exact value always
wins, so `hin` and `HIN` stay two different real slices of `audio_language`. The case
fold only applies when the value matches nothing exactly, so `LIVE` still finds `live`.
And the fold now refuses when it would land on more than one real value, so a third
casing such as `Hin` matches nothing at all instead of quietly merging both and turning
the 1,614 headline into 1,899. Refusing is the honest answer there, and it matches how
the rest of the surface behaves.

`list_dimensions` returns both the accepted values and the window the data actually
covers, in epoch minutes and in UTC. The dataset is a fixed historical extract, so a
range built from `now()` finds nothing; the tool description says so, and both sets of
`serverInstructions` tell the model to resolve relative phrases such as "last week"
against the last minute the data holds rather than against today.

A question about a named programme goes through the `title` argument on
`concurrency_peak` and `concurrency_series`, which resolves the title against
`marts.v_titles` and returns the content_id behind it. If the dataset holds no such
title the call fails and says so, and if the title is ambiguous the error lists the
near matches with their ids. It never falls through to the unfiltered total, which is
what used to happen: asked how many watched a show the dataset does not contain, the
answer was the whole-dataset peak with the show's name on it and a source citation
underneath.

## The guardrails

The model never emits SQL. Filter values are checked against the values the data holds
and integers against explicit bounds, and whatever survives reaches ClickHouse as a
bound query parameter, never as text spliced into a statement. That holds for the title
lookup too: the title is bound into `positionCaseInsensitive(title, {needle:String})`,
never concatenated. The server connects as `marts_agent` rather than as the pipeline's
own user, so the query budget is enforced by ClickHouse and not by this project's good
intentions. Checked live against the Cloud service rather than argued, and re-checked
after every rebuild of the `marts` database, by `tests/test_mcp.py`:

```
marts_agent SELECT ON clickliv.minute_occupancy   Code 497, not enough privileges
marts_agent SELECT ON clickliv.raw_events         Code 497
marts_agent SELECT ON system.query_log            Code 497
marts_agent CREATE TABLE marts.nope               refused, readonly = 1 CONST
marts_agent DROP DATABASE marts                   refused
marts_agent INSERT INTO marts.dimension_value     refused
marts_agent SET max_execution_time = 600          Code 164, readonly = 1 CONST
platform = "ANDROID_PHONE' OR 1=1 --"             tool error, before any SQL is built
platform = "Roku"                                 tool error, names the real values
```

The role and the settings profile behind those refusals are described in
[serving.md](serving.md#rbac-and-the-query-budget).

## What the marts database publishes

The MCP tools read `v_concurrency`, `v_occupancy_minute`, `v_data_window`,
`v_dimension_values`, `v_titles` and `v_overcount`. The rest exist so that a model
exploring the schema on the escape hatch learns the conventions instead of guessing
them, and every view and column carries a ClickHouse `COMMENT`, so `SHOW CREATE` and
`DESCRIBE` teach the call syntax and the sentinel rule.

| view | what it is for |
| --- | --- |
| `v_occupancy_minute` | per-minute concurrency, the four common dimensions, six parameters |
| `v_concurrency` | the same bucketed to a grain, seven parameters |
| `v_occupancy_full` | all eight sort key dimensions, eleven parameters |
| `v_concurrency_full` | all eight plus a grain, twelve parameters |
| `v_data_window` | the window the data covers, in epoch minutes and UTC |
| `v_dimension_values` | every value every string dimension takes |
| `v_titles` | every content_id that carries sessions, with its title |
| `v_naive_vs_foreground` | the foreground count against the naive one, minute by minute |
| `v_overcount` | the same comparison as a single row |
| `dimension_value` | a small table, not a view: the distinct values per dimension |

The shorter pair is kept separate rather than widened because the Vercel functions,
the Cloud dashboard and this MCP server all call it with six parameters, and a
ClickHouse parameterized view has no defaults, so widening the signature in place
would break every caller the moment the SQL is applied.

`dimension_value` exists for one reason. The case-fold fallback has to ask whether a
value exists exactly, and asking that of `minute_occupancy` costs a full scan of the
serving table once per filtered dimension: 96,818 rows with no filters, 871,362 with all
eight, one further scan of the fact table per predicate. That cancels out the read
advantage the whole design exists to demonstrate. Resolving against the small table
instead makes the cost flat in the number of filters, 97,043 rows at zero filters and
98,393 at eight.

The after figures are measured rather than restated here, one table for the whole
project: `evidence/read_cost_by_filter_count.txt` carries every row with the `query_id`
the client generated before the query ran, so each one can be looked up in
`system.query_log` and checked. [scale.md](scale.md#the-serving-layers-read-cost-tracks-the-rollup-not-the-raw-event-count)
reads that file and makes the flatness argument in full.

`v_dimension_values` deliberately carries no concurrency figure. A peak per value has
to sum across the other dimensions before the maximum is taken, and a `GROUP BY` there
would take the maximum first and publish a number that is quietly too small. Peaks per
value come from `top_slices` or from the concurrency views with the dimension
filtered. It also leaves out the empty value on purpose: `video_type` genuinely holds
one, carrying 455 minutes and a peak of 92, but the empty string is also the no-filter
sentinel, so passing it back as a filter returns the whole dataset rather than that
slice. Publishing it as selectable was a trap on our own data and would be a worse one
on a dataset nobody has read yet.

`v_overcount` puts the project's headline claim behind a query instead of behind
prose, and the `overcount` tool puts it one call away in chat: 3,743 naive against
2,692 foreground, 39.0% on the peak and 49.0% on the average, and the two peaks land in
different minutes. Asked in chat how much counting every open session would overcount,
the model reads that one row from the guardrailed surface, with no SQL and no escape
hatch involved.

## Every answer carries its own receipt

Every answer the server returns ends with its `query_id`, the rows the server read, the
server-side elapsed time, and the user it ran as, so a reader can go and check it. The
rows-read figure comes out of the response's own statistics block; verified byte
identical to `system.query_log.read_rows` for the same `query_id`.

## The tool picker no longer has to be touched

`docker/librechat.yaml` declares a `modelSpecs` entry, `clickliv-concurrency-desk`,
marked `default: true` with `prioritize: true`, that lists both MCP servers under
`mcpServers` and carries a system prompt. LibreChat unions a spec's `mcpServers` with
whatever the tool picker sends, and it does so server side in the ephemeral agent
builder, not in the browser. A request carrying `spec` and an explicitly empty
`ephemeralAgent.mcp` still gets both surfaces attached, which is the whole point: the
tools cannot be switched off by accident.

Proven in a real browser rather than argued. A fresh login, `/c/new`, nothing touched
in the picker, one question typed. The request the client sent carried
`spec: clickliv-concurrency-desk` and `mcp: ["clickliv-marts","clickhouse-official"]`,
the picker read "2 selected", and the answer was 2,692 at 2026-07-26 10:56 UTC from
`concurrency_peak`. The residual gap is a conversation created before the spec existed,
which stores no spec and therefore gets neither the pinned tools nor the prompt. Start a
new chat, which is what the demo does anyway.

The prompt on the spec is the second layer, for the case where the MCP server itself is
down and LibreChat attaches nothing. It forbids answering a concurrency question from
memory, forbids asking the user which filter values are valid, forbids reporting an
empty result or a zero as a finding, requires a tool call to be attempted before
anything is declined, and states that both connections are read only. An earlier draft
of it made things worse rather than better: phrased as "if no tools are attached, say
so", `gpt-5.2` reached for that line whenever it merely did not want to answer, and
told the user to switch the tools back on in four conversations where the tools were
demonstrably attached. The rule now fires only on genuinely having no tools, and the
positive instruction to try the tool first carries the weight.

LibreChat's `GET /api/mcp/connection/status` reports `disconnected` for both servers
until the first tool call of a session establishes the per-user connection, on both the
laptop and the EC2 box, so it is a false negative; `GET /api/mcp/tools` is the endpoint
that tells you whether the tools are really attached.

## LibreChat v0.8.7 talks to two MCP surfaces, and says which one it used

`make chat-up` brings it up from `docker compose --profile chat`, with Google AI Studio
`gemini-3-flash-preview`
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
values, says relative phrases resolve against the data rather than against today, and
hands off explicitly when the question filters on a dimension the tools do not take.
The escape hatch one says to read the window from `v_data_window` rather than assume
one, states the sentinel rule, and carries a copyable example of the parameterized call
syntax, because parameters go inside the parentheses as `name = value` and a model that
has not been shown that puts them in a `WHERE` clause and gets "unknown expression
identifier" instead of an answer.

Twenty two sloppy, judge-realistic questions were driven end to end through the live
LibreChat on EC2 after the changes above, covering no filters at all, every sentinel,
mixed-case values, a platform that does not exist, a real title, a missing title, an
ambiguous title, two time ranges outside the window, the session against people
ambiguity, the overcount thesis, a write request, a request to list its own tools, and
a question the dataset cannot answer. Every one returned either a correct number or a
refusal that named what does exist. None returned an empty result as an answer and none
asked the user which filter values were valid. The transcripts, including the failures
these replaced, are in `evidence/conversational_layer.txt`.

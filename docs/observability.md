# Observability and the OSS pillars

The requirement is to meaningfully integrate at least one of ClickStack, Langfuse or
LibreChat. All three are integrated, and ClickHouse is underneath every one of them.

| Pillar | Version | Brought up by |
|---|---|---|
| ClickHouse | 26.7.1.1315 in Docker, 26.4.1.2029 on Cloud | `make up`, or `.env` pointed at Cloud |
| ClickStack | all-in-one | `make obs-up` |
| Langfuse | 4.1.0 | `make llm-up` |
| LibreChat | v0.8.7 | `make chat-up` |

Host ports for each are listed under
[local development surfaces](operations.md#local-development-surfaces). LibreChat is
covered in [mcp.md](mcp.md), because what it talks to is the point.

## ClickStack observes the pipeline

It runs beside the pipeline, never inside it.

```sh
make obs-up      # all-in-one: OTLP on 4317 and 4318, HyperDX on 8080, its ClickHouse on 8124
make all
make obs         # read the trace back out of ClickStack
```

Set `CLICKSTACK_OTLP` and `CLICKSTACK_KEY` in `.env`, the key being the ingestion key from
Team Settings in the HyperDX UI. Leave `CLICKSTACK_OTLP` unset and tracing is a no-op:
no network call, byte-identical output. The exporter is OTLP over JSON on the standard
library, so the project still has **zero Python dependencies**.

Each run emits one trace: a root span for the command, a span per pipeline stage, a span
per ingest, and a span per ClickHouse query. The query spans deliberately do not report
client wall clock. Before export the tracer issues `SYSTEM FLUSH LOGS`, reads
`system.query_log` for the query ids it collected, and attaches what the server itself
recorded (D14):

```
stages
  SpanName             spans  ms
  clickliv.all         1      5440.1
  stage.reference      1      2372.9
  stage.load           1      1843.8
  ingest.raw_events    1      1742.1
  stage.verify         1      467
  stage.occupancy      1      435.8
  stage.sessionize     1      225.8
  stage.deltas         1      77.2

queries by rows read, server side
  server_ms  read_rows  read_bytes  statement
  45         3622235    135833299   SELECT (SELECT count() FROM raw_events) ...
  314        937722     85082999    INSERT INTO session_minutes WITH covered AS ...
  1736       905558     265203213   INSERT INTO raw_events SELECT video_session_id ...
  212        905558     69731258    INSERT INTO active_intervals WITH 90 * 1000 AS gap_ms ...
```

Ingest spans carry `ingest.rows`, `ingest.bytes`, `ingest.duration_ms`, and
`ingest.visible_lag_ms`, the delay between the insert being acknowledged and the rows
being queryable. It is 3.3ms for 905,558 rows here, which is the honest answer for
synchronous MergeTree inserts and the panel that would move first on a live feeder.

`make obs` reads that telemetry back out of the ClickHouse that ClickStack stores it in,
over the same client that runs the pipeline. ClickHouse is the analytical engine on both
sides of the integration.

## Langfuse 4.1.0, with a ClickHouse product on both sides of it

`make llm-up` brings it up self-hosted, from `docker compose --profile llm`. The
notable part is not that it runs, it is what it runs on. Langfuse keeps an analytical
store and a transactional store, and here both of them are ClickHouse products. The
trace store is this project's own ClickHouse Cloud service, database `langfuse`, where
92 Langfuse migrations have applied and the tables sit on `SharedMergeTree` and
`SharedReplacingMergeTree`, Cloud's own engines. The transactional store is ClickHouse
managed Postgres 17.10 in `ap-south-1`, provisioned with
`clickhousectl cloud postgres create`. Blob storage is a real S3 bucket in the same
region, reached through an IAM user scoped to that one bucket: those credentials can
write `events/` and are refused `s3:ListAllMyBuckets`. Only Redis runs as a local
container, because Langfuse requires Redis and ClickHouse is not a queue.

## One exporter, two sinks

Tracing is one exporter with two sinks, not two exporters. ClickStack observes the
pipeline, Langfuse observes the LLM and MCP calls, both over the same stdlib OTLP
writer. Each is off until its own variables are set, so with neither `CLICKSTACK_OTLP`
nor `LANGFUSE_HOST` configured the default pipeline makes no network call and its
output is unchanged.

## Decline alerting

Decline alerting is called out as explicitly optional, "an LLM & ClickStack
use-case," for concurrency dropping because an asset ended, a system issue, or
disengaging content. `make decline` builds detection deterministic: a
minute-over-minute drop threshold read from `marts.v_occupancy_minute`, not an LLM
call. The problem statement itself says AI is not required for the core, and this
sits adjacent to the core, not inside it, so a threshold rule is the right scope for
detection. One real event exists in the tuning data: 214 to 7 sessions, a 96.7% drop,
found by the rule rather than manufactured. `evidence/decline_alerts.txt`.

On top of that, one genuinely optional LLM call narrates *which* of the three named
causes the pattern suggests, off unless a provider key is set, same no-op-by-default
discipline as tracing. OpenAI `gpt-5.2` is preferred when `OPENAI_API_KEY` is set, and
Bedrock `openai.gpt-oss-120b` is kept as the fallback rather than deleted; both speak
the OpenAI Responses shape, so one reader parses either, and the evidence file names
whichever produced the text it carries. Not Claude: Bedrock's Claude inference profiles
are not reachable from this account in `ap-south-1` (checked directly, token quota is
stuck at 0 and not self-service adjustable). Both working providers were verified with a
real call on the 96.7% drop above, and each reasoned "asset ended" from the shape of the
drop alone, which is what a human would conclude from the same number. The call is
traced to Langfuse as a generation, carrying the model, the prompt, the completion and
the token usage. No LLM sits anywhere in the correctness path: every number in
`answers/`, `evidence/` and `submission/` is produced by SQL this repository ran.

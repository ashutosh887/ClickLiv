# ClickLiv

Real-time foreground-only concurrency for SonyLIV streaming telemetry, on ClickHouse.

A viewer counts as concurrent only while they are **playing**, **foregrounded**, and
**heartbeat-fresh**. Counting every open session instead overstates peak concurrency by
**39%** and average concurrency by **49%** on the provided dataset, and it puts the peak
in the wrong minute.

## Everything you can open

Every surface in one place. Start the stack with `make up && make obs-up && make llm-up
&& make chat-up`, then `make mcp` and `make ui` in their own shells.

| Open this | Where | Started by | What it shows |
|---|---|---|---|
| Concurrency dashboard | <http://localhost:8090> | `make ui` | The concurrency curve with a platform filter, read straight from `marts.v_concurrency` |
| LibreChat | <http://localhost:3080> | `make chat-up` | Ask for concurrency in plain language, answered through the guardrailed MCP tools |
| Langfuse | <http://localhost:3300> | `make llm-up` | LLM and MCP traces, with token usage and cost, stored in our own ClickHouse Cloud service |
| ClickStack (HyperDX) | <http://localhost:8080> | `make obs-up` | Pipeline traces, every stage and every query, with server-side `read_rows` attached |
| MCP health | <http://localhost:8765/health> | `make mcp` | The four pre-vetted tools and the restricted user they run as |
| ClickHouse MCP health | <http://localhost:8766/health> | `make chat-up` | The official read-only ClickHouse MCP surface |
| ClickHouse Cloud console | <https://console.clickhouse.cloud> | provisioned | Service `ClickLiv`, `ap-south-1`, plus the `clickliv-langfuse` managed Postgres service |

Answers and evidence live in the repo rather than behind a URL: `answers/`,
`evidence/` and `submission/`. `sql/09_dashboard.sql` holds the saved queries for a
Cloud console dashboard.

## The correctness argument

Five independent paths compute the same number, and gates diff them row for row.

```
ch-hackathon-content-data.csv ──▶ content_meta ──▶ content_dict
ch-hackathon-raw-data.csv ──▶ raw_events ──▶ active_intervals
                                             │
                                             ├──▶ session_minutes ──▶ minute_occupancy
                                             │    one row per (session, minute), deduped
                                             │    PRIMARY SERVING PATH
                                             │
                                             ├──▶ minute_deltas
                                             │    +1/-1 on merged runs, windowed cumsum
                                             │    SECOND SERVING PATH
                                             │
                                             └──▶ maxIntersections
                                                  arithmetic oracle, no rollup involved

src/clickliv/reference.py   reads the CSV directly and owes ClickHouse nothing
chDB                        runs 01 through 04 unmodified, in-process, same hashes
```

Two serving paths that agree is a claim no single path can make. `make verify` proves it:

```
PASS  intervals: SQL == python reference             0 only in SQL, 0 only in reference
PASS  rollup: occupancy == python reference          0 only in SQL, 0 only in reference
PASS  deltas == occupancy, no filter                 3649 minutes, peak 2692
PASS  deltas == occupancy, platform ANDROID_PHONE    3561 minutes, peak 1704
PASS  deltas == occupancy, platform SONY_ANDROID_TV  119 minutes, peak 279
PASS  deltas == occupancy, video_type live           65 minutes, peak 425
PASS  deltas == occupancy, audio_language hin        3398 minutes, peak 1614
PASS  deltas == occupancy, IPHONE in india           763 minutes, peak 329
PASS  deltas == occupancy, vod on Mweb               60 minutes, peak 62
PASS  half-open sweep == python instantaneous peak   sweep 2282, reference 2282
PASS  maxIntersections >= half-open sweep            maxIntersections 2282, sweep 2282, difference 0
PASS  instantaneous peak <= occupancy peak           2282 <= 2692, gap 410

Gate A: PASS  (12/12 checks)
```

## Running it

```sh
cp .env.example .env
make up          # ClickHouse 26.7 in Docker, or point .env at ClickHouse Cloud
make all         # schema, load, sessionize, both serving paths, reference, Gate A
make gate-b      # rebuild twice, assert byte-identical serving tables
make sweep       # threshold sensitivity grid
make chdb        # same SQL in-process on chDB, no server at all
make gate-c      # held-out single-day dry run, evidence in answers/gate_c and evidence/gate_c
make scale       # O7: sharding and read-cost proofs at 1x/10x/100x, evidence/scale.txt
make userlevel   # O4: session-level vs user-level concurrency, evidence/user_level.txt
make crossover   # the problem statement's own dimension-crossover example, measured
make decline     # optional: deterministic concurrency-decline alerting
make incremental # a real open session absorbs a new heartbeat live, proven vs batch
make instantaneous # O3: occupancy vs instantaneous overlap, for every dimension slice
make submission  # O2: the answer bundle, plus the measured serving SLO (O6b)
make replay      # the graded unseen-day run, reset to submission bundle, one command
make mcp         # the guardrailed MCP server, four pre-vetted tools over marts
make llm-up      # Langfuse 4.1.0 on 3300, both of its databases ClickHouse products
make chat-up     # LibreChat v0.8.7 on 3080, wired to both MCP surfaces
```

`make all` runs CSV to Gate A in about 8 seconds. Every command runs unchanged against
ClickHouse Cloud, the submission's actual requirement ("load the data into your team's
own ClickHouse Cloud service, there is no shared instance"): `.env` holds one active
target at a time, Cloud or local Docker, the other block commented out (see
`.env.example`). `.env` here is currently pointed at the real Cloud service.

Verified end to end against it (Mumbai, `ap-south-1`, 2 replicas): Gate A 12/12, Gate B
byte-identical hashes to local, marts, answers, projections, scale, userlevel,
crossover, decline, incremental, instantaneous, submission and the MCP surface all
pass, matching local numbers exactly. Four
real, Cloud-specific differences found and fixed while proving that, not assumed away:

- **A multi-replica read-after-write race.** A plain `SYSTEM RELOAD DICTIONARY` or
  `SYSTEM FLUSH LOGS` only reaches whichever replica handled that one HTTP request; a
  follow-up read on a different replica can see stale (or missing) state. Never
  visible on the single-node local target. Fixed at the source:
  `SYSTEM RELOAD DICTIONARY ON CLUSTER default` (deterministic, falls back to the
  plain form where there is no Keeper, i.e. locally) for the content dictionary, and
  a bounded flush-and-retry helper (`ClickHouse.query_log_rows`) for every
  `system.query_log` read.
- **`EXPLAIN ANALYZE` needs ClickHouse 26.7+**, confirmed as a hard version gate, not
  a config flag: syntax error, not a runtime error, on Cloud's 26.4. `answers.py`
  degrades gracefully, records why in the evidence file, and every other check is
  unaffected.
- **A replica's `system.query_log` holds only the queries that replica ran.** Cloud
  routes each HTTP request to either replica, so reading `system.query_log` after a
  round-robined batch of queries returned roughly half of them: 2 of 8 rows in one
  case, and the missing latencies were silently absent rather than reported as
  missing. Latency evidence was incomplete without ever failing. Every `query_log`
  read now spans every replica through
  `clusterAllReplicas(default, system.query_log)`, resolved once per connection by
  probing for a cluster so the single-node local target still reads the plain table.
  The same fix collapsed the tracer's own second, differently-worded `query_log` read
  into that one helper.
- **Cloud enforces a password complexity policy** (uppercase + digit + special
  character) that local Docker does not. `MARTS_PASSWORD` needed a real password, not
  the local placeholder.

`make chdb` needs no server at all. It builds the entire pipeline inside the Python process
with chDB and checks it against the served tables:

```
chDB 26.5.1.1 built the whole pipeline in-process in 2.0s, no server
server is ClickHouse 26.7.1.1315

PASS  minute_occupancy     96,818 rows  hash 8231330d0e0603ee
PASS  minute_deltas        33,748 rows  hash 2d7c0e268430f7ee
PASS  active_intervals     32,164 rows  hash 1f6726b4cec03404

Gate D: PASS  chDB agrees with the server
```

Same SQL files, two ClickHouse runtimes, two ClickHouse versions, identical hashes. The
portability is not a claim, it is a target you can run.

## Observability, and the four OSS pillars

The requirement is to meaningfully integrate at least one of ClickStack, Langfuse or
LibreChat. All three are integrated, and ClickHouse is underneath every one of them.

| Pillar | Version | Brought up by | Host port |
|---|---|---|---|
| ClickHouse | 26.7.1.1315 in Docker, 26.4.1.2029 on Cloud | `make up`, or `.env` pointed at Cloud | 8123, 8443 on Cloud |
| ClickStack | all-in-one | `make obs-up` | HyperDX 8080, OTLP 4317 and 4318 |
| Langfuse | 4.1.0 | `make llm-up` | 3300 |
| LibreChat | v0.8.7 | `make chat-up` | 3080 |

ClickStack observes the pipeline. It runs beside it, never inside it.

```sh
make obs-up      # all-in-one: OTLP on 4317 and 4318, HyperDX on 8080, its ClickHouse on 8124
make all
make obs         # read the trace back out of ClickStack
```

Set `CLICKSTACK_OTLP` and `CLICKSTACK_KEY` in `.env`, the key being the ingestion key from
Team Settings at `localhost:8080`. Leave `CLICKSTACK_OTLP` unset and tracing is a no-op:
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

**Langfuse 4.1.0, with a ClickHouse product on both sides of it.** `make llm-up` brings
it up at `localhost:3300`, self-hosted, from `docker compose --profile llm`. The
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

Tracing is one exporter with two sinks, not two exporters. ClickStack observes the
pipeline, Langfuse observes the LLM and MCP calls, both over the same stdlib OTLP
writer. Each is off until its own variables are set, so with neither `CLICKSTACK_OTLP`
nor `LANGFUSE_HOST` configured the default pipeline makes no network call and its
output is unchanged.

## The serving surface

The benchmark question shapes are private (O2). Rather than guess the exact wording,
`marts` answers any (dimension filter, time range, grain) combination through one
parameterized view, called as a table function:

```sh
make marts
```

```sql
SELECT * FROM marts.v_concurrency(
    grain_minutes = 60, country = '', platform = 'ANDROID_PHONE', video_type = '',
    content_id = 0, minute_from = 0, minute_to = 4294967295);
```

An empty string or a zero content_id means "no filter on this dimension", via
`coalesce(nullIf({param}, ''), column)`. Filtering happens before the aggregation and
the aggregation is always to `minute`, so D6 holds regardless of which dims are
supplied: sum across whatever is left unfiltered, then take `max()` or `avg()` over
minutes, never the reverse. Numbers from `marts.v_concurrency` match Gate A exactly:
2,692 for the whole day, 1,704 for `platform = ANDROID_PHONE`, 425 for `video_type =
live`.

`marts` is the only granted surface. `marts_agent` holds a role scoped to `SELECT ON
marts.*`, nothing on the tables underneath, enforced by `SQL SECURITY DEFINER` on the
views so the invoker's own grants are never checked against `minute_occupancy`.
Verified: dropping `DEFINER` makes the same query 403 for `marts_agent` even though
the view itself is granted, because ClickHouse checks the invoker's rights on the
underlying table by default.

`marts_agent`'s settings profile carries `readonly = 1 CONST`, so it cannot raise its
own ceiling; every attempt to touch `max_execution_time`, `max_rows_to_read`, or
`readonly` itself is rejected before the query runs, not after. A raw scan is not
merely slow, it does not start.

## The MCP surface, where a model can ask

`make mcp` runs `src/clickliv/mcp.py`, a Streamable HTTP MCP server on port 8765 at
`/mcp`, on the standard library like the rest of the project. It exposes four
pre-vetted, parameterized tools over the `marts` views and nothing else:
`concurrency_peak`, `concurrency_series`, `top_slices`, `list_dimensions`. Set
`MCP_PORT` to move it, since the dashboard defaults to the same port.

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

Every answer the server returns ends with its `query_id`, the rows the server read, the
server-side elapsed time, and the user it ran as, so a reader can go and check it. The
rows-read figure comes out of the response's own statistics block; verified byte
identical to `system.query_log.read_rows` for the same `query_id`, 96,818 rows both
ways on the unfiltered day-grain call.

Proven end to end through LibreChat itself, not just against the server directly: asked
"what is the peak foreground-only concurrency, and what was it for platform
ANDROID_PHONE", `gpt-5.2` called `concurrency_peak` on the `clickliv-marts` surface
twice and answered 2,692 and 1,704, both exact. `system.query_log` shows the matching
two queries from `marts_agent` against `marts.v_concurrency` in the same window. Full
transcript and the cross-check in `evidence/conversational_layer.txt`. In the UI, the
MCP tool picker must have `clickliv-marts` turned on per conversation; it is off by
default until chosen.

**LibreChat v0.8.7 talks to two MCP surfaces, and says which one it used.**
`make chat-up` brings it up at `localhost:3080` from `docker compose --profile chat`,
with OpenAI `gpt-5.2` as the model provider and MongoDB for its own state. Meilisearch
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

## Answers and evidence

No pipeline evidence, no credit. `make answers` runs a benchmark set of peak and
average concurrency at minute, hour and day grain, unfiltered and across the same
dimension slices Gate A checks, entirely through `marts.v_concurrency`, and writes:

```
answers/benchmark_answers.csv   query_label, params, peak and average concurrency,
                                 the stated average denominator, byte-identical
                                 across runs because it carries no query_id or
                                 timestamp
answers/latencies.csv           the same queries' query_duration_ms, read_rows,
                                 read_bytes, result_rows, memory_usage, read from
                                 system.query_log by query_id, never client wall
                                 clock (D14)
evidence/query_log.csv          the same query_log rows again, as the artifact a
                                 judge can check against a query_id
evidence/explain_*.txt          EXPLAIN indexes=1 and EXPLAIN ANALYZE for the
                                 unfiltered day-grain query, showing the granule
                                 count against minute_occupancy directly
evidence/oracle_match.csv       occupancy peak, maxIntersections, and the Python
                                 reference's independent numbers, side by side
```

Answers and latencies are two files because an answer must be stable across runs and
a latency should not be forced to pretend it is. Every number is computed by a query
this repository ran, tagged with a `query_id`, and traceable to `system.query_log`;
none of it is hand-typed.

`make submission` runs that same benchmark set five times over and turns it into the
two things nobody upstream specified.

**A serving SLO, because no SLA was published (O6b).** Rather than leave serving
latency unstated, the target is ours and stated: p99 under 100ms. The committed run
measures p99 **30ms**, p50 22ms, p95 27ms, min 16ms over 40 samples, 8 benchmark
queries times 5 repetitions, every one of them server-side `query_duration_ms` read
from `system.query_log` by `query_id` rather than client wall clock. Latency moves a
millisecond or two run to run, which is exactly why `evidence/serving_slo.txt` carries
the percentiles and `evidence/serving_slo.csv` carries the per-sample rows: they can be
recomputed rather than believed.

**A format-agnostic answer bundle, because no answer format was published (O2).**
`submission/` holds the answers as `benchmark_answers.csv` and as
`benchmark_answers.json`, written from one source of truth so the two cannot disagree,
plus `manifest.json` recording the ClickHouse version, the host and database, row counts
for `raw_events`, `active_intervals`, `minute_occupancy` and `minute_deltas`, the minute
range in both epoch minutes and UTC, the git commit, the gap and grace thresholds, the
Python reference's own peak, and a SHA-256 plus byte size for every other file in the
bundle. `README.txt` explains the columns, names the average's denominator, and states
where the latencies come from, so a grader can read the bundle cold.

## Projections, proven not asserted

`content_id` sits last among the dims in `minute_occupancy`'s `ORDER BY` (D7), so a
`content_id` filter only gets partial pruning off the base table. `make projections`
adds `proj_content_minute`, reordered by `(content_id, minute)`, and captures the
before, the after, and the forced comparison to `evidence/projections.txt`:

```
before, optimize_use_projections = 0            ReadFromMergeTree (minute_occupancy)
                                                 Granules: 16/16, generic exclusion search
after, default settings, planner's own choice    ReadFromMergeTree (proj_content_minute)
                                                 Granules: 6/16, binary search
forced, force_optimize_projection_name           same plan, same 6/16
```

The planner picks the projection on its own; forcing it by name lands on the identical
plan, which is the point, not a coincidence to explain away. `system.query_log.projections`
records `['clickliv.minute_occupancy.proj_content_minute']` for the query, so the claim
is checkable after the fact and not just at EXPLAIN time.

## The dashboard

`make ui` serves a minimal concurrency dashboard at `localhost:8765`: one line chart of
peak and average concurrency per hour, one platform filter, nothing more. No new
dependency: it is a standard-library `http.server` reusing the same zero-dependency
`ClickHouse` client as the rest of the project, reading `marts.v_concurrency` directly.
The platform list in the filter is queried live from `minute_occupancy`, not hand-typed.

## The unseen-day run

The graded drop is one fresh CSV (O8), so the whole run is one command. Drop the new
file into `data/`, point `RAW_CSV` at it in `.env`, then:

```sh
make replay      # reset, schema, load, sessionize, occupancy, deltas, reference,
                 # verify (Gate A), marts, projections, answers, instantaneous, submission
```

Then commit `answers/`, `evidence/` and `submission/`. `replay` prints each step as it
starts, stops at the first one that fails and names it, and reports its own wall clock
at the end. Nothing about it is specific to the tuning file: the CSV paths are
environment variables, and the thresholds are substituted into the SQL from the same
place.

That day-agnosticism is a property the loader had to be fixed to have, and it is worth
stating plainly because the bug would have been expensive. `reconcile` used to compare
any input against the tuning day's measured counts (905,558 events, 10,866 sessions,
9,618 users, 3,357 referenced content ids, 33,463 content rows) and fail the run on any
mismatch, so a fresh day that was merely different rather than wrong would have aborted
the graded run before it did any work. It now separates the two kinds of check. Day
invariants still fail the run: every `content_id` in the events has to resolve through
the content dictionary, and something has to have actually loaded. Row counts are
reported as drift instead, marked `differs (expected on a new day)`, followed by
`input differs from the tuning data; day-invariant checks still enforced`.

## Gate C, the held-out dry run

The tuning CSV spans 11.8 days, but the real submission is one fresh day (O8). `make
gate-c` rehearses that drop before it happens: it holds out the busiest calendar day in
the tuning data (20660, 849,888 of 905,558 events, also the most recent, which is what a
freshly landed day looks like), reloads the pipeline against that slice alone, and runs
schema through chDB against it, unmodified:

```
schema, load, sessionize, occupancy, deltas, reference, verify   Gate A on the slice
sessionize, occupancy, deltas again                              Gate C: idempotent rebuild
marts, answers, evidence                                         the full serving layer
07_projections.sql, evidence                                     the projection, rebuilt
chDB                                                              Gate D on the slice
```

Every gate passes on data the pipeline was not specifically run against before:

```
Gate A: PASS  (12/12 checks)
Gate C: PASS  rebuild is idempotent on the held-out day
Gate D: PASS  chDB agrees with the server
```

Output lands in `answers/gate_c/` and `evidence/gate_c/`, alongside the full-dataset
answers and evidence rather than overwriting them, so both are checkable at once.

**Gate C caught a real bug the very first time it ran.** `MATERIALIZE PROJECTION` is an
asynchronous mutation; querying immediately after issuing it can race the projection
still being built, and forcing it by name then fails with `INCORRECT_DATA` because it
genuinely is not yet there to use. Every previous run of `make projections` had enough
wall-clock gap between separate commands to never hit this. Fixed with
`SETTINGS mutations_sync = 2` on the `MATERIALIZE PROJECTION` statement in
`sql/07_projections.sql`, so the ALTER blocks until the projection is actually built.
This is what Gate C is for: whatever breaks on a full, uninterrupted, unfamiliar-data
run is what would have broken on the real unseen-day drop, not a rehearsal problem.

After the dry run, `make gate-c` reloads the full dataset and re-verifies Gate A, so the
live database and the committed `answers/`/`evidence/` are left describing the full
tuning data, not the held-out slice, whether or not the dry run passed.

## O7, scale beyond the real peak

Real peak concurrency here is 2,692, not the worked example's 300K, so judges will ask
how the design behaves at 100x. `make scale` answers with two measured proofs instead of
an assertion, written to `evidence/scale.txt`:

**Sharding is exact, not approximate.** Sessionization never lets a session cross a
shard boundary, so splitting `active_intervals` across 8 independent chDB instances by
`cityHash64(video_session_id) % 8`, computing each shard's per-minute session count
alone, and summing the 8 results reproduces the live server's `minute_occupancy` peak
and its full 3,649-minute series exactly. No session is ever double-counted or missed,
by construction, which is why this fans out on any number of workers with no
coordination between them.

**The serving layer's read cost tracks the rollup, not the raw event count.** At 1x,
10x and 100x the real data (built by exact duplication, shifted session ids and time),
`system.query_log` shows the rollup reading a constant 7.4x fewer rows than a naive scan
of the raw events at every scale:

```
 scale    raw_rows  serving_rows  naive_read_rows  rollup_read_rows    ratio
    1x     905,558       121,954          905,558           121,954      7.4
   10x   9,055,580     1,219,481        9,055,580         1,219,481      7.4
  100x  90,555,800    12,194,810       90,555,800        12,194,810      7.4
```

That flat ratio is a property of exact duplication (both tables scale by the same K),
stated rather than hidden. What it does show honestly: the collapse from event grain to
session-minute grain is structural and does not erode as the table grows; under organic
growth, where sessions gain more events rather than being cloned wholesale, the serving
table grows slower than raw events and this ratio would widen further.

## Update handling, proven live

"Update handling" is one of the five named evaluation criteria: "sessions in the
dataset include ones still open when the day ends and heartbeats that keep arriving.
Judges will look at how your serving layer absorbs them: incrementally, or by
recomputing?" The served tables (`minute_occupancy`, `minute_deltas`) are a full,
idempotent rebuild by default (D9, D13), gated for correctness but not incremental.

`make incremental` adds a real, narrowly-scoped incremental path for exactly the
scenario the problem statement names: an already-open session receiving a new
heartbeat. A `ReplacingMergeTree` table, `open_session_state`, tracks sessions known
to still be open; a materialized view, `mv_extend_open_session`, fires on every
insert into `raw_events` and extends the tracked state live, with no rebuild, when a
later, non-closing event lands within the gap threshold. The proof: pick a real
session that is genuinely open at data end, insert one synthetic heartbeat, read the
live state (no rebuild), then run the full batch sessionizer from scratch and
compare. Measured: the two agree to the millisecond. `evidence/incremental_update.txt`.

Both objects are dropped after the run, same leave-no-trace discipline as Gate C, so
this does not become a permanent tax on every other command's inserts.

## Dimension crossover and decline alerting

The problem statement gives its own worked example: "platform and a content might
peak at one minute, while platform + country might reach its peak at an entirely
different minute." `make crossover` reproduces it with real numbers through
`marts.v_concurrency`, the served surface, not a hand-picked illustration:
`evidence/dimension_crossover.txt` shows 4 distinct peak minutes across 5 real
slices. D6 (filter, sum across excluded dims, then max over minutes, never max
first) is why the served view gets this right automatically.

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

## Session-level or user-level concurrency

The data dictionary says user-level concurrency can be derived from `user_id`. The
serving layer stays session-level by default; `make userlevel` measures the
alternative instead of asserting it, writing `evidence/user_level.txt`. At the peak
minute, 2,692 concurrent sessions resolve to 2,626 concurrent users, exact and
HyperLogLog agreeing to 0.00% error at this cardinality. The 66-session gap is
explained entirely by 784 users running more than one concurrent session, a second
device on an account already counted, not by noise. That also confirms `uniq` /
`uniqState` / `uniqMerge` reproduces `uniqExact` exactly here, so it is a bounded,
mergeable choice if user-level concurrency is ever asked for. No second persisted
serving table was built for a metric nobody asked for; this diagnostic proves the
design choice is sound and stops there.

## The active rule

A session is active at time `t` when it is playing **and** foregrounded **and**
heartbeat-fresh. All three, because no single signal is sufficient on this data:

| Signal | Why it alone is not enough |
|---|---|
| Explicit background and foreground markers | Not guaranteed. 407 sessions carry an unmatched `AppBackgrounded`, 45 a foreground with no preceding background, 344 end backgrounded. |
| Heartbeat gaps | Telemetry keeps flowing during pause. 79.4% of pause windows over 60s contain other telemetry, 314,277 events. A gap rule sees those as alive. |
| Explicit pause and resume | Also not guaranteed, and a session can die silently without ever pausing. |

Segments close on pause, background, error, session end, session restart, and on any gap
over the threshold. They reopen on play, resume, and foreground while playing.

**Pause is excluded from active time.** The question is who is watching, not who has the
app open. That is a design choice worth 27,340 pause events, so it is stated rather than
buried, and it is one predicate to flip.

## Where the data dictionary is wrong

Everything below is measured, not inferred, and reproducible from this repo.

**The heartbeat is 40s, not the documented 60s.** Four telemetry streams sit at a p90 of
exactly 40.0s. Every liveness threshold derives from that number, so the tail grace is one
cadence and the gap threshold is 2.25 cadences.

**`event_type='VideoHeartbeat'` is a bucket of 41 distinct `event` values**, not a periodic
beat. It carries the playback-state markers: `pause`, `resume`, `speed-pause`, `AdPause`.
There is no `VideoPause` event type, so any rule keyed only on `event_type` cannot exclude
paused time, which is one of the three exclusions this track is scored on.

**Dimensions are unstable inside sessions.** `subtitle_language` changes within 99.97% of
sessions and `audio_language` within 81%. `any(dim) GROUP BY session` therefore fabricates
a label for most sessions. The tuple is resolved per `(session, minute)` with `argMax` on
event order, which also guarantees exactly one tuple per session per minute.

**The span is 11.8 days, not one**, so `PARTITION BY tuple()` is not appropriate.

**161,660 events share a timestamp with another event in the same session, and 6,058 of
those collisions carry conflicting state effects.** Order within a millisecond changes the
answer, so it is fixed by an explicit rule rather than left to insertion order:
deactivating events apply last. Both implementations sort by
`(timestamp, kind, dimension tuple)`, which is a total order, and that is why the two agree
exactly rather than approximately.

**One content row carries a negative `content_id`** and is referenced by no event. The
loader rejects it and says so, rather than widening the dictionary key to hide it.

## Per-minute concurrency is additive across dimensions; peak is not additive across time

With one row per `(session, minute)`, each session sits in exactly one dimension tuple, so
summing slice counts gives the total. That is what lets a single rollup serve any filter
combination.

`max` does not distribute over sums, so `max(A+B) != max(A) + max(B)`. Per-platform peaks
cannot be combined into a platform-plus-country peak. The order of operations is **filter,
sum across excluded dimensions, then take the max over minutes.** Never max first.

## Threshold sensitivity

The gap and grace thresholds are the two guessed numbers in the model, so they are swept
rather than asserted.

| | grace 20s | grace 40s | grace 60s |
|---|---|---|---|
| **gap 60s** | 2,688 | 2,691 | 2,697 |
| **gap 90s** | 2,689 | **2,692** | 2,697 |
| **gap 120s** | 2,690 | 2,692 | 2,697 |

Peak concurrency moves 0.3% across the entire grid, and the peak minute never moves. The
answer does not depend on the guess. That is a stronger result than defending a particular
value, and it is the honest one.

## Occupancy or instantaneous overlap

Two defensible readings of "concurrency at minute m", and they give different answers
on every slice, not only in aggregate. Occupancy is any active playback during minute
m; instantaneous is the overlap at a single point in time. `make instantaneous`
computes both for all seven slices Gate A checks and writes
`evidence/instantaneous_vs_occupancy.txt`:

| Slice | Occupancy | Instantaneous | Gap |
|---|---|---|---|
| no filter | 2,692 | 2,282 | 15.2% |
| platform ANDROID_PHONE | 1,704 | 1,442 | 15.4% |
| platform SONY_ANDROID_TV | 279 | 246 | 11.8% |
| video_type live | 425 | 354 | 16.7% |
| audio_language hin | 1,614 | 1,405 | 12.9% |
| IPHONE in india | 329 | 263 | 20.1% |
| vod on Mweb | 62 | 51 | 17.7% |

The gap runs from 11.8% to 20.1%, so the two readings are not interchangeable at any
slice and the choice has to be stated rather than assumed. Two independent SQL paths
produce the instantaneous column and agree exactly on all seven: `maxIntersections`
over closed millisecond intervals, and a signed event sweep on the half-open form.

The method has to add dimensions without moving the number. Each active interval is
clipped to every minute it covers, that minute's dimension tuple is joined in from
`session_minutes`, the filter is applied, each session's surviving pieces are merged
back into continuous presence, and only then is the overlap peaked. Unfiltered, the
clipping produces 137,738 pieces and the merge returns exactly 32,164 intervals, the
same segment count as `active_intervals` and as the independent Python reference, so the
merge validates itself rather than being taken on trust. The unfiltered instantaneous
peak is 2,282 either way, which Gate A pins to that reference.

Occupancy leads, because the problem statement's own worked example reads that way, and
the instantaneous figure is reported alongside it per slice. Instantaneous can never
exceed occupancy, and Gate A asserts that.

## Design notes

**Dictionary, not join, for content enrichment.** 33,463 content rows, 3,357 of them
referenced. A materialized view fires only on inserts to the left-most table of a join and
freezes the right side at insert time, so content loaded after events would never be picked
up. A dictionary makes the dependency explicit. Either way content must load first, and the
loader asserts it rather than assuming it.

**Partitioning by day is data management, not performance.** Unnecessary partitioning is
measured at 46x slower elsewhere, so the justification has to be the right one: the
partition is the atomic promotion unit, it bounds part counts, and it gives TTL a target.

**Serving reads always aggregate.** `SummingMergeTree` merges are asynchronous, so every
read groups explicitly instead of trusting that a merge has happened. `FINAL` never appears
in the hot path.

## Layout

```
sql/01_schema.sql            raw_events, content_meta, content_dict
sql/02_sessionize.sql        the state machine, as window functions
sql/03_occupancy.sql         session_minutes and the minute_occupancy rollup
sql/04_deltas.sql            merged minute runs to signed deltas
sql/05_oracles.sql           tables the Python reference is loaded into
sql/06_marts.sql             parameterized views, RBAC, the query budget
src/clickliv/answers.py      benchmark answers, latencies and evidence, no hand-typing
sql/07_projections.sql       proj_content_minute, reordered by (content_id, minute)
src/clickliv/projections.py  before/after/forced EXPLAIN, query_log confirmation
src/clickliv/gate_c.py       Gate C, the held-out single-day dry run
src/clickliv/scale.py        O7, the sharding and read-cost proofs at scale
src/clickliv/ui.py           the minimal concurrency dashboard
src/clickliv/userlevel.py    O4, session-level vs user-level concurrency, measured
src/clickliv/crossover.py    the problem statement's dimension-crossover example
src/clickliv/decline.py      optional: deterministic concurrency-decline alerting
src/clickliv/llm.py          one optional LLM call, OpenAI first, Bedrock fallback
sql/08_incremental.sql       open_session_state, mv_extend_open_session
src/clickliv/incremental.py  proves the incremental path agrees with a batch rebuild
src/clickliv/instantaneous.py O3, instantaneous overlap beside occupancy, per slice
src/clickliv/submission.py   O2 answer bundle and the O6b serving SLO, one run
src/clickliv/mcp.py          the MCP server, four pre-vetted tools as marts_agent
src/clickliv/cli.py          command dispatch, identical for local and Cloud
src/clickliv/ch.py           zero-dependency ClickHouse HTTP client
src/clickliv/load.py         CSV ingestion, content before events
src/clickliv/reference.py    ground truth, reads the CSV directly
src/clickliv/verify.py       Gate A
src/clickliv/gates.py        Gate B
src/clickliv/chdb_engine.py  Gate D, the whole pipeline in-process
src/clickliv/sweep.py        threshold sensitivity grid
src/clickliv/otel.py         OTLP exporter, two sinks, server-side metrics on spans
src/clickliv/observe.py      reads the trace back out of ClickStack
docker/librechat.yaml        LibreChat wired to both MCP surfaces, labelled
docker/                      access management, ClickStack user, LibreChat config
tests/                       stdlib unittest, zero dependencies, make test
```

Thresholds and credentials are `${VAR}` placeholders in the SQL, substituted from the
environment, which is what lets one set of files serve local, Cloud, and the sweep.

## Tests

`make test`, 24 tests, `python -m unittest discover -s tests`, zero new dependencies.
Covers the pure, deterministic logic that the four correctness gates never happen to
exercise on their own: SQL statement splitting, credential redaction, JSONCompact
parsing, config construction, the Gate B fingerprint comparison, Gate C's day-splitting
on a CSV, LLM provider selection (no key is a no-op, OpenAI wins when both are set,
Bedrock remains the fallback), and a pin on the exact counts `reconcile()` diffs
against. The gates are the correctness tests for the pipeline itself; this suite is for
the parts around it.

## Licence

MIT.

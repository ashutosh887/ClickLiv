# ClickLiv

Real-time foreground-only concurrency for SonyLIV streaming telemetry, on ClickHouse.

A viewer counts as concurrent only while they are **playing**, **foregrounded**, and
**heartbeat-fresh**. Counting every open session instead overstates peak concurrency by
**39%** and average concurrency by **49%** on the provided dataset, and it puts the peak
in the wrong minute. Peak is 2,692 foreground-only against 3,743 naive.

## Results

| Measure | Value |
|---|---|
| Peak concurrency, foreground-only, unfiltered | **2,692** |
| Peak concurrency, naive (any open session) | 3,743 |
| Naive overcount | 39% on peak, 49% on average, and the peak lands in a different minute |
| Instantaneous peak (point-in-time overlap, not occupancy) | 2,282 |
| Peak, platform ANDROID_PHONE | 1,704 |
| Peak, platform SONY_ANDROID_TV | 279 |
| Peak, video_type live | 425 |
| Peak, audio_language hin | 1,614 |
| Peak, IPHONE in india | 329 |
| Peak, vod on Mweb | 62 |
| Heartbeat cadence, measured (the data dictionary says 60s) | 40s |
| Threshold sensitivity, grace 20s to 60s by gap 60s to 120s | peak moves 0.3%, peak minute never moves |
| Serving latency, server-side, 40 samples | p99 42ms against a stated 100ms target, p50 29ms, p95 32ms |
| Gates | A 12/12 PASS, B byte-identical rebuild, C PASS on a held-out day, D chDB agrees with the server |

Every number above is produced by a query this repository ran, tagged with a `query_id`
and traceable to `system.query_log`. See [docs/evidence.md](docs/evidence.md).

## The strategy

Everything below is a decision we made on purpose, with the measurement that forced it.

### Define active from three signals, because no single one is sufficient

A session is active when it is playing **and** foregrounded **and** heartbeat-fresh. We
measured why each signal alone fails on this data:

| Signal | Why it alone is not enough |
|---|---|
| Background and foreground markers | Not guaranteed. 407 sessions carry an unmatched `AppBackgrounded`, 45 a foreground with no preceding background, 344 end backgrounded. |
| Heartbeat gaps | Telemetry keeps flowing during pause. 79.4% of pause windows over 60s contain other telemetry, 314,277 events. A gap rule sees those as alive. |
| Pause and resume markers | Also not guaranteed, and a session can die silently without ever pausing. |

Segments close on pause, background, error, session end, session restart, and on any gap
over the threshold. They reopen on play, resume, and foreground while playing.

**Pause is excluded from active time.** The question is who is watching, not who has the
app open. That choice is worth 27,340 pause events, so it is stated here rather than
buried, and it is one predicate to flip if SonyLIV defines it the other way.

### Trust the data, not the data dictionary

Three findings that change the design, all measured and reproducible from this repo:

**The heartbeat is 40s, not the documented 60s.** Four telemetry streams sit at a p90 of
exactly 40.0s. Every liveness threshold derives from that measured number, so the tail
grace is one cadence and the gap threshold is 2.25 cadences. A team that trusted the
document has thresholds wrong by a full cadence.

**There is no pause event type.** `event_type='VideoHeartbeat'` is a bucket of 41 distinct
`event` values, and the playback-state markers live inside it: `pause`, `resume`,
`speed-pause`, `AdPause`. Anything keyed only on `event_type` cannot exclude paused time,
which is one of the three exclusions this track exists to test.

**Dimensions are unstable inside sessions.** `subtitle_language` changes within 99.97% of
sessions and `audio_language` within 81%, so `any(dim) GROUP BY session` fabricates a
label for most sessions. The tuple is resolved per `(session, minute)` with `argMax` on
event order, which also guarantees exactly one tuple per session per minute.

### Store one row per session-minute, not per interval

8,496 session-minutes contain more than one active segment, median 2 and p95 of 7. Signed
per-interval deltas bucketed to minutes therefore either double count those or lose them.
Deduping to one active flag per `(session, minute)` fixes it, and it buys the property the
whole serving layer rests on.

**Per-minute concurrency is additive across dimensions.** Each session sits in exactly one
dimension tuple per minute, so summing slice counts gives the total, which is what lets a
single rollup answer any filter combination.

**Peak is not composable across time,** because `max` does not distribute over sums. The
order of operations is therefore fixed: **filter, sum across the excluded dimensions, then
take the max over minutes.** Never max first. `make crossover` reproduces the problem
statement's own worked example through the served view, showing 4 distinct peak minutes
across 5 real slices.

### Settle ordering explicitly, because ties change the answer

161,660 events share a timestamp with another event in the same session, and 6,058 of
those collisions carry conflicting state effects. Order within a millisecond changes the
result, so it is fixed by rule rather than left to insertion order: deactivating events
apply last. Both implementations sort by `(timestamp, kind, dimension tuple)`, a total
order, and that is why they agree exactly rather than approximately.

### Smaller decisions, each for a stated reason

**A dictionary, not a join, for content enrichment.** A materialized view fires only on
inserts to the left-most table of a join and freezes the right side at insert time, so
content loaded after events would never be picked up. A dictionary makes the dependency
explicit, and the loader asserts content loads first rather than assuming it.

**Partitioning by day is data management, not performance.** Unnecessary partitioning is
measured at 46x slower elsewhere, so the justification has to be the right one: the
partition is the atomic promotion unit, it bounds part counts, and it gives TTL a target.

**Serving reads always aggregate.** `SummingMergeTree` merges are asynchronous, so every
read groups explicitly instead of trusting that a merge has happened. `FINAL` never
appears in the hot path.

### Prove correctness five ways, then diff them

Two paths that agree is a claim no single path can make.

- `minute_occupancy`, one row per (session, minute), the primary serving path.
- `minute_deltas`, signed +1/-1 runs and a windowed cumulative sum, the second serving path.
- `maxIntersections`, an arithmetic oracle with no rollup involved.
- `src/clickliv/reference.py`, a Python reference that reads the CSV and owes ClickHouse nothing.
- chDB, the same SQL files in-process, no server, identical hashes.

Four gates diff them. **Gate A** is 12 cross-path checks. **Gate B** rebuilds twice and
asserts the serving tables are byte identical. **Gate C** reloads against the busiest
single day alone, rehearsing the unseen-day drop before it happens, and it caught a real
bug the first time it ran. **Gate D** builds the entire pipeline in-process with chDB and
matches the server hash for hash. Same SQL files, three ClickHouse runtimes, three
ClickHouse versions, identical hashes.

**The two guessed numbers are swept, not defended.** Across the whole gap and grace grid,
peak concurrency moves 0.3% and the peak minute never moves. The answer does not depend on
the guess, which is a stronger result than arguing for a particular value.

**Both readings of "concurrency" are computed.** Occupancy is any active playback during
minute m; instantaneous is overlap at a point in time. They differ by 11.8% to 20.1% at
every slice, so they are not interchangeable anywhere. Occupancy leads because the problem
statement's own example reads that way, and the instantaneous figure is reported beside it
per slice.

### Serve from one parameterized view, behind a budgeted role

The benchmark question shapes are private, so rather than guess the wording, `marts`
answers any (dimension filter, time range, grain) combination through one parameterized
view called as a table function. Filtering happens before aggregation and aggregation is
always to minute, so the order of operations holds no matter which dimensions are supplied.

`marts` is the only granted surface. Its consumers hold a role scoped to `SELECT ON
marts.*` and nothing on the tables underneath, enforced by `SQL SECURITY DEFINER` so the
invoker's own grants are never checked against the raw tables. The settings profile
carries `readonly = 1 CONST`, so a consumer cannot raise its own ceiling: a raw scan is not
merely slow, it does not start. Verified live against Cloud rather than argued, including
refusals on the underlying tables and on `SET max_execution_time`.

**Projections are proven, not asserted.** `content_id` sits last among the dimensions in
the base table's ordering, so `proj_content_minute` reorders by `(content_id, minute)`.
The planner picks it on its own, forcing it by name lands on the identical plan, and
`system.query_log.projections` records it, so the claim is checkable after the fact rather
than only at EXPLAIN time.

### Absorb open sessions incrementally, and prove it against a rebuild

Update handling is a named evaluation criterion. The served tables are a full idempotent
rebuild by default. On top of that, a `ReplacingMergeTree` tracks sessions known to still
be open and a materialized view fires on every insert into `raw_events`, extending the
tracked state live with no rebuild when a later non-closing event lands inside the gap
threshold.

The proof is the part that matters: take a session genuinely open at data end, insert one
heartbeat, read the live state without rebuilding, then run the full batch sessionizer
from scratch and compare. They agree to the millisecond.

### Answer the 100x question with measurements

**Sharding is exact by construction.** Sessionization never lets a session cross a shard
boundary, so splitting across 8 independent chDB instances by hash of session id,
computing each shard alone, and summing reproduces the server's peak and its full
3,649-minute series exactly. No session is double counted or missed, so this fans out on
any number of workers with no coordination.

**Read cost tracks the rollup, not the raw event count.** At 1x, 10x and 100x, the rollup
reads 7.4x fewer rows than a naive scan. That flat ratio is a property of exact
duplication and we say so rather than hiding it. What it shows honestly is that the
collapse from event grain to session-minute grain is structural and does not erode as the
table grows.

### Build for the unseen day, not the data we tuned on

The sealed evaluation dataset arrives in the final hours, and hand-computed answers score
nothing. `make unseen` takes a fresh raw CSV and content CSV and produces the answers, the
latencies and the pipeline evidence in one command, into a separate output tree so the
tuning-data results are never overwritten.

It is hardened against the ways a fresh day differs, each verified against an adversarial
fixture rather than reasoned about:

- **The loader reads the real header** and refuses to start if a required column is
  missing, printing the header it found. Binding by name silently loaded empty strings for
  a renamed column, which would have made every sliced answer quietly wrong.
- **Sessionize aborts if the event vocabulary stops matching.** It prints the event types
  actually present next to the ones expected, so a renamed event is diagnosable from the
  error alone. All answers coming back zero with nothing complaining is the worst failure
  mode available, so it is a hard stop.
- Gzip, alternate delimiters, extra columns and a column rename mapping are all handled.
- Sessions still open at end of file, unbalanced background markers, duplicate session
  starts, late arrivals out of timestamp order, and dimension values never seen before are
  all exercised by `fixtures/unseen_events.csv`.

The runbook is [docs/unseen-day.md](docs/unseen-day.md).

## Pipeline

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

## Quickstart

```sh
git clone https://github.com/ashutosh887/ClickLiv.git
cd ClickLiv
cp .env.example .env
make up          # ClickHouse 26.7 in Docker, or point .env at ClickHouse Cloud
make all         # schema, load, sessionize, both serving paths, reference, Gate A
```

`make all` runs CSV to Gate A in about 8 seconds and ends here:

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
```

Every other target, the ClickHouse Cloud path, and the surfaces you can start on your own
machine are in [docs/operations.md](docs/operations.md). The data and the observability
stores themselves run on a ClickHouse Cloud service and a ClickHouse managed Postgres
service in `ap-south-1`, both private to the team's org, and the answers are committed as
files rather than served from a URL.

## The four OSS pillars, with ClickHouse underneath every one

The rules require one of ClickStack, Langfuse or LibreChat, integrated meaningfully. All
three are, and none of them is a name-drop.

**ClickHouse** stores and serves everything, locally in Docker or on Cloud, unchanged.

**ClickStack** traces the pipeline itself: a root span per command, a span per stage, a
span per ingest, and a span per query. The query spans deliberately do not report client
wall clock. Before export the tracer flushes logs, reads `system.query_log` for the query
ids it collected, and attaches what the server itself recorded. The exporter is OTLP over
JSON on the standard library, so this pillar added zero dependencies, and leaving the
endpoint unset makes tracing a byte-identical no-op.

**Langfuse** traces the LLM and MCP calls, and its own storage is entirely ClickHouse
products: traces in this project's ClickHouse Cloud service, transactional state in
ClickHouse managed Postgres.

**LibreChat** asks the question in plain language through a guardrailed MCP server. On the
default path the model never emits SQL. It calls pre-vetted parameterized tools, filter
values are checked against an allowlist of real dimension values, and whatever survives
reaches ClickHouse as a bound parameter. The server connects as a restricted role, so the
query budget is enforced by ClickHouse rather than by prompt instructions. A labelled
escape hatch to the official read-only ClickHouse MCP server exists for schema exploration,
and its instructions require the model to show the SQL it ran so a reader can tell an ad
hoc query from a published mart.

## Live demo

- **[clickliv.vercel.app](https://clickliv.vercel.app)** is the concurrency chart,
  deployed on Vercel, reading `marts.v_concurrency` through a serverless proxy as the
  same restricted budgeted role every other consumer uses. No admin credential exists in
  that environment.
- **[librechat.15-252-63-157.sslip.io](https://librechat.15-252-63-157.sslip.io)** is the
  conversational surface. Demo login in `credentials.env`, which is gitignored and local
  only.
- **[langfuse.15-252-63-157.sslip.io](https://langfuse.15-252-63-157.sslip.io)** is the
  LLM observability pillar.
- **[clickstack.15-252-63-157.sslip.io](https://clickstack.15-252-63-157.sslip.io)** is
  the ClickStack pillar, tracing the pipeline itself. The starred **ClickLiv pipeline
  telemetry** dashboard is the panel worth opening.

All three self-hosted surfaces run on one EC2 instance in `ap-south-1`, next to the
ClickHouse Cloud service, behind Caddy for automatic HTTPS. Stable as long as the instance
is up, not tied to anyone's laptop. See [docs/operations.md](docs/operations.md).

## Documentation

| Page | What it covers |
|---|---|
| [docs/architecture.md](docs/architecture.md) | The model, the active rule, where the data dictionary is wrong, additivity across dimensions and why peak is not composable across time, the repository layout |
| [docs/correctness.md](docs/correctness.md) | Gates A through D, the oracles, threshold sensitivity, occupancy versus instantaneous per slice, the test suite |
| [docs/serving.md](docs/serving.md) | The `marts` surface, RBAC and the query budget, projections, update handling |
| [docs/observability.md](docs/observability.md) | ClickStack, Langfuse, the two-sink exporter, decline alerting |
| [docs/mcp.md](docs/mcp.md) | The MCP tools, the guardrails, LibreChat and its two surfaces, the proven round trip |
| [docs/operations.md](docs/operations.md) | Running it, every make target, local development surfaces, ClickHouse Cloud findings, Gate C |
| [docs/unseen-day.md](docs/unseen-day.md) | The sealed-dataset runbook, command by command, and what to do when the CSV differs |
| [docs/scale.md](docs/scale.md) | Sharding and read-cost proofs at 1x, 10x and 100x, user-level concurrency |
| [docs/evidence.md](docs/evidence.md) | What lands in `answers/`, `evidence/` and `submission/`, the serving SLO, checking any number against a `query_id` |

## Licence

MIT. See [LICENSE](LICENSE).

# Architecture

How a concurrent viewer is defined, why the data dictionary cannot be taken at face
value, and what that forces the storage model to look like.

## The pipeline

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

Two serving paths that agree is a claim no single path can make. The gates that diff
them are described in [correctness.md](correctness.md).

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

### Dimension crossover, measured

The problem statement gives its own worked example: "platform and a content might
peak at one minute, while platform + country might reach its peak at an entirely
different minute." `make crossover` reproduces it with real numbers through
`marts.v_concurrency`, the served surface, not a hand-picked illustration:
`evidence/dimension_crossover.txt` shows 4 distinct peak minutes across 5 real
slices. D6 (filter, sum across excluded dims, then max over minutes, never max
first) is why the served view gets this right automatically.

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

## Repository layout

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

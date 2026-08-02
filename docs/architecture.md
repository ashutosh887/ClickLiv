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
| Explicit background and foreground markers | Not guaranteed. 9,008 sessions carry an unmatched `AppBackgrounded`, 1,148 a foreground with no preceding background, 9,306 end backgrounded. |
| Heartbeat gaps | Telemetry keeps flowing during pause. Of the 14,626 pause windows that run past 60s, 79.4% contain other telemetry, 91,043 events. A gap rule sees those as alive. |
| Explicit pause and resume | Also not guaranteed, and a session can die silently without ever pausing. |

Segments close on pause, background, error, session end, session restart, and on any gap
over the threshold. They reopen on play, resume, and foreground while playing.

**Pause is excluded from active time.** The question is who is watching, not who has the
app open. That is a design choice worth 98,703 pause events, so it is stated rather than
buried, and it is one predicate to flip.

## Where the data dictionary is wrong

Everything below is measured against the graded dataset now loaded in `clickliv`, not
inferred, and reproducible from this repo.

**The heartbeat is 40s, not the documented 60s.** Four telemetry streams sit at a p90 of
exactly 40.0s, and the graded day measures the same 40.0s, which is what a sealed drop
with unchanged event semantics owes. Every liveness threshold derives from that number, so
the tail grace is one cadence and the gap threshold is 2.25 cadences.

**`event_type='VideoHeartbeat'` is a bucket of 42 distinct `event` values**, not a periodic
beat. It carries the playback-state markers: `pause`, `resume`, `speed-pause`, `AdPause`.
There is no `VideoPause` event type, so any rule keyed only on `event_type` cannot exclude
paused time, which is one of the three exclusions this track is scored on.

**Dimensions are unstable inside sessions.** `subtitle_language` changes within 79.0% of
sessions and `audio_language` within 47.2%, against 99.97% and 81% on the tuning extract.
Either way `any(dim) GROUP BY session` fabricates a label for most sessions. The tuple is
resolved per `(session, minute)` with `argMax` on event order, which also guarantees
exactly one tuple per session per minute.

**The window has to be reported twice, and `PARTITION BY tuple()` is exactly the wrong
answer to it.** The full extent of the graded data runs from 2014-12-31 18:31 to
2026-08-03 11:26, 4232.7 days, because a handful of sessions carry stray timestamps. The
dense window, the span of the calendar days carrying at least one percent of the session
minutes, is a single day: 2026-07-31, 0.999 days, with 3,360 outlier minutes and 9,200
outlier rows sitting outside it. Nothing is filtered; `marts.v_data_window` publishes both
readings so the strays are a number rather than a deletion. One partition holding one dense
day and twelve years of strays is the case unpartitioned storage handles worst, so
partitioning by day is more justified on this data than it was on the extract, not less.

**1,502,588 (session, timestamp) groups hold more than one event, 1,864,015 rows beyond the
first of each, and 739 of those groups carry conflicting state effects.** Order within a
millisecond changes the answer, so it is fixed by an explicit rule rather than left to
insertion order:
deactivating events apply last. Both implementations sort by
`(timestamp, kind, dimension tuple)`, which is a total order, and that is why the two agree
exactly rather than approximately.

**A negative `content_id` is rejected at load rather than hidden.** The tuning extract
carried exactly one such row, referenced by no event. The rule lives in the loader, not in
a note about one dataset, so it holds on whatever lands next instead of widening the
dictionary key to swallow it.

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
`marts.v_concurrency`, the served surface, not a hand-picked illustration. The same five
slices give 4 distinct peak minutes on the tuning extract and 2 on the graded day, where
`platform=SONY_ANDROID_TV` peaks 45 minutes before every other slice. One slice is enough
to break the assumption, and a single dense day gives the effect less room than twelve
days did. D6 (filter, sum across excluded dims, then max over minutes, never max first) is
why the served view gets this right automatically.

## Design notes

**Dictionary, not join, for content enrichment.** 33,325 content rows, 15,094 of them
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
sql/01_schema.sql             raw_events, content_meta, content_dict
sql/02_sessionize.sql         the state machine, as window functions
sql/03_occupancy.sql          session_minutes and the minute_occupancy rollup
sql/04_deltas.sql             merged minute runs to signed deltas
sql/05_oracles.sql            tables the Python reference is loaded into
sql/06_marts.sql              parameterized views, RBAC, the query budget
sql/07_projections.sql        proj_content_minute, reordered by (content_id, minute)
sql/08_incremental.sql        open_session_state, mv_extend_open_session
sql/09_dashboard.sql          the seven Cloud console saved queries, one per -- name:

src/clickliv/cli.py           command dispatch, identical for local and Cloud
src/clickliv/ch.py            zero-dependency ClickHouse HTTP client
src/clickliv/load.py          CSV ingestion, content before events, and preflight
src/clickliv/reference.py     ground truth, reads the CSV directly
src/clickliv/verify.py        Gate A
src/clickliv/gates.py         Gate B
src/clickliv/gate_c.py        Gate C, the held-out single-day dry run
src/clickliv/chdb_engine.py   Gate D, the whole pipeline in-process
src/clickliv/sweep.py         threshold sensitivity grid
src/clickliv/answers.py       benchmark answers, latencies and evidence, no hand-typing
src/clickliv/submission.py    O2 answer bundle and the O6b serving SLO, one run
src/clickliv/projections.py   before/after/forced EXPLAIN, query_log confirmation
src/clickliv/scale.py         O7, the sharding and read-cost proofs at scale
src/clickliv/userlevel.py     O4, session-level vs user-level concurrency, measured
src/clickliv/instantaneous.py O3, instantaneous overlap beside occupancy, per slice
src/clickliv/incremental.py   proves the incremental path agrees with a batch rebuild
src/clickliv/crossover.py     the problem statement's dimension-crossover example
src/clickliv/decline.py       optional: deterministic concurrency-decline alerting
src/clickliv/llm.py           one optional LLM call, Google first, OpenAI then Bedrock fallbacks
src/clickliv/claims.py        re-reads every published figure live, names stale docs
src/clickliv/mcp.py           the MCP server, five pre-vetted tools as marts_agent
src/clickliv/ui.py            the minimal local concurrency dashboard
src/clickliv/otel.py          OTLP exporter, two sinks, server-side metrics on spans
src/clickliv/observe.py       reads the trace back out of ClickStack

scripts/copy_dataset.sh       clones a built dataset into a *_sample schema, fails closed
scripts/verify_dashboard.sh   runs every sql/09_dashboard.sql query, read only
scripts/public_demo.sh        opens and closes the public demo grants
scripts/revoke_public.md      how to shut the public surface off again

web/index.html                the hosted landing page
web/dashboard.html            the hosted concurrency chart
web/api/                      Vercel functions, marts_agent only, no admin credential

docker/librechat.yaml         LibreChat wired to both MCP surfaces, labelled
docker/                       access management, ClickStack user, provisioning
tools/                        data fetch, the small fixture, the unseen-day fixtures
tests/                        stdlib unittest, zero dependencies, make test
fixtures/                     the small pipeline fixture and the adversarial fresh day
answers/ evidence/ submission/ artifacts/   what a run produces, described in evidence.md
docs/                         these pages
```

Thresholds and credentials are `${VAR}` placeholders in the SQL, substituted from the
environment, which is what lets one set of files serve local, Cloud, and the sweep.

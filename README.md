# ClickLiv

Real-time foreground-only concurrency for SonyLIV streaming telemetry, on ClickHouse.

A viewer counts as concurrent only while they are **playing**, **foregrounded**, and
**heartbeat-fresh**. Counting every open session instead overstates peak concurrency by
**39%** and average concurrency by **49%** on the provided dataset, and it puts the peak
in the wrong minute.

## The correctness argument

Four independent paths compute the same number, and a gate diffs them row for row.

```
ch-hackathon-raw-data.csv ──▶ raw_events ──▶ active_intervals ──┬──▶ session_minutes ──▶ minute_occupancy
ch-hackathon-content-data.csv ──▶ content_meta ──▶ content_dict │                         primary serving path
                                                                │
                                                                ├──▶ minute_deltas
                                                                │    +1/-1 on merged runs, windowed cumsum
                                                                │
                                                                └──▶ maxIntersections
                                                                     arithmetic oracle

src/clickliv/reference.py reads the CSV directly and owes ClickHouse nothing
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
```

`make all` runs CSV to Gate A in about 8 seconds. The same commands run unchanged against
ClickHouse Cloud: only `.env` changes.

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

Two defensible readings of "concurrency at minute m", and they give different answers:

- **Occupancy**, any active playback during minute m: **2,692**.
- **Instantaneous**, overlap at a point in time: **2,282**.

Both are computed. Occupancy leads, because the problem statement's own worked example
reads that way, and the instantaneous figure is reported alongside it. Instantaneous can
never exceed occupancy, and Gate A asserts that.

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
sql/01_schema.sql       raw_events, content_meta, content_dict
sql/02_sessionize.sql   the state machine, as window functions
sql/03_occupancy.sql    session_minutes and the minute_occupancy rollup
sql/04_deltas.sql       merged minute runs to signed deltas
sql/05_oracles.sql      tables the Python reference is loaded into
src/clickliv/ch.py         HTTP client, one code path for local and Cloud
src/clickliv/reference.py  ground truth, reads the CSV directly
src/clickliv/verify.py     Gate A
src/clickliv/gates.py      Gate B
src/clickliv/sweep.py      threshold sensitivity grid
```

Thresholds and credentials are `${VAR}` placeholders in the SQL, substituted from the
environment, which is what lets one set of files serve local, Cloud, and the sweep.

## Licence

MIT.

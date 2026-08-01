# Correctness

Five independent paths compute the same number, and gates diff them row for row. The
paths are drawn in [architecture.md](architecture.md): the `minute_occupancy` rollup, the
`minute_deltas` cumulative sum, the `maxIntersections` arithmetic oracle, a Python
reference that reads the CSV directly, and chDB running the same SQL in-process.

## Gate A, the cross-path diff

`make verify` proves that two serving paths agree, which is a claim no single path can
make:

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

```
Gate A: PASS  (12/12 checks)
```

## Gate B, idempotent rebuild

`make gate-b` rebuilds the pipeline twice and asserts the serving tables are
byte-identical across the two runs. Against ClickHouse Cloud it produces the same hashes
as the local Docker target, `minute_deltas` hash `2d7c0e268430f7ee`.

## Gate C, the held-out single-day dry run

`make gate-c` reloads the pipeline against the busiest calendar day alone and runs schema
through chDB against that slice, unmodified, so the unseen-day drop is rehearsed before it
happens. It caught a real bug the first time it ran. The full account is in
[operations.md](operations.md#gate-c-the-held-out-dry-run), next to the unseen-day runbook
it rehearses.

## Gate D, chDB agrees with the server

`make chdb` needs no server at all. It builds the entire pipeline inside the Python process
with chDB and checks it against the served tables:

Against the ClickHouse Cloud service:

```
chDB 26.5.1.1 built the whole pipeline in-process in 1.8s, no server

server is ClickHouse 26.4.1.2029

PASS  minute_occupancy     96,818 rows  hash 8231330d0e0603ee
PASS  minute_deltas        33,748 rows  hash 2d7c0e268430f7ee
PASS  active_intervals     32,164 rows  hash 1f6726b4cec03404

Gate D: PASS  chDB agrees with the server
```

Point `.env` at local Docker instead and the same command prints `server is ClickHouse
26.7.1.1315` above the same three hashes. Three ClickHouse versions run this project,
and each one is stated with the environment it came from: **26.4.1.2029** on the Cloud
service, **26.7.1.1315** in local Docker, **26.5.1.1** for embedded chDB. The hashes are
`groupBitXor` over `cityHash64`, which is order independent, so they pin the contents of
the serving tables and nothing about how those rows happen to be laid out on disk.

Same SQL files, three ClickHouse runtimes, three ClickHouse versions, identical hashes.
The portability is not a claim, it is a target you can run.

One capability differs across those versions and the docs say which side of it they are
on. Runnable `EXPLAIN ANALYZE` needs 26.7 or newer, so it works against local Docker and
fails as a syntax error, not a runtime error, against Cloud's 26.4. `answers.py` catches
that and records the reason in the evidence file rather than dropping the section, which
is why `evidence/explain_*.txt` from a Cloud run carries the `EXPLAIN indexes = 1` plan
and an explicit note in place of the `EXPLAIN ANALYZE` one.

## Threshold sensitivity

The gap and grace thresholds are the two guessed numbers in the model, so they are swept
rather than asserted. `make sweep` runs the grid.

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

## Tests

`make test`, 78 tests, `python -m unittest discover -s tests`, zero new dependencies.
Covers the pure, deterministic logic that the four correctness gates never happen to
exercise on their own: SQL statement splitting, credential redaction, JSONCompact
parsing, config construction, the Gate B fingerprint comparison, Gate C's day-splitting
on a CSV, LLM provider selection (no key is a no-op, OpenAI wins when both are set,
Bedrock remains the fallback), and a pin on the exact counts `reconcile()` diffs
against. The gates are the correctness tests for the pipeline itself; this suite is for
the parts around it.

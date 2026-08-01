# Scale

Real peak concurrency here is 2,692, not the worked example's 300K, so judges will ask
how the design behaves at 100x. `make scale` answers with two measured proofs instead of
an assertion, written to `evidence/scale.txt`.

## Sharding is exact, not approximate

Sessionization never lets a session cross a shard boundary, so splitting
`active_intervals` across 8 independent chDB instances by
`cityHash64(video_session_id) % 8`, computing each shard's per-minute session count
alone, and summing the 8 results reproduces the live server's `minute_occupancy` peak
and its full 3,649-minute series exactly. No session is ever double-counted or missed,
by construction, which is why this fans out on any number of workers with no
coordination between them.

## The serving layer's read cost tracks the rollup, not the raw event count

The rollup collapses 905,558 events into 96,818 session-minute rows, and the served
query reads the rollup rather than the events. `evidence/read_cost_by_filter_count.txt`
measures the advantage from `system.query_log.read_rows`, looked up by a query_id the
client generated before the query ran:

```
 filters   read_rows   vs naive
       0      97,043      9.33x
       1      97,493      9.29x
       2      97,943      9.25x
       3      98,168      9.22x
       5      98,393      9.20x
       8      98,393      9.20x
```

The naive baseline is 905,558 rows, the same answer computed straight off the events
with no rollup. The number that matters is not the 9.33x headline but the flatness:
adding eight dimension filters costs 1,350 extra rows, not eight more table scans.

That flatness had to be earned and briefly was not true. The case-fold fallback in
`sql/06_marts.sql` originally resolved an unmatched dimension value with `NOT IN (SELECT
dim FROM minute_occupancy)`, so every added predicate cost another full scan of the fact
table and the advantage decayed toward 1x by the eighth filter. A red team found it by
measurement. The fix materializes the distinct values into `marts.dimension_value`, and
the table above is the after. The before is recorded here rather than quietly deleted,
because the failure mode is the interesting part: a per-predicate subquery against the
fact table is invisible in a single-filter benchmark and linear in the filters a real
user applies.

### What the 10x and 100x sets do and do not prove

The scale sets are built by exact duplication with shifted session ids and time, so both
tables grow by the same factor K and any rollup-to-raw ratio is flat by construction.
That is arithmetic, not evidence of scaling, and it is stated rather than leaned on. What
the sets do establish is that the serving path stays sub-linear in rows scanned as the
table grows, and that sharding stays exact, which is the section above. A claim about
behaviour under organic growth, where sessions gain events rather than being cloned,
would need data we do not have.

## The sort key is the part of this that only matters at scale

The rollup being small is what makes the numbers above comfortable, and it is also what
makes the sort key look free. At 96,818 rows the whole table is one 12-granule part and a
full scan is 7 ms, so no layout can be measurably wrong. The reason the layout was fixed
anyway is that the failure it had was a scaling failure, not a latency one.

`minute` used to sit last in the `ORDER BY`, behind nine dimension columns, and a range
predicate on the last key column cannot binary search. The planner fell back to generic
exclusion search and selected every granule of the largest part for every query. That
costs nothing while a partition holds a single granule, which is where this dataset sits:
an A/B of the two layouts on the same 90-minute window reads 3,930 rows either way. At
100x it is the difference between a query that reads its time window and a query that
reads the day, and it grows linearly with the data while the answer stays the same size.
The [serving
notes](serving.md#the-sort-key-serves-the-one-predicate-the-index-can-actually-use) carry
both plans and are explicit that only the search algorithm moves today.

The bound that replaces it is the one worth quoting at scale: a time-ranged query reads
granules proportional to the window it asks for, not to the table. Partition pruning
already bounds a query to the days it touches, and the primary key now bounds it inside
the day as well; a quiet 90-minute window through `marts.v_occupancy_minute` eliminates
all 17 granules and reads 225 rows, while the peak window reads 89,964 because that is
where the data actually is. Those two bounds compose, so the read cost of the dashboard
tracks the length of the range on the x axis rather than the size of the history behind
it.

The cost is paid in storage and it is bounded and known, because leading with time
breaks up the runs that let the low-cardinality dimensions compress. Measured on the
Cloud service by building both layouts side by side from the same 96,818 rows with
identical codecs and partitioning, and reading `system.parts`:

| `minute_occupancy` sort key | compressed bytes |
|---|---|
| dimensions first, `minute` last | 103,880 |
| `minute` first, the shipped order | 281,006 |

2.71x, and it buys the pruning above. The live table carries 341,142 bytes in
`system.parts` plus 168,636 bytes of `proj_content_minute` in `system.projection_parts`,
which is the honest full footprint of the served table: the projection is a second copy
of the data and is counted as one. Even so the whole serving surface is half a megabyte
against 4.4 MB of raw events, so the rollup stays an order of magnitude smaller than the
thing it replaces.

## Codecs

<a id="codecs"></a>
Every explicit `CODEC` in the schema was measured rather than picked from the column
type. Two of the original choices were actively costing storage, which only showed up
once they were tested.

The method is a controlled A/B: build the table twice on the Cloud service from the same
rows with the same sort key and partitioning, changing only the codec, `OPTIMIZE FINAL`
both, and read `data_compressed_bytes` from `system.parts`. `estimateCompressionRatio`
gives the same ranking in a single query and is the cheap way to re-check on new data.

| column | codec | compressed bytes |
|---|---|---|
| `raw_events.event_time` | `DoubleDelta, ZSTD(1)` | 2,107,768 |
| | `ZSTD(1)` | 1,723,857 |
| | **`Delta, ZSTD(1)`** | **1,525,866** |
| `raw_events.content_id` | `T64, ZSTD(1)` | 302,171 |
| | **`ZSTD(1)`** | **96,077** |
| `raw_events.session_start` | `DoubleDelta, ZSTD(1)` | 164,727 |
| | **`ZSTD(1)`** | **97,054** |

Whole table, same 905,558 rows: 4,616,924 compressed bytes with the old codecs against
3,725,521 with these, 19.3% smaller.

The reasons matter more than the numbers, because they are what carries over to the real
dataset. `DoubleDelta` wins on a near-constant stride and loses on anything jittery, and
heartbeat arrival times jitter at millisecond resolution, so the second-order delta is
noise where the first-order one is small and repetitive. `T64` transposes bit planes on
the assumption that values occupy a narrow range; `content_id` is a wide sparse 64-bit
id, so the transposition destroys the byte runs `ZSTD` would otherwise have found, and
it measures worse on every table that carries the column. `session_start` is one constant
repeated for every event of a session, which compresses on repetition alone.

`DoubleDelta` stays on `minute`, and only there. That column is a dense
constant-stride integer, which is the shape the codec exists for, and it measures 143.81x
against 39.71x for plain `ZSTD` on `minute_occupancy`. The lesson is not that
`DoubleDelta` is bad, it is that a codec is a claim about the shape of the data and has
to be checked against the column it is applied to.

These are declarations, not tuning, so tomorrow's dataset gets them for free. Re-checking
the ranking on new data is one `estimateCompressionRatio` query per column.

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

### Why no approximate cardinality estimator ships

`uniqTheta` and `uniqCombined64` were the obvious things to reach for and neither earned
its place, which is worth stating with the measurement rather than leaving as a gap.
Counted over `raw_events`, memory from `system.query_log.memory_usage`:

| function | distinct users | error | memory |
|---|---|---|---|
| `uniqExact` | 9,618 | 0.000% | 60.0 MB |
| `uniq` | 9,618 | 0.000% | 69.3 MB |
| `uniqCombined64` | 9,628 | 0.104% | 62.4 MB |
| `uniqTheta` | 9,484 | 1.393% | 46.4 MB |

At 9,618 distinct users the hash set is not the cost, the scan is, so an approximate
estimator buys nothing and spends accuracy on a headline number. The crossover is real
but far away, and it was measured rather than guessed:

| distinct values | `uniqExact` memory | `uniqCombined64` memory |
|---|---|---|
| 10,000 | 7.1 MB | 5.6 MB |
| 1,000,000 | 152.4 MB | 5.9 MB |
| 100,000,000 | 7,559.6 MB | 6.0 MB |

`uniqExact` grows linearly and `uniqCombined64` stays flat at roughly 6 MB with 0.002%
error at 100 million. So the rule this project follows is: exact while the distinct count
is in the tens of thousands, which is where SonyLIV's concurrent-user counts sit, and
`uniqCombined64` rather than `uniqTheta` if a future slice ever pushes past a million,
because it was both more accurate and no more expensive at every size tested.

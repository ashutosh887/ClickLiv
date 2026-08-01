# The serving surface

One parameterized view answers any filter combination, a restricted role is the only
granted surface, and a projection is proven rather than asserted.

## The marts surface

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

## RBAC and the query budget

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

The live check of both of those, against the Cloud service, is in [mcp.md](mcp.md), because
the MCP server is the client that runs as `marts_agent`.

## The sort key serves the one predicate the index can actually use

`minute_occupancy` is sorted by `(minute, country, platform, video_type, category,
app_version, player_version, audio_language, subtitle_language, content_id)`. Time
leads, and that is not the obvious choice, so here is the measurement that forced it.

`EXPLAIN indexes = 1` on the served view is the whole argument. Every call into
`marts.v_occupancy_minute` carries a minute range and any of the nine dimension filters,
and the index analyzer reports exactly one usable key:

```
PrimaryKey
  Keys:
    minute
```

The dimension predicates never appear, because the view matches values case
insensitively and `lower(col) = lower(...)` is not something `KeyCondition` can invert
into a range. That applies to all eight string dimensions and it is not a bug to fix,
it is the price of letting a model write `LIVE` and get the live slice. So the nine
dimension columns earn nothing at all as a sort key prefix on the served surface, while
sitting in front of `minute` and pushing the one predicate that does work down to
position ten. A range predicate on the last key column cannot binary
search; it falls back to generic exclusion search, which on this data meant reading every
granule of the largest part on every query.

Measured on the Cloud service by rebuilding both layouts from the same 96,818 rows with
identical codecs and partitioning, and running the same 90-minute window against each:

| 90-minute window | dimensions first | `minute` first |
|---|---|---|
| granules | 1/17 | 1/17 |
| search algorithm | generic exclusion search | binary search |
| rows read | 3,930 | 3,930 |

**Read the row that did not move, not the one that did.** At this size partition pruning
already narrows the query to a single day before the primary key is consulted, so both
layouts read the same rows and the reorder buys nothing measurable today. What changes is
the algorithm the index analyzer picks, and that is the whole claim: generic exclusion
search walks the granules, binary search seeks. The two are indistinguishable when a
partition holds one granule and diverge as it holds thousands. This is a layout fix
justified by where it goes, not by what it currently saves, and
[scale.md](scale.md#the-sort-key-is-the-part-of-this-that-only-matters-at-scale) makes
that case in full.

**Aggregation means output rows are never the denominator.** A live window through
`marts.v_occupancy_minute` at peak reads 89,964 rows to return 80, which is not 1,124x of
waste: nearly all of those rows genuinely match the filter and collapse into the
aggregate. The busiest 90 minutes hold most of their partition and no sort key can prune
them. Any read-amplification claim in this project is measured against rows that match,
never against rows returned.

**The cost is storage, not latency.** Leading with `minute` breaks up the runs that made
the low-cardinality dimensions compress. Measured on the Cloud service by building both
layouts from the same 96,818 rows with identical codecs and partitioning, the compressed
base table goes from 103,880 to 281,006 bytes, 2.71x. Explicit `ZSTD` on those columns
was tried and recovers 5%, so the cost is structural rather than a codec mistake. Wall
clock does not move: an A/B of the two layouts on the same service at the same moment,
full scan of all 96,818 rows, is 7 ms against 7 ms.

**Skip indexes were tried at every stage and none of them earned a place.** With the old
dimensions-first order, a `minmax` index on `minute` eliminates 2 granules of 11 and a
`set(0)` index eliminates 3, because every granule spans nearly the full time range.
Leading with `intDiv(minute, 60)` to keep an hour of dimension clustering does not prune
at all, because `KeyCondition` will not derive a range on `intDiv(minute, 60)` from a
range on `minute`. Only moving the column works.

They were re-tested against the shipped order too, on a copy of `minute_occupancy`
carrying three of them at once:

| index | predicate | granules |
|---|---|---|
| none | `platform = 'ANDROID_PHONE'` | 17/17 |
| `set(0)` on `platform` | `platform = 'ANDROID_PHONE'` | 16/17 |
| none | `content_id = 2078157818` | 17/17 |
| `minmax` on `content_id` | `content_id = 2078157818` | 16/17 |
| `bloom_filter(0.01)` on `content_id` | `content_id = 2078157818` | no granules eliminated |
| `proj_content_minute` | `content_id = 2078157818` | **6/17** |

The arithmetic explains all of it. 96,818 rows over 3,649 minutes is about 26 rows per
minute, so one 8,192-row granule spans roughly 300 minutes and contains essentially every
platform and a large share of the content ids. A per-granule summary of a column cannot
prune when every granule holds every value. Reordering the data can, which is why the
projection wins by a factor the skip indexes never approach. This project therefore ships
no skip index, and that is a measurement rather than an omission.

Every dimension is still in the `ORDER BY`, so the `SummingMergeTree` grouping key is the
same set it always was and no number moves: Gate A stayed 12 of 12, all seven slice peaks
are identical, and the Gate B and Gate D hashes are unchanged, which they must be since
those fingerprints are `groupBitXor` and cannot see layout at all. `country` stays second
rather than being demoted for having one distinct value. Behind a range key its position
is measurably irrelevant, and on a day that carries more than one country it is the first
dimension an equality filter would want. The fix was to promote `minute`, not to punish
`country`.

## Projections, proven not asserted

`content_id` sits last among the dims in `minute_occupancy`'s `ORDER BY` (D7), so a
`content_id` filter gets no useful pruning off the base table. `make projections`
adds `proj_content_minute`, reordered by `(content_id, minute)`, and captures the
before, the after, and the forced comparison to `evidence/projections.txt`:

```
before, optimize_use_projections = 0            ReadFromMergeTree (minute_occupancy)
                                                 Granules: 17/17, generic exclusion search
after, default settings, planner's own choice    ReadFromMergeTree (proj_content_minute)
                                                 Granules: 6/17, binary search
forced, force_optimize_projection_name           same plan, same 6/17
```

The planner picks the projection on its own; forcing it by name lands on the identical
plan, which is the point, not a coincidence to explain away. `system.query_log.projections`
records `['clickliv.minute_occupancy.proj_content_minute']` for the query, so the claim
is checkable after the fact and not just at EXPLAIN time. Read rows for that query fall
from 96,792 to 15,245.

**The projection is persistent, not a demo artifact.** It lives on the Cloud service
between runs and `SELECT * FROM system.projections` lists it, so "show me the projection
working" is answerable without running anything first. The one thing that removes it is a
rebuild: `sql/03_occupancy.sql` drops and recreates `minute_occupancy`, which takes the
projection with it, so `projections` runs after `occupancy` in every pipeline that
rebuilds the table. If `system.projections` is ever empty, that is the reason, and
`make projections` puts it back.

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

**Both objects are deliberately ephemeral.** `open_session_state` and
`mv_extend_open_session` are created by the run and dropped by it again, on the failure
path as well as the success path, same leave-no-trace discipline as Gate C. Expect to
find neither of them on the Cloud service: a materialized view on `raw_events` is a tax
on every subsequent insert, and this one exists to prove a property, not to serve
traffic. `make incremental` recreates them, proves the property, and removes them, and
`evidence/incremental_update.txt` is what survives.

## The dashboard

`make ui` serves a minimal concurrency dashboard: one line chart of peak and average
concurrency per hour, one platform filter, nothing more. No new dependency: it is a
standard-library `http.server` reusing the same zero-dependency `ClickHouse` client as the
rest of the project, reading `marts.v_concurrency` directly. The platform list in the
filter is queried live from `minute_occupancy`, not hand-typed. It listens on your own
machine; see [local development surfaces](operations.md#local-development-surfaces).

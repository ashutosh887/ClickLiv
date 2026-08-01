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

## The dashboard

`make ui` serves a minimal concurrency dashboard: one line chart of peak and average
concurrency per hour, one platform filter, nothing more. No new dependency: it is a
standard-library `http.server` reusing the same zero-dependency `ClickHouse` client as the
rest of the project, reading `marts.v_concurrency` directly. The platform list in the
filter is queried live from `minute_occupancy`, not hand-typed. It listens on your own
machine; see [local development surfaces](operations.md#local-development-surfaces).

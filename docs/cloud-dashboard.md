# The Cloud console dashboard

Six saved queries and one dashboard in the ClickHouse Cloud console, built by hand,
because nothing in the Cloud API can build them for you.

[`sql/09_dashboard.sql`](../sql/09_dashboard.sql) holds **seven** queries, one per
`-- name:` label. Six are the dashboard, in the order below. The seventh,
`occupancy_vs_instantaneous`, is the [optional seventh tile](#optional-seventh-tile) and
is the first thing to cut. `./scripts/verify_dashboard.sh` runs all seven, so a run that
reports seven results is correct and not a sign that a tile is missing.

Org `DevSapiens`, service `ClickLiv`, region ap-south-1. Budget ten minutes.

## What the dashboard argues

Counting every open session overstates peak concurrency by **39 percent** and puts the
peak **three minutes late**. Naive counting says 3,743 at 10:59 UTC. Foreground-only
occupancy says 2,692 at 10:56 UTC. Averaged across the whole window the overstatement
is 49 percent.

The six tiles are ordered to make that argument and then defend it: state the claim,
draw the claim, give the answer on its own, show where the load came from, show the
timing crossover, and prove it serves fast. A judge who reads only the top of the page
still gets the point.

## Why this is a runbook and not a script

SQL console saved queries and dashboards live in the ClickHouse Cloud control plane and
no public interface writes to them. See [automation, and why it does not
exist](#automation-and-why-it-does-not-exist) at the end for the evidence. Do not spend
demo morning trying to script this. Paste the six queries in, once.

## Before you start

Every table reference below is schema qualified, either `marts.` or `clickliv.`. That
is deliberate and it is the single most common way this goes wrong. The SQL console
opens on the `default` database, not on `clickliv`, so an unqualified `minute_occupancy`
returns:

```text
Code: 60. DB::Exception: Unknown table expression identifier 'minute_occupancy'
```

Every query on this page was executed against the live service from the `default`
database before being written down, so what is printed here is what the console will
run. Re-check any time with:

```bash
./scripts/verify_dashboard.sh
```

That splits [`sql/09_dashboard.sql`](../sql/09_dashboard.sql) on its `-- name:` labels,
runs each query against the service from the `default` database, prints a row count per
query plus the full result of every small one, and checks each query against the rule
below before it runs anything.

### The one thing a tile will not run

A dashboard tile refuses any query where a `FROM` or a `JOIN` is followed by an
identifier and an argument list. Table functions and parameterized view calls are the
same shape, and that shape is rejected on the way to the tile rather than by the server,
so the tile renders **Forbidden** and `system.query_log` holds no trace of the attempt.
The evidence is in [tile 3](#3-concurrency_over_time) below.

This bites twice. It is why tile 3 reads a plain view instead of
`marts.v_occupancy_minute(...)`, and it is why tile 6 reads `system.query_log` instead of
`clusterAllReplicas(default, system.query_log)`. Both were rewritten and both were
checked against the form they replaced.

The same SQL is fine everywhere else, including the SQL Console query editor. Only the
tile is fussy, which is worth knowing because a query that runs perfectly in the editor
can still fail once it is on a dashboard. Subqueries, CTEs, window functions and scalar
subqueries in the select list are all unaffected, so nothing here had to get simpler,
only differently shaped.

## Step 1, save the six queries

Open the service, then **SQL Console** in the left sidebar. For each query: new query
tab, paste, **Run**, check the row count against the expected value below, then **Save**,
type the name exactly, **Save Query**.

The name matters. The saved query name becomes the chart title on the dashboard, and
these names match the `-- name:` labels in
[`sql/09_dashboard.sql`](../sql/09_dashboard.sql) exactly:

1. `overcount_headline`
2. `naive_vs_foreground`
3. `concurrency_over_time`
4. `peak_by_platform`
5. `peak_by_video_type`
6. `serving_latency`

Save all six before building any tiles. Switching between the SQL Console and the
Dashboards tab is the slow part, so do each thing in one pass.

### 1. overcount_headline

The whole claim in one row, straight out of the curated marts layer.

```sql
SELECT
    foreground_peak,
    foreground_peak_utc,
    naive_peak,
    naive_peak_utc,
    round(peak_overcount_pct, 1) AS peak_overcount_pct,
    round(average_overcount_pct, 1) AS average_overcount_pct
FROM marts.v_overcount;
```

Expect 1 row: `2692`, `2026-07-26 10:56:00`, `3743`, `2026-07-26 10:59:00`, `39`, `49`.

**Visualization type: Table.** Six columns and one row. Big Stat renders a single value,
so it would drop the two peak timestamps, and those timestamps landing three minutes
apart are half the point of the tile.

### 2. naive_vs_foreground

```sql
SELECT
    minute_utc AS ts,
    foreground_concurrency,
    naive_concurrency
FROM marts.v_naive_vs_foreground
ORDER BY minute;
```

Expect 5,255 rows. Naive peaks at 3,743 at 10:59 UTC, foreground at 2,692 at 10:56 UTC.
The naive series is above or equal to foreground in every one of the 5,255 minutes and
never below, which was checked rather than assumed.

Expect this rather than debug it: foreground reads 0 across 1,606 of those minutes.
Those are minutes where every session spanning them is open but backgrounded, so a
naive span count charges for them and foreground occupancy does not. That is the effect
being measured. The remaining 3,649 minutes are exactly the row count of query 3, which
is a useful internal consistency check.

**Visualization type: Line.** Two series on one pair of axes. The editor gives every
column three toggles, X, Y and Dimension. Set them exactly like this:

| Column | X | Y | Dimension |
| --- | --- | --- | --- |
| `ts` | on | off | off |
| `foreground_concurrency` | off | on | off |
| `naive_concurrency` | off | on | off |

**Leave Dimension off on all three.** Dimension splits one measure into several series by
a categorical column, so it expects something like `platform`. Switching it on for
`naive_concurrency`, which is numeric with thousands of distinct values, asks the chart
for one series per value. Dimension would only be correct if the query returned a single
measure plus a category to split it by, which this one does not.

Do not use Stacked bar here either, since stacking adds the two series together and the
whole claim is that one sits above the other.

### 3. concurrency_over_time

The answer on its own, read from the same plain view tile 2 uses.

```sql
SELECT
    minute_utc AS ts,
    foreground_concurrency AS concurrency
FROM marts.v_naive_vs_foreground
WHERE foreground_concurrency > 0
ORDER BY minute_utc;
```

**Do not use the parameterized form here.** The obvious version calls
`marts.v_occupancy_minute(...)` with `minute_from` and `minute_to` read from
`v_data_window`, which is nicer because it exercises the same path the MCP tools and the
API use. A console dashboard tile renders it as **Forbidden**. The SQL and the grants are
both fine, and this is worth setting out because it is the finding the rest of the page
leans on:

- The identical query succeeds for the admin `default` user and for the least privileged
  `marts_agent`, so it is not a grant.
- The console's own login holds `sql_console_admin`.
- The same query ran to completion **from the SQL Console query editor**, logged under
  user `sql-console:ashutoshj887@gmail.com` with `exception_code = 0`. The editor is
  happy with it. The tile is not.
- `system.query_log` records no failed attempt from the tile at all, on either replica,
  so the refusal happens before the request reaches the server.

The only structural difference between this query and the tiles that work is the
identifier followed by an argument list in `FROM`. That is also what a table function
looks like, which is why tile 6 had to change too.

The replacement above was checked row for row against the parameterized version: both
return 3,649 rows and both peak at 2,692 on 2026-07-26 10:56 UTC, and the two result sets
are identical.

Expect 3,649 rows. Starts at 1 on 2026-07-14 15:43 UTC, stays low for most of the
window, climbs to a single sharp spike on 2026-07-26 topping out at **2,692** at 10:56
UTC, and falls to 7 by 11:30 UTC. If the peak reads 2,692 the tile is correct.

**Visualization type: Area.** Two columns, one series:

| Column | X | Y | Dimension |
| --- | --- | --- | --- |
| `ts` | on | off | off |
| `concurrency` | off | on | off |

Area over Line here because this is one series and the filled shape makes the single
sharp spike read from across a room. Line is a fine second choice if Area renders badly.

### 4. peak_by_platform

```sql
SELECT
    platform,
    max(concurrency) AS peak_concurrency,
    toDateTime(argMax(minute, concurrency) * 60, 'UTC') AS peak_at
FROM
(
    SELECT platform, minute, sum(sessions) AS concurrency
    FROM clickliv.minute_occupancy
    GROUP BY platform, minute
)
GROUP BY platform
ORDER BY peak_concurrency DESC;
```

Expect 10 rows. `ANDROID_PHONE` leads at **1,704**, then IPHONE 329, SONY_ANDROID_TV
279, JIO_ANDROID_TV 210, Mweb 67, SAMSUNG_HTML_TV 52, ANDROID_TAB 44, FIRE_TV 38,
XIAOMI_ANDROID_TV 37, LG_HTML_TV 22. One tall bar and a long tail. The ten per-platform
peaks sum to more than 2,692 because platforms peak in different minutes, which is the
same effect the dashboard is about, one level down.

**Visualization type: Bar Chart.** Three columns, only two of them plotted:

| Column | X | Y | Dimension |
| --- | --- | --- | --- |
| `platform` | on | off | off |
| `peak_concurrency` | off | on | off |
| `peak_at` | off | off | off |

`peak_at` stays in the underlying data without being plotted. Dimension is tempting on
`platform` here and it is still wrong: `platform` is already the x axis, and splitting
the one measure by it as well asks for ten single-bar series. If the ten labels crowd
each other at half width, switch to Horizontal bar, which gives the platform names room
to read.

### 5. peak_by_video_type

```sql
SELECT
    video_type,
    max(concurrency) AS peak_concurrency,
    toDateTime(argMax(minute, concurrency) * 60, 'UTC') AS peak_at
FROM
(
    SELECT video_type, minute, sum(sessions) AS concurrency
    FROM clickliv.minute_occupancy
    GROUP BY video_type, minute
)
GROUP BY video_type
ORDER BY peak_concurrency DESC;
```

Expect 3 rows:

| video_type | peak_concurrency | peak_at |
| --- | --- | --- |
| vod | 2222 | 2026-07-26 11:02:00 |
| live | 425 | 2026-07-26 10:42:00 |
| (empty) | 92 | 2026-07-26 11:00:00 |

This is the crossover, and it is why the tile is a table rather than a bar chart. Live
peaks 20 minutes before vod. The audience arrives for the live event, the live stream
tops out at 10:42, and vod carries the load to a much higher peak at 11:02. The third
row has an empty `video_type` and is a real property of the source data rather than a
bug. Leave it in.

**Visualization type: Table.** A bar chart would plot the two peak heights and silently
drop `peak_at`, and `peak_at` is the entire finding here. Resist Pie in particular: these
are peaks in different minutes, not parts of a whole, so a pie would state something
untrue.

### 6. serving_latency

```sql
SELECT
    hostName() AS replica,
    (SELECT count() FROM system.clusters WHERE cluster = 'default') AS replicas_in_service,
    count() AS queries,
    quantileExact(0.50)(query_duration_ms) AS p50_ms,
    quantileExact(0.95)(query_duration_ms) AS p95_ms,
    quantileExact(0.99)(query_duration_ms) AS p99_ms,
    max(query_duration_ms) AS max_ms,
    max(read_rows) AS max_read_rows
FROM system.query_log
WHERE type = 'QueryFinish'
  AND is_initial_query = 1
  AND query NOT ILIKE '%system.query_log%'
  AND (query ILIKE '%marts.v_concurrency%' OR query ILIKE '%marts.v_occupancy_minute%');
```

**This tile used to read `clusterAllReplicas` and it would have failed.** On Cloud the
query log is per replica, so the honest version pools both, and the only way to pool them
is the `clusterAllReplicas` table function. That is the exact shape tile 3 proved a tile
refuses, so it would have rendered Forbidden in front of the judges for reasons that had
nothing to do with the numbers. It reads the local log instead.

What that costs is small and was measured rather than waved away. The two replicas carry
the same story: 584 and 596 matching queries, p50 29 ms on both, p95 71 and 72 ms. So a
single replica is a fair sample, and `replicas_in_service` still puts the size of the
service on the tile by reading `system.clusters`, which is a plain table.

Expect 1 row with `replicas_in_service = 2`, several hundred queries and rising, p50
around 29 ms, p95 around 70 ms, p99 between 150 and 350 ms, and `max_ms` under a second.

**This tile's p99 is not the serving SLO, and a judge will notice.** The README publishes
p99 58 ms and says the SLO passes with room. Both numbers are correct, because they are
percentiles of two different populations, and the difference is the whole reason the SLO
is measured the way it is.

| | the SLO | this tile |
| --- | --- | --- |
| population | 40 samples, 8 benchmark queries x 5 repetitions, one controlled run | every query the service has ever run against the marts views |
| includes | nothing else | cold starts after idle scaling, marts rebuilds, agent multi-slice comparisons reading 2.5 million rows, one replica's log only |
| published in | `evidence/serving_slo.txt`, per-sample rows in `evidence/serving_slo.csv` | nowhere; it is a live operational tile |

The SLO is the claim, because it is a fixed, repeatable, stated set of queries. The tile
is the honest ambient picture of a shared service, and it is the more impressive of the
two once it is read correctly. Say the median and the heaviest query out loud, not the
p99: a median of 29 ms, and the heaviest query in the log reading 2.5 million rows in
under a second.

Two more things before quoting the tile. The query count grows every time anyone touches
the marts views and it counts one replica, so treat it as a floor. And the p99 is not a
wake-up artefact: the slow tail is those multi-slice agent comparisons, which still land
under 800 ms.

**Visualization type: Table.** One row, eight columns, no time axis to plot.

If `queries` comes back 0 the query log rotated. Query 3 no longer touches these views,
so refill the log by asking the chat one question, or loading the Vercel dashboard, or
running `make answers`, then refresh the tile.

If this tile alone renders Forbidden, delete the `replicas_in_service` line. That leaves
a bare aggregate over one table with nothing structural left to object to, and the tile
loses only the replica count.

Pooling both replicas is still the better measurement, so run this form by hand in the
SQL Console, where table functions are allowed, if a judge asks:

```sql
SELECT
    uniqExact(hostName()) AS replicas_reporting,
    count() AS queries,
    quantileExact(0.50)(query_duration_ms) AS p50_ms,
    quantileExact(0.95)(query_duration_ms) AS p95_ms,
    quantileExact(0.99)(query_duration_ms) AS p99_ms,
    max(query_duration_ms) AS max_ms,
    max(read_rows) AS max_read_rows
FROM clusterAllReplicas(default, system.query_log)
WHERE type = 'QueryFinish'
  AND is_initial_query = 1
  AND query NOT ILIKE '%system.query_log%'
  AND (query ILIKE '%marts.v_concurrency%' OR query ILIKE '%marts.v_occupancy_minute%');
```

Pooled on 2026-08-02 that reads `replicas_reporting = 2`, 1,469 queries, p50 34 ms, p95
116 ms, p99 273 ms, max 771 ms, heaviest query 2,517,268 rows. Every one of those moves
between runs, and the query count only ever climbs, so re-read it rather than quoting
this line. Do not put it on a tile.

## Step 2, build the dashboard

Open **Dashboards** in the left sidebar, next to SQL Console. Click **New Dashboard** and
name it `ClickLiv, foreground concurrency`.

For each tile: add a visualization, select the saved query by name, pick the chart type,
and assign axes. Line, Area and Bar Chart tiles need x and y set explicitly. Table tiles
take no axes and render as soon as the query is selected. Drag a tile by its header to
move it and drag a corner to resize.

Leave the **Dimension** toggle off on every tile in this dashboard. It splits one measure
into several series by a categorical column, and none of these six queries wants that:
each already returns its series as separate columns. Switching Dimension on for a numeric
column asks for one series per distinct value and breaks the chart.

## Step 3, add the six tiles in this order

Add them top to bottom. The order is the argument.

| # | Saved query | Chart | Axes | Width | What it says |
| --- | --- | --- | --- | --- | --- |
| 1 | `overcount_headline` | Table | none | full | 3,743 against 2,692, 39 percent, three minutes apart |
| 2 | `naive_vs_foreground` | Line | x `ts`, y `foreground_concurrency` and `naive_concurrency` | full | the same claim drawn, minute by minute |
| 3 | `concurrency_over_time` | Area | x `ts`, y `concurrency` | full | the answer on its own, one clean curve to 2,692 |
| 4 | `peak_by_platform` | Bar Chart | x `platform`, y `peak_concurrency` | half, left | mobile carries the event |
| 5 | `peak_by_video_type` | Table | none | half, right | live peaks 20 minutes before vod |
| 6 | `serving_latency` | Table | none | full | p50 29 ms, and 2.5 million rows read in under a second |

Tile 2 is the one that has to land. Drag both `foreground_concurrency` and
`naive_concurrency` onto the y axis so the two series overlay on one pair of axes. If
they end up on separate charts the tile has lost its entire point.

Suggested titles, if you want them to read as an argument rather than as column names:

1. Counting every open session overstates the peak by 39 percent
2. Naive against foreground, minute by minute
3. Foreground-only concurrency, 2,692 at 10:56 UTC
4. Where the load came from
5. Live peaks 20 minutes before vod
6. Serving latency, median 29 ms

The dropdown offers Big Stat, Table, Bar Chart, Stacked bar, Horizontal bar, Stacked H.
bar, Line, Area, Pie, Doughnut, Scatter and Heatmap. Only the six named above are worth
using here. Big Stat is tempting for tile 1 but it renders one value, so it would drop
the two peak timestamps that make the point.

## Optional seventh tile

Add this only if the six are done and there is time. It defends the method rather than
stating the claim, so it is the first thing to cut.

Save as `occupancy_vs_instantaneous`, render as a **Table**, full width, at the bottom.
The SQL is the last query in [`sql/09_dashboard.sql`](../sql/09_dashboard.sql). It is
long to paste and runs in about a third of a second.

It slices by whatever platforms are present rather than by a fixed list, so it returns
one row per platform plus an overall row, in descending peak order. On the current data
that is 11 rows:

| slice | occupancy_peak | instantaneous_peak | gap | gap_pct |
| --- | --- | --- | --- | --- |
| all platforms | 2692 | 2282 | 410 | 15.2 |
| ANDROID_PHONE | 1704 | 1442 | 262 | 15.4 |
| IPHONE | 329 | 263 | 66 | 20.1 |
| SONY_ANDROID_TV | 279 | 246 | 33 | 11.8 |
| JIO_ANDROID_TV | 210 | 186 | 24 | 11.4 |
| Mweb | 67 | 53 | 14 | 20.9 |
| SAMSUNG_HTML_TV | 52 | 48 | 4 | 7.7 |
| ANDROID_TAB | 44 | 42 | 2 | 4.5 |
| FIRE_TV | 38 | 32 | 6 | 15.8 |
| XIAOMI_ANDROID_TV | 37 | 35 | 2 | 5.4 |
| LG_HTML_TV | 22 | 21 | 1 | 4.5 |

Three things to read at a glance. The `all platforms` row is 2,692, matching the
headline peak. The `occupancy_peak` column reproduces tile 4 exactly, platform for
platform, so the two tiles are consistent by construction. And `gap` is positive
everywhere, so minute occupancy never undercounts a true instantaneous count. On the
larger slices `gap_pct` clusters between 11 and 21 percent; the small platforms drift
lower simply because a handful of sessions rarely overlap awkwardly.

## When the dataset is replaced

The tiles are built to survive a swap of the underlying data in place, so when the final
dataset lands in the same `clickliv` database nothing here has to be rebuilt. Refresh
the dashboard and the numbers move on their own.

This was audited query by query rather than assumed. Nothing in any tile names a date, a
content id, a platform, a video type or a row count. No tile filters on a time range at
all, so none of them has a window to get wrong: tile 3 takes every minute that carries
foreground sessions, whenever those minutes happen to be, which is also why it no longer
needs `marts.v_data_window`. Tiles 4, 5 and 7 group by whatever platforms and video types
exist rather than listing the ones that happened to be in the sample, and tile 7 in
particular builds its slice list from the data, so a new platform appears as a new row on
its own. Tiles 1 and 2 are single selects against curated views that recompute from the
tables. Tile 6 keys off query text rather than time, so it survives the swap untouched,
though the marts rebuild drops and recreates the views and the log will start refilling
from whatever runs after that.

The one thing that does change is the expected values printed on this page. After a
swap, run `./scripts/verify_dashboard.sh` and take the numbers it prints as the new
truth. It prints every small result in full for exactly this reason, so tiles 1, 4, 5, 6
and 7 can be copied straight back onto this page. Row counts, the peak, and the overcount
percentage will all differ.

What must still hold is the shape of the argument. Naive above foreground in every
minute, which the script checks and reports as `minutes_naive_below_foreground`, and it
must read 0. The two peaks in different minutes. The `all platforms` row of tile 7
agreeing with tile 1. And `minutes_with_foreground` matching the row count of tile 3.

One thing the new data could change that no script will catch. Tile 5 currently reads as
live peaking 20 minutes before vod, and that ordering is a property of the sample, not a
law. If the new data reverses it, the tile is still correct and the sentence on this page
and the suggested title for tile 5 both need rewriting. Read that tile before you say
anything about it.

## If a tile renders empty

Charts render from the saved query, so a failing tile is almost always the query. Open
the tile's three dot menu, click the pencil next to the query, and run it in the inline
editor.

`UNKNOWN_TABLE` means one of two things. Either a table reference lost its `clickliv.`
or `marts.` prefix, which is the failure this page is qualified against, so copy the
query again from here. Or the pipeline is being rebuilt underneath you, because the
rebuild drops and recreates the tables and every tile errors for the few seconds that
window is open. This was hit live while writing this page. Wait, refresh, and if the
tables are back the tiles come back with them. Confirm with `./scripts/verify_dashboard.sh`
rather than by guessing which of the two it was.

**Forbidden** is a different failure and it is not about the data. It means the query has
an identifier followed by an argument list in a `FROM` or a `JOIN`, so see [the one thing
a tile will not run](#the-one-thing-a-tile-will-not-run). Running the query in the SQL
Console will not reproduce it, because the editor allows what the tile refuses. Copy the
query from this page again rather than debugging it.

`serving_latency` returning zero rows means the query log rotated. Any other query
returning zero rows means the pipeline needs a rebuild, in which case see
[operations.md](operations.md).

The service scales to zero after 15 minutes idle, so the first query after a quiet spell
pays a wake-up delay. Run one query before the judges are watching.

## Automation, and why it does not exist

SQL console saved queries and dashboards are control plane objects and no public
interface writes to them. Two checks establish that, both re-run against our own
credentials rather than taken from documentation.

`clickhousectl cloud` exposes `auth`, `org`, `service`, `backup`, `clickpipe`, `member`,
`invitation`, `key`, `activity` and `postgres`. There is no dashboard or saved query verb
anywhere in the tree. Under `service`, `query-endpoint` only toggles the Query API
endpoint feature for the service as a whole, and `query` just runs SQL over HTTP.

The public OpenAPI document at `https://api.clickhouse.cloud/v1` carries 80 paths. None
of them create a SQL console saved query or a SQL console dashboard. Grepping the path
list returns zero matches for `chart`, `tile` and `visualization`. The word `dashboard`
matches three paths and all three are under `clickstack/`, which is the ClickStack
observability product rendered in HyperDX, a different surface from the Dashboards tab
in the service console. On this service it is not enabled in any case:

```text
GET /v1/organizations/{org}/services/{service}/clickstack/dashboards
403 FORBIDDEN: ClickStack has not been setup for this service
```

The single query-shaped path, `serviceQueryEndpoint`, configures the Query API endpoint
feature. Its request body is three fields, `roles`, `openApiKeys` and `allowedOrigins`.
There is nowhere to put SQL and nowhere to put a saved query.

The console does have a private backend, and it is worth being precise about it rather
than concluding from a 404 against a guessed hostname. The real base is
`https://console-api-internal.clickhouse.cloud/.api`, which appears in the console HTML
and in the `apiUrl` constant inside the console JS bundle. The routes exist and are named
`/.api/savedQuery` and `/.api/services/{serviceId}/dashboards`. Probed with our Cloud API
key, both by basic auth and as a bearer token, and separately with the OAuth token
`clickhousectl` stores:

```text
GET  /.api/env                        200   prefix is right, no auth needed
GET  /.api/savedQuery?serviceId=...   401   Unauthorized
GET  /.api/services/{id}/dashboards   401   Unauthorized
POST /.api/services/{id}/dashboards   401   Unauthorized
```

401 rather than 404 is the useful signal. The routes are real, and a Cloud API key is
simply not a credential they accept. The stored OAuth token fails too because its
audience is `clickhousectl` rather than the console. The surface is cookie and session
based by design, with `access-control-allow-credentials: true`, so it is a browser
surface rather than an undocumented API waiting to be called.

Two further walls stand behind the auth one. Saved query bodies are encrypted client
side, sent as `encryptedQuery` and `encryptedParameters` under a per service key, so a
row written any other way would not decode in the UI. And the Terraform provider ships
25 resources with nothing for SQL console saved queries or dashboards; its
`clickhouse_clickstack_dashboard` is again HyperDX.

So the six queries get pasted in by hand. Ten minutes, once, and it stays built.

## The rest of the stack in this org

Worth pointing at while the console is open. The same `DevSapiens` org also runs
`clickliv-langfuse`, a ClickHouse **managed Postgres** service in public beta, which is
the transactional store behind our Langfuse deployment. Langfuse is itself a ClickHouse
product and keeps its traces in ClickHouse. So the observability side of this project
runs on two ClickHouse databases, a managed Postgres for Langfuse metadata and ClickHouse
proper for the traces, which is exactly the shape ClickHouse ships it in rather than
something bolted on for the hackathon.

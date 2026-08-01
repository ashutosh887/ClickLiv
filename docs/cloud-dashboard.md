# The Cloud console dashboard

Six saved queries and one dashboard in the ClickHouse Cloud console, built by hand,
because nothing in the Cloud API can build them for you.

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
runs each query against the service from the `default` database, and prints a row count
per query plus the headline numbers.

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

### 3. concurrency_over_time

The answer on its own, through the parameterized view the MCP tools and the API also
call, so the tile exercises the same path as the rest of the project.

```sql
SELECT
    toDateTime(minute * 60, 'UTC') AS ts,
    concurrency
FROM marts.v_occupancy_minute(
    country = '', platform = '', video_type = '', content_id = 0,
    minute_from = (SELECT min_minute FROM marts.v_data_window),
    minute_to = (SELECT max_minute FROM marts.v_data_window))
ORDER BY minute;
```

Expect 3,649 rows. Starts at 1 on 2026-07-14 15:43 UTC, stays low for most of the
window, climbs to a single sharp spike on 2026-07-26 topping out at **2,692** at 10:56
UTC, and falls to 7 by 11:30 UTC. If the peak reads 2,692 the tile is correct.

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

### 6. serving_latency

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

Expect 1 row with `replicas_reporting = 2`, a query count in the high hundreds and
rising, p50 around 27 ms, p95 around 70 ms, p99 under 150 ms.

The query count grows every time anyone touches the marts views, so treat it as a floor
rather than a fixed number. Two things must hold. `replicas_reporting = 2` proves the
numbers come off both replicas rather than whichever one the console happened to route
to, which is why this reads `clusterAllReplicas` instead of `system.query_log` directly.
And p99 stays comfortably under 200 ms.

If `replicas_reporting` comes back 1 the service scaled down to a single replica. If
`queries` comes back 0 the query log rotated, so run query 3 once and refresh the tile.

## Step 2, build the dashboard

Open **Dashboards** in the left sidebar, next to SQL Console. Click **New Dashboard** and
name it `ClickLiv, foreground concurrency`.

For each tile: add a visualization, select the saved query by name, pick the chart type,
and assign axes. Line and bar tiles need x and y set explicitly. Table tiles take no
axes and render as soon as the query is selected. Drag a tile by its header to move it
and drag a corner to resize.

## Step 3, add the six tiles in this order

Add them top to bottom. The order is the argument.

| # | Saved query | Chart | Axes | Width | What it says |
| --- | --- | --- | --- | --- | --- |
| 1 | `overcount_headline` | Table | none | full | 3,743 against 2,692, 39 percent, three minutes apart |
| 2 | `naive_vs_foreground` | Line | x `ts`, y `foreground_concurrency` and `naive_concurrency` | full | the same claim drawn, minute by minute |
| 3 | `concurrency_over_time` | Line | x `ts`, y `concurrency` | full | the answer on its own, one clean curve to 2,692 |
| 4 | `peak_by_platform` | Bar | x `platform`, y `peak_concurrency` | half, left | mobile carries the event |
| 5 | `peak_by_video_type` | Table | none | half, right | live peaks 20 minutes before vod |
| 6 | `serving_latency` | Table | none | full | p99 under 150 ms across both replicas |

Tile 2 is the one that has to land. Drag both `foreground_concurrency` and
`naive_concurrency` onto the y axis so the two series overlay on one pair of axes. If
they end up on separate charts the tile has lost its entire point.

Suggested titles, if you want them to read as an argument rather than as column names:

1. Counting every open session overstates the peak by 39 percent
2. Naive against foreground, minute by minute
3. Foreground-only concurrency, 2,692 at 10:56 UTC
4. Where the load came from
5. Live peaks 20 minutes before vod
6. Serving latency across both replicas

If the console offers a single value, number or metric chart type, use it for tile 1
with `peak_overcount_pct` as the value. A table is the safe fallback and it fits.

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

Nothing in any tile names a date, a content id or a dimension value. Tile 3 reads its
minute range out of `marts.v_data_window` instead of assuming when the data starts and
ends. Tiles 4, 5 and 7 group by whatever platforms and video types exist rather than
listing the ones that happened to be in the sample. Tiles 1 and 2 are single selects
against curated views that recompute from the tables. Tile 6 keys off query text, not
time.

The one thing that does change is the expected values printed on this page. After a
swap, run `./scripts/verify_dashboard.sh` and take the numbers it prints as the new
truth. Row counts, the peak, and the overcount percentage will all differ. What must
still hold is the shape of the argument: naive above foreground in every minute, the two
peaks in different minutes, and the `all platforms` row of tile 7 agreeing with tile 1.

## If a tile renders empty

Charts render from the saved query, so a failing tile is almost always the query. Open
the tile's three dot menu, click the pencil next to the query, and run it in the inline
editor.

`UNKNOWN_TABLE` means a table reference lost its `clickliv.` or `marts.` prefix. That is
the failure this page is qualified against, so copy the query again from here.

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

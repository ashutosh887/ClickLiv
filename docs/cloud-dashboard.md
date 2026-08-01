# The Cloud console dashboard

Six saved queries and one dashboard, built by hand in the ClickHouse Cloud console,
because nothing in the Cloud API can build them for you.

## Why this is a runbook and not a script

SQL console saved queries and dashboards live in the ClickHouse Cloud control plane,
and no public interface writes to them. Three checks establish that:

`clickhousectl cloud` exposes `auth`, `org`, `service`, `backup`, `clickpipe`,
`member`, `invitation`, `key`, `activity`, and `postgres`. There is no dashboard or
saved query verb anywhere in the tree. Under `service`, `query-endpoint` only enables
or disables the Query API endpoint feature for the service as a whole, and `query`
just runs SQL over HTTP.

The public OpenAPI document at `https://api.clickhouse.cloud/v1` carries 80 paths.
None of them touch SQL console saved queries or SQL console dashboards. The word
`dashboard` appears only under `clickstack/dashboards`, which is the ClickStack
observability product and renders in HyperDX rather than in the console Dashboards
tab. On this service it is unavailable anyway:

```text
GET /v1/organizations/{org}/services/{service}/clickstack/dashboards
403 FORBIDDEN: ClickStack has not been setup for this service
```

The console's own backend documents exactly one route,
`https://console-api.clickhouse.cloud/.api/query-endpoints/{queryEndpointId}/run`,
and it runs a saved query that already exists. Probing that host with the same
read/write API key that returns 200 on `/v1/organizations` returns 404 on
`/.api/query-endpoints`, `/.api/saved-queries`, `/.api/dashboards`, and on the
org-scoped and service-scoped variants of both. Creation is a console UI operation.

So the six queries below get pasted in by hand. Ten minutes, once.

## Context

Org `DevSapiens`, service `ClickLiv`, region ap-south-1. Open the service, then the
**SQL Console** in the left sidebar. Every query below runs against the default
database on that service.

All six numbers in this file were re-verified live against the Cloud service after
the most recent pipeline rebuild. Nothing has moved.

## Step 1, save the six queries

For each query: open a new query tab, paste the SQL, press **Run**, confirm the
result matches the expected output, then click **Save** next to **Run**, type the
name exactly as given, and click **Save Query**. The name matters, because the chart
title on the dashboard is taken from the query name.

Name them exactly. These match the `-- name:` labels in
[`sql/09_dashboard.sql`](../sql/09_dashboard.sql), with one deliberate exception:
query 2 is saved as `naive_vs_foreground` rather than its full label
`naive_vs_foreground_any_open_session_span_vs_foreground_only`, because the saved
query name becomes the chart title and the full label does not fit on a tile.

1. `concurrency_over_time`
2. `naive_vs_foreground`
3. `peak_by_platform`
4. `peak_by_video_type`
5. `occupancy_vs_instantaneous`
6. `serving_latency`

### 1. concurrency_over_time

Visualization: **Line**. x axis `ts`, y axis `concurrency`.

```sql
SELECT
    toDateTime(minute * 60, 'UTC') AS ts,
    minute,
    concurrency
FROM marts.v_occupancy_minute(
    country = '', platform = '', video_type = '', content_id = 0,
    minute_from = 0, minute_to = 4294967295)
ORDER BY minute;
```

Expect 3,649 rows. The series starts at 1 on 2026-07-14 15:43 UTC, stays low for
most of the window, then climbs into a single sharp spike on 2026-07-26 that tops out
at **2,692** at 10:56 UTC, and falls to 7 by 11:30 UTC. If the peak reads 2,692 the
tile is correct. This is the answer to the headline question, drawn.

### 2. naive_vs_foreground

Visualization: **Line**. x axis `ts`, then drag both `foreground_only_concurrency`
and `naive_any_open_session_concurrency` onto the y axis so the two series overlay.

```sql
SELECT
    toDateTime(minute * 60, 'UTC') AS ts,
    minute,
    maxIf(concurrency, series = 'foreground') AS foreground_only_concurrency,
    maxIf(concurrency, series = 'naive') AS naive_any_open_session_concurrency
FROM
(
    SELECT
        minute,
        toUInt64(sum(sessions)) AS concurrency,
        'foreground' AS series
    FROM minute_occupancy
    GROUP BY minute
    UNION ALL
    SELECT
        minute,
        toUInt64(uniqExact(video_session_id)) AS concurrency,
        'naive' AS series
    FROM
    (
        SELECT
            video_session_id,
            arrayJoin(range(toUInt32(ts_start_ms DIV 60000),
                            toUInt32((ts_end_ms - 1) DIV 60000) + 1)) AS minute
        FROM
        (
            SELECT
                video_session_id,
                min(toUnixTimestamp64Milli(session_start)) AS ts_start_ms,
                max(toUnixTimestamp64Milli(event_time)) + 1 AS ts_end_ms
            FROM raw_events
            GROUP BY video_session_id
        )
    )
    GROUP BY minute
)
GROUP BY minute
ORDER BY minute;
```

Expect 5,255 rows. The naive line peaks at **3,743** at 10:59 UTC. The foreground
line peaks at **2,692** at 10:56 UTC. Naive overstates the peak by 28.1 percent and
puts it three minutes late. The naive line sits above the foreground line everywhere,
never below.

One thing to expect rather than debug: the foreground line reads 0 across 1,606 of
those minutes. Those are minutes where every session that spans them is open but
backgrounded, so a naive span count charges them and foreground occupancy does not.
That is the effect being measured, and it is why this query returns more rows than
query 1.

This is the headline tile. Give it the top of the dashboard.

### 3. peak_by_platform

Visualization: **Bar**. x axis `platform`, y axis `peak_concurrency`.

```sql
SELECT
    platform,
    max(concurrency) AS peak_concurrency,
    argMax(minute, concurrency) AS peak_minute,
    toDateTime(argMax(minute, concurrency) * 60, 'UTC') AS peak_at
FROM
(
    SELECT platform, minute, sum(sessions) AS concurrency
    FROM minute_occupancy
    GROUP BY platform, minute
)
GROUP BY platform
ORDER BY peak_concurrency DESC;
```

Expect 10 rows, sorted descending. `ANDROID_PHONE` leads at **1,704** at minute
29751056, which is 10:56 UTC. Then IPHONE 329, SONY_ANDROID_TV 279, JIO_ANDROID_TV
210, Mweb 67, SAMSUNG_HTML_TV 52, ANDROID_TAB 44, FIRE_TV 38, XIAOMI_ANDROID_TV 37,
LG_HTML_TV 22. One tall bar and a long tail. Note that the ten per-platform peaks sum
to more than 2,692, because platforms peak at different minutes.

### 4. peak_by_video_type

Visualization: **Table**. The timing column is the point, and a three-bar chart would
hide it.

```sql
SELECT
    video_type,
    max(concurrency) AS peak_concurrency,
    argMax(minute, concurrency) AS peak_minute,
    toDateTime(argMax(minute, concurrency) * 60, 'UTC') AS peak_at
FROM
(
    SELECT video_type, minute, sum(sessions) AS concurrency
    FROM minute_occupancy
    GROUP BY video_type, minute
)
GROUP BY video_type
ORDER BY peak_concurrency DESC;
```

Expect 3 rows:

| video_type | peak_concurrency | peak_minute | peak_at |
| --- | --- | --- | --- |
| vod | 2222 | 29751062 | 2026-07-26 11:02:00 |
| live | 425 | 29751042 | 2026-07-26 10:42:00 |
| (empty) | 92 | 29751060 | 2026-07-26 11:00:00 |

This is the crossover. Live peaks 20 minutes before vod does. The audience arrives
for the live event, the live stream tops out at 10:42, and vod carries the load to a
much higher peak at 11:02. The third row has an empty `video_type` and is a real
property of the source data rather than a bug in the query. Leave it in.

### 5. occupancy_vs_instantaneous

Visualization: **Table**. Six columns, seven rows, all of them load-bearing.

```sql
WITH clipped AS
(
    SELECT
        arrayJoin(arrayFilter(x -> x != '', [
            'no filter',
            if(dims.platform = 'ANDROID_PHONE', 'platform ANDROID_PHONE', ''),
            if(dims.platform = 'SONY_ANDROID_TV', 'platform SONY_ANDROID_TV', ''),
            if(dims.video_type = 'live', 'video_type live', ''),
            if(dims.audio_language = 'hin', 'audio_language hin', ''),
            if(dims.platform = 'IPHONE' AND dims.country = 'india', 'IPHONE in india', ''),
            if(dims.video_type = 'vod' AND dims.platform = 'Mweb', 'vod on Mweb', '')])) AS slice,
        pieces.video_session_id AS sid,
        greatest(pieces.ts_start_ms, toInt64(pieces.minute) * 60000) AS clip_start,
        least(pieces.ts_end_ms, (toInt64(pieces.minute) + 1) * 60000) AS clip_end
    FROM
    (
        SELECT
            video_session_id,
            ts_start_ms,
            ts_end_ms,
            arrayJoin(range(toUInt32(ts_start_ms DIV 60000),
                            toUInt32((ts_end_ms - 1) DIV 60000) + 1)) AS minute
        FROM active_intervals
    ) AS pieces
    INNER JOIN session_minutes AS dims
        ON dims.video_session_id = pieces.video_session_id AND dims.minute = pieces.minute
),
merged AS
(
    SELECT slice, sid, min(clip_start) AS clip_start, max(clip_end) AS clip_end
    FROM
    (
        SELECT
            slice, sid, clip_start, clip_end,
            sum(opens) OVER (PARTITION BY slice, sid ORDER BY clip_start ASC, clip_end ASC
                ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS run
        FROM
        (
            SELECT
                slice, sid, clip_start, clip_end,
                if(max(clip_end) OVER (PARTITION BY slice, sid ORDER BY clip_start ASC, clip_end ASC
                    ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) >= clip_start, 0, 1) AS opens
            FROM clipped
        )
    )
    GROUP BY slice, sid, run
),
instantaneous AS
(
    SELECT slice, toUInt32(maxIntersections(clip_start, clip_end - 1)) AS instantaneous_peak
    FROM merged
    GROUP BY slice
),
occupancy AS
(
    SELECT
        slice,
        max(concurrency) AS occupancy_peak,
        argMax(minute, concurrency) AS occupancy_peak_minute
    FROM
    (
        SELECT slice, minute, sum(sessions) AS concurrency
        FROM
        (
            SELECT
                arrayJoin(arrayFilter(x -> x != '', [
                    'no filter',
                    if(platform = 'ANDROID_PHONE', 'platform ANDROID_PHONE', ''),
                    if(platform = 'SONY_ANDROID_TV', 'platform SONY_ANDROID_TV', ''),
                    if(video_type = 'live', 'video_type live', ''),
                    if(audio_language = 'hin', 'audio_language hin', ''),
                    if(platform = 'IPHONE' AND country = 'india', 'IPHONE in india', ''),
                    if(video_type = 'vod' AND platform = 'Mweb', 'vod on Mweb', '')])) AS slice,
                minute,
                sessions
            FROM minute_occupancy
        )
        GROUP BY slice, minute
    )
    GROUP BY slice
)
SELECT
    occupancy.slice AS slice,
    occupancy.occupancy_peak AS occupancy_peak,
    instantaneous.instantaneous_peak AS instantaneous_peak,
    occupancy.occupancy_peak - instantaneous.instantaneous_peak AS gap,
    round(100 * (occupancy.occupancy_peak - instantaneous.instantaneous_peak)
          / occupancy.occupancy_peak, 1) AS gap_pct,
    occupancy.occupancy_peak_minute AS occupancy_peak_minute
FROM occupancy
INNER JOIN instantaneous ON instantaneous.slice = occupancy.slice
ORDER BY occupancy_peak DESC;
```

Expect 7 rows:

| slice | occupancy_peak | instantaneous_peak | gap | gap_pct | occupancy_peak_minute |
| --- | --- | --- | --- | --- | --- |
| no filter | 2692 | 2282 | 410 | 15.2 | 29751056 |
| platform ANDROID_PHONE | 1704 | 1442 | 262 | 15.4 | 29751056 |
| audio_language hin | 1614 | 1405 | 209 | 12.9 | 29751058 |
| video_type live | 425 | 354 | 71 | 16.7 | 29751042 |
| IPHONE in india | 329 | 263 | 66 | 20.1 | 29751055 |
| platform SONY_ANDROID_TV | 279 | 246 | 33 | 11.8 | 29751053 |
| vod on Mweb | 62 | 51 | 11 | 17.7 | 29751062 |

Check two things at a glance. The first row reads 2692 against 2282, matching the
headline peak in tile 1. And `gap_pct` stays in a narrow band from 11.8 to 20.1
across all seven slices, so the difference between minute occupancy and a true
instantaneous count is a stable property of the workload rather than an artifact of
one particular filter.

### 6. serving_latency

Visualization: **Table**. One row, seven columns.

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

Expect `replicas_reporting = 2`, roughly 291 queries, p50 24 ms, p95 83 ms, p99 133
ms, max 144 ms, max_read_rows 1,170,722.

This one is live and it grows. The query count rises every time anyone hits the marts
views, so treat 291 as a floor rather than a fixed number. What must hold is
`replicas_reporting = 2`, which proves the numbers come off both replicas rather than
whichever one the console happened to route to, and a p99 comfortably under 200 ms.
If `replicas_reporting` comes back 1, the service scaled down to a single replica.
If `queries` comes back 0, the query log was rotated, so run one query against
`marts.v_occupancy_minute` and refresh the tile.

## Step 2, build the dashboard

Open **Dashboards** in the left sidebar of the service, next to SQL Console. Click
**New Dashboard** and name it `ClickLiv, foreground concurrency`.

For each tile, add a visualization, give it a title and subtitle, select the saved
query by name, pick the chart type from the chart type selector, and assign the axes
listed above. Bar and line tiles need x and y assigned explicitly. Table tiles do not
take axes, so they render as soon as the query is selected.

Drag tiles by their header to move them and drag a corner to resize.

## Step 3, arrange the six so they tell the story

The order below reads top to bottom as one argument: the metric is wrong by default,
here is what it looks like when it is right, here is where the load actually came
from, and here is proof it serves fast.

**Row 1, full width, the claim.** `naive_vs_foreground` as a line chart. Two lines, 3,743 against 2,692. Title it
  something like "Counting open sessions overstates the peak by 28 percent". This is
  the only tile that has to land, so give it the full width of the dashboard and the
  top row.

**Row 2, full width, the answer.** `concurrency_over_time` as a line chart. One clean curve to 2,692. This is the
  number being submitted, on its own, with no comparison line to argue with.

**Row 3, two tiles side by side, where the load came from.** `peak_by_platform` as a bar chart on the left, `peak_by_video_type` as a table on
  the right. Mobile carries the event, and live peaks 20 minutes before vod. Put them
  next to each other so the crossover in the table is read against the platform mix
  in the bar chart.

**Row 4, two tiles side by side, why to trust it and how fast it serves.** `occupancy_vs_instantaneous` as a table on the left, `serving_latency` as a table on
the right. The left one shows the method is stable across seven different slices.
  The right one shows p99 under 200 ms across both replicas.

Four rows, six tiles. If someone only looks at the top of the page they still get the
whole point.

## If a tile renders empty

Charts render from the saved query, so a tile that fails is almost always the query
rather than the dashboard. Open the tile's three dot menu, click the pencil next to
the query to open the inline editor, and run it there. `serving_latency` returning
zero rows means the query log was rotated. Any other query returning zero rows means
the pipeline needs a rebuild, in which case see [operations.md](operations.md).

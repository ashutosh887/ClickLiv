-- name: overcount_headline
SELECT
    foreground_peak,
    foreground_peak_utc,
    naive_peak,
    naive_peak_utc,
    round(peak_overcount_pct, 1) AS peak_overcount_pct,
    round(average_overcount_pct, 1) AS average_overcount_pct
FROM marts.v_overcount;

-- name: naive_vs_foreground
SELECT
    minute_utc AS ts,
    foreground_concurrency,
    naive_concurrency
FROM marts.v_naive_vs_foreground
ORDER BY minute;

-- name: concurrency_over_time
SELECT
    toDateTime(minute * 60, 'UTC') AS ts,
    concurrency
FROM marts.v_occupancy_minute(
    country = '', platform = '', video_type = '', content_id = 0,
    minute_from = (SELECT min_minute FROM marts.v_data_window),
    minute_to = (SELECT max_minute FROM marts.v_data_window))
ORDER BY minute;

-- name: peak_by_platform
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

-- name: peak_by_video_type
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

-- name: serving_latency
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

-- name: occupancy_vs_instantaneous
WITH pieces AS
(
    SELECT
        video_session_id,
        ts_start_ms,
        ts_end_ms,
        arrayJoin(range(toUInt32(ts_start_ms DIV 60000),
                        toUInt32((ts_end_ms - 1) DIV 60000) + 1)) AS minute
    FROM clickliv.active_intervals
),
clipped AS
(
    SELECT
        arrayJoin(['all platforms', dims.platform]) AS slice,
        pieces.video_session_id AS sid,
        greatest(pieces.ts_start_ms, toInt64(pieces.minute) * 60000) AS clip_start,
        least(pieces.ts_end_ms, (toInt64(pieces.minute) + 1) * 60000) AS clip_end
    FROM pieces
    INNER JOIN clickliv.session_minutes AS dims
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
            SELECT arrayJoin(['all platforms', platform]) AS slice, minute, sessions
            FROM clickliv.minute_occupancy
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
    toDateTime(occupancy.occupancy_peak_minute * 60, 'UTC') AS occupancy_peak_at
FROM occupancy
INNER JOIN instantaneous ON instantaneous.slice = occupancy.slice
ORDER BY occupancy_peak DESC;

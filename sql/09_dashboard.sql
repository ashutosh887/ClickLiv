-- name: concurrency_over_time
SELECT
    toDateTime(minute * 60, 'UTC') AS ts,
    minute,
    concurrency
FROM marts.v_occupancy_minute(
    country = '', platform = '', video_type = '', content_id = 0,
    minute_from = 0, minute_to = 4294967295)
ORDER BY minute;

-- name: naive_vs_foreground_any_open_session_span_vs_foreground_only
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

-- name: peak_by_platform
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

-- name: peak_by_video_type
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

-- name: occupancy_vs_instantaneous
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

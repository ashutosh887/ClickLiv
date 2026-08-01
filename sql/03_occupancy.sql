DROP TABLE IF EXISTS session_minutes;

CREATE TABLE session_minutes
(
    video_session_id  String CODEC(ZSTD(1)),
    minute            UInt32 CODEC(DoubleDelta, ZSTD(1)),
    platform          LowCardinality(String),
    app_version       LowCardinality(String),
    country           LowCardinality(String),
    audio_language    LowCardinality(String),
    subtitle_language LowCardinality(String),
    player_version    LowCardinality(String),
    content_id        UInt64 CODEC(T64, ZSTD(1)),
    video_type        LowCardinality(String),
    category          LowCardinality(String)
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(toDateTime(minute * 60, 'UTC'))
ORDER BY (video_session_id, minute);

INSERT INTO session_minutes
WITH
    covered AS
    (
        SELECT DISTINCT
            video_session_id,
            arrayJoin(range(toUInt32(ts_start_ms DIV 60000),
                            toUInt32((ts_end_ms - 1) DIV 60000) + 1)) AS minute
        FROM active_intervals
    ),
    observed AS
    (
        SELECT
            video_session_id,
            toUInt32(t DIV 60000) AS minute,
            argMax(dims, (ord, dims)) AS dims
        FROM
        (
            SELECT
                video_session_id,
                toUnixTimestamp64Milli(event_time) AS t,
                t * 8 + multiIf(
                    event_type = 'VideoSessionStart', 1,
                    event_type = 'VideoPlay'
                        OR event IN ('resume', 'speed-resume', 'AdResume'), 2,
                    event IN ('pause', 'speed-pause', 'AdPause'), 4,
                    event_type = 'AppBackgrounded', 5,
                    event_type = 'AppForegrounded', 3,
                    event_type IN ('VideoError', 'VideoSessionEnd'), 6,
                    0) AS ord,
                CAST((platform, app_version, country, audio_language,
                      subtitle_language, player_version, content_id),
                     'Tuple(String, String, String, String, String, String, UInt64)') AS dims
            FROM raw_events
        )
        GROUP BY video_session_id, minute
    )
SELECT
    video_session_id,
    minute,
    dims.1,
    dims.2,
    dims.3,
    dims.4,
    dims.5,
    dims.6,
    dims.7,
    dictGetOrDefault('content_dict', 'video_type', dims.7, ''),
    dictGetOrDefault('content_dict', 'category', dims.7, '')
FROM
(
    SELECT
        video_session_id,
        minute,
        is_covered,
        any_dims,
        argMax(dims, if(any_dims, toInt64(minute), -1)) OVER (
            PARTITION BY video_session_id ORDER BY minute
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS dims
    FROM
    (
        SELECT
            video_session_id,
            minute,
            max(is_covered) AS is_covered,
            max(has_dims) AS any_dims,
            argMax(dims, has_dims) AS dims
        FROM
        (
            SELECT
                video_session_id,
                minute,
                1 AS is_covered,
                0 AS has_dims,
                CAST(('', '', '', '', '', '', 0),
                     'Tuple(String, String, String, String, String, String, UInt64)') AS dims
            FROM covered
            UNION ALL
            SELECT
                video_session_id,
                minute,
                0 AS is_covered,
                1 AS has_dims,
                dims
            FROM observed
        )
        GROUP BY video_session_id, minute
    )
)
WHERE is_covered;

DROP TABLE IF EXISTS minute_occupancy;

CREATE TABLE minute_occupancy
(
    country           LowCardinality(String),
    platform          LowCardinality(String),
    video_type        LowCardinality(String),
    category          LowCardinality(String),
    app_version       LowCardinality(String),
    player_version    LowCardinality(String),
    audio_language    LowCardinality(String),
    subtitle_language LowCardinality(String),
    content_id        UInt64 CODEC(T64, ZSTD(1)),
    minute            UInt32 CODEC(DoubleDelta, ZSTD(1)),
    sessions          UInt32
)
ENGINE = SummingMergeTree(sessions)
PARTITION BY toYYYYMMDD(toDateTime(minute * 60, 'UTC'))
ORDER BY (country, platform, video_type, category, app_version,
          player_version, audio_language, subtitle_language, content_id, minute);

INSERT INTO minute_occupancy
SELECT
    country,
    platform,
    video_type,
    category,
    app_version,
    player_version,
    audio_language,
    subtitle_language,
    content_id,
    minute,
    toUInt32(count()) AS sessions
FROM session_minutes
GROUP BY
    country, platform, video_type, category, app_version,
    player_version, audio_language, subtitle_language, content_id, minute;

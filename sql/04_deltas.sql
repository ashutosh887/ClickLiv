DROP TABLE IF EXISTS minute_deltas;

CREATE TABLE minute_deltas
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
    delta             Int32
)
ENGINE = SummingMergeTree(delta)
PARTITION BY toYYYYMMDD(toDateTime(minute * 60, 'UTC'))
ORDER BY (country, platform, video_type, category, app_version,
          player_version, audio_language, subtitle_language, content_id, minute);

INSERT INTO minute_deltas
WITH
    runs AS
    (
        SELECT
            dims,
            min(minute) AS first_minute,
            max(minute) AS last_minute
        FROM
        (
            SELECT
                video_session_id,
                minute,
                dims,
                sum(is_new) OVER (
                    PARTITION BY video_session_id ORDER BY minute
                    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS run_id
            FROM
            (
                SELECT
                    video_session_id,
                    minute,
                    dims,
                    (lagInFrame(minute, 1, toUInt32(0)) OVER w = 0)
                        OR (minute != lagInFrame(minute, 1, toUInt32(0)) OVER w + 1)
                        OR (dims != lagInFrame(dims, 1, dims) OVER w) AS is_new
                FROM
                (
                    SELECT
                        video_session_id,
                        minute,
                        CAST((country, platform, video_type, category, app_version,
                              player_version, audio_language, subtitle_language, content_id),
                             'Tuple(String, String, String, String, String, String, String, String, UInt64)')
                            AS dims
                    FROM session_minutes
                )
                WINDOW w AS (
                    PARTITION BY video_session_id ORDER BY minute
                    ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING)
            )
        )
        GROUP BY video_session_id, run_id, dims
    )
SELECT dims.1, dims.2, dims.3, dims.4, dims.5, dims.6, dims.7, dims.8, dims.9,
       point.1 AS minute, point.2 AS delta
FROM
(
    SELECT
        dims,
        arrayJoin([(toUInt32(first_minute), toInt32(1)),
                   (toUInt32(last_minute + 1), toInt32(-1))]) AS point
    FROM runs
);

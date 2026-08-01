DROP TABLE IF EXISTS ref_intervals;

CREATE TABLE ref_intervals
(
    video_session_id String,
    segment_id       UInt32,
    ts_start_ms      Int64,
    ts_end_ms        Int64
)
ENGINE = MergeTree
ORDER BY (video_session_id, ts_start_ms);

DROP TABLE IF EXISTS ref_rollup;

CREATE TABLE ref_rollup
(
    minute            UInt32,
    platform          LowCardinality(String),
    app_version       LowCardinality(String),
    country           LowCardinality(String),
    audio_language    LowCardinality(String),
    subtitle_language LowCardinality(String),
    player_version    LowCardinality(String),
    content_id        UInt64,
    video_type        LowCardinality(String),
    category          LowCardinality(String),
    sessions          UInt32
)
ENGINE = MergeTree
ORDER BY (minute, platform, content_id);

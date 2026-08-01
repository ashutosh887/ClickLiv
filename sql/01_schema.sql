CREATE TABLE IF NOT EXISTS content_meta
(
    content_id UInt64,
    title      String,
    video_type LowCardinality(String),
    category   LowCardinality(String)
)
ENGINE = MergeTree
ORDER BY content_id;

CREATE DICTIONARY IF NOT EXISTS content_dict
(
    content_id UInt64,
    title      String,
    video_type String,
    category   String
)
PRIMARY KEY content_id
SOURCE(CLICKHOUSE(
    USER '${CH_USER}' PASSWORD '${CH_PASSWORD}'
    DB '${CH_DATABASE}' TABLE 'content_meta'))
LAYOUT(HASHED())
LIFETIME(MIN 300 MAX 600);

CREATE TABLE IF NOT EXISTS raw_events
(
    video_session_id  String               CODEC(ZSTD(1)),
    event_time        DateTime64(3, 'UTC') CODEC(DoubleDelta, ZSTD(1)),
    user_id           String               CODEC(ZSTD(1)),
    content_id        UInt64               CODEC(T64, ZSTD(1)),
    event_type        LowCardinality(String),
    event             LowCardinality(String),
    platform          LowCardinality(String),
    app_version       LowCardinality(String),
    country           LowCardinality(String),
    audio_language    LowCardinality(String),
    subtitle_language LowCardinality(String),
    player_version    LowCardinality(String),
    session_start     DateTime64(3, 'UTC') CODEC(DoubleDelta, ZSTD(1))
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(event_time)
ORDER BY (video_session_id, event_time);

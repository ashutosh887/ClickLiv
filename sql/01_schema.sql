CREATE TABLE IF NOT EXISTS content_meta
(
    content_id UInt64,
    title      String,
    video_type LowCardinality(String),
    category   LowCardinality(String),
-- show_name is plain String, not LowCardinality: it is closer to title in cardinality
-- than to category, and a LowCardinality dictionary stops paying its way well before
-- one distinct value per show across a 33K-title catalogue. Empty for a content file
-- that does not carry the column, so one schema serves both datasets.
    show_name  String
)
ENGINE = MergeTree
ORDER BY content_id;

CREATE DICTIONARY IF NOT EXISTS content_dict
(
    content_id UInt64,
    title      String,
    video_type String,
    category   String,
    show_name  String
)
PRIMARY KEY content_id
SOURCE(CLICKHOUSE(
    USER '${CH_USER}' PASSWORD '${CH_PASSWORD}'
    DB '${CH_DATABASE}' TABLE 'content_meta'))
LAYOUT(HASHED())
LIFETIME(MIN 300 MAX 600);

CREATE TABLE IF NOT EXISTS raw_events
(
-- Codecs here are measured, not assumed; see docs/scale.md#codecs. Delta beats
-- DoubleDelta on event_time because millisecond heartbeat arrival jitters, so the
-- second-order delta is noise where the first-order one is small and repetitive.
-- content_id is a wide sparse UInt64, which is the case T64 is worst at: transposing
-- the bit planes of near-random 64-bit ids destroys the byte runs ZSTD would have
-- found. Plain ZSTD is 3.1x smaller there. session_start is one constant per session,
-- so it compresses on repetition and any delta stage only gets in the way.
    video_session_id  String               CODEC(ZSTD(1)),
    event_time        DateTime64(3, 'UTC') CODEC(Delta, ZSTD(1)),
    user_id           String               CODEC(ZSTD(1)),
    content_id        UInt64               CODEC(ZSTD(1)),
    event_type        LowCardinality(String),
    event             LowCardinality(String),
    platform          LowCardinality(String),
    app_version       LowCardinality(String),
    country           LowCardinality(String),
    audio_language    LowCardinality(String),
    subtitle_language LowCardinality(String),
    player_version    LowCardinality(String),
    session_start     DateTime64(3, 'UTC') CODEC(ZSTD(1)),
    video_resolution  LowCardinality(String)
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(event_time)
ORDER BY (video_session_id, event_time);

DROP TABLE IF EXISTS open_session_state;

-- Tracks, per session still open when we last looked, exactly enough state to extend
-- its active segment as new heartbeats arrive: where the segment started, and the
-- latest moment it is still known to be on. ReplacingMergeTree because a session gets
-- touched repeatedly; version = the same t*8+kind ordinal used everywhere else, so
-- later events always win the merge, and reads use argMax(..., version) rather than
-- FINAL, same discipline as every other table here.
CREATE TABLE open_session_state
(
    video_session_id String CODEC(ZSTD(1)),
    segment_start_ms Int64 CODEC(DoubleDelta, ZSTD(1)),
    last_active_ms   Int64 CODEC(DoubleDelta, ZSTD(1)),
    version          Int64
)
ENGINE = ReplacingMergeTree(version)
ORDER BY video_session_id;

DROP VIEW IF EXISTS mv_extend_open_session;

-- Fires on every INSERT INTO raw_events, sees only the new block. For a session this
-- table already tracks as open, a later event that is not itself a close signal
-- (pause/background/error/end) and lands within the gap threshold of the last known
-- activity extends the open segment live, with no rebuild of active_intervals. A
-- session not currently tracked here, or an event outside the gap, is left to the
-- batch sessionizer: this view targets exactly the scenario the problem statement
-- names, "open sessions keep evolving as heartbeats arrive", not every edge case the
-- full window-function sessionizer already owns.
CREATE MATERIALIZED VIEW mv_extend_open_session
TO open_session_state
AS
WITH ${GAP_SECONDS} * 1000 AS gap_ms
SELECT
    new.video_session_id AS video_session_id,
    state.segment_start_ms AS segment_start_ms,
    new.t AS last_active_ms,
    new.ord AS version
FROM
(
    SELECT
        video_session_id,
        toUnixTimestamp64Milli(event_time) AS t,
        toUnixTimestamp64Milli(event_time) * 8 + multiIf(
            event_type = 'VideoSessionStart', 1,
            event_type = 'VideoPlay' OR event IN ('resume', 'speed-resume', 'AdResume'), 2,
            event IN ('pause', 'speed-pause', 'AdPause'), 4,
            event_type = 'AppBackgrounded', 5,
            event_type = 'AppForegrounded', 3,
            event_type IN ('VideoError', 'VideoSessionEnd'), 6, 0) AS ord,
        multiIf(
            event_type = 'VideoSessionStart', 1,
            event_type = 'VideoPlay' OR event IN ('resume', 'speed-resume', 'AdResume'), 2,
            event IN ('pause', 'speed-pause', 'AdPause'), 4,
            event_type = 'AppBackgrounded', 5,
            event_type = 'AppForegrounded', 3,
            event_type IN ('VideoError', 'VideoSessionEnd'), 6, 0) AS kind
    FROM raw_events
) AS new
INNER JOIN
(
    SELECT video_session_id,
           argMax(segment_start_ms, version) AS segment_start_ms,
           argMax(last_active_ms, version) AS last_active_ms
    FROM open_session_state
    GROUP BY video_session_id
) AS state
ON new.video_session_id = state.video_session_id
WHERE new.kind NOT IN (4, 5, 6)
  AND new.t > state.last_active_ms
  AND new.t - state.last_active_ms <= gap_ms;

-- Seed: sessions whose last-ever event so far is not a close signal, and whose last
-- active_intervals segment was closed only by running out of data (ts_end_ms equals
-- last event plus grace, per 02_sessionize.sql). These are genuinely open right now.
INSERT INTO open_session_state
WITH
    ${GAP_SECONDS} * 1000   AS gap_ms,
    ${GRACE_SECONDS} * 1000 AS grace_ms,
    last_event AS
    (
        SELECT
            video_session_id,
            argMax(multiIf(
                event_type = 'VideoSessionStart', 1,
                event_type = 'VideoPlay' OR event IN ('resume', 'speed-resume', 'AdResume'), 2,
                event IN ('pause', 'speed-pause', 'AdPause'), 4,
                event_type = 'AppBackgrounded', 5,
                event_type = 'AppForegrounded', 3,
                event_type IN ('VideoError', 'VideoSessionEnd'), 6, 0),
                toUnixTimestamp64Milli(event_time)) AS last_kind,
            max(toUnixTimestamp64Milli(event_time)) AS last_t
        FROM raw_events
        GROUP BY video_session_id
    ),
    last_segment AS
    (
        SELECT video_session_id, max(segment_id) AS max_seg
        FROM active_intervals
        GROUP BY video_session_id
    )
SELECT ai.video_session_id, ai.ts_start_ms, le.last_t, le.last_t * 8 + le.last_kind
FROM last_event AS le
INNER JOIN last_segment AS ls ON le.video_session_id = ls.video_session_id
INNER JOIN active_intervals AS ai
    ON ai.video_session_id = ls.video_session_id AND ai.segment_id = ls.max_seg
WHERE le.last_kind NOT IN (4, 5, 6)
  AND ai.ts_end_ms = le.last_t + grace_ms;

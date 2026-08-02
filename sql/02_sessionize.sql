DROP TABLE IF EXISTS active_intervals;

CREATE TABLE active_intervals
(
    video_session_id String CODEC(ZSTD(1)),
    segment_id       UInt32,
-- Segment boundaries are millisecond epochs ordered by session, not by time, so
-- consecutive values jump arbitrarily and neither delta stage pays for itself.
-- Measured: DoubleDelta 2.00x, Delta 2.18x, plain ZSTD 2.25x (docs/scale.md#codecs).
    ts_start_ms      Int64 CODEC(ZSTD(1)),
    ts_end_ms        Int64 CODEC(ZSTD(1))
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(fromUnixTimestamp64Milli(ts_start_ms, 'UTC'))
ORDER BY (video_session_id, ts_start_ms);

INSERT INTO active_intervals
WITH
    ${GAP_SECONDS} * 1000   AS gap_ms,
    ${GRACE_SECONDS} * 1000 AS grace_ms
SELECT
    video_session_id,
    toUInt32(row_number() OVER (
        PARTITION BY video_session_id ORDER BY ts_start_ms)) AS segment_id,
    ts_start_ms,
    ts_end_ms
FROM
(
    SELECT
        video_session_id,
        min(ord) DIV 8 AS ts_start_ms,
        argMax(segment_end, ord) AS ts_end_ms
    FROM
    (
        SELECT
            video_session_id,
            ord,
            is_on,
            sum(opens) OVER (
                PARTITION BY video_session_id ORDER BY ord
                ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS segment_id,
            if(next_t < 0 OR next_t - t > gap_ms, t + grace_ms, next_t) AS segment_end
        FROM
        (
            SELECT
                video_session_id,
                t,
                ord,
                is_on,
                lagInFrame(is_on, 1, 0) OVER win AS prev_on,
                lagInFrame(t, 1, -1)    OVER win AS prev_t,
                leadInFrame(t, 1, -1)   OVER win AS next_t,
                is_on AND (prev_on = 0 OR (prev_t >= 0 AND t - prev_t > gap_ms)) AS opens
            FROM
            (
                SELECT
                    video_session_id,
                    t,
                    ord,
                    argMax(coalesce(playing_signal, 1), if(playing_signal IS NULL, -1, ord)) OVER run
                        AND
                    argMax(coalesce(fg_signal, 1), if(fg_signal IS NULL, -1, ord)) OVER run
                        AS is_on
                FROM
                (
                    SELECT
                        video_session_id,
                        t,
                        t * 8 + kind AS ord,
-- Playing defaults to on, and a session start asserts it. An extract is a window cut
-- out of a longer stream, so a session already playing when the window opens emitted
-- its VideoPlay before the first row and will never emit another. Defaulting to off
-- made those sessions invisible until they happened to resume, which manufactured a
-- 46 minute ramp on the sealed day where the data is flat: 2,552 rising to 18,161
-- against a true 21,400. Pause, background, error and end all still switch it off, so
-- every exclusion the problem statement names is unaffected.
                        multiIf(kind = 1, 1, kind = 2, 1, kind = 4, 0, kind = 6, 0, NULL)
                            AS playing_signal,
                        multiIf(kind = 1, 1, kind = 3, 1, kind = 5, 0, NULL)
                            AS fg_signal
                    FROM
                    (
                        SELECT DISTINCT
                            video_session_id,
                            toUnixTimestamp64Milli(event_time) AS t,
                            multiIf(
                                event_type = 'VideoSessionStart', 1,
                                event_type = 'VideoPlay'
                                    OR lower(event) IN ('resume', 'speed-resume', 'adresume'), 2,
                                lower(event) IN ('pause', 'speed-pause', 'adpause'), 4,
                                event_type = 'AppBackgrounded', 5,
                                event_type = 'AppForegrounded', 3,
                                event_type IN ('VideoError', 'VideoSessionEnd'), 6,
                                0) AS kind
                        FROM raw_events
                    )
                )
                WINDOW run AS (
                    PARTITION BY video_session_id ORDER BY ord
                    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
            )
            WINDOW win AS (
                PARTITION BY video_session_id ORDER BY ord
                ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING)
        )
    )
    WHERE is_on
    GROUP BY video_session_id, segment_id
)
WHERE ts_end_ms > ts_start_ms;

-- ${MARTS_DB} is "marts" for the normal database and "marts_<database>" for anything
-- else, so a held-out or unseen-day run against a scratch database builds its own
-- surface instead of dropping and rebinding the one the live demo reads.
DROP DATABASE IF EXISTS ${MARTS_DB};
CREATE DATABASE ${MARTS_DB};

-- Filter, then sum across whatever dims are left unfiltered, then bucket by minute.
-- Never group by a subset of dims: D6 holds only if summing happens before the max.
-- A zero content_id means "no filter on that dim", via the coalesce/nullIf idiom,
-- verified against 26.7 in DOSSIER_REVIEW Part 2.
--
-- The string dims take a wider sentinel set. Empty string is the canonical "no filter"
-- value and stays exactly as it was, but a model writing SQL against this view by hand
-- reaches for 'ALL' or '*' long before it reaches for '', and a guessed sentinel that is
-- not recognised silently returns zero rows instead of erroring. So every common guess
-- is accepted, case-insensitively and after trimming: '', ALL, ANY, NONE, NULL, * and %.
-- None of them collide with a real value (india, live, vod, the platform names), so the
-- semantics for real values are untouched.
--
-- Real values fall back to a case-insensitive match, but an exact match always wins. A
-- model asked to compare live against vod writes 'LIVE' and 'VOD', which used to match
-- nothing and came back as a confident zero rather than an error. Blanket case folding
-- would be worse: audio_language holds both 'hin' and 'HIN' as distinct slices, and
-- subtitle_language does the same, so folding unconditionally would silently merge two
-- real values and inflate the answer. So the rule is: if the value the caller passed
-- exists exactly, match it exactly; only when it matches nothing at all does the case
-- fold apply. 'hin' stays 1,614, 'HIN' stays its own slice, and 'LIVE' still finds live.
--
-- Every one of the eight sort key dimensions is a parameter here, so one view really does
-- answer any filter combination rather than the common four.
--
-- SQL SECURITY DEFINER: marts_agent is granted SELECT on ${MARTS_DB}.* only, never on
-- clickliv.minute_occupancy directly. Without DEFINER a view runs with the invoker's
-- own privileges, and ClickHouse checks those against the underlying table, which
-- would force granting the raw serving tables too and defeat the whole point of a
-- marts surface. Verified: an invoker-rights view 403s for marts_agent even though
-- the view itself is granted; a DEFINER view does not.
CREATE VIEW ${MARTS_DB}.v_occupancy_full
(
    `minute`      UInt32 COMMENT 'Minutes since the unix epoch. Multiply by 60 for a unix timestamp. Query the v_data_window view for the range this dataset actually covers.',
    `concurrency` UInt64 COMMENT 'Foreground-only concurrent sessions in that minute, summed across every dimension left unfiltered.'
)
DEFINER = ${CH_USER} SQL SECURITY DEFINER
COMMENT 'Parameterized, one parameter per filterable dimension: country, platform, video_type, category, app_version, player_version, audio_language, subtitle_language, content_id, minute_from, minute_to. All eleven are required. For no filter on a string dimension pass an empty string, or ALL, ANY, NONE, NULL, * or % in any case; for no filter on content_id pass 0. An exact value always wins; a value that matches nothing exactly falls back to a case-insensitive match, so LIVE finds live while hin and HIN stay distinct. Valid values are in v_dimension_values, titles are in v_titles, and the minute range is in v_data_window.'
AS
SELECT minute, sum(sessions) AS concurrency
FROM minute_occupancy
WHERE (lower(trimBoth({country:String}))           IN ('', 'all', 'any', 'none', 'null', '*', '%') OR country           = trimBoth({country:String})           OR (lower(country)           = lower(trimBoth({country:String}))           AND trimBoth({country:String})           NOT IN (SELECT country           FROM minute_occupancy)))
  AND (lower(trimBoth({platform:String}))          IN ('', 'all', 'any', 'none', 'null', '*', '%') OR platform          = trimBoth({platform:String})          OR (lower(platform)          = lower(trimBoth({platform:String}))          AND trimBoth({platform:String})          NOT IN (SELECT platform          FROM minute_occupancy)))
  AND (lower(trimBoth({video_type:String}))        IN ('', 'all', 'any', 'none', 'null', '*', '%') OR video_type        = trimBoth({video_type:String})        OR (lower(video_type)        = lower(trimBoth({video_type:String}))        AND trimBoth({video_type:String})        NOT IN (SELECT video_type        FROM minute_occupancy)))
  AND (lower(trimBoth({category:String}))          IN ('', 'all', 'any', 'none', 'null', '*', '%') OR category          = trimBoth({category:String})          OR (lower(category)          = lower(trimBoth({category:String}))          AND trimBoth({category:String})          NOT IN (SELECT category          FROM minute_occupancy)))
  AND (lower(trimBoth({app_version:String}))       IN ('', 'all', 'any', 'none', 'null', '*', '%') OR app_version       = trimBoth({app_version:String})       OR (lower(app_version)       = lower(trimBoth({app_version:String}))       AND trimBoth({app_version:String})       NOT IN (SELECT app_version       FROM minute_occupancy)))
  AND (lower(trimBoth({player_version:String}))    IN ('', 'all', 'any', 'none', 'null', '*', '%') OR player_version    = trimBoth({player_version:String})    OR (lower(player_version)    = lower(trimBoth({player_version:String}))    AND trimBoth({player_version:String})    NOT IN (SELECT player_version    FROM minute_occupancy)))
  AND (lower(trimBoth({audio_language:String}))    IN ('', 'all', 'any', 'none', 'null', '*', '%') OR audio_language    = trimBoth({audio_language:String})    OR (lower(audio_language)    = lower(trimBoth({audio_language:String}))    AND trimBoth({audio_language:String})    NOT IN (SELECT audio_language    FROM minute_occupancy)))
  AND (lower(trimBoth({subtitle_language:String})) IN ('', 'all', 'any', 'none', 'null', '*', '%') OR subtitle_language = trimBoth({subtitle_language:String}) OR (lower(subtitle_language) = lower(trimBoth({subtitle_language:String})) AND trimBoth({subtitle_language:String}) NOT IN (SELECT subtitle_language FROM minute_occupancy)))
  AND content_id = coalesce(nullIf({content_id:UInt64}, toUInt64(0)), content_id)
  AND minute BETWEEN {minute_from:UInt32} AND {minute_to:UInt32}
GROUP BY minute
ORDER BY minute;

-- The four dimensions almost every caller filters on, over the same expression. Kept as
-- its own signature because the dashboard, the Vercel functions and the MCP server all
-- call it with six parameters; widening that signature in place would break them the
-- moment this file is applied, since a parameterized view has no defaults.
CREATE VIEW ${MARTS_DB}.v_occupancy_minute
(
    `minute`      UInt32 COMMENT 'Minutes since the unix epoch. Multiply by 60 for a unix timestamp. Query the v_data_window view for the range this dataset actually covers.',
    `concurrency` UInt64 COMMENT 'Foreground-only concurrent sessions in that minute, summed across every dimension left unfiltered.'
)
DEFINER = ${CH_USER} SQL SECURITY DEFINER
COMMENT 'Parameterized. Call as v_occupancy_minute(country=..., platform=..., video_type=..., content_id=..., minute_from=..., minute_to=...); all six are required. For no filter pass an empty string, or ALL, ANY, NONE, NULL, * or % in any case, and pass content_id = 0. An exact value always wins; a value that matches nothing exactly falls back to a case-insensitive match, so LIVE finds live while hin and HIN stay distinct. For category, app_version, player_version, audio_language or subtitle_language use v_occupancy_full instead. Valid values are in v_dimension_values and the minute range is in v_data_window.'
AS
SELECT minute, concurrency
FROM ${MARTS_DB}.v_occupancy_full(
    country = {country:String}, platform = {platform:String},
    video_type = {video_type:String}, category = '', app_version = '',
    player_version = '', audio_language = '', subtitle_language = '',
    content_id = {content_id:UInt64},
    minute_from = {minute_from:UInt32}, minute_to = {minute_to:UInt32});

-- Same filter contract, bucketed to any grain. peak is max(concurrency) inside the
-- bucket, average is the mean of the per-minute concurrency inside the bucket, and
-- the caller states which by picking a column, per D21.
CREATE VIEW ${MARTS_DB}.v_concurrency
(
    `bucket_minute`       UInt64  COMMENT 'Start of the bucket, in minutes since the unix epoch.',
    `peak_concurrency`    UInt64  COMMENT 'Highest per-minute concurrency inside the bucket. Order by this descending for the busiest bucket.',
    `average_concurrency` Float64 COMMENT 'Mean per-minute concurrency inside the bucket.',
    `minutes_in_bucket`   UInt64  COMMENT 'Minutes inside the bucket that carried at least one session.'
)
DEFINER = ${CH_USER} SQL SECURITY DEFINER
COMMENT 'Parameterized. Call as v_concurrency(country=..., platform=..., video_type=..., content_id=..., minute_from=..., minute_to=..., grain_minutes=...); all seven are required. For no filter pass an empty string, or ALL, ANY, NONE, NULL, * or % in any case, and pass content_id = 0. grain_minutes is 1 for minute, 60 for hour, 1440 for day. For the busiest moment overall, take minute_from and minute_to from v_data_window, grain_minutes = 1, and order by peak_concurrency descending. For the other four dimensions use v_concurrency_full.'
AS
SELECT
    intDiv(minute, {grain_minutes:UInt32}) * {grain_minutes:UInt32} AS bucket_minute,
    max(concurrency)                                                AS peak_concurrency,
    avg(concurrency)                                                AS average_concurrency,
    count()                                                         AS minutes_in_bucket
FROM ${MARTS_DB}.v_occupancy_minute(
    country = {country:String}, platform = {platform:String},
    video_type = {video_type:String}, content_id = {content_id:UInt64},
    minute_from = {minute_from:UInt32}, minute_to = {minute_to:UInt32})
GROUP BY bucket_minute
ORDER BY bucket_minute;

-- The bucketed form of the eleven-parameter surface.
CREATE VIEW ${MARTS_DB}.v_concurrency_full
(
    `bucket_minute`       UInt64  COMMENT 'Start of the bucket, in minutes since the unix epoch.',
    `peak_concurrency`    UInt64  COMMENT 'Highest per-minute concurrency inside the bucket. Order by this descending for the busiest bucket.',
    `average_concurrency` Float64 COMMENT 'Mean per-minute concurrency inside the bucket.',
    `minutes_in_bucket`   UInt64  COMMENT 'Minutes inside the bucket that carried at least one session.'
)
DEFINER = ${CH_USER} SQL SECURITY DEFINER
COMMENT 'Parameterized, every dimension plus a grain: country, platform, video_type, category, app_version, player_version, audio_language, subtitle_language, content_id, minute_from, minute_to, grain_minutes. All twelve are required. Sentinels and case folding work exactly as in v_occupancy_full. grain_minutes is 1 for minute, 60 for hour, 1440 for day.'
AS
SELECT
    intDiv(minute, {grain_minutes:UInt32}) * {grain_minutes:UInt32} AS bucket_minute,
    max(concurrency)                                                AS peak_concurrency,
    avg(concurrency)                                                AS average_concurrency,
    count()                                                         AS minutes_in_bucket
FROM ${MARTS_DB}.v_occupancy_full(
    country = {country:String}, platform = {platform:String},
    video_type = {video_type:String}, category = {category:String},
    app_version = {app_version:String}, player_version = {player_version:String},
    audio_language = {audio_language:String},
    subtitle_language = {subtitle_language:String},
    content_id = {content_id:UInt64},
    minute_from = {minute_from:UInt32}, minute_to = {minute_to:UInt32})
GROUP BY bucket_minute
ORDER BY bucket_minute;

-- Discoverability, so a model exploring the schema learns the window instead of
-- assuming "now". This dataset is a fixed historical extract, so a query written
-- against the last hour or the last day matches nothing and looks like an empty
-- table rather than a wrong time range.
CREATE VIEW ${MARTS_DB}.v_data_window
(
    `min_minute`            UInt32          COMMENT 'Earliest minute with sessions, in minutes since the unix epoch. Pass this as minute_from.',
    `max_minute`            UInt32          COMMENT 'Latest minute with sessions, in minutes since the unix epoch. Pass this as minute_to.',
    `min_utc`               DateTime('UTC') COMMENT 'Earliest minute as a UTC timestamp.',
    `max_utc`               DateTime('UTC') COMMENT 'Latest minute as a UTC timestamp.',
    `span_days`             Float64         COMMENT 'Length of the window in days.',
    `minutes_with_sessions` UInt64          COMMENT 'Distinct minutes that carry at least one session.',
    `occupancy_rows`        UInt64          COMMENT 'Rows in the underlying per-minute occupancy table.'
)
DEFINER = ${CH_USER} SQL SECURITY DEFINER
COMMENT 'The time window this dataset actually covers. It is a fixed historical extract, not a live feed, so never assume now() is inside it. Read min_minute and max_minute straight into the minute_from and minute_to parameters of the concurrency views.'
AS
SELECT
    min(minute)                            AS min_minute,
    max(minute)                            AS max_minute,
    toDateTime(min(minute) * 60, 'UTC')    AS min_utc,
    toDateTime(max(minute) * 60, 'UTC')    AS max_utc,
    (max(minute) - min(minute)) / 1440.0   AS span_days,
    uniqExact(minute)                      AS minutes_with_sessions,
    count()                                AS occupancy_rows
FROM minute_occupancy;

-- Every value each string dim can take, so a filter is picked from the data rather
-- than guessed. Deliberately no peak column: a peak per value has to sum across the
-- other dims before taking the maximum (D6), and a GROUP BY here would take the
-- maximum first and publish a number that is quietly too small. Use v_concurrency
-- with the dim filtered, or the top_slices MCP tool, for peaks.
CREATE VIEW ${MARTS_DB}.v_dimension_values
(
    `dimension`       String COMMENT 'Which filter parameter this value belongs to.',
    `value`           String COMMENT 'A value the dimension actually takes. Matching folds case, so pass it in any case.',
    `minutes_present` UInt64 COMMENT 'Distinct minutes in which this value carries at least one session.',
    `first_minute`    UInt32 COMMENT 'First minute this value appears, in minutes since the unix epoch.',
    `last_minute`     UInt32 COMMENT 'Last minute this value appears, in minutes since the unix epoch.'
)
DEFINER = ${CH_USER} SQL SECURITY DEFINER
COMMENT 'Every accepted value of every filterable string dimension: country, platform, video_type, category, app_version, player_version, audio_language and subtitle_language. Pass one as the matching parameter; pass an empty string, or ALL, ANY, NONE, NULL, * or %, to leave that dimension unfiltered. content_id is numeric, 0 means unfiltered, and titles resolve through v_titles. This view carries no concurrency figure on purpose, because a peak per value has to be summed across the other dimensions before the maximum is taken.'
AS
SELECT * FROM (
    SELECT 'country'    AS dimension, toString(country)    AS value,
           uniqExact(minute) AS minutes_present, min(minute) AS first_minute,
           max(minute) AS last_minute
    FROM minute_occupancy GROUP BY country
    UNION ALL
    SELECT 'platform', toString(platform), uniqExact(minute), min(minute), max(minute)
    FROM minute_occupancy GROUP BY platform
    UNION ALL
    SELECT 'video_type', toString(video_type), uniqExact(minute), min(minute), max(minute)
    FROM minute_occupancy GROUP BY video_type
    UNION ALL
    SELECT 'category', toString(category), uniqExact(minute), min(minute), max(minute)
    FROM minute_occupancy GROUP BY category
    UNION ALL
    SELECT 'app_version', toString(app_version), uniqExact(minute), min(minute), max(minute)
    FROM minute_occupancy GROUP BY app_version
    UNION ALL
    SELECT 'player_version', toString(player_version), uniqExact(minute), min(minute), max(minute)
    FROM minute_occupancy GROUP BY player_version
    UNION ALL
    SELECT 'audio_language', toString(audio_language), uniqExact(minute), min(minute), max(minute)
    FROM minute_occupancy GROUP BY audio_language
    UNION ALL
    SELECT 'subtitle_language', toString(subtitle_language), uniqExact(minute), min(minute), max(minute)
    FROM minute_occupancy GROUP BY subtitle_language
)
ORDER BY dimension, minutes_present DESC, value;

-- Titles, so "how many watched X at its peak" resolves to a content_id instead of
-- quietly falling through to the unfiltered total. Only ids that carry sessions are
-- listed, so an empty match means the dataset does not hold that title, which is an
-- answer in itself.
CREATE VIEW ${MARTS_DB}.v_titles
(
    `content_id`      UInt64 COMMENT 'Pass this as the content_id parameter of the concurrency views.',
    `title`           String COMMENT 'Title from the content catalogue. Empty when the catalogue has no row for the id.',
    `video_type`      String COMMENT 'live or vod, as the catalogue records it.',
    `category`        String COMMENT 'Catalogue category.',
    `minutes_present` UInt64 COMMENT 'Distinct minutes in which this title carries at least one session.',
    `first_minute`    UInt32 COMMENT 'First minute this title appears, in minutes since the unix epoch.',
    `last_minute`     UInt32 COMMENT 'Last minute this title appears, in minutes since the unix epoch.'
)
DEFINER = ${CH_USER} SQL SECURITY DEFINER
COMMENT 'Every content_id that carries sessions, with its title. Match a title here first, case insensitively or by substring, then pass the content_id you found. If nothing matches, the dataset does not contain that title; say so rather than answering with the unfiltered total.'
AS
SELECT
    seen.content_id                       AS content_id,
    toString(catalogue.title)             AS title,
    toString(catalogue.video_type)        AS video_type,
    toString(catalogue.category)          AS category,
    seen.minutes_present                  AS minutes_present,
    seen.first_minute                     AS first_minute,
    seen.last_minute                      AS last_minute
FROM
(
    SELECT content_id, uniqExact(minute) AS minutes_present,
           min(minute) AS first_minute, max(minute) AS last_minute
    FROM minute_occupancy GROUP BY content_id
) AS seen
LEFT JOIN content_meta AS catalogue ON catalogue.content_id = seen.content_id
ORDER BY minutes_present DESC, content_id;

-- The thesis, as a mart rather than as prose. Naive counts a session as present from
-- its first event to its last, whatever the app was doing in between; foreground-only
-- counts the minutes the session was actually in the foreground. Same minutes, two
-- definitions, side by side, so the overcount can be shown live instead of quoted.
CREATE VIEW ${MARTS_DB}.v_naive_vs_foreground
(
    `minute`                  UInt32          COMMENT 'Minutes since the unix epoch.',
    `minute_utc`              DateTime('UTC') COMMENT 'The same minute as a UTC timestamp.',
    `foreground_concurrency`  UInt64          COMMENT 'Sessions in the foreground in that minute. This is the number the rest of the marts serve.',
    `naive_concurrency`       UInt64          COMMENT 'Sessions with the app open at all in that minute, background included.',
    `overcount`               Int64           COMMENT 'naive minus foreground, in sessions.'
)
DEFINER = ${CH_USER} SQL SECURITY DEFINER
COMMENT 'The foreground-only claim, minute by minute, against the naive any-open-session count it corrects. Peak of foreground_concurrency is the headline number; peak of naive_concurrency is what counting every open session would have reported, and the two peaks do not land in the same minute. Use v_overcount for the summary.'
AS
SELECT
    minute,
    toDateTime(minute * 60, 'UTC')                  AS minute_utc,
    maxIf(concurrency, series = 'foreground')       AS foreground_concurrency,
    maxIf(concurrency, series = 'naive')            AS naive_concurrency,
    toInt64(maxIf(concurrency, series = 'naive'))
        - toInt64(maxIf(concurrency, series = 'foreground')) AS overcount
FROM
(
    SELECT minute, toUInt64(sum(sessions)) AS concurrency, 'foreground' AS series
    FROM minute_occupancy
    GROUP BY minute
    UNION ALL
    SELECT minute, toUInt64(uniqExact(video_session_id)) AS concurrency, 'naive' AS series
    FROM
    (
        SELECT video_session_id,
               arrayJoin(range(toUInt32(ts_start_ms DIV 60000),
                               toUInt32((ts_end_ms - 1) DIV 60000) + 1)) AS minute
        FROM
        (
            SELECT video_session_id,
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

-- One row, so the headline claim answers in a single question.
CREATE VIEW ${MARTS_DB}.v_overcount
(
    `foreground_peak`            UInt64          COMMENT 'Peak foreground-only concurrency, the number this project publishes.',
    `foreground_peak_utc`        DateTime('UTC') COMMENT 'The minute the foreground-only peak lands in.',
    `naive_peak`                 UInt64          COMMENT 'Peak concurrency if every open session counted, background included.',
    `naive_peak_utc`             DateTime('UTC') COMMENT 'The minute the naive peak lands in. It is a different minute, which is the point.',
    `peak_overcount_pct`         Float64         COMMENT 'How much higher the naive peak is, as a percentage of the foreground peak.',
    `foreground_average`         Float64         COMMENT 'Mean foreground-only concurrency across minutes that carry sessions.',
    `naive_average`              Float64         COMMENT 'Mean naive concurrency across the same minutes.',
    `average_overcount_pct`      Float64         COMMENT 'How much higher the naive average is, as a percentage of the foreground average.'
)
DEFINER = ${CH_USER} SQL SECURITY DEFINER
COMMENT 'The overcount claim as one row: counting every open session rather than only foreground sessions inflates the peak and the average, and moves the peak into a different minute. Answers "how much would we overcount if we counted every open session" without any arithmetic on the caller side.'
AS
SELECT
    max(foreground_concurrency)                                          AS foreground_peak,
    toDateTime(argMax(minute, foreground_concurrency) * 60, 'UTC')       AS foreground_peak_utc,
    max(naive_concurrency)                                               AS naive_peak,
    toDateTime(argMax(minute, naive_concurrency) * 60, 'UTC')            AS naive_peak_utc,
    100 * (max(naive_concurrency) / max(foreground_concurrency) - 1)     AS peak_overcount_pct,
    avg(foreground_concurrency)                                          AS foreground_average,
    avg(naive_concurrency)                                               AS naive_average,
    100 * (avg(naive_concurrency) / avg(foreground_concurrency) - 1)     AS average_overcount_pct
FROM ${MARTS_DB}.v_naive_vs_foreground;

-- The MCP or dashboard surface. Everything upstream of marts is ungranted.
CREATE ROLE IF NOT EXISTS marts_readonly;
GRANT SELECT ON ${MARTS_DB}.* TO marts_readonly;

-- readonly=1 CONST: the agent cannot raise its own ceiling. max_rows_to_read fails a
-- raw-table scan fast instead of running it slowly; that is the guardrail, not a
-- suggestion.
CREATE SETTINGS PROFILE IF NOT EXISTS marts_budget SETTINGS
    readonly = 1 CONST,
    max_execution_time = 10 READONLY,
    max_memory_usage = 2000000000 READONLY,
    max_rows_to_read = 200000000 READONLY,
    max_result_rows = 100000 READONLY,
    max_threads = 4 READONLY
    TO marts_readonly;

CREATE USER IF NOT EXISTS marts_agent IDENTIFIED WITH sha256_password BY '${MARTS_PASSWORD}'
    DEFAULT ROLE marts_readonly
    SETTINGS PROFILE 'marts_budget';

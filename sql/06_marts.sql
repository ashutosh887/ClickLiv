DROP DATABASE IF EXISTS marts;
CREATE DATABASE marts;

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
-- SQL SECURITY DEFINER: marts_agent is granted SELECT on marts.* only, never on
-- clickliv.minute_occupancy directly. Without DEFINER a view runs with the invoker's
-- own privileges, and ClickHouse checks those against the underlying table, which
-- would force granting the raw serving tables too and defeat the whole point of a
-- marts surface. Verified: an invoker-rights view 403s for marts_agent even though
-- the view itself is granted; a DEFINER view does not.
CREATE VIEW marts.v_occupancy_minute
(
    `minute`      UInt32 COMMENT 'Minutes since the unix epoch. Multiply by 60 for a unix timestamp. Query marts.v_data_window for the range this dataset actually covers.',
    `concurrency` UInt64 COMMENT 'Foreground-only concurrent sessions in that minute, summed across every dimension left unfiltered.'
)
DEFINER = ${CH_USER} SQL SECURITY DEFINER
COMMENT 'Parameterized. Call as marts.v_occupancy_minute(country=..., platform=..., video_type=..., content_id=..., minute_from=..., minute_to=...); all six are required. For no filter pass an empty string, or ALL, ANY, NONE, NULL, * or % in any case, and pass content_id = 0. Valid values are in marts.v_dimension_values and the minute range is in marts.v_data_window.'
AS
SELECT minute, sum(sessions) AS concurrency
FROM minute_occupancy
WHERE country      = if(lower(trimBoth({country:String}))    IN ('', 'all', 'any', 'none', 'null', '*', '%'), country,    {country:String})
  AND platform     = if(lower(trimBoth({platform:String}))   IN ('', 'all', 'any', 'none', 'null', '*', '%'), platform,   {platform:String})
  AND video_type   = if(lower(trimBoth({video_type:String})) IN ('', 'all', 'any', 'none', 'null', '*', '%'), video_type, {video_type:String})
  AND content_id   = coalesce(nullIf({content_id:UInt64}, toUInt64(0)), content_id)
  AND minute BETWEEN {minute_from:UInt32} AND {minute_to:UInt32}
GROUP BY minute
ORDER BY minute;

-- Same filter contract, bucketed to any grain. peak is max(concurrency) inside the
-- bucket, average is the mean of the per-minute concurrency inside the bucket, and
-- the caller states which by picking a column, per D21.
CREATE VIEW marts.v_concurrency
(
    `bucket_minute`       UInt64  COMMENT 'Start of the bucket, in minutes since the unix epoch.',
    `peak_concurrency`    UInt64  COMMENT 'Highest per-minute concurrency inside the bucket. Order by this descending for the busiest bucket.',
    `average_concurrency` Float64 COMMENT 'Mean per-minute concurrency inside the bucket.',
    `minutes_in_bucket`   UInt64  COMMENT 'Minutes inside the bucket that carried at least one session.'
)
DEFINER = ${CH_USER} SQL SECURITY DEFINER
COMMENT 'Parameterized. Call as marts.v_concurrency(country=..., platform=..., video_type=..., content_id=..., minute_from=..., minute_to=..., grain_minutes=...); all seven are required. For no filter pass an empty string, or ALL, ANY, NONE, NULL, * or % in any case, and pass content_id = 0. grain_minutes is 1 for minute, 60 for hour, 1440 for day. For the busiest moment overall, take minute_from and minute_to from marts.v_data_window, grain_minutes = 1, and order by peak_concurrency descending.'
AS
SELECT
    intDiv(minute, {grain_minutes:UInt32}) * {grain_minutes:UInt32} AS bucket_minute,
    max(concurrency)                                                AS peak_concurrency,
    avg(concurrency)                                                AS average_concurrency,
    count()                                                         AS minutes_in_bucket
FROM marts.v_occupancy_minute(
    country = {country:String}, platform = {platform:String},
    video_type = {video_type:String}, content_id = {content_id:UInt64},
    minute_from = {minute_from:UInt32}, minute_to = {minute_to:UInt32})
GROUP BY bucket_minute
ORDER BY bucket_minute;

-- Discoverability, so a model exploring the schema learns the window instead of
-- assuming "now". This dataset is a fixed historical extract, so a query written
-- against the last hour or the last day matches nothing and looks like an empty
-- table rather than a wrong time range.
CREATE VIEW marts.v_data_window
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
COMMENT 'The time window this dataset actually covers. It is a fixed historical extract, not a live feed, so never assume now() is inside it. Read min_minute and max_minute straight into the minute_from and minute_to parameters of marts.v_occupancy_minute and marts.v_concurrency.'
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
-- maximum first and publish a number that is quietly too small. Use marts.v_concurrency
-- with the dim filtered, or the top_slices MCP tool, for peaks.
CREATE VIEW marts.v_dimension_values
(
    `dimension`       String COMMENT 'Which filter parameter this value belongs to: country, platform or video_type.',
    `value`           String COMMENT 'A value the dimension actually takes. Pass it verbatim, it is case sensitive.',
    `minutes_present` UInt64 COMMENT 'Distinct minutes in which this value carries at least one session.',
    `first_minute`    UInt32 COMMENT 'First minute this value appears, in minutes since the unix epoch.',
    `last_minute`     UInt32 COMMENT 'Last minute this value appears, in minutes since the unix epoch.'
)
DEFINER = ${CH_USER} SQL SECURITY DEFINER
COMMENT 'Every accepted value of every filterable string dimension. Pass one verbatim as country, platform or video_type; pass an empty string, or ALL, ANY, NONE, NULL, * or %, to leave that dimension unfiltered. content_id is numeric and 0 means unfiltered. This view carries no concurrency figure on purpose, because a peak per value has to be summed across the other dimensions before the maximum is taken.'
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
)
ORDER BY dimension, value;

-- The MCP or dashboard surface. Everything upstream of marts is ungranted.
CREATE ROLE IF NOT EXISTS marts_readonly;
GRANT SELECT ON marts.* TO marts_readonly;

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

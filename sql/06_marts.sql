DROP DATABASE IF EXISTS marts;
CREATE DATABASE marts;

-- Filter, then sum across whatever dims are left unfiltered, then bucket by minute.
-- Never group by a subset of dims: D6 holds only if summing happens before the max.
-- An empty string or zero sentinel means "no filter on this dim", via the coalesce/
-- nullIf idiom, verified against 26.7 in DOSSIER_REVIEW Part 2.
-- SQL SECURITY DEFINER: marts_agent is granted SELECT on marts.* only, never on
-- clickliv.minute_occupancy directly. Without DEFINER a view runs with the invoker's
-- own privileges, and ClickHouse checks those against the underlying table, which
-- would force granting the raw serving tables too and defeat the whole point of a
-- marts surface. Verified: an invoker-rights view 403s for marts_agent even though
-- the view itself is granted; a DEFINER view does not.
CREATE VIEW marts.v_occupancy_minute
DEFINER = ${CH_USER} SQL SECURITY DEFINER
AS
SELECT minute, sum(sessions) AS concurrency
FROM minute_occupancy
WHERE country      = coalesce(nullIf({country:String}, ''), country)
  AND platform      = coalesce(nullIf({platform:String}, ''), platform)
  AND video_type    = coalesce(nullIf({video_type:String}, ''), video_type)
  AND content_id    = coalesce(nullIf({content_id:UInt64}, toUInt64(0)), content_id)
  AND minute BETWEEN {minute_from:UInt32} AND {minute_to:UInt32}
GROUP BY minute
ORDER BY minute;

-- Same filter contract, bucketed to any grain. peak is max(concurrency) inside the
-- bucket, average is the mean of the per-minute concurrency inside the bucket, and
-- the caller states which by picking a column, per D21.
CREATE VIEW marts.v_concurrency
DEFINER = ${CH_USER} SQL SECURITY DEFINER
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

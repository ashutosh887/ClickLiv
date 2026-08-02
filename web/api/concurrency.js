import {
  activeFilters, bind, callSql, config, dimensionsOf, failure, query, resolve, send, signature,
} from './_clickhouse.js';

const VIEW = 'v_concurrency_full';
const GRAINS = { minute: 1, hour: 60, day: 1440 };

const SQL = (call) => `
SELECT bucket_minute,
       toDateTime(bucket_minute * 60, 'UTC') AS bucket_start,
       peak_concurrency,
       round(average_concurrency, 2) AS average_concurrency,
       minutes_in_bucket
FROM ${call}
ORDER BY bucket_minute`;

const NAIVE = (schema) => `
SELECT intDiv(minute, {grain_minutes:UInt32}) * {grain_minutes:UInt32} AS bucket_minute,
       max(naive_concurrency) AS naive_concurrency
FROM ${schema}.v_naive_vs_foreground
WHERE foreground_concurrency > 0
  AND minute BETWEEN {minute_from:UInt32} AND {minute_to:UInt32}
GROUP BY bucket_minute
ORDER BY bucket_minute`;

async function naiveSeries(schema, bound) {
  try {
    const { grain_minutes, minute_from, minute_to } = bound;
    const result = await query(NAIVE(schema), { grain_minutes, minute_from, minute_to }, schema);
    const rows = (result.data || []).map(([bucket, naive]) => [Number(bucket), Number(naive)]);
    if (!rows.length) return null;
    return {
      rows,
      peak: rows.reduce((most, [, naive]) => Math.max(most, naive), 0),
      rows_read: result.statistics?.rows_read ?? null,
      served_by: `${schema}.v_naive_vs_foreground`,
    };
  } catch {
    return null;
  }
}

export default async function handler(req, res) {
  const grainName = String(req.query.grain || 'hour');
  if (!(grainName in GRAINS)) {
    return send(res, 400, { error: `grain must be one of ${Object.keys(GRAINS).join(', ')}` }, 0);
  }

  try {
    const { dataset, schema, datasets, unknown } = await resolve(req.query.dataset);
    if (unknown) {
      return send(res, 400, { error: `unknown dataset, available: ${datasets.join(', ')}` }, 0);
    }
    const names = await signature(schema, VIEW);
    const grain = GRAINS[grainName];
    const bound = bind(names, req.query, { grain_minutes: grain });
    const filters = activeFilters(names, bound);
    const call = callSql(schema, VIEW, names);
    const [result, naive] = await Promise.all([
      query(SQL(call), bound, schema),
      Object.keys(filters).length ? Promise.resolve(null) : naiveSeries(schema, bound),
    ]);
    const rows = result.data.map(([bucket, start, peak, average, minutes]) => ({
      bucket_minute: bucket,
      bucket_start: start,
      peak_concurrency: peak,
      average_concurrency: average,
      minutes_in_bucket: minutes,
    }));
    return send(res, 200, {
      dataset,
      schema,
      grain: grainName,
      dimensions: dimensionsOf(names),
      filters,
      peak: rows.reduce((most, row) => Math.max(most, row.peak_concurrency), 0),
      rows,
      naive,
      statistics: result.statistics,
      sql: SQL(call).trim(),
      parameters: bound,
      served_by: `${schema}.${VIEW} as ${config().user}, readonly with a query budget`,
    });
  } catch (error) {
    if (error?.client) return send(res, 400, { error: error.message }, 0);
    return send(res, 502, { error: failure(error) }, 0);
  }
}

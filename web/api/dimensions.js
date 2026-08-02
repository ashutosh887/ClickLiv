import {
  activeFilters, bind, callSql, dimensionsOf, failure, query, resolve, send, signature,
} from './_clickhouse.js';

const VIEW = 'v_concurrency_full';
const MAX_VALUES = 400;
const MAX_TITLES = 2000;
const CROSSOVER_PREFERRED = ['platform', 'video_type'];
const CROSSOVER_MAX_VALUES = 12;
const CROSSOVER_MAX_SLICES = 20;

const VALUES = (schema) => `
SELECT dimension, value, minutes_present
FROM ${schema}.v_dimension_values
WHERE value != ''
ORDER BY dimension, minutes_present DESC, value
LIMIT {limit:UInt32} BY dimension`;

const TITLES = (schema) => `
SELECT content_id, title
FROM ${schema}.v_titles
WHERE title != ''
ORDER BY minutes_present DESC, content_id
LIMIT {limit:UInt32}`;

const PEAK = (call) => `
SELECT peak_concurrency, toDateTime(bucket_minute * 60, 'UTC') AS peak_at, bucket_minute
FROM ${call}
ORDER BY peak_concurrency DESC, bucket_minute ASC
LIMIT 1`;

const HEADLINE = (schema) => `
SELECT foreground_peak, naive_peak, peak_overcount_pct, average_overcount_pct
FROM ${schema}.v_overcount`;

const WINDOW = (schema) => `
SELECT min_minute, max_minute, span_days FROM ${schema}.v_data_window`;

const DAY = 1440;
const SPREAD_DAYS = 2;

async function peakFor(schema, names, bound) {
  const result = await query(PEAK(callSql(schema, VIEW, names)), bound, schema);
  const [peak, peakAt, minute] = result.data?.[0] || [];
  return {
    peak: peak === null || peak === undefined ? 0 : Number(peak),
    peak_at: peakAt ?? null,
    peak_minute: minute === null || minute === undefined ? null : Number(minute),
    rows_read: Number(result.statistics?.rows_read ?? 0),
  };
}

function windowsFor(windowRow, peakMinute) {
  const [minMinute, maxMinute, spanDays] = windowRow;
  const full = {
    key: 'full', label: 'Full window',
    minute_from: Number(minMinute ?? 0), minute_to: Number(maxMinute ?? 4294967295),
  };
  if (peakMinute === null || Number(spanDays ?? 0) <= SPREAD_DAYS) {
    return { windows: [full], default_window: full.key };
  }
  const start = Math.floor(peakMinute / DAY) * DAY;
  const busiest = {
    key: 'peak_day', label: 'Busiest day',
    minute_from: start, minute_to: start + DAY - 1,
  };
  return { windows: [busiest, full], default_window: busiest.key };
}

function crossoverDimensions(filterable, values) {
  const sized = filterable.filter((name) => {
    const count = (values[name] || []).length;
    return count >= 2 && count <= CROSSOVER_MAX_VALUES;
  });
  const preferred = CROSSOVER_PREFERRED.filter((name) => sized.includes(name));
  return (preferred.length ? preferred : sized).slice(0, 2);
}

export default async function handler(req, res) {
  try {
    const { dataset, schema, datasets, unknown } = await resolve(req.query.dataset);
    if (unknown) {
      return send(res, 400, { error: `unknown dataset, available: ${datasets.join(', ')}` }, 0);
    }
    const names = await signature(schema, VIEW);
    const bound = bind(names, req.query, { grain_minutes: 1 });
    const filters = activeFilters(names, bound);

    const [valueRows, titleRows, windowRows] = await Promise.all([
      query(VALUES(schema), { limit: MAX_VALUES }, schema),
      query(TITLES(schema), { limit: MAX_TITLES }, schema),
      query(WINDOW(schema), {}, schema),
    ]);
    const values = {};
    for (const [dimension, value, minutes] of valueRows.data || []) {
      (values[dimension] ||= []).push({ value: String(value), minutes_present: Number(minutes) });
    }
    const filterable = dimensionsOf(names).filter((name) => (values[name] || []).length);

    const chosen = crossoverDimensions(filterable, values);
    const jobs = [];
    for (const dimension of chosen) {
      for (const { value } of (values[dimension] || []).slice(0, CROSSOVER_MAX_VALUES)) {
        if (jobs.length >= CROSSOVER_MAX_SLICES) break;
        jobs.push({ dimension, value });
      }
    }
    const [headline, overall, ...peaks] = await Promise.all([
      query(HEADLINE(schema), {}, schema),
      peakFor(schema, names, bound),
      ...jobs.map((job) => peakFor(schema, names, { ...bound, [job.dimension]: job.value })),
    ]);

    const crossover = {};
    jobs.forEach((job, index) => {
      const found = peaks[index];
      if (found.peak > 0) (crossover[job.dimension] ||= []).push({ name: job.value, ...found });
    });
    for (const rows of Object.values(crossover)) rows.sort((a, b) => b.peak - a.peak);

    const row = headline.data?.[0] || [];
    const readRows = [overall, ...peaks].reduce((total, one) => total + one.rows_read, 0)
      + Number(headline.statistics?.rows_read || 0)
      + Number(valueRows.statistics?.rows_read || 0)
      + Number(titleRows.statistics?.rows_read || 0);

    return send(res, 200, {
      dataset,
      datasets,
      schema,
      dimensions: dimensionsOf(names),
      filterable,
      filters,
      ...windowsFor(windowRows.data?.[0] || [], overall.peak_minute),
      values,
      titles: (titleRows.data || []).map(([id, title]) => ({
        content_id: String(id), title: String(title),
      })),
      crossover,
      overall_peak: overall.peak,
      overall_peak_at: overall.peak_at,
      headline: {
        foreground_peak: Number(row[0] ?? 0),
        naive_peak: Number(row[1] ?? 0),
        peak_overcount_pct: Number(row[2] ?? 0),
        average_overcount_pct: Number(row[3] ?? 0),
      },
      rows_read: readRows,
      served_by: `${schema}.${VIEW} at minute grain, one call per slice`,
    }, 60);
  } catch (error) {
    if (error?.client) return send(res, 400, { error: error.message }, 0);
    return send(res, 502, { error: failure(error) }, 0);
  }
}

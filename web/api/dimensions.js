import { query, resolve, send } from './_clickhouse.js';

const MAX_VALUES = 12;

const VALUES = (schema) => `
SELECT dimension, value
FROM ${schema}.v_dimension_values
WHERE dimension = {dimension:String} AND value != ''
ORDER BY minutes_present DESC
LIMIT {limit:UInt32}`;

const PEAK = (schema) => `
SELECT max(peak_concurrency)
FROM ${schema}.v_concurrency(
    grain_minutes = 1440, country = '', platform = {platform:String},
    video_type = {video_type:String}, content_id = 0,
    minute_from = 0, minute_to = 4294967295)`;

const HEADLINE = (schema) => `
SELECT foreground_peak, naive_peak, peak_overcount_pct, average_overcount_pct
FROM ${schema}.v_overcount`;

async function valuesFor(schema, dimension) {
  const result = await query(VALUES(schema), { dimension, limit: MAX_VALUES }, schema);
  return (result.data || []).map((row) => String(row[1]));
}

async function peakFor(schema, platform, videoType) {
  const result = await query(PEAK(schema), { platform, video_type: videoType }, schema);
  const value = result.data?.[0]?.[0];
  return value === null || value === undefined ? 0 : Number(value);
}

async function withPeaks(schema, names, build) {
  const rows = await Promise.all(
    names.map(async (name) => ({ name, peak: await peakFor(schema, ...build(name)) })));
  return rows.filter((row) => row.peak > 0).sort((a, b) => b.peak - a.peak);
}

export default async function handler(req, res) {
  try {
    const { dataset, schema, datasets, unknown } = await resolve(req.query.dataset);
    if (unknown) {
      return send(res, 400, { error: `unknown dataset, available: ${datasets.join(', ')}` }, 0);
    }
    const [platformNames, videoTypeNames] = await Promise.all([
      valuesFor(schema, 'platform'), valuesFor(schema, 'video_type')]);
    const headline = await query(HEADLINE(schema), {}, schema);
    const row = headline.data?.[0] || [];
    return send(res, 200, {
      dataset,
      datasets,
      overall_peak: await peakFor(schema, '', ''),
      platforms: await withPeaks(schema, platformNames, (name) => [name, '']),
      video_types: await withPeaks(schema, videoTypeNames, (name) => ['', name]),
      headline: {
        foreground_peak: Number(row[0] ?? 0),
        naive_peak: Number(row[1] ?? 0),
        peak_overcount_pct: Number(row[2] ?? 0),
        average_overcount_pct: Number(row[3] ?? 0),
        server_ms: Math.round((headline.statistics?.elapsed ?? 0) * 1000),
      },
    }, 3600);
  } catch (error) {
    return send(res, 502, { error: String(error.message || error) }, 0);
  }
}

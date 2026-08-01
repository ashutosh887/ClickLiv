import { query, send } from './_clickhouse.js';

const MAX_VALUES = 12;

const VALUES = `
SELECT dimension, value
FROM marts.v_dimension_values
WHERE dimension = {dimension:String} AND value != ''
ORDER BY minutes_present DESC
LIMIT {limit:UInt32}`;

const PEAK = `
SELECT max(peak_concurrency)
FROM marts.v_concurrency(
    grain_minutes = 1440, country = '', platform = {platform:String},
    video_type = {video_type:String}, content_id = 0,
    minute_from = 0, minute_to = 4294967295)`;

const HEADLINE = `
SELECT foreground_peak, naive_peak, peak_overcount_pct, average_overcount_pct
FROM marts.v_overcount`;

async function valuesFor(dimension) {
  const result = await query(VALUES, { dimension, limit: MAX_VALUES });
  return (result.data || []).map((row) => String(row[1]));
}

async function peakFor(platform, videoType) {
  const result = await query(PEAK, { platform, video_type: videoType });
  const value = result.data?.[0]?.[0];
  return value === null || value === undefined ? 0 : Number(value);
}

async function withPeaks(names, build) {
  const rows = await Promise.all(
    names.map(async (name) => ({ name, peak: await peakFor(...build(name)) })));
  return rows.filter((row) => row.peak > 0).sort((a, b) => b.peak - a.peak);
}

export default async function handler(req, res) {
  try {
    const [platformNames, videoTypeNames] = await Promise.all([
      valuesFor('platform'), valuesFor('video_type')]);
    const headline = await query(HEADLINE);
    const row = headline.data?.[0] || [];
    return send(res, 200, {
      overall_peak: await peakFor('', ''),
      platforms: await withPeaks(platformNames, (name) => [name, '']),
      video_types: await withPeaks(videoTypeNames, (name) => ['', name]),
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

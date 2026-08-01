import { config, query, resolve, send } from './_clickhouse.js';

const GRAINS = { minute: 1, hour: 60, day: 1440 };

const SQL = (schema) => `
SELECT bucket_minute,
       toDateTime(bucket_minute * 60, 'UTC') AS bucket_start,
       peak_concurrency,
       round(average_concurrency, 2) AS average_concurrency,
       minutes_in_bucket
FROM ${schema}.v_concurrency(
    grain_minutes = {grain:UInt32}, country = '', platform = {platform:String},
    video_type = {video_type:String}, content_id = 0,
    minute_from = 0, minute_to = 4294967295)
ORDER BY bucket_minute`;

export default async function handler(req, res) {
  const grainName = String(req.query.grain || 'hour');
  if (!(grainName in GRAINS)) {
    return send(res, 400, { error: `grain must be one of ${Object.keys(GRAINS).join(', ')}` }, 0);
  }
  const platform = String(req.query.platform || '');
  const videoType = String(req.query.video_type || '');
  if (platform.length > 40 || videoType.length > 40) {
    return send(res, 400, { error: 'filter value too long' }, 0);
  }

  try {
    const { dataset, schema, datasets, unknown } = await resolve(req.query.dataset);
    if (unknown) {
      return send(res, 400, { error: `unknown dataset, available: ${datasets.join(', ')}` }, 0);
    }
    const result = await query(SQL(schema), {
      grain: GRAINS[grainName],
      platform,
      video_type: videoType,
    }, schema);
    const rows = result.data.map(([bucket, start, peak, average, minutes]) => ({
      bucket_minute: bucket,
      bucket_start: start,
      peak_concurrency: peak,
      average_concurrency: average,
      minutes_in_bucket: minutes,
    }));
    return send(res, 200, {
      dataset,
      grain: grainName,
      filters: { platform, video_type: videoType },
      peak: rows.reduce((most, row) => Math.max(most, row.peak_concurrency), 0),
      rows,
      statistics: result.statistics,
      served_by: `${schema}.v_concurrency as ${config().user}, readonly with a query budget`,
    });
  } catch (error) {
    return send(res, 502, { error: String(error.message || error) }, 0);
  }
}

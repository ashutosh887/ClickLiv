import { config, failure, query, resolve, send } from './_clickhouse.js';

const OVERCOUNT = (schema) => `
SELECT foreground_peak, foreground_peak_utc, naive_peak, naive_peak_utc,
       peak_overcount_pct, foreground_average, naive_average, average_overcount_pct
FROM ${schema}.v_overcount`;

const WINDOW = (schema) => `
SELECT min_utc, max_utc, round(span_days, 2), minutes_with_sessions, occupancy_rows,
       dense_min_utc, dense_max_utc, round(dense_span_days, 2), dense_days,
       outlier_minutes, outlier_rows
FROM ${schema}.v_data_window`;

export default async function handler(req, res) {
  try {
    const { dataset, schema, datasets, unknown } = await resolve(req.query.dataset);
    if (unknown) {
      return send(res, 400, { error: `unknown dataset, available: ${datasets.join(', ')}` }, 0);
    }
    const overcount = await query(OVERCOUNT(schema), {}, schema);
    const [
      foregroundPeak, foregroundPeakUtc, naivePeak, naivePeakUtc,
      peakOvercount, foregroundAverage, naiveAverage, averageOvercount,
    ] = overcount.data?.[0] || [];
    const windowed = await query(WINDOW(schema), {}, schema);
    const [
      from, to, span, minutes, rows,
      denseFrom, denseTo, denseSpan, denseDays, outlierMinutes, outlierRows,
    ] = windowed.data?.[0] || [];
    return send(res, 200, {
      dataset,
      datasets,
      schema,
      foreground_peak: Number(foregroundPeak ?? 0),
      foreground_peak_utc: foregroundPeakUtc ?? null,
      naive_peak: Number(naivePeak ?? 0),
      naive_peak_utc: naivePeakUtc ?? null,
      peak_overcount_pct: Number(peakOvercount ?? 0),
      foreground_average: Number(foregroundAverage ?? 0),
      naive_average: Number(naiveAverage ?? 0),
      average_overcount_pct: Number(averageOvercount ?? 0),
      window_from: from ?? null,
      window_to: to ?? null,
      span_days: span ?? null,
      minutes_with_sessions: Number(minutes ?? 0),
      occupancy_rows: Number(rows ?? 0),
      dense_window_from: denseFrom ?? null,
      dense_window_to: denseTo ?? null,
      dense_span_days: denseSpan ?? null,
      dense_days: Number(denseDays ?? 0),
      outlier_minutes: Number(outlierMinutes ?? 0),
      outlier_rows: Number(outlierRows ?? 0),
      server_ms: Math.round((overcount.statistics?.elapsed ?? 0) * 1000),
      served_by: `${schema}.v_overcount as ${config().user}, readonly with a query budget`,
    }, 60);
  } catch (error) {
    return send(res, 502, { error: failure(error) }, 0);
  }
}

import { datasets, failure, schemaFor, send, query } from './_clickhouse.js';

const WINDOW = `
SELECT min_utc, max_utc, round(span_days, 2) AS span_days,
       minutes_with_sessions, occupancy_rows,
       dense_min_utc, dense_max_utc, round(dense_span_days, 2) AS dense_span_days,
       dense_days, outlier_minutes, outlier_rows
FROM {schema}.v_data_window`;

async function describe(dataset) {
  const schema = schemaFor(dataset);
  try {
    const result = await query(WINDOW.replace('{schema}', schema), {}, schema);
    const [
      from, to, span, minutes, rows,
      denseFrom, denseTo, denseSpan, denseDays, outlierMinutes, outlierRows,
    ] = result.data?.[0] || [];
    return {
      name: dataset,
      schema,
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
    };
  } catch {
    return { name: dataset, schema, window_from: null, window_to: null, span_days: null };
  }
}

function fingerprint(d) {
  return [d.window_from, d.window_to, d.minutes_with_sessions, d.occupancy_rows].join('|');
}

export default async function handler(req, res) {
  try {
    const names = await datasets();
    const described = await Promise.all(names.map(describe));
    const primary = described[0];
    const distinct = described.filter(
      (d, i) => i === 0 || fingerprint(d) !== fingerprint(primary));
    return send(res, 200, {
      default: primary.name,
      datasets: distinct,
    }, 60);
  } catch (error) {
    return send(res, 502, { error: failure(error) }, 0);
  }
}

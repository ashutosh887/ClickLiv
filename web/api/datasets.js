import { datasets, schemaFor, send, query } from './_clickhouse.js';

const WINDOW = `
SELECT min_utc, max_utc, round(span_days, 2) AS span_days,
       minutes_with_sessions, occupancy_rows
FROM {schema}.v_data_window`;

async function describe(dataset) {
  const schema = schemaFor(dataset);
  try {
    const result = await query(WINDOW.replace('{schema}', schema), {}, schema);
    const [from, to, span, minutes, rows] = result.data?.[0] || [];
    return {
      name: dataset,
      schema,
      window_from: from ?? null,
      window_to: to ?? null,
      span_days: span ?? null,
      minutes_with_sessions: Number(minutes ?? 0),
      occupancy_rows: Number(rows ?? 0),
    };
  } catch {
    return { name: dataset, schema, window_from: null, window_to: null, span_days: null };
  }
}

export default async function handler(req, res) {
  try {
    const names = await datasets();
    return send(res, 200, {
      default: names[0],
      datasets: await Promise.all(names.map(describe)),
    }, 60);
  } catch (error) {
    return send(res, 502, { error: String(error.message || error) }, 0);
  }
}

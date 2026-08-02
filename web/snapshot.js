const BASE = '/snapshot';
const UNFILTERED = new Set(['', 'all', 'any', 'none', 'null', '*', '%']);
const GRAINS = { minute: 1, hour: 60, day: 1440 };
const CROSSOVER_PREFERRED = ['platform', 'video_type'];
const CROSSOVER_MAX_VALUES = 12;
const CROSSOVER_MAX_SLICES = 20;
const MAX_VALUES = 2500;
const MAX_TITLES = 2000;
const DAY = 1440;
const SPREAD_DAYS = 2;
const MINUTE_CEILING = 4294967295;

let loaded = null;
let described = null;

function stamp(minute) {
  return new Date(minute * 60000).toISOString().replace('T', ' ').slice(0, 19);
}

async function unpack(meta) {
  const response = await fetch(`${BASE}/${meta.rollup.file}`);
  if (!response.ok) throw new Error(`snapshot rollup missing, status ${response.status}`);
  const body = meta.rollup.encoding === 'gzip'
    ? response.body.pipeThrough(new DecompressionStream('gzip'))
    : response.body;
  const buffer = await new Response(body).arrayBuffer();
  const rows = meta.rollup.rows;
  const want = meta.rollup.columns.length * rows * 2;
  if (buffer.byteLength !== want) {
    throw new Error(`snapshot rollup is ${buffer.byteLength} bytes, expected ${want}`);
  }
  const columns = {};
  meta.rollup.columns.forEach((name, at) => {
    columns[name] = new Uint16Array(buffer, at * rows * 2, rows);
  });
  return columns;
}

function rowIndex(columns, minuteCount, rows) {
  const starts = new Int32Array(minuteCount + 1).fill(rows);
  starts[0] = 0;
  let seen = 0;
  for (let i = 0; i < rows; i++) {
    const minute = columns.minute[i];
    while (seen < minute) starts[++seen] = i;
  }
  while (seen < minuteCount) starts[++seen] = rows;
  return starts;
}

export async function metadata() {
  if (!described) {
    described = (async () => {
      const response = await fetch(`${BASE}/meta.json`);
      if (!response.ok) throw new Error(`snapshot metadata missing, status ${response.status}`);
      return response.json();
    })();
  }
  return described;
}

export async function headline() {
  const meta = await metadata();
  return {
    dataset: meta.source.database,
    schema: meta.source.schema,
    captured_utc: meta.captured_utc,
    ...meta.headline,
    ...meta.window,
  };
}

export async function snapshot() {
  if (loaded) return loaded;
  const meta = await metadata();
  const columns = await unpack(meta);
  const minutes = meta.rollup.minutes;
  const lookup = {};
  for (const [name, entry] of Object.entries(meta.catalogue)) {
    lookup[name] = new Map(entry.values.map((value, at) => [value, at]));
  }
  loaded = {
    meta, columns, minutes, lookup,
    rows: meta.rollup.rows,
    starts: rowIndex(columns, minutes.length, meta.rollup.rows),
  };
  return loaded;
}

function codeFor(held, name, wanted) {
  const entry = held.meta.catalogue[name];
  const exact = held.lookup[name].get(wanted);
  if (exact !== undefined) return exact;
  const folded = wanted.toLowerCase();
  const matches = [];
  entry.values.forEach((value, at) => {
    if (value.toLowerCase() === folded) matches.push(at);
  });
  return matches.length === 1 ? matches[0] : -1;
}

function plan(held, wanted) {
  const active = [];
  const filters = {};
  for (const name of held.meta.dimensions) {
    const given = String(wanted[name] ?? '').trim();
    if (UNFILTERED.has(given.toLowerCase())) continue;
    filters[name] = given;
    active.push([name, codeFor(held, name, given)]);
  }
  const content = String(wanted.content_id ?? '').trim();
  if (content && content !== '0') {
    filters.content_id = content;
    active.push(['content_id', codeFor(held, 'content_id', content)]);
  }
  const from = Number(wanted.minute_from ?? 0) || 0;
  const to = Number(wanted.minute_to ?? MINUTE_CEILING) || MINUTE_CEILING;
  let first = held.minutes.findIndex((minute) => minute >= from);
  if (first === -1) first = held.minutes.length;
  let last = held.minutes.length - 1;
  while (last >= first && held.minutes[last] > to) last--;
  return { active, filters, first, last, empty: active.some(([, code]) => code < 0) };
}

function series(held, shape) {
  const { active, first, last, empty } = shape;
  const totals = new Int32Array(held.minutes.length);
  if (empty || first > last) return { totals, scanned: 0 };
  const from = held.starts[first];
  const to = held.starts[last + 1];
  const sessions = held.columns.sessions;
  const minute = held.columns.minute;
  const lanes = active.map(([name, code]) => [held.columns[name], code]);
  for (let i = from; i < to; i++) {
    let keep = true;
    for (let at = 0; at < lanes.length; at++) {
      if (lanes[at][0][i] !== lanes[at][1]) { keep = false; break; }
    }
    if (keep) totals[minute[i]] += sessions[i];
  }
  return { totals, scanned: to - from };
}

function bucket(held, totals, shape, grain) {
  const rows = [];
  let current = -1;
  for (let at = shape.first; at <= shape.last; at++) {
    const value = totals[at];
    if (!value) continue;
    const minute = held.minutes[at];
    const start = Math.floor(minute / grain) * grain;
    if (!rows.length || rows[current].bucket_minute !== start) {
      rows.push({
        bucket_minute: start, bucket_start: stamp(start),
        peak_concurrency: value, sum: value, minutes_in_bucket: 1,
      });
      current = rows.length - 1;
      continue;
    }
    const row = rows[current];
    row.sum += value;
    row.minutes_in_bucket += 1;
    if (value > row.peak_concurrency) row.peak_concurrency = value;
  }
  for (const row of rows) {
    row.average_concurrency = Math.round((row.sum / row.minutes_in_bucket) * 100) / 100;
    delete row.sum;
  }
  return rows;
}

function peakOf(held, totals, shape) {
  let peak = 0;
  let at = -1;
  for (let i = shape.first; i <= shape.last; i++) {
    if (totals[i] > peak) { peak = totals[i]; at = i; }
  }
  return {
    peak,
    peak_at: at < 0 ? null : stamp(held.minutes[at]),
    peak_minute: at < 0 ? null : held.minutes[at],
  };
}

function naiveFor(held, shape, grain) {
  const rows = [];
  let peak = 0;
  for (let at = shape.first; at <= shape.last; at++) {
    const value = held.meta.naive[at] || 0;
    if (!value) continue;
    const start = Math.floor(held.minutes[at] / grain) * grain;
    const last = rows.length - 1;
    if (last >= 0 && rows[last][0] === start) {
      if (value > rows[last][1]) rows[last][1] = value;
    } else {
      rows.push([start, value]);
    }
    if (value > peak) peak = value;
  }
  if (!rows.length) return null;
  return { rows, peak, rows_read: rows.length, served_by: 'snapshot of marts.v_naive_vs_foreground' };
}

function callSql(schema, view, dimensions, bound) {
  const args = [...dimensions, 'content_id', 'minute_from', 'minute_to', 'grain_minutes']
    .map((name) => {
      const value = bound[name];
      const quoted = typeof value === 'string' ? `'${value}'` : value;
      return `    ${name} = ${quoted}`;
    });
  return `SELECT bucket_minute,
       toDateTime(bucket_minute * 60, 'UTC') AS bucket_start,
       peak_concurrency,
       round(average_concurrency, 2) AS average_concurrency,
       minutes_in_bucket
FROM ${schema}.${view}(
${args.join(',\n')})
ORDER BY bucket_minute`;
}

function bindingOf(held, shape, grain) {
  const bound = {};
  for (const name of held.meta.dimensions) bound[name] = shape.filters[name] ?? '';
  bound.content_id = Number(shape.filters.content_id ?? 0);
  bound.minute_from = held.minutes[shape.first] ?? 0;
  bound.minute_to = held.minutes[shape.last] ?? 0;
  bound.grain_minutes = grain;
  return bound;
}

function windowsFor(held, peakMinute) {
  const span = held.meta.window;
  const full = {
    key: 'full', label: 'Full window',
    minute_from: span.min_minute, minute_to: span.max_minute,
  };
  if (peakMinute === null || Number(span.span_days) <= SPREAD_DAYS) {
    return { windows: [full], default_window: full.key };
  }
  const start = Math.floor(peakMinute / DAY) * DAY;
  const busiest = {
    key: 'peak_day', label: 'Busiest day', minute_from: start, minute_to: start + DAY - 1,
  };
  return { windows: [busiest, full], default_window: busiest.key };
}

export async function datasets() {
  const held = await snapshot();
  const span = held.meta.window;
  return {
    default: held.meta.source.database,
    datasets: [{ name: held.meta.source.database, schema: held.meta.source.schema, ...span }],
  };
}

export async function dimensions(wanted) {
  const held = await snapshot();
  const started = performance.now();
  const shape = plan(held, wanted);
  const overall = series(held, shape);
  const top = peakOf(held, overall.totals, shape);
  let scanned = overall.scanned;

  const values = {};
  const totals = {};
  for (const name of held.meta.dimensions) {
    const entry = held.meta.catalogue[name];
    const rows = [];
    entry.values.forEach((value, at) => {
      if (value !== '') rows.push({ value, minutes_present: entry.minutes_present[at] });
    });
    totals[name] = rows.length;
    values[name] = rows.slice(0, MAX_VALUES);
  }
  const filterable = held.meta.dimensions.filter((name) => values[name].length);

  const sized = filterable.filter((name) => {
    const count = values[name].length;
    return count >= 2 && count <= CROSSOVER_MAX_VALUES;
  });
  const preferred = CROSSOVER_PREFERRED.filter((name) => sized.includes(name));
  const chosen = (preferred.length ? preferred : sized).slice(0, 2);

  const crossover = {};
  let slices = 0;
  for (const name of chosen) {
    for (const { value } of values[name].slice(0, CROSSOVER_MAX_VALUES)) {
      if (slices >= CROSSOVER_MAX_SLICES) break;
      slices += 1;
      const inner = plan(held, { ...wanted, [name]: value });
      const found = series(held, inner);
      scanned += found.scanned;
      const at = peakOf(held, found.totals, inner);
      if (at.peak > 0) (crossover[name] ||= []).push({ name: value, ...at });
    }
  }
  for (const rows of Object.values(crossover)) rows.sort((a, b) => b.peak - a.peak);

  const catalogue = held.meta.catalogue.content_id;
  const titled = [];
  catalogue.values.forEach((value, at) => {
    const title = catalogue.titles[at];
    if (title) titled.push({ content_id: value, title });
  });

  return {
    dataset: held.meta.source.database,
    datasets: [held.meta.source.database],
    schema: held.meta.source.schema,
    dimensions: held.meta.dimensions,
    filterable,
    filters: shape.filters,
    ...windowsFor(held, top.peak_minute),
    values,
    totals,
    titles: titled.slice(0, MAX_TITLES),
    titles_total: titled.length,
    crossover,
    overall_peak: top.peak,
    overall_peak_at: top.peak_at,
    headline: held.meta.headline,
    rows_read: scanned,
    elapsed: (performance.now() - started) / 1000,
    served_by: `snapshot of ${held.meta.source.schema}.v_concurrency_full at minute grain`,
  };
}

export async function concurrency(wanted) {
  const held = await snapshot();
  const grainName = String(wanted.grain || 'hour');
  if (!(grainName in GRAINS)) throw new Error(`grain must be one of ${Object.keys(GRAINS)}`);
  const grain = GRAINS[grainName];
  const started = performance.now();
  const shape = plan(held, wanted);
  const found = series(held, shape);
  const rows = bucket(held, found.totals, shape, grain);
  const naive = Object.keys(shape.filters).length ? null : naiveFor(held, shape, grain);
  const bound = bindingOf(held, shape, grain);
  return {
    dataset: held.meta.source.database,
    schema: held.meta.source.schema,
    grain: grainName,
    dimensions: held.meta.dimensions,
    filters: shape.filters,
    peak: rows.reduce((most, row) => Math.max(most, row.peak_concurrency), 0),
    rows,
    naive,
    statistics: {
      elapsed: (performance.now() - started) / 1000,
      rows_read: found.scanned,
    },
    sql: callSql(held.meta.source.schema, 'v_concurrency_full', held.meta.dimensions, bound),
    parameters: bound,
    served_by: `snapshot of ${held.meta.source.schema}.v_concurrency_full, computed in the browser`,
  };
}

export async function about() {
  const held = await snapshot();
  return { ...held.meta.source, captured_utc: held.meta.captured_utc, rows: held.rows };
}

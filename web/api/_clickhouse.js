const USER = 'marts_agent';
const PRIMARY_DATASET = 'clickliv';
const PRIMARY_SCHEMA = 'marts';
const REQUIRED = ['CH_HOST', 'MARTS_PASSWORD'];
const NAME = /^[A-Za-z0-9_]{1,48}$/;
const DISCOVERY_TTL_MS = 60_000;
const SAFE = /^(upstream (unreachable|rejected the query, ClickHouse code \d+|returned status \d+)|no marts schema is present on this service)$/;

const DATASETS = `
SELECT name
FROM system.databases
WHERE name = 'marts' OR startsWith(name, 'marts_')
ORDER BY name`;

const SIGNATURE = `
SELECT extractAll(create_table_query, '\\\\{([a-z_]+):') AS names
FROM system.tables
WHERE database = {schema:String} AND name = {view:String}`;

const NUMERIC = {
  grain_minutes: 'UInt32', minute_from: 'UInt32', minute_to: 'UInt32', content_id: 'UInt64',
};
const RESERVED = new Set(Object.keys(NUMERIC));
export const MAX_FILTER = 40;

let discovered = { at: 0, names: [] };
const signatures = new Map();

export function config() {
  const missing = REQUIRED.filter((name) => !process.env[name]);
  if (missing.length) {
    throw new Error(`missing environment: ${missing.join(', ')}`);
  }
  return {
    host: process.env.CH_HOST,
    user: USER,
    password: process.env.MARTS_PASSWORD,
    database: PRIMARY_SCHEMA,
  };
}

export function schemaFor(dataset) {
  if (!NAME.test(dataset)) throw new Error(`invalid dataset name: ${dataset}`);
  return dataset === PRIMARY_DATASET ? PRIMARY_SCHEMA : `${PRIMARY_SCHEMA}_${dataset}`;
}

export function datasetFor(schema) {
  return schema === PRIMARY_SCHEMA ? PRIMARY_DATASET : schema.slice(PRIMARY_SCHEMA.length + 1);
}

export async function query(sql, params = {}, schema = PRIMARY_SCHEMA) {
  const { host, user, password } = config();
  const search = new URLSearchParams({ database: schema, default_format: 'JSONCompact' });
  for (const [name, value] of Object.entries(params)) {
    search.set(`param_${name}`, String(value));
  }
  let response;
  try {
    response = await fetch(`https://${host}:8443/?${search}`, {
      method: 'POST',
      headers: {
        'X-ClickHouse-User': user,
        'X-ClickHouse-Key': password,
        'Content-Type': 'text/plain',
      },
      body: sql,
    });
  } catch (error) {
    console.error('clickhouse transport', error);
    throw new Error('upstream unreachable');
  }
  const text = await response.text();
  if (!response.ok) {
    console.error('clickhouse', response.status, text.slice(0, 500));
    const code = text.match(/Code:\s*(\d+)/);
    throw new Error(code
      ? `upstream rejected the query, ClickHouse code ${code[1]}`
      : `upstream returned status ${response.status}`);
  }
  return JSON.parse(text);
}

export async function datasets() {
  if (discovered.names.length && Date.now() - discovered.at < DISCOVERY_TTL_MS) {
    return discovered.names;
  }
  const result = await query(DATASETS);
  const names = (result.data || [])
    .map(([schema]) => datasetFor(String(schema)))
    .filter((name) => NAME.test(name))
    .sort((a, b) => {
      if (a === PRIMARY_DATASET) return -1;
      if (b === PRIMARY_DATASET) return 1;
      return a.localeCompare(b);
    });
  if (!names.length) throw new Error('no marts schema is present on this service');
  discovered = { at: Date.now(), names };
  return names;
}

export async function resolve(requested) {
  const names = await datasets();
  const wanted = String(requested || '');
  const dataset = names.includes(wanted) ? wanted : names[0];
  return { dataset, schema: schemaFor(dataset), datasets: names, unknown: Boolean(wanted) && wanted !== dataset };
}

export async function signature(schema, view) {
  const key = `${schema}.${view}`;
  const held = signatures.get(key);
  if (held && Date.now() - held.at < DISCOVERY_TTL_MS) return held.names;
  const result = await query(SIGNATURE, { schema, view }, schema);
  const names = [...new Set(result.data?.[0]?.[0] || [])].filter((name) => NAME.test(name));
  if (!names.length) throw new Error(`${view} exposes no parameters`);
  signatures.set(key, { at: Date.now(), names });
  return names;
}

export function dimensionsOf(names) {
  return names.filter((name) => !RESERVED.has(name));
}

export function callSql(schema, view, names) {
  const args = names.map((name) => `    ${name} = {${name}:${NUMERIC[name] || 'String'}}`);
  return `${schema}.${view}(\n${args.join(',\n')})`;
}

export function bind(names, wanted, extra = {}) {
  const bound = { minute_from: 0, minute_to: 4294967295, content_id: 0, ...extra };
  for (const name of dimensionsOf(names)) {
    const value = String(wanted[name] ?? '');
    if (value.length > MAX_FILTER) {
      throw Object.assign(new Error(`filter ${name} must be ${MAX_FILTER} characters or fewer`),
        { client: true });
    }
    bound[name] = value;
  }
  for (const name of ['content_id', 'minute_from', 'minute_to']) {
    const given = String(wanted[name] ?? '').trim();
    if (!given) continue;
    if (!/^\d{1,19}$/.test(given)) {
      throw Object.assign(new Error(`${name} must be a whole number`), { client: true });
    }
    bound[name] = given;
  }
  if (Number(bound.minute_from) > Number(bound.minute_to)) {
    throw Object.assign(new Error('minute_from must not exceed minute_to'), { client: true });
  }
  return Object.fromEntries(names.map((name) => [name, bound[name] ?? '']));
}

export function activeFilters(names, bound) {
  const active = {};
  for (const name of dimensionsOf(names)) if (bound[name]) active[name] = bound[name];
  if (Number(bound.content_id)) active.content_id = String(bound.content_id);
  return active;
}

export function send(res, status, payload, seconds = 60) {
  res.setHeader('Content-Type', 'application/json');
  res.setHeader('Cache-Control', seconds > 0
    ? `public, s-maxage=${seconds}, stale-while-revalidate=${seconds * 2}`
    : 'no-store');
  res.status(status).send(JSON.stringify(payload));
}

export function failure(error) {
  console.error('handler', error);
  const text = String(error?.message || '');
  return SAFE.test(text) ? text : 'the query could not be served';
}

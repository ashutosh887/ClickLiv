const USER = 'marts_agent';
const PRIMARY_DATASET = 'clickliv';
const PRIMARY_SCHEMA = 'marts';
const REQUIRED = ['CH_HOST', 'MARTS_PASSWORD'];
const NAME = /^[A-Za-z0-9_]{1,48}$/;
const DISCOVERY_TTL_MS = 60_000;

const DATASETS = `
SELECT name
FROM system.databases
WHERE name = 'marts' OR startsWith(name, 'marts_')
ORDER BY name`;

let discovered = { at: 0, names: [] };

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
  const response = await fetch(`https://${host}:8443/?${search}`, {
    method: 'POST',
    headers: {
      'X-ClickHouse-User': user,
      'X-ClickHouse-Key': password,
      'Content-Type': 'text/plain',
    },
    body: sql,
  });
  const text = await response.text();
  if (!response.ok) {
    throw new Error(`clickhouse ${response.status}: ${text.slice(0, 300)}`);
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

export function send(res, status, payload, seconds = 60) {
  res.setHeader('Content-Type', 'application/json');
  res.setHeader('Cache-Control', `public, s-maxage=${seconds}, stale-while-revalidate=600`);
  res.status(status).send(JSON.stringify(payload));
}

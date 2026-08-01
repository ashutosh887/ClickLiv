const USER = 'marts_agent';
const DATABASE = 'marts';
const REQUIRED = ['CH_HOST', 'MARTS_PASSWORD'];

export function config() {
  const missing = REQUIRED.filter((name) => !process.env[name]);
  if (missing.length) {
    throw new Error(`missing environment: ${missing.join(', ')}`);
  }
  return {
    host: process.env.CH_HOST,
    user: USER,
    password: process.env.MARTS_PASSWORD,
    database: DATABASE,
  };
}

export async function query(sql, params = {}) {
  const { host, user, password, database } = config();
  const search = new URLSearchParams({ database, default_format: 'JSONCompact' });
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

export function send(res, status, payload, seconds = 60) {
  res.setHeader('Content-Type', 'application/json');
  res.setHeader('Cache-Control', `public, s-maxage=${seconds}, stale-while-revalidate=600`);
  res.status(status).send(JSON.stringify(payload));
}

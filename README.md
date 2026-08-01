# ClickLiv

Real-time foreground-only concurrency for SonyLIV streaming telemetry, on ClickHouse.

A viewer counts as concurrent only while they are **playing**, **foregrounded**, and
**heartbeat-fresh**. Counting every open session instead overstates peak concurrency by
**39%** and average concurrency by **49%** on the provided dataset, and it puts the peak
in the wrong minute. Peak is 2,692 foreground-only against 3,743 naive.

## Results

| Measure | Value |
|---|---|
| Peak concurrency, foreground-only, unfiltered | **2,692** |
| Peak concurrency, naive (any open session) | 3,743 |
| Naive overcount | 39% on peak, 49% on average, and the peak lands in a different minute |
| Instantaneous peak (point-in-time overlap, not occupancy) | 2,282 |
| Peak, platform ANDROID_PHONE | 1,704 |
| Peak, platform SONY_ANDROID_TV | 279 |
| Peak, video_type live | 425 |
| Peak, audio_language hin | 1,614 |
| Peak, IPHONE in india | 329 |
| Peak, vod on Mweb | 62 |
| Heartbeat cadence, measured (the data dictionary says 60s) | 40s |
| Threshold sensitivity, grace 20s to 60s by gap 60s to 120s | peak moves 0.3%, peak minute never moves |
| Serving latency, server-side, 40 samples | p99 30ms against a stated 100ms target, p50 22ms, p95 27ms |
| Gates | A 12/12 PASS, B byte-identical rebuild, C PASS on a held-out day, D chDB agrees with the server |

Every number above is produced by a query this repository ran, tagged with a `query_id`
and traceable to `system.query_log`. See [docs/evidence.md](docs/evidence.md).

## Pipeline

```
ch-hackathon-content-data.csv ──▶ content_meta ──▶ content_dict
ch-hackathon-raw-data.csv ──▶ raw_events ──▶ active_intervals
                                             │
                                             ├──▶ session_minutes ──▶ minute_occupancy
                                             │    one row per (session, minute), deduped
                                             │    PRIMARY SERVING PATH
                                             │
                                             ├──▶ minute_deltas
                                             │    +1/-1 on merged runs, windowed cumsum
                                             │    SECOND SERVING PATH
                                             │
                                             └──▶ maxIntersections
                                                  arithmetic oracle, no rollup involved

src/clickliv/reference.py   reads the CSV directly and owes ClickHouse nothing
chDB                        runs 01 through 04 unmodified, in-process, same hashes
```

## Quickstart

```sh
git clone https://github.com/ashutosh887/ClickLiv.git
cd ClickLiv
cp .env.example .env
make up          # ClickHouse 26.7 in Docker, or point .env at ClickHouse Cloud
make all         # schema, load, sessionize, both serving paths, reference, Gate A
```

`make all` runs CSV to Gate A in about 8 seconds and ends here:

```
PASS  intervals: SQL == python reference             0 only in SQL, 0 only in reference
PASS  rollup: occupancy == python reference          0 only in SQL, 0 only in reference
PASS  deltas == occupancy, no filter                 3649 minutes, peak 2692
PASS  deltas == occupancy, platform ANDROID_PHONE    3561 minutes, peak 1704
PASS  deltas == occupancy, platform SONY_ANDROID_TV  119 minutes, peak 279
PASS  deltas == occupancy, video_type live           65 minutes, peak 425
PASS  deltas == occupancy, audio_language hin        3398 minutes, peak 1614
PASS  deltas == occupancy, IPHONE in india           763 minutes, peak 329
PASS  deltas == occupancy, vod on Mweb               60 minutes, peak 62
PASS  half-open sweep == python instantaneous peak   sweep 2282, reference 2282
PASS  maxIntersections >= half-open sweep            maxIntersections 2282, sweep 2282, difference 0
PASS  instantaneous peak <= occupancy peak           2282 <= 2692, gap 410

Gate A: PASS  (12/12 checks)
```

Every other target, the ClickHouse Cloud path, and the surfaces you can start on your own
machine are in [docs/operations.md](docs/operations.md). The data and the observability
stores themselves run on a ClickHouse Cloud service and a ClickHouse managed Postgres
service in `ap-south-1`, both private to the team's org, and the answers are committed as
files rather than served from a URL.

## Live demo

- **[clickliv.vercel.app](https://clickliv.vercel.app)** is the concurrency chart,
  deployed on Vercel, calling ClickHouse Cloud through a serverless proxy so the read
  only key never reaches the browser.
- **[librechat.15-252-63-157.sslip.io](https://librechat.15-252-63-157.sslip.io)** is
  the conversational surface. Ask about foreground concurrency and the guardrailed
  marts tools answer; it can also fall back to a read only ClickHouse MCP server for
  ad hoc questions. Demo login in `credentials.env` (gitignored, template in
  `credentials.env.example`).
- **[langfuse.15-252-63-157.sslip.io](https://langfuse.15-252-63-157.sslip.io)** is the
  LLM observability pillar: every chat call above is traced here, and its own storage
  is entirely ClickHouse products, traces in ClickHouse Cloud, transactional state in
  ClickHouse managed Postgres.
- **[clickstack.15-252-63-157.sslip.io](https://clickstack.15-252-63-157.sslip.io)** is
  the ClickStack observability pillar, tracing the pipeline itself.

All three self-hosted surfaces run on one EC2 instance in `ap-south-1`, next to the
ClickHouse Cloud service, behind Caddy for automatic HTTPS. Stable as long as the
instance is up, not tied to anyone's laptop. See
[docs/operations.md](docs/operations.md) for the deploy.

## What's in the box

Five independent verification paths, diffed against each other by the gates:

- `minute_occupancy`, one row per (session, minute), the primary serving path.
- `minute_deltas`, signed +1/-1 runs and a windowed cumulative sum, the second serving path.
- `maxIntersections`, an arithmetic oracle with no rollup involved.
- `src/clickliv/reference.py`, a Python reference that reads the CSV and owes ClickHouse nothing.
- chDB, the same SQL files in-process, no server, identical hashes.

Four OSS pillars, with ClickHouse underneath every one of them:

- **ClickHouse** stores and serves everything, locally in Docker or on Cloud, unchanged.
- **ClickStack** traces every pipeline stage and query, with server-side `read_rows` attached.
- **Langfuse** traces the LLM and MCP calls, on this project's own Cloud service plus ClickHouse managed Postgres.
- **LibreChat** asks the question in plain language, through a guardrailed MCP server that never lets a model emit SQL.

## Documentation

| Page | What it covers |
|---|---|
| [docs/architecture.md](docs/architecture.md) | The model, the active rule, where the data dictionary is wrong, additivity across dimensions and why peak is not composable across time, the repository layout |
| [docs/correctness.md](docs/correctness.md) | Gates A through D, the oracles, threshold sensitivity, occupancy versus instantaneous per slice, the test suite |
| [docs/serving.md](docs/serving.md) | The `marts` surface, RBAC and the query budget, projections, update handling |
| [docs/observability.md](docs/observability.md) | ClickStack, Langfuse, the two-sink exporter, decline alerting |
| [docs/mcp.md](docs/mcp.md) | The MCP tools, the guardrails, LibreChat and its two surfaces, the proven round trip |
| [docs/operations.md](docs/operations.md) | Running it, every make target, local development surfaces, ClickHouse Cloud findings, the unseen-day runbook, Gate C |
| [docs/scale.md](docs/scale.md) | O7 sharding and read-cost proofs at 1x, 10x and 100x, user-level concurrency |
| [docs/evidence.md](docs/evidence.md) | What lands in `answers/`, `evidence/` and `submission/`, the serving SLO, checking any number against a `query_id` |

## Licence

MIT. See [LICENSE](LICENSE).

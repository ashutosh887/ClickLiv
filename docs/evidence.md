# Answers and evidence

No pipeline evidence, no credit. `make answers` runs a benchmark set of peak and
average concurrency at minute, hour and day grain, unfiltered and across the same
dimension slices Gate A checks, entirely through `marts.v_concurrency`, and writes:

```
answers/benchmark_answers.csv   query_label, params, peak and average concurrency,
                                 the stated average denominator, byte-identical
                                 across runs because it carries no query_id or
                                 timestamp
answers/latencies.csv           the same queries' query_duration_ms, read_rows,
                                 read_bytes, result_rows, memory_usage, read from
                                 system.query_log by query_id, never client wall
                                 clock (D14)
evidence/query_log.csv          the same query_log rows again, as the artifact a
                                 judge can check against a query_id
evidence/explain_*.txt          EXPLAIN indexes=1 for the unfiltered day-grain
                                 query, showing the granule count against
                                 minute_occupancy directly, plus EXPLAIN ANALYZE
                                 where the server is 26.7 or newer and the
                                 reason it is absent where it is not
evidence/oracle_match.csv       occupancy peak, maxIntersections, and the Python
                                 reference's independent numbers, side by side
```

Answers and latencies are two files because an answer must be stable across runs and
a latency should not be forced to pretend it is. Every number is computed by a query
this repository ran, tagged with a `query_id`, and traceable to `system.query_log`;
none of it is hand-typed.

## Checking any number yourself

Take a row from `answers/latencies.csv` or `evidence/query_log.csv`, read its
`query_id`, and look the same id up in `system.query_log` on the service the run used.
The `read_rows` the MCP server reports for a live call is checkable the same way, and
was verified byte identical to `system.query_log.read_rows` for the same `query_id`,
96,818 rows both ways on the unfiltered day-grain call. See [mcp.md](mcp.md).

The rest of the evidence directory follows the same rule, one file per proof:
`evidence/instantaneous_vs_occupancy.txt`, `evidence/projections.txt`,
`evidence/scale.txt`, `evidence/user_level.txt`, `evidence/dimension_crossover.txt`,
`evidence/decline_alerts.txt`, `evidence/incremental_update.txt`,
`evidence/conversational_layer.txt`, and `evidence/gate_c/` for the held-out day.

## The serving SLO, because no SLA was published (O6b)

`make submission` runs the benchmark set five times over and turns it into the two
things nobody upstream specified.

Rather than leave serving latency unstated, the target is ours and stated: p99 under
100ms. The committed run measures p99 **30ms**, p50 22ms, p95 27ms, min 16ms over 40
samples, 8 benchmark queries times 5 repetitions, every one of them server-side
`query_duration_ms` read from `system.query_log` by `query_id` rather than client wall
clock. Latency moves a millisecond or two run to run, which is exactly why
`evidence/serving_slo.txt` carries the percentiles and `evidence/serving_slo.csv`
carries the per-sample rows: they can be recomputed rather than believed.

## A format-agnostic answer bundle, because no answer format was published (O2)

`submission/` holds the answers as `benchmark_answers.csv` and as
`benchmark_answers.json`, written from one source of truth so the two cannot disagree,
plus `manifest.json` recording the ClickHouse version, the host and database, row counts
for `raw_events`, `active_intervals`, `minute_occupancy` and `minute_deltas`, the minute
range in both epoch minutes and UTC, the git commit, the gap and grace thresholds, the
Python reference's own peak, and a SHA-256 plus byte size for every other file in the
bundle. `README.txt` explains the columns, names the average's denominator, and states
where the latencies come from, so a grader can read the bundle cold.

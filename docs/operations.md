# Operations

Running the pipeline locally or against ClickHouse Cloud, every make target, the
surfaces you can start on your own machine, and the runbook for the graded unseen day.

## Running it

```sh
cp .env.example .env
make up          # ClickHouse 26.7 in Docker, or point .env at ClickHouse Cloud
make all         # schema, load, sessionize, both serving paths, reference, Gate A
```

`make all` runs CSV to Gate A in about 8 seconds.

## Every make target

```sh
make up          # ClickHouse 26.7 in Docker, or point .env at ClickHouse Cloud
make all         # schema, load, sessionize, both serving paths, reference, Gate A
make gate-b      # rebuild twice, assert byte-identical serving tables
make sweep       # threshold sensitivity grid
make chdb        # same SQL in-process on chDB, no server at all
make gate-c      # held-out single-day dry run, evidence in answers/gate_c and evidence/gate_c
make scale       # O7: sharding and read-cost proofs at 1x/10x/100x, evidence/scale.txt
make userlevel   # O4: session-level vs user-level concurrency, evidence/user_level.txt
make crossover   # the problem statement's own dimension-crossover example, measured
make decline     # optional: deterministic concurrency-decline alerting
make incremental # a real open session absorbs a new heartbeat live, proven vs batch
make instantaneous # O3: occupancy vs instantaneous overlap, for every dimension slice
make submission  # O2: the answer bundle, plus the measured serving SLO (O6b)
make replay      # the graded unseen-day run, reset to submission bundle, one command
make mcp         # the guardrailed MCP server, four pre-vetted tools over marts
make llm-up      # Langfuse 4.1.0, both of its databases ClickHouse products
make chat-up     # LibreChat v0.8.7, wired to both MCP surfaces
```

The pipeline stages are also individually callable: `make schema`, `make load`,
`make reconcile`, `make sessionize`, `make occupancy`, `make deltas`, `make reference`,
`make verify`, `make pipeline`, `make marts`, `make answers`, `make projections`,
`make ui`, `make obs`, `make ping`, `make test`, `make reset`. The Docker profiles have
matching `down` and `logs` targets: `make down`, `make logs`, `make obs-down`,
`make obs-logs`, `make llm-down`, `make llm-logs`, `make chat-down`, `make chat-logs`.

## Local development surfaces

These are development surfaces on your own machine. Each one starts when you run its
`make` command and stops when you stop it; none of them is hosted anywhere, and the URLs
below only resolve on the machine that started them.

Start the stack with `make up && make obs-up && make llm-up && make chat-up`, then
`make mcp` and `make ui` in their own shells.

| Open this | On your machine | Started by | What it shows |
|---|---|---|---|
| Concurrency dashboard | <http://localhost:8090> | `make ui` | The concurrency curve with a platform filter, read straight from `marts.v_concurrency` |
| LibreChat | <http://localhost:3080> | `make chat-up` | Ask for concurrency in plain language, answered through the guardrailed MCP tools |
| Langfuse | <http://localhost:3300> | `make llm-up` | LLM and MCP traces, with token usage and cost, stored in our own ClickHouse Cloud service |
| ClickStack (HyperDX) | <http://localhost:8080> | `make obs-up` | Pipeline traces, every stage and every query, with server-side `read_rows` attached |
| MCP health | <http://localhost:8765/health> | `make mcp` | The four pre-vetted tools and the restricted user they run as |
| ClickHouse MCP health | <http://localhost:8766/health> | `make chat-up` | The official read-only ClickHouse MCP surface |

`UI_PORT` and `MCP_PORT` move the two Python servers; ClickStack also listens for OTLP on
4317 and 4318, and keeps its own ClickHouse on 8124. ClickHouse itself answers on 8123
in Docker and on 8443 when `.env` points at Cloud.

The only things that are not local are the managed services this project stores data in:
a ClickHouse Cloud service named `ClickLiv` in `ap-south-1`, and the
`clickliv-langfuse` managed Postgres service beside it. Both are private to the team's
org, reached through the ClickHouse Cloud console at <https://console.clickhouse.cloud>.
Answers and evidence live in the repo rather than behind a URL, in `answers/`,
`evidence/` and `submission/`, described in [evidence.md](evidence.md).
`sql/09_dashboard.sql` holds the saved queries for a Cloud console dashboard.

## Running against ClickHouse Cloud

Every command runs unchanged against ClickHouse Cloud, the submission's actual
requirement ("load the data into your team's own ClickHouse Cloud service, there is no
shared instance"): `.env` holds one active target at a time, Cloud or local Docker, the
other block commented out (see `.env.example`). `.env` here is currently pointed at the
real Cloud service.

Verified end to end against it (Mumbai, `ap-south-1`, 2 replicas): Gate A 12/12, Gate B
byte-identical hashes to local, marts, answers, projections, scale, userlevel,
crossover, decline, incremental, instantaneous, submission and the MCP surface all
pass, matching local numbers exactly. Four
real, Cloud-specific differences found and fixed while proving that, not assumed away:

- **A multi-replica read-after-write race.** A plain `SYSTEM RELOAD DICTIONARY` or
  `SYSTEM FLUSH LOGS` only reaches whichever replica handled that one HTTP request; a
  follow-up read on a different replica can see stale (or missing) state. Never
  visible on the single-node local target. Fixed at the source:
  `SYSTEM RELOAD DICTIONARY ON CLUSTER default` (deterministic, falls back to the
  plain form where there is no Keeper, i.e. locally) for the content dictionary, and
  a bounded flush-and-retry helper (`ClickHouse.query_log_rows`) for every
  `system.query_log` read.
- **`EXPLAIN ANALYZE` needs ClickHouse 26.7+**, confirmed as a hard version gate, not
  a config flag: syntax error, not a runtime error, on Cloud's 26.4. `answers.py`
  degrades gracefully, records why in the evidence file, and every other check is
  unaffected.
- **A replica's `system.query_log` holds only the queries that replica ran.** Cloud
  routes each HTTP request to either replica, so reading `system.query_log` after a
  round-robined batch of queries returned roughly half of them: 2 of 8 rows in one
  case, and the missing latencies were silently absent rather than reported as
  missing. Latency evidence was incomplete without ever failing. Every `query_log`
  read now spans every replica through
  `clusterAllReplicas(default, system.query_log)`, resolved once per connection by
  probing for a cluster so the single-node local target still reads the plain table.
  The same fix collapsed the tracer's own second, differently-worded `query_log` read
  into that one helper.
- **Cloud enforces a password complexity policy** (uppercase + digit + special
  character) that local Docker does not. `MARTS_PASSWORD` needed a real password, not
  the local placeholder.

## The unseen-day run

The graded drop is one fresh CSV (O8), so the whole run is one command. Drop the new
file into `data/`, point `RAW_CSV` at it in `.env`, then:

```sh
make replay      # reset, schema, load, sessionize, occupancy, deltas, reference,
                 # verify (Gate A), marts, projections, answers, instantaneous, submission
```

Then commit `answers/`, `evidence/` and `submission/`. `replay` prints each step as it
starts, stops at the first one that fails and names it, and reports its own wall clock
at the end. Nothing about it is specific to the tuning file: the CSV paths are
environment variables, and the thresholds are substituted into the SQL from the same
place.

That day-agnosticism is a property the loader had to be fixed to have, and it is worth
stating plainly because the bug would have been expensive. `reconcile` used to compare
any input against the tuning day's measured counts (905,558 events, 10,866 sessions,
9,618 users, 3,357 referenced content ids, 33,463 content rows) and fail the run on any
mismatch, so a fresh day that was merely different rather than wrong would have aborted
the graded run before it did any work. It now separates the two kinds of check. Day
invariants still fail the run: every `content_id` in the events has to resolve through
the content dictionary, and something has to have actually loaded. Row counts are
reported as drift instead, marked `differs (expected on a new day)`, followed by
`input differs from the tuning data; day-invariant checks still enforced`.

## Gate C, the held-out dry run

The tuning CSV spans 11.8 days, but the real submission is one fresh day (O8). `make
gate-c` rehearses that drop before it happens: it holds out the busiest calendar day in
the tuning data (20660, 849,888 of 905,558 events, also the most recent, which is what a
freshly landed day looks like), reloads the pipeline against that slice alone, and runs
schema through chDB against it, unmodified:

```
schema, load, sessionize, occupancy, deltas, reference, verify   Gate A on the slice
sessionize, occupancy, deltas again                              Gate C: idempotent rebuild
marts, answers, evidence                                         the full serving layer
07_projections.sql, evidence                                     the projection, rebuilt
chDB                                                              Gate D on the slice
```

Every gate passes on data the pipeline was not specifically run against before:

```
Gate A: PASS  (12/12 checks)
Gate C: PASS  rebuild is idempotent on the held-out day
Gate D: PASS  chDB agrees with the server
```

Output lands in `answers/gate_c/` and `evidence/gate_c/`, alongside the full-dataset
answers and evidence rather than overwriting them, so both are checkable at once.

**Gate C caught a real bug the very first time it ran.** `MATERIALIZE PROJECTION` is an
asynchronous mutation; querying immediately after issuing it can race the projection
still being built, and forcing it by name then fails with `INCORRECT_DATA` because it
genuinely is not yet there to use. Every previous run of `make projections` had enough
wall-clock gap between separate commands to never hit this. Fixed with
`SETTINGS mutations_sync = 2` on the `MATERIALIZE PROJECTION` statement in
`sql/07_projections.sql`, so the ALTER blocks until the projection is actually built.
This is what Gate C is for: whatever breaks on a full, uninterrupted, unfamiliar-data
run is what would have broken on the real unseen-day drop, not a rehearsal problem.

After the dry run, `make gate-c` reloads the full dataset and re-verifies Gate A, so the
live database and the committed `answers/`/`evidence/` are left describing the full
tuning data, not the held-out slice, whether or not the dry run passed.

# Operations

Running the pipeline locally or against ClickHouse Cloud, every make target, the
surfaces you can start on your own machine, and the runbook for the graded unseen day.

## Running it

```sh
cp .env.example .env
make up          # ClickHouse 26.7 in Docker, or point .env at ClickHouse Cloud
make all         # schema, load, sessionize, both serving paths, reference, Gate A
```

`make all` runs CSV to Gate A in seconds against local Docker and in minutes against
Cloud, where every query pays a round trip. Measured figures are in the
[wall clock table](unseen-day.md#wall-clock).

## Every make target

Regenerated from the `Makefile`. Every target it declares is here.

**The pipeline, end to end.**

```sh
make all         # schema, load, sessionize, both serving paths, reference, Gate A
make pipeline    # the same without reference and Gate A
make schema      # each stage is also callable on its own
make load
make reconcile
make sessionize
make occupancy
make deltas
make reference
make verify
make marts       # the parameterized views, the role and the query budget
make projections
make answers
make reset       # drop everything this project created, including the marts schema
```

**The gates and the measured proofs.**

```sh
make gate-b      # rebuild twice, assert byte-identical serving tables
make gate-c      # held-out single-day dry run, evidence in answers/gate_c and evidence/gate_c
make chdb        # Gate D: same SQL in-process on chDB, no server at all
make sweep       # threshold sensitivity grid
make scale       # O7: sharding and read-cost proofs at 1x/10x/100x, evidence/scale.txt
make userlevel   # O4: session-level vs user-level concurrency, evidence/user_level.txt
make instantaneous # O3: occupancy vs instantaneous overlap, for every dimension slice
make crossover   # the problem statement's own dimension-crossover example, measured
make incremental # a real open session absorbs a new heartbeat live, proven vs batch
make decline     # optional: deterministic concurrency-decline alerting
make submission  # O2: the answer bundle, plus the measured serving SLO (O6b)
make claims      # re-read every published figure live, name every doc stating an old one
```

**The sealed-dataset path.** Described in full in [the unseen-day
runbook](unseen-day.md).

```sh
make preflight RAW=<events> CONTENT=<content>   # read only, touches nothing
make unseen    RAW=<events> CONTENT=<content>   # the graded run, one command
make rollback                                    # put the previous demo back
make unseen-fixture     # the adversarial synthetic fresh day
make unseen-variants    # that day in every container and CSV quirk
make replay             # superseded by make unseen, see the runbook
```

**Surfaces and fixtures.**

```sh
make up          # ClickHouse 26.7 in Docker, or point .env at ClickHouse Cloud
make mcp         # the guardrailed MCP server, five pre-vetted tools over marts
make ui          # the local concurrency dashboard
make obs         # read the pipeline trace back out of ClickStack
make ping        # name the host and database .env currently points at
make llm-up      # Langfuse 4.1.0, both of its databases ClickHouse products
make chat-up     # LibreChat v0.8.7, wired to both MCP surfaces
make obs-up      # ClickStack
make test        # 78 stdlib unittest tests, zero dependencies
make data        # refetch the sample CSVs into data/
make fixture     # the small pipeline fixture
make fixture-pipeline   # the whole pipeline over that fixture, in-process
```

The Docker profiles have matching `down` and `logs` targets: `make down`, `make logs`,
`make obs-down`, `make obs-logs`, `make llm-down`, `make llm-logs`, `make chat-down`,
`make chat-logs`.

`python -m clickliv snapshot` moves the serving tables aside without running anything
else, and `python -m clickliv sql "<query>"` runs one query against the configured
service. Neither has a make target.

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
| MCP health | <http://localhost:8765/health> | `make mcp` | The five pre-vetted tools and the restricted user they run as |
| ClickHouse MCP health | <http://localhost:8766/health> | `make chat-up` | The official read-only ClickHouse MCP surface |

`UI_PORT` and `MCP_PORT` move the two Python servers; ClickStack also listens for OTLP on
4317 and 4318, and keeps its own ClickHouse on 8124. ClickHouse itself answers on 8123
in Docker and on 8443 when `.env` points at Cloud.

## Deploying the public page

`clickliv.vercel.app` is deployed from the CLI, out of `web/`:

```sh
cd web && vercel --prod
```

**The Vercel project has no Git repository connected,** so pushing to GitHub does not
redeploy it. That is deliberate while the site serves a frozen snapshot, but it is the
thing to remember if the page ever looks out of date against `main`. The project holds
no environment variables at all; `make web-snapshot` regenerates `web/snapshot` and the
deploy carries it.

## The public EC2 deployment, retired

> This deployment no longer exists. The instance was terminated and the Elastic IP
> released after the Click-a-thon, so every `sslip.io` hostname below is dead and
> the three surfaces run locally instead, one `make` target each. The section is
> kept because it records how the hosted deployment was put together.

LibreChat, Langfuse and ClickStack also ran permanently on one EC2 instance
(`i-04c48ddbea3351191`, `t3.large`, `ap-south-1`), behind Caddy for automatic HTTPS
on sslip.io hostnames (no domain needed, real Let's Encrypt certs). Same
`docker-compose.yml`, same `.env`, pointed at the same ClickHouse Cloud service, just
not tied to anyone's laptop. The marts MCP server runs there too, as a systemd unit
(`clickliv-mcp.service`) rather than a foreground process.

The `clickhouse-mcp` container authenticates as `mcp_agent`, a read-only role scoped
to `clickliv`/`marts` with a 20s/2GB/10k-row budget, never the Cloud admin `default`
user. See `.env.example` for the SQL that creates it.

An Elastic IP (`15.252.63.157`) keeps the address fixed across restarts. SSH key is
`~/.ssh/clickliv-demo.pem`, security group `clickliv-demo-sg` allows only 22/80/443.
Teardown after the deadline: `aws ec2 terminate-instances --instance-ids
i-04c48ddbea3351191 --region ap-south-1` and release the Elastic IP, or it keeps
billing in a tiny amount against the AWS hackathon credit.

The managed services this project stored data in were a ClickHouse Cloud service named
`ClickLiv` in `ap-south-1` and the `clickliv-langfuse` managed Postgres beside it, both
private to the team's org and reached through <https://console.clickhouse.cloud>. The
Postgres has since been deleted and the ClickHouse service is stopped with its data
intact; starting it again from the console is all that live mode needs.

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

The graded drop is one fresh pair of CSVs (O8), and the whole run is one command:

```sh
make unseen RAW=<events.csv> CONTENT=<content.csv>
```

The full procedure, what every stage prints, how to roll it back and what to do when the
numbers look wrong is [the unseen-day runbook](unseen-day.md). Read that page, not this
one, on the day.

**Use `make unseen`, never `make replay`.** Both exist in the `Makefile` and both run
essentially the same stage list, so the difference is easy to miss and expensive to get
wrong. `replay` writes into `artifacts/`, `answers/`, `evidence/` and `submission/`,
which are the committed tuning-data results, so it overwrites the very numbers the
sealed run has to be compared against. `unseen` redirects all four roots under
`unseen/`, takes its input files as arguments rather than from `.env`, names the host
and database before it touches anything, additionally runs the `incremental` stage, and
ends by printing the comparison table the README needs. It is a strict superset with
guard rails. `replay` is kept only so an older transcript still resolves.

Nothing about either is specific to the tuning file: the CSV paths are environment
variables, and the thresholds are substituted into the SQL from the same place.

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

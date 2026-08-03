# ClickLiv

Foreground-only concurrency for SonyLIV streaming telemetry. ClickHouse is the primary
datastore and every stage of the model runs inside it.

## The one idea

A session counts as a concurrent viewer only while it is playing, foregrounded and
heartbeat-fresh. Counting every open session instead overstates the peak. The whole
repository exists to compute that rule correctly, serve it fast, and prove it against
implementations that share no code.

## Pipeline

`sql/` runs in numeric order and each file owns one stage.

| File | Produces | Note |
|---|---|---|
| `01_schema.sql` | `raw_events`, `content_meta`, `content_dict` | Events land exactly as they arrived, nothing aggregated on the way in |
| `02_sessionize.sql` | `active_intervals` | A state machine over ordered events per session |
| `03_occupancy.sql` | `session_minutes`, `minute_occupancy` | One row per session and minute, then summed per dimension |
| `04_deltas.sql` | `minute_deltas` | The same intervals as signed deltas, a second path built differently |
| `05_oracles.sql` | `ref_intervals`, `ref_rollup` | The Python reference loaded back for diffing |
| `06_marts.sql` | `marts.*` views, roles, budgets | **Opens with DROP DATABASE. See Safety** |
| `07_projections.sql` | `proj_content_minute` | |
| `08_incremental.sql` | `open_session_state` | Absorbs heartbeats for sessions still open |
| `09_dashboard.sql` | Saved queries for the Cloud console | Console-safe shapes only, see below |

## How activity is decided

Two signals are carried forward independently per session, and a session is active only
when both are on.

- `playing` is set by `VideoPlay`, `resume`, `speed-resume`, `AdResume`, and cleared by
  `pause`, `speed-pause`, `AdPause`, `VideoError`, `VideoSessionEnd` and
  `VideoSessionStart`. It defaults to off.
- `foreground` is set by `AppForegrounded` and `VideoSessionStart`, cleared by
  `AppBackgrounded`. It defaults to on, because the data dictionary states those two
  markers are not guaranteed.

Heartbeats carry neither signal. They change no state, but they extend the live interval,
which is how heartbeat-missing time is excluded without a separate rule. An interval
closes at the next event, or at the last event plus `GRACE_SECONDS` when nothing follows.
A gap wider than `GAP_SECONDS` closes the interval and a later event opens a new one.

`GRACE_SECONDS` and `GAP_SECONDS` are derived from the measured heartbeat cadence, not
from the data dictionary. The dictionary says 60 seconds; the data says 40. Trust the
measurement and re-derive with `make sweep` if a new dataset disagrees.

Two readings of concurrency are computed and both are published, because the problem
statement does not say which it means. Occupancy counts a session in a minute if it was
active at any point inside it. Instantaneous counts overlap at a point in time.
Occupancy leads because the worked example in the problem statement reads that way.

## Correctness

Five implementations that share no code must agree, and `make verify` diffs them in
twelve checks. The rollup, the signed deltas, `maxIntersections`, a half-open sweep, and
a pure Python reference that reads the CSV and owes ClickHouse nothing. When they
disagree the model is wrong, not the data.

## Serving

Consumers read `marts` and nothing underneath it. `marts_agent` and `mcp_agent` are
read-only roles with server-enforced budgets on time, memory and rows, so the limit is
not a prompt instruction. Filtering happens before aggregation and aggregation is always
to minute, so any combination of dimension, range and grain gives the same answer.

Per-minute concurrency is additive across dimensions. That property is what the whole
serving layer rests on, and it comes from deduplicating to one active flag per session
and minute.

## Safety

These have each caused a real outage or a near miss.

- **`make reset`, `make all`, `make replay`, `make unseen`, `make marts`, `make schema`,
  `make load` and `make pipeline` are destructive.** They drop and rebuild the live
  serving layer. Every one honours `DB=` to target a scratch database; there is a test
  asserting it, because the Makefile once did not thread it and a scratch run took the
  public demo down.
- **`make unseen` is the graded run.** Never `make replay`, which writes over the
  committed results the sealed run is meant to be compared against. Always
  `make preflight` first; it is read-only and fails before anything is dropped.
- **From `reset` until the marts stage completes, every public surface is down.** Vercel,
  the MCP tools, LibreChat and the Cloud dashboard. `snapshot` renames the serving tables
  to `__prev` beforehand, so `make rollback` restores in seconds.
- **On Cloud, `system.query_log` is per replica.** Read it through
  `clusterAllReplicas(default, system.query_log)` or silently lose half the evidence.
  `ch.query_log_rows` does this for you. The one deliberate exception is the console tile
  in `09_dashboard.sql`, because the tile runner refuses table functions.
- **The Cloud console refuses some query shapes** with no server-side trace, table
  functions and parameterized views among them. `scripts/verify_dashboard.sh` checks for
  them statically; a query that runs fine by hand can still fail as a tile.
- Secrets live in `.env`, which is gitignored by `*.env`. That pattern does not match a
  name like `.env.backup`, so do not create one inside the repository.

## Datasets

`clickliv` is the primary and every live surface reads it. `clickliv_sample` preserves an
earlier dataset so results can be published side by side. `copy_dataset.sh` creates the
copy; running it after a rebuild destroys the comparison.

The schema carries optional columns, so a dataset with extra fields loads without a
change. Present columns bind, absent ones default to empty, and both datasets stay
queryable from one schema.

## Conventions

- No comments in Python, shell or JavaScript. SQL keeps the comments that record a
  measurement or justify a trade-off, because that is the reasoning a reader needs.
- Docstrings are one or two lines. Longer explanations belong in the README, which is the
  only prose document; `docs/` holds the pitch deck and nothing else.
- No file, dependency or abstraction that is not needed. Delete dead code on sight.
- Every published number must be reproducible. `make claims` reads them live and names
  any document stating a superseded value.

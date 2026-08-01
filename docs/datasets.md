# Two datasets, one contract

The project was built and tuned against a sample extract. The graded SonyLIV dataset
replaces it in `clickliv`. Both stay queryable, through the same marts contract, so the
dashboard can put them side by side.

| dataset | database | schema | what it holds |
| --- | --- | --- | --- |
| final | `clickliv` | `marts` | the graded SonyLIV readings |
| sample | `clickliv_sample` | `marts_clickliv_sample` | the readings the project was tuned against |

The schema names follow `marts_database()` in `src/clickliv/cli.py`: only the default
database owns the bare `marts` name, and every other database `X` is served by `marts_X`.
Both schemas expose the same views with the same parameters, so switching dataset is a
change of schema name and nothing else. `marts_agent` reads both and reaches neither set
of underlying tables.

## Making the copy

```
./scripts/copy_dataset.sh                  # copies clickliv into clickliv_sample
./scripts/copy_dataset.sh other_sample     # any target, as long as it ends in _sample
```

It copies the built tables with `CREATE TABLE ... AS` plus `INSERT ... SELECT` rather
than replaying the pipeline from the CSVs, because the pipeline output is already
verified and 905,558 raw events take seconds to clone. It then builds the marts layer for
the copy from `sql/06_marts.sql` with `${MARTS_DB}` bound to the copy's schema. Re-running
it rebuilds the copy from scratch, so it is safe to run twice.

Copied: `raw_events`, `content_meta`, `active_intervals`, `session_minutes`,
`minute_occupancy`, `minute_deltas`, `ref_intervals`, `ref_rollup`, the
`proj_content_minute` projection on `minute_occupancy`, and a `content_dict` dictionary
pointed at the copy's own `content_meta`.

## The guard

The script renders the SQL, then checks every statement before sending any of it. A
statement that writes must name a database whose name ends in `_sample`. An unqualified
write target, or a statement shape the checker does not recognise, aborts the run as
well, so it fails closed. `./scripts/copy_dataset.sh clickliv` and
`./scripts/copy_dataset.sh marts` both refuse before opening a connection.

The guard lives in the script rather than in the environment because the environment is
what cannot be trusted. `step_reset` in `src/clickliv/cli.py` issues a literal
`DROP DATABASE IF EXISTS marts` plus drops of `marts_agent`, `marts_readonly` and
`marts_budget`, and it ignores `CH_DATABASE`. Since `reset` is the third step of both
`replay` and `unseen`, running either against a scratch database takes the live `marts`
schema down with it. Copying tables directly avoids that path entirely.

## Verified against the live schema

Run on 2026-08-01. Row counts and `sum(cityHash64(*))` content hashes matched on all
eight tables, 1,357,842 rows in total.

| figure | `marts` | `marts_clickliv_sample` |
| --- | --- | --- |
| foreground peak | 2,692 at 2026-07-26 10:56:00 UTC | 2,692 at 2026-07-26 10:56:00 UTC |
| naive peak | 3,743 at 2026-07-26 10:59:00 UTC | 3,743 at 2026-07-26 10:59:00 UTC |
| peak overcount | 39.0 percent | 39.0 percent |
| average overcount | 49.0 percent | 49.0 percent |
| `v_dimension_values` | 224 rows | 224 rows |
| `v_titles` | 3,357 rows | 3,357 rows |
| `v_naive_vs_foreground` | 5,255 rows | 5,255 rows |

`v_data_window` on the copy reports 2026-07-14 15:43:00 to 2026-07-26 11:30:00 UTC,
3,649 minutes carrying sessions over 96,818 occupancy rows.

`marts_agent` reads `marts_clickliv_sample.v_overcount` and gets 2,692. The same user on
`clickliv_sample.minute_occupancy` is refused with code 497, so the copy inherits the
least-privilege posture rather than opening a second way in.

Storage: 6.41 MiB on disk for the copy against 7.40 MiB for `clickliv`, both from 175.05
MiB uncompressed. The copy is the smaller of the two because it was written in one pass
and has fewer parts to merge.

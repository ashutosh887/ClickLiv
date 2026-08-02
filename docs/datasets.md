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
what cannot be trusted. `reset` is the third stage of both `replay` and `unseen`, and it
issues an unconditional `DROP DATABASE` against whichever marts schema it resolves.
`step_reset` in `src/clickliv/cli.py` resolves that name through `marts_database()`, so a
scratch run drops only its own `marts_<database>` and leaves the global `marts_agent`
user, `marts_readonly` role and `marts_budget` profile alone. It did not always: a
scratch run used to take the live `marts` and its user down with it, which is what
commit `b28826a` fixed. Copying tables directly avoids the whole path rather than relying
on that resolution staying correct.

## Verified against the live schema

Re-verified 2026-08-02, after the sealed day replaced the tuning data in `clickliv` and
the `coalesce(playing_signal, 0)` fix landed. `clickliv_sample` is a separate copy, not a
live mirror of `clickliv`, so its own figures move only when `copy_dataset.sh` is re-run;
they now reflect the fixed code.

| figure | `marts` (sealed) | `marts_clickliv_sample` (tuning) |
| --- | --- | --- |
| foreground peak | 22,175 at 2026-07-31 11:16:00 UTC | 2,710 at 2026-07-26 10:56:00 UTC |
| naive peak | 24,196 at 2026-07-31 11:16:00 UTC | 3,743 at 2026-07-26 10:59:00 UTC |
| peak overcount | 9.1 percent | 38.1 percent |
| average overcount | 90.1 percent | 45.9 percent |

`v_data_window` on the copy reports 2026-07-14 15:43:00 to 2026-07-26 11:30:00 UTC,
3,650 minutes carrying sessions over 98,034 occupancy rows.

`marts_agent` reads `marts_clickliv_sample.v_overcount` and gets 2,710. The same user on
`clickliv_sample.minute_occupancy` is refused with code 497, so the copy inherits the
least-privilege posture rather than opening a second way in.

Storage: 6.41 MiB on disk for the copy against 7.40 MiB for `clickliv`, both from 175.05
MiB uncompressed. The copy is the smaller of the two because it was written in one pass
and has fewer parts to merge.

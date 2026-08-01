# The unseen day

The sealed dataset lands, you run one command, you commit what it produced. This page
is the whole procedure. Read it top to bottom once; at 3am read only the boxed commands.

## The one command

```sh
make unseen RAW=data/final-raw.csv CONTENT=data/final-content.csv
```

It preflights both files, moves the serving tables aside, ingests, runs the full
pipeline, proves the four gates, emits the benchmark answers, the latencies, the
pipeline evidence and a comparison table against the tuning run, then prints every file
it wrote with its byte size.

With no `DB` argument it builds into whatever `CH_DATABASE` says, which is the primary
`clickliv`. That is deliberate and it is the production path: the Vercel dashboard, the
MCP server and the ClickHouse Cloud dashboard all read `clickliv` and `marts`, so
building the final data anywhere else leaves every live surface serving the sample data.
The final dataset is the single source of truth, and the two datasets are never mixed:
the run replaces `clickliv` wholesale and rebuilds `marts` over it.

Outputs land under `unseen/`, so the committed tuning-data run in `answers/`,
`evidence/` and `submission/` is never touched.

Options, all optional:

| Variable | Meaning | Default |
| --- | --- | --- |
| `OUT=somewhere` | output root | `unseen` |
| `DB=name` | ClickHouse database to build in, created if absent | whatever `CH_DATABASE` says |
| `CSV_RENAME=theirs=ours,...` | map a renamed column back | none |

Two warnings about `DB=`. It builds a scratch database for rehearsal only. The answers
step reads the `marts` database by name, so a scratch run's `benchmark_answers.csv`
comes from whatever `marts` currently points at rather than from the scratch tables.
Rehearse with it, never read answers off it.

The target reads `.env` for the server, exactly like every other target. Nothing else
needs editing.

## Before you start, 90 seconds

```sh
make ping                                          # names the host and database
make preflight RAW=<events> CONTENT=<content>      # reads the files, touches nothing
```

`make ping` prints the host and database, so you find out you are pointed at the wrong
service before you drop tables rather than after. `make preflight` is read only and is
the same check the run starts with, so you can run it the moment the files land, before
you are ready to swap anything.

## What each stage should say

The run prints `===== stage =====` before every stage and stops at the first failure,
naming it. Watch for these lines.

**preflight.** Everything knowable about the new files while the live tables are still
up. It reads both files start to finish and prints the shape, the span, the dimension
values, the vocabulary and the heartbeat cadence, then either

```
preflight OK, nothing about this file violates an assumption
```

or a list of `FAIL` lines and a non zero exit, having touched nothing. It fails on: a
missing required column, rows with fewer fields than the header, a non integer
`content_id`, `event_timestamp` or `session_start_epoch`, a timestamp outside the
millisecond epoch range (seconds where milliseconds belong puts every row in 1970), an
event `content_id` the content file does not carry, an event vocabulary the sessionizer
would not recognise at all, and a heartbeat cadence the grace cannot cover. It warns,
without failing, about new dimension values, benchmark slices whose values never appear
in the file, sessions carrying more than one `content_id`, and duplicated rows.

The cadence line is the one to read twice:

```
cadence   p50 40.000s  p90 40.000s  p99 60.000s  mode 40s x507  (517 gaps ...)
```

`GRACE_SECONDS` credits a session for that long after each heartbeat. If the sealed data
beats every 60 seconds and the grace is 40, every session loses 20 seconds between
beats, sessions fragment, and the peak collapses into a number that looks plausible and
is wrong. Preflight fails when the observed p90 gap exceeds the grace, and when
`GAP_SECONDS` is at or below the p90 gap. The recovery is to re-derive the pair rather
than to guess:

```sh
make sweep                       # sweeps gap and grace, reports peak sensitivity
# then set GAP_SECONDS and GRACE_SECONDS in .env and run make unseen again
```

**snapshot.** Renames the six serving tables to `<name>__prev` instead of dropping them.
Metadata only, so it costs about four seconds against Cloud whatever the row count.

**load.** Two lines describing the files as read, then the row counts, then the
reconcile table:

```
final-content.csv                  4 columns
final-raw.csv                     13 columns
content_meta         33,463 rows    0.4s
raw_events          905,558 rows    1.9s
```

Row counts differing from the tuning data are expected and are printed as
`differs (expected on a new day)`. Only two things fail here: `join_orphans` non zero,
meaning some `content_id` in the events is absent from the content file, and nothing
loading at all.

**sessionize, occupancy, deltas.** Row counts, and a vocabulary check that aborts the
run rather than letting it produce zeros. If no session is ever active, or fewer than
half of them are, the stage stops and prints every `event_type` and `event` value the
file actually contains with its row count, marks which ones the sessionizer recognises,
and lists the recognised tokens that are absent. A rename reads straight off that table:
`PlaybackStart 25 no` sitting next to `recognised event_type values absent from this
file: VideoPlay` is the whole diagnosis. The half is a heuristic bound, chosen well
below the 99.97% of sessions that are active in the tuning data and the 99.95% in its
busiest single day. Passing it means the vocabulary was recognised, not that the answers
are right. Gate A is what checks the answers.

**verify.** Gate A, twelve checks, all PASS. This is the important one: it diffs the
ClickHouse result against an independent pure Python recomputation of the same day,
interval by interval and rollup row by rollup row. If Gate A passes, the answers are
right or both implementations are wrong in the same way.

**incremental.** Writes `unseen/evidence/incremental_update.txt`. On a day that ends
with sessions still open it proves those sessions absorb a new heartbeat through the
materialized view with no rebuild, then rebuilds in full and confirms the two agree to
the millisecond. On a day where every session is closed it says so and moves on.

**answers.** The eight benchmark rows and their `query_id` values.

**submission.** The bundle plus the measured serving SLO. A missed SLO prints a warning
and does not fail the run; the bundle is still complete.

**the comparison table.** The last thing printed, and also written to
`unseen/answers/comparison.md`: every headline number from the tuning run beside the
same number from the sealed run, same names, same order, same units, ready to paste into
the README as is.

## If the run dies midway

The serving tables are not dropped, they are renamed. Whatever stage failed, this puts
the demo back:

```sh
make rollback
```

It renames every `__prev` table back, recreates the dictionary, and rebuilds `marts`
over the restored tables. Measured against ClickHouse Cloud with the tuning data loaded:
snapshot 4.4s, rollback 7.8s, after which `raw_events` was back at 905,558 rows and
`marts.v_concurrency` answered 2,692 again. That is the number that decides the
question: rolling back is seconds, so the swap can be run live.

A failure in preflight needs no rollback at all. Nothing was touched, the demo is still
serving what it served before, and the run says so.

The slow path exists only if both the new data and the `__prev` tables are gone:
`data/` is gitignored, so `make data` refetches the sample CSVs and `make all` followed
by `make marts` rebuilds the demo from them. Budget a few minutes against Cloud for
that, against seconds for `make rollback`. Prefer the rollback.

`__prev` tables survive a successful run on purpose. The next snapshot drops them, so
they never accumulate past one generation.

## Wall clock

| Input | `make unseen` |
| --- | --- |
| 751 events, 27 sessions, local Docker | 3 s |
| 751 events, 27 sessions, Cloud, ingest through Gate A | 21 s |
| 905,558 events, 10,866 sessions, local Docker (`make all` portion) | 11 s |

Against ClickHouse Cloud, budget a few minutes for a tuning-sized day: the queries are
the same but each one pays a network round trip, and the first query after an idle
period pays a wake up. If it has not finished in fifteen minutes something is wrong,
not slow.

## If the CSV is not the shape we expect

The loader reads the real header and builds the input schema from it, so most shape
changes need nothing from you. Every row in this table is covered by a test in
`tests/test_unseen_formats.py`, and the container rows were each run end to end through
`make unseen` and produced byte-identical answers.

| What changed | What to do |
| --- | --- |
| Extra columns | Nothing. They are declared and ignored, and the load line names them. |
| Columns in a different order | Nothing. Position is taken from the header. |
| A different delimiter (`;`, tab, `\|`) | Nothing. It is detected from the header line. |
| A byte order mark | Nothing. The file is read as utf-8-sig, so the mark never becomes part of the first column name. |
| CRLF line endings | Nothing. |
| Quoted fields holding commas or newlines | Nothing. Both the loader and the Python reference parse them as one field. |
| A header in different case, `Country` or `COUNTRY` | Nothing. Names bind case insensitively. |
| Gzip (`.csv.gz`) | Nothing. It is streamed to the server compressed and read through gzip here. |
| Zip, tar, `.tar.gz`, `.tgz`, bzip2, zstd | Nothing. The archive is unpacked once into the temp directory and read from there, and the run prints where it put it. A single data file per archive; two stops the run rather than guessing. macOS `__MACOSX` junk is ignored. |
| Zstd specifically | Needs a `zstd` binary on PATH, which is the one format Python cannot open by itself. Without it the run stops and tells you to `brew install zstd`. |
| A renamed column | `make unseen ... CSV_RENAME=geo=country`. Comma separate several. The error message lists the header it found, so you can read the right names straight off it. |
| A genuinely missing column | The run stops before touching the server. Either map some other column onto it with `CSV_RENAME`, or add the column to the file. Do not delete the check: a missing column used to load as an empty string and quietly change every answer that slices on it. |
| Two files rather than one events file | Concatenate them, keeping one header: `{ cat a.csv; tail -n +2 b.csv; } > merged.csv`. |

The required columns are exactly these thirteen:

```
content_id  video_session_id  user_id  event_type  event  event_timestamp
platform  app_version  country  audio_language  subtitle_language
player_version  session_start_epoch
```

and for the content file: `content_id  title  video_type  category`.

## If a stage fails

| Symptom | Cause | Fix |
| --- | --- | --- |
| `preflight found N problem(s)` | the file breaks an assumption | read the FAIL lines; nothing was touched |
| `heartbeat cadence p90 is ...` | the sealed data beats slower than `GRACE_SECONDS` | `make sweep`, then set `GAP_SECONDS` and `GRACE_SECONDS` in `.env` |
| `is missing required column(s)` | header mismatch | `CSV_RENAME`, see above |
| `holds N data files` | the archive carries more than one CSV | unpack it and pass the one you want |
| `join_orphans` non zero | the content file does not cover every `content_id` in the events | reload with the right content file; the events load is refused rather than silently unlabelled |
| `FAIL sessionize produced 0 active interval(s)` | an event token was renamed, so nothing ever counts as playing | read the table the failure prints. Add the new token to the `multiIf` in `sql/02_sessionize.sql` and to `classify()` in `src/clickliv/reference.py`, keeping the two identical, then re run. Both sides of Gate A have to agree, so changing only one is worse than changing neither. |
| `FAIL ... N of M sessions (x%) are ever active` | some but not all of the vocabulary was renamed | same fix, same table |
| `minute_occupancy is empty` | nothing became active | same as above |
| Gate A FAIL | ClickHouse and the Python reference disagree | do not ship the answers. The failing check names which side has the extra rows. |
| A timeout or a wake up error against Cloud | the service was idle | run `make ping` once, then re run |
| Anything at all, after snapshot | whatever it was | `make rollback` restores the demo in seconds, then debug |

## What to commit

```sh
git add unseen/answers unseen/submission unseen/evidence
git commit -m "feat: answers and pipeline evidence for the sealed dataset"
```

`unseen/artifacts/` holds the Python reference tables Gate A diffs against. Commit them
too if they are small, skip them if they are not; the manifest already carries a
SHA-256 for every file in the bundle. `unseen/answers/comparison.md` is the table for
the README.

## Rehearsing it

```sh
make unseen-fixture
make unseen RAW=fixtures/unseen_events.csv CONTENT=fixtures/unseen_content.csv \
            OUT=/tmp/unseen-rehearsal DB=clickliv_rehearsal
```

`fixtures/unseen_events.csv` is a synthetic fresh day carrying every case the tuning
data lacks: sessions still open when the file ends, an `AppBackgrounded` that never
comes back to the foreground, a duplicated `VideoSessionStart`, a quarter of the
heartbeats repeating the millisecond before them, a session that switches `content_id`
halfway through, a heartbeat gap wide enough to be excluded, a country that is not
`india`, a platform never seen before, a `video_type` never seen before, and rows out of
timestamp order. Point `DB` at a throwaway database, never at the one holding the real
load, and read Gate A rather than the answers, for the reason in the options table above.

To rehearse the formats rather than the day:

```sh
make unseen-variants DIR=/tmp/variants
```

That writes the same fresh day as plain CSV, gzip, bzip2, zstd, zip and `.tar.gz`, plus
a deliberately hostile pair that is semicolon delimited, CRLF, byte order marked, column
reordered, carrying two columns we ignore and quoted fields holding a comma and a
newline, with `country` renamed to `geo` and two other names shouted. The command it
prints at the end runs that hostile pair end to end. It produces byte-identical answers
to the plain pair, which is the check that matters.

# The unseen day

The sealed dataset lands, you run one command, you commit what it produced. This page
is the whole procedure. Read it top to bottom once; at 3am read only the boxed commands.

## The one command

```sh
make unseen RAW=data/unseen-raw.csv CONTENT=data/unseen-content.csv
```

It ingests both files, runs the full pipeline, proves the four gates, emits the
benchmark answers, the latencies and the pipeline evidence, and prints every file it
wrote with its byte size. Everything lands under `unseen/`, so the committed
tuning-data run in `answers/`, `evidence/` and `submission/` is never touched.

Options, all optional:

| Variable | Meaning | Default |
| --- | --- | --- |
| `OUT=somewhere` | output root | `unseen` |
| `DB=name` | ClickHouse database to build in, created if absent | whatever `CH_DATABASE` says |
| `CSV_RENAME=theirs=ours,...` | map a renamed column back | none |

The target reads `.env` for the server, exactly like every other target. Nothing else
needs editing.

## Before you start, 60 seconds

```sh
make ping                      # names the host and database it will write to
head -1 data/unseen-raw.csv    # the header the loader will bind to
wc -l data/unseen-raw.csv
```

`make ping` is the one that matters. It prints the host and database, so you find out
you are pointed at the wrong service before you drop tables rather than after.

## What each stage should say

The run prints `===== stage =====` before every stage and stops at the first failure,
naming it. Watch for these lines.

**load.** Two lines describing the files as read, then the row counts, then the
reconcile table:

```
unseen-content.csv                4 columns
unseen-raw.csv                   13 columns
content_meta         33,463 rows    0.4s
raw_events          905,558 rows    1.9s
```

Row counts differing from the tuning data are expected and are printed as
`differs (expected on a new day)`. Only two things fail here: `join_orphans` non zero,
meaning some `content_id` in the events is absent from the content file, and nothing
loading at all. If the file has extra columns or a non comma delimiter the first two
lines say so explicitly. If a required column is absent the run stops immediately and
prints the header it actually found.

**sessionize, occupancy, deltas.** Row counts. `active_intervals` at zero means the
event vocabulary did not match; see the troubleshooting table below.

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

## Wall clock

Measured on this laptop against local Docker, ClickHouse 26.7:

| Input | `make unseen` |
| --- | --- |
| 688 events, 25 sessions | 2 s |
| 905,558 events, 10,866 sessions (`make all` portion) | 11 s |

Against ClickHouse Cloud, budget a few minutes for a tuning-sized day: the queries are
the same but each one pays a network round trip, and the first query after an idle
period pays a wake up. If it has not finished in fifteen minutes something is wrong,
not slow.

## If the CSV is not the shape we expect

The loader reads the real header and builds the input schema from it, so most shape
changes need nothing from you.

| What changed | What to do |
| --- | --- |
| Extra columns | Nothing. They are declared and ignored, and the load line names them. |
| Columns in a different order | Nothing. Position is taken from the header. |
| A different delimiter (`;`, tab, `\|`) | Nothing. It is detected from the header line. |
| Gzip (`.csv.gz`) | Nothing, as long as the name ends in `.gz`. Both the loader and the Python reference read it directly. |
| A renamed column | `make unseen ... CSV_RENAME=geo=country`. Comma separate several. The error message lists the header it found, so you can read the right names straight off it. |
| A genuinely missing column | The run stops before touching the server. Either map some other column onto it with `CSV_RENAME`, or add the column to the file. Do not delete the check: a missing column used to load as an empty string and quietly change every answer that slices on it. |
| Zip rather than gzip, or any other container | Unpack it first. Only `.gz` is handled in place. |
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
| `is missing required column(s)` | header mismatch | `CSV_RENAME`, see above |
| `join_orphans` non zero | the content file does not cover every `content_id` in the events | reload with the right content file; the events load is refused rather than silently unlabelled |
| `active_intervals 0 rows` | no event matched the play or foreground vocabulary | check the distinct `event_type` and `event` values in the new file against `sql/02_sessionize.sql` |
| `minute_occupancy is empty` | nothing became active | same as above |
| Gate A FAIL | ClickHouse and the Python reference disagree | do not ship the answers. The failing check names which side has the extra rows. |
| A timeout or a wake up error against Cloud | the service was idle | run `make ping` once, then re run |

## What to commit

```sh
git add unseen/answers unseen/submission unseen/evidence
git commit -m "feat: answers and pipeline evidence for the sealed dataset"
```

`unseen/artifacts/` holds the Python reference tables Gate A diffs against. Commit them
too if they are small, skip them if they are not; the manifest already carries a
SHA-256 for every file in the bundle.

## Rehearsing it

```sh
make unseen-fixture
make unseen RAW=fixtures/unseen_events.csv CONTENT=fixtures/unseen_content.csv \
            OUT=/tmp/unseen-rehearsal DB=clickliv_rehearsal
```

`fixtures/unseen_events.csv` is a synthetic fresh day carrying every case the tuning
data lacks: sessions still open when the file ends, an `AppBackgrounded` that never
comes back to the foreground, a duplicated `VideoSessionStart`, a heartbeat gap wide
enough to be excluded, a country that is not `india`, a platform never seen before, a
`video_type` never seen before, and rows out of timestamp order. Point `DB` at a
throwaway database, never at the one holding the real load.

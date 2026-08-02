ClickLiv submission bundle
==========================

The grading answer-file format was never published, so the same answers are emitted
in every plausible shape from one source of truth.

benchmark_answers.csv   one row per benchmark query.
benchmark_answers.json  the same rows, same column order, as a JSON array.
manifest.json           what produced these numbers: server version, row counts,
                        minute range, thresholds, git commit, and a SHA-256 plus byte
                        size for every other file here.
README.txt              this file.

Columns that carry the answer
-----------------------------

peak_concurrency        answers "peak": the maximum concurrent sessions seen in any
                        single minute inside the bucket.
average_concurrency     answers "average": session-minutes divided by minutes, that
                        is sum(average_concurrency * minutes_in_bucket) over
                        sum(minutes_in_bucket).
average_denominator     names the denominator explicitly. It is active minutes, not
                        wall-clock minutes: minute_occupancy stores no zero rows, so a
                        minute with no sessions is absent rather than present as a 0.
                        Dividing by the wall-clock span instead would give a smaller
                        number for the same data.
active_minutes          the denominator's value, so the average can be recomputed by hand.

grain_minutes 1440 is a day bucket, 60 an hour, 1 a single minute. Empty country,
platform and video_type mean unfiltered; content_id 0 means all content.

Latency
-------

Every latency reported anywhere in this project is server-side
system.query_log.query_duration_ms, looked up by the query_id the client generated
before sending the query. It is not client wall clock, so it excludes network round
trip and Python overhead. See evidence/serving_slo.txt and evidence/serving_slo.csv.

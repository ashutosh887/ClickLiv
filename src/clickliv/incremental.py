"""Proves the problem statement's update-handling scenario for real: "sessions that
are still open, whose active ranges keep growing as new heartbeats arrive." A new
heartbeat lands, mv_extend_open_session absorbs it with no rebuild, and the number it
predicts is then checked against a full batch resessionize, the same two-paths-must-
agree discipline as the rest of this project (D1). Restores the dataset afterward and
drops the demo objects it created, same leave-no-trace pattern as Gate C: this is a
self-contained proof, not a table left attached to raw_events for every future insert
to pay for.
"""

from __future__ import annotations

import os
from pathlib import Path

from .ch import ClickHouse


def escape(value: str) -> str:
    return value.replace("'", "''")


def pick_open_session(ch: ClickHouse) -> dict:
    row = ch.query("""
        SELECT video_session_id, argMax(segment_start_ms, version) AS segment_start_ms,
               argMax(last_active_ms, version) AS last_active_ms
        FROM open_session_state
        GROUP BY video_session_id
        ORDER BY video_session_id
        LIMIT 1
    """).dicts()
    if not row:
        raise SystemExit("no open sessions found; open_session_state is empty")
    return row[0]


def dims_for(ch: ClickHouse, session_id: str) -> dict:
    return ch.query(f"""
        SELECT user_id, content_id, platform, app_version, country,
               audio_language, subtitle_language, player_version, session_start
        FROM raw_events
        WHERE video_session_id = '{escape(session_id)}'
        ORDER BY event_time DESC
        LIMIT 1
    """).dicts()[0]


def insert_heartbeat(ch: ClickHouse, session_id: str, dims: dict, event_ms: int) -> None:
    session_start = dims["session_start"]
    ch.command(f"""
        INSERT INTO raw_events
        (video_session_id, event_time, user_id, content_id, event_type, event,
         platform, app_version, country, audio_language, subtitle_language,
         player_version, session_start)
        VALUES
        ('{escape(session_id)}', fromUnixTimestamp64Milli({event_ms}), '{escape(dims['user_id'])}',
         {dims['content_id']}, 'VideoHeartbeat', 'network-activity',
         '{escape(dims['platform'])}', '{escape(dims['app_version'])}', '{escape(dims['country'])}',
         '{escape(dims['audio_language'])}', '{escape(dims['subtitle_language'])}',
         '{escape(dims['player_version'])}', '{session_start}')
    """)


def live_state(ch: ClickHouse, session_id: str) -> dict:
    return ch.query(f"""
        SELECT argMax(segment_start_ms, version) AS segment_start_ms,
               argMax(last_active_ms, version) AS last_active_ms
        FROM open_session_state
        WHERE video_session_id = '{escape(session_id)}'
    """).dicts()[0]


def batch_segment_end(ch: ClickHouse, session_id: str) -> int:
    return int(ch.scalar(f"""
        SELECT ts_end_ms FROM active_intervals
        WHERE video_session_id = '{escape(session_id)}'
        ORDER BY segment_id DESC LIMIT 1
    """))


def run(ch: ClickHouse, evidence: Path) -> bool:
    from . import cli

    cli.run_sql_file(ch, "08_incremental.sql")

    before = pick_open_session(ch)
    session_id = before["video_session_id"]
    dims = dims_for(ch, session_id)

    gap_ms = int(os.environ["GAP_SECONDS"]) * 1000
    grace_ms = int(os.environ["GRACE_SECONDS"]) * 1000
    new_event_ms = int(before["last_active_ms"]) + gap_ms // 3

    insert_heartbeat(ch, session_id, dims, new_event_ms)
    after = live_state(ch, session_id)
    live_ok = int(after["last_active_ms"]) == new_event_ms

    try:
        cli.STEPS["sessionize"](ch)
        cli.STEPS["occupancy"](ch)
        batch_end = batch_segment_end(ch, session_id)
        predicted_end = new_event_ms + grace_ms
        agree = batch_end == predicted_end

        lines = [
            "-- update handling: an open session absorbs a new heartbeat live\n",
            f"session {session_id}\n",
            f"before: segment_start_ms={before['segment_start_ms']} "
            f"last_active_ms={before['last_active_ms']}\n",
            f"inserted one new heartbeat at t={new_event_ms} "
            f"(last_active_ms + {gap_ms // 3}ms, inside the {gap_ms}ms gap threshold)\n\n",
            f"mv_extend_open_session fired with no rebuild: "
            f"open_session_state.last_active_ms is now {after['last_active_ms']} "
            f"({'matches' if live_ok else 'does NOT match'} the new event time)\n\n",
            f"full batch resessionize (ground truth): active_intervals' last segment "
            f"for this session now ends at {batch_end}\n",
            f"incremental prediction was {predicted_end} (new event + {grace_ms}ms grace)\n",
            f"{'PASS' if agree and live_ok else 'FAIL'}: incremental and batch "
            f"{'agree exactly' if agree else 'DISAGREE'}\n",
        ]
        (evidence / "incremental_update.txt").write_text("".join(lines))
        print(f"evidence/incremental_update.txt   "
              f"{'PASS' if agree and live_ok else 'FAIL'}, session {session_id[:16]}...")
        ok = live_ok and agree
    finally:
        cli.step_all(ch)
        cli.STEPS["marts"](ch)
        ch.command("DROP VIEW IF EXISTS mv_extend_open_session")
        ch.command("DROP TABLE IF EXISTS open_session_state")

    return ok

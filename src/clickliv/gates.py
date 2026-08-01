"""Gate B. Two consecutive rebuilds must produce byte-identical serving tables."""

from __future__ import annotations

from .ch import ClickHouse

SERVING = {
    "minute_occupancy": ("country", "platform", "video_type", "category", "app_version",
                         "player_version", "audio_language", "subtitle_language",
                         "content_id", "minute", "sessions"),
    "minute_deltas": ("country", "platform", "video_type", "category", "app_version",
                      "player_version", "audio_language", "subtitle_language",
                      "content_id", "minute", "delta"),
    "active_intervals": ("video_session_id", "segment_id", "ts_start_ms", "ts_end_ms"),
}


def fingerprint(ch: ClickHouse) -> dict[str, tuple[int, int]]:
    """Row count plus an order-independent hash, so part layout cannot affect the result."""
    out = {}
    for table, columns in SERVING.items():
        row = ch.query(f"""
            SELECT count(), groupBitXor(cityHash64({', '.join(columns)}))
            FROM {table}
        """).rows[0]
        out[table] = (int(row[0]), int(row[1]))
    return out


def compare(first: dict, second: dict) -> bool:
    width = max(len(name) for name in first)
    ok = True
    print()
    for table in first:
        same = first[table] == second[table]
        ok &= same
        rows, digest = second[table]
        print(f"{'PASS' if same else 'FAIL'}  {table:<{width}}  {rows:>9,} rows  "
              f"hash {digest:016x}" + ("" if same else f"  was {first[table][1]:016x}"))
    print(f"\nGate B: {'PASS' if ok else 'FAIL'}  rebuild is idempotent")
    return ok

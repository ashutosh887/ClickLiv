"""Read every published headline number live, and say which docs still state an old one."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .ch import ClickHouse

BASELINE = Path("artifacts/claims_baseline.json")

DOCS = ["README.md", "docs", "localdocs/script.md", "web/index.html"]


def marts_db(database: str) -> str:
    return "marts" if database == "clickliv" else f"marts_{database}"


def measure(ch: ClickHouse) -> dict:
    marts = marts_db(ch.config.database)
    out: dict[str, float | int | str] = {}

    row = ch.query(f"SELECT * FROM {marts}.v_overcount").dicts()[0]
    out["foreground_peak"] = row["foreground_peak"]
    out["foreground_peak_utc"] = str(row["foreground_peak_utc"])
    out["naive_peak"] = row["naive_peak"]
    out["naive_peak_utc"] = str(row["naive_peak_utc"])
    out["peak_overcount_pct"] = round(float(row["peak_overcount_pct"]), 1)
    out["average_overcount_pct"] = round(float(row["average_overcount_pct"]), 1)
    out["foreground_average"] = round(float(row["foreground_average"]), 1)

    window = ch.query(f"SELECT * FROM {marts}.v_data_window").dicts()[0]
    out["min_utc"] = str(window["min_utc"])
    out["max_utc"] = str(window["max_utc"])
    out["span_days"] = round(float(window["span_days"]), 2)
    out["minutes_with_sessions"] = window["minutes_with_sessions"]
    out["occupancy_rows"] = window["occupancy_rows"]

    out["raw_events"] = ch.scalar("SELECT count() FROM raw_events")
    out["content_rows"] = ch.scalar("SELECT count() FROM content_meta")
    out["active_intervals"] = ch.scalar("SELECT count() FROM active_intervals")
    out["sessions"] = ch.scalar("SELECT uniqExact(video_session_id) FROM raw_events")

    for dim in ("country", "platform", "video_type", "category",
                "audio_language", "subtitle_language"):
        out[f"distinct_{dim}"] = ch.scalar(
            f"SELECT count() FROM {marts}.dimension_value WHERE dimension = '{dim}'")

    window_args = (f"minute_from = {window['min_minute']}, "
                   f"minute_to = {window['max_minute']}")
    for label, dim, value in (("peak_hin", "audio_language", "hin"),
                              ("peak_live", "video_type", "live"),
                              ("peak_vod", "video_type", "vod")):
        args = ", ".join(
            f"{d} = '{value if d == dim else ''}'"
            for d in ("country", "platform", "video_type", "category", "app_version",
                      "player_version", "audio_language", "subtitle_language"))
        out[label] = ch.scalar(
            f"SELECT max(concurrency) FROM {marts}.v_occupancy_full("
            f"{args}, content_id = 0, {window_args})")

    return out


def literals(value) -> list[str]:
    """Every spelling a doc might use for one measured value."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, float):
        text = f"{value:,.1f}".rstrip("0").rstrip(".")
        return sorted({text, text.replace(",", ""), f"{value:,.0f}", f"{value:.0f}"})
    return sorted({f"{value:,}", str(value)})


def doc_files() -> list[Path]:
    out: list[Path] = []
    for entry in DOCS:
        path = Path(entry)
        if path.is_dir():
            out.extend(sorted(path.rglob("*.md")))
        elif path.exists():
            out.append(path)
    return out


def mentions(text_literals: list[str]) -> list[str]:
    hits = []
    for path in doc_files():
        body = path.read_text(encoding="utf-8", errors="replace")
        for literal in text_literals:
            if not literal:
                continue
            for match in re.finditer(re.escape(literal), body):
                line = body.count("\n", 0, match.start()) + 1
                hits.append(f"{path}:{line}")
                break
    return sorted(set(hits))


def run(ch: ClickHouse, update: bool = False) -> int:
    live = measure(ch)
    baseline = json.loads(BASELINE.read_text()) if BASELINE.exists() else {}

    width = max(len(k) for k in live)
    moved, unstated = [], []

    print(f"claims measured against {ch.config.host} db={ch.config.database}\n")
    for key, value in live.items():
        was = baseline.get(key)
        stale = mentions(literals(was)) if was is not None and was != value else []
        stated = mentions(literals(value))
        if was is not None and was != value:
            moved.append((key, was, value, stale))
            flag = "MOVED"
        elif not stated:
            unstated.append(key)
            flag = "not in docs"
        else:
            flag = f"{len(stated)} doc refs"
        print(f"  {key:<{width}}  {str(value):>22}   {flag}")

    if moved:
        print("\nnumbers that moved, with the docs still stating the old value:\n")
        for key, was, now, stale in moved:
            print(f"  {key}: {was} -> {now}")
            for hit in stale:
                print(f"      {hit}")
            if not stale:
                print("      no doc states the old value")

    if update or not baseline:
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(json.dumps(live, indent=2, sort_keys=True) + "\n")
        print(f"\nbaseline written to {BASELINE}")

    return 1 if moved else 0

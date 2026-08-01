"""Gate C. A held-out single-day dry run of the whole pipeline, standing in for the
unseen day (O8). Whatever breaks here is what would have broken on the real thing.
"""

from __future__ import annotations

import csv
import os
from pathlib import Path

from . import cli
from . import gates
from . import load as loader
from .ch import ClickHouse

DAY_MS = 86_400_000
SLICE_CSV = Path("artifacts/gate_c/raw_events.csv")
GATE_C_ARTIFACTS = Path("artifacts/gate_c")
GATE_C_ANSWERS = Path("answers/gate_c")
GATE_C_EVIDENCE = Path("evidence/gate_c")

STEP_ORDER = ("schema", "load", "sessionize", "occupancy", "deltas", "reference", "verify")


def held_out_day(raw_csv: Path) -> int:
    """The last calendar day in the tuning data, standing in for the day that has not landed yet."""
    latest = None
    with loader.open_text(raw_csv) as fh:
        for row in csv.DictReader(fh):
            day = int(row["event_timestamp"]) // DAY_MS
            if latest is None or day > latest:
                latest = day
    if latest is None:
        raise SystemExit(f"{raw_csv} has no rows to hold out a day from")
    return latest


def split_day(raw_csv: Path, day: int, out_path: Path) -> int:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with loader.open_text(raw_csv) as fh, out_path.open("w", newline="") as out:
        reader = csv.reader(fh)
        writer = csv.writer(out)
        header = next(reader)
        writer.writerow(header)
        ts_idx = header.index("event_timestamp")
        for row in reader:
            if int(row[ts_idx]) // DAY_MS == day:
                writer.writerow(row)
                written += 1
    return written


def run(ch: ClickHouse) -> bool:
    from . import answers
    from . import chdb_engine
    from . import projections

    raw_csv = loader.raw_csv()
    day = held_out_day(raw_csv)
    rows = split_day(raw_csv, day, SLICE_CSV)
    print(f"Gate C: held-out day {day}, {rows:,} events, standing in for the unseen day\n")

    saved = {key: os.environ.get(key) for key in ("RAW_CSV", "ARTIFACTS")}
    os.environ["RAW_CSV"] = str(SLICE_CSV)
    os.environ["ARTIFACTS"] = str(GATE_C_ARTIFACTS)
    GATE_C_EVIDENCE.mkdir(parents=True, exist_ok=True)

    try:
        for name in STEP_ORDER:
            if name == "load":
                loader.load(ch)
                continue
            if cli.STEPS[name](ch):
                print(f"\nGate C: FAIL  step {name} did not complete")
                return False

        before = gates.fingerprint(ch)
        for name in ("sessionize", "occupancy", "deltas"):
            cli.STEPS[name](ch)
        ok = gates.compare(before, gates.fingerprint(ch),
                            "Gate C: rebuild is idempotent on the held-out day")

        cli.STEPS["marts"](ch)
        answers.run(ch, GATE_C_ARTIFACTS, GATE_C_ANSWERS, GATE_C_EVIDENCE)

        cli.run_sql_file(ch, "07_projections.sql")
        projections.run(ch, GATE_C_EVIDENCE)

        ok &= chdb_engine.run(ch, cli.render, cli.SQL_DIR, GATE_C_ARTIFACTS)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    print(f"\nGate C: {'PASS' if ok else 'FAIL'}  held-out day {day} ran schema through "
          f"chDB with no code path special-cased for a single day")
    return ok

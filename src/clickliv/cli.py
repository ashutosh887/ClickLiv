"""Command dispatch. Every subcommand runs identically against local Docker and Cloud."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from . import otel
from .ch import ClickHouse

SQL_DIR = Path(__file__).resolve().parents[2] / "sql"

DEFAULTS = {
    "CH_HOST": "localhost",
    "CH_PORT": "8123",
    "CH_USER": "clickliv",
    "CH_PASSWORD": "clickliv",
    "CH_DATABASE": "clickliv",
    "CH_SECURE": "0",
    "GAP_SECONDS": "90",
    "GRACE_SECONDS": "40",
}

PIPELINE = ("schema", "load", "sessionize", "occupancy", "deltas")


def load_dotenv(path: str = ".env") -> None:
    p = Path(path)
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())
    for key, value in DEFAULTS.items():
        os.environ.setdefault(key, value)


def render(sql: str) -> str:
    """Substitute ${VAR} from the environment so one SQL file serves local, Cloud and the sweep."""
    def sub(match: re.Match) -> str:
        key = match.group(1)
        if key not in os.environ:
            raise SystemExit(f"{key} is referenced in SQL but not set")
        return os.environ[key]
    return re.sub(r"\$\{(\w+)\}", sub, sql)


def artifacts_dir() -> Path:
    return Path(os.environ.get("ARTIFACTS", "artifacts"))


def run_sql_file(ch: ClickHouse, name: str) -> None:
    print(f"-- {name}")
    ch.script(render((SQL_DIR / name).read_text()))


def counts(ch: ClickHouse, *tables: str) -> None:
    for table in tables:
        print(f"{table:<18}{int(ch.scalar(f'SELECT count() FROM {table}')):>12,} rows")


def step_schema(ch: ClickHouse) -> int:
    run_sql_file(ch, "01_schema.sql")
    return 0


def step_load(ch: ClickHouse) -> int:
    from . import load as loader
    loader.load(ch)
    return 0 if loader.reconcile(ch) else 1


def step_sessionize(ch: ClickHouse) -> int:
    run_sql_file(ch, "02_sessionize.sql")
    counts(ch, "active_intervals")
    return 0


def step_occupancy(ch: ClickHouse) -> int:
    run_sql_file(ch, "03_occupancy.sql")
    counts(ch, "session_minutes", "minute_occupancy")
    return 0


def step_deltas(ch: ClickHouse) -> int:
    run_sql_file(ch, "04_deltas.sql")
    counts(ch, "minute_deltas")
    return 0


def step_reference(ch: ClickHouse) -> int:
    from . import load as loader
    from . import reference
    reference.write(reference.build(loader.raw_csv(), loader.content_csv()), artifacts_dir())
    return 0


def step_verify(ch: ClickHouse) -> int:
    from . import verify
    run_sql_file(ch, "05_oracles.sql")
    verify.load_reference_tables(ch, artifacts_dir())
    return 0 if verify.run(ch, artifacts_dir()) else 1


def step_reconcile(ch: ClickHouse) -> int:
    from . import load as loader
    return 0 if loader.reconcile(ch) else 1


def step_ping(ch: ClickHouse) -> int:
    print(f"clickhouse {ch.ping()} at {ch.config.host}:{ch.config.port} "
          f"db={ch.config.database}")
    return 0


def run_step(ch: ClickHouse, name: str) -> int:
    with otel.span(f"stage.{name}"):
        return STEPS[name](ch)


def step_pipeline(ch: ClickHouse) -> int:
    for name in PIPELINE:
        status = run_step(ch, name)
        if status:
            return status
    return 0


def step_all(ch: ClickHouse) -> int:
    return step_pipeline(ch) or run_step(ch, "reference") or run_step(ch, "verify")


def step_gate_b(ch: ClickHouse) -> int:
    from . import gates
    before = gates.fingerprint(ch)
    status = step_pipeline(ch)
    if status:
        return status
    return 0 if gates.compare(before, gates.fingerprint(ch)) else 1


def step_sweep(ch: ClickHouse) -> int:
    from . import sweep
    sweep.run(ch, artifacts_dir(), step_sessionize)
    return 0


def step_chdb(ch: ClickHouse) -> int:
    from . import chdb_engine
    return 0 if chdb_engine.run(ch, render, SQL_DIR, artifacts_dir()) else 1


def step_obs(ch: ClickHouse) -> int:
    from . import observe
    return observe.report()


def step_reset(ch: ClickHouse) -> int:
    ch.command("DROP DICTIONARY IF EXISTS content_dict")
    for table in ("raw_events", "content_meta", "active_intervals", "session_minutes",
                  "minute_occupancy", "minute_deltas", "ref_intervals", "ref_rollup"):
        ch.command(f"DROP TABLE IF EXISTS {table}")
    print("dropped")
    return 0


STEPS = {
    "ping": step_ping,
    "schema": step_schema,
    "load": step_load,
    "reconcile": step_reconcile,
    "sessionize": step_sessionize,
    "occupancy": step_occupancy,
    "deltas": step_deltas,
    "reference": step_reference,
    "verify": step_verify,
    "pipeline": step_pipeline,
    "all": step_all,
    "gate-b": step_gate_b,
    "sweep": step_sweep,
    "chdb": step_chdb,
    "obs": step_obs,
    "reset": step_reset,
}


def main(argv: list[str]) -> int:
    load_dotenv()
    if not argv:
        print(f"commands: {', '.join(STEPS)}, sql")
        return 2
    command, args = argv[0], argv[1:]
    ch = ClickHouse()

    if command == "sql":
        result = ch.query(" ".join(args))
        print("\t".join(result.columns))
        for row in result.rows:
            print("\t".join(str(v) for v in row))
        return 0

    if command not in STEPS:
        print(f"unknown command: {command}")
        return 2

    otel.TRACER = otel.Tracer(os.environ.get("CLICKSTACK_OTLP"),
                              os.environ.get("CLICKSTACK_KEY"))
    otel.TRACER.attach(ch)
    try:
        with otel.span(f"clickliv.{command}"):
            return STEPS[command](ch)
    finally:
        otel.TRACER.export(ch)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

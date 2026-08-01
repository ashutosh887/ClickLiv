"""Command dispatch. Every subcommand runs identically against local Docker and Cloud."""

from __future__ import annotations

import os
import re
import sys
import time
from contextlib import nullcontext
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

SERVERS = ("mcp", "ui")

REPLAY = ("reset", "schema", "load", "sessionize", "occupancy", "deltas", "reference",
          "verify", "marts", "projections", "answers", "instantaneous", "submission")

UNSEEN = ("reset", "schema", "load", "sessionize", "occupancy", "deltas", "reference",
          "verify", "incremental", "marts", "projections", "answers", "instantaneous",
          "submission")

UNSEEN_ROOTS = (("ARTIFACTS", "artifacts"), ("ANSWERS", "answers"),
                ("EVIDENCE", "evidence"), ("SUBMISSION", "submission"))


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


def out_dir(key: str, default: str) -> Path:
    """Every output root is redirectable, so an unseen day never overwrites the
    committed tuning-data run."""
    path = Path(os.environ.get(key, default))
    path.mkdir(parents=True, exist_ok=True)
    return path


def artifacts_dir() -> Path:
    return out_dir("ARTIFACTS", "artifacts")


def answers_dir() -> Path:
    return out_dir("ANSWERS", "answers")


def evidence_dir() -> Path:
    return out_dir("EVIDENCE", "evidence")


def submission_dir() -> Path:
    return out_dir("SUBMISSION", "submission")


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


def step_gate_c(ch: ClickHouse) -> int:
    from . import gate_c
    try:
        ok = gate_c.run(ch)
    finally:
        print("\nrestoring the full dataset after the held-out dry run")
        status = step_all(ch)
        if status == 0:
            status = run_step(ch, "marts")
    return 0 if ok and status == 0 else 1


def step_sweep(ch: ClickHouse) -> int:
    from . import sweep
    sweep.run(ch, artifacts_dir(), step_sessionize)
    return 0


def step_chdb(ch: ClickHouse) -> int:
    from . import chdb_engine
    return 0 if chdb_engine.run(ch, render, SQL_DIR, artifacts_dir()) else 1


def step_marts(ch: ClickHouse) -> int:
    run_sql_file(ch, "06_marts.sql")
    return 0


def step_answers(ch: ClickHouse) -> int:
    from . import answers
    return 0 if answers.run(ch, artifacts_dir(), answers_dir(), evidence_dir()) else 1


def step_projections(ch: ClickHouse) -> int:
    from . import projections
    run_sql_file(ch, "07_projections.sql")
    return 0 if projections.run(ch, evidence_dir()) else 1


def step_scale(ch: ClickHouse) -> int:
    from . import scale
    return 0 if scale.run(ch, artifacts_dir() / "scale", evidence_dir()) else 1


def step_ui(ch: ClickHouse) -> int:
    from . import ui
    ui.run(ch, port=int(os.environ.get("UI_PORT", "8090")))
    return 0


def step_userlevel(ch: ClickHouse) -> int:
    from . import userlevel
    return 0 if userlevel.run(ch, evidence_dir()) else 1


def step_crossover(ch: ClickHouse) -> int:
    from . import crossover
    return 0 if crossover.run(ch, evidence_dir()) else 1


def step_decline(ch: ClickHouse) -> int:
    from . import decline
    return 0 if decline.run(ch, evidence_dir()) else 1


def step_incremental(ch: ClickHouse) -> int:
    from . import incremental
    return 0 if incremental.run(ch, evidence_dir()) else 1


def step_instantaneous(ch: ClickHouse) -> int:
    from . import instantaneous
    return 0 if instantaneous.run(ch, evidence_dir()) else 1


def step_submission(ch: ClickHouse) -> int:
    from . import submission
    return 0 if submission.run(ch, artifacts_dir(), submission_dir(),
                               evidence_dir()) else 1


def step_replay(ch: ClickHouse) -> int:
    """The graded run: one command from a fresh CSV to a submission bundle."""
    started = time.time()
    for name in REPLAY:
        print(f"\n===== {name} =====")
        status = run_step(ch, name)
        if status:
            print(f"\nreplay FAILED at {name}")
            return status
    print(f"\nreplay complete in {time.time() - started:.0f}s")
    return 0


def step_unseen(ch: ClickHouse) -> int:
    """The sealed-dataset run: a fresh pair of CSVs to answers, latencies and evidence,
    in one command, into an output root of its own."""
    from . import load as loader

    root = Path(os.environ.get("UNSEEN_DIR", "unseen"))
    for key, name in UNSEEN_ROOTS:
        os.environ[key] = str(root / name)

    print(f"raw      {loader.raw_csv()}")
    print(f"content  {loader.content_csv()}")
    print(f"target   {ch.config.host}:{ch.config.port} database {ch.config.database}")
    print(f"output   {root}/\n")

    ch.command(f"CREATE DATABASE IF NOT EXISTS {ch.config.database}", database="")

    started = time.time()
    slo = 0
    for name in UNSEEN:
        print(f"\n===== {name} =====")
        status = run_step(ch, name)
        if status and name == "submission":
            slo = status
        elif status:
            print(f"\nunseen FAILED at {name}; nothing downstream of it was produced")
            return status

    print(f"\n===== produced in {time.time() - started:.0f}s =====")
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        print(f"{str(path):<52}{path.stat().st_size:>10,} bytes")
    print(f"\nanswers  {root}/answers/benchmark_answers.csv")
    print(f"bundle   {root}/submission/  (csv, json, manifest, README)")
    print(f"evidence {root}/evidence/    (query_log, latencies, explain, oracles, "
          f"incremental update)")
    if slo:
        print("\nthe self-imposed serving SLO was missed; the bundle is complete and the "
              "measured numbers are in the evidence")
    return 0


def step_mcp(ch: ClickHouse) -> int:
    from . import mcp
    mcp.serve(ch, port=int(os.environ.get("MCP_PORT", "8765")))
    return 0


def step_obs(ch: ClickHouse) -> int:
    from . import observe
    return observe.report()


def step_reset(ch: ClickHouse) -> int:
    ch.command("DROP DATABASE IF EXISTS marts")
    ch.command("DROP USER IF EXISTS marts_agent")
    ch.command("DROP ROLE IF EXISTS marts_readonly")
    ch.command("DROP SETTINGS PROFILE IF EXISTS marts_budget")
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
    "gate-c": step_gate_c,
    "sweep": step_sweep,
    "chdb": step_chdb,
    "marts": step_marts,
    "answers": step_answers,
    "projections": step_projections,
    "scale": step_scale,
    "ui": step_ui,
    "userlevel": step_userlevel,
    "crossover": step_crossover,
    "decline": step_decline,
    "incremental": step_incremental,
    "instantaneous": step_instantaneous,
    "submission": step_submission,
    "replay": step_replay,
    "unseen": step_unseen,
    "mcp": step_mcp,
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

    otel.TRACER = otel.Tracer(otel.sinks_from_env())
    otel.TRACER.attach(ch)
    scope = nullcontext() if command in SERVERS else otel.span(f"clickliv.{command}")
    try:
        with scope:
            return STEPS[command](ch)
    finally:
        otel.TRACER.export(ch)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

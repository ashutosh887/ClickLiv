"""Command dispatch. Every subcommand runs identically against local Docker and Cloud."""

from __future__ import annotations

import csv
import json
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

REPLAY = ("preflight", "snapshot", "reset", "schema", "load", "sessionize", "occupancy",
          "deltas", "reference", "verify", "marts", "projections", "answers",
          "instantaneous", "submission")

UNSEEN = ("preflight", "snapshot", "reset", "schema", "load", "sessionize", "occupancy",
          "deltas", "reference", "verify", "incremental", "marts", "projections",
          "answers", "instantaneous", "submission")

SERVING_TABLES = ("raw_events", "content_meta", "active_intervals", "session_minutes",
                  "minute_occupancy", "minute_deltas")

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


MIN_ACTIVE_SESSION_RATIO = 0.5

SESSIONIZE_SHAPE = """
    SELECT
        (SELECT count() FROM raw_events)                        AS events,
        (SELECT uniqExact(video_session_id) FROM raw_events)    AS sessions,
        (SELECT count() FROM active_intervals)                  AS intervals,
        (SELECT uniqExact(video_session_id) FROM active_intervals) AS active_sessions
"""


def vocabulary_report(ch: ClickHouse) -> str:
    """What the file actually contains beside what the sessionizer looks for, so a
    renamed token is readable straight off the error."""
    from .reference import VOCABULARY

    lines = []
    for column, expected in VOCABULARY.items():
        rows = ch.query(f"SELECT {column}, count() AS n FROM raw_events "
                        f"GROUP BY {column} ORDER BY n DESC LIMIT 25").rows
        width = max([len(str(v)) for v, _ in rows] + [len(column)])
        lines.append(f"\n{column + ' in raw_events':<{width + 16}}{'rows':>12}  recognised")
        for value, n in rows:
            lines.append(f"{str(value):<{width + 16}}{int(n):>12,}  "
                         f"{'yes' if value in expected else 'no'}")
        absent = [v for v in expected if v not in {r[0] for r in rows}]
        lines.append(f"\nrecognised {column} values absent from this file: "
                     + (", ".join(absent) if absent else "none, the vocabulary matches"))
    return "\n".join(lines)


def step_sessionize(ch: ClickHouse) -> int:
    run_sql_file(ch, "02_sessionize.sql")
    counts(ch, "active_intervals")

    shape = ch.query(SESSIONIZE_SHAPE).dicts()[0]
    sessions = int(shape["sessions"])
    active = int(shape["active_sessions"])
    ratio = active / sessions if sessions else 0.0
    if int(shape["intervals"]) and ratio >= MIN_ACTIVE_SESSION_RATIO:
        return 0

    print(f"\nFAIL  sessionize produced {int(shape['intervals']):,} active interval(s) "
          f"from {int(shape['events']):,} events in {sessions:,} sessions")
    print(f"      {active:,} of {sessions:,} sessions ({ratio:.1%}) are ever active, "
          f"under the {MIN_ACTIVE_SESSION_RATIO:.0%} this step requires.")
    print("      That bound is a heuristic, not a proof: passing it means the event "
          "vocabulary was recognised, not that the answers are right. Gate A is what "
          "checks the answers.")
    print("      The usual cause is a renamed event token, which makes every answer "
          "zero without anything else complaining.")
    print(vocabulary_report(ch))
    print("\n      Fix the vocabulary in sql/02_sessionize.sql and the matching "
          "classify() in src/clickliv/reference.py, or map the tokens upstream. "
          "See docs/unseen-day.md.")
    return 1


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


DATABASE_NAME = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]{0,62}\Z")


def marts_database() -> str:
    """Only the default database owns the bare `marts` name, so a scratch run cannot
    rebind, drop or grant against the surface the live demo reads."""
    database = os.environ.get("CH_DATABASE", DEFAULTS["CH_DATABASE"])
    if not DATABASE_NAME.match(database):
        raise SystemExit(
            f"CH_DATABASE={database!r} is not a plain identifier. It names the database "
            f"this run drops and rebuilds, and it is interpolated into DDL, so it has to "
            f"be a letter or underscore followed by letters, digits and underscores.")
    if database == DEFAULTS["CH_DATABASE"]:
        return "marts"
    return f"marts_{database}"


def step_marts(ch: ClickHouse) -> int:
    os.environ["MARTS_DB"] = marts_database()
    print(f"-- building {os.environ['MARTS_DB']} from {ch.config.database}")
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


def step_claims(ch: ClickHouse) -> int:
    from . import claims
    return claims.run(ch, update="--update" in sys.argv)


def step_submission(ch: ClickHouse) -> int:
    from . import submission
    return 0 if submission.run(ch, artifacts_dir(), submission_dir(),
                               evidence_dir()) else 1


def table_exists(ch: ClickHouse, table: str) -> bool:
    return bool(int(ch.scalar(
        f"SELECT count() FROM system.tables WHERE database = '{ch.config.database}' "
        f"AND name = '{table}'")))


def step_preflight(ch: ClickHouse) -> int:
    """Read only. Everything wrong with the new files is found here, while the tables the
    demo is serving are still up."""
    from . import load as loader
    from .ch import ClickHouseError

    ok = loader.preflight()
    try:
        for table in ("raw_events", "minute_occupancy"):
            if table_exists(ch, table):
                rows = int(ch.scalar(f"SELECT count() FROM {table}"))
                print(f"{ch.config.database}.{table} holds {rows:,} rows right now, "
                      f"and this run replaces them")
    except (ClickHouseError, OSError) as exc:
        print(f"could not read the current row counts from {ch.config.host}: {exc}")
    return 0 if ok else 1


def step_snapshot(ch: ClickHouse) -> int:
    """Move the serving tables aside instead of dropping them, so a run that dies midway
    is one make rollback away from the demo it was replacing."""
    ch.command("DROP DICTIONARY IF EXISTS content_dict")
    ch.command("DROP VIEW IF EXISTS mv_extend_open_session")
    moved = []
    for table in SERVING_TABLES:
        if not table_exists(ch, table):
            continue
        ch.command(f"DROP TABLE IF EXISTS {table}__prev")
        ch.command(f"RENAME TABLE {table} TO {table}__prev")
        moved.append(table)
    print(f"kept aside as __prev: {', '.join(moved) if moved else 'nothing, the database was empty'}")
    print("make rollback puts them back and rebuilds the serving views")
    return 0


def step_rollback(ch: ClickHouse) -> int:
    """Put the __prev tables back and rebuild the views over them."""
    restored = []
    for table in SERVING_TABLES:
        if not table_exists(ch, f"{table}__prev"):
            continue
        ch.command(f"DROP TABLE IF EXISTS {table}")
        ch.command(f"RENAME TABLE {table}__prev TO {table}")
        restored.append(table)
    if not restored:
        print(f"no __prev tables in {ch.config.database}, so there is nothing to roll back")
        return 1
    run_sql_file(ch, "01_schema.sql")
    from . import load as loader
    loader.reload_dictionary_everywhere(ch)
    status = run_step(ch, "marts")
    print(f"\nrestored {', '.join(restored)} and rebuilt {marts_database()}")
    return status


def step_replay(ch: ClickHouse) -> int:
    """The graded run: one command from a fresh CSV to a submission bundle."""
    started = time.time()
    for name in REPLAY:
        print(f"\n===== {name} =====")
        status = run_step(ch, name)
        if status:
            print(f"\nreplay FAILED at {name}")
            print("no table was touched." if name == "preflight" else
                  "the tables it replaced are still there under __prev: make rollback")
            return status
    print(f"\nreplay complete in {time.time() - started:.0f}s")
    return 0


REFERENCE_KEYS = ("sessions", "segments", "session_minutes", "minutes_with_activity",
                  "peak_concurrency", "peak_minute", "instantaneous_peak",
                  "average_over_active_minutes")


def figure(value) -> str:
    if isinstance(value, float):
        return f"{value:,.4f}"
    return f"{value:,}" if isinstance(value, int) else str(value)


def benchmarks_by_label(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    with path.open(newline="") as fh:
        return {row["query_label"]: row for row in csv.DictReader(fh)}


def comparison(root: Path) -> Path:
    """The new numbers beside the committed tuning numbers, same names, same order, same
    units, so the README table is a copy and paste rather than a re-derivation."""
    def reference(base: Path) -> dict:
        path = base / "reference.json"
        return json.loads(path.read_text()) if path.exists() else {}

    tuning, sealed = reference(Path("artifacts")), reference(root / "artifacts")
    old = benchmarks_by_label(Path("answers/benchmark_answers.csv"))
    new = benchmarks_by_label(root / "answers/benchmark_answers.csv")

    lines = ["| metric | tuning data | sealed data |", "| --- | --- | --- |"]
    for key in REFERENCE_KEYS:
        lines.append(f"| {key.replace('_', ' ')} | {figure(tuning.get(key, ''))} "
                     f"| {figure(sealed.get(key, ''))} |")
    for label, row in new.items():
        was = old.get(label, {})
        lines.append(f"| {label} peak | {was.get('peak_concurrency', '')} "
                     f"| {row['peak_concurrency']} |")
        lines.append(f"| {label} average | {was.get('average_concurrency', '')} "
                     f"| {row['average_concurrency']} |")

    out = root / "answers" / "comparison.md"
    out.write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return out


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
            print("no table was touched, so the demo is still serving what it served "
                  "before. Fix the input and run it again." if name == "preflight" else
                  f"the tables this run replaced are still in {ch.config.database} under "
                  f"__prev. Restore them with: make rollback")
            return status

    print(f"\n===== the same metrics as the tuning run, in the same order =====")
    comparison(root)

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
    mcp.serve(ch, host=os.environ.get("MCP_HOST", "0.0.0.0"),
              port=int(os.environ.get("MCP_PORT", "8765")))
    return 0


def step_obs(ch: ClickHouse) -> int:
    from . import observe
    return observe.report()


def step_reset(ch: ClickHouse) -> int:
    """A scratch run drops only its own marts. The role, profile and user are global, so
    only the primary run may drop them; otherwise a rehearsal takes the live demo down."""
    marts = marts_database()
    ch.command(f"DROP DATABASE IF EXISTS {marts}")
    if marts == "marts":
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
    "claims": step_claims,
    "replay": step_replay,
    "unseen": step_unseen,
    "preflight": step_preflight,
    "snapshot": step_snapshot,
    "rollback": step_rollback,
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
    marts_database()
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

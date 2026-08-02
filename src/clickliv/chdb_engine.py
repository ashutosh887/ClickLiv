"""The same SQL, in-process. chDB as a fifth path and a zero-install reproduction."""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from .ch import Result, parse_jsoncompact, split_statements
from .load import (CONTENT_OPTIONAL, CONTENT_TYPES, RAW_OPTIONAL, RAW_TYPES, content_csv,
                   content_insert, raw_csv, raw_insert, shape)

CREDENTIALS = {"CH_USER": "default", "CH_PASSWORD": ""}


class ChdbEngine:
    """Same surface as ClickHouse, so gates and verification run against either."""

    def __init__(self, path: Path, database: str = "clickliv"):
        import chdb.session

        self.path = path
        self.database = database
        self.session = chdb.session.Session(str(path))
        self.session.query(f"CREATE DATABASE IF NOT EXISTS {database} ENGINE = Atomic")
        self.session.query(f"USE {database}")

    def command(self, sql: str, **_) -> str:
        result = self.session.query(sql)
        return result.bytes().decode("utf-8", "replace").strip() if result else ""

    def query(self, sql: str, **_) -> Result:
        result = self.session.query(sql.rstrip().rstrip(";"), "JSONCompact")
        return parse_jsoncompact(result.bytes())

    def scalar(self, sql: str, **_):
        return self.query(sql).scalar()

    def script(self, sql: str, **_) -> None:
        for statement in split_statements(sql):
            self.command(statement)

    def close(self) -> None:
        self.session.close()


def file_source(path: Path, structure: str) -> str:
    return f"file('{path.resolve()}', 'CSVWithNames', '{structure}')"


def build(engine: ChdbEngine, render, sql_dir: Path) -> None:
    """Schema and pipeline SQL are the project's own files, byte for byte."""
    engine.script(render((sql_dir / "01_schema.sql").read_text()))

    content_shape = shape(content_csv(), CONTENT_TYPES, CONTENT_OPTIONAL)
    raw_shape = shape(raw_csv(), RAW_TYPES, RAW_OPTIONAL)
    engine.command(content_insert(
        file_source(content_csv(), content_shape.structure()), content_shape))
    engine.command("SYSTEM RELOAD DICTIONARY content_dict")
    engine.command(raw_insert(file_source(raw_csv(), raw_shape.structure()), raw_shape))

    for name in ("02_sessionize.sql", "03_occupancy.sql", "04_deltas.sql"):
        engine.script(render((sql_dir / name).read_text()))


def run(server, render, sql_dir: Path, artifacts: Path) -> bool:
    from . import gates

    store = artifacts / "chdb"
    if store.exists():
        shutil.rmtree(store)
    store.mkdir(parents=True)

    original = {key: os.environ.get(key) for key in CREDENTIALS}
    os.environ.update(CREDENTIALS)
    started = time.time()
    try:
        engine = ChdbEngine(store)
        build(engine, render, sql_dir)
        embedded = gates.fingerprint(engine)
        version = engine.scalar("SELECT version()")
        engine.close()
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    print(f"chDB {version} built the whole pipeline in-process in "
          f"{time.time() - started:.1f}s, no server\n")
    try:
        served = gates.fingerprint(server)
        print(f"server is ClickHouse {server.scalar('SELECT version()')}")
    except (ClickHouseError, OSError) as exc:
        print(f"Gate D: SKIPPED. The in-process build succeeded, but the gate diffs it "
              f"against a populated server and {server.config.host}:{server.config.port} "
              f"did not answer: {exc}")
        print("Start one with make up, build it with make all, then run this again.")
        for name, value in embedded.items():
            print(f"{name:<20}{value}")
        return False
    return gates.compare(served, embedded, label="Gate D: chDB agrees with the server")

"""The same SQL, in-process. chDB as a fifth path and a zero-install reproduction."""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from .ch import Result, parse_jsoncompact, split_statements
from .load import (CONTENT_STRUCTURE, RAW_STRUCTURE, content_csv, content_insert,
                   raw_csv, raw_insert)

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

    engine.command(content_insert(file_source(content_csv(), CONTENT_STRUCTURE)))
    engine.command("SYSTEM RELOAD DICTIONARY content_dict")
    engine.command(raw_insert(file_source(raw_csv(), RAW_STRUCTURE)))

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
    served = gates.fingerprint(server)
    print(f"server is ClickHouse {server.scalar('SELECT version()')}")
    return gates.compare(served, embedded, label="Gate D: chDB agrees with the server")

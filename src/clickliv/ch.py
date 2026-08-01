"""Minimal ClickHouse HTTP client. One code path for local Docker and Cloud."""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path


class ClickHouseError(RuntimeError):
    pass


SECRETS = re.compile(r"(PASSWORD|IDENTIFIED\s+(?:WITH\s+\w+\s+)?BY)\s*'[^']*'", re.IGNORECASE)


def redact(sql: str) -> str:
    """Strip credentials before SQL reaches an error, a log or a trace."""
    return SECRETS.sub(r"\1 '[redacted]'", sql)


@dataclass
class Result:
    columns: list[str]
    rows: list[tuple]
    query_id: str
    statistics: dict = field(default_factory=dict)

    def scalar(self):
        return self.rows[0][0]

    def column(self, name: str) -> list:
        i = self.columns.index(name)
        return [r[i] for r in self.rows]

    def dicts(self) -> list[dict]:
        return [dict(zip(self.columns, r)) for r in self.rows]


@dataclass
class Config:
    host: str = "localhost"
    port: int = 8123
    user: str = "clickliv"
    password: str = "clickliv"
    database: str = "clickliv"
    secure: bool = False

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            host=os.environ.get("CH_HOST", "localhost"),
            port=int(os.environ.get("CH_PORT", "8123")),
            user=os.environ.get("CH_USER", "clickliv"),
            password=os.environ.get("CH_PASSWORD", "clickliv"),
            database=os.environ.get("CH_DATABASE", "clickliv"),
            secure=os.environ.get("CH_SECURE", "0") not in ("0", "", "false", "False"),
        )

    @property
    def url(self) -> str:
        scheme = "https" if self.secure else "http"
        return f"{scheme}://{self.host}:{self.port}/"


class ClickHouse:
    """Thin wrapper over the HTTP interface. Returns query_ids so latency comes from system.query_log."""

    def __init__(self, config: Config | None = None, timeout: int = 900):
        self.config = config or Config.from_env()
        self.timeout = timeout
        self.observer = None

    def _post(self, sql: str, body=None, length: int | None = None,
              query_id: str | None = None, settings: dict | None = None,
              database: str | None = None) -> tuple[bytes, str]:
        qid = query_id or str(uuid.uuid4())
        params = {"query": sql, "query_id": qid}
        db = self.config.database if database is None else database
        if db:
            params["database"] = db
        for k, v in (settings or {}).items():
            params[k] = str(v)
        url = self.config.url + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("X-ClickHouse-User", self.config.user)
        req.add_header("X-ClickHouse-Key", self.config.password)
        if length is not None:
            req.add_header("Content-Length", str(length))
        start = time.time_ns()
        error = None
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read(), qid
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace").strip()
            error = f"{e.code} {redact(detail)}"
            raise ClickHouseError(
                f"{e.code} on query {qid}\n{redact(detail)}\n---\n{redact(sql)[:2000]}") from None
        finally:
            if self.observer:
                self.observer(sql, qid, start, time.time_ns(), error)

    def command(self, sql: str, settings: dict | None = None,
                database: str | None = None, query_id: str | None = None) -> str:
        out, _ = self._post(sql, query_id=query_id, settings=settings, database=database)
        return out.decode("utf-8", "replace").strip()

    def query(self, sql: str, settings: dict | None = None,
              query_id: str | None = None) -> Result:
        out, qid = self._post(sql.rstrip().rstrip(";") + "\nFORMAT JSONCompact",
                              query_id=query_id, settings=settings)
        return parse_jsoncompact(out, qid)

    def scalar(self, sql: str, settings: dict | None = None):
        return self.query(sql, settings=settings).scalar()

    def insert_csv(self, sql: str, path: str | Path, settings: dict | None = None) -> str:
        """sql must end in a FORMAT clause; the CSV body is streamed as the request payload.

        Trailing whitespace is stripped: a newline after FORMAT makes the server read an
        empty first data line out of the query string and mis-detect the header.
        """
        sql = sql.strip()
        path = Path(path)
        size = path.stat().st_size
        with path.open("rb") as fh:
            _, qid = self._post(sql, body=fh, length=size, settings=settings)
        return qid

    def script(self, sql: str, settings: dict | None = None) -> None:
        for statement in split_statements(sql):
            self.command(statement, settings=settings)

    def ping(self) -> str:
        return self.scalar("SELECT version()")

    def query_log_rows(self, columns: str, query_ids: list[str],
                        retries: int = 5, wait: float = 1.0) -> list[dict]:
        """SYSTEM FLUSH LOGS then read back system.query_log for the given query_ids.

        On a single node, flush-then-select is enough. On a multi-replica Cloud
        service a query, the flush, and the select can each land on a different
        replica, so a query that really ran can be briefly invisible here. Retried,
        not assumed away: re-flush and re-select until every id is accounted for or
        the retries run out, same bounded-retry discipline as load.reconcile.
        """
        ids = ",".join(f"'{q}'" for q in query_ids)
        rows: list[dict] = []
        for attempt in range(retries):
            self.command("SYSTEM FLUSH LOGS")
            rows = self.query(
                f"SELECT {columns} FROM system.query_log "
                f"WHERE type = 'QueryFinish' AND query_id IN ({ids})").dicts()
            if len(rows) >= len(query_ids) or attempt == retries - 1:
                return rows
            time.sleep(wait)
        return rows


def parse_jsoncompact(payload: bytes | str, query_id: str = "") -> Result:
    data = json.loads(payload)
    return Result(
        columns=[c["name"] for c in data["meta"]],
        rows=[tuple(r) for r in data["data"]],
        query_id=query_id,
        statistics=data.get("statistics", {}),
    )


def split_statements(sql: str) -> list[str]:
    """Split on semicolons outside of quotes and line comments."""
    out, buf = [], []
    quote = None
    i = 0
    while i < len(sql):
        c = sql[i]
        if quote:
            buf.append(c)
            if c == "\\" and i + 1 < len(sql):
                buf.append(sql[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in "'\"`":
            quote = c
            buf.append(c)
        elif c == "-" and sql[i:i + 2] == "--":
            end = sql.find("\n", i)
            i = len(sql) if end == -1 else end
            continue
        elif c == ";":
            statement = "".join(buf).strip()
            if statement:
                out.append(statement)
            buf = []
        else:
            buf.append(c)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out

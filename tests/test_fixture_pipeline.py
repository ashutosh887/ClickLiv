"""The real SQL, end to end, on the committed fixture, in chDB. No server, no secrets."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"

ENVIRONMENT = {
    "RAW_CSV": str(FIXTURES / "events.csv"),
    "CONTENT_CSV": str(FIXTURES / "content.csv"),
    "CH_DATABASE": "clickliv",
    "CH_USER": "default",
    "CH_PASSWORD": "",
    "GAP_SECONDS": "90",
    "GRACE_SECONDS": "40",
}

AGREEMENT_QUERY = """
    SELECT countIf(running != sessions)
    FROM
    (
        SELECT
            minute,
            sum(delta) OVER (ORDER BY minute
                ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS running,
            sessions
        FROM
        (
            SELECT minute, sum(delta) AS delta, sum(sessions) AS sessions
            FROM
            (
                SELECT
                    arrayJoin(range(
                        toUInt32((SELECT min(minute) FROM minute_deltas)),
                        toUInt32((SELECT max(minute) FROM minute_deltas) + 1))) AS minute,
                    toInt64(0) AS delta,
                    toInt64(0) AS sessions
                UNION ALL
                SELECT minute, toInt64(delta), toInt64(0) FROM minute_deltas
                UNION ALL
                SELECT minute, toInt64(0), toInt64(sessions) FROM minute_occupancy
            )
            GROUP BY minute
        )
    )
"""


@unittest.skipUnless(importlib.util.find_spec("chdb"), "needs the embedded extra: chdb")
class FixturePipelineTests(unittest.TestCase):
    """One build in setUpClass; every test then interrogates the same serving tables."""

    @classmethod
    def setUpClass(cls):
        from clickliv import chdb_engine
        from clickliv.cli import SQL_DIR, render

        for path in (ENVIRONMENT["RAW_CSV"], ENVIRONMENT["CONTENT_CSV"]):
            if not Path(path).exists():
                raise FileNotFoundError(f"{path}; regenerate with tools/make_fixture.py")

        cls.previous = {k: os.environ.get(k) for k in ENVIRONMENT}
        os.environ.update(ENVIRONMENT)
        cls.store = tempfile.TemporaryDirectory()
        cls.render = staticmethod(render)
        cls.sql_dir = SQL_DIR
        cls.engine = chdb_engine.ChdbEngine(Path(cls.store.name) / "state")
        chdb_engine.build(cls.engine, render, SQL_DIR)

    @classmethod
    def tearDownClass(cls):
        cls.engine.close()
        cls.store.cleanup()
        for key, value in cls.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def count(self, table: str) -> int:
        return int(self.engine.scalar(f"SELECT count() FROM {table}"))

    def test_the_fixture_loaded_whole(self):
        self.assertEqual(self.count("raw_events"), 19_154)
        self.assertEqual(self.count("content_meta"), 135)
        self.assertEqual(
            int(self.engine.scalar("SELECT uniqExact(video_session_id) FROM raw_events")), 221)

    def test_the_dictionary_join_has_no_orphans(self):
        orphans = int(self.engine.scalar("""
            SELECT count() FROM (
                SELECT DISTINCT content_id FROM raw_events
                WHERE NOT dictHas('content_dict', content_id))
        """))
        self.assertEqual(orphans, 0)

    def test_reconcile_passes_on_a_fixture(self):
        from clickliv import load
        with contextlib.redirect_stdout(io.StringIO()) as out:
            ok = load.reconcile(self.engine)
        self.assertTrue(ok, out.getvalue())
        self.assertIn("day-invariant checks still enforced", out.getvalue())

    def test_active_intervals_are_produced_and_well_formed(self):
        self.assertGreater(self.count("active_intervals"), 0)
        self.assertEqual(
            self.count("active_intervals WHERE ts_end_ms <= ts_start_ms"), 0)

    def test_pauses_and_backgrounding_split_sessions_into_several_intervals(self):
        split = int(self.engine.scalar("""
            SELECT count() FROM (
                SELECT video_session_id FROM active_intervals
                GROUP BY video_session_id HAVING count() > 1)
        """))
        self.assertGreater(split, 0)

    def test_occupancy_is_built_and_reaches_real_concurrency(self):
        self.assertGreater(self.count("session_minutes"), 0)
        self.assertGreater(self.count("minute_occupancy"), 0)
        peak = int(self.engine.scalar("""
            SELECT max(concurrent) FROM (
                SELECT minute, sum(sessions) AS concurrent
                FROM minute_occupancy GROUP BY minute)
        """))
        self.assertGreater(peak, 1)

    def test_deltas_agree_with_occupancy_minute_by_minute(self):
        self.assertGreater(self.count("minute_deltas"), 0)
        self.assertEqual(int(self.engine.scalar(AGREEMENT_QUERY)), 0)
        self.assertEqual(int(self.engine.scalar("SELECT sum(delta) FROM minute_deltas")), 0)

    def test_rebuilding_is_idempotent(self):
        from clickliv import gates
        before = gates.fingerprint(self.engine)
        for name in ("02_sessionize.sql", "03_occupancy.sql", "04_deltas.sql"):
            self.engine.script(self.render((self.sql_dir / name).read_text()))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            same = gates.compare(before, gates.fingerprint(self.engine))
        self.assertTrue(same, out.getvalue())


if __name__ == "__main__":
    unittest.main()

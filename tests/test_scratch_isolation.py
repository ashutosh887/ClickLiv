"""Pins the one rule that keeps a rehearsal from taking the public demo down: only a run
against the default database may name the live marts surface or its global role."""

from __future__ import annotations

import os
import re
import subprocess
import unittest
from pathlib import Path

from clickliv import cli

ROOT = Path(__file__).resolve().parents[1]

LIVE = ("marts", "marts_agent", "marts_readonly", "marts_budget")

GLOBAL_DROPS = ("DROP USER", "DROP ROLE", "DROP SETTINGS PROFILE")


def reset_statements(database: str | None) -> list[str]:
    """Every statement step_reset would send for a given CH_DATABASE, without a server."""
    sent: list[str] = []

    class Recorder:
        config = type("Config", (), {"database": database or "clickliv"})()

        def command(self, sql, **kwargs):
            sent.append(sql)
            return ""

    previous = os.environ.get("CH_DATABASE")
    if database is None:
        os.environ.pop("CH_DATABASE", None)
    else:
        os.environ["CH_DATABASE"] = database
    try:
        cli.step_reset(Recorder())
    finally:
        os.environ.pop("CH_DATABASE", None)
        if previous is not None:
            os.environ["CH_DATABASE"] = previous
    return sent


class MartsNameTests(unittest.TestCase):
    def setUp(self):
        previous = os.environ.get("CH_DATABASE")
        self.addCleanup(lambda: os.environ.__setitem__("CH_DATABASE", previous)
                        if previous is not None else os.environ.pop("CH_DATABASE", None))

    def test_only_the_default_database_owns_the_bare_marts_name(self):
        os.environ["CH_DATABASE"] = "clickliv"
        self.assertEqual(cli.marts_database(), "marts")

    def test_an_unset_database_still_resolves_to_the_default(self):
        os.environ.pop("CH_DATABASE", None)
        self.assertEqual(cli.marts_database(), "marts")

    def test_every_other_database_gets_a_marts_of_its_own(self):
        for database in ("scratch", "clickliv_quirky", "unseen", "Clickliv", "CLICKLIV",
                         "clickliv2", "marts", "clickliv_"):
            with self.subTest(database=database):
                os.environ["CH_DATABASE"] = database
                self.assertEqual(cli.marts_database(), f"marts_{database}")

    def test_a_database_that_is_not_an_identifier_stops_the_run(self):
        for database in ("", " ", "clickliv ", " clickliv", "click-liv", "click.liv",
                         "click liv", "1clickliv", "clickliv;", "clickliv`",
                         "x; DROP DATABASE marts", "clickliv' OR '1", "a" * 64):
            with self.subTest(database=database):
                os.environ["CH_DATABASE"] = database
                with self.assertRaises(SystemExit):
                    cli.marts_database()


class ResetTests(unittest.TestCase):
    def test_a_scratch_reset_never_names_a_live_object(self):
        for database in ("scratch", "clickliv_quirky", "unseen", "held_out"):
            with self.subTest(database=database):
                sent = reset_statements(database)
                self.assertIn(f"DROP DATABASE IF EXISTS marts_{database}", sent)
                for statement in sent:
                    for name in LIVE:
                        self.assertNotRegex(statement, rf"\b{re.escape(name)}\b")

    def test_a_scratch_reset_drops_nothing_global(self):
        sent = reset_statements("scratch")
        for statement in sent:
            for verb in GLOBAL_DROPS:
                self.assertNotIn(verb, statement)

    def test_the_primary_reset_is_the_only_one_that_drops_the_global_role(self):
        sent = reset_statements("clickliv")
        self.assertIn("DROP DATABASE IF EXISTS marts", sent)
        for verb in GLOBAL_DROPS:
            self.assertTrue(any(statement.startswith(verb) for statement in sent), verb)

    def test_a_database_that_is_not_an_identifier_drops_nothing_at_all(self):
        for database in ("", "x; DROP DATABASE marts", "click-liv"):
            with self.subTest(database=database):
                with self.assertRaises(SystemExit):
                    reset_statements(database)


class MakefileTests(unittest.TestCase):
    """Every target that can drop or rebuild has to honour DB=, or the documented way to
    run a rehearsal safely silently aims at the live database instead."""

    DESTRUCTIVE = ("reset", "marts", "schema", "load", "pipeline", "all", "replay",
                   "unseen", "rollback", "preflight", "gate-b", "gate-c")

    def expand(self, target: str, database: str) -> str:
        return subprocess.run(
            ["make", "-n", target, f"DB={database}", "RAW=/dev/null",
             "CONTENT=/dev/null"],
            cwd=ROOT, capture_output=True, text=True, check=True).stdout

    def test_every_destructive_target_threads_db(self):
        for target in self.DESTRUCTIVE:
            with self.subTest(target=target):
                self.assertIn('CH_DATABASE="scratch"', self.expand(target, "scratch"))

    def test_no_target_hardcodes_a_database(self):
        text = (ROOT / "Makefile").read_text()
        self.assertNotIn("CH_DATABASE=clickliv", text)


class MartsSqlTests(unittest.TestCase):
    def test_the_marts_script_never_names_a_database_directly(self):
        text = (ROOT / "sql" / "06_marts.sql").read_text()
        body = "\n".join(line for line in text.splitlines()
                         if not line.lstrip().startswith("--"))
        self.assertNotRegex(body, r"\bmarts\.")
        self.assertNotRegex(body, r"\bDROP DATABASE IF EXISTS marts\b")
        self.assertIn("DROP DATABASE IF EXISTS ${MARTS_DB}", body)

    def test_the_global_auth_objects_are_never_dropped_by_the_script(self):
        text = (ROOT / "sql" / "06_marts.sql").read_text()
        for verb in GLOBAL_DROPS:
            self.assertNotIn(verb, text)

    def test_no_module_hardcodes_the_live_marts_database(self):
        offenders = []
        for path in sorted((ROOT / "src" / "clickliv").glob("*.py")):
            if path.name == "mcp.py":
                continue
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if re.search(r"(FROM|JOIN)\s+marts\.", line):
                    offenders.append(f"{path.name}:{number}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()

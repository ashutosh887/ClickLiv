"""Pure logic only: no live ClickHouse needed. The DB-dependent behavior is proven by
Gates A/B/C/D instead, which are the real correctness tests for this project; these
cover the parsing and redaction logic gates never happen to exercise on their own.
"""

from __future__ import annotations

import os
import unittest

from clickliv.ch import Config, parse_jsoncompact, redact, split_statements


class RedactTests(unittest.TestCase):
    def test_password_literal_is_redacted(self):
        sql = "CREATE USER x IDENTIFIED WITH sha256_password BY 'super-secret'"
        self.assertNotIn("super-secret", redact(sql))
        self.assertIn("[redacted]", redact(sql))

    def test_dictionary_password_clause_is_redacted(self):
        sql = "SOURCE(CLICKHOUSE(USER 'x' PASSWORD 'hunter2' DB 'clickliv'))"
        out = redact(sql)
        self.assertNotIn("hunter2", out)
        self.assertIn("USER 'x'", out)

    def test_query_without_credentials_is_unchanged(self):
        sql = "SELECT count() FROM raw_events"
        self.assertEqual(redact(sql), sql)


class SplitStatementsTests(unittest.TestCase):
    def test_splits_on_semicolons(self):
        out = split_statements("SELECT 1; SELECT 2;")
        self.assertEqual(out, ["SELECT 1", "SELECT 2"])

    def test_semicolon_inside_string_literal_is_not_a_split_point(self):
        out = split_statements("SELECT 'a;b'; SELECT 2;")
        self.assertEqual(out, ["SELECT 'a;b'", "SELECT 2"])

    def test_line_comment_is_stripped(self):
        out = split_statements("SELECT 1; -- a comment; with a semicolon\nSELECT 2;")
        self.assertEqual(out, ["SELECT 1", "SELECT 2"])

    def test_trailing_statement_without_semicolon_is_kept(self):
        out = split_statements("SELECT 1;\nSELECT 2")
        self.assertEqual(out, ["SELECT 1", "SELECT 2"])

    def test_blank_input_yields_no_statements(self):
        self.assertEqual(split_statements("  \n  "), [])


class ParseJsonCompactTests(unittest.TestCase):
    def test_columns_and_rows(self):
        payload = (
            b'{"meta":[{"name":"a","type":"UInt64"},{"name":"b","type":"String"}],'
            b'"data":[[1,"x"],[2,"y"]]}'
        )
        result = parse_jsoncompact(payload, "qid-1")
        self.assertEqual(result.columns, ["a", "b"])
        self.assertEqual(result.rows, [(1, "x"), (2, "y")])
        self.assertEqual(result.query_id, "qid-1")

    def test_scalar_and_dicts_helpers(self):
        payload = b'{"meta":[{"name":"n"}],"data":[[42]]}'
        result = parse_jsoncompact(payload)
        self.assertEqual(result.scalar(), 42)
        self.assertEqual(result.dicts(), [{"n": 42}])

    def test_column_helper_extracts_one_field_across_rows(self):
        payload = b'{"meta":[{"name":"a"},{"name":"b"}],"data":[[1,10],[2,20]]}'
        result = parse_jsoncompact(payload)
        self.assertEqual(result.column("b"), [10, 20])


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in
                      ("CH_HOST", "CH_PORT", "CH_SECURE")}

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_defaults_when_nothing_set(self):
        for k in self.saved:
            os.environ.pop(k, None)
        config = Config.from_env()
        self.assertEqual(config.host, "localhost")
        self.assertEqual(config.port, 8123)
        self.assertFalse(config.secure)
        self.assertEqual(config.url, "http://localhost:8123/")

    def test_secure_flag_selects_https(self):
        os.environ["CH_SECURE"] = "1"
        os.environ["CH_HOST"] = "example.clickhouse.cloud"
        os.environ["CH_PORT"] = "8443"
        config = Config.from_env()
        self.assertTrue(config.secure)
        self.assertEqual(config.url, "https://example.clickhouse.cloud:8443/")


if __name__ == "__main__":
    unittest.main()

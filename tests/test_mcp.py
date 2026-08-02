"""The MCP surface: argument handling as pure logic, then the case rules against Cloud."""

from __future__ import annotations

import os
import unittest

from clickliv import mcp

KNOWN = {
    "country": ("india",),
    "platform": ("ANDROID_PHONE", "IPHONE", "Mweb"),
    "video_type": ("live", "vod"),
}


class FakeResult:
    def __init__(self, rows):
        self.rows = rows
        self.query_id = "fake"
        self.statistics = {}


class FakeAgent:
    def __init__(self, known=KNOWN):
        self.rows = [(name, value) for name, values in known.items() for value in values]
        self.reads = 0

    def query(self, sql, settings=None):
        self.reads += 1
        return FakeResult(self.rows)


class MatchValueTests(unittest.TestCase):
    """The rule a blanket fold once broke: an exact value wins, folding is only a fallback."""

    def test_exact_value_wins_over_a_fold(self):
        self.assertEqual(mcp.match_value(("hin", "HIN"), "hin"), "hin")
        self.assertEqual(mcp.match_value(("hin", "HIN"), "HIN"), "HIN")

    def test_fold_applies_only_when_nothing_matches_exactly(self):
        self.assertEqual(mcp.match_value(("live", "vod"), "LIVE"), "live")
        self.assertEqual(mcp.match_value(("ANDROID_PHONE",), "android_phone"), "ANDROID_PHONE")

    def test_surrounding_space_is_trimmed(self):
        self.assertEqual(mcp.match_value(("vod",), "  vod  "), "vod")

    def test_an_unknown_value_matches_nothing(self):
        self.assertIsNone(mcp.match_value(("live", "vod"), "ROKU"))


class EnumArgumentTests(unittest.TestCase):
    def setUp(self):
        mcp.CACHE.clear()
        mcp.CACHE_READ = 0.0
        self.agent = FakeAgent()

    def test_a_missing_filter_means_no_filter(self):
        self.assertEqual(mcp.enum_argument(self.agent, {}, "platform"), "")

    def test_every_sentinel_collapses_to_no_filter(self):
        for sentinel in ("", "ALL", "all", "All", " ALL ", "*", "any", "NONE", "null", "%"):
            self.assertEqual(mcp.enum_argument(self.agent, {"country": sentinel}, "country"), "",
                             f"{sentinel!r} should mean no filter")

    def test_a_real_value_is_canonicalised(self):
        self.assertEqual(
            mcp.enum_argument(self.agent, {"platform": "iphone"}, "platform"), "IPHONE")

    def test_an_unknown_value_is_rejected_and_the_error_names_the_real_ones(self):
        with self.assertRaises(mcp.ToolError) as caught:
            mcp.enum_argument(self.agent, {"platform": "ROKU"}, "platform")
        self.assertIn("ANDROID_PHONE", str(caught.exception))

    def test_an_injection_attempt_is_rejected_before_any_sql(self):
        with self.assertRaises(mcp.ToolError):
            mcp.enum_argument(self.agent, {"platform": "ANDROID_PHONE' OR 1=1 --"}, "platform")

    def test_values_are_read_from_the_data_not_from_a_baked_in_list(self):
        mcp.CACHE.clear()
        mcp.CACHE_READ = 0.0
        agent = FakeAgent({"country": ("india",), "platform": ("NEW_TV_STICK",),
                           "video_type": ("vod",)})
        self.assertEqual(
            mcp.enum_argument(agent, {"platform": "new_tv_stick"}, "platform"), "NEW_TV_STICK")


class IntegerArgumentTests(unittest.TestCase):
    def test_a_missing_value_takes_the_default(self):
        self.assertEqual(mcp.integer_argument({}, "minute_from", 7, 0, 10), 7)

    def test_a_value_out_of_bounds_is_rejected(self):
        with self.assertRaises(mcp.ToolError):
            mcp.integer_argument({"minute_from": 99}, "minute_from", 0, 0, 10)

    def test_a_boolean_is_not_an_integer(self):
        with self.assertRaises(mcp.ToolError):
            mcp.integer_argument({"minute_from": True}, "minute_from", 0, 0, 10)


class RejectUnknownTests(unittest.TestCase):
    def test_an_unknown_argument_is_named_back(self):
        with self.assertRaises(mcp.ToolError) as caught:
            mcp.reject_unknown({"regoin": "india"}, ("country",))
        self.assertIn("regoin", str(caught.exception))


@unittest.skipUnless(os.environ.get("CH_HOST") and os.environ.get("MARTS_PASSWORD"),
                     "needs the Cloud service and MARTS_PASSWORD")
class LiveCaseRuleTests(unittest.TestCase):
    """The case rule lives in the view, so it is only proven against the real service."""

    @classmethod
    def setUpClass(cls):
        from clickliv.ch import ClickHouse
        cls.agent = mcp.agent_connection(ClickHouse())

    def peak(self, **filters) -> int:
        names = ("country", "platform", "video_type", "category", "app_version",
                 "player_version", "audio_language", "subtitle_language",
                 "video_resolution", "show_name")
        args = ", ".join(f"{name} = {{{name}:String}}" for name in names)
        sql = (f"SELECT max(peak_concurrency) FROM marts.v_concurrency_full({args}, "
               "content_id = 0, minute_from = 0, minute_to = 4294967295, grain_minutes = 1)")
        settings = {f"param_{name}": filters.get(name, "") for name in names}
        return int(self.agent.query(sql, settings=settings).rows[0][0])

    def test_an_exact_value_is_never_merged_with_another_casing(self):
        unfiltered = self.peak()
        hin = self.peak(audio_language="hin")
        upper = self.peak(audio_language="HIN")
        self.assertGreater(hin, upper)
        self.assertLess(hin + upper, unfiltered * 2)
        self.assertNotEqual(hin, hin + upper)

    def test_a_casing_that_matches_nothing_exactly_still_finds_the_one_real_value(self):
        self.assertEqual(self.peak(video_type="LIVE"), self.peak(video_type="live"))
        self.assertEqual(self.peak(platform="android_phone"), self.peak(platform="ANDROID_PHONE"))

    def test_a_casing_that_would_merge_two_real_values_matches_nothing(self):
        self.assertEqual(self.peak(audio_language="Hin"), 0)

    def test_every_sentinel_is_the_unfiltered_total(self):
        unfiltered = self.peak()
        for sentinel in ("ALL", "all", "*", "any", "NONE", "null", "%"):
            self.assertEqual(self.peak(country=sentinel), unfiltered, sentinel)

    def test_the_empty_value_is_not_published_as_selectable(self):
        rows = self.agent.query(
            "SELECT count() FROM (SELECT value FROM marts.v_dimension_values) "
            "WHERE empty(value)").rows
        self.assertEqual(int(rows[0][0]), 0)

    def test_the_agent_cannot_read_outside_marts(self):
        from clickliv.ch import ClickHouseError
        for table in ("clickliv.raw_events", "clickliv.minute_occupancy", "system.query_log"):
            with self.assertRaises(ClickHouseError, msg=table):
                self.agent.query(f"SELECT count() FROM {table}")

    def test_the_agent_cannot_raise_its_own_budget(self):
        from clickliv.ch import ClickHouseError
        with self.assertRaises(ClickHouseError):
            self.agent.query("SELECT 1 SETTINGS max_execution_time = 600")

    def test_the_agent_cannot_write(self):
        from clickliv.ch import ClickHouseError
        for statement in ("CREATE TABLE marts.nope (x UInt8) ENGINE = Memory",
                          "DROP DATABASE marts",
                          "INSERT INTO marts.dimension_value VALUES ('x', 'y')"):
            with self.assertRaises(ClickHouseError, msg=statement):
                self.agent.query(statement)


if __name__ == "__main__":
    unittest.main()

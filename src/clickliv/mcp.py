"""MCP over Streamable HTTP. Four pre-vetted tools, curated marts views only, and a
server-side query budget: the model never sends SQL.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import otel
from .ch import ClickHouse, ClickHouseError, Config

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "clickliv"
SERVER_VERSION = "0.1.0"
ENDPOINT = "/mcp"
AGENT_USER = "marts_agent"

MINUTE_MIN = 0
MINUTE_MAX = 4294967295
ROW_CAP = 40

GRAINS = {"minute": 1, "hour": 60, "day": 1440}
DEFAULT_GRAIN = "minute"

NO_FILTER = frozenset({"", "all", "any", "none", "null", "*", "%"})

DIMENSION_NAMES = ("country", "platform", "video_type")
DIMENSION_TTL = 300
NAMED_VALUES = 20

FILTER_ARGS = (
    "country = {country:String}, platform = {platform:String}, "
    "video_type = {video_type:String}, content_id = {content_id:UInt64}, "
    "minute_from = {minute_from:UInt32}, minute_to = {minute_to:UInt32}"
)

PEAK_SQL = (
    "SELECT bucket_minute, peak_concurrency, average_concurrency, minutes_in_bucket "
    f"FROM marts.v_concurrency(grain_minutes = {{grain_minutes:UInt32}}, {FILTER_ARGS})"
)

SERIES_SQL = f"SELECT minute, concurrency FROM marts.v_occupancy_minute({FILTER_ARGS})"

WINDOW_SQL = ("SELECT min_minute, max_minute, minutes_with_sessions, span_days "
              "FROM marts.v_data_window")

OVERCOUNT_SQL = ("SELECT foreground_peak, foreground_peak_utc, naive_peak, naive_peak_utc, "
                 "peak_overcount_pct, foreground_average, naive_average, "
                 "average_overcount_pct FROM marts.v_overcount")

UNIT_NOTE = ("concurrency counts video sessions that were in the foreground at the same "
             "moment, not distinct people or accounts")

DIMENSION_SQL = ("SELECT dimension, value FROM marts.v_dimension_values WHERE dimension IN "
                 f"({', '.join(repr(name) for name in DIMENSION_NAMES)}) AND value != '' "
                 "ORDER BY dimension, minutes_present DESC, value")

TITLE_SQL = ("SELECT content_id, title, minutes_present FROM marts.v_titles "
             "WHERE title != '' AND positionCaseInsensitive(title, {needle:String}) > 0 "
             "ORDER BY lower(title) = lower({needle:String}) DESC, minutes_present DESC "
             "LIMIT 10")


class ToolError(ValueError):
    pass


CACHE: dict[str, tuple[str, ...]] = {}
CACHE_READ = 0.0
CACHE_LOCK = threading.Lock()


def dimensions(agent: ClickHouse, refresh: bool = False) -> dict[str, tuple[str, ...]]:
    """The accepted values come out of the data, so a replacement dataset needs no code change."""
    global CACHE, CACHE_READ
    with CACHE_LOCK:
        if refresh or not CACHE or time.monotonic() - CACHE_READ > DIMENSION_TTL:
            found: dict[str, list[str]] = {name: [] for name in DIMENSION_NAMES}
            for name, value in agent.query(DIMENSION_SQL).rows:
                found.setdefault(str(name), []).append(str(value))
            CACHE = {name: tuple(values) for name, values in found.items()}
            CACHE_READ = time.monotonic()
        return CACHE


def named_values(values: tuple[str, ...]) -> str:
    if len(values) <= NAMED_VALUES:
        return ", ".join(values)
    return (f"{', '.join(values[:NAMED_VALUES])} and {len(values) - NAMED_VALUES} more, "
            "all listed by list_dimensions")


def agent_connection(ch: ClickHouse) -> ClickHouse:
    """Reconnect as marts_agent so the marts_budget profile enforces the budget, not this process."""
    password = os.environ.get("MARTS_PASSWORD")
    if not password:
        raise SystemExit("MARTS_PASSWORD is not set, so the MCP server would have to run "
                         "as the admin user; refusing")
    base = ch.config
    return ClickHouse(Config(host=base.host, port=base.port, user=AGENT_USER,
                             password=password, database=base.database, secure=base.secure))


def reject_unknown(arguments: dict, allowed: tuple[str, ...]) -> None:
    if not isinstance(arguments, dict):
        raise ToolError("arguments must be a JSON object")
    unknown = sorted(set(arguments) - set(allowed))
    if unknown:
        raise ToolError(f"unknown argument {', '.join(unknown)}; this tool accepts "
                        f"{', '.join(allowed) or 'no arguments'}")


def match_value(known: tuple[str, ...], value: str) -> str | None:
    """Exact match wins and the case fold is only a fallback, so hin and HIN stay distinct."""
    trimmed = value.strip()
    if trimmed in known:
        return trimmed
    return next((entry for entry in known if entry.lower() == trimmed.lower()), None)


def enum_argument(agent: ClickHouse, arguments: dict, name: str) -> str:
    """Filters come from the values the data holds, so a hallucinated value is rejected, not
    queried. The sentinels a model reaches for collapse to no filter."""
    value = arguments.get(name)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ToolError(f"{name} must be a string, got {value!r}")
    if value.strip().lower() in NO_FILTER:
        return ""
    canonical = match_value(dimensions(agent).get(name, ()), value)
    if canonical is None:
        known = dimensions(agent, refresh=True).get(name, ())
        canonical = match_value(known, value)
        if canonical is None:
            raise ToolError(f"{name} must be one of {named_values(known)}, or left out "
                            f"for no filter, got {value!r}")
    return canonical


def integer_argument(arguments: dict, name: str, default: int, low: int, high: int) -> int:
    value = arguments.get(name)
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        raise ToolError(f"{name} must be an integer, got a boolean")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ToolError(f"{name} must be an integer, got {value!r}") from None
    if not low <= number <= high:
        raise ToolError(f"{name} must be between {low} and {high}, got {number}")
    return number


def resolve_title(agent: ClickHouse, title: str) -> int:
    """A title becomes a content_id or the call fails; it never falls through to the total."""
    needle = title.strip()
    if not needle:
        return 0
    rows = agent.query(TITLE_SQL, settings={"param_needle": needle}).rows
    if not rows:
        raise ToolError(f"no title in this dataset matches {needle!r}, so there is no "
                        "number to report for it; say the dataset does not cover that "
                        "title rather than answering with a wider total")
    exact = [row for row in rows if str(row[1]).lower() == needle.lower()]
    if len(exact) == 1:
        return int(exact[0][0])
    if len(rows) == 1:
        return int(rows[0][0])
    listed = "; ".join(f"{row[1]} (content_id {row[0]})" for row in rows[:5])
    raise ToolError(f"{needle!r} matches {len(rows)} titles, so it is ambiguous; call "
                    f"again with one of these content_id values instead: {listed}")


def content_id_argument(agent: ClickHouse, arguments: dict) -> int:
    title = arguments.get("title")
    if title in (None, ""):
        return integer_argument(arguments, "content_id", 0, 0, 2 ** 64 - 1)
    if not isinstance(title, str):
        raise ToolError(f"title must be a string, got {title!r}")
    if integer_argument(arguments, "content_id", 0, 0, 2 ** 64 - 1):
        raise ToolError("pass either title or content_id, not both")
    return resolve_title(agent, title)


def filter_settings(agent: ClickHouse, arguments: dict, content_id: int) -> dict:
    return {
        "param_country": enum_argument(agent, arguments, "country"),
        "param_platform": enum_argument(agent, arguments, "platform"),
        "param_video_type": enum_argument(agent, arguments, "video_type"),
        "param_content_id": content_id,
    }


def filter_label(settings: dict, arguments: dict, content_id: int) -> str:
    """Reports the canonical values, so a sentinel reads as no filter rather than as a value."""
    parts = [f"{name}={settings['param_' + name]}"
             for name in ("platform", "country", "video_type") if settings["param_" + name]]
    if arguments.get("title"):
        parts.append(f"title={arguments['title']} (content_id {content_id})")
    elif content_id:
        parts.append(f"content_id={content_id}")
    return ", ".join(parts) or "none"


def stamp(minute: int | None) -> str:
    if minute is None:
        return "unknown"
    return datetime.fromtimestamp(int(minute) * 60, UTC).strftime("%Y-%m-%d %H:%M UTC")


def empty_note(agent: ClickHouse, label: str) -> list[str]:
    """An empty match is never an answer, so say what does exist instead of returning a zero."""
    low, high, minutes, _ = agent.query(WINDOW_SQL).rows[0]
    return [
        f"nothing matched, so there is no number to report and zero is not the answer; "
        f"the filters asked for were {label}",
        f"the dataset is a fixed historical extract covering {stamp(low)} to {stamp(high)}, "
        f"epoch minutes {low} to {high}, {int(minutes):,} of which carry sessions, and "
        f"nothing outside that window exists",
        "call list_dimensions for every filter value this surface accepts, and tell the "
        "user which part of the question the data does not cover",
    ]


def slice_branch(index: int, dimension: str) -> str:
    filters = {name: "{blank:String}" for name in DIMENSION_NAMES}
    filters[dimension] = f"{{value{index}:String}}"
    return (f"SELECT {{dimension{index}:String}} AS dimension, {{value{index}:String}} AS value, "
            "max(concurrency) AS peak_concurrency, argMax(minute, concurrency) AS peak_minute, "
            "count() AS minutes_present FROM marts.v_occupancy_minute("
            f"country = {filters['country']}, platform = {filters['platform']}, "
            f"video_type = {filters['video_type']}, content_id = {{zero:UInt64}}, "
            "minute_from = {minute_from:UInt32}, minute_to = {minute_to:UInt32})")


def slice_query(pairs: list[tuple[str, str]]) -> tuple[str, dict]:
    """One UNION ALL over the view, one branch per candidate value, so the sum happens before the max."""
    settings = {"param_blank": "", "param_zero": 0,
                "param_minute_from": MINUTE_MIN, "param_minute_to": MINUTE_MAX}
    branches = []
    for index, (dimension, value) in enumerate(pairs):
        settings[f"param_dimension{index}"] = dimension
        settings[f"param_value{index}"] = value
        branches.append(slice_branch(index, dimension))
    sql = ("SELECT * FROM (" + " UNION ALL ".join(branches) +
           ") ORDER BY peak_concurrency DESC, value")
    return sql, settings


def render_table(columns: list[str], rows: list[tuple], cap: int = ROW_CAP) -> str:
    if not rows:
        return "no rows matched"
    shown = [tuple(str(value) for value in row) for row in rows[:cap]]
    widths = [max([len(columns[i])] + [len(row[i]) for row in shown])
              for i in range(len(columns))]
    lines = ["  ".join(name.ljust(widths[i]) for i, name in enumerate(columns))]
    lines += ["  ".join(value.ljust(widths[i]) for i, value in enumerate(row)) for row in shown]
    if len(rows) > cap:
        lines.append(f"{len(rows) - cap} further rows not shown")
    return "\n".join(lines)


def answer(summary: list[str], columns: list[str], rows: list[tuple], result) -> str:
    """Every answer carries its query_id and the rows the server read, so a reader can check it."""
    trace = (f"query_id {result.query_id}, rows read {int(result.statistics.get('rows_read', 0)):,}, "
             f"server elapsed {float(result.statistics.get('elapsed', 0.0)):.3f}s, "
             f"user {AGENT_USER}")
    return "\n".join([*summary, "", render_table(columns, rows), "", trace])


def downsample(rows: list[tuple], cap: int) -> tuple[list[tuple], int]:
    """Keep the maximum of each window so a downsampled series still carries its peak."""
    if len(rows) <= cap:
        return rows, 1
    stride = -(-len(rows) // cap)
    return [max(rows[i:i + stride], key=lambda row: row[1])
            for i in range(0, len(rows), stride)], stride


def tool_concurrency_peak(agent: ClickHouse, arguments: dict):
    """Peak and average concurrency per bucket from marts.v_concurrency.
    No arguments means the whole dataset at minute grain, which is the busiest moment."""
    reject_unknown(arguments, ("grain", "platform", "country", "video_type", "content_id",
                               "title", "minute_from", "minute_to"))
    grain = arguments.get("grain") or DEFAULT_GRAIN
    if grain not in GRAINS:
        raise ToolError(f"grain must be one of {', '.join(GRAINS)}, got {grain!r}")
    minute_from = integer_argument(arguments, "minute_from", MINUTE_MIN, MINUTE_MIN, MINUTE_MAX)
    minute_to = integer_argument(arguments, "minute_to", MINUTE_MAX, MINUTE_MIN, MINUTE_MAX)
    if minute_from > minute_to:
        raise ToolError(f"minute_from {minute_from} is after minute_to {minute_to}")
    content_id = content_id_argument(agent, arguments)
    settings = {**filter_settings(agent, arguments, content_id),
                "param_grain_minutes": GRAINS[grain],
                "param_minute_from": minute_from, "param_minute_to": minute_to}
    label = filter_label(settings, arguments, content_id)
    result = agent.query(PEAK_SQL, settings=settings)
    peak = max((row[1] for row in result.rows), default=0)
    peak_bucket = next((row[0] for row in result.rows if row[1] == peak), None)
    weighted = sum(float(row[2]) * int(row[3]) for row in result.rows)
    minutes = sum(int(row[3]) for row in result.rows)
    if not minutes:
        return answer(empty_note(agent, label), [], [], result), result
    summary = [f"peak concurrency {peak} in the {grain} bucket starting {stamp(peak_bucket)}",
               f"average concurrency {weighted / minutes:.1f} over {minutes:,} active minutes",
               UNIT_NOTE, f"filters: {label}"]
    if len(result.rows) > 1:
        summary.append("buckets are listed busiest first, not in time order")
    rows = [(row[0], stamp(row[0]), row[1], round(float(row[2]), 1), row[3])
            for row in sorted(result.rows, key=lambda row: (-int(row[1]), int(row[0])))]
    columns = ["bucket_minute", "bucket_start", "peak", "average", "minutes_in_bucket"]
    return answer(summary, columns, rows, result), result


def tool_concurrency_series(agent: ClickHouse, arguments: dict):
    """Per minute concurrency from marts.v_occupancy_minute, bound as query parameters.
    Downsampled to stay readable in a chat, keeping the peak of each window."""
    reject_unknown(arguments, ("platform", "country", "video_type", "content_id",
                               "title", "minute_from", "minute_to"))
    minute_from = integer_argument(arguments, "minute_from", MINUTE_MIN, MINUTE_MIN, MINUTE_MAX)
    minute_to = integer_argument(arguments, "minute_to", MINUTE_MAX, MINUTE_MIN, MINUTE_MAX)
    if minute_from > minute_to:
        raise ToolError(f"minute_from {minute_from} is after minute_to {minute_to}")
    content_id = content_id_argument(agent, arguments)
    settings = {**filter_settings(agent, arguments, content_id),
                "param_minute_from": minute_from, "param_minute_to": minute_to}
    label = filter_label(settings, arguments, content_id)
    result = agent.query(SERIES_SQL, settings=settings)
    if not result.rows:
        return answer(empty_note(agent, label), [], [], result), result
    peak = max(row[1] for row in result.rows)
    peak_minute = next(row[0] for row in result.rows if row[1] == peak)
    points, stride = downsample(result.rows, ROW_CAP)
    summary = [f"{len(result.rows):,} minutes with sessions, peak {peak} at {stamp(peak_minute)}",
               f"window: {stamp(result.rows[0][0])} to {stamp(result.rows[-1][0])}",
               UNIT_NOTE, f"filters: {label}"]
    if stride > 1:
        summary.append(f"downsampled to {len(points)} points, each the peak of a "
                       f"{stride} minute window")
    rows = [(row[0], stamp(row[0]), row[1]) for row in points]
    return answer(summary, ["minute", "minute_start", "concurrency"], rows, result), result


def tool_top_slices(agent: ClickHouse, arguments: dict):
    """Each value of one dimension ranked by its own peak.
    The sum across the unfiltered dimensions happens before the maximum, never after."""
    reject_unknown(arguments, ("dimension",))
    dimension = arguments.get("dimension")
    if dimension not in DIMENSION_NAMES:
        raise ToolError(f"dimension must be one of {', '.join(DIMENSION_NAMES)}, "
                        f"got {dimension!r}")
    known = dimensions(agent)[dimension]
    sql, settings = slice_query([(dimension, value) for value in known])
    result = agent.query(sql, settings=settings)
    present = [row for row in result.rows if int(row[4]) > 0]
    summary = [
        f"{dimension} values ranked by peak concurrency, each summed across the other "
        f"dimensions before the maximum is taken",
        f"{len(present)} of {len(known)} values carry sessions",
    ]
    rows = [(row[1], row[2], row[3], stamp(row[3]), row[4]) for row in present]
    columns = [dimension, "peak", "peak_minute", "peak_minute_start", "minutes_present"]
    return answer(summary, columns, rows, result), result


def tool_overcount(agent: ClickHouse, arguments: dict):
    """The foreground-only count against the naive any-open-session count, from marts.v_overcount."""
    reject_unknown(arguments, ())
    result = agent.query(OVERCOUNT_SQL)
    (foreground_peak, foreground_utc, naive_peak, naive_utc,
     peak_pct, foreground_average, naive_average, average_pct) = result.rows[0]
    summary = [
        f"counting every open session instead of only foreground sessions overcounts the "
        f"peak by {float(peak_pct):.1f}% and the average by {float(average_pct):.1f}%",
        f"foreground peak {int(foreground_peak):,} at {foreground_utc} UTC against naive "
        f"peak {int(naive_peak):,} at {naive_utc} UTC",
        f"foreground average {float(foreground_average):.2f} against naive average "
        f"{float(naive_average):.2f}",
        "the two peaks land in different minutes, so the naive count is wrong about when "
        "the busiest moment was as well as how big it was",
    ]
    rows = [("peak", int(foreground_peak), int(naive_peak), f"{float(peak_pct):.2f}%"),
            ("average", round(float(foreground_average), 2), round(float(naive_average), 2),
             f"{float(average_pct):.2f}%")]
    columns = ["measure", "foreground_only", "naive_any_open_session", "overcount"]
    return answer(summary, columns, rows, result), result


def tool_list_dimensions(agent: ClickHouse, arguments: dict):
    """The filter values this server accepts, each checked against the data in one query."""
    reject_unknown(arguments, ())
    known = dimensions(agent, refresh=True)
    pairs = [(dimension, value) for dimension, values in known.items() for value in values]
    sql, settings = slice_query(pairs)
    result = agent.query(sql, settings=settings)
    low, high, minutes, days = agent.query(WINDOW_SQL).rows[0]
    summary = [
        "these are the only filter values this server accepts; anything else is rejected "
        "before it reaches SQL, and leaving a filter out means no filter on that dimension",
        f"the dataset is a fixed historical extract covering {stamp(low)} to {stamp(high)}, "
        f"{float(days):.1f} days, so do not assume the present is inside it",
        f"epoch minutes {low} to {high}, {int(minutes):,} minutes carry sessions",
        f"grain values for concurrency_peak: {', '.join(GRAINS)}, default {DEFAULT_GRAIN}",
    ]
    rows = [(row[0], row[1], row[2], row[4]) for row in
            sorted(result.rows, key=lambda row: (row[0], -int(row[2])))]
    columns = ["dimension", "value", "peak", "minutes_present"]
    return answer(summary, columns, rows, result), result


TOOLS = [
    {
        "name": "concurrency_peak",
        "description": "Answers when foreground concurrency was highest and how high it got. "
                       "Call it with no arguments at all for the busiest moment in the whole "
                       "dataset, which is what a question like what was the busiest time is "
                       "asking for: grain defaults to minute and the window defaults to every "
                       "minute the dataset holds, so no time range is ever needed. Add a "
                       "filter only to narrow the question, and leave a filter out to mean no "
                       "filter on that dimension. Reads marts.v_concurrency.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "grain": {"type": "string", "enum": list(GRAINS),
                          "description": "Bucket size for the peak. Defaults to minute, which "
                                         "is the right grain for the busiest moment. Use hour "
                                         "or day only when the question asks for the busiest "
                                         "hour or the busiest day."},
                "platform": {"type": "string",
                             "description": "Optional platform filter, case sensitive. Leave "
                                            "it out for every platform."},
                "country": {"type": "string",
                            "description": "Optional country filter, case sensitive. Leave it "
                                           "out for every country."},
                "video_type": {"type": "string",
                               "description": "Optional video type filter, case sensitive. "
                                              "Leave it out for both live and vod."},
                "content_id": {"type": "integer", "minimum": 0,
                               "description": "Optional content id filter. Leave it out, or "
                                              "pass 0, for every title."},
                "title": {"type": "string",
                          "description": "Optional title of a single programme, matched "
                                         "case insensitively and by substring against the "
                                         "catalogue. Use this instead of content_id when "
                                         "the question names a show or a film. If the "
                                         "dataset holds no such title the call fails "
                                         "rather than answering for everything, so report "
                                         "that the dataset does not cover it."},
                "minute_from": {"type": "integer", "minimum": MINUTE_MIN, "maximum": MINUTE_MAX,
                                "description": "Optional inclusive start, in minutes since the "
                                               "unix epoch, so a unix timestamp divided by 60. "
                                               "Leave it out for the whole dataset, which is "
                                               "what the busiest moment asks for. Call "
                                               "list_dimensions for the valid range."},
                "minute_to": {"type": "integer", "minimum": MINUTE_MIN, "maximum": MINUTE_MAX,
                              "description": "Optional inclusive end, in minutes since the unix "
                                             "epoch. Leave it out for the whole dataset."},
            },
            "additionalProperties": False,
        },
        "run": tool_concurrency_peak,
    },
    {
        "name": "concurrency_series",
        "description": "The concurrency curve minute by minute, for plotting a shape or "
                       "reading a specific stretch of time. With no arguments it returns the "
                       "whole dataset, downsampled so each point keeps the peak of its window. "
                       "minute_from and minute_to are epoch minutes, not dates, and both "
                       "default to the full window; call list_dimensions for the range the "
                       "dataset covers. For a single peak number prefer concurrency_peak. "
                       "Reads marts.v_occupancy_minute.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "platform": {"type": "string",
                             "description": "Optional platform filter, case sensitive. Leave "
                                            "it out for every platform."},
                "country": {"type": "string",
                            "description": "Optional country filter, case sensitive. Leave it "
                                           "out for every country."},
                "video_type": {"type": "string",
                               "description": "Optional video type filter, case sensitive. "
                                              "Leave it out for both live and vod."},
                "content_id": {"type": "integer", "minimum": 0,
                               "description": "Optional content id filter. Leave it out, or "
                                              "pass 0, for every title."},
                "title": {"type": "string",
                          "description": "Optional title of a single programme, matched "
                                         "case insensitively and by substring against the "
                                         "catalogue. The call fails if the dataset holds "
                                         "no such title, rather than answering for "
                                         "everything."},
                "minute_from": {"type": "integer", "minimum": MINUTE_MIN, "maximum": MINUTE_MAX,
                                "description": "Inclusive start, in minutes since the unix "
                                               "epoch, so a unix timestamp divided by 60. "
                                               "Defaults to the first minute in the dataset. "
                                               "Call list_dimensions for the valid range."},
                "minute_to": {"type": "integer", "minimum": MINUTE_MIN, "maximum": MINUTE_MAX,
                              "description": "Inclusive end, in minutes since the unix epoch. "
                                             "Defaults to the last minute in the dataset."},
            },
            "additionalProperties": False,
        },
        "run": tool_concurrency_series,
    },
    {
        "name": "top_slices",
        "description": "Every value of one dimension ranked by its own peak concurrency, with "
                       "the minute it peaked, so crossovers between slices are visible. Use it "
                       "for the busiest platform, country or video type. Each value is summed "
                       "across the other dimensions before its maximum is taken, so the "
                       "figures are comparable and do not add up to the overall peak.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "dimension": {"type": "string", "enum": list(DIMENSION_NAMES),
                              "description": "Dimension whose values are ranked."},
            },
            "required": ["dimension"],
            "additionalProperties": False,
        },
        "run": tool_top_slices,
    },
    {
        "name": "overcount",
        "description": "How much a naive concurrency count overstates the truth. It compares "
                       "the foreground only count this project publishes against counting "
                       "every session that had the app open at all, background included, and "
                       "returns both peaks, both averages, the percentage gap and the two "
                       "minutes the peaks land in. This is the tool for any question about "
                       "overcounting, background sessions, the naive count, or why "
                       "foreground only matters. Takes no arguments. Reads marts.v_overcount.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "run": tool_overcount,
    },
    {
        "name": "list_dimensions",
        "description": "The accepted platform, country and video type values, and the time "
                       "window the dataset actually covers as both epoch minutes and UTC "
                       "timestamps. The data is a fixed historical extract, not a live feed, "
                       "so call this before filtering or before naming any date, and never "
                       "assume the present is inside the window.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "run": tool_list_dimensions,
    },
]


def listing(agent: ClickHouse) -> list[dict]:
    """The dimension enums are filled from the data at list time, never from a baked in list."""
    try:
        known = dimensions(agent)
    except (ClickHouseError, OSError):
        known = {}
    tools = []
    for tool in TOOLS:
        schema = json.loads(json.dumps(tool["inputSchema"]))
        for name, values in known.items():
            if values and name in schema["properties"]:
                schema["properties"][name]["enum"] = list(values)
        tools.append({"name": tool["name"], "description": tool["description"],
                      "inputSchema": schema})
    return tools


def call_tool(agent: ClickHouse, name: str, arguments: dict) -> dict:
    tool = next((entry for entry in TOOLS if entry["name"] == name), None)
    if tool is None:
        raise ToolError(f"unknown tool {name!r}; available: "
                        f"{', '.join(entry['name'] for entry in TOOLS)}")
    attributes = {f"mcp.argument.{key}": value for key, value in (arguments or {}).items()}
    with otel.span(f"mcp.tool.{name}", **attributes) as record:
        text, result = tool["run"](agent, arguments or {})
        otel.note(record, **{
            "mcp.rows": len(result.rows),
            "db.query_id": result.query_id,
            "clickhouse.read_rows": int(result.statistics.get("rows_read", 0)),
        })
    return {"content": [{"type": "text", "text": text}]}


def rpc_result(request_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def rpc_error(request_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def dispatch(agent: ClickHouse, message: dict) -> dict | None:
    """Returns None for notifications, which take no response body."""
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}
    if method == "initialize":
        return rpc_result(request_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
    if method == "ping":
        return rpc_result(request_id, {})
    if isinstance(method, str) and method.startswith("notifications/"):
        return None
    if method == "tools/list":
        return rpc_result(request_id, {"tools": listing(agent)})
    if method == "tools/call":
        try:
            return rpc_result(request_id, call_tool(
                agent, params.get("name"), params.get("arguments") or {}))
        except (ToolError, ClickHouseError, ValueError, TypeError, KeyError) as exc:
            return rpc_result(request_id, {
                "content": [{"type": "text", "text": f"error: {str(exc)[:800]}"}],
                "isError": True})
    return rpc_error(request_id, -32601, f"unknown method {method!r}")


def health(agent: ClickHouse) -> dict:
    try:
        result = agent.query(WINDOW_SQL)
        low, high, minutes, _ = result.rows[0]
        return {"ok": True, "user": AGENT_USER, "host": agent.config.host,
                "minute_from": int(low), "minute_to": int(high), "minutes": int(minutes),
                "tools": [tool["name"] for tool in TOOLS]}
    except (ClickHouseError, OSError) as exc:
        return {"ok": False, "user": AGENT_USER, "error": str(exc)[:400]}


def handler_for(ch: ClickHouse):
    agent = agent_connection(ch)
    otel.TRACER.attach(agent)
    sessions: list[str] = []
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args) -> None:
            pass

        def session_id(self) -> str:
            return self.headers.get("Mcp-Session-Id") or (sessions[-1] if sessions else "")

        def send_json(self, payload: dict, status: int = 200, session: str = "") -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if session:
                self.send_header("Mcp-Session-Id", session)
            self.end_headers()
            self.wfile.write(body)

        def send_empty(self, status: int, session: str = "") -> None:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            if session:
                self.send_header("Mcp-Session-Id", session)
            self.end_headers()

        def do_POST(self) -> None:
            if self.path.rstrip("/") != ENDPOINT:
                self.send_json({"error": "not found"}, status=404)
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                message = json.loads(raw or b"{}")
            except json.JSONDecodeError as exc:
                self.send_json(rpc_error(None, -32700, f"parse error: {exc}"), status=400)
                return
            if not isinstance(message, dict):
                self.send_json(rpc_error(None, -32600, "batched requests are not supported"),
                               status=400)
                return
            session = self.session_id()
            if message.get("method") == "initialize":
                session = str(uuid.uuid4())
                sessions.append(session)
            with lock:
                response = dispatch(agent, message)
            if response is None:
                self.send_empty(202, session)
                return
            self.send_json(response, session=session)
            with lock:
                otel.TRACER.flush(ch)

        def do_GET(self) -> None:
            if self.path.rstrip("/") == ENDPOINT:
                self.send_json({"error": "this endpoint accepts POST only"}, status=405)
            elif self.path.rstrip("/") == "/health":
                report = health(agent)
                self.send_json(report, status=200 if report["ok"] else 503)
            else:
                self.send_json({"error": "not found"}, status=404)

    return Handler


def serve(ch: ClickHouse, host: str = "0.0.0.0", port: int = 8765) -> None:
    server = ThreadingHTTPServer((host, port), handler_for(ch))
    print(f"clickliv mcp at http://{host}:{port}{ENDPOINT} as {AGENT_USER}")
    print(f"tools: {', '.join(tool['name'] for tool in TOOLS)}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()


if __name__ == "__main__":
    from .cli import load_dotenv

    load_dotenv()
    serve(ClickHouse())

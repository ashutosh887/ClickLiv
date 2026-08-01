"""CSV ingestion. Content must land before events (D12), and that is asserted, not assumed.
The header of the file in hand decides the input schema, so a fresh day cannot silently
default a column away. See docs/unseen-day.md."""

from __future__ import annotations

import bz2
import csv
import gzip
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

from . import otel
from .ch import ClickHouse, ClickHouseError

CONTENT_TYPES = {
    "content_id": "Int64", "title": "String",
    "video_type": "String", "category": "String",
}

RAW_TYPES = {
    "content_id": "UInt64", "video_session_id": "String", "user_id": "String",
    "event_type": "String", "event": "String", "event_timestamp": "Int64",
    "platform": "String", "app_version": "String", "country": "String",
    "audio_language": "String", "subtitle_language": "String",
    "player_version": "String", "session_start_epoch": "Int64",
}

CONTENT_STRUCTURE = ", ".join(f"{n} {t}" for n, t in CONTENT_TYPES.items())

RAW_STRUCTURE = ", ".join(f"{n} {t}" for n, t in RAW_TYPES.items())

DELIMITERS = (",", "\t", ";", "|")

UNPACKERS = {".zip": "zip", ".tar": "tar", ".tgz": "tar", ".bz2": "bz2", ".zst": "zst"}

MEMBER_SUFFIXES = (".csv", ".tsv", ".txt", ".gz", ".bz2", ".zst")

UNPACKED: dict[Path, Path] = {}

CONTENT_PROJECTION = "toUInt64(content_id), title, video_type, category"

RAW_PROJECTION = """
    video_session_id,
    fromUnixTimestamp64Milli(event_timestamp, 'UTC'),
    user_id,
    content_id,
    event_type,
    event,
    platform,
    app_version,
    country,
    audio_language,
    subtitle_language,
    player_version,
    fromUnixTimestamp64Milli(session_start_epoch, 'UTC')
"""


def content_insert(source: str) -> str:
    return (f"INSERT INTO content_meta SELECT {CONTENT_PROJECTION} "
            f"FROM {source} WHERE content_id >= 0")


def raw_insert(source: str) -> str:
    return f"INSERT INTO raw_events SELECT {RAW_PROJECTION} FROM {source}"


INSERT_SETTINGS = {
    "input_format_parallel_parsing": 1,
    "max_insert_block_size": 1_000_000,
    "min_insert_block_size_rows": 1_000_000,
    "date_time_input_format": "best_effort",
    "input_format_with_names_use_header": 0,
}

EXPECTED = {
    "raw_rows": 905_558,
    "sessions": 10_866,
    "users": 9_618,
    "raw_content_ids": 3_357,
    "content_rows": 33_463,
    "join_orphans": 0,
}

INVARIANTS = ("join_orphans",)


def content_csv() -> Path:
    return unpack(Path(os.environ.get("CONTENT_CSV", "data/ch-hackathon-content-data.csv")))


def raw_csv() -> Path:
    return unpack(Path(os.environ.get("RAW_CSV", "data/ch-hackathon-raw-data.csv")))


def container(path: Path) -> str:
    """Which unpacker the name asks for. Gzip answers nothing: it is streamed to the
    server compressed and read through gzip here, so it never needs unpacking."""
    suffixes = [s.lower() for s in path.suffixes]
    if ".tar" in suffixes[-2:]:
        return "tar"
    return UNPACKERS.get(suffixes[-1], "") if suffixes else ""


def only_member(names: list[str], path: Path) -> str:
    """One data file in the archive is unambiguous. Anything else stops the run rather
    than guessing which file the answers should come from."""
    files = [n for n in names if not n.endswith("/") and not n.startswith("__MACOSX/")
             and not Path(n).name.startswith(".")]
    wanted = [n for n in files if Path(n).suffix.lower() in MEMBER_SUFFIXES] or files
    if len(wanted) != 1:
        raise SystemExit(
            f"{path} holds {len(wanted)} data files: {', '.join(wanted) or 'none'}\n"
            f"unpack it yourself and pass the one file you want loaded")
    return wanted[0]


def staged(path: Path, name: str) -> Path:
    stat = path.stat()
    return Path(tempfile.gettempdir()) / f"clickliv-{stat.st_size:x}-{stat.st_mtime_ns:x}-{name}"


def spill(out: Path, source) -> None:
    """Write through a .part name so an interrupted unpack can never be reused as input."""
    part = out.with_name(out.name + ".part")
    with part.open("wb") as fh:
        shutil.copyfileobj(source, fh)
    part.replace(out)


def unzstd(path: Path, out: Path) -> None:
    tool = shutil.which("zstd") or shutil.which("unzstd")
    if not tool:
        raise SystemExit(f"{path} is zstd compressed and no zstd binary is on PATH.\n"
                         f"brew install zstd, or unpack it first: unzstd {path}")
    part = out.with_name(out.name + ".part")
    with part.open("wb") as fh:
        failed = subprocess.run([tool, "-dcq", str(path)], stdout=fh).returncode
    if failed:
        raise SystemExit(f"zstd could not decompress {path}")
    part.replace(out)


def unpack(path: Path) -> Path:
    """Zip, tar, bzip2 and zstd are unpacked once into the temp directory and read from
    there. Plain CSV and gzip are read where they lie."""
    kind = container(path)
    if not kind or not path.exists():
        return path
    if path in UNPACKED:
        return UNPACKED[path]

    if kind == "zip":
        with zipfile.ZipFile(path) as archive:
            name = only_member(archive.namelist(), path)
            out = staged(path, Path(name).name)
            if not out.exists():
                with archive.open(name) as member:
                    spill(out, member)
    elif kind == "tar":
        with tarfile.open(path) as archive:
            name = only_member([m.name for m in archive.getmembers() if m.isfile()], path)
            out = staged(path, Path(name).name)
            if not out.exists():
                spill(out, archive.extractfile(name))
    elif kind == "bz2":
        out = staged(path, path.with_suffix("").name)
        if not out.exists():
            with bz2.open(path, "rb") as member:
                spill(out, member)
    else:
        out = staged(path, path.with_suffix("").name)
        if not out.exists():
            unzstd(path, out)

    print(f"unpacked {path.name} to {out}  {out.stat().st_size:,} bytes")
    UNPACKED[path] = unpack(out)
    return UNPACKED[path]


@dataclass(frozen=True)
class Shape:
    """What a CSV actually looks like: header after renaming, delimiter, compression."""

    path: Path
    header: tuple[str, ...]
    delimiter: str
    gzipped: bool

    def structure(self, types: dict[str, str]) -> str:
        return ", ".join(
            f"`{name}` {types[name]}" if name in types else f"`ignored_{i}` String"
            for i, name in enumerate(self.header))

    def settings(self) -> dict:
        extra = {} if self.delimiter == "," else {"format_csv_delimiter": self.delimiter}
        return {**INSERT_SETTINGS, **extra}

    def describe(self, types: dict[str, str]) -> str:
        extra = [name for name in self.header if name not in types]
        return (f"{self.path.name:<34} {len(self.header)} columns"
                f"{'' if self.delimiter == ',' else f', delimiter {self.delimiter!r}'}"
                f"{', gzip' if self.gzipped else ''}"
                f"{'' if not extra else f', ignoring {len(extra)} extra: ' + ', '.join(extra)}")


def renames() -> dict[str, str]:
    pairs = [p.strip() for p in os.environ.get("CSV_RENAME", "").split(",") if p.strip()]
    return dict(p.split("=", 1) for p in pairs)


def open_text(path: Path):
    """utf-8-sig, so a byte order mark on the first column name is eaten rather than
    becoming part of it."""
    opener = gzip.open if path.suffix.lower() == ".gz" else open
    return opener(path, "rt", newline="", encoding="utf-8-sig")


def column_names(line: str, delimiter: str, types: dict[str, str]) -> tuple[str, ...]:
    """Bind each header name to one of ours by trying it as written, then through
    CSV_RENAME, then case folded, so neither a rename nor a shouted header hides a column."""
    mapping = renames()
    names = []
    for raw in next(csv.reader([line], delimiter=delimiter)):
        name = raw.strip()
        candidates = (name, mapping.get(name, name), name.lower(),
                      mapping.get(name.lower(), name.lower()))
        names.append(next((c for c in candidates if c in types), candidates[1]))
    return tuple(names)


def shape(path: Path, types: dict[str, str]) -> Shape:
    """Read the real header and fail loudly on a missing column. Without this a renamed
    or dropped column inserts a silent default and the answers are quietly wrong."""
    with open_text(path) as fh:
        line = fh.readline()
    if not line.strip():
        raise SystemExit(f"{path} has no header row")
    delimiter = max(DELIMITERS, key=line.count)
    header = column_names(line, delimiter, types)
    missing = [name for name in types if name not in header]
    if missing:
        raise SystemExit(
            f"{path} is missing required column(s): {', '.join(missing)}\n"
            f"header found: {', '.join(header)}\n"
            f"map a renamed column with CSV_RENAME=their_name=our_name,...")
    return Shape(path, header, delimiter, path.suffix.lower() == ".gz")


def ingest(ch: ClickHouse, table: str, statement: str, sh: Shape) -> int:
    """One traced insert. Visible lag is the delay from acknowledgement to the rows being queryable."""
    path = sh.path
    with otel.span(f"ingest.{table}", **{"ingest.source": path.name,
                                         "ingest.bytes": path.stat().st_size}) as span:
        started = time.time()
        ch.insert_csv(statement, path, settings=sh.settings(), gzipped=sh.gzipped)
        acknowledged = time.time()
        rows = int(ch.scalar(f"SELECT count() FROM {table}"))
        otel.note(span, **{
            "ingest.rows": rows,
            "ingest.duration_ms": round((acknowledged - started) * 1000, 1),
            "ingest.visible_lag_ms": round((time.time() - acknowledged) * 1000, 1),
        })
    print(f"{table:<14}{rows:>9,} rows  {acknowledged - started:5.1f}s")
    return rows


def reload_dictionary_everywhere(ch: ClickHouse) -> None:
    """ON CLUSTER reload reaches every replica at once (D32); falls back to a plain
    reload where there is no Keeper, i.e. the local single-node target."""
    try:
        ch.command(f"SYSTEM RELOAD DICTIONARY ON CLUSTER default {ch.config.database}.content_dict")
    except ClickHouseError:
        ch.command("SYSTEM RELOAD DICTIONARY content_dict")


def load(ch: ClickHouse) -> None:
    for path in (content_csv(), raw_csv()):
        if not path.exists():
            raise SystemExit(f"missing input: {path}")

    content_shape = shape(content_csv(), CONTENT_TYPES)
    raw_shape = shape(raw_csv(), RAW_TYPES)
    print(content_shape.describe(CONTENT_TYPES))
    print(raw_shape.describe(RAW_TYPES))

    ch.command("TRUNCATE TABLE content_meta")
    ch.command("TRUNCATE TABLE raw_events")

    n_content = ingest(ch, "content_meta", content_insert(
        f"input('{content_shape.structure(CONTENT_TYPES)}')") + "\nFORMAT CSVWithNames",
        content_shape)

    with open_text(content_csv()) as fh:
        source_rows = sum(1 for _ in csv.reader(fh, delimiter=content_shape.delimiter)) - 1
    if source_rows != n_content:
        print(f"  rejected {source_rows - n_content} row(s) with a negative content_id")

    if n_content == 0:
        raise ClickHouseError(
            "content_meta is empty. Loading events now would enrich against an empty "
            "dictionary and silently produce unlabelled rows. See D12."
        )
    reload_dictionary_everywhere(ch)
    ingest(ch, "raw_events", raw_insert(
        f"input('{raw_shape.structure(RAW_TYPES)}')") + "\nFORMAT CSVWithNames", raw_shape)


RECONCILE_QUERY = """
    SELECT
        (SELECT count() FROM raw_events)                                    AS raw_rows,
        (SELECT uniqExact(video_session_id) FROM raw_events)                AS sessions,
        (SELECT uniqExact(user_id) FROM raw_events)                         AS users,
        (SELECT uniqExact(content_id) FROM raw_events)                      AS raw_content_ids,
        (SELECT count() FROM content_meta)                                  AS content_rows,
        (SELECT count() FROM (
            SELECT DISTINCT content_id FROM raw_events
            WHERE NOT dictHas('content_dict', content_id)))                 AS join_orphans
"""


def reconcile(ch: ClickHouse, retries: int = 3, retry_wait: float = 2.0) -> bool:
    """Diff what landed against the measured tuning CSVs. Retries only a lone
    join_orphans mismatch (defense in depth after reload_dictionary_everywhere)."""
    for attempt in range(retries):
        actual = ch.query(RECONCILE_QUERY).dicts()[0]
        mismatched = {k for k, want in EXPECTED.items() if int(actual[k]) != want}
        if not mismatched or mismatched != {"join_orphans"} or attempt == retries - 1:
            break
        reload_dictionary_everywhere(ch)
        time.sleep(retry_wait)

    ok = True
    drifted = False
    print(f"\n{'check':<18}{'measured':>12}{'FINDINGS.md':>14}")
    for key, want in EXPECTED.items():
        got = int(actual[key])
        if key in INVARIANTS:
            ok &= got == want
            flag = "" if got == want else "  MISMATCH"
        else:
            drifted |= got != want
            flag = "" if got == want else "  differs (expected on a new day)"
        print(f"{key:<18}{got:>12,}{want:>14,}{flag}")

    if int(actual["raw_rows"]) == 0 or int(actual["sessions"]) == 0:
        print("nothing loaded")
        ok = False
    print("input matches the tuning data" if not drifted else
          "input differs from the tuning data; day-invariant checks still enforced")
    return ok


PREFLIGHT_ROWS = 5_000_000

CADENCE_SESSIONS = 5_000

MS_FLOOR = 1_000_000_000_000

MS_CEILING = 20_000_000_000_000

HEARTBEAT = "VideoHeartbeat"

STOP_EVENTS = frozenset({"VideoSessionEnd", "VideoError"})


def data_rows(sh: Shape):
    with open_text(sh.path) as fh:
        reader = csv.reader(fh, delimiter=sh.delimiter)
        next(reader)
        yield from reader


def quantile(histogram: dict[int, int], fraction: float) -> float:
    """Seconds, read off a millisecond histogram so the row count never bounds memory."""
    total = sum(histogram.values())
    if not total:
        return 0.0
    target, seen = fraction * total, 0
    for value in sorted(histogram):
        seen += histogram[value]
        if seen >= target:
            return value / 1000.0
    return max(histogram) / 1000.0


def scan_content(sh: Shape) -> dict:
    index = {name: i for i, name in enumerate(sh.header)}
    ids: set[int] = set()
    video_types: dict[str, int] = {}
    ragged = negative = rows = 0
    for values in data_rows(sh):
        rows += 1
        if len(values) != len(sh.header):
            ragged += 1
            continue
        try:
            cid = int(values[index["content_id"]])
        except ValueError:
            ragged += 1
            continue
        if cid < 0:
            negative += 1
            continue
        ids.add(cid)
        kind = values[index["video_type"]]
        video_types[kind] = video_types.get(kind, 0) + 1
    return {"rows": rows, "ids": ids, "video_types": video_types,
            "ragged": ragged, "negative": negative}


def scan_events(sh: Shape, content_ids: set[int]) -> dict:
    """One pass over the events file, before anything is dropped. Everything here is a
    property of the file alone, so it is knowable while the live tables are still up."""
    index = {name: i for i, name in enumerate(sh.header)}
    columns = len(sh.header)
    counters = {name: {} for name in ("event_type", "event", "platform", "country")}
    sessions: dict[str, list] = {}
    beats: dict[str, set] = {}
    stamps: dict[str, set] = {}
    orphans: list[int] = []
    mixed: set[str] = set()
    rows = ragged = unparsable = seconds_like = orphan_rows = sampled_rows = 0
    low = high = None

    for values in data_rows(sh):
        rows += 1
        if rows > PREFLIGHT_ROWS:
            rows -= 1
            break
        if len(values) != columns:
            ragged += 1
            continue
        try:
            ts = int(values[index["event_timestamp"]])
            cid = int(values[index["content_id"]])
            int(values[index["session_start_epoch"]])
        except ValueError:
            unparsable += 1
            continue
        if not MS_FLOOR <= ts <= MS_CEILING:
            seconds_like += 1
            continue
        low = ts if low is None or ts < low else low
        high = ts if high is None or ts > high else high

        for name, counter in counters.items():
            value = values[index[name]]
            counter[value] = counter.get(value, 0) + 1

        if content_ids and cid not in content_ids:
            orphan_rows += 1
            if len(orphans) < 5:
                orphans.append(cid)

        sid = values[index["video_session_id"]]
        seen = sessions.get(sid)
        kind = values[index["event_type"]]
        if seen is None:
            sessions[sid] = [cid, kind in STOP_EVENTS]
            if len(beats) < CADENCE_SESSIONS:
                beats[sid], stamps[sid] = set(), set()
        else:
            if seen[0] != cid:
                mixed.add(sid)
            seen[1] = seen[1] or kind in STOP_EVENTS
        if sid in stamps:
            sampled_rows += 1
            stamps[sid].add(ts)
            if kind == HEARTBEAT:
                beats[sid].add(ts)

    sampled = beats if sum(len(marks) for marks in beats.values()) else stamps
    histogram: dict[int, int] = {}
    for marks in sampled.values():
        ordered = sorted(marks)
        for a, b in zip(ordered, ordered[1:]):
            histogram[b - a] = histogram.get(b - a, 0) + 1
    distinct = sum(len(marks) for marks in stamps.values())

    return {
        "rows": rows, "ragged": ragged, "unparsable": unparsable,
        "seconds_like": seconds_like, "truncated": rows >= PREFLIGHT_ROWS,
        "low": low, "high": high, "counters": counters,
        "sessions": len(sessions), "mixed": len(mixed),
        "open_sessions": sum(1 for state in sessions.values() if not state[1]),
        "orphan_rows": orphan_rows, "orphans": orphans,
        "histogram": histogram, "cadence_basis": HEARTBEAT if sampled is beats else "all",
        "sampled_sessions": len(sampled),
        "duplicate_rows": sampled_rows - distinct, "sampled_rows": sampled_rows,
    }


def top(counter: dict[str, int], limit: int = 8) -> str:
    ordered = sorted(counter.items(), key=lambda kv: -kv[1])[:limit]
    return ", ".join(f"{value or '(empty)'} {n:,}" for value, n in ordered)


def cadence_check(events: dict, problems: list[str], warnings: list[str]) -> None:
    """The highest severity assumption in the pipeline. GRACE_SECONDS credits a session
    for that long after each heartbeat, so a slower cadence than the grace leaves a hole
    in every session and collapses the peak into a plausible looking wrong number."""
    grace = float(os.environ.get("GRACE_SECONDS", "40"))
    gap = float(os.environ.get("GAP_SECONDS", "90"))
    histogram = events["histogram"]
    samples = sum(histogram.values())
    if samples < 100:
        warnings.append(f"only {samples} inter event gaps sampled, too few to judge the "
                        f"heartbeat cadence against GRACE_SECONDS={grace:g}")
        return
    p50, p90, p99 = (quantile(histogram, f) for f in (0.5, 0.9, 0.99))
    mode = max(histogram.items(), key=lambda kv: kv[1])
    print(f"cadence   p50 {p50:.3f}s  p90 {p90:.3f}s  p99 {p99:.3f}s  "
          f"mode {mode[0] / 1000:g}s x{mode[1]:,}  "
          f"({samples:,} gaps from {events['sampled_sessions']:,} sessions, "
          f"{events['cadence_basis']} rows)")
    if p90 > grace + 1:
        problems.append(
            f"heartbeat cadence p90 is {p90:.3f}s but GRACE_SECONDS is {grace:g}. Every "
            f"session would lose {p90 - grace:.1f}s of credit between heartbeats, "
            f"fragmenting sessions and collapsing the peak into a wrong but plausible "
            f"number. Re derive the pair with make sweep, set GAP_SECONDS and "
            f"GRACE_SECONDS in .env, then run this again.")
    elif grace > 1.5 * p90 + 1:
        warnings.append(f"GRACE_SECONDS={grace:g} is far above the observed p90 gap of "
                        f"{p90:.3f}s, so each session is credited past its last heartbeat")
    if gap <= p90:
        problems.append(f"GAP_SECONDS={gap:g} is at or below the observed p90 gap of "
                        f"{p90:.3f}s, so ordinary heartbeat spacing would be read as an "
                        f"absence and split every session. Re derive it with make sweep.")


def slice_check(events: dict, content: dict, warnings: list[str]) -> None:
    """The benchmark slices name real dimension values. On a day that spells them
    differently the answers are zero rather than wrong, which is easy to miss."""
    from .answers import BENCHMARKS

    present = {"platform": events["counters"]["platform"],
               "country": events["counters"]["country"],
               "video_type": content["video_types"]}
    for spec in BENCHMARKS:
        absent = [f"{dim}={spec[dim]}" for dim in present
                  if spec[dim] and spec[dim] not in present[dim]]
        if absent:
            warnings.append(f"benchmark {spec['label']} filters on "
                            f"{', '.join(absent)}, which this file never contains, so it "
                            f"will answer zero")


def vocabulary_check(events: dict, problems: list[str], warnings: list[str]) -> None:
    from .reference import VOCABULARY

    for column, expected in VOCABULARY.items():
        seen = events["counters"][column]
        known = [token for token in expected if token in seen]
        if not known and column == "event_type":
            problems.append(
                f"none of the event_type values the sessionizer recognises "
                f"({', '.join(expected)}) appears in this file. It contains "
                f"{top(seen)}. Nothing would ever count as playing. Map the tokens or "
                f"update sql/02_sessionize.sql and classify() in reference.py together.")
        elif len(known) < len(expected):
            warnings.append(f"{column} values not present in this file: "
                            + ", ".join(t for t in expected if t not in seen))


def preflight() -> bool:
    """Everything knowable about the new pair of files before a single table is dropped.
    Read only: it touches the files and nothing else."""
    for path in (content_csv(), raw_csv()):
        if not path.exists():
            raise SystemExit(f"missing input: {path}")
        if path.stat().st_size == 0:
            raise SystemExit(f"empty input: {path}")

    content_shape = shape(content_csv(), CONTENT_TYPES)
    raw_shape = shape(raw_csv(), RAW_TYPES)
    print(content_shape.describe(CONTENT_TYPES))
    print(raw_shape.describe(RAW_TYPES))

    content = scan_content(content_shape)
    events = scan_events(raw_shape, content["ids"])
    problems: list[str] = []
    warnings: list[str] = []

    if not events["rows"]:
        raise SystemExit(f"{raw_shape.path} has a header and no data rows")

    span = ""
    if events["low"] is not None:
        span = (f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(events['low'] / 1000))} to "
                f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(events['high'] / 1000))} UTC, "
                f"{(events['high'] - events['low']) / 3_600_000:.1f}h")
    print(f"content   {content['rows']:,} rows, {len(content['ids']):,} usable ids, "
          f"video_type {top(content['video_types'])}")
    print(f"events    {events['rows']:,} rows{' (sampled, the file is longer)' if events['truncated'] else ''}, "
          f"{events['sessions']:,} sessions, {events['open_sessions']:,} with no end event")
    print(f"span      {span}")
    print(f"platform  {top(events['counters']['platform'])}")
    print(f"country   {top(events['counters']['country'])}")
    print(f"event_type {top(events['counters']['event_type'])}")

    cadence_check(events, problems, warnings)
    vocabulary_check(events, problems, warnings)
    slice_check(events, content, warnings)

    if events["ragged"] or content["ragged"]:
        problems.append(f"{events['ragged']:,} event and {content['ragged']:,} content "
                        f"rows do not have as many fields as the header")
    if events["unparsable"]:
        problems.append(f"{events['unparsable']:,} rows have a non integer content_id, "
                        f"event_timestamp or session_start_epoch")
    if events["seconds_like"]:
        problems.append(f"{events['seconds_like']:,} rows have an event_timestamp outside "
                        f"the millisecond epoch range. Seconds where milliseconds are "
                        f"expected land every row in 1970 and every answer is zero.")
    if events["orphan_rows"]:
        problems.append(f"{events['orphan_rows']:,} event rows name a content_id the "
                        f"content file does not have, for example "
                        f"{', '.join(str(c) for c in events['orphans'])}. The load refuses "
                        f"this rather than loading unlabelled rows.")
    if events["mixed"]:
        warnings.append(f"{events['mixed']:,} sessions carry more than one content_id; "
                        f"the interval model takes the last one seen in each minute")
    if events["duplicate_rows"] > 0:
        warnings.append(f"{events['duplicate_rows']:,} of {events['sampled_rows']:,} rows "
                        f"in the sampled sessions repeat a timestamp already seen in the "
                        f"same session; intervals take min and max per segment, so "
                        f"repeats cannot inflate the answer")

    for note in warnings:
        print(f"\nWARN  {note}")
    for note in problems:
        print(f"\nFAIL  {note}")
    print("\npreflight OK, nothing about this file violates an assumption" if not problems
          else f"\npreflight found {len(problems)} problem(s); no table was touched")
    return not problems

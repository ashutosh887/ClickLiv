#!/usr/bin/env bash

set -euo pipefail
cd "$(dirname "$0")/.."

set -a; . ./.env; set +a

SOURCE="${CH_DATABASE:-clickliv}"
TARGET="${1:-clickliv_sample}"
MARTS_SOURCE="marts"
MARTS_TARGET="marts_$TARGET"

TABLES="raw_events content_meta active_intervals session_minutes minute_occupancy minute_deltas ref_intervals ref_rollup"
CONTRACT_VIEWS="v_data_window v_dimension_values v_titles v_overcount v_naive_vs_foreground"

case "$TARGET" in
  *_sample) ;;
  *) echo "refusing to run: the target database must be named *_sample, got '$TARGET'" >&2
     exit 1;;
esac

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

raw() {
  curl -sS --max-time 900 \
    "https://$CH_HOST:$CH_PORT/?database=$3&default_format=${4:-TSVRaw}" \
    -u "$1" --data-binary "$2" 2>&1 || true
}

ch() {
  local out
  out=$(raw "$CH_USER:$CH_PASSWORD" "$1" "${2:-$TARGET}" "${3:-TSVRaw}")
  if printf '%s' "$out" | grep -q 'DB::Exception'; then
    printf 'FAIL  %s\n' "$(printf '%s' "$out" | head -2)" >&2
    return 1
  fi
  printf '%s' "$out"
}

agent() {
  raw "marts_agent:$MARTS_PASSWORD" "$1" default
}

fail=0
note() { printf '%s\n' "$1"; }
head2() { printf '\n===== %s =====\n' "$1"; }

head2 "plan"
note "source      $SOURCE, read only"
note "target      $TARGET"
note "marts       $MARTS_TARGET, adapted from sql/06_marts.sql rather than run through the CLI"
note "guard       every write is checked to name a database ending in _sample before it is sent"

{
  for table in $TABLES; do
    printf 'DROP TABLE IF EXISTS %s.%s;\n' "$TARGET" "$table"
    printf 'CREATE TABLE %s.%s AS %s.%s;\n' "$TARGET" "$table" "$SOURCE" "$table"
    printf 'INSERT INTO %s.%s SELECT * FROM %s.%s;\n' "$TARGET" "$table" "$SOURCE" "$table"
  done

  # An unqualified dictGet resolves against the session database, so the copy needs a
  # dictionary of its own or anything running with $TARGET as the default breaks.
  printf 'DROP DICTIONARY IF EXISTS %s.content_dict;\n' "$TARGET"
  cat <<SQL
CREATE DICTIONARY $TARGET.content_dict
    (content_id UInt64, title String, video_type String, category String)
    PRIMARY KEY content_id
    SOURCE(CLICKHOUSE(USER '$CH_USER' PASSWORD '$CH_PASSWORD'
                      DB '$TARGET' TABLE 'content_meta'))
    LAYOUT(HASHED())
    LIFETIME(MIN 300 MAX 600);
SQL
} > "$WORK/copy.sql"

MARTS_DB="$MARTS_TARGET" python3 - sql/06_marts.sql "$WORK/marts.sql" <<'PY'
import os, pathlib, re, sys
src = pathlib.Path(sys.argv[1]).read_text()
env = {"MARTS_DB": os.environ["MARTS_DB"], "CH_USER": os.environ["CH_USER"],
       "MARTS_PASSWORD": os.environ["MARTS_PASSWORD"]}
def sub(m):
    key = m.group(1)
    if key not in env:
        sys.exit(f"sql/06_marts.sql references ${{{key}}}, which this script does not bind")
    return env[key]
pathlib.Path(sys.argv[2]).write_text(re.sub(r"\$\{(\w+)\}", sub, src))
PY

cat "$WORK/copy.sql" "$WORK/marts.sql" > "$WORK/plan.sql"

TARGET_DB="$TARGET" MARTS_TARGET_DB="$MARTS_TARGET" \
python3 - "$WORK/plan.sql" "$WORK/stmt" <<'PY'
import os, pathlib, re, sys

allowed = {os.environ["TARGET_DB"], os.environ["MARTS_TARGET_DB"]}


def split_statements(sql):
    """Same rule as ClickHouse.script: split on semicolons outside quotes and comments."""
    out, buf, quote, i = [], [], None, 0
    while i < len(sql):
        c = sql[i]
        if quote:
            buf.append(c)
            if c == "\\" and i + 1 < len(sql):
                buf.append(sql[i + 1]); i += 2; continue
            if c == quote:
                quote = None
        elif c in "'\"`":
            quote = c; buf.append(c)
        elif sql[i:i + 2] == "--":
            end = sql.find("\n", i)
            i = len(sql) if end == -1 else end
            continue
        elif c == ";":
            s = "".join(buf).strip()
            if s:
                out.append(s)
            buf = []
        else:
            buf.append(c)
        i += 1
    s = "".join(buf).strip()
    if s:
        out.append(s)
    return out


DATABASE = re.compile(r"^(?:CREATE|DROP|ATTACH)\s+DATABASE\s+(?:IF\s+(?:NOT\s+)?EXISTS\s+)?"
                      r"`?(\w+)`?", re.I)
OBJECT = re.compile(r"^(?:CREATE(?:\s+OR\s+REPLACE)?|DROP|TRUNCATE|RENAME|ATTACH)\s+"
                    r"(?:TEMPORARY\s+)?(?:MATERIALIZED\s+)?(?:TABLE|VIEW|DICTIONARY)\s+"
                    r"(?:IF\s+(?:NOT\s+)?EXISTS\s+)?([\w.`]+)", re.I)
INSERT = re.compile(r"^INSERT\s+INTO\s+(?:TABLE\s+)?([\w.`]+)", re.I)
ALTER = re.compile(r"^ALTER\s+TABLE\s+([\w.`]+)", re.I)
GRANT = re.compile(r"^GRANT\s+.+?\s+ON\s+`?(\w+)`?\.", re.I | re.S)
ACCESS = re.compile(r"^CREATE\s+(?:ROLE|USER|SETTINGS\s+PROFILE)\s+IF\s+NOT\s+EXISTS\s", re.I)


def target_database(statement):
    """The database a statement writes to, or None when it only reads."""
    for pattern in (DATABASE, GRANT):
        m = pattern.match(statement)
        if m:
            return m.group(1)
    for pattern in (OBJECT, INSERT, ALTER):
        m = pattern.match(statement)
        if m:
            name = m.group(1).replace("`", "")
            if "." not in name:
                return f"<unqualified {name}>"
            return name.split(".")[0]
    if ACCESS.match(statement) or re.match(r"^(SELECT|WITH|SHOW|DESCRIBE)\b", statement, re.I):
        return None
    return "<unrecognised>"


statements = split_statements(pathlib.Path(sys.argv[1]).read_text())
for n, statement in enumerate(statements):
    db = target_database(statement)
    if db is not None and not db.endswith("_sample"):
        sys.exit(f"ABORT  statement {n} would write to '{db}', which is not a _sample "
                 f"database:\n  {statement[:200]}")
    pathlib.Path(f"{sys.argv[2]}_{n:03d}.sql").write_text(statement)
print(f"{len(statements)} statements checked, every write lands in a _sample database")
PY

head2 "copying $SOURCE into $TARGET and building $MARTS_TARGET"
ch "CREATE DATABASE IF NOT EXISTS $TARGET" default >/dev/null
for path in "$WORK"/stmt_*.sql; do
  tr '\n' ' ' < "$path" \
    | sed -E "s/(PASSWORD|BY) '[^']*'/\1 '[redacted]'/g" \
    | cut -c1-96
  ch "$(cat "$path")" >/dev/null
done

head2 "row counts and content hashes, $SOURCE against $TARGET"
printf '%-18s %14s %14s %22s %22s  %s\n' table "$SOURCE" "$TARGET" "hash $SOURCE" "hash $TARGET" verdict
for table in $TABLES; do
  read -r src_rows src_hash <<<"$(ch "SELECT count(), sum(cityHash64(*)) FROM $SOURCE.$table")"
  read -r dst_rows dst_hash <<<"$(ch "SELECT count(), sum(cityHash64(*)) FROM $TARGET.$table")"
  if [ "$src_rows" = "$dst_rows" ] && [ "$src_hash" = "$dst_hash" ]; then
    verdict="match"
  else
    verdict="MISMATCH"; fail=1
  fi
  printf '%-18s %14s %14s %22s %22s  %s\n' \
    "$table" "$src_rows" "$dst_rows" "$src_hash" "$dst_hash" "$verdict"
done

read -r src_proj dst_proj <<<"$(ch "SELECT countIf(database = '$SOURCE'), countIf(database = '$TARGET')
                                    FROM system.tables
                                    WHERE name = 'minute_occupancy'
                                      AND create_table_query LIKE '%PROJECTION%'")"
if [ "$src_proj" = "$dst_proj" ]; then
  note "minute_occupancy carries proj_content_minute in both databases"
else
  note "MISMATCH  projection present in $SOURCE ($src_proj), in $TARGET ($dst_proj)"
  fail=1
fi

head2 "the headline, $MARTS_SOURCE against $MARTS_TARGET"
overcount() {
  ch "SELECT foreground_peak, foreground_peak_utc, naive_peak, naive_peak_utc,
             round(peak_overcount_pct, 1), round(average_overcount_pct, 1),
             round(foreground_average, 4), round(naive_average, 4)
      FROM $1.v_overcount"
}
src_head=$(overcount "$MARTS_SOURCE")
dst_head=$(overcount "$MARTS_TARGET")
printf '%-26s %s\n' "$MARTS_SOURCE" "$src_head"
printf '%-26s %s\n' "$MARTS_TARGET" "$dst_head"
if [ "$src_head" = "$dst_head" ]; then
  note "match on both peaks, both peak minutes, and the peak and average overcount"
else
  note "MISMATCH  the copy does not reproduce the live headline"
  fail=1
fi

head2 "the rest of the marts contract answers from $MARTS_TARGET"
printf '%-26s %12s %12s  %s\n' view "$MARTS_SOURCE" "$MARTS_TARGET" verdict
for view in $CONTRACT_VIEWS; do
  src_rows=$(ch "SELECT count() FROM (SELECT * FROM $MARTS_SOURCE.$view)")
  dst_rows=$(ch "SELECT count() FROM (SELECT * FROM $MARTS_TARGET.$view)")
  if [ "$src_rows" = "$dst_rows" ] && [ "$dst_rows" -gt 0 ]; then
    verdict="match"
  else
    verdict="MISMATCH"; fail=1
  fi
  printf '%-26s %12s %12s  %s\n' "$view" "$src_rows" "$dst_rows" "$verdict"
done
note "v_data_window on the copy: $(ch "SELECT min_utc, max_utc, minutes_with_sessions, occupancy_rows FROM $MARTS_TARGET.v_data_window")"

head2 "marts_agent reaches the copy through marts only"
granted=$(agent "SELECT foreground_peak FROM $MARTS_TARGET.v_overcount")
if printf '%s' "$granted" | grep -q 'DB::Exception'; then
  note "FAIL     marts_agent cannot read $MARTS_TARGET.v_overcount"
  note "         $(printf '%s' "$granted" | head -1 | cut -c1-150)"
  fail=1
else
  note "granted  marts_agent reads $MARTS_TARGET.v_overcount and gets $granted"
fi

refused=$(agent "SELECT count() FROM $TARGET.minute_occupancy")
if printf '%s' "$refused" | grep -q 'ACCESS_DENIED'; then
  note "refused  marts_agent on $TARGET.minute_occupancy"
  note "         $(printf '%s' "$refused" | head -1 | cut -c1-150)"
else
  note "FAIL     marts_agent reached $TARGET.minute_occupancy, which it must not"
  fail=1
fi

head2 "storage"
ch "SELECT database,
           formatReadableSize(sum(bytes_on_disk))          AS on_disk,
           formatReadableSize(sum(data_uncompressed_bytes)) AS uncompressed,
           sum(rows)                                        AS rows
    FROM system.parts
    WHERE active AND database IN ('$SOURCE', '$TARGET')
    GROUP BY database
    ORDER BY database" "$TARGET" TSVWithNames
printf '\n'

head2 "result"
if [ "$fail" = 0 ]; then
  note "$TARGET is a verified twin of $SOURCE, served by $MARTS_TARGET."
  note "Nothing in this run wrote to $SOURCE or to $MARTS_SOURCE."
else
  note "the copy is not a faithful twin, see the MISMATCH lines above"
fi
exit "$fail"

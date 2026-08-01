#!/usr/bin/env bash
# Run every named query in sql/09_dashboard.sql against the Cloud service and check
# it still returns rows. Queries run against the `default` database, which is what the
# Cloud SQL console uses, so an unqualified table name fails here the same way it would
# fail on a dashboard tile.
#
#   ./scripts/verify_dashboard.sh

set -euo pipefail
cd "$(dirname "$0")/.."

set -a; . ./.env; set +a

SQL_FILE="sql/09_dashboard.sql"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

python3 - "$SQL_FILE" "$WORK" <<'PY'
import re, sys, pathlib
src = pathlib.Path(sys.argv[1]).read_text()
parts = re.split(r'^-- name: (\w+)\n', src, flags=re.M)
for i in range(1, len(parts), 2):
    pathlib.Path(sys.argv[2], parts[i]).write_text(parts[i + 1].strip().rstrip(';'))
PY

fail=0
for name in $(ls "$WORK"); do
  out=$(curl -sS --max-time 300 \
    "https://$CH_HOST:$CH_PORT/?database=default&default_format=TSV" \
    -u "$CH_USER:$CH_PASSWORD" --data-binary "@$WORK/$name" 2>&1) || true
  if printf '%s' "$out" | grep -q 'DB::Exception'; then
    printf '%-28s FAIL  %s\n' "$name" "$(printf '%s' "$out" | head -1 | cut -c1-110)"
    fail=1
  elif [ -z "$out" ]; then
    printf '%-28s FAIL  no rows\n' "$name"
    fail=1
  else
    printf '%-28s ok    %s rows\n' "$name" "$(printf '%s\n' "$out" | wc -l | tr -d ' ')"
  fi
done

echo
echo "headline"
curl -sS --max-time 60 "https://$CH_HOST:$CH_PORT/?database=default&default_format=Vertical" \
  -u "$CH_USER:$CH_PASSWORD" --data-binary \
  "SELECT foreground_peak, foreground_peak_utc, naive_peak, naive_peak_utc,
          round(peak_overcount_pct, 1) AS peak_overcount_pct FROM marts.v_overcount"

exit $fail

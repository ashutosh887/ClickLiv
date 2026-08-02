#!/usr/bin/env bash

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
    body = re.sub(r'^\s*--.*$', '', parts[i + 1], flags=re.M)
    pathlib.Path(sys.argv[2], parts[i]).write_text(body.strip().rstrip(';'))
PY

fail=0

if ! python3 - "$WORK" <<'PY'
import re, sys, pathlib
found = []
for path in sorted(pathlib.Path(sys.argv[1]).iterdir()):
    for name in re.findall(r'\b(?:FROM|JOIN)\s+`?([A-Za-z_][A-Za-z0-9_.]*)`?\s*\(',
                           path.read_text(), re.I):
        found.append((path.name, name))
for query, name in found:
    print(f'{query:<28} FAIL  {name}() in FROM or JOIN, the console dashboard runner refuses that shape')
sys.exit(1 if found else 0)
PY
then fail=1; fi

for name in $(ls "$WORK"); do
  out=$(curl -sS --max-time 300 \
    "https://$CH_HOST:$CH_PORT/?database=default&default_format=TSVWithNames" \
    -u "$CH_USER:$CH_PASSWORD" --data-binary "@$WORK/$name" 2>&1) || true
  rows=$(( $(printf '%s\n' "$out" | wc -l) - 1 ))
  if printf '%s' "$out" | grep -q 'DB::Exception'; then
    printf '%-28s FAIL  %s\n' "$name" "$(printf '%s' "$out" | head -1 | cut -c1-110)"
    fail=1
  elif [ -z "$out" ] || [ "$rows" -lt 1 ]; then
    printf '%-28s FAIL  no rows\n' "$name"
    fail=1
  else
    printf '%-28s ok    %s rows\n' "$name" "$rows"
    if [ "$rows" -le 12 ]; then
      printf '%s\n' "$out" | awk '{ print "    " $0 }'
    fi
  fi
done

echo
echo "headline"
curl -sS --max-time 60 "https://$CH_HOST:$CH_PORT/?database=default&default_format=Vertical" \
  -u "$CH_USER:$CH_PASSWORD" --data-binary \
  "SELECT foreground_peak, foreground_peak_utc, naive_peak, naive_peak_utc,
          round(peak_overcount_pct, 1) AS peak_overcount_pct,
          round(average_overcount_pct, 1) AS average_overcount_pct,
          (SELECT count() FROM marts.v_naive_vs_foreground) AS minutes_total,
          (SELECT count() FROM marts.v_naive_vs_foreground WHERE foreground_concurrency > 0)
              AS minutes_with_foreground,
          (SELECT countIf(naive_concurrency < foreground_concurrency)
             FROM marts.v_naive_vs_foreground) AS minutes_naive_below_foreground
   FROM marts.v_overcount"

exit $fail

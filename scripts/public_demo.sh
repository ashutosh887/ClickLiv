#!/usr/bin/env bash
# Give every local demo surface a public HTTPS URL using Cloudflare quick tunnels.
# No Cloudflare account and no domain needed. The URLs die when you stop the script,
# which is exactly the expiry you want after the event.
#
#   ./scripts/public_demo.sh          start tunnels and print the URLs
#   ./scripts/public_demo.sh --stop   kill every tunnel this script started
#
# LibreChat and Langfuse pin their own origin, so after the URLs appear this script
# writes them into .env and recreates those two containers. Without that step both
# serve a login page that cannot actually log in.

set -euo pipefail
cd "$(dirname "$0")/.."

RUN_DIR=".clickhouse/tunnels"
SURFACES="librechat:3080 langfuse:3300 dashboard:8090 mcp:8765 clickstack:8080"

if [ "${1:-}" = "--stop" ]; then
  if [ -d "$RUN_DIR" ]; then
    for pidfile in "$RUN_DIR"/*.pid; do
      [ -f "$pidfile" ] || continue
      kill "$(cat "$pidfile")" 2>/dev/null && echo "stopped $(basename "$pidfile" .pid)"
      rm -f "$pidfile"
    done
  fi
  echo "all tunnels stopped"
  exit 0
fi

command -v cloudflared >/dev/null || { echo "cloudflared missing: brew install cloudflared"; exit 1; }
mkdir -p "$RUN_DIR"

for pair in $SURFACES; do
  name="${pair%%:*}"; port="${pair##*:}"
  if ! curl -fsS -o /dev/null --max-time 3 "http://localhost:$port" 2>/dev/null \
     && ! curl -fsS -o /dev/null --max-time 3 "http://localhost:$port/health" 2>/dev/null; then
    echo "skipping $name, nothing listening on $port"
    continue
  fi
  cloudflared tunnel --url "http://localhost:$port" > "$RUN_DIR/$name.log" 2>&1 &
  echo $! > "$RUN_DIR/$name.pid"
done

echo "waiting for Cloudflare to assign URLs"
sleep 20

LIBRECHAT_URL=""; LANGFUSE_URL=""
printf '\n%-12s %s\n' "SURFACE" "PUBLIC URL"
for pair in $SURFACES; do
  name="${pair%%:*}"
  [ -f "$RUN_DIR/$name.log" ] || continue
  url=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$RUN_DIR/$name.log" | head -1 || true)
  printf '%-12s %s\n' "$name" "${url:-not assigned yet, check $RUN_DIR/$name.log}"
  [ "$name" = "librechat" ] && LIBRECHAT_URL="$url"
  [ "$name" = "langfuse" ] && LANGFUSE_URL="$url"
done

if [ -n "$LIBRECHAT_URL" ] || [ -n "$LANGFUSE_URL" ]; then
  echo
  echo "pointing the two origin-pinned apps at their public URLs"
  grep -v '^LIBRECHAT_PUBLIC_URL=\|^LANGFUSE_PUBLIC_URL=' .env > .env.tmp || true
  [ -n "$LIBRECHAT_URL" ] && echo "LIBRECHAT_PUBLIC_URL=$LIBRECHAT_URL" >> .env.tmp
  [ -n "$LANGFUSE_URL" ] && echo "LANGFUSE_PUBLIC_URL=$LANGFUSE_URL" >> .env.tmp
  mv .env.tmp .env
  docker compose --profile llm --profile chat up -d >/dev/null 2>&1
  echo "recreated librechat and langfuse; give them about a minute, then log in"
fi

echo
echo "stop everything with: ./scripts/public_demo.sh --stop"

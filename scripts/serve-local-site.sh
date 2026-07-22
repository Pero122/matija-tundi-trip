#!/bin/zsh
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd -P)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd -P)"
SITE_ROOT="$REPO_ROOT/deploy/public"
PORT="${CUTETRIP_PORT:-8799}"
PYTHON="$(command -v python3 || true)"

if [[ -z "$PYTHON" ]]; then
  print -u2 "python3 is required to serve the local trip site"
  exit 1
fi
if [[ ! "$PORT" =~ '^[0-9]+$' ]]; then
  print -u2 "CUTETRIP_PORT must be numeric: $PORT"
  exit 1
fi
if [[ ! -f "$SITE_ROOT/trip-plan.html" || ! -f "$SITE_ROOT/trip-map.html" || ! -f "$SITE_ROOT/budapest-london/tripadvisor/index.html" ]]; then
  print -u2 "generated site is missing; run $REPO_ROOT/deploy/build.sh"
  exit 1
fi

exec "$PYTHON" -u -m http.server "$PORT" \
  --bind 127.0.0.1 \
  --directory "$SITE_ROOT"

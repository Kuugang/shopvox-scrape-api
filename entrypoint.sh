#!/bin/sh
set -e

API_PORT="${PORT:-8000}"
CDP_PORT="${CDP_PORT:-9222}"

echo "Starting Chrome headless on 0.0.0.0:${CDP_PORT}..."

# Import certs in the background (if that script exits non-zero, it won't kill this script)
import_cert.sh "$HOME" >/var/log/import_cert.log 2>&1 &

CHROME_ARGS="--disable-gpu \
  --headless \
  --no-sandbox \
  --remote-debugging-address=0.0.0.0 \
  --remote-debugging-port=${CDP_PORT} \
  --user-data-dir=/data \
  --disable-dev-shm-usage"

# Allow extra flags from env
[ -n "${CHROME_OPTS}" ] && CHROME_ARGS="${CHROME_ARGS} ${CHROME_OPTS}"

# Start Chrome in the BACKGROUND
/usr/bin/google-chrome-unstable $CHROME_ARGS >/var/log/chrome.log 2>&1 &

# Optional: wait for the Chrome DevTools endpoint to be ready (up to ~10s)
for i in $(seq 1 20); do
  if curl -fsS "http://127.0.0.1:${CDP_PORT}/json/version" >/dev/null 2>&1; then
    echo "Chrome DevTools is up."
    break
  fi
  sleep 0.5
done

echo "Starting FastAPI app on 0.0.0.0:${API_PORT}"
# Exec so uvicorn becomes PID 1 (good for container stop signals)
exec python3 -m uvicorn shopvox_scrape_api:app --host 0.0.0.0 --port "${API_PORT}"


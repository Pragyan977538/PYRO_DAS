#!/usr/bin/env bash
# FireWatch: run the three-minute demo path (Stage 10).
#
#   scripts/demo.sh              # each stop opens in the browser for 30 s
#   scripts/demo.sh --pause 10
#   scripts/demo.sh --check      # verify every stop against the API, open nothing
#
# Starts the API and map on http://localhost:8000 if nothing answers there, and
# stops it again at the end; a server that was already running is left alone.
# Needs the database up (make db) and the 2024 replay loaded (make inference
# events risk).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -x "$REPO/.venv/bin/python" ]]; then
  PY="$REPO/.venv/bin/python"
else
  PY="$REPO/.venv/Scripts/python.exe"          # Git Bash on Windows
fi
HEALTH="http://localhost:8000/api/health"
[[ -x "$PY" ]] || { echo ">> FAILED: .venv missing; run make install"; exit 1; }

api_up() { curl -fsS -m 5 "$HEALTH" >/dev/null 2>&1; }

SERVER=""
cleanup() { if [[ -n "$SERVER" ]]; then kill "$SERVER" 2>/dev/null || true; echo ">> API stopped"; fi; }
trap cleanup EXIT

if ! api_up; then
  echo ">> starting the API on http://localhost:8000"
  (cd "$REPO" && exec "$PY" -m uvicorn firewatch.api.main:app --host 127.0.0.1 --port 8000 \
     >/dev/null 2>&1) &
  SERVER=$!
  for _ in $(seq 60); do api_up && break; sleep 1; done
  api_up || { echo ">> FAILED: the API did not come up in 60 s (is the database running?)"; exit 1; }
fi

"$PY" "$REPO/scripts/demo.py" "$@"

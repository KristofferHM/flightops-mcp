#!/usr/bin/env bash
# The load test, reproducibly, on a host without Docker.
#
#   bash scripts/load_test.sh                         # 50 rps for 60 s
#   LOAD_RPS=20 LOAD_DURATION=30 bash scripts/load_test.sh
#
# Starts, as local processes on 127.0.0.1:
#
#   idp            :9000   the local IdP (services/idp), minting the load test's user token
#   mock upstream  :9200   OpenSky, AviationWeather and Schiphol in their documented shapes
#   server         :8002   flightops-mcp, streamable HTTP, every upstream pointed at the mock
#
# then checks the second adapter through the stack (scripts/second_upstream.py), runs
# scripts/load_test.py, and stops everything on exit. No third party is called and no
# real credit is spent. Secrets are generated for this run and die with it.
#
# The result is written to $LOAD_OUT (default: load-test.json in the log directory).
# MOCK_LATENCY_MS (default 25) is the simulated upstream time per data request.

set -u

RPS="${LOAD_RPS:-50}"
DURATION="${LOAD_DURATION:-60}"
SERVER_PORT="${LOAD_SERVER_PORT:-8002}"
export MOCK_LATENCY_MS="${MOCK_LATENCY_MS:-25}"
RESOURCE="http://127.0.0.1:${SERVER_PORT}/mcp"
LOG_DIR="${LOAD_LOG_DIR:-$(mktemp -d)}"
mkdir -p "$LOG_DIR"
if command -v cygpath >/dev/null 2>&1; then
  LOG_DIR_NATIVE="$(cygpath -w "$LOG_DIR")"
else
  LOG_DIR_NATIVE="$LOG_DIR"
fi
OUT="${LOAD_OUT:-$LOG_DIR_NATIVE/load-test.json}"

secret() { uv run python -c "import secrets; print(secrets.token_hex(24))"; }
AGENT_SECRET="$(secret)"
OPENSKY_SECRET="$(secret)"
SCHIPHOL_SECRET="$(secret)"

PIDS=()
stop_all() {
  local pid winpid
  for pid in "${PIDS[@]}"; do
    winpid="$(cat "/proc/$pid/winpid" 2>/dev/null || echo "$pid")"
    if command -v taskkill >/dev/null 2>&1; then
      MSYS2_ARG_CONV_EXCL='*' taskkill /F /T /PID "$winpid" >/dev/null 2>&1 || true
    else
      kill "$pid" 2>/dev/null || true
    fi
  done
}
trap stop_all EXIT

start() {
  local log="$1"
  shift
  PYTHONUNBUFFERED=1 "$@" >"$log" 2>&1 &
  PIDS+=("$!")
}

wait_for() {
  local url="$1" tries=0
  until uv run python -c "import urllib.request as u; u.urlopen('$url', timeout=2)" >/dev/null 2>&1; do
    tries=$((tries + 1))
    if [ "$tries" -gt 60 ]; then echo "not ready: $url" >&2; exit 1; fi
    sleep 1
  done
}

IDP_ISSUER="http://127.0.0.1:9000" \
IDP_CLIENTS="[{\"client_id\": \"flightops-agent\", \"client_secret\": \"${AGENT_SECRET}\", \"app_roles\": {\"${RESOURCE}\": [\"flightops.reference.read\"]}}]" \
  start "$LOG_DIR/idp.log" uv run uvicorn --factory services.idp.app:create_app --host 127.0.0.1 --port 9000 --no-access-log

MOCK_OPENSKY_CLIENT_SECRET="$OPENSKY_SECRET" \
MOCK_SCHIPHOL_CLIENT_SECRET="$SCHIPHOL_SECRET" \
  start "$LOG_DIR/mock.log" uv run uvicorn --factory services.mock_upstream.app:create_app --host 127.0.0.1 --port 9200 --no-access-log

wait_for "http://127.0.0.1:9000/.well-known/jwks.json"
wait_for "http://127.0.0.1:9200/healthz"

FLIGHTOPS_LOG_LEVEL="INFO" \
FLIGHTOPS_AUTH_ISSUER="http://127.0.0.1:9000" \
FLIGHTOPS_AUTH_JWKS_URL="http://127.0.0.1:9000/.well-known/jwks.json" \
FLIGHTOPS_RESOURCE_SERVER_URL="$RESOURCE" \
FLIGHTOPS_OPENSKY_BASE_URL="http://127.0.0.1:9200/opensky/api" \
FLIGHTOPS_OPENSKY_AUTH_URL="http://127.0.0.1:9200/opensky/token" \
FLIGHTOPS_OPENSKY_CLIENT_ID="flightops-opensky" \
FLIGHTOPS_OPENSKY_CLIENT_SECRET="$OPENSKY_SECRET" \
FLIGHTOPS_AVWX_BASE_URL="http://127.0.0.1:9200/noaa/api/data" \
FLIGHTOPS_SCHIPHOL_BASE_URL="http://127.0.0.1:9200/schiphol/public" \
FLIGHTOPS_SCHIPHOL_AUTH_URL="http://127.0.0.1:9200/schiphol/oauth/token" \
FLIGHTOPS_SCHIPHOL_CLIENT_ID="flightops-schiphol" \
FLIGHTOPS_SCHIPHOL_CLIENT_SECRET="$SCHIPHOL_SECRET" \
  start "$LOG_DIR/server.log" uv run flightops-mcp --transport streamable-http --host 127.0.0.1 --port "$SERVER_PORT"

wait_for "http://127.0.0.1:${SERVER_PORT}/readyz"

echo "== the second adapter through the stack =="
MATRIX_SERVER="http://127.0.0.1:${SERVER_PORT}" MATRIX_IDP="http://127.0.0.1:9000" \
MATRIX_MOCK="http://127.0.0.1:9200" uv run python scripts/second_upstream.py || exit 1

echo "== load: ${RPS} rps for ${DURATION} s, mock upstream latency ${MOCK_LATENCY_MS} ms =="
uv run python scripts/load_test.py --server "http://127.0.0.1:${SERVER_PORT}" \
  --rps "$RPS" --duration "$DURATION" --out "$OUT"
status=$?

echo "result: $OUT"
echo "service logs: $LOG_DIR"
exit "$status"

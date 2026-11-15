#!/usr/bin/env bash
# Run the Phase 1 acceptance matrix on a host without Docker.
#
# Starts the same services docker-compose.yml describes, as local processes:
#
#   idp        127.0.0.1:9000   issuer http://127.0.0.1:9000
#   ops-api    127.0.0.1:9100
#   server     127.0.0.1:8001   (8000 is often taken; override with MATRIX_SERVER_PORT)
#   other idp  127.0.0.1:9001   a second, untrusted issuer, for the wrong-issuer line
#
# then runs scripts/local_matrix.py against them and stops everything on exit.
#
# Secrets come from the environment, never from this file. Create them once:
#
#   echo "DEV_AGENT_CLIENT_SECRET=$(openssl rand -hex 24)" >> .env
#   echo "DEV_MCP_CLIENT_SECRET=$(openssl rand -hex 24)" >> .env
#   set -a; . ./.env; set +a
#   bash scripts/local_matrix.sh

set -u

: "${DEV_AGENT_CLIENT_SECRET:?export it first, see the top of this file}"
: "${DEV_MCP_CLIENT_SECRET:?export it first, see the top of this file}"

SERVER_PORT="${MATRIX_SERVER_PORT:-8001}"
RESOURCE="http://127.0.0.1:${SERVER_PORT}/mcp"
LOG_DIR="${MATRIX_LOG_DIR:-$(mktemp -d)}"
mkdir -p "$LOG_DIR"

# Native Python cannot open an MSYS path: it reads `/tmp/tmp.XXXX` as
# `\tmp\tmp.XXXX`, a path that does not exist, and the run dies at the first
# log assertion with FileNotFoundError. Bash handles `/tmp` fine, so translate
# once, for the arguments handed to Python only. Verified by execution on
# git-bash; see docs/LOCAL-MATRIX.md.
if command -v cygpath >/dev/null 2>&1; then
  LOG_DIR_NATIVE="$(cygpath -w "$LOG_DIR")"
else
  LOG_DIR_NATIVE="$LOG_DIR"
fi

PIDS=()

stop_all() {
  local pid winpid
  for pid in "${PIDS[@]}"; do
    # On Windows `uv run` leaves a child behind unless the whole tree is killed.
    # Single slashes, with argument conversion off: `//F` is rejected by some shells.
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

# idp: the client list is the one in docker-compose.yml, with the server's own URL.
IDP_ISSUER="http://127.0.0.1:9000" \
IDP_CLIENTS="[{\"client_id\": \"flightops-agent\", \"client_secret\": \"${DEV_AGENT_CLIENT_SECRET}\", \"app_roles\": {\"${RESOURCE}\": [\"flightops.reference.read\", \"flightops.weather.read\", \"flightops.traffic.read\", \"flightops.status.read\"]}}, {\"client_id\": \"flightops-mcp\", \"client_secret\": \"${DEV_MCP_CLIENT_SECRET}\", \"resource\": \"${RESOURCE}\", \"exchange_targets\": {\"api://flightops-ops\": [\"flightops.passengers.read\"]}}]" \
  start "$LOG_DIR/idp.log" uv run uvicorn --factory services.idp.app:create_app --host 127.0.0.1 --port 9000

# A second issuer with its own key. Its tokens name this server as audience.
IDP_ISSUER="http://127.0.0.1:9001" \
  start "$LOG_DIR/other-idp.log" uv run uvicorn --factory services.idp.app:create_app --host 127.0.0.1 --port 9001

# ops-api: trusts the first IdP only.
OPS_ISSUER="http://127.0.0.1:9000" \
OPS_JWKS_URL="http://127.0.0.1:9000/.well-known/jwks.json" \
OPS_AUDIENCE="api://flightops-ops" \
  start "$LOG_DIR/ops-api.log" uv run uvicorn --factory services.ops_api.app:create_app --host 127.0.0.1 --port 9100

# server: every FLIGHTOPS_* value below is the compose value with the host name changed.
FLIGHTOPS_LOG_LEVEL="INFO" \
FLIGHTOPS_AUTH_ISSUER="http://127.0.0.1:9000" \
FLIGHTOPS_AUTH_JWKS_URL="http://127.0.0.1:9000/.well-known/jwks.json" \
FLIGHTOPS_RESOURCE_SERVER_URL="$RESOURCE" \
FLIGHTOPS_AUTH_TOKEN_URL="http://127.0.0.1:9000/token" \
FLIGHTOPS_AUTH_CLIENT_ID="flightops-mcp" \
FLIGHTOPS_AUTH_CLIENT_SECRET="$DEV_MCP_CLIENT_SECRET" \
FLIGHTOPS_OPS_API_BASE_URL="http://127.0.0.1:9100" \
FLIGHTOPS_OPS_API_AUDIENCE="api://flightops-ops" \
  start "$LOG_DIR/server.log" uv run flightops-mcp --transport streamable-http --host 127.0.0.1 --port "$SERVER_PORT"

MATRIX_SERVER="http://127.0.0.1:${SERVER_PORT}" \
MATRIX_IDP_LOG="$LOG_DIR_NATIVE/idp.log" \
MATRIX_SERVER_LOG="$LOG_DIR_NATIVE/server.log" \
  uv run python scripts/local_matrix.py
status=$?

echo "service logs: $LOG_DIR"
exit "$status"

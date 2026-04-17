#!/bin/sh
# Docker HEALTHCHECK shim for the ibg-controller /health endpoint.
#
# Curls /health on the configured port (and paper-offset port under
# DUAL_MODE=yes). Exit 0 if every probed endpoint returned 2xx, non-zero
# otherwise — Docker interprets non-zero as unhealthy. The controller's
# /health itself returns 503 (not 200) whenever the state machine isn't
# in MONITORING or the API port isn't open, so this script's job is
# just to aggregate probes, not re-check liveness conditions.
set -eu

PORT="${CONTROLLER_HEALTH_SERVER_PORT:-8080}"
HOST="127.0.0.1"

probe() {
    _p="$1"
    # -sS silences progress but keeps errors on stderr, -f fails on any
    # non-2xx response (503 → exit nonzero, which is what we want).
    # Short --max-time so a wedged server doesn't stall the healthcheck
    # past Docker's own timeout.
    curl -sSf --max-time 3 -o /dev/null "http://${HOST}:${_p}/health"
}

probe "$PORT"

if [ "${DUAL_MODE:-}" = "yes" ]; then
    PAPER_PORT=$((PORT + 1))
    probe "$PAPER_PORT"
fi

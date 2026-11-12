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

# On the IBC path there is no controller and so no /health to ask: report
# healthy, as the base image (which has no HEALTHCHECK) effectively does.
# The value is read the way docker/run.sh's choose_login_automation reads
# it; tests/test_run_sh.py keeps the two in step.
uic=$(printf '%s' "${USE_IBG_CONTROLLER-}" | tr '[:upper:]' '[:lower:]' | tr -d ' \t"'"'")
case "$uic" in
no | false | 0 | off) exit 0 ;;
esac
if [ -z "$uic" ] && [ "${CUSTOM_CONFIG:-}" = "yes" ]; then
    exit 0
fi

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

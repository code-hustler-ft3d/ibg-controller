#!/bin/sh
# Docker HEALTHCHECK for the ibg-controller /health endpoint.
#
# Asks /health on CONTROLLER_HEALTH_SERVER_PORT and exits 0 for a 2xx, 1
# otherwise (Docker reads non-zero as unhealthy). /health itself answers
# 503 unless the controller is in MONITORING with the API port open and
# the JVM alive, so this only asks and reports.
#
# With two modes in one container (TRADING_MODE=both) the first port is
# live and the next one is paper. The exit status follows live. Paper is
# asked too and its state is printed, which lands in the health log
# (docker inspect), but a paper fault does not turn the container
# unhealthy: anything that restarts unhealthy containers would then log
# live out to fix paper. HEALTHCHECK_PAPER=yes makes paper count as well.
#
#   healthcheck.sh             ask, print one line per mode, set the status
#   healthcheck.sh --explain   print what the status covers; ask nothing
set -eu

PORT="${CONTROLLER_HEALTH_SERVER_PORT:-8080}"
HOST="127.0.0.1"

# Lines printed here can carry a setting's value as the operator wrote it;
# printf keeps sh's echo from reading backslashes in it.
say() { printf '%s\n' "$*"; }

DUAL=no
if [ "${TRADING_MODE:-}" = "both" ] || [ "${DUAL_MODE:-}" = "yes" ]; then
    DUAL=yes
fi

# Does paper count? HEALTHCHECK_PAPER decides. Without it, paper counts
# only where it always has: a container started with DUAL_MODE=yes in its
# own environment. TRADING_MODE=both does not put that variable there
# (run.sh sets it for its own processes), so there the status is live's.
PAPER_COUNTS=no
PAPER_NOTE=""
hp=$(printf '%s' "${HEALTHCHECK_PAPER-}" | tr '[:upper:]' '[:lower:]' | tr -d ' \t"'"'")
case "$hp" in
yes | true | 1 | on) PAPER_COUNTS=yes ;;
no | false | 0 | off) ;;
"") [ "${DUAL_MODE:-}" = "yes" ] && PAPER_COUNTS=yes ;;
*) PAPER_NOTE="HEALTHCHECK_PAPER='${HEALTHCHECK_PAPER}' is not yes or no, so paper is not counted" ;;
esac

if [ "${1:-}" = "--explain" ]; then
    [ "$DUAL" = yes ] || exit 0
    [ -z "$PAPER_NOTE" ] || say ".> WARNING: ${PAPER_NOTE}."
    if [ "$PAPER_COUNTS" = yes ]; then
        say ".> Docker health status: unhealthy when live or paper is not ready."
    else
        say ".> Docker health status: follows live. Paper's state is in the health log; HEALTHCHECK_PAPER=yes makes paper count too."
    fi
    exit 0
fi

# ask LABEL PORT SECONDS: print what /health answered, return 0 for a 2xx.
# curl's own error text (connection refused, timed out) goes to stderr,
# which Docker keeps in the health log next to the line printed here.
ask() {
    _out=$(curl -sS --max-time "$3" -w '\n%{http_code}' "http://${HOST}:$2/health") || true
    _code=$(printf '%s\n' "$_out" | tail -n 1)
    _state=$(printf '%s\n' "$_out" | sed -n 's/.*"state": *"\([A-Za-z_]*\)".*/\1/p' | head -n 1)
    case "$_code" in
    2[0-9][0-9])
        say "$1: healthy (${_state:-no state given})"
        return 0
        ;;
    [1-9][0-9][0-9])
        say "$1: not healthy (state ${_state:-unknown}, HTTP ${_code})"
        ;;
    *)
        say "$1: no answer from /health on port $2"
        ;;
    esac
    return 1
}

FIRST="${TRADING_MODE:-gateway}"
[ "$DUAL" = yes ] && FIRST=live

rc=0
# 3 s: short enough that a wedged server cannot run into Docker's own
# 5 s timeout, which would lose the output.
ask "$FIRST" "$PORT" 3 || rc=1

if [ "$DUAL" = yes ]; then
    PAPER_PORT=$((PORT + 1))
    if [ "$PAPER_COUNTS" = yes ] && [ "$rc" -eq 0 ]; then
        ask paper "$PAPER_PORT" 3 || rc=1
    elif [ "$PAPER_COUNTS" = yes ]; then
        # Live already failed: the status is settled, so only spend 1 s.
        ask paper "$PAPER_PORT" 1 || true
    elif ! ask paper "$PAPER_PORT" 1; then
        say "paper is not counted in the health status (${PAPER_NOTE:-HEALTHCHECK_PAPER=yes makes it count})"
    fi
fi

exit "$rc"

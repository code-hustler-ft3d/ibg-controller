#!/bin/bash
#
# From gnzsnz/ib-gateway-docker (MIT License, Copyright 2022 Emanuel
# Fernandes, 2023 gnzsnz), stable/scripts/run_socat.sh at commit 811ab817.
# Unchanged apart from this note. See NOTICE.
set -Eo pipefail

LOCAL_PORT="$API_PORT"
# shellcheck disable=SC2153
PUBLISHED_PORT="$SOCAT_PORT"
_RESTART="$SSH_RESTART"

while true; do
	printf "Forking :::%d onto 0.0.0.0:%d > trading mode %s \n" \
		"${LOCAL_PORT}" "${PUBLISHED_PORT}" "${TRADING_MODE}"
	socat TCP-LISTEN:"${PUBLISHED_PORT}",fork TCP:127.0.0.1:"${LOCAL_PORT}"
	sleep "${_RESTART:-5}"
done

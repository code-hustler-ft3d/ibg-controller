#!/bin/bash
# Install the IB Gateway controller from a release tarball into a
# running ibgateway container's filesystem.
#
# Usage (from the docker image's setup stage):
#   ARG IBG_CONTROLLER_VERSION=0.5.13
#   RUN curl -sSLO https://github.com/code-hustler-ft3d/ibg-controller/releases/download/v${IBG_CONTROLLER_VERSION}/ibg-controller-${IBG_CONTROLLER_VERSION}.tar.gz && \
#       tar -xzf ibg-controller-${IBG_CONTROLLER_VERSION}.tar.gz && \
#       cd ibg-controller-${IBG_CONTROLLER_VERSION} && \
#       DESTDIR=/root ./install.sh
#
# Installs:
#   $DESTDIR/gateway-input-agent.jar      ← loaded via -javaagent:
#   $DESTDIR/scripts/gateway_controller.py ← the Python controller
#
# The Docker image's run.sh should launch the controller via:
#   python3 $DESTDIR/scripts/gateway_controller.py

set -euo pipefail

DESTDIR="${DESTDIR:-/home/ibgateway}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "[install] installing ibg-controller into $DESTDIR"

install -d "$DESTDIR/scripts"
install -m 0644 "$SCRIPT_DIR/gateway-input-agent.jar" "$DESTDIR/gateway-input-agent.jar"
install -m 0755 "$SCRIPT_DIR/gateway_controller.py" "$DESTDIR/scripts/gateway_controller.py"

echo "[install] done"
echo "  agent: $DESTDIR/gateway-input-agent.jar"
echo "  controller: $DESTDIR/scripts/gateway_controller.py"

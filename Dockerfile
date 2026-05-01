# ibg-controller image recipe.
#
# Extends a gnzsnz/ib-gateway base with the ibg-controller artifacts
# (agent jar + Python controller) and swaps upstream's run.sh for the
# controller-aware variant shipped alongside.
#
# UPSTREAM_IMAGE defaults to the :stable moving tag for low-friction
# local builds. Production consumers should pin a digest via --build-arg
# so rebuilds are reproducible, e.g.:
#
#   docker build -t ibg-controller:local \
#     --build-arg UPSTREAM_IMAGE=ghcr.io/gnzsnz/ib-gateway:10.45.1c@sha256:... .
#
# Build prerequisites: run `make` in the repo root first to populate
# dist/ with the agent jar and the controller .py, then `docker build .`
# from the same directory.

ARG UPSTREAM_IMAGE=ghcr.io/gnzsnz/ib-gateway:stable
FROM ${UPSTREAM_IMAGE}

USER root

# Runtime packages. `gettext-base socat xvfb x11vnc sshpass openssh-client
# sudo telnet` are already in the upstream image; listed nowhere here
# because the upstream provides them. We add:
#   - python3 + python3-gi + gir1.2-atspi-2.0 + at-spi2-core: the Python
#     controller still does `from gi.repository import Atspi` at module
#     load. The AT-SPI bridge is disabled in the JVM (see
#     gateway_controller.py launch_gateway) and the bus daemons are no
#     longer started, but the typelib + libatspi.so.0 must be installable
#     for the import to succeed.
#   - matchbox-window-manager: Xvfb has no concept of focused window
#     without a WM, and Gateway's input routing depends on focus.
#   - curl: used by scripts/healthcheck.sh.
RUN apt-get update -y \
 && apt-get install --no-install-recommends --yes \
      python3 python3-gi gir1.2-atspi-2.0 at-spi2-core \
      matchbox-window-manager curl \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

# Install the controller artifacts from the local build. Run `make` before
# `docker build` so dist/ is populated.
COPY dist/gateway-input-agent.jar /home/ibgateway/gateway-input-agent.jar
COPY dist/gateway_controller.py  /home/ibgateway/scripts/gateway_controller.py

# Swap in the controller-aware run.sh. Replaces upstream's IBC-first
# dispatch with a path that starts the controller, waits for its
# readiness signal, then brings up socat port forwarding.
COPY docker/run.sh /home/ibgateway/scripts/run.sh

# Healthcheck shim — curls the controller's /health endpoint on the
# configured port (and on the paper-side offset port when DUAL_MODE=yes).
# Used by the HEALTHCHECK directive below.
COPY scripts/healthcheck.sh /home/ibgateway/scripts/healthcheck.sh

# Default port for the /health HTTP server the controller starts in
# main(). docker/run.sh offsets the paper instance to base+1 when
# DUAL_MODE=yes so both controllers can bind in the same container.
# Override with --env CONTROLLER_HEALTH_SERVER_PORT=0 to disable.
ENV CONTROLLER_HEALTH_SERVER_PORT=8080 \
    CONTROLLER_HEALTH_SERVER_HOST=0.0.0.0

RUN chown -R 1000:1000 /home/ibgateway \
 && chmod 0755 /home/ibgateway/scripts/run.sh \
 && chmod 0755 /home/ibgateway/scripts/gateway_controller.py \
 && chmod 0755 /home/ibgateway/scripts/healthcheck.sh \
 && chmod 0644 /home/ibgateway/gateway-input-agent.jar

# start-period gives the JVM + login pipeline time to finish before
# failures count. The controller's /health returns 503 (not 200) during
# login, so without the grace window a fresh container would be marked
# unhealthy for ~2min during normal boot.
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 \
    CMD /home/ibgateway/scripts/healthcheck.sh

USER 1000:1000
WORKDIR /home/ibgateway
CMD ["/home/ibgateway/scripts/run.sh"]

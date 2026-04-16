# ibg-controller image recipe.
#
# Extends a gnzsnz/ib-gateway base with the AT-SPI accessibility stack,
# the Java ATK bridge, and the ibg-controller artifacts (agent jar +
# Python controller). Swaps upstream's run.sh for the
# USE_PYATSPI2_CONTROLLER=yes-aware variant shipped alongside.
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

# AT-SPI stack + matchbox WM. Matches docs/MIGRATION.md §"Production stage
# additions". `gettext-base socat xvfb x11vnc sshpass openssh-client sudo
# telnet` are already in the upstream image; listed here only as a safety
# net in case a rebase loses them.
RUN apt-get update -y \
 && apt-get install --no-install-recommends --yes \
      python3 python3-gi gir1.2-atspi-2.0 at-spi2-core \
      libatk-wrapper-java libatk-wrapper-java-jni dbus-x11 \
      matchbox-window-manager \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

# Configure the Java accessibility bridge into Gateway's JRE. Handles both
# amd64 (install4j JRE at /usr/local/i4j_jres/...) and arm64 (Zulu JRE at
# /usr/local/zulu17.*). See docs/ARCHITECTURE.md for why the .so must live
# in the JRE lib dir.
RUN GW_JAVA=$(find /usr/local/i4j_jres -name java -type f 2>/dev/null | head -1); \
    if [ -z "$GW_JAVA" ]; then \
      GW_JAVA=$(find /usr/local -path "*/zulu*/bin/java" -type f 2>/dev/null | head -1); \
    fi; \
    if [ -z "$GW_JAVA" ]; then \
      echo "ERROR: no Gateway JRE found under /usr/local"; exit 1; \
    fi; \
    JAVA_HOME=$(dirname $(dirname "$GW_JAVA")); \
    echo "Configuring ATK bridge for JRE at $JAVA_HOME"; \
    echo "assistive_technologies=org.GNOME.Accessibility.AtkWrapper" \
      > "$JAVA_HOME/conf/accessibility.properties"; \
    JNI_SO=$(find /usr -name "libatk-wrapper.so*" -type f 2>/dev/null | head -1); \
    if [ -z "$JNI_SO" ]; then echo "ERROR: libatk-wrapper.so not found"; exit 1; fi; \
    cp "$JNI_SO" "$JAVA_HOME/lib/"

# Install the controller artifacts from the local build. Run `make` before
# `docker build` so dist/ is populated.
COPY dist/gateway-input-agent.jar /home/ibgateway/gateway-input-agent.jar
COPY dist/gateway_controller.py  /home/ibgateway/scripts/gateway_controller.py

# Swap in the USE_PYATSPI2_CONTROLLER-aware run.sh. Replaces upstream's
# IBC-first dispatch with a path that starts the controller, waits for
# its readiness signal, then brings up socat port forwarding.
COPY docker/run.sh /home/ibgateway/scripts/run.sh

RUN chown -R 1000:1000 /home/ibgateway \
 && chmod 0755 /home/ibgateway/scripts/run.sh \
 && chmod 0755 /home/ibgateway/scripts/gateway_controller.py \
 && chmod 0644 /home/ibgateway/gateway-input-agent.jar

USER 1000:1000
WORKDIR /home/ibgateway
CMD ["/home/ibgateway/scripts/run.sh"]

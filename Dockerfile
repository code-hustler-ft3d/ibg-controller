# ibg-controller image recipe.
#
# Builds the whole image from Ubuntu: IB Gateway from IBKR's installer, the
# start-up scripts, IBC (for USE_IBG_CONTROLLER=no) and the controller
# (agent jar + Python controller).
#
# The image and every file the build downloads are pinned here, and these
# ARG defaults are the single source of truth for the release:
# release-image.yml passes no build args, so a bare `docker build .` builds
# what a release builds. Ubuntu's packages are the exception: they install
# at their current versions from Ubuntu's archive.
#
#   - Ubuntu, by image digest.
#   - IBKR's Gateway installer, by SHA-256, one per architecture. The build
#     fails on a file that doesn't match.
#   - IBC's release archive and the config.ini template its mode renders,
#     by SHA-256.
#
# IBKR's download URL serves only the current build of each channel, so
# the installers are kept, byte for byte as IBKR served them, under this
# repository's `build-inputs` release, and the build reads them from there.
# A daily workflow archives a new stable build and proposes its pin
# (.github/workflows/gateway-bump.yml).
#
# Another Gateway version, for example a newer one from IBKR's `latest`
# channel: pass its version and both checksums, and IB_GATEWAY_INSTALLER_URL
# if the files are somewhere else. The URL is a directory holding
# ibgateway-<version>-standalone-linux-x64.sh and ...-linux-arm.sh:
#
#   docker build -t ibg-controller:edge \
#     --build-arg IB_GATEWAY_VERSION=10.52.1d \
#     --build-arg IB_GATEWAY_SHA256_X64=<sha256 of the x64 installer> \
#     --build-arg IB_GATEWAY_SHA256_ARM=<sha256 of the arm installer> .
#
# CI builds one `latest`-channel version too, but only as a build-and-boot
# check: the login, 2FA and dialog paths are verified against the pinned
# stable line (README, Requirements).
#
# Build prerequisites: run `make` in the repo root first to populate
# dist/ with the agent jar and the controller .py, then `docker build .`
# from the same directory.

ARG UBUNTU_IMAGE=ubuntu:26.04@sha256:d66b366dac61b575bee2aef07e9c26e8070096fbe84a3d4d3eb0da069b5d76db
ARG IB_GATEWAY_VERSION=10.50.1h
ARG IB_GATEWAY_SHA256_X64=eb578b23b636e19c8417fc969264853a22792bcffa8fc79990cf15ff032fe7aa
ARG IB_GATEWAY_SHA256_ARM=1352b9f119c194409084a7081e629aba63a3eabb177777ec372a0f2d3bac6537
ARG IB_GATEWAY_INSTALLER_URL=https://github.com/code-hustler-ft3d/ibg-controller/releases/download/build-inputs
ARG IBC_URL=https://github.com/code-hustler-ft3d/ibg-controller/releases/download/build-inputs/IBCLinux-3.24.2.zip
ARG IBC_SHA256=cc097ca1dfa75413a5fbe02e4743050af15f3308eb6a97c769b3cff9850c80c5
ARG IBC_CONFIG_TMPL_URL=https://github.com/code-hustler-ft3d/ibg-controller/releases/download/build-inputs/ibc-config.ini.tmpl
ARG IBC_CONFIG_TMPL_SHA256=a45969ef75ba1fb9a1efe74cdbeabef184e80c09ef976e7899ac1ca2966a83c9

##############################################################################
# Setup stage: download, verify and install. The archives stay in this
# stage and never reach the image.
##############################################################################
FROM ${UBUNTU_IMAGE} AS setup

ARG IB_GATEWAY_VERSION
ARG IB_GATEWAY_SHA256_X64
ARG IB_GATEWAY_SHA256_ARM
ARG IB_GATEWAY_INSTALLER_URL
ARG IBC_URL
ARG IBC_SHA256
ARG IBC_CONFIG_TMPL_URL
ARG IBC_CONFIG_TMPL_SHA256
ARG DEBIAN_FRONTEND=noninteractive

WORKDIR /tmp/setup

# Each download is checked against its pinned SHA-256 before it is used.
# The comparison is spelled out, not left to `sha256sum --check`, which
# skips a line it can't parse: an empty or malformed pin must fail the
# build, not go unchecked.
#
# The installer runs unattended (-q) into Jts/ibgateway/<version>. Where it
# ships its own JRE (10.50 and later), install4j's record of the JRE path is
# rewritten for the image's home directory; older installers put the JRE
# under /usr/local/i4j_jres, which the final stage copies.
RUN apt-get update -y \
 && apt-get install --no-install-recommends --yes curl ca-certificates unzip \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/* \
 && case "$(uname -m)" in \
      aarch64) arch=arm; sum="${IB_GATEWAY_SHA256_ARM}" ;; \
      x86_64)  arch=x64; sum="${IB_GATEWAY_SHA256_X64}" ;; \
      *) echo "unsupported architecture: $(uname -m)" >&2; exit 1 ;; \
    esac \
 && installer="ibgateway-${IB_GATEWAY_VERSION}-standalone-linux-${arch}.sh" \
 && curl -fsSL --retry 3 -o "${installer}" "${IB_GATEWAY_INSTALLER_URL}/${installer}" \
 && curl -fsSL --retry 3 -o ibc.zip "${IBC_URL}" \
 && curl -fsSL --retry 3 -o config.ini.tmpl "${IBC_CONFIG_TMPL_URL}" \
 && verify() { \
      have="$(sha256sum "$1" | cut -d' ' -f1)"; \
      if [ "${#have}" -ne 64 ] || [ "${have}" != "$2" ]; then \
        echo "SHA-256 of $1 is '${have}', not the pinned '$2'" >&2; \
        exit 1; \
      fi; \
    } \
 && verify "${installer}" "${sum}" \
 && verify ibc.zip "${IBC_SHA256}" \
 && verify config.ini.tmpl "${IBC_CONFIG_TMPL_SHA256}" \
 && chmod a+x "${installer}" \
 && "./${installer}" -q -dir "/root/Jts/ibgateway/${IB_GATEWAY_VERSION}" \
 && if [ -d "/root/Jts/ibgateway/${IB_GATEWAY_VERSION}/jre" ]; then \
      echo "/home/ibgateway/Jts/ibgateway/${IB_GATEWAY_VERSION}/jre" \
        > "/root/Jts/ibgateway/${IB_GATEWAY_VERSION}/.install4j/inst_jre.cfg"; \
    fi \
 && mkdir /root/ibc \
 && unzip -q ibc.zip -d /root/ibc \
 && chmod -R u+x /root/ibc/*.sh /root/ibc/scripts/*.sh \
 && cp config.ini.tmpl /root/ibc/config.ini.tmpl

##############################################################################
# The image.
##############################################################################
FROM ${UBUNTU_IMAGE}

# Re-declared so the global defaults are in scope in this stage.
ARG UBUNTU_IMAGE
ARG IB_GATEWAY_VERSION
ARG DEBIAN_FRONTEND=noninteractive

# Self-describing image: the bundled IB Gateway version and the exact Ubuntu
# base, so `docker inspect` (and the GHCR page) report them without starting
# the container.
LABEL com.ibg-controller.ib-gateway-version="${IB_GATEWAY_VERSION}" \
      org.opencontainers.image.base.name="${UBUNTU_IMAGE}"

# The names the start-up scripts, IBC and the controller read.
ENV IB_GATEWAY_VERSION=${IB_GATEWAY_VERSION} \
    HOME=/home/ibgateway
ENV TWS_MAJOR_VRSN=${IB_GATEWAY_VERSION} \
    TWS_PATH=${HOME}/Jts \
    TWS_INI=jts.ini \
    TWS_INI_TMPL=jts.ini.tmpl \
    IBC_PATH=${HOME}/ibc \
    IBC_INI=${HOME}/ibc/config.ini \
    IBC_INI_TMPL=${HOME}/ibc/config.ini.tmpl \
    SCRIPT_PATH=${HOME}/scripts \
    GATEWAY_OR_TWS=gateway

COPY --from=setup /usr/local/ /usr/local/
COPY --chown=1000:1000 --from=setup /root/ ${HOME}

# Runtime packages and the ibgateway user (1000:1000).
#   - gettext-base (envsubst), socat, sshpass, openssh-client: settings
#     templates, port forwarding and the optional SSH tunnel.
#   - xvfb, x11vnc: the display Gateway draws on, and optional VNC access.
#   - the lib* list, fonts-liberation, xdg-utils: Gateway's GUI and its
#     embedded browser (the passkey prompt).
#   - python3: runs gateway_controller.py.
#   - matchbox-window-manager: Xvfb has no concept of focused window
#     without a WM, and Gateway's input routing depends on focus.
#   - curl: used by scripts/healthcheck.sh.
#   - sudo (passwordless for ibgateway) and telnet: for start-up hook
#     scripts and debugging.
RUN apt-get update -y \
 && apt-get upgrade -y \
 && apt-get install --no-install-recommends --yes \
      gettext-base socat xvfb x11vnc sshpass openssh-client sudo telnet ca-certificates \
      libatk-bridge2.0-0 libatk1.0-0 libatspi2.0-0 libc6 libcairo2 libcups2 libdbus-1-3 \
      libdrm2 libexpat1 libgbm1 libglib2.0-0 libgtk-3-0 libnspr4 libnss3 libpango-1.0-0 \
      libx11-6 libxcb1 libxcomposite1 libxdamage1 libxext6 libxfixes3 libxkbcommon0 \
      libxrandr2 fonts-liberation libasound2t64 libxtst6 xdg-utils \
      python3 matchbox-window-manager curl \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/* \
 && if id ubuntu >/dev/null 2>&1; then userdel -rf ubuntu; fi \
 && groupadd --gid 1000 ibgateway \
 && useradd -ms /bin/bash --uid 1000 --gid 1000 ibgateway \
 && echo "ibgateway ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers \
 && mkdir -p /tmp/.X11-unix \
 && chmod 1777 /tmp/.X11-unix

# Start-up scripts. run.sh is the entry point; common.sh, run_socat.sh and
# run_ssh.sh are its helpers (settings, port forwarding, SSH tunnel), and
# jts.ini.tmpl seeds Gateway's settings file on a fresh volume.
COPY docker/run.sh docker/common.sh docker/run_socat.sh docker/run_ssh.sh ${HOME}/scripts/
COPY docker/jts.ini.tmpl ${HOME}/Jts/jts.ini.tmpl

# The controller artifacts from the local build. Run `make` before
# `docker build` so dist/ is populated.
COPY dist/gateway-input-agent.jar ${HOME}/gateway-input-agent.jar
COPY dist/gateway_controller.py  ${HOME}/scripts/gateway_controller.py

# Healthcheck: asks the controller's /health on the configured port. With
# TRADING_MODE=both it asks paper's port too and prints its state, but the
# status follows live unless HEALTHCHECK_PAPER=yes. Used by the HEALTHCHECK
# directive below.
COPY scripts/healthcheck.sh ${HOME}/scripts/healthcheck.sh

# Default port for the /health HTTP server the controller starts in
# main(). docker/run.sh offsets the paper instance to base+1 when
# DUAL_MODE=yes so both controllers can bind in the same container.
# Override with --env CONTROLLER_HEALTH_SERVER_PORT= (empty) to disable;
# 0 would bind a random port.
ENV CONTROLLER_HEALTH_SERVER_PORT=8080 \
    CONTROLLER_HEALTH_SERVER_HOST=0.0.0.0

RUN chown -R 1000:1000 ${HOME} \
 && chmod 0755 ${HOME}/scripts/*.sh ${HOME}/scripts/gateway_controller.py \
 && chmod 0644 ${HOME}/gateway-input-agent.jar ${HOME}/Jts/jts.ini.tmpl

# start-period gives the JVM + login pipeline time to finish before
# failures count. The controller's /health returns 503 (not 200) during
# login, so without the grace window a fresh container would be marked
# unhealthy for ~2min during normal boot.
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 \
    CMD /home/ibgateway/scripts/healthcheck.sh

USER 1000:1000
WORKDIR /home/ibgateway
CMD ["/home/ibgateway/scripts/run.sh"]

# Migration: IBC → ibg-controller

This document covers swapping IBC for `ibg-controller` inside a
[gnzsnz/ib-gateway-docker](https://github.com/gnzsnz/ib-gateway-docker)-style
image. If you're running vanilla IBC today, the swap requires:

- Adding a few runtime packages to your Docker image
- Compiling and installing the controller in your Dockerfile (or
  downloading a release)
- Setting one new env var (`TWS_SERVER`) for the bootstrap
- Flipping one flag (`USE_PYATSPI2_CONTROLLER=yes`) at runtime

Existing env vars (`TWS_USERID`, `TWS_PASSWORD`, `TRADING_MODE`,
`TWOFACTOR_CODE`, `EXISTING_SESSION_DETECTED_ACTION`, etc.) work
unchanged. The controller reads env vars directly — it doesn't parse
`config.ini`. If you already have an IBC `config.ini` that isn't
already mirrored into env vars, [`FROM_IBC.md`](FROM_IBC.md) documents
the IBC-key → env-var mapping and ships a one-shot
`ibc_config_to_env.py` that does the conversion for you.

## What changes in your Dockerfile

### Setup stage additions

```dockerfile
# Add make + default-jdk-headless to the existing curl/ca-certificates/unzip line:
RUN apt-get update -y && \
  apt-get install --no-install-recommends --yes \
  curl ca-certificates unzip default-jdk-headless make && \
  ...

# After the existing IB Gateway install + scripts copy, add:
COPY ./controller /root/controller
RUN cd /root/controller && \
  make install DESTDIR=/root && \
  cd / && rm -rf /root/controller /root/build
```

This compiles the agent jar and drops:
- `/root/gateway-input-agent.jar`
- `/root/scripts/gateway_controller.py`

Into the setup-stage filesystem. The existing `COPY --chown ... --from=setup /root/` copies them into `/home/ibgateway/` in the production stage, so no changes needed there.

### Production stage additions

```dockerfile
RUN apt-get update -y && \
  apt-get upgrade -y && \
  apt-get install --no-install-recommends --yes \
  gettext-base socat xvfb x11vnc sshpass openssh-client sudo telnet \
  python3 python3-gi gir1.2-atspi-2.0 at-spi2-core \
  libatk-wrapper-java libatk-wrapper-java-jni dbus-x11 \
  matchbox-window-manager && \
  ...
```

The new runtime packages:
- `python3` + `python3-gi` + `gir1.2-atspi-2.0` — Python AT-SPI bindings
- `at-spi2-core` — provides `at-spi-bus-launcher` and `at-spi2-registryd`
- `libatk-wrapper-java` + `libatk-wrapper-java-jni` — bridges Swing to AT-SPI
- `dbus-x11` — the `dbus-launch` utility
- `matchbox-window-manager` — a minimal window manager (Xvfb has no WM
  by default, and synthetic input handling needs a focus owner)

### JRE configuration at build time

```dockerfile
# Configure the Java accessibility bridge into Gateway's JRE.
# Handles both amd64 (install4j-bundled JRE at /usr/local/i4j_jres/...)
# and arm64 (system Zulu JRE at /usr/local/zulu17.*).
RUN GW_JAVA=$(find /usr/local/i4j_jres -name java -type f 2>/dev/null | head -1); \
  if [ -z "$GW_JAVA" ]; then \
    GW_JAVA=$(find /usr/local -path "*/zulu*/bin/java" -type f 2>/dev/null | head -1); \
  fi; \
  if [ -z "$GW_JAVA" ]; then \
    echo "ERROR: no Gateway JRE found"; exit 1; \
  fi; \
  JAVA_HOME=$(dirname $(dirname "$GW_JAVA")); \
  echo "assistive_technologies=org.GNOME.Accessibility.AtkWrapper" \
    > "$JAVA_HOME/conf/accessibility.properties"; \
  JNI_SO=$(find /usr -name "libatk-wrapper.so*" -type f 2>/dev/null | head -1); \
  cp "$JNI_SO" "$JAVA_HOME/lib/"
```

Writes `accessibility.properties` into the JRE's `conf/` directory and
copies `libatk-wrapper.so` into the JRE's `lib/` directory. Both are
required for Gateway's JVM to load the AT-SPI bridge at startup. See
[ARCHITECTURE.md](ARCHITECTURE.md) for why the .so has to live in the
JRE lib (and not just on `LD_LIBRARY_PATH`).

## What changes in `run.sh`

The gnzsnz `run.sh` gains a branch: when `USE_PYATSPI2_CONTROLLER=yes`,
start the AT-SPI infrastructure and launch the controller instead of IBC.
When unset, fall through to the IBC path unchanged. Full diff:

```bash
# New helper functions
start_dbus_session() { ... }    # dbus-launch + export
start_atspi() { ... }            # at-spi-bus-launcher + at-spi2-registryd
start_window_manager() { ... }   # matchbox-window-manager
start_controller() { ... }       # python3 gateway_controller.py
wait_for_controller_ready() { ... } # block on /tmp/gateway_ready

# In the Common Start section, after start_xvfb:
if [ "$USE_PYATSPI2_CONTROLLER" = "yes" ]; then
    wait_x_socket
    start_window_manager
    start_dbus_session
    start_atspi
fi

# In start_process(), replace the unconditional start_IBC with:
if [ "$USE_PYATSPI2_CONTROLLER" = "yes" ]; then
    start_controller
    wait_for_controller_ready
    port_forwarding                 # socat starts AFTER readiness
else
    port_forwarding
    start_IBC
fi
```

The controller path gates `port_forwarding` on `/tmp/gateway_ready`,
which fixes a long-standing issue in the IBC path where socat races the
Gateway login and the first few API client connection attempts hit a
closed port.

## What changes at runtime

### New env vars

- **`USE_PYATSPI2_CONTROLLER=yes`** — opts into the new path. Default
  (unset) uses IBC as before.
- **`TWS_SERVER`** / **`TWS_SERVER_PAPER`** — your IBKR regional server.
  See [BOOTSTRAP.md](BOOTSTRAP.md) for how to find it.

### Existing env vars — behavior unchanged

These keep working the same way IBC used them:
- `TWS_USERID`, `TWS_PASSWORD`
- `TWS_USERID_PAPER`, `TWS_PASSWORD_PAPER`
- `TRADING_MODE` — `live`, `paper`, or **`both`** (v0.2 adds full
  dual-mode support with per-instance state isolation)
- `TWOFACTOR_CODE` (the base32 TOTP secret)
- `EXISTING_SESSION_DETECTED_ACTION` — clicks `Continue Login` for
  primary on Gateway 10.45.1c
- `VNC_SERVER_PASSWORD`
- `TIME_ZONE`
- `SSH_TUNNEL`, `SSH_OPTIONS`, ...

### Honored by v0.2 — post-login API config

The controller drives Gateway's Configure → Settings → API dialog
after login completes. These IBC env vars now take effect:

- **`TWS_MASTER_CLIENT_ID`** — integer, sets the Master API client ID
- **`READ_ONLY_API`** — `yes`/`no`, toggles the Read-Only API checkbox
- **`AUTO_LOGOFF_TIME`** — `HH:MM`, sets Lock and Exit → Set Auto
  Log Off Time
- **`AUTO_RESTART_TIME`** — `HH:MM AM/PM`, sets Lock and Exit → Set
  Auto Restart Time

Gateway shows *either* Auto Log Off Time *or* Auto Restart Time in
the Lock and Exit panel depending on whether the account has an
autorestart daily-token cycle active. Setting both env vars is safe —
the handler tries both labels and sets whichever is visible, and
logs a clear warning for the one that's not.

### Honored by v0.2 — command server

IBC's telnet command server can be re-enabled with:

- **`CONTROLLER_COMMAND_SERVER_PORT`** — TCP port (e.g. `7462` for
  IBC compat). Unset = disabled. In dual mode the paper instance
  auto-offsets to `port+1` to avoid a bind collision.
- **`CONTROLLER_COMMAND_SERVER_HOST`** — bind address, default
  `0.0.0.0` so Docker port forwarding works. Restrict exposure via
  `docker run -p 127.0.0.1:7462:7462`.

Supported commands: `STOP`, `RESTART` (in-place Gateway JVM
re-launch with full re-login), `RECONNECTACCOUNT`, `ENABLEAPI`.
`RECONNECTDATA` returns a clean error on Gateway (no File →
Reconnect Data menu item) and dispatches against TWS.

### Honored by v0.2 — product selector

- **`GATEWAY_OR_TWS`** — `gateway` (default) or `tws`. Switches
  launcher discovery and AT-SPI app-name search. TWS live validation
  is pending a TWS-with-controller Dockerfile variant.

### Env vars NOT honored (still not implemented)

Gateway's config dialog doesn't expose these, so the controller
warns and ignores them:

- `ALLOW_BLIND_TRADING` — TWS Precautions tab; Gateway's simplified
  config has no equivalent
- `SAVE_TWS_SETTINGS` — not a Gateway knob

If you need these, keep IBC for now.

- `CUSTOM_CONFIG` — no IBC config file is written; the controller
  reads env vars directly.
- `TWOFA_DEVICE`, `TWOFA_TIMEOUT_ACTION` — only TOTP is handled via
  `TWOFACTOR_CODE`. IB Key push device support is impossible from a
  headless container.

## Shutdown grace period

**Required for v0.5.11 and later.** If you run ibg-controller under
`docker compose`, set `stop_grace_period: 90s` on the `ib-gateway`
service. The default of 10s is too short — docker will SIGKILL the
container before the clean-logout chain finishes, and IBKR will hold
the brokerage session slot open for hours after every restart.

```yaml
services:
  ib-gateway:
    image: ghcr.io/code-hustler-ft3d/ibg-controller:latest
    stop_grace_period: 90s
    environment:
      TRADING_MODE: both
      USE_PYATSPI2_CONTROLLER: "yes"
      # ... rest of your config
```

### Why 90 seconds

When docker sends SIGTERM, `run.sh`'s `stop_ibc()` walks this
sequence (v0.5.11+):

| Step | Budget | What happens |
|---|---|---|
| 1. SIGTERM controllers | up to 60s | Each controller calls `_attempt_clean_logout()`, which uses the in-JVM input agent to dispatch a `WINDOW_CLOSING` AWT event to Gateway's main window. Gateway's `WindowListener` runs a real CCP session-close handshake and the JVM exits. Per-controller timeout `_CLEAN_LOGOUT_TIMEOUT_SECONDS = 15s` per instance — in dual mode (`TRADING_MODE=both`) two instances run sequentially, so 30s of clean-logout is normal. The 60s outer budget covers JVM shutdown-hook work that runs after the WindowListener returns. |
| 2. Tear down x11vnc / Xvfb / AT-SPI / socat | a few seconds | Only after controllers have exited (or 60s elapsed). |
| 3. IBKR FIN-ACK margin | a few seconds | Server-side acknowledgement of the session-close TCP teardown. |

**Total budget: ~90s.** Setting `stop_grace_period: 90s` matches that
budget. Setting it lower means docker SIGKILLs the container partway
through step 1, the WindowListener never gets to run, and IBKR
treats the session as a network drop rather than a clean logout —
holding the slot open until the server-side timeout (>15 min).

If you're running single-mode (`TRADING_MODE=live` *or*
`TRADING_MODE=paper`, not both), 60s would technically suffice, but
we recommend keeping 90s for symmetry with dual-mode and for the
FIN-ACK margin.

### How to verify clean shutdown is working

After running `docker compose stop` (or sending SIGTERM directly),
your container logs should include:

```
ALERT_CLEAN_LOGOUT mode=<live|paper> pid=<n> status=succeeded
  reason="JVM exited cleanly within 15s of WINDOW_CLOSING"
ALERT_SHUTDOWN     mode=<live|paper> signal=SIGTERM graceful=true
```

If you see `status=failed_timeout`, `status=failed_unreachable`, or
no `ALERT_CLEAN_LOGOUT` line at all, the slot probably leaked. Check
that `stop_grace_period: 90s` is set and that you're running
v0.5.11+ (the underlying clean-logout pipeline didn't actually work
end-to-end before that release — see CHANGELOG.md).

## Testing the migration

1. Build your Docker image with the new Dockerfile.
2. Keep your existing `docker-compose.yml` — add
   `USE_PYATSPI2_CONTROLLER: yes` and `TWS_SERVER: <your-server>` to the
   environment section.
3. Leave all existing env vars as-is.
4. `docker compose up -d` and watch logs.
5. Expected log sequence (single paper mode, no post-login config):
   ```
   .> Starting Gateway controller in paper mode.
   .>		agent-socket: /tmp/gateway-input-paper.sock
   .>		ready-file:   /tmp/gateway_ready_paper
   .>		jts-config:   /home/ibgateway/Jts
   [INFO] Loading input agent
   [INFO] Input agent is up
   [INFO] Gateway JVM PID (from agent): 37
   [INFO] App registered: 'IBKR Gateway' (pid=37)
   [INFO] Login dialog detected
   [INFO] set_text on text 'Username': ok via agent (verified)
   [INFO] click on push button:'Paper Log In': ok
   [INFO] 2FA dialog detected: (...Second Factor Authentication...)  (if TOTP)
   [INFO] Typing TOTP code into the 2FA dialog
   [INFO] API port 4002 accepting connections after 0s
   [INFO] Post-login config: no supported env vars set, skipping
   [INFO] Readiness signal: /tmp/gateway_ready_paper
   [INFO] Command server: CONTROLLER_COMMAND_SERVER_PORT not set, skipping
   [INFO] Login complete. Entering monitor loop.
   ```

   Dual-mode (`TRADING_MODE=both`) runs the same sequence twice in
   sequence, first for live then for paper (separated by a 15-second
   sleep). Each instance uses its own `Jts_live` / `Jts_paper`
   settings directory, agent socket, and ready file.

   With post-login config set (e.g. `TWS_MASTER_CLIENT_ID=5`,
   `READ_ONLY_API=yes`), you'll also see:
   ```
   [INFO] Applying post-login configuration from env vars
   [INFO]   Navigating to API → Settings
   [INFO]   Setting Master API client ID = 5
   [INFO]   Setting Read-Only API = True
   [INFO] Post-login config applied and dialog closed
   ```

   With `CONTROLLER_COMMAND_SERVER_PORT=7462` set, you'll see:
   ```
   [INFO] Command server: listening on 0.0.0.0:7462
   ```

6. Verify your API client can connect through the forwarded port
   (4001 for live, 4002 for paper, or whatever you expose).

## Rolling back

If the controller fails for you, flip `USE_PYATSPI2_CONTROLLER` back to
unset (or `no`). The image still contains IBC and all its dependencies.
`run.sh`'s default branch uses IBC unchanged.

You can also run both side-by-side (two containers, one IBC, one
controller) on different account credentials to compare behavior before
committing.

## Reporting issues

Please include:
- The full controller log (`docker logs <container>`)
- Gateway's `launcher.log` from inside the container
  (`docker exec <container> cat /home/ibgateway/Jts/launcher.log`)
- Your `TWS_SERVER` value and whether you're running live or paper
- Gateway version (`TWS_MAJOR_VRSN`)
- Container architecture (amd64 / arm64)

**Never include your credentials, TOTP secret, or account numbers.**
The controller logs redact these, but Gateway's launcher.log may include
fragments. Sanitize before sharing.

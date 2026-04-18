# ibg-controller

[![CI](https://github.com/code-hustler-ft3d/ibg-controller/actions/workflows/ci.yml/badge.svg)](https://github.com/code-hustler-ft3d/ibg-controller/actions/workflows/ci.yml)
[![Release image](https://github.com/code-hustler-ft3d/ibg-controller/actions/workflows/release-image.yml/badge.svg)](https://github.com/code-hustler-ft3d/ibg-controller/actions/workflows/release-image.yml)
[![Latest release](https://img.shields.io/github/v/release/code-hustler-ft3d/ibg-controller?sort=semver)](https://github.com/code-hustler-ft3d/ibg-controller/releases)
[![License: MIT](https://img.shields.io/github/license/code-hustler-ft3d/ibg-controller)](LICENSE)
[![cosign signed](https://img.shields.io/badge/cosign-signed-0a84ff?logo=sigstore&logoColor=white)](SECURITY.md)

A Python + in-JVM Java agent drop-in replacement for
[IBC](https://github.com/IbcAlpha/IBC) on IB Gateway, targeted at the
headless Docker use case. Launches Gateway, drives the login dialog
(including TOTP 2FA), applies post-login API config, monitors for
re-authentication events, and exposes IBC's TCP command server.

> IBC is being deprecated in September 2026. This is one of the
> community paths forward — written in Python so the maintainer
> community of `gnzsnz/ib-gateway-docker` can read, patch, and extend
> it without a JVM or Rust toolchain.

## Quick start

The fastest way to use `ibg-controller` is the pre-built image on
GitHub Container Registry:

```bash
docker pull ghcr.io/code-hustler-ft3d/ibg-controller:latest

docker run -d --name ibkr \
  --env-file /path/to/your/.env \
  -e TRADING_MODE=paper \
  -e TWS_SERVER_PAPER=cdc1.ibllc.com \
  -p 127.0.0.1:4002:4004 \
  ghcr.io/code-hustler-ft3d/ibg-controller:latest
```

Tags published: `:latest`, `:<major>.<minor>` (e.g. `:0.5`), and
`:v<major>.<minor>.<patch>` (e.g. `:v0.5.4`). Every tag is signed with
cosign via Sigstore keyless signing — see [`SECURITY.md`](SECURITY.md)
for the verification recipe. For reproducible deployments, pin to a
digest (`ghcr.io/code-hustler-ft3d/ibg-controller@sha256:...`) — the
digest is printed in each release's CI log.

If you'd rather build the image yourself (or compose ibg-controller
into a larger image of your own):

```dockerfile
# In the Dockerfile's setup stage:
COPY ./ibg-controller /root/ibg-controller
RUN cd /root/ibg-controller && \
    make install DESTDIR=/root && \
    cd / && rm -rf /root/ibg-controller /root/build

# In the production stage, add these packages:
#   python3 python3-gi gir1.2-atspi-2.0 at-spi2-core
#   libatk-wrapper-java libatk-wrapper-java-jni
#   dbus-x11 matchbox-window-manager
# and follow docs/MIGRATION.md for the JRE accessibility bridge
# configuration.
```

Then at `docker run` time:

```bash
docker run -d --name ibkr \
  --env-file /path/to/your/.env \
  -e TRADING_MODE=paper \
  -e USE_PYATSPI2_CONTROLLER=yes \
  -e TWS_SERVER_PAPER=cdc1.ibllc.com \
  -e TWOFACTOR_CODE=<your TOTP secret> \
  -p 127.0.0.1:4002:4004 \
  your-ib-gateway-image
```

Full Dockerfile migration instructions: [`docs/MIGRATION.md`](docs/MIGRATION.md).
Already running IBC? Converting your `config.ini` to ibg-controller
env vars: [`docs/FROM_IBC.md`](docs/FROM_IBC.md).
Finding your regional server (`TWS_SERVER`): [`docs/BOOTSTRAP.md`](docs/BOOTSTRAP.md).
Why each piece is necessary: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Using the shipped Dockerfile

A ready-to-use `Dockerfile` is shipped at the repo root. It extends
[`gnzsnz/ib-gateway-docker`](https://github.com/gnzsnz/ib-gateway-docker),
installs the AT-SPI stack, configures the Java accessibility bridge
into Gateway's JRE, and drops the controller artifacts from `dist/`
into `/home/ibgateway/`:

```bash
# Build the agent jar and stage the controller
make

# Build the image. UPSTREAM_IMAGE defaults to ghcr.io/gnzsnz/ib-gateway:stable
# (a moving tag). For reproducible production builds, pin a digest:
docker build -t ibg-controller:local \
  --build-arg UPSTREAM_IMAGE=ghcr.io/gnzsnz/ib-gateway:10.45.1c@sha256:... \
  .
```

Use this when you want a single ready-to-run image with no further
customization. If you're composing ibg-controller into a larger image
of your own, use the `make install DESTDIR=...` path shown in the
Quick start above instead.

## System requirements

| Requirement | Notes |
|---|---|
| **Linux** | `amd64` and `arm64`. Ubuntu 24.04 base tested. |
| **IB Gateway** | Tested on **10.45.1c**. Should work on any 10.x with a compatible install4j launcher and a Zulu JRE 17+. |
| **Python 3.10+** | For f-strings with `=` and type hints. |
| **JDK 17+** | Only at *build* time, for the agent. Runtime uses Gateway's bundled Zulu JRE. |
| **Ubuntu packages** | `python3 python3-gi gir1.2-atspi-2.0 at-spi2-core libatk-wrapper-java libatk-wrapper-java-jni dbus-x11 matchbox-window-manager` |
| **JRE config** | `$JAVA_HOME/conf/accessibility.properties` with `assistive_technologies=org.GNOME.Accessibility.AtkWrapper`, and `libatk-wrapper.so` copied into `$JAVA_HOME/lib/` |

## Compatibility table

| Feature | Gateway | TWS | Notes |
|---|---|---|---|
| Single-mode paper cold-start | ✅ verified | ⚠️ code in place | TWS code-path unit-tested; app-name match needs real TWS to validate |
| Single-mode live cold-start | ✅ verified | ⚠️ code in place | |
| Dual mode (`TRADING_MODE=both`) | ✅ verified | ⚠️ code in place | Per-instance state isolation, agent sockets, ready files, JVM-PID-scoped find_app |
| TOTP 2FA | ✅ verified | ⚠️ code in place | |
| IB Key push 2FA | ✅ wait mode | ✅ wait mode | Controller detects the 2FA dialog, logs "approve on your phone", polls for dialog dismissal. User approves via IB Key mobile app. Same approach as ibctl. |
| Existing-session dialog | ✅ verified | ⚠️ code in place | Clicks `Continue Login`; late-arrival handler catches the dialog if it shows during the 2FA wait |
| `TWS_MASTER_CLIENT_ID` | ✅ verified | ⚠️ untested | Set + read back |
| `READ_ONLY_API` | ✅ verified | ⚠️ untested | Set + read back via JCHECK |
| `AUTO_LOGOFF_TIME` / `AUTO_RESTART_TIME` | ✅ verified | ⚠️ untested | Gateway shows one or the other based on account state; controller tries both labels |
| `STOP` command | ✅ verified | ⚠️ untested | |
| `RESTART` command (in-place) | ✅ code verified | ⚠️ untested | Full tear-down / re-launch / re-drive login pipeline — verified against live Gateway |
| `RECONNECTACCOUNT` | ✅ verified | ⚠️ untested | |
| `ENABLEAPI` | ✅ verified | ⚠️ untested | |
| `RECONNECTDATA` | ❌ no Gateway menu item | ⚠️ dispatch in place | TWS-only |

"✅ verified" = run end-to-end against a real IB account with logged
evidence in the parent repo's `spike/` directory. "⚠️ code in place"
= written, unit-tested where possible, not yet run against the
corresponding product.

## What it replaces

- **IBC** — Java, ~8000 lines. Replaced entirely for Gateway.
  TWS support is an isolated code switch (`GATEWAY_OR_TWS=tws`) that's
  unit-tested for the launcher-discovery path but not yet live-tested
  against real TWS.
- **oathtool** — stdlib-only TOTP generation.
- **xdotool** — not needed for input; the in-JVM agent handles text entry.

## Architecture at a glance

```
                   ┌────────────────────────────────────────┐
                   │  Docker container (headless)           │
                   │                                        │
                   │  Xvfb :1  ← matchbox WM               │
                   │     │                                  │
                   │     ↓                                  │
                   │  IB Gateway JVM                        │
                   │  ├─ -javaagent:gateway-input-agent.jar │
                   │  │      │                              │
                   │  │      ↓                              │
                   │  │   Unix socket (/tmp/gateway-input-{mode}.sock)
                   │  │      ↑                              │
                   │  └─ AT-SPI2 (org.GNOME.Accessibility.AtkWrapper)
                   │         │                              │
                   │         ↓                              │
                   │  gateway_controller.py (Python)        │
                   │    ├─ pyatspi2: find components, click │
                   │    ├─ agent socket: type text, set     │
                   │    │    config, navigate JTree         │
                   │    ├─ state machine: login → 2FA →     │
                   │    │    config → ready → monitor       │
                   │    ├─ IBC-compat command server (TCP)  │
                   │    └─ signals /tmp/gateway_ready_{mode} │
                   │                                        │
                   └────────────────────────────────────────┘
```

- **Python controller** does component discovery, clicks, state observation,
  the re-auth monitor loop, and the IBC-compat command server.
- **Java agent** (~750 lines) loaded via `-javaagent:` into Gateway's JVM
  exists because Gateway's Swing fields reject every external input
  mechanism (AT-SPI `EditableText` writes return `false`, synthetic X11
  events get filtered by Swing's AWT subsystem). The agent uses Swing's
  own `JTextField.setText()`, `AbstractButton.doClick()`,
  `JTree.setSelectionPath()`, and `JToggleButton.doClick()` — the only
  things that actually work.
- **AT-SPI2** drives component discovery and button clicks in the main
  window. Text input, tree navigation, and config dialog manipulation
  go through the agent.

Full diagnostic history and architectural reasoning: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)

## Env vars

### Credentials
| Var | Notes |
|---|---|
| `TWS_USERID` | Your IB username (or paper username if `TRADING_MODE=paper` and `TWS_USERID_PAPER` isn't set) |
| `TWS_PASSWORD` | Your IB password |
| `TWS_PASSWORD_FILE` | Alternative: read the password from a file at this path (Docker secrets pattern). `run.sh` loads it via `file_env` before launching the controller. |
| `TWS_USERID_PAPER` | Paper account username — used when `TRADING_MODE=paper` |
| `TWS_PASSWORD_PAPER` | Paper account password — used when `TRADING_MODE=paper` |
| `TRADING_MODE` | `live`, `paper`, or `both` (default: `paper`). `both` runs two Gateway JVMs in parallel with isolated state. |
| `TWOFACTOR_CODE` | Base32 TOTP secret from IBKR Mobile Authenticator setup. When set, the controller generates a TOTP code and enters it into the Second Factor Authentication dialog. |
| `TWOFACTOR_CODE_FILE` | Alternative: read the TOTP secret from a file (Docker secrets pattern). |

### 2FA timeout behavior (IBC-compat)
| Var | Notes |
|---|---|
| `TWOFA_EXIT_INTERVAL` | Seconds to wait for the Second Factor Authentication dialog to appear. Default `120`. Matches IBC's env var name. |
| `TWOFA_TIMEOUT_ACTION` | What to do if the wait expires: `exit` (controller exits non-zero), `restart` (in-place Gateway re-launch via the RESTART command path), or `none` (fall through to `wait_for_api_port` — matches Phase 1 behavior, useful for paper accounts / autorestart tokens that skip 2FA entirely). Default `none`. |
| `RELOGIN_AFTER_TWOFA_TIMEOUT` | `yes`/`no`. If `yes`, re-drive the login form once before dispatching `TWOFA_TIMEOUT_ACTION`. Matches IBC. |

### Disclaimer dismissal (IBC-compat)
| Var | Notes |
|---|---|
| `BYPASS_WARNING` | Comma- or semicolon-separated list of extra button labels to auto-dismiss in the post-login disclaimer loop. Extends the built-in allowlist (`I understand and accept`, `I Accept`, `Acknowledge`, `Accept and Continue`). Bare `OK` is permanently refused — clicking OK on Gateway's "Connecting to server..." progress modal cancels the in-progress login. |
| `TWS_COLD_RESTART` | `yes`/`no`. If `yes`, `apply_warm_state()` skips any `GATEWAY_WARM_STATE` copy and forces Gateway to cold-start. Useful for debugging stuck state. Matches IBC's env var. |

### Server (the bootstrap knob)
| Var | Notes |
|---|---|
| `TWS_SERVER` | IBKR regional server hostname (e.g. `ndc1.ibllc.com`, `cdc1.ibllc.com`). Default: Gateway's built-in default. **See [`docs/BOOTSTRAP.md`](docs/BOOTSTRAP.md) for how to find your server.** |
| `TWS_SERVER_PAPER` | Same but for paper mode. Some users have live on one region and paper on another. |

### Dialog behavior
| Var | Notes |
|---|---|
| `EXISTING_SESSION_DETECTED_ACTION` | `primary` (default) / `primaryoverride` / `secondary` / `manual`. Matches IBC's setting of the same name. Gateway 10.45.1c's dialog uses `Continue Login` for primary and `Cancel` for secondary. |

### Post-login API config (v0.2)
| Var | Notes |
|---|---|
| `TWS_MASTER_CLIENT_ID` | Integer. Sets Master API client ID in Configure → Settings → API. |
| `READ_ONLY_API` | `yes`/`no`. Toggles Read-Only API checkbox. |
| `AUTO_LOGOFF_TIME` | `HH:MM`. Sets Configure → Settings → Lock and Exit → Set Auto Log Off Time. |
| `AUTO_RESTART_TIME` | `HH:MM AM/PM`. Sets Configure → Settings → Lock and Exit → Set Auto Restart Time. |

Gateway's Lock and Exit panel shows **either** the Auto Log Off Time
field **or** the Auto Restart Time field depending on whether the
account has an active autorestart daily-token cycle. The controller
tries both labels and sets the one Gateway is currently displaying.
If the user sets the one Gateway isn't showing, a clear warning is
logged. Setting *both* env vars makes the controller handle whichever
Gateway is displaying in any given session.

### Command server (v0.2)
| Var | Notes |
|---|---|
| `CONTROLLER_COMMAND_SERVER_PORT` | TCP port to listen on for IBC-compat commands. Unset (default) disables the server. Set to `7462` to match IBC's default. In dual mode, the paper instance auto-offsets to `port+1` to avoid a bind collision. |
| `CONTROLLER_COMMAND_SERVER_HOST` | Bind address. Default `0.0.0.0` so Docker port forwarding works; restrict exposure with Docker's `-p 127.0.0.1:7462:7462` for loopback-only external access. |

Supported commands: `STOP`, `RESTART` (in-place Gateway JVM re-launch
+ full re-login), `RECONNECTACCOUNT`, `ENABLEAPI`. `RECONNECTDATA`
returns a clean error on Gateway (no File → Reconnect Data menu item)
and dispatches on TWS.

**Optional auth token (recommended if you expose the port beyond
localhost)**: set `CONTROLLER_COMMAND_SERVER_AUTH_TOKEN=<random-secret>`.
When set, clients must send `AUTH <token>\n` as their first line
before a command:

```
AUTH <token>
STOP
```

The token is checked with `hmac.compare_digest` to resist timing
side-channels. Without the token set, the command server runs in
IBC-compat no-auth mode and logs a loud WARNING at startup.

### Observability (v0.4.9)
| Var | Notes |
|---|---|
| `CONTROLLER_HEALTH_SERVER_PORT` | TCP port for the HTTP `/health` endpoint. Default `8080` in the shipped image, unset on source checkout. In dual mode, paper auto-offsets to `port+1`. Set to empty to disable the health server entirely. |
| `CONTROLLER_HEALTH_SERVER_HOST` | Bind address. Default `0.0.0.0` so Docker port forwarding works. Restrict external exposure with Docker's `-p 127.0.0.1:8080:8080` on the host side. |

`GET /health` returns JSON with the controller's state, Gateway JVM
liveness, API port status, CCP lockout streak, and the timestamp of
the most recent successful auth. HTTP 200 if the controller is logged
in and serving (`state==MONITORING` AND `api_port_open` AND
`jvm_alive`); 503 otherwise. `GET /ready` returns 200 while the
process is running (for Kubernetes-style readiness).

The controller also emits stable grep-contract log tokens
(`ALERT_CCP_PERSISTENT`, `ALERT_JVM_RESTART_EXHAUSTED`,
`ALERT_2FA_FAILED`, `ALERT_PASSWORD_EXPIRED`, `ALERT_LOGIN_FAILED`,
`ALERT_SHUTDOWN`) that external monitors can pattern-match on
regardless of log level. `ALERT_PASSWORD_EXPIRED` fires in two
flavors — `status=warning` (with `days_remaining=N` when the dialog
reports it, actionable before lockout) and `status=expired` (login
is already blocked, rotate the password in IBKR's web portal).
`ALERT_LOGIN_FAILED` fires when Gateway surfaces a credential-rejection
modal or when the `launcher.log` fingerprint matches a bad-credentials
auth flow (`reason=bad-credentials`), so monitors can tell a
stale-password account lockout apart from IBKR's silent cooldown
(`ALERT_CCP_PERSISTENT`) without waiting for the CCP-retry ceiling.
`ALERT_SHUTDOWN` is the lifecycle complement — INFO-level and emitted
on SIGTERM/SIGINT with `graceful=true|false` so dashboards can
distinguish operator-initiated shutdowns from Gateway-JVM crashes,
and flag stuck-JVM `graceful=false` restarts that need attention.

The shipped `Dockerfile` includes a `HEALTHCHECK` that curls `/health`
every 30s with a 180s start-period. In `DUAL_MODE=yes` it probes both
the live and paper ports; either side being unhealthy marks the
container unhealthy.

Full protocol, field semantics, and integration examples (cron,
Prometheus blackbox_exporter, jq): [`docs/OBSERVABILITY.md`](docs/OBSERVABILITY.md).

Playbook for operator-action scenarios (CCP lockout, 2FA failure,
JVM restart exhaustion, Gateway JVM crash): [`docs/DISCONNECT_RECOVERY.md`](docs/DISCONNECT_RECOVERY.md).

### Product selector (v0.2)
| Var | Notes |
|---|---|
| `GATEWAY_OR_TWS` | `gateway` (default) or `tws`. Switches launcher discovery (`$TWS_PATH/ibgateway/...` vs `$TWS_PATH/tws/...`) and AT-SPI app name search between IB Gateway and Trader Workstation. Code path unit-tested; live validation pending a TWS-with-controller Dockerfile variant. |

### Dual-mode per-instance (v0.2.1)
| Var | Notes |
|---|---|
| `TWS_SETTINGS_PATH` | In dual mode, `run.sh` sets this per instance (e.g. `/home/ibgateway/Jts_live`) so live and paper have isolated state. Single-mode users don't need to set it. |
| `CONTROLLER_READY_FILE` | Override the readiness signal file. Defaults to `/tmp/gateway_ready` (single mode) or `/tmp/gateway_ready_{mode}` (dual mode, set automatically by run.sh). |

### Other
| Var | Notes |
|---|---|
| `GATEWAY_WARM_STATE` | Optional path. If set, the controller copies files from this directory into Gateway's settings dir before launch. Useful for seeding a fresh container with a previously-captured jts.ini + encrypted state dir (for autorestart token support). |
| `GATEWAY_INPUT_AGENT_JAR` | Override path to the agent jar. Default: `~/gateway-input-agent.jar`. |
| `GATEWAY_INPUT_AGENT_SOCKET` | Override Unix socket path. Default: `/tmp/gateway-input.sock` (single mode) or `/tmp/gateway-input-{mode}.sock` (dual mode, set automatically by run.sh). |
| `CONTROLLER_DEBUG` | Set to `1` to enable debug-level logging. |
| `CONTROLLER_TEST_MODE` | Set to `1` to exit immediately after clicking Log In (for smoke tests). |

## Docker integration

This tool is designed to drop into a
[gnzsnz/ib-gateway-docker](https://github.com/gnzsnz/ib-gateway-docker)
image via a `Dockerfile.template` addition, replacing IBC. Install via
the Makefile target (`make install DESTDIR=...`) that drops
`gateway-input-agent.jar` and `scripts/gateway_controller.py` into the
`ibgateway` user's home.

See [`docs/MIGRATION.md`](docs/MIGRATION.md) for swapping out IBC in a
`gnzsnz/ib-gateway-docker`-style Dockerfile.

If you already have an IBC `config.ini`, the
[`ibc_config_to_env.py`](scripts/ibc_config_to_env.py) one-shot tool
converts it to the equivalent ibg-controller env vars (`.env`,
`docker run -e`, or `docker-compose` format), warning on unsupported
keys instead of silently dropping them. Full mapping table and cutover
recipe: [`docs/FROM_IBC.md`](docs/FROM_IBC.md).

```bash
./ibc_config_to_env.py /path/to/your/IBC/config.ini > .env
# or: ./ibc_config_to_env.py --format compose config.ini
```

## Building

```bash
# Build the agent jar and stage the controller into dist/
make

# Syntax-check the Python controller + validate the agent jar manifest
make test

# Create a release tarball (dist/ibg-controller-0.5.4.tar.gz)
make release VERSION=0.5.4

# Install directly into a running ibgateway home (for dev on host, or
# as called by the Docker image's setup stage)
make install DESTDIR=/home/ibgateway
```

Build requires a JDK 17+ (`javac` + `jar`) and `make`. No Maven, no Gradle.

### Installing from a release tarball (for consumers who don't build)

```bash
VER=0.5.4
curl -sSLO https://github.com/code-hustler-ft3d/ibg-controller/releases/download/v${VER}/ibg-controller-${VER}.tar.gz
tar -xzf ibg-controller-${VER}.tar.gz
cd ibg-controller-${VER}
DESTDIR=/home/ibgateway ./install.sh
```

The tarball layout is flat:
```
ibg-controller-0.5.4/
├── gateway-input-agent.jar    ← installed to $DESTDIR/gateway-input-agent.jar
├── gateway_controller.py      ← installed to $DESTDIR/scripts/gateway_controller.py
├── ibc_config_to_env.py       ← one-shot IBC config.ini → env migration tool
├── install.sh
├── README.md, CHANGELOG.md, LICENSE, SECURITY.md
└── docs/
    ├── ADR-001-in-jvm-dialog-dispatcher.md
    ├── ARCHITECTURE.md
    ├── BOOTSTRAP.md
    ├── DISCONNECT_RECOVERY.md
    ├── FROM_IBC.md
    ├── MIGRATION.md
    ├── OBSERVABILITY.md
    └── UPGRADING.md
```

## Troubleshooting

### IBKR auth lockouts and the controller's automatic backoff (v0.3.2+)

IBKR's auth server occasionally stops responding to fresh password
logins for several minutes (and occasionally hours) after a burst of
failed attempts from the same account. There are two visible failure
modes:

1. **CCP silent timeout** — Gateway logs a 20-second silent
   `AuthTimeoutMonitor-CCP: Timeout!` in `launcher.log` with no dialog
   on-screen. Gateway 10.45.1c also hides the error dialog that
   10.44.1g surfaces in the same state, making this worse.
2. **Stuck-connecting retry loop** — Gateway's login dialog stays up
   showing `Attempt N: connecting to server (trying for another XX
   seconds)`. The auth protocol never starts, so no `Timeout!` line
   appears in `launcher.log` at all — the only visible signal is the
   dialog text.

**What the controller does automatically**: as of v0.4.0, the controller
detects both modes, applies an exponential backoff between retries
(60s → 120s → 240s → 480s → 600s cap), and recovers by **re-driving
Log In on the existing Gateway JVM** — never by killing and
relaunching it. This matches
[IBC's `LoginManager.initiateLogin`](https://github.com/IbcAlpha/IBC/blob/master/src/ibcalpha/ibc/LoginManager.java)
pattern: IBKR's auth server treats each new JVM as a fresh handshake
and keeps the CCP limiter armed, so the previous v0.2.2–v0.3.2
"backoff + `do_restart_in_place`" design never let the lockout clear.
You'll see these lines:

```
CCP LOCKOUT DETECTED — IBKR's auth server silently dropped the auth request
CCP backoff: waiting 60s before next auth attempt
Retrying auth in-JVM after CCP backoff (attempt 1/8)
In-JVM relogin attempt (no JVM restart — matches IBC's LoginManager.initiateLogin semantics)
```

or

```
Login dialog stuck in 'connecting to server' retry loop — IBKR auth server isn't accepting sessions right now. Applying CCP backoff before retry.
CCP backoff: waiting 120s before next auth attempt
```

The backoff counter is per-trading-mode (live and paper run as
separate processes in dual mode, so they don't share state), and
resets on genuine 2FA-success. Up to 8 in-JVM retries per controller
lifetime; past that the controller exits and the container
orchestrator's restart policy takes over. **Just let it run** — the
controller will keep retrying with increasing delays until IBKR's
rate limiter clears, often 5–60 minutes total.

**If you're still stuck after an hour of patient backoff**, double-check:

1. You're sending the right username for the trading mode (the
   controller auto-swaps to `TWS_USERID_PAPER` when
   `TRADING_MODE=paper`, but double-check your env file)
2. Your `TWS_SERVER` / `TWS_SERVER_PAPER` matches the regional
   server your account is actually hosted on — see
   [`docs/BOOTSTRAP.md`](docs/BOOTSTRAP.md)
3. If you have a *previously working* container's `/home/ibgateway/Jts`
   state available, mount it via `GATEWAY_WARM_STATE` — autorestart
   token reauth goes through a different code path than fresh-password
   auth and bypasses the cooldown

If a clean retry from a known-good config still fails after the
backoff has run its course, the issue is almost certainly account-side
(wrong server, wrong userid, account locked), not the controller.
Gateway's `launcher.log` at `/home/ibgateway/Jts/launcher.log` will
confirm the CCP-Timeout case — you'll see the
`Authenticating` → `Timeout!` pattern with nothing in between. The
stuck-connecting case won't show in `launcher.log`; check the
controller's own logs for the "stuck in 'connecting to server'"
warning instead.

### "IBKR Gateway never appeared in AT-SPI desktop tree within 120s"

The ATK bridge didn't load. Check:

1. `ls $JAVA_HOME/conf/accessibility.properties` inside the container —
   must contain `assistive_technologies=org.GNOME.Accessibility.AtkWrapper`
2. `ls $JAVA_HOME/lib/libatk-wrapper.so` — must exist (copied from
   `/usr/lib/*/jni/libatk-wrapper.so` at image build time)
3. `at-spi-bus-launcher --launch-immediately` was started BEFORE
   Gateway. If the accessibility bus isn't up when Gateway's JVM
   initializes, the ATK wrapper silently skips itself.

### "Existing session detected" dialog keeps appearing in a loop

The controller's `EXISTING_SESSION_DETECTED_ACTION=primary` clicks
`Continue Login`, which tells IBKR to kick the *other* session. If
something else keeps reconnecting as that account (another container,
the mobile app, TWS on your desktop), you'll ping-pong forever. Shut
down the other session first.

### The controller logs "Gateway JVM PID (from agent): None"

The Java agent's `GET_PID` command returned nothing. This usually
means the agent's Unix socket is the old IBC default path (check
`GATEWAY_INPUT_AGENT_SOCKET` in the container's env) or the
`ProcessHandle` API isn't available (you're running on JRE < 17).
Verify the JRE is Zulu 17 or newer.

### Post-login config: "Auto Log Off Time" label not found

Gateway's Lock and Exit panel shows *either* "Set Auto Log Off Time
(HH:MM)" *or* "Set Auto Restart Time (HH:MM)" depending on account
state. The controller tries the label matching the env var you set.
If it can't find it, it warns which label Gateway is currently
showing and suggests the other env var. Set both `AUTO_LOGOFF_TIME`
and `AUTO_RESTART_TIME` if you want the controller to handle whichever
Gateway is displaying.

## Security

Full supply chain details (cosign verification, SBOM extraction,
pinning to digests, vuln reporting): [`SECURITY.md`](SECURITY.md).
Quick version of the deployment hygiene below.

This is a narrow threat model — single-user container running a
trading tool. But there are real things to get right.

**If you're running with `CONTROLLER_COMMAND_SERVER_PORT` set**:

1. **Bind to loopback only** via Docker: `-p 127.0.0.1:7462:7462`.
   The in-container bind is `0.0.0.0` by default so Docker port
   forwarding works at all; *Docker's* mapping is what controls
   external exposure. Never use a bare `-p 7462:7462` (which
   binds the host's external interface) unless you also set an
   auth token.
2. **Set `CONTROLLER_COMMAND_SERVER_AUTH_TOKEN`** to a random
   secret if the port is reachable by anything other than
   `127.0.0.1` on the host, or if the host is multi-tenant.
   Without it, anyone who can reach the port can send
   `STOP`/`RESTART`/`RECONNECTACCOUNT`.
3. The controller logs a loud WARNING at startup if the command
   server is enabled without an auth token. Don't ignore it.

**Credentials**:

1. Pass them via `docker run --env-file /path/to/.env`, never on the
   command line. The controller redacts them in its own logs; Gateway
   itself also redacts them in `launcher.log`.
2. Set your `.env` file to `600` on the host. Docker Engine only
   reads the file at `docker run` time; what's in `/proc/<pid>/environ`
   after that depends on your Docker Engine version.
3. For additional safety, consider Docker secrets (`_FILE` env vars
   like `TWS_PASSWORD_FILE`) — the controller delegates those to
   `run.sh`'s `file_env` helper which loads them from a separate
   file path.

**Logs and sharing them**:

1. `CONTROLLER_DEBUG=1` dumps modal dialog contents. The controller
   redacts account numbers matching `DU\d{5,10}` / `U\d{5,10}` (IBKR
   account format) before logging. Other identifying information
   like your username may still appear in window titles. **Review
   logs before posting them publicly**.
2. Gateway's own `/home/ibgateway/Jts/launcher.log` is NOT
   controlled by us and may include fragments of your session. If
   you attach it to a bug report, sanitize first.

**Warm state and file inputs**:

1. `GATEWAY_WARM_STATE` is trusted — only set it to a directory
   you own or copied from your own working container. A malicious
   warm-state dir could inject arbitrary content into
   `$JTS_CONFIG_DIR`.
2. `TWS_SERVER` / `TWS_SERVER_PAPER` are validated as hostnames
   (DNS label characters only) at controller startup. An invalid
   hostname aborts before Gateway starts.

**What's NOT protected**:

- The Java agent's Unix socket is reachable by any process in the
  same container that can read `/tmp/gateway-input-*.sock`
  (owner-only perms are set, but the container is single-user so
  this is mostly for defense-in-depth).
- The command server has no rate limiting.
- No TLS on the command server. Mitigated by recommending
  loopback-only binding via Docker's `-p 127.0.0.1:...`.

## License

MIT — see [LICENSE](LICENSE). Builds on work from IBC, ibctl, and
gnzsnz/ib-gateway-docker, all credited below.

## Acknowledgements

- **@rlktradewright** for [IBC](https://github.com/IbcAlpha/IBC). Most
  of what we know about driving Gateway's dialogs comes from reading IBC.
- **[Lcstyle/ibctl](https://github.com/Lcstyle/ibctl)** for the Java-agent
  architecture and the edge-case catalog. The idea of using an in-JVM
  agent for the operations that Swing rejects externally came from ibctl.
- **@gnzsnz** for steering the tool's architecture in
  [issue #366](https://github.com/gnzsnz/ib-gateway-docker/issues/366)
  and for making the `ib-gateway-docker` image this is meant to drop
  into.

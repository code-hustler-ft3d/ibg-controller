# ibg-controller

[![CI](https://github.com/code-hustler-ft3d/ibg-controller/actions/workflows/ci.yml/badge.svg)](https://github.com/code-hustler-ft3d/ibg-controller/actions/workflows/ci.yml)
[![Release image](https://github.com/code-hustler-ft3d/ibg-controller/actions/workflows/release-image.yml/badge.svg)](https://github.com/code-hustler-ft3d/ibg-controller/actions/workflows/release-image.yml)
[![Latest release](https://img.shields.io/github/v/release/code-hustler-ft3d/ibg-controller?sort=semver)](https://github.com/code-hustler-ft3d/ibg-controller/releases)
[![License: MIT](https://img.shields.io/github/license/code-hustler-ft3d/ibg-controller)](LICENSE)
[![cosign signed](https://img.shields.io/badge/cosign-signed-0a84ff?logo=sigstore&logoColor=white)](SECURITY.md)

A drop-in replacement for [IBC](https://github.com/IbcAlpha/IBC) on
headless Docker IB Gateway. A Python controller plus a small in-JVM
Java agent: launches Gateway, drives the login dialog (including TOTP
2FA), applies post-login API config, monitors for re-auth events, and
speaks IBC's TCP command protocol.

IBC is deprecated as of September 2026. This is one of the community
paths forward, written in Python so the
[`gnzsnz/ib-gateway-docker`](https://github.com/gnzsnz/ib-gateway-docker)
community can read and patch it without a JVM or Rust toolchain. That
image is also the base this project builds on and is tested against
(`UPSTREAM_IMAGE` is a build arg if you need a different one).

## Quick start

```bash
docker pull ghcr.io/code-hustler-ft3d/ibg-controller:latest

docker run -d --name ibkr \
  --env-file /path/to/your/.env \
  -e TRADING_MODE=paper \
  -e TWS_SERVER_PAPER=cdc1.ibllc.com \
  -p 127.0.0.1:4002:4004 \
  ghcr.io/code-hustler-ft3d/ibg-controller:latest
```

Tags: `:latest`, `:<major>.<minor>`, `:v<major>.<minor>.<patch>`. All
cosign-signed; verification recipe and digest pinning in
[`SECURITY.md`](SECURITY.md).

**If you use docker compose, set `stop_grace_period: 90s`.** Docker's
default 10s is too short for the clean-logout chain, and cutting it
short strands IBKR session slots on every restart
([timing math](docs/MIGRATION.md#shutdown-grace-period)):

```yaml
services:
  ib-gateway:
    image: ghcr.io/code-hustler-ft3d/ibg-controller:latest
    stop_grace_period: 90s   # required
    environment:
      TRADING_MODE: paper
      TWS_SERVER_PAPER: cdc1.ibllc.com
      USE_IBG_CONTROLLER: "yes"
      # ... your other env vars
```

Deeper guides:

- Build your own image, or add the controller to an existing one: [`docs/MIGRATION.md`](docs/MIGRATION.md)
- Coming from IBC (`config.ini` → env vars): [`docs/FROM_IBC.md`](docs/FROM_IBC.md)
- Finding your regional server (`TWS_SERVER`): [`docs/BOOTSTRAP.md`](docs/BOOTSTRAP.md)
- Why each piece exists: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)

## Requirements

| Requirement | Notes |
|---|---|
| Linux `amd64`/`arm64` | Ubuntu 24.04 base tested |
| IB Gateway 10.x | Release images pin **10.45.1j** (gnzsnz `:stable` line) |
| Python 3.10+ | Runtime; stdlib only, no pip installs |
| JDK 17+ | Build time only — runtime uses the JRE bundled with Gateway |
| `python3`, `matchbox-window-manager` | The only packages added on top of the upstream image |

## What works

| Feature | Gateway | TWS | Notes |
|---|---|---|---|
| Paper / live / dual-mode cold start | ✅ verified | ⚠️ code in place | dual mode = two isolated JVMs |
| TOTP 2FA (single method) | ✅ verified | ⚠️ code in place | |
| IB Key push 2FA | ✅ wait mode | ✅ wait mode | waits for you to approve on the phone |
| Multi-method 2FA | ⚠️ fails loud | ⚠️ fails loud | both dialog shapes detected; see [2FA](#2fa) |
| Existing-session dialog | ✅ verified | ⚠️ code in place | |
| Post-login config (`READ_ONLY_API`, `TWS_MASTER_CLIENT_ID`, auto logoff/restart times) | ✅ verified | ⚠️ untested | |
| Command server (`STOP`, `RESTART`, `RECONNECTACCOUNT`, `ENABLEAPI`) | ✅ verified | ⚠️ untested | `RECONNECTDATA` is TWS-only |

✅ verified = run end-to-end against a real IB account.
⚠️ code in place = written and unit-tested, not yet run against the
real product.

## 2FA

- **Use Mobile Authenticator (TOTP)** for unattended operation: set
  `TWOFACTOR_CODE` to the base32 secret from IBKR's authenticator
  setup. This is the only method a headless container can satisfy on
  its own.
- **IB Key push** requires a human to tap approve on a phone. The
  controller will wait for that (leave `TWOFACTOR_CODE` unset), which
  is fine attended and a dead end for automation.
- **Passkey / WebAuthn accounts are not supported for unattended
  login.** IBKR forced some regions (Hong Kong and Japan as of 2026-08)
  onto passkeys; Gateway then opens an in-app browser expecting a
  hardware security key, which a headless container can't present. The
  controller detects this and fails loudly (`ALERT_2FA_FAILED
  reason="passkey/WebAuthn 2FA flow ..."`) instead of hanging — it does
  not drive the ceremony. Emulating a WebAuthn authenticator (a
  FIDO2/USB device or a browser debug channel) is a different job than
  driving Gateway's dialogs and out of scope for this tool; it also
  can't run on arm64, which ships no jxbrowser build. If the account
  still offers Mobile Authenticator, switch to it; otherwise log in
  attended via VNC, or run your own authenticator alongside the
  container. See
  [#22](https://github.com/code-hustler-ft3d/ibg-controller/issues/22).
- **Accounts with more than one method**: Gateway pre-picks one, and
  the dialog shape varies by account — some get a code dialog
  defaulted to one method, others a device-selector list. The
  controller detects both shapes. If the pre-pick doesn't match
  `TWOFACTOR_CODE` it selects the right device where the dialog allows
  it, and otherwise fails loudly (`ALERT_2FA_FAILED`) with the fix in
  the log — it never types the code into the wrong method. IBKR
  rejects mid-challenge method switching server-side
  ([#20](https://github.com/code-hustler-ft3d/ibg-controller/issues/20)),
  so the durable fix is on the account: make Mobile Authenticator your
  default (or only) method in Client Portal → Settings → User Settings
  → Security → Secure Login System. Background:
  [#7](https://github.com/code-hustler-ft3d/ibg-controller/issues/7),
  [#20](https://github.com/code-hustler-ft3d/ibg-controller/issues/20).

## Env vars

### Credentials

| Var | Notes |
|---|---|
| `TWS_USERID` / `TWS_PASSWORD` | IB credentials |
| `TWS_USERID_PAPER` / `TWS_PASSWORD_PAPER` | Paper credentials, used when `TRADING_MODE=paper` |
| `TWS_PASSWORD_FILE`, `TWOFACTOR_CODE_FILE` | Docker-secrets variants: read the value from a file |
| `TRADING_MODE` | `live`, `paper` (default), or `both` |
| `TWOFACTOR_CODE` | Base32 TOTP secret; enables automatic 2FA entry |
| `TWOFA_DEVICE` | IBC-compatible. Multi-method accounts only: names the method `TWOFACTOR_CODE` satisfies (default `Mobile Authenticator app`). Ignored on single-method accounts. |

### Connection

| Var | Notes |
|---|---|
| `TWS_SERVER` / `TWS_SERVER_PAPER` | IBKR regional server hostname — see [`docs/BOOTSTRAP.md`](docs/BOOTSTRAP.md) |
| `GATEWAY_OR_TWS` | `gateway` (default) or `tws` |

### Dialog handling (IBC-compat)

| Var | Notes |
|---|---|
| `EXISTING_SESSION_DETECTED_ACTION` | `primary` (default) / `primaryoverride` / `secondary` / `manual` |
| `TWOFA_EXIT_INTERVAL` | Seconds to wait for the 2FA dialog (default `120`) |
| `TWOFA_TIMEOUT_ACTION` | On timeout: `exit`, `restart`, or `none` (default) |
| `RELOGIN_AFTER_TWOFA_TIMEOUT` | `yes`/`no`: re-drive the login form once before the timeout action |
| `BYPASS_WARNING` | Extra disclaimer button labels to auto-dismiss (comma/semicolon-separated). Bare `OK` is refused — it cancels in-progress logins. |
| `TWS_COLD_RESTART` | `yes` skips the warm-state copy and cold-starts Gateway |

### Post-login API config

| Var | Notes |
|---|---|
| `TWS_MASTER_CLIENT_ID` | Master API client ID |
| `READ_ONLY_API` | `yes`/`no` |
| `AUTO_LOGOFF_TIME` / `AUTO_RESTART_TIME` | `HH:MM` / `HH:MM AM/PM`. Gateway shows one field or the other depending on account state; set both vars and the controller handles whichever is displayed. |
| `AUTO_RESTART_ADOPT` | Default `yes`: when Gateway restarts itself at `AUTO_RESTART_TIME`, adopt the new JVM instead of launching a second one. Gateway normally carries the session across, so no login and no 2FA; when it doesn't, the controller re-drives login on the adopted JVM. `no` restores always-relaunch. Wait budget: `AUTO_RESTART_ADOPT_TIMEOUT_SECONDS` (90). |

### Command server

| Var | Notes |
|---|---|
| `CONTROLLER_COMMAND_SERVER_PORT` | TCP port for IBC-compat commands (`STOP`, `RESTART`, `RECONNECTACCOUNT`, `ENABLEAPI`). Unset = disabled. IBC's default was `7462`. |
| `CONTROLLER_COMMAND_SERVER_HOST` | Bind address, default `0.0.0.0` (control exposure with Docker's `-p 127.0.0.1:...`) |
| `CONTROLLER_COMMAND_SERVER_AUTH_TOKEN` | Optional shared secret; clients send `AUTH <token>` first. Strongly recommended if the port is reachable beyond localhost. |

### Health and recovery

| Var | Notes |
|---|---|
| `CONTROLLER_HEALTH_SERVER_PORT` | HTTP `/health` port (default `8080` in the shipped image; empty disables) |
| `CONTROLLER_HEALTH_SERVER_HOST` | Bind address, default `0.0.0.0` |
| `CCP_MAINTENANCE_RECOVERY_DELAY_SECONDS` | Delay before re-auth inside IBKR's nightly maintenance window (default `480`) |
| `CCP_LOCKOUT_MAX_JVM_RESTARTS` | JVM restarts allowed on persistent CCP lockout (default `0` = halt loudly) |

### Paths and debugging

| Var | Notes |
|---|---|
| `TWS_SETTINGS_PATH` | Gateway settings dir; set per instance by `run.sh` in dual mode |
| `GATEWAY_WARM_STATE` | Optional dir copied into the settings dir before launch (seeds `jts.ini` + autorestart tokens) |
| `GATEWAY_INPUT_AGENT_JAR` / `GATEWAY_INPUT_AGENT_SOCKET` | Agent jar / socket path overrides |
| `CONTROLLER_READY_FILE` | Readiness signal file override |
| `CONTROLLER_DEBUG` | `1` = debug logging |
| `CONTROLLER_TEST_MODE` | `1` = exit right after clicking Log In (smoke tests) |

## Monitoring

`GET /health` returns controller state, JVM liveness, API port status,
and last-auth timestamp — HTTP 200 when logged in and serving, 503
otherwise. `GET /ready` is a process-liveness probe. The shipped
Dockerfile wires this into a Docker `HEALTHCHECK`.

The logs carry stable `ALERT_*` tokens (`ALERT_2FA_FAILED`,
`ALERT_LOGIN_FAILED`, `ALERT_CCP_PERSISTENT`, `ALERT_PASSWORD_EXPIRED`,
`ALERT_SHUTDOWN`, ...) that monitors can grep regardless of log level.
Token names and keys are a stability contract. Full inventory, field
semantics, and integration examples:
[`docs/OBSERVABILITY.md`](docs/OBSERVABILITY.md).

Operator playbook for failure scenarios (CCP lockout, 2FA failure, JVM
crash): [`docs/DISCONNECT_RECOVERY.md`](docs/DISCONNECT_RECOVERY.md).

## How it works

```
                   ┌────────────────────────────────────────┐
                   │  Docker container (headless)           │
                   │                                        │
                   │  Xvfb :1  ← matchbox WM                │
                   │     │                                  │
                   │     ↓                                  │
                   │  IB Gateway JVM                        │
                   │  └─ -javaagent:gateway-input-agent.jar │
                   │            │                           │
                   │            ↓                           │
                   │     Unix socket (/tmp/gateway-input-{mode}.sock)
                   │            ↑                           │
                   │  gateway_controller.py (Python)        │
                   │    ├─ state machine: login → 2FA →     │
                   │    │    config → ready → monitor       │
                   │    ├─ IBC-compat command server (TCP)  │
                   │    └─ /health endpoint                 │
                   │                                        │
                   └────────────────────────────────────────┘
```

Gateway's Swing fields reject every external input mechanism
(synthetic X11 events, AT-SPI writes), so a small Java agent
(~950 lines, no dependencies) is loaded into Gateway's JVM via
`-javaagent:` and does the UI work from inside — `setText`, `doClick`,
tree/list selection — over a line-based Unix-socket protocol. The
Python controller runs the state machine and never touches the UI
directly. Design history and the reasoning behind each piece:
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

Replaces IBC (entirely, for Gateway), oathtool (stdlib TOTP), and
xdotool (the agent types from inside the JVM).

## Building

```bash
make            # build the agent jar, stage controller into dist/
make test       # build checks + full unit suite (stdlib unittest, no pip)
make release VERSION=x.y.z
make install DESTDIR=/home/ibgateway
```

Needs `make` and a JDK 17+. No Maven, no Gradle, no pip. Release
tarballs with an `install.sh` are attached to each
[GitHub release](https://github.com/code-hustler-ft3d/ibg-controller/releases).

## Troubleshooting

**`CCP LOCKOUT DETECTED` / login retries looping.** IBKR's auth server
rate-limits fresh logins after failed attempts. The controller detects
it, backs off exponentially (60s → 600s), and retries in-JVM — just
let it run; it typically clears in 5–60 minutes. If you're stuck after
an hour: check the userid matches the trading mode, check
`TWS_SERVER` against [`docs/BOOTSTRAP.md`](docs/BOOTSTRAP.md), and see
the [full playbook](docs/DISCONNECT_RECOVERY.md#scenario-ccp-lockout-concurrent-ibkr-session).
On images before v0.5.12 this warning was usually a JVM-internal
deadlock, not a real lockout — upgrade.

**"Gateway PID unknown (agent never reported one)".** The in-JVM agent
didn't start. Check the `-javaagent:` flag is on the JVM command line,
the socket in `GATEWAY_INPUT_AGENT_SOCKET` exists, and
`/tmp/jvm_console_${TRADING_MODE}.log` for agent boot errors.

**"Existing session detected" loops forever.** Something else keeps
logging in as the same account (another container, TWS on your
desktop, the mobile app). Shut the other session down.

**"Auto Log Off Time" label not found.** Gateway shows either the
logoff or the restart field depending on account state — set both
`AUTO_LOGOFF_TIME` and `AUTO_RESTART_TIME` and the controller handles
whichever is displayed.

## Security

Supply chain: no third-party dependencies (Python stdlib + one
dependency-free Java file), digest-pinned base image, cosign-signed
release images. Verification recipes and reporting:
[`SECURITY.md`](SECURITY.md).

Deployment hygiene:

- Command server: keep it loopback-only (`-p 127.0.0.1:7462:7462`)
  or set `CONTROLLER_COMMAND_SERVER_AUTH_TOKEN`. Without a token,
  anyone who can reach the port can send `STOP`/`RESTART`.
- Credentials: use `--env-file` (mode `600`) or the `_FILE` secrets
  variants, never `-e` on the command line.
- Logs: the controller redacts account numbers and (v0.6.3+) password
  fields at the source, but window titles can still carry your
  username, and Gateway's own `launcher.log` is not under our control.
  Review before posting publicly.
- `GATEWAY_WARM_STATE` is trusted input — only point it at a directory
  you own.

## License

MIT — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

## Acknowledgements

- **@rlktradewright** for [IBC](https://github.com/IbcAlpha/IBC) —
  most of what we know about driving Gateway's dialogs comes from
  reading IBC.
- **[Lcstyle/ibctl](https://github.com/Lcstyle/ibctl)** for the
  in-JVM agent idea and the edge-case catalog.
- **@gnzsnz** for
  [ib-gateway-docker](https://github.com/gnzsnz/ib-gateway-docker)
  and for steering this tool's architecture in
  [issue #366](https://github.com/gnzsnz/ib-gateway-docker/issues/366).

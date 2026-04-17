# Migrating from IBC

A practical playbook for moving an IBC-based IB Gateway deployment to
`ibg-controller`. Covers the env-var mapping, the one-shot migration
tool, a cutover recipe, a rollback path, and behavior differences
worth knowing about.

This guide targets the **Gateway / headless Docker** use case that
most `gnzsnz/ib-gateway-docker` users are on. TWS migration has the
same mapping but isn't yet live-validated — see the [Compatibility
table](../README.md#compatibility-table) in the README before you
commit to it for TWS.

## TL;DR

```bash
# 1. Run the one-shot tool against your existing IBC config.ini
./ibc_config_to_env.py /path/to/your/IBC/config.ini > .env

# 2. Review .env — especially anything in the warning lines on stderr
vim .env

# 3. Stand up a paper-mode container next to your existing one,
#    pointing at .env. Observe for 72 hours, then cut live over.
docker run -d --name ibkr-new --env-file .env \
  -e TRADING_MODE=paper \
  -p 127.0.0.1:4002:4004 \
  your-ibg-controller-image
```

## Env-var mapping (IBC → ibg-controller)

All rows are what `./ibc_config_to_env.py` produces automatically.
Keys not listed here fall into one of three buckets: handled
implicitly (no env var needed), unsupported (you stay on IBC if you
depend on them), or unknown (the tool emits a warning so you can
review). The tool's `--help` output lists every key it knows about.

### Credentials

| IBC key | controller env | Notes |
|---|---|---|
| `IbLoginId` | `TWS_USERID` | Straight rename. With `--trading-mode paper`, becomes `TWS_USERID_PAPER`. |
| `IbPassword` | `TWS_PASSWORD` | Same. Prefer `TWS_PASSWORD_FILE` (Docker secrets) over inline. |
| `TradingMode` | `TRADING_MODE` | `live` / `paper` / `both` (ibg-controller extension — runs both modes in one container). |

### 2FA

| IBC key | controller env | Notes |
|---|---|---|
| `TwoFactorDevice` / `SecondFactorDevice` | — | Handled implicitly — controller polls for the 2FA dialog to be dismissed (same approach IBC takes). Add `TWOFACTOR_CODE=<base32-secret>` if you're on TOTP. |
| `SecondFactorAuthenticationExitInterval` | `TWOFA_EXIT_INTERVAL` | Same value (seconds). |
| `ExitAfterSecondFactorAuthenticationTimeout` | `TWOFA_TIMEOUT_ACTION` | `yes` → `exit`, `no` → `none`. ibg-controller also supports `restart` (in-place relogin). |
| `ReloginAfterSecondFactorAuthenticationTimeout` | `RELOGIN_AFTER_TWOFA_TIMEOUT` | Same yes/no. |

### Post-login / session

| IBC key | controller env | Notes |
|---|---|---|
| `ExistingSessionDetectedAction` | `EXISTING_SESSION_DETECTED_ACTION` | `primary` / `primaryoverride` / `secondary` / `manual`. |
| `ReadOnlyApi` | `READ_ONLY_API` | yes/no. |
| `AutoRestartTime` | `AUTO_RESTART_TIME` | `HH:MM AM/PM`. |
| `IbAutoClosedown` | — | Set `AUTO_LOGOFF_TIME=HH:MM` directly; the controller drives the Gateway field with that value. |
| `AllowBlindTrading` | `ALLOW_BLIND_TRADING` | yes/no. |
| `BypassWarning` | `BYPASS_WARNING` | **Different shape**. IBC's yes/no maps to a comma-separated allowlist of exact button labels. Review what dialogs your Gateway actually shows and list them: `BYPASS_WARNING="Yes,Continue,Acknowledge"`. |
| `SaveTwsSettingsAt` | `SAVE_TWS_SETTINGS` | Same. |
| `TimeZone` | `TIME_ZONE` | Same. |
| `TwsSettingsPath` | `TWS_SETTINGS_PATH` | Same. |

### Command server

| IBC key | controller env | Notes |
|---|---|---|
| `CommandServerPort` | `CONTROLLER_COMMAND_SERVER_PORT` | Same. In dual-mode, paper auto-offsets to `port+1`. |
| `IbControllerPort` | `CONTROLLER_COMMAND_SERVER_PORT` | Legacy IBC alias — same target. |
| `BindAddress` | `CONTROLLER_COMMAND_SERVER_HOST` | Same. Defaults to `0.0.0.0` so Docker port forwarding works; restrict external exposure with `-p 127.0.0.1:7462:7462` on the host. |
| `ControlFrom` | — | IBC uses an IP allowlist. ibg-controller uses an auth token instead: set `CONTROLLER_COMMAND_SERVER_AUTH_TOKEN=<random-secret>` and clients send `AUTH <token>\n` before each command. See [README.md §Security](../README.md#security). |

### Unsupported in ibg-controller

If you rely on any of these, **stay on IBC**:

- `FIX` / `FIXLoginId` / `FIXPassword` — FIX CTCI mode
- `CustomConfig` — the controller reads env vars directly, not an ini file
- `MinimizeMainWindow` / `MaximizeMainWindow` — no-op in headless Docker
- `StoreSettingsOnServer` — Gateway's own default is used
- `SuppressInfoMessages` / `LogComponents` — controller logging is governed by `CONTROLLER_DEBUG=1` only
- `ClosedownAt` — IBC-specific scheduled-shutdown; use `AUTO_LOGOFF_TIME` for the Gateway-driven equivalent

## The one-shot tool

`./ibc_config_to_env.py` (shipped in the release tarball at the root,
next to `install.sh`) parses an IBC `config.ini` and emits env
mappings in your choice of format:

```bash
# Docker --env-file format (default)
./ibc_config_to_env.py config.ini > .env

# Flags to paste into `docker run`
./ibc_config_to_env.py --format docker config.ini

# YAML block for docker-compose.yml
./ibc_config_to_env.py --format compose config.ini

# Dual-mode paper credentials (renames IbLoginId → TWS_USERID_PAPER)
./ibc_config_to_env.py --trading-mode paper config.ini
```

Warnings go to stderr — always read them before trusting the output.
The tool is intentionally conservative: when IBC's semantics don't
cleanly map to one controller env var, it warns rather than guessing.

## Cutover recipe

### 1. Build the ibg-controller image

```bash
git clone https://github.com/code-hustler-ft3d/ibg-controller
cd ibg-controller
make                         # builds agent jar + stages controller
docker build -t ibg-controller:local .
```

The shipped `Dockerfile` extends `ghcr.io/gnzsnz/ib-gateway:stable`.
For reproducible builds, pin to a digest via `--build-arg
UPSTREAM_IMAGE=...@sha256:...`.

### 2. Convert your IBC config

```bash
./scripts/ibc_config_to_env.py /path/to/IBC/config.ini > .env.new
```

Review `.env.new` and the stderr warnings. The most common manual
fix is `BYPASS_WARNING`: IBC's `yes` becomes the controller's exact
comma-separated allowlist, and the right list depends on which
dialogs your Gateway actually surfaces. If in doubt, start with
`BYPASS_WARNING=""` (the built-in allowlist covers the common ones)
and add labels if you see a specific dialog blocking the flow in
your logs.

### 3. Stand up a paper-mode container alongside your existing IBC one

Do NOT cut the live side over directly. Run paper-mode for at least
72 hours to catch login flow issues, then swap live.

```bash
docker run -d --name ibkr-new \
  --env-file .env.new \
  -e TRADING_MODE=paper \
  -e TWS_SERVER_PAPER=<your-regional-server> \
  -p 127.0.0.1:4002:4004 \
  -p 127.0.0.1:8080:8080 \
  ibg-controller:local
```

Watch the logs (`docker logs -f ibkr-new`) through a full login cycle
including any 2FA prompt. Confirm:

- `CONTROLLER: READY` appears
- `curl http://127.0.0.1:8080/health` returns `{"status":"healthy",...}`
- Your trading client on port 4002 connects cleanly

### 4. Observe for 72 hours

Look for any of the `ALERT_*` tokens in the logs:

```bash
docker logs ibkr-new 2>&1 | grep -E 'ALERT_(CCP_PERSISTENT|JVM_RESTART_EXHAUSTED|2FA_FAILED|PASSWORD_EXPIRED)'
```

See [docs/OBSERVABILITY.md](OBSERVABILITY.md) for what each one
means and [docs/DISCONNECT_RECOVERY.md](DISCONNECT_RECOVERY.md) for
operator playbooks.

### 5. Cut live over

Once paper is clean, repeat the process for live:

```bash
docker stop ibkr-ibc            # the old IBC container
docker run -d --name ibkr-live \
  --env-file .env.new \
  -e TRADING_MODE=live \
  -e TWS_SERVER=<your-regional-server> \
  -p 127.0.0.1:4001:4003 \
  -p 127.0.0.1:8081:8080 \
  ibg-controller:local
```

Or, if you want both modes in one container, use
`TRADING_MODE=both` — ibg-controller runs two isolated Gateway JVMs
with per-mode state directories, and the command + health servers
auto-offset by one on the paper side.

## Rollback path

The old IBC container still exists (`docker ps -a`) unless you
deleted it. Stop the new one, start the old one:

```bash
docker stop ibkr-live
docker start ibkr-ibc
```

No shared state between them — each container has its own
`/home/ibgateway/Jts` settings directory. Rolling back is
zero-destructive.

If you've already deleted the IBC container and need to rebuild it:
IBC's own image is still available on Docker Hub and works with the
same `config.ini` you were using before.

## Behavior differences worth knowing

1. **Per-mode credentials in dual-mode**. IBC runs one process per
   mode; ibg-controller can run both modes in one container. In that
   mode you set `TWS_USERID` + `TWS_PASSWORD` for live and
   `TWS_USERID_PAPER` + `TWS_PASSWORD_PAPER` for paper. The migration
   tool's `--trading-mode paper` flag renames the IBC keys accordingly
   if you're going that route.

2. **Command server auth model**. IBC gates the command server by
   source-IP allowlist (`ControlFrom`). ibg-controller gates it by
   token (`CONTROLLER_COMMAND_SERVER_AUTH_TOKEN`). Token is
   verified with `hmac.compare_digest` to resist timing attacks. If
   the token is unset the server runs in IBC-compat no-auth mode and
   logs a loud warning.

3. **CCP lockout recovery**. IBC keeps hitting Gateway's login on a
   fixed retry cadence. ibg-controller runs an exponential backoff
   (60s → 120s → 240s → 480s → 600s cap) and does the relogin
   *in-JVM* (matches IBC's own `LoginManager.initiateLogin` semantics
   but without the JVM-restart churn that re-arms IBKR's rate
   limiter). If concurrent-session lockout persists it escalates to
   a silent cool-down with the JVM killed — see
   [docs/DISCONNECT_RECOVERY.md](DISCONNECT_RECOVERY.md).

4. **Observability surface**. ibg-controller adds a first-class
   `/health` HTTP endpoint (default port 8080) and stable
   `ALERT_*` grep tokens in the logs. IBC has neither — if you've
   been tailing IBC logs for known strings, the
   [OBSERVABILITY.md](OBSERVABILITY.md) contract gives you proper
   versioned signals to alert on instead.

5. **`BYPASS_WARNING` shape**. As noted above, IBC's `yes` becomes a
   list of exact button labels. This is strictly *safer* than IBC's
   approach — clicking "OK" on Gateway's "Connecting to server..."
   progress modal cancels the in-progress login, so a blanket
   allow-all could silently break your cold start. The
   [ARCHITECTURE.md](ARCHITECTURE.md) file has the investigation
   behind that deny-list.

6. **Password expiry**. IBC's `PasswordExpiryWarningDialogHandler`
   just dismisses the warning. ibg-controller (v0.5.0+) dismisses it
   *and* emits `ALERT_PASSWORD_EXPIRED` with `status=warning`
   (`days_remaining=N` when the dialog reports it) or `status=expired`
   when login is already blocked, so your monitoring can rotate the
   password *before* the account locks out and escalate differently
   once it has.

## Questions / gotchas

**Do I need to change my trading-client (gnzsnz-style socat port)
configuration?** No. The controller doesn't touch the socat
forwarder or the ports your trading code connects to. The swap is
transparent to the client side.

**What about the `run.sh`?** The shipped `Dockerfile` swaps in an
ibg-controller-aware `run.sh` that sets `USE_PYATSPI2_CONTROLLER=yes`
and dispatches to the controller instead of IBC's jar. If you're
building a custom image, pick up `docker/run.sh` from the release
tarball.

**Where's the `.env`? Can I commit it?** Keep credentials out of git.
Use Docker secrets (`TWS_PASSWORD_FILE=/run/secrets/tws_password`),
or an untracked `.env.local` sourced only at `docker run` time. The
migration tool emits raw values because that's what IBC's
`config.ini` has; it's on you to replace them with secret references
before the file leaves your machine.

**I hit a dialog the controller doesn't handle.** File an issue with
the window dump (set `CONTROLLER_DEBUG=1` to get one in the logs).
Each new dialog handler is ~20 lines once the shape is known. PRs
welcome.

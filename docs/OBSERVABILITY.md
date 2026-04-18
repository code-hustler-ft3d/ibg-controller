# Observability

Everything the controller exposes for external monitoring: the HTTP
`/health` endpoint, the stable `ALERT_*` log tokens that external
watchers can grep on, the Docker `HEALTHCHECK`, and the env vars that
control it all.

## TL;DR

```bash
# Single-mode container: one /health endpoint on 8080.
curl http://ibkr:8080/health

# Dual-mode container: live on 8080, paper on 8081 (auto-offset).
curl http://ibkr:8080/health   # live
curl http://ibkr:8081/health   # paper
```

HTTP 200 = controller is in `MONITORING` state, API port is open,
JVM is alive. HTTP 503 = anything else. The JSON body is the same
either way, so parsers can inspect it regardless of status.

## The `/health` endpoint

### Protocol

- **Method**: `GET`
- **Path**: `/health`
- **Auth**: none (bind to loopback or put it behind your own reverse
  proxy if you expose the port beyond the container).
- **Content-Type**: `application/json`
- **Status**: `200` if healthy, `503` if unhealthy. Any other path
  → `404`.

There's also a shallow `GET /ready` that returns `200` with
`{"status":"up"}` as long as the controller process is running. Useful
for Kubernetes-style readiness where "process up" is the signal.

### JSON shape (v0.5.2)

```json
{
  "status": "healthy",
  "version": "0.5.2",
  "mode": "live",
  "state": "MONITORING",
  "jvm_pid": 12345,
  "jvm_alive": true,
  "api_port": 4001,
  "api_port_open": true,
  "last_auth_success_ts": 1712345678.9,
  "last_auth_success_age_seconds": 42.5,
  "ccp_lockout_streak": 0,
  "ccp_backoff_seconds": 0.0,
  "uptime_seconds": 3456.7
}
```

| Field | Type | Meaning |
|---|---|---|
| `status` | `"healthy"` \| `"unhealthy"` | `healthy` iff `state == "MONITORING"` AND `api_port_open` AND `jvm_alive`. Everything else is `unhealthy`. |
| `version` | string | Controller version (`__version__`). |
| `mode` | `"live"` \| `"paper"` | The `TRADING_MODE` this controller is driving. |
| `state` | string | Controller state machine position. One of `INIT`, `LAUNCHING`, `AGENT_WAIT`, `APP_DISCOVERY`, `LOGIN`, `POST_LOGIN`, `TWO_FA`, `DISCLAIMERS`, `API_WAIT`, `CONFIG`, `COMMAND_SERVER`, `READY`, `MONITORING`. |
| `jvm_pid` | int \| null | OS PID of the Gateway JVM. `null` before agent discovery completes. |
| `jvm_alive` | bool | `true` iff the controller's `subprocess.Popen` handle reports the JVM hasn't exited. |
| `api_port` | int | `4001` (live) or `4002` (paper). |
| `api_port_open` | bool | TCP-probe of `127.0.0.1:api_port` inside the container. **Note**: this probes the Gateway's real listener directly, *not* the socat forwarder — so this is the true authenticated-and-serving signal. |
| `last_auth_success_ts` | float \| null | Wall-clock `time.time()` of the most recent successful auth. `null` until the first success in this process's lifetime. |
| `last_auth_success_age_seconds` | float \| null | `time.time() - last_auth_success_ts` at request time. |
| `ccp_lockout_streak` | int | Number of consecutive CCP lockouts seen. Resets to 0 on auth success. `>= 3` triggers `ALERT_CCP_PERSISTENT` in the logs (see below). |
| `ccp_backoff_seconds` | float | Current CCP backoff duration (exponential: 60 → 120 → 240 → 480 → 600). `0` when no backoff is active. |
| `uptime_seconds` | float | Seconds since the Python controller module loaded. |

### Healthy vs. unhealthy — what to do

- **Healthy (200)**: do nothing. Controller is logged in and serving.
- **Unhealthy (503) with `state != "MONITORING"`**: controller is still
  booting up. Wait. The Dockerfile's `HEALTHCHECK --start-period=180s`
  gives a grace window for this.
- **Unhealthy (503) with `state == "MONITORING"` and
  `api_port_open == false`**: Gateway crashed or auth slot was lost.
  The controller's own recovery loop will attempt to restart. If
  `ccp_lockout_streak >= 3`, see the [CCP lockout
  playbook](DISCONNECT_RECOVERY.md#scenario-ccp-lockout).
- **Unhealthy (503) with `jvm_alive == false`**: Gateway JVM has
  exited. The controller will relaunch it.
- **Endpoint not reachable at all**: controller process is down.
  Restart the container.

## `ALERT_*` log tokens

Stable grep-contract tokens emitted to the controller's stdout. External
monitoring should **grep for the prefix, not rely on log level** — log
levels can drift between versions, but the token prefix is part of the
contract.

Format: `ALERT_<NAME> key1=value1 key2="value 2 with spaces" ...`

### `ALERT_CCP_PERSISTENT`

```
ALERT_CCP_PERSISTENT consecutive_lockouts=3 mode=live suggested_action="log out of IBKR web/mobile to release the session slot"
```

**When fired**: after `_ccp_lockout_streak` reaches 3 or more. Repeats
on every subsequent lockout at or past that threshold until auth
succeeds (which resets the streak).

**What it means**: the controller has hit three consecutive CCP
lockouts despite its own backoff and silent-cool-down recovery. The
overwhelmingly likely cause is **a concurrent IBKR session** (web
portal, mobile app, another TWS instance) holding the auth slot on
the same account. The controller cannot resolve this — operator action
is required.

**What the operator should do**: see the
[CCP lockout scenario in `DISCONNECT_RECOVERY.md`](DISCONNECT_RECOVERY.md#scenario-ccp-lockout-concurrent-ibkr-session).
Short version: log out of IBKR web and mobile, then let the controller's
next auto-retry pick up the freed slot.

**Recommended debounce for external notifications**: 20 min (matches
the internal JVM-restart cooldown cycle).

### `ALERT_JVM_RESTART_EXHAUSTED`

```
ALERT_JVM_RESTART_EXHAUSTED mode=live attempts=5 reason="5 in-JVM relogins exhausted in main CCP pre-loop"
```

**When fired**: exactly once, just before the controller calls
`sys.exit(1)` after all `_JVM_RESTART_MAX_ATTEMPTS` (default 5) silent
cool-down / relaunch cycles have failed.

**What it means**: the controller has fully given up. The Python
process is about to exit. Whether the container then restarts depends
on your Docker restart policy (and in dual-mode containers, one mode
exiting does NOT bring the container down — the other mode's PID keeps
it alive; see [MIGRATION.md](MIGRATION.md#dual-mode-run-sh-wait-semantics)).

**What the operator should do**: verify IBKR account state (web login
to confirm credentials still work, check for account-side restrictions),
then `docker compose restart` the Gateway container.

**Recommended debounce**: 1 hour.

### `ALERT_PASSWORD_EXPIRED`

```
ALERT_PASSWORD_EXPIRED status=warning mode=live days_remaining=7 suggested_action="rotate IBKR password in Account Settings within 7 days to avoid lockout; update TWS_PASSWORD after rotation"
ALERT_PASSWORD_EXPIRED status=warning mode=live suggested_action="rotate IBKR password soon; dialog didn't report remaining days — check IBKR Account Settings for the exact date, then update TWS_PASSWORD after rotation"
ALERT_PASSWORD_EXPIRED status=expired mode=live suggested_action="password has expired; rotate in IBKR Account Settings before login will succeed again, then update TWS_PASSWORD"
```

**When fired**: Gateway/TWS surfaces a password-expiry modal during
`handle_post_login_dialogs`. Three variants:

- `status=warning days_remaining=N` — "will expire in N days" wording,
  login proceeded, operator has time to rotate.
- `status=warning` (no `days_remaining`) — "will expire" wording
  without a day count; unusual, but the controller emits this rather
  than guess a number.
- `status=expired` (no `days_remaining`) — "has expired" wording,
  login is blocked until the password is rotated in IBKR's web portal.

**What it means**: IBKR's password rotation window is open or has
already closed. Gateway shows the dialog on every login once you're
inside the window. The warning variants still let the login proceed;
the expired variant blocks Gateway from completing login until the
password is rotated.

**What the operator should do**: log in to IBKR Account Management,
rotate the password, then update `TWS_PASSWORD` (or the secret file
referenced by `TWS_PASSWORD_FILE`) and restart the container. The
controller cannot drive the change-password dialog itself — that has
to happen in IBKR's web portal.

**Recommended debounce**: 24 hours (fired on every login inside the
rotation window; one alert per day is enough).

### `ALERT_LOGIN_FAILED`

```
ALERT_LOGIN_FAILED mode=live reason="bad-credentials" suggested_action="Gateway surfaced a credential-rejection modal; verify TWS_USERID / TWS_PASSWORD (or _PAPER variants) and update env if password was rotated in IBKR Account Settings"
ALERT_LOGIN_FAILED mode=live reason="bad-credentials" suggested_action="IBKR rejected the credentials after the handshake (NS_AUTH_START present, then timeout); verify TWS_USERID / TWS_PASSWORD (or _PAPER variants) and update env if password was rotated in IBKR Account Settings"
ALERT_LOGIN_FAILED mode=live reason="post-auth-no-progress" suggested_action="server accepted the auth handshake but login never completed; verify TWS_USERID / TWS_PASSWORD (or _PAPER variants) and scan logs for an unrecognized post-auth dialog"
```

**When fired**: two distinct code paths, both emitting the same
grep-contract token with different `reason=` values:

- `reason="bad-credentials"` from `attempt_inplace_relogin` — Gateway
  popped a visible "Login failed" / "Authentication failed" modal
  during re-auth; the controller dismisses it and retries.
- `reason="bad-credentials"` from `_diagnose_login_failure` — terminal
  initial-login path, `launcher.log` shows `NS_AUTH_START` *and* a
  `CCP: Timeout!` (handshake completed, credentials rejected at
  postauth).
- `reason="post-auth-no-progress"` from `_diagnose_login_failure` —
  terminal initial-login path, `NS_AUTH_START` appeared but neither
  success nor an auth timeout followed. Usually also bad credentials,
  but can indicate an unrecognized post-auth dialog we failed to
  dismiss.

**What it means**: IBKR rejected the username/password. The usual
trigger is a password rotation in the IBKR web portal that wasn't
mirrored into the container's env file.

**Why this matters separately from `ALERT_CCP_PERSISTENT`**: with
only the CCP alert, an operator would watch the streak counter climb
and eventually assume an IBKR silent cooldown. But CCP backoff
against bad credentials never recovers — it just waits, retries with
the same bad password, and waits longer. `ALERT_LOGIN_FAILED` fires
*before* the CCP streak escalates, so monitoring can page a human
earlier.

**What the operator should do**: verify the credentials in the
container env (`TWS_USERID` / `TWS_PASSWORD`, or `_PAPER` variants)
against IBKR Account Management. If the password was recently
rotated, update the env (or the secret file referenced by
`TWS_PASSWORD_FILE`) and restart the container. Repeating the
rejected attempt risks IBKR account lockout.

**Recommended debounce**: 15 minutes (first alert should page
immediately; re-auth retries repeat the alert every ~3 minutes, and
the `_diagnose_login_failure` terminal path emits once per process
lifetime before the controller exits).

### `ALERT_SHUTDOWN`

```
ALERT_SHUTDOWN mode=live signal=SIGTERM graceful=true reason="controller received SIGTERM; Gateway JVM exited cleanly within 15s"
ALERT_SHUTDOWN mode=live signal=SIGTERM graceful=false reason="controller received SIGTERM; Gateway JVM did not exit within 15s of SIGTERM and was SIGKILL'd"
ALERT_SHUTDOWN mode=paper signal=SIGINT graceful=true reason="controller received SIGINT; Gateway JVM exited cleanly within 15s"
```

**When fired**: once, from the `signal.SIGTERM` / `signal.SIGINT`
handler, as the final log line before `sys.exit(0)`. Every clean
shutdown emits this, so its *absence* in the last ~N seconds of
container logs (where N is your JVM shutdown timeout) is itself a
signal: it means the controller process died without going through
the signal handler, i.e. an unexpected JVM or interpreter crash.

**Log level**: `INFO`, deliberately. This is a lifecycle event, not an
alert that should wake someone. It sits outside the ERROR-level
`wake-someone-up` grep (see **Grepping logs for ALERT tokens** below)
but is still catchable via the `ALERT_` prefix.

**What `graceful=false` means**: the controller sent `SIGTERM` to the
Gateway JVM, waited 15s for a clean exit, got none, and fell through
to `SIGKILL`. Root causes are usually one of:
1. A Swing EDT deadlock — the JVM's shutdown hook can't drain because
   the UI thread is blocked (rare; usually points at a Gateway-version
   bug worth reporting upstream).
2. A blocked native I/O call in the IBKR networking stack.
3. The JVM is mid-GC / in a stop-the-world pause. A 15s wait should
   normally cover this, so seeing this repeatedly points at resource
   starvation on the host.

**What the operator should do**: `graceful=true` is informational only.
`graceful=false` on a one-off is usually not worth paging on; repeated
occurrences warrant checking host CPU/memory pressure and, if the
host looks fine, capturing a JVM thread dump before the next
`graceful=false` SIGKILL (`kill -3 <jvm-pid>` into `stderr` — watch
`docker logs`).

**Recommended debounce**: none for `graceful=true`. `graceful=false`
should page on the 3rd occurrence in 1h, not the 1st.

### `ALERT_2FA_FAILED`

```
ALERT_2FA_FAILED mode=live reason="agent SETTEXT_IN_WIN on 2FA dialog failed"
ALERT_2FA_FAILED mode=live reason="agent CLICK_IN_WIN OK on 2FA dialog failed"
ALERT_2FA_FAILED mode=live reason="2FA dialog timeout; TWOFA_TIMEOUT_ACTION=exit"
ALERT_2FA_FAILED mode=live reason="2FA dialog timeout and do_restart_in_place failed"
```

**When fired**: on terminal 2FA failure paths in `handle_2fa`:
1. The TOTP code couldn't be typed into the 2FA dialog (agent
   `SETTEXT_IN_WIN` returned false).
2. The OK button couldn't be clicked (agent `CLICK_IN_WIN` returned
   false).
3. 2FA dialog never appeared within `TWOFA_EXIT_INTERVAL` and
   `TWOFA_TIMEOUT_ACTION=exit`.
4. Same timeout but `TWOFA_TIMEOUT_ACTION=restart` and the restart also
   failed.

**What the operator should do**: connect via VNC
(`vnc://<container-host>:5900`) and enter the TOTP manually, or
verify `TWOFACTOR_CODE` in the env is the correct base32 secret from
IBKR's Mobile Authenticator setup QR code.

**Recommended debounce**: 15 min.

## Docker `HEALTHCHECK`

The shipped `Dockerfile` includes:

```dockerfile
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 \
    CMD /home/ibgateway/scripts/healthcheck.sh
```

`scripts/healthcheck.sh` curls `/health` on
`CONTROLLER_HEALTH_SERVER_PORT` (default `8080`). Under `DUAL_MODE=yes`
it also curls the paper-offset port (`8081`), and **either side being
unhealthy marks the container unhealthy**. This is deliberate: in
dual-mode you probably want to know if live is logged in but paper
isn't, rather than have the container appear healthy just because one
side is up.

`--start-period=180s` gives the initial login pipeline (launch JVM,
discover the AT-SPI tree, click through the login dialog, possibly
wait for 2FA) time to finish before failures count against the health
state. Without this, a fresh container would be marked unhealthy for
~2 minutes during normal boot.

To disable the healthcheck, override with `docker run
--health-cmd=none` or set `CONTROLLER_HEALTH_SERVER_PORT=` (empty) in
the image env — the controller then doesn't start the server, the
shim's curl fails, and you'll see unhealthy. So really, to disable,
set `--no-healthcheck` at runtime or patch the Dockerfile.

## Env vars

| Var | Default | Notes |
|---|---|---|
| `CONTROLLER_HEALTH_SERVER_PORT` | `8080` (in the shipped image), unset (source checkout) | TCP port to listen on. In `DUAL_MODE=yes`, paper auto-offsets to `port+1`. Set to empty to disable the health server entirely. |
| `CONTROLLER_HEALTH_SERVER_HOST` | `0.0.0.0` (in the shipped image), `0.0.0.0` (code default) | Bind address. `0.0.0.0` is required for Docker port mapping to work; restrict external exposure with `-p 127.0.0.1:8080:8080` on the host side, not the container-internal bind. |

## Example integrations

### Plain shell (cron)

```bash
#!/bin/sh
# /etc/cron.d/ibg-health — every 2 min, alert if unhealthy
*/2 * * * * root \
  curl -sf http://ibkr:8080/health >/dev/null || \
  logger -t ibg-health "controller unhealthy"
```

### Prometheus (via blackbox_exporter)

```yaml
# prometheus.yml
scrape_configs:
  - job_name: 'ibg-controller'
    metrics_path: /probe
    params:
      module: [http_2xx]
    static_configs:
      - targets:
          - http://ibkr:8080/health   # live
          - http://ibkr:8081/health   # paper (dual-mode only)
    relabel_configs:
      - source_labels: [__address__]
        target_label: __param_target
      - source_labels: [__param_target]
        target_label: instance
      - target_label: __address__
        replacement: blackbox-exporter:9115
```

Alert on `probe_success == 0` for 5m.

### Grepping logs for ALERT tokens

```bash
# Tier 1: wake somebody up (ERROR-level only)
docker logs --since=5m ibkr 2>&1 | grep -E 'ALERT_(CCP_PERSISTENT|JVM_RESTART_EXHAUSTED|2FA_FAILED|PASSWORD_EXPIRED|LOGIN_FAILED)'

# Just the latest occurrence of each (ERROR-level only)
docker logs ibkr 2>&1 | grep -E '^[0-9]+:[0-9]+ \[ERROR\] ALERT_' | tail

# Tier 2: lifecycle dashboards — includes ALERT_SHUTDOWN (INFO-level).
# Useful to distinguish clean operator-driven restarts from JVM crashes.
docker logs --since=1h ibkr 2>&1 | grep -Eo 'ALERT_[A-Z_]+[^"]*"[^"]*"'

# Stuck-JVM detector: ALERT_SHUTDOWN with graceful=false means Gateway
# ignored SIGTERM for 15s and had to be SIGKILL'd. 3+ in the last hour
# is a host-health or Gateway-version problem worth investigating.
docker logs --since=1h ibkr 2>&1 | grep -c 'ALERT_SHUTDOWN .* graceful=false'
```

### JSON field extraction

```bash
curl -sf http://ibkr:8080/health | \
  jq -r 'select(.status == "unhealthy") |
         "mode=\(.mode) state=\(.state) jvm_alive=\(.jvm_alive) api_port_open=\(.api_port_open) ccp_streak=\(.ccp_lockout_streak)"'
```

## Stability contract

The field names and semantics of `/health` JSON and the prefix + key
names of `ALERT_*` tokens are part of the public API as of v0.4.9.
`ALERT_PASSWORD_EXPIRED` was added in v0.5.0, `ALERT_LOGIN_FAILED`
in v0.5.1, and `ALERT_SHUTDOWN` (INFO-level, lifecycle signal) in
v0.5.2 — all under the same stability contract.
Breaking changes will be called out in the CHANGELOG and accompany a
minor version bump. Adding new fields to `/health` or new
`ALERT_*` tokens is not a breaking change.

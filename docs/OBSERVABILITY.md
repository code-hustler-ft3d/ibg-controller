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
  "version": "0.5.9",
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
ALERT_CCP_PERSISTENT consecutive_lockouts=3 mode=live suggested_action="log into IBKR Mobile as this username to force-log-out the held TWS/Gateway slot; IBKR Client Portal (web) does NOT kick the slot"
```

**When fired**: after `_ccp_lockout_streak` reaches 3 or more. Repeats
on every subsequent lockout at or past that threshold until auth
succeeds (which resets the streak).

**What it means**: the controller has hit three consecutive CCP
lockouts despite its own backoff and silent-cool-down recovery. The
cause is either **a concurrent TWS/Gateway session** on the same
account or a **stranded slot** from a prior unclean teardown. The
controller cannot resolve this — operator action is required.

**What the operator should do**: see the
[CCP lockout scenario in `DISCONNECT_RECOVERY.md`](DISCONNECT_RECOVERY.md#scenario-ccp-lockout-concurrent-ibkr-session).
Short version: **log into IBKR Mobile as the affected username** —
per IBKR's docs, mobile login auto-logs-out all TWS/Gateway sessions
and is the reliable kick for both concurrent and stranded slots. The
web Client Portal does NOT kick the slot (read-only concurrent —
production-validated). After the kick, the controller's next
auto-retry picks up the freed slot.

**Recommended debounce for external notifications**: 20 min (matches
the internal JVM-restart cooldown cycle).

### `ALERT_CCP_PERSISTENT_HALT`

```
ALERT_CCP_PERSISTENT_HALT mode=live reason="persistent CCP lockout after in-JVM relogin loop exhausted; CCP_LOCKOUT_MAX_JVM_RESTARTS=0" remediation="log into IBKR Mobile as this username to force-log-out the held TWS/Gateway slot (IBKR Client Portal login does NOT kick the slot — confirmed in production), then restart the container"
```

**When fired**: exactly once, from `_escalate_to_jvm_restart`, just
before the controller calls `sys.exit(1)`. v0.5.9 made halt-by-default
the new behaviour: pre-v0.5.9 the controller would cycle up to 5
SIGKILL-capable teardown attempts with adaptive cool-downs before
emitting `ALERT_JVM_RESTART_EXHAUSTED`. That loop is now opt-in via
`CCP_LOCKOUT_MAX_JVM_RESTARTS` (default `0`); with the default, the
controller emits this alert and exits immediately rather than
participating in the slot-stranding feedback loop.

**What it means**: a persistent CCP lockout that the in-JVM relogin
path couldn't clear. Root cause is almost always one of:
1. A concurrent IBKR session (web portal, mobile, another TWS
   instance) holding the auth slot.
2. A stranded session slot from a prior SIGKILL teardown that IBKR
   hasn't drained yet (the v0.5.5 pattern).

**What the operator should do**: follow the alert's own `remediation=`
field — it embeds the runbook so oncall doesn't need to look it up.
Short version: **log into IBKR Mobile as the affected username** (iOS
or Android). Per IBKR's docs, mobile login auto-logs-out all
TWS/Gateway sessions and is the reliable kick path for both genuine
concurrent sessions and stranded slots. Once the mobile app
authenticates, `docker restart ibkr` (or equivalent) — the controller
comes back and the next auth grabs the freed slot. Do NOT rely on
IBKR's web Client Portal — it is read-only concurrent for TWS auth
slots and does not kick a held slot (production-validated: 8h of
web-only remediation did not clear a stranded slot; mobile login
cleared it immediately and the controller authed in ~30s on next
restart). Server-side drain without operator action takes many hours
and is not a reliable path.

**Log level**: `ERROR`. Paging target. The controller process is exited
after this alert, so downstream monitoring that infers "unhealthy"
from the `/health` endpoint being unreachable will fire in parallel.
The grep-contract alert gives earlier visibility with actionable
context.

**Why this replaces `ALERT_JVM_RESTART_EXHAUSTED` in the default config**:
`ALERT_JVM_RESTART_EXHAUSTED` still fires when `CCP_LOCKOUT_MAX_JVM_RESTARTS`
is set to a positive integer and that cap is reached — the loop
semantics are preserved for operators who opted in. With the default
`=0`, the loop doesn't run, so `ALERT_JVM_RESTART_EXHAUSTED` doesn't
fire either; `ALERT_CCP_PERSISTENT_HALT` is the immediate-halt
equivalent. Both should be wired to paging.

**Recommended debounce**: none. Page on first occurrence — the
controller is already exited.

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

### `ALERT_CLEAN_LOGOUT`

```
ALERT_CLEAN_LOGOUT mode=live pid=12345 status=succeeded reason="JVM exited cleanly within 15s of WINDOW_CLOSING"
ALERT_CLEAN_LOGOUT mode=live pid=12345 status=failed_unreachable reason="agent CLOSE_WIN did not succeed; falling back to SIGTERM"
ALERT_CLEAN_LOGOUT mode=paper pid=12346 status=failed_timeout reason="JVM still alive 15s after WINDOW_CLOSING dispatched; Gateway close handler may be stalled"
```

**When fired**: from `_teardown_jvm_for_restart` (mid-life JVM restart
after a CCP lockout) and from the `SIGTERM`/`SIGINT` signal handler
(controller-lifecycle shutdown), exactly once per teardown attempt.
v0.5.6 drives Gateway to close via a `WindowEvent.WINDOW_CLOSING`
dispatched to the main frame — the same code path a user clicking
the window's X button would take. Gateway's registered WindowListener
performs a proper CCP session-close before the JVM exits, which
releases the IBKR session slot server-side instead of stranding it
(the root cause documented in v0.5.5's
[`ALERT_JVM_UNCLEAN_SHUTDOWN`](#alert_jvm_unclean_shutdown) section).

**Log level**: `INFO`. This is a lifecycle/diagnostic signal, not an
alert that should wake someone. Sits outside the ERROR-level
wake-someone-up grep, but is catchable via the `ALERT_` prefix for
dashboard use — the clean-logout success rate is the key metric.

**Status values** (part of the grep-contract). v0.5.6 introduced
the first three; v0.5.9 added four more to cover pre-MONITORING
shutdown paths that previously emitted a misleading
`failed_unreachable`:

- `succeeded` (v0.5.6) — JVM exited cleanly within
  `CLEAN_LOGOUT_TIMEOUT_SECONDS` of the WINDOW_CLOSING dispatch. No
  SIGTERM was needed, no slot was stranded. Happy path.
- `failed_unreachable` (v0.5.6) — the agent didn't accept `CLOSE_WIN`
  (socket missing, agent never initialised, or the EDT stalled before
  we could post the event). The controller fell through to the
  v0.5.5 SIGTERM → grace → SIGKILL path. Now only emitted in states
  where the main window should exist (MONITORING + post-auth
  pre-monitoring); pre-v0.5.9 also emitted during boot/LOGIN/2FA.
- `failed_timeout` (v0.5.6) — the agent accepted `CLOSE_WIN` but the
  JVM didn't exit within `CLEAN_LOGOUT_TIMEOUT_SECONDS`. Gateway's
  WindowListener is stuck. Controller falls through to SIGTERM and
  if that also times out, `ALERT_JVM_UNCLEAN_SHUTDOWN` fires on top.
- `safe_no_session` (v0.5.9) — SIGTERM received in a pre-auth state
  (`INIT`, `LAUNCHING`, `AGENT_WAIT`, `APP_DISCOVERY`, `LOGIN`), or
  received when no Gateway JVM is running. No CCP slot is held, so
  SIGTERM is safe; the alert is for audit only. This replaces the
  bulk of pre-v0.5.9's spurious `failed_unreachable` emissions.
- `zombie_slot_cannot_release` (v0.5.9) — SIGTERM received in
  `POST_LOGIN`. A CCP slot is in flight but Gateway hasn't rendered
  a main window yet, so there is no UI close path; SIGTERM strands
  the slot server-side until IBKR's timeout drains it. Distinct
  label so operators don't mistake this for a UI-close failure.
  If you see this frequently, correlate with boot duration —
  POST_LOGIN is usually a few-second window.
- `cancelled_pending_2fa` (v0.5.9) — SIGTERM received in `TWO_FA`;
  controller dispatched `CLOSE_WIN` on the 2FA dialog and the JVM
  exited within `CLEAN_LOGOUT_TIMEOUT_SECONDS`, cancelling the
  half-authed handshake before IBKR could fully allocate the slot.
  Not a slot-stranding event.
- `failed_cancel_2fa` (v0.5.9) — SIGTERM received in `TWO_FA`; agent
  rejected `CLOSE_WIN` or JVM didn't exit within the timeout. Falls
  through to SIGTERM. May strand a slot depending on how far the 2FA
  handshake progressed.

**Why this matters**: pre-v0.5.6, the only teardown path was SIGTERM →
grace → SIGKILL, which runs JVM shutdown hooks on a dedicated thread.
When those hooks stall (Swing EDT deadlock, blocked native I/O), IBKR
never receives a session-close and holds the slot server-side until
its own timeout drains — the stranded-self-session pattern from v0.5.5.
v0.5.6 attempts the UI-level close path first so Gateway's own
close handler does the session-close directly, bypassing the shutdown
hooks entirely. If this path works (the common case), stranded slots
stop happening. If it doesn't, the v0.5.5 adaptive cool-down still
absorbs the strand.

**What the operator should do**: nothing for `status=succeeded` — the
metric to watch is the ratio of `succeeded` vs `failed_*` over time.

- If `failed_unreachable` dominates: the agent isn't coming up or is
  crashing mid-session. Check `docker logs` for agent-related errors
  and verify `gateway-input-agent.jar` is present at
  `DESTDIR/gateway-input-agent.jar`.
- If `failed_timeout` dominates: Gateway's WindowListener is stalled
  (deadlocked EDT, blocked native I/O). Bump
  `CLEAN_LOGOUT_TIMEOUT_SECONDS` to 30 for more headroom, and if the
  ratio stays high, capture a JVM thread dump on the next occurrence
  (`kill -3 <pid>` visible in docker logs) to find where the
  WindowListener is hanging.

**Recommended debounce**: none for `succeeded`. `failed_*` should page
on the 3rd in 1h (correlated with `ALERT_CCP_PERSISTENT` — the pattern
"clean logout keeps failing and CCP lockout keeps firing" indicates
real host-level health issues).

### `ALERT_JVM_UNCLEAN_SHUTDOWN`

```
ALERT_JVM_UNCLEAN_SHUTDOWN mode=live pid=12345 reason="Gateway JVM ignored SIGTERM within 30s grace; required SIGKILL" implication="IBKR CCP session slot likely held server-side until timeout; next auth attempt may hit lockout despite cool-down"
ALERT_JVM_UNCLEAN_SHUTDOWN mode=paper pid=12346 reason="teardown raised OSError: [Errno 3] No such process" implication="IBKR CCP session slot likely held server-side until timeout; next auth attempt may hit lockout despite cool-down"
```

**When fired**: from `_teardown_jvm_for_restart`, exactly once per
restart where `SIGTERM` didn't bring the JVM down within the
`JVM_TEARDOWN_GRACE_SECONDS` window (default 30s) or where the
teardown raised an exception. Distinct from `ALERT_SHUTDOWN` which
covers controller-lifecycle exits — this one fires on *mid-life*
JVM restarts (CCP lockout escalation, monitor-loop recovery).

**Log level**: `WARNING`. Indicates a degraded but non-terminal state
— the restart loop continues, but the current teardown likely
stranded an IBKR session slot that will hold until IBKR's own
server-side timeout drains it.

**Why this matters**: the v0.5.5 CHANGELOG documents the empirical
finding that persistent CCP lockouts accumulating across multiple
full escalation cycles (observed at v0.3.2 / v0.4.x) trace back to
stranded session slots from SIGKILL'd JVMs. The v0.5.5 combination
of the extended grace window, this alert, and the adaptive
`CCP_COOLDOWN_MAX_SECONDS` lets operators see when the teardown was
unclean and gives IBKR enough silence to drain the stranded slot
before the next auth attempt.

**What the operator should do**: one-off occurrences are usually
absorbed by the adaptive cool-down (the next attempt will sleep
long enough for the stranded slot to drain). Repeated occurrences
(3+ in 1h) indicate Gateway's shutdown hooks aren't running cleanly
— check host CPU/memory pressure, consider bumping
`JVM_TEARDOWN_GRACE_SECONDS` to 60, and if the ratio stays high,
capture a JVM thread dump (`kill -3 <pid>`) on the next occurrence
to find where shutdown is hanging.

**Recommended debounce**: page on the 3rd occurrence in 1h. Correlate
with subsequent `ALERT_CCP_PERSISTENT` emissions — the expected
pattern is unclean-shutdown → adaptive-cool-down succeeds → no
`ALERT_CCP_PERSISTENT` follow-up. If `ALERT_CCP_PERSISTENT` fires
right after, the adaptive cool-down cap may be too low for your
IBKR tenant's session timeout; raise `CCP_COOLDOWN_MAX_SECONDS`.

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
| `JVM_TEARDOWN_GRACE_SECONDS` | `30` | Seconds to wait for Gateway JVM to exit after `SIGTERM` during a *mid-life* restart before escalating to `SIGKILL`. Bump to 60 if `ALERT_JVM_UNCLEAN_SHUTDOWN` is frequent — Gateway's shutdown hooks may need more time under resource pressure. Distinct from the 15s lifecycle-shutdown window in the SIGTERM handler. Added v0.5.5. |
| `CCP_COOLDOWN_SECONDS` | `1200` | Base duration (seconds) of the silent cool-down applied before a mid-life JVM restart after a CCP lockout. This is the sleep time on the *first* restart attempt; subsequent attempts scale up via `CCP_COOLDOWN_MULTIPLIER`. |
| `CCP_COOLDOWN_MAX_SECONDS` | `3600` | Upper cap on the adaptive cool-down (seconds). Raise if your IBKR tenant's server-side session timeout is longer than 1h and lockouts keep firing after the cap is hit. Added v0.5.5. |
| `CCP_COOLDOWN_MULTIPLIER` | `1.5` | Multiplicative factor applied per restart attempt: attempt-1 = base, attempt-2 = base×1.5, attempt-3 = base×2.25, etc., capped at `CCP_COOLDOWN_MAX_SECONDS`. Set to `1.0` to restore the v0.5.4-and-earlier fixed-duration behaviour. Added v0.5.5. |
| `CLEAN_LOGOUT_TIMEOUT_SECONDS` | `15` | Seconds to wait for the Gateway JVM to exit after dispatching `WindowEvent.WINDOW_CLOSING` (the v0.5.6 clean-logout path). Gateway's WindowListener performs a CCP session-close, which can take a few seconds (network round-trip to IBKR + state flush). If this expires, the controller falls through to the SIGTERM path. Shorten (e.g. `7`) if Docker's `--stop-timeout` is tight; lengthen on slow-network hosts. Added v0.5.6. |
| `CCP_LOCKOUT_MAX_JVM_RESTARTS` | `0` | Number of SIGKILL-capable JVM teardown cycles `_escalate_to_jvm_restart` will attempt before giving up. Default `0` = halt immediately and emit `ALERT_CCP_PERSISTENT_HALT` (v0.5.9's new behaviour; rationale: the retry loop can compound the lockout it's trying to clear by re-stranding slots on each teardown). Set to `5` to restore pre-v0.5.9 auto-retry behaviour. Supersedes the internal `_JVM_RESTART_MAX_ATTEMPTS` constant when set positive. Added v0.5.9. |

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
docker logs --since=5m ibkr 2>&1 | grep -E 'ALERT_(CCP_PERSISTENT|CCP_PERSISTENT_HALT|JVM_RESTART_EXHAUSTED|2FA_FAILED|PASSWORD_EXPIRED|LOGIN_FAILED)'

# Just the latest occurrence of each (ERROR-level only)
docker logs ibkr 2>&1 | grep -E '^[0-9]+:[0-9]+ \[ERROR\] ALERT_' | tail

# Tier 1.5: operational warnings (WARNING-level) — not wake-someone-up on
# a single occurrence, but worth a dashboard. ALERT_JVM_UNCLEAN_SHUTDOWN
# fires when a mid-life JVM restart needed SIGKILL after the
# JVM_TEARDOWN_GRACE_SECONDS window expired, which typically strands
# an IBKR session slot server-side.
docker logs --since=1h ibkr 2>&1 | grep 'ALERT_JVM_UNCLEAN_SHUTDOWN'

# Count unclean shutdowns in the last hour. 3+ in 1h is the
# page-a-human threshold.
docker logs --since=1h ibkr 2>&1 | grep -c 'ALERT_JVM_UNCLEAN_SHUTDOWN'

# Correlate unclean shutdowns with subsequent CCP lockouts — expected
# pattern is unclean-shutdown, adaptive cool-down succeeds, no
# follow-up ALERT_CCP_PERSISTENT. If ALERT_CCP_PERSISTENT keeps
# firing right after, raise CCP_COOLDOWN_MAX_SECONDS.
docker logs --since=1h ibkr 2>&1 | grep -E 'ALERT_(JVM_UNCLEAN_SHUTDOWN|CCP_PERSISTENT)'

# Clean-logout success rate (v0.5.6). This is the key health signal
# for the stranded-session fix: if succeeded/(succeeded+failed_*) is
# close to 1.0, Gateway is closing cleanly and stranded slots are
# prevented at the source.
succeeded=$(docker logs --since=1h ibkr 2>&1 | grep -c 'ALERT_CLEAN_LOGOUT .* status=succeeded')
failed=$(docker logs --since=1h ibkr 2>&1 | grep -cE 'ALERT_CLEAN_LOGOUT .* status=failed_')
echo "clean-logout: succeeded=$succeeded failed=$failed"

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
in v0.5.1, `ALERT_SHUTDOWN` (INFO-level, lifecycle signal) in
v0.5.2, `ALERT_JVM_UNCLEAN_SHUTDOWN` (WARNING-level, mid-life
restart signal) in v0.5.5, `ALERT_CLEAN_LOGOUT` (INFO-level,
teardown diagnostic) in v0.5.6 with three initial `status=` values,
and `ALERT_CCP_PERSISTENT_HALT` (ERROR-level, halt-and-page) plus
four additional `ALERT_CLEAN_LOGOUT` `status=` values
(`safe_no_session`, `zombie_slot_cannot_release`,
`cancelled_pending_2fa`, `failed_cancel_2fa`) in v0.5.9 — all under
the same stability contract. Breaking changes will be called out in
the CHANGELOG and accompany a minor version bump. Adding new fields
to `/health`, new `ALERT_*` tokens, or new `status=` values to
existing tokens is not a breaking change.

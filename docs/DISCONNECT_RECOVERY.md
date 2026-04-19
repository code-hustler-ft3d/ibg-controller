# Disconnect Recovery Playbook

Scenarios the controller can run into where it cannot recover on its
own, and what the operator needs to do. Everything here is about
*controller-side* behavior — if you're running `ibg-controller` inside
a larger stack (connector, database, message bus), your own services
have their own recovery procedures that are out of scope for this
document.

See also:
- [Observability](OBSERVABILITY.md) for how to *detect* that a scenario
  is happening (the `/health` endpoint and `ALERT_*` log tokens).
- [Architecture](ARCHITECTURE.md) for *why* these scenarios exist at all.
- The [Troubleshooting section in the README](../README.md#troubleshooting)
  for common first-login failures (AT-SPI bridge not loaded, "Existing
  session detected" loops, agent PID discovery failure).

---

## Scenario: CCP lockout (concurrent IBKR session)

**TL;DR**: another TWS/Gateway session (or a stranded slot from a
prior unclean teardown) is holding the auth slot for your account.
The controller cannot see or clear it. **Log into IBKR Mobile as the
affected username** — mobile login force-logs-out all TWS/Gateway
sessions and is the reliable kick path. The web Client Portal does
NOT kick the slot.

### Symptoms

- Logs show repeated lockouts, each one followed by an exponential or
  long silent cool-down, and none of them clear:
  ```
  CCP LOCKOUT DETECTED — IBKR's auth server silently dropped the auth request (no NS_AUTH_START before Timeout)
  CCP backoff: waiting 60s before next auth attempt
  ...
  JVM restart attempt 1/5: tearing down JVM before long cool-down
  JVM restart attempt 1/5: cool-down complete, launching fresh JVM
  CCP LOCKOUT DETECTED — IBKR's auth server silently dropped the auth request
  ```
- After the second lockout in a row, the controller flags it:
  ```
  CCP lockout has hit 2 times in a row. Most common cause is a concurrent IBKR session (another TWS/Gateway) or a stranded slot from a prior unclean teardown on the live account. Remediation: log into IBKR Mobile as this username — mobile login auto-logs-out all TWS/Gateway sessions and is the reliable kick path. IBKR Client Portal (web) login does NOT kick the slot. See docs/DISCONNECT_RECOVERY.md — scenario 'CCP lockout (concurrent IBKR session)'.
  ```
- On the third+ lockout, the structured alert token fires:
  ```
  ALERT_CCP_PERSISTENT consecutive_lockouts=3 mode=live suggested_action="log into IBKR Mobile as this username to force-log-out the held TWS/Gateway slot; IBKR Client Portal (web) does NOT kick the slot"
  ```
- `/health` shows:
  ```json
  { "status": "unhealthy",
    "state": "MONITORING",
    "api_port_open": false,
    "ccp_lockout_streak": 3,
    "ccp_backoff_seconds": 1200.0, ... }
  ```
- In dual-mode: typically one mode (usually `live`) is affected while
  the other (`paper`) is fine. They're different IBKR accounts with
  independent auth slots.

### Root cause

**Another TWS-class session is holding the auth slot, or a stranded
slot from a prior unclean teardown hasn't drained yet.** In
descending order of frequency:
1. A separate TWS or Gateway instance running somewhere else (a
   previous container you forgot to shut down, a desktop TWS left on,
   another `ibg-controller` against the same account, an IBC
   deployment).
2. A **stranded self-session** from a prior SIGKILL'd Gateway of ours
   (pre-v0.5.6 pattern, or v0.5.6+ when the clean-logout path
   fallback fires). IBKR holds the slot server-side until the
   operator kicks it or its internal timeout drains (observed >8h).
3. IBKR Mobile app logged in on the same username (mobile holds a
   TWS-class slot).
4. IBKR web portal (`https://www.interactivebrokers.com/`) — usually
   NOT the cause; the web portal is read-only concurrent for TWS auth
   slots, and logging in or out of it does not kick a held slot.

IBKR's CCP (Client Connection Processor) allows only one active session
per account. When a second login is attempted, CCP **silently drops**
the handshake — the Gateway sends its auth request, CCP does not reply
with `NS_AUTH_START`, and after 20 seconds Gateway logs
`AuthTimeoutMonitor-CCP: Timeout!` to `launcher.log`. There's no error
dialog, no bounce message. The controller sees only the silent drop
and interprets it as rate limiting.

Long cool-downs (the v0.4.6 silent cool-down that kills the JVM before
sleeping 20 min) are designed to give IBKR's rate limiter time to
clear. They do NOT help with concurrent sessions — only logging out
elsewhere does.

### Recovery

```bash
# 1. Confirm the pattern. If you see this token, it's concurrent-session
#    with very high probability.
docker logs --tail=200 <container> 2>&1 | grep ALERT_CCP_PERSISTENT

# 2. Or check the streak via /health:
curl -s http://<container>:8080/health | jq '.ccp_lockout_streak'
# streak >= 3 → almost certainly concurrent session
```

Then on your end:

1. **Log into IBKR Mobile as the affected username** (iOS or Android).
   Per IBKR's own docs: *"When you log in to your IBKR Mobile
   application, all other TWS sessions will automatically be logged
   out."* This is the reliable kick path — it works for both genuine
   concurrent sessions and for stranded slots from a prior unclean
   teardown. Complete any mobile-side TOTP; the kick happens as soon
   as the mobile app authenticates.

2. **Stop any other TWS-class session on the same account.** Desktop
   TWS, another ibg-controller container, another IBC deployment.
   Otherwise these will re-grab the slot and you'll ping-pong with
   step 1.

3. **Skip the web Client Portal.** It's read-only concurrent for TWS
   auth slots — logging in or out of it does NOT kick a held slot
   (confirmed in production: 8h of web-portal logout attempts did not
   release a stranded slot).

4. **Wait for the next auto-retry.** Do NOT manually restart the
   container — the controller is already in a retry loop, and
   restarting mid-cool-down can re-arm CCP for a fresh cycle. On
   v0.5.9+ with `CCP_LOCKOUT_MAX_JVM_RESTARTS=0`, the controller will
   eventually emit `ALERT_CCP_PERSISTENT_HALT` and exit instead of
   looping; after you've done step 1 you can safely
   `docker compose restart` in that case (see the halt scenario
   below).

5. **If you're in the middle of a 20-min silent cool-down** and want
   to skip it: `docker compose restart <container>` is allowed but
   only safe if you've completed step 1 first. Otherwise you'll
   restart into the same lockout.

### Verification

```bash
docker logs -f <container> 2>&1 | \
  grep -E "CCP backoff reset|JVM restart succeeded|STATE: READY|heartbeat API port"
```

Expected sequence after you log out elsewhere:
```
CCP backoff reset — auth succeeded
2FA dialog detected: ...
Typing TOTP code into the 2FA dialog
[STATE: READY]
Monitor: JVM pid=<n>, heartbeat API port <4001|4002> every 30s
```

Then `/health` returns `200` with `"status":"healthy"`.

### Prevention

- **Don't log into IBKR Mobile while the Gateway is running.** Mobile
  login auto-logs-out all TWS/Gateway sessions by design — this is
  the #1 controllable cause of an in-band CCP lockout.
- **Don't run concurrent TWS-class sessions on the same account.**
  Desktop TWS, another ibg-controller, another IBC. Any two of these
  against the same credentials hold each other's slots.
- **Web portal is safer than mobile** (read-only concurrent for TWS
  slots) but not zero-risk — if you log in there and IBKR prompts
  any kind of session migration, the Gateway can still lose its
  slot. Treat it as: OK to use occasionally, don't rely on it.
- For longer-term setups: ask IBKR support whether your account
  supports an **API-only sub-user** dedicated to the Gateway. This
  avoids login conflicts with the primary credentials. Not all
  account types support this.

---

## Scenario: 2FA automation failed

**TL;DR**: the controller couldn't get past the Second Factor
Authentication dialog. Either the TOTP couldn't be typed, or no 2FA
dialog ever appeared. Check your `TWOFACTOR_CODE` env var, or connect
via VNC and enter the code manually.

### Symptoms

- `ALERT_2FA_FAILED` token in logs:
  ```
  ALERT_2FA_FAILED mode=live reason="agent SETTEXT_IN_WIN on 2FA dialog failed"
  ```
  or
  ```
  ALERT_2FA_FAILED mode=live reason="2FA dialog timeout; TWOFA_TIMEOUT_ACTION=exit"
  ```
- `/health` shows `state` stuck in `TWO_FA` or the process has exited.

### Root causes

1. **Wrong or missing `TWOFACTOR_CODE`** — the base32 TOTP secret from
   IBKR's Mobile Authenticator setup QR code wasn't set, or was set to
   the 6-digit code instead of the secret.
2. **Dialog shape changed** — Gateway version updated and the agent
   can't find the TOTP input field anymore (rare; the controller's
   dialog catalog is maintained for the tested version).
3. **IB Key push mode enabled** — account uses IB Key push
   notifications rather than TOTP. The controller *does* handle IB Key
   wait mode, but if the operator doesn't tap Approve on their phone
   within `TWOFA_EXIT_INTERVAL` (default 120s), the wait times out.

### Recovery

1. **Verify the TOTP secret**. It's the base32 string from the IBKR
   Mobile Authenticator setup — typically 32 characters, looks like
   `JBSWY3DPEHPK3PXP...`. Not the 6-digit code you type on your
   phone. Set it via `TWOFACTOR_CODE` or the `TWOFACTOR_CODE_FILE`
   Docker secrets pattern.

2. **For IB Key push users**: log into your IBKR mobile app first,
   then restart the container and approve the push notification
   promptly. Increase `TWOFA_EXIT_INTERVAL=300` if 120s is too tight.

3. **Manual VNC recovery**: the shipped image runs x11vnc on port
   5900. Connect with your VNC password (`VNC_SERVER_PASSWORD` env),
   and enter the TOTP manually. The controller's fall-through path
   (`TWOFA_TIMEOUT_ACTION=none`) then waits for the API port to open.

### Verification

```
[STATE: TWO_FA]
2FA dialog detected: ...
Typing TOTP code into the 2FA dialog
Clicking OK in 2FA dialog
2FA handled successfully
[STATE: DISCLAIMERS]
```

---

## Scenario: JVM restart limit exhausted

**TL;DR**: the controller has tried five silent-cool-down-and-relaunch
cycles and none of them worked. It's about to `sys.exit(1)`. Check
account credentials, then restart the container manually.

### Symptoms

- `ALERT_JVM_RESTART_EXHAUSTED` token in logs, emitted exactly once
  before the controller exits:
  ```
  ALERT_JVM_RESTART_EXHAUSTED mode=live attempts=5 reason="5 in-JVM relogins exhausted in main CCP pre-loop"
  ```
- In dual-mode containers, the affected mode's PID dies but the
  container stays up on the other mode's PID — `/health` for the
  dead mode becomes unreachable (connection refused) while the
  surviving mode's `/health` still responds.

### Root cause

The controller has a hard cap (`_JVM_RESTART_MAX_ATTEMPTS`, default
5) on how many times it will kill the JVM, sleep 20 minutes silent,
and relaunch before giving up. Hitting this cap usually means:
1. A **persistent concurrent session** that the operator hasn't
   cleared — see the CCP lockout scenario above.
2. **Credentials are actually wrong** for the current trading mode
   (e.g., live `TWS_USERID` set to a paper user, or password
   changed on IBKR's side).
3. **Account is locked** on IBKR's side (too many bad password
   attempts, compliance hold, etc.).

### Recovery

1. **Verify account by logging into IBKR web portal from a browser.**
   If the web login itself is failing (wrong password, account locked,
   2FA not working), fix that first — the controller cannot recover
   from an account-side issue.

2. **Then log out of the web portal** before restarting the container
   (else you'll immediately hit CCP lockout from the concurrent
   session you just created).

3. `docker compose restart <container>`.

4. Watch for a clean `[STATE: READY]` within 2 minutes.

### In dual-mode: your surviving mode keeps working

The dual-mode `run.sh` waits on both live and paper controller PIDs
(`wait "${pid[@]}"`), so one mode exiting does not bring the container
down. If live is dead but paper is still serving, you can safely keep
running paper while you debug live — just be aware the live socat
forwarder still exists and will reject connections.

---

## Scenario: Gateway JVM crashed mid-session

**TL;DR**: the controller detects a dead JVM in its monitor loop and
attempts an in-place restart. This is usually automatic — no operator
action needed unless it escalates to `ALERT_JVM_RESTART_EXHAUSTED`.

### Symptoms

- Monitor logs show:
  ```
  Monitor: JVM pid=12345 exited with code 0 (re-auth needed)
  ```
  or
  ```
  Monitor: API port 4001 closed without warning
  ```
- `/health` flips to `unhealthy` with `jvm_alive: false`.

### Root cause

IBKR-side session disconnect, Gateway internal crash, or an overnight
session kick. Some IBKR accounts get disconnected after long silence;
the controller recovers by relaunching.

### Recovery

The controller's `_recover_jvm_or_escalate` helper (v0.4.7+) handles
this automatically:
1. Fast in-place restart (`do_restart_in_place`) — no cool-down.
2. If the fast path fails, fall through to
   `_escalate_to_jvm_restart` (v0.4.6 silent cool-down).
3. If that also hits the 5-attempt cap, see the JVM restart exhausted
   scenario above.

Operator action is only needed if the exhausted token fires.

---

## Summary: which scenarios are auto-recoverable?

| Scenario | Auto-recovery | Operator action | Detection |
|---|---|---|---|
| Short API port flap | ✅ next monitor cycle | ❌ not needed | `/health` flips briefly |
| Gateway JVM crash | ✅ in-place restart | ❌ not needed | `jvm_alive: false` in `/health` |
| CCP rate limiter tripped (genuine) | ✅ silent cool-down | ❌ not needed (wait up to 20 min) | `ccp_backoff_seconds > 0` |
| **CCP lockout — concurrent/stranded session** | ❌ cannot auto-recover | ✅ log into IBKR Mobile (force-kicks TWS slot; web Portal does NOT) | `ALERT_CCP_PERSISTENT` + `ccp_lockout_streak >= 3` |
| **2FA automation failed** | ❌ cannot auto-recover | ✅ fix `TWOFACTOR_CODE` or VNC-enter | `ALERT_2FA_FAILED` |
| **JVM restart cap exhausted** | ❌ process exits | ✅ verify account, restart container | `ALERT_JVM_RESTART_EXHAUSTED` |

The three unrecoverable scenarios all emit structured `ALERT_*` log
tokens — wire your monitoring to grep for those prefixes. See
[`OBSERVABILITY.md`](OBSERVABILITY.md) for the full token contract.

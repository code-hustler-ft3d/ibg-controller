# Bootstrap: finding your `TWS_SERVER`

IB accounts are bound to regional IBKR data centers. IB Gateway needs to
connect to the correct one to authenticate. The controller asks you to
provide the hostname via `TWS_SERVER` (or `TWS_SERVER_PAPER`) because
Gateway itself has no built-in way to auto-discover it from a cold start —
Gateway's first-run flow relies on the user picking a region in a GUI
dropdown, which isn't viable for headless Docker.

## The hostnames

| Region | Live / Paper hostname |
|---|---|
| North America (New York)   | `ndc1.ibllc.com` |
| North America (Chicago)    | `cdc1.ibllc.com` |
| Europe                     | `gdc1.ibllc.com` |
| Asia (Hong Kong)           | `hdc1.ibllc.com` |
| Asia (Singapore)           | `hdc1.ibllc.com` |

Your server hostname **does not depend on where you live** — it depends on
where IBKR provisioned your account. Users in New York can have accounts
on `cdc1`; users in Zurich can have accounts on `ndc1`. You also may have
live and paper on different servers (e.g. live on
`ndc1` and paper on `cdc1`).

## Three ways to find out

### 1. Run Gateway interactively via VNC and check its settings

This is the most reliable. Spin up Gateway once with VNC enabled, click
through the first-run wizard, log in successfully, then grab the generated
`jts.ini`.

```bash
docker run --rm --platform linux/arm64 \
    -e USE_PYATSPI2_CONTROLLER=no  \
    -e TWS_USERID=... -e TWS_PASSWORD=... \
    -e TRADING_MODE=live \
    -e VNC_SERVER_PASSWORD=changeme \
    -p 127.0.0.1:5900:5900 \
    -v gateway-jts:/home/ibgateway/Jts \
    ghcr.io/gnzsnz/ib-gateway:latest
```

Connect with a VNC client to `localhost:5900`, log in manually (including
2FA). After login, Gateway has written the server into
`/home/ibgateway/Jts/jts.ini` (or `Jts_live/jts.ini` / `Jts_paper/jts.ini`
for dual mode). Extract it:

```bash
docker run --rm -v gateway-jts:/data alpine:latest cat /data/jts.ini | grep '^Peer='
# Peer=cdc1.ibllc.com:4001
```

The host before `:4001` is your `TWS_SERVER`.

Once you have it, shut down this interactive container and use the
controller with:

```bash
-e TWS_SERVER=cdc1.ibllc.com
```

### 2. Check your IB Account Management portal

Log in to https://www.interactivebrokers.com, go to Account Management →
Settings → User Settings → Connection Information. The server hostname
is listed there (IBKR sometimes calls it "primary connection server").

### 3. Email IB support

Open a ticket: "what is the TWS data center hostname for my account?"
They will tell you. Same-day response usually.

## What happens if you guess wrong

- **If you set `TWS_SERVER` to a server that isn't yours**: Gateway
  connects, IBKR's auth server rejects the login with "this user is not
  known here" or similar. The controller logs the error and exits.
- **If you leave `TWS_SERVER` unset**: Gateway uses its built-in default
  (`ndc1.ibllc.com`). This works for many US accounts. If your account
  is elsewhere, Gateway fails the TLS handshake on the misc URLs port
  (`ndc1.ibllc.com:4000`) with `SSLHandshakeException — Remote host
  terminated the handshake`. The controller logs this and times out.
- **If you set `TWS_SERVER` to an unreachable hostname**: TCP connect
  fails, Gateway retries, eventually times out.

All failure modes are visible in Gateway's own `launcher.log`, which
lives at `/home/ibgateway/Jts/launcher.log` inside the running container.

## If your first cold-start gets locked out by IBKR's rate limiter

We've observed IBKR's auth server sometimes refuse fresh password-based
logins for several minutes (or longer) after a burst of failed attempts
from the same account. Two visible patterns:

1. **CCP silent timeout** — Gateway logs a 20-second silent
   `AuthTimeoutMonitor-CCP: Timeout!` in `launcher.log` with no dialog
   on-screen. Gateway 10.45.1c swallows the corresponding IBKR error
   message; on 10.44.1g the same state sometimes surfaces a dialog
   instead.
2. **Stuck-connecting retry loop** — Gateway's login dialog stays up
   showing `Attempt N: connecting to server (trying for another XX
   seconds)`. The auth protocol never starts, so no `Timeout!` line is
   logged — the only signal is the dialog text.

**As of v0.4.0 the controller handles both modes automatically** by
applying an exponential backoff (60s → 120s → 240s → 480s → 600s cap)
between retries *and* recovering via in-JVM relogin on the existing
Gateway process — the same pattern IBC uses. Previous versions
(v0.2.2–v0.3.2) killed and relaunched the JVM on each retry, which
IBKR's auth server saw as a fresh handshake and kept the lockout
armed; the ramp never cleared. Look for these log lines:

```
CCP LOCKOUT DETECTED ...
CCP backoff: waiting 60s before next auth attempt
```

or

```
Login dialog stuck in 'connecting to server' retry loop ...
CCP backoff: waiting 120s before next auth attempt
```

The right behavior on your end is **let it run** — the controller will
back off increasingly between attempts until IBKR allows the session
through. Total recovery often takes 5–60 minutes.

If after a full hour of the backoff cycle paper or live still hasn't
authenticated, check:

1. You're sending the right username for the trading mode (live mode
   wants your live userid, paper mode wants `TWS_USERID_PAPER` — the
   controller handles this swap automatically, but double-check your
   env)
2. Your `TWS_SERVER` / `TWS_SERVER_PAPER` matches what your account
   actually uses (see "The hostnames" above)

If a clean retry from a known-good config still fails, the issue is
almost certainly account-side (wrong server, wrong userid, account
locked), not the controller. Gateway's `launcher.log` will confirm
the CCP-Timeout case — you'll see the `Authenticating` → `Timeout!`
pattern with nothing in between. The stuck-connecting case won't show
in `launcher.log`; check the controller's own logs for the "stuck in
'connecting to server'" warning instead.

## Persisting the right state

Once Gateway has authenticated once against the right server, it writes
a `SupportsSSL` cache entry into `jts.ini` so subsequent startups skip
the TLS negotiation entirely. **If you mount `/home/ibgateway/Jts` as a
Docker volume**, this cache survives restarts and the controller doesn't
need to re-bootstrap.

```yaml
services:
  ib-gateway:
    image: your-ib-gateway-image
    environment:
      TWS_USERID: ...
      TWS_PASSWORD: ...
      TRADING_MODE: live
      TWS_SERVER: ndc1.ibllc.com    # one-time bootstrap hint
    volumes:
      - gateway-jts:/home/ibgateway/Jts

volumes:
  gateway-jts:
```

With the volume in place, **`TWS_SERVER` is only strictly required on
the very first run** — after that, the controller sees an existing
`jts.ini` and leaves it alone, and Gateway reuses the cached state.

## Pre-seeding `SupportsSSL` manually

If you can't run Gateway interactively and don't want to guess, the
controller automatically writes a `SupportsSSL` cache entry in the
`jts.ini` it creates when `TWS_SERVER` is set. The entry uses today's
date. This has been enough to get Gateway past the SSL handshake in our
real-credential testing — no other pre-seeding required.

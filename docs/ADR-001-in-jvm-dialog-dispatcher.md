# ADR-001: In-JVM AWTEventListener Dialog Dispatcher

**Status:** Partially superseded by v0.5.12 (see "v0.5.12 update"
below). The remaining out-of-scope dialogs (post-login config, 2FA
late-arrival ping-pong) are still candidates for the dispatcher
pattern if they show up as production failures, but the cold-start
deadlock that originally motivated this ADR has been resolved by a
simpler approach: disabling the AT-SPI bridge in the JVM entirely.
**Date:** 2026-04-16 (original) / 2026-04-27 (v0.5.12 update)
**Context at time of writing:** v0.4.1 shipped earlier today with in-JVM
relogin (`attempt_inplace_relogin`) and dual-loop CCP-lockout /
stuck-connecting detection. This ADR captures the next architectural step
so it can be executed without re-researching.

## v0.5.12 update (2026-04-27)

The cold-start "stuck at Connecting…" / `CCP LOCKOUT DETECTED`
failure that motivated the LoginDialogHandler priority of this ADR
turned out to be an **intra-JVM `AtkWrapper` deadlock**, not a
pyatspi-tree-walk timing issue. SIGQUIT thread dumps showed
`JTS-Login-*` parked in `AtkUtil.invokeInSwing.get()` while
`AWT-EventQueue-1` was itself stuck at `AtkWrapper$6.dispatchEvent`
holding the same monitor reentrantly. java-atk-wrapper's
java↔native bridge is not safe for re-entrant Swing event dispatch.

v0.5.12 fixes this by **disabling the AT-SPI bridge** via
`-Djavax.accessibility.assistive_technologies=` (empty value). The
`AtkWrapper` class is never instantiated; the deadlock is
structurally impossible. As a coupled change, `handle_login()` and
`find_app()` were rewritten to use the existing
`gateway-input-agent` socket exclusively (it's pure Swing/AWT —
unaffected by the bridge disable). The pyatspi tree-walk path is no
longer used in the cold-start hot path.

This means the ADR's headline win — eliminating Python's pyatspi
dependency for cold-start dialogs — has effectively happened
already, but via a simpler route than the proposed
AWTEventListener+DialogHandler infrastructure. The remaining trigger
conditions (post-login config dialog, 2FA late-arrival, dialog
internals breaking on Gateway 10.46+) are still valid; the
dispatcher pattern is the right answer if any of those become
production blockers. Until then, the agent-socket route used in
v0.5.12 covers the hot path.

## Decision

When the trigger conditions below are met, extend
`agent/GatewayInputAgent.java` with an `AWTEventListener` that watches for
window open/activate events inside the Gateway JVM and dispatches to a
small set of `DialogHandler` classes. This eliminates the need for the
Python controller to poll AT-SPI (over D-Bus, over Xvfb) for the hot-path
dialogs.

## Why this is the right long-term direction

The architectural friction we keep hitting is structural: an external
process (the Python controller running `pyatspi2`) is trying to observe
and drive an internal Swing UI. The glue (D-Bus + Xvfb + matchbox-wm +
at-spi-bus-launcher + at-spi2-registryd) has timing and ordering quirks
that surface as flaky logins and stuck-connecting loops. Two iterations
(ib-gateway-docker-totp, then ibg-controller) have hit the same bug
class.

The `GatewayInputAgent.java` already runs inside the JVM as a
`-javaagent:`. It already has `SwingUtilities.invokeAndWait` /
`invokeLater` primitives for click/settext/gettext. What's missing is
event-driven window detection — exactly what
`AWTEventListener(WINDOW_EVENT_MASK)` provides.

IBC takes the same approach (`LoginManager`,
`secondFactorAuthenticationDialogClosed`, etc. in
`src/ibcalpha/ibc/`). The existence-proof is there; we're not inventing
a pattern, we're porting one.

## Trigger conditions (execute when any fires)

1. v0.4.1 shows a specific dialog-handling failure in production that
   isn't a CCP-lockout or stuck-connecting issue (those are the v0.4.1
   fix targets). Example: login clicks landing on wrong element,
   second-factor field type latency, existing-session ping-pong that
   the pyatspi tree walk misses.
2. A Gateway version bump (IB Gateway 10.46+) changes dialog internals
   in a way that breaks accessible-name-based selectors.
3. The user explicitly decides to invest the time regardless, having
   confirmed v0.4.1 is working and the bottleneck is architectural.

Do NOT execute this ADR pre-emptively. The whole point of the deferral
is to avoid churn on top of an unverified baseline.

## Scope — minimum viable dispatcher

In-JVM handlers to implement (priority order):

1. **LoginDialogHandler** — types `TWS_USERID` / `TWS_PASSWORD`, selects
   trading mode (live vs paper), clicks "Log In" or "Paper Log In". This
   is the cold-start hot path and the one we have the most pyatspi
   timing bugs on.
2. **SecondFactorDialogHandler** — computes TOTP from `TWOFACTOR_CODE`
   (base32 secret, RFC 6238, ~50 LOC of Java using `javax.crypto.Mac`
   HmacSHA1), types into the unnamed text field, clicks OK. Also
   detects IB Key push mode (code field absent → wait for dialog to
   dismiss itself after mobile approval).
3. **ExistingSessionDialogHandler** — clicks based on
   `EXISTING_SESSION_DETECTED_ACTION` env (primary/secondary/manual),
   with ping-pong mitigation ported from `gateway_controller.py`
   lines ~1219–1303.
4. **AutoRestartDialogHandler** — clicks OK on Gateway's daily-restart
   and "Auto Log Off" notifications.

NOT in scope for initial execution:
- Gateway configuration dialog automation (API settings, master client
  ID). That's cold-start-once; pyatspi handles it fine.
- Post-login notification dismissals (NSE compliance, bid/ask size
  update). Low frequency, pyatspi handles.

## Design — AWTEventListener skeleton

```java
// In premain(), after starting the socket server:
Toolkit.getDefaultToolkit().addAWTEventListener(evt -> {
    if (!(evt instanceof WindowEvent we)) return;
    if (we.getID() != WindowEvent.WINDOW_ACTIVATED
            && we.getID() != WindowEvent.WINDOW_OPENED) return;
    Window w = we.getWindow();
    // Defer actual handling to a worker thread — don't block the EDT
    // or we deadlock modal dialogs.
    DISPATCH_EXEC.submit(() -> dispatchDialog(w));
}, AWTEvent.WINDOW_EVENT_MASK);

private static void dispatchDialog(Window w) {
    for (DialogHandler h : HANDLERS) {
        if (h.matches(w)) {
            h.handle(w);  // uses SwingUtilities.invokeLater for clicks
            return;
        }
    }
}
```

`DialogHandler.matches(Window)` inspects title + a few marker strings
in the component tree. `handle(Window)` uses the existing primitives
(`findByName`, `doClickInWindow`, etc.) refactored into static
helpers — the socket protocol methods can then delegate to the same
helpers for backwards compatibility.

## Secrets — where they come from

Read from JVM env at `premain()` time:
- `TWS_USERID`, `TWS_PASSWORD` — set by `docker-compose.yml`
- `TWOFACTOR_CODE` (base32 TOTP secret)
- `TRADING_MODE` (live|paper)
- `EXISTING_SESSION_DETECTED_ACTION` (primary|secondary|manual)

All of these already land in the JVM process env via the existing
gnzsnz-style Docker setup. Nothing new to wire.

## Feature flag

Env var `IBG_INJVM_DISPATCHER=1`. When set, the AWTEventListener
registers and handles matching dialogs. When unset (default), only the
socket-protocol path runs and the Python controller's pyatspi flow is
unchanged. This lets us roll the dispatcher out per-environment (paper
first, then live) and revert instantly by unsetting the flag.

## Rollout path (when triggered)

1. Land dispatcher + LoginDialogHandler + SecondFactorDialogHandler,
   flag default OFF. Version bump to 0.5.0-preview.
2. Enable flag on the paper-side container only (via env var in
   docker-compose.yml). Observe for 72 hours.
3. Port ExistingSessionDialogHandler + AutoRestartDialogHandler.
4. Enable flag in live-side.
5. Once both sides stable for 1 week, delete the superseded
   `handle_login` / `handle_2fa` code paths from
   `gateway_controller.py` and drop the flag.

## What we learn by deferring

Running v0.4.1 as-is tells us which pyatspi paths actually fail in
production vs which we preemptively feared. The handler list above
may shrink (maybe only Login needs porting) or shift priority.

## References

- IBC LoginManager source:
  https://raw.githubusercontent.com/IbcAlpha/IBC/master/src/ibcalpha/ibc/LoginManager.java
- v0.4.1 in-JVM relogin memory:
  `memory/auth_recovery_inplace_relogin.md`
- Existing agent primitives: `agent/GatewayInputAgent.java`
- Current Python login/2FA flow: `gateway_controller.py`
  `handle_login` line 1098, `handle_2fa` line 1306

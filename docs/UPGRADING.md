# Upgrading

Short reference for moving an existing `ibg-controller` deployment
from one version to the next. The [CHANGELOG](../CHANGELOG.md) is the
authoritative list of what changed; this file is the operator-facing
how-to.

## Version scheme

`ibg-controller` is pre-1.0 and follows [SemVer](https://semver.org/)
with the pre-1.0 caveat: **minor bumps in the `0.x` series are
allowed to contain breaking changes**. Every release that does
contain one calls it out in the **Removed** or **Changed** sections
of the CHANGELOG, and in this file under the corresponding version.

What's covered by the stability contract regardless of version:

- Names and key structure of `ALERT_*` log tokens
  ([`OBSERVABILITY.md`](OBSERVABILITY.md)).
- Field names and semantics of the `/health` JSON shape.
- Env var names listed in the README's env table.

Changes to any of those within `0.x` will still be called out — the
contract is "we won't break it *silently*", not "we won't break it".

## The upgrade workflow

Same workflow regardless of how you deployed:

### Docker image (built locally from source)

```bash
cd ibg-controller
git fetch --tags
git checkout vX.Y.Z                    # the tag you want
make                                    # rebuild the agent jar + stage controller
docker build -t ibg-controller:vX.Y.Z .
docker rm -f ibkr && docker run -d \
  --name ibkr \
  --env-file /path/to/.env \
  -p 127.0.0.1:4001:4001 \
  -p 127.0.0.1:8080:8080 \
  ibg-controller:vX.Y.Z
```

### Release tarball (prebuilt)

```bash
VER=X.Y.Z
curl -sSLO https://github.com/code-hustler-ft3d/ibg-controller/releases/download/v${VER}/ibg-controller-${VER}.tar.gz
tar -xzf ibg-controller-${VER}.tar.gz
cd ibg-controller-${VER}
DESTDIR=/home/ibgateway ./install.sh
# Restart the Gateway container so the new controller + agent are picked up
docker restart ibkr
```

### Rollback

`ibg-controller` keeps no on-disk state besides the readiness file
`/tmp/gateway_ready*`, so rollback is just "redeploy the previous
version":

```bash
git checkout v<previous>
make && docker build -t ibg-controller:v<previous> . && docker rm -f ibkr && docker run ...
```

Your `.env` does not need to change on rollback. New env vars
introduced in the release you're rolling back *from* get ignored
when they're absent; env vars you already set stay honored.

## Per-version notes

Only versions that need operator attention are listed. If a version
isn't listed, it contained only additive changes that don't require
anything from you.

### v0.5.12

**No breaking changes.** Image-internal fix for the most common
cause of `CCP LOCKOUT DETECTED` warnings — they were almost always
an intra-JVM `AtkWrapper` deadlock, not an IBKR-side rate-limit
kick. The 20-second `AuthTimeoutMonitor-CCP` timer was firing
locally because `JTS-Login-*` was parked in `AtkUtil.invokeInSwing`
waiting on a `FutureTask` that the AWT EventQueue couldn't run
(itself stuck at `AtkWrapper$6.dispatchEvent`). IBKR was never
reached. See CHANGELOG.md v0.5.12 for the SIGQUIT thread-dump
evidence.

v0.5.12 disables the AT-SPI bridge inside the JVM by passing
`-Djavax.accessibility.assistive_technologies=` (empty value).
`AtkWrapper` is never instantiated. The deadlock is structurally
impossible. Login UI work that previously used pyatspi tree-walking
now goes through the in-JVM `gateway-input-agent` socket — which is
pure Swing/AWT and unaffected by the disable.

**No operator action required beyond the redeploy.**

- No compose changes.
- No env var changes.
- No Dockerfile changes (the JRE's `accessibility.properties` file
  is left as-is; the runtime JVM flag overrides it).

What you'll notice in the logs:

- The `APP_DISCOVERY` line changed from
  `Waiting for IBKR Gateway in the AT-SPI desktop tree` to
  `Resolving Gateway app handle (agent-reported PID)`.
- Cold-start time is faster — no 2-second AT-SPI-tree poll cycles
  before login starts.
- `CCP LOCKOUT DETECTED` should be much rarer. If you still see
  it, see [DISCONNECT_RECOVERY.md → CCP lockout
  triage](DISCONNECT_RECOVERY.md#scenario-ccp-lockout-concurrent-ibkr-session)
  to distinguish a real broker-side lockout from the
  (now-impossible) deadlock signature.
- A new diagnostic file:
  `/tmp/jvm_console_${TRADING_MODE}.log` captures Gateway's JVM
  stdout/stderr. SIGQUIT (`kill -3 <jvm_pid>`) thread dumps land
  here. Pre-v0.5.12 the JVM streams were redirected to `/dev/null`.

### v0.5.10

**No breaking changes.** Additive bugfix for the IBKR daily-maintenance
CCP cascade. 2026-04-20/21 production incident: at 23:45:12 ET both
live and paper Gateway JVMs exited cleanly (code 0) when IBKR's
server-side maintenance window (~23:45-00:15 ET) forcibly shut down
every session. The existing recovery path re-auth'd ~8 seconds later
into a still-draining IBKR auth server, every re-auth was silently
dropped, and a CCP LOCKOUT cascade fired `ALERT_CCP_PERSISTENT_HALT`
on both modes before paper recovered ~35 min later (live was still
halted 60 min later when the operator intervened).

v0.5.10 adds a wallclock-driven maintenance-window guard:

- When the JVM exits with code 0 AND the current time is inside
  23:30-00:30 `America/New_York` (widened slightly around IBKR's
  published window), sleep `CCP_MAINTENANCE_RECOVERY_DELAY_SECONDS`
  (default 480 = 8 min) before touching IBKR's auth server.
- Same guard on cold start — a container booting inside the window
  delays before clicking Log In. `on-failure` restart policies
  otherwise drive a fresh container straight into the same landmine.
- Non-zero exits (crashes, SIGTERM, SIGKILL) bypass the guard —
  they're not maintenance shutdowns and still get the fast-restart
  path.

No operator action required beyond the redeploy. No config change.
The TZ is hardcoded to `America/New_York` inside the guard and is
*not* read from the container's `TIME_ZONE` env — IBKR's window is
ET-anchored regardless of where the container thinks it lives, so
setting `TIME_ZONE=Europe/London` won't shift the guard off of ET.

New env var (optional; default is 8 min):

| Var | Default | When to tune |
|---|---|---|
| `CCP_MAINTENANCE_RECOVERY_DELAY_SECONDS` | `480` (8 min) | Tune upward if empirical data shows IBKR's server-side drain takes longer in your region. Tune downward only if you have strong evidence the drain finishes faster — otherwise you're giving up the mitigation. |

New grep-contract token (INFO-level, emitted once per recovery-path
entry when the guard fires):

```
ALERT_IBKR_MAINTENANCE_RECOVERY delay_seconds=480 mode=live reason="JVM exited with code 0"
```

What to watch after upgrading:

- If your containers previously fired `ALERT_CCP_PERSISTENT_HALT` in
  the 23:45-00:15 ET band, you should now see
  `ALERT_IBKR_MAINTENANCE_RECOVERY` in its place and a clean re-auth
  ~8 min later. The halt should still fire for genuine CCP lockouts
  outside the window (IBKR Mobile-kick remediation unchanged).
- `ALERT_IBKR_MAINTENANCE_RECOVERY` is expected and benign — the
  delay itself is the mitigation, not an error. Don't page on it.
  Wire it to a low-priority INFO channel if you want visibility into
  how often the guard fires.
- Dual-mode deployments (`TRADING_MODE=both`) emit the token
  independently per mode — expect live and paper each to fire once
  per maintenance cycle.

### v0.5.9

**BEHAVIOR CHANGE — read this before upgrading.** The automatic
CCP-lockout-triggered JVM restart loop is now **opt-in**. Pre-v0.5.9,
when the controller saw a persistent CCP lockout it fell into
`_escalate_to_jvm_restart` and cycled up to 5 SIGTERM-grace-SIGKILL
teardown attempts with adaptive cool-downs between them. v0.5.9 emits
the new `ALERT_CCP_PERSISTENT_HALT` grep token and `sys.exit(1)`
immediately by default. The restart loop is still available — you opt
in by setting `CCP_LOCKOUT_MAX_JVM_RESTARTS` to a positive integer.

Why the flip: a 2026-04-19 production incident traced 24h of stuck
state across live + paper accounts to the escalation loop itself.
Each teardown's SIGKILL fallback re-stranded the IBKR session slot
and extended IBKR's server-side zombie timer, so the 5 retries
compounded the lockout they were trying to clear instead of
resolving it. v0.5.6's clean UI logout (`WINDOW_CLOSING`) reduced how
often a teardown ends in SIGKILL but doesn't help in the post-CCP
disposed-shell state where Gateway's main window isn't findable —
exactly the state `_escalate_to_jvm_restart` runs in. The safer
default is "stop and page a human"; the loop is still there for
operators who decided the recovery vs. lockout-compounding tradeoff
the other way.

Also fixed in v0.5.9: SIGTERMs received before `MONITORING` state
(e.g., during boot, during LOGIN, while the 2FA dialog is up) no
longer emit a misleading `status=failed_unreachable` `ALERT_CLEAN_LOGOUT`.
Pre-auth states now emit `status=safe_no_session`, `POST_LOGIN` emits
`status=zombie_slot_cannot_release`, and `TWO_FA` tries to cancel
the in-flight 2FA dialog and emits `status=cancelled_pending_2fa` or
`status=failed_cancel_2fa`.

**If your deployment depended on the old auto-recovery behaviour**,
set `CCP_LOCKOUT_MAX_JVM_RESTARTS=5` to keep pre-v0.5.9 semantics.
For most operators, the recommended default is to **leave it at 0
and wire `ALERT_CCP_PERSISTENT_HALT` to paging** — the historical
"recovery" path was adding to the problem more often than it was
resolving it.

New env var (opt-in; default preserves halt-by-default behaviour):

| Var | Default | When to tune |
|---|---|---|
| `CCP_LOCKOUT_MAX_JVM_RESTARTS` | `0` | Set to `5` to restore the pre-v0.5.9 auto-restart loop (5 SIGKILL-capable teardowns with adaptive cool-downs). Supersedes `JVM_RESTART_MAX_ATTEMPTS`. Leave at `0` unless you have automation downstream that expects auto-recovery and no operator paging. |

New grep-contract statuses on `ALERT_CLEAN_LOGOUT`:
`safe_no_session`, `zombie_slot_cannot_release`,
`cancelled_pending_2fa`, `failed_cancel_2fa`. See
[`OBSERVABILITY.md`](OBSERVABILITY.md#alert_clean_logout) for the
full 7-status table.

What to watch after upgrading:

- `ALERT_CCP_PERSISTENT_HALT` (ERROR): new paging target. If this
  fires, the runbook is in the alert's `remediation=` field —
  **log into IBKR Mobile as this username** (iOS or Android). Per
  IBKR's docs, mobile login auto-logs-out all TWS/Gateway sessions
  and is the reliable kick for both concurrent and stranded slots.
  Then restart the container. IBKR's web Client Portal does NOT
  kick the slot (read-only concurrent; production-validated), so
  don't rely on it. Until the operator runs through this, the
  controller process stays exited.
- `ALERT_CLEAN_LOGOUT status=failed_unreachable` rate should drop
  sharply. Pre-v0.5.9, every boot-time SIGTERM (e.g., `docker stop`
  during the first 30s) emitted this — noisy and misleading. v0.5.9
  reclassifies those as `safe_no_session`.
- If you set `CCP_LOCKOUT_MAX_JVM_RESTARTS` to a positive value, the
  existing v0.5.5 alerts (`ALERT_JVM_UNCLEAN_SHUTDOWN`,
  `ALERT_JVM_RESTART_EXHAUSTED`) still apply. The loop behaviour is
  unchanged beyond the cap now coming from the new env var instead
  of the internal `_JVM_RESTART_MAX_ATTEMPTS` constant.

### v0.5.6

**No breaking changes.** Attacks the root cause of the stranded-session
CCP lockout that v0.5.5 contained but did not eliminate. Where v0.5.5
made the JVM teardown *safer* (longer grace, adaptive cool-down,
visibility), v0.5.6 makes it *correct*: the controller now dispatches a
UI-level window-close to Gateway's main window before any SIGTERM,
firing Gateway's own `WindowListener` — the same code path as a user
clicking the X button — which performs an ordered CCP session-close
server-side. If the clean close succeeds within
`CLEAN_LOGOUT_TIMEOUT_SECONDS` (default 15s), SIGTERM is skipped
entirely and no stranded slot is produced. If it fails (agent
unreachable, or JVM doesn't exit in time), the controller falls
through to v0.5.5's SIGTERM + grace → SIGKILL → adaptive cool-down
defenses.

Upgrade is strictly additive: best case, stranded slots stop
happening; worst case, you're back to v0.5.5 behaviour.

What to watch after upgrading:

- New `ALERT_CLEAN_LOGOUT` (INFO) emits on every teardown and
  lifecycle shutdown. `status=succeeded` is the happy path and the
  dominant case. `status=failed_unreachable` points at the agent /
  Gateway UI not being ready (rare after auth succeeds).
  `status=failed_timeout` means the UI close was delivered but
  Gateway's close handler didn't exit the JVM in time — if you see
  this alongside subsequent `ALERT_CCP_PERSISTENT`, raise
  `CLEAN_LOGOUT_TIMEOUT_SECONDS` before touching anything else. See
  [`OBSERVABILITY.md`](OBSERVABILITY.md#alert_clean_logout).

New env vars (all optional, all defaulted):

| Var | Default | When to tune |
|---|---|---|
| `CLEAN_LOGOUT_TIMEOUT_SECONDS` | `15` | Bump to 30 if `ALERT_CLEAN_LOGOUT status=failed_timeout` is frequent and correlates with follow-up `ALERT_CCP_PERSISTENT`. Lower only if you want to intentionally fall through to SIGTERM faster. |

v0.5.5's env vars (`JVM_TEARDOWN_GRACE_SECONDS`, `CCP_COOLDOWN_*`)
all still apply — they now govern the fallback path when the clean
logout fails, rather than every teardown.

### v0.5.5

**No breaking changes.** Defensive fix for a CCP-lockout failure mode
diagnosed on 2026-04-18: when a mid-life JVM restart had to SIGKILL
an unresponsive Gateway, IBKR's server sometimes held the session
slot past the then-fixed 1200s cool-down, so the *next* auth attempt
from the same controller hit silent-drop lockout even though no
concurrent web or mobile session existed. Symptoms looked identical
to the "log out of IBKR elsewhere" scenario from v0.4.7, but the
remediation was different — the stranded slot was ours, held
server-side until IBKR's timeout drained it.

What v0.5.5 changes:

- **Adaptive long cool-down.** After a CCP-triggered JVM restart, the
  controller now scales its silent wait by attempt index: 1200s →
  1800s → 2700s → 3600s (capped) → 3600s for the default
  `CCP_COOLDOWN_MULTIPLIER=1.5`. This gives IBKR escalating quiet
  time to drain any stranded slot before we next auth. Operators who
  want the old fixed-duration behaviour can set
  `CCP_COOLDOWN_MULTIPLIER=1.0`.
- **Extended SIGTERM grace for mid-life restarts.** Teardown now
  waits 30s (was hardcoded 20s) before escalating to SIGKILL,
  reducing the rate at which teardowns strand a slot in the first
  place. Tunable via `JVM_TEARDOWN_GRACE_SECONDS`. Distinct from the
  15s lifecycle-shutdown window in the SIGTERM handler, which is
  unchanged.
- **New `ALERT_JVM_UNCLEAN_SHUTDOWN` log token (WARNING)** fires on
  every SIGKILL-escalated teardown. Use it to correlate with
  subsequent `ALERT_CCP_PERSISTENT` emissions — if the pattern is
  "unclean shutdown → CCP lockout that *doesn't* clear after the
  adaptive cool-down", raise `CCP_COOLDOWN_MAX_SECONDS` above 3600.
  See
  [`OBSERVABILITY.md`](OBSERVABILITY.md#alert_jvm_unclean_shutdown).

New env vars (all optional, all defaulted):

| Var | Default | When to tune |
|---|---|---|
| `JVM_TEARDOWN_GRACE_SECONDS` | `30` | Bump to 60 if `ALERT_JVM_UNCLEAN_SHUTDOWN` is frequent — host is likely under CPU/memory pressure. |
| `CCP_COOLDOWN_SECONDS` | `1200` | Base cool-down duration. Unchanged from v0.5.4's internal default; just now tunable. |
| `CCP_COOLDOWN_MAX_SECONDS` | `3600` | Raise above 3600 only if lockouts keep firing after the cap is already being hit. |
| `CCP_COOLDOWN_MULTIPLIER` | `1.5` | Set to `1.0` to restore v0.5.4's fixed-duration behaviour. |

**If you're running pre-v0.5.5 and seeing the symptom** (persistent
CCP lockout on a mode you know has no concurrent session), the
manual remediation is: `docker stop` the container, **log into
IBKR Mobile as the affected username** (mobile login auto-logs-out
all TWS/Gateway sessions — the reliable kick for stranded slots),
then `docker start`. IBKR's web Client Portal is NOT effective here
(read-only concurrent for TWS slots; production-validated not to
kick). Once you're on v0.5.5+ the adaptive cool-down plus v0.5.6's
clean-logout path prevents most strandings before they happen.

### v0.5.4

**No breaking changes.** Polish + release-pipeline fix:

- `release-image.yml` now actually fires on tag push (v0.5.3's
  trigger was suppressed by GitHub's recursion guard). v0.5.4 is the
  first tag whose image, SBOM, and cosign attestation publish
  automatically end-to-end. No operator action required — just
  `docker pull ghcr.io/code-hustler-ft3d/ibg-controller:v0.5.4`.
- If you deployed v0.5.3 by building locally, you can keep that
  deployment; there is no functional difference in the controller
  between 0.5.3 and 0.5.4. Upgrade whenever convenient.
- If you want v0.5.3's image retroactively (e.g. to pin a known
  deployment by digest), a maintainer can invoke `gh workflow run
  "Release image" -f tag=v0.5.3` from the Actions tab to backfill
  it. File an issue if you need this.
- New community touchpoints: `CONTRIBUTING.md`, a feature-request
  template, and a question template. Nothing to upgrade — these
  just make it easier to extend or ask about the controller.

### v0.5.3

**No breaking changes.** Supply chain additions:

- Pre-built images are now published to GHCR on every tag:
  `ghcr.io/code-hustler-ft3d/ibg-controller:v0.5.3` (and `:0.5`,
  `:latest`). If you were building locally with `docker build`, you
  can switch to `docker pull` instead, but the local-build path is
  not deprecated.
- Every image is cosign-signed keyless. If you want to enforce
  signature verification in your deployment (recommended), see
  [`SECURITY.md`](../SECURITY.md) for the `cosign verify` recipe.
- SBOM is attached to the image as a signed attestation and to the
  GitHub release as `sbom.spdx.json`.

No action required — the existing `.env` and deployment flow keep
working. Wire up cosign verification at your leisure.

### v0.5.2

**No breaking changes.** Additive:

- New `ALERT_SHUTDOWN` log token (INFO-level) emitted on SIGTERM /
  SIGINT. Optional to wire up — it helps distinguish
  operator-initiated restarts from JVM crashes in dashboards. See
  [`OBSERVABILITY.md`](OBSERVABILITY.md#alert_shutdown) for the
  recommended threshold on `graceful=false` occurrences.
- New [`FROM_IBC.md` unsupported-IBC-keys matrix](FROM_IBC.md#unsupported-ibc-keys)
  for users on IBC evaluating a switch.

### v0.5.1

**No breaking changes.** Bug fix + new alert token:

- `BYPASS_WARNING` is now honored in both dismissal code paths (was
  only honored in the opportunistic post-login sweep before). If you
  had `BYPASS_WARNING` set and were seeing post-login disclaimers
  *still* block, v0.5.1 fixes that. No action required.
- New `ALERT_LOGIN_FAILED` token. Wire your alerting on it to catch a
  rotated-password-not-yet-mirrored-into-env scenario before the CCP
  streak escalates and IBKR locks the account.

### v0.5.0

**No breaking changes.** New tooling + alert:

- `scripts/ibc_config_to_env.py` one-shot migration tool for users
  coming from IBC. Also in the release tarball at the root.
- `ALERT_PASSWORD_EXPIRED` token. Surfaces the IBKR password-rotation
  warning dialog (with `days_remaining=N` when available) and the
  login-blocking expired variant. Wire alerting on this — IBC doesn't
  surface it this cleanly.

### v0.4.0

**Breaking behaviour change worth knowing about.** Auth-recovery
paths no longer invoke `do_restart_in_place` on credential failures.
Instead, a new in-JVM relogin sequence matches IBC's
`LoginManager.initiateLogin` semantics, staying in one JVM. This
avoids feeding IBKR's CCP rate limiter during retry loops. If you
had monitoring counting JVM restarts as a liveness signal, note that
a healthy running-but-reauthing controller will now show fewer JVM
restarts than previously.

## Watch for in your logs after an upgrade

First 30 minutes on a new version:

```bash
docker logs -f ibkr 2>&1 | grep -E 'ALERT_|ERROR|CRITICAL'
```

First successful login cycle confirms the upgrade is healthy:

```bash
curl -sf http://ibkr:8080/health | jq
# Expect: "status":"healthy", "state":"MONITORING", and
# the "version" field reflects the new release.
```

If `/health` reports the old version number, the image rebuild
didn't pick up the new controller — check your Dockerfile's `COPY`
step and rebuild without cache (`docker build --no-cache`).

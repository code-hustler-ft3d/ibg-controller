# Changelog

All notable changes to `ibg-controller` are documented here. The
format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project follows [Semantic Versioning](https://semver.org/).

## [0.3.2] - 2026-04-16

### Fixed

- **CCP backoff counter defeated by premature reset during
  stuck-connecting cycles**: v0.3.1's `handle_2fa` detection worked
  (the tight 90s relogin loop stopped), but the exponential ramp
  never fired — every cycle applied a flat 60s backoff. Verified in
  production over 4 consecutive cycles, ~160s apart, all at 60s
  instead of the expected 60 → 120 → 240 → 480s ramp.
- Root cause: three sites unconditionally called `_reset_ccp_backoff()`
  right after `_detect_ccp_lockout(timeout=25)` returned False, on the
  implicit assumption that "no `Timeout!` in 25s ⇒ auth progressed
  past CCP". That assumption holds for the v0.2.2 CCP-Timeout failure
  mode, but it breaks for exactly the stuck-connecting mode v0.3.1
  just taught the controller to recognize: Gateway's internal
  "connecting to server (trying for another N)" retry loop never
  emits a `Timeout!` signature, so `_detect_ccp_lockout` returns
  False, and the reset then fires even though auth hasn't made any
  progress. `do_restart_in_place` recurses, `handle_2fa` detects
  stuck again, applies backoff — but from a freshly-reset counter,
  so always 60s.
- Fix: gate the three premature resets on
  `not _detect_login_stuck_connecting()`. If the login dialog still
  shows the retry-loop label, we've passed the `Timeout!` check but
  haven't actually progressed past the auth gate — keep the backoff
  counter intact. The three gated sites are in `main()`,
  `do_restart_in_place()`, and `attempt_reauth()`. Three other
  reset sites (after 2FA success in `handle_2fa`, and after
  `do_restart_in_place` returns True from the lockout-retry arm)
  are left unchanged — those are true-success signals.

### Validation

- All 66 unit tests pass unchanged. The fix is a three-line gate at
  three call sites; the helper it gates on (`_detect_login_stuck_connecting`)
  was added and unit-tested in v0.3.1.
- Live-side paths and healthy-restart paths are unaffected: when auth
  genuinely progresses past the CCP gate, `_detect_login_stuck_connecting`
  returns False and the reset fires exactly as before.

## [0.3.1] - 2026-04-16

### Fixed

- **Paper-side infinite relogin loop with no backoff**: when IBKR's
  auth server stops accepting new sessions for an account, Gateway's
  login dialog enters an internal `"Attempt N: connecting to server
  (trying for another XX seconds)"` retry state rather than emitting
  the `AuthTimeoutMonitor-CCP: Timeout!` line that the v0.2.2 backoff
  watches for. `handle_2fa` was timing out after not seeing a 2FA
  dialog, falling into the `RELOGIN_AFTER_TWOFA_TIMEOUT=yes` branch,
  and re-clicking Log In with zero backoff — approximately every
  ~90s indefinitely. Observed in production on the paper instance
  while live was healthy: 30+ minutes of unbacked-off retries, each
  resetting Gateway's internal attempt counter and extending the
  lockout from IBKR's perspective.
- Added `_detect_login_stuck_connecting()` that inspects visible
  JLabel text for the "connecting to server" / "trying for another"
  signature. `handle_2fa` now calls it on 2FA-wait timeout and, if
  Gateway is stuck in the retry loop, applies the same CCP
  exponential backoff (60s → 600s cap) the pre-auth path uses
  before any relogin or `TWOFA_TIMEOUT_ACTION` dispatch. The fix
  covers all three auth paths that eventually call `handle_2fa`:
  `main()`, `do_restart_in_place()`, and `attempt_reauth()`.
- Added `_reset_ccp_backoff()` at the two 2FA-success return points
  in `handle_2fa` so the backoff counter doesn't carry stale state
  when an earlier stuck-connecting detection applied backoff and
  the subsequent retry succeeded.
- 6 new unit tests cover the helper: positive matches for
  `connecting to server`, `trying for another`, case-insensitive
  matches; negative cases for unrelated labels, empty label lists,
  and agent-socket exceptions (should return False rather than
  propagate).

## [0.3.0] - 2026-04-16

The repo's `Dockerfile` and `docker/run.sh` are now tracked and shipped
as first-class deliverables. Previously they lived outside version
control as a temporary scaffold intended to be upstreamed into
`gnzsnz/ib-gateway-docker`. That fork has been retired, so this repo
is now the canonical home of both the controller *and* its image
recipe. No controller behavior has changed between v0.2.2 and v0.3.0
— only what the repo ships.

### Added

- `Dockerfile` at repo root. Extends a gnzsnz/ib-gateway base
  (`UPSTREAM_IMAGE` build-arg, default `:stable`), installs the
  AT-SPI stack, configures the ATK bridge into Gateway's JRE, and
  drops the controller artifacts from `dist/` into
  `/home/ibgateway/`. Pin a digest via `--build-arg UPSTREAM_IMAGE=...@sha256:...`
  for reproducible production builds.
- `docker/run.sh` — the `USE_PYATSPI2_CONTROLLER=yes`-aware launcher
  that replaces upstream's IBC-first entrypoint. Starts the
  controller, waits for the readiness signal, then brings up socat
  port forwarding.
- "Using the shipped Dockerfile" section in `README.md` with
  build-arg examples.

### Changed

- `Dockerfile` header rewritten: removed the stopgap framing that
  described the file as a wrapper pending an upstream PR. That PR
  was cancelled and the fork retired; this is now the canonical
  image recipe. Documented the `UPSTREAM_IMAGE` digest-pin pattern
  in the header comment.

## [0.2.2] - 2026-04-15

### Fixed

- **CCP lockout exponential backoff**: when IBKR's auth server
  silently drops an auth request (CCP lockout), the controller's
  `TWOFA_TIMEOUT_ACTION=restart` path immediately retried with zero
  backoff. Each retry extended the lockout — observed in production
  as ~15 auth attempts over 27 minutes, each resetting the cooldown
  timer. Fix: after clicking Log In, poll `launcher.log` for 25s
  for the `AuthTimeoutMonitor-CCP: Timeout!` signature. If
  detected, skip the 2FA wait, apply exponential backoff
  (60s → 120s → 240s → 480s → 600s cap), log `CCP LOCKOUT
  DETECTED` + the backoff duration, then retry via
  `do_restart_in_place`. Detection includes a stale-guard that
  checks whether a new auth cycle's `activate` appears after the
  `Timeout!` — if so, the Timeout is from a previous attempt and
  the poll keeps going rather than false-positive. Wired into all
  three auth paths: `main()` initial startup,
  `do_restart_in_place()` restart path, and `attempt_reauth()`
  monitor-loop re-login.

## [0.2.1] - 2026-04-12

### Fixed

- **Root cause of persistent auth timeouts**: the install4j launcher
  passes `-DjtsConfigDir=${installer:jtsConfigDir}` (an unsubstituted
  placeholder) to Java BEFORE any `INSTALL4J_ADD_VM_PARAMS` override.
  Java uses the first `-D` definition, so our override was silently
  ignored and Gateway read a nonexistent config path. Fixed by passing
  `-VjtsConfigDir=<path>` as a command-line argument to the install4j
  launcher, which substitutes the variable before constructing the
  Java command. Live dual-mode auth now completes in 3 seconds.

### Added

- 19 `--add-opens` / `--add-exports` JVM module-access flags (matching
  IBC's `ibcstart.sh`) added to `INSTALL4J_ADD_VM_PARAMS`. Gateway's
  auth and UI code uses reflection into `java.desktop` and `java.base`
  internals that Java 17's module system blocks by default.
- CI auto-release: pushing a `v*` tag now builds the tarball and
  publishes a GitHub Release automatically.
- Issue template and PR template for contributors.
- `.gitignore` expanded for IDE, editor, and `.env` patterns.

## [0.2.0] - 2026-04-11

Full IBC replacement for common production deployments of
`gnzsnz/ib-gateway-docker`-style images. Dual-mode (`TRADING_MODE=both`)
works end-to-end, post-login API config knobs land, IBC-compat
command server is present, and the env-var surface has been expanded
to hit parity with IBC's knobs for users migrating off IBC.

### IBC env var parity matrix

| IBC env var | Honored | Notes |
|---|---|---|
| `TWS_USERID` / `TWS_PASSWORD` | ✅ | including `_FILE` variants via run.sh |
| `TWS_USERID_PAPER` / `TWS_PASSWORD_PAPER` | ✅ | auto-swap when `TRADING_MODE=paper` |
| `TRADING_MODE` | ✅ | `live`, `paper`, `both` |
| `TWOFACTOR_CODE` / `TWOFACTOR_CODE_FILE` | ✅ | TOTP via stdlib hmac |
| `EXISTING_SESSION_DETECTED_ACTION` | ✅ | clicks `Continue Login` for primary |
| `TWS_MASTER_CLIENT_ID` | ✅ | API → Settings → Master client ID |
| `READ_ONLY_API` | ✅ | API → Settings → Read-Only API |
| `AUTO_LOGOFF_TIME` | ✅ | Lock and Exit, when Gateway shows the Log Off field |
| `AUTO_RESTART_TIME` | ✅ | Lock and Exit, when Gateway shows the Restart field |
| `TWOFA_EXIT_INTERVAL` | ✅ | 2FA wait timeout (seconds) |
| `TWOFA_TIMEOUT_ACTION` | ✅ | `exit` / `restart` / `none` |
| `RELOGIN_AFTER_TWOFA_TIMEOUT` | ✅ | retry login once before dispatching action |
| `BYPASS_WARNING` | ✅ | extends `SAFE_DISMISS_BUTTONS` allowlist |
| `TWS_COLD_RESTART` | ✅ | skips `apply_warm_state()` |
| `TIME_ZONE` / `TZ` | ✅ | written to jts.ini |
| `JAVA_HEAP_SIZE` | ✅ | via run.sh → INSTALL4J_ADD_VM_PARAMS |
| `VNC_SERVER_PASSWORD` | ✅ | via run.sh start_vnc |
| `SSH_TUNNEL`, `SSH_OPTIONS`, … | ✅ | via run.sh setup_ssh |
| `ALLOW_BLIND_TRADING` | ❌ | TWS Precautions tab only; warned at runtime |
| `SAVE_TWS_SETTINGS` | ❌ | not a Gateway knob; warned |
| `CUSTOM_CONFIG` | ❌ | controller reads env directly, no IBC config.ini; warned |
| `TWOFA_DEVICE` | ❌ | IB Key push requires mobile approval, impossible headless; warned |
| `IBC_SCRIPTS` | ✅ (via `CONTROLLER_SCRIPTS`) | analog hook in run.sh for the controller path |

### New capabilities that IBC doesn't have

- **Standalone bootstrap via `TWS_SERVER` / `TWS_SERVER_PAPER`**: set
  the regional server hostname directly, no warm state required.
- **Silent-cooldown vs wrong-credentials disambiguation**: parses
  Gateway's `launcher.log` on login failure and emits a targeted
  error message for each of four observed failure modes.
- **IBKR cold-start cooldown documentation** in `docs/BOOTSTRAP.md`.
- **Existing-session ping-pong backoff**: 5 clicks in 5 minutes
  triggers a 60s sleep to break loops with another container.
- **TWS_SERVER / GATEWAY_WARM_STATE hostname + path validation**:
  rejects injection attempts and system-dir paths at startup.
- **Account-number redaction** in debug logs (IBKR `DU\d+` / `U\d+`).
- **Command server auth token** (`CONTROLLER_COMMAND_SERVER_AUTH_TOKEN`)
  via `hmac.compare_digest`.
- **Monitor loop wedge escalation**: 3 minutes of "API port closed +
  no login dialog" triggers an in-place restart automatically.
- **Automated test suite**: 39 unit tests covering hostname
  validation, log redaction, yes/no coercion, TOTP against RFC 6238
  vectors, API port mapping, `BYPASS_WARNING` allowlist extension,
  and the `_warn_unsupported_env_vars` list maintenance.
- **GitHub Actions CI**: `make test` + release tarball build + install
  smoke test, plus a real-pyatspi2 module-load check in an ubuntu
  container with `python3-gi` / `gir1.2-atspi-2.0` installed.

### Added

- **Dual-mode support (`TRADING_MODE=both`)**: two IB Gateway JVMs in a
  single container, one live one paper, with fully isolated state
  (separate `Jts_live` / `Jts_paper` directories, separate agent Unix
  sockets, separate readiness files, separate process IDs). The Java
  agent's new `GET_PID` command lets the controller match its own
  Gateway JVM in AT-SPI disambiguation via `find_app(match_pid=...)`.
  Live-verified end-to-end. In dual mode, the command server's port
  auto-offsets by +1 on the paper instance to avoid a bind collision.
- **Post-login API configuration** (`handle_post_login_config`): drives
  Gateway's Configure → Settings dialog to apply these env vars after
  login completes:
  - `TWS_MASTER_CLIENT_ID` — integer, sets API → Settings → Master
    client ID. Live-verified.
  - `READ_ONLY_API` — yes/no, toggles API → Settings → Read-Only API.
    Live-verified.
  - `AUTO_LOGOFF_TIME` — `HH:MM`, sets Lock and Exit → Set Auto Log
    Off Time (when Gateway is showing that label).
  - `AUTO_RESTART_TIME` — `HH:MM AM/PM`, sets Lock and Exit → Set
    Auto Restart Time (when Gateway is showing that label).
    Live-verified via warm-state test: re-opened the dialog post-set
    and confirmed "at 06:15 PM" in the panel.

  Gateway's Lock and Exit panel shows *either* the Auto Log Off Time
  field *or* the Auto Restart Time field depending on whether the
  account has the autorestart daily-token cycle active. The handler
  tries both labels and sets the one Gateway is displaying; if the
  user set the other one, a clear warning is logged suggesting the
  matching env var.

  `ALLOW_BLIND_TRADING` and `SAVE_TWS_SETTINGS` are recognized and
  trigger a warning — they're TWS-only config knobs with no equivalent
  in Gateway's simplified dialog tree.
- **IBC-compat TCP command server** (Phase 2.4): daemon thread listening
  on `CONTROLLER_COMMAND_SERVER_PORT` (unset = disabled, `7462` matches
  IBC). Commands:
  - `STOP` — clean shutdown via SIGTERM
  - `RESTART` — tear down Gateway JVM and re-drive the full login flow
    in place, preserving the controller process and the monitor loop's
    heartbeat state
  - `RECONNECTACCOUNT` — re-drive login via `attempt_reauth`
  - `ENABLEAPI` — no-op (`ApiOnly=true` is always set in `jts.ini`)
  - `RECONNECTDATA` — returns a clean error on Gateway (no File →
    Reconnect Data menu item; TWS users get the click dispatch)
  Binds `0.0.0.0` by default so Docker port forwarding works; restrict
  via `docker run -p 127.0.0.1:7462:7462` for loopback-only external
  access.
- **TWS product switch** (`GATEWAY_OR_TWS=tws`): branches launcher
  discovery and AT-SPI app name search so the same controller drives
  either IB Gateway or Trader Workstation from the same image. Code
  path is in place; live-tested against Gateway only (TWS validation
  is a follow-up once a TWS image is built).
- **New agent commands**:
  - `GET_PID` — returns the JVM's OS PID for dual-mode disambiguation
  - `JTREE_SELECT_PATH <title>|<p1>/<p2>/...` — navigate a `JTree` to
    a slash-separated path by matching `node.toString()` at each
    level. Expands parent nodes as it walks.
  - `JCHECK <title>|<name>|<bool>` — idempotent toggle of a
    checkbox/radio/toggle button by accessible name or text, scoped
    to the specified window.
  - `SETTEXT_BY_LABEL <title>|<label>|<value>` — set a text field by
    its adjacent `JLabel`'s text. Handles `JSpinner` editors by
    calling `commitEdit()` after `setText`.
- **Late-arriving existing-session dialog handler**: the initial
  post-login dialog inspection poll was extended from a fixed 2s to
  a 6s polling loop; `handle_2fa` also watches for the existing-session
  dialog on each iteration in case it arrives during the 2FA wait.
  Both paths click `Continue Login` via `CLICK_IN_WIN` so clicks are
  scoped to the dialog, not the main window.
- **IBKR cold-start cooldown documentation**: `docs/BOOTSTRAP.md`
  documents the ~5-minute silent `AuthTimeoutMonitor-CCP: Timeout!`
  that IBKR occasionally returns after bursts of failed auth attempts,
  with instructions for what to check.

### Changed

- `wait_for_controller_ready()` in `run.sh` no longer returns non-zero
  on timeout. Previously under `set -Eeo pipefail` this would crash
  the entire container on a single controller timeout, which in dual
  mode killed the sibling before it got a chance to start. Now it
  warns and continues, matching the legacy IBC behavior.
- `start_controller()` force-exports `TWS_SETTINGS_PATH` so the Python
  subprocess sees the per-instance config directory set by the outer
  dual-mode dispatch. Without this, both Gateway JVMs in dual mode
  wrote state into the shared `Jts/` directory.
- Command server port in dual mode: paper instance gets
  `CONTROLLER_COMMAND_SERVER_PORT + 1` to avoid a bind collision with
  live. Single-mode passes through unchanged.
- `handle_existing_session_dialog` candidate list now includes
  `Continue Login` (the actual button text on Gateway 10.45.1c)
  ahead of the older IBC fallback labels.
- `EXISTING_SESSION_DETECTED_ACTION=secondary` now maps to `Cancel`
  on Gateway's modern dialog, which has no separate "connect as
  secondary" button.

### Fixed

- Dual-mode `find_app` AT-SPI collision: when two `IBKR Gateway` apps
  are present, the controller now picks its own via
  `get_process_id()` matched against the agent's reported PID.
- `ensure_jts_ini` writes to `JTS_CONFIG_DIR` (the new per-instance
  path abstraction) rather than `TWS_PATH`, so dual-mode instances
  write their `jts.ini` to the right place.
- `handle_post_login_dialogs` poll window (see Added).

## [0.1.0] - 2026-04-10

Initial working single-mode cold-start. Replaces IBC for the common
case of a paper-or-live-only `gnzsnz/ib-gateway-docker` container.

### Added

- Python controller with AT-SPI2-based component discovery
- In-JVM Java agent (loaded via `-javaagent:`) for text input and
  clicks that Swing rejects from outside the JVM — `SETTEXT`,
  `GETTEXT`, `CLICK`, `LIST`, `WINDOWS`, `WINDOW`, `LABELS`,
  `SETTEXT_IN_WIN`, `CLICK_IN_WIN`
- Login dialog automation (username, password, trading mode toggle,
  Log In button)
- TOTP 2FA handling via the `TWOFACTOR_CODE` env var
- Post-login disclaimer auto-dismiss (`I understand and accept` etc.)
- `EXISTING_SESSION_DETECTED_ACTION` dialog handler
- API port readiness signal (`/tmp/gateway_ready`)
- Re-auth detection in the monitor loop (daily restart + silent
  session loss)
- `TWS_SERVER` / `TWS_SERVER_PAPER` env vars for regional server
  override in cold-start without warm state
- `GATEWAY_WARM_STATE` for docker-cp-based state seeding
- Makefile with `make`, `make install DESTDIR=...`, `make release
  VERSION=...`, `make clean`, `make test`
- Full docs: `README.md`, `docs/ARCHITECTURE.md`, `docs/BOOTSTRAP.md`,
  `docs/MIGRATION.md`

[0.3.2]: https://github.com/code-hustler-ft3d/ibg-controller/releases/tag/v0.3.2
[0.3.1]: https://github.com/code-hustler-ft3d/ibg-controller/releases/tag/v0.3.1
[0.3.0]: https://github.com/code-hustler-ft3d/ibg-controller/releases/tag/v0.3.0
[0.2.2]: https://github.com/code-hustler-ft3d/ibg-controller/releases/tag/v0.2.2
[0.2.1]: https://github.com/code-hustler-ft3d/ibg-controller/releases/tag/v0.2.1
[0.2.0]: https://github.com/code-hustler-ft3d/ibg-controller/releases/tag/v0.2.0
[0.1.0]: https://github.com/code-hustler-ft3d/ibg-controller/releases/tag/v0.1.0

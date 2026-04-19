# Changelog

All notable changes to `ibg-controller` are documented here. The
format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project follows [Semantic Versioning](https://semver.org/).

## [0.5.9] - 2026-04-19

### Changed (BEHAVIOR CHANGE — see UPGRADING.md)

- **CCP-lockout-triggered JVM restart is now opt-in.** Default behaviour
  flipped: when `_escalate_to_jvm_restart` would previously loop up to 5
  SIGKILL-capable teardown cycles with adaptive cool-downs, it now emits
  `ALERT_CCP_PERSISTENT_HALT` and exits immediately. Root cause from a
  2026-04-19 production incident: each teardown's SIGKILL fallback was
  re-stranding the IBKR session slot and extending IBKR's server-side
  zombie timer, so 5 retries compounded the lockout we were trying to
  clear (24h of stuck state; operator ultimately cleared it by
  logging into IBKR Mobile, which per IBKR's docs auto-logs-out all
  TWS/Gateway sessions — web Client Portal login/logout was
  ineffective against the stranded slot despite ~8h of attempts,
  confirming Client Portal is read-only concurrent for TWS auth
  slots).
  Halt-by-default prevents the controller from participating in the
  slot-stranding feedback loop. v0.5.6's clean UI logout reduced how
  often a teardown ends in SIGKILL, but didn't help in the post-CCP
  disposed-shell state where the main window isn't findable — exactly
  the state the escalation loop runs in.
  - New `CCP_LOCKOUT_MAX_JVM_RESTARTS` env var (default 0). Set to a
    positive integer to restore the pre-v0.5.9 auto-restart loop,
    capped at that many attempts. Supersedes `JVM_RESTART_MAX_ATTEMPTS`
    when set.
  - Existing deployments that depended on auto-recovery must set
    `CCP_LOCKOUT_MAX_JVM_RESTARTS=5` to keep working without operator
    intervention on lockout. Recommended default for most operators:
    leave at 0 and wire `ALERT_CCP_PERSISTENT_HALT` to paging.

### Fixed

- **Pre-MONITORING SIGTERMs no longer strand slots silently.** v0.5.6's
  `_attempt_clean_logout` only found the main "IB Gateway" window,
  which doesn't exist in INIT / LAUNCHING / AGENT_WAIT / APP_DISCOVERY /
  LOGIN states. Signal handlers received in those states fell through
  to SIGTERM-then-SIGKILL and reported `failed_unreachable` to
  monitoring — misleading because the agent wasn't unreachable, the
  window just hadn't been rendered yet. v0.5.9 dispatches by state:
  - Pre-auth states (no slot held): emit `status=safe_no_session` and
    proceed directly to SIGTERM. No slot to release; no misleading
    alert noise.
  - `POST_LOGIN` (slot in flight, no closable UI yet): emit
    `status=zombie_slot_cannot_release`. Distinct label so operators
    can see that a SIGTERM here stranded a slot server-side, rather
    than mistaking it for a UI-close failure.
  - `TWO_FA`: try to close the 2FA dialog via the agent before SIGTERM.
    `status=cancelled_pending_2fa` on success, `status=failed_cancel_2fa`
    on agent rejection or JVM stall.
  - `MONITORING` + post-auth pre-monitoring states (`DISCLAIMERS`,
    `API_WAIT`, `CONFIG`, `READY`, `COMMAND_SERVER`): unchanged — still
    use the v0.5.6 UI-close path.

### Added

- **`ALERT_CCP_PERSISTENT_HALT`** log token (ERROR-level) emitted when
  `_escalate_to_jvm_restart` is reached with
  `CCP_LOCKOUT_MAX_JVM_RESTARTS=0` (the default). Format:
  `ALERT_CCP_PERSISTENT_HALT mode=<live|paper> reason="..." remediation="..."`.
  Stability-contract grep token; wire to your operator paging channel.
  The `remediation` field includes the standard IBKR Client Portal
  session-clear steps so oncall doesn't need to look up the runbook.
- **`ALERT_CLEAN_LOGOUT` status value set extended to seven**:
  `succeeded` / `failed_unreachable` / `failed_timeout` (v0.5.6) plus
  `safe_no_session` / `zombie_slot_cannot_release` /
  `cancelled_pending_2fa` / `failed_cancel_2fa` (v0.5.9). All seven
  are part of the public stability contract.
- **`CCP_LOCKOUT_MAX_JVM_RESTARTS` env var** (default 0). Caps the
  number of JVM-teardown cycles `_escalate_to_jvm_restart` will
  attempt. Default 0 halts immediately.
- **`_classify_shutdown_for_state(state)` pure-logic helper**
  returning `(attempt_close, fallback_status, reason)`. Split out so
  the State → status-label decision table is unit-testable independent
  of the signal-handler shell.
- **`_attempt_state_aware_clean_logout(state)`** wrapper. For TWO_FA,
  closes the 2FA dialog via the agent (`CLOSE_WIN "Second Factor"`)
  before polling for JVM exit. For all other states, delegates to the
  v0.5.6 helper unchanged.
- **22 new unit tests** in `tests/test_pure_logic.py`:
  `TestCcpPersistentHalt` (4), `TestStateAwareShutdown` (9),
  `TestClassifyShutdownForState` (4), `TestAttemptStateAwareCleanLogout`
  (5). Test total: 186 → 208.

### Docs

- `CHANGELOG.md` — this entry.
- `UPGRADING.md` — v0.5.9 section added (BEHAVIOR CHANGE; explains the
  restart-loop removal, how to opt back in, and what to watch for
  post-upgrade).

## [0.5.8] - 2026-04-19

### Fixed

- **Release image now pins Gateway upstream by digest** (`gnzsnz/ib-gateway:10.45.1c@sha256:b4ede80…`) instead of resolving `:stable` at build time. v0.5.7 shipped Gateway 10.37.1q because `:stable` resolved to an older build — a silent downgrade from the 10.45.1c consumers were running from local builds. v0.5.8 is byte-identical controller code to 0.5.6/0.5.7; only the upstream pin changes.

## [0.5.7] - 2026-04-19

### Changed

- **Release image now ships linux/amd64 + linux/arm64.** Previously
  `linux/amd64` only, which forced consumers on Apple Silicon to run
  the image under rosetta emulation with a measurable JVM performance
  hit. The `Dockerfile`'s ATK-bridge step already handled both JRE
  layouts (install4j on amd64, Zulu on arm64), and upstream
  `gnzsnz/ib-gateway:10.45.1c` is multi-arch, so this is a
  workflow-only change — no source deltas from v0.5.6.

## [0.5.6] - 2026-04-18

### Fixed

- **Stranded IBKR session slots — root-cause fix.** v0.5.5 was
  containment (extended grace + adaptive cool-down + visibility); it
  reduced how often stranded slots happen and softened the blast when
  they do, but did not eliminate the underlying cause. v0.5.6 attacks
  the root cause: during mid-life restart and controller shutdown, the
  controller now dispatches a UI-level window-close to Gateway's main
  window *before* any SIGTERM. A `WINDOW_CLOSING` `AWTEvent` posted to
  the system event queue fires Gateway's own `WindowListener` — the
  same handler a user clicking the X button would trigger — which
  performs an ordered CCP session-close on the way out. This is the
  shutdown path the Gateway vendor expects, and it drains the IBKR
  session slot *server-side* before the JVM exits. If the clean close
  succeeds within `CLEAN_LOGOUT_TIMEOUT_SECONDS` (default 15s),
  SIGTERM is skipped entirely. If it fails (agent unreachable, or JVM
  doesn't exit in time), the controller falls through to v0.5.5's
  defense-in-depth: SIGTERM + `JVM_TEARDOWN_GRACE_SECONDS` grace →
  SIGKILL → adaptive CCP cool-down. So v0.5.6 is strictly additive —
  best case, no stranded slot at all; worst case, same safety net as
  v0.5.5.

### Added

- **`ALERT_CLEAN_LOGOUT`** log token (INFO-level) emitted by both
  `_teardown_jvm_for_restart` and the `SIGTERM`/`SIGINT` handler.
  Format:
  `ALERT_CLEAN_LOGOUT mode=<live|paper> pid=<pid|none> status=<succeeded|failed_unreachable|failed_timeout> reason="..."`.
  `status=succeeded` is the happy path and the new stability-contract
  signal operators should watch. `failed_unreachable` means the
  AT-SPI agent didn't respond to `CLOSE_WIN` (Gateway UI not yet up,
  main window title moved); `failed_timeout` means the close event
  was delivered but the JVM didn't exit in `CLEAN_LOGOUT_TIMEOUT_SECONDS`
  — the Gateway close handler may be stalled on CCP I/O. Either
  failure mode falls through to the SIGTERM path, so no session is
  "more stuck" than it was pre-0.5.6 — they just don't get the clean
  path's benefit. Fully documented in
  [`docs/OBSERVABILITY.md`](docs/OBSERVABILITY.md#alert_clean_logout)
  with emission shape, operator guidance, and debounce recommendation.
  Part of the public stability contract from v0.5.6 onward.
- **`CLEAN_LOGOUT_TIMEOUT_SECONDS`** env var (default 15). Seconds to
  wait for the Gateway JVM to exit after `WINDOW_CLOSING` is
  dispatched. Bump to 30 if your tenant's CCP session-close regularly
  takes longer than 15s (visible as `status=failed_timeout` without
  follow-up `ALERT_CCP_PERSISTENT`); lower only if you explicitly
  want to accept failed_timeout more aggressively and rely on the
  SIGTERM fallback.
- **`CLOSE_WIN <title_substr>` agent command** in
  `GatewayInputAgent.java` — finds a top-level Swing window whose
  title contains the substring and posts a `WindowEvent.WINDOW_CLOSING`
  via `Toolkit.getDefaultToolkit().getSystemEventQueue().postEvent`.
  This mimics a user clicking the X button so Gateway's native
  `WindowListener` runs, vs signal-dispatched JVM shutdown hooks which
  skip that code path. Agent returns `OK` on dispatch or
  `ERR not_found …` if no window matched — idempotent and safe to
  retry.
- **`_attempt_clean_logout(timeout_seconds=None)`** pure-logic helper
  returning a `(ok, status, reason)` tuple. Returns
  `(True, "succeeded", …)` when the JVM exits within the poll
  deadline, `(False, "failed_unreachable", …)` when the agent
  `CLOSE_WIN` call is rejected, and `(False, "failed_timeout", …)`
  when the event was delivered but the JVM remains alive past the
  deadline. Fully unit-tested independent of the teardown/shutdown
  shells.
- **10 new unit tests** in `tests/test_pure_logic.py`: 5 for
  `_attempt_clean_logout` (`TestAttemptCleanLogout`), 3 added to
  `TestShutdownAlert` (clean-logout happy path, fall-through to
  SIGTERM, timeout-then-SIGKILL still reports
  `graceful=false`), and 2 added to `TestUncleanShutdownAlert`
  (clean logout skips teardown SIGTERM path; clean-logout failure
  still emits the fall-through alerts). Test total: 176 → 186.

### Docs

- `OBSERVABILITY.md` — added the `ALERT_CLEAN_LOGOUT` section with
  emission shape, status-value grep contract, per-status operator
  remediation, and a recommended debounce. Added
  `CLEAN_LOGOUT_TIMEOUT_SECONDS` to the env-var reference table and a
  clean-logout success-rate grep example. Bumped the JSON-shape
  `version` field to 0.5.6. Stability-contract paragraph now notes
  v0.5.6 added `ALERT_CLEAN_LOGOUT`.
- `UPGRADING.md` — added the v0.5.6 section (non-breaking upgrade;
  explains the root-cause attack; tuning `CLEAN_LOGOUT_TIMEOUT_SECONDS`;
  note that v0.5.5 defenses remain as fallback).

## [0.5.5] - 2026-04-18

### Fixed

- **Stranded IBKR session slots from unclean JVM teardowns.** Empirical
  finding during a 2026-04-18 incident: a container showed
  `ALERT_CCP_PERSISTENT` on both live AND paper modes for 2+ hours
  despite no concurrent web/mobile session. Root-cause analysis of
  `_teardown_jvm_for_restart` showed SIGTERM → 20s wait → SIGKILL with
  no explicit CCP session-close — the restart path relied on Gateway's
  shutdown hooks to drain the IBKR session. When those hooks don't run
  cleanly (Swing EDT stall, blocked native I/O), IBKR's server holds
  the session slot until its own timeout fires, so the *next* auth
  attempt from the *same* controller hits silent-drop CCP lockout as
  if a concurrent session existed. The v0.5.4 fixed-duration cool-down
  (1200s) was often shorter than IBKR's server-side drain, so the
  restart loop would consume attempts against a still-stranded slot.
  v0.5.5 attacks this three ways:
  1. **Extended SIGTERM grace** — bumped from 20s to 30s via the new
     `JVM_TEARDOWN_GRACE_SECONDS` env var, reducing the rate at which
     SIGKILL is needed in the first place.
  2. **Adaptive cool-down** — `_apply_ccp_long_cooldown` now scales by
     attempt index (`base × multiplier^(attempt-1)`, capped). Default
     progression: 1200s → 1800s → 2700s → 3600s (capped) → 3600s.
     Gives IBKR escalating quiet time to drain any stranded slot
     before the next auth attempt, instead of firing the same short
     wait five times in a row against the same held slot.
  3. **Operator visibility** — new `ALERT_JVM_UNCLEAN_SHUTDOWN` fires
     on every SIGKILL-escalated teardown, so monitoring can correlate
     unclean shutdowns with follow-up CCP lockouts and tune
     `CCP_COOLDOWN_MAX_SECONDS` upward for longer-draining tenants.

### Added

- **`ALERT_JVM_UNCLEAN_SHUTDOWN`** log token (WARNING-level) emitted
  by `_teardown_jvm_for_restart` when the JVM ignored SIGTERM past the
  grace window or the teardown raised. Distinct from `ALERT_SHUTDOWN`
  (controller-lifecycle, INFO-level) — this one fires on *mid-life*
  restarts only. Fully documented in
  [`docs/OBSERVABILITY.md`](docs/OBSERVABILITY.md#alert_jvm_unclean_shutdown)
  with emission shape, operator remediation, and debounce guidance.
  Part of the public stability contract from v0.5.5 onward.
- **`JVM_TEARDOWN_GRACE_SECONDS`** env var (default 30). Seconds to
  wait for the Gateway JVM to exit after SIGTERM during mid-life
  restart before SIGKILL. Bump to 60 under host resource pressure.
- **`CCP_COOLDOWN_MAX_SECONDS`** env var (default 3600). Upper cap on
  the adaptive long cool-down. Raise if your IBKR tenant's server-side
  session timeout exceeds 1h.
- **`CCP_COOLDOWN_MULTIPLIER`** env var (default 1.5). Multiplicative
  factor per restart attempt. Set to `1.0` to restore v0.5.4 and
  earlier's fixed-duration behaviour.
- **`_compute_adaptive_cooldown` pure-logic helper** — extracted from
  `_apply_ccp_long_cooldown` so the scaling math is covered by unit
  tests independent of the sleep + logging shell.
- **10 new unit tests** in `tests/test_pure_logic.py`: 7 for the
  adaptive-cooldown scaling (`TestAdaptiveCooldown`) and 3 for the
  unclean-shutdown alert emission (`TestUncleanShutdownAlert`). Test
  total: 166 → 176.

### Docs

- `OBSERVABILITY.md` — added the `ALERT_JVM_UNCLEAN_SHUTDOWN` section,
  a Tier 1.5 grep-examples block for operational warnings, and all
  three new env vars in the reference table. Bumped the JSON-shape
  `version` field to 0.5.5. Stability-contract paragraph now notes
  v0.5.5 added `ALERT_JVM_UNCLEAN_SHUTDOWN`.
- `UPGRADING.md` — added the v0.5.5 section (non-breaking upgrade,
  explains the stranded-session diagnosis + when to tune the new env
  vars).

## [0.5.4] - 2026-04-18

### Fixed

- **`release-image.yml` trigger** — v0.5.3's `on: release: types:
  [published]` trigger never fired because GitHub's recursion guard
  suppresses downstream workflow triggers from `GITHUB_TOKEN`-originated
  events (ci.yml creates the release). Switched to `on: push: tags:
  ['v*']` so the image workflow runs in parallel with ci.yml and polls
  (30 × 10s) for the release object before uploading the SBOM asset.
  Result: v0.5.4 is the first tag whose image + SBOM + cosign attestation
  land automatically end-to-end.
- **`workflow_dispatch` input** — added a manual `tag` input so any
  past tag (including v0.5.3) can be retroactively built by invoking
  `gh workflow run "Release image" -f tag=v0.5.3`. Also handles the
  case where CI-validated tags get re-built after a Dockerfile fix
  without needing a new version bump.
- **Metadata-action tag derivation** — replaced `type=semver` (which
  reads `github.ref`, wrong on workflow_dispatch) with explicit
  `type=raw` values computed from a centralised `version` step, so
  both triggers produce identical tag sets.

### Added

- **[`CONTRIBUTING.md`](CONTRIBUTING.md)** at repo root — dev env setup,
  test commands, and four "Adding a new..." walkthroughs (ALERT token,
  dialog handler, env var, IBC-key mapping). Each walkthrough numbers
  the implementation + doc + test steps so first-time contributors
  have a deterministic path. Lowers the bar for the
  `gnzsnz/ib-gateway-docker` community to extend this codebase as IBC
  sunsets in September 2026.
- **[`.github/CODEOWNERS`](.github/CODEOWNERS)** — path-scoped review
  routing so workflow edits, the stability-contract docs
  (`OBSERVABILITY.md`, `UPGRADING.md`, `CHANGELOG.md`), and
  `SECURITY.md` auto-notify on any PR that touches them, even from
  contributors who don't know those paths are sensitive.
- **Feature request and question issue templates** under
  `.github/ISSUE_TEMPLATE/`. Feature requests nudge contributors to
  check `FROM_IBC.md`'s unsupported-keys matrix before filing;
  questions route into a dedicated `question` label and point at the
  relevant doc sections so answerable questions get answered faster.
- **Enhanced PR template** — added a "Why" section so reviewers
  aren't reverse-engineering motivation, updated the stale "60 tests
  green" to "166+ tests green", expanded the doc-update checklist
  (`OBSERVABILITY.md`, `FROM_IBC.md`, `UPGRADING.md`), and linked
  `CONTRIBUTING.md`'s walkthroughs inline.
- **README badges** — release version, release-image workflow
  status, license, and cosign-signed shield. Gives drive-by visitors
  a signal on release cadence and supply-chain posture without having
  to open CHANGELOG or SECURITY.md.

### Notes

- v0.5.3's image was backfilled immediately after v0.5.4 shipped
  by dispatching the fixed workflow against `tag=v0.5.3`. Both
  tags now exist on GHCR with signatures and SBOM attestations;
  no operator action needed.

## [0.5.3] - 2026-04-18

### Added

- **Published container image** at `ghcr.io/code-hustler-ft3d/ibg-controller`.
  Each git tag push now triggers
  `.github/workflows/release-image.yml` which builds the shipped
  `Dockerfile`, pushes to GHCR with tags `:v<version>`, `:<major>.<minor>`,
  and `:latest`, and records the digest in the CI log for pinning.
  Drops the "you must `git clone` + `make` + `docker build`" barrier
  for users who just want a ready-to-run image. Build is
  reproducible from the tag: same upstream base, same dist/ artifacts,
  same layer graph.
- **Keyless cosign signing** via Sigstore of every pushed image. The
  signing identity is the GitHub Actions OIDC token for this repo's
  `release-image.yml` workflow. No private key to manage, no way for
  a forked workflow to sign as us. Verify with `cosign verify` using
  the recipe in [`SECURITY.md`](SECURITY.md).
- **SPDX SBOM** generated with [syft](https://github.com/anchore/syft)
  against the pushed image by digest, attached to the image as a
  signed cosign attestation AND uploaded to the GitHub release page
  as `sbom.spdx.json`. Consumers can audit the full layer-wise
  dependency tree without pulling the image; reproducibility check:
  the SBOM's root digest matches the image digest printed in CI.
- **New [`SECURITY.md`](SECURITY.md) at the repo root** — supply chain
  model, cosign verification walkthrough, pinning-by-digest recipe,
  threat model, and private vulnerability reporting flow. Shows up
  automatically on the GitHub repo's **Security** tab.
- **README Quick start** updated to lead with `docker pull
  ghcr.io/code-hustler-ft3d/ibg-controller:latest` — the "build
  yourself" path is now a fallback rather than the default.

### Non-goals

- Multi-arch (`linux/arm64`) isn't enabled yet. The upstream
  `gnzsnz/ib-gateway` base image's ARM-path behaviour (Zulu JRE at a
  different location, ATK wrapper lookup) hasn't been verified inside
  our CI gateway-version matrix. `linux/amd64` is the only supported
  platform until that's validated — adding `linux/arm64` is a
  one-line change in `release-image.yml` once it is.

## [0.5.2] - 2026-04-18

### Added

- New **`ALERT_SHUTDOWN`** grep-contract log token (INFO-level)
  emitted from the `SIGTERM` / `SIGINT` handler. Format:
  `ALERT_SHUTDOWN mode=<live|paper> signal=<SIGTERM|SIGINT> graceful=<true|false> reason="..."`.
  `graceful=false` means the Gateway JVM ignored `SIGTERM` for 15s
  and had to be `SIGKILL`'d — points at a deadlocked Swing EDT, a
  blocked native I/O call, or host resource starvation. Its *absence*
  in the last ~N seconds of a container's logs before an exit is
  itself a signal: the controller died without going through the
  signal handler, i.e. unexpected JVM / interpreter crash rather
  than operator-initiated restart. Sits at INFO deliberately so it
  doesn't trip ERROR-level wake-someone-up grep filters, but remains
  catchable via the `ALERT_` prefix.
  Full grep-contract:
  [`docs/OBSERVABILITY.md`](docs/OBSERVABILITY.md#alert_shutdown).
- New [`docs/UPGRADING.md`](docs/UPGRADING.md) — version-to-version
  upgrade workflow, rollback recipe, and per-version operator notes.
  Fills a gap: the CHANGELOG lists *what* changed, not *what an
  operator needs to do* to move from one tag to the next. Pre-1.0
  version scheme is called out explicitly: minor bumps in the `0.x`
  series are allowed to contain breaking changes, and every one that
  does will be called out in the CHANGELOG's **Removed** / **Changed**
  sections and in `UPGRADING.md`.
- Expanded [`docs/FROM_IBC.md` unsupported-IBC-keys matrix](docs/FROM_IBC.md#unsupported-ibc-keys):
  converted the old 6-bullet list into four grouped tables
  (stay-on-IBC, no-op in headless Docker, config-shape mismatch with
  workaround, handled implicitly) covering every IBC key
  `ibc_config_to_env.py` knows about. Gives IBC users evaluating a
  switch a single place to confirm whether their setup has a clean
  migration path.

## [0.5.1] - 2026-04-17

### Added

- New **`ALERT_LOGIN_FAILED`** grep-contract log token. Emitted when
  Gateway surfaces a credential-rejection modal during in-JVM relogin
  (`reason="bad-credentials"`) or when the terminal-failure path in
  `_diagnose_login_failure` matches a bad-password `launcher.log`
  fingerprint (`reason="bad-credentials"` or
  `reason="post-auth-no-progress"`). Closes an observability gap:
  previously a stale `TWS_PASSWORD` (password rotated in the IBKR
  portal but not yet mirrored into the container env) would surface
  only as `ALERT_CCP_PERSISTENT` after the CCP streak hit its
  threshold — by which time the account could already be locked out.
  `ALERT_LOGIN_FAILED` fires on the first rejected attempt so
  monitoring can page before the streak escalates. Format:
  `ALERT_LOGIN_FAILED mode=<live|paper> reason="<bad-credentials|post-auth-no-progress>" suggested_action="..."`.
  Full grep-contract + dedupe guidance:
  [`docs/OBSERVABILITY.md`](docs/OBSERVABILITY.md#alert_login_failed).

### Fixed

- **`BYPASS_WARNING` is now honored everywhere the controller
  dismisses disclaimers**, not just opportunistically inside
  `wait_for_api_port`. Previously `dismiss_post_login_disclaimers`
  (called on initial login, after RESTART, and after re-auth)
  hardcoded a local `SAFE_BUTTONS` list and ignored the env var,
  contradicting README and `FROM_IBC.md` claims. The module-level
  `SAFE_DISMISS_BUTTONS` is now an ordered tuple built once at import
  (built-in defaults first, then `BYPASS_WARNING` additions in
  user-specified order) and consumed by both dismissal paths. Users
  who had `BYPASS_WARNING` set were only getting partial coverage
  before; no behaviour change for users on defaults.

### Non-goals

- `ALERT_LOGIN_FAILED` is a detection signal, not a corrective
  action. The controller still retries login in the usual CCP-backoff
  pattern after emitting the alert; stopping retries automatically
  risks false positives on transient IBKR auth glitches. If the
  alert fires repeatedly and the credentials are genuinely wrong,
  the operator should stop the container to avoid account lockout.

## [0.5.0] - 2026-04-17

### Added

- **Password-expiry dialog handler + `ALERT_PASSWORD_EXPIRED` log token**.
  Closes a real IBC-parity gap: Gateway/TWS surface a "Your password will
  expire in N days" modal after login inside IBKR's rotation window, and
  a "Your password has expired" blocker once the window closes. Without
  a handler, the warning variant could silently pass through (no alert
  to the operator before lockout) and the blocker variant would chew
  through CCP retries on an auth that can't succeed.
  `handle_post_login_dialogs` now recognizes the wording, classifies
  it as `status=warning` (parses `days_remaining` when the dialog
  reports it) or `status=expired` (login-blocking), emits the stable
  grep-contract line
  `ALERT_PASSWORD_EXPIRED status=<warning|expired> mode=<live|paper> [days_remaining=N] suggested_action="..."`,
  and clicks OK/Continue/Acknowledge/Close to dismiss. External
  monitoring (the same `ALERT_*` pipeline as v0.4.8/v0.4.9) can now
  notify the operator to rotate the IBKR password *before* the account
  locks out — a gap IBC's own PasswordExpiryWarningDialogHandler doesn't
  close in the same structured way.
- New `scripts/ibc_config_to_env.py` one-shot migration tool: parses an
  existing IBC `config.ini`, maps each honored key to the equivalent
  ibg-controller env var, and emits `env`, `docker`, or `compose` output.
  Warns on unsupported IBC keys (FIX, CustomConfig, MinimizeMainWindow,
  etc.). Lowers the "rewrite 50 lines of config" barrier for IBC users
  evaluating a switch.
- New `docs/FROM_IBC.md` migration guide: IBC-key → controller env-var
  mapping table, step-by-step cutover recipe, rollback path,
  behaviour-difference notes (command-server auth, per-mode usernames,
  CCP backoff semantics, observability endpoints).
- New CI job `gateway-version-matrix` that builds the shipped
  `Dockerfile` against multiple `UPSTREAM_IMAGE` tags and runs a
  container-level module-load smoke test inside each. Catches breakage
  in the AT-SPI / JRE-bridge wiring when the base image moves across
  Gateway versions, without needing live IBKR creds.

### Non-goals

- The blocking "password has expired" variant still can't be
  auto-recovered by the software — rotation has to happen in IBKR's
  web portal. v0.5.0 makes detection observable; it does not try to
  drive the change-password dialog headless.

## [0.4.9] - 2026-04-17

### Added

- **HTTP `/health` endpoint** on the controller. Motivation: v0.4.8
  made CCP lockouts visible as stable log tokens, but monitoring still
  had to tail docker logs to read them. v0.4.9 adds a first-class
  `GET /health` returning JSON with `status`, `mode`, `state`,
  `jvm_pid`, `jvm_alive`, `api_port`, `api_port_open`,
  `last_auth_success_ts`, `last_auth_success_age_seconds`,
  `ccp_lockout_streak`, `ccp_backoff_seconds`, `uptime_seconds`, and
  `version`. HTTP 200 if `state==MONITORING` AND `api_port_open` AND
  JVM alive, HTTP 503 otherwise. Binds per `CONTROLLER_HEALTH_SERVER_PORT`
  (default 8080 in the image) and `CONTROLLER_HEALTH_SERVER_HOST`
  (default `0.0.0.0` in the image). Served by stdlib
  `http.server.BaseHTTPRequestHandler` in a daemon thread — no new
  Python dependencies. Shallow `GET /ready` also available (always
  200 while the process is running) for Kubernetes-style readiness.
- **`ALERT_JVM_RESTART_EXHAUSTED` log token** — emitted before the
  terminal `sys.exit(1)` in `_escalate_to_jvm_restart` when all
  `_JVM_RESTART_MAX_ATTEMPTS` silent cool-down cycles have failed.
  Format: `ALERT_JVM_RESTART_EXHAUSTED mode=<live|paper> attempts=N reason="..."`.
  See [`docs/OBSERVABILITY.md`](docs/OBSERVABILITY.md) for the
  grep-contract and recommended external-monitor wiring.
- **`ALERT_2FA_FAILED` log token** — emitted in two terminal 2FA
  failure paths: (a) `agent_settext_in_window` or `agent_click_in_window`
  failed while entering the TOTP, (b) `TWOFA_TIMEOUT_ACTION=exit` or
  `TWOFA_TIMEOUT_ACTION=restart` after `do_restart_in_place` failed.
  Format: `ALERT_2FA_FAILED mode=<live|paper> reason="..."`.
- **`_last_auth_success_ts` module state** — set to `time.time()` from
  `_reset_ccp_backoff` on every successful auth. Surfaced via
  `/health` so external monitoring can alert on "logged in at some
  point but hasn't re-authed in hours" (e.g. daily-restart-failed).
- **`__version__ = "0.4.9"` module constant** — exposed in the
  `/health` JSON so deployed versions can be verified without
  shelling into the container.
- **Dockerfile `HEALTHCHECK` directive** — curls
  `scripts/healthcheck.sh` every 30s with a 180s start-period (long
  enough for the initial login pipeline to finish). In `DUAL_MODE=yes`
  the script probes both the live port (default 8080) and the paper
  port (8081) — either failure marks the container unhealthy.
- New apt packages in the image: `curl` for the healthcheck shim.

### Changed

- `docker/run.sh` now mirrors the existing `CONTROLLER_COMMAND_SERVER_PORT`
  dual-mode offset for `CONTROLLER_HEALTH_SERVER_PORT`: paper bumps
  the configured port by one so both controllers can bind inside the
  same container with a single env var.

### Non-goals

- No change to recovery behavior. The /health endpoint is a read-only
  observability surface; no `POST /restart` or similar. Operators
  continue to use the TCP command server (or `docker compose restart`)
  for side-effects.
- Concurrent-session CCP lockouts still require user-side logout to
  clear — the software cannot resolve them. v0.4.9 makes them easier
  to detect (ALERT_CCP_PERSISTENT via /health's `ccp_lockout_streak`)
  but not easier to recover from.

## [0.4.8] - 2026-04-17

### Added

- **Consecutive-CCP-lockout streak counter** with diagnostic messaging.
  2026-04-17 incident: live was stuck in CCP lockout for ~3 hours across
  3 full v0.4.7 escalation attempts. Root cause turned out to be a
  concurrent IBKR session on the web portal holding the auth slot —
  IBKR's CCP silently drops the Gateway's handshake when another session
  is already authenticated. v0.4.6/v0.4.7 silent cool-downs can't clear
  a concurrent session, only user-side logout can. The software worked
  as designed; the diagnosis took hours because the existing log line
  ("IBKR's auth server silently dropped the auth request") didn't hint
  at concurrent-session as a cause.
- After the 2nd consecutive CCP lockout, ``_detect_ccp_lockout`` now
  emits a specific WARNING naming the concurrent-session cause and
  pointing at ``docs/DISCONNECT_RECOVERY.md`` Scenario 7 in the
  ``futures-admin`` repo with the exact recovery steps.
- After the 3rd+ consecutive lockout, ``_detect_ccp_lockout`` emits a
  structured ``ALERT_CCP_PERSISTENT`` ERROR line that external
  monitoring (``futures-admin`` health checker, push-notification
  watchers) can grep on. Format:
  ``ALERT_CCP_PERSISTENT consecutive_lockouts=N mode=<live|paper>
  suggested_action="log out of IBKR web/mobile to release the session
  slot"`` — stable prefix, key=value pairs.
- ``_reset_ccp_backoff`` now also resets the streak counter on any
  successful auth, so the next incident starts at streak=1 again.

### Non-goals

- No behavioral change to the recovery loop itself — the new warnings
  are purely diagnostic. v0.4.7's ``_recover_jvm_or_escalate`` +
  ``_escalate_to_jvm_restart`` still drive recovery the same way.
  Concurrent-session lockouts fundamentally cannot be resolved by the
  software; they require user-side logout.

## [0.4.7] - 2026-04-17

### Fixed

- **`monitor_loop` was silently dropping this mode's JVM on four
  recoverable failure paths.** After v0.4.5/v0.4.6 fixed the CCP
  paths, ``monitor_loop`` still had four ``sys.exit`` calls
  (Gateway JVM exited / wedge do_restart returned False / wedge
  do_restart raised / re-auth failed) that hit the same dual-mode
  trap: the container stays up on the other mode's PID while this
  mode's JVM stays dead, so ``futures-admin`` sees ECONNREFUSED on
  the dangling socat forever.
- Validation 2026-04-17: v0.4.6 paper recovery worked perfectly
  (silent cool-down → port 4002 came up clean), but live JVM
  exited cleanly (code 0) 18min after container start — likely
  IBKR-side session kick or auto-logoff behavior — and
  ``monitor_loop`` ``sys.exit(0)``'d. Container stayed up on paper
  controller's PID, live port 4001 refused from outside.
- Fix: new ``_recover_jvm_or_escalate(reason)`` helper that tries a
  fast in-place JVM restart (``do_restart_in_place``) first — no
  20min wait if the failure isn't CCP-related — and falls through
  to ``_escalate_to_jvm_restart`` (v0.4.6 silent cool-down) only
  when the fast restart can't recover. Never returns False; on the
  exhausted path ``_escalate_to_jvm_restart`` calls ``sys.exit(1)``.
- All four ``monitor_loop`` ``sys.exit`` sites now route through
  either ``_recover_jvm_or_escalate`` (JVM-exited, re-auth-failed:
  fast path worth trying) or ``_escalate_to_jvm_restart`` directly
  (wedge path that already tried ``do_restart_in_place`` once, so
  skip the retry and go straight to cool-down).
- New tests ``TestRecoverJvmOrEscalate`` cover the fast-success,
  escalate-on-False, escalate-on-exception, and propagate-SystemExit
  contracts.

## [0.4.6] - 2026-04-16

### Fixed

- **v0.4.5's long cool-down was not silent from IBKR's perspective.**
  Validation in production (futures-admin dual-mode container,
  2026-04-16): paper escalated to `_escalate_to_jvm_restart` on
  schedule, sat through the full 1200s (20min) cool-down, then called
  `do_restart_in_place`. The relaunched JVM immediately hit CCP
  lockout again — the "Attempt 3: connecting to server" stuck-
  connecting state came back within seconds of the new login. The
  20min cool-down didn't actually clear the CCP limiter.
- Root cause: `_escalate_to_jvm_restart` ran `_apply_ccp_long_cooldown`
  BEFORE `do_restart_in_place`, which meant the old JVM stayed alive
  throughout the cool-down. That JVM's internal "Attempt N: connecting
  to server" retry loop kept making auth attempts against IBKR's
  server during the whole 20min — silence from the controller, loud
  from IBKR's perspective, so the CCP limiter stayed armed the whole
  time. The memory's claim that "this silence lets the limiter reset"
  was false: the controller was silent, the JVM wasn't.
- Fix: split `do_restart_in_place` into `_teardown_jvm_for_restart`
  (kill + clean socket/ready files) and `_relaunch_and_login_in_place`
  (launch + agent + app discovery + handle_login + CCP retry loop +
  post-login + 2FA + disclaimers + wait_for_api_port + signal_ready).
  `do_restart_in_place` itself becomes a thin wrapper that calls both
  (preserving command-server `RESTART` and monitor-loop wedge
  escalation behavior). `_escalate_to_jvm_restart` now calls them
  separately with the cool-down in between:
      teardown → cool-down → relaunch.
  JVM is dead for the full 20min = zero CCP traffic on these
  credentials during the cool-down = limiter actually has a chance
  to reset.
- Not the v0.4.0 feed-the-limiter bug: v0.4.0 was kill+relaunch+retry
  with 60-600s gaps. v0.4.6 is kill+wait_20min+relaunch. Same
  sequencing, vastly longer silent window. The v0.4.0 lesson was
  "don't kill+relaunch quickly", not "don't kill at all" — v0.4.6
  holds the line on both.
- New test `test_teardown_fires_before_cooldown` asserts the call
  order invariant so this never regresses.

## [0.4.5] - 2026-04-16

### Fixed

- **v0.4.4's "escalate to container-level recovery" was a no-op in
  dual-mode containers.** Validation (futures-admin agent, live+paper
  combined container): paper correctly detected the disposed-shell
  state and called `sys.exit(1)`, but the container did NOT restart.
  Root cause is in `docker/run.sh`: dual-mode spawns both live and
  paper controllers as children and ends with
  `wait "${pid[@]}"`. When one child exits, `wait` keeps polling the
  other — the container stays up, docker's restart policy never
  fires, and that mode's Gateway JVM is orphaned. Paper was left
  with a dead JVM on port 4002 and a socat on 4004 respawning with
  `ECONNREFUSED` against it. Live was untouched, so the user could
  trade live but not paper — exactly what happened in the validation
  run.
- Fix: replaced the four `sys.exit(1)` calls on CCP-exhaustion paths
  (two in `main()`'s CCP pre-loop, two in
  `wait_for_api_port_with_retry`) with
  `_escalate_to_jvm_restart(reason)`. The helper does a long CCP
  cool-down (default 1200s = 20min, env `CCP_COOLDOWN_SECONDS`) to
  let IBKR's rate limiter clear, then calls `do_restart_in_place`
  to tear down THIS mode's JVM and relaunch it. The other mode's
  JVM is untouched. Retries up to `_JVM_RESTART_MAX_ATTEMPTS`
  (default 5, env `JVM_RESTART_MAX_ATTEMPTS`) with a fresh cool-down
  on each attempt before finally `sys.exit(1)`'ing — 5 × 20min =
  100min of wall clock at the cap, more than enough for CCP to
  clear if it's going to.
- Why the long cool-down is mandatory before the JVM restart:
  killing+relaunching the JVM without a cool-down is exactly what
  v0.4.0 retired because it feeds IBKR's rate limiter a fresh TCP/
  TLS handshake each cycle and keeps the lockout armed. v0.4.5
  brings JVM restart back ONLY behind the long cool-down and ONLY
  on paths where in-JVM recovery is demonstrably impossible
  (disposed login frame) or has exhausted its cap. The v0.4.0
  "stay in one JVM on auth failure" invariant still holds for the
  common case; v0.4.5 just adds a realistic escape hatch for the
  narrow class where it can't.
- Consistent with `do_restart_in_place`'s existing semantics:
  step 6 of that function includes its own in-JVM CCP retry loop,
  so if the fresh JVM ALSO hits CCP lockout (the limiter hasn't
  fully cleared), it returns False and `_escalate_to_jvm_restart`
  cools down again and tries one more time. Hard-capped so we
  can't spin forever.

## [0.4.4] - 2026-04-16

### Fixed

- **`attempt_inplace_relogin` dead-waited 120s after CCP lockout
  disposed the login frame.** Live + paper validation of v0.4.3 (report
  from futures-admin agent) showed `WAIT_LOGIN_FRAME` correctly timing
  out but on a failure mode v0.4.3's premise didn't cover: the login
  frame isn't occluded by a modal, it's been *disposed*. Gateway's
  main application shell comes up with the File/Configure/Help menu
  bar and "API Server: disconnected" status labels. The captured
  `loginFrame` reference that v0.4.2/v0.4.3's `findLoginFrame` returns
  no longer points at a live Window, and `LoginManager.initiateLogin`
  on a disposed reference is a silent no-op. With the 120s timeout,
  eight retry attempts burn 16 minutes of the CCP backoff budget
  before `wait_for_api_port_with_retry` gives up and escalates.
- Fix: `attempt_inplace_relogin` now probes with a short 2s
  `agent_wait_login_frame` first. If that probe fails and
  `agent_windows()` returns exactly one non-modal window with
  "IBKR Gateway" in its title (the disposed-shell signature), the
  function returns False immediately instead of doing the full 120s
  wait. `wait_for_api_port_with_retry` treats the False return as an
  in-JVM-relogin failure and `sys.exit(1)`'s for container-level
  kill+relaunch — which is the only path that can recover from a
  disposed-frame JVM. The short-circuit only triggers on the specific
  disposed-shell shape; stuck-connecting (login frame present, modal
  progress dialog on top) still falls through to the full 120s wait
  so Gateway's internal ~60s retry cycle can self-clear.
- Consistent with the v0.4.0 "no kill-and-relaunch on auth failure"
  invariant: that rule was premised on the login frame being
  re-enterable via `initiateLogin(capturedLoginFrame)`. When the
  frame is disposed, there is no UI to re-drive; the JVM has lost
  its handle on the login workflow and kill+relaunch is the only
  remaining option. This narrow class escapes the invariant only
  because in-JVM recovery is impossible, not because it's cheaper.

## [0.4.3] - 2026-04-16

### Fixed

- **`attempt_inplace_relogin` timed out before exercising the v0.4.2
  credential-typing fix.** Step 2 of the relogin primitive used
  `wait_for(app, "password text", timeout=30)` to wait for the login
  frame to redisplay. AT-SPI filters the login frame's password-text
  role while Gateway's "Attempt N: connecting to server" modal is up,
  and the modal self-clears after ~60s — so the 30s wait returned
  None and the function exited before `handle_login` (and the v0.4.2
  SETTEXT_LOGIN_USER / SETTEXT_LOGIN_PASSWORD commands) could run.
  Reported by the futures-admin agent after the v0.4.2 deploy cycle:
  live authed cleanly on first attempt so the relogin path wasn't
  exercised, but paper was stuck until this fix.
- Fix: new agent command `WAIT_LOGIN_FRAME <timeout_ms>` in
  `agent/GatewayInputAgent.java` that uses v0.4.2's `findLoginFrame`
  infrastructure (showing Window containing a JPasswordField — stable
  Swing-type invariant) plus a `modalDialogBlocking` check that
  confirms no modal Dialog is overlaying the login frame. Polls every
  200ms until the deadline. Returns OK only when the frame is
  interactable.
- `attempt_inplace_relogin` in `gateway_controller.py` now calls
  `agent_wait_login_frame(timeout_ms=120_000)` instead of
  `wait_for(app, "password text", timeout=30)`. 120s covers one full
  "Attempt N: connecting" retry cycle (~60s) with margin. On timeout,
  logs the output of `agent_windows()` so the next failure mode is
  diagnosable.
- Why Swing's view differs from AT-SPI's: a modal dialog on top of
  the login frame doesn't change `loginFrame.isShowing()` (Window
  visibility is self-rooted — it has no ancestors), but AT-SPI's
  assistive-tech tree prunes the obscured subtree. The Java agent
  runs inside the JVM and sees the Swing state directly; the Python
  controller's pyatspi path doesn't.

## [0.4.2] - 2026-04-16

### Fixed

- **In-JVM relogin iteration 2+ failed at `SETTEXT Username`** on paper-
  side production (observed 17:56:48 UTC-04 on 2026-04-16). v0.4.1's
  outer retry loop correctly routed the stuck-connecting pattern into
  `wait_for_api_port_with_retry` and engaged the 120s CCP backoff, but
  the follow-up `attempt_inplace_relogin` iteration exited with
  `agent SETTEXT 'Username': ERR not_found type=text name=Username`.
  Password field was found (stable `JPasswordField` Swing type);
  Username was not (accessible name mutates after a failed attempt —
  the field can become a JComboBox autocomplete editor whose inner
  JTextField has null AccessibleName).
- Fix: new agent commands `SETTEXT_LOGIN_USER <text>` and
  `SETTEXT_LOGIN_PASSWORD <text>` in `agent/GatewayInputAgent.java`
  that identify the login frame by "contains a JPasswordField" and
  locate fields by Swing type rather than accessible name. The
  commands poll up to 10s for the field to become editable — the
  username field is temporarily disabled during Gateway's "Attempt N:
  connecting to server" retry animation, which the old immediate
  lookup would have missed as well.
- `handle_login` in `gateway_controller.py` now calls
  `agent_settext_login_user` / `agent_settext_login_password` instead
  of `find_descendant` + `set_text` for the credential-typing step.
  The trading-mode selection and "Log In" button click still use the
  AT-SPI path — both of those selectors remain stable across
  attempts.
- Matches IBC's `LoginManager.getUsernameField()` approach (component-
  tree traversal, not accessibility name). See ADR-001 for the broader
  direction this surgical fix sits within.

### Known — not v0.4.2 scope

- Controller readiness 300s timeout starts socat regardless of paper
  auth state, which orphans socat alongside the JVM on `sys.exit(1)`.
  Pre-existing; not a v0.4.1 or v0.4.2 regression. Tracked for a
  future release.

## [0.4.1] - 2026-04-16

### Fixed

- **v0.4.0 recovery loop never iterated on the production lockout
  path**: the in-JVM relogin primitive was added and wired into
  `main()`'s outer CCP loop (gateway_controller.py:2383) and into
  `do_restart_in_place()`, but the lockout pattern observed in paper-
  side production (stuck-connecting retry loop — Gateway's login
  dialog stuck on "connecting to server (trying for another N
  seconds)") emits NO launcher.log `AuthTimeoutMonitor-CCP: Timeout!`
  line, so `_detect_ccp_lockout(timeout=25)` returned False and the
  8-attempt relogin loop never entered. Control flowed into
  `handle_2fa()`, whose `RELOGIN_AFTER_TWOFA_TIMEOUT=yes` branch
  re-drove login exactly once via `handle_login()` (not via
  `attempt_inplace_relogin`), then fell through to
  `wait_for_api_port(timeout=180)`. On timeout, `sys.exit(1)` —
  controller dead, JVM orphaned to PPID=1.
- Fix: added `wait_for_api_port_with_retry(app)` (gateway_controller.py
  next to `attempt_inplace_relogin`). It wraps the final API-port
  wait in an 8-attempt relogin loop: if the port doesn't open and
  EITHER `_detect_ccp_lockout` OR `_detect_login_stuck_connecting`
  returns True, it applies `_apply_ccp_backoff()`, runs
  `attempt_inplace_relogin(app)`, and retries. No lockout signature
  = terminal failure (wrong creds / wrong server / network) with the
  same diagnostic dump as before. Cap exhaustion exits for container-
  level recovery. Replaces the bare `wait_for_api_port` call at
  main() line 2434.
- `handle_2fa`'s `RELOGIN_AFTER_TWOFA_TIMEOUT=yes` branch now calls
  `attempt_inplace_relogin(fresh_app)` instead of `handle_login`
  directly. The characteristic "In-JVM relogin attempt (no JVM
  restart — matches IBC's LoginManager.initiateLogin semantics)"
  warning now fires on this path, so validation scripts can actually
  observe the primitive running. The dismiss-error-modal /
  skip-connecting-to-server-progress-dialog guards also run.
- Unit tests: `TestWaitForApiPortWithRetry` mocks
  `wait_for_api_port`, `_detect_ccp_lockout`,
  `_detect_login_stuck_connecting`, `_apply_ccp_backoff`, and
  `attempt_inplace_relogin` to exercise the immediate-success,
  CCP-retry-then-success, stuck-connecting-retry-then-success,
  no-signature-terminal, cap-exhausted, and relogin-failure
  branches. Also asserts that successful retries clear the backoff
  via `_reset_ccp_backoff`.
- `do_restart_in_place` is still reserved for legitimate process-death
  recovery (monitor-loop wedge at :2894, command-server RESTART at
  :2763, opt-in `TWOFA_TIMEOUT_ACTION=restart` at :1537). Auth-failure
  paths still route through `attempt_inplace_relogin`.

### Known — not v0.4.1 scope

- `AUTO_RESTART_TIME` (gateway_controller.py:1760) is a Gateway-
  internal config value set via the Configure → Lock and Exit dialog
  at post-login time; Gateway itself handles the daily restart. If
  Gateway never authenticates, the post-login config never applies
  and there's nothing for Gateway to auto-restart from. Not a
  controller bug.

## [0.4.0] - 2026-04-16

### Fixed

- **CCP lockout cycle never clears because every retry kills the
  Gateway JVM**: v0.2.2–v0.3.2 added exponential backoff around a
  recovery path that was itself the cause of the problem. Both
  `main()` (cold-start CCP branch, gateway_controller.py:2289 in
  v0.3.2) and `do_restart_in_place()` (CCP-after-relaunch branch,
  line 2606 in v0.3.2) recovered from CCP lockout by calling
  `do_restart_in_place()` — which terminates the Gateway JVM via
  `GATEWAY_PROC.terminate()` and relaunches a fresh one. IBKR's auth
  server treats each new JVM as a fresh TCP/TLS handshake and rearms
  the CCP rate limiter on it, so the exponential backoff ramped
  60→120→240→480→600s forever without ever letting the lockout
  clear. The live instance stayed up once authenticated; paper kept
  cycling.
- Root cause confirmed against
  [IBC's LoginManager.secondFactorAuthenticationDialogClosed](https://raw.githubusercontent.com/IbcAlpha/IBC/master/src/ibcalpha/ibc/LoginManager.java):
  IBC recovers from a 2FA / auth timeout by calling
  `getLoginHandler().initiateLogin(getLoginFrame())` on the **same
  JVM** after a 5-second delay. No process restart. That's why IBC-
  based deployments (gnzsnz/ib-gateway-docker) don't accumulate
  CCP lockouts across retries — the retry reuses the existing auth
  session.
- Fix: added `attempt_inplace_relogin(app)`
  (gateway_controller.py:2010). It does NOT call `launch_gateway`,
  does NOT terminate `GATEWAY_PROC`, does NOT unlink the agent
  socket. It dismisses known login-failure error modals (skipping
  "Connecting to server" progress dialogs, which cancel login if
  clicked), waits up to 30s for the login frame to redisplay, and
  re-drives `handle_login(app)` on the same app reference. Both
  CCP-lockout recovery sites now loop on this primitive with the
  existing exponential backoff between attempts. `do_restart_in_place()`
  is reserved for actual process-death recovery (monitor-loop wedge
  escalation at :2894 and the command-server RESTART at :2763) and
  for the opt-in nuclear `TWOFA_TIMEOUT_ACTION=restart` dispatch at
  :1537. Auth-failure paths no longer touch it.
- Hard cap of 8 in-JVM relogin attempts per controller lifetime
  (`_INPLACE_RELOGIN_MAX_ATTEMPTS`). If the lockout persists that
  long, the controller exits so the container orchestrator's own
  restart policy takes over — better than spinning forever.
- Exponential backoff (v0.2.2) is retained. It was always correct as
  *spacing* between retries; the bug was the accompanying kill+relaunch.
- The v0.3.2 premature-reset gate is retained as defense-in-depth.
  With in-JVM relogin, the gate rarely matters on the cold-start path
  (the loop only exits when CCP is clear), but it still protects
  `attempt_reauth` and the reset points after `handle_post_login_dialogs`.

### Added

- 7 new unit tests for `attempt_inplace_relogin` covering: login
  frame never reappears, handle_login re-drive on same app ref,
  handle_login failure propagation, "Connecting to server" progress
  dialog is never clicked, recognized error modal is dismissed via
  OK/Close, non-modal windows are ignored, and `agent_windows`
  exceptions don't crash the helper.

### Downstream

- `RELOGIN_AFTER_TWOFA_TIMEOUT=yes` is now the recommended setting
  for deployments that can tolerate a single extra in-JVM retry
  during stuck-connecting lockouts. It triggers the same in-JVM
  relogin behavior inside `handle_2fa`'s timeout dispatch. No change
  to `TWOFA_TIMEOUT_ACTION` defaults; leave it unset (default
  "none") or set it to "exit" for explicit container-level recovery.

### Validation

- All 73 unit tests pass (66 from v0.3.2 + 7 new).
- Success criterion in production: after a forced 2FA timeout or CCP
  lockout, `launcher.log` shows recovery via re-click with the same
  Gateway PID throughout, no fresh `AuthTimeoutMonitor-CCP: Timeout!`
  cycles after the first retry, and the backoff ramp plateaus
  instead of running to 600s indefinitely.

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

# Changelog

All notable changes to `ibg-controller` are documented here. The
format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- **`ALERT_AUTO_RESTART` grep-contract token** — `status=adopted`
  (INFO) or `failed_no_agent` / `failed_jvm_exited` / `failed_login` /
  `failed_api_timeout` (WARNING), one line per adoption attempt. See
  [docs/OBSERVABILITY.md](docs/OBSERVABILITY.md).
- **`AUTO_RESTART_ADOPT`** (default `yes`) — set to `no` to restore the
  always-relaunch behaviour; **`AUTO_RESTART_ADOPT_TIMEOUT_SECONDS`**
  (default `90`) — how long to wait for the self-restarted JVM's agent;
  and **`AUTO_RESTART_PROBE_SECONDS`** (default `15`) — how long to
  probe the agent socket when no `restarter.log` was written. The probe
  plus a 5 s late-log grace is the only added latency, and only on clean
  exits.

### Changed

- **Upstream base bumped 10.45.1g → 10.45.1j** (still the gnzsnz
  `:stable` line; digest `sha256:91165c07…`). Three upstream patch
  releases, and on arm64 it drops 112 MB of dead weight: 10.45.1g's
  aarch64 image carried `jxbrowser-linux64-8.9.4.jar`, whose
  `libtoolkit.so` is an x86-64 ELF binary that cannot execute there. It
  was a side effect of the pre-#397 build, which assembled aarch64
  images from IBKR's **x64** installer plus a Zulu JRE. Upstream
  gnzsnz/ib-gateway-docker#397 moved aarch64 to IBKR's native arm
  installer, first available at 10.45.1h.
- The arm64 Java runtime changes with it: 10.45.1g used
  `/usr/local/zulu17…`, while 10.45.1j uses install4j's own JRE
  registry (`/usr/local/i4j_jres/…`) with no Zulu present. The
  `release-image.yml` comment describing arm64 as Zulu-based is
  corrected.
- CI matrix gains 10.45.1j.
- **CI now builds against upstream's `latest` channel as well (issue
  #24).** The `gateway-version-matrix` job gains
  `ghcr.io/gnzsnz/ib-gateway:10.50.1e`, so a layout change on the newer
  Gateway line surfaces here rather than in someone's build. Build and
  boot only — login, 2FA and the dialog handlers are verified against
  10.45.x, and nothing here claims otherwise. Version tags, never the
  floating `:stable` / `:latest`, so PR runs stay deterministic.
- The `Dockerfile` header and `docs/FROM_IBC.md` document building
  against any gnzsnz base, including the `latest` channel, via
  `--build-arg UPSTREAM_IMAGE=`. No repo change is needed to do this;
  the recipe was simply undocumented.

### Fixed

- **Gateway's own daily auto-restart no longer races the controller
  into a cold login (issue #23).** With `AUTO_RESTART_TIME` set,
  Gateway restarts *itself* at that time: the JVM spawns install4j's
  `.install4j/restarter`, which shuts the calling launcher down and
  re-executes it, and the replacement re-uses the session credentials
  — no login, no second factor. `monitor_loop` saw that code-0 exit as
  a crash and `do_restart_in_place` launched a **second** Gateway ~2 s
  before install4j launched its own: two instances on one display and
  one settings dir, the agent drove the wrong window, login failed,
  the controller halted, and the container's restart policy brought
  it back as a cold login — a fresh IB Key push every night (the
  reporter measured 23 container restarts and 1-2 h without a session
  per night over five days; an otherwise identical paper container
  with no auto-restart time ran 13 days clean).
  - On a JVM exit the controller now checks the mtime of install4j's
    `restarter.log` next to the launcher. That file being freshly
    written is the restarter's own statement that it ran. Not a
    wall-clock comparison against `AUTO_RESTART_TIME`, which would have
    to guess which timezone Gateway's Lock-and-Exit field is in and
    would fire on any code-0 exit that landed in the window.
  - **`restarter.log`'s location depends on the working directory.**
    The restarter writes it via `-Dinstall4j.alternativeLogfile=`
    `./.install4j/restarter.log` — a *relative* path, resolved against
    the working directory it inherits from Gateway's JVM. Gateway runs
    with its install directory as the working directory (verified in a
    running container), so in a real auto-restart the log does land
    exactly where this code looks for it. But nothing guarantees that
    for every install, so the log is the primary signal, not the only
    one: after a clean exit with no usable log the controller asks a
    more direct question for up to `AUTO_RESTART_PROBE_SECONDS`
    (default 15) — is a live Gateway JVM we never spawned already
    answering on our agent socket? Only a running JVM can answer it,
    and launching a second instance in that state is exactly this bug.
  - **The restarter is itself a JVM, and it loads our agent.** Verified
    2026-09-06 by running the real `.install4j/restarter` binary: it
    inherits `INSTALL4J_ADD_VM_PARAMS` from the JVM that spawned it, so
    it loads `-javaagent:gateway-input-agent.jar`, binds the agent
    socket, and answers `GET_PID` with *its own* PID for the couple of
    seconds it lives. Adoption now identifies it by the
    `-Dinstall4j.alternativeLogfile` flag that Gateway's own JVM does
    not carry (`i4jruntime.jar` is not a discriminator — Gateway's
    cmdline contains it too) and waits for the Gateway JVM it launches
    instead of adopting a process that is about to exit. Seeing the
    restarter on the socket is itself conclusive evidence of a
    self-restart. `ALERT_AUTO_RESTART` reports which signal fired via
    `detected_via=restarter_log|agent_socket|install4j_restarter`.
  - If it was just written: no relaunch. The controller waits for the
    instance install4j is bringing up (it inherits
    `INSTALL4J_ADD_VM_PARAMS` through the restarter, so its agent binds
    the same socket and reports its PID), adopts it as `GATEWAY_PROC`
    through a Popen-shaped stand-in (`os.kill(pid, 0)` liveness,
    zombie- and PID-reuse-aware via `/proc`), and resumes monitoring
    as soon as the API port is open. The reporter's equivalent patch
    in their production: three seconds, no login dialog, no push, no
    container restart.
  - If the session was **not** preserved and a login dialog appears
    instead (e.g. IBKR's weekly full re-authentication), the existing
    re-auth pipeline is driven on the adopted JVM.
  - If the first JVM to answer dies before its API port opens, the
    controller looks once more for another instance from the restarter
    chain before giving up, and never re-offers a PID it already found
    dead.
  - Fail-safe: no `restarter.log`, a stale or far-future one, one that
    hasn't changed since the last adoption attempt (so a JVM that dies
    inside the 120 s freshness window is treated as a real failure, not
    another self-restart), no new PID within
    `AUTO_RESTART_ADOPT_TIMEOUT_SECONDS` (default 90), or an API port
    that never opens — each falls through to the previous behaviour
    (`do_restart_in_place`), tearing down a half-adopted instance first
    so it isn't left running alongside the relaunch.
  - Adoption is ordered ahead of the v0.5.10 maintenance-window guard:
    `AUTO_RESTART_TIME` commonly sits inside that window, and an
    8-minute sleep is pointless when there is nothing to re-auth. The
    guard still applies on the branch that *does* re-auth (session not
    preserved).
  - Reported, diagnosed, and prototyped in production by
    @maciejlaska.

### Fixed during validation

- **A dead adopted JVM could look alive indefinitely.** `_AdoptedProcess`
  inferred liveness from `os.kill(pid, 0)` plus `/proc`. Where `/proc`
  is absent, or the exit is left unreaped, a dead JVM still answered
  "alive", so teardown spent its full `JVM_TEARDOWN_GRACE_SECONDS`
  SIGTERM wait and then a SIGKILL on a process that had already exited —
  turning a 6 s failure path into a 40 s one and emitting a spurious
  `ALERT_JVM_UNCLEAN_SHUTDOWN`. Liveness now attempts a `WNOHANG` reap
  first (a no-op in production, where the JVM is install4j's child, not
  ours) before the signal and `/proc` checks. Caught by the end-to-end
  tests below, not by the mocked unit tests.

### Validation

- New unit coverage: `_install4j_restarter_age` (fresh / stale /
  missing / future-dated log, unknown launcher, discovery fallback),
  `_AdoptedProcess` against real child processes (live, terminated,
  killed, gone-before-adoption, wait timeout; unreaped-zombie and
  `/proc` parsing on Linux), `_adopt_self_restarted_gateway`
  orchestration (not a self-restart, grace re-check for a late
  restarter write, no grace on non-zero exits, no new agent, adopted
  via API port, login dialog re-driven, the maintenance guard applying
  to that re-login, login failure and API timeout leaving the adopted
  instance in place for teardown, adopted JVM dying, retry onto a
  second candidate from the restarter chain, the same restarter.log
  not being adopted twice, disclaimer dismissal while waiting),
  `_recover_jvm_or_escalate`
  ordering (adoption before the maintenance guard and the fast
  restart; half-adopted teardown; exception fall-through; env
  kill-switch; alive-JVM skip; unobservable exit status treated like
  0 by the guard), and the `attempt_reauth` / `_redrive_login` split.
- **End-to-end coverage with real processes**
  (`TestAutoRestartAdoptionEndToEnd`): a stand-in Gateway JVM binds the
  agent's Unix socket the way the real agent does and optionally opens
  an API port, so adoption runs against real PIDs, real signals, a real
  socket and the real `_AdoptedProcess` — only Gateway itself is
  substituted. Covers the self-restart being adopted with no second
  launch, adoption via the socket probe when no `restarter.log` is
  written, a genuine crash still falling through to the relaunch inside
  a bounded detection budget, a stale log being ignored, an adopted JVM
  dying before its API port, and the same restart not being adopted
  twice.
- **The load-bearing assumption is now verified rather than assumed.**
  The fix rests on the restarted Gateway inheriting
  `INSTALL4J_ADD_VM_PARAMS`, hence loading the agent and reporting a new
  PID on the same socket. Running the real `.install4j/restarter` binary
  in a container off the release image confirms the inheritance
  directly: given a deliberately bogus `-javaagent` value the
  restarter's own VM refused to start, and given the real agent jar it
  logged `[gateway-input-agent] listening on <socket>` and answered
  `GET_PID`. The environment does propagate through the restarter chain.
- Path assumptions checked against a real install (Gateway 10.45.1g in
  the maintainer's running container): `.install4j/restarter` sits where
  the code looks for it, Gateway's working directory is the install
  directory the relative log path resolves against, and a deployment
  using `AUTO_LOGOFF_TIME` rather than `AUTO_RESTART_TIME` has never
  written a `restarter.log` — the unchanged-behaviour case.
- **Driven against a real Gateway and the real restarter**
  (`tests/integration/gateway_autorestart_drill.py`, 17/17 on Gateway
  10.45.1g, 2026-09-07). The drill launches Gateway through its real
  install4j launcher with the real agent, invokes
  `.install4j/restarter` with the environment Gateway itself carries,
  and runs the recovery path against whatever comes up. Observed: the
  restarter inherits `INSTALL4J_ADD_VM_PARAMS` and loads the agent; it
  holds the agent socket for about a second between the old JVM dying
  and the replacement binding it (the window the restarter
  discrimination exists for, now observed rather than hypothesised);
  and the controller adopts the replacement — a JVM it never spawned —
  in 0.5 s, with `do_restart_in_place` never called and exactly one
  Gateway left running. No credentials are used and nothing
  authenticates, so no session slot is touched. Not part of `make
  test`; it needs a Gateway install, an X display and ~2 minutes.
- One fact the drill stands in for: with no credentials the replacement
  Gateway sits at its login dialog and never opens its API port, so
  "the session is preserved across the restart" is asserted rather than
  observed. That is IBKR behaviour the controller neither causes nor
  changes, documented by IBC and reported in production by the issue
  reporter. `monitor_loop`'s adopted-exit branch is exercised only
  indirectly, through `_recover_jvm_or_escalate`.

- `tests/integration/gateway_autorestart_drill.py` run against a
  controller image built on 10.45.1j: **17/17**, so the launcher, agent
  injection and the issue #23 restart-adoption path all work on the
  changed arm64 build. The same drill passes on 10.45.1g and on
  10.50.1e.
- Login, 2FA and the dialog handlers are unchanged by this bump but are
  not re-verified against a real account here; they are exercised on
  10.45.x in production.

## [0.8.1] - 2026-08-24

### Added

- **Passkey/WebAuthn login flows now fail loudly instead of hanging
  (issue #22).** IBKR forced some regions (Hong Kong, Japan as of
  2026-08, possibly wider over time) off TOTP onto passkeys. On such an
  account Gateway launches an in-app browser (jxbrowser) that runs a
  WebAuthn ceremony expecting a hardware security key, rather than the
  Second Factor dialog the controller drives — so with `TWOFACTOR_CODE`
  set the old behavior was to wait out the full `TWOFA_EXIT_INTERVAL`
  and report a generic timeout. `handle_2fa` now detects the passkey
  flow (`_detect_passkey_flow`) and emits `ALERT_2FA_FAILED
  reason="passkey/WebAuthn 2FA flow - unattended login not supported"`
  with remediation, plus a fallback hint on the timeout path when the
  TOTP dialog never appears at all.
  - **Deliberately does not drive the ceremony.** Automating WebAuthn
    means emulating an authenticator (a FIDO2/uhid device or a browser
    debug channel) — a different job than driving Gateway's Swing
    dialogs, out of scope for this stdlib-only tool, and unable to run
    on arm64 regardless. Unattended login isn't possible for a
    passkey-forced account — the resolution is account-side (switch back
    to Mobile Authenticator if IBKR still offers it), attended login via
    VNC, or an authenticator you run yourself alongside the container.
  - Note: on arm64, Gateway currently ships no jxbrowser build, so the
    passkey flow can't run there regardless — an upstream IBKR
    installer gap (tracked in gnzsnz/ib-gateway-docker#440 for the
    dependency side). Reported by @jpike88.

### Validation

- New unit coverage: `_detect_passkey_flow` (signature matching, case
  insensitivity, no false positive on a normal login window set) and a
  `handle_2fa` orchestration test (passkey window → fail-loud with the
  dedicated reason, no TOTP typed). Not live-validated — no
  passkey-forced account available; the window-title signature is
  provisional pending a real `WINDOW` dump (invited in #22). The
  timeout-path fallback diagnostic covers the case where the signature
  doesn't match.

## [0.8.0] - 2026-08-16

### Changed

- **README rewritten as a compact reference (issue #19).** ~700 →
  ~290 lines. The env-var tables, quick start, `stop_grace_period`
  warning, and compatibility table stay; the architecture history,
  CCP-backoff exposition, and troubleshooting essays now live only in
  `docs/` where they always had canonical copies. Claims refreshed
  along the way (multi-method 2FA per #20, agent size, `make test`
  description, stale version examples). Env var names in the README
  table are unchanged (stability contract).

- **`ibc_config_to_env.py` now maps `TwoFactorDevice` /
  `SecondFactorDevice` → `TWOFA_DEVICE`.** The converter previously
  called these "handled implicitly — no env var needed", which has
  been wrong since v0.7.0 started honoring `TWOFA_DEVICE` on
  multi-method accounts. Tests updated to pin the mapping.

- **CI and release workflows pin actions to commit SHAs** (tag noted
  in a comment on each line) instead of mutable version tags —
  removes the last trust-a-moving-tag link in the supply chain. No
  behavior change at current versions.

- **Upstream Gateway pin bumped 10.45.1c → 10.45.1g.** Both are on
  gnzsnz's `:stable` line (`:stable` resolved to 10.45.1g at the time of
  this change); the previous pin had fallen a few patch revisions behind.
  Same 10.45 minor, so dialog behavior is unchanged — the 2FA/login UI
  was validated on 10.45.1c and the CI build matrix now exercises both
  10.45.1c and 10.45.1g.

### Added

- **Multi-method 2FA: the device-selector dialog variant is now
  detected and driven (#20, #21).** Gateway 10.45.x has TWO
  account-dependent shapes for the "Second Factor Authentication"
  dialog: the pre-defaulted code dialog v0.7.0 handles (issue #7
  spike), and a real device selector — a "Select second factor device"
  JTextArea heading over a `JList` of the account's methods, IB Key
  pre-selected (issue #20 ground truth; this is the shape IBC's
  `SecondFactorDevice` targets). The heading is invisible to `LABELS`
  (JLabel-only), so v0.7.0's method-prompt guard couldn't see it. The
  controller now detects the selector via the `WINDOW` component dump
  (`_twofa_selector_present`), selects `TWOFA_DEVICE` with the new
  `JLIST_SELECT` agent command, clicks OK, and polls for the
  "Enter <method> code" prompt before typing the TOTP.
  - **Honest scope**: on current Gateway the in-dialog switch is
    rejected server-side (#20: the pre-selected method's challenge is
    already in flight when the selector opens; 3/3 reproduction on
    10.45.1c). So the practical effect today is a clear failure —
    `ALERT_2FA_FAILED reason="2FA device switch produced no code-entry
    dialog"` with remediation — instead of the old silent
    `2FA handled successfully` followed by an unexplained dead login.
    New agent-failure reasons `"JLIST_SELECT on 2FA device selector
    failed"` / `"CLICK_IN_WIN OK on 2FA device selector failed"` cover
    the drive path itself. If a future Gateway/IBKR change accepts the
    switch, the full selector → code-entry → TOTP flow is already in
    place (exercised against a mock dialog).
  - Contributed by @xuanmingguo (#21) with the decisive component-tree
    dump in #20; integration adds the prompt-based readiness poll, the
    dedicated failure reason, `_twofa_selector_present` + unit tests,
    and doc/contract updates.

- **Login now recognizes the "Invalid username or password"
  credential-rejection modal.** `handle_post_login_dialogs` previously
  left this dialog unhandled and let it fall through; it now emits the
  existing `ALERT_LOGIN_FAILED reason="bad-credentials"` grep-contract
  token and dismisses the modal so the normal CCP-backoff login retry
  proceeds (detection is a signal, not a corrective abort). The in-JVM
  relogin path (`attempt_inplace_relogin`) recognizes the same wording
  too. Gap spotted via @efJerryYang's fork; implemented independently.
- **Image now self-reports its bundled Gateway version (#15, #16).**
  The release image carries a `com.ibg-controller.ib-gateway-version`
  label (plus `org.opencontainers.image.base.name`), so
  `docker inspect` and the GHCR page show the bundled IB Gateway build
  without starting the container. Release notes also state the version
  from now on.

### Fixed

- **`make test` now fails when the unit suite fails.** The unittest
  invocation was piped through `tail -5`, so the pipeline's exit
  status was tail's and a red suite still exited 0 — CI has never
  been able to fail on a unit-test regression. The pipe is gone
  (non-verbose discover output is already compact) and a failing
  suite now propagates. Verified in both directions (green passes,
  sabotaged suite fails).

- **`SETTEXT_IN_WIN` can no longer type into JTextArea headings — the
  actual mechanism of issue #7's "code entered in a weird place"
  (#20, #21).** The command's fallback took the *first JTextComponent
  regardless* when no editable field existed; on the selector dialog
  that was the "Select second factor device" heading, so the TOTP
  replaced the heading text, the agent returned OK, and the controller
  logged `2FA handled successfully` while actually submitting an
  unanswered IB Key push. The fallback now only considers real input
  fields (`JTextField`, which includes `JPasswordField`) and returns
  `ERR` otherwise. Found by @xuanmingguo (#20).

### Docs

- **Multi-method 2FA accuracy sweep across the doc set.**
  `docs/FROM_IBC.md` and `docs/MIGRATION.md` no longer claim
  `TwoFactorDevice` is handled implicitly; `docs/UPGRADING.md` gains
  the next-release section (two dialog shapes, new ALERT reasons,
  agent-jar rebuild note) superseding v0.7.0's "switch control can't
  be driven" claim; `docs/DISCONNECT_RECOVERY.md`'s 2FA scenario adds
  the multi-method root cause and its account-side fix;
  `docs/ARCHITECTURE.md`'s state-machine walkthrough now describes the
  selector detection and method-prompt gate.

- **CONTRIBUTING.md now describes the real testing model.** It
  previously claimed "there is no automated test suite" (stale since
  the suite was introduced; 253 tests today) and the PR template
  referenced "Adding a new..." walkthroughs that didn't exist. The
  Testing section now documents both layers (stdlib unit suite +
  live-validation spikes), and the four walkthroughs (ALERT tokens,
  env vars, dialog handlers, agent commands) are written. The PR
  template no longer hardcodes a test count.

### Validation

- **Live-validated (2026-08-16, dual-mode rc on the maintainer's real
  account, Gateway 10.45.1g)**: the code-dialog (link-variant) happy
  path is unchanged through the new code — selector detection
  correctly no-ops on the real dialog (`2FA method prompt 'Enter
  Mobile Authenticator app code' matches 'Mobile Authenticator app'`
  → TOTP typed → MONITORING, both instances, zero ALERT lines), and
  the login completing confirms the narrowed `SETTEXT_IN_WIN`
  fallback never engages on the real code field. The
  selector/rejected-switch path remains harness-validated only — it
  needs an account that presents the selector (invited in #20/#21).
- `handle_2fa`'s selector orchestration now has direct tests
  (`TestHandle2faSelectorFlow`): switch-rejected → dedicated ALERT
  reason and no code typed; switch-accepted → full selector →
  code-entry → TOTP flow; JLIST failure → fail-loud; no selector →
  v0.7.0 flow untouched (the no-regression invariant). These mirror
  the mock-dialog harness scenarios that validated the #21 merge.
- Harness reproduction (2026-08-14): mock Swing dialogs of BOTH
  real-world variants (component trees per the #20 dump and the
  2026-05-29 spike dump) driven by the real agent jar and the real
  `handle_2fa`. Pre-fix code reproduced the silent failure end-to-end
  (`OK` + heading corrupted + OK clicked with IB Key selected +
  "2FA handled successfully"); post-fix code fails loud on the selector
  (dedicated ALERT reason) and is byte-identical in behavior on the
  link-variant happy path. `make test` green (249 tests).
- NOT yet exercised live: the rejected-switch path on a real
  multi-method account (the #20 reporter's account shape — asked in
  #21) and a re-run of the link-variant happy path on real Gateway
  (futures-admin box) to confirm the code field is picked by the
  primary editable-field selector, not the narrowed fallback.

## [0.7.0] - 2026-05-30

### Fixed

- **Multi-method 2FA no longer mis-types the TOTP into the wrong method
  (issue #7).** On an IBKR account with more than one second-factor
  method enabled, Gateway pre-defaults the Second Factor dialog to one
  method and shows an "Enter `<method>` code" prompt. Previously the
  controller typed the TOTP regardless of which method the dialog was
  asking for — so if Gateway defaulted to IB Key (not the TOTP/Mobile
  Authenticator method), the code went into the wrong control and login
  failed silently. The controller now **reads the dialog's method
  prompt** and:
  - types the code when the prompt matches the method `TWOFACTOR_CODE`
    satisfies (default `Mobile Authenticator app`, overridable via the
    IBC-compatible `TWOFA_DEVICE`); else
  - **fails loudly** with `ALERT_2FA_FAILED reason="2FA method mismatch"`
    and a remediation line (set your IBKR preferred method), instead of
    mis-typing.
  - **Lenient by design:** an unrecognized or absent prompt falls
    through to the existing "type the code" behavior, so single-method
    accounts and any dialog wording we don't recognize are unchanged —
    no regression.

  Scope note: this fixes the *silent failure* and gives clear guidance.
  Automated *switching* to a non-default method (driving Gateway's
  "Change input method" control) is **not** included — a live spike
  (2026-05-29) established that the real 10.45.1c UI is a defaulted code
  dialog + a hidden "Change input method" link, not the JList chooser an
  earlier draft assumed; driving that control needs data we don't yet
  have. Tracked under #7.

### Security

- **launcher.log diagnostic echo now redacts account numbers.** The
  auth-failure diagnostic logs the last 10 lines of Gateway's
  `launcher.log` at ERROR; those lines now pass through `_redact_logs`
  (DU/U account-number masking). Gateway already redacts the password
  in launcher.log, so this closes the residual account-number exposure
  in a path that fires exactly when users collect logs for a bug report.

### Changed

- `TWOFA_DEVICE` is now honored (the method-match check above) instead of
  being a silent no-op; the prior misleading comment in
  `_warn_unsupported_env_vars()` is corrected.

### Validation

- 237 unit tests pass, incl. coverage for `_resolve_twofa_device`,
  `_twofa_requested_method` (incl. window-scoped extraction from a
  realistic full label set), and `_twofa_method_mismatch` (match,
  positive-mismatch, lenient no-prompt/no-desired — guaranteeing no
  single-method regression).
- `make test` green end-to-end. No Java agent change — pure Python,
  reusing the existing `LABELS` command.
- The prompt is read via `agent_labels()` (no arg) and scoped by window
  TITLE in `_twofa_requested_method(window_substr=...)`. A first spike
  (rc1) showed the detection didn't engage because the call passed the
  window substring to `agent_labels()`, which filters by label TEXT —
  the prompt text doesn't contain "Second Factor", so it was dropped.
  Fixed; the agent's recursive label walk does reach the nested prompt.
- **Live-validated (2026-05-30 spike, real multi-method account):** the
  happy path engaged correctly —
  `2FA method prompt 'Enter Mobile Authenticator app code' matches
  'Mobile Authenticator app'` logged before the code was typed, login
  reached MONITORING, no regression, no CCP lockout.
- **Mismatch / fail-loud path is unit-tested + code-verified, not yet
  exercised live** (would require an account defaulting to a non-TOTP
  method). It is strictly safe: a positive mismatch returns False with
  `ALERT_2FA_FAILED`, and any unrecognized case falls through to the
  prior behavior — it cannot regress a working login.

## [0.6.3] - 2026-05-11

### Security

- **Plaintext password could be written to logs via a window dump.**
  The in-JVM agent's `WINDOW` dump (`dumpComponentTree`) called
  `getText()` on every `JTextComponent`. `JPasswordField` is a
  `JTextComponent`, and its `getText()` returns the **plaintext
  password** — which the controller had typed into the login frame.
  The login-failure diagnostic in `gateway_controller.py` logs that
  dump of the `"IBKR Gateway"` window at **ERROR level** (always
  emitted, not gated on `CONTROLLER_DEBUG`), so on a login-button
  click failure the IBKR password could land in `docker logs` — the
  very output the bug-report template asks users to attach.

  - **Scope / reachability:** not an every-run leak. It required the
    `Log In` / `Paper Log In` click to fail (both agent clicks
    returning false), which triggers the ERROR-level dump while the
    password field is populated. No evidence it was triggered in
    practice; what shipped was the latent path, present in releases
    up to and including **v0.6.2**.
  - **Fix (root):** the agent now masks `JPasswordField` in
    `dumpComponentTree` — it emits `<redacted password len=N>` and
    never calls `getText()` on a password field (the transient
    `getPassword()` char[] is zeroed after reading its length). This
    closes the leak for *all* dump callers at once.
  - **Fix (defense-in-depth):** the login-failure dump and the
    `CONTROLLER_TEST_MODE` dump in `gateway_controller.py` now run
    every line through `_redact_logs` (which strips `DU/U` account
    numbers from window titles).
  - **Hardening (same invariant):** the agent's `GETTEXT` command now
    refuses a `JPasswordField` (returns `ERR refused type=password`)
    so the agent never hands back a password over the socket via any
    command. No controller code path calls `GETTEXT` on the password
    field, so this is non-breaking.

  **If you ran v0.6.2 or earlier:** upgrade, and treat any saved
  controller logs from a *login failure* as potentially
  password-bearing — rotate the password if such logs were shared or
  shipped to an aggregator. Single successful logins did not hit this
  path.

### Validation

- 224 unit tests pass (no behavioral change to tested Python paths;
  the fix is at the agent boundary + two dump callsites).
- `python3 -m py_compile gateway_controller.py`: OK.
- The Java agent change is compile-checked by CI (`make` +
  `gateway-version-matrix`); the masked-dump output is confirmed by
  spike against a live login.

## [0.6.2] - 2026-05-01

### Fixed

- **Dual-mode `_config_open()` race that silently broke post-login
  config on live but not paper.** v0.6.1's diagnostic logging
  revealed the actual failure mode (neither scenario (a)
  env-transmission nor scenario (b) log-attribution from the
  original report — a third scenario (c)): live mode transitions
  `API_WAIT → CONFIG` within ~3 s of the API port opening, with the
  EDT still processing post-2FA tear-down. The `agent CLICK
  'Configure'` succeeded (the JMenu's selected property flipped) but
  by the time the follow-up `agent CLICK 'Settings'` walked
  `Window.getWindows()`, the `JPopupMenu`'s heavyweight peer window
  hadn't been realized yet. The agent reported
  `ERR not_found type=button name=Settings` and post-login config
  silently failed for live (not paper, which skips 2FA and hits the
  same code path with a quiescent EDT).

  Three changes in `_config_open()`:

  1. **Wait for the post-login window stack to settle** before
     clicking `Configure` — no modal dialogs visible, no
     `Authenticating...` window present. Bounded at 5 s; logs and
     proceeds anyway if Gateway is showing a different transitional
     window we haven't explicitly named.
  2. **Inter-click delay between Configure and Settings raised
     from 0.3 s to 1.0 s.** That's the time the EDT needs to
     realize the `JPopupMenu`'s heavyweight peer window when it's
     coming out of a busy state. Empirically 0.3 s was enough on a
     quiescent EDT (paper) and not enough on a busy one (live
     post-2FA).
  3. **Outer retry loop** — up to 3 attempts to open the dialog,
     1 s between attempts. Backstop for any other transient that
     hasn't shown up in the logs yet.

  Net cost on the success path is ~1 s (the longer inter-click
  delay; the window-settle wait returns immediately when already
  settled). For the failing case, post-login config now applies on
  live in dual-mode runs.

### Validation

- 224 unit tests pass (no test changes — the `_config_open` retry
  logic is at the agent-socket boundary, exercised by integration
  not unit tests).
- `python3 -m py_compile gateway_controller.py`: OK.
- The repro setup from the original bug report
  (`TWS_USERID + TWS_USERID_PAPER + READ_ONLY_API=no` in dual mode)
  is the spike target for confirming the live-mode fix lands.

## [0.6.1] - 2026-05-01

### Fixed (diagnostic)

- **Dual-mode log attribution.** Pre-v0.6.1 the controller's log
  format was `<time> [<LEVEL>] <message>` with no mode marker. In
  dual mode (`TRADING_MODE=both`) two controller processes — one
  `live`, one `paper` — both write to the same container stdout,
  so `docker logs <container>` interleaved their lines with no way
  to tell them apart. Reports like 2026-05-01's "post-login config
  ran for paper but not live" were impossible to confirm or refute
  from logs alone. The format is now
  `<time> [<LEVEL>] [<mode>] <message>`. The mode is fixed at
  controller-process startup (TRADING_MODE doesn't change for the
  life of a controller), so this is a per-process baked prefix —
  no per-call overhead.

  In single-mode runs the prefix is `[live]` or `[paper]` —
  informative but non-noisy. In dual-mode it makes log streams
  decisively attributable.

  Note for monitoring consumers: any log-grep that anchors strictly
  on `^<timestamp> [<LEVEL>] <token>` (the regex starts at the
  level bracket and expects the token immediately after) will need
  to accommodate the new `[<mode>] ` segment between them. Greps
  that match on `ALERT_*` tokens elsewhere in the line are
  unaffected — the ALERT tokens themselves already include
  `mode=<value>` per `OBSERVABILITY.md`'s grep-contract.

- **`handle_post_login_config()` now logs the env-var values it
  observed, on both the apply and the skip paths.** Pre-v0.6.1
  the function emitted only `"Post-login config: no supported
  env vars set, skipping"` on the early-return — silent on
  exactly which env vars were empty. With dual-mode log
  attribution above, a single new diagnostic line tells you
  precisely what the controller saw:

  ```
  HH:MM:SS [INFO] [live] Post-login config env:
    TWS_MASTER_CLIENT_ID='', READ_ONLY_API='no' (coerced=False),
    AUTO_LOGOFF_TIME='', AUTO_RESTART_TIME=''
  ```

  These knobs are not secrets (they're IBC-equivalent post-login
  API settings) so logging the values is safe.

### Why a diagnostic-only patch and not a fix

The 2026-05-01 bug report ("paper sets Read-Only API = False but
live silently skips") fits two scenarios that pre-v0.6.1 logs
couldn't distinguish: (a) a real env-var transmission failure to
the live controller, or (b) misattribution of two interleaved
dual-mode log streams. v0.6.1's mode prefix + env-var dump
discriminates between them on the next reproduction. If (a) is
real, the next dual-mode log will show
`[live] Post-login config env: ... READ_ONLY_API='' ...` —
smoking gun. If (b), both `[live]` and `[paper]` lines will show
the same env values and the report dissolves.

### Validation

- 224 unit tests pass (no test changes).
- `python3 -m py_compile gateway_controller.py`: OK.
- Module-load smoke test: format string interpolates TRADING_MODE
  cleanly at startup; no per-message overhead.

## [0.6.0] - 2026-05-01

### Removed (breaking)

- **`USE_PYATSPI2_CONTROLLER` deprecated alias.** The env var was
  renamed to `USE_IBG_CONTROLLER` in v0.5.13 with the old name
  honored as a deprecated alias (with a startup warning). v0.6.0
  removes the alias entirely. Containers that still set
  `USE_PYATSPI2_CONTROLLER=yes` without `USE_IBG_CONTROLLER=yes`
  will fall through to the IBC path (which is the documented
  default behavior for an unset toggle).

  Why now and not later: the project is pre-1.0 with no
  production deployments outside the maintainer's own stack, so
  the cost of a breaking change is at its minimum right now.
  Letting the alias linger creates "two env vars for the same
  toggle" cruft that compounds over time. SemVer-wise, removing
  a public env var is a breaking change and `0.5.x → 0.6.0` is
  the project's stated boundary for that
  (per `docs/UPGRADING.md`).
  Migration: rename `USE_PYATSPI2_CONTROLLER` to
  `USE_IBG_CONTROLLER` in your `docker-compose.yml` / `.env` /
  `docker run -e` invocation. v0.5.13 and v0.5.14 both shipped
  the deprecation warning, so anyone who watched their logs has
  had two release cycles to migrate.

- The alias-handling block at the top of `docker/run.sh` (the
  `if [ -z "${USE_IBG_CONTROLLER:-}" ] && [ -n "${USE_PYATSPI2_CONTROLLER:-}" ]; then ... fi`
  block plus the explanatory comment).
- The "Backwards compatibility" callout in `docs/MIGRATION.md`.
- The deprecated-alias parenthetical in `docs/FROM_IBC.md`.
- The `<!-- or USE_PYATSPI2_CONTROLLER if you're still on the
  deprecated alias -->` HTML comment in
  `.github/ISSUE_TEMPLATE/bug_report.md`.

### Changed

- `docs/MIGRATION.md`'s description of the new env var now points
  forward at `UPGRADING.md#v060` for the rename history rather
  than carrying the migration text inline.

### Validation

- `python3 -m unittest discover -s tests`: 224 tests pass.
- `python3 -m py_compile gateway_controller.py`: OK.
- `bash -n docker/run.sh`: OK.
- `grep -r USE_PYATSPI2_CONTROLLER`: only matches in
  CHANGELOG.md and UPGRADING.md (historical record).

## [0.5.14] - 2026-04-28

### Removed

- **Dead AT-SPI tree-walking helpers** in `gateway_controller.py`,
  along with the `gi.repository.Atspi` import they required.
  v0.5.12 left these in place as dead code with zero live callers
  (everything routed through `agent_*`); v0.5.14 deletes them.
  Specifically:
  - `find_descendant(node, ...)`, `wait_for(app, role, ...)`,
    `get_states(node)`, `_dump_tree(node, ...)`, `_read_text(node)`,
    `set_text(node, text)`, and the `click(node)` AT-SPI overload.
  - `import gi`, `gi.require_version("Atspi", "2.0")`, and
    `from gi.repository import Atspi` from the module preamble.
  - The `_STATE_NAMES`-style `Atspi.StateType.*` mapping inside
    `get_states` (gone with the function).
- **The runtime apt install of `python3-gi`, `gir1.2-atspi-2.0`,
  and `at-spi2-core`** in the `Dockerfile`. These were only kept
  alive in v0.5.13 because the controller's module-load imports
  needed the typelib and `libatspi.so.0`. With the imports gone,
  the runtime layer is now just `python3 matchbox-window-manager
  curl` on top of the upstream `gnzsnz/ib-gateway` base.
- The `python3-gi` / `gir1.2-atspi-2.0` / `at-spi2-core` apt install
  in the `smoke-test-image` CI job. The job's runner is now vanilla
  python3, and the import succeeding without those packages is the
  regression guard.
- The `sys.modules` gi-stack mock in `tests/test_pure_logic.py`
  (no longer needed; the controller imports cleanly on any host).

### Changed

- `_StubApp` renamed to `_AppHandle`. Old class exposed a full
  pyatspi-Accessible-like surface (`get_role_name`,
  `get_state_set`, `get_child_count`, `get_child_at_index`,
  `get_description`) for the now-deleted tree-walking callers.
  The new class carries only the two methods callers actually
  use: `get_name()` and `get_process_id()`. Behavior unchanged.
- `CONTROLLER_TEST_MODE=1`'s post-Log-In dump switches from
  `_dump_tree(app)` (which walked the empty AT-SPI tree post-v0.5.12
  and produced nothing useful) to dumping the live Swing window
  state via the agent's `WINDOW` command. The dump is finally
  informative again.
- `_AppHandle`, `find_app`, `handle_login`, the gateway-version-matrix
  CI step's comments, and Dockerfile / CONTRIBUTING / MIGRATION /
  UPGRADING docs all rewritten to reflect post-v0.5.14 reality.

### Validation

- `python3 -m unittest discover -s tests`: 224 tests pass.
- `python3 -m py_compile gateway_controller.py`: OK.
- Bash syntax checks pass.
- Greps confirm zero `Atspi.`, `import gi`, or `gi.require_version`
  references remain in `gateway_controller.py`.

## [0.5.13] - 2026-04-28

### Removed

- **AT-SPI / ATK install steps and JRE accessibility-bridge
  configuration.** v0.5.12 disabled the bridge in the JVM after a
  thread-dump-confirmed deadlock; v0.5.13 removes the now-vestigial
  install steps the README and MIGRATION docs were still telling
  users to add to their images.
  - `Dockerfile`: drop `libatk-wrapper-java`, `libatk-wrapper-java-jni`,
    `dbus-x11` from apt install. Drop the `RUN` block that writes
    `$JAVA_HOME/conf/accessibility.properties` and copies
    `libatk-wrapper.so` into `$JAVA_HOME/lib/`.
  - `docker/run.sh`: drop `start_dbus_session` and `start_atspi`.
    Drop the AT-SPI teardown branch in `stop_ibc`. The controller
    still needs `start_window_manager` (matchbox) for Xvfb focus
    routing — that stays.
  - `gateway_controller.py`: drop the
    `-Xbootclasspath/a:/usr/share/java/java-atk-wrapper.jar` JVM
    arg (the JAR is no longer in the image).
    `-Djavax.accessibility.assistive_technologies=` stays as
    defense-in-depth in case a base image ships the JAR
    pre-installed.
- Image surface area for AT-SPI runtime infrastructure
  (`at-spi-bus-launcher`, `at-spi2-registryd`, the `org.a11y.Bus`
  D-Bus services) is no longer started. `python3-gi`,
  `gir1.2-atspi-2.0`, and `at-spi2-core` remain installed because
  `gateway_controller.py` still does
  `from gi.repository import Atspi` at module load (the helper
  functions that reference `Atspi.StateType.*` / `Atspi.Text.*` /
  `Atspi.Action.*` have no live callers post-v0.5.12 but were not
  physically removed in this release; that's a future cleanup).

### Changed

- **Env var rename, backwards compatible.**
  `USE_PYATSPI2_CONTROLLER=yes` is renamed to `USE_IBG_CONTROLLER=yes`.
  The old name is honored as a deprecated alias — `run.sh` checks for
  it at startup and copies the value across, printing a one-line
  warning. Existing `docker-compose.yml` files keep working without
  modification. Rename at your leisure.
- README, `docs/MIGRATION.md`, `docs/ARCHITECTURE.md`,
  `docs/CONTRIBUTING.md`, `docs/UPGRADING.md`,
  `docs/BOOTSTRAP.md`, `docs/FROM_IBC.md`, `scripts/install.sh`,
  `Makefile`, and the `agent/GatewayInputAgent.java` javadoc were
  rewritten to reflect post-v0.5.12 reality. Historical context
  (the AT-SPI deadlock that motivated the change, the v0.5.12
  triage signature, the spike findings about why external input
  mechanisms fail) is preserved as labeled history.
- `docs/ARCHITECTURE.md` collapses the four-section AT-SPI / D-Bus /
  JRE-config block into a single historical-context callout. The
  remaining "things that surprised us" entries about AT-SPI tree
  staleness and the on-screen-keyboard `Action` interface are
  retained as record of the 2026-Q1 spike.

### Validation

- `python3 -m py_compile gateway_controller.py` — OK.
- `bash -n docker/run.sh` — OK.
- Greps for `at.spi`, `atspi`, `ATK`, `atk-wrapper`, `pyatspi`,
  `libatk`, `AtkWrapper`, `USE_PYATSPI2` across the repo show only
  intentional historical mentions (CHANGELOG, post-mortem callouts)
  or the deprecated-alias plumbing in `run.sh`.

### Issue resolved

- [#1](https://github.com/code-hustler-ft3d/ibg-controller/issues/1) —
  README should no longer mention AT-SPI / ATK approach for the sake
  of clarity.

## [0.5.12] - 2026-04-27

### Fixed

- **Dual-mode "CCP lockout" was a misnamed intra-JVM deadlock.**
  SIGQUIT thread dumps on hung live JVMs (PID 1183, 2026-04-27 11:45)
  showed `JTS-Login-14` parked in `AtkUtil.invokeInSwing.get()` while
  `AWT-EventQueue-1` was itself stuck in `AtkWrapper$6.dispatchEvent`
  waiting for a synchronized monitor the same wrapper held
  reentrantly. `java-atk-wrapper`'s java↔native bridge is not safe
  for re-entrant Swing event dispatch from `JProgressBar.setValue`
  (the login progress bar). `JTS-CCPListenerS2` then couldn't
  acquire the connection-state lock to receive `NS_AUTH_START` from
  IBKR — and after 20 s `AuthTimeoutMonitor-CCP` fired `Timeout!`
  locally, which the python misinterpreted as `CCP LOCKOUT
  DETECTED`. **IBKR was never reached.** Single-mode runs deadlocked
  too — this is intra-JVM, not cross-JVM.
- **Fix: disable the AT-SPI bridge entirely.** Added
  `-Djavax.accessibility.assistive_technologies=` (empty value) to
  the JVM args in `launch_gateway()`. The empty system property
  overrides the JRE's `accessibility.properties` file setting;
  `AtkWrapper` is never instantiated; the deadlock is structurally
  impossible.
- **Coupled python changes** (AT-SPI no longer reachable from JVM):
  - `find_app()` now returns a `_StubApp` carrying the
    agent-reported JVM PID instead of polling the AT-SPI desktop
    tree (which a bridge-disabled JVM never populates). The stub
    keeps a minimal pyatspi-Accessible-like surface so the handful
    of callsites that still pass `app` around for logging or as a
    passthrough argument keep working without per-callsite
    branching.
  - `handle_login()` uses agent-socket commands exclusively
    (`SETTEXT_LOGIN_USER`, `SETTEXT_LOGIN_PASSWORD`, `JCHECK`,
    `CLICK`, `WAIT_LOGIN_FRAME`). `gateway-input-agent.jar` is pure
    Swing/AWT (`Window.getWindows()` + `SwingUtilities.invokeAndWait`)
    and is unaffected by the AtkWrapper disable.
- **JVM stdout/stderr now captured** to
  `/tmp/jvm_console_${TRADING_MODE}.log` so SIGQUIT thread dumps can
  be read after the fact. Prior to v0.5.12 the JVM streams went to
  `/dev/null`.

### Validation

- Two consecutive container restarts (Apple Silicon, Darwin
  arm64-on-amd64-via-rosetta), both modes reached MONITORING with
  `ccp_lockout_streak=0`. Live: 31 s `APP_DISCOVERY` → MONITORING.
  Paper: 32 s. JVM cmdlines confirmed to carry the new flag. Both
  `/health` endpoints reported
  `version=0.5.12, state=MONITORING, ccp_lockout_streak=0,
  ccp_backoff_seconds=0`.
- One transient paper failure observed on the first restart wave —
  no `JTS-Login-14` parked in `AtkUtil.invokeInSwing` in the JVM
  thread dump, just the standard 20-second `AuthTimeoutMonitor-CCP`
  fire. Looks like an IBKR-side login race rather than a regression
  of the deadlock. Paper retried automatically and reached
  MONITORING ~50 s later.

### Superseded

- The earlier hypothesis that two JVMs sharing one
  `at-spi2-registryd` caused the deadlock was wrong. The deadlock is
  intra-JVM: `AtkWrapper` holds a monitor reentrantly during a
  single Swing dispatch. Single-mode runs deadlocked too. The
  per-controller D-Bus / AT-SPI registry split that had been
  prototyped during the debugging session was **removed** before
  this release; the AtkWrapper disable is the only load-bearing fix
  and adding the registry split would have been moving parts not
  justified by root cause.
- v0.5.11's `stop_grace_period: 90s` requirement still applies for
  clean shutdown but is unrelated to this bug.
- v0.5.9's `CCP_LOCKOUT_MAX_JVM_RESTARTS=0` halt-by-default behavior
  remains correct as a safety guard, but is now rarely reached
  because the underlying deadlock has been removed.

### Known follow-ups (not in this release)

- `ALERT_CCP_PERSISTENT_HALT` text still says "log into IBKR Mobile
  to force-log-out the held slot" — wrong for this failure mode.
  Detect: if no `AuthDispatcher.connect` thread spawned in the JVM
  logs, the hang was ours, not IBKR's. Rewording deferred to a
  docs-only patch.
- `CCP LOCKOUT DETECTED` warning name is also misleading; consider
  renaming or splitting into `JVM_LOGIN_DEADLOCK` vs
  `CCP_AUTH_TIMEOUT` in a future release.

## [0.5.11] - 2026-04-27

### Fixed

- **The v0.5.6 clean-logout pipeline never actually worked
  end-to-end before this release.** Two latent bugs masked it:
  - **`docker/run.sh` shutdown sequence was upside-down.**
    `stop_ibc()` killed Xvfb / AT-SPI / socat / x11vnc *before*
    SIGTERMing the gateway controllers. By the time the controller's
    shutdown handler tried to drive a clean logout via
    `_attempt_clean_logout`, AWT's `WINDOW_CLOSING` dispatch had no
    live X11 connection to deliver to Gateway's `WindowListener` and
    the JVM hung on the dead AWT EventQueue until docker SIGKILLed it.
    Reordered: SIGTERM controllers FIRST, wait up to 60s for clean
    logout + JVM exit, then tear down X11 / AT-SPI / socat / x11vnc.
  - **`_GATEWAY_MAIN_WINDOW_TITLE_SUBSTR` was too narrow.** Was
    `"IB Gateway"` (with a space), but Gateway 10.45.1c's actual main
    window title is `"IBKR Gateway"` (no space after IB), so the
    `findWindowByTitleSubstring` lookup missed every time and clean
    logout fell through to SIGTERM. Widened to `"Gateway"` — the
    stable fragment across all observed variants, and not a substring
    of any other top-level window title (auth dialogs are
    `Authenticating...`, `Second Factor Authentication`, etc.).
- **`_escalate_to_jvm_restart` halt path now drives clean logout
  before `sys.exit(1)`.** Under the v0.5.9 halt-by-default behavior
  (`CCP_LOCKOUT_MAX_JVM_RESTARTS=0`), the controller exited without
  closing Gateway, so docker SIGKILLed the JVM as part of the
  container process-tree teardown. That stranded the IBKR session
  slot for hours and produced a restart-cascade under
  `restart: on-failure`. Now: call `_attempt_clean_logout` first (the
  disposed-login-frame state still carries a top-level Gateway main
  window, so `WINDOW_CLOSING` reaches Gateway's `WindowListener` and
  triggers a real CCP session-close); if that fails, fall back to
  `GATEWAY_PROC.terminate()` with a 30s wait so the JVM at least gets
  to run its shutdown hook before docker SIGKILLs it.

### Added

- **First observed clean shutdown — validation evidence.** Today's
  shutdown produced:
  ```
  ALERT_CLEAN_LOGOUT mode=live pid=36 status=succeeded
    reason="JVM exited cleanly within 15s of WINDOW_CLOSING"
  ALERT_SHUTDOWN     mode=live signal=SIGTERM graceful=true
  ```
  Prior to v0.5.11 the `ALERT_CLEAN_LOGOUT` token only emitted with
  `status=failed_timeout` or `status=failed_unreachable` — every
  other shutdown left a stranded session.

### Docs

- **README quickstart**: new "IMPORTANT: required consumer config"
  callout. Compose users must set `stop_grace_period: 90s` on the
  `ib-gateway` service. Docker's default of 10s is too short for the
  clean-logout chain (60s controller wait + 15s × 2 instances of
  `_CLEAN_LOGOUT_TIMEOUT_SECONDS` + JVM shutdown hook + IBKR
  FIN-ACK margin).
- **`docs/MIGRATION.md`**: new "Shutdown grace period" section with
  the full timing math and a worked compose snippet.

## [0.5.10] - 2026-04-21

### Fixed

- **IBKR daily-maintenance CCP cascade.** 2026-04-20/21 production
  incident: at 23:45:12 ET both live and paper JVMs exited cleanly
  (code 0) under IBKR's daily server-side maintenance window
  (published 23:45-00:15 ET, during which every Gateway/TWS session
  receives a cooperative shutdown). The existing
  `_recover_jvm_or_escalate` "Trying fast in-place restart first" path
  re-auth'd ~8 seconds after each exit. IBKR's auth server had not
  finished draining the prior session, so every re-auth was silently
  dropped — CCP LOCKOUT detected 21 seconds later on both sides.
  The subsequent cascade piled retries onto the still-draining server
  and fired multiple `ALERT_CCP_PERSISTENT_HALT` on both modes; paper
  eventually recovered after ~35 min, live was still halted 60 min
  later. v0.5.10 adds a maintenance-window guard:
  - When the JVM exits with code 0 AND wallclock is inside
    23:30-00:30 `America/New_York` (slightly widened around IBKR's
    published window), sleep `CCP_MAINTENANCE_RECOVERY_DELAY_SECONDS`
    (default 480 = 8 min) before touching IBKR's auth server. The
    delay lets IBKR's server-side session teardown propagate before we
    re-auth.
  - Same guard on cold start — a container booting inside the window
    delays before clicking Log In. `on-failure` restart policies
    otherwise drive a fresh container straight into the same
    cooperative-shutdown landmine.
  - Non-zero exits (crashes, SIGTERM, SIGKILL) bypass the guard —
    they're not maintenance shutdowns and still benefit from the
    fast-restart path.

### Added

- **`ALERT_IBKR_MAINTENANCE_RECOVERY`** log token (INFO-level,
  grep-contract). Emitted once per recovery-path entry when the
  maintenance guard fires. Format:
  `ALERT_IBKR_MAINTENANCE_RECOVERY delay_seconds=<int> mode=<live|paper> reason="..."`.
  Operators can distinguish this benign delay from a real CCP cascade
  (`ALERT_CCP_PERSISTENT` / `ALERT_CCP_PERSISTENT_HALT`).
- **`CCP_MAINTENANCE_RECOVERY_DELAY_SECONDS` env var** (default 480).
  Seconds to sleep after a code-0 exit inside IBKR's maintenance
  window before re-auth. Tune upward if empirical data shows IBKR's
  server-side drain takes longer in your region.
- **`_is_ibkr_maintenance_window(now=None)`** and
  **`_apply_maintenance_recovery_delay(reason)`** helpers. Split out
  so the window predicate is unit-testable without a wall clock.
- **16 new unit tests** in `tests/test_pure_logic.py`:
  `TestIBKRMaintenanceWindow` (9, including midnight-cross boundaries
  and the 23:46 incident timestamp), `TestMaintenanceRecoveryDelay`
  (3), `TestRecoverJvmMaintenanceGuard` (4 — covering code-0 in
  window, code-0 outside, non-zero in window, no exit_code).
  Test total: 208 → 224.

### Docs

- `CHANGELOG.md` — this entry.
- `UPGRADING.md` — v0.5.10 section added (additive bugfix; no ops
  intervention required beyond redeploy).
- `OBSERVABILITY.md` — `ALERT_IBKR_MAINTENANCE_RECOVERY` token
  documented under the ALERT_* grep contract.
- `DISCONNECT_RECOVERY.md` — new scenario covering the
  daily-maintenance cooperative-shutdown pattern and how to recognize
  it in logs.
- `README.md` — `CCP_MAINTENANCE_RECOVERY_DELAY_SECONDS` listed in a
  new "Recovery tunables" subsection.

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

# Contributing to ibg-controller

Thanks for helping. This is a small project — short contribution guide.

## Build and run locally

```bash
# Build the agent jar and stage the controller into dist/
make

# Install directly into a running ibgateway home (for on-host dev)
make install DESTDIR=/home/ibgateway

# Create a release tarball
make release VERSION=0.7.0
```

Requires JDK 17+ (`javac` + `jar`) and `make`. No Maven, no Gradle.

## Runtime requirements (from the `Dockerfile.template` integration)

See `docs/ARCHITECTURE.md` for the full list and why each is needed.
The short version:

- `python3` — runs `gateway_controller.py` (stdlib-only at module load
  as of v0.5.14 — no gi/Atspi/PyGObject required)
- `matchbox-window-manager` — Xvfb has no WM by default; Gateway's
  input routing depends on a focused window

Pre-v0.5.13 the image also installed `python3-gi gir1.2-atspi-2.0
at-spi2-core libatk-wrapper-java libatk-wrapper-java-jni dbus-x11` and
configured `$JAVA_HOME/conf/accessibility.properties` for the AT-SPI
bridge. v0.5.12 disabled the bridge in the JVM (it was deadlocking on
Swing dispatch); v0.5.13 removed the JVM-side bridge install steps;
v0.5.14 finished the cleanup by removing the dead Python helpers that
had kept `python3-gi gir1.2-atspi-2.0 at-spi2-core` alive in the
apt install. If you're rebasing from an older fork, drop those
packages and the JRE accessibility.properties write when you bring
your image forward.

## Code layout

```
agent/                    ← In-JVM Java agent (~650 lines)
  GatewayInputAgent.java
  manifest.mf
docs/                     ← User-facing docs
  ARCHITECTURE.md         ← Design + spike retrospective
  BOOTSTRAP.md            ← TWS_SERVER bootstrap + cold-start cooldown gotcha
  MIGRATION.md            ← IBC → controller drop-in guide
gateway_controller.py     ← Python controller (~2000 lines)
scripts/
  install.sh              ← Installer for release tarballs
Makefile                  ← Build + install + release
README.md                 ← User entry point
CHANGELOG.md              ← Version history
LICENSE                   ← MIT
```

## Submitting changes

1. Open an issue first for non-trivial changes so we can agree on
   scope before you spend time.
2. One logical change per PR. Rebase-and-merge strategy — please
   keep commits meaningful, no "fix typo in WIP commit".
3. If you add or change an agent protocol command: add a row to the
   command table in the `GatewayInputAgent.java` header comment, tag
   the new dispatch `case` with the version it ships in, and add the
   command to the list in `docs/ARCHITECTURE.md`.
4. If you touch the state machine in `gateway_controller.py`:
   - Hand-write a test plan in the PR description
   - Spike logs go in the parent `spike/` directory of whatever repo
     is integrating this (we don't keep test logs with credentials in
     this repo)
5. Run `make clean && make && make test` before pushing — any fresh
   build must succeed and the unit suite must be green.
6. Contributions are welcome from the community at large. The
   maintainer may decline contributions that would create ambiguity
   about the project's IP provenance (for example, work products of an
   employment relationship).

## Testing

Three layers:

**Unit suite** — `tests/`, stdlib `unittest` only, no pip installs:

```
make test                                 # syntax + jar manifest + unit suite
python3 -m unittest discover -s tests     # suite alone
```

The controller imports cleanly on any host with a Python 3 stdlib (no
Gateway, no display). The suite covers the pure decision helpers
(`_twofa_*`, `_detect_*`, `_coerce_*`, `_redact_logs`, TOTP
generation, env parsing) directly, and orchestration paths by stubbing
the `agent_*` socket wrappers with `unittest.mock.patch.object` — see
`tests/test_pure_logic.py` for the house patterns. New logic should
follow the same shape: keep decisions in small pure helpers, test them
directly, and mock the agent boundary for flow tests.

**Integration drills** — `tests/integration/`, run by hand against a
real Gateway install in a **throwaway container**, never against one
holding a live session. They use no credentials and never authenticate,
so no IBKR session slot is touched:

```
docker run -d --name ibg-drill --entrypoint sleep \
  -v "$PWD":/repo:ro ghcr.io/<owner>/ibg-controller:<tag> 3600
docker exec -d ibg-drill sh -c 'Xvfb :1 -screen 0 1024x768x24 &'
docker exec -e DISPLAY=:1 -e TRADING_MODE=paper \
  -e TWS_SETTINGS_PATH=/home/ibgateway/Jts_paper \
  -e GATEWAY_INPUT_AGENT_SOCKET=/tmp/gateway-input-paper.sock \
  ibg-drill python3 /repo/tests/integration/gateway_autorestart_drill.py
```

`gateway_autorestart_drill.py` is the model: it exercises Gateway's own
daily restart (issue #23) by launching the real JVM through the real
install4j launcher and invoking the real `.install4j/restarter`. Reach
for this layer when a change depends on how Gateway or install4j
actually behaves as a *process* — launcher chains, PID identity,
environment inheritance, the agent socket handoff. Mocked tests cannot
falsify assumptions in that territory; this drill has already caught
two defects that the unit suite passed clean.

Each check prints `[PASS]`/`[FAIL]` with a one-line reason, and the
docstring records what was observed and when. Anything the drill has to
stand in for (an API port that only opens after a real login, say) must
be called out there rather than quietly patched.

**Live validation** — end-to-end truth (real dialog wording, window
timing, IBKR server-side behavior) requires a real IB account +
credentials + TOTP secret, which can't live in CI. State-machine
changes therefore still need a hand-written test plan in the PR
description, and get validated by maintainer spike runs against a real
account before release; sanitized spike logs go in the integrating
repo's `spike/` directory, never here. The README compatibility
table's vocabulary tracks this: ✅ = validated against real Gateway,
⚠️ = code in place and unit-tested, not yet run against the real
product.

## Adding a new ALERT token or reason

- Format: `ALERT_<NAME> mode={TRADING_MODE} key=value key="quoted
  prose"` — one `log.error`/`log.info` call per emission, `mode=`
  first.
- Token names and key names are a stability contract
  (`docs/OBSERVABILITY.md`). Adding a new token, or a new `reason=`
  value on an existing token, is additive and fine; renaming or
  removing is a breaking change.
- Update `docs/OBSERVABILITY.md`: example line(s), **When fired**,
  **What the operator should do**, **Recommended debounce** — plus the
  grep recipe near the end of that file and README's monitoring token
  list if the token itself is new.

## Adding a new env var

- Read it where it's used (`os.environ.get` at the use site or at
  module load, matching its neighbors); document it in README's env
  table.
- IBC-compatible names: add the mapping row to `docs/FROM_IBC.md`,
  and if `scripts/ibc_config_to_env.py` should translate it, update
  that script together with `tests/test_ibc_config_to_env.py`.
- If the var was previously listed as unsupported, remove it from
  `_warn_unsupported_env_vars` and update `tests/test_env_compat.py`.

## Adding a new dialog handler

- Detect windows by title substring via `agent_windows()`; read
  dialog text via `agent_labels()` — but note that is JLabel-only:
  headings can be JTextArea, which only the `WINDOW` component dump
  shows (this distinction caused real bugs; see issue #20).
- Follow the wrapper contract: `agent_*` helpers catch all
  exceptions, `log.error`, and return a bool — they never raise into
  the state machine.
- Poll with `time.monotonic()` deadlines and ~0.5–1s sleeps; log
  window lists only when they change; never log typed payloads, and
  run window dumps through `_redact_logs` before logging them.
- Keep the decision in a pure helper (`_detect_*` / `_twofa_*` style)
  and unit-test it; mock the wrappers for the flow test.

## Adding a new agent command

- Java: a dispatch `case` with a `// Protocol:` comment and version
  tag, a `doXxx` method mirroring the closest existing command, and a
  row in the header's command table. Error strings follow
  `ERR <snake_case_reason> key=value`.
- EDT rules (see the threading note in the agent header):
  `invokeAndWait` for mutations that cannot open a modal dialog;
  `invokeLater` plus a short sleep for clicks that might. Read-only
  commands walk the component tree without EDT synchronization
  (established precedent).
- Never echo typed text or payloads in responses or logs, and never
  emit `JPasswordField` contents (v0.6.3 security posture).
- Python: an `agent_xxx` wrapper following the catch-log-bool
  contract, and a `docs/ARCHITECTURE.md` command-list entry.

## Scope

What this tool is:
- A Python + in-JVM Java agent replacement for IBC
- Targeted at the headless Docker use case
- Scoped to what the `gnzsnz/ib-gateway-docker` image needs

What this tool is NOT:
- A general-purpose GUI automation library
- A replacement for TWS's rich desktop functionality
- A trading framework

If you want to use this outside Docker, it should mostly work — but
you'll need an X display (Xvfb is the easy answer) and a window
manager (matchbox is what the shipped image uses). See
`docs/ARCHITECTURE.md`.

## Questions

Open an issue. The author(s) check notifications daily during weekdays.

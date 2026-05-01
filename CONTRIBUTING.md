# Contributing to ibg-controller

Thanks for helping. This is a small project — short contribution guide.

## Build and run locally

```bash
# Build the agent jar and stage the controller into dist/
make

# Install directly into a running ibgateway home (for on-host dev)
make install DESTDIR=/home/ibgateway

# Create a release tarball
make release VERSION=0.5.13
```

Requires JDK 17+ (`javac` + `jar`) and `make`. No Maven, no Gradle.

## Runtime requirements (from the `Dockerfile.template` integration)

See `docs/ARCHITECTURE.md` for the full list and why each is needed.
The short version:

- `python3` — runs `gateway_controller.py`
- `matchbox-window-manager` — Xvfb has no WM by default; Gateway's
  input routing depends on a focused window

Pre-v0.5.13 the image also installed `python3-gi gir1.2-atspi-2.0
at-spi2-core libatk-wrapper-java libatk-wrapper-java-jni dbus-x11` and
configured `$JAVA_HOME/conf/accessibility.properties`. v0.5.12 disabled
the AT-SPI bridge in the JVM (it was deadlocking on Swing dispatch);
v0.5.13 removed the install steps. If you're rebasing from an older
fork, drop those packages and the JRE accessibility.properties write
when you bring your image forward.

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
3. If you change agent protocol (new commands, new wire format),
   bump the version in both the agent source and the Python side's
   protocol check.
4. If you touch the state machine in `gateway_controller.py`:
   - Hand-write a test plan in the PR description
   - Spike logs go in the parent `spike/` directory of whatever repo
     is integrating this (we don't keep test logs with credentials in
     this repo)
5. Run `make clean && make` before pushing — any fresh build must
   succeed.

## Testing

There is no automated test suite. End-to-end testing requires a real
IB account + credentials + TOTP secret, which can't live in CI. The
testing model is:

- Maintainers run a spike container against their own real account
  after any state-machine change
- Spike logs (sanitized, no credentials) get committed under
  `spike/PHASE*_SUCCESS.md` in the integrating image's parent repo
- Breaking changes documented in `CHANGELOG.md`

If you have ideas for a fake/mock Gateway that would let us run some
automated tests, please open an issue.

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

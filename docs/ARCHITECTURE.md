# Architecture

Why `ibg-controller` looks the way it does. This is both a design
document and a record of what we learned during the initial spike — if
you're trying to understand **why** a particular piece is necessary,
look here.

## The core insight

**The IBC replacement has to be in-process with Gateway's JVM for text
input, and out-of-process for everything else.**

IB Gateway's Swing-based login form rejects every external input
mechanism we tried:

| Mechanism | Result |
|---|---|
| `Atspi.EditableText.set_text_contents(node, text)` | Returns `False`. No exception. Field stays empty. |
| `Atspi.EditableText.delete_text + insert_text` | Both return `False`. Field stays empty. |
| `Atspi.generate_keyboard_event(0, text, KeySynthType.STRING)` | No exception. Field stays empty. |
| `xdotool key`/`xdotool type` (XTest) | Runs cleanly (rc=0). Field stays empty. |
| `xdotool key --window <wid>` (XSendEvent) | Runs cleanly. Field stays empty. Swing's AWT subsystem rejects synthetic events. |
| `xdotool windowfocus + click + key + type` (XSetInputFocus) | Same. Field stays empty. |
| Click on the on-screen security keyboard via AT-SPI | Labels have no `Action` interface — can't click them. |
| Click those labels by bounds via xdotool | Bounds are `-1,-1 -1x-1` (not rendered unless field is focused, and even then unreliable). |

Inside the JVM, **`JTextField.setText()` works instantly**. IBC works
because it runs as the JVM's main class; Lcstyle's `ibctl` works because
it uses `-javaagent:` to inject a Java agent. We use the same trick.

**But** everything else — component discovery, button clicks, state
observation, dialog detection, monitoring for re-auth — works perfectly
from outside the JVM via AT-SPI2, using the public Swing accessibility
API.

So the architecture splits:
- **Python controller, out-of-process**: state machine, monitoring,
  discovery, button clicks via `Atspi.Action.do_action`. All the logic
  and the bulk of the code.
- **Tiny Java agent, in-process**: loaded via `-javaagent:`, exposes
  a Unix-socket protocol with two operations: "type text into component
  named X" and "click button named X". ~750 lines of Java, rarely
  touched.

## The pieces and why each one is necessary

### 1. Xvfb + a window manager

Gateway is a Swing GUI app and refuses to start in `java.awt.headless`
mode. Xvfb provides a display. That much is standard.

**But Xvfb alone isn't enough** — we also need a window manager. Why?
Gateway's installer-bundled JVM routes some input through X11 focus
tracking, and Xvfb has no concept of "focused window" without a WM.
When we tried to inject events without a WM, they went nowhere. With
`matchbox-window-manager` (chosen for being tiny and not adding titlebars:
`matchbox-window-manager -use_titlebar no`), focus routing works.

### 2. D-Bus session bus

AT-SPI2 uses a D-Bus session bus to publish the `org.a11y.Bus` service
name. Without a session bus, there's no place for the bus to publish
itself. `dbus-launch --sh-syntax` produces a fresh session bus for the
container.

### 3. AT-SPI2 infrastructure (`at-spi-bus-launcher` + `at-spi2-registryd`)

AT-SPI2 actually uses **two** buses: the normal D-Bus session bus, and
a **separate accessibility bus** managed by `at-spi-bus-launcher`.
Applications (like Gateway's JVM) find the accessibility bus via the
`org.a11y.Bus.GetAddress` method call on the session bus, which
`at-spi-bus-launcher` services.

**Don't start `at-spi2-registryd` directly on the session bus** — we
tried that early in the spike and it silently failed. You need to start
`at-spi-bus-launcher --launch-immediately`, which will:
1. Claim `org.a11y.Bus` on the session bus
2. Spawn a fresh `dbus-daemon` for the accessibility bus
3. Start `at-spi2-registryd` on the accessibility bus (via D-Bus activation)

After this, Python's `Atspi.get_desktop(0)` returns something useful
and Gateway's application appears in the tree.

### 4. Java accessibility configuration in the JRE

Gateway's bundled JRE needs to load `org.GNOME.Accessibility.AtkWrapper`
as an assistive technology at startup. This requires two things:

**a)** A file at `$JAVA_HOME/conf/accessibility.properties` containing:
```
assistive_technologies=org.GNOME.Accessibility.AtkWrapper
```

**b)** The native `libatk-wrapper.so` placed at `$JAVA_HOME/lib/libatk-wrapper.so`
(not just on `LD_LIBRARY_PATH`). This is because the Java wrapper jar
is loaded via `-Xbootclasspath/a:/usr/share/java/java-atk-wrapper.jar`,
and `System.loadLibrary("atk-wrapper")` from a boot-classpath class
searches `sun.boot.library.path` (which is `$JAVA_HOME/lib`) — NOT
`java.library.path`.

The `libatk-wrapper-java-jni` Ubuntu package ships the .so at
`/usr/lib/<arch>-linux-gnu/jni/libatk-wrapper.so`. We copy it to
`$JAVA_HOME/lib/` at build time.

#### JRE path discovery

IB Gateway's JRE lives in different places depending on architecture:
- **amd64**: install4j-bundled at `/usr/local/i4j_jres/<install-id>/<version>-zulu/`
  (the install ID is random per-install)
- **arm64**: system Zulu at `/usr/local/zulu17.<full-version>/`
  (downloaded as a tarball during the Docker build — no install4j)

The Dockerfile build-time step does:
```bash
GW_JAVA=$(find /usr/local/i4j_jres -name java -type f 2>/dev/null | head -1)
[ -z "$GW_JAVA" ] && GW_JAVA=$(find /usr/local -path "*/zulu*/bin/java" -type f 2>/dev/null | head -1)
JAVA_HOME=$(dirname $(dirname "$GW_JAVA"))
```

### 5. Gateway launch via install4j launcher

The controller launches Gateway via its bundled launcher script at
`$TWS_PATH/ibgateway/<version>/ibgateway`. That script is generated by
install4j. The controller passes two kinds of arguments:

**Command-line `-V` flags** (install4j variable substitution):
```
-VjtsConfigDir=/home/ibgateway/Jts
-VinstallerType=standalone
```

The install4j launcher has literal unsubstituted placeholders like
`${installer:jtsConfigDir}` in its embedded config. These get passed
to Java as `-DjtsConfigDir=${installer:jtsConfigDir}` — a broken
literal string. The `-V` flag substitutes the variable BEFORE the
launcher constructs the Java command line, so the placeholder resolves
correctly. **Without `-V`, the first `-D` (the placeholder) wins over
any override in `INSTALL4J_ADD_VM_PARAMS` because Java uses the first
definition of a system property.** This was the root cause of the
persistent auth-timeout bug found during initial deployment.

**`INSTALL4J_ADD_VM_PARAMS`** (additional JVM arguments):
```
--add-opens=java.base/java.util=ALL-UNNAMED
--add-opens=java.desktop/javax.swing=ALL-UNNAMED
... (19 module-access flags total)
-Xbootclasspath/a:/usr/share/java/java-atk-wrapper.jar
-DjtsConfigDir=/home/ibgateway/Jts
-javaagent:/home/ibgateway/gateway-input-agent.jar=/tmp/gateway-input.sock
```

- **Module-access flags**: Gateway's auth and UI code uses reflection
  into `java.desktop` and `java.base` internals. Java 17's module
  system blocks this by default. These are the same 19 flags IBC's
  `ibcstart.sh` passes; the install4j launcher's `.vmoptions` file
  does not include them.
- **`-Xbootclasspath/a:`** puts the AT-SPI Java wrapper jar on the boot
  classpath so the JVM can find `org.GNOME.Accessibility.AtkWrapper`
  referenced in `accessibility.properties`.
- **`-DjtsConfigDir=`** is also set here as a belt-and-suspenders
  backup to the `-V` flag (in case the install4j launcher doesn't
  support `-V` on some platform).
- **`-javaagent:`** loads our input agent at JVM startup. The argument
  after `=` is passed to the agent as its `agentArgs` — we use it to
  pass the Unix socket path.

### 6. The input agent (`gateway-input-agent.jar`)

~750 lines of Java. Three core responsibilities (extended with additional commands in v0.2):

1. **Listen on a Unix domain socket** (via JEP 380 — JDK 16+). Single
   client at a time, line-protocol, no concurrency.
2. **Walk `Window.getWindows()`** recursively via `Container.getComponents()`
   to find Swing components. Search by `AccessibleContext.getAccessibleName()`
   (matches what Python sees via AT-SPI) OR by window title (for
   components that have no accessible name — see the 2FA dialog section).
3. **Dispatch operations on the EDT**: `SwingUtilities.invokeAndWait`
   for `SETTEXT`/`GETTEXT` (synchronous), `SwingUtilities.invokeLater`
   + `Thread.sleep(50)` for `CLICK`. We use `invokeLater` for clicks
   because `doClick()` may open a modal dialog that blocks the EDT, and
   `invokeAndWait` would deadlock. This threading rule was borrowed
   directly from `ibctl`.

Protocol commands (line-based, `\n`-terminated):

**v0.1 (Phase 1) commands**:
```
PING                                    → OK pong
SETTEXT <name> <text...>                → OK
GETTEXT <name>                          → OK <current text>
CLICK <name>                            → OK
LIST [substring]                        → OK\n<text|button> <name>\n...END
WINDOWS                                 → OK\n<type> | <title> | modal=<bool>\n...END
WINDOW [title_substring]                → OK\n<recursive component dump>\nEND
LABELS [substring]                      → OK\n[<window>] <label text>\nEND
SETTEXT_IN_WIN <title_substr>|<text>    → OK
CLICK_IN_WIN <title_substr>|<button>    → OK
```

**v0.2 (Phase 2) additions**:
```
GET_PID                                    → OK <jvm_pid>
JTREE_SELECT_PATH <title>|<p1>/<p2>/...    → OK selected=<path>
JCHECK <title>|<name>|<true|false>         → OK unchanged=<v> | OK changed=<v>
SETTEXT_BY_LABEL <title>|<label>|<value>   → OK set label=<label> value=<v>
```

Why each was added:

- **`GET_PID`** — returns `ProcessHandle.current().pid()`. Used by the
  Python controller to disambiguate its own Gateway JVM from any other
  Gateway JVM in the same container. Dual mode (`TRADING_MODE=both`)
  runs two Gateway JVMs that both register as "IBKR Gateway" in AT-SPI;
  `find_app(match_pid=...)` filters to the right one.
- **`JTREE_SELECT_PATH`** — navigates a `JTree` to a slash-separated
  path by matching `node.toString()` at each level, expanding parents
  as it walks. Gateway's ConfigurationTree renders cells on demand via
  a `CellRendererPane` — the cell components aren't real `Container`
  children, so AT-SPI traversal + `Atspi.Action.do_action` can't reach
  them. The only way to drive the tree is through the `JTree` model
  API directly from inside the JVM.
- **`JCHECK`** — idempotent toggle of any `JToggleButton` (covers
  `JCheckBox`, `JRadioButton`, and install4j subclasses) matched by
  accessible name or button text. Reads the current selected state
  before acting and skips the click if already at target, so applying
  the same config repeatedly is a no-op instead of a toggle-thrash.
- **`SETTEXT_BY_LABEL`** — sets a text field identified by its adjacent
  `JLabel`'s text instead of by the field's own accessible name. Needed
  for spinners and unnamed fields (e.g. Gateway's "Master API client
  ID" field is a `JSpinner` whose editor has no accessible name of its
  own, but sits next to a `JLabel` with the descriptive text). Also
  calls `commitEdit()` on `JFormattedTextField` editors so spinner
  values actually propagate to the underlying model.

### 7. The Python controller (`gateway_controller.py`)

~2000 lines. The state machine, in rough order:

1. **Launch**: `launch_gateway()` — spawn the install4j launcher with
   `INSTALL4J_ADD_VM_PARAMS` set. Sets `GATEWAY_PROC` global so the
   command server's `RESTART` handler can terminate + re-launch the
   JVM in place.
2. **Wait for agent**: `agent_wait_ready()` — poll the Unix socket
   until `PING` returns `OK pong`. Then call `GET_PID` and store the
   JVM PID in the `JVM_PID` global.
3. **Wait for AT-SPI app**: `find_app(APP_NAME_CANDIDATES, timeout=120,
   match_pid=JVM_PID)` — poll `Atspi.get_desktop(0)` until an
   application registers with a matching name *and* process ID. The
   app-name candidate list is `["IBKR Gateway"]` by default, or
   `["Trader Workstation", "IB Trader Workstation", "TWS"]` when
   `GATEWAY_OR_TWS=tws`.
4. **Login**: `handle_login(app)` — find the `password text` role via
   AT-SPI, detect the login form, select trading mode (via toggle
   button click), type credentials via the agent, click Log In via
   `Atspi.Action.do_action`.
5. **Post-login dialogs**: `handle_post_login_dialogs(app)` — polls
   up to 6s for any modal to appear, dumps each via `WINDOW`,
   recognizes `Existing session detected` by body text, clicks
   `Continue Login` via `CLICK_IN_WIN`. Leaves unrecognized modals
   alone.
6. **2FA**: `handle_2fa(app)` — if `TWOFACTOR_CODE` is set, poll for a
   window titled `Second Factor`. When present, generate a TOTP code
   and use `SETTEXT_IN_WIN` to type it, then `CLICK_IN_WIN Second
   Factor|OK`. Early-exits if the API port opens. Also opportunistically
   handles a late-arriving `Existing session detected` dialog in case
   the initial `handle_post_login_dialogs` poll missed it.
7. **Disclaimers**: `dismiss_post_login_disclaimers()` — click any
   "I understand and accept" style buttons. Conservative: never clicks
   bare "OK".
8. **Wait for API port**: `wait_for_api_port()` — poll TCP port 4001
   (live) / 4002 (paper) until it accepts a connection. **This is the
   definitive readiness signal.**
9. **Post-login config** (v0.2): `handle_post_login_config()` — if any
   of `TWS_MASTER_CLIENT_ID` / `READ_ONLY_API` / `AUTO_LOGOFF_TIME` /
   `AUTO_RESTART_TIME` is set, open Configure → Settings, navigate
   tree via `JTREE_SELECT_PATH`, apply values via `SETTEXT_BY_LABEL`
   and `JCHECK`, commit via `CLICK_IN_WIN Configuration|OK`. Gateway
   shows *either* Auto Log Off Time *or* Auto Restart Time in Lock
   and Exit depending on account state; the handler tries both labels.
10. **Command server** (v0.2): `start_command_server()` — if
    `CONTROLLER_COMMAND_SERVER_PORT` is set, start a daemon thread
    listening on that TCP port for IBC-compat commands (`STOP`,
    `RESTART`, `RECONNECTACCOUNT`, `ENABLEAPI`, `RECONNECTDATA`).
11. **Signal ready**: touch `$CONTROLLER_READY_FILE` so `run.sh` can
    start socat. Defaults to `/tmp/gateway_ready` in single mode,
    `/tmp/gateway_ready_{mode}` in dual mode.
12. **Monitor loop**: `monitor_loop(app)` — re-reads the `GATEWAY_PROC`
    and `CURRENT_APP` globals each iteration (so a RESTART command can
    transparently swap them mid-loop). Checks JVM exit every iteration,
    heartbeats the API port every 30s, triggers `attempt_reauth()`
    after three consecutive port-closed heartbeats + a visible login
    dialog.

### 8. Dual mode (`TRADING_MODE=both`) dispatch (v0.2)

When `TRADING_MODE=both` and `USE_PYATSPI2_CONTROLLER=yes`, `run.sh`
spawns `start_process` twice in sequence — once with live env, then
with paper env. Each invocation exports distinct per-instance values:

| Env var | Live | Paper |
|---|---|---|
| `TRADING_MODE` | `live` | `paper` |
| `TWS_SETTINGS_PATH` | `$TWS_PATH_live` | `$TWS_PATH_paper` |
| `GATEWAY_INPUT_AGENT_SOCKET` | `/tmp/gateway-input-live.sock` | `/tmp/gateway-input-paper.sock` |
| `CONTROLLER_READY_FILE` | `/tmp/gateway_ready_live` | `/tmp/gateway_ready_paper` |
| `CONTROLLER_COMMAND_SERVER_PORT` | (as configured) | (auto-offset by +1 to avoid collision) |

Each controller launches its own Gateway JVM in its own
`JTS_CONFIG_DIR` (`Jts_live` / `Jts_paper`) so both can write to
`jts.ini`, encrypted state, and `launcher.log` without stepping on
each other. `find_app(match_pid=...)` uses the per-instance JVM PID
to pick the right AT-SPI app when both JVMs are registered.

The legacy `wait_for_controller_ready` behavior (return non-zero
under `set -Eeo pipefail`) was changed to always return 0 on
timeout, because in dual mode a stuck live instance previously
crashed the whole container before paper even started.

## Things that surprised us during the spike

A catalog of dead ends and non-obvious behaviors, so future maintainers
don't retrace them.

### AT-SPI tree goes stale after login frame teardown

After Gateway tears down the login frame and creates the post-login
main window, the AT-SPI application accessible's `child_count` returns
`-1` and tree-walking finds nothing. Even a fresh Python AT-SPI
connection inside the same container sees the broken view. This is a
`java-atk-wrapper` bridge bug, not stale caching on the client side.

**Workaround**: for all post-login state detection, use the agent's
`Window.getWindows()` walk inside the JVM instead of AT-SPI. The
in-JVM view is the source of truth for the live Swing component tree.

### Swing secure-input rejects every external mechanism

See the table at the top. The JDK marks XTest events as synthetic and
Swing's AWT subsystem filters them. The accessibility `EditableText`
write methods return `false` without warning. You cannot type into IB
Gateway's JTextFields from outside the JVM. This is the entire reason
the Java agent exists.

### Gateway has a cold-start SSL handshake problem

On a fresh container with a minimal `jts.ini`, Gateway tries to connect
to IBKR's "misc URLs" server on port 4000 and the SSL handshake fails
with `Remote host terminated the handshake`. This cascades to the auth
dispatcher being interrupted. Gateway never progresses past
"Authenticating".

The cause is that Gateway needs a `SupportsSSL` cache entry in its
`jts.ini` to skip the TLS negotiation. The cache format is
`<host>:<port>,<supported_bool>,<cache_date YYYYMMDD>,<secondary_flag>`.

**Workaround**: when `TWS_SERVER` is set, the controller writes a
`jts.ini` that includes a `SupportsSSL` entry with today's date. After
a successful login, Gateway populates the cache itself and subsequent
starts don't need the pre-seeding.

### The "Gateway" modal dialog is a progress indicator, not a dismissable popup

A modal `JDialog` titled "Gateway" with only an OK button appears
during the login network round-trip. It is **NOT an informational popup
to be dismissed** — it's a "Connecting to server..." progress modal.
Clicking OK **cancels** the in-progress login and bounces back to the
login form.

**Don't click bare "OK" on post-login modals unless you've verified
what the dialog is.** The controller's `handle_post_login_dialogs` is
conservative: it dumps the dialog content for diagnostics, recognizes
specific dialogs by body text (existing-session-detected), and leaves
unrecognized ones alone.

### The post-login "main window" is drawn BEFORE authentication completes

Buttons like `Show log`, `Clear`, `File`, `Configure`, `Help` appear in
the main Gateway window shell from the moment Gateway starts — not
after a successful login. We initially used them as readiness markers
and got false positives. **The only reliable readiness signal is the
API port being open.**

### The 2FA input field has no accessible name

The `JTextField` inside the "Second Factor Authentication" dialog is
a bare class (`u` after install4j obfuscation) with no accessible name
or description. You can't find it via AT-SPI by name, and the agent's
original `SETTEXT` command (which looks up by `getAccessibleName()`)
can't find it either. We had to add `SETTEXT_IN_WIN` which takes a
window title substring and types into the first `JTextComponent`
found inside. This pattern handles every dialog whose fields have no
names.

### `libatk-wrapper-java-jni` isn't in the "wrong arch" — the initial check was stale

An early check suggested `libatk-wrapper-java-jni` was only packaged
for amd64 on Ubuntu 24.04, not arm64. The check was wrong — running
`apt-cache policy` after `rm -rf /var/lib/apt/lists/*`, which returned
`Candidate: (none)` for every package because the lists were empty.
It IS in the noble arm64 archive as `0.40.0-3build2`. Verified by
installing it and checking `dpkg -L`.

## Further reading

- `spike/PHASE1_FINDINGS.md` in the development repo — the raw spike
  writeup with every dead end and every step that worked.
- `spike/PHASE0_FINDINGS.md` — the feasibility spike that came before,
  establishing that AT-SPI2 exposes enough of Gateway's Swing tree to
  drive it at all.
- [Lcstyle/ibctl](https://github.com/Lcstyle/ibctl) — the Rust+Java
  prior art. Different language choice but the same architectural
  conclusion about the in-JVM agent.
- [IBC source](https://github.com/IbcAlpha/IBC) — the canonical
  reference for Gateway's dialogs, even though IBC is being deprecated.
- [java-atk-wrapper (GNOME)](https://gitlab.gnome.org/GNOME/java-atk-wrapper) —
  the bridge that makes AT-SPI work for Swing.

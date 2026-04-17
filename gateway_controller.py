#!/usr/bin/env python3
"""
IB Gateway Controller — pyatspi2-based replacement for IBC.

Single-file controller that:
  1. Launches IB Gateway directly via its install4j launcher with the
     ATK accessibility bridge enabled.
  2. Walks Gateway's Swing component tree via AT-SPI2 to drive the
     login dialog, 2FA dialog, and configuration dialogs.
  3. Verifies every action by reading state back through the same tree.
  4. Signals readiness via /tmp/gateway_ready so socat starts only AFTER
     login succeeds and the main window is up.
  5. Stays alive monitoring the JVM and watching for re-auth events.

Why pyatspi2 and not xdotool: xdotool synthesizes X events but can't
verify components actually received them. AT-SPI2 exposes the live Swing
component tree (same approach IBC uses internally), so every action can
be verified by reading state back. No pixel coordinates, no screen
scraping.

Why a single Python file: the upstream maintainer (gnzsnz) doesn't write
Java or Rust, and wants something he can read, debug, and patch when IB
ships a new dialog on a Friday night. Python with stdlib + pyatspi2 hits
that bar.

Reference material: Lcstyle's ibctl (https://github.com/Lcstyle/ibctl)
for dialog catalog and edge cases. Original work by @rlktradewright (IBC).
"""

import base64
import enum
import hashlib
import hmac
import logging
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time

import gi


# ── Controller state machine ──────────────────────────────────────────
#
# The controller proceeds through these states in order. Each state
# transition is logged so the current position is visible in the output.
# A RESTART command resets to LAUNCHING and re-drives the sequence.
#
#   INIT → LAUNCHING → AGENT_WAIT → APP_DISCOVERY → LOGIN → POST_LOGIN
#   → TWO_FA → DISCLAIMERS → API_WAIT → CONFIG → COMMAND_SERVER → READY
#   → MONITORING
#          ↑                                              │
#          └──── (RESTART / re-auth on session loss) ─────┘
#
# This is not a formal state machine library (gnzsnz suggested
# python-statemachine — open to refactoring). But the states are
# explicit, logged at each transition, and visible in the output so
# the flow is clear to anyone reading the logs or the code.

class State(enum.Enum):
    INIT = "INIT"
    LAUNCHING = "LAUNCHING"
    AGENT_WAIT = "AGENT_WAIT"
    APP_DISCOVERY = "APP_DISCOVERY"
    LOGIN = "LOGIN"
    POST_LOGIN = "POST_LOGIN"
    TWO_FA = "TWO_FA"
    DISCLAIMERS = "DISCLAIMERS"
    API_WAIT = "API_WAIT"
    CONFIG = "CONFIG"
    COMMAND_SERVER = "COMMAND_SERVER"
    READY = "READY"
    MONITORING = "MONITORING"


_current_state = State.INIT


def _set_state(new_state):
    """Transition to a new controller state. Logs the transition."""
    global _current_state
    old = _current_state
    _current_state = new_state
    log.info(f"[STATE: {new_state.value}]")
gi.require_version("Atspi", "2.0")
from gi.repository import Atspi  # noqa: E402


# ── Config from environment ─────────────────────────────────────────────
# Same env var names IBC uses, so existing docker-compose files keep working.

USERNAME = os.environ.get("TWS_USERID", "")
PASSWORD = os.environ.get("TWS_PASSWORD", "")
TRADING_MODE = os.environ.get("TRADING_MODE", "paper").lower()
TOTP_SECRET = os.environ.get("TWOFACTOR_CODE", "")

# When TRADING_MODE=paper, prefer the *_PAPER credentials if set. This
# mirrors run.sh's dual-mode logic, where the paper instance is started
# with TWS_USERID = $TWS_USERID_PAPER. Users running TRADING_MODE=both
# in production typically set TWS_USERID to their LIVE account and
# TWS_USERID_PAPER to their paper account; our paper-only test mode
# should pick the paper account, otherwise IBKR rejects the login as
# 'multiple paper trading users associated with this user'.
if TRADING_MODE == "paper":
    _paper_user = os.environ.get("TWS_USERID_PAPER", "")
    _paper_pass = os.environ.get("TWS_PASSWORD_PAPER", "")
    if _paper_user:
        USERNAME = _paper_user
    if _paper_pass:
        PASSWORD = _paper_pass

# IBKR regional server hostname. IB accounts are bound to one regional
# data center (ndc1.ibllc.com / cdc1.ibllc.com / gdc1.ibllc.com / etc.).
# Without this set, Gateway tries the default and fails the SSL
# handshake on the misc URLs port for accounts hosted elsewhere.
# Users can find their server in their IB account portal, or by running
# Gateway interactively (via VNC) once and checking the Peer line in
# the resulting Jts/jts.ini.
#
# Same _PAPER convention: an IB user can have their LIVE account on one
# server and PAPER on another (we observed this in real-world testing).
def _validate_hostname(value, varname):
    """Strict hostname validation for env vars we write into jts.ini.

    IBKR regional servers are always simple hostnames like
    `cdc1.ibllc.com`, so anything with whitespace, newlines, or
    control characters is either a typo or an injection attempt.
    Defends against the theoretical attack of a user setting e.g.
    `TWS_SERVER="cdc1.ibllc.com\\n[Logon]\\nEvil=yes"` which would
    otherwise inject extra .ini sections when we write jts.ini.
    """
    if not value:
        return value
    import re as _re
    if not _re.fullmatch(r"[A-Za-z0-9._-]+", value):
        raise ValueError(
            f"{varname}={value!r} is not a valid hostname "
            "(expected DNS label characters only). Refusing to proceed "
            "because this value would be written into jts.ini and a "
            "malformed value could inject unintended .ini content."
        )
    return value


def _redact_logs(s):
    """Strip sensitive patterns from a log line before we emit it.

    Gateway window titles like "DU9999999 Trader Workstation
    Configuration (Simulated Trading)" include the user's account
    number (DU prefix for paper, U prefix for live). When
    CONTROLLER_DEBUG=1 dumps modal windows, those titles appear in
    logs users may share to ask for help. Redact them so the default
    debug-log experience doesn't leak identifiers.

    Also masks obvious username-looking tokens in titles. Applied
    conservatively — we don't redact log bodies of components we
    care about (e.g. "Existing session detected"), only the
    account-number pattern.
    """
    if not isinstance(s, str):
        return s
    import re as _re
    # Paper accounts start with DU, live with U + digits (IBKR convention)
    s = _re.sub(r"\b(DU|U)\d{5,10}\b", r"\1[REDACTED]", s)
    return s


TWS_SERVER = _validate_hostname(os.environ.get("TWS_SERVER", ""), "TWS_SERVER")
if TRADING_MODE == "paper":
    _paper_server = _validate_hostname(
        os.environ.get("TWS_SERVER_PAPER", ""), "TWS_SERVER_PAPER")
    if _paper_server:
        TWS_SERVER = _paper_server

# Gateway install path (where the install4j launcher + JRE live). This
# is shared across dual-mode instances — both JVMs run the same Gateway
# binary.
TWS_PATH = os.environ.get("TWS_PATH", os.path.expanduser("~/Jts"))
TWS_VERSION = os.environ.get("TWS_MAJOR_VRSN", "")

# Per-instance config/state directory. In dual-mode containers, run.sh
# sets TWS_SETTINGS_PATH to Jts_live / Jts_paper so each Gateway JVM
# has isolated state (its own jts.ini, encrypted state dir, autorestart
# tokens, launcher.log). Falls back to TWS_PATH for single-mode.
#
# This is what we pass to Gateway as -DjtsConfigDir and what we write
# jts.ini / read warm state from. TWS_PATH is used only for locating
# the install4j launcher.
JTS_CONFIG_DIR = os.environ.get("TWS_SETTINGS_PATH") or TWS_PATH

# Readiness signal file. Each dual-mode instance uses a distinct path so
# run.sh can wait for each instance independently (live vs paper).
READY_FILE = os.environ.get("CONTROLLER_READY_FILE", "/tmp/gateway_ready")

# In-JVM input agent — provides text input that's structurally impossible
# from outside the JVM. The agent jar is loaded via -javaagent: in
# INSTALL4J_ADD_VM_PARAMS and listens on this Unix socket.
AGENT_JAR = os.environ.get("GATEWAY_INPUT_AGENT_JAR",
                           os.path.expanduser("~/gateway-input-agent.jar"))
AGENT_SOCKET = os.environ.get("GATEWAY_INPUT_AGENT_SOCKET",
                              "/tmp/gateway-input.sock")

# Optional warm-state directory. If set and exists, the controller copies
# its contents into TWS_PATH before launching Gateway. Used to seed a
# fresh container with previously-saved settings (jts.ini, encrypted
# account state, autorestart tokens) so Gateway connects to the correct
# regional server (e.g. cdc1.ibllc.com vs the default ndc1.ibllc.com)
# and can bypass full re-auth via the autorestart token if it's recent.
WARM_STATE_DIR = os.environ.get("GATEWAY_WARM_STATE", "")

# Spike-mode flag — when set, the controller will exit cleanly after
# clicking Log In instead of waiting for a real main window. Used by the
# spike test harness with bogus credentials.
TEST_MODE = os.environ.get("CONTROLLER_TEST_MODE", "") == "1"

# Our Gateway JVM's OS process ID. Populated in main() right after
# agent_wait_ready() succeeds, from the agent's GET_PID response. Used
# by find_app() to pick "this controller's" Gateway instance out of the
# AT-SPI desktop tree in dual-mode containers where two 'IBKR Gateway'
# apps are present simultaneously. Stays None until the agent is up.
JVM_PID = None

# The Gateway JVM subprocess and its AT-SPI application accessible.
# Made module-global so the Phase 2.4 command server's RESTART handler
# can tear down the current JVM and re-launch + re-login in place,
# updating these globals so the monitor loop automatically picks up
# the new references without having to be restarted itself.
GATEWAY_PROC = None
CURRENT_APP = None

# Product switch: 'gateway' (default) or 'tws'. Phase 2.3 lets the same
# controller drive IB Gateway OR Trader Workstation from the same image.
# TWS exposes a different AT-SPI application name than Gateway, so
# find_app() scans both names and a couple of common variants.
GATEWAY_OR_TWS = os.environ.get("GATEWAY_OR_TWS", "gateway").strip().lower()
if GATEWAY_OR_TWS == "tws":
    APP_NAME_CANDIDATES = ["Trader Workstation", "IB Trader Workstation", "TWS"]
else:
    APP_NAME_CANDIDATES = ["IBKR Gateway"]


# ── Logging ─────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.DEBUG if os.environ.get("CONTROLLER_DEBUG") else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("controller")


# ── TOTP generation (stdlib only — no oathtool, no pyotp) ───────────────

def generate_totp(secret_b32, period=30, digits=6):
    """Generate a TOTP code from a base32 secret. Stdlib only."""
    key = base64.b32decode(secret_b32, casefold=True)
    counter = struct.pack(">Q", int(time.time()) // period)
    mac = hmac.new(key, counter, hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    code = struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** digits)).zfill(digits)


# ── AT-SPI helpers ──────────────────────────────────────────────────────

def safe(fn, default=None):
    """Run an AT-SPI call with retry. AT-SPI calls can timeout under load."""
    for _ in range(3):
        try:
            return fn()
        except Exception:
            time.sleep(0.2)
    return default


def find_descendant(node, role=None, name=None, depth=0, max_depth=20):
    """Walk a subtree DFS looking for the first node matching role and/or name.

    Match semantics:
      - role: exact match against get_role_name()
      - name: exact match against get_name() OR get_description()
        (Gateway uses description for icon-only buttons like Close/Min/Max)
    """
    if depth > max_depth:
        return None
    try:
        n_role = node.get_role_name()
    except Exception:
        n_role = None
    try:
        n_name = node.get_name() or ""
    except Exception:
        n_name = ""
    try:
        n_desc = node.get_description() or ""
    except Exception:
        n_desc = ""

    role_ok = role is None or n_role == role
    name_ok = name is None or n_name == name or n_desc == name
    if role_ok and name_ok:
        return node

    n_children = safe(lambda: node.get_child_count(), 0) or 0
    for i in range(n_children):
        child = safe(lambda: node.get_child_at_index(i))
        if child is None:
            continue
        hit = find_descendant(child, role=role, name=name,
                              depth=depth + 1, max_depth=max_depth)
        if hit is not None:
            return hit
    return None


def find_app(name_substring, timeout=120, match_pid=None):
    """Poll until an AT-SPI application matching name_substring appears.

    `name_substring` can be a single string or a list of candidate
    substrings (Phase 2.3: TWS exposes multiple possible app names
    depending on version/locale, so we try each in turn per poll tick).

    If `match_pid` is set, only return an app whose process ID matches.
    This is how dual-mode containers tell their own Gateway JVM apart
    from the other instance — both appear as 'IBKR Gateway' in the
    desktop tree, but each has a distinct `get_process_id()`.

    If `match_pid` is None, fall back to the first matching app with
    any children (Phase 1 behavior, still correct for single-mode).
    """
    if isinstance(name_substring, str):
        candidates = [name_substring]
    else:
        candidates = list(name_substring)
    candidates_lower = [c.lower() for c in candidates]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        desktop = Atspi.get_desktop(0)
        n = safe(lambda: desktop.get_child_count(), 0) or 0
        for i in range(n):
            app = safe(lambda: desktop.get_child_at_index(i))
            if app is None:
                continue
            app_name = safe(lambda: app.get_name(), "") or ""
            if not any(c in app_name.lower() for c in candidates_lower):
                continue
            if (safe(lambda: app.get_child_count(), 0) or 0) <= 0:
                continue
            if match_pid is not None:
                app_pid = safe(lambda: app.get_process_id(), None)
                if app_pid != match_pid:
                    continue
            return app
        time.sleep(0.5)
    return None


def wait_for(app, role, name=None, timeout=60):
    """Poll the app subtree until a node with role (and optional name) appears."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        node = find_descendant(app, role=role, name=name)
        if node is not None:
            return node
        time.sleep(0.3)
    return None


def get_states(node):
    """Return the set of state names on a node, e.g. {'focusable', 'checked'}."""
    state_set = safe(lambda: node.get_state_set())
    if state_set is None:
        return set()
    states = set()
    # AT-SPI state enum values are 0..N. We probe a known range.
    state_map = {
        Atspi.StateType.FOCUSABLE: "focusable",
        Atspi.StateType.FOCUSED: "focused",
        Atspi.StateType.EDITABLE: "editable",
        Atspi.StateType.SHOWING: "showing",
        Atspi.StateType.VISIBLE: "visible",
        Atspi.StateType.ENABLED: "enabled",
        Atspi.StateType.SENSITIVE: "sensitive",
        Atspi.StateType.SELECTED: "selected",
        Atspi.StateType.CHECKED: "checked",
        Atspi.StateType.PRESSED: "pressed",
    }
    for st, label in state_map.items():
        try:
            if state_set.contains(st):
                states.add(label)
        except Exception:
            pass
    return states


# ── In-JVM agent client ─────────────────────────────────────────────────

def _agent_request(line, timeout=10.0):
    """Send a single line to the input agent and read its single-line response.
    Opens a fresh Unix socket connection per request — the agent is
    single-threaded and the controller is its only client, so connection
    pooling buys us nothing."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(AGENT_SOCKET)
        s.sendall((line + "\n").encode("utf-8"))
        # Read until newline
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        return buf.split(b"\n", 1)[0].decode("utf-8", errors="replace")
    finally:
        s.close()


def agent_wait_ready(timeout=60):
    """Wait until the agent's Unix socket exists and answers PING."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(AGENT_SOCKET):
            try:
                resp = _agent_request("PING", timeout=2)
                if resp.startswith("OK"):
                    return True
            except Exception:
                pass
        time.sleep(0.3)
    return False


def agent_get_pid():
    """Return the JVM's OS process ID via the agent, or None on failure.

    Used to disambiguate "this controller's Gateway JVM" from any other
    Gateway JVM running in the same container (dual-mode case: both live
    and paper JVMs appear as 'IBKR Gateway' in the AT-SPI desktop tree).
    """
    try:
        resp = _agent_request("GET_PID", timeout=2)
    except Exception as e:
        log.warning(f"agent GET_PID failed: {type(e).__name__}: {e}")
        return None
    if not resp.startswith("OK "):
        log.warning(f"agent GET_PID unexpected response: {resp!r}")
        return None
    try:
        return int(resp[3:].strip())
    except ValueError:
        log.warning(f"agent GET_PID non-integer response: {resp!r}")
        return None


def agent_settext(name, text):
    """Set text on a Swing JTextComponent by accessible name. Returns True on success."""
    try:
        resp = _agent_request(f"SETTEXT {name} {text}")
    except Exception as e:
        log.error(f"agent SETTEXT {name!r}: {type(e).__name__}: {e}")
        return False
    if resp.startswith("OK"):
        return True
    log.error(f"agent SETTEXT {name!r}: {resp}")
    return False


def agent_gettext(name):
    """Read text from a Swing JTextComponent by accessible name. Returns string or None."""
    try:
        resp = _agent_request(f"GETTEXT {name}")
    except Exception as e:
        log.error(f"agent GETTEXT {name!r}: {type(e).__name__}: {e}")
        return None
    if resp.startswith("OK "):
        return resp[3:]
    if resp == "OK":
        return ""
    log.error(f"agent GETTEXT {name!r}: {resp}")
    return None


def agent_click(name):
    """Click an AbstractButton by accessible name. Returns True on success."""
    try:
        resp = _agent_request(f"CLICK {name}")
    except Exception as e:
        log.error(f"agent CLICK {name!r}: {type(e).__name__}: {e}")
        return False
    if resp.startswith("OK"):
        return True
    log.error(f"agent CLICK {name!r}: {resp}")
    return False


def agent_settext_login_user(text):
    """Set the Gateway login frame's username via in-JVM role-based lookup.

    v0.4.2: bypasses the name-based SETTEXT path. After a failed login
    attempt, the username field can become a JComboBox autocomplete
    editor whose JTextField child has null AccessibleName, so SETTEXT
    by name ("Username") returns not_found on re-drive from
    attempt_inplace_relogin. The agent-side command finds the field by
    Swing type (first editable non-password JTextComponent on the
    window containing a JPasswordField). Waits up to 10s for the field
    to become editable (disabled during Gateway's "Attempt N:
    connecting to server" retry animation).
    """
    try:
        resp = _agent_request(f"SETTEXT_LOGIN_USER {text}")
    except Exception as e:
        log.error(f"agent SETTEXT_LOGIN_USER: {type(e).__name__}: {e}")
        return False
    if resp.startswith("OK"):
        return True
    log.error(f"agent SETTEXT_LOGIN_USER: {resp}")
    return False


def agent_settext_login_password(text):
    """Set the Gateway login frame's password via in-JVM role-based lookup.

    Symmetric to agent_settext_login_user. Password's accessible name
    is currently stable, but role-based lookup (match on JPasswordField
    Swing type) future-proofs against name drift.
    """
    try:
        resp = _agent_request(f"SETTEXT_LOGIN_PASSWORD {text}")
    except Exception as e:
        log.error(f"agent SETTEXT_LOGIN_PASSWORD: {type(e).__name__}: {e}")
        return False
    if resp.startswith("OK"):
        return True
    log.error(f"agent SETTEXT_LOGIN_PASSWORD: {resp}")
    return False


def agent_wait_login_frame(timeout_ms=120_000):
    """Block until a showing Window containing a JPasswordField exists
    AND no other modal dialog is blocking it. Returns True on success,
    False on timeout.

    v0.4.3: replaces pyatspi ``wait_for(app, "password text")`` in
    attempt_inplace_relogin. The AT-SPI tree filters the login frame's
    password-text role while Gateway's "Attempt N: connecting to
    server" modal is up, so the old 30s wait would time out before
    Gateway's internal retry self-cleared (typical ~60s). Swing's
    ``isShowing()`` remains truthful regardless of modal overlay, and
    the Java agent additionally checks that no modal dialog is
    blocking — only returns OK when the login frame is actually
    interactable.
    """
    try:
        resp = _agent_request(
            f"WAIT_LOGIN_FRAME {int(timeout_ms)}",
            timeout=int(timeout_ms / 1000) + 10,
        )
    except Exception as e:
        log.error(f"agent WAIT_LOGIN_FRAME: {type(e).__name__}: {e}")
        return False
    if resp.startswith("OK"):
        return True
    log.error(f"agent WAIT_LOGIN_FRAME: {resp}")
    return False


def agent_settext_in_window(title_substring, text):
    """Type text into the first editable JTextComponent of the first visible
    window whose title contains the substring. Used for fields that have
    no accessible name (e.g. the Second Factor Authentication TOTP input)."""
    try:
        resp = _agent_request(f"SETTEXT_IN_WIN {title_substring}|{text}")
    except Exception as e:
        log.error(f"agent SETTEXT_IN_WIN {title_substring!r}: {type(e).__name__}: {e}")
        return False
    if resp.startswith("OK"):
        return True
    log.error(f"agent SETTEXT_IN_WIN {title_substring!r}: {resp}")
    return False


def agent_click_in_window(title_substring, button_text):
    """Click a button (matched by getText() or accessible name) inside
    a window whose title contains the substring. Used for dialogs whose
    button identifiers overlap with main window buttons."""
    try:
        resp = _agent_request(f"CLICK_IN_WIN {title_substring}|{button_text}")
    except Exception as e:
        log.error(f"agent CLICK_IN_WIN {title_substring!r}: {type(e).__name__}: {e}")
        return False
    if resp.startswith("OK"):
        return True
    log.error(f"agent CLICK_IN_WIN {title_substring!r}: {resp}")
    return False


def agent_jtree_select_path(title_substring, path):
    """Select a JTree node by path. Path is slash-separated, each
    component matching node.toString(). Used to drive Gateway's
    ConfigurationTree (Configure → Settings dialog) to a specific
    section like 'API/Settings' or 'Lock and Exit'."""
    try:
        resp = _agent_request(f"JTREE_SELECT_PATH {title_substring}|{path}")
    except Exception as e:
        log.error(f"agent JTREE_SELECT_PATH {path!r}: {type(e).__name__}: {e}")
        return False
    if resp.startswith("OK"):
        return True
    log.error(f"agent JTREE_SELECT_PATH {path!r}: {resp}")
    return False


def agent_jcheck(title_substring, name, desired):
    """Set a toggle-style button (JCheckBox/JRadioButton/JToggleButton)
    to the desired state. Returns True on success, False on error.
    'desired' is a Python bool."""
    state = "true" if desired else "false"
    try:
        resp = _agent_request(f"JCHECK {title_substring}|{name}|{state}")
    except Exception as e:
        log.error(f"agent JCHECK {name!r}: {type(e).__name__}: {e}")
        return False
    if resp.startswith("OK"):
        return True
    log.error(f"agent JCHECK {name!r}: {resp}")
    return False


def agent_settext_by_label(title_substring, label_text, value):
    """Set a text field's value by matching against an adjacent
    JLabel's text. Used for config fields like 'Master API client ID'
    where the JSpinner's editor has no accessible name of its own but
    sits next to a descriptive JLabel. Returns True on success."""
    try:
        resp = _agent_request(
            f"SETTEXT_BY_LABEL {title_substring}|{label_text}|{value}")
    except Exception as e:
        log.error(f"agent SETTEXT_BY_LABEL {label_text!r}: {type(e).__name__}: {e}")
        return False
    if resp.startswith("OK"):
        return True
    log.error(f"agent SETTEXT_BY_LABEL {label_text!r}: {resp}")
    return False


def _agent_multiline(command, timeout=5):
    """Send a command and read the multi-line response (terminated by 'END')."""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(AGENT_SOCKET)
        s.sendall((command + "\n").encode("utf-8"))
        buf = b""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            chunk = s.recv(8192)
            if not chunk:
                break
            buf += chunk
            if buf.rstrip().endswith(b"END"):
                break
        s.close()
        return buf.decode("utf-8", errors="replace")
    except Exception as e:
        log.error(f"agent {command.split()[0]}: {type(e).__name__}: {e}")
        return ""


def agent_list(filter_substring=""):
    """Ask the agent for all visible text components and buttons.

    Returns a tuple (text_names, button_names) — both sets of strings.

    Used for live state detection after Gateway transitions away from
    the login frame. AT-SPI's view of the application accessible can go
    stale after a frame teardown (child_count returns -1, tree-walking
    finds nothing) but the in-JVM agent always sees the live Swing
    component tree via Window.getWindows().
    """
    raw = _agent_multiline(f"LIST {filter_substring}")
    text_names = set()
    button_names = set()
    for line in raw.splitlines():
        if line.startswith("text "):
            n = line[5:]
            if n != "(null)":
                text_names.add(n)
        elif line.startswith("button "):
            n = line[7:]
            if n != "(null)" and n != "":
                button_names.add(n)
    return text_names, button_names


def agent_windows():
    """Ask the agent for all currently-showing top-level windows.

    Returns a list of (type, title, modal) tuples. Critical for spotting
    blocking dialogs (existing-session, EULA, info popups) that have no
    text fields and only an OK button — we can't distinguish them from
    LIST output alone.
    """
    raw = _agent_multiline("WINDOWS")
    out = []
    for line in raw.splitlines():
        if line in ("OK", "END") or not line:
            continue
        # Format: "<type> | <title> | modal=<bool>"
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 3:
            type_ = parts[0]
            title = parts[1]
            modal = parts[2].endswith("true")
            out.append((type_, title, modal))
    return out


def agent_labels(filter_substring=""):
    """Ask the agent for all visible JLabel text content (HTML stripped).

    Returns a list of (window_title, label_text) tuples. Used to read
    dialog message bodies — e.g. distinguishing the existing-session
    dialog from a wrong-credentials dialog when both expose only an OK
    button.
    """
    raw = _agent_multiline(f"LABELS {filter_substring}")
    out = []
    for line in raw.splitlines():
        if line in ("OK", "END") or not line:
            continue
        # Format: "[<window_title>] <label_text>"
        if line.startswith("[") and "]" in line:
            close = line.index("]")
            wtitle = line[1:close]
            text = line[close + 1:].strip()
            out.append((wtitle, text))
    return out


def agent_window(title_substring=""):
    """Dump the full component tree of windows whose title contains the
    given substring (empty = all visible windows). Returns the raw
    multi-line string. Captures text from JLabel, JTextComponent
    (including JTextArea/JEditorPane/JTextPane that LABELS misses),
    and AbstractButton."""
    return _agent_multiline(f"WINDOW {title_substring}", timeout=10)


def _dump_tree(node, depth=0, max_depth=15):
    """Diagnostic-only — dump a subtree to the log."""
    if depth > max_depth * 2:
        return
    role = safe(lambda: node.get_role_name(), "?")
    name = safe(lambda: node.get_name(), "") or ""
    desc = safe(lambda: node.get_description(), "") or ""
    parts = [role]
    if name: parts.append(f'name={name!r}')
    if desc: parts.append(f'desc={desc!r}')
    txt = _read_text(node)
    if txt: parts.append(f'text={txt!r}')
    log.info(" " * depth + " ".join(parts))
    n = safe(lambda: node.get_child_count(), 0) or 0
    for i in range(n):
        c = safe(lambda: node.get_child_at_index(i))
        if c is not None:
            _dump_tree(c, depth + 2, max_depth)


def _read_text(node):
    """Read the current contents of a text node via the Text interface."""
    try:
        n_chars = Atspi.Text.get_character_count(node)
        if n_chars <= 0:
            return ""
        return Atspi.Text.get_text(node, 0, n_chars) or ""
    except Exception:
        return ""


def set_text(node, text):
    """Set text on an editable node by delegating to the in-JVM agent.

    External text input (AT-SPI EditableText, xdotool XTest, XSendEvent)
    all fail against Gateway's hardened login form. Phase 1 spike proved
    that only in-JVM JTextField.setText() reaches the field. The agent
    runs inside Gateway's JVM (loaded via -javaagent:) and exposes the
    operation over a Unix socket. See spike/PHASE1_FINDINGS.md for the
    full diagnostic story.

    The agent looks up components by AccessibleContext.getAccessibleName(),
    which is the same name AT-SPI exposes — so the AT-SPI node we found
    via Python and the Java component the agent finds are the same object.
    """
    role = safe(lambda: node.get_role_name(), "?")
    is_password = role == "password text"
    name = safe(lambda: node.get_name(), "") or ""

    if not name:
        log.error(f"set_text on {role}: node has no accessible name — can't dispatch to agent")
        return False

    if not agent_settext(name, text):
        return False

    # Verify (skip for passwords — readback is masked anyway).
    if is_password:
        log.info(f"set_text on {role} {name!r}: ok via agent (verify skipped — masked)")
        return True

    actual = agent_gettext(name)
    if actual == text:
        log.info(f"set_text on {role} {name!r}: ok via agent (verified)")
        return True
    log.warning(f"set_text on {role} {name!r}: agent reported OK but readback was {actual!r}")
    # Trust the agent's OK over the readback — the agent is more reliable.
    return True


def click(node):
    """Perform the default action (action 0) on a node. For buttons this is 'click'."""
    role = safe(lambda: node.get_role_name(), "?")
    name = safe(lambda: node.get_name(), "") or ""
    try:
        n_actions = safe(lambda: Atspi.Action.get_n_actions(node), 0) or 0
        if n_actions == 0:
            log.error(f"click on {role}:{name!r}: no actions exposed")
            return False
        ok = Atspi.Action.do_action(node, 0)
        log.info(f"click on {role}:{name!r}: {'ok' if ok else 'failed'}")
        return ok
    except Exception as e:
        log.error(f"click on {role}:{name!r}: {e}")
        return False


# ── Gateway launch ──────────────────────────────────────────────────────

def find_gateway_launcher():
    """Locate the install4j ibgateway or tws launcher script. Returns
    absolute path.

    Controlled by the GATEWAY_OR_TWS env var ('gateway' default, 'tws'
    switches to TWS). For Gateway, the install path is
      $TWS_PATH/ibgateway/<version>/ibgateway
    For TWS it's
      $TWS_PATH/tws/<version>/tws
    matching the subdir / binary name to the product. Phase 2.3 adds
    this switch so the same controller drives either product from the
    same image with different env vars.
    """
    product = os.environ.get("GATEWAY_OR_TWS", "gateway").strip().lower()
    if product == "tws":
        subdir = "tws"
        binary = "tws"
    else:
        subdir = "ibgateway"
        binary = "ibgateway"

    if TWS_VERSION:
        candidate = os.path.join(TWS_PATH, subdir, TWS_VERSION, binary)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    # Fallback: pick the highest version directory
    root = os.path.join(TWS_PATH, subdir)
    if not os.path.isdir(root):
        return None
    for v in sorted(os.listdir(root), reverse=True):
        candidate = os.path.join(root, v, binary)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def apply_warm_state():
    """If GATEWAY_WARM_STATE points at a directory, copy its contents into
    JTS_CONFIG_DIR so Gateway starts with previously-saved state.

    This is the workaround for the cold-start SSL handshake failure: the
    user's account is served by a regional server (e.g. cdc1.ibllc.com)
    that's NOT the default `ndc1.ibllc.com` Gateway tries on first boot.
    The warm jts.ini has the correct `Peer=` and `SupportsSSL=` cache,
    so Gateway routes to the right server. Recent autorestart tokens in
    the warm state may also let Gateway skip the full re-auth.
    """
    # IBC-compat: TWS_COLD_RESTART=yes forces a cold restart by skipping
    # the warm state copy entirely. Users who suspect stale state is
    # causing problems can set this to force Gateway to start fresh
    # (and also to clear any existing jts.ini, because ensure_jts_ini
    # will regenerate it).
    if _coerce_yes_no(os.environ.get("TWS_COLD_RESTART", "")) is True:
        log.info("TWS_COLD_RESTART=yes — skipping warm state application")
        return

    if not WARM_STATE_DIR:
        return
    if not os.path.isdir(WARM_STATE_DIR):
        log.warning(f"GATEWAY_WARM_STATE={WARM_STATE_DIR} is not a directory")
        return
    if not os.path.isabs(WARM_STATE_DIR):
        log.warning(f"GATEWAY_WARM_STATE={WARM_STATE_DIR} is not an absolute "
                    "path — refusing to apply warm state from a relative path "
                    "to avoid path-resolution ambiguity. Pass an absolute "
                    "path (e.g. /home/ibgateway/warm-state).")
        return
    # Reject absurd paths — / and single-component /root, /etc, /var that
    # would pull in vast amounts of the host filesystem. Legitimate warm
    # state is always under the user's home or a mounted volume.
    suspicious = {"/", "/etc", "/root", "/home", "/var", "/usr", "/tmp"}
    if os.path.realpath(WARM_STATE_DIR) in suspicious:
        log.error(f"GATEWAY_WARM_STATE={WARM_STATE_DIR} resolves to a "
                  f"system directory. Refusing to proceed — this is almost "
                  f"certainly a misconfiguration. Set GATEWAY_WARM_STATE to "
                  f"a dedicated warm-state directory only.")
        return
    # Cap the warm-state size to something reasonable. Gateway's Jts state
    # is typically <10 MB; anything much larger is probably user error or
    # a path pointing at the wrong directory.
    total_bytes = 0
    WARM_STATE_MAX_BYTES = 500 * 1024 * 1024  # 500 MB
    try:
        for root, _, files in os.walk(WARM_STATE_DIR, followlinks=False):
            for f in files:
                try:
                    total_bytes += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
                if total_bytes > WARM_STATE_MAX_BYTES:
                    break
            if total_bytes > WARM_STATE_MAX_BYTES:
                break
    except Exception as e:
        log.warning(f"GATEWAY_WARM_STATE: couldn't measure directory size: {e}")
        return
    if total_bytes > WARM_STATE_MAX_BYTES:
        log.error(f"GATEWAY_WARM_STATE={WARM_STATE_DIR} contains "
                  f"more than {WARM_STATE_MAX_BYTES // (1024 * 1024)} MB. "
                  f"Refusing to copy — this is far larger than any real "
                  f"Jts state directory. Check the path.")
        return
    import shutil
    log.info(f"Applying warm state from {WARM_STATE_DIR} → {JTS_CONFIG_DIR} "
             f"({total_bytes // 1024} KB)")
    os.makedirs(JTS_CONFIG_DIR, exist_ok=True)
    for item in os.listdir(WARM_STATE_DIR):
        # Skip log files and Gateway installation directories — those
        # belong to the test container's own version, not the warm state.
        if item.startswith("launcher") and item.endswith(".log"):
            continue
        if item == "ibgateway":
            continue
        src = os.path.join(WARM_STATE_DIR, item)
        dst = os.path.join(JTS_CONFIG_DIR, item)
        try:
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
            log.info(f"  copied {item}")
        except Exception as e:
            log.error(f"  failed to copy {item}: {e}")


def ensure_jts_ini():
    """Ensure a usable jts.ini is in place before Gateway starts.

    Resolution order (highest precedence first):
      1. TWS_SERVER (or TWS_SERVER_PAPER for paper mode) — explicit user
         choice. The controller writes a complete jts.ini with the
         regional server, port routing, AND a SupportsSSL cache entry.
         Overwrites any existing jts.ini (e.g. the empty file that
         run.sh's apply_settings may have left us).
      2. Existing jts.ini (from apply_warm_state(), from run.sh's
         apply_settings rendering a user-provided template, or from a
         mounted volume) — leave alone.
      3. Minimal default — let Gateway figure it out.

    The SupportsSSL cache entry is critical. Without it, Gateway
    re-negotiates SSL with IBKR's misc URLs server on port 4000 on
    every boot, and the negotiation sometimes fails with
    "Remote host terminated the handshake". Pre-populating the cache
    with today's date tells Gateway "SSL is known to work on this
    endpoint, skip negotiation".
    """
    jts_ini = os.path.join(JTS_CONFIG_DIR, "jts.ini")
    os.makedirs(JTS_CONFIG_DIR, exist_ok=True)
    time_zone = os.environ.get("TIME_ZONE", "Etc/UTC")

    if TWS_SERVER:
        import datetime
        cache_date = datetime.datetime.now().strftime("%Y%m%d")
        existed = os.path.exists(jts_ini)
        log.info(f"Writing jts.ini for server {TWS_SERVER} (overwriting={existed})")
        # Do NOT set RemoteHostOrderRouting here. Auth and order routing
        # can be on DIFFERENT IBKR regional servers (e.g. auth on cdc1,
        # orders on ndc1). Gateway discovers the order routing endpoint
        # from the auth server's response and auto-populates
        # RemoteHostOrderRouting in jts.ini after a successful login.
        # Writing it to the same value as Peer (which is what we have)
        # causes a "No Internet connection" retry loop on accounts where
        # the two are different.
        #
        # Bug found via internet research: confirmed by mvberg/ib-gateway-docker
        # and the user's own warm-state jts.ini (Peer=cdc1, RemoteHost=ndc1).
        content = (
            "[IBGateway]\n"
            "WriteDebug=false\n"
            "TrustedIPs=127.0.0.1\n"
            "ApiOnly=true\n"
            "LocalServerPort=4000\n"
            "\n"
            "[Logon]\n"
            f"TimeZone={time_zone}\n"
            "Locale=en\n"
            "displayedproxymsg=1\n"
            "UseSSL=true\n"
            "s3store=true\n"
            "useRemoteSettings=false\n"
            # Pre-populated SSL support cache. Tells Gateway "SSL works
            # on this endpoint, don't re-negotiate" — the missing cache
            # is what causes SSLHandshakeException on cold-start misc
            # URLs requests.
            f"SupportsSSL={TWS_SERVER}:4000,true,{cache_date},false\n"
            "\n"
            "[Communication]\n"
            f"Peer={TWS_SERVER}:4001\n"
            "Region=usr\n"
        )
        with open(jts_ini, "w") as f:
            f.write(content)
        log.info(f"Wrote {jts_ini}")
        return

    if os.path.exists(jts_ini):
        log.info(f"Existing jts.ini at {jts_ini} — leaving in place")
        return

    log.warning("TWS_SERVER not set — writing minimal jts.ini, Gateway will use defaults")
    content = (
        "[IBGateway]\n"
        "WriteDebug=false\n"
        "TrustedIPs=127.0.0.1\n"
        "ApiOnly=true\n"
        "\n"
        "[Logon]\n"
        f"TimeZone={time_zone}\n"
        "Locale=en\n"
        "displayedproxymsg=1\n"
        "UseSSL=true\n"
        "s3store=true\n"
    )
    with open(jts_ini, "w") as f:
        f.write(content)
    log.info(f"Wrote minimal {jts_ini}")


def launch_gateway():
    """Spawn the Gateway JVM via the install4j launcher.

    Sets INSTALL4J_ADD_VM_PARAMS to:
      - inject the java-atk-wrapper jar onto the boot classpath so the
        ATK assistive technology can load
      - override -DjtsConfigDir, because the bundled launcher script has
        a literal unsubstituted `${installer:jtsConfigDir}` placeholder
        that would otherwise route Gateway's writes to /root/Jts (which
        fails for the non-root ibgateway user)
    """
    launcher = find_gateway_launcher()
    if launcher is None:
        log.error(f"No Gateway launcher found under {TWS_PATH}/ibgateway")
        sys.exit(1)
    log.info(f"Gateway launcher: {launcher}")
    log.info(f"Gateway config dir (jtsConfigDir): {JTS_CONFIG_DIR}")

    # Apply warm state BEFORE writing the default jts.ini, so a warm
    # jts.ini wins over the default and routes Gateway to the right
    # regional server.
    apply_warm_state()
    ensure_jts_ini()

    env = os.environ.copy()
    # Java module-access flags required by Gateway's auth and UI code.
    # Without these, reflective access to internal JDK classes fails
    # silently and AuthDispatcher.connect never fires — the auth request
    # is never sent, producing a 20-second silent timeout.
    #
    # These are the same flags IBC's ibcstart.sh passes. The install4j
    # launcher's .vmoptions file does NOT include them; IBC adds them
    # externally. We must do the same via INSTALL4J_ADD_VM_PARAMS.
    #
    # Root cause: Gateway 10.45+ uses reflection into java.desktop and
    # java.base internals for Swing threading, AWT event dispatch, and
    # the auth connection handshake. Java 17's module system blocks
    # this by default. The --add-opens/--add-exports flags grant the
    # access Gateway expects.
    module_access = [
        "--add-opens=java.base/java.util=ALL-UNNAMED",
        "--add-opens=java.base/java.util.concurrent=ALL-UNNAMED",
        "--add-exports=java.base/sun.util=ALL-UNNAMED",
        "--add-exports=java.desktop/com.sun.java.swing.plaf.motif=ALL-UNNAMED",
        "--add-opens=java.desktop/java.awt=ALL-UNNAMED",
        "--add-opens=java.desktop/java.awt.dnd=ALL-UNNAMED",
        "--add-opens=java.desktop/javax.swing=ALL-UNNAMED",
        "--add-opens=java.desktop/javax.swing.event=ALL-UNNAMED",
        "--add-opens=java.desktop/javax.swing.plaf.basic=ALL-UNNAMED",
        "--add-opens=java.desktop/javax.swing.table=ALL-UNNAMED",
        "--add-opens=java.desktop/sun.awt=ALL-UNNAMED",
        "--add-exports=java.desktop/sun.awt.X11=ALL-UNNAMED",
        "--add-exports=java.desktop/sun.swing=ALL-UNNAMED",
        "--add-opens=javafx.graphics/com.sun.javafx.application=ALL-UNNAMED",
        "--add-exports=javafx.media/com.sun.media.jfxmedia=ALL-UNNAMED",
        "--add-exports=javafx.media/com.sun.media.jfxmedia.events=ALL-UNNAMED",
        "--add-exports=javafx.media/com.sun.media.jfxmedia.locator=ALL-UNNAMED",
        "--add-exports=javafx.media/com.sun.media.jfxmediaimpl=ALL-UNNAMED",
        "--add-exports=javafx.web/com.sun.javafx.webkit=ALL-UNNAMED",
        "--add-opens=jdk.management/com.sun.management.internal=ALL-UNNAMED",
    ]
    vm_params = module_access + [
        "-Xbootclasspath/a:/usr/share/java/java-atk-wrapper.jar",
        f"-DjtsConfigDir={JTS_CONFIG_DIR}",
    ]
    if os.path.exists(AGENT_JAR):
        vm_params.append(f"-javaagent:{AGENT_JAR}={AGENT_SOCKET}")
        log.info(f"Loading input agent: {AGENT_JAR} (socket={AGENT_SOCKET})")
        # Clear any stale socket from a previous run
        try:
            os.unlink(AGENT_SOCKET)
        except FileNotFoundError:
            pass
    else:
        log.warning(f"Input agent jar not found at {AGENT_JAR} — text input will fail")
    env["INSTALL4J_ADD_VM_PARAMS"] = " ".join(vm_params)

    # The install4j launcher has a literal unsubstituted placeholder
    # `-DjtsConfigDir=${installer:jtsConfigDir}` that it passes to Java
    # BEFORE our INSTALL4J_ADD_VM_PARAMS. Java uses the FIRST -D for
    # any given property, so our `-DjtsConfigDir=...` override gets
    # ignored and Gateway reads a nonexistent path.
    #
    # Fix: pass `-VjtsConfigDir=<path>` as a command-line argument to
    # the install4j launcher. The `-V` flag sets the installer variable
    # BEFORE the launcher constructs the Java command line, so the
    # `${installer:jtsConfigDir}` placeholder gets correctly substituted
    # to our desired path. Same mechanism for `installerType`.
    launcher_args = [
        launcher,
        f"-VjtsConfigDir={JTS_CONFIG_DIR}",
        "-VinstallerType=standalone",
    ]

    proc = subprocess.Popen(
        launcher_args,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    log.info(f"Gateway PID: {proc.pid}")
    return proc


# ── Dialog handlers ─────────────────────────────────────────────────────

def handle_login(app):
    """Drive the login dialog: select trading mode, type credentials, click Log In."""
    log.info("Waiting for login dialog (password text)")
    pw_field = wait_for(app, "password text", timeout=120)
    if pw_field is None:
        log.error("Login dialog never appeared (no password text role)")
        return False
    log.info("Login dialog detected")

    # Trading mode — must select BEFORE typing credentials, because Gateway
    # may reset the form when toggling.
    target = "Paper Trading" if TRADING_MODE == "paper" else "Live Trading"
    mode_btn = find_descendant(app, role="toggle button", name=target)
    if mode_btn is None:
        log.warning(f"Trading mode button {target!r} not found, continuing")
    else:
        if "checked" not in get_states(mode_btn):
            log.info(f"Selecting trading mode: {target}")
            click(mode_btn)
            time.sleep(0.5)
        else:
            log.info(f"Trading mode already {target}")

    # Username — v0.4.2: route through in-JVM role-based lookup instead
    # of AT-SPI name-based find_descendant + set_text. The username
    # field's accessible name mutates between login attempts (can
    # become a JComboBox autocomplete editor with null AccessibleName),
    # breaking the old name-based SETTEXT path on attempt_inplace_relogin
    # re-drive. The new agent command matches on Swing type (first
    # editable non-password JTextComponent on the login frame).
    user_ok = agent_settext_login_user(USERNAME)
    if not user_ok and not TEST_MODE:
        return False

    # Password — symmetric role-based lookup. JPasswordField Swing type
    # is stable across Gateway versions, so this is equivalent to the
    # old name-based path but future-proof against accessible-name drift.
    pw_ok = agent_settext_login_password(PASSWORD)
    if not pw_ok and not TEST_MODE:
        return False

    if TEST_MODE and (not user_ok or not pw_ok):
        log.warning("set_text reported failure but TEST_MODE — proceeding to click Log In so we can observe Gateway response")

    # Log In button. Gateway renames the button based on trading mode:
    #   Live Trading  → "Log In"
    #   Paper Trading → "Paper Log In"
    login_btn = (find_descendant(app, role="push button", name="Log In")
                 or find_descendant(app, role="push button", name="Paper Log In"))
    if login_btn is None:
        log.error("Log In / Paper Log In button not found — dumping current tree")
        _dump_tree(app, max_depth=15)
        return False
    log.info(f"Found login button: {login_btn.get_name()!r}")
    if not click(login_btn):
        return False

    log.info("Log In clicked successfully")
    return True


def handle_post_login_dialogs(app):
    """Inspect any modal dialog that appears right after the Log In click,
    handle the ones we recognize, and leave the rest alone.

    The 'Gateway' modal that we previously observed is a
    "Connecting to server..." progress dialog — we should NOT click OK on
    it because that cancels the login. Other modals (existing-session-
    detected, EULA acknowledgement) DO need clicking.

    Strategy:
      1. Wait briefly for any dialog to render
      2. For each modal, dump its full content via the agent's WINDOW
         command and log it for diagnostics
      3. Recognize known dialogs by body text and click the right button
      4. Leave unknown dialogs alone — the downstream waits will catch
         dialog dismissal naturally
    """
    log.info("Inspecting post-login dialogs")
    # Poll for dialog appearance. The 'Existing session detected' dialog
    # can take 3–5 seconds to show up after Log In is clicked (Gateway
    # has to do a network round-trip first). Waiting 2s was enough for
    # the "no dialog" case but missed late-arriving modals.
    modal_dialogs = []
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        time.sleep(0.5)
        windows = agent_windows()
        modal_dialogs = [(t, title) for t, title, modal in windows if modal]
        if modal_dialogs:
            break
    if not modal_dialogs:
        log.info("No modal dialogs after login")
        return True

    log.info(f"Modal dialog(s) detected: {[(t, _redact_logs(title)) for t, title in modal_dialogs]}")
    for type_, title in modal_dialogs:
        dump = agent_window(title)
        # Window dumps are big — emit at debug level so they only
        # appear when the user explicitly asks for them with
        # CONTROLLER_DEBUG=1. Redact account numbers defensively;
        # even at debug level, users share logs when asking for help.
        log.debug(f"=== Window dump: {_redact_logs(repr(title))} ===")
        for line in dump.split("\n"):
            if line and line not in ("OK", "END"):
                log.debug(f"  {_redact_logs(line)}")
        log.debug("=== End window dump ===")

        # Recognize known dialogs by body text content
        body_lower = dump.lower()
        if "existing session" in body_lower or "another session" in body_lower:
            log.info("Recognized: existing-session-detected dialog")
            if not handle_existing_session_dialog():
                log.error("Existing-session dialog handling failed")
                return False
        else:
            log.info(f"Unrecognized modal — leaving in place to let Gateway flow proceed")

    return True


# Ping-pong mitigation: track recent 'Continue Login' clicks so we can
# detect two containers fighting for the same IBKR account and back off
# instead of flapping forever. Module-level ring buffer of monotonic
# click timestamps, bounded in size.
_existing_session_click_times = []
_EXISTING_SESSION_BACKOFF_WINDOW = 300.0  # 5-minute window
_EXISTING_SESSION_BACKOFF_THRESHOLD = 5   # clicks in the window that trigger backoff
_EXISTING_SESSION_BACKOFF_SLEEP = 60.0    # seconds to wait once tripped


def handle_existing_session_dialog():
    """Click the appropriate button on Gateway's 'Existing session detected'
    dialog based on the EXISTING_SESSION_DETECTED_ACTION env var.

    IBC supports four actions for this dialog (from the IBC documentation):
      primary          — accept this session as the primary, kicking out
                         the existing one (the most common automation
                         setting; what most production users want)
      primaryoverride  — same as primary but always overrides
      secondary        — connect as secondary, leaving the existing
                         session intact
      manual           — leave the dialog up; the user must click

    The modern Gateway dialog (verified on 10.45.1c) has buttons
    "Continue Login" (= primary / accept and kick the other session)
    and "Cancel" (abort this login, leave the other session alone).
    There is no separate "connect as secondary" button — the workflow
    is "use the already-running session, don't connect here" which maps
    onto Cancel.

    For headless operation, 'primary' is the typical default.

    Ping-pong backoff: if this handler fires more than
    _EXISTING_SESSION_BACKOFF_THRESHOLD times in
    _EXISTING_SESSION_BACKOFF_WINDOW seconds, we're probably in a fight
    with another container that keeps reconnecting as the same account.
    We sleep for _EXISTING_SESSION_BACKOFF_SLEEP seconds before
    clicking again to give the user a chance to intervene without
    flapping forever.
    """
    action = os.environ.get("EXISTING_SESSION_DETECTED_ACTION", "primary").lower()
    log.info(f"existing-session dialog: action={action}")

    # Ping-pong detection
    now = time.monotonic()
    _existing_session_click_times[:] = [
        t for t in _existing_session_click_times
        if now - t < _EXISTING_SESSION_BACKOFF_WINDOW
    ]
    _existing_session_click_times.append(now)
    if len(_existing_session_click_times) >= _EXISTING_SESSION_BACKOFF_THRESHOLD:
        log.warning(f"existing-session: handled this dialog "
                    f"{len(_existing_session_click_times)} times in "
                    f"{int(_EXISTING_SESSION_BACKOFF_WINDOW)}s — another "
                    f"container or app is probably reconnecting as the same "
                    f"account. Backing off for {int(_EXISTING_SESSION_BACKOFF_SLEEP)}s.")
        time.sleep(_EXISTING_SESSION_BACKOFF_SLEEP)
        _existing_session_click_times.clear()

    # Ordered candidates: try the most specific/modern button text first,
    # then fall back to older labels for compatibility with older Gateway
    # versions that may have had different button naming.
    button_candidates_by_action = {
        "primary":         ["Continue Login", "Primary", "Yes", "OK", "Continue"],
        "primaryoverride": ["Continue Login", "Primary", "Yes", "OK", "Continue"],
        "secondary":       ["Cancel", "Secondary", "No"],
        "manual":          [],  # do nothing
    }
    candidates = button_candidates_by_action.get(action, ["OK"])
    if not candidates:
        log.info("action=manual — leaving dialog for user")
        return True

    # Scope the click to the existing-session dialog window specifically,
    # via CLICK_IN_WIN. Using the global agent_list + agent_click would
    # risk hitting a button with the same label in the main window
    # (especially in dual-mode containers).
    title_substr = "Existing session"
    for cand in candidates:
        log.info(f"Clicking existing-session button {cand!r} in {title_substr!r} window")
        if agent_click_in_window(title_substr, cand):
            return True
    log.error(f"existing-session: none of {candidates!r} worked as a "
              f"button label in the dialog")
    return False


def handle_2fa(app):
    """Handle Gateway's Second Factor Authentication dialog.

    Supports two modes:

    1. **TOTP mode** (TWOFACTOR_CODE set): the controller generates a
       TOTP code and types it into the dialog's text field, then clicks
       OK. Fully automated, no human interaction.

    2. **IB Key push mode** (TWOFACTOR_CODE NOT set, but the 2FA dialog
       appears anyway): IBKR sends a push notification to the user's
       phone via the IB Key mobile app. The controller detects the
       dialog, logs "Waiting for IB Key mobile approval — approve on
       your phone", and polls for the dialog to disappear (which means
       the user approved) or the API port to open. No text entry, no
       click — just waiting for the human to approve.

       This is how ibctl handles push 2FA too: the Swing tree shows
       the dialog, the user acts on their phone, the dialog closes
       itself, and the automation proceeds.

    3. **No 2FA at all** (neither TWOFACTOR_CODE set nor dialog appears):
       common for paper accounts and autorestart-token sessions. The
       handler polls briefly for the dialog; if the API port opens
       first, it skips out.

    Selectors captured from real-credential live-mode testing:
      - Window title: "Second Factor Authentication" (JDialog, modal)
      - TOTP body label: "Enter Mobile Authenticator app code"
      - IB Key body label: typically mentions "IB Key" or "approval"
      - Input field (TOTP): first JTextComponent inside the window
      - Submit: button with text="OK"
      - Cancel: button with text="Cancel"

    Early exit: if the API port opens before the 2FA dialog appears,
    Gateway authenticated without 2FA — skip the wait entirely.
    """
    # Even without TOTP_SECRET, we still enter the polling loop so we
    # can detect and wait for IB Key push approval. The only difference
    # is: with TOTP_SECRET we type the code; without it we just wait
    # for the dialog to disappear.
    ib_key_mode = not TOTP_SECRET
    if ib_key_mode:
        log.info("No TWOFACTOR_CODE set — will watch for IB Key push dialog if it appears")

    # IBC-compat 2FA timeout configuration:
    #   TWOFA_EXIT_INTERVAL   — seconds to wait for the dialog (default 120)
    #   TWOFA_TIMEOUT_ACTION  — what to do on timeout: 'exit' (default,
    #                           controller exits non-zero), 'restart'
    #                           (call do_restart_in_place), or 'none'
    #                           (fall through to wait_for_api_port as we
    #                           did previously)
    #   RELOGIN_AFTER_TWOFA_TIMEOUT — yes/no; if yes, after a TWOFA
    #                           timeout drive handle_login again before
    #                           giving up
    try:
        wait_seconds = int(os.environ.get("TWOFA_EXIT_INTERVAL", "120"))
    except ValueError:
        log.warning("TWOFA_EXIT_INTERVAL not an integer; using 120")
        wait_seconds = 120
    timeout_action = os.environ.get("TWOFA_TIMEOUT_ACTION", "none").strip().lower()
    relogin_after_timeout = _coerce_yes_no(
        os.environ.get("RELOGIN_AFTER_TWOFA_TIMEOUT", ""))

    TWOFA_WINDOW_SUBSTR = "Second Factor"
    api_port = api_port_for_mode()
    start = time.monotonic()
    deadline = start + wait_seconds
    last_log = 0.0
    last_windows = None

    log.info(f"Waiting for 2FA dialog (window title contains {TWOFA_WINDOW_SUBSTR!r}) "
             f"for up to {wait_seconds}s")
    while time.monotonic() < deadline:
        # Early exit: API port already open → login complete, no 2FA needed
        if is_api_port_open(api_port):
            log.info(f"API port {api_port} already open — no 2FA dialog needed")
            return True

        windows = agent_windows()

        # Log window changes for diagnostics
        if windows != last_windows:
            log.info(f"2FA wait: windows -> {windows}")
            last_windows = windows

        # Opportunistic: handle an 'Existing session detected' modal if
        # one appears after our initial handle_post_login_dialogs pass.
        # It's normal for this dialog to render several seconds after
        # Log In is clicked (Gateway has to do a network round-trip to
        # discover the existing session). If we catch it here, click
        # through it and continue waiting for 2FA / API port.
        existing_session_window = None
        for type_, title, modal in windows:
            if modal and ("existing session" in title.lower()
                          or "another session" in title.lower()):
                existing_session_window = (type_, title, modal)
                break
        if existing_session_window is not None:
            log.info(f"Late existing-session dialog detected in 2FA wait: "
                     f"{existing_session_window}")
            if not handle_existing_session_dialog():
                log.error("Late existing-session dialog handling failed")
                return False
            # Re-poll windows on next iteration — the dialog is gone and
            # something new (2FA, startup parameters, etc.) may be up.
            last_windows = None
            time.sleep(0.5)
            continue

        # Look for the 2FA dialog
        two_fa_window = None
        for type_, title, modal in windows:
            if TWOFA_WINDOW_SUBSTR in title:
                two_fa_window = (type_, title, modal)
                break

        if two_fa_window is not None:
            if ib_key_mode:
                # IB Key push mode: the dialog appeared, IBKR sent a
                # push notification to the user's phone. We don't type
                # anything — just wait for the dialog to go away (which
                # means the user approved) or for the API port to open.
                log.info(f"IB Key 2FA dialog detected: {two_fa_window}")
                log.info("Waiting for IB Key mobile approval — "
                         "approve on your phone via the IB Key app")
                while time.monotonic() < deadline:
                    if is_api_port_open(api_port):
                        log.info("API port opened — IB Key approval succeeded")
                        return True
                    ws = agent_windows()
                    still_there = any(TWOFA_WINDOW_SUBSTR in t
                                      for _, t, _ in ws)
                    if not still_there:
                        log.info("2FA dialog dismissed — IB Key approval "
                                 "detected, proceeding")
                        return True
                    now = time.monotonic()
                    if now - last_log > 10:
                        log.info(f"  IB Key wait t+{int(now - start)}s: "
                                 "still waiting for approval...")
                        last_log = now
                    time.sleep(1.0)
                log.warning("IB Key approval wait timed out")
                # Fall through to the timeout-action dispatch below
                break
            else:
                # TOTP mode: type the code and click OK
                log.info(f"2FA dialog detected: {two_fa_window}")
                code = generate_totp(TOTP_SECRET)
                log.info(f"Typing TOTP code into the 2FA dialog")
                if not agent_settext_in_window(TWOFA_WINDOW_SUBSTR, code):
                    log.error("SETTEXT_IN_WIN on 2FA dialog failed")
                    return False
                time.sleep(0.5)
                log.info("Clicking OK in 2FA dialog")
                if not agent_click_in_window(TWOFA_WINDOW_SUBSTR, "OK"):
                    log.error("CLICK_IN_WIN OK on 2FA dialog failed")
                    return False
                log.info("2FA handled successfully")
                _reset_ccp_backoff()
                return True

        # Periodic status line
        now = time.monotonic()
        if now - last_log > 10:
            log.info(f"  2FA wait t+{int(now - start)}s: still waiting")
            last_log = now

        # 1s instead of 500ms — halves the agent polling rate without
        # hurting perceived latency (the dialog appears on the order
        # of seconds, not hundreds of ms).
        time.sleep(1.0)

    log.warning(f"No 2FA dialog appeared within {wait_seconds}s")

    # If Gateway's login dialog is stuck in its internal "connecting to
    # server (trying for another N seconds)" retry loop, IBKR's auth
    # server isn't accepting sessions for this account right now. The
    # 2FA dialog never appeared because the auth protocol never got
    # off the ground. Re-clicking Log In here or immediately falling
    # through to TWOFA_TIMEOUT_ACTION=restart (which re-launches the
    # JVM and re-clicks) extends the lockout. Apply the same CCP
    # exponential backoff the pre-auth path uses before the relogin
    # or restart dispatch kicks in.
    if _detect_login_stuck_connecting():
        log.warning("Login dialog stuck in 'connecting to server' retry "
                    "loop — IBKR auth server isn't accepting sessions "
                    "right now. Applying CCP backoff before retry.")
        _apply_ccp_backoff()

    # Optional relogin attempt before dispatching the timeout action.
    # Mirrors IBC's RELOGIN_AFTER_TWOFA_TIMEOUT behavior: if set, we
    # re-drive handle_login once on the assumption that the login form
    # is still up and the user got distracted/approval-expired.
    if relogin_after_timeout is True:
        log.info("RELOGIN_AFTER_TWOFA_TIMEOUT=yes — in-JVM relogin")
        fresh_app = find_app(APP_NAME_CANDIDATES, timeout=10,
                             match_pid=JVM_PID) or app
        # v0.4.1: route through attempt_inplace_relogin so the
        # "In-JVM relogin attempt..." log line fires for observability
        # and the dismiss-error-modal / skip-progress-dialog guards run.
        if attempt_inplace_relogin(fresh_app):
            # Re-enter the wait with a fresh timer
            log.info("Relogin triggered, waiting again for 2FA dialog")
            start = time.monotonic()
            deadline = start + wait_seconds
            last_windows = None
            while time.monotonic() < deadline:
                if is_api_port_open(api_port):
                    return True
                windows = agent_windows()
                for type_, title, modal in windows:
                    if TWOFA_WINDOW_SUBSTR in title:
                        code = generate_totp(TOTP_SECRET)
                        if agent_settext_in_window(TWOFA_WINDOW_SUBSTR, code):
                            time.sleep(0.5)
                            if agent_click_in_window(TWOFA_WINDOW_SUBSTR, "OK"):
                                log.info("2FA handled successfully (post-relogin)")
                                _reset_ccp_backoff()
                                return True
                time.sleep(1.0)
            log.warning("2FA dialog didn't appear in relogin window either")

    # Dispatch the configured timeout action.
    if timeout_action == "exit":
        log.error("TWOFA_TIMEOUT_ACTION=exit — controller exiting")
        try:
            os.unlink(READY_FILE)
        except FileNotFoundError:
            pass
        sys.exit(3)
    elif timeout_action == "restart":
        log.warning("TWOFA_TIMEOUT_ACTION=restart — invoking do_restart_in_place")
        if do_restart_in_place():
            return True
        log.error("Restart after 2FA timeout failed — exiting")
        sys.exit(4)
    else:
        # 'none' or unrecognized — fall through to wait_for_api_port so
        # the paper-account / autorestart-token path (no 2FA) still
        # works. This preserves Phase 1 behavior.
        log.info(f"TWOFA_TIMEOUT_ACTION={timeout_action!r} — falling through "
                 "to wait_for_api_port")
        return True


def dismiss_post_login_disclaimers(timeout=30):
    """Click through known post-login disclaimer dialogs by their button name.

    These are informational dialogs Gateway shows after login (paper-trading
    risk disclaimer, terms-of-service updates, etc.) that IBC normally
    auto-clicks via its BypassWarning-style settings.

    We're conservative about which buttons we'll click — only ones that are
    UNAMBIGUOUSLY safe-to-dismiss disclaimer buttons. We deliberately do
    NOT click bare 'OK' because earlier testing showed that clicking OK on
    the 'Gateway' connection-progress dialog actually CANCELS the login.
    """
    SAFE_BUTTONS = [
        "I understand and accept",
        "I Understand And Accept",
        "I Accept",
        "Acknowledge",
        "Accept and Continue",
    ]
    log.info("Looking for post-login disclaimer dialogs to dismiss")
    deadline = time.monotonic() + timeout
    dismissed = 0
    while time.monotonic() < deadline:
        _, buttons = agent_list()
        target = None
        for btn in SAFE_BUTTONS:
            if btn in buttons:
                target = btn
                break
        if target is None:
            if dismissed > 0:
                log.info(f"No more disclaimer buttons after dismissing {dismissed}")
            else:
                log.info("No disclaimer dialogs found")
            return
        log.info(f"Clicking disclaimer button {target!r}")
        agent_click(target)
        dismissed += 1
        time.sleep(1.5)  # let the dialog dismiss; another may appear


_DEFAULT_SAFE_DISMISS_BUTTONS = {
    "I understand and accept",
    "I Understand And Accept",
    "I Accept",
    "Acknowledge",
    "Accept and Continue",
}


def _resolve_safe_dismiss_buttons():
    """IBC-compat: let users extend the disclaimer allowlist via env.

    BYPASS_WARNING (comma-separated button labels) adds entries to the
    allowlist that dismiss_post_login_disclaimers() will click. Users
    migrating from IBC who relied on IBC's BYPASS_WARNING /
    BYPASS_NO_SECURITY_DIALOG behavior can reproduce it by setting
    BYPASS_WARNING to the exact button text they want auto-dismissed.

    Safety: we still refuse to dismiss bare "OK" even if the user
    names it — clicking OK on Gateway's "Connecting to server..."
    modal cancels the in-progress login (see ARCHITECTURE.md). That
    dialog is unrecognizable from an informational popup, so "OK" is
    permanently on the deny list.
    """
    allowlist = set(_DEFAULT_SAFE_DISMISS_BUTTONS)
    extra_raw = os.environ.get("BYPASS_WARNING", "").strip()
    if extra_raw:
        # Allow comma-separated OR semicolon-separated for convenience
        parts = [p.strip() for p in extra_raw.replace(";", ",").split(",")]
        added = []
        for p in parts:
            if not p:
                continue
            if p.strip().lower() == "ok":
                log.warning("BYPASS_WARNING: refusing to add bare 'OK' to "
                            "the dismiss allowlist — clicking OK on "
                            "Gateway's 'Connecting to server...' modal "
                            "cancels the login. Use the exact dialog "
                            "button text instead.")
                continue
            allowlist.add(p)
            added.append(p)
        if added:
            log.info(f"BYPASS_WARNING: extended dismiss allowlist with {added}")
    return allowlist


SAFE_DISMISS_BUTTONS = _resolve_safe_dismiss_buttons()


def api_port_for_mode():
    """Return Gateway's API listen port for the current trading mode."""
    return 4002 if TRADING_MODE == "paper" else 4001


def is_api_port_open(port=None):
    """Quick non-blocking probe of Gateway's API port. Returns True if a TCP
    connection succeeds. Used both for the initial readiness wait and for
    the long-running monitor loop's heartbeat."""
    if port is None:
        port = api_port_for_mode()
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except (ConnectionRefusedError, socket.timeout, OSError):
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


# ── Post-login configuration (Phase 2.2) ──────────────────────────────
#
# Honor IBC's post-login config env vars by driving Gateway's
# Configure → Settings dialog. Runs after wait_for_api_port so we know
# the config tree has live data. Non-fatal on failure.

CONFIG_WINDOW_TITLE_SUBSTR = "Trader Workstation Configuration"


def _config_open():
    """Open the Configure → Settings dialog. Idempotent — returns True
    whether it's newly opened or was already open. Returns False if the
    dialog couldn't be opened (e.g. main window not in a state to
    accept menu input)."""
    # Check if it's already open
    windows = agent_windows() or []
    if any(CONFIG_WINDOW_TITLE_SUBSTR in title for _, title, _ in windows):
        return True

    # Click the Configure menu first. In the post-login main window
    # this is a JMenu in a JMenuBar. Clicking it via doClick() in the
    # agent exposes its child JMenuItems as findable AbstractButtons
    # even though the popup isn't shown on screen.
    if not agent_click("Configure"):
        log.warning("Could not click Configure menu")
        return False
    time.sleep(0.3)
    # Now click Settings inside the (conceptually open) Configure menu.
    # This opens the Trader Workstation Configuration dialog.
    if not agent_click("Settings"):
        log.warning("Could not click Settings menu item")
        return False
    # Give Gateway a moment to render the dialog.
    for _ in range(20):  # up to 4s
        windows = agent_windows() or []
        if any(CONFIG_WINDOW_TITLE_SUBSTR in title for _, title, _ in windows):
            return True
        time.sleep(0.2)
    log.warning("Settings dialog did not appear after clicking Configure → Settings")
    return False


def _config_close(action="OK"):
    """Close the config dialog by clicking OK (apply + close) or
    Cancel (discard + close). Uses CLICK_IN_WIN to scope the click to
    the dialog's own OK button and not the main window."""
    return agent_click_in_window(CONFIG_WINDOW_TITLE_SUBSTR, action)


def _coerce_yes_no(val):
    """Translate IBC-style yes/no strings to Python bool. Returns
    None for unrecognized values (so the caller can skip the knob)."""
    if val is None:
        return None
    v = str(val).strip().lower()
    if v in ("yes", "true", "1", "on"):
        return True
    if v in ("no", "false", "0", "off"):
        return False
    return None


def handle_post_login_config():
    """Drive Gateway's Configure → Settings dialog to apply env-var
    overrides for the IBC-compatible API config knobs.

    Env vars (matching IBC's names where possible):

      Present in Gateway's API → Settings panel (always):
        TWS_MASTER_CLIENT_ID — integer, API → Settings → Master client ID
        READ_ONLY_API        — yes/no, API → Settings → Read-Only API

      Present in Gateway's Lock and Exit panel (one of two, depending
      on whether the account has the autorestart daily-token cycle
      enabled — Gateway shows *either* Auto Log Off Time *or* Auto
      Restart Time but not both):
        AUTO_LOGOFF_TIME  — HH:MM, "Set Auto Log Off Time (HH:MM)"
        AUTO_RESTART_TIME — HH:MM AM/PM, "Set Auto Restart Time (HH:MM)"
      The handler tries both labels and sets the one Gateway is
      currently displaying. If the user set the one that Gateway
      isn't showing in this session, a clear warning is logged.

      NOT present in Gateway's config dialog at all (TWS-only; we warn):
        ALLOW_BLIND_TRADING  — TWS Precautions tab; Gateway has no
                               equivalent in its simplified config
        SAVE_TWS_SETTINGS    — not a Gateway knob

    Skips any knob whose env var isn't set or is empty. Non-fatal on
    per-knob failure — logs a warning and moves on so users still get
    a working API port even if one setting couldn't be applied.
    """
    master_client_id = os.environ.get("TWS_MASTER_CLIENT_ID", "").strip()
    read_only_api = _coerce_yes_no(os.environ.get("READ_ONLY_API", ""))
    auto_logoff_time = os.environ.get("AUTO_LOGOFF_TIME", "").strip()
    auto_restart_time = os.environ.get("AUTO_RESTART_TIME", "").strip()
    # Truly TWS-only — not present in any Gateway config state we've observed
    allow_blind_trading = _coerce_yes_no(os.environ.get("ALLOW_BLIND_TRADING", ""))
    save_tws_settings = os.environ.get("SAVE_TWS_SETTINGS", "").strip()

    wanted_api_tab = master_client_id or read_only_api is not None
    wanted_lock_exit_tab = bool(auto_logoff_time) or bool(auto_restart_time)
    wanted_anything = wanted_api_tab or wanted_lock_exit_tab

    # Early warning for the truly TWS-only vars
    tws_only_set = []
    if allow_blind_trading is not None:
        tws_only_set.append("ALLOW_BLIND_TRADING")
    if save_tws_settings:
        tws_only_set.append("SAVE_TWS_SETTINGS")
    if tws_only_set:
        log.warning("Post-login config: these env vars are not present in "
                    f"Gateway's config dialog and are being ignored: "
                    f"{', '.join(tws_only_set)}. They're TWS-specific. "
                    "See controller/docs/MIGRATION.md for details.")

    if not wanted_anything:
        log.info("Post-login config: no supported env vars set, skipping")
        return True

    log.info("Applying post-login configuration from env vars")
    if not _config_open():
        log.warning("Post-login config: could not open Configure → Settings "
                    "dialog — settings not applied")
        return False

    changed = False

    if wanted_api_tab:
        log.info("  Navigating to API → Settings")
        if not agent_jtree_select_path(CONFIG_WINDOW_TITLE_SUBSTR, "API/Settings"):
            log.warning("  Could not select API/Settings tree node")
        else:
            time.sleep(0.5)  # let the right panel render
            if master_client_id:
                log.info(f"  Setting Master API client ID = {master_client_id}")
                if agent_settext_by_label(CONFIG_WINDOW_TITLE_SUBSTR,
                                          "Master API client ID",
                                          master_client_id):
                    changed = True
                else:
                    log.warning("  Failed to set Master API client ID")
            if read_only_api is not None:
                log.info(f"  Setting Read-Only API = {read_only_api}")
                if agent_jcheck(CONFIG_WINDOW_TITLE_SUBSTR,
                                "Read-Only API", read_only_api):
                    changed = True
                else:
                    log.warning("  Failed to toggle Read-Only API")

    if wanted_lock_exit_tab:
        log.info("  Navigating to Lock and Exit")
        if not agent_jtree_select_path(CONFIG_WINDOW_TITLE_SUBSTR, "Lock and Exit"):
            log.warning("  Could not select Lock and Exit tree node")
        else:
            # Gateway shows ONE of these two labels depending on whether
            # the account has the autorestart daily-token cycle enabled:
            #   "Set Auto Log Off Time (HH:MM)"   (logoff mode)
            #   "Set Auto Restart Time (HH:MM)"   (autorestart mode)
            # Whichever one is displayed is the one we can set on this
            # run. Try the user's requested setting against both labels
            # and warn if the other mode is active.
            time.sleep(1.0)  # panel render
            logoff_label = "Set Auto Log Off Time (HH:MM)"
            restart_label = "Set Auto Restart Time (HH:MM)"
            if auto_logoff_time:
                log.info(f"  Setting Auto Log Off Time = {auto_logoff_time}")
                if agent_settext_by_label(CONFIG_WINDOW_TITLE_SUBSTR,
                                          logoff_label, auto_logoff_time):
                    changed = True
                else:
                    # Gateway may be in autorestart-mode for this account —
                    # the field is labeled "Set Auto Restart Time" instead.
                    log.warning("  Failed to set Auto Log Off Time — "
                                "Gateway is showing 'Set Auto Restart Time' "
                                "in this session instead. Set AUTO_RESTART_TIME "
                                "to drive that field.")
            if auto_restart_time:
                log.info(f"  Setting Auto Restart Time = {auto_restart_time}")
                if agent_settext_by_label(CONFIG_WINDOW_TITLE_SUBSTR,
                                          restart_label, auto_restart_time):
                    changed = True
                else:
                    log.warning("  Failed to set Auto Restart Time — "
                                "Gateway is showing 'Set Auto Log Off Time' "
                                "in this session instead. Set AUTO_LOGOFF_TIME "
                                "to drive that field.")

    # Apply + close the dialog (OK commits and closes).
    if changed:
        if not _config_close("OK"):
            log.warning("Could not click OK to commit config changes")
            return False
        log.info("Post-login config applied and dialog closed")
    else:
        # Nothing was actually changed — use Cancel to avoid any
        # side-effect of OK that might otherwise fire even for an
        # unchanged form.
        if not _config_close("Cancel"):
            log.warning("Could not click Cancel on unchanged config dialog")
        log.info("Post-login config: no changes applied, dialog closed")
    return True


# ── CCP lockout detection + exponential backoff ──────────────────────
#
# IBKR's auth server silently drops fresh-password auth requests when
# it's in a "CCP lockout" state (AuthTimeoutMonitor-CCP: Timeout! in
# launcher.log, with no preceding NS_AUTH_START). The controller's
# TWOFA_TIMEOUT_ACTION=restart path was inadvertently feeding this
# lockout by immediately retrying auth with zero backoff — every retry
# extended the lockout window.
#
# The fix: after clicking Log In, poll launcher.log for the CCP Timeout
# signature. If detected, skip the 2FA wait (it can't succeed if auth
# was rejected) and enter an exponential backoff before the next retry.

_ccp_backoff_seconds = 0.0  # current backoff; 0 = no backoff active
_CCP_BACKOFF_INITIAL = 60.0
_CCP_BACKOFF_MAX = 600.0
_CCP_BACKOFF_MULTIPLIER = 2.0


def _detect_ccp_lockout(timeout=25):
    """Poll launcher.log for CCP auth timeout within `timeout` seconds
    of the Log In click. Returns True if CCP lockout is detected (auth
    was silently rejected), False if auth appears to have proceeded
    normally (NS_AUTH_START appeared, or no timeout within the window).

    Gateway's auth protocol:
      1. Click Log In → Gateway sends auth request
      2. Server replies with NS_AUTH_START within ~200ms (success)
         OR server is silent for 20s → CCP Timeout (lockout)

    We check by reading the LAST few lines of launcher.log repeatedly
    for up to `timeout` seconds. If we see 'AuthTimeoutMonitor-CCP:
    Timeout!' without a preceding 'NS_AUTH_START', auth was rejected.
    """
    launcher_log = os.path.join(JTS_CONFIG_DIR, "launcher.log")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with open(launcher_log, encoding="utf-8", errors="replace") as f:
                # Read last 4KB — enough to catch the recent auth sequence
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 4096))
                tail = f.read()
        except (FileNotFoundError, PermissionError):
            time.sleep(1)
            continue

        timeout_str = "AuthTimeoutMonitor-CCP: Timeout!"
        activate_str = "AuthTimeoutMonitor-CCP: activate"
        if timeout_str in tail:
            timeout_pos = tail.rfind(timeout_str)
            activate_pos = tail.rfind(activate_str)

            # Stale detection: if a NEW auth cycle started (activate)
            # AFTER the Timeout!, the Timeout! is from a previous
            # attempt and the current one is still in progress. Keep
            # polling — don't false-positive.
            if activate_pos > timeout_pos:
                time.sleep(1)
                continue

            # Check if NS_AUTH_START appeared AFTER the most recent
            # "Authenticating" line. If it did, auth progressed past
            # the CCP check and the timeout is a different failure.
            auth_pos = tail.rfind("Authenticating")
            ns_pos = tail.rfind("NS_AUTH_START")
            if ns_pos > auth_pos and auth_pos >= 0:
                # NS_AUTH_START came after Authenticating — auth
                # reached the server, this is a credential or
                # post-auth failure, not CCP lockout.
                return False
            log.warning("CCP LOCKOUT DETECTED — IBKR's auth server "
                        "silently dropped the auth request (no "
                        "NS_AUTH_START before Timeout)")
            return True
        time.sleep(1)
    return False


def _apply_ccp_backoff():
    """Sleep for the current CCP backoff duration, doubling it for
    the next call. Logs the delay so the user can see what's happening.
    Returns the duration slept."""
    global _ccp_backoff_seconds
    if _ccp_backoff_seconds == 0:
        _ccp_backoff_seconds = _CCP_BACKOFF_INITIAL
    else:
        _ccp_backoff_seconds = min(
            _ccp_backoff_seconds * _CCP_BACKOFF_MULTIPLIER,
            _CCP_BACKOFF_MAX,
        )
    log.warning(f"CCP backoff: waiting {int(_ccp_backoff_seconds)}s before "
                "next auth attempt (exponential backoff to let IBKR's "
                "rate limiter clear)")
    time.sleep(_ccp_backoff_seconds)
    return _ccp_backoff_seconds


def _reset_ccp_backoff():
    """Reset the backoff after a successful auth."""
    global _ccp_backoff_seconds
    if _ccp_backoff_seconds > 0:
        log.info("CCP backoff reset — auth succeeded")
        _ccp_backoff_seconds = 0.0


def _detect_login_stuck_connecting():
    """Return True if the login dialog is displaying the 'connecting to
    server (trying for another N seconds)' retry-loop label.

    This is a sibling signal to _detect_ccp_lockout's launcher.log
    signature. CCP detection catches the case where IBKR's auth server
    replies with a silent `AuthTimeoutMonitor-CCP: Timeout!`; this
    helper catches an earlier failure mode where Gateway can't even
    establish the session — the login window shows an internal retry
    counter and the auth protocol never starts. Both states have the
    same remediation: back off, don't re-click Log In.

    Implementation: ask the agent for all currently-visible JLabel text
    and look for the signature substrings. Cheap (one socket round-trip,
    no polling) because the caller is already in a post-timeout
    decision path and we don't want to add another long wait on top.
    """
    try:
        labels = agent_labels()
    except Exception:
        return False
    for _wtitle, text in labels:
        lower = text.lower()
        if "connecting to server" in lower or "trying for another" in lower:
            return True
    return False


# Hard cap on in-JVM relogin retries before escalating to JVM restart.
# Reached in two situations: (a) CCP lockout keeps rearming across
# attempts, or (b) ``attempt_inplace_relogin`` returns False (disposed
# login frame — see v0.4.4). Escalation then calls
# ``_escalate_to_jvm_restart`` (v0.4.5), which long-cools-down and soft-
# restarts this mode's JVM via ``do_restart_in_place``.
_INPLACE_RELOGIN_MAX_ATTEMPTS = 8

# v0.4.5: cap on JVM-restart attempts. Each attempt does a long CCP
# cool-down + ``do_restart_in_place``. 5 × CCP_COOLDOWN_SECONDS (default
# 1200s = 20min) = 100 min of wall clock at the cap, which is more than
# enough for IBKR's CCP rate limiter to clear if it's going to clear.
# Past this cap the controller exits (``sys.exit(1)``).
_JVM_RESTART_MAX_ATTEMPTS = int(os.environ.get("JVM_RESTART_MAX_ATTEMPTS", "5"))

# v0.4.5: long CCP cool-down before a JVM restart. Much longer than the
# exponential attempt-spacing backoff (``_apply_ccp_backoff``) — CCP
# needs silence to reset, not just spacing. Empirical observation:
# short waits (< 5 min) keep the limiter armed; 20+ min usually clears.
# Env var ``CCP_COOLDOWN_SECONDS`` (default 1200 = 20min).
_CCP_COOLDOWN_SECONDS_DEFAULT = 1200


def _apply_ccp_long_cooldown(reason):
    """Sleep a long duration (default 20min) to let IBKR's CCP rate
    limiter fully clear before a JVM restart. Required because
    killing and relaunching the JVM without the cool-down feeds the
    limiter a fresh handshake and keeps the lockout armed (exactly
    the failure mode the v0.4.0 changelog described).

    Env var: ``CCP_COOLDOWN_SECONDS`` (int seconds, default 1200).
    """
    cool_down_s = int(os.environ.get(
        "CCP_COOLDOWN_SECONDS", str(_CCP_COOLDOWN_SECONDS_DEFAULT)))
    log.warning(f"CCP long cool-down ({reason}): sleeping {cool_down_s}s "
                "before JVM restart. Short waits keep IBKR's rate "
                "limiter armed; this silence lets it reset.")
    time.sleep(cool_down_s)


def _recover_jvm_or_escalate(reason):
    """v0.4.7: Attempt a fast in-place JVM restart; on failure fall
    through to ``_escalate_to_jvm_restart`` (long CCP cool-down).

    Used by ``monitor_loop`` on paths where the old code called
    ``sys.exit(rc)``. In dual-mode those exits were silent no-ops —
    the container stayed up on the *other* mode's PID and this mode's
    JVM stayed dead forever (same trap v0.4.5/v0.4.6 fixed for the
    CCP-lockout paths). 2026-04-17 validation: live JVM exited
    cleanly (code 0) 18min after container start, ``monitor_loop``
    ``sys.exit``'d, live port 4001 was refused from outside the
    container while the container itself kept serving paper.

    Fast path first because an unexpected clean JVM exit (IBKR session
    kick, auto-logoff) usually isn't CCP-related — ``do_restart_in_place``
    will relaunch and log in immediately, no 20min wait. If it does
    fail (CCP lockout on the relaunched JVM, for instance), we fall
    through to the silent-cool-down escalation.

    Never returns False. Returns True on recovery; if everything fails,
    ``_escalate_to_jvm_restart`` calls ``sys.exit(1)`` after exhausting
    ``_JVM_RESTART_MAX_ATTEMPTS``.
    """
    log.warning(f"Recovery: {reason}. Trying fast in-place restart first.")
    try:
        if do_restart_in_place():
            log.info("Recovery: fast restart succeeded")
            return True
    except Exception as e:
        log.error(f"Recovery: do_restart_in_place raised "
                  f"{type(e).__name__}: {e}")
    log.warning("Recovery: fast restart failed; escalating to "
                "long-cool-down JVM restart")
    return _escalate_to_jvm_restart(reason)


def _escalate_to_jvm_restart(reason):
    """v0.4.5: Dual-mode-aware escape hatch for CCP lockout.

    Replaces the old ``sys.exit(1)`` terminus on paths where in-JVM
    relogin is exhausted (max attempts) or impossible (disposed login
    frame, v0.4.4). In dual-mode the container won't restart on
    sys.exit because ``run.sh`` waits on both live+paper controller
    PIDs and only the exiting mode's process dies — the container
    stays up on the other mode's PID and this mode's JVM stays dead
    forever. That leaves futures-admin connecting to a dangling socat
    that ECONNREFUSEs every request.

    v0.4.6 sequencing (kill first, THEN cool-down, THEN relaunch):
    kill this mode's Gateway JVM, sleep the long CCP cool-down
    (default 20min) with NO JVM running on these credentials, then
    launch a fresh JVM and re-drive login. If the JVM is kept alive
    during the cool-down its internal "Attempt N: connecting to
    server" retry loop keeps hitting IBKR's auth server and keeps
    the CCP rate limiter armed — observed 2026-04-16 with v0.4.5,
    where a 20min cool-down with the JVM alive failed to clear CCP
    on the relaunch. v0.4.6 makes the cool-down genuinely silent.

    Not the v0.4.0 bug: v0.4.0 was kill+relaunch+retry with 60-600s
    gaps, too short for CCP to clear and each fresh handshake rearmed
    the limiter. v0.4.6 keeps the 20min duration but positions it so
    the JVM is dead for the full wait.

    Returns True on successful JVM restart. Calls ``sys.exit(1)`` if
    restart keeps failing past ``_JVM_RESTART_MAX_ATTEMPTS`` (the fresh
    JVM keeps hitting CCP lockout even after each silent cool-down).
    """
    for attempt in range(1, _JVM_RESTART_MAX_ATTEMPTS + 1):
        log.warning(f"JVM restart attempt {attempt}/{_JVM_RESTART_MAX_ATTEMPTS}: "
                    "tearing down JVM before long cool-down (v0.4.6 silent cool-down)")
        _teardown_jvm_for_restart()
        _apply_ccp_long_cooldown(
            f"{reason}; JVM restart {attempt}/{_JVM_RESTART_MAX_ATTEMPTS}"
        )
        log.warning(f"JVM restart attempt {attempt}/{_JVM_RESTART_MAX_ATTEMPTS}: "
                    "cool-down complete, launching fresh JVM")
        if _relaunch_and_login_in_place():
            log.info("JVM restart succeeded; API port is open")
            _reset_ccp_backoff()
            return True
        log.error(f"JVM restart attempt {attempt} failed")
    log.error(f"JVM restart limit ({_JVM_RESTART_MAX_ATTEMPTS}) exhausted "
              "after silent cool-downs; exiting")
    sys.exit(1)


def _looks_like_disposed_shell(windows):
    """True when the visible window set matches Gateway's post-CCP
    disposed-login-frame state.

    Observed shape (v0.4.3 live validation, futures-admin agent report):
    a single non-modal top-level Window titled ``IBKR Gateway`` with no
    JPasswordField anywhere in its tree. Gateway's main application
    shell has rendered with its File/Configure/Help menu bar and the
    "API Server: disconnected" status labels — the login frame has
    been disposed, not occluded.

    In this state ``LoginManager.initiateLogin(capturedLoginFrame)`` is
    a silent no-op because the captured reference points at a disposed
    Window. In-JVM relogin cannot recover; the only path forward is a
    full JVM restart via container-level kill+relaunch.

    The caller should only trust this signature AFTER a short
    ``agent_wait_login_frame`` probe has already failed — that probe
    confirms there is no JPasswordField-bearing Window currently up,
    which distinguishes this case from the stuck-connecting retry
    (where the login frame IS present but a modal progress dialog is
    on top).
    """
    if len(windows) != 1:
        return False
    _wtype, title, modal = windows[0]
    return not modal and "IBKR Gateway" in title


def attempt_inplace_relogin(app):
    """In-JVM relogin primitive — match IBC's
    ``getLoginHandler().initiateLogin(getLoginFrame())`` semantics from
    outside the JVM via the existing AT-SPI path.

    Does NOT terminate GATEWAY_PROC, does NOT unlink AGENT_SOCKET, does
    NOT call launch_gateway. CURRENT_APP / GATEWAY_PROC / JVM_PID stay
    intact across the call.

    Why: killing + relaunching the Gateway JVM creates a fresh TCP/TLS
    session that IBKR's auth server treats as a new handshake, which
    keeps the CCP rate limiter armed and the lockout cycle open. IBC
    has worked in production for years by staying in one JVM and just
    re-invoking ``initiateLogin`` on the existing login frame. See the
    v0.4.0 CHANGELOG entry for the full rationale.

    Strategy (AT-SPI side):
      1. Scan open modal windows. Dismiss known login-failure error
         modals via OK/Close. Never click on a 'Connecting to server'
         progress dialog — clicking OK cancels the login.
      2. Wait up to 30s for the login form to redisplay by polling for
         the password text field (same signal handle_login uses).
      3. Re-drive handle_login(app) on the same app reference. Return
         its result.

    Returns True if handle_login succeeded, False if the login frame
    never reappeared or handle_login failed. Callers typically couple
    this with _apply_ccp_backoff() BEFORE the call for spacing.
    """
    log.warning("In-JVM relogin attempt (no JVM restart — matches "
                "IBC's LoginManager.initiateLogin semantics)")

    # 1. Dismiss error modals only. Progress dialogs are off-limits.
    try:
        windows = agent_windows()
    except Exception as e:
        log.warning(f"In-JVM relogin: agent_windows failed ({type(e).__name__}: {e}); "
                    "skipping modal dismissal and going straight to login-frame wait")
        windows = []
    for _wtype, title, modal in windows:
        if not modal:
            continue
        tl = title.lower()
        # Leave any in-flight progress dialog alone — OK cancels login.
        if "connecting to server" in tl:
            log.info(f"In-JVM relogin: leaving progress dialog intact: {title!r}")
            continue
        # Inspect body text to decide whether this is a dismissible error.
        try:
            body = agent_window(title)
        except Exception:
            body = ""
        body_lower = body.lower()
        error_markers = (
            "login failed",
            "login error",
            "could not be performed",
            "unable to connect",
            "authentication failed",
            "server cannot be reached",
        )
        if any(m in body_lower for m in error_markers):
            log.info(f"In-JVM relogin: dismissing error modal: {title!r}")
            for btn in ("OK", "Close"):
                if agent_click_in_window(title, btn):
                    break

    # 2. Wait for the login frame to redisplay, but short-circuit the
    # wait when we can tell in-JVM relogin is impossible.
    #
    # v0.4.3: use the Java agent's Swing-type-based lookup
    # (findLoginFrame → showing Window containing JPasswordField, plus
    # no-modal-blocking check) instead of pyatspi
    # ``wait_for(app, "password text")``. AT-SPI filters the login
    # frame's role while Gateway's "Attempt N: connecting to server"
    # modal is up, causing the old 30s wait to time out before
    # Gateway's internal retry self-cleared (typical ~60s). 120s
    # covers one full retry cycle with margin.
    #
    # v0.4.4: probe first with a short 2s timeout. If that fails AND
    # the visible windows match the disposed-shell signature (single
    # non-modal "IBKR Gateway" frame, no JPasswordField), bail
    # immediately and return False. `wait_for_api_port_with_retry`
    # treats that as an in-JVM-relogin failure and escalates to
    # container-level kill+relaunch, which is the only recovery path
    # once Gateway has disposed the login frame. Without this
    # short-circuit we burn 120s per attempt × 8 attempts = 16min
    # waiting for a frame that will never come back.
    log.info("In-JVM relogin: probing for login frame (2s) before full wait")
    if agent_wait_login_frame(timeout_ms=2_000):
        pass  # login frame already interactable, skip to step 3
    else:
        try:
            probe_windows = agent_windows()
        except Exception as e:
            log.warning(f"In-JVM relogin: agent_windows probe failed "
                        f"({type(e).__name__}: {e}); proceeding with full 120s wait")
            probe_windows = None
        if probe_windows is not None and _looks_like_disposed_shell(probe_windows):
            log.error("In-JVM relogin unavailable: login frame disposed "
                      "(post-CCP disconnected shell detected).")
            log.error(f"  windows: {probe_windows}")
            log.error("  Escalating to container-level recovery "
                      "(kill+relaunch JVM — the captured login frame "
                      "reference is a disposed Window, initiateLogin() "
                      "on it is a no-op).")
            return False
        log.info("In-JVM relogin: short probe failed but no disposed-shell "
                 "signature; waiting up to 120s for login frame")
        if not agent_wait_login_frame(timeout_ms=120_000):
            log.error("In-JVM relogin: login frame never became interactable within 120s")
            try:
                windows = agent_windows()
                log.error(f"  windows at timeout: {windows}")
            except Exception as e:
                log.error(f"  windows dump failed: {type(e).__name__}: {e}")
            return False

    # 3. Same app, same JVM — just re-drive handle_login.
    log.info("In-JVM relogin: login frame is up, re-driving handle_login "
             "on the existing JVM")
    return handle_login(app)


def wait_for_api_port_with_retry(app, port_timeout=180,
                                 max_attempts=_INPLACE_RELOGIN_MAX_ATTEMPTS):
    """wait_for_api_port wrapped in an in-JVM relogin retry loop.

    v0.4.1: the v0.4.0 CCP-lockout loop in main() only catches the
    launcher.log ``AuthTimeoutMonitor-CCP: Timeout!`` signature.
    Stuck-connecting mode — Gateway's login dialog stuck in its
    internal "connecting to server (trying for another N seconds)"
    retry — emits no Timeout! line, so it slipped past the v0.4.0
    outer poll at main():2383 and flowed into handle_2fa, which did
    a single RELOGIN_AFTER_TWOFA_TIMEOUT re-drive and then fell
    through to wait_for_api_port with no further retry. On timeout,
    the controller exited and the JVM was orphaned.

    This helper catches both failure modes at the final indicator
    (the API port). If the port doesn't open and either CCP-Timeout
    or stuck-connecting is currently visible, back off and relogin
    in-JVM on the same app reference. Capped at ``max_attempts``
    relogins; past that the controller exits for container-level
    recovery.

    Does NOT call launch_gateway, terminate GATEWAY_PROC, or unlink
    AGENT_SOCKET. The retry primitive is ``attempt_inplace_relogin``.

    Returns True when the API port opens. Calls ``sys.exit(1)`` on
    terminal failure (no lockout signature) or cap exhaustion —
    matching the behavior of the existing v0.4.0 CCP-lockout loop
    in main().
    """
    if wait_for_api_port(timeout=port_timeout):
        _reset_ccp_backoff()
        return True
    for attempt in range(1, max_attempts + 1):
        ccp = _detect_ccp_lockout(timeout=25)
        stuck = _detect_login_stuck_connecting()
        if not (ccp or stuck):
            # No lockout signature — this is a terminal failure
            # (wrong credentials, wrong server, network problem).
            # Preserve the v0.4.0 diagnostic dump for operators.
            log.error("API port never opened and no lockout signature "
                      "detected — treating as terminal failure (wrong "
                      "creds, wrong server, or network)")
            _diagnose_login_failure()
            log.error("Final state dump:")
            log.error(f"  windows: {agent_windows()}")
            labels = agent_labels()
            for wtitle, text in labels[:30]:
                log.error(f"  label [{wtitle}] {text!r}")
            sys.exit(1)
        log.warning(f"API port timeout with lockout signature "
                    f"(ccp_timeout={ccp}, stuck_connecting={stuck}); "
                    f"in-JVM relogin attempt {attempt}/{max_attempts}")
        _apply_ccp_backoff()
        if not attempt_inplace_relogin(app):
            log.error("In-JVM relogin failed during API-port retry "
                      "(login frame disposed or handle_login failed); "
                      "escalating to long-cool-down JVM restart (v0.4.5)")
            return _escalate_to_jvm_restart("in-JVM relogin returned False")
        if wait_for_api_port(timeout=port_timeout):
            _reset_ccp_backoff()
            return True
    log.error(f"CCP lockout persists after {max_attempts} in-JVM "
              "relogin attempts at API-port wait; escalating to "
              "long-cool-down JVM restart (v0.4.5 — dual-mode container's "
              "run.sh does NOT restart on sys.exit, so we self-heal)")
    return _escalate_to_jvm_restart(f"{max_attempts} in-JVM relogin attempts exhausted")


def _diagnose_login_failure():
    """Parse Gateway's launcher.log to turn a generic 'API port never
    opened' failure into a specific, actionable error message.

    Gateway's auth protocol on the wire:
      1. TCP connect + SSL handshake
      2. Gateway logs 'Authenticating'
      3. Server replies with 'Received NS_AUTH_START'
      4. Gateway sends credentials
      5. Server replies with 'PostAuthenticate' or an error dialog
      6. Gateway opens the API port

    Three observed failure modes look identical at step 6 but leave
    different fingerprints in launcher.log:

      a) IBKR silent cooldown / wrong server / network block:
         'Authenticating' appears, NS_AUTH_START never appears,
         'AuthTimeoutMonitor-CCP: Timeout!' fires 20 seconds later.
         Nothing from the server — it's ignoring us or we can't
         reach it.

      b) Wrong credentials:
         'Authenticating' appears, 'NS_AUTH_START' appears, but
         PostAuthenticate never happens (the server processed our
         hello but rejected our credentials). Usually also surfaces
         as a dialog we'd normally catch; if we got here, the dialog
         was either missed or dismissed.

      c) Never reached Authenticating:
         Usually means the SSL handshake failed, the server was
         unreachable, or the install4j launcher refused to start.
         Look at the tail of launcher.log for the specific error.

    We print a targeted error message for whichever case fits.
    """
    launcher_log = os.path.join(JTS_CONFIG_DIR, "launcher.log")
    if not os.path.isfile(launcher_log):
        log.error(f"Diagnosis: launcher.log not found at {launcher_log} — "
                  "Gateway may have failed before creating its log directory. "
                  "Check Xvfb, the install4j launcher path, and the ATK bridge "
                  "setup in $JAVA_HOME.")
        return

    try:
        with open(launcher_log, encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception as e:
        log.error(f"Diagnosis: couldn't read {launcher_log}: {type(e).__name__}: {e}")
        return

    has_authenticating = "Authenticating" in content
    has_ns_auth_start = "NS_AUTH_START" in content
    has_timeout = "AuthTimeoutMonitor-CCP: Timeout!" in content
    has_ssl_fail = "SSLHandshakeException" in content or "Remote host terminated" in content

    if has_ssl_fail:
        log.error("Diagnosis: SSL HANDSHAKE FAILED. This usually means "
                  "TWS_SERVER / TWS_SERVER_PAPER points at the wrong "
                  "regional server for this account, OR Gateway's "
                  "SupportsSSL cache is invalid.")
        log.error("  Check controller/docs/BOOTSTRAP.md for how to find "
                  "your account's correct regional server.")
        return

    if not has_authenticating:
        log.error("Diagnosis: Gateway NEVER reached the Authenticating state.")
        log.error("  Possible causes: SSL handshake failed before auth, "
                  "install4j launcher didn't start properly, or the JVM "
                  "crashed during startup.")
        log.error(f"  Last 10 non-debug lines of launcher.log:")
        lines = [line for line in content.splitlines()
                 if line and "DeadlockMonitor" not in line
                 and "AdManager" not in line]
        for line in lines[-10:]:
            log.error(f"    {line}")
        return

    if has_timeout and not has_ns_auth_start:
        log.error("Diagnosis: IBKR SILENTLY DROPPED THE AUTH REQUEST.")
        log.error("  Gateway reached 'Authenticating' and waited 20s for "
                  "IBKR to send NS_AUTH_START, but nothing came back. "
                  "This is typically one of:")
        log.error("    (1) IBKR rate-limit / cooldown after repeated "
                  "failed logins — wait 5-60 minutes and retry")
        log.error("    (2) Wrong regional server — check your TWS_SERVER "
                  "/ TWS_SERVER_PAPER value matches your account")
        log.error("    (3) Another session holding the account — "
                  "shut down any other container / mobile app / TWS "
                  "logged in as this user")
        log.error("  See controller/docs/BOOTSTRAP.md for more.")
        return

    if has_ns_auth_start and not has_timeout:
        log.error("Diagnosis: auth request sent, server responded with "
                  "NS_AUTH_START, but we never reached PostAuthenticate.")
        log.error("  Most likely causes:")
        log.error("    (1) Wrong username or password — verify your "
                  "TWS_USERID / TWS_PASSWORD (or _PAPER variants)")
        log.error("    (2) A post-auth dialog appeared that we didn't "
                  "recognize — check the window dump above")
        return

    if has_ns_auth_start and has_timeout:
        log.error("Diagnosis: auth request sent, server responded with "
                  "NS_AUTH_START, then auth timed out. Credentials were "
                  "probably rejected — verify TWS_USERID / TWS_PASSWORD.")
        return

    log.error("Diagnosis: unexpected state in launcher.log. Check "
              "the full launcher.log for details:")
    log.error(f"  docker exec <container> cat {launcher_log}")


def wait_for_api_port(timeout=180):
    """Probe Gateway's API port until it's actually accepting connections.

    This is the DEFINITIVE readiness signal — the API port only opens
    after Gateway has fully authenticated AND finished post-login setup.
    Swing-level "main window" markers proved unreliable because the
    main window shell is drawn even before login completes.

    Also opportunistically dismisses post-login disclaimer dialogs that
    appear during the wait — Gateway sometimes shows them right before
    opening the API port, and we want to clear them as they pop up.
    """
    api_port = api_port_for_mode()
    log.info(f"Probing API port {api_port} for readiness (up to {timeout}s)")
    start = time.monotonic()
    last_status = 0.0
    while time.monotonic() - start < timeout:
        if is_api_port_open(api_port):
            elapsed = int(time.monotonic() - start)
            log.info(f"API port {api_port} accepting connections after {elapsed}s")
            return True

        # Opportunistically dismiss disclaimer dialogs each iteration
        _, buttons = agent_list()
        for btn in SAFE_DISMISS_BUTTONS & buttons:
            log.info(f"  dismissing disclaimer {btn!r}")
            agent_click(btn)
            time.sleep(0.5)

        # Log progress every 10s with a snapshot of current windows
        now = time.monotonic()
        if now - last_status > 10:
            elapsed = int(now - start)
            windows = agent_windows()
            log.info(f"  API port still closed at t+{elapsed}s; windows={windows}")
            last_status = now
        time.sleep(0.5)
    return False


def signal_ready():
    """Touch the readiness file so run.sh can start socat."""
    with open(READY_FILE, "w") as f:
        f.write(str(int(time.time())))
    log.info(f"Readiness signal: {READY_FILE}")


# ── Process lifecycle ───────────────────────────────────────────────────

gateway_proc = None  # global so signal handler can reach it


def shutdown(signum, frame):
    log.info(f"Received signal {signum}, shutting down Gateway")
    if gateway_proc is not None and gateway_proc.poll() is None:
        gateway_proc.terminate()
        try:
            gateway_proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            log.warning("Gateway didn't terminate cleanly, killing")
            gateway_proc.kill()
    try:
        os.unlink(READY_FILE)
    except FileNotFoundError:
        pass
    sys.exit(0)


def _warn_unsupported_env_vars():
    """Warn loudly at startup about IBC env vars we don't implement.

    Users migrating from IBC may have these set in their .env without
    realizing the controller ignores them. A silent ignore means the
    user thinks they're getting a behavior they're not. This is the
    one chance to tell them at startup so they don't debug a
    phantom bug later.
    """
    # IBC env vars we still don't honor. Vars we DID wire up are:
    #   TWOFA_EXIT_INTERVAL / TWOFA_TIMEOUT_ACTION /
    #   RELOGIN_AFTER_TWOFA_TIMEOUT  → handle_2fa() respects all three
    #   BYPASS_WARNING  → _resolve_safe_dismiss_buttons() extends the
    #                     disclaimer allowlist at module load
    #   TWS_COLD_RESTART → apply_warm_state() skips when set
    # TWOFA_DEVICE is no longer in this list: the controller handles
    # IB Key push 2FA by polling for the dialog to disappear (the user
    # approves on their phone, the dialog goes away, we proceed). Same
    # approach as ibctl. Not as hands-free as TOTP but not impossible.
    unsupported = {
        "CUSTOM_CONFIG":
            "not honored; the controller reads env vars directly and "
            "does not render an IBC config.ini",
    }
    hit = []
    for name, reason in unsupported.items():
        if os.environ.get(name):
            hit.append((name, reason))
    if hit:
        log.warning("These IBC-compat env vars are SET but NOT honored by "
                    "the controller. If you're migrating from IBC and "
                    "depending on any of these, you'll need to stay on IBC "
                    "for those specific behaviors:")
        for name, reason in hit:
            log.warning(f"  - {name}: {reason}")


def main():
    global gateway_proc

    if not USERNAME or not PASSWORD:
        log.error("TWS_USERID and TWS_PASSWORD must be set")
        sys.exit(2)

    _warn_unsupported_env_vars()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    _set_state(State.LAUNCHING)
    # 1. Launch Gateway JVM
    global GATEWAY_PROC, CURRENT_APP, JVM_PID
    GATEWAY_PROC = launch_gateway()
    gateway_proc = GATEWAY_PROC  # local alias for readability below

    _set_state(State.AGENT_WAIT)
    # 2a. Wait for the input agent's Unix socket to come up. The agent is
    # loaded via -javaagent:, so it starts when the JVM starts — earlier
    # than the AT-SPI tree gets populated. If the jar is missing the agent
    # will never come up; the controller will then fail at first SETTEXT.
    if os.path.exists(AGENT_JAR):
        log.info(f"Waiting for input agent at {AGENT_SOCKET}")
        if not agent_wait_ready(timeout=60):
            log.error("Input agent never came up — text input will fail")
        else:
            log.info("Input agent is up")
            # Record the Gateway JVM PID so find_app() can distinguish our
            # Gateway from any other Gateway JVM running in the same
            # container. Critical for TRADING_MODE=both where both live
            # and paper JVMs appear as 'IBKR Gateway' in AT-SPI.
            JVM_PID = agent_get_pid()
            if JVM_PID is not None:
                log.info(f"Gateway JVM PID (from agent): {JVM_PID}")
            else:
                log.warning("Agent did not report PID — find_app will not "
                            "be able to disambiguate in dual-mode containers")

    _set_state(State.APP_DISCOVERY)
    # 2b. Wait for the app to register with AT-SPI
    log.info("Waiting for IBKR Gateway in the AT-SPI desktop tree")
    app = find_app(APP_NAME_CANDIDATES, timeout=120, match_pid=JVM_PID)
    if app is None:
        log.error("IBKR Gateway never appeared in AT-SPI tree within 120s")
        sys.exit(1)
    CURRENT_APP = app
    log.info(f"App registered: {app.get_name()!r} (pid={JVM_PID})")

    _set_state(State.LOGIN)
    # 3. Drive the login dialog
    if not handle_login(app):
        log.error("Login dialog handling failed")
        sys.exit(1)

    if TEST_MODE:
        log.info("CONTROLLER_TEST_MODE=1 — waiting 5s for Gateway response then dumping tree")
        time.sleep(5)
        log.info("=== POST-CLICK TREE DUMP ===")
        _dump_tree(app, max_depth=15)
        sys.exit(0)

    # 3a. Check for CCP lockout BEFORE entering the 2FA wait. Gateway's
    # auth timeout fires ~20s after the Log In click. If we detect it,
    # there's no point waiting 120s for a 2FA dialog — auth was rejected.
    #
    # Recovery is via IN-JVM relogin, never by killing the JVM. Killing
    # and relaunching creates a fresh auth handshake that IBKR treats
    # as a new session and keeps the CCP lockout armed. IBC's
    # ReloginAfterSecondFactorAuthenticationTimeout pattern stays
    # entirely within one JVM (same rationale). See v0.4.0 CHANGELOG.
    ccp_retry = 0
    while _detect_ccp_lockout(timeout=25):
        _apply_ccp_backoff()
        ccp_retry += 1
        if ccp_retry > _INPLACE_RELOGIN_MAX_ATTEMPTS:
            log.error(f"CCP lockout persists after {_INPLACE_RELOGIN_MAX_ATTEMPTS} "
                      "in-JVM relogin attempts; escalating to long-cool-down "
                      "JVM restart (v0.4.5)")
            _escalate_to_jvm_restart(
                f"{_INPLACE_RELOGIN_MAX_ATTEMPTS} in-JVM relogins exhausted in main CCP pre-loop")
            # Escalation returned True — fresh JVM is up, login completed,
            # API port open (do_restart_in_place handled it all). Rebind
            # app to the new JVM's AT-SPI reference and exit the CCP loop
            # — the post-login steps below are now redundant but
            # idempotent, so letting them run is harmless.
            app = CURRENT_APP
            break
        log.info(f"Retrying auth in-JVM after CCP backoff "
                 f"(attempt {ccp_retry}/{_INPLACE_RELOGIN_MAX_ATTEMPTS})")
        if not attempt_inplace_relogin(app):
            log.error("In-JVM relogin failed (login frame disposed or "
                      "handle_login failed); escalating to long-cool-down "
                      "JVM restart (v0.4.5)")
            _escalate_to_jvm_restart(
                "in-JVM relogin returned False in main CCP pre-loop")
            app = CURRENT_APP
            break
        # Loop back: _detect_ccp_lockout polls the NEW Log In click's
        # auth outcome. If no fresh Timeout! appears within 25s, the
        # lockout has cleared and we fall through to post-login.

    _set_state(State.POST_LOGIN)
    # 3b. Dismiss any modal dialog Gateway shows immediately after the
    # Log In click — most commonly the existing-session-detected dialog
    # if IBKR's session-tracker hasn't released the previous session.
    if not handle_post_login_dialogs(app):
        log.error("Post-login dialog handling failed")
        sys.exit(1)

    # Gate: _detect_ccp_lockout==False is ambiguous — it can mean "auth
    # progressed past CCP" OR "stuck in the connecting-to-server retry
    # loop, which doesn't emit the Timeout! signature". Only reset when
    # we're confident we've actually progressed. See v0.3.2 CHANGELOG.
    if not _detect_login_stuck_connecting():
        _reset_ccp_backoff()

    _set_state(State.TWO_FA)
    # 4. 2FA if applicable
    if not handle_2fa(app):
        log.error("2FA handling failed")
        sys.exit(1)

    _set_state(State.DISCLAIMERS)
    # 4b. Dismiss known post-login disclaimer dialogs (paper-trading
    # risks acknowledgement, terms-of-service updates, etc.) that
    # otherwise sit on screen forever. IBC normally auto-clicks these
    # via its BypassWarning settings.
    dismiss_post_login_disclaimers(timeout=30)

    _set_state(State.API_WAIT)
    # 5. Wait for the API port to actually be listening. This is the
    # definitive readiness signal — Swing-level "main window detected"
    # markers turned out to be unreliable (the buttons we look for exist
    # in Gateway's empty pre-login shell too). API port == authenticated.
    #
    # v0.4.1: wrapped in an in-JVM relogin retry loop so late-manifesting
    # stuck-connecting failures (which don't emit the launcher.log
    # ``Timeout!`` signature caught by the v0.4.0 outer CCP loop at
    # line 2383) are still recovered via attempt_inplace_relogin. A
    # terminal failure with no lockout signature still exits with the
    # same diagnostic dump as before.
    wait_for_api_port_with_retry(CURRENT_APP)

    _set_state(State.CONFIG)
    # 5b. Apply post-login API configuration (Master Client ID, Read-Only
    # API, etc.) if the user set any of the corresponding env vars. Runs
    # AFTER the API port is open so we know the Configure menu is real
    # and the settings dialog has live data. Non-fatal on failure — we
    # just warn and continue to readiness so the user gets a connectable
    # API even if the config knobs didn't take.
    handle_post_login_config()

    _set_state(State.READY)
    # 6. Signal ready
    signal_ready()

    _set_state(State.COMMAND_SERVER)
    # 6b. Optional IBC-compat command server. Listens on TCP port
    # (default 7462, IBC's default) for text commands like STOP /
    # RESTART / RECONNECTDATA / RECONNECTACCOUNT. Started in a
    # background thread so it runs alongside the monitor loop. Only
    # enabled if CONTROLLER_COMMAND_SERVER_PORT is set (defaults to off
    # so we don't grab a port users didn't ask for).
    start_command_server(app)

    _set_state(State.MONITORING)
    # 7. Long-running monitor: watch JVM + API port + re-auth events
    log.info("Login complete. Entering monitor loop.")
    monitor_loop(app)


# ── IBC-compat TCP command server (Phase 2.4) ─────────────────────────
#
# IBC ships a small TCP listener that accepts text commands from
# external orchestration tools (cron jobs, watchdogs). We implement
# the same protocol so existing users can swap in the controller
# without rewriting their orchestration.
#
# Protocol: line-based, plain text, one command per connection.
#   STOP              → clean shutdown of Gateway + controller
#   RESTART           → kill Gateway JVM and re-launch (NOT YET IMPLEMENTED)
#   RECONNECTDATA     → click File → Reconnect Data menu item
#   RECONNECTACCOUNT  → re-drive the full login flow via attempt_reauth
#   ENABLEAPI         → no-op (ApiOnly=true is already set in jts.ini)
#
# The listener runs in a daemon thread so it dies with the process.
# Single-connection at a time (no concurrency). Bind address is
# configurable via CONTROLLER_COMMAND_SERVER_HOST (default 127.0.0.1
# — localhost only, matching IBC's default).

_command_server_thread = None
_command_server_app = None
_command_server_socket = None


def start_command_server(app):
    """Start the IBC-compat command server in a daemon thread if
    CONTROLLER_COMMAND_SERVER_PORT is set. No-op otherwise."""
    global _command_server_thread, _command_server_app
    port_str = os.environ.get("CONTROLLER_COMMAND_SERVER_PORT", "").strip()
    if not port_str:
        log.info("Command server: CONTROLLER_COMMAND_SERVER_PORT not set, skipping")
        return
    try:
        port = int(port_str)
    except ValueError:
        log.warning(f"Command server: invalid port {port_str!r}, skipping")
        return
    # Default to 0.0.0.0 because the controller is Docker-first — the
    # container's localhost is not reachable from the host's `docker
    # run -p 7462:7462`, you need to bind to the container's external
    # interface. Users who want to restrict exposure should rely on
    # Docker's port mapping (e.g. -p 127.0.0.1:7462:7462) rather than
    # the in-container bind address.
    host = os.environ.get("CONTROLLER_COMMAND_SERVER_HOST", "0.0.0.0").strip()

    _command_server_app = app
    _command_server_thread = threading.Thread(
        target=_command_server_main,
        args=(host, port),
        daemon=True,
        name="command-server",
    )
    _command_server_thread.start()
    log.info(f"Command server: listening on {host}:{port}")


def _command_server_main(host, port):
    """Accept loop for the IBC-compat command server. Runs forever
    until the socket is closed (on process exit via daemon-thread
    teardown).

    If CONTROLLER_COMMAND_SERVER_AUTH_TOKEN is set, clients must send
    an AUTH line before any command, e.g.:
        AUTH <token>
        STOP
    The token is checked with hmac.compare_digest to resist timing
    side channels. Connections that don't AUTH, or send a wrong token,
    get an ERR and are closed without reaching _handle_command.
    Connections with no token configured run as before (IBC behavior).
    """
    global _command_server_socket
    auth_token = os.environ.get("CONTROLLER_COMMAND_SERVER_AUTH_TOKEN", "")
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, port))
        s.listen(5)
        _command_server_socket = s
        if auth_token:
            log.info("Command server: auth token required")
        else:
            log.warning("Command server: NO AUTH TOKEN SET. Anyone "
                        "who can reach the bind address can send STOP / "
                        "RESTART / RECONNECTACCOUNT. For localhost-only "
                        "access use docker -p 127.0.0.1:PORT:PORT; for "
                        "remote access set CONTROLLER_COMMAND_SERVER_AUTH_TOKEN.")
    except Exception as e:
        log.error(f"Command server: bind/listen failed: {type(e).__name__}: {e}")
        return

    # Simple rate limit: cap how fast we can accept new connections.
    # Protects against someone spamming the server even with a valid
    # token (defense in depth — a real brute-force attacker would
    # grind through AUTH attempts faster than this allows).
    min_accept_interval = 0.25  # 4 accepts/sec max
    last_accept = 0.0

    while True:
        try:
            conn, addr = s.accept()
        except Exception as e:
            log.warning(f"Command server: accept error: {type(e).__name__}: {e}")
            return

        now = time.monotonic()
        delay = min_accept_interval - (now - last_accept)
        if delay > 0:
            time.sleep(delay)
        last_accept = time.monotonic()

        try:
            conn.settimeout(5.0)

            # Read up to two lines: optional AUTH <token>, then the
            # command. Max payload kept small to avoid buffer-bloat
            # attacks.
            data = b""
            while data.count(b"\n") < 2 and len(data) < 1024:
                chunk = conn.recv(256)
                if not chunk:
                    break
                data += chunk
                if auth_token == "" and b"\n" in data:
                    break

            text = data.decode("utf-8", errors="replace")
            lines = text.splitlines()
            cmd = ""

            if auth_token:
                # Expect: first line AUTH <token>, second line <command>
                if not lines or not lines[0].upper().startswith("AUTH "):
                    conn.sendall(b"ERR auth_required\n")
                    log.warning(f"Command server: {addr[0]}:{addr[1]} rejected — no AUTH line")
                    continue
                provided = lines[0][5:].strip()
                if not hmac.compare_digest(provided, auth_token):
                    conn.sendall(b"ERR auth_failed\n")
                    log.warning(f"Command server: {addr[0]}:{addr[1]} rejected — auth failed")
                    continue
                if len(lines) < 2:
                    conn.sendall(b"ERR empty_command\n")
                    continue
                cmd = lines[1].strip().upper()
            else:
                cmd = (lines[0].strip().upper() if lines else "")

            log.info(f"Command server: {addr[0]}:{addr[1]} sent {cmd!r}")
            response = _handle_command(cmd)
            try:
                conn.sendall((response + "\n").encode("utf-8"))
            except Exception:
                pass
        except Exception as e:
            log.warning(f"Command server: handler error: {type(e).__name__}: {e}")
        finally:
            try:
                conn.close()
            except Exception:
                pass


def _teardown_jvm_for_restart():
    """v0.4.6: Terminate the current Gateway JVM and clear per-instance
    state files (agent socket, ready file) so a subsequent launch starts
    from a clean slate.

    Split out of ``do_restart_in_place`` so ``_escalate_to_jvm_restart``
    can kill the JVM *before* the long CCP cool-down. When the JVM is
    kept alive during the cool-down its internal "Attempt N: connecting
    to server" retry loop keeps banging on IBKR's auth server, which
    keeps the CCP rate limiter armed — observed 2026-04-16 with v0.4.5
    (20min cool-down with JVM alive → CCP still locked on restart).
    Killing the JVM first makes the cool-down genuinely silent from
    IBKR's perspective, which is the whole point.
    """
    global GATEWAY_PROC
    if GATEWAY_PROC is not None and GATEWAY_PROC.poll() is None:
        log.info(f"RESTART: terminating Gateway PID {GATEWAY_PROC.pid}")
        try:
            GATEWAY_PROC.terminate()
            try:
                GATEWAY_PROC.wait(timeout=20)
            except subprocess.TimeoutExpired:
                log.warning("RESTART: Gateway didn't exit cleanly, killing")
                GATEWAY_PROC.kill()
                GATEWAY_PROC.wait(timeout=5)
        except Exception as e:
            log.warning(f"RESTART: Gateway termination error: {e}")
    GATEWAY_PROC = None

    for p in (AGENT_SOCKET, READY_FILE):
        try:
            os.unlink(p)
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning(f"RESTART: couldn't remove {p}: {e}")


def _relaunch_and_login_in_place():
    """v0.4.6: Launch a fresh Gateway JVM and re-run the full login
    pipeline. Assumes ``_teardown_jvm_for_restart`` has already run —
    i.e., GATEWAY_PROC is None, agent socket is cleared.

    Returns True on full success (re-login completed, API port open,
    readiness re-signalled), False on any failure. Updates the module
    globals GATEWAY_PROC / CURRENT_APP / JVM_PID so the monitor loop
    picks up the new references on its next iteration.
    """
    global GATEWAY_PROC, CURRENT_APP, JVM_PID, _command_server_app

    GATEWAY_PROC = launch_gateway()

    # 4. Re-discover the agent (new JVM = new socket).
    if not agent_wait_ready(timeout=60):
        log.error("RESTART: agent did not come up after re-launch")
        return False
    JVM_PID = agent_get_pid()
    log.info(f"RESTART: new Gateway JVM PID (from agent): {JVM_PID}")

    # 5. Re-discover the app in the AT-SPI tree.
    new_app = find_app(APP_NAME_CANDIDATES, timeout=120, match_pid=JVM_PID)
    if new_app is None:
        log.error("RESTART: Gateway app never reappeared in AT-SPI tree")
        return False
    CURRENT_APP = new_app
    _command_server_app = new_app  # the command server now drives the new app

    # 6. Re-drive login through the same pipeline main() uses.
    if not handle_login(new_app):
        log.error("RESTART: login dialog handling failed")
        return False

    # Check for CCP lockout on the freshly-relaunched JVM. If detected,
    # recovery must be IN-JVM — do NOT recurse do_restart_in_place here.
    # Killing this JVM and spawning another one is exactly what keeps
    # IBKR's CCP limiter armed (each new JVM = new auth handshake).
    # Stay in this JVM and re-click Log In instead. See v0.4.0 CHANGELOG.
    ccp_retry = 0
    while _detect_ccp_lockout(timeout=25):
        _apply_ccp_backoff()
        ccp_retry += 1
        if ccp_retry > _INPLACE_RELOGIN_MAX_ATTEMPTS:
            log.error(f"RESTART: CCP lockout persists after "
                      f"{_INPLACE_RELOGIN_MAX_ATTEMPTS} in-JVM relogin "
                      "attempts on the relaunched JVM; giving up")
            return False
        log.info(f"RESTART: in-JVM relogin after CCP backoff "
                 f"(attempt {ccp_retry}/{_INPLACE_RELOGIN_MAX_ATTEMPTS})")
        if not attempt_inplace_relogin(new_app):
            log.error("RESTART: in-JVM relogin failed on the relaunched JVM")
            return False

    # Gate: same reasoning as the main() path — don't reset on the
    # stuck-connecting case, only on genuine CCP-gate progress.
    if not _detect_login_stuck_connecting():
        _reset_ccp_backoff()

    if not handle_post_login_dialogs(new_app):
        log.error("RESTART: post-login dialog handling failed")
        return False
    if not handle_2fa(new_app):
        log.error("RESTART: 2FA handling failed")
        return False
    dismiss_post_login_disclaimers(timeout=30)
    if not wait_for_api_port(timeout=180):
        log.error("RESTART: API port never opened after re-launch")
        return False

    # 7. Re-apply post-login configuration (Master Client ID, etc.).
    handle_post_login_config()

    # 8. Re-signal readiness so run.sh / external watchers see the
    #    second round has stabilized.
    signal_ready()

    log.info("RESTART: Gateway re-launched successfully")
    return True


def do_restart_in_place():
    """Tear down the current Gateway JVM, clear per-instance state
    files, and re-run the full login sequence. Updates the module
    globals GATEWAY_PROC / CURRENT_APP / JVM_PID so the monitor loop
    picks up the new references on its next iteration.

    Used by the IBC-compat RESTART command and the monitor-loop wedge
    escalation. Returns True on full success (re-login completed, API
    port open, readiness re-signalled), False on any failure during
    the re-launch sequence.

    v0.4.6: Thin wrapper over ``_teardown_jvm_for_restart`` +
    ``_relaunch_and_login_in_place`` so ``_escalate_to_jvm_restart`` can
    interleave a long CCP cool-down *between* the teardown and the
    relaunch (JVM dead during the cool-down = genuinely silent from
    IBKR's perspective = CCP limiter actually clears).
    """
    _teardown_jvm_for_restart()
    return _relaunch_and_login_in_place()


def _handle_command(cmd):
    """Dispatch a single command string to the appropriate controller
    action. Returns the response string to send back to the client."""
    if cmd == "STOP":
        log.info("Command server: STOP received — initiating shutdown")
        # Send SIGTERM to ourselves so the existing shutdown path runs
        try:
            os.kill(os.getpid(), signal.SIGTERM)
        except Exception as e:
            return f"ERR STOP: {type(e).__name__}: {e}"
        return "OK STOP"

    if cmd == "RESTART":
        log.info("Command server: RESTART — tearing down and re-launching Gateway")
        try:
            ok = do_restart_in_place()
        except Exception as e:
            return f"ERR RESTART: {type(e).__name__}: {e}"
        return "OK RESTART" if ok else "ERR RESTART: re-launch failed"

    if cmd == "RECONNECTDATA":
        # Gateway 10.45's File menu has: Gateway Logs, API Logs, Gateway
        # Layout/Settings, Close. No "Reconnect Data" item. RECONNECTDATA
        # is a TWS-only command (on TWS the File menu has Reconnect Data
        # and Reconnect Account). On Gateway, the equivalent workflow is
        # to RECONNECTACCOUNT (re-login) instead.
        product = os.environ.get("GATEWAY_OR_TWS", "gateway").strip().lower()
        if product == "tws":
            log.info("Command server: RECONNECTDATA — clicking File → Reconnect Data")
            if not agent_click("File"):
                return "ERR RECONNECTDATA: could not click File menu"
            time.sleep(0.3)
            if not agent_click("Reconnect Data"):
                return "ERR RECONNECTDATA: could not click Reconnect Data item"
            return "OK RECONNECTDATA"
        return ("ERR RECONNECTDATA: not supported on Gateway — "
                "Gateway's File menu has no Reconnect Data item. "
                "Use RECONNECTACCOUNT to re-drive the login flow instead.")

    if cmd == "RECONNECTACCOUNT":
        # Re-drive the full login flow. This is what our monitor loop
        # already does when it detects the login dialog after a
        # session loss — we just invoke it directly.
        log.info("Command server: RECONNECTACCOUNT — re-driving login")
        if _command_server_app is None:
            return "ERR RECONNECTACCOUNT: no app reference"
        ok = attempt_reauth(_command_server_app)
        return "OK RECONNECTACCOUNT" if ok else "ERR RECONNECTACCOUNT: reauth failed"

    if cmd == "ENABLEAPI":
        # We run Gateway with ApiOnly=true in jts.ini, so the API is
        # always enabled once login completes. Nothing to do.
        return "OK ENABLEAPI (already enabled)"

    return f"ERR unknown_command: {cmd}"


def monitor_loop(app):
    """Long-running post-ready monitor.

    Responsibilities:
      - Detect JVM exit and propagate the exit code
      - Heartbeat the API port; if it closes, check for a re-auth dialog
      - If a re-auth dialog is up (daily restart, session expiry), re-drive
        the full login flow

    The heartbeat runs every HEARTBEAT_INTERVAL seconds. Three consecutive
    port-closed heartbeats trigger a re-auth check. This tolerates brief
    network blips (one or two missed heartbeats) without flapping into
    the re-auth path.

    The `app` and `gateway_proc` references are re-read from the module
    globals GATEWAY_PROC / CURRENT_APP each iteration so that a RESTART
    command (Phase 2.4) which replaces the underlying Gateway JVM
    transparently hands the new references to this loop without having
    to be restarted itself.
    """
    HEARTBEAT_INTERVAL = 30          # seconds between port probes
    PORT_FAILURE_THRESHOLD = 3       # consecutive failures before we act
    # How many consecutive port-closed heartbeats we'll tolerate when
    # no login dialog is visible before we escalate. This is the
    # "Gateway is wedged in some non-login state with the port closed"
    # case — the controller can't re-drive login because there's no
    # login dialog to drive, so we eventually force-restart the JVM
    # via do_restart_in_place(). This is the escape hatch for the
    # previously-observed dead-end where the loop just kept logging
    # "probably a transient issue, will re-check next heartbeat"
    # forever.
    WEDGED_ESCALATION_THRESHOLD = 6  # = 6 heartbeats × 30s = 3 minutes of wedge

    api_port = api_port_for_mode()
    consecutive_failures = 0
    wedged_failures = 0
    last_heartbeat = time.monotonic()
    log.info(f"Monitor: JVM pid={GATEWAY_PROC.pid}, heartbeat API port {api_port} every {HEARTBEAT_INTERVAL}s")

    while True:
        # Always consult the live globals — a RESTART command may have
        # replaced them since the last iteration.
        gw = GATEWAY_PROC
        live_app = CURRENT_APP if CURRENT_APP is not None else app

        # JVM process check — fast, every iteration
        if gw is None or gw.poll() is not None:
            rc = gw.returncode if gw is not None else 1
            log.error(f"Gateway JVM exited with code {rc}")
            try:
                os.unlink(READY_FILE)
            except FileNotFoundError:
                pass
            # v0.4.7: dual-mode-safe recovery. sys.exit(rc) here would
            # leave this mode's JVM dead while the container stays up
            # on the other mode's PID — same trap v0.4.5/v0.4.6 fixed
            # for the CCP paths. _recover_jvm_or_escalate tries a fast
            # restart first (cheap if IBKR just kicked the session) and
            # falls through to long cool-down if CCP is actually locked.
            _recover_jvm_or_escalate(f"JVM exited with code {rc}")
            consecutive_failures = 0
            wedged_failures = 0
            last_heartbeat = time.monotonic()
            continue

        now = time.monotonic()
        if now - last_heartbeat >= HEARTBEAT_INTERVAL:
            last_heartbeat = now
            if is_api_port_open(api_port):
                if consecutive_failures > 0:
                    log.info(f"API port {api_port} recovered after {consecutive_failures} failed heartbeat(s)")
                consecutive_failures = 0
                wedged_failures = 0
            else:
                consecutive_failures += 1
                log.warning(f"API port {api_port} closed "
                            f"(heartbeat failure {consecutive_failures}/{PORT_FAILURE_THRESHOLD})")
                if consecutive_failures >= PORT_FAILURE_THRESHOLD:
                    log.warning("Sustained API port closure — checking for re-auth")
                    result = attempt_reauth(live_app)
                    if result is True:
                        # attempt_reauth returns True both when it re-auth'd
                        # AND when there was nothing to do (no login dialog).
                        # Distinguish: if port is actually open now, we're
                        # recovered; otherwise we're still wedged and should
                        # count this heartbeat as a wedged failure.
                        if is_api_port_open(api_port):
                            consecutive_failures = 0
                            wedged_failures = 0
                        else:
                            wedged_failures += 1
                            log.warning(f"Wedged failure {wedged_failures}/{WEDGED_ESCALATION_THRESHOLD}: "
                                        "API port still closed, no login dialog")
                            if wedged_failures >= WEDGED_ESCALATION_THRESHOLD:
                                log.error("Gateway appears wedged (sustained "
                                          "port closure, no login dialog "
                                          "appearing). Escalating to "
                                          "do_restart_in_place()")
                                try:
                                    if do_restart_in_place():
                                        log.info("Wedge recovered via in-place restart")
                                        consecutive_failures = 0
                                        wedged_failures = 0
                                    else:
                                        # v0.4.7: don't sys.exit here —
                                        # in dual-mode the container stays
                                        # up on the other PID and this
                                        # mode's JVM is orphaned.
                                        log.error("In-place restart "
                                                  "returned False; "
                                                  "escalating to long "
                                                  "cool-down")
                                        _escalate_to_jvm_restart(
                                            "wedge do_restart_in_place failed")
                                        consecutive_failures = 0
                                        wedged_failures = 0
                                except Exception as e:
                                    log.error(f"In-place restart raised: "
                                              f"{type(e).__name__}: {e}")
                                    # v0.4.7: same reason — escalate
                                    # instead of sys.exit.
                                    _escalate_to_jvm_restart(
                                        f"wedge do_restart_in_place raised "
                                        f"{type(e).__name__}")
                                    consecutive_failures = 0
                                    wedged_failures = 0
                    else:
                        # v0.4.7: reauth failed => dual-mode-safe recovery
                        # (was sys.exit(1), which is a no-op in dual-mode).
                        log.error("Re-auth failed; attempting dual-mode-"
                                  "safe recovery")
                        try:
                            os.unlink(READY_FILE)
                        except FileNotFoundError:
                            pass
                        _recover_jvm_or_escalate("monitor_loop re-auth failed")
                        consecutive_failures = 0
                        wedged_failures = 0

        time.sleep(5)


def attempt_reauth(app):
    """If a new login dialog has appeared during the monitor loop, re-drive
    the full login sequence. Returns True if re-auth completed (or was
    unnecessary), False if it definitively failed."""
    texts, _ = agent_list()
    if "Username" not in texts and "Password" not in texts:
        # API port is closed but no login dialog is on screen. Could be a
        # transient network issue, a shutdown-in-progress, or silent session
        # loss. Nothing to do here — the next heartbeat will re-check.
        log.warning("API port closed but no login dialog visible — "
                    "probably a transient issue, will re-check next heartbeat")
        return True

    log.info("Login dialog detected during monitor loop — re-driving login")
    # Refresh the app reference (the old one may have gone stale).
    # Scope to our own JVM so dual-mode containers don't cross-drive the
    # other instance's login.
    fresh_app = find_app(APP_NAME_CANDIDATES, timeout=30, match_pid=JVM_PID) or app

    # Clear the readiness file while we're re-authing so consumers know
    # not to use the API port
    try:
        os.unlink(READY_FILE)
    except FileNotFoundError:
        pass

    if not handle_login(fresh_app):
        log.error("Re-auth: login failed")
        return False

    # Check for CCP lockout before burning 120s+ on 2FA/API waits
    if _detect_ccp_lockout(timeout=25):
        _apply_ccp_backoff()
        log.info("Re-auth: CCP lockout detected — backing off. "
                 "Monitor loop will retry on next heartbeat cycle.")
        return True  # let the monitor loop re-check after the backoff

    # Gate: same reasoning as the main() path — don't reset on the
    # stuck-connecting case, only on genuine CCP-gate progress.
    if not _detect_login_stuck_connecting():
        _reset_ccp_backoff()

    if not handle_post_login_dialogs(fresh_app):
        log.error("Re-auth: post-login dialogs failed")
        return False
    if not handle_2fa(fresh_app):
        log.error("Re-auth: 2FA failed")
        return False
    dismiss_post_login_disclaimers(timeout=30)
    if not wait_for_api_port(timeout=120):
        log.error("Re-auth: API port never came back up")
        return False

    signal_ready()
    log.info("Re-auth complete, resuming monitor")
    return True


if __name__ == "__main__":
    main()

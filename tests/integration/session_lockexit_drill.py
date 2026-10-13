"""Existing-session and Lock and Exit drill (issues #63 and #65).

Runs the controller's own code, unmodified, against the Gateway-shaped
windows in SessionLockExitFixture.java through the real agent and its
Unix socket. Only the parts that need a live IBKR session are stubbed:
the API port, and the halt (recorded instead of blocking).

- #65: with only AUTO_RESTART_TIME set, the controller selects Auto
  restart, writes the time and AM/PM, clicks OK and reads it back. Also:
  a panel already in restart mode is left alone, an AM logoff time
  sticks, and a mode Gateway won't show (no daily restart) is switched
  back rather than saved without its time.
- #69: newly selecting Auto restart makes Gateway answer OK with its
  Automatic Restart notice (a modal titled "IBKR Gateway", like the main
  frame) and save the rest only once that's answered; the controller
  recognises it by its text, acknowledges it and waits for the dialog to
  close before reading back. Read-Only API is read back; a value Gateway didn't keep
  raises ALERT_CONFIG_NOT_APPLIED.
- #63: an existing-session prompt during the API-port wait is answered
  with Continue Login once, even though the dialog stays listed for
  1.5 s after the click; a second prompt in the same Gateway gets Cancel
  and a halt; a new Gateway may continue again.

Not part of `make test`: it needs a display and a JDK (~60 s). From the
repo root, with a JDK 17 on PATH:

    rm -rf build/drill && mkdir -p build/drill/agent build/drill/fixture &&
    javac --release 17 -d build/drill/agent agent/GatewayInputAgent.java &&
    jar cfm build/drill/gateway-input-agent.jar agent/manifest.mf -C build/drill/agent . &&
    javac --release 17 -d build/drill/fixture tests/integration/SessionLockExitFixture.java &&
    python3 tests/integration/session_lockexit_drill.py
"""
import logging
import os
import subprocess
import sys
import threading
import time
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
JAR = os.environ.get("AGENT_JAR", os.path.join(REPO, "build/drill/gateway-input-agent.jar"))
FIXTURE = os.environ.get("FIXTURE_CLASSES", os.path.join(REPO, "build/drill/fixture"))
SOCK = "/tmp/session-lockexit-drill.sock"
os.environ["GATEWAY_INPUT_AGENT_SOCKET"] = SOCK
# CONTROLLER_DIR runs another copy of gateway_controller.py, e.g. a release.
sys.path.insert(0, os.environ.get("CONTROLLER_DIR", REPO))
import gateway_controller as gc  # noqa: E402

RESULTS = []
LINES = []
LOGS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""),
          flush=True)


class _Collect(logging.Handler):
    def emit(self, record):
        LOGS.append(record.getMessage())


def since(mark, prefix):
    return [ln for ln in LINES[mark:] if ln.startswith(prefix)]


def config_pass(env):
    """One real post-login config pass with only ``env`` set."""
    keys = ("AUTO_RESTART_TIME", "AUTO_LOGOFF_TIME", "READ_ONLY_API", "TWS_MASTER_CLIENT_ID")
    clean = {k: "" for k in keys}
    clean.update(env)
    mark, log_mark = len(LINES), len(LOGS)
    with patch.dict(os.environ, clean):
        gc.handle_post_login_config()
    time.sleep(0.5)
    return since(mark, "SAVED|"), since(mark, "CANCELLED"), LOGS[log_mark:]


def main():
    for path in (JAR, FIXTURE):
        if not os.path.exists(path):
            print(f"FATAL: {path} missing -- build first (see module docstring)")
            return 2
    try:
        os.unlink(SOCK)
    except FileNotFoundError:
        pass
    java = os.environ.get("JAVA", "java")
    proc = subprocess.Popen(
        [java, f"-javaagent:{JAR}={SOCK}", "-cp", FIXTURE, "SessionLockExitFixture"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def pump():
        for ln in proc.stdout:
            LINES.append(ln.rstrip("\n"))

    threading.Thread(target=pump, daemon=True).start()

    def send(cmd):
        proc.stdin.write(cmd + "\n")
        proc.stdin.flush()
        deadline = time.monotonic() + 5
        while f"ACK|{cmd}" not in LINES and time.monotonic() < deadline:
            time.sleep(0.05)

    handler = _Collect()
    logging.getLogger("controller").addHandler(handler)
    try:
        deadline = time.monotonic() + 60
        while "READY" not in LINES and time.monotonic() < deadline and proc.poll() is None:
            time.sleep(0.2)
        while time.monotonic() < deadline:
            try:
                if (gc._agent_request("PING") or "").startswith("OK"):
                    break
            except OSError:
                pass
            time.sleep(0.2)
        else:
            print("FATAL: fixture/agent never became ready\n" + "\n".join(LINES[-30:]))
            return 2

        print("\n--- #65: only AUTO_RESTART_TIME set, Gateway in its default Auto logoff ---")
        mark = len(LINES)
        saved, cancelled, logs = config_pass(
            {"AUTO_RESTART_TIME": "08:00 PM", "READ_ONLY_API": "no"})
        check("#69: Gateway's Automatic Restart notice was acknowledged",
              since(mark, "NOTICE|") == ["NOTICE|Automatic Restart"]
              and since(mark, "ACKNOWLEDGED") == ["ACKNOWLEDGED"],
              str(since(mark, "NOTICE|") + since(mark, "ACKNOWLEDGED")))
        check("one save", len(saved) == 1, str(saved))
        check("saved in Auto restart mode at 08:00 PM",
              saved and saved[0].startswith("SAVED|mode=restart|time=08:00|ampm=PM|ro=false"),
              str(saved))
        check("Gateway's own read-back verified it",
              any("Verified: Set Auto Restart Time (HH:MM)" in m for m in logs),
              [m for m in logs if "erif" in m][-2:])
        check("no ALERT_CONFIG_NOT_APPLIED",
              not any("ALERT_CONFIG_NOT_APPLIED" in m for m in logs))
        if hasattr(gc, "agent_jstate"):
            check("Read-Only API read back as off",
                  any("Verified: Read-Only API reads back as False" in m for m in logs),
                  [m for m in logs if "Read-Only" in m])

        print("\n--- #65: next boot, the panel is already in Auto restart ---")
        mark = len(LINES)
        if hasattr(gc, "_select_lock_exit_radio"):
            with patch.object(gc, "_select_lock_exit_radio",
                              wraps=gc._select_lock_exit_radio) as radio:
                saved, _c, logs = config_pass({"AUTO_RESTART_TIME": "08:00 PM"})
            modes = [c.args for c in radio.call_args_list
                     if any(a.startswith("Auto") for a in c.args)]
            check("the mode radios were not touched", not modes, str(modes))
        else:
            saved, _c, logs = config_pass({"AUTO_RESTART_TIME": "08:00 PM"})
        check("still Auto restart at 08:00 PM",
              saved and saved[-1].startswith("SAVED|mode=restart|time=08:00|ampm=PM"), str(saved))
        check("no notice when Auto restart was already selected",
              not since(mark, "NOTICE|"), str(since(mark, "NOTICE|")))

        print("\n--- AM logoff time, from a panel in Auto restart ---")
        saved, _c, logs = config_pass({"AUTO_LOGOFF_TIME": "11:39 AM"})
        check("saved in Auto logoff mode at 11:39 AM",
              saved and saved[-1].startswith("SAVED|mode=logoff|time=11:39|ampm=AM"), str(saved))
        check("no ALERT_CONFIG_NOT_APPLIED",
              not any("ALERT_CONFIG_NOT_APPLIED" in m for m in logs))

        print("\n--- Gateway won't show the restart field (daily restart not allowed) ---")
        send("RESET")
        send("NO_DAILY")
        saved, cancelled, logs = config_pass(
            {"AUTO_RESTART_TIME": "08:00 PM", "TWS_MASTER_CLIENT_ID": "7"})
        check("nothing saved in Auto restart mode",
              not any("mode=restart" in s for s in saved), str(saved))
        check("the logoff schedule Gateway had is kept",
              all(s.startswith("SAVED|mode=logoff|time=11:45|ampm=PM") for s in saved), str(saved))
        check("ALERT_CONFIG_NOT_APPLIED says it switched back",
              any("ALERT_CONFIG_NOT_APPLIED" in m and "switched back" in m for m in logs),
              [m for m in logs if "ALERT" in m])
        send("DAILY")

        print("\n--- #69: Gateway doesn't keep Read-Only API off after OK ---")
        send("RESET")
        send("RO_STUCK")
        saved, cancelled, logs = config_pass({"READ_ONLY_API": "no"})
        check("the untick was saved with OK", len(saved) == 1, str(saved))
        check("Gateway kept it on", saved and "|ro=true|" in saved[-1], str(saved))
        check("the read-back closed with Cancel", len(cancelled) == 1, str(cancelled))
        check("ALERT_CONFIG_NOT_APPLIED for Read-Only API",
              any("ALERT_CONFIG_NOT_APPLIED" in m and 'setting="Read-Only API"' in m
                  and "cannot place or change orders" in m for m in logs),
              [m for m in logs if "ALERT" in m or "Read-Only" in m])
        send("RO_KEPT")
        send("RESET")

        print("\n--- #63: prompt during the API-port wait (click lands 1.5 s late) ---")
        for name in ("_existing_session_takeovers", "_existing_session_click_times"):
            getattr(gc, name, []).clear()
        gc._existing_session_continued_this_jvm = False
        gc._existing_session_closed_after_continue = False
        halts = []

        def api_wait(timeout):
            """The real wait_for_api_port; the port opens 2 s after a
            Continue Login click, as Gateway's would once login resumes."""
            mark, log_mark, seen = len(LINES), len(LOGS), {}

            def is_open(*_a, **_k):
                if "CLICKED|Continue Login" in LINES[mark:]:
                    seen.setdefault("t", time.monotonic())
                    return time.monotonic() - seen["t"] > 2.0
                return False

            with patch.object(gc, "is_api_port_open", side_effect=is_open), \
                 patch.object(gc, "_halt_for_operator",
                              side_effect=lambda *a, **k: halts.append(a)):
                send("SESSION")
                ok = gc.wait_for_api_port(timeout=timeout)
            time.sleep(2.0)   # let the fixture's lagged close finish
            return ok, since(mark, "CLICKED|"), LOGS[log_mark:]

        ok, clicks, logs = api_wait(30)
        check("the wait ends with the API port open", ok is True)
        check("Continue Login clicked exactly once", clicks == ["CLICKED|Continue Login"], str(clicks))
        check("no halt", not halts, str(halts))
        check("IBKR's prompt logged with the address masked",
              any("IBKR: IP [IP] connected at 09:28:28" in m for m in logs)
              and not any("8.8.8.8" in m for m in logs),
              [m for m in logs if "IBKR:" in m])

        print("\n--- the prompt comes back in the same Gateway ---")
        ok, clicks, logs = api_wait(15)
        check("Cancel clicked, not Continue", clicks == ["CLICKED|Cancel"], str(clicks))
        check("ALERT_EXISTING_SESSION_CONFLICT logged",
              any(m.startswith("ALERT_EXISTING_SESSION_CONFLICT ") for m in logs),
              [m for m in logs if "ALERT" in m])
        check("halted for an operator", len(halts) == 1, str(halts))

        print("\n--- the 2FA wait re-polls every 0.5 s while the click lands ---")
        # The handler is called again on each poll that still lists the
        # dialog; the answered prompt must not count as IBKR asking twice.
        gc._existing_session_continued_this_jvm = False
        gc._existing_session_closed_after_continue = False
        halts.clear()
        mark, log_mark, seen = len(LINES), len(LOGS), {}

        def is_open(*_a, **_k):
            if "CLICKED|Continue Login" in LINES[mark:]:
                seen.setdefault("t", time.monotonic())
                return time.monotonic() - seen["t"] > 3.0
            return False

        with patch.object(gc, "TOTP_SECRET", ""), \
             patch.dict(os.environ, {"TWOFA_EXIT_INTERVAL": "25"}), \
             patch.object(gc, "is_api_port_open", side_effect=is_open), \
             patch.object(gc, "_halt_for_operator",
                          side_effect=lambda *a, **k: halts.append(a)):
            send("SESSION")
            ok = gc.handle_2fa(None)
        time.sleep(2.0)
        clicks = since(mark, "CLICKED|")
        check("Continue Login clicked once, nothing else", clicks == ["CLICKED|Continue Login"],
              str(clicks))
        check("no halt", not halts, str(halts))

        print("\n--- a new Gateway launch may continue again ---")
        gc._existing_session_continued_this_jvm = False      # what launch_gateway does
        gc._existing_session_closed_after_continue = False
        halts.clear()
        ok, clicks, logs = api_wait(30)
        check("Continue Login clicked once", clicks == ["CLICKED|Continue Login"], str(clicks))
        check("no halt", not halts, str(halts))
    finally:
        logging.getLogger("controller").removeHandler(handler)
        proc.kill()

    passed = sum(1 for _, ok in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} checks passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())

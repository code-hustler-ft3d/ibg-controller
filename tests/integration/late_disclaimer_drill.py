"""Late paper-disclaimer drill (issue #81).

Runs the controller's own _dismiss_late_disclaimer, unmodified, against
the windows in LateDisclaimerFixture.java through the real agent and its
Unix socket. The monitor loop calls that function on every pass.

- The agent's LIST reports a button that is hidden inside a showing
  window, and its plain CLICK acts on one. The check runs every few
  seconds for a whole paper session, so it must not: it clicks through
  CLICK_IN_WIN with an empty title, which takes only a button that is
  showing and enabled, in any showing window.
- Paper: a disclaimer that appears later is accepted and the dialog
  closes. Live: it is left up.
- A label whose button isn't showing is passed over for the next label
  on the allowlist.
- A matching button that keeps coming back is clicked 3 times, then left
  for the operator with one warning.

Not part of `make test`: it needs a display and a JDK (~15 s). From the
repo root, with a JDK 17 or later on PATH:

    rm -rf build/drill && mkdir -p build/drill/agent build/drill/fixture &&
    javac --release 17 -d build/drill/agent agent/GatewayInputAgent.java &&
    jar cfm build/drill/gateway-input-agent.jar agent/manifest.mf -C build/drill/agent . &&
    javac --release 17 -d build/drill/fixture tests/integration/LateDisclaimerFixture.java &&
    python3 tests/integration/late_disclaimer_drill.py
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
SOCK = "/tmp/late-disclaimer-drill.sock"
os.environ["GATEWAY_INPUT_AGENT_SOCKET"] = SOCK
# CONTROLLER_DIR runs another copy of gateway_controller.py, e.g. a release.
sys.path.insert(0, os.environ.get("CONTROLLER_DIR", REPO))
import gateway_controller as gc  # noqa: E402

ACCEPT = "I understand and accept"
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


def clicked(mark):
    return [ln for ln in LINES[mark:] if ln.startswith("CLICKED|")]


def sweep(mode):
    """One monitor-loop pass of the disclaimer check, then time for the
    click to land (CLICK_IN_WIN is asynchronous)."""
    with patch.object(gc, "TRADING_MODE", mode):
        result = gc._dismiss_late_disclaimer()
    time.sleep(0.5)
    return result


def wait_for(predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


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
        [java, f"-javaagent:{JAR}={SOCK}", "-cp", FIXTURE, "LateDisclaimerFixture"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def pump():
        for ln in proc.stdout:
            LINES.append(ln.rstrip("\n"))

    threading.Thread(target=pump, daemon=True).start()

    def send(cmd):
        proc.stdin.write(cmd + "\n")
        proc.stdin.flush()
        wait_for(lambda: f"ACK|{cmd}" in LINES)

    logging.getLogger("controller").addHandler(_Collect())
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

        print("\n--- nothing on screen but a hidden and a greyed-out allowlisted button ---")
        _, buttons = gc.agent_list()
        check("LIST reports the hidden 'I Accept' and the greyed-out 'Acknowledge'",
              {"I Accept", "Acknowledge"} <= buttons, sorted(buttons))
        mark = len(LINES)
        for _ in range(3):
            result = sweep("paper")
        check("paper: three passes click nothing", result is None and not clicked(mark),
              f"result={result!r} {clicked(mark)}")
        check("no click was counted towards the cap", gc._late_disclaimer_clicks == [],
              gc._late_disclaimer_clicks)

        print("\n--- the paper disclaimer appears ---")
        send("DISCLAIMER")
        check("the disclaimer's button is listed",
              wait_for(lambda: ACCEPT in gc.agent_list()[1]))
        mark, log_mark = len(LINES), len(LOGS)
        result = sweep("live")
        check("live: left up", result is None and not clicked(mark)
              and ACCEPT in gc.agent_list()[1], f"result={result!r} {clicked(mark)}")
        result = sweep("paper")
        check("paper: accepted", result == ACCEPT
              and clicked(mark) == [f"CLICKED|dialog|{ACCEPT}"],
              f"result={result!r} {clicked(mark)}")
        check("the dialog closed", wait_for(lambda: ACCEPT not in gc.agent_list()[1]))
        check("logged at WARNING with the button's label",
              any(ln == f"Monitor: clicked {ACCEPT!r} on a disclaimer that appeared "
                        "after the post-login check" for ln in LOGS[log_mark:]),
              LOGS[log_mark:])

        print("\n--- a matching button that keeps coming back ---")
        gc._late_disclaimer_clicks.clear()
        send("STUCK")
        mark, log_mark = len(LINES), len(LOGS)
        results = [sweep("paper") for _ in range(6)]
        check("'I Accept' (hidden) is passed over for the showing 'Acknowledge'",
              results[0] == "Acknowledge", results)
        check("clicked 3 times, then left alone",
              results == ["Acknowledge"] * 3 + [None] * 3
              and clicked(mark) == ["CLICKED|stuck|Acknowledge"] * 3,
              f"{results} {clicked(mark)}")
        check("the greyed-out 'Acknowledge' in the main window was never clicked",
              not any("|disabled|" in ln or "|hidden|" in ln for ln in LINES), clicked(0))
        warned = [ln for ln in LOGS[log_mark:] if "leaving it for the operator" in ln]
        check("one warning, naming the button that was clicked",
              warned == ["Monitor: 'Acknowledge' is still showing after 3 clicks in "
                         "10 min; leaving it for the operator (VNC)"], warned)
        send("CLOSE")

        print("\n--- why not the agent's plain CLICK ---")
        mark = len(LINES)
        gc.agent_click("I Accept")
        check("CLICK acts on the hidden button",
              wait_for(lambda: clicked(mark) == ["CLICKED|hidden|I Accept"]), clicked(mark))
        mark = len(LINES)
        reply = gc._agent_request("CLICK_IN_WIN |I Accept")
        time.sleep(0.5)
        check("CLICK_IN_WIN with an empty title refuses it",
              reply.startswith("ERR not_found") and not clicked(mark),
              f"{reply!r} {clicked(mark)}")
    finally:
        proc.kill()

    passed = sum(1 for _, ok in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} checks passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())

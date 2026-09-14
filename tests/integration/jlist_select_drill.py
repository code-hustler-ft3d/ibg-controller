"""JLIST_SELECT drill against real Swing JLists (issue #33).

Drives the agent's JLIST_SELECT over its real Unix socket against the
dialogs in JListSelectFixture.java, using an agent jar built from this
checkout, inside the release image (Xvfb plus a Java 17 runtime). For
every case it checks the agent's reply AND that the list's selection
actually moved, or didn't, as reported by the fixture.

Issue #33: TWOFA_DEVICE="Mobile Authenticator App" failed against the
list entry "Mobile Authenticator app" because matching was exact. The
drill pins the fix: exact still wins; otherwise one entry matching
without regard to case, spacing or HTML is accepted; entries differing
only in case are refused as ambiguous; and a failure lists the entries.

Not part of `make test`: it needs a display, a JDK to build and ~20 s.
From the repo root:

    docker run --rm -v "$PWD":/work -w /work eclipse-temurin:17-jdk sh -c '
      rm -rf build/drill && mkdir -p build/drill/agent build/drill/fixture &&
      javac --release 17 -d build/drill/agent agent/GatewayInputAgent.java &&
      jar cfm build/drill/gateway-input-agent.jar agent/manifest.mf -C build/drill/agent . &&
      javac --release 17 -d build/drill/fixture tests/integration/JListSelectFixture.java'
    docker run --rm -v "$PWD":/work --entrypoint sh \
      ghcr.io/code-hustler-ft3d/ibg-controller:<tag> -c \
      'python3 /work/tests/integration/jlist_select_drill.py'
"""
import glob
import os
import socket
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
JAR = os.environ.get("AGENT_JAR", os.path.join(REPO, "build/drill/gateway-input-agent.jar"))
FIXTURE = os.environ.get("FIXTURE_CLASSES", os.path.join(REPO, "build/drill/fixture"))
SOCK = "/tmp/jlist-select-drill.sock"
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""),
          flush=True)


def find_java():
    if os.environ.get("JAVA"):
        return os.environ["JAVA"]
    hits = sorted(glob.glob("/usr/local/i4j_jres/*/*/bin/java"))
    return hits[0] if hits else "java"


def request(line, timeout=10):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(SOCK)
        s.sendall((line + "\n").encode("utf-8"))
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        return buf.split(b"\n", 1)[0].decode("utf-8", "replace")
    finally:
        s.close()


def main():
    for path in (JAR, FIXTURE):
        if not os.path.exists(path):
            print(f"FATAL: {path} missing -- build first (see module docstring)")
            return 2

    env = dict(os.environ)
    xvfb = None
    if not env.get("DISPLAY"):
        xvfb = subprocess.Popen(["Xvfb", ":99", "-screen", "0", "1024x768x24"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        env["DISPLAY"] = ":99"
        time.sleep(2)
    try:
        os.unlink(SOCK)
    except FileNotFoundError:
        pass

    java = find_java()
    print(f"java: {java}\nagent jar: {JAR}")
    proc = subprocess.Popen(
        [java, f"-javaagent:{JAR}={SOCK}", "-cp", FIXTURE, "JListSelectFixture"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    lines = []

    def pump():
        for ln in proc.stdout:
            lines.append(ln.rstrip("\n"))

    threading.Thread(target=pump, daemon=True).start()

    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and "READY" not in lines:
            if proc.poll() is not None:
                break
            time.sleep(0.2)
        if "READY" not in lines:
            print("FATAL: fixture never became ready")
            print("\n".join(lines[-30:]))
            return 2
        while time.monotonic() < deadline:
            try:
                if request("PING", timeout=2).startswith("OK"):
                    break
            except OSError:
                time.sleep(0.2)

        def select(title, item, expect_ok, index=None, reply_has=(), name=""):
            before = len(lines)
            reply = request(f"JLIST_SELECT {title}|{item}")
            check(f"{name}: replies {'OK' if expect_ok else 'ERR'}",
                  reply.startswith("OK") == expect_ok, reply)
            for frag in reply_has:
                check(f"{name}: reply contains {frag!r}", frag in reply, reply)
            time.sleep(0.8)
            moved = [ln for ln in lines[before:] if ln.startswith("SELECTED|")]
            if expect_ok:
                check(f"{name}: selection actually moved to index {index}",
                      bool(moved) and moved[-1].endswith(f"|{index}"), str(moved))
            else:
                check(f"{name}: selection left alone", not moved, str(moved))

        print("\n--- Gateway-shaped selector, IB Key pre-selected ---")
        select("Second Factor", "Mobile Authenticator app", True, 1,
               ["selected=Mobile Authenticator app"], "exact label")
        select("Second Factor", "ib key", True, 0, ["selected=IB Key"], "lower-case")
        select("Second Factor", "Mobile Authenticator App", True, 1,
               ["selected=Mobile Authenticator app"], "issue #33's value")
        select("Second Factor", "IB KEY", True, 0, (), "upper-case")
        select("Second Factor", "  mobile   authenticator   app  ", True, 1, (), "extra whitespace")
        select("Second Factor", "Mobile Authenticator", False, None,
               ["jlist_item_not_found", "have=[IB Key | Mobile Authenticator app]"],
               "a prefix is not a match, and the error lists the entries")

        print("\n--- toString() differs from the painted label ---")
        select("Renderer Case", "Mobile Authenticator app", True, 1,
               ["selected=Mobile Authenticator app"], "painted label")
        select("Renderer Case", "IBKEY", True, 0, ["selected=IB Key"], "toString() still honoured")

        print("\n--- renderer paints HTML ---")
        select("Html Case", "mobile authenticator app", True, 1,
               ["selected=Mobile Authenticator app"], "markup ignored")

        print("\n--- two entries differing only in case ---")
        select("Ambiguous Case", "mobile authenticator app", False, None,
               ["jlist_item_ambiguous"], "refused, not guessed")
        select("Ambiguous Case", "Mobile Authenticator app", True, 0, (), "exact still wins")

        print("\n--- errors that must not change ---")
        r = request("JLIST_SELECT No Such Window|x")
        check("missing window: ERR not_found", r.startswith("ERR not_found"), r)
        r = request("JLIST_SELECT no pipe here")
        check("malformed request: ERR missing_pipe", r.startswith("ERR jlist_select_missing_pipe"), r)
    finally:
        proc.kill()
        if xvfb is not None:
            xvfb.kill()

    passed = sum(1 for _, ok in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} checks passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())

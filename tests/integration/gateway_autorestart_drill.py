"""Real-Gateway restart drill for the issue #23 adoption path.

Launches the REAL IB Gateway JVM through the REAL install4j launcher with
the REAL input agent, invokes the REAL .install4j/restarter with the
environment Gateway itself carries, and runs the controller's recovery
path against whatever comes up.

This is the integration counterpart to the mocked unit tests in
tests/test_pure_logic.py and the process-level tests in
tests/test_core_logic.py. Those substitute Gateway; this one doesn't.

**No credentials are supplied and nothing authenticates against IBKR**,
so no session slot is touched and there is no CCP-lockout exposure. The
replacement Gateway therefore sits at its login dialog and never opens
its API port, so the one fact the drill stands in for is the API port
being open -- i.e. Gateway carrying the session across its own restart,
which is IBKR behaviour the controller neither causes nor changes.

Not part of `make test`: it needs a Gateway installation, an X display
and ~2 minutes. Run it inside a throwaway container built from the
release image, never against a container holding a live session:

    docker run -d --name ibg-drill --entrypoint sleep \
      -v "$PWD":/repo:ro ghcr.io/<owner>/ibg-controller:<tag> 3600
    docker exec -d ibg-drill sh -c 'Xvfb :1 -screen 0 1024x768x24 &'
    docker exec -e DISPLAY=:1 -e TRADING_MODE=paper \
      -e TWS_SETTINGS_PATH=/home/ibgateway/Jts_paper \
      -e GATEWAY_INPUT_AGENT_SOCKET=/tmp/gateway-input-paper.sock \
      -e CONTROLLER_READY_FILE=/tmp/gateway_ready_paper \
      ibg-drill python3 /repo/tests/integration/gateway_autorestart_drill.py

Observed 2026-09-07 against Gateway 10.45.1g, 17/17 checks: the
restarter inherits INSTALL4J_ADD_VM_PARAMS and loads the agent, it holds
the agent socket for ~1s between the old JVM dying and the replacement
binding it, and the controller adopts the replacement (a JVM it never
spawned) in 0.5s without launching a second Gateway.
"""
import importlib.util
import os
import subprocess
import sys
import time
from unittest.mock import patch

os.environ.setdefault("TRADING_MODE", "paper")
_REPO = os.environ.get("IBG_REPO", "/repo")
spec = importlib.util.spec_from_file_location(
    "gateway_controller", os.path.join(_REPO, "gateway_controller.py"))
gc = importlib.util.module_from_spec(spec)
sys.modules["gateway_controller"] = gc
spec.loader.exec_module(gc)

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""),
          flush=True)


def java_pids():
    out = []
    for p in os.listdir("/proc"):
        if not p.isdigit():
            continue
        cl = gc._process_cmdline(int(p)) or ""
        if "/bin/java" in cl and "jts" in cl.lower():
            out.append(int(p))
    return out


def main():
    install = os.path.dirname(gc.find_gateway_launcher())
    restarter = os.path.join(install, ".install4j", "restarter")
    log_path = os.path.join(install, ".install4j", "restarter.log")
    print(f"install dir : {install}")
    print(f"restarter   : {restarter} (exists={os.path.exists(restarter)})")
    try:
        os.unlink(log_path)
    except FileNotFoundError:
        pass

    print("\n--- launching the real Gateway JVM ---", flush=True)
    gc.GATEWAY_PROC = gc.launch_gateway()
    launcher_pid = gc.GATEWAY_PROC.pid
    if not gc.agent_wait_ready(timeout=120):
        print("FATAL: agent never came up")
        return 2
    gc.JVM_PID = gc.agent_get_pid()
    old_pid = gc.JVM_PID
    print(f"launcher pid={launcher_pid}  agent-reported JVM pid={old_pid}")
    print(f"java procs before: {java_pids()}")
    cl = gc._process_cmdline(old_pid) or ""
    check("real Gateway JVM loaded our agent",
          "gateway-input-agent.jar" in cl)

    # The premise the whole fix rests on: Gateway's own process
    # environment carries INSTALL4J_ADD_VM_PARAMS, so the restarter it
    # spawns inherits it, and so does the Gateway the restarter launches.
    with open(f"/proc/{old_pid}/environ", "rb") as f:
        raw_env = f.read()
    gw_env = {}
    for item in raw_env.split(b"\x00"):
        if b"=" in item:
            k, v = item.split(b"=", 1)
            gw_env[k.decode()] = v.decode("utf-8", "replace")
    vm_params = gw_env.get("INSTALL4J_ADD_VM_PARAMS", "")
    check("Gateway's own environ carries INSTALL4J_ADD_VM_PARAMS",
          bool(vm_params))
    check("...and it names our agent jar + socket",
          "gateway-input-agent.jar" in vm_params and gc.AGENT_SOCKET in vm_params,
          vm_params[:90])
    check("real Gateway is NOT flagged as the restarter",
          not gc._is_install4j_restarter_pid(old_pid))

    # Gateway's own restart does two things: ask the calling launcher to
    # shut down (via a shutdown-file property only Gateway's JVM sets),
    # then re-execute the launcher. The drill supplies the first half
    # directly and lets the real restarter do the second, so the sequence
    # the controller observes is the production one: the old JVM exits,
    # and a replacement it did not spawn comes up on the same socket.
    print("\n--- invoking the REAL install4j restarter ---", flush=True)
    launcher = gc.find_gateway_launcher()
    r = subprocess.Popen(
        [restarter, str(launcher_pid), launcher,
         f"-VjtsConfigDir={gc.JTS_CONFIG_DIR}", "-VinstallerType=standalone"],
        cwd=install, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env=gw_env)                      # exactly Gateway's own environment
    gc.GATEWAY_PROC.terminate()          # stands in for the shutdown file

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and gc.GATEWAY_PROC.poll() is None:
        time.sleep(0.2)
    rc = gc.GATEWAY_PROC.poll()
    print(f"old Gateway exit code: {rc}")
    check("old Gateway is gone", rc is not None, f"rc={rc}")
    # The socket does not sit idle: install4j's restarter binds it within
    # a second of the old JVM dying, before the replacement Gateway takes
    # it over. That is precisely the window the restarter discrimination
    # exists for, and it is observed here rather than hypothesised.
    check("the agent socket file survives the old JVM",
          os.path.exists(gc.AGENT_SOCKET))

    saw_restarter_on_socket = []
    t0 = time.monotonic()
    while time.monotonic() - t0 < 10:
        try:
            pid = gc.agent_get_pid()
        except Exception:
            pid = None
        if pid and pid != old_pid and gc._is_install4j_restarter_pid(pid):
            saw_restarter_on_socket.append(pid)
            break
        time.sleep(0.2)
    print(f"restarter seen holding the agent socket: {saw_restarter_on_socket}")

    print("\n--- controller recovery (the adoption path) ---", flush=True)
    relaunches = []
    # Without credentials the replacement Gateway sits at its login
    # dialog and never opens the API port. In production it carries the
    # session across and the port is already open, which is the branch
    # under test, so stand in for that one fact and leave everything
    # else real.
    with patch.object(gc, "do_restart_in_place",
                      side_effect=lambda: (relaunches.append(1), True)[1]), \
         patch.object(gc, "is_api_port_open", lambda *a, **k: True), \
         patch.object(gc, "_redrive_login",
                      side_effect=AssertionError(
                          "re-login must not run when the session is preserved")):
        t1 = time.monotonic()
        ok = gc._recover_jvm_or_escalate(
            f"JVM exited with code {rc}", exit_code=0)
        elapsed = time.monotonic() - t1

    new_pid = gc.JVM_PID
    print(f"recovery returned {ok} in {elapsed:.1f}s; JVM_PID {old_pid} -> {new_pid}")
    print(f"restarter.log written: {os.path.exists(log_path)}")
    if os.path.exists(log_path):
        print("--- restarter.log ---")
        print(open(log_path).read()[:600])

    check("restarter.log landed where the code looks",
          os.path.exists(log_path))
    check("controller did NOT launch a second Gateway", not relaunches,
          f"do_restart_in_place calls={len(relaunches)}")
    check("adoption succeeded", bool(ok))
    check("adopted a DIFFERENT JVM than the one that exited",
          new_pid is not None and new_pid != old_pid,
          f"{old_pid} -> {new_pid}")
    check("GATEWAY_PROC is the adopted stand-in",
          isinstance(gc.GATEWAY_PROC, gc._AdoptedProcess))
    if isinstance(gc.GATEWAY_PROC, gc._AdoptedProcess):
        check("adopted process reports alive",
              gc.GATEWAY_PROC.poll() is None)
    if new_pid:
        ncl = gc._process_cmdline(new_pid) or ""
        check("adopted process is Gateway, not the restarter",
              "gateway-input-agent.jar" in ncl
              and not gc._is_install4j_restarter_pid(new_pid))
        check("adopted Gateway inherited our -javaagent",
              "gateway-input-agent.jar" in ncl)

    check("readiness re-signalled", os.path.exists(gc.READY_FILE))
    live = java_pids()
    classified = []
    for p in live:
        kind = ("restarter" if gc._is_install4j_restarter_pid(p)
                else "gateway" if p == new_pid else "OTHER-GATEWAY")
        classified.append(f"{p}={kind}")
    print(f"java procs after: {classified}")
    gateways = [p for p in live if not gc._is_install4j_restarter_pid(p)]
    check("exactly one Gateway JVM (restarter aside) is running",
          len(gateways) == 1 and gateways[0] == new_pid, str(classified))
    # The restarter is short-lived; confirm it goes away on its own.
    t = time.monotonic()
    while time.monotonic() - t < 20:
        leftover = [p for p in java_pids() if gc._is_install4j_restarter_pid(p)]
        if not leftover:
            break
        time.sleep(0.5)
    check("the restarter JVM exits on its own",
          not [p for p in java_pids() if gc._is_install4j_restarter_pid(p)])
    live = java_pids()

    try:
        r.wait(timeout=5)
    except Exception:
        r.kill()
    out = r.stdout.read().decode("utf-8", "replace") if r.stdout else ""
    print(f"--- restarter output ---\n{out[:400]}")

    for p in live:
        try:
            os.kill(p, 9)
        except Exception:
            pass

    passed = sum(1 for _, o in RESULTS if o)
    print(f"\n{passed}/{len(RESULTS)} checks passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())

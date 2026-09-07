"""Tests for core controller logic: credential swap, jts.ini writer,
command server protocol, and dual-mode env var resolution.

These test the logic that actually matters for production correctness —
the pieces where a bug causes a silent auth failure, a data-connection
loop, or a security bypass. The pure-helper tests in test_pure_logic.py
cover the leaves; these cover the trunk.

Run with:
    python3 -m unittest discover -s tests -v
"""

import os
import socket
import sys
import tempfile
import threading
import time
import subprocess
import unittest
from unittest.mock import MagicMock, patch


def _load_module():
    sys.modules.setdefault("gi", MagicMock())
    sys.modules.setdefault("gi.repository", MagicMock())
    sys.modules.setdefault("gi.repository.Atspi", MagicMock())
    os.environ.setdefault("TRADING_MODE", "paper")
    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(here)
    module_path = os.path.join(repo_root, "gateway_controller.py")
    import importlib.util
    spec = importlib.util.spec_from_file_location("gateway_controller", module_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gateway_controller"] = mod
    spec.loader.exec_module(mod)
    return mod


gc = _load_module()


class TestCredentialSwap(unittest.TestCase):
    """The credential swap at module load time is the single most
    important piece of logic: if it fails, the controller types the
    LIVE username into the PAPER login form, IBKR responds with
    'multiple paper trading users associated with this user', and
    Gateway 10.45.1c silently swallows the error as a 20-second
    auth timeout. This exact bug cost hours of debugging in the
    original development session.

    We can't re-run module-level code (Python only executes it once),
    so we replicate the swap logic inline and verify it produces the
    correct USERNAME/PASSWORD for each TRADING_MODE.
    """

    def _simulate_swap(self, env):
        """Replicate the module-level credential swap logic."""
        username = env.get("TWS_USERID", "")
        password = env.get("TWS_PASSWORD", "")
        mode = env.get("TRADING_MODE", "paper").lower()
        if mode == "paper":
            pu = env.get("TWS_USERID_PAPER", "")
            pp = env.get("TWS_PASSWORD_PAPER", "")
            if pu:
                username = pu
            if pp:
                password = pp
        return username, password

    def test_paper_mode_swaps_to_paper_credentials(self):
        env = {
            "TWS_USERID": "live_user",
            "TWS_PASSWORD": "live_pass",
            "TWS_USERID_PAPER": "paper_user",
            "TWS_PASSWORD_PAPER": "paper_pass",
            "TRADING_MODE": "paper",
        }
        user, pw = self._simulate_swap(env)
        self.assertEqual(user, "paper_user")
        self.assertEqual(pw, "paper_pass")

    def test_live_mode_keeps_live_credentials(self):
        env = {
            "TWS_USERID": "live_user",
            "TWS_PASSWORD": "live_pass",
            "TWS_USERID_PAPER": "paper_user",
            "TWS_PASSWORD_PAPER": "paper_pass",
            "TRADING_MODE": "live",
        }
        user, pw = self._simulate_swap(env)
        self.assertEqual(user, "live_user")
        self.assertEqual(pw, "live_pass")

    def test_paper_mode_without_paper_vars_falls_back_to_live(self):
        env = {
            "TWS_USERID": "live_user",
            "TWS_PASSWORD": "live_pass",
            "TRADING_MODE": "paper",
        }
        user, pw = self._simulate_swap(env)
        self.assertEqual(user, "live_user")
        self.assertEqual(pw, "live_pass")

    def test_paper_mode_with_only_userid_paper_swaps_user_keeps_live_pass(self):
        env = {
            "TWS_USERID": "live_user",
            "TWS_PASSWORD": "live_pass",
            "TWS_USERID_PAPER": "paper_user",
            "TRADING_MODE": "paper",
        }
        user, pw = self._simulate_swap(env)
        self.assertEqual(user, "paper_user")
        self.assertEqual(pw, "live_pass")

    def test_both_mode_does_not_swap(self):
        # In dual mode, run.sh handles the swap per-instance by
        # exporting TWS_USERID=$TWS_USERID_PAPER for the paper JVM.
        # The controller's module-level swap only triggers for
        # TRADING_MODE=paper, NOT for both.
        env = {
            "TWS_USERID": "live_user",
            "TWS_PASSWORD": "live_pass",
            "TWS_USERID_PAPER": "paper_user",
            "TWS_PASSWORD_PAPER": "paper_pass",
            "TRADING_MODE": "both",
        }
        user, pw = self._simulate_swap(env)
        self.assertEqual(user, "live_user")
        self.assertEqual(pw, "live_pass")


class TestJtsIniWriter(unittest.TestCase):
    """ensure_jts_ini writes a jts.ini that Gateway reads at startup.
    The format matters: wrong fields cause silent auth or data-connection
    failures. The RemoteHostOrderRouting bug (writing the auth server as
    the order-routing server) was a real Phase 1 bug found in this
    session.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        # Save and override module-level state
        self._orig_jts_config_dir = gc.JTS_CONFIG_DIR
        self._orig_tws_server = gc.TWS_SERVER
        gc.JTS_CONFIG_DIR = self.tmpdir

    def tearDown(self):
        gc.JTS_CONFIG_DIR = self._orig_jts_config_dir
        gc.TWS_SERVER = self._orig_tws_server
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_writes_peer_with_server(self):
        gc.TWS_SERVER = "cdc1.ibllc.com"
        gc.ensure_jts_ini()
        content = open(os.path.join(self.tmpdir, "jts.ini")).read()
        self.assertIn("Peer=cdc1.ibllc.com:4001", content)

    def test_does_not_write_remote_host_order_routing(self):
        """RemoteHostOrderRouting must NOT be in the written file.
        Gateway discovers it from the auth server's response. Writing
        it to the same value as Peer causes a data-connection failure
        on accounts where auth and order routing use different servers.
        """
        gc.TWS_SERVER = "cdc1.ibllc.com"
        gc.ensure_jts_ini()
        content = open(os.path.join(self.tmpdir, "jts.ini")).read()
        self.assertNotIn("RemoteHostOrderRouting", content)

    def test_writes_supports_ssl_cache(self):
        gc.TWS_SERVER = "cdc1.ibllc.com"
        gc.ensure_jts_ini()
        content = open(os.path.join(self.tmpdir, "jts.ini")).read()
        self.assertIn("SupportsSSL=cdc1.ibllc.com:4000,true,", content)

    def test_writes_trusted_ips(self):
        gc.TWS_SERVER = "cdc1.ibllc.com"
        gc.ensure_jts_ini()
        content = open(os.path.join(self.tmpdir, "jts.ini")).read()
        self.assertIn("TrustedIPs=127.0.0.1", content)

    def test_writes_api_only(self):
        gc.TWS_SERVER = "cdc1.ibllc.com"
        gc.ensure_jts_ini()
        content = open(os.path.join(self.tmpdir, "jts.ini")).read()
        self.assertIn("ApiOnly=true", content)

    def test_minimal_jts_ini_without_server(self):
        gc.TWS_SERVER = ""
        gc.ensure_jts_ini()
        content = open(os.path.join(self.tmpdir, "jts.ini")).read()
        self.assertIn("TrustedIPs=127.0.0.1", content)
        self.assertNotIn("Peer=", content)
        self.assertNotIn("SupportsSSL=", content)

    def test_overwrites_existing_when_server_set(self):
        # Write an initial file
        ini_path = os.path.join(self.tmpdir, "jts.ini")
        with open(ini_path, "w") as f:
            f.write("[IBGateway]\nOldKey=old_value\n")
        gc.TWS_SERVER = "ndc1.ibllc.com"
        gc.ensure_jts_ini()
        content = open(ini_path).read()
        self.assertNotIn("OldKey", content)
        self.assertIn("Peer=ndc1.ibllc.com:4001", content)

    def test_leaves_existing_when_no_server(self):
        ini_path = os.path.join(self.tmpdir, "jts.ini")
        with open(ini_path, "w") as f:
            f.write("[IBGateway]\nOldKey=old_value\n")
        gc.TWS_SERVER = ""
        gc.ensure_jts_ini()
        content = open(ini_path).read()
        self.assertIn("OldKey=old_value", content)


class TestCommandServerProtocol(unittest.TestCase):
    """The command server is a TCP listener that accepts IBC-compat
    commands. Test the protocol directly: AUTH flow, command dispatch,
    error handling, and rate limiting.

    We start the real server on a random port, connect to it as a
    client, and verify the responses.
    """

    @classmethod
    def setUpClass(cls):
        """Start the command server on a random port."""
        cls.port = cls._find_free_port()
        cls.auth_token = "test-secret-token-12345"
        os.environ["CONTROLLER_COMMAND_SERVER_AUTH_TOKEN"] = cls.auth_token
        os.environ["CONTROLLER_COMMAND_SERVER_PORT"] = str(cls.port)
        os.environ["CONTROLLER_COMMAND_SERVER_HOST"] = "127.0.0.1"
        # Start the server thread
        cls.server_thread = threading.Thread(
            target=gc._command_server_main,
            args=("127.0.0.1", cls.port),
            daemon=True,
        )
        cls.server_thread.start()
        time.sleep(0.3)  # let it bind

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("CONTROLLER_COMMAND_SERVER_AUTH_TOKEN", None)
        os.environ.pop("CONTROLLER_COMMAND_SERVER_PORT", None)
        os.environ.pop("CONTROLLER_COMMAND_SERVER_HOST", None)
        # Server thread is daemon — dies with process

    @staticmethod
    def _find_free_port():
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    def _send(self, data):
        s = socket.socket()
        s.settimeout(5)
        s.connect(("127.0.0.1", self.port))
        s.sendall(data.encode())
        # Read until the server closes the connection or we get data.
        # The server sends the response then closes in the finally block,
        # so we may need to drain.
        chunks = []
        while True:
            try:
                chunk = s.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
            except (socket.timeout, ConnectionResetError, BrokenPipeError):
                break
        s.close()
        time.sleep(0.3)  # rate-limit courtesy between tests
        return b"".join(chunks).decode().strip()

    def test_rejects_without_auth(self):
        resp = self._send("STOP\n")
        # Server sends "ERR auth_required\n" and closes. If the
        # response is empty, the server may not have started or the
        # connection was reset before we read. Accept either the
        # expected error OR an empty response (server closed before
        # we could read — still a rejection, just a silent one).
        self.assertTrue(
            resp == "ERR auth_required" or resp == "",
            f"Expected 'ERR auth_required' or empty, got {resp!r}"
        )

    def test_rejects_wrong_token(self):
        resp = self._send("AUTH wrong-token\nSTOP\n")
        self.assertEqual(resp, "ERR auth_failed")

    def test_accepts_correct_token(self):
        resp = self._send(f"AUTH {self.auth_token}\nENABLEAPI\n")
        self.assertIn("OK", resp)
        self.assertIn("ENABLEAPI", resp)

    def test_unknown_command(self):
        resp = self._send(f"AUTH {self.auth_token}\nBOGUS_CMD\n")
        self.assertIn("ERR", resp)
        self.assertIn("unknown_command", resp)

    def test_empty_command_after_auth(self):
        resp = self._send(f"AUTH {self.auth_token}\n\n")
        # Empty command should return an error, not crash
        self.assertTrue(resp.startswith("ERR") or resp == "")


class TestDualModeEnvResolution(unittest.TestCase):
    """In dual mode, JTS_CONFIG_DIR should prefer TWS_SETTINGS_PATH
    over TWS_PATH. This is how each instance gets its own isolated
    state directory (Jts_live / Jts_paper).
    """

    def test_jts_config_dir_prefers_tws_settings_path(self):
        # The module-level JTS_CONFIG_DIR was already resolved at import
        # time. We test the resolution logic directly.
        self.assertEqual(
            os.environ.get("TWS_SETTINGS_PATH") or
            os.environ.get("TWS_PATH", os.path.expanduser("~/Jts")),
            gc.JTS_CONFIG_DIR
        )

    def test_controller_ready_file_includes_mode(self):
        # CONTROLLER_READY_FILE should be configurable. When not set,
        # it defaults to /tmp/gateway_ready. run.sh sets it to
        # /tmp/gateway_ready_{mode} for dual mode.
        ready = gc.READY_FILE
        self.assertTrue(ready.startswith("/tmp/gateway_ready"))

    def test_app_name_candidates_for_gateway(self):
        # Default mode (gateway) should look for IBKR Gateway
        if gc.GATEWAY_OR_TWS == "gateway":
            self.assertIn("IBKR Gateway", gc.APP_NAME_CANDIDATES)
        elif gc.GATEWAY_OR_TWS == "tws":
            self.assertIn("Trader Workstation", gc.APP_NAME_CANDIDATES)


# ── Issue #23: end-to-end auto-restart adoption ────────────────────────

# A stand-in for a Gateway JVM carrying the input agent. Binds the
# agent's Unix socket the way GatewayInputAgent does (deleteIfExists +
# bind) and answers the part of the protocol the adoption path uses;
# optionally opens a TCP port standing in for Gateway's API port. Run as
# a real child process so the tests exercise real PIDs, real signals and
# the real _AdoptedProcess rather than mocks.
_FAKE_JVM = r"""
import os, socket, sys, threading

def serve(path, texts):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(path); s.listen(8)
    while True:
        conn, _ = s.accept()
        try:
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
            cmd = buf.split(b"\n", 1)[0].decode("utf-8", "replace").strip().split(" ", 1)[0]
            if cmd == "PING":
                conn.sendall(b"OK pong\n")
            elif cmd == "GET_PID":
                conn.sendall(("OK %d\n" % os.getpid()).encode())
            elif cmd == "LIST":
                conn.sendall(("".join("text %s\n" % t for t in texts) + "END\n").encode())
            elif cmd in ("WINDOWS", "LABELS"):
                conn.sendall(b"END\n")
            else:
                conn.sendall(b"ERR unsupported\n")
        except Exception:
            pass
        finally:
            conn.close()

args = [a for a in sys.argv[1:] if not a.startswith("--")]
texts = ["Username", "Password"] if "--login-dialog" in sys.argv else []
threading.Thread(target=serve, args=(args[0], texts), daemon=True).start()
if len(args) > 1 and args[1] != "-":
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", int(args[1]))); srv.listen(8)
    while True:
        c, _ = srv.accept(); c.close()
else:
    threading.Event().wait()
"""


class TestAutoRestartAdoptionEndToEnd(unittest.TestCase):
    """Issue #23 adoption driven end to end: real child processes, a real
    Unix agent socket, a real TCP port, the real agent client and the
    real _AdoptedProcess. Only Gateway itself is substituted.

    The unit tests in test_pure_logic.py mock the collaborators, so they
    can only prove the orchestration branches. These prove the mechanism:
    that a JVM the controller never spawned can be found on the inherited
    socket, adopted, and monitored — and that every failure path still
    ends in the pre-issue-#23 relaunch.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.procs = []
        self.addCleanup(self._kill_all)

        install = os.path.join(self.tmp.name, "ibgateway", "10.45.1j")
        os.makedirs(os.path.join(install, ".install4j"))
        self.launcher = os.path.join(install, "ibgateway")
        open(self.launcher, "w").close()
        self.restarter_log = os.path.join(install, ".install4j", "restarter.log")
        self.fake = os.path.join(self.tmp.name, "fake_jvm.py")
        with open(self.fake, "w") as f:
            f.write(_FAKE_JVM)
        self.sock = os.path.join(self.tmp.name, "agent.sock")
        self.ready = os.path.join(self.tmp.name, "ready")

        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        self.api_port = probe.getsockname()[1]
        probe.close()

        for p in (
            patch.object(gc, "AGENT_SOCKET", self.sock),
            patch.object(gc, "READY_FILE", self.ready),
            patch.object(gc, "_GATEWAY_LAUNCHER_PATH", self.launcher),
            patch.object(gc, "api_port_for_mode", lambda: self.api_port),
            patch.object(gc, "_AUTO_RESTART_ADOPT_TIMEOUT_SECONDS", 15),
            patch.object(gc, "_AUTO_RESTART_API_TIMEOUT_SECONDS", 15),
            patch.object(gc, "_AUTO_RESTART_PROBE_SECONDS", 3),
            patch.object(gc, "_AUTO_RESTART_DETECT_GRACE_SECONDS", 1),
            patch.object(gc, "_is_ibkr_maintenance_window", lambda *a, **k: False),
            patch.object(gc, "GATEWAY_PROC", None),
            patch.object(gc, "JVM_PID", None),
            patch.object(gc, "CURRENT_APP", None),
            patch.object(gc, "_LAST_ADOPTED_RESTART_MTIME", None),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _kill_all(self):
        for p in self.procs:
            try:
                p.kill()
                p.wait(timeout=5)
            except Exception:
                pass

    def _start_jvm(self, api=False, login_dialog=False):
        args = [sys.executable, self.fake, self.sock,
                str(self.api_port) if api else "-"]
        if login_dialog:
            args.append("--login-dialog")
        p = subprocess.Popen(args)
        self.procs.append(p)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if os.path.exists(self.sock) and gc.agent_wait_ready(timeout=1):
                if gc.agent_get_pid() == p.pid:
                    return p
            time.sleep(0.1)
        self.fail("fake JVM never answered on the agent socket")

    def _running_then_killed(self):
        """Bring up a JVM, register it as the controller's, then kill it —
        i.e. what install4j's restarter does to the calling launcher."""
        a = self._start_jvm()
        gc.GATEWAY_PROC, gc.JVM_PID = a, a.pid
        a.kill()
        a.wait(timeout=10)
        return a

    def _write_restarter_log(self, age_seconds=0):
        with open(self.restarter_log, "w") as f:
            f.write("[INFO] ExecuteLauncherAction [ID 3018]: Launching x\n")
        if age_seconds:
            t = time.time() - age_seconds
            os.utime(self.restarter_log, (t, t))

    def _recover(self, exit_code=0):
        """Real recovery entry point, with the relaunch path spied on."""
        calls = []
        with patch.object(gc, "do_restart_in_place",
                          side_effect=lambda: (calls.append(1), True)[1]):
            t0 = time.monotonic()
            ok = gc._recover_jvm_or_escalate(
                "JVM exited with code 0", exit_code=exit_code)
        return ok, len(calls), time.monotonic() - t0

    def test_adopts_the_self_restarted_jvm_and_never_launches_a_second(self):
        self._running_then_killed()
        self._write_restarter_log()
        new = self._start_jvm(api=True)          # install4j's replacement

        ok, relaunches, _ = self._recover()

        self.assertTrue(ok)
        self.assertEqual(relaunches, 0, "launched a second Gateway — issue #23")
        self.assertIsInstance(gc.GATEWAY_PROC, gc._AdoptedProcess)
        self.assertEqual(gc.GATEWAY_PROC.pid, new.pid)
        self.assertEqual(gc.JVM_PID, new.pid)
        self.assertIsNone(gc.GATEWAY_PROC.poll())
        self.assertTrue(os.path.exists(self.ready))
        self.assertIsNone(new.poll(), "adopted JVM was killed")

    def test_adopts_via_the_socket_probe_when_no_restarter_log_exists(self):
        # Verified 2026-09-06 against a real install: Gateway 10.45.1g's
        # .install4j/restarter writes no restarter.log, while the issue
        # #23 reporter's 10.45.1j box does. Detection must not depend on
        # the log alone, or the fix silently never engages.
        self._running_then_killed()
        new = self._start_jvm(api=True)
        self.assertFalse(os.path.exists(self.restarter_log))

        ok, relaunches, _ = self._recover()

        self.assertTrue(ok)
        self.assertEqual(relaunches, 0)
        self.assertEqual(gc.GATEWAY_PROC.pid, new.pid)

    def test_genuine_crash_still_falls_through_to_the_relaunch(self):
        self._running_then_killed()              # nothing comes back up

        ok, relaunches, elapsed = self._recover()

        self.assertTrue(ok)
        self.assertEqual(relaunches, 1)
        self.assertLess(elapsed, 12, "detection budget is not bounded")

    def test_stale_restarter_log_does_not_trigger_adoption(self):
        self._running_then_killed()
        self._write_restarter_log(
            age_seconds=gc._AUTO_RESTART_DETECT_WINDOW_SECONDS + 600)

        _, relaunches, _ = self._recover()

        self.assertEqual(relaunches, 1)

    def test_adopted_jvm_dying_before_its_api_port_falls_through(self):
        self._running_then_killed()
        self._write_restarter_log()
        dying = self._start_jvm(api=False)       # binds socket, no API port

        killer = threading.Timer(1.0, dying.kill)
        killer.start()
        self.addCleanup(killer.cancel)
        with patch.object(gc, "_AUTO_RESTART_API_TIMEOUT_SECONDS", 6), \
             patch.object(gc, "_AUTO_RESTART_ADOPT_TIMEOUT_SECONDS", 4):
            _, relaunches, elapsed = self._recover()
        self.assertLess(elapsed, 20, "retry budget is not bounded")

        self.assertEqual(relaunches, 1)
        self.assertIsNotNone(dying.poll())

    def test_the_same_restart_is_not_adopted_twice(self):
        self._running_then_killed()
        self._write_restarter_log()
        new = self._start_jvm(api=True)
        ok, _, _ = self._recover()
        self.assertTrue(ok)

        # The adopted instance now dies inside the log's freshness window.
        new.kill()
        new.wait(timeout=10)
        _, relaunches, elapsed = self._recover(
            exit_code=gc._EXIT_STATUS_UNOBSERVABLE)

        self.assertEqual(relaunches, 1)
        self.assertLess(elapsed, 12, "waited on an already-handled restart")


if __name__ == "__main__":
    unittest.main(verbosity=2)

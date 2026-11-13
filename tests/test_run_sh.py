"""Tests for docker/run.sh's controller supervision (field report 2026-09-30).

In dual mode run.sh starts two controllers. It used to end with a plain
``wait`` on both PIDs, which returns only when BOTH have exited: one mode
dying was masked by the other, Docker's restart policy never fired, and a
live controller stayed down for about nine hours while paper kept the
container healthy-looking.

``wait_for_controllers`` makes the first exit visible: it stops the other
controller cleanly and returns non-zero so the container exits and the
restart policy acts. These tests extract the real function from run.sh and
drive it against stub controllers, so they pin the shipped code rather than
a copy of it.

The choice between ibg-controller and IBC (``choose_login_automation``) and
the healthcheck's reading of the same setting are tested the same way.

The ``wait_for_controllers`` tests need bash >= 5.1 for ``wait -n`` with PID
arguments. CI runs them on
its Ubuntu 24.04 runner (bash 5.2) and again inside each built image, on the
image's own bash (5.3 on the Ubuntu 26.04 base); macOS's /bin/bash is 3.2,
so the tests skip there rather than report a false failure.

scripts/healthcheck.sh is tested here too, for the same reason: it is a
shell script, and the image's sh, curl, sed, tail and tr are the ones that
count. Its tests run the real script with the real curl against local
HTTP servers that answer with the controller's own /health body.
"""
import contextlib
import http.server
import importlib.util
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import textwrap
import threading
import time
import unittest
import unittest.mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN_SH = os.path.join(REPO, "docker", "run.sh")
HEALTHCHECK = os.path.join(REPO, "scripts", "healthcheck.sh")


def _bash_with_wait_n():
    bash = shutil.which("bash")
    if not bash:
        return None
    try:
        out = subprocess.run(
            [bash, "-c", 'echo "${BASH_VERSINFO[0]}.${BASH_VERSINFO[1]}"'],
            capture_output=True, text=True, timeout=5).stdout.strip()
        major, minor = (int(x) for x in out.split("."))
    except Exception:
        return None
    return bash if (major, minor) >= (5, 1) else None


def _extract(name):
    with open(RUN_SH, encoding="utf-8") as f:
        src = f.read()
    m = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", src, re.S | re.M)
    if not m:
        raise AssertionError(f"{name}() not found in docker/run.sh")
    return m.group(0)


BASH = _bash_with_wait_n()

# Mirrors run.sh's shell options and the one collaborator the function calls.
# The stub stop_ibc records that it ran and stops the stub controllers the
# way the real one does (SIGTERM, then wait).
PREAMBLE = textwrap.dedent("""\
    set -Eeo pipefail
    USE_IBG_CONTROLLER=yes
    forwarder=()
    _FORWARDER_RESPAWN_DELAY=0.1
    stop_ibc() {
        echo STOP_IBC
        kill -TERM "${pid[@]}" 2>/dev/null || true
        wait "${pid[@]}" 2>/dev/null || true
    }
    """)


@unittest.skipUnless(BASH, "needs bash >= 5.1 for `wait -n` with PIDs "
                           "(the image ships 5.2; macOS /bin/bash is 3.2)")
class TestWaitForControllers(unittest.TestCase):

    def _run(self, body, timeout=30):
        script = PREAMBLE + _extract("respawn_forwarder") + _extract("wait_for_controllers") + textwrap.dedent(body)
        start = time.monotonic()
        r = subprocess.run([BASH, "-c", script], capture_output=True,
                           text=True, timeout=timeout)
        return r, time.monotonic() - start

    def _rc(self, out):
        m = re.search(r"^RC=(\d+)$", out, re.M)
        self.assertIsNotNone(m, f"no RC line in: {out!r}")
        return int(m.group(1))

    def test_dual_mode_first_exit_stops_the_other_and_fails_the_container(self):
        # The 2026-09-30 incident: live exits, paper would keep running
        # forever. Now paper is stopped and the failure surfaces.
        r, took = self._run("""
            pid=()
            ( sleep 0.3; exit 3 ) & pid+=("$!")
            sleep 30 & pid+=("$!")
            rc=0; wait_for_controllers || rc=$?
            echo "RC=$rc"
            """)
        self.assertIn("STOP_IBC", r.stdout)
        self.assertEqual(self._rc(r.stdout), 3)
        self.assertLess(took, 10, "must not wait for the survivor to finish")

    def test_a_clean_exit_still_fails_the_container(self):
        # restart: on-failure ignores status 0, so a controller that exits
        # cleanly while the other mode runs must still produce non-zero.
        r, _ = self._run("""
            pid=()
            ( sleep 0.2; exit 0 ) & pid+=("$!")
            sleep 30 & pid+=("$!")
            rc=0; wait_for_controllers || rc=$?
            echo "RC=$rc"
            """)
        self.assertIn("STOP_IBC", r.stdout)
        self.assertEqual(self._rc(r.stdout), 1)

    def test_controller_already_dead_before_the_wait(self):
        # run.sh gives up on live's readiness after 300 s and starts paper
        # anyway, so live can be gone before we ever reach the wait.
        r, took = self._run("""
            pid=()
            ( exit 5 ) & pid+=("$!")
            sleep 0.5
            sleep 30 & pid+=("$!")
            rc=0; wait_for_controllers || rc=$?
            echo "RC=$rc"
            """)
        self.assertIn("STOP_IBC", r.stdout)
        self.assertEqual(self._rc(r.stdout), 5)
        self.assertLess(took, 10)

    def test_paper_exiting_leaves_live_alone(self):
        # Live takes priority: a paper failure must not bounce live. Live's
        # own exit code (7) coming back proves stop_ibc never killed it early
        # (a SIGTERM'd sleep would report 143).
        r, took = self._run("""
            pid=()
            sleep 1.0 & pid+=("$!")
            ( sleep 0.2; exit 4 ) & pid+=("$!")
            ( sleep 1.0; exit 7 ) & live_exit=$!
            pid[0]=$live_exit
            rc=0; wait_for_controllers || rc=$?
            echo "RC=$rc"
            """)
        self.assertIn("paper controller exited (status 4)", r.stdout)
        self.assertEqual(self._rc(r.stdout), 7)
        self.assertGreaterEqual(took, 0.9, "must keep waiting on live")
        # stop_ibc runs only once live has gone, never on paper's account
        self.assertEqual(r.stdout.count("STOP_IBC"), 1)
        self.assertLess(r.stdout.index("paper controller exited"),
                        r.stdout.index("STOP_IBC"))

    def test_paper_already_dead_before_the_wait(self):
        r, _ = self._run("""
            pid=()
            ( sleep 0.8; exit 6 ) & pid+=("$!")
            ( exit 2 ) & pid+=("$!")
            sleep 0.3
            rc=0; wait_for_controllers || rc=$?
            echo "RC=$rc"
            """)
        self.assertIn("paper controller exited (status 2)", r.stdout)
        self.assertEqual(self._rc(r.stdout), 6)

    def _fake_socat(self, d, exit_after=None):
        """A stand-in run_socat.sh that records the ports it was started with.
        With exit_after it dies after that many seconds, otherwise it runs."""
        path = os.path.join(d, "run_socat.sh")
        # Detach from the test's stdout first: a background child holding the
        # pipe makes subprocess.run wait for it. In run.sh nothing waits on
        # it, and the container's exit takes it down.
        body = 'exec >/dev/null 2>&1\n'
        body += 'echo "$API_PORT:$SOCAT_PORT:$TRADING_MODE" >> "$MARK"\n'
        body += (f"sleep {exit_after}\n" if exit_after else "sleep 30\n")
        with open(path, "w") as f:
            f.write("#!/bin/bash\n" + body)
        os.chmod(path, 0o755)

    def test_dead_forwarder_is_restarted_with_its_own_ports(self):
        # Field report 2026-09-27: the run_socat.sh wrapper itself died, so
        # nothing restarted socat and 4003 stayed refused for ~29 hours.
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            self._fake_socat(d)
            r, _ = self._run(f"""
                SCRIPT_PATH={d}; export MARK={d}/mark
                pid=()
                ( sleep 1.5; exit 4 ) & pid+=("$!")
                ( sleep 0.2 ) & forwarder[$!]="4001:4003:live"
                rc=0; wait_for_controllers || rc=$?
                echo "RC=$rc"
                cat {d}/mark
                kill "${{!forwarder[@]}}" 2>/dev/null || true
                """)
        self.assertIn("live port forwarder (:4003 -> 127.0.0.1:4001) exited", r.stdout)
        self.assertIn("4001:4003:live", r.stdout)
        self.assertEqual(self._rc(r.stdout), 4, "the controller's own exit still decides")
        self.assertNotIn("STOP_IBC", r.stdout)

    def test_a_restarted_forwarder_stays_supervised(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            self._fake_socat(d, exit_after="0.2")
            r, _ = self._run(f"""
                SCRIPT_PATH={d}; export MARK={d}/mark
                pid=()
                ( sleep 1.5; exit 0 ) & pid+=("$!")
                ( sleep 0.1 ) & forwarder[$!]="4002:4004:paper"
                rc=0; wait_for_controllers || rc=$?
                echo "RC=$rc"
                echo "STARTS=$(wc -l < {d}/mark | tr -d ' ')"
                kill "${{!forwarder[@]}}" 2>/dev/null || true
                """)
        starts = int(re.search(r"STARTS=(\d+)", r.stdout).group(1))
        self.assertGreaterEqual(starts, 2, f"respawn must keep working: {r.stdout}")

    def test_paper_forwarder_dying_never_touches_live(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            self._fake_socat(d)
            r, _ = self._run(f"""
                SCRIPT_PATH={d}; export MARK={d}/mark
                pid=()
                ( sleep 1.5; exit 7 ) & pid+=("$!")
                sleep 30 & pid+=("$!")
                ( sleep 0.2 ) & forwarder[$!]="4002:4004:paper"
                rc=0; wait_for_controllers || rc=$?
                echo "RC=$rc"
                cat {d}/mark
                kill "${{!forwarder[@]}}" 2>/dev/null || true
                """)
        self.assertIn("4002:4004:paper", r.stdout)
        self.assertEqual(self._rc(r.stdout), 7, "live exited on its own schedule")
        self.assertEqual(r.stdout.count("STOP_IBC"), 1)
        self.assertLess(r.stdout.index("port forwarder"), r.stdout.index("STOP_IBC"))

    def test_single_mode_is_a_plain_wait(self):
        r, _ = self._run("""
            pid=()
            ( sleep 0.2; exit 4 ) & pid+=("$!")
            rc=0; wait_for_controllers || rc=$?
            echo "RC=$rc"
            """)
        self.assertNotIn("STOP_IBC", r.stdout)
        self.assertEqual(self._rc(r.stdout), 4)

    def test_ibc_path_keeps_its_old_behaviour(self):
        # Scoped to the controller path: the legacy IBC path is untouched.
        # choose_login_automation leaves the setting as exactly yes or no.
        r, _ = self._run("""
            USE_IBG_CONTROLLER=no
            pid=()
            ( sleep 0.2; exit 0 ) & pid+=("$!")
            ( sleep 0.4; exit 0 ) & pid+=("$!")
            rc=0; wait_for_controllers || rc=$?
            echo "RC=$rc"
            """)
        self.assertNotIn("STOP_IBC", r.stdout)
        self.assertEqual(self._rc(r.stdout), 0)

    def test_exit_during_shutdown_is_not_treated_as_a_failure(self):
        r, _ = self._run("""
            pid=()
            ( sleep 0.2; exit 0 ) & pid+=("$!")
            sleep 0.4 & pid+=("$!")
            _SHUTTING_DOWN=1
            rc=0; wait_for_controllers || rc=$?
            echo "RC=$rc"
            """)
        self.assertNotIn("STOP_IBC", r.stdout)

    def test_sigterm_during_the_wait_stops_everything_exactly_once(self):
        # docker stop: the trap sets _SHUTTING_DOWN and runs stop_ibc. The
        # function must not run stop_ibc a second time on the way out.
        script = PREAMBLE + _extract("respawn_forwarder") + _extract("wait_for_controllers") + textwrap.dedent("""
            trap '_SHUTTING_DOWN=1; stop_ibc' SIGTERM
            pid=()
            sleep 30 & pid+=("$!")
            sleep 30 & pid+=("$!")
            echo READY
            _rc=0; wait_for_controllers || _rc=$?
            echo "RC=$_rc"
            """)
        proc = subprocess.Popen([BASH, "-c", script], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        try:
            self.assertEqual(proc.stdout.readline().strip(), "READY")
            time.sleep(0.3)
            start = time.monotonic()
            proc.send_signal(signal.SIGTERM)
            out, _ = proc.communicate(timeout=20)
        finally:
            if proc.poll() is None:
                proc.kill()
        self.assertEqual(out.count("STOP_IBC"), 1, out)
        self.assertLess(time.monotonic() - start, 10)


ANY_BASH = shutil.which("bash")

# value -> the path run.sh takes: "yes" (ibg-controller), "no" (IBC) or
# None (refused). Includes a value that differs from every default.
SETTINGS = {
    "yes": "yes", "YES": "yes", "Yes": "yes", "true": "yes", "True": "yes",
    "1": "yes", "on": "yes", " yes ": "yes", '"yes"': "yes", "'true'": "yes",
    "no": "no", "No": "no", "false": "no", "FALSE": "no", "0": "no",
    "off": "no", '"no"': "no", " off": "no",
    "enabled": None, "y": None, "2": None, "ibc": None, "yes please": None,
}


@unittest.skipUnless(ANY_BASH, "needs bash")
class TestChooseLoginAutomation(unittest.TestCase):
    """The image runs ibg-controller unless USE_IBG_CONTROLLER says no.
    These run the real functions from run.sh."""

    def _choose(self, env):
        script = (_extract("use_controller_setting") + _extract("choose_login_automation")
                  + 'rc=0; choose_login_automation || rc=$?\n'
                  + 'echo "RC=$rc CHOICE=${USE_IBG_CONTROLLER-<unset>}"\n'
                  + 'bash -c \'echo "CHILD=${USE_IBG_CONTROLLER-<unset>}"\'\n')
        base = {"PATH": os.environ["PATH"]}
        base.update(env)
        r = subprocess.run([ANY_BASH, "-c", script], capture_output=True,
                           text=True, timeout=20, env=base)
        m = re.search(r"^RC=(\d+) CHOICE=(.*)$", r.stdout, re.M)
        self.assertIsNotNone(m, r.stdout + r.stderr)
        child = re.search(r"^CHILD=(.*)$", r.stdout, re.M).group(1)
        return int(m.group(1)), m.group(2), child, r.stdout, r.stderr

    def test_unset_runs_the_controller(self):
        rc, choice, child, out, _ = self._choose({})
        self.assertEqual((rc, choice, child), (0, "yes", "yes"))
        self.assertIn("Login automation: ibg-controller", out)

    def test_empty_runs_the_controller(self):
        rc, choice, _, _, _ = self._choose({"USE_IBG_CONTROLLER": ""})
        self.assertEqual((rc, choice), (0, "yes"))

    def test_every_spelling(self):
        for value, want in SETTINGS.items():
            with self.subTest(value=value):
                rc, choice, child, _, err = self._choose({"USE_IBG_CONTROLLER": value})
                if want is None:
                    self.assertEqual(rc, 78)
                    self.assertIn("is not a value this image understands", err)
                    self.assertIn(value, err)
                else:
                    # Exactly yes or no, and exported: the rest of run.sh
                    # and its children compare against those two words.
                    self.assertEqual((rc, choice, child), (0, want, want))

    def test_no_starts_ibc_and_says_so(self):
        rc, choice, _, out, _ = self._choose({"USE_IBG_CONTROLLER": "no"})
        self.assertEqual((rc, choice), (0, "no"))
        self.assertIn("Login automation: IBC", out)

    def test_unset_with_a_hand_kept_ibc_config_stays_on_ibc(self):
        # CUSTOM_CONFIG=yes: the user maintains IBC's config.ini, and their
        # credentials may be only there.
        rc, choice, _, out, _ = self._choose({"CUSTOM_CONFIG": "yes"})
        self.assertEqual((rc, choice), (0, "no"))
        self.assertIn("CUSTOM_CONFIG=yes: starting IBC", out)

    def test_an_explicit_yes_wins_over_custom_config(self):
        rc, choice, _, out, _ = self._choose({"CUSTOM_CONFIG": "yes",
                                              "USE_IBG_CONTROLLER": "yes"})
        self.assertEqual((rc, choice), (0, "yes"))
        self.assertNotIn("starting IBC", out)

    def test_custom_config_other_than_yes_does_not_change_the_default(self):
        rc, choice, _, _, _ = self._choose({"CUSTOM_CONFIG": "no"})
        self.assertEqual((rc, choice), (0, "yes"))

    def test_run_sh_chooses_before_anything_reads_the_setting(self):
        with open(RUN_SH, encoding="utf-8") as f:
            src = f.read()
        call = src.index('\nchoose_login_automation || exit "$?"\n')
        body_end = src.index("\n}\n", src.index("\nchoose_login_automation() {")) + 3
        uses = [m.start() for m in re.finditer(r'"\$USE_IBG_CONTROLLER" !?= "', src)]
        self.assertGreaterEqual(len(uses), 5)
        self.assertTrue(all(u > call for u in uses if u > body_end))
        # Every later test compares with the two words the choice leaves.
        for m in re.finditer(r'"\$USE_IBG_CONTROLLER" !?= "([^"]*)"', src):
            self.assertIn(m.group(1), ("yes", "no"))


@unittest.skipUnless(shutil.which("sh"), "needs sh")
class TestHealthcheckPath(unittest.TestCase):
    """scripts/healthcheck.sh asks the controller's /health, and on the
    IBC path, where nothing serves it, reports healthy without asking."""

    def _check(self, env, code=200, args=()):
        # A stub curl that records what it was asked and answers the way
        # the script's own curl call prints: the body, then the status.
        with tempfile.TemporaryDirectory() as d:
            calls = os.path.join(d, "calls")
            curl = os.path.join(d, "curl")
            with open(curl, "w") as f:
                f.write(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{calls}"\n'
                        f"printf '%s\\n%s' '{{\"state\": \"LOGIN\"}}' {code}\n")
            os.chmod(curl, 0o755)
            base = {"PATH": d + os.pathsep + os.environ["PATH"]}
            base.update(env)
            r = subprocess.run(["sh", HEALTHCHECK, *args], capture_output=True,
                               text=True, timeout=20, env=base)
            probes = []
            if os.path.exists(calls):
                with open(calls) as f:
                    probes = f.read().splitlines()
        return r.returncode, probes

    def test_controller_path_asks_health_and_reports_its_answer(self):
        rc, probes = self._check({})
        self.assertEqual(rc, 0)
        self.assertEqual(len(probes), 1)
        self.assertIn("http://127.0.0.1:8080/health", probes[0])
        rc, probes = self._check({}, code=503)
        self.assertNotEqual(rc, 0)

    def test_agrees_with_run_sh_on_every_spelling(self):
        for value, want in SETTINGS.items():
            with self.subTest(value=value):
                rc, probes = self._check({"USE_IBG_CONTROLLER": value}, code=503)
                if want == "no":
                    self.assertEqual((rc, probes), (0, []))
                else:
                    # The controller path, and values run.sh refuses (that
                    # container never starts): ask, and report the answer.
                    self.assertEqual(len(probes), 1)
                    self.assertNotEqual(rc, 0)

    def test_ibc_path_asks_nothing_with_two_modes_either(self):
        for extra in ({}, {"HEALTHCHECK_PAPER": "yes"}, {"DUAL_MODE": "yes"}):
            env = {"USE_IBG_CONTROLLER": "no", "TRADING_MODE": "both", **extra}
            with self.subTest(env=env):
                self.assertEqual(self._check(env, code=503), (0, []))
                self.assertEqual(self._check(env, code=503, args=("--explain",)), (0, []))

    def test_unset_with_custom_config_is_the_ibc_path(self):
        rc, probes = self._check({"CUSTOM_CONFIG": "yes"}, code=503)
        self.assertEqual((rc, probes), (0, []))
        rc, probes = self._check({"CUSTOM_CONFIG": "yes", "USE_IBG_CONTROLLER": "yes"},
                                 code=503)
        self.assertEqual(len(probes), 1)
        self.assertNotEqual(rc, 0)


def _health_body(state):
    """The JSON the controller's /health sends, with ``state`` swapped in.

    Built by the controller's own snapshot function so the healthcheck is
    read against the real set of keys, in the real serialisation."""
    global _GC
    if _GC is None:
        spec = importlib.util.spec_from_file_location(
            "gateway_controller_for_healthcheck",
            os.path.join(REPO, "gateway_controller.py"))
        _GC = importlib.util.module_from_spec(spec)
        with unittest.mock.patch.dict(os.environ, {"TRADING_MODE": "paper"}):
            spec.loader.exec_module(_GC)
    snap = _GC._build_health_snapshot()
    snap["state"] = state
    snap["status"] = "healthy" if state == "MONITORING" else "unhealthy"
    return json.dumps(snap).encode("utf-8")


_GC = None


class _Answer(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        return

    def do_GET(self):
        self.server.asked.append(self.path)
        body = _health_body(self.server.state)
        self.send_response(200 if self.server.state == "MONITORING" else 503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@contextlib.contextmanager
def _health_ports(first, second):
    """Two neighbouring ports on 127.0.0.1, as live and paper get.

    Each of ``first`` and ``second`` is a controller state to answer
    with ("MONITORING" answers 200, anything else 503), "wedged" for a
    port that accepts the connection and never answers, or None for a
    port nothing listens on. Yields (first port, [servers])."""
    for _ in range(50):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        opened = []
        try:
            for offset, what in enumerate((first, second)):
                if what is None:
                    # Must really be closed: hold it open to prove it is
                    # free, then release it.
                    s = socket.socket()
                    s.bind(("127.0.0.1", port + offset))
                    s.close()
                    opened.append(None)
                elif what == "wedged":
                    s = socket.socket()
                    s.bind(("127.0.0.1", port + offset))
                    s.listen(8)
                    opened.append(s)
                else:
                    srv = http.server.ThreadingHTTPServer(
                        ("127.0.0.1", port + offset), _Answer)
                    srv.state = what
                    srv.asked = []
                    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02},
                                     daemon=True).start()
                    opened.append(srv)
        except OSError:
            for o in opened:
                _close(o)
            continue
        try:
            yield port, opened
        finally:
            for o in opened:
                _close(o)
        return
    raise AssertionError("no two neighbouring free ports found")


def _close(o):
    if isinstance(o, http.server.HTTPServer):
        o.shutdown()
        o.server_close()
    elif o is not None:
        o.close()


@unittest.skipUnless(shutil.which("sh") and shutil.which("curl"), "needs sh and curl")
class TestHealthcheckModes(unittest.TestCase):
    """With two modes in one container the Docker health status follows
    live. Paper is asked and reported, and counts only when asked to
    (HEALTHCHECK_PAPER=yes) or where it always did (DUAL_MODE=yes in the
    container's own environment)."""

    def _check(self, first, second, env, args=()):
        with _health_ports(first, second) as (port, servers):
            full = {"PATH": os.environ["PATH"],
                    "CONTROLLER_HEALTH_SERVER_PORT": str(port)}
            full.update(env)
            start = time.monotonic()
            r = subprocess.run(["sh", HEALTHCHECK, *args], capture_output=True,
                               text=True, timeout=30, env=full)
            took = time.monotonic() - start
            asked = [len(s.asked) if isinstance(s, http.server.HTTPServer) else None
                     for s in servers]
        return r.returncode, r.stdout, asked, took, port

    BOTH = {"TRADING_MODE": "both"}

    def test_both_modes_ready_is_healthy_and_names_each(self):
        rc, out, asked, _, _ = self._check("MONITORING", "MONITORING", self.BOTH)
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.splitlines(), ["live: healthy (MONITORING)",
                                            "paper: healthy (MONITORING)"])
        self.assertEqual(asked, [1, 1])

    def test_paper_down_is_reported_but_does_not_turn_the_container_unhealthy(self):
        # The case this default exists for: paper halted, live trading.
        # Unhealthy here would have restart tooling log live out.
        for state in ("HALTED", "LOGIN", "UPSTREAM_DOWN"):
            with self.subTest(state=state):
                rc, out, asked, _, _ = self._check("MONITORING", state, self.BOTH)
                self.assertEqual(rc, 0, out)
                lines = out.splitlines()
                self.assertEqual(lines[0], "live: healthy (MONITORING)")
                self.assertEqual(lines[1], f"paper: not healthy (state {state}, HTTP 503)")
                self.assertIn("not counted", lines[2])
                self.assertIn("HEALTHCHECK_PAPER=yes", lines[2])
                self.assertEqual(asked, [1, 1])

    def test_paper_not_listening_is_reported_but_not_counted(self):
        rc, out, _, _, port = self._check("MONITORING", None, self.BOTH)
        self.assertEqual(rc, 0, out)
        self.assertIn(f"paper: no answer from /health on port {port + 1}", out.splitlines())

    def test_live_down_is_unhealthy_whatever_paper_says(self):
        for paper in ("MONITORING", "HALTED", None):
            for extra in ({}, {"HEALTHCHECK_PAPER": "yes"}, {"HEALTHCHECK_PAPER": "no"}):
                with self.subTest(paper=paper, extra=extra):
                    rc, out, _, _, _ = self._check("HALTED", paper, {**self.BOTH, **extra})
                    self.assertEqual(rc, 1, out)
                    lines = out.splitlines()
                    self.assertEqual(lines[0], "live: not healthy (state HALTED, HTTP 503)")
                    self.assertTrue(lines[1].startswith("paper: "), out)

    def test_live_not_listening_is_unhealthy(self):
        rc, out, _, _, port = self._check(None, "MONITORING", self.BOTH)
        self.assertEqual(rc, 1, out)
        self.assertEqual(out.splitlines()[0], f"live: no answer from /health on port {port}")

    def test_healthcheck_paper_yes_makes_paper_count(self):
        for value in ("yes", "YES", "true", "1", "on", ' "yes" ', "'Yes'"):
            with self.subTest(value=value):
                env = {**self.BOTH, "HEALTHCHECK_PAPER": value}
                rc, out, _, _, _ = self._check("MONITORING", "HALTED", env)
                self.assertEqual(rc, 1, out)
                self.assertNotIn("not counted", out)
                rc, out, _, _, _ = self._check("MONITORING", None, env)
                self.assertEqual(rc, 1, out)
                rc, out, _, _, _ = self._check("MONITORING", "MONITORING", env)
                self.assertEqual(rc, 0, out)

    def test_healthcheck_paper_no_spellings_leave_the_status_to_live(self):
        for value in ("no", "NO", "false", "0", "off", ""):
            with self.subTest(value=value):
                env = {**self.BOTH, "HEALTHCHECK_PAPER": value}
                rc, out, _, _, _ = self._check("MONITORING", "HALTED", env)
                self.assertEqual(rc, 0, out)
                self.assertIn("not counted", out)

    def test_a_value_it_cannot_read_never_counts_paper_and_says_so(self):
        # A typo must not be able to make the container unhealthy, and
        # must not pass silently either.
        env = {**self.BOTH, "HEALTHCHECK_PAPER": "ture"}
        rc, out, _, _, _ = self._check("MONITORING", "HALTED", env)
        self.assertEqual(rc, 0, out)
        self.assertIn("HEALTHCHECK_PAPER='ture' is not yes or no", out)
        rc, out, _, _, _ = self._check("MONITORING", "MONITORING", env)
        self.assertEqual(rc, 0, out)

    def test_dual_mode_in_the_container_environment_counts_paper_as_before(self):
        # Before HEALTHCHECK_PAPER existed, DUAL_MODE=yes passed to the
        # container was the one way paper was counted. It still is.
        for env in ({"DUAL_MODE": "yes"}, {"DUAL_MODE": "yes", "TRADING_MODE": "both"}):
            with self.subTest(env=env):
                rc, out, _, _, _ = self._check("MONITORING", "HALTED", env)
                self.assertEqual(rc, 1, out)
                rc, out, _, _, _ = self._check("MONITORING", "HALTED",
                                               {**env, "HEALTHCHECK_PAPER": "no"})
                self.assertEqual(rc, 0, out)

    def test_single_mode_asks_one_port_and_reports_it(self):
        for env, label in (({"TRADING_MODE": "paper"}, "paper"),
                           ({"TRADING_MODE": "live"}, "live"),
                           ({}, "gateway"),
                           ({"TRADING_MODE": "live", "HEALTHCHECK_PAPER": "yes"}, "live")):
            with self.subTest(env=env):
                rc, out, asked, _, _ = self._check("MONITORING", "HALTED", env)
                self.assertEqual(rc, 0, out)
                self.assertEqual(out.splitlines(), [f"{label}: healthy (MONITORING)"])
                self.assertEqual(asked, [1, 0])
                rc, out, asked, _, _ = self._check("LOGIN", "MONITORING", env)
                self.assertEqual(rc, 1, out)
                self.assertEqual(out.splitlines(),
                                 [f"{label}: not healthy (state LOGIN, HTTP 503)"])
                self.assertEqual(asked, [1, 0])

    def test_a_wedged_paper_server_cannot_run_into_dockers_timeout(self):
        # Docker gives the check 5 s and counts a timeout as a failure:
        # an uncounted paper probe must not be able to cause one.
        rc, out, _, took, _ = self._check("MONITORING", "wedged", self.BOTH)
        self.assertEqual(rc, 0, out)
        self.assertIn("paper: no answer", out)
        self.assertLess(took, 3.0)

    def test_both_wedged_still_finishes_inside_dockers_timeout(self):
        rc, out, _, took, _ = self._check("wedged", "wedged",
                                          {**self.BOTH, "HEALTHCHECK_PAPER": "yes"})
        self.assertEqual(rc, 1, out)
        self.assertEqual(len(out.splitlines()), 2, out)
        self.assertLess(took, 4.8)

    def test_explain_says_what_the_status_covers_and_asks_nothing(self):
        cases = (
            ({"TRADING_MODE": "paper"}, None),
            ({"TRADING_MODE": "live", "HEALTHCHECK_PAPER": "yes"}, None),
            (self.BOTH, "follows live"),
            ({**self.BOTH, "HEALTHCHECK_PAPER": "no"}, "follows live"),
            ({**self.BOTH, "HEALTHCHECK_PAPER": "yes"}, "live or paper is not ready"),
            ({"DUAL_MODE": "yes"}, "live or paper is not ready"),
        )
        for env, want in cases:
            with self.subTest(env=env):
                rc, out, asked, _, _ = self._check("HALTED", "HALTED", env, ("--explain",))
                self.assertEqual(rc, 0, out)
                self.assertEqual(asked, [0, 0])
                if want is None:
                    self.assertEqual(out, "")
                else:
                    self.assertEqual(len(out.splitlines()), 1, out)
                    self.assertIn(want, out)
        rc, out, asked, _, _ = self._check(
            "HALTED", "HALTED", {**self.BOTH, "HEALTHCHECK_PAPER": "ture"}, ("--explain",))
        self.assertEqual((rc, asked), (0, [0, 0]))
        self.assertIn("WARNING: HEALTHCHECK_PAPER='ture' is not yes or no", out)
        self.assertIn("follows live", out)

    def test_run_sh_explains_before_it_rewrites_the_mode_variables(self):
        # The healthcheck runs in the container's own environment, where
        # TRADING_MODE is still "both" and DUAL_MODE is whatever the
        # operator passed. run.sh must ask for the explanation while its
        # own copies still say the same.
        with open(RUN_SH, encoding="utf-8") as f:
            src = f.read()
        explain = src.index('"${SCRIPT_PATH}/healthcheck.sh" --explain || true')
        rewrite = src.index("\tDUAL_MODE=yes\n\texport DUAL_MODE\n")
        self.assertLess(explain, rewrite)
        self.assertEqual(src.count("healthcheck.sh\" --explain"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)

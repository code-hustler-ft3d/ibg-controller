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

They need bash >= 5.1 for ``wait -n`` with PID arguments. The release image
ships bash 5.2 and CI runs on Ubuntu 24.04 (5.2); macOS's /bin/bash is 3.2,
so the tests skip there rather than report a false failure.
"""
import os
import re
import shutil
import signal
import subprocess
import textwrap
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN_SH = os.path.join(REPO, "docker", "run.sh")


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
        script = PREAMBLE + _extract("wait_for_controllers") + textwrap.dedent(body)
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
        r, _ = self._run("""
            USE_IBG_CONTROLLER=
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
        script = PREAMBLE + _extract("wait_for_controllers") + textwrap.dedent("""
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
